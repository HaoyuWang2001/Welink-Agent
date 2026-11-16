#!/usr/bin/env python3
"""welink-cli 模拟器：真实 CLI 的收发接口+状态文件+429 注入，供端到端测试。

环境变量：
  WELINK_SIM_STATE    群消息状态文件（必需）
  WELINK_SIM_SELF     发送者账号（bridge 以本人身份发消息，默认 "self"）
  WELINK_SIM_429      前 N 次发送返回 429（限流重试测试）
  WELINK_SIM_FAILQ    非零时 query 直接失败（拉取异常测试，默认不启用）
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import simlib


def fail(msg, rc=2):
    print(msg, file=sys.stderr)
    sys.exit(rc)


def get_flag(argv, name):
    if name in argv:
        return argv[argv.index(name) + 1]
    return None


def main():
    state_path = os.environ.get("WELINK_SIM_STATE") or fail("WELINK_SIM_STATE 未设置")
    latency = float(os.environ.get("WELINK_SIM_LATENCY", "0"))
    if latency:
        time.sleep(latency / 1000.0)

    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        print("usage: welink-cli {im,auth,search,config} ...")
        print("  im send-to-group --group-id ID --text TEXT")
        print("  im query-history-message --group-id ID --query-count N")
        print("  auth login | auth status")
        sys.exit(0)
    if not argv:
        fail("usage: welink-cli <cmd> ...")
    cmd = argv[0]

    if cmd == "im" and len(argv) > 1 and argv[1] == "send-to-group":
        text = get_flag(argv, "--text")
        if text is None:
            fail("--text 缺失")
        content = simlib.unescape_text(text)
        n429 = int(os.environ.get("WELINK_SIM_429", "0"))
        # 用状态文件记录已发送次数判断是否注入 429
        st, f = simlib.load_state(state_path)
        if n429 > 0 and st.get("sends_429_left", n429) > 0:
            st["sends_429_left"] = st.get("sends_429_left", n429) - 1
            simlib.save_state(st, f)
            fail("HTTP 429 Too Many Requests", 1)
        mid = simlib.append_msg(st, f,
                                os.environ.get("WELINK_SIM_SELF", "self"), content)
        print(json.dumps({"respData": {"msgIds": [str(mid)]}}, ensure_ascii=False))
        return

    if cmd == "im" and len(argv) > 1 and argv[1] == "query-history-message":
        if os.environ.get("WELINK_SIM_FAILQ"):
            fail("HTTP 500 query failed", 1)
        count = int(get_flag(argv, "--query-count") or "30")
        st, f = simlib.load_state(state_path)
        msgs = sorted(st["msgs"], key=lambda m: int(m["msgId"]))[-count:]
        simlib.save_state(st, f)
        print(json.dumps({"respData": {"chatInfo": [
            {"msgId": str(m["msgId"]), "sender": m["sender"], "content": m["content"]}
            for m in msgs]}}, ensure_ascii=False))
        return

    if cmd == "auth" and len(argv) > 1 and argv[1] == "status":
        print(json.dumps({"respData": {"status": "logged_in",
                                       "account": os.environ.get("WELINK_SIM_SELF", "self")}}))
        return

    fail(f"模拟器不支持的命令: {' '.join(argv)}")


if __name__ == "__main__":
    main()
