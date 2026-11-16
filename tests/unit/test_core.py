"""核心模块单元/集成测试（纯逻辑 + 真实进程原语）。"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from wabridge import procs
from wabridge.config import load_config, Config
from wabridge.state import StateStore
from wabridge.templates import load_templates, TemplateError, Template
from wabridge import registry as reg
from wabridge.gates import GateManager
from wabridge.scheduler import Scheduler
from wabridge.agentio import build_task_prompt
from wabridge.welink import chunk_text

REPO = Path(__file__).resolve().parents[2]
TASKS_DIR = REPO / "bridge" / "tasks"


# ---------- chunk_text ----------

def test_chunk_text_limits():
    assert chunk_text("abc", 10) == ["abc"]
    parts = chunk_text("line1\nline2\nline3", 6)
    assert all(len(p) <= 6 for p in parts)
    assert "".join(parts).replace("\n", "") == "line1line2line3"
    long = "x" * 25
    assert chunk_text(long, 10) == ["x" * 10, "x" * 10, "x" * 5]


# ---------- config ----------

def test_config_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("WA_GROUP_ID", "env-group")
    cfg = load_config({})
    assert cfg.group_id == "env-group"
    cfg = load_config({"group_id": "cli-group"})
    assert cfg.group_id == "cli-group"
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("welink:\n  group_id: yaml-group\n  poll_interval: 7.5\n",
                        encoding="utf-8")
    monkeypatch.delenv("WA_GROUP_ID")
    cfg = load_config({"_config_path": str(cfg_file)})
    assert cfg.group_id == "yaml-group" and cfg.poll_interval == 7.5
    cfg = load_config({"_config_path": str(cfg_file), "group_id": "cli"})
    assert cfg.group_id == "cli"


def test_config_paths_absolute_override(tmp_path):
    cfg = load_config({"workdir": str(tmp_path)})
    assert cfg.state_file == tmp_path / "bridge_state.json"
    cfg2 = load_config({})
    assert cfg2.workdir.name == "bridge"


# ---------- templates ----------

def test_repo_templates_load():
    tmpls = load_templates(TASKS_DIR)
    assert set(tmpls) == {"compile-mooncake", "demo-build", "demo-log-check",
                          "demo-api-probe"}
    assert tmpls["demo-build"].exclusive and not tmpls["demo-build"].readonly
    assert tmpls["demo-log-check"].readonly
    assert tmpls["demo-api-probe"].params["url"]["required"]
    cm = tmpls["compile-mooncake"]
    assert cm.title == "编译 mooncake" and cm.exclusive and not cm.readonly
    assert cm.confirm_steps and cm.params["clean_build"]["default"] == "true"
    assert cm.params["on_running"]["default"] == "abort"


def test_template_errors(tmp_path):
    (tmp_path / "bad1.yaml").write_text("name: bad1\nsteps: ['echo {nope}']\n",
                                        encoding="utf-8")
    with pytest.raises(TemplateError, match="未定义参数"):
        load_templates(tmp_path)


def test_template_param_and_render():
    t = Template({"name": "ok1", "params": {"x": {"default": "1", "pattern": "^\\d+$"}},
                  "steps": ["run {x}", "done"], "readonly": True}, source="ok1")
    assert t.validate_param("x", "42") is None
    assert t.validate_param("x", "abc") is not None
    assert t.validate_param("y", "1") is not None
    assert t.render_steps({"x": "9"}) == ["run 9", "done"]
    assert "readonly免确认" in t.catalog_line()


# ---------- state & registry ----------

def test_state_watermark_and_corruption(tmp_path):
    p = tmp_path / "s.json"
    st = StateStore(p, _Silent())
    st.advance_watermark(100)
    st.advance_watermark(50)
    assert st["watermark"] == 100
    p.write_text("{corrupt", encoding="utf-8")
    st2 = StateStore(p, _Silent())
    assert st2["watermark"] == 0


def test_registry_persistence_and_addressing(tmp_path):
    st = StateStore(tmp_path / "s.json", _Silent())
    r = reg.TaskRegistry(st, _Silent())
    t1 = r.new_task("build", {"tag": "1"}, {"tag": "message"})
    assert t1.tid == 1 and t1.short == "build 1"
    r.set_status(t1, reg.ST_QUEUED)
    # 重开（模拟 bridge 重启）
    st2 = StateStore(tmp_path / "s.json", _Silent())
    r2 = reg.TaskRegistry(st2, _Silent())
    assert r2.counter == 1 and 1 in r2.tasks and r2.get(1).status == reg.ST_QUEUED
    t1b = r2.get(1)
    t1b.status = reg.ST_RUNNING
    t2 = r2.new_task("log", {"d": "1"}, {})
    t2.status = reg.ST_RUNNING
    assert [t.tid for t in r2.find_by_text("#1 停止", (reg.ST_RUNNING,))] == [1]
    assert [t.tid for t in r2.find_by_text("停止 build", (reg.ST_RUNNING,))] == [1]
    assert [t.tid for t in r2.find_by_text("3", (reg.ST_RUNNING,))] == []
    assert r2.has_id_mention("跑 #12") and not r2.has_id_mention("没有编号")


class _Silent:
    def info(self, *a): pass
    def warning(self, *a): pass
    def error(self, *a): pass
    def debug(self, *a): pass
    def exception(self, *a): pass
    def setLevel(self, *a): pass
    def addHandler(self, *a): pass
    def handlers(self): return []


# ---------- procs kill_tree 真实进程树 ----------

def test_kill_tree_full_process_tree(tmp_path):
    script = tmp_path / "kid.py"
    script.write_text(
        "import subprocess, time\n"
        "c = subprocess.Popen(['sleep', '300'])\n"
        "open(%r, 'w').write(str(c.pid))\n"
        "time.sleep(300)\n" % str(tmp_path / "child.pid"), encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(script)],
                         **procs.popen_kwargs())
    deadline = time.time() + 10
    while not (tmp_path / "child.pid").exists() and time.time() < deadline:
        time.sleep(0.1)
    child = int((tmp_path / "child.pid").read_text())
    assert procs.kill_tree(p.pid)
    p.wait(timeout=10)
    assert not procs.pid_alive(p.pid, timeout=5)
    t0 = time.time()
    while procs.pid_alive(child) and time.time() - t0 < 10:
        time.sleep(0.1)
    assert not procs.pid_alive(child), "孙进程未被杀"


# ---------- gates 路由状态机（无网络单元级） ----------

class _StubWelink:
    def __init__(self):
        self.sent = []
        self._id = 900

    def send(self, text, prefix="🤖 [Agent]"):
        self._id += 1
        self.sent.append((prefix, text))
        return self._id


def _make_gates(tmp_path):
    cfg = load_config({"workdir": str(tmp_path)})
    st = StateStore(cfg.state_file, _Silent())
    r = reg.TaskRegistry(st, _Silent())
    w = _StubWelink()
    tmpl = Template({"name": "g1",
                     "params": {"x": {"default": "d"}, "y": {"required": True,
                                                             "pattern": "^\\d+$"}},
                     "steps": ["cmd {x} {y}"]}, source="g1")
    tmpls = {"g1": tmpl}
    confirmed, cancelled = [], []
    g = GateManager(cfg, w, r, _Silent(),
                    on_confirm=lambda t, p, s: confirmed.append((t.tid, dict(p))),
                    on_cancel=lambda t, reason, note="": cancelled.append((t.tid, reason)),
                    render_steps_fn=lambda n, p: tmpls[n].render_steps(p),
                    param_valid_fn=lambda n, k, v: tmpls[n].validate_param(k, v))
    task = r.new_task("g1", {"x": "d", "y": ""}, {"x": "default", "y": "missing"})
    task.params = {"x": "d", "y": "5"}
    task.sources = {"x": "default", "y": "user"}
    return g, w, task, confirmed, cancelled, r


def test_gate_route_single_bare(tmp_path):
    g, w, task, confirmed, cancelled, r = _make_gates(tmp_path)
    assert not g.route("随便聊天")
    g.open(task, task.params, task.sources)
    assert any(p == "🔐 [参数确认]" for p, _ in w.sent)
    assert g.route("cmd无关的其他文本") is False
    assert g.route("确认") is True
    assert confirmed and not cancelled and not g.pending


def test_gate_route_multi_and_explicit(tmp_path):
    g, w, t1, confirmed, cancelled, r = _make_gates(tmp_path)
    t2 = r.new_task("g1", {"x": "e", "y": "6"}, {"x": "default", "y": "user"})
    t2.params = dict(t2.params)
    g.open(t1, t1.params, t1.sources)
    g.open(t2, t2.params, t2.sources)
    assert g.route("确认") is True          # 消费但只追问
    assert any(p == "❓ [提问]" for p, _ in w.sent)
    assert len(g.pending) == 2
    assert g.route("#1 确认") is True
    assert list(g.pending) == [t2.tid]
    assert g.route("x=zzz y=7") is True     # 修正重开
    pend = g.pending[t2.tid]
    assert pend.params == {"x": "zzz", "y": "7"}
    assert pend.sources["x"] == "user"


def test_gate_route_cancel_and_kv_invalid(tmp_path):
    g, w, task, confirmed, cancelled, r = _make_gates(tmp_path)
    g.open(task, task.params, task.sources)
    assert g.route("取消") is True
    assert cancelled and not confirmed


def test_gate_sweep_expiry(tmp_path):
    g, w, task, confirmed, cancelled, r = _make_gates(tmp_path)
    g.open(task, task.params, task.sources)
    g.pending[task.tid].expires = time.time() - 1
    g.sweep()
    assert not g.pending
    assert (task.tid, "expired") in cancelled


# ---------- scheduler gating ----------

def _mk_sched(tmp_path, max_c=2, cap=3):
    cfg = load_config({"workdir": str(tmp_path), "max_concurrent": max_c,
                       "queue_warn": cap})
    st = StateStore(cfg.state_file, _Silent())
    r = reg.TaskRegistry(st, _Silent())
    return cfg, r, st


def test_scheduler_concurrency_and_exclusive(tmp_path):
    cfg, r, st = _mk_sched(tmp_path, max_c=2, cap=4)
    events = []
    gate1 = threading.Event()
    gate2 = threading.Event()

    def run_fn(task):
        events.append((task.name, task.tid))
        if task.exclusive:
            gate2.wait(timeout=15)
        else:
            gate1.wait(timeout=15)
        return "done", "ok"

    sched = Scheduler(cfg, _Silent(), st, r, run_fn=run_fn, on_result=lambda *_: None)
    sched.start()
    n1 = r.new_task("n1", {}, {})
    n2 = r.new_task("n2", {}, {})
    n3 = r.new_task("n3", {}, {})
    ex = r.new_task("ex", {}, {}, exclusive=True)
    for t in (n1, n2, n3, ex):
        assert sched.enqueue(t)
    time.sleep(0.8)
    running = {e[0] for e in events}
    assert running == {"n1", "n2"}          # 并发2，n3/ex 排队（ex 须全空闲）
    gate1.set()
    time.sleep(2.5)
    assert {"n1", "n2", "n3"} <= {e[0] for e in events}
    gate2.set()
    sched.stop()


def test_scheduler_reject_over_capacity(tmp_path):
    cfg, r, st = _mk_sched(tmp_path, max_c=1, cap=2)
    hold = threading.Event()
    sched = Scheduler(cfg, _Silent(), st, r,
                      run_fn=lambda t: (hold.wait(timeout=15), ("done", ""))[1],
                      on_result=lambda *_: None)
    sched.start()
    tasks = [r.new_task(f"x{i}", {}, {}) for i in range(5)]
    ok = [sched.enqueue(t) for t in tasks]
    # 容量口径 = 在跑 + 排队，恒定上限 2：前 2 收，后 3 拒（与调度时序无关）
    assert ok == [True, True, False, False, False]
    removed = sched.clear_queue()
    assert len(removed) <= 2
    hold.set()
    sched.stop()


# ---------- task prompt ----------

def test_build_task_prompt_contains_guardrails():
    t = Template({"name": "tt", "params": {"k": {"default": "v"}},
                  "steps": ["do {k}"]}, source="tt")
    task = Task = reg.Task(9, "tt", {"k": "v"}, {"k": "message"})
    p = build_task_prompt(task, t, "/tmp/x.log")
    assert "禁止增删改命令、禁止重试" in p
    assert "do v" in p and "[任务#9" in p and "/tmp/x.log" in p
