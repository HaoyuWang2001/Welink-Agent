"""任务实例注册：#N 身份、寻址解析、持久化视图。"""
import re
import threading
import time

ST_CREATED = "created"           # 已解析，待开确认门
ST_AWAIT = "awaiting_confirm"    # 确认门挂起
ST_QUEUED = "queued"             # 已确认，排队等调度
ST_RUNNING = "running"
ST_DONE = "done"
ST_FAILED = "failed"
ST_KILLED = "killed"             # 用户停止
ST_TIMEOUT = "timeout"
ST_CANCELLED = "cancelled"       # 用户取消确认
ST_EXPIRED = "expired"           # 确认超时
ST_REJECTED = "rejected"         # 队列满/模板错

TERMINAL = {ST_DONE, ST_FAILED, ST_KILLED, ST_TIMEOUT, ST_CANCELLED, ST_EXPIRED, ST_REJECTED}

ID_HASH_RE = re.compile(r"#(\d+)")
ID_LEAD_RE = re.compile(r"^\s*(\d+)\b")


class Task:
    def __init__(self, tid, name, params, sources, exclusive=False, readonly=False,
                 timeout=900, heartbeat=300, short=None, cwd=None):
        self.tid = tid
        self.name = name
        self.params = dict(params)
        self.sources = dict(sources)      # 参数来源: default/message/user
        self.exclusive = exclusive
        self.readonly = readonly
        self.timeout = timeout
        self.heartbeat = heartbeat
        self.status = ST_CREATED
        self.created_ts = time.time()
        self.started_ts = None
        self.ended_ts = None
        self.short = short or self._make_short()
        self.proc_pid = None
        self.stop_event = threading.Event()
        self.log_path = None
        self.report_text = None
        self.fail_note = None
        self.cwd = cwd

    def _make_short(self):
        """短名 = 模板名 + 最具区分度的参数值。"""
        for k in self.params:
            if self.sources.get(k) and self.sources[k] != "default":
                return f"{self.name} {self.params[k]}"[:40].strip()
        for k, v in self.params.items():
            if v:
                return f"{self.name} {v}"[:40].strip()
        return self.name

    def ident(self): return f"#{self.tid}"
    def label(self): return f"[任务#{self.tid} {self.short}]"
    def elapsed(self):
        start = self.started_ts or self.created_ts
        end = self.ended_ts or time.time()
        return max(0.0, end - start)

    def to_dict(self):
        return {"tid": self.tid, "name": self.name, "short": self.short,
                "params": self.params, "sources": self.sources,
                "exclusive": self.exclusive, "readonly": self.readonly,
                "timeout": self.timeout, "heartbeat": self.heartbeat,
                "status": self.status, "created": self.created_ts,
                "started": self.started_ts, "ended": self.ended_ts,
                "cwd": self.cwd}

    @staticmethod
    def from_dict(d):
        t = Task(d["tid"], d["name"], d.get("params") or {}, d.get("sources") or {},
                 exclusive=d.get("exclusive", False), readonly=d.get("readonly", False),
                 timeout=d.get("timeout", 900), heartbeat=d.get("heartbeat", 300),
                 short=d.get("short"), cwd=d.get("cwd"))
        t.status = d.get("status", ST_QUEUED)
        t.created_ts = d.get("created", time.time())
        return t


class TaskRegistry:
    def __init__(self, state_store, logger):
        self.state = state_store
        self.log = logger
        self.lock = threading.Lock()
        self.counter = int(state_store.get("task_counter", 0))
        self.tasks = {}                      # tid -> Task
        for d in state_store.get("tasks", [])[-30:]:
            try:
                t = Task.from_dict(d)
                if t.status in (ST_AWAIT, ST_RUNNING):   # 崩溃残留 → 中断
                    t.status = ST_FAILED
                    t.fail_note = "bridge 重启，任务中断"
                self.tasks[t.tid] = t
            except Exception as e:  # noqa: BLE001
                state_store.set(tasks=[])
                self.log.warning(f"任务历史载入失败，清空: {e}")
                break

    def new_task(self, name, params, sources, **kw) -> Task:
        with self.lock:
            self.counter += 1
            t = Task(self.counter, name, params, sources, **kw)
            self.tasks[t.tid] = t
            self._persist_locked()
        return t

    def get(self, tid): return self.tasks.get(tid)

    def update(self, task, **fields):
        with self.lock:
            for k, v in fields.items():
                setattr(task, k, v)
            self._persist_locked()

    def set_status(self, task, status, **fields):
        fields.setdefault("status", status)
        if status in (ST_RUNNING,) and task.started_ts is None:
            fields.setdefault("started_ts", time.time())
        if status in TERMINAL:
            fields.setdefault("ended_ts", time.time())
        self.update(task, **fields)

    def active(self, statuses=(ST_RUNNING,)):
        return [t for t in self.tasks.values() if t.status in statuses]

    def running(self):
        return self.active((ST_RUNNING,))

    def find_by_text(self, text, statuses=None):
        """从文本解析 #N / 前导 N / 模板名 token，返回命中的任务集合。"""
        out, seen = [], set()
        for m in ID_HASH_RE.finditer(text):
            t = self.tasks.get(int(m.group(1)))
            if t and (statuses is None or t.status in statuses) and t.tid not in seen:
                out.append(t); seen.add(t.tid)
        m = ID_LEAD_RE.match(text)
        if m:
            t = self.tasks.get(int(m.group(1)))
            if t and (statuses is None or t.status in statuses) and t.tid not in seen:
                out.append(t); seen.add(t.tid)
        toks = re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text.lower())
        for t in self.tasks.values():
            if t.tid in seen or (statuses is not None and t.status not in statuses):
                continue
            if t.name in toks:
                out.append(t); seen.add(t.tid)
        return out

    def has_id_mention(self, text):
        return bool(ID_HASH_RE.search(text) or ID_LEAD_RE.match(text))

    def _persist_locked(self):
        recent = sorted(self.tasks.values(), key=lambda t: t.tid)[-30:]
        self.state.set(task_counter=self.counter, tasks=[t.to_dict() for t in recent])
