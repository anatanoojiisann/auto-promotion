# Auto Promotion

一个面向 Telegram、Reddit、Instagram、X、Discord 和 TikTok 的本机推广工作台，将账号、任务启动、调度和执行记录集中在同一个入口。

**工作台和六个平台后端均包含在本仓库中，真实写入默认关闭。** Telegram 使用 Telethon，Reddit/X 使用本机独立浏览器，Instagram 使用 Graph API，Discord 使用官方 Bot HTTP API。无需另一台电脑或相邻工程；账号须在本机重新登录或授权，平台账号和运行数据不随源码分发。

## 功能

新增 **高频互动**：默认十分钟增量扫描，合格 P0/P1 自动进入原持久发送队列，
P2 固定五小时批次，连续五轮完整空扫描后才评估 P3 人工候补。
包含真实后端设置、候选选择、通知、运行状态、重启恢复和未知结果核验。
默认停用、dry-run；本机当前缺少官方 X API 与模型/媒体接入条件，
不会把浏览器登录或模拟结果当作真实自动发布资格。
接入、迁移、启停与外部缺口见 [高频互动](docs/high-frequency-interactions.md)。

新增 **X 原创发布与任务依赖**：在单个任务中明确开启自有账号原创文字或配图发布，支持等待指定互动任务的真实发布回执后再安排发送。原账号动作权限、额度和未知结果保护继续生效，详见 [X 原创发布](docs/x-original-posts.md)。

新增 **Telegram 准备内容后线上发布**：将有原文依据的精确回复交给原云端队列，保留发布前上下文复核、父任务共享额度与只读回执核验；同时修复扫码计时器和账号读写／登录冷却隔离。详见 [准备内容后线上发布](docs/prepared-telegram-replies.md) 与 [云端执行器](docs/telegram-cloud-executor.md)。

新增 **任务模板与任务导出**：在任务详情保存常用发现规则和执行节奏，下次重新选择账号和日期创建独立草稿；同时下载完整审阅清单及包含全部任务关联日志的 ZIP 包。账号、旧目标、内容与启动状态不会自动复用。常用流程和历史辅助脚本的对应关系见 [每次任务的固定工具](docs/task-tools.md)。

新增 **业务增长与回复待办**：设置下载和付费目标，按渠道导入商店指标及匿名应用事件，区分到期试用、真实首笔扣费和退款；将真实回复关联后续任务草稿。共享内容支持版本核对及指定公开群的定时只读采集。真实商店与应用数据需要导入或接入认证接口，说明见 [业务增长](docs/business-growth.md)。

新增 **号池与人设、共享内容池、推广活动**：按人设和剩余容量规划数十个账号与上千条内容，生成执行批次，再由账号串行、跨账号有界并发的调度器执行。支持 JSON/CSV 导入、公开 Telegram 群消息只读采集、模板及可选 AI 草稿、跨活动去重、额度预留和账号效果统计。操作和接口见 [号池说明](docs/account-pools.md)。

当前 Reddit 的真实互动使用人工清单；X 按账号已有动作权限和任务明确设置决定自动或人工路线，Browser Use 只读观察连接保持人工模式，高频策略另行检查官方 API 及目标资格。全局额度默认100次/日、20次/小时，调高设置不会提高旧任务创建时的额度上限。

| 模块 | 能力 |
| --- | --- |
| 账号中心 | 真实/模拟账号登记、身份去重、账号资料与限额编辑、连接检查、停用及冷却状态 |
| 任务中心 | 跨平台批次、候选筛选、直接启动、文字和媒体清单、排期、暂停/取消、结果核对 |
| API 中心 | 平台接入目录、调用记录、错误类型、审计日志、OpenAPI |

任务使用持久化 `task_id`。SQLite 原子领取和唯一尝试记录防止重复提交；进程中断或结果未知时保留状态，通过只读核对恢复。

| 平台 | 适配器支持的动作 |
| --- | --- |
| Telegram | 公开目标发现、任务政策内入群、文字、回复、单文件、原生定时文字/文件 |
| Reddit | 公开帖子发现、文字帖子、文字回复 |
| Instagram | 自有账号媒体发布；第三方评论转为人工处理 |
| X | 话题发现、点赞、回复、转发、引用，以及任务明确开启的自有账号原创发布 |
| Discord | 已授权服务器的公开频道目录、文字、文件/图片、回复、本机定时发送与回执核对 |

