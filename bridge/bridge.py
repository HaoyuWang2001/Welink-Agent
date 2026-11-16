#!/usr/bin/env python3
"""
WeLink <-> CodeAgentCLI Bridge（常驻主 agent 架构）

架构：
  - 一个常驻 codeagent stream-json 进程作为"主 agent"，接收所有 WeLink 消息，保持对话上下文连续
  - 主 agent 决定：直接回复（adhoc 问答）还是输出 SPAWN_SKILL 指令启动子 agent 跑长任务
  - bridge.py 角色：WeLink 收发管道 + 常驻进程管理 + 子 agent 并发控制 + 崩溃恢复

特性:
  - 常驻主 agent（stream-json 多轮对话，上下文连续）
  - skill 沉淀：长任务封装为 skill，主 agent 输出 [[SPAWN_SKILL: name args]] 触发子 agent
  - 子 agent 并发控制（--max-concurrent 限制同时运行的 skill 子进程数）
  - 崩溃恢复：常驻进程崩溃自动重启
  - 资源监控（活跃 agent 数、累计完成数、最长运行时长）

用法:
    python bridge.py
    python bridge.py --group-id 1023824099283107996
    python bridge.py --poll-interval 15 --agent-timeout 600 --max-concurrent 3

停止: Ctrl+C  (state 自动落盘)
"""
import argparse
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ---------- 常量 ----------
DEFAULT_GROUP_ID = "1023824099283107996"
DEFAULT_SELF_ACCOUNT = "w00921547"          # 你本人的 WeLink 账号
AGENT_PREFIX = "🤖 [Agent]"                  # agent 输出前缀，也是"非指令"判定标记
POLL_INTERVAL_DEFAULT = 15                   # 秒
AGENT_TIMEOUT_DEFAULT = 900                  # 秒（skill 子 agent 超时，mooncake 编译+构建+传输需 10+ 分钟）
MAX_CONCURRENT_DEFAULT = 1                   # 同时运行的 skill 子进程数上限
MSG_CHUNK_LIMIT = 1800                       # 单条 WeLink 消息最大字符数（保守值）
WELINK_CLI_DEFAULT = "welink-cli.cmd"
CODEAGENT_BIN_DEFAULT = "codeagent.bat"
WELINK_CLI_BIN = WELINK_CLI_DEFAULT      # 启动时由 main() 覆盖
CODEAGENT_BIN = CODEAGENT_BIN_DEFAULT    # 启动时由 main() 覆盖
XIAOLUBAN_ACCOUNT = "p_xiaoluban"            # 群内小鲁班机器人账号
XIAOLUBAN_TRIGGER = "@小鲁班"                # 触发小鲁班的文本前缀（实测有效）
LUBAN_HOOK_PATTERN = re.compile(r"\[\[ASK_LUBAN:\s*(.+?)\]\]", re.DOTALL)
LUBAN_REPLY_TIMEOUT = 60                     # 等待小鲁班回复的最长时间（秒）
LUBAN_REPLY_POLL = 2                         # 轮询小鲁班回复的间隔（秒）
# ASK_USER 钩子：agent 输出 [[ASK_USER: 问题]] 时 Bridge 代发到群并取回用户回复
ASK_USER_HOOK_PATTERN = re.compile(r"\[\[ASK_USER:\s*(.+?)\]\]", re.DOTALL)
ASK_USER_PREFIX = "❓ [提问]"                 # 提问消息前缀（区别于 AGENT_PREFIX / CONTROL_PLANE_PREFIX）
ASK_USER_REPLY_PREFIX = "💬 [你的回答]"       # 注入回 agent 结果的回答回显前缀
ASK_USER_TIMEOUT = 300                       # 等待用户回复的最长时间（秒），比小鲁班长（用户可能不在）
ASK_USER_POLL = 3                            # 轮询用户回复的间隔（秒）
# ---------- 参数确认门 ----------
PARAM_CONFIRM_PREFIX = "🔐 [参数确认]"         # 参数确认消息前缀（区别于其他前缀）
PARAM_CONFIRM_TIMEOUT = 300                  # 等待用户确认参数的最长时间（秒）
PARAM_CONFIRM_POLL = 3                       # 轮询用户确认的间隔（秒）
PARAM_CONFIRM_KEYWORDS = ("确认", "正确", "对", "yes", "ok", "开始", "没问题", "继续", "跑吧", "执行")
PARAM_CANCEL_KEYWORDS = ("取消", "算了", "不要了", "abort", "cancel")
# skill 默认参数表（用于参数确认门展示完整参数列表；按需为具体 skill 填充）
SKILL_DEFAULTS = {}
# ---------- 中途停止 skill ----------
STOP_KEYWORDS = ("停止", "停下", "终止", "取消任务", "stop", "abort", "kill")
# ---------- 常驻主 agent ----------
RESIDENT_TIMEOUT_DEFAULT = 60               # 常驻主 agent 单轮响应超时（秒，60s 无响应即异常）
RESIDENT_PROMPT_FILE_DEFAULT = Path(__file__).resolve().parent / "resident_prompt.md"
SPAWN_SKILL_PATTERN = re.compile(
    r"\[\[SPAWN_SKILL:\s*(\w[\w-]*)\s*(.*?)\]\]", re.DOTALL)

WORK_DIR = Path(__file__).resolve().parent
STATE_FILE = WORK_DIR / "bridge_state.json"
LOG_FILE = WORK_DIR / "bridge.log"

# 控制面配置
CONTROL_PLANE_LEVELS = ("off", "essential", "all")  # off=关 | essential=开始/完成/超时/异常 | all=含接收
CONTROL_PLANE_PREFIX = "📡 [控制面]"             # 控制面消息前缀（与 AGENT_PREFIX 区分）
CONTROL_DEBOUNCE_DEFAULT = 5                  # 同事件类型最小间隔（秒）
QUEUE_WARN_DEFAULT = 3                        # 队列长度告警阈值

# 安全: 拒绝代操作模式（来自 SKILL.md 的限制）
DENY_PATTERNS = [
    r"发消息给(他|她|别人|其他人)",
    r"代我发",
    r"帮我发邮件给(他|她|别人)",
    r"上传文件给(他|她|别人)",
]

# ---------- 系统内存 ----------
def get_system_memory_mb():
    """返回 (可用内存MB, 总内存MB)。
    Windows 用 GlobalMemoryStatusEx；其它平台回退 /proc/meminfo。"""
    try:
        if platform.system() == "Windows":
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            kernel32 = ctypes.windll.kernel32
            kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
            kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return stat.ullAvailPhys / (1024 * 1024), stat.ullTotalPhys / (1024 * 1024)
        else:
            # Linux/macOS 回退
            total = None
            avail = None
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total = int(line.split()[1]) / 1024
                    elif line.startswith("MemAvailable:"):
                        avail = int(line.split()[1]) / 1024
                    if total is not None and avail is not None:
                        break
            if total is not None and avail is not None:
                return avail, total
    except Exception as e:
        log.debug(f"取系统内存失败: {e}")
    return -1.0, -1.0

