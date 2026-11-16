#!/usr/bin/env python3
"""自诊断工具：在内网部署机上机械执行，输出可整段复制的极简结果块。

用法:
    python doctor.py [--config path/to/config.yaml]

输出为 [PASS]/[FAIL]/[WARN] 行 + 末尾 SUMMARY 块。把 SUMMARY 块及后续原文
发给开发方即可，无需阅读内容本身。
"""
import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

RESULTS = []


def check(name, ok, detail="", level_fail="FAIL"):
    tag = "PASS" if ok else level_fail
    line = f"[{tag}] {name}"
    if detail:
        line += f" -- {detail}"
        if not ok:
            line += "（常见处理见 AGENT-OPS.md 排障表）"
    print(line, flush=True)
    RESULTS.append((tag, name))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    print("=== welink-agent doctor ===")
    print(f"平台: {platform.system()} {platform.release()}  python: {sys.version.split()[0]}")

    # 1) 依赖
    try:
        import yaml  # noqa: F401
        check("依赖 PyYAML", True)
    except ImportError:
        if not check("依赖 PyYAML", False, "pip install pyyaml（内网源见 AGENT-OPS 步骤2）"):
            return report()

    # 2) 配置
    from wabridge.config import load_config
    try:
        cfg = load_config({"_config_path": args.config} if args.config else {})
        check("配置解析", True)
    except Exception as e:  # noqa: BLE001
        check("配置解析", False, str(e))
        return report()

    if "REPLACE" in cfg.group_id or not cfg.group_id:
        check("群 ID 已填", False, f"group_id={cfg.group_id!r}，编辑 config.yaml")
    else:
        check("群 ID 已填", True, cfg.group_id)
    if not cfg.self_account or "REPLACE" in cfg.self_account:
        check("本人账号已填", False, "需『姓氏首字母+工号』格式如 z00123456")
    else:
        check("本人账号已填", True, cfg.self_account)

    # 3) welink-cli 可达 + 登录态
    wbin = cfg.welink_bin
    if shutil.which(wbin) or Path(wbin).is_file():
        rc, out, err = -1, "", ""
        try:
            p = subprocess.run([wbin, "auth", "status"], capture_output=True,
                               text=True, timeout=30, encoding="utf-8")
            rc, out, err = p.returncode, p.stdout, p.stderr
        except Exception as e:  # noqa: BLE001
            err = str(e)
        if rc == 0 and "logged" in (out + err).lower():
            check("welink-cli 登录态", True, (out.strip()[:80] or "ok"))
        elif rc == 0:
            check("welink-cli 登录态", True, out.strip()[:80])
        else:
            check("welink-cli 登录态", False, f"rc={rc} {err.strip()[:120]} 需 welink-cli auth login")
        # 群可达：拉 1 条历史
        try:
            p = subprocess.run([wbin, "im", "query-history-message", "--group-id",
                                cfg.group_id, "--query-count", "1"],
                               capture_output=True, text=True, timeout=30, encoding="utf-8")
            if p.returncode == 0:
                data = json.loads(p.stdout)
                n = len(data.get("respData", {}).get("chatInfo", []) or [])
                check("群可读", True, f"样例消息数={n}")
            else:
                check("群可读", False, f"rc={p.returncode} {p.stderr[:120]}")
        except Exception as e:  # noqa: BLE001
            check("群可读", False, str(e)[:160])
    else:
        check("welink-cli 可执行", False, f"PATH/绝对路径均找不到 {wbin}")

    # 4) codeagent 可达 + 关键参数存在 + stream-json 冒烟
    cbin = cfg.codeagent_bin
    if shutil.which(cbin) or Path(cbin).is_file():
        check("codeagent 可执行", True, cbin)
        try:
            p = subprocess.run([cbin, "--help"], capture_output=True, text=True,
                               timeout=30, encoding="utf-8", errors="replace")
            help_txt = (p.stdout or "") + (p.stderr or "")
            for flag in ("--input-format", "--output-format", "--append-system-prompt-file",
                         "--dangerously-skip-permissions"):
                check(f"codeagent 支持 {flag}", flag in help_txt,
                      "help 输出未见该参数" if flag not in help_txt else "")
            # 冒烟：resident 方式起进程，喂一条 ping，等 result 事件
            ok_smoke, det = False, ""
            try:
                proc = subprocess.Popen(
                    [cbin, "-p", "--input-format", "stream-json",
                     "--output-format", "stream-json", "--verbose",
                     "--effort", "low", "--dangerously-skip-permissions"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", bufsize=1)
                line = json.dumps({"type": "user",
                                   "message": {"role": "user", "content": "只回复：pong"}}) + "\n"
                proc.stdin.write(line)
                proc.stdin.flush()
                import time as _t
                deadline = _t.time() + 90
                while _t.time() < deadline:
                    s = proc.stdout.readline()
                    if not s:
                        det = "stdout EOF（进程退出）"
                        break
                    try:
                        evt = json.loads(s)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("type") == "result":
                        ok_smoke = True
                        break
                proc.stdin.close()
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    proc.kill()
            except Exception as e:  # noqa: BLE001
                det = str(e)
            check("codeagent stream-json 冒烟", ok_smoke, det or "90s 内收到 result 事件")
        except Exception as e:  # noqa: BLE001
            check("codeagent --help", False, str(e)[:160])
    else:
        check("codeagent 可执行", False, f"PATH/绝对路径均找不到 {cbin}")

    # 5) 任务模板
    from wabridge.templates import load_templates, TemplateError
    try:
        tmpls = load_templates(cfg.tasks_dir)
        if not tmpls:
            check("任务模板目录", False, f"{cfg.tasks_dir} 无 *.yaml（示例见 bridge/tasks/）")
        else:
            check("任务模板加载", True,
                  ", ".join(f"{k}({'只读' if t.readonly else '独占' if t.exclusive else '普通'})"
                            for k, t in sorted(tmpls.items())))
            bad = [t for t in tmpls.values()
                   if t.heartbeat is not None and float(t.heartbeat) >= float(t.timeout or 0)
                   and t.timeout]
            if bad:
                check("心跳<超时", False, f"模板心跳不小于超时: {[t.name for t in bad]}")
    except TemplateError as e:
        check("任务模板加载", False, str(e))

    # 6) 可写目录
    for what, p in (("workdir 可写", cfg.workdir), ("task_logs", cfg.workdir / "task_logs")):
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".doctor_probe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            check(what, True, str(p))
        except Exception as e:  # noqa: BLE001
            check(what, False, str(e)[:120])

    return report()


def report():
    fails = [n for tag, n in RESULTS if tag == "FAIL"]
    warns = [n for tag, n in RESULTS if tag == "WARN"]
    print("\n=== SUMMARY ===")
    print(f"PASS={sum(1 for t, _ in RESULTS if t == 'PASS')} FAIL={len(fails)} WARN={len(warns)}")
    if fails:
        print("待处理: " + "; ".join(fails))
        print("请整段复制本输出发给开发方，不要自行修改配置以外的东西。")
        return 1
    print("结论: 部署环境就绪，可启动 bridge：python bridge.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
