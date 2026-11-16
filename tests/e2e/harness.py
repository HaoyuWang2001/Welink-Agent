"""端到端测试环境：真实 bridge 子进程 + 两个 CLI 模拟器 + 群消息状态文件。"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SIM_DIR = REPO / "tests" / "sim"
sys.path.insert(0, str(SIM_DIR))
import simlib  # noqa: E402

BRIDGE_PY = REPO / "bridge" / "bridge.py"

BASE_TEMPLATES = {
    "t-fast.yaml": """name: t-fast
description: 快速任务
params:
  tag: {default: d0, pattern: "^[A-Za-z0-9]+$"}
steps:
  - "echo build TAG={tag}"
report: 一行结论
""",
    "t-req.yaml": """name: t-req
description: 必填参数任务
params:
  url: {required: true, pattern: "^https?://.*"}
steps:
  - "curl -sS {url}"
report: 一行结论
""",
    "t-ro.yaml": """name: t-ro
readonly: true
description: 只读任务
steps:
  - "cat /etc/hostname"
report: 一行结论
""",
    "t-excl.yaml": """name: t-excl
exclusive: true
description: 独占任务
params:
  tag: {default: e0}
steps:
  - "echo excl TAG={tag}"
report: 一行结论
""",
    "t-hang.yaml": """name: t-hang
timeout: 60
description: 长时间任务
params:
  tag: {default: h0}
steps:
  - "sleep 300"
report: 一行结论
""",
    "t-tmo.yaml": """name: t-tmo
timeout: 2
description: 会超时的任务
steps:
  - "sleep 300"