# ---------- 日志 ----------
def setup_logger():
    logger = logging.getLogger("bridge")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger

log = setup_logger()

# ---------- state ----------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"state 文件损坏，重置: {e}")
    return {"watermark": 0}

_state_lock = threading.Lock()
def save_state(state):
    with _state_lock:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------- welink-cli 封装 ----------
def run_cli(args, timeout=60):
    """同步调用 welink-cli，返回 (rc, stdout, stderr)。
    Windows 下 welink-cli 实为 .cmd，需带后缀且 shell=False 以正确处理中文参数。
    可执行文件路径由 main() 通过 --welink-cli-bin / WELINK_CLI_BIN 环境变量覆盖。"""
    cmd = [WELINK_CLI_BIN] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8")
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", f"{WELINK_CLI_BIN} not found in PATH (可用 --welink-cli-bin 或 WELINK_CLI_BIN 环境变量指定绝对路径)"

def send_group(group_id, text, add_prefix=True, prefix=None):
    """发群消息，自动分段。返回最后一段的 msgId 或 None。
    注意: Windows .cmd 参数不支持真实换行符，需把 \\n 转成字面 '\\n' 让 welink-cli 解析转义。
    prefix: 自定义前缀（默认 AGENT_PREFIX）；add_prefix=False 则不加前缀。
    429 限流时自动退避重试（最多 3 次，2/4/8 秒）。"""
    use_prefix = prefix if prefix is not None else AGENT_PREFIX
    last_id = None
    chunks = chunk_text(text, MSG_CHUNK_LIMIT)
    for i, chunk in enumerate(chunks):
        # 第一段加前缀（除非已有 或 调用方禁用）
        if i == 0 and add_prefix and not chunk.startswith(use_prefix):
            chunk = f"{use_prefix}\n{chunk}"
        # 真实换行 → 字面 \n，交给 welink-cli 解析
        chunk_escaped = chunk.replace("\\", "\\\\").replace("\n", "\\n")
        # 429 退避重试
        for attempt in range(3):
            rc, out, err = run_cli(["im", "send-to-group", "--group-id", group_id, "--text", chunk_escaped], timeout=30)
            if rc == 0:
                break
            if "429" in str(err) and attempt < 2:
                backoff = 2 ** (attempt + 1)  # 2s, 4s
                log.warning(f"发送 429 限流，{backoff}s 后重试 (attempt {attempt+1}/3)")
                time.sleep(backoff)
                continue
            log.error(f"发送失败 rc={rc}: {err}")
            return last_id
        try:
            data = json.loads(out)
            ids = data.get("respData", {}).get("msgIds", [])
            if ids:
                last_id = ids[-1]
        except Exception:
            pass
    return last_id

def chunk_text(text, limit):
    if len(text) <= limit:
        return [text]
    parts = []
    buf = ""
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

def query_new_messages(group_id, watermark):
    """拉取 msgId > watermark 的所有消息，返回列表（按时间升序）。
    不带 --query-direction，API 默认返回最新消息。"""
    rc, out, err = run_cli(
        ["im", "query-history-message",
         "--group-id", group_id,
         "--query-count", "100"],
        timeout=30
    )
    if rc != 0:
        log.error(f"拉取消息失败 rc={rc}: {err}")
        return []
    try:
        data = json.loads(out)
    except Exception as e:
        log.error(f"解析消息失败: {e}")
        return []
    msgs = data.get("respData", {}).get("chatInfo", []) or []
    new = [m for m in msgs if int(m.get("msgId", 0)) > watermark]
    new.sort(key=lambda m: int(m.get("msgId", 0)))
    return new

def query_reply(group_id, after_msg_id, timeout, poll_interval, self_account,
                allowed_senders, exclude_prefixes=None):
    """轮询群里满足条件的回复（通用版）。
    allowed_senders: 允许的 sender 集合（如 {self_account} 或 {XIAOLUBAN_ACCOUNT, self_account}）
    exclude_prefixes: content 以这些前缀开头则跳过（如 (XIAOLUBAN_TRIGGER,) 或 (ASK_USER_PREFIX, AGENT_PREFIX, CONTROL_PLANE_PREFIX)）
    按 msgId 升序取第一条匹配。超时返回 None。"""
    exclude_prefixes = exclude_prefixes or ()
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, out, err = run_cli(
            ["im", "query-history-message",
             "--group-id", group_id,
             "--query-count", "20"],
            timeout=30
        )
        if rc == 0:
            try:
                data = json.loads(out)
                msgs = data.get("respData", {}).get("chatInfo", []) or []
                candidates = [m for m in msgs if int(m.get("msgId", 0)) > after_msg_id]
                candidates.sort(key=lambda m: int(m.get("msgId", 0)))
                for m in candidates:
                    sender = m.get("sender", "")
                    content = m.get("content", "").strip()
                    if any(content.startswith(p) for p in exclude_prefixes):
                        continue
                    if sender in allowed_senders:
                        return content
            except Exception:
                pass
        time.sleep(poll_interval)
    return None

def query_luban_reply(group_id, after_msg_id, timeout, poll_interval, self_account):
    """轮询群里小鲁班的回复（包装 query_reply）。
    sender 限 self_account 或 p_xiaoluban；排除 @小鲁班 触发消息。"""
    return query_reply(group_id, after_msg_id, timeout, poll_interval, self_account,
                       allowed_senders={XIAOLUBAN_ACCOUNT, self_account},
                       exclude_prefixes=(XIAOLUBAN_TRIGGER,))

def query_user_reply(group_id, after_msg_id, timeout, poll_interval, self_account):
    """轮询群里用户的回复（包装 query_reply）。
    sender 限本人；排除 Bridge 自己发的各类前缀消息（提问/Agent/控制面）。"""
    return query_reply(group_id, after_msg_id, timeout, poll_interval, self_account,
                       allowed_senders={self_account},
                       exclude_prefixes=(ASK_USER_PREFIX, AGENT_PREFIX, CONTROL_PLANE_PREFIX))

