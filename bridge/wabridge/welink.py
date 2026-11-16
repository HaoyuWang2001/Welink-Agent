"""WeLink CLI 封装：发送（分段+429退避）、拉取增量消息、取回复。"""
import json
import subprocess
import time


# Bridge 发出的所有消息都会带以下某个前缀行，用于回读时自我过滤
PREFIX_AGENT = "🤖 [Agent]"
PREFIX_CONTROL = "📡 [控制面]"
PREFIX_ASK = "❓ [提问]"
PREFIX_CONFIRM = "🔐 [参数确认]"
OWN_PREFIXES = (PREFIX_AGENT, PREFIX_CONTROL, PREFIX_ASK, PREFIX_CONFIRM)


def chunk_text(text: str, limit: int):
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit:
            if buf:
                parts.append(buf)
                buf = ""
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
            buf = line
        else:
            buf += line
    if buf:
        parts.append(buf)
    return parts


class WelinkClient:
    def __init__(self, cfg, logger):
        self.cfg = cfg
        self.log = logger
        self.bin = cfg.welink_bin
        self.group_id = cfg.group_id

    def run_cli(self, args, timeout=30):
        cmd = [self.bin] + list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8")
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
        except FileNotFoundError:
            return 127, "", f"{self.bin} 不存在（用 WA_CLI_BIN/config.yaml 指定路径）"

    def send(self, text, prefix=PREFIX_AGENT):
        """发群消息，自动分段；**每段都带前缀行**（保证回读时全部可自我识别，
        续段不会被误当作新用户消息）。返回最后一段的 msgId 或 None。"""
        last_id = None
        chunks = chunk_text(text, self.cfg.chunk_limit)
        for i, chunk in enumerate(chunks):
            payload = chunk
            if prefix and not chunk.startswith(prefix):
                payload = f"{prefix}\n{chunk}"
            escaped = payload.replace("\\", "\\\\").replace("\n", "\\n")
            sent = False
            for attempt in range(3):
                rc, out, err = self.run_cli(
                    ["im", "send-to-group", "--group-id", self.group_id,
                     "--text", escaped], timeout=self.cfg.get("welink", "send_timeout", default=30))
                if rc == 0:
                    sent = True
                    break
                if "429" in str(err) and attempt < 2:
                    backoff = 2 ** (attempt + 1)
                    self.log.warning(f"发送 429 限流，{backoff}s 后重试 ({attempt+1}/3)")
                    time.sleep(backoff)
                    continue
                self.log.error(f"发送失败 rc={rc}: {err}")
                return last_id
            if not sent:
                return last_id
            try:
                ids = json.loads(out).get("respData", {}).get("msgIds", [])
                if ids:
                    last_id = int(ids[-1])
            except Exception:  # noqa: BLE001
                pass
        return last_id

    def query_new(self, watermark: int, count=None):
        """拉取 msgId > watermark 的消息（按 msgId 升序）。"""
        rc, out, err = self.run_cli(
            ["im", "query-history-message", "--group-id", self.group_id,
             "--query-count", str(count or self.cfg.get("welink", "query_count", default=100))],
            timeout=self.cfg.get("welink", "send_timeout", default=30))
        if rc != 0:
            self.log.error(f"拉取消息失败 rc={rc}: {err}")
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            self.log.error(f"消息 JSON 解析失败: {out[:120]}")
            return []
        msgs = data.get("respData", {}).get("chatInfo", []) or []
        new = [m for m in msgs if int(m.get("msgId", 0)) > watermark]
        new.sort(key=lambda m: int(m.get("msgId", 0)))
        return new

    def is_own(self, content: str) -> bool:
        return content.startswith(OWN_PREFIXES)

    def query_reply_blocking(self, after_msg_id, allowed_senders, exclude_prefixes,
                             timeout, poll_interval):
        """同步等待一条满足条件的回复（doctor/脚本用；bridge 主流程用异步路由）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.query_new(after_msg_id, count=20):
                if m.get("sender", "") in allowed_senders:
                    c = (m.get("content") or "").strip()
                    if c and not c.startswith(tuple(exclude_prefixes)):
                        return c
            time.sleep(poll_interval)
        return None
