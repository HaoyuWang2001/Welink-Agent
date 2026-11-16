#!/usr/bin/env python3
"""codeagent CLI 模拟器：stream-json 协议 + 场景脚本行为，供端到端测试。

识别模式：argv 含 --append-system-prompt-file → 常驻主 agent；否则 → 任务子 agent。

场景文件（env CODEAGENT_SIM_SCENARIO）:
{
  "resident": {"responses": [{"text","sleep","parts","part_sleep"}...],
               "default": "...", "crash_after_turns": N},
  "tasks": {"<tid>" | "name:<模板名>" | "default":
             {"mode":"ok|hang|anomaly|empty", "parts":[...], "final":"...",
              "pre_sleep":s, "part_sleep":s, "children":N, "child_sleep":s,
              "child_log":path}}
}
回合游标跨进程持久化于 CODEAGENT_SIM_STATE。hang 模式会派生真实 sleep 子进程，
pid 记入 CODEAGENT_SIM_CHILD，用于断言 kill_tree 真的清了进程树。
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import simlib


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def read_user_line(f):
    line = f.readline()
    if not line:
        return None
    try:
        return json.loads(line).get("message", {}).get("content", "")
    except json.JSONDecodeError:
        return None


def run_resident():
    scen = simlib.load_scenario().get("resident", {})
    emit({"type": "system", "subtype": "init", "session_id": f"sim-res-{os.getpid()}"})
    responses = scen.get("responses", [])
    while True:
        content = read_user_line(sys.stdin)
        if content is None:
            return
        resp = None
        for r in responses:
            if isinstance(r, str):
                r = {"text": r}
            if r.get("contains", "") in content:
                resp = r
                break
        if resp is None:
            resp = {"text": scen.get("default", "好的")}
        if resp.get("text") == "__CRASH__":
            sys.exit(3)          # 模拟处理该消息时进程崩溃
        if resp.get("sleep"):
            time.sleep(float(resp["sleep"]))
        text = resp.get("text", "")
        parts = resp.get("parts") or ([text] if text else [])
        for p in parts:
            emit({"type": "assistant", "message": {"role": "assistant",
                  "content": [{"type": "text", "text": p}]}})
            if resp.get("part_sleep"):
                time.sleep(float(resp["part_sleep"]))
        emit({"type": "result", "subtype": "success", "result": text})


def run_task():
    scen = simlib.load_scenario()
    tasks = scen.get("tasks", {})
    content = read_user_line(sys.stdin)
    if content is None:
        return
    emit({"type": "system", "subtype": "init", "session_id": f"sim-task-{os.getpid()}"})
    m_tid = re.search(r"#(\d+)", content)
    m_name = re.search(r"「([^」]+)」", content)
    beh = (tasks.get(m_tid.group(1)) if m_tid else None) \
        or (tasks.get("name:" + m_name.group(1)) if m_name else None) \
        or tasks.get("default") \
        or {"mode": "ok", "final": "模拟完成：一切正常。"}
    mode = beh.get("mode", "ok")
    child_log = beh.get("child_log", os.environ.get("CODEAGENT_SIM_CHILD"))

    children = []
    for _ in range(int(beh.get("children", 0))):
        p = subprocess.Popen(["sleep", str(beh.get("child_sleep", 300))],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(p)
        if child_log:
            with open(child_log, "a", encoding="utf-8") as f:
                f.write(f"{p.pid}\n")

    def emit_text(t):
        emit({"type": "assistant", "message": {"role": "assistant",
              "content": [{"type": "text", "text": t}]}})

    if beh.get("pre_sleep"):
        time.sleep(float(beh["pre_sleep"]))
    for t in beh.get("parts", []):
        emit_text(t)
        if beh.get("part_sleep"):
            time.sleep(float(beh["part_sleep"]))

    if mode == "hang":
        while True:
            time.sleep(1)                     # 等 bridge 超时/用户停止 kill
    if mode == "anomaly":
        for c in children:                    # 模拟执行 agent 自行终止子进程
            c.terminate()
            try:
                c.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
    if mode == "empty":
        emit({"type": "result", "subtype": "success", "result": ""})
        return
    final = beh.get("final", "")
    if final:
        emit_text(final)
    emit({"type": "result", "subtype": "success", "result": final})


if __name__ == "__main__":
    if "--help" in sys.argv:
        print("usage: codeagent [-p] [--input-format stream-json] [--output-format "
              "stream-json] [--verbose] [--effort LEVEL] [--append-system-prompt-file FILE] "
              "[--dangerously-skip-permissions]")
        sys.exit(0)
    if "--append-system-prompt-file" in sys.argv:
        run_resident()
    else:
        run_task()
