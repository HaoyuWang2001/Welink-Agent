"""watch_mooncake.py 集成测试：真实子进程 × 本地 mock sd(8310) 服务。"""
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

WATCH = str(Path(__file__).resolve().parents[2] / "bridge" / "tasks" / "scripts"
            / "watch_mooncake.py")
SVC = "ssh-0-mooncake-compile"
LOGPATH = f"/api/logs/services/{SVC}/nodes/node4-compile/content"


def inst(status, pid=None, run_id="r-1", started_at="t0", exit_code=None):
    d = {"service": SVC, "node": "node4-compile", "status": status,
         "pid": pid, "run_id": run_id, "started_at": started_at}
    if exit_code is not None:
        d["exit_code"] = exit_code
    return d


class SdHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _hit(self, method, path):
        srv = self.server
        key = (method, path)
        srv.calls.append(key)
        if key not in srv.queues:
            return self._send(404, {"detail": f"no route {key}"})
        q = srv.queues[key]
        status, obj = q.pop(0) if len(q) > 1 else q[0]
        return self._send(status, obj)

    def do_GET(self):
        self._hit("GET", self.path.split("?")[0])

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) or b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"_raw": raw.decode(errors="replace")}
        self.server.posts.append((self.path, body))
        self._hit("POST", self.path)


