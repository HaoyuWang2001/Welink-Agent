"""doctor 全链路验证 + 启动语义验证。"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from harness import SIM_DIR, REPO, SimEnv


def test_doctor_green_against_sims(tmp_path):
    """doctor 面向模拟器配置应全绿（等价于内网机验收路径的本地镜像）。"""
    state = tmp_path / "welink_state.json"
    state.write_text(json.dumps({"msgs": [], "next_id": 89000000000000000}),
                     encoding="utf-8")
    scen = tmp_path / "scenario.json"
    scen.write_text(json.dumps({"resident": {"default": "pong"}}), encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "welink": {"group_id": "grp", "self_account": "self1",
                   "cli_bin": str(SIM_DIR / "welink-cli.py")},
        "agent": {"bin": str(SIM_DIR / "codeagent.py")},
        "paths": {"workdir": str(tmp_path / "wd"),
                  "tasks_dir": str(REPO / "bridge" / "tasks"),
                  "resident_prompt": "resident_prompt.md"},
    }, allow_unicode=True), encoding="utf-8")

    env = {"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
           "WELINK_SIM_STATE": str(state), "WELINK_SIM_SELF": "self1",
           "CODEAGENT_SIM_SCENARIO": str(scen),
           "CODEAGENT_SIM_STATE": str(tmp_path / "cur.json"),
           "PYTHONPATH": str(REPO / "bridge")}
    p = subprocess.run([sys.executable, str(REPO / "bridge" / "doctor.py"),
                        "--config", str(cfg)],
                       capture_output=True, text=True, timeout=120, env=env)
    out = p.stdout
    assert "=== welink-agent doctor ===" in out
    assert f"FAIL={0}" in out, out
    assert "部署环境就绪" in out, out
    assert "[PASS] 任务模板加载" in out and "demo-build" in out


def test_skip_history_on_start(tmp_path):
    """--skip-history：启动前已在群里的旧消息不触发处理。"""
    env = SimEnv(tmp_path, scenario={"resident": {"default": "NEW-ONLY"}})
    env.user_say("旧消息不该处理")               # 启动前注入
    time.sleep(0.3)
    env.start(args=["--skip-history"])
    try:
        time.sleep(2.0)
        assert not env.bridge_texts(), "旧消息不应产生回复"
        env.user_say("新消息")
        env.wait_for("NEW-ONLY")
    finally:
        env.stop()


def test_real_compile_mooncake_template_gate(tmp_path):
    """真实模板 compile-mooncake：确认门展示意图描述+title 短名+CLEAN_BUILD，
    修改参数后渲染命令随之更新。"""
    repo_tpl = (REPO / "bridge" / "tasks" / "compile-mooncake.yaml").read_text(
        encoding="utf-8")
    scen = {"resident": {"responses": [
        {"contains": "编译mooncake", "text": "好的。\n[[SPAWN_TASK: compile-mooncake]]"}],
        "default": "好"},
        "tasks": {"name:compile-mooncake": {"mode": "ok",
                                            "final": "RESULT: SUCCESS 编译成功"}}}
    env = SimEnv(tmp_path, scenario=scen,
                 templates={"compile-mooncake.yaml": repo_tpl})
    with env:
        env.user_say("编译mooncake")
        env.wait_for("即将执行")
        gate = "".join(env.bridge_texts())          # gate 长会分段，合并断言
        assert "[任务#1 编译 mooncake start]" in gate      # title+区分参数作短名
        assert "任务意图:" in gate                       # confirm_steps 渲染
        assert "CLEAN_BUILD=true" in gate
        assert "--clean-build true" in gate
        assert "--on-running abort" in gate
        assert "watch_mooncake.py" in gate               # 逐字命令行
        env.user_say("on_running=wait clean_build=false")
        env.wait_for("（更新确认）")
        g2 = "".join(env.bridge_texts())
        assert "--on-running wait" in g2 and "--clean-build false" in g2
        assert "（你修改）" in g2
        env.user_say("确认")
        env.wait_for("RESULT: SUCCESS 编译成功")