这些是代码支持范围。真实可用性还取决于后端版本、登录身份、平台权限和任务启动。接入方式见 [平台接入说明](docs/platforms.md)。

## 快速开始

需要 **Python 3.11+**。使用工作台和内置后端不需要 Node.js。

Windows PowerShell：

```powershell
git clone https://github.com/anatanoojiisann/auto-promotion.git
cd auto-promotion
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
.\start.cmd seed-simulation
.\start.cmd serve --port 8780
```

安装后可双击 `start.cmd` 启动工作台。`start.cmd doctor` 检查环境；`start.cmd login telegram --account brand-telegram` 配置本机 Telegram。六个平台的账号配置和真实验收见 [平台接入说明](docs/platforms.md)。

macOS / Linux：

```sh
git clone https://github.com/anatanoojiisann/auto-promotion.git
cd auto-promotion
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[all]'
python -m playwright install chromium
auto-promotion seed-simulation
auto-promotion serve --port 8780
```

打开 **http://127.0.0.1:8780**。页面、后台 worker 和 API 使用同一份数据库。默认每 10 秒运行一轮；用 `serve --no-worker` 可以关闭后台调度。

也可使用 `./start.sh`，参数与 `auto-promotion` 相同。它优先使用 `AUTO_PROMOTION_PYTHON`，其次项目 `.venv`，最后使用系统 `python3`。

新数据库默认开启全局停止。演示流程：

1. 选择带“模拟”标签的账号，创建任务并填写发现范围。
2. 搜索或录入候选，编辑目标、正文、媒体和计划时间。
3. 点击“开始发送”，系统自动固定清单并执行；需要先入群时等待核验后自动继续。
4. 全局停止开启时先恢复全局执行。模拟回执以 `simulation://` 开头。

也可以在 Codex 对话中明确要求“开始发送任务 AP-…”或“开始互动”，由 MCP
`start_task` 工具执行同一路径。只要求创建、编辑或预览不会启动发送。

## 配置

项目自带覆盖六个平台的 MCP 服务，`install.ps1` 会安装并检查连接。
客户端配置、新电脑接入与工具说明见 [MCP 安装说明](docs/mcp.md)。

可复制 `.env.example` 为 `.env`。CLI 读取**当前工作目录**中的 `.env`，已有环境变量优先；不会执行 shell 展开。数据路径相对当前工作目录解析。

| 配置 | 默认值 / 用途 |
| --- | --- |
| `AP_DB` | `var/auto-promotion.db` |
| `AP_API_TOKEN` | 可选；缺省时自动生成 `var/operator.key` |
| `AUTO_PROMOTION_LIVE_WRITES` | `0`，关闭真实外发 |
| `AP_ACCOUNTS_DIR` | 数据库同级 `accounts/`；本机私密账号、会话、浏览器及回执 |
| `AP_<PLATFORM>_BACKEND` | `builtin`；只有明确设为 `legacy` 才调用旧工程 |
| `AUTO_PROMOTION_WORKSPACE` | 当前工作目录的父目录；仅用于旧工程兼容模式及历史索引 |
| `AP_<PLATFORM>_OWNER` | 缺省为空；接管后设为 `auto-promotion` |
| `AP_<PLATFORM>_PYTHON` | 仅旧工程兼容模式使用；默认按操作系统选择解释器路径 |

`PLATFORM` 为 `TELEGRAM`、`REDDIT`、`INSTAGRAM`、`X` 或 `DISCORD`。账号中心只保存标签；`login` 命令在本机私密配置中保存登录身份，标签必须与网页登记一致。密钥和令牌不在页面展示。

## 执行规则

