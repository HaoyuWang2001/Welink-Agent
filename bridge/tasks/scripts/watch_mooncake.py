#!/usr/bin/env python3
"""ssh-0-mooncake-compile 编译任务监控脚本（bridge 任务模板的执行体）。

流程全部确定化（子 agent 只负责运行本脚本并转述输出）：
  start: 探活 → 预检实例状态(on_running 策略) → [异常态自动 stop 清理]
         → POST start(注入 CLEAN_BUILD) → 轮询启动任务 → 轮询编译实例到终态
         → 取日志 tail → 输出 RESULT 行
  stop:  预检 → 在跑才 POST stop → 轮询 → 确认停止

退出码/最后一行:
  0 RESULT: SUCCESS 编译成功(实例 exit 0)          0 RESULT: STOPPED 已停止/本就没跑
  1 RESULT: FAILED  启动被拒/编译失败(exit≠0)      1 RESULT: ERROR  服务不可达等
  3 RESULT: ABORTED on_running=abort 命中 / 需人工处理的实例状态
  4 RESULT: TIMEOUT 超 --max-wait 仍在跑（未杀服务端）

只依赖标准库；已禁用系统代理（WSL/内网代理会把本机请求拦成 502）。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

RUNNINGISH = ("running", "deploying", "degraded", "starting")
CLEANUP_BEFORE_START = ("abnormal",)          # 官方语义：先 stop 再 start
MANUAL_STATES = ("legacy", "reused", "unknown")

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    print(f"[watch {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api(base, path, payload=None, timeout=30):
    """返回 (status_code, json_or_text)。网络异常抛 RuntimeError。"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if data else "GET")
    try:
        with _opener.open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, _maybe_json(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, _maybe_json(body)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise RuntimeError(f"API {path} 不可达: {e}") from e


def _maybe_json(body):
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body


def get_instance(base, service, node):
    _, data = api(base, "/api/instances")
    items = data.get("instances", []) if isinstance(data, dict) else []
    for it in items:
        if it.get("service") == service and it.get("node") == node:
            return it
    return None


def inst_status(inst):
    if inst is None:
        return "none"
    return inst.get("status") or inst.get("state") or "unknown"


def wait_instance_state(base, service, node, ok_states, deadline, interval,
                        label="等待实例状态转换"):
    """轮询直到状态进入 ok_states 或脱离 RUNNINGISH（进入稳定态）。
    返回最终状态；超时返回 'timeout'。"""
    last = None
    while time.time() < deadline:
        try:
            st = inst_status(get_instance(base, service, node))
        except RuntimeError as e:
            log(f"探活失败重试: {e}")
            time.sleep(interval)
            continue
        if st != last:
            log(f"{label}: {st}")
            last = st
        if st in ok_states or (st not in RUNNINGISH and st != "none"):
            return st
        time.sleep(interval)
    return "timeout"


def _is_new_run(cur, pre):
    """判断当前实例记录是否属于本次新启动（区别于上一次遗留的终态）。"""
    if cur is None:
        return False
    if pre is None:
        return True
    for k in ("pid", "started_at", "run_id"):
        if cur.get(k) != pre.get(k):
            return True
    return False


def wait_compile(base, service, node, pre_inst, deadline, interval):
    """监视本次编译实例直到终态；防旧终态误判：须先观察到运行态，
    或终态记录的 pid/started_at/run_id 与启动前不同。"""
    seen_running = False
    last = None
    while time.time() < deadline:
        try:
            cur = get_instance(base, service, node)
        except RuntimeError as e:
            log(f"探活失败重试: {e}")
            time.sleep(interval)
            continue
        s = inst_status(cur)
        if s != last:
            log(f"编译监视: {s}")
            last = s
        if s in RUNNINGISH:
            seen_running = True
        elif s in ("completed", "abnormal", "stopped"):
            if seen_running or _is_new_run(cur, pre_inst):
                return s, cur
        elif s == "none":
            if seen_running:
                return "none", cur
        elif s in MANUAL_STATES:
            if seen_running or _is_new_run(cur, pre_inst):
                return s, cur
        time.sleep(interval)
    return "timeout", None


def run_task(base, task_id, deadline, interval, service, node):
    """轮询 POST /api/tasks 生成的任务到终态。返回 (task_dict|None)。"""
    last = None
    while time.time() < deadline:
        code, t = api(base, f"/api/tasks/{task_id}")
        if code == 404:
            time.sleep(interval)
            continue
        if not isinstance(t, dict):
            time.sleep(interval)
            continue
        status = t.get("status")
        if status != last:
            log(f"任务 {task_id} 状态: {status}")
            last = status
        if status != "running":
            return t
        time.sleep(interval)
    return None


def fail_with_task_error(t, base, service, node, tail_n, prefix="启动失败"):
    errs = []
    for st in (t or {}).get("subtasks", []) or []:
        if st.get("status") == "failed":
            errs.append(st.get("error") or st.get("detail") or "未知错误")
    detail = "；".join(errs) or "任务详情缺失"
    log(f"{prefix}: {detail}")
    print_tail(base, service, node, tail_n)
    print(f"RESULT: FAILED {prefix}: {detail}")
    return 1


def print_tail(base, service, node, tail_n):
    try:
        code, data = api(base, f"/api/logs/services/{service}/nodes/{node}/content"
                               f"?run_id=latest&tail={tail_n}")
        content = data.get("content", "") if isinstance(data, dict) else str(data)
        lines = [l for l in content.splitlines() if l.strip()][-tail_n:]
        if lines:
            log(f"日志尾部({len(lines)}行):")
            for l in lines:
                print(f"    | {l[:200]}")
    except RuntimeError as e:
        log(f"日志获取失败(忽略): {e}")


def do_start(a):
    base, service, node = a.base, a.service, a.node
    deadline = time.time() + a.max_wait

    try:
        api(base, "/api/health", timeout=10)
    except RuntimeError as e:
        print(f"RESULT: ERROR 服务不可达: {e}")
        return 1

    inst = get_instance(base, service, node)
    st = inst_status(inst)

    if st in RUNNINGISH:
        if a.on_running == "abort":
            who = (f"run_id={inst.get('run_id')} pid={inst.get('pid')} "
                   f"started_at={inst.get('started_at')}")
            log(f"已有实例运行中({st})，按 on_running=abort 放弃: {who}")
            print(f"RESULT: ABORTED 已有编译在运行({st}; {who})，本次未提交。"
                  f"想等它跑完再接编译请改用 on_running=wait 重新发起。")
            return 3
        log("已有实例运行中，按 on_running=wait 排队等待其结束…")
        st = wait_instance_state(base, service, node,
                                 ok_states=("completed", "abnormal", "stopped"),
                                 deadline=deadline, interval=a.interval,
                                 label="排队等待现有实例")
        if st == "timeout":
            print("RESULT: TIMEOUT 排队等待超过最大时长，对方实例仍在运行。")
            return 4

    if st in MANUAL_STATES:
        log(f"实例状态 {st} 需要人工处理，不自动干预")
        print(f"RESULT: ABORTED 实例状态 {st} 需人工处理（见 _state_guidance）。")
        return 3

    if st in CLEANUP_BEFORE_START:
        log(f"实例处于 {st}（异常退出未清理），按官方路径先 stop 清理…")
        rc = _do_stop(a, from_cleanup=True)
        if rc != 0:
            return rc

    # 提交编译（显式携带变量 = 合并更新实例快照，无需删除重建实例）
    pre_inst = get_instance(base, service, node)   # 提交前快照，用于甄别新运行
    payload = {"service": service, "action": "start", "nodes": [node],
               "variables": {"CLEAN_BUILD": a.clean_build}}
    code, task = api(base, "/api/tasks", payload)
    if code != 202 or not isinstance(task, dict) or "id" not in task:
        detail = task if isinstance(task, str) else (task or {}).get("detail", task)
        print(f"RESULT: FAILED 提交被拒(HTTP {code}): {detail}")
        return 1
    task_id = task["id"]
    log(f"已提交 start 任务 {task_id}（CLEAN_BUILD={a.clean_build}）")

    t = run_task(base, task_id, time.time() + a.task_wait, min(a.interval, 5.0),
                 service, node)
    if t is None:
        print(f"RESULT: TIMEOUT 启动任务 {task_id} 在 {a.task_wait}s 内未确认。")
        return 4
    if t.get("status") != "completed":
        return fail_with_task_error(t, base, service, node, a.tail, "启动任务未成功")

    # 编译监视：run 脚本作为实例进程在跑，exit0=编译成功
    log(f"编译进行中（轮询间隔 {a.interval:.0f}s，最长 {a.max_wait:.0f}s）…")
    t0 = time.time()
    s, cur = wait_compile(base, service, node, pre_inst, deadline, a.interval)

    if s == "timeout":
        print(f"RESULT: TIMEOUT 编译超过 {a.max_wait:.0f}s 仍在运行（未杀服务端）。"
              f"如需中止请在群里再发『停止mooncake编译』(action=stop)。")
        return 4
    if s == "completed":
        print_tail(base, service, node, a.tail)
        print(f"RESULT: SUCCESS mooncake 编译成功 run_id={(cur or {}).get('run_id')} "
              f"exit=0 耗时≈{time.time()-t0:.0f}s（不含排队）")
        return 0
    if s == "abnormal":
        code_ = (cur or {}).get("exit_code")
        log(f"编译进程异常退出 exit_code={code_}")
        print_tail(base, service, node, a.tail)
        print(f"RESULT: FAILED 编译失败（实例 exit_code={code_}），详见上方日志尾部")
        return 1
    if s == "stopped":
        print("RESULT: ABORTED 编译实例被外部停止，未出结果。")
        return 3
    log(f"编译监视遇到未预期状态 {s}")
    print(f"RESULT: FAILED 编译监视遇到未预期状态 {s}")
    return 1


def _do_stop(a, from_cleanup=False):
    base, service, node = a.base, a.service, a.node
    try:
        inst = get_instance(base, service, node)
    except RuntimeError as e:
        print(f"RESULT: ERROR 服务不可达: {e}")
        return 1
    st = inst_status(inst)
    if st not in RUNNINGISH and st != "abnormal":
        print(f"RESULT: STOPPED 当前没有运行中的编译（状态 {st}），无需停止。")
        return 0
    code, task = api(base, "/api/tasks",
                     {"service": service, "action": "stop", "nodes": [node]})
    if code != 202 or not isinstance(task, dict) or "id" not in task:
        detail = task if isinstance(task, str) else (task or {}).get("detail", task)
        print(f"RESULT: FAILED 停止提交被拒(HTTP {code}): {detail}")
        return 1
    log(f"已提交 stop 任务 {task['id']}")
    t = run_task(base, task["id"], time.time() + a.task_wait, min(a.interval, 5.0),
                 service, node)
    if t is None:
        print(f"RESULT: TIMEOUT stop 任务 {task['id']} 未确认。")
        return 4
    if t.get("status") != "completed":
        return fail_with_task_error(t, base, service, node, a.tail, "停止失败")
    st2 = wait_instance_state(base, service, node, ok_states=("stopped", "none"),
                              deadline=time.time() + a.task_wait,
                              interval=min(a.interval, 5.0), label="确认停止")
    if from_cleanup and st2 not in ("stopped", "none"):
        print(f"RESULT: FAILED 清理异常实例未完成（状态 {st2}）")
        return 1
    print(f"RESULT: STOPPED 编译实例已停止（状态 {st2}）"
          + ("（为重新编译做的清理）" if from_cleanup else ""))
    return 0


def main():
    ap = argparse.ArgumentParser(description="mooncake 编译提交+轮询监视")
    ap.add_argument("--base", default="http://127.0.0.1:8310")
    ap.add_argument("--service", default="ssh-0-mooncake-compile")
    ap.add_argument("--action", choices=("start", "stop"), default="start")
    ap.add_argument("--node", default="node4-compile")
    ap.add_argument("--clean-build", choices=("true", "false"), default="true",
                    help="注入实例的 CLEAN_BUILD 环境变量")
    ap.add_argument("--on-running", choices=("abort", "wait"), default="abort",
                    help="已有实例运行中策略：abort=放弃并报告 wait=排队等其结束")
    ap.add_argument("--interval", type=float, default=20.0, help="轮询间隔(秒)")
    ap.add_argument("--task-wait", type=float, default=120.0, help="单任务确认超时(秒)")
    ap.add_argument("--max-wait", type=float, default=3300.0,
                    help="编译+排队的总预算(秒)，须小于 bridge 任务 timeout")
    ap.add_argument("--tail", type=int, default=12, help="结果日志尾部行数")
    a = ap.parse_args()
    try:
        if a.action == "stop":
            sys.exit(_do_stop(a))
        sys.exit(do_start(a))
    except RuntimeError as e:
        print(f"RESULT: ERROR {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("RESULT: ABORTED 被中断")
        sys.exit(3)


if __name__ == "__main__":
    main()
