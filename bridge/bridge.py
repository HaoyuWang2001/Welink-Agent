#!/usr/bin/env python3
"""WeLink <-> CodeAgent bridge 入口。

用法:
    python bridge.py                      # 读 bridge/config.yaml（或 WA_CONFIG）
    python bridge.py --skip-history       # 首次启动跳过历史消息
    python bridge.py --config path.yaml --max-concurrent 3
停止: Ctrl+C（在跑任务会被终止，已确认未跑的队列落盘续跑）
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wabridge.config import load_config          # noqa: E402
from wabridge.main import Bridge                 # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config.yaml 路径（默认找 bridge/config.yaml，或 WA_CONFIG）")
    ap.add_argument("--group-id")
    ap.add_argument("--self-account")
    ap.add_argument("--poll-interval", type=float)
    ap.add_argument("--welink-cli-bin")
    ap.add_argument("--codeagent-bin")
    ap.add_argument("--resident-timeout", type=float, help="常驻主 agent 单轮超时(秒)")
    ap.add_argument("--agent-timeout", type=float, help="任务默认超时(秒)")
    ap.add_argument("--max-concurrent", type=int)
    ap.add_argument("--heartbeat-interval", type=float)
    ap.add_argument("--confirm-timeout", type=float, help="确认门超时(秒)")
    ap.add_argument("--queue-warn", type=int, help="队列容量上限")
    ap.add_argument("--control-plane", choices=("off", "essential"))
    ap.add_argument("--skip-history", action="store_true")
    ap.add_argument("--init-watermark", type=int)
    ap.add_argument("--workdir")
    ap.add_argument("--tasks-dir")
    ap.add_argument("--resident-prompt")
    args = ap.parse_args()

    overrides = {k: v for k, v in vars(args).items()
                 if v is not None and k not in ("config", "skip_history", "init_watermark")}
    if args.config:
        overrides["_config_path"] = args.config

    try:
        cfg = load_config(overrides)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"配置错误: {e}", file=sys.stderr)
        sys.exit(2)

    if not cfg.group_id or not cfg.self_account:
        print("缺少 group_id/self_account：请复制 config.example.yaml 为 config.yaml 并填写，"
              "或用 --group-id/--self-account/WA_GROUP_ID 指定", file=sys.stderr)
        sys.exit(2)
    if args.max_concurrent is not None and args.max_concurrent < 1:
        ap.error("--max-concurrent 必须 >= 1")

    bridge = Bridge(cfg)
    try:
        bridge.run(skip_history=args.skip_history, init_watermark=args.init_watermark)
    except KeyboardInterrupt:
        bridge.shutdown()
    except Exception as e:  # noqa: BLE002  模板等启动期硬错误
        print(f"启动失败: {e}", file=sys.stderr)
        bridge.log.error(f"启动失败: {e}")
        sys.exit(3)


if __name__ == "__main__":
    main()
