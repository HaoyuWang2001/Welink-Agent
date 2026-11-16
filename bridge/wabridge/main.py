"""Bridge 主编排：轮询收集 → 消息分类路由（确认门/指令/批次闲聊）→ 任务生命周期管理。"""
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import agentio, gates, procs, registry as reg
from .scheduler import Scheduler
from .state import ProcRegistry, StateStore
from .templates import load_templates, TemplateError
from .welink import WelinkClient, PREFIX_CONTROL, PREFIX_ASK

SPAWN_RE = re.compile(r"\[\[SPAWN_TASK:\s*([A-Za-z0-9_-]+)\s*(.*?)\]\]", re.DOTALL)
KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\s]+)")

DENY_PATTERNS = [
    r"发消息给(他|她|别人|其他人)",
    r"发邮件给(他|她|别人|其他人)",
    r"上传文件给(他|她|别人|其他人)",
    r"代我发",
    r"帮(他|她|别人|其他人)(发|执行|运行|操作)",
]


def default_logger():
    log = logging.getLogger("bridge")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


class Choice:
    """一次性指令澄清（如多个任务在跑时裸『停止』）。"""
    def __init__(self, kind, candidates, expires, prompt_msg_id):
        self.kind = kind
        self.candidates = candidates     # {tid: Task}
        self.expires = expires
        self.prompt_msg_id = prompt_msg_id


