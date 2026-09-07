# Auto Promotion

一个面向 Telegram、Reddit、Instagram、X 的本机推广工作台，将账号、任务审批、调度和执行记录集中在同一个入口。

**应用和模拟流程可以独立运行。真实平台操作通过可选后端接入，默认关闭。** 本仓库包含统一控制层与兼容补丁，不包含第三方后端、平台账号或运行数据。

## 功能

| 模块 | 能力 |
| --- | --- |
| 账号中心 | 真实/模拟账号登记、能力目录、只读身份与连接检查、停用及冷却状态 |
| 任务中心 | 跨平台批次、候选筛选、两阶段精确审批、文字和媒体清单、排期、暂停/取消、结果核对 |
| API 中心 | 平台接入目录、调用记录、错误类型、审计日志、OpenAPI |

任务使用持久化 `task_id`。SQLite 原子领取和唯一尝试记录防止重复提交；进程中断或结果未知时保留状态，通过只读核对恢复。

| 平台 | 适配器支持的动作 |
| --- | --- |
| Telegram | 公开目标发现、批准政策内入群、文字、回复、单文件、原生定时文字/文件 |
| Reddit | 公开帖子发现、文字帖子、文字回复 |
| Instagram | 自有账号媒体发布；第三方评论转为人工处理 |
| X | 话题发现、点赞、回复、转发、引用 |

这些是代码支持范围。真实可用性还取决于后端版本、登录身份、平台权限和任务批准。接入方式见 [平台接入说明](docs/platforms.md)。

## 快速开始

需要 **Python 3.11+**，以下命令适用于 macOS / Linux。使用工作台不需要 Node.js。

```sh
git clone https://github.com/anatanoojiisann/auto-promotion.git
cd auto-promotion
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
auto-promotion seed-simulation
auto-promotion serve --port 8780
```

打开 **http://127.0.0.1:8780**。页面、后台 worker 和 API 使用同一份数据库。默认每 10 秒运行一轮；用 `serve --no-worker` 可以关闭后台调度。

也可使用 `./start.sh`，参数与 `auto-promotion` 相同。它优先使用 `AUTO_PROMOTION_PYTHON`，其次项目 `.venv`，最后使用系统 `python3`。

新数据库默认开启全局停止。演示流程：

1. 选择带“模拟”标签的账号，创建任务并填写发现政策。
2. 核对范围，输入页面显示的 `discovery_join` 完整确认短语。
3. 搜索/录入候选，编辑目标、正文、媒体和计划时间。
4. 冻结清单，核对每项内容，再输入 `outbound` 完整确认短语。
5. 恢复全局执行，等待到期或点击“运行一轮”。模拟回执以 `simulation://` 开头。

## 配置

可复制 `.env.example` 为 `.env`。CLI 读取**当前工作目录**中的 `.env`，已有环境变量优先；不会执行 shell 展开。数据路径相对当前工作目录解析。

| 配置 | 默认值 / 用途 |
| --- | --- |
| `AP_DB` | `var/auto-promotion.db` |
| `AP_API_TOKEN` | 可选；缺省时自动生成 `var/operator.key` |
| `AUTO_PROMOTION_LIVE_WRITES` | `0`，关闭真实外发 |
| `AUTO_PROMOTION_WORKSPACE` | 当前工作目录的父目录；仅用于可选后端及历史索引 |
| `AP_<PLATFORM>_OWNER` | 缺省为空；接管后设为 `auto-promotion` |
| `AP_<PLATFORM>_PYTHON` | 可选后端解释器；缺省为该后端 `.venv/bin/python` |
| `AP_INSTAGRAM_ACCOUNT` | 允许发布的 Instagram 用户名；缺省为空，禁止发布 |

`PLATFORM` 为 `TELEGRAM`、`REDDIT`、`INSTAGRAM` 或 `X`。账号中心只保存平台账号标签，登录与 OAuth 由原后端负责。

## 执行规则

- 每个任务最多两次精确审批：“发现与入群政策”和“最终外发清单”。“好的”“继续”不能代替确认短语。
- 清单绑定账号、目标、完整内容、媒体哈希、计划时间、顺序及限额。冻结后如需修改，应关闭旧任务并创建新任务。
- 每项只尝试一次。提交后断线或超过 300 秒的执行租约记为 `unknown`，不自动重发；核对未得到确证时继续保留未知状态。
- `scheduled` 表示 Telegram 已接受并核验排期，**不等于已经投递**。暂停或取消本地任务不会撤回平台已接受的排期。
- 账号、任务、用户、目标和全局限额共同生效；日额度按 UTC 自然日计算，页面显示北京时间。原生排期也预留投递日额度。普通任务超过计划时间 15 分钟后标记 `missed`。
- 真实写入需要连接检查、执行接管配置和有效任务审批同时成立。接管前须停止同账号的旧 worker；所有适用的停止、冷却与更严格限额继续生效。

这是单机 SQLite MVP，不承诺跨外部平台的严格 exactly-once。多人租户、完整反馈归因、持续自动对话、X 原创帖/Thread 均不在本版范围。

## API 与命令行

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
python -m pip install -e '.[dev]'
ruff check src tests
ruff format --check src tests
python -m unittest discover -s tests -v
python -m build
```

本仓库有 **65 项独立 Python 测试**，不依赖相邻工程或真实账号。浏览器验收需要 Node.js 20+：

```sh
npm ci
npx playwright install chromium
npm run format:check
npm run test:ui
```

浏览器脚本自行启动临时服务器和模拟数据库，检查三个中心、精确审批、执行回执及移动布局，并在结束后清理临时数据。不会操作日常工作台数据库。设置 `AP_UI_SKIP_SCREENSHOTS=1` 可跳过截图。

验证范围和限制见 [测试说明](docs/testing.md)。GitHub Actions 自动执行 Python 检查、打包和浏览器测试。

## 项目结构

```text
src/auto_promotion/
  api.py, cli.py         HTTP 与命令行入口
  core.py, store.py      任务状态、审批、执行、SQLite
  models.py, config.py   校验模型与统一配置
  adapters.py           模拟与真实适配器选择
  provider_bridge.py    隔离调用现有平台实现
  legacy.py             历史任务只读导入
  web/                  随 Python 包分发的中文界面
integrations/           Telegram / X 可选兼容补丁
tests/                  独立单元与契约测试
scripts/ui_test.cjs     隔离浏览器验收
docs/                   平台接入与测试说明
```

`.env`、密钥、数据库、媒体、缓存、截图和日志均被 Git 忽略。仓库只分发源码、测试、配置模板和文档。
