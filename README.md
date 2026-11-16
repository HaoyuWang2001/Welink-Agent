# WeLink ↔ CodeAgentCLI Bridge（常驻主 agent 架构）

常驻 Python 脚本，把指定 WeLink 群里"你本人发的消息"交给一个常驻 CodeAgentCLI 进程（stream-json 模式）处理，保持多轮对话上下文连续。主 agent 决定直接回复还是启动子 agent 跑长任务，结果加 `🤖 [Agent]` 前缀发回群里。

## 架构

```
你在群里发消息
  → Bridge 轮询 query-history-message
  → 过滤 sender==本人 且 content 不以 🤖/📡/❓/🔐 前缀开头
  → 喂给常驻 codeagent stream-json 进程（保持对话上下文）
  → 主 agent 决定：
      ├─ 直接回复 → 加 🤖 前缀发回群
      └─ 输出 [[SPAWN_SKILL: name args]] → 参数确认门 → spawn 子进程跑 skill → 结果发群
  → 常驻进程崩溃时自动重启
```

## 文件

| 文件 | 作用 |
|---|---|
| `bridge.py` | 主脚本：WeLink 收发、常驻进程管理、skill 并发控制、崩溃恢复 |
| `resident_prompt.md` | 主 agent system prompt（决策规则、输出格式） |
| `bridge_state.json` | watermark（自动生成，重启续跑） |
| `proc_registry.json` | 子进程 PID 注册表（自动生成，启动时清理孤儿） |
| `bridge.log` | 运行日志（自动生成） |

## 前置条件

1. `welink-cli` 已全局安装（实际调用 `welink-cli.cmd`），可用 `--welink-cli-bin` 或 `WELINK_CLI_BIN` 指定。
2. `codeagent` 已安装（实际调用 `codeagent.bat`），可用 `--codeagent-bin` 或 `CODEAGENT_BIN` 指定。
3. 已 `welink-cli auth login` 完成 OAuth 登录。
4. 已创建交互用 WeLink 群，并把群 ID 传给 Bridge。
5. skill 放在 `~/.cac/skills/<name>/SKILL.md`。

## 启动

```bash
# 首次启动：跳过历史消息
python F:\welink-cli\bridge\bridge.py --skip-history

# 精确指定起点
python F:\welink-cli\bridge\bridge.py --init-watermark 89713690597261161

# 自定义参数
python F:\welink-cli\bridge\bridge.py \
  --group-id 1023824099283107996 \
  --self-account w00921547 \
  --poll-interval 15 \
  --agent-timeout 600 \
  --max-concurrent 3 \
  --resident-timeout 300
```

停止：`Ctrl+C`，state 自动落盘，常驻进程被停止。

## 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--group-id` | `1023824099283107996` | WeLink 群 ID |
| `--self-account` | `w00921547` | 本人 WeLink 账号 |
| `--poll-interval` | `15` | 轮询间隔（秒） |
| `--agent-timeout` | `900` | skill 子 agent 超时（秒） |
| `--max-concurrent` | `1` | 同时运行的 skill 子进程数上限 |
| `--resident-timeout` | `60` | 常驻主 agent 单轮响应超时（秒） |
| `--resident-prompt` | `resident_prompt.md` | 主 agent system prompt 文件 |
| `--skip-history` | - | 启动时跳过历史消息 |
| `--init-watermark` | `0` | 精确设置起始 watermark |
| `--control-plane` | `essential` | 控制面级别：off/essential/all |
| `--control-debounce` | `5` | 同事件类型最小间隔（秒） |
| `--queue-warn` | `3` | 队列积压告警阈值 |
| `--welink-cli-bin` | PATH 解析 | welink-cli 可执行文件路径 |
| `--codeagent-bin` | PATH 解析 | codeagent 可执行文件路径 |

## SPAWN_SKILL 格式

主 agent 回复中包含 `[[SPAWN_SKILL: <skill名> <参数>]]` 时，Bridge 解析后启动子 agent：

```
收到，开始执行任务。
[[SPAWN_SKILL: my-skill 参数1=值 参数2=值]]
```

Bridge 会：提取 skill 名和参数 → 参数确认门（展示参数等待用户确认/修正）→ spawn 独立 codeagent 子进程流式执行 → 把输出发回群 → 通知常驻主 agent 子任务完成。

## 中途停止

skill 运行中用户在群里发"停止/stop"等关键词，Bridge 会 taskkill 子进程树并通知。

## 崩溃恢复

常驻 codeagent 进程退出时，Bridge 自动重启，并发群通知 `🔄 主 agent 重启中`。启动时通过 `proc_registry.json` 清理上次残留的孤儿进程。

## 添加新 Skill

1. 创建 `~/.cac/skills/<name>/SKILL.md`，写 frontmatter（name, description）+ 执行步骤。
2. 在 `resident_prompt.md` 的"可用 skill"章节补充说明。
3. 如需参数确认门展示默认参数，在 `bridge.py` 的 `SKILL_DEFAULTS` 中为 skill 添加条目。
4. 重启 bridge。