# ---------- 小鲁班钩子 ----------
def process_luban_hooks(text, group_id, self_account):
    """检测 text 中 [[ASK_LUBAN: 问题]] 标记，逐个代发 @小鲁班 并取回回复。
    返回 (处理后的文本, 是否触发过小鲁班)。"""
    matches = list(LUBAN_HOOK_PATTERN.finditer(text))
    if not matches:
        return text, False

    log.info(f"检测到 {len(matches)} 个小鲁班钩子")
    parts = []
    last_end = 0
    for m in matches:
        parts.append(text[last_end:m.start()])
        question = m.group(1).strip()
        log.info(f"询问小鲁班: {question[:80]}")
        # 1) 代发 @小鲁班 消息（不加 🤖 前缀，确保 @小鲁班 在行首才能触发机器人）
        trigger_text = f"{XIAOLUBAN_TRIGGER} {question}"
        sent_id = send_group(group_id, trigger_text, add_prefix=False)
        if not sent_id:
            parts.append(f"[小鲁班调用失败: 发送失败]")
            last_end = m.end()
            continue
        # 2) 轮询取回小鲁班回复（用 sent_id 作为水位，避免取到触发消息本身）
        reply = query_luban_reply(group_id, sent_id, LUBAN_REPLY_TIMEOUT, LUBAN_REPLY_POLL, self_account)
        if reply is None:
            parts.append(f"[小鲁班未在 {LUBAN_REPLY_TIMEOUT}s 内回复]")
        else:
            log.info(f"小鲁班回复长度={len(reply)}")
            parts.append(f"🤝 [小鲁班] {reply}")
        last_end = m.end()
    parts.append(text[last_end:])
    return "".join(parts), True

# ---------- ASK_USER 钩子 ----------
def process_ask_user_hooks(text, group_id, self_account):
    """检测 text 中 [[ASK_USER: 问题]] 标记，逐个代发到群并取回用户回复。
    返回 (处理后的文本, 是否触发过提问, [(问题, 回复|None), ...])。
    qa_pairs 供调用方决定是否需要重跑 todo（有有效回答则重跑带上下文）。"""
    matches = list(ASK_USER_HOOK_PATTERN.finditer(text))
    if not matches:
        return text, False, []

    log.info(f"检测到 {len(matches)} 个 ASK_USER 钩子")
    parts = []
    last_end = 0
    qa_pairs = []
    for m in matches:
        parts.append(text[last_end:m.start()])
        question = m.group(1).strip()
        log.info(f"向用户提问: {question[:80]}")
        # 1) 代发提问消息
        sent_id = send_group(group_id, question, add_prefix=True, prefix=ASK_USER_PREFIX)
        if not sent_id:
            parts.append(f"[提问发送失败: {question[:40]}]")
            qa_pairs.append((question, None))
            last_end = m.end()
            continue
        # 2) 轮询取回用户回复
        reply = query_user_reply(group_id, sent_id, ASK_USER_TIMEOUT, ASK_USER_POLL, self_account)
        if reply is None:
            log.warning(f"ASK_USER 超时({ASK_USER_TIMEOUT}s): {question[:60]}")
            parts.append(f"[用户未在 {ASK_USER_TIMEOUT}s 内回复: {question[:40]}]")
            qa_pairs.append((question, None))
        else:
            log.info(f"用户回复长度={len(reply)}")
            parts.append(f"{ASK_USER_REPLY_PREFIX} {reply}")
            qa_pairs.append((question, reply))
        last_end = m.end()
    parts.append(text[last_end:])
    return "".join(parts), True, qa_pairs

def is_denied(text):
    for pat in DENY_PATTERNS:
        if re.search(pat, text):
            return True
    return False

# ---------- SPAWN_SKILL 指令解析 ----------
def parse_spawn_skill_commands(text):
    """从主 agent 回复中提取 SPAWN_SKILL 指令。
    格式：[[SPAWN_SKILL: mooncake-build 编译机ip=141.61.84.245 tag=20260101]]"""
    matches = list(SPAWN_SKILL_PATTERN.finditer(text))
    return [{"skill": m.group(1), "args": m.group(2).strip()} for m in matches]

def remove_spawn_skill_markers(text):
    """从文本中移除 SPAWN_SKILL 标记（发群前清理）。"""
    return SPAWN_SKILL_PATTERN.sub("", text).strip()

# ---------- 常驻主 agent 进程管理 ----------
class ResidentAgent:
    """管理常驻 codeagent stream-json 进程的生命周期和 I/O。
    - start(): 启动子进程，启动 stdout 读取线程
    - send(content): 向 stdin 写一行 user message JSON，同步等待 result 事件返回 assistant 文本
    - 崩溃检测：进程退出 / 读 stdout 报错 → 触发 on_dead 回调
    线程安全：stdin_lock 保护写入，result_event 同步等待单轮完成。"""
    def __init__(self, on_dead, resident_timeout=RESIDENT_TIMEOUT_DEFAULT,
                 prompt_file=None, proc_registry=None):
        self.proc = None
        self.stdin_lock = threading.Lock()
        self.reader_thread = None
        self.on_dead = on_dead                      # callback(reason: str)
        self.session_id = None
        self.alive = False
        self.resident_timeout = resident_timeout
        self.prompt_file = prompt_file or RESIDENT_PROMPT_FILE_DEFAULT
        self.result_event = threading.Event()
        self.assistant_text_parts = []
        self._lock = threading.Lock()
        self.proc_registry = proc_registry          # 进程注册表（持久化 PID）

    def start(self):
        """启动常驻 codeagent 进程 + reader 线程。"""
        cmd = [CODEAGENT_BIN, "-p",
               "--input-format", "stream-json",
               "--output-format", "stream-json",
               "--verbose",
               "--effort", "low",
               "--dangerously-skip-permissions"]
        # 注入 system prompt
        if self.prompt_file and Path(self.prompt_file).exists():
            cmd += ["--append-system-prompt-file", str(self.prompt_file)]
            log.info(f"resident: 使用 system prompt: {self.prompt_file}")
        else:
            log.warning(f"resident: system prompt 文件不存在: {self.prompt_file}，使用默认 prompt")

        log.info(f"启动常驻主 agent: {' '.join(cmd[:6])}...")
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                cwd=str(WORK_DIR.parent), bufsize=1  # 行缓冲
            )
        except Exception as e:
            log.error(f"启动常驻 agent 失败: {e}")
            self.alive = False
            return

        self.alive = True
        if self.proc_registry:
            self.proc_registry.register(self.proc.pid, "resident", "main-agent")
        self.reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name="resident-reader")
        self.reader_thread.start()
        log.info("常驻主 agent 已启动，等待 init 事件...")

    def send(self, content) -> str:
        """发送 user 消息，等待 result 事件返回 assistant 文本。"""
        if not self.alive or self.proc is None:
            raise RuntimeError("resident agent not alive")
        line = json.dumps({"type": "user",
                           "message": {"role": "user", "content": content}}) + "\n"
        with self._lock:
            self.assistant_text_parts = []
            self.result_event.clear()
        with self.stdin_lock:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self.alive = False
                raise RuntimeError(f"stdin 写入失败: {e}")
        # 等待 result 事件（由 _read_loop 设置）
        if not self.result_event.wait(timeout=self.resident_timeout):
            return f"⏱️ 主 agent 响应超时（{self.resident_timeout}s）"
        with self._lock:
            text = "".join(self.assistant_text_parts).strip()
        return text or "(主 agent 无文本输出)"

    def _read_loop(self):
        """逐行读 stdout，解析 JSON，分发事件。"""
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break  # EOF
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    log.debug(f"resident: 无法解析行: {line[:120]}")
                    continue
                self._handle_event(evt)
        except Exception as e:
            log.error(f"resident reader 异常: {e}")
        finally:
            self.alive = False
            log.warning("resident reader 线程退出，进程可能已死")
            try:
                self.on_dead("reader_thread_exit")
            except Exception:
                pass

    def _handle_event(self, evt):
        """处理单个 stream-json 事件。"""
        t = evt.get("type")
        if t == "system" and evt.get("subtype") == "init":
            self.session_id = evt.get("session_id")
            log.info(f"resident 会话初始化完成 session_id={self.session_id}")
        elif t == "assistant":
            msg = evt.get("message", {})
            for part in msg.get("content", []):
                if part.get("type") == "text":
                    with self._lock:
                        self.assistant_text_parts.append(part["text"])
        elif t == "result":
            # 单轮完成
            self.result_event.set()
        elif t == "system" and evt.get("subtype") == "error":
            log.error(f"resident 系统错误: {evt}")

    def stop(self):
        """停止常驻进程。用 taskkill /T 杀整个进程树（codeagent.bat → codeagentcli.exe）。"""
        self.alive = False
        if self.proc:
            pid = self.proc.pid
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                                   capture_output=True, timeout=10)
                    log.info(f"resident stop: taskkill /T PID={pid}")
                except Exception as e:
                    log.warning(f"resident stop: taskkill 失败: {e}")
            if self.proc_registry:
                self.proc_registry.unregister(pid)

