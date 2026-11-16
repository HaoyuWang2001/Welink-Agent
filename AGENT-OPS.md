# AGENT-OPS —— 内网部署机操作手册（读者是机器上的 CodeAgent，不是人）

你（执行本手册的 agent）按编号步骤逐条机械执行。每步执行后用"验证"行确认结果，
失败就跳到【排障表】对应行处理后重来。人只通过 WeLink 群使用系统，不会阅读本文件；
你不需要向人解释文档内容，只在需要人操作（扫码、发消息、复制输出）时告诉他在群里/窗口做什么。

## A. 首次部署（Windows）

1. 确认 Python ≥ 3.10。命令：`python --version`
   验证：显示 3.10+。否则先安装 Python 并重开终端。
2. 安装 PyYAML。命令：`pip install pyyaml`
   （内网源不通时用：`pip install pyyaml -i http://mirrors.tools.huawei.com/pypi/simple --trusted-host mirrors.tools.huawei.com`）
   验证：`python -c "import yaml; print(yaml.__version__)"` 输出版本号。
3. 安装 welink-cli。命令：`npm install -g @welink/welink-cli`
   前置：`~/.npmrc` 需含（见 SKILL.md）：
   `registry=http://mirrors.tools.huawei.com/npm/`、`strict-ssl=false`、
   `@welink:registry=https://cmc.centralrepo.rnd.huawei.com/artifactory/api/npm/product_npm/`
   验证：`welink-cli --help` 正常显示 usage。
4. 登录。命令：`welink-cli auth login` —— 需要本人扫码，提示他去扫。
   验证：`welink-cli auth status` 返回含 logged。
5. 把本仓库放到固定目录，例如 `D:\welink-agent`（整个目录复制即可，无构建步骤）。
6. 生成配置：复制 `bridge\config.example.yaml` 为 `bridge\config.yaml`，改两处：
   - `group_id`：真实群 ID。可执行 `welink-cli search group --text "群名关键词"` 获取；拿不到就问人要。
   - `self_account`：本人的「姓氏首字母+工号」（如 z00123456）。不确定就用
     `welink-cli search person --text "姓名"` 查。
7. 自诊断。命令：`cd bridge && python doctor.py`
   验证：最后一行输出 `结论: 部署环境就绪`。有 FAIL 按排障表处理后重跑第 7 步。
8. 把 doctor 全部输出（从 `=== welink-agent doctor ===` 到末尾）发给开发方存档
   （让人整段复制粘贴即可，他不需要阅读）。
9. 启动桥。命令：`cd bridge && python bridge.py --skip-history`
   验证：终端出现 `Bridge 启动 group=...` 行且不再有新日志输出（空闲轮询中）。
   **此窗口必须保持打开**（当前是丙方案：手动窗口常驻；关窗口=停止服务，在跑任务会被终止）。
10. 端到端验收（人在群里操作，你只观察）：请人发一句
    `用 demo-api-probe 查 https://example.huawei.com/health`
    验证：群里出现 `🔐 [参数确认]` 消息，内含 `curl -sS -m 30 -X GET https://example.huawei.com/health`；
    人回"确认"后，最终出现 `✅ [任务#1 ...] 完成`。任何一步不对，记录现象发给开发方，勿自行改代码。

## B. 添加任务模板

1. 在 `bridge/tasks/` 新建 `<名字>.yaml`，照 `demo-build.yaml` 的结构写。规则：
   - `name` 小写字母数字连字符；`steps` 里只能用 `{已在params声明的键}` 占位；
   - 每条 step 是一行完整命令；禁止把"你来决定做什么"写进 steps；
   - 只读查询任务加 `readonly: true`（免确认门），重资源任务加 `exclusive: true`；
   - `params.k.pattern` 必须给正则防注入（示例里的写法照抄改）。
2. 验证语法：`cd bridge && python -c "import sys;sys.path.insert(0,'.');from wabridge.templates import load_templates;print(sorted(load_templates('tasks')))"`
   输出应包含新名字；报错则按报错提示改 yaml 再来。
3. 重启桥（Ctrl+C，重新执行 A-9）。验证：启动日志 `templates=[...]` 含新模板名。
4. 让人在群里发一句自然语言试用，确认出现带正确参数的 🔐 消息。

## C. 日常运维

- 重启：群里通知在跑任务会被终止（报告缺失属正常），Ctrl+C 旧窗口 → 重新 A-9。
  已确认未执行完的排队任务会自动续跑；确认门状态不续（需重新发起）。
- 停止全部任务（人在群里发）：`停止全部`；单人多任务在跑时发 `停止` 会被追问指认 `#编号`。
- 日志位置：`bridge/bridge.log`（总日志）、`bridge/task_logs/task-<编号>-<模板>.log`（每任务原始输出）。
  排障时把相关文件整段发给开发方，不要节选。

## C2. compile-mooncake 任务专用须知

1. **前置源码修复（一次性，人来做）**：`Services-Deployment/services/ssh-0-mooncake-compile/build-mooncake.sh`
   第 7 行现为写死 `CLEAN_BUILD=false`，会覆盖服务注入的环境变量。必须改成
   `CLEAN_BUILD=${CLEAN_BUILD:-false}` 并重新部署该服务，否则确认门里选什么都永远是增量编译。
   验证：部署后在 WebUI 里带变量启动一次，日志里清理分支是否按预期触发。
2. 任务由 `bridge/tasks/scripts/watch_mooncake.py` 全程执行（提交→轮询→判定→取日志），
   子 agent 只运行这一条命令并转述输出。改监视行为=改这个脚本（开发方的活）。
3. 群里说法样例：`编译mooncake`（默认全量重编、已有实例在跑则放弃）；
   `增量编译mooncake`→门里 `clean_build=false`；`排队编译mooncake`→`on_running=wait`；
   `停止mooncake编译`→`action=stop`。
4. 任务超时预算：模板 3900s。脚本报 RESULT: TIMEOUT 时编译仍在服务端跑，
   需要停就用 action=stop，不要重复提交（会撞上 on_running 保护）。

## D. 排障表

| 症状 | 处理 |
|---|---|
| doctor: welink-cli 登录态 FAIL | 重跑 `welink-cli auth login`（人扫码） |
| doctor: codeagent 参数不支持 FAIL | 把 `codeagent --help` 全文发给开发方，不要硬改 bridge 参数 |
| 群里发消息完全无响应 | 看桥窗口有无报错；跑 A-7 doctor；确认发信人是 config 里的 self_account 本人 |
| 确认门很久不出现 | 内网 agent 可能慢：等 `resident_timeout`；桥窗口有 `⏱️ 主 agent 响应超时` 就再发一次消息触发重启 |
| 429 刷屏 | 属限流，bridge 自带退避重试；若持续，降低群内并发使用 |
| 任务卡在 running 不动 | 群里发 `停止` 或 `停止全部`；看对应 task_logs 文件发给开发方 |
| bridge 窗口关闭/崩溃 | 重新 A-9；启动时会自动清理残留进程（日志有 kill_stale 行属正常） |

## E. 你的边界（禁止事项）

1. 禁止修改 `bridge.py`、`wabridge/`、`resident_prompt.md` 源码——框架问题由开发方改。
2. 禁止替任何人执行任务：只有 bridge 进程会执行任务，你只是部署运维。
3. 禁止把 welink-cli 的 client-secret 写进任何文件；登录只靠 `auth login`。
4. 群里他人的消息不会触发任务（bridge 只认 self_account），不要试图"帮别人转发指令"。