class Bridge:
    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self.log = logger or default_logger()
        procs.set_logger(self.log)
        cfg.workdir.mkdir(parents=True, exist_ok=True)
        (cfg.workdir / "task_logs").mkdir(exist_ok=True)
        fh = logging.FileHandler(cfg.log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self.log.addHandler(fh)

        self.state = StateStore(cfg.state_file, self.log)
        self.proc_registry = ProcRegistry(cfg.registry_file, self.log, procs.kill_tree)
        self.welink = WelinkClient(cfg, self.log)
        self.templates = load_templates(cfg.tasks_dir)
        self.registry = reg.TaskRegistry(self.state, self.log)

        self.gates = gates.GateManager(cfg, self.welink, self.registry, self.log,
                                       on_confirm=self._on_confirm,
                                       on_cancel=self._on_gate_abort,
                                       render_steps_fn=self._render_steps,
                                       param_valid_fn=self._validate_param,
                                       render_desc_fn=self._render_desc)
        self.scheduler = Scheduler(cfg, self.log, self.state, self.registry,
                                   run_fn=self._run_task, on_result=self._on_result)
        self.choice = None
        self.choice_lock = threading.Lock()

        self.resident = None
        self.resident_lock = threading.Lock()
        self.resident_restarting = False
        self.chat_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat")
        self._stop = False

    # ---------- 启动/停止 ----------
    def bootstrap(self, skip_history=False, init_watermark=None):
        if init_watermark:
            self.state.advance_watermark(init_watermark)
            self.log.info(f"watermark 初始化 = {init_watermark} (--init-watermark)")
        if skip_history:
            msgs = self.welink.query_new(0)
            if msgs:
                self.state.advance_watermark(int(msgs[-1]["msgId"]))
                self.log.info(f"watermark 初始化 = {self.state['watermark']}（--skip-history 跳过 {len(msgs)} 条）")
        self.proc_registry.kill_stale()
        self.scheduler.start(resume_tasks=self._load_resumable())
        self._start_resident()
        self.log.info(f"Bridge 启动 group={self.cfg.group_id} self={self.cfg.self_account} "
                      f"templates={sorted(self.templates)} watermark={self.state['watermark']} "
                      f"concurrent={self.cfg.max_concurrent} poll={self.cfg.poll_interval}s")

    def _load_resumable(self):
        out = []
        for d in self.state.get("queue", []) or []:
            try:
                t = reg.Task.from_dict(d)
            except Exception:  # noqa: BLE001
                continue
            if t.name not in self.templates:
                self.log.warning(f"恢复队列跳过：模板 {t.name} 已不存在 #{t.tid}")
                self.registry.set_status(t, reg.ST_REJECTED)
                continue
            t.status = reg.ST_QUEUED
            out.append(t)
            self.registry.tasks[t.tid] = t
        return out

    def shutdown(self):
        self._stop = True
        self.scheduler.stop()
        for t in self.registry.running():
            t.stop_event.set()
            if t.proc_pid:
                procs.kill_tree(t.proc_pid)
        with self.resident_lock:
            if self.resident:
                self.resident.stop()
        self.chat_executor.shutdown(wait=False, cancel_futures=True)
        self.log.info("Bridge 已停止；在跑任务被终止，队列已持久化可续跑")

    # ---------- 控制面 ----------
    def _control(self, text):
        if self.cfg.control_plane == "off":
            return
        self.welink.send(text, prefix=PREFIX_CONTROL)

    # ---------- 常驻 agent ----------
    def _resident_prompt_text(self):
        base = ""
        p = self.cfg.resident_prompt
        if Path(p).exists():
            base = Path(p).read_text(encoding="utf-8")
        else:
            self.log.warning(f"resident prompt 缺失: {p}")
        cat = "\n".join(t.catalog_line() for t in self.templates.values()) or "（暂无模板）"
        return (base + "\n\n## 可用任务模板（实时清单，仅可执行这些）\n" + cat + "\n")

    def _start_resident(self):
        ra = agentio.ResidentAgent(self.cfg, self.log, self._on_resident_dead,
                                   self._resident_prompt_text())
        ra.registry = self.proc_registry
        ra.start()
        self.resident = ra

    def _ensure_resident(self):
        with self.resident_lock:
            if self.resident is None or not self.resident.alive:
                self._start_resident()

    def _on_resident_dead(self, reason):
        self.log.error(f"常驻 agent 死亡: {reason}")
        if self._stop:
            return
        self._control(f"🔄 主 agent 进程退出（{reason}），正在重启")
        with self.resident_lock:
            if self.resident_restarting:
                return
            self.resident_restarting = True
        try:
            time.sleep(1)
            with self.resident_lock:
                self._start_resident()
        except Exception as e:  # noqa: BLE001
            self.log.exception(f"常驻重启失败: {e}")
            self._control(f"⚠️ 主 agent 重启失败: {e}，稍后自动重试")
        finally:
            with self.resident_lock:
                self.resident_restarting = False

    # ---------- 主循环 ----------
    def run(self, **boot_kw):
        self.bootstrap(**boot_kw)
        while not self._stop:
            try:
                self._poll_once()
            except Exception as e:  # noqa: BLE001
                self.log.exception(f"轮询异常: {e}")
            self.gates.sweep()
            self._sweep_choice()
            time.sleep(self.cfg.poll_interval)

    def _poll_once(self):
        msgs = self.welink.query_new(self.state["watermark"])
        batch = []
        for m in msgs:
            mid = int(m.get("msgId", 0))
            self._classify(m, batch)
            self.state.advance_watermark(mid)
        if batch:
            self.chat_executor.submit(self._dispatch_chat, batch)

    # ---------- 分类路由 ----------
    def _classify(self, msg, batch):
        content = (msg.get("content") or "").strip()
        sender = msg.get("sender", "")
        if not content:
            return "empty"
        if self.welink.is_own(content):
            return "own"
        if sender != self.cfg.self_account:
            self.log.info(f"跳过非本人消息 sender={sender}")
            return "other-user"
        if self._route_choice(content):
            return "choice"
        if self.gates.route(content):
            return "gate"
        low = content.lower()
        a = self.cfg.get("agent")
        if a["stop_all_keyword"] in low:
            self._stop_all()
            return "cmd"
        if a["clear_queue_keyword"] in low:
            self._clear_queue()
            return "cmd"
        if a["status_keyword"] in low:
            self._send_status()
            return "cmd"
        if any(k in low for k in a["stop_keywords"]):
            self._handle_stop(content)
            return "cmd"
        for pat in DENY_PATTERNS:
            if re.search(pat, content):
                self.welink.send(f"⚠️ 检测到代操作请求，已拒绝。我是 {self.cfg.self_account} 的助手，无法代为操作他人。")
                return "deny"
        batch.append(content)
        return "chat"

    # ---------- 指令 ----------
    def _handle_stop(self, content):
        running = self.registry.running()
        targets = [t for t in self.registry.find_by_text(content, statuses=(reg.ST_RUNNING,))]
        if targets:
            for t in targets:
                self._kill_task(t)
            return
        queued = [t for t in self.registry.active((reg.ST_QUEUED,))
                  if self._mentioned(content, t)]
        if queued and not running:
            for t in queued:
                if self.scheduler.remove(t):
                    self.registry.set_status(t, reg.ST_CANCELLED)
                    self.welink.send(f"🛑 {t.label()} 已从队列移除，未执行。")
            return
        if not running:
            # 没有可停对象 → 放行给常驻当闲聊
            self.chat_executor.submit(self._dispatch_chat, [content])
            # 注意：调用方 _classify 已把该消息标记 cmd，这里手动兜底转发
            return
        if len(running) == 1:
            self._kill_task(running[0])
            return
        self._open_choice("stop", running,
                          "当前有多个任务在跑：{list}。回复『#编号』停掉对应任务，回复『取消』不动，"
                          "或『停止全部』全停。")

    @staticmethod
    def _mentioned(content, t):
        return (f"#{t.tid}" in content or re.search(rf"^\s*{t.tid}\b", content)
                or t.name in content.lower())

    def _kill_task(self, t):
        t.stop_event.set()
        if t.proc_pid:
            ok = procs.kill_tree(t.proc_pid)
            self.log.info(f"停止 {t.label()} pid={t.proc_pid} kill_tree={ok}")

    def _stop_all(self):
        killed = [t for t in self.registry.running()]
        for t in killed:
            self._kill_task(t)
        removed = self.scheduler.clear_queue()
        for t in removed:
            self.registry.set_status(t, reg.ST_CANCELLED)
        with self.choice_lock:
            self.choice = None
        self.welink.send(f"🛑 已停止全部：{len(killed)} 个在跑（进程树已杀），"
                         f"{len(removed)} 个排队项已移除。")

    def _clear_queue(self):
        removed = self.scheduler.clear_queue()
        for t in removed:
            self.registry.set_status(t, reg.ST_CANCELLED)
        lst = "，".join(t.label() for t in removed) or "（空）"
        self.welink.send(f"🧹 队列已清空：{lst}")

    def _send_status(self):
        snap = self.scheduler.snapshot()
        running = self.registry.running()
        pend = list(self.gates.pending.values())
        lines = [f"在跑 {len(running)} | 排队 {len(snap['queued'])} | 待确认 {len(pend)}"
                 f" | 并发上限 {self.cfg.max_concurrent}"]
        for t in running:
            lines.append(f"  {t.ident()} {t.short} 已跑 {t.elapsed():.0f}s"
                         f"（心跳 {t.heartbeat:.0f}s）")
        for tid, short in snap["queued"]:
            lines.append(f"  #{tid} {short} 排队中")
        for g in pend:
            lines.append(f"  {g.task.ident()} {g.task.short} 等确认 "
                         f"{max(0, g.expires - time.time()):.0f}s")
        self.welink.send("\n".join(lines))

    # ---------- 澄清选择 ----------
    def _open_choice(self, kind, tasks, template_msg):
        lst = "，".join(f"#{t.tid} {t.short}" for t in tasks)
        msg = template_msg.replace("{list}", lst)
        sent_id = self.welink.send(msg, prefix=PREFIX_ASK)
        with self.choice_lock:
            self.choice = Choice(kind, {t.tid: t for t in tasks},
                                 time.time() + self.cfg.choice_timeout, sent_id)

    def _sweep_choice(self):
        with self.choice_lock:
            if self.choice and self.choice.expires <= time.time():
                self.log.info(f"澄清超时: {self.choice.kind}")
                self.choice = None

    def _route_choice(self, content) -> bool:
        with self.choice_lock:
            ch = self.choice
        if not ch:
            return False
        low = content.lower()
        if any(k in low for k in self.cfg.get("confirm", "cancel_keywords", default=[])):
            with self.choice_lock:
                self.choice = None
            self.welink.send("好，不动任何任务。")
            return True
        hits = [t for t in ch.candidates.values() if self._mentioned(content, t)]
        if len(hits) == 1 and ch.kind == "stop":
            with self.choice_lock:
                if self.choice is ch:
                    self.choice = None
            self._kill_task(hits[0])
            return True
        if len(hits) > 1:
            self.welink.send("请只带一个任务号。", prefix=PREFIX_ASK)
            return True
        return False

    # ---------- 闲聊/路由批量 ----------
    def _dispatch_chat(self, messages):
        try:
            self._ensure_resident()
        except Exception as e:  # noqa: BLE001
            self.log.error(f"常驻启动失败: {e}")
            self.welink.send(f"⚠️ 主 agent 启动失败: {e}")
            return
        content = messages[0] if len(messages) == 1 else \
            "\n".join(f"【消息{i+1}】{m}" for i, m in enumerate(messages))
        try:
            reply = self.resident.send(content)
        except Exception as e:  # noqa: BLE001
            self.log.error(f"resident.send 异常: {e}")
            self._on_resident_dead(f"send_exception: {e}")
            self.welink.send("⚠️ 主 agent 异常，正在重启，这条消息请稍后重发")
            return
        if reply.startswith("⏱️"):
            self.welink.send(reply)
            return
        spawns = [{"skill": mm.group(1), "args": mm.group(2).strip()}
                  for mm in SPAWN_RE.finditer(reply)]
        rest = SPAWN_RE.sub("", reply).strip()
        if rest:
            self.welink.send(rest)
        for sc in spawns:
            self._spawn_task(sc)

    def _spawn_task(self, sc):
        name = sc["skill"]
        tmpl = self.templates.get(name)
        if tmpl is None:
            self.welink.send(f"❌ 任务模板『{name}』不存在，无法执行。"
                             f"可用模板: {', '.join(sorted(self.templates)) or '无'}")
            return
        params, sources, warns = {}, {}, []
        for k, v in KV_RE.findall(sc["args"] or ""):
            if k not in tmpl.params:
                warns.append(f"忽略未知参数 {k}={v}")
                continue
            err = tmpl.validate_param(k, v)
            if err:
                warns.append(err)
                continue
            params[k], sources[k] = v, "message"
        for k, spec in tmpl.params.items():
            if k not in params:
                if spec["default"] is not None:
                    params[k], sources[k] = spec["default"], "default"
                elif spec["required"]:
                    params[k], sources[k] = "", "missing"   # 开缺参门，等用户补
                    warns.append(f"必填参数 {k} 缺失，请在确认门中补上")
                else:
                    params[k], sources[k] = "", "default"
        timeout = float(tmpl.timeout) if tmpl.timeout else self.cfg.default_task_timeout
        hb = self.cfg.heartbeat_interval if tmpl.heartbeat is None else float(tmpl.heartbeat)
        task = self.registry.new_task(
            name, params, sources, exclusive=tmpl.exclusive, readonly=tmpl.readonly,
            timeout=timeout, heartbeat=hb, cwd=tmpl.cwd, title=tmpl.title)
        note = "；".join(warns[:3])
        desc = self._render_desc(name, params)
        if task.readonly:
            if note:
                self.welink.send(f"⚠️ {task.label()} 只读任务参数不完整：{note}，未执行。")
                self.registry.set_status(task, reg.ST_REJECTED)
                return
            self.welink.send(f"🚀 {task.label()} 只读任务免确认，直接排队。")
            self._on_confirm(task, params, sources, quiet=True)
        else:
            self.gates.open(task, params, sources, note=note, desc_lines=desc)

    # ---------- 门回调 ----------
    def _render_steps(self, name, params):
        tmpl = self.templates[name]
        return tmpl.render_steps(params)

    def _render_desc(self, name, params):
        tmpl = self.templates.get(name)
        if tmpl is None or not tmpl.confirm_steps:
            return None
        try:
            return [s.format(**params) for s in tmpl.confirm_steps]
        except KeyError:
            return tmpl.confirm_steps

    def _validate_param(self, name, k, v):
        tmpl = self.templates.get(name)
        if tmpl is None:
            return f"模板 {name} 不存在"
        return tmpl.validate_param(k, v)

    def _on_confirm(self, task, params, sources, quiet=False):
        tmpl = self.templates[task.name]
        missing = [k for k, spec in tmpl.params.items()
                   if spec["required"] and not str(params.get(k, "")).strip()]
        if missing:
            if not quiet:
                self.gates.open(task, params, sources,
                                note=f"必填参数缺失: {'、'.join(missing)}，请回复『{' '.join(missing)}=值』补上",
                                resend=True)
            else:
                self.welink.send(f"⚠️ {task.label()} 必填参数缺失: {missing}，未执行。")
                self.registry.set_status(task, reg.ST_REJECTED)
            return
        task.params, task.sources = params, sources
        if not self.scheduler.enqueue(task):
            self.registry.set_status(task, reg.ST_REJECTED)
            self.welink.send(f"⚠️ {task.label()} 排队被拒：队列已满"
                             f"（上限 {self.cfg.queue_capacity}）。稍后重试。")
            return
        if not quiet:
            self.welink.send(f"✅ {task.label()} 参数已确认，进入排队（在跑 "
                             f"{len(self.registry.running())}，排队 "
                             f"{len(self.scheduler.snapshot()['queued'])}）。")

    def _on_gate_abort(self, task, reason, note=""):
        icon = {"cancelled": "🚫", "expired": "⏱️", "error": "❌"}[reason]
        verb = {"cancelled": "已取消，未执行", "expired": "确认超时，未执行",
                "error": f"无法执行：{note}"}[reason]
        self.registry.set_status(task, {"cancelled": reg.ST_CANCELLED,
                                        "expired": reg.ST_EXPIRED,
                                        "error": reg.ST_REJECTED}[reason])
        self.welink.send(f"{icon} {task.label()} {verb}")

    # ---------- 任务执行 ----------
    def _run_task(self, task):
        tmpl = self.templates[task.name]
        task.log_path = str(self.cfg.workdir / "task_logs" /
                           f"task-{task.tid}-{task.name}.log")
        self.registry.set_status(task, reg.ST_RUNNING)
        self.log.info(f"任务开始 {task.label()} params={task.params}")
        runner = agentio.TaskRunner(self.cfg, self.log, self.proc_registry, task, tmpl,
                                    on_heartbeat=self._heartbeat)
        return runner.run()

    def _heartbeat(self, task, elapsed):
        left = max(0.0, task.timeout - elapsed)
        self.welink.send(f"⏳ {task.label()} 运行中：已 {elapsed:.0f}s / 预算 {task.timeout:.0f}s"
                         f"（剩余 {left:.0f}s）")

    def _on_result(self, task, kind, text):
        dur = task.elapsed()
        status_map = {"done": reg.ST_DONE, "timeout": reg.ST_TIMEOUT,
                      "killed": reg.ST_KILLED, "error": reg.ST_FAILED,
                      "empty": reg.ST_FAILED}
        status = status_map.get(kind, reg.ST_FAILED)
        task.report_text = text[:2000]
        self.registry.set_status(task, status)
        head = {"done": f"✅ {task.label()} 完成（耗时 {dur:.0f}s）",
                "timeout": f"⏱️ {task.label()} 超时（{task.timeout:.0f}s），进程树已终止。详见 "
                           f"{task.log_path}",
                "killed": f"🛑 {task.label()} 已被你停止。详见 {task.log_path}",
                "error": f"❌ {task.label()} 异常（耗时 {dur:.0f}s）: {text}",
                "empty": f"⚠️ {task.label()} 结束但无输出（耗时 {dur:.0f}s）"}[kind]
        self.welink.send(f"{head}\n{text}" if kind == "done" else head)
        self.log.info(f"任务结束 {task.label()} status={status} len={len(text)}")
        # 通知常驻 agent（只作背景，不转述）
        def _notify():
            try:
                self._ensure_resident()
                self.resident.send(
                    f"【子任务完成通知】{task.label()} 状态={status}，"
                    f"耗时 {dur:.0f}s。摘要：{text[:600]}")
            except Exception as e:  # noqa: BLE001
                self.log.warning(f"resident 通知失败: {e}")
        self.chat_executor.submit(_notify)