report: 一行结论
""",
}

OWN_MARKS = ("🤖", "🔐", "❓", "📡")


class SimEnv:
    def __init__(self, tmp_path, scenario=None, cfg_over=None, templates=None):
        for sim in ("welink-cli.py", "codeagent.py"):
            os.chmod(SIM_DIR / sim, 0o755)
        self.root = Path(tmp_path)
        self.work = self.work_dir = self.root / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "task_logs").mkdir(exist_ok=True)
        (self.work / "resident_prompt.md").write_text("stub prompt", encoding="utf-8")

        self.state_path = self.root / "welink_state.json"
        simlib.save_state({"msgs": [], "next_id": simlib.BASE_MSG_ID},
                          self._new_state_file())
        self.scen_path = self.root / "scenario.json"
        self.scenario = scenario or {}
        self._write_scenario()
        self.cursor_path = self.root / "cursor.json"
        self.child_path = self.root / "children.txt"

        self.tpl_dir = self.root / "templates"
        self.tpl_dir.mkdir(exist_ok=True)
        for name, text in {**BASE_TEMPLATES, **(templates or {})}.items():
            (self.tpl_dir / name).write_text(text, encoding="utf-8")

        cfg = {
            "welink": {"group_id": "grp-e2e", "self_account": "self",
                       "cli_bin": str(SIM_DIR / "welink-cli.py"),
                       "poll_interval": 0.4, "chunk_limit": 400, "send_timeout": 5,
                       "query_count": 100},
            "agent": {"bin": str(SIM_DIR / "codeagent.py"), "resident_timeout": 8,
                      "task_timeout_default": 6, "max_concurrent": 3,
                      "heartbeat_interval": 1.5, "queue_capacity": 3},
            "confirm": {"timeout": 4, "poll_interval": 0.5, "choice_timeout": 6},
            "paths": {"workdir": str(self.work), "tasks_dir": str(self.tpl_dir),
                      "resident_prompt": "resident_prompt.md"},
            "control_plane": "essential",
        }
        _deep = json.loads(json.dumps(cfg))
        if cfg_over:
            _merge(_deep, cfg_over)
        self.cfg_path = self.root / "config.yaml"
        import yaml
        self.cfg_path.write_text(yaml.safe_dump(_deep, allow_unicode=True), encoding="utf-8")
        self.proc = None
        self.bridge_log = open(self.root / "bridge_stderr.log", "w", encoding="utf-8")

    # ---------- 生命周期 ----------
    def _new_state_file(self):
        return open(self.state_path, "w+", encoding="utf-8")

    def _write_scenario(self):
        self.scen_path.write_text(json.dumps(self.scenario, ensure_ascii=False),
                                  encoding="utf-8")

    def start(self, args=None):
        env = {**os.environ,
               "WELINK_SIM_STATE": str(self.state_path),
               "WELINK_SIM_SELF": "self",
               "CODEAGENT_SIM_SCENARIO": str(self.scen_path),
               "CODEAGENT_SIM_STATE": str(self.cursor_path),
               "CODEAGENT_SIM_CHILD": str(self.child_path)}
        env.update(getattr(self, "extra_env", {}) or {})
        self.proc = subprocess.Popen(
            [sys.executable, str(BRIDGE_PY), "--config", str(self.cfg_path)]
            + list(args or []),
            cwd=str(REPO / "bridge"), stdout=self.bridge_log,
            stderr=subprocess.STDOUT, env=env)

    def stop(self, sig=signal.SIGINT, wait=20):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.stop()

    # ---------- 群消息 ----------
    def user_say(self, text, sender="self"):
        st, f = simlib.load_state(str(self.state_path))
        simlib.append_msg(st, f, sender, text)

    def msgs(self):
        return simlib.read_msgs(str(self.state_path))

    def bridge_texts(self):
        return [m["content"] for m in self.msgs() if m["sender"] == "self"
                and m["content"].startswith(OWN_MARKS)]

    def user_texts(self):
        """用户输入消息（排除 bridge 自身发出——自身发送 sender 也是 self，
        靠 OWN_MARKS/内容前缀区分）。"""
        bt = set(self.bridge_texts())
        return [m["content"] for m in self.msgs() if m["content"] not in bt]

    def wait_for(self, needle, timeout=20, from_idx=0, sender="bridge"):
        """等待某条消息包含 needle。sender: bridge=只看 🤖/🔐/❓/📡 前缀消息（默认）,
        user=只看本人裸消息, other=只看他人消息。"""
        pred = (lambda c: needle in c) if isinstance(needle, str) else needle

        def match(c):
            if sender == "bridge":
                return c.startswith(OWN_MARKS) and pred(c)
            if sender == "user":
                return not c.startswith(OWN_MARKS) and pred(c)
            return pred(c)

        deadline = time.time() + timeout
        while time.time() < deadline:
            texts = []
            for m in self.msgs():
                if sender == "other":
                    if m["sender"] == "self":
                        continue
                elif m["sender"] != "self":
                    continue
                texts.append(m["content"])
            window = texts[from_idx:] if from_idx else texts
            for c in window:
                if match(c):
                    return c
            if self.proc and self.proc.poll() is not None:
                self.dump_failure(f"bridge 进程退出 rc={self.proc.returncode}")
            time.sleep(0.2)
        self.dump_failure(f"超时等待: {needle!r}")

    def count_msgs_after(self, idx):
        return len([m for m in self.msgs()[idx:] if m["sender"] == "self"])

    def snapshot_idx(self):
        return len(self.msgs())

    def dump_failure(self, why):
        msgs = self.msgs()
        log = ""
        try:
            bl = self.work / "bridge.log"
            if bl.exists():
                log = bl.read_text(encoding="utf-8")[-4000:]
        except Exception:  # noqa: BLE001
            pass
        raise AssertionError(
            f"{why}\n--- msgs ---\n" +
            "\n".join(f"[{m['sender']}] {m['content'][:160]}" for m in msgs) +
            "\n--- bridge.log tail ---\n" + log)

    # ---------- 子进程/断言辅助 ----------
    def child_pids(self):
        if not self.child_path.exists():
            return []
        return [int(x) for x in self.child_path.read_text().split() if x.strip()]

    @staticmethod
    def pid_alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def wait_children(self, n=1, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.child_pids()) >= n:
                return self.child_pids()
            time.sleep(0.2)
        raise AssertionError(f"等不到 {n} 个模拟子进程，现有 {self.child_pids()}")

    def wait_all_children_dead(self, pids=None, timeout=20):
        pids = pids if pids is not None else self.child_pids()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if all(not self.pid_alive(p) for p in pids):
                return True
            time.sleep(0.2)
        alive = [p for p in pids if self.pid_alive(p)]
        raise AssertionError(f"子进程仍存活: {alive}")

    def set_scenario(self, scenario):
        self.scenario = scenario
        self._write_scenario()

    def inject_429(self, n):
        self.extra_env = {"WELINK_SIM_429": str(n)}


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v
