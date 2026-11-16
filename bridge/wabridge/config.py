"""配置加载：优先级 CLI arg > 环境变量 > config.yaml > 内置默认。"""
import copy
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

PACKAGE_DIR = Path(__file__).resolve().parent          # bridge/wabridge
REPO_ROOT = PACKAGE_DIR.parent.parent                  # Welink-Agent/

DEFAULTS = {
    "welink": {
        "group_id": "",
        "self_account": "",
        "cli_bin": "welink-cli.cmd",
        "poll_interval": 15.0,
        "chunk_limit": 1800,
        "send_timeout": 30,
        "query_count": 100,
    },
    "agent": {
        "bin": "codeagent.bat",
        "resident_timeout": 60,
        "task_timeout_default": 900,
        "max_concurrent": 3,
        "heartbeat_interval": 300,
        "queue_capacity": 6,
        "stop_keywords": ["停止", "停下", "终止", "取消任务", "stop", "abort", "kill"],
        "stop_all_keyword": "停止全部",
        "status_keyword": "状态",
        "clear_queue_keyword": "清空队列",
    },
    "confirm": {
        "timeout": 300,
        "poll_interval": 3.0,
        "keywords": ["确认", "正确", "对", "yes", "ok", "开始", "没问题", "继续", "跑吧", "执行"],
        "cancel_keywords": ["取消", "算了", "不要了", "abort", "cancel"],
        "choice_timeout": 120,
    },
    "paths": {
        "workdir": None,               # 默认 = REPO_ROOT/bridge
        "state_file": "bridge_state.json",
        "log_file": "bridge.log",
        "registry_file": "proc_registry.json",
        "tasks_dir": "tasks",
        "resident_prompt": "resident_prompt.md",
    },
    "control_plane": "essential",       # off | essential
    "control_debounce": 5,
}

# 环境变量映射: ENV -> (路径, 类型)
ENV_MAP = {
    "WA_GROUP_ID": (("welink", "group_id"), str),
    "WA_SELF_ACCOUNT": (("welink", "self_account"), str),
    "WA_POLL_INTERVAL": (("welink", "poll_interval"), float),
    "WA_CHUNK_LIMIT": (("welink", "chunk_limit"), int),
    "WA_CLI_BIN": (("welink", "cli_bin"), str),
    "WA_CODEAGENT_BIN": (("agent", "bin"), str),
    "WA_RESIDENT_TIMEOUT": (("agent", "resident_timeout"), float),
    "WA_TASK_TIMEOUT": (("agent", "task_timeout_default"), float),
    "WA_MAX_CONCURRENT": (("agent", "max_concurrent"), int),
    "WA_HEARTBEAT_INTERVAL": (("agent", "heartbeat_interval"), float),
    "WA_QUEUE_CAP": (("agent", "queue_capacity"), int),
    "WA_GATE_TIMEOUT": (("confirm", "timeout"), float),
    "WA_CONTROL_PLANE": (("control_plane",), str),
    "WA_CONFIG": (("_config_path",), str),
}


def _deep_merge(base, override):
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


