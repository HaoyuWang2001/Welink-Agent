# WeLink ↔ CodeAgent Bridge

内网 Windows 机常驻桥：群里发自然语言 → 常驻主 agent（codeagent）理解 → 预定义任务模板
渲染出确定命令 → 🔐 确认门逐字核验 → 子 agent 执行（并发 3，心跳，安静模式）→ 结果简报回群。

设计原则：执行动作 100% 来自任务模板，LLM 无自主重试权；每个写操作必过人工确认门。

## 文件地图（读者：维护 agent）

| 路径 | 作用 |
|---|---|
| `bridge/bridge.py` | 入口（argparse → wabridge.main.Bridge） |
| `bridge/wabridge/config.py` | 配置：CLI > 环境变量 > config.yaml > 默认 |
| `bridge/wabridge/welink.py` | welink-cli 封装：分段发送(每段带前缀)/429退避/增量拉取 |
| `bridge/wabridge/agentio.py` | ResidentAgent(stream-json 常驻) + TaskRunner(任务子进程) |
| `bridge/wabridge/templates.py` | 任务模板 `tasks/*.yaml` 加载/校验/渲染 |
| `bridge/wabridge/registry.py` | 任务实例 `#N` 身份、寻址解析、持久化 |
| `bridge/wabridge/gates.py` | 异步确认门状态机（单门裸答/多门指认/修正重发/超时） |
| `bridge/wabridge/scheduler.py` | 并发上限 + exclusive 独占 + 队列容量 + 落盘续跑 |
| `bridge/wabridge/main.py` | 主循环：消息分类路由(门/指令/批处理闲聊)、生命周期 |
| `bridge/wabridge/state.py` | bridge_state.json + proc_registry.json(孤儿清理) |
| `bridge/wabridge/procs.py` | 跨平台进程树强杀（Windows taskkill /T；POSIX 进程组） |
| `bridge/resident_prompt.md` | 主 agent system prompt（模板清单启动时自动追加） |
| `bridge/tasks/` | 任务模板目录 |
| `bridge/doctor.py` | 部署机自诊断（输出整段发给开发方） |
| `AGENT-OPS.md` | 内网部署机操作手册（给那台机器上的 agent 照做） |
| `tests/sim/` | welink-cli / codeagent 高保真模拟器 |
| `tests/e2e/` | 端到端：真实 bridge 进程 × 模拟器 × 群消息状态机 |
| `tests/unit/` | 核心逻辑单测 |

## 快速开始

部署与验收一律按 [AGENT-OPS.md](AGENT-OPS.md) 的编号步骤执行（A 节首次部署 / B 节加任务模板 / D 节排障）。

开发侧本地验证（无需内网）：

```bash
pip install pyyaml
python -m pytest tests/unit tests/e2e -q     # 全绿才算改动有效
```

## 消息协议（bridge 视角）

- 入：仅 `self_account` 的裸文本；🤖/📡/❓/🔐 前缀消息视为自身输出跳过。
- 指令词：`状态` / `停止 [#N]` / `停止全部` / `清空队列`；确认门：`确认` / `取消` / `k=v`。
- 主 agent 输出标记：`[[SPAWN_TASK: <模板名> k=v ...]]`（一轮可多个），其余输出直接转发群。
- 任务消息一律带 `[任务#N 短名]` 身份头；心跳 `⏳` 每 `heartbeat_interval` 秒。
