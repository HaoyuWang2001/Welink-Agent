"""任务调度器：并发上限 + exclusive 独占 + 队列容量 + 持久化恢复。

启动判定：
  - exclusive 任务：必须无任何其他任务在跑；
  - 普通任务：无 exclusive 在跑 且 普通并发 < max_concurrent。
队列只收「已确认」任务；容量超限由调用方拒收。
"""
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from . import registry as reg


class Scheduler:
    def __init__(self, cfg, logger, state, registry, run_fn, on_result, on_reject=None):
        self.cfg = cfg
        self.log = logger
        self.state = state
        self.registry = registry
        self.run_fn = run_fn            # (task) -> (kind, text)
        self.on_result = on_result      # (task, kind, text)
        self.executor = ThreadPoolExecutor(max_workers=cfg.max_concurrent + 2,
                                           thread_name_prefix="task")
        self.cond = threading.Condition()
        self.queue = deque()            # 待启动 Task
        self.active_normal = 0
        self.active_exclusive = 0
        self._thread = None
        self._stop = False

    def start(self, resume_tasks=None):
        for t in (resume_tasks or []):
            self.queue.append(t)
        self._persist_queue()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()

    def stop(self):
        self._stop = True
        with self.cond:
            self.cond.notify_all()

    # ---------- 入队 ----------
    def enqueue(self, task):
        with self.cond:
            pending = len(self.queue) + self.active_normal + self.active_exclusive
            if pending >= self.cfg.queue_capacity:
                return False
            self.queue.append(task)
            self.registry.set_status(task, reg.ST_QUEUED)
            self._persist_queue()
            self.cond.notify_all()
            return True

    def clear_queue(self):
        with self.cond:
            items = list(self.queue)
            self.queue.clear()
            self._persist_queue()
        return items

    def remove(self, task):
        """从队列移除指定任务（用户停止排队项）。True=移除成功。"""
        with self.cond:
            if task in self.queue:
                self.queue.remove(task)
                self._persist_queue()
                return True
            return False

    # ---------- 主循环 ----------
    def _can_start(self, task):
        if task.exclusive:
            return self.active_normal == 0 and self.active_exclusive == 0
        return self.active_exclusive == 0 and self.active_normal < self.cfg.max_concurrent

    def _loop(self):
        while not self._stop:
            with self.cond:
                for task in list(self.queue):
                    if self._can_start(task):
                        self.queue.remove(task)
                        self._persist_queue()
                        if task.exclusive:
                            self.active_exclusive += 1
                        else:
                            self.active_normal += 1
                        self.executor.submit(self._worker, task)
                        break
                else:
                    self.cond.wait(timeout=1.0)

    def _worker(self, task):
        kind, text = "error", ""
        try:
            kind, text = self.run_fn(task)
        except Exception as e:  # noqa: BLE001
            self.log.exception(f"任务线程异常 {task.label()}: {e}")
            kind, text = "error", str(e)
        finally:
            with self.cond:
                if task.exclusive:
                    self.active_exclusive -= 1
                else:
                    self.active_normal -= 1
                self.cond.notify_all()
        try:
            self.on_result(task, kind, text)
        except Exception as e:  # noqa: BLE001
            self.log.exception(f"on_result 异常: {e}")

    # ---------- 视图 ----------
    def snapshot(self):
        with self.cond:
            return {"queued": [(t.tid, t.short) for t in self.queue],
                    "active_normal": self.active_normal,
                    "active_exclusive": self.active_exclusive}

    def _persist_queue(self):
        # 调用方须持 self.cond（或启动初期单线程）
        self.state.set(queue=[t.to_dict() for t in self.queue])