@dataclass
class Config:
    raw: dict = field(default_factory=dict)

    def get(self, *path, default=None):
        node = self.raw
        for p in path:
            if not isinstance(node, dict) or p not in node:
                return default
            node = node[p]
        return node

    # ---- 便捷属性 ----
    @property
    def group_id(self): return str(self.get("welink", "group_id"))
    @property
    def self_account(self): return str(self.get("welink", "self_account"))
    @property
    def welink_bin(self): return str(self.get("welink", "cli_bin"))
    @property
    def codeagent_bin(self): return str(self.get("agent", "bin"))
    @property
    def poll_interval(self): return float(self.get("welink", "poll_interval"))
    @property
    def chunk_limit(self): return int(self.get("welink", "chunk_limit"))
    @property
    def resident_timeout(self): return float(self.get("agent", "resident_timeout"))
    @property
    def default_task_timeout(self): return float(self.get("agent", "task_timeout_default"))
    @property
    def max_concurrent(self): return int(self.get("agent", "max_concurrent"))
    @property
    def heartbeat_interval(self): return float(self.get("agent", "heartbeat_interval"))
    @property
    def queue_capacity(self): return int(self.get("agent", "queue_capacity"))
    @property
    def gate_timeout(self): return float(self.get("confirm", "timeout"))
    @property
    def gate_poll(self): return float(self.get("confirm", "poll_interval"))
    @property
    def choice_timeout(self): return float(self.get("confirm", "choice_timeout"))
    @property
    def control_plane(self): return str(self.get("control_plane", "essential"))
    @property
    def workdir(self):
        w = self.get("paths", "workdir")
        return Path(w).resolve() if w else (REPO_ROOT / "bridge")
    def _p(self, name):
        v = str(self.get("paths", name))
        p = Path(v)
        return p if p.is_absolute() else self.workdir / p
    @property
    def state_file(self): return self._p("state_file")
    @property
    def log_file(self): return self._p("log_file")
    @property
    def registry_file(self): return self._p("registry_file")
    @property
    def tasks_dir(self): return self._p("tasks_dir")
    @property
    def resident_prompt(self): return self._p("resident_prompt")


def load_config(cli_overrides: dict | None = None) -> Config:
    """cli_overrides: {'welink': {...}, ...} 嵌套或 {'group_id': ...} 平铺均可，None 值忽略。"""
    cfg_raw = copy.deepcopy(DEFAULTS)

    # 1) config.yaml
    path = None
    env_cfg = os.environ.get("WA_CONFIG")
    if cli_overrides and cli_overrides.get("_config_path"):
        path = cli_overrides.pop("_config_path")
    elif env_cfg:
        path = env_cfg
    else:
        default_cfg = REPO_ROOT / "bridge" / "config.yaml"
        if default_cfg.exists():
            path = str(default_cfg)
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        if yaml is None:
            raise RuntimeError("缺少 PyYAML，无法解析 config.yaml (pip install pyyaml)")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        _deep_merge(cfg_raw, data)

    # 2) 环境变量
    for env, (cpath, typ) in ENV_MAP.items():
        if env in os.environ:
            node = cfg_raw
            for seg in cpath[:-1]:
                node = node.setdefault(seg, {})
            try:
                node[cpath[-1]] = typ(os.environ[env])
            except ValueError:
                raise ValueError(f"环境变量 {env} 值非法: {os.environ[env]!r}（应为 {typ.__name__}）")

    # 3) CLI 覆盖（支持平铺简写键）
    flat = {
        "group_id": ("welink", "group_id"), "self_account": ("welink", "self_account"),
        "poll_interval": ("welink", "poll_interval"),
        "welink_cli_bin": ("welink", "cli_bin"), "codeagent_bin": ("agent", "bin"),
        "resident_timeout": ("agent", "resident_timeout"),
        "agent_timeout": ("agent", "task_timeout_default"),
        "max_concurrent": ("agent", "max_concurrent"),
        "heartbeat_interval": ("agent", "heartbeat_interval"),
        "queue_warn": ("agent", "queue_capacity"),
        "confirm_timeout": ("confirm", "timeout"),
        "control_plane": ("control_plane",),
        "workdir": ("paths", "workdir"),
        "resident_prompt": ("paths", "resident_prompt"),
        "tasks_dir": ("paths", "tasks_dir"),
    }
    for k, v in (cli_overrides or {}).items():
        if v is None or k.startswith("_"):
            continue
        if k in flat:
            node = cfg_raw
            for seg in flat[k][:-1]:
                node = node.setdefault(seg, {})
            node[flat[k][-1]] = v
        elif isinstance(v, dict):
            _deep_merge(cfg_raw.setdefault(k, {}), v)
    return Config(raw=cfg_raw)
