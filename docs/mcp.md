# 本机 MCP 安装

本项目提供一个真正的 MCP 服务 `auto-promotion`，通过标准 stdio 协议连接
Telegram、Reddit、Instagram、X、Discord 六个平台的内置后端。
它不是旧电脑上的四个独立工程；无需寻找或安装那些旧工程。
使用[官方 Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)。

业务增长另提供目标设置、转化事件／商店指标导入、业务报告和回复待办6个工具；仍不会启动外发或解除停止。数据契约和真实接入边界见 [业务增长](business-growth.md)。修改服务代码后需要重启 MCP 服务才能加载新增工具。

## 新电脑安装

Windows 在项目目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

脚本安装平台依赖及 MCP、下载 Chromium，并通过真实 MCP 握手和只读调用检查。
配置生成在 `var/mcp-client-config.json`，可填入支持 stdio 的 MCP 客户端。
其中使用当前电脑的 Python 和项目绝对路径，不包含账号密码、令牌或会话。

macOS / Linux：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[all]'
.venv/bin/python -m playwright install chromium
.venv/bin/python -m auto_promotion.mcp_server --print-config
.venv/bin/python scripts/check_mcp.py
```

`-SkipBrowser` 仅跳过下载；如果本机没有项目需要的 Chromium，完整检查会报告未就绪。

## 接入 Codex

安装后使用项目 Python 的完整路径注册，例如 Windows：

```powershell
codex mcp add auto-promotion -- "$PWD\.venv\Scripts\python.exe" -m auto_promotion.mcp_server --project-dir "$PWD"
```

注册后，在 Codex MCP 设置中重新连接；若当前聊天未加载新服务，重启客户端或新建聊天。
其他客户端读取生成配置中的 `command` 和 `args`。服务由客户端按需启动，无需常驻另一进程。
参见 [OpenAI 官方 MCP 配置说明](https://developers.openai.com/codex/mcp)。

## 工具与账号

`authorize_local_channel(task_id, account_id, coordinate_once=true, source=...)`
只在用户明确请求该账号/任务的一次控制服务协调时调用。它复用已有凭证建立本机通道归属，
不启动草稿、不创建发送尝试、不操作浏览器、不解除其他账号的严格离线范围。
任务必须已经明确启动并选择 `local`；纯离线且缺少归属证明仍保持等待。
完整条件、工作台按钮及 API 流程见[执行通道](execution-channels.md)。加载新增工具及数据库
迁移需要在安全窗口正常升级服务；源文件修改本身不会刷新旧 MCP 进程。

真实 X 内容发现使用 `search_x_content(account_id, query, lookback_hours=12,
timeout_seconds=30, max_results=100)`，通过注册账号的统一只读后端读取一页，
不再使用临时浏览器脚本。结果包含原帖时间、读取预算及耗时；
`quality_status=context_review_required` 表示仍需核对正文和优先级，
不能把搜索命中当成P0/P1。429和验证阻断仍持久保留，覆盖不完整保持显式。
网页/API可调用 `POST /api/x/search`；真实X的任务发现入口同样接入此读取器。
`system_status.runtime` 返回MCP启动时PID和源码指纹，用于重新连接后验收。

任务工具另提供 `list_task_templates`、`save_task_template`、`create_task_from_template`、`task_evidence` 和 `task_review`。模板只复用设置，新任务不继承旧启动状态、目标或正文；证据只读取所选任务的关联记录。详情见 [固定工具](task-tools.md)。

号池工具包括 `list_account_pools`、`save_account_pool`、`get_account_profile`、`save_account_profile`；内容工具包括 `list_opportunities`、`import_opportunities`、`collect_telegram_content`。活动使用 `create_campaign`、`plan_campaign`、`get_campaign`、`list_campaigns`、`edit_campaign_draft`、`create_campaign_batches`、`populate_campaign_batch`。

这些工具只能规划、编辑本地草稿或只读采集，不会启动发送。`populate_campaign_batch` 只载入本地草稿，创建批次不会自动发送。旧 MCP 进程需要重新连接才能载入新工具目录。具体字段见 [号池说明](account-pools.md)。

服务提供状态、六个平台账号目录与连接检查、Discord 频道目录、任务草稿、候选发现、
候选筛选、清单编辑与冻结、明确启动后的入群/执行，以及回执与成员核对。

新增只读 `interaction_statistics`（默认当天，可指定 UTC+8 日期或全部日期）与
`statistical_reports`（保存的三小时、20:00 和完整自然日报告）。两者汇总全部真实任务，
模拟数据不进入真实效果统计；没有回复采集数据时返回 `null`，不能解读为零回复。

账号登录继续在[本机工作台或登录命令](platforms.md)中完成。
MCP 安装完成不代表每个平台账号已登录，也不代表具备全部发布权限。
服务读取项目 `.env` 并使用与工作台相同的数据库，不自动改变执行开关。
任务由客户端“开始发送”按钮或 Codex 对话中的明确执行指令启动。MCP `start_task` 自动载入活动清单、固定内容并执行；`start_joins` 仅启动入群。两者均保留全局停止、冷却、额度和排期检查。MCP 不开启真实写入或解除全局停止。
执行工具沿用任务完整性、限额、冷却、一次尝试和未知结果核对规则。

服务源码、安装脚本与依赖声明属于项目代码；提交到 GitHub 后同事才可拉取。
虚拟环境、生成的本机配置、账号会话及运行数据库不会上传。


TikTok 的授权配置、发布与自有评论管理、独立回调和真实验收见 [TikTok 接入说明](tiktok.md)。
