"""异步确认门状态机。

路由规则（Round4 Q2 已确认）：
  - 回复含显式身份（#N 或 开头 N）→ 路由到对应门；
  - 含确认/取消关键词或 key=value 修正：
      名字 token 命中唯一挂起门 → 路由；否则单门时路由，多门时发 ❓ 追问（带防抖），消费不闲聊；
  - 其他文本 → 放行给常驻 agent。
  - 无任何可执行动作的空口 #N → 视作确认。
"""
import re
import threading
import time

KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\s]+)")


class Gate:
    def __init__(self, task, params, sources, sent_id, expires):
        self.task = task
        self.params = params
        self.sources = sources
        self.sent_id = sent_id
        self.expires = expires


class GateManager:
    def __init__(self, cfg, welink, registry, logger, on_confirm, on_cancel,
                 render_steps_fn, param_valid_fn, render_desc_fn=None):
        self.cfg = cfg
        self.welink = welink
        self.registry = registry
        self.log = logger
        self.on_confirm = on_confirm          # (task, params, sources)
        self.on_cancel = on_cancel            # (task, reason, note)
        self.render_steps = render_steps_fn   # (template_name, params) -> list[str]
        self.render_desc = render_desc_fn     # (template_name, params) -> list[str]|None
        self.validate_param = param_valid_fn  # (template_name, k, v) -> err|None
        self.lock = threading.Lock()
        self.pending = {}                     # tid -> Gate
        self._last_ambig = {}

    # ---------- 开关门 ----------
    def open(self, task, params, sources, note="", resend=False, desc_lines=None):
        tmpl = task.name
        try:
            steps = self.render_steps(tmpl, params)
        except KeyError as e:
            self.on_cancel(task, "error", f"缺少参数: {e.args}")
            return False
        lines = [f"即将执行 {task.label()}" + ("（更新确认）" if resend else "")]
        if desc_lines:
            lines.append("任务意图:")
            for dline in desc_lines:
                lines.append(f"  · {dline}")
        if params:
            lines.append("参数:")
            for k in sorted(params):
                src = sources.get(k, "default")
                src_zh = {"default": "默认", "message": "消息提取", "user": "你修改",
                          "missing": "缺失待补"}.get(src, src)
                lines.append(f"  {k} = {params[k]!r}（{src_zh}）")
        else:
            lines.append("参数: 无")
        lines.append("将逐字执行的命令:")
        for i, s in enumerate(steps, 1):
            lines.append(f"  {i}. {s}")
        if note:
            lines.append(f"提示: {note}")
        lines.append("回复『确认』开始执行；回复『key=value』修改参数；回复『取消』放弃。"
                     + ("多个确认待回复时须带任务号，如『#3 确认』。" if len(self.pending) >= 1 else ""))
        text = "\n".join(lines)
        sent_id = self.welink.send(text, prefix="🔐 [参数确认]")
        with self.lock:
            if sent_id is None:
                gate_id = int(time.time() * 1000)
            else:
                gate_id = sent_id
            self.pending[task.tid] = Gate(task, params, sources, gate_id,
                                          time.time() + self.cfg.gate_timeout)
        self.registry.set_status(task, "awaiting_confirm")
        return True

    def close(self, tid):
        with self.lock:
            return self.pending.pop(tid, None)

    # ---------- 路由 ----------
    def route(self, text: str) -> bool:
        """尝试把一条用户消息路由给挂起的门。True=已消费。"""
        with self.lock:
            pendings = dict(self.pending)
        if not pendings:
            return False

        kvs = dict(KV_RE.findall(text))
        has_kw = self._has_confirm(text) or self._has_cancel(text)
        explicit = self._explicit_ids(text, pendings)

        if explicit:
            for tid in explicit:
                self._act(pendings[tid], text, kvs)
            return True

        if not (kvs or has_kw):
            return False

        # 名字 token 命中
        toks = set(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text.lower()))
        by_name = [g for tid, g in pendings.items() if g.task.name in toks]
        if len(by_name) == 1:
            self._act(by_name[0], text, kvs)
            return True
        if len(pendings) == 1:
            self._act(next(iter(pendings.values())), text, kvs)
            return True
        self._ask_disambig(pendings)
        return True

    def _explicit_ids(self, text, pendings):
        ids = {int(m) for m in re.findall(r"#(\d+)", text)}
        m = re.match(r"^\s*(\d+)\b", text)
        if m:
            ids.add(int(m.group(1)))
        return [tid for tid in sorted(ids) if tid in pendings]

    def _has_confirm(self, text):
        low = text.lower()
        return any(k in low for k in self.cfg.get("confirm", "keywords", default=[]))

    def _has_cancel(self, text):
        low = text.lower()
        return any(k in low for k in self.cfg.get("confirm", "cancel_keywords", default=[]))

    def _act(self, gate, text, kvs):
        tid = gate.task.tid
        if kvs:
            self._modify(gate, kvs)
            return
        if self._has_cancel(text):
            self.close(tid)
            self.on_cancel(gate.task, "cancelled", "")
            return
        self.close(tid)
        self.on_confirm(gate.task, gate.params, gate.sources)

    def _modify(self, gate, kvs):
        task = gate.task
        new_params, new_sources = dict(gate.params), dict(gate.sources)
        errs = []
        for k, v in kvs.items():
            err = self.validate_param(task.name, k, v)
            if err:
                errs.append(err)
                continue
            new_params[k] = v
            new_sources[k] = "user"
        self.close(task.tid)
        note = "；".join(errs[:3]) if errs else ""
        desc = self.render_desc(task.name, new_params) if self.render_desc else None
        if new_params != gate.params:
            self.open(task, new_params, new_sources, note=note, resend=True,
                      desc_lines=desc)
        else:
            note2 = (note or "未识别到有效参数修改")
            self.open(task, new_params, new_sources, note=note2 +
                      "。回复『确认』可直接开始，『取消』放弃。", resend=True,
                      desc_lines=desc)

    def _ask_disambig(self, pendings):
        now = time.time()
        key = "gate"
        if now - self._last_ambig.get(key, 0) < 20:
            return
        self._last_ambig[key] = now
        items = "，".join(f"#{g.task.tid} {g.task.short}" for g in pendings.values())
        self.welink.send(f"有 {len(pendings)} 个任务的确认待回复：{items}。"
                         "请用『#编号 确认』『#编号 取消』或『#编号 key=value』指明对象。",
                         prefix="❓ [提问]")

    # ---------- 超时 ----------
    def sweep(self):
        now = time.time()
        with self.lock:
            expired = [g for g in self.pending.values() if g.expires <= now]
            for g in expired:
                self.pending.pop(g.task.tid, None)
        for g in expired:
            self.log.info(f"确认门超时 {g.task.label()}")
            self.on_cancel(g.task, "expired", "")
