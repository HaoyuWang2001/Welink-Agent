"""模拟器共享库：flock 保护的群消息状态、场景文件读取。"""
import json
import os
import time

try:
    import fcntl
    _HAVE_FCNTL = True
except ImportError:
    _HAVE_FCNTL = False

BASE_MSG_ID = 89_000_000_000_000_000  # 仿雪花 ID


def _lock(f):
    if _HAVE_FCNTL:
        fcntl.flock(f, fcntl.LOCK_EX)


def _unlock(f):
    if _HAVE_FCNTL:
        fcntl.flock(f, fcntl.LOCK_UN)


def load_state(path):
    """打开状态文件（带锁）返回 (data, fh)。调用方用 save_state 关闭。"""
    try:
        f = open(path, "r+", encoding="utf-8")
    except FileNotFoundError:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        f = open(path, "w+", encoding="utf-8")
    _lock(f)
    try:
        f.seek(0)
        data = json.load(f)
    except json.JSONDecodeError:
        data = {"msgs": [], "next_id": BASE_MSG_ID}
    data.setdefault("msgs", [])
    data.setdefault("next_id", BASE_MSG_ID)
    return data, f


def save_state(data, f):
    f.seek(0)
    f.truncate()
    json.dump(data, f, ensure_ascii=False)
    _unlock(f)
    f.close()


def append_msg(state, f, sender, content):
    mid = state["next_id"]
    state["next_id"] = mid + 1
    state["msgs"].append({"msgId": mid, "sender": sender,
                          "content": content, "ts": time.time()})
    save_state(state, f)
    return mid


def read_msgs(path):
    try:
        return json.loads(open(path, encoding="utf-8").read()).get("msgs", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def unescape_text(raw):
    """welink-cli 传参约定：真实换行以字面 \\n 传递。"""
    out, buf, i = [], "", 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == "n":
                out.append(buf); buf = ""; i += 2; continue
            if nxt == "\\":
                buf += "\\"; i += 2; continue
        buf += c
        i += 1
    out.append(buf)
    return "\n".join(out)


def load_scenario():
    p = os.environ.get("CODEAGENT_SIM_SCENARIO")
    if not p or not os.path.exists(p):
        return {}
    return json.loads(open(p, encoding="utf-8").read())


def cursor_next():
    """常驻 agent 的回合游标（跨进程重启持久）。"""
    p = os.environ.get("CODEAGENT_SIM_STATE", "/tmp/codeagent_sim_state.json")
    d = {}
    try:
        d = json.loads(open(p, encoding="utf-8").read())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    d["turns"] = d.get("turns", 0) + 1
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f)
    return d["turns"]


def record_child(pid, log_path):
    if not log_path:
        return
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{pid}\n")