class MockSD:
    def __init__(self, queues):
        self.queues = {(m, p): list(v) for (m, p), v in queues.items()}
        self.posts = []
        self.calls = []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), SdHandler)
        self.srv.queues = self.queues
        self.srv.posts = self.posts
        self.srv.calls = self.calls
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def run_watch(*extra, base, **kw):
    args = [sys.executable, WATCH, "--base", base, "--interval", "0.05",
            "--task-wait", "3", "--max-wait", "4"] + list(extra)
    p = subprocess.run(args, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout + p.stderr


HEALTH = {("GET", "/api/health"): [(200, {"ok": True})]}
OK = {"ok": True}


def last_result(out):
    for line in reversed(out.splitlines()):
        if line.startswith("RESULT:"):
            return line
    return ""


# ---------------- start 流程 ----------------

def test_clean_start_success():
    m = MockSD({**HEALTH,
                ("GET", "/api/instances"): [(200, {"instances": []}),
                                            (200, {"instances": []}),
                                            (200, {"instances": [inst("running", pid=10)]}),
                                            (200, {"instances": [inst("completed", pid=10)]})],
                ("POST", "/api/tasks"): [(202, {"id": "t1"})],
                ("GET", "/api/tasks/t1"): [(200, {"status": "running"}),
                                           (200, {"status": "completed"})],
                ("GET", LOGPATH): [(200, {"content": "build done\nall ok"})]})
    try:
        rc, out = run_watch(base=m.base)
        assert rc == 0, out
        assert last_result(out).startswith("RESULT: SUCCESS")
        posts = [b for _, b in m.posts]
        assert posts[0]["action"] == "start"
        assert posts[0]["variables"] == {"CLEAN_BUILD": "true"}   # 默认全量
        assert posts[0]["nodes"] == ["node4-compile"]
        assert "RESULT: FAILED" not in out
    finally:
        m.close()


def test_clean_build_false_passed():
    m = MockSD({**HEALTH,
                ("GET", "/api/instances"): [(200, {"instances": []}),
                                            (200, {"instances": []}),
                                            (200, {"instances": [inst("running", pid=2)]}),
                                            (200, {"instances": [inst("completed", pid=2)]})],
                ("POST", "/api/tasks"): [(202, {"id": "t1"})],
                ("GET", "/api/tasks/t1"): [(200, {"status": "completed"})],
                ("GET", LOGPATH): [(200, {"content": "x"})]})
    try:
        rc, out = run_watch("--clean-build", "false", base=m.base)
        assert rc == 0, out
        assert m.posts[0][1]["variables"] == {"CLEAN_BUILD": "false"}
    finally:
        m.close()


def test_submit_rejected_400():
    m = MockSD({**HEALTH,
                ("GET", "/api/instances"): [(200, {"instances": []})],
                ("POST", "/api/tasks"): [(400, {"detail": "未知节点: ['node4-compile']"})]})
    try:
        rc, out = run_watch(base=m.base)
        assert rc == 1
        assert "提交被拒" in out and "未知节点" in out
    finally:
        m.close()


def test_start_task_failed():
    m = MockSD({**HEALTH,
                ("GET", "/api/instances"): [(200, {"instances": []})],
                ("POST", "/api/tasks"): [(202, {"id": "t1"})],
                ("GET", "/api/tasks/t1"): [(200, {"status": "running"}),
                                           (200, {"status": "failed",
                                                  "subtasks": [{"node": "node4-compile",
                                                                "status": "failed",
                                                                "error": "当前状态不允许启动"}]})],
                ("GET", LOGPATH): [(200, {"content": "irrelevant"})]})
    try:
        rc, out = run_watch(base=m.base)
        assert rc == 1
        assert last_result(out).startswith("RESULT: FAILED")
        assert "当前状态不允许启动" in out
    finally:
        m.close()


def test_running_abort_policy():
    m = MockSD({**HEALTH,
                ("GET", "/api/instances"): [(200, {"instances": [inst("running", pid=9)]})]})
    try:
        rc, out = run_watch(base=m.base)
        assert rc == 3
        assert last_result(out).startswith("RESULT: ABORTED")
        assert not m.posts, "abort 策略不得提交任何任务"
    finally:
        m.close()


def test_running_wait_policy_queues_then_starts():
    old = inst("running", pid=9)
    done_old = inst("completed", pid=9)
    new = inst("running", pid=10)
    done_new = inst("completed", pid=10)
    m = MockSD({**HEALTH,
                ("GET", "/api/instances"): [
                    (200, {"instances": [old]}),        # 预检 running
                    (200, {"instances": [old]}),        # 排队等待轮询1
                    (200, {"instances": [done_old]}),   # 排队等待轮询2 → 结束
                    (200, {"instances": [done_old]}),   # pre_inst 快照
                    (200, {"instances": [done_old]}),   # watch 轮1：旧终态不得误判
                    (200, {"instances": [new]}),        # watch 轮2
                    (200, {"instances": [done_new]}),   # watch 轮3
                ],
                ("POST", "/api/tasks"): [(202, {"id": "t1"})],
                ("GET", "/api/tasks/t1"): [(200, {"status": "completed"})],
                ("GET", LOGPATH): [(200, {"content": "ok"})]})
    try:
        rc, out = run_watch("--on-running", "wait", base=m.base)
        assert rc == 0, out
        assert last_result(out).startswith("RESULT: SUCCESS")
        assert m.posts[0][1]["action"] == "start"
        assert "排队等待" in out
    finally:
        m.close()


def test_abnormal_cleanup_then_start():
    abn = inst("abnormal", pid=8, exit_code=137)
    stopped = inst("stopped", pid=None)
    new = inst("running", pid=11)
    done_new = inst("completed", pid=11)
    m = MockSD({**HEALTH,
                ("GET", "/api/instances"): [
                    (200, {"instances": [abn]}),      # 预检
                    (200, {"instances": [abn]}),      # _do_stop 检查
                    (200, {"instances": [stopped]}),  # stop 确认
                    (200, {"instances": [stopped]}),  # pre_inst
                    (200, {"instances": [stopped]}),  # watch 轮1(旧态不误判)
                    (200, {"instances": [new]}),
                    (200, {"instances": [done_new]}),
                ],
                ("POST", "/api/tasks"): [(202, {"id": "ts"}), (202, {"id": "tt"})],
                ("GET", "/api/tasks/ts"): [(200, {"status": "completed"})],
                ("GET", "/api/tasks/tt"): [(200, {"status": "completed"})],
                ("GET", LOGPATH): [(200, {"content": "ok"})]})
    try:
        rc, out = run_watch(base=m.base)
        assert rc == 0, out
        acts = [b["action"] for _, b in m.posts]
        assert acts == ["stop", "start"]          # 先清理再启动，无需删除重建
        assert last_result(out).startswith("RESULT: SUCCESS")
    finally:
        m.close()


def test_compile_failure_reads_log_tail():
    m = MockSD({**HEALTH,
                ("GET", "/api/instances"): [(200, {"instances": []}),
                                            (200, {"instances": []}),
                                            (200, {"instances": [inst("running", pid=1)]}),
                                            (200, {"instances": [inst("abnormal", pid=1,
                                                                      exit_code=2)]})],
                ("POST", "/api/tasks"): [(202, {"id": "t1"})],
                ("GET", "/api/tasks/t1"): [(200, {"status": "completed"})],
                ("GET", LOGPATH): [(200, {"content": "cc1 error: undefined reference\nmake: *** [all] Error 2"})]})
    try:
        rc, out = run_watch(base=m.base)
        assert rc == 1
        assert last_result(out).startswith("RESULT: FAILED 编译失败")
        assert "undefined reference" in out        # 日志尾部供 agent 摘要
    finally:
        m.close()


def test_compile_timeout_not_killed():
    m = MockSD({**HEALTH,
                ("GET", "/api/instances"): [(200, {"instances": []}),
                                            (200, {"instances": []}),
                                            (200, {"instances": [inst("running", pid=1)]})],
                ("POST", "/api/tasks"): [(202, {"id": "t1"})],
                ("GET", "/api/tasks/t1"): [(200, {"status": "completed"})]})
    try:
        rc, out = run_watch(base=m.base)          # --max-wait 4s
        assert rc == 4
        assert last_result(out).startswith("RESULT: TIMEOUT")
        assert "停止mooncake编译" in out
    finally:
        m.close()


# ---------------- stop 流程 ----------------

def test_stop_when_nothing_running():
    m = MockSD({**HEALTH, ("GET", "/api/instances"): [(200, {"instances": []})]})
    try:
        rc, out = run_watch("--action", "stop", base=m.base)
        assert rc == 0
        assert "无需停止" in out
        assert not m.posts
    finally:
        m.close()


def test_stop_running():
    m = MockSD({**HEALTH,
                ("GET", "/api/instances"): [(200, {"instances": [inst("running", pid=5)]}),
                                            (200, {"instances": [inst("running", pid=5)]}),
                                            (200, {"instances": [inst("stopped", pid=None)]})],
                ("POST", "/api/tasks"): [(202, {"id": "t9"})],
                ("GET", "/api/tasks/t9"): [(200, {"status": "completed"})]})
    try:
        rc, out = run_watch("--action", "stop", base=m.base)
        assert rc == 0
        assert last_result(out).startswith("RESULT: STOPPED")
        assert m.posts[0][1]["action"] == "stop"
        assert m.posts[0][1].get("variables") in (None, {}, {})   # stop 不携带变量
    finally:
        m.close()


def test_service_unreachable():
    rc, out = run_watch(base="http://127.0.0.1:1")
    assert rc == 1
    assert last_result(out).startswith("RESULT: ERROR")