- 通过 Codex 的明确发送／互动指令，或客户端“开始发送”按钮启动任务；不再需要两阶段审批或确认短语。草稿、导入和预览不会自动发送。
- 启动时自动固定清单，绑定账号、目标、完整内容、媒体哈希、计划时间、顺序及限额；变更已固定清单需要新任务。
- 任务最小间隔跨账号生效。创建任务时可选择严格按清单顺序，或让各账号独立执行；后一方式仍保持同一账号内的顺序。执行方式属于任务范围，冻结后不可更改。
- 清单编辑器可在同平台健康账号间轮流分配，也可按已经通过资格筛选的目标匹配账号；分配后的完整账号、目标和时间仍在发送清单中展示。
- 每项只尝试一次。提交后断线或超过 300 秒的执行租约记为 `unknown`，不自动重发；核对未得到确证时继续保留未知状态。
- `scheduled` 表示 Telegram 已接受并核验排期，**不等于已经投递**。暂停或取消本地任务不会撤回平台已接受的排期。
- 账号、任务、用户、目标和全局限额共同生效；日额度按 UTC 自然日计算，页面显示北京时间。原生排期也预留投递日额度。普通任务超过计划时间 15 分钟后标记 `missed`。
- 真实写入需要本机登录、连接检查、执行配置和有效任务启动同时成立。停止、冷却与各级限额继续生效。
- 同一平台身份只能绑定一个真实账号记录；账号显示名和限额可编辑，但未结束任务正在使用的账号须先结束旧任务。连接进度在重启后保留，中断的登录需要重新连接。

这是单机 SQLite MVP，不承诺跨外部平台的严格 exactly-once。多人租户、完整反馈归因、持续自动对话、X 原创帖/Thread 均不在本版范围。

执行诊断记录浏览器最后执行步骤、步骤开始时间、是否已开始提交，以及脱敏后的异常类型和项目内调用栈。调用栈只包含源码文件名、函数名和行号，不保存异常原文、页面正文、Cookie 或局部变量。发布回执会先持久保存；诊断更新不会覆盖它，未知结果仍不自动重发。API 中心将未知结果和错误类型单独显示，不再把后端正常返回的未知结果显示为调用成功。

发送进程每 10 秒写入独立心跳，超过 60 秒未更新显示“发送心跳超时”；正常退出保存原因和退出时间。任务中心分别显示任务状态和进程执行状态，数据库 `active` 不再作为进程在线的依据。后台服务和 `worker-once` 均接入监测；专用批次脚本使用父进程监督，可记录子进程被终止时的实际退出码。如果整个应用或电脑一起停止，恢复查看时只能确定心跳超时，不能据此编造操作系统终止原因。

## API 与命令行

### 互动统计与报告

“互动统计”汇总全部真实账号和任务，模拟数据不进入真实效果统计。成功发布必须有已核验的外部引用；当日归属使用平台实际发布时间，缺失时间的已确认记录单独列出，不用计划时间或复核时间代替。已排期、未知、失败、取消各自保留状态。

收到回复总条数按回复发生时间统计；“有回复的互动”按原互动实际发布时间统计，仅计算他人直接回复。点赞和转发不进入回复率分母。尚未采集、分页尚未完成、采集失败或覆盖不到报告截止时间时，回复指标为 `null`（页面显示“不可统计”），另列已观察到的数量。

工作台报告调度独立于外发停止开关：每三小时（北京时间 00/03/06/09/12/15/18/21 点）保存区间和当日累计；每天 20:00 保存截至当时的当日累计，次日 00:00 保存前一天完整自然日。报告按类型和截止时间去重，数据库保存补跑进度。迟到核验和回复记录会修订原报告，保留版本号。长时间离线每轮补跑最多 31 天，后续轮次继续。电脑和服务需要保持运行，离线期间恢复后补跑。

Telegram 文本原生排期增加投递后只读核验：在计划时间附近查找唯一、内容相同且由本账号从排期发送的消息，再独立读取实际消息 ID。找不到、匹配不唯一、扫描截断时保留待核验状态；文件排期暂需更强的回执映射。已确认的 Telegram 消息按保存的游标分页读取群消息，仅保留直接回复的标识和时间，排除本账号回复，不保存回复正文。每个三小时、20:00 和午夜边界自动采集一次，遵守账号冷却。X、Reddit、Instagram、Discord 的回复采集尚未接入，页面明确显示缺少数据。

```sh
auto-promotion statistics --day 2026-10-12
auto-promotion statistics --all
auto-promotion report-once
auto-promotion collect-statistics
auto-promotion serve --reports-only
```