# ---------- 进程注册表 ----------
class ProcRegistry:
    """持久化进程注册表。记录所有由 bridge 启动的子进程 PID。
    进程结束后从注册表删除。bridge 启动时清理上次的残留进程。"""

    REGISTRY_FILE = WORK_DIR / "proc_registry.json"

    def __init__(self):
        self._lock = threading.Lock()
        self._procs = {}  # pid(int) -> {"type": "resident"|"skill", "name": str, "started": ts}

    def register(self, pid, ptype, name):
        """注册一个进程。"""
        with self._lock:
            self._procs[pid] = {"type": ptype, "name": name, "started": time.time()}
            self._save_locked()

    def unregister(self, pid):
        """注销一个进程（进程已结束）。"""
        with self._lock:
            self._procs.pop(pid, None)
            self._save_locked()

    def kill_stale(self):
        """清理上次 bridge 退出后残留的进程。
        读取磁盘注册表文件，对每个 PID 执行 taskkill /PID /F /T，
        然后清空注册表文件。"""
        try:
            with open(self.REGISTRY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            log.info("kill_stale: 无残留进程注册表")
            return
        if not data:
            log.info("kill_stale: 注册表为空，无残留")
            return
        pids = list(data.keys())
        log.info(f"kill_stale: 发现 {len(pids)} 个残留进程，开始清理")
        for pid_str in pids:
            pid = int(pid_str)
            info = data[pid_str]
            log.info(f"kill_stale: 清理 PID={pid} type={info.get('type')} name={info.get('name')}")
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True, timeout=10)
                log.info(f"kill_stale: 已终止 PID={pid}")
            except Exception as e:
                log.warning(f"kill_stale: taskkill PID={pid} 失败: {e}")
        with self._lock:
            self._procs = {}
            self._save_locked()
        log.info("kill_stale: 清理完成，注册表已清空")

    def _save_locked(self):
        """持久化到磁盘（调用方需持锁）。"""
        try:
            with open(self.REGISTRY_FILE, "w", encoding="utf-8") as f:
                json.dump(self._procs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"proc_registry 保存失败: {e}")

# ---------- 资源监控 ----------
class ResourceMonitor:
    """记录 agent 任务运行情况。线程安全。"""
    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.completed = 0
        self.failed = 0
        self.max_concurrent_seen = 0
        self.start_times = {}   # mid -> start_ts
        self.longest_run = 0.0  # 秒

    def on_start(self, mid):
        with self._lock:
            self.active += 1
            self.max_concurrent_seen = max(self.max_concurrent_seen, self.active)
            self.start_times[mid] = time.time()

    def on_finish(self, mid, success):
        with self._lock:
            self.active = max(0, self.active - 1)
            if success:
                self.completed += 1
            else:
                self.failed += 1
            st = self.start_times.pop(mid, None)
            if st is not None:
                dur = time.time() - st
                self.longest_run = max(self.longest_run, dur)

    def snapshot(self):
        with self._lock:
            return {
                "active": self.active,
                "completed": self.completed,
                "failed": self.failed,
                "max_concurrent_seen": self.max_concurrent_seen,
                "longest_run_s": round(self.longest_run, 1),
            }

# ---------- 控制面 ----------
class ControlPlane:
    """向群里发送控制面信息（接收/开始/完成/超时/异常/积压）。
    - level: off | essential | all。essential 不发"接收/入队"事件，all 发。
    - debounce: 同 event_type 至少间隔 N 秒，避免刷屏。
    线程安全。"""
    def __init__(self, group_id, level="essential", debounce=CONTROL_DEBOUNCE_DEFAULT,
                 monitor=None, bridge=None):
        self.group_id = group_id
        self.level = level
        self.debounce = debounce
        self.monitor = monitor
        self.bridge = bridge  # 用于读 queue 长度
        self._lock = threading.Lock()
        self._last_emit = {}  # event_type -> last_ts

    def _enabled(self, event_type):
        if self.level == "off":
            return False
        if self.level == "essential":
            # essential: 只发 开始/完成/超时/异常/积压 + skill 相关
            return event_type in ("start", "completed", "timeout", "error", "backlog",
                                  "skill_start", "skill_done", "resident_restart")
        return True  # all

    def _stats_line(self):
        """生成"当前处理数/排队数/系统可用内存/总内存"统计行。"""
        active = "?"
        if self.monitor is not None:
            active = self.monitor.snapshot()["active"]
        queue_n = "?"
        if self.bridge is not None:
            with self.bridge.queue_lock:
                queue_n = len(self.bridge.queue)
        avail, total = get_system_memory_mb()
        if avail >= 0 and total >= 0:
            mem_str = f"{avail/1024:.1f}GB/{total/1024:.1f}GB"
        else:
            mem_str = "N/A"
        return f"处理中={active} | 排队={queue_n} | 内存={mem_str}"

    def emit(self, event_type, message, force=False):
        """发送一条控制面消息。
        event_type: receive | start | completed | timeout | error | backlog | skill_start | skill_done | resident_restart
        message:   人类可读描述（不含前缀）。
        force=True 跳过 debounce（用于超时/异常等必发场景）。"""
        if not self._enabled(event_type):
            return
        now = time.time()
        with self._lock:
            last = self._last_emit.get(event_type, 0)
            if not force and (now - last) < self.debounce:
                return
            self._last_emit[event_type] = now
        stats = self._stats_line()
        text = f"{event_type.upper()} | {message}\n{stats}"
        log.info(f"[控制面] {text}")
        try:
            send_group(self.group_id, text, add_prefix=True, prefix=CONTROL_PLANE_PREFIX)
        except Exception as e:
            log.warning(f"[控制面] 发送失败: {e}")

