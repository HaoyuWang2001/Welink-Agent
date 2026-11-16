"""持久化：bridge_state.json（watermark/任务计数/队列/近期任务）与 proc_registry.json（PID 防孤儿）。"""
import json
import os
import threading
import time


class StateStore:
    def __init__(self, path, logger):
        self.path = path
        self.log = logger
        self.lock = threading.Lock()
        self.data = {"watermark": 0, "task_counter": 0, "queue": [], "tasks": []}
        if os.path.exists(path):
            try:
                loaded = json.loads(open(path, encoding="utf-8").read())
                if isinstance(loaded, dict):
                    self.data.update({k: loaded[k] for k in
                                      ("watermark", "task_counter", "queue", "tasks") if k in loaded})
            except Exception as e:  # noqa: BLE001
                logger.warning(f"state 文件损坏，重置: {e}")

    def __getitem__(self, k): return self.data[k]
    def get(self, k, d=None): return self.data.get(k, d)

    def set(self, **kv):
        with self.lock:
            self.data.update(kv)
            self._save_locked()

    def mutate(self, fn):
        """fn(data) 在锁内修改，改后落盘。"""
        with self.lock:
            fn(self.data)
            self._save_locked()

    def _save_locked(self):
        tmp = str(self.path) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            self.log.warning(f"state 保存失败: {e}")

    def advance_watermark(self, mid: int):
        with self.lock:
            if mid > self.data["watermark"]:
                self.data["watermark"] = mid
                self._save_locked()
                return True
        return False


class ProcRegistry:
    """记录 bridge 启动的所有 codeagent 子进程 PID；启动时清理上次残留的孤儿。"""

    def __init__(self, path, logger, kill_tree_fn):
        self.path = path
        self.log = logger
        self.kill_tree = kill_tree_fn
        self.lock = threading.Lock()
        self.procs = {}  # pid -> {"type","name","started"}

    def register(self, pid, ptype, name):
        with self.lock:
            self.procs[str(pid)] = {"type": ptype, "name": name, "started": time.time()}
            self._save_locked()

    def unregister(self, pid):
        with self.lock:
            if self.procs.pop(str(pid), None) is not None:
                self._save_locked()

    def kill_stale(self):
        try:
            data = json.loads(open(self.path, encoding="utf-8").read())
        except (FileNotFoundError, json.JSONDecodeError):
            self.log.info("kill_stale: 无残留进程注册表")
            data = {}
        if data:
            self.log.info(f"kill_stale: 清理 {len(data)} 个残留进程")
            for pid_s, info in data.items():
                try:
                    self.kill_tree(int(pid_s))
                    self.log.info(f"kill_stale: 已终止 PID={pid_s} type={info.get('type')} name={info.get('name')}")
                except Exception as e:  # noqa: BLE001
                    self.log.warning(f"kill_stale: PID={pid_s} 清理失败: {e}")
        with self.lock:
            self.procs = {}
            self._save_locked()

    def _save_locked(self):
        tmp = str(self.path) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.procs, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            self.log.warning(f"proc_registry 保存失败: {e}")