`report-once` 仅根据本地证据生成报告；`collect-statistics` 只读取 Telegram 实际投递和回复。`--reports-only` 启动工作台及报告调度、Telegram 只读采集，不运行外发调度。报告保存在本机工作台；这些命令不会创建或恢复 Codex 聊天通知任务。从项目父级工作目录启动时，默认使用 `auto-promotion/var/auto-promotion.db`，显式 `AP_DB` 或 `--db` 仍优先。

统一接口：`GET /api/statistics?day=YYYY-MM-DD`、`GET /api/statistics/reports`、`POST /api/statistics/run`、`POST /api/items/{item_id}/feedback`。MCP 提供只读 `interaction_statistics` 和 `statistical_reports`，与页面、命令行共用统计逻辑。

- Swagger：`http://127.0.0.1:8780/docs`
- OpenAPI：`http://127.0.0.1:8780/openapi.json`
- 健康检查：`GET /health`
- 业务接口：`/api/accounts`、`/api/tasks`、`/api/services`、`/api/calls`、`/api/audit`

API 使用 Bearer token；本机页面使用 HttpOnly 会话。服务仅监听回环地址，并检查 Host、Origin 和写入请求来源。不要直接暴露到公网。

```sh
auto-promotion --help
auto-promotion init
auto-promotion check-accounts --live-only
auto-promotion worker-once
auto-promotion import-workspace-accounts
```

历史导入仅读取已知任务表或 JSON 中的账号标签；不会读取凭据表，也不会接管旧任务。

## 开发与测试

```sh
python -m pip install -e '.[dev,telegram,mcp]'
ruff check src tests
ruff format --check src tests
python -m unittest discover -s tests -v
python -m build
```

本仓库包含独立 Python 单元与契约测试，不依赖相邻工程或真实账号。开发安装可使用 `python -m pip install -e '.[all,dev]'`，额外验证内置 Telegram 传输。网页浏览器验收需要 Node.js 20+：

```sh
npm ci
npx playwright install chromium
npm run format:check
npm run test:ui
```

浏览器脚本自行启动临时服务器和模拟数据库，检查三个中心、直接启动、执行回执及移动布局，并在结束后清理临时数据。不会操作日常工作台数据库。设置 `AP_UI_SKIP_SCREENSHOTS=1` 可跳过截图。

验证范围和限制见 [测试说明](docs/testing.md)。GitHub Actions 自动执行 Python 检查、打包和浏览器测试。

## 项目结构

```text
src/auto_promotion/
  api.py, cli.py         HTTP 与命令行入口
  core.py, store.py      任务状态、启动、执行、SQLite
  models.py, config.py   校验模型与统一配置
  adapters.py           模拟与真实适配器选择
  native_bridge.py      隔离调用内置后端、启动状态复核、持久化回执
  backends/             Telegram、Reddit/X 浏览器、Instagram、Discord Bot 后端
  diagnostics.py        本机运行环境检查
  provider_bridge.py    旧工程兼容模式，默认不启用
  legacy.py             历史任务只读导入
  web/                  随 Python 包分发的中文界面
integrations/           Telegram / X 可选兼容补丁
tests/                  独立单元与契约测试
scripts/ui_test.cjs     隔离浏览器验收
install.ps1, start.cmd  Windows 安装与启动入口
docs/                   平台接入与测试说明
```

`.env`、密钥、数据库、媒体、缓存、截图和日志均被 Git 忽略。仓库只分发源码、测试、配置模板和文档。


TikTok 的授权配置、发布与自有评论管理、独立回调和真实验收见 [TikTok 接入说明](docs/tiktok.md)。

六个平台的持久任务、按平台和接入方式划分的执行池、Cloudflare 部署与常驻节点配置见 [持久化执行服务](docs/cloudflare-execution.md)。工作台“推广活动 → 调度与额度 → 发送通道”可查看通道积压、核验状态和并发设置。

任务和模板支持单独选择在线或本地队列，交接状态见 [双通道执行](docs/execution-channels.md)。新增的 [云端活动工作流](docs/complete-cloud-workflow.md) 保存分阶段证据和依赖；[Telegram 专用云端执行器](docs/telegram-cloud-executor.md) 提供独立登录、只读采集和受原生队列保护的执行适配。研究评估、回复审核模型及原生子任务生成编排尚未接齐，不能把工作流入队当作完整推广已运行。