# ---------- Bridge 主类 ----------
class Bridge:
    def __init__(self, args):
        self.args = args
        self.state = load_state()
        self.monitor = ResourceMonitor()
        # 控制面：在事件点向群发"处理中/排队/内存"等统计
        self.control = ControlPlane(
            group_id=args.group_id,
            level=args.control_plane,
            debounce=args.control_debounce,
            monitor=self.monitor,
            bridge=self,
        )
        # 任务队列：待执行的 skill 子任务 (skill_cmd, mid)
        self.queue = deque()
        self.queue_lock = threading.Lock()
        # 状态保护：watermark 推进用锁
        self.wm_lock = threading.Lock()
        # 信号量：硬性限制同时在跑的 skill 子 agent 进程数
        self.concurrency_sem = threading.Semaphore(args.max_concurrent)
        # 线程池：工作线程数取一个合理上限
        self.executor = ThreadPoolExecutor(max_workers=args.max_concurrent * 2 + 4,
                                           thread_name_prefix="agent")
        # 运行中任务的 future -> mid
        self.futures = {}
        self.futures_lock = threading.Lock()
        # 常驻主 agent
        self.resident = None
        self.resident_lock = threading.Lock()
        self.resident_restarting = False
        # 运行中 skill 子 agent 进程追踪（用于中途停止）
        self.active_skill_proc = None        # subprocess.Popen 或 None
        self.active_skill_name = None        # 当前运行的 skill 名
        self.active_skill_lock = threading.Lock()
        self.skill_killed = False            # 用户请求停止的标记
        # 进程注册表：持久化记录所有子进程 PID，崩溃后重启可清理孤儿
        self.proc_registry = ProcRegistry()

    def advance_watermark(self, mid):
        """推进 watermark 到至少 mid。只增不减。"""
        with self.wm_lock:
            if mid > self.state["watermark"]:
                self.state["watermark"] = mid
                save_state(self.state)

    def enqueue(self, item):
        with self.queue_lock:
            self.queue.append(item)

    def dequeue(self):
        with self.queue_lock:
            if self.queue:
                return self.queue.popleft()
            return None

    # ---------- 常驻 agent 管理 ----------
    def ensure_resident(self):
        """确保常驻 agent 已启动且存活。不在锁内调用 send（避免死锁）。"""
        with self.resident_lock:
            if self.resident is None or not self.resident.alive:
                self._start_resident_locked()

    def _start_resident_locked(self):
        """在 resident_lock 内启动新常驻进程。"""
        if self.resident is not None:
            try:
                self.resident.stop()
            except Exception:
                pass
        self.resident = ResidentAgent(
            on_dead=self._on_resident_dead,
            resident_timeout=self.args.resident_timeout,
            prompt_file=self.args.resident_prompt,
            proc_registry=self.proc_registry,
        )
        self.resident.start()

    def _on_resident_dead(self, reason):
        """常驻进程死亡回调（从 reader_thread 触发）。异步重启避免递归。"""
        log.error(f"常驻 agent 死亡: {reason}，调度异步重启")
        self.control.emit("resident_restart", f"主 agent 进程退出: {reason}，正在重启...", force=True)
        self.executor.submit(self._restart_resident, reason)

    def _restart_resident(self, reason):
        """重启常驻 agent。"""
        with self.resident_lock:
            if self.resident_restarting:
                log.info("重启已在进行中，跳过")
                return
            self.resident_restarting = True
        log.warning(f"重启常驻 agent: {reason}")
        try:
            with self.resident_lock:
                self._start_resident_locked()
        except Exception as e:
            log.exception(f"重启常驻 agent 失败: {e}")
        finally:
            with self.resident_lock:
                self.resident_restarting = False

    # ---------- 消息处理 ----------
    def handle_message(self, msg):
        """过滤单条消息：合格消息提交到子线程喂给常驻主 agent。
        无论是否处理，watermark 都推进到该 mid。"""
        mid = int(msg.get("msgId", 0))
        sender = msg.get("sender", "")
        content = msg.get("content", "").strip()

        # 1) 跳过 Bridge 自己发的输出
        if (content.startswith(AGENT_PREFIX) or content.startswith(CONTROL_PLANE_PREFIX)
                or content.startswith(ASK_USER_PREFIX) or content.startswith(PARAM_CONFIRM_PREFIX)):
            pfx = ('控制面' if content.startswith(CONTROL_PLANE_PREFIX)
                   else ('提问' if content.startswith(ASK_USER_PREFIX)
                         else ('参数确认' if content.startswith(PARAM_CONFIRM_PREFIX) else 'Agent')))
            log.info(f"跳过自身输出 msgId={mid} prefix={pfx}")
            self.advance_watermark(mid)
            return

        # 1b) 检测中途停止指令——skill 运行中用户发"停止/stop"等
        c_lower = content.lower().strip()
        if any(kw in c_lower for kw in STOP_KEYWORDS):
            with self.active_skill_lock:
                proc = self.active_skill_proc
                skill_name = self.active_skill_name
            if proc is not None and proc.poll() is None:
                log.info(f"用户请求停止 skill: {skill_name}, msgId={mid}")
                self.skill_killed = True
                try:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                                   capture_output=True, timeout=10)
                    log.info(f"已 taskkill skill 子进程树 PID={proc.pid}")
                except Exception as e:
                    log.warning(f"taskkill 失败: {e}")
                self.proc_registry.unregister(proc.pid)
                with self.active_skill_lock:
                    self.active_skill_proc = None
                    self.active_skill_name = None
                send_group(self.args.group_id,
                           f"🛑 已停止 skill {skill_name}，子进程已终止")
                self.advance_watermark(mid)
                return
            # 没有运行中的 skill，继续正常处理（可能是普通对话中提到"停止"）

        # 2) 只处理本人消息
        if sender != self.args.self_account:
            log.info(f"跳过非本人消息 sender={sender} msgId={mid}")
            self.advance_watermark(mid)
            return
        # 3) 安全拒绝
        if is_denied(content):
            send_group(self.args.group_id,
                       f"⚠️ 检测到代操作请求，已拒绝。我是 {self.args.self_account} 的助手，无法代为操作他人。")
            self.advance_watermark(mid)
            return
        # 4) 空消息跳过
        if not content:
            self.advance_watermark(mid)
            return

        # 5) 合格消息：提交到子线程喂给常驻 agent
        log.info(f"分发消息 msgId={mid}: {content[:80]}{'...' if len(content)>80 else ''}")
        self.executor.submit(self.dispatch_to_resident, msg)
        self.advance_watermark(mid)

    def dispatch_to_resident(self, msg):
        """把用户消息喂给常驻主 agent，取回复发群。
        主 agent 回复中若含 SPAWN_SKILL 指令，解析后 spawn 子 agent 跑 skill。"""
        mid = int(msg.get("msgId", 0))
        content = msg.get("content", "").strip()
        # 确保常驻 agent 存活
        try:
            self.ensure_resident()
        except Exception as e:
            log.error(f"启动常驻 agent 失败: {e}")
            send_group(self.args.group_id, f"⚠️ 主 agent 启动失败: {e}")
            return

        # 发送给常驻 agent
        try:
            reply = self.resident.send(content)
        except Exception as e:
            log.error(f"常驻 agent 发送异常: {e}")
            # 触发重启
            self._on_resident_dead(f"send_exception: {e}")
            send_group(self.args.group_id, f"⚠️ 主 agent 异常，正在重启，请稍后重试: {e}")
            return

        if reply.startswith("⏱️"):
            log.warning(f"主 agent 响应超时 msgId={mid}")
            send_group(self.args.group_id, reply)
            return

        # 解析 SPAWN_SKILL 指令
        skill_cmds = parse_spawn_skill_commands(reply)
        if skill_cmds:
            # 主 agent 决定跑长任务——先过参数确认门
            reply_text = remove_spawn_skill_markers(reply)
            if reply_text.strip():
                send_group(self.args.group_id, reply_text)
            for sc in skill_cmds:
                confirmed_cmd = self._confirm_skill_params(sc, mid)
                if confirmed_cmd is not None:
                    self.enqueue((confirmed_cmd, mid))
            self.maybe_schedule()
        else:
            # adhoc：直接发回复
            send_group(self.args.group_id, reply)

    def _confirm_skill_params(self, skill_cmd, trigger_mid):
        """参数确认门：启动 skill 前向用户展示参数，循环确认直到用户同意或取消。
        返回确认后的 skill_cmd（可能含更新后的 args），或 None 表示用户取消/超时。
        skill_cmd: {"skill": "mooncake-build", "args": "编译机ip=... tag=..."}"""
        skill_name = skill_cmd["skill"]
        skill_args = skill_cmd.get("args", "")
        max_rounds = 10  # 防止无限循环
        for _ in range(max_rounds):
            # 构造完整参数展示：默认参数 + 用户覆盖值
            defaults = SKILL_DEFAULTS.get(skill_name, {})
            # 解析用户传入的 args 覆盖默认值
            overrides = {}
            for part in skill_args.split():
                if "=" in part:
                    k, v = part.split("=", 1)
                    overrides[k.strip()] = v.strip()
            param_lines = []
            for k, default_val in defaults.items():
                if k in overrides:
                    param_lines.append(f"{k} = {overrides[k]}（已覆盖默认值 {default_val}）")
                else:
                    param_lines.append(f"{k} = {default_val}")
            param_text = f"即将执行 {skill_name}，完整参数如下：\n" + "\n".join(param_lines)
            prompt_text = (f"{param_text}\n\n"
                           f"请确认参数是否正确。回复『确认』开始执行，"
                           f"或直接回复修正值（如 分支=xxx 目标ip=xxx）来修改参数。")
            sent_id = send_group(self.args.group_id, prompt_text,
                                 add_prefix=True, prefix=PARAM_CONFIRM_PREFIX)
            if not sent_id:
                log.error("参数确认消息发送失败，跳过确认直接执行")
                return skill_cmd
            # 轮询等待用户回复
            reply = query_user_reply(
                self.args.group_id, sent_id,
                PARAM_CONFIRM_TIMEOUT, PARAM_CONFIRM_POLL, self.args.self_account)
            if reply is None:
                log.warning(f"参数确认超时({PARAM_CONFIRM_TIMEOUT}s)，放弃执行 skill {skill_name}")
                send_group(self.args.group_id,
                           f"⏱️ 参数确认超时，skill {skill_name} 未执行")
                return None
            reply_clean = reply.strip()
            reply_lower = reply_clean.lower()
            log.info(f"参数确认回复: {reply_clean[:80]}")
            # 检查确认关键词
            if any(kw in reply_lower for kw in PARAM_CONFIRM_KEYWORDS):
                send_group(self.args.group_id,
                           f"✅ 参数已确认，开始执行 {skill_name}")
                return skill_cmd
            # 检查取消关键词
            if any(kw in reply_lower for kw in PARAM_CANCEL_KEYWORDS):
                send_group(self.args.group_id,
                           f"🚫 已取消 skill {skill_name} 执行")
                return None
            # 否则视为修正值——把用户回复追加到 args
            # 用户可能回复"分支=xxx"或"目标ip=xxx 分支=yyy"等 key=value 形式
            # 解析新增/覆盖的 key=value 对，合并到 skill_args
            new_args = self._merge_skill_args(skill_args, reply_clean)
            if new_args != skill_args:
                skill_args = new_args
                skill_cmd = {"skill": skill_name, "args": skill_args}
                # 循环重新展示更新后的参数
                continue
            # 回复不含 key=value 也不含确认/取消关键词——视为确认
            send_group(self.args.group_id,
                       f"✅ 参数已确认，开始执行 {skill_name}")
            return skill_cmd
        log.warning(f"参数确认超过 {max_rounds} 轮，放弃执行")
        send_group(self.args.group_id, "⚠️ 参数确认轮次过多，已取消执行")
        return None

    @staticmethod
    def _merge_skill_args(old_args, user_reply):
        """把用户回复中的 key=value 对合并到旧 args。
        同 key 覆盖，新 key 追加。返回合并后的 args 字符串。"""
        params = {}
        # 解析旧 args
        for part in old_args.split():
            if "=" in part:
                k, v = part.split("=", 1)
                params[k.strip()] = v.strip()
        # 解析用户回复中的 key=value
        for part in user_reply.split():
            if "=" in part:
                k, v = part.split("=", 1)
                params[k.strip()] = v.strip()
        return " ".join(f"{k}={v}" for k, v in params.items())

    def run_skill_subagent(self, skill_cmd, trigger_mid):
        """spawn 独立 codeagent 子进程跑 skill（stream-json 流式模式，实时反馈）。
        占 sem 许可。
        skill_cmd: {"skill": "mooncake-build", "args": "编译机ip=141.61.84.245 ..."}
        流式执行：逐行读 stdout，assistant 文本实时发群，每个步骤进度都反馈。"""
        skill_name = skill_cmd["skill"]
        skill_args = skill_cmd.get("args", "")
        self.concurrency_sem.acquire()
        self.monitor.on_start(trigger_mid)
        self.control.emit("skill_start",
                          f"开始执行 skill: {skill_name} {skill_args[:50]}", force=True)
        success = False
        try:
            prompt = f"/{skill_name} {skill_args}".strip()
            full_output = self._run_skill_streaming(prompt, skill_name, self.args.agent_timeout)
            # ASK_USER 钩子（skill 子 agent 也可能发问）
            if ASK_USER_HOOK_PATTERN.search(full_output):
                full_output, asked, _ = process_ask_user_hooks(
                    full_output, self.args.group_id, self.args.self_account)
            # 小鲁班钩子
            full_output, hooked = process_luban_hooks(
                full_output, self.args.group_id, self.args.self_account)
            # 最终结果发群
            if full_output.startswith("🛑"):
                # 用户中途停止——不发完成消息（停止时已发过），只通知常驻 agent
                log.info(f"skill {skill_name} 被用户停止")
            elif full_output.startswith("⏱️"):
                self.control.emit("timeout",
                                  f"skill {skill_name} 超时（{self.args.agent_timeout}s）",
                                  force=True)
                send_group(self.args.group_id, full_output)
            else:
                send_group(self.args.group_id, f"✅ skill {skill_name} 完成:\n{full_output}")
            # 把结果通知喂回常驻 agent（让它知道子任务完成了，可继续对话）
            result_msg = f"【子任务完成通知】skill {skill_name} 执行完成，结果摘要：\n{full_output[:800]}"
            try:
                if self.resident and self.resident.alive:
                    self.resident.send(result_msg)
            except Exception as e:
                log.warning(f"通知常驻 agent 子任务完成失败: {e}")
            self.control.emit("skill_done", f"skill {skill_name} 完成", force=True)
            success = (not full_output.startswith("⏱️")
                       and not full_output.startswith("🛑"))
        except Exception as e:
            log.exception(f"skill 子任务异常: {e}")
            send_group(self.args.group_id, f"❌ skill {skill_name} 异常: {e}")
        finally:
            self.concurrency_sem.release()
            self.monitor.on_finish(trigger_mid, success)

    def _run_skill_streaming(self, prompt, skill_name, timeout):
        """以 stream-json 模式启动子 agent，逐行读 stdout，实时把 assistant 文本发群。
        返回完整 assistant 文本拼接。
        - 等 init 事件后才通过 stdin 发 prompt（codeagent 需先初始化）
        - 每个 assistant text 事件：实时发群
        - result 事件：单轮完成，skill 可能多轮继续等
        - 超时：kill 进程，返回 ⏱️ 前缀文本"""
        cmd = [CODEAGENT_BIN, "-p",
               "--input-format", "stream-json",
               "--output-format", "stream-json",
               "--verbose",
               "--dangerously-skip-permissions"]
        log.info(f"启动流式 skill 子 agent: {skill_name}, prompt 长度={len(prompt)}, timeout={timeout}s")
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                cwd=str(WORK_DIR.parent), bufsize=1
            )
        except Exception as e:
            return f"❌ 启动子 agent 失败: {e}"

        # 注册到进程注册表（持久化 PID，崩溃后可清理）
        self.proc_registry.register(proc.pid, "skill", skill_name)

        # 追踪运行中 skill 进程（供中途停止用）
        with self.active_skill_lock:
            self.active_skill_proc = proc
            self.active_skill_name = skill_name
            self.skill_killed = False

        # 独立线程读 stderr 防管道死锁，同时记录错误信息
        def _drain_stderr():
            try:
                for line in proc.stderr:
                    line = line.strip()
                    if line:
                        log.warning(f"skill {skill_name} stderr: {line[:200]}")
            except Exception:
                pass
        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        full_parts = []
        last_text_sent = ""
        killed = [False]
        stdin_lock = threading.Lock()

        # 立即发 prompt（不等 init，codeagent 会排队处理）
        user_line = json.dumps({"type": "user",
                                "message": {"role": "user", "content": prompt}}) + "\n"
        try:
            with stdin_lock:
                proc.stdin.write(user_line)
                proc.stdin.flush()
            log.info(f"skill {skill_name} prompt 已立即发送")
        except Exception as e:
            log.error(f"skill {skill_name} 写 stdin 失败: {e}")
            return f"❌ skill {skill_name} 写 stdin 失败: {e}"

        def _timeout_kill():
            if proc.poll() is None:
                killed[0] = True
                log.warning(f"skill {skill_name} 超时 {timeout}s，kill 进程树")
                try:
                    # Windows: taskkill /T 杀整个进程树（codeagent.bat → codeagentcli.exe）
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                                   capture_output=True, timeout=10)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        timer = threading.Timer(timeout, _timeout_kill)
        timer.daemon = True
        timer.start()

        try:
            line_count = 0
            while True:
                line = proc.stdout.readline()
                if not line:
                    log.info(f"skill {skill_name} stdout EOF (已读 {line_count} 行)")
                    break  # EOF
                line_count += 1
                if line_count <= 5:
                    log.info(f"skill {skill_name} stdout 行#{line_count}: {line.strip()[:100]}")
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = evt.get("type")
                if t == "system" and evt.get("subtype") == "init":
                    log.info(f"skill {skill_name} init 完成 session_id={evt.get('session_id')}")
                elif t == "assistant":
                    msg = evt.get("message", {})
                    for part in msg.get("content", []):
                        if part.get("type") == "text":
                            text = part["text"]
                            full_parts.append(text)
                            # 实时发群（避免重复发相同内容）
                            if text.strip() and text != last_text_sent:
                                last_text_sent = text
                                try:
                                    send_group(self.args.group_id,
                                               f"📝 {skill_name}: {text}",
                                               add_prefix=True, prefix="🤖 [Agent]")
                                except Exception as e:
                                    log.warning(f"实时发送进度失败: {e}")
                elif t == "result":
                    # 单轮完成——skill 子 agent 只跑一轮，收到 result 即结束
                    log.info(f"skill {skill_name} result 事件，单轮完成，退出读取循环")
                    break
                elif t == "system" and evt.get("subtype") == "error":
                    log.error(f"skill {skill_name} 系统错误: {evt}")
        except Exception as e:
            log.error(f"skill {skill_name} 读 stdout 异常: {e}")
        finally:
            timer.cancel()
            try:
                with stdin_lock:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                               capture_output=True, timeout=10)
            # 清除运行中 skill 追踪
            with self.active_skill_lock:
                self.active_skill_proc = None
                self.active_skill_name = None
            # 从进程注册表注销
            self.proc_registry.unregister(proc.pid)

        full_output = "".join(full_parts).strip()
        if killed[0]:
            return f"⏱️ skill {skill_name} 执行超时（{timeout}s）"
        if self.skill_killed:
            return f"🛑 skill {skill_name} 已被用户停止"
        return full_output or "(agent 无输出)"

    def maybe_schedule(self):
        """尽量把队列中的 skill 任务提交到线程池。"""
        while True:
            item = self.dequeue()
            if item is None:
                return
            skill_cmd, mid = item
            fut = self.executor.submit(self.run_skill_subagent, skill_cmd, mid)
            with self.futures_lock:
                self.futures[fut] = mid

    def log_status(self):
        snap = self.monitor.snapshot()
        resident_alive = "alive" if (self.resident and self.resident.alive) else "dead"
        log.info(f"状态: active={snap['active']} completed={snap['completed']} "
                 f"failed={snap['failed']} max_seen={snap['max_concurrent_seen']} "
                 f"longest={snap['longest_run_s']}s queue={len(self.queue)} "
                 f"watermark={self.state['watermark']} resident={resident_alive}")

    def run(self):
        args = self.args
        # 启动时 watermark 初始化
        if args.init_watermark:
            self.state["watermark"] = args.init_watermark
            log.info(f"初始化 watermark = {self.state['watermark']}（--init-watermark）")
            save_state(self.state)
        elif args.skip_history or self.state["watermark"] == 0:
            msgs = query_new_messages(args.group_id, 0)
            if msgs:
                self.state["watermark"] = int(msgs[-1]["msgId"])
                log.info(f"初始化 watermark = {self.state['watermark']}（跳过历史 {len(msgs)} 条）")
            save_state(self.state)

        # 清理上次 bridge 退出后残留的孤儿进程
        self.proc_registry.kill_stale()

        # 启动常驻主 agent
        log.info("启动常驻主 agent...")
        self.ensure_resident()

        log.info(f"Bridge 启动 | group={args.group_id} | self={args.self_account} | "
                 f"watermark={self.state['watermark']} | poll={args.poll_interval}s | "
                 f"agent_timeout={args.agent_timeout}s | resident_timeout={args.resident_timeout}s | "
                 f"max_concurrent={args.max_concurrent} | "
                 f"welink-cli={WELINK_CLI_BIN} | codeagent={CODEAGENT_BIN} | "
                 f"prompt={args.resident_prompt}")

        last_status = time.time()
        STATUS_INTERVAL = 60  # 每 60s 打一次状态
        try:
            while True:
                # 1) 拉新消息并分发
                msgs = query_new_messages(args.group_id, self.state["watermark"])
                for m in msgs:
                    self.handle_message(m)
                # 2) 尽量调度队列中的 skill 任务
                self.maybe_schedule()
                # 2b) 队列积压告警
                with self.queue_lock:
                    qlen = len(self.queue)
                if qlen > args.queue_warn:
                    self.control.emit("backlog",
                                      f"队列积压={qlen}（阈值={args.queue_warn}）")
                # 3) 清理已完成的 future
                with self.futures_lock:
                    done = [f for f in self.futures if f.done()]
                    for f in done:
                        self.futures.pop(f, None)
                # 4) 周期状态
                if time.time() - last_status > STATUS_INTERVAL:
                    self.log_status()
                    last_status = time.time()
                time.sleep(args.poll_interval)
        except KeyboardInterrupt:
            log.info("收到 Ctrl+C，停止常驻 agent，等待在途任务完成...")
            if self.resident:
                self.resident.stop()
            self.executor.shutdown(wait=True, cancel_futures=False)
            self.proc_registry.kill_stale()  # 兜底清理所有残留进程
            save_state(self.state)
            self.log_status()
            log.info("已退出")

