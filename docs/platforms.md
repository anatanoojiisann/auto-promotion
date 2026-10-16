# 内置平台接入

六个平台的实现随 Python 包一起分发，默认使用本项目的解释器；不需要相邻工程、旧电脑、旧数据库、MCP 服务或 CDP 服务。模拟模式仍不需要登录任何账号。

## Windows 安装与启动

从仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
.\start.cmd doctor
.\start.cmd seed-simulation
.\start.cmd serve --port 8780
```

安装程序创建项目 `.venv`，安装 `.[all]` 和 Playwright Chromium，首次复制 `.env.example`，初始化 SQLite。不会启用真实写入或登录账号。可用 `-SkipBrowser` 暂时跳过浏览器下载，仅验收工作台和 Telegram。

打开 http://127.0.0.1:8780。`start.cmd` 无参数时也会启动工作台。`doctor` 只检查解释器、依赖、时区和 Chromium，不读取账号凭据，也不联系社交平台。

macOS / Linux 可使用 `python3 -m venv .venv`、`.venv/bin/python -m pip install -e '.[all]'`、`.venv/bin/python -m playwright install chromium`，再使用 `./start.sh`。

## 私密账号配置

每个平台账号使用一个固定本地标签，例如 `brand-telegram`。在 CLI 登录时的 `--account` 与网页“平台账号标签”必须完全一致。

账号配置、Telegram session、独立浏览器资料和传输回执默认保存在数据库同级的 `accounts/<平台>/<标签哈希>/`，均处于忽略提交的 `var/` 中。不要把该目录加入 Git。可通过 `AP_ACCOUNTS_DIR` 指定其他本机私密目录；如改变路径，应迁移完整资料并重新检查账号。

更换账号身份时使用新标签并登记新账号，不能把原账号的批准批次用于另一身份。登录命令不会导入旧任务，也不会发送推广消息。
同一个真实平台身份不能用不同标签重复登记；连接检查会阻止重复账号投入执行。已有账号更新凭据时沿用原标签。账号中心可修改显示名和限额，正在被未结束任务使用的账号不能修改。

## Telegram（优先完成的内置后端）

```powershell
.\start.cmd login telegram --account brand-telegram
```

第一次运行会隐藏输入 API ID、API hash、手机号、登录验证码和两步密码。在 [my.telegram.org](https://my.telegram.org) 的 API Development Tools 取得自己的应用凭据。网络需要代理时，交互输入 SOCKS5 的 `host:port`；留空使用直连。API 凭据与会话仅保存本机，不回传网页。

然后在网页登记一个 Telegram 真实账号，“平台账号标签”填写 `brand-telegram`，点击只读连接检查。验证的是登录会话中的稳定用户 ID，不接受机器人账号或身份不匹配的会话。

内置能力包括公开群搜索与证据筛选、批准政策内入群、只读成员检查、纯文本、回复、单文件、原生定时文字/文件，以及只读消息核对。公开群搜索采集简介、成员数和最近24小时的消息样本；无法证明语言、活跃度或置信度时保守拒绝，可通过候选录入补全证据。英文不能仅凭拉丁字母自动判定。

每次执行使用独占账号锁。Telegram 推广执行的自动请求重试、断线重连和 FloodWait 自动睡眠被关闭；中央调度负责冷却。登录阶段允许有限的数据中心迁移处理和断线恢复，等待人工输入不会阻塞网络保活。提交后先保存消息 ID，再独立读取消息或排期列表核验。已提交但无法验证的结果保持 `unknown`，不会自动重发。排期消失不能单凭消失判定已投递，需人工核对。管理员审核中的入群请求保持 `pending_admin_approval`。

## Reddit / X

X 也可以直接在网页的“账号中心”点击“连接 X”，填写平台用户名，选择 Chromium 或已安装的 Microsoft Edge，然后在弹出的独立浏览器中完成登录。网页显示登录进度，核验通过后自动登记真实账号。浏览器选择与登录资料一起保存，后续检查和执行使用同一浏览器；不同浏览器使用独立资料目录。等待登录最多15分钟，可在网页取消；重新登录已有账号时填写原来的本机账号标签。此流程只保存登录与检查身份，不开启真实发布。

```powershell
.\start.cmd login reddit --account brand-reddit
.\start.cmd login x --account brand-x
```

输入平台用户名，在弹出的独立 Chromium 中手动完成登录，回到终端按 Enter。程序核对当前身份后保存资料。每个标签有独立浏览器目录，不连接日常浏览器，不依赖 CDP 地址或外部动作服务。

Chrome Browser Use 账号可登记到号池供交互操作使用。先通过 Browser Use 同时读取 X 的 Account menu 和 Profile 链接，保存一致的用户名、浏览器实例、标签页及观察时间到该账号的本机配置（`browser_channel=browser_use`、`browser_family=chrome`、`browser_instance_id`、`browser_tab_id`、`browser_use_observation`）。核验记录仅接受最近5分钟的真实观察；超时后需要重新通过 Browser Use 核对身份并更新记录。此模式不导出登录凭据，也不提供后台执行或采集；`automatic_actions` 始终为空，执行前检查返回 `BrowserUseInteractiveSessionRequired`。Chrome 扩展连接不等于后台自动化连接。

X 也支持绑定已登录的 Tabbit 会话。本机账号配置使用 `browser_channel=tabbit`，绑定 `identity`、`tabbit_instance` 和可选的 `tabbit_tab_id`；通过稳定 `tabbit-cli` 核对账号菜单与个人资料链接，不导出 Cookie。原标签页在核验后保留，切换账号会使核验失败。连接默认只读；明确要求启用自动回复后，可在该账号的私密配置中设置 `automatic_actions: ["reply"]`。Tabbit 自动回复目前仅支持文字，先准备正文，再重新验证执行授权，最后提交一次并核对帖子作者、正文、回复关联及发布时间。

Reddit 仍采用人工清单。X 默认采用人工清单；单独启用 `automatic_actions: ["reply"]` 的账号可以自动回复公开帖子，同时仍须启用 X 执行器、明确启动任务并通过身份、目标、正文、间隔、限额及停止状态检查。未启用账号保持人工模式，自动点赞、转发和引用不随自动回复启用。改变冻结清单的执行方式须新建批次，不能修改旧清单。

自动回复通过已登录网页提交并读取回执，没有接入 X 官方发帖 API。页面变化可能需要更新控件与回执核验；提交结果未知时不能重发。未实际提交的人工交接不占实际发送额度，取消后可在新批次重新安排；历史清单和记录保留。号池的自动/人工执行范围见 [号池说明](account-pools.md)。

## Instagram

网页“账号中心”提供“连接 IG”：填写平台用户名、Instagram 用户访问令牌和 Meta 应用的 API 版本。令牌只通过受保护的本机接口提交，不写入公开连接状态、校验错误或审计记录。程序核对令牌所属用户名和稳定用户 ID 后保存私密配置，并自动登记与检查真实账号；身份不匹配不会保存。核验成功后连接状态会显示自有发布目标 ID。
多个 Instagram 账号可分别使用不同标签连接和发布；每个发布项必须指向对应账号已核验的自有目标 ID。

使用 **Instagram API with Instagram Login** 的用户访问令牌，账号须具备专业账号的相应能力与发布权限。此实现不接受旧 Meta 服务的操作令牌。

```powershell
.\start.cmd login instagram --account brand-instagram
```

隐藏输入用户访问令牌，并输入 Meta 应用使用的 API 版本（例如 `v25.0`，实际以应用支持版本为准）。程序通过 `/me` 只读验证稳定用户 ID 和用户名，成功后显示可填写为目标的 `instagram_user_id`。网页登记账号时使用同一个本地标签；发布目标填写该 ID，并勾选自有目标。

支持单图、视频/Reels、轮播和发布结果核验。媒体须提供平台能获取的公共 HTTPS 来源 URL，正文最多2200字符。创建容器、等待处理、发布和独立读取自有媒体都在项目内完成。第三方评论仍为人工登记。令牌的取得、续期及应用权限由 Meta 账号配置负责，不能用代码代替平台授权。

官方参考：[Meta 的 Instagram API 集合](https://www.postman.com/meta/workspace/instagram/documentation/23987686-9386f468-7714-490f-9bfc-9442db5c8f00)。

## Discord（官方 Bot）

第一版支持 Bot 已获授权服务器中的普通文字频道和公告频道：文字、最多 10 个文件/图片、普通文字频道内的消息回复、本机定时发送和只读回执核对。频道必须对该服务器的 `@everyone` 角色可见；私密频道、NSFW、私信、论坛和 Thread 暂不支持。此处的公开指服务器成员可见，不代表服务器本身可被全网搜索。不会搜索或自动加入其他服务器。

1. 在 [Discord 开发者后台](https://discord.com/developers/applications) 创建应用，从 Bot 页面生成 Bot Token。
2. 在 Installation 配置 `Guild Install` 和 `bot` scope，由拥有服务器管理权限的人使用安装链接添加 Bot。需要 `View Channels`、`Send Messages`、`Read Message History`；文件/图片还需 `Attach Files`。当前版本不使用 Slash Commands、Gateway 或 Message Content intent。
3. 网页账号中心点击“连接 Discord”，在本机输入 Token 和固定标签，例如 `brand-discord`。工作台通过 `/users/@me` 核验 Bot 身份，自动登记账号。同一标签只能绑定同一个 Bot；Token 更新可以复用原标签。
4. 创建任务并批准发现政策，编辑执行项，选择 Discord 账号，点击“读取已授权频道”后选择频道。频道目录为只读，不发送测试消息；若任务设置了目标白名单，仅显示白名单内频道 ID。
5. 选择“发送文字”“发送文件”或“回复”。回复填写完整 **Discord 原消息 ID**（开启 Discord Developer Mode 后复制 Message ID）。上传的文件绑定本机哈希；单文件上限 10 MiB，附件总计上限 25 MiB，正文最多 2000 字符。这是本工作台的保守上限；平台仍可能根据实际服务器限制拒绝文件。
6. 冻结并核对完整外发清单，输入第二阶段精确批准短语。按需配置下方真实执行开关，重启服务，恢复全局执行。定时发送由本机 worker 到期执行，需要电脑和工作台保持运行；这不是 Discord 原生排期。

命令行也可隐藏输入 Token：

```powershell
.\start.cmd login discord --account brand-discord
```

Token 仅保存到私密 `accounts/discord/<标签哈希>/credentials.json`，不进入公开连接状态、验证错误或审计日志。CLI 配置后在网页用相同标签登记真实 Discord 账号。Discord 没有旧工程兼容后端，保持 `AP_DISCORD_BACKEND=builtin`（默认）。

每次发送前重查 Bot 身份、频道权限、原回复消息和批准清单；默认关闭所有提及通知，包括 `@everyone`、角色、用户和回复通知。收到消息 ID 后立即持久化，再独立读取并检查作者、频道、正文、附件名称/大小及回复关联。核验失败保持 `unknown`，不会自动重发；“只读核对结果”仅查询原消息。附件不下载到本机做二次哈希核验。限流记录账号冷却，包括只读频道目录和发送前检查。

验收先完成 Bot 身份与频道目录只读检查，再由操作者为测试频道批准一个精确批次，核对消息链接与平台实际结果。自动测试使用合成 Bot 和模拟账号，不代表真实 Token、服务器权限或网络已通过验收。

官方参考：[Bot 设置](https://docs.discord.com/developers/quick-start/getting-started)、[频道权限](https://docs.discord.com/developers/topics/permissions)、[消息接口](https://docs.discord.com/developers/resources/message)、[限流](https://docs.discord.com/developers/topics/rate-limits)。

## 真实执行开关与验收

先完成账号只读检查，再在 `.env` 中为需要执行的平台设置：

```dotenv
AUTO_PROMOTION_LIVE_WRITES=1
AP_TELEGRAM_OWNER=auto-promotion
# 其他平台按需启用：
# AP_REDDIT_OWNER=auto-promotion
# AP_INSTAGRAM_OWNER=auto-promotion
# AP_X_OWNER=auto-promotion
# AP_DISCORD_OWNER=auto-promotion
```

重启工作台后，为每个真实批次完成原有的两阶段精确审批，并恢复全局执行。配置开关不替代审批；后端在实际提交前会从中央数据库再次校验批准清单、已领取执行项、账号、时间窗口、停止开关与媒体哈希。账号锁和持久化回执防止同一项目重复提交。

如仍保存原四个工程，可显式选择 [旧工程兼容模式](legacy-platforms.md)，默认独立运行不会读取它们。


TikTok 的授权配置、发布与自有评论管理、独立回调和真实验收见 [TikTok 接入说明](tiktok.md)。
