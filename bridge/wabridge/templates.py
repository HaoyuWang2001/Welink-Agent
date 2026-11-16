"""任务模板：加载/校验 tasks/*.yaml，渲染参数与步骤。

模板 schema（字段均可缺省）:
  name            必填, ^[a-z0-9][a-z0-9_-]*$
  title           群消息短名（默认=name）
  description     给常驻主 agent 判断适用性
  readonly        true 时豁免确认门
  exclusive       true 时独占执行
  timeout         整任务超时秒（默认取 config）
  heartbeat       心跳间隔秒, 0=关（默认取 config）
  cwd             子 agent 工作目录（默认 bridge workdir 上级）
  params          {k: {default, required, pattern, desc, example}}
  confirm_steps   确认门里的可读意图描述（可选；完整命令始终照原样展示）
  steps           ["命令模板 {param}", ...]，渲染后即为将执行的逐字内容
  report          简报格式要求（发给执行 agent）
"""
import re
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class TemplateError(Exception):
    pass


class Template:
    def __init__(self, d: dict, source: str):
        self.source = source
        name = str(d.get("name") or Path(source).stem)
        if not NAME_RE.match(name):
            raise TemplateError(f"模板名非法: {name!r}（需匹配 {NAME_RE.pattern}）")
        self.name = name
        self.title = str(d.get("title") or name)
        self.description = str(d.get("description", ""))
        self.readonly = bool(d.get("readonly", False))
        self.exclusive = bool(d.get("exclusive", False))
        self.timeout = d.get("timeout")
        self.heartbeat = d.get("heartbeat")
        self.cwd = d.get("cwd")
        self.report = str(d.get("report",
                                "最后输出简报：第一行结论（成功/失败+一句话）；耗时；"
                                "失败时附关键错误行(最多5)与完整日志路径"))
        params = d.get("params") or {}
        self.params = {}
        for k, v in params.items():
            if not NAME_RE.match(str(k)):
                raise TemplateError(f"{name}: 参数名非法: {k!r}")
            v = v or {}
            self.params[k] = {
                "default": str(v.get("default", "")) if v.get("default") is not None else None,
                "required": bool(v.get("required", False)),
                "pattern": v.get("pattern"),
            }
        steps = d.get("steps") or []
        if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
            raise TemplateError(f"{name}: steps 必须是字符串列表")
        self.steps = steps
        desc = d.get("confirm_steps") or []
        if not isinstance(desc, list) or not all(isinstance(s, str) for s in desc):
            raise TemplateError(f"{name}: confirm_steps 必须是字符串列表")
        self.confirm_steps = desc
        self._check_placeholders()

    def _check_placeholders(self):
        allowed = set(self.params)
        text = "\n".join(self.steps + self.confirm_steps) + "\n" + self.report
        for ph in set(PLACEHOLDER_RE.findall(text)):
            if ph not in allowed:
                raise TemplateError(f"{self.name}: 引用了未定义参数 {{{ph}}}")

    def defaults(self):
        return {k: (v["default"] or "") for k, v in self.params.items()
                if v["default"] is not None}

    def validate_param(self, k, val):
        spec = self.params.get(k)
        if spec is None:
            return f"未知参数 {k}（模板 {self.name} 允许: {sorted(self.params) or '无'}）"
        if spec["pattern"] and not re.match(spec["pattern"], val):
            return f"参数 {k}={val!r} 不满足校验 {spec['pattern']}"
        return None

    def render_steps(self, params: dict):
        return [s.format(**params) for s in self.steps]

    def catalog_line(self):
        ps = ", ".join(
            f"{k}{'*' if v['required'] else ''}={v['default'] or '?'}"
            for k, v in self.params.items())
        flags = " ".join(f for f, on in
                         (("readonly免确认", self.readonly), ("exclusive独占", self.exclusive)) if on)
        return f"- {self.name}: {self.description} 参数({ps}) {flags}".rstrip()


def load_templates(tasks_dir: Path):
    """加载并校验目录下全部模板。返回 {name: Template}；任一非法即抛 TemplateError。"""
    if yaml is None:
        raise TemplateError("缺少 PyYAML，无法加载任务模板 (pip install pyyaml)")
    out = {}
    tasks_dir = Path(tasks_dir)
    if not tasks_dir.exists():
        return out
    for f in sorted(tasks_dir.glob("*.y*ml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if not isinstance(d, dict):
            raise TemplateError(f"{f.name}: 顶层必须是 mapping")
        t = Template(d, source=f.name)
        if t.name in out:
            raise TemplateError(f"模板名重复: {t.name} ({f.name})")
        out[t.name] = t
    return out