def resolve_bin(arg_val, env_var, default):
    """优先级: CLI arg > 环境变量 > 默认值。返回最终可执行文件名/路径。"""
    return arg_val or os.environ.get(env_var) or default

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    ap.add_argument("--self-account", default=DEFAULT_SELF_ACCOUNT)
    ap.add_argument("--poll-interval", type=int, default=POLL_INTERVAL_DEFAULT)
    ap.add_argument("--agent-timeout", type=int, default=AGENT_TIMEOUT_DEFAULT,
                    help=f"skill 子 agent 超时秒数（默认{AGENT_TIMEOUT_DEFAULT}）")
    ap.add_argument("--max-concurrent", type=int, default=MAX_CONCURRENT_DEFAULT,
                    help="同时运行的 skill 子进程数上限（默认1）")
    ap.add_argument("--init-watermark", type=int, default=0,
                    help="启动时把 watermark 直接设为该值（精确控制起点）")
    ap.add_argument("--skip-history", action="store_true",
                    help="启动时把 watermark 推到当前群最大 msgId，只处理之后的新消息")
    ap.add_argument("--control-plane", choices=CONTROL_PLANE_LEVELS,
                    default="essential",
                    help="控制面消息级别: off(关) | essential(开始/完成/超时/异常/积压) | all(含接收)")
    ap.add_argument("--control-debounce", type=int, default=CONTROL_DEBOUNCE_DEFAULT,
                    help=f"控制面同事件类型最小间隔秒数（默认{CONTROL_DEBOUNCE_DEFAULT}）")
    ap.add_argument("--queue-warn", type=int, default=QUEUE_WARN_DEFAULT,
                    help=f"队列积压告警阈值（默认{QUEUE_WARN_DEFAULT}）")
    ap.add_argument("--resident-timeout", type=int, default=RESIDENT_TIMEOUT_DEFAULT,
                    help=f"常驻主 agent 单轮响应超时秒数（默认{RESIDENT_TIMEOUT_DEFAULT}）")
    ap.add_argument("--resident-prompt", default=str(RESIDENT_PROMPT_FILE_DEFAULT),
                    help="主 agent system prompt 文件路径")
    ap.add_argument("--welink-cli-bin", default=None,
                    help="welink-cli 可执行文件路径（默认从 PATH 解析 welink-cli.cmd，"
                         "也可用环境变量 WELINK_CLI_BIN 指定）")
    ap.add_argument("--codeagent-bin", default=None,
                    help="codeagent 可执行文件路径（默认从 PATH 解析 codeagent.bat，"
                         "也可用环境变量 CODEAGENT_BIN 指定）")
    args = ap.parse_args()

    if args.max_concurrent < 1:
        ap.error("--max-concurrent 必须 >= 1")
    if args.control_debounce < 0:
        ap.error("--control-debounce 必须 >= 0")

    global WELINK_CLI_BIN, CODEAGENT_BIN
    WELINK_CLI_BIN = resolve_bin(args.welink_cli_bin, "WELINK_CLI_BIN", WELINK_CLI_DEFAULT)
    CODEAGENT_BIN  = resolve_bin(args.codeagent_bin,  "CODEAGENT_BIN",  CODEAGENT_BIN_DEFAULT)
    # 启动前校验
    for label, path in (("welink-cli", WELINK_CLI_BIN), ("codeagent", CODEAGENT_BIN)):
        if not shutil.which(path) and not Path(path).is_file():
            log.warning(f"⚠️ {label} 解析失败: {path}（PATH/绝对路径均找不到）")

    bridge = Bridge(args)
    bridge.run()

if __name__ == "__main__":
    main()
