# TikTok 接入与验收

本实现使用 TikTok for Business **Organic API / Accounts API v1.3**。代码已接入号池、任务清单、人设规划、配额、排期、单次提交、持久化回执和日志导出。离线测试使用接口替身，不能代替 TikTok 权限审批和真实账号验收。

## 支持范围

- 逐个账号 OAuth 授权；一次性 state、十分钟有效期、身份绑定、令牌自动续期。刷新令牌过期或授权撤销后重新连接。
- 发布单个视频，或一组 JPEG/WebP 图片（本工作台一项最多十个媒体）。视频通过该接口公开发布；图文按账号允许的可见范围发布。
- 关闭评论、合拍和拼接；自有品牌推广、付费合作标记；视频 AI 标记与封面帧；图文封面序号和推荐音乐。
- 读取自有内容；读取并分页导入自有视频下的评论；发布评论、回复、点赞／取消点赞、隐藏／恢复评论。删除只支持自己发表的评论。
- 查询发布状态并核对自有内容；记录处理中、成功、失败和未知结果。后台每三分钟核对一批未决项目，不重新提交。
- 独立 OAuth 回调与 Webhook 接收器；应用级 VIDEO、COMMENT 通知注册、签名验证、五分钟时间偏差限制、重复事件去重。通知仅作为更新线索，发布成功仍查询官方接口确认。评论通知会记录，不会自动回复；评论需从“内容与评论”读取、审核并准备新任务。
- 按已授权 scope 读取账号基本资料及可选的累计统计；缺失指标保持未知。

陌生视频下的批量点赞、评论、关注及私信不属于此实现的支持范围。

## TikTok 后台准备

1. 创建 TikTok for Business 开发者应用，申请 Accounts API / TikTok Accounts 权限并完成官方接入申请。
2. 申请并让每个账号授权：`user.info.basic`、`user.info.username`、`video.list`、`video.publish`、`comment.list`、`comment.list.manage`。按实际用途申请，发布和评论可分别开通。累计粉丝、关注、获赞、内容数等统计另外需要 `user.info.stats`。
3. 配置公网 HTTPS **TikTok 账号持有人重定向 URL**，例如 `https://your-domain.example/oauth/tiktok/`。必须与后台完全一致，以 `/` 结尾，不带端口、查询参数或 fragment。
4. 从应用详情复制 **TikTok account holder authorization URL**，不要使用广告主授权链接或另一开发者平台的凭据。应用 ID、回调地址和权限须对应同一应用。
5. 将素材放到平台能访问的 HTTPS 地址，并在 TikTok 完成 URL 前缀／域名所有权验证。工作台的素材前缀配置只是本地边界检查，不会替你完成验证。

## 在工作台连接

重启工作台加载新代码，进入“账号中心 → 连接 TikTok”。填写 App ID、App secret、回调地址、后台提供的授权 URL 和已验证素材前缀，每行一个前缀且以 `/` 结尾。

输入账号用户名和固定本机标签，生成链接后在 TikTok 完成授权。部署了独立接收器时，账号自动登记；未部署接收器时，在回调页面从地址栏复制完整地址，粘贴到本机工作台的“授权完成后的完整回调地址”。代码和 state 只在这次连接中使用，不进入账号目录和审计日志。

应用密钥和令牌保存在配置的私密账号目录中，不应放入共享内容、人设、任务正文或代码仓库。单个标签绑定固定平台身份；换账号使用新标签。连接账号和保存应用配置不会启用真实发布。

## 回调接收器

在与工作台共享私密账号目录和数据库的可信主机上运行：

```powershell
.\start.cmd tiktok-callback --port 8781
```

该进程仅监听 `127.0.0.1:8781`，由 HTTPS 反向代理转发授权回调路径和 `/webhooks/tiktok`。访问日志关闭，避免记录 URL 中的一次性授权码。该服务不暴露工作台管理接口，不运行发送任务。工作台主服务继续只监听本机。

接收器部署后，在“连接 TikTok → 发布和评论回调”分别注册发布结果和评论更新通知。注册调用会更新 TikTok 应用的回调配置；地址固定为授权回调同域名的 `/webhooks/tiktok`。如 TikTok 应用先前限定了 COMMENT 的 `item_list`，应在官方后台核对订阅覆盖范围；本界面不声称能清除既有过滤。

Webhook 只保存事件信封中的必要标识，不保存完整评论正文。发布回执轮询由工作台后台报告进程执行；仅运行回调接收器不会轮询。`--no-worker` 会同时关闭默认后台进程，可手动点击“只读核对结果”。

## 准备与启动任务

1. 在号池中选择已连接的 TikTok 账号。推广活动需要先配置账号人设、语言和工作时段；普通任务也可直接选择多个账号。
2. 发布动作选择“发布媒体”，目标填写该账号的自有目标 ID（界面自动带入），勾选自有目标。上传本机媒体绑定哈希，填写对应的公共 HTTPS 素材地址。网页单文件最多 50 MiB；图片额外限制 20 MiB。媒体格式、混合类型、文案、封面、URL 边界在本地检查；时长、分辨率和最终平台处理以 TikTok 返回结果为准。
3. 图文必须选择可见范围。视频文案最多 2,200 UTF-16 code units；图文标题最多 90、文案最多 4,000。品牌合作和 AI 标记按实际内容填写。
4. 评论动作同时填写自有账号 ID、视频／图文 ID 和目标评论 ID。创建新评论不填评论 ID。评论首版仅支持文字，保守限制为 1,200 UTF-8 字节。没有正文的管理动作不能附加正文、标题或媒体。
5. “账号中心 → 内容与评论”可以分页读取自有内容，选择目标并读取、导入评论。自有评论会被跳过；未识别语言保持 `unknown`，规划前应审核语言、人设和精确回复内容。导入不启动互动。
6. 核对清单后明确点击“开始任务”，或在对话中要求启动指定任务。仍须满足全局停止状态、执行器开关、账号健康、配额、冷却和时间窗口。不会新增批准短语流程。

真实执行还需要既有 `AUTO_PROMOTION_LIVE_WRITES=1` 和 `AP_TIKTOK_OWNER=auto-promotion` 配置。本次开发不会改变这些开关。跨账号轮流分配发布时会同步自有目标；评论保持原账号归属。

## 回执与限制

- 发布返回 `share_id` 后立即持久化，再以该值作为 `publish_id` 查询 `/business/publish/status/`。接口返回“完成”而尚无公开 post ID 时，继续等待，不报告成功。
- 公开内容须同时通过完成状态、自有内容 ID 和文案读取核对。私密或好友可见图文可能没有公开 post ID，因此会保持待核验／未知，不能据此重新提交。
- 评论回复核对评论 ID、所属视频、原评论关联、正文和 owner 标记。点赞、隐藏、删除分别读取对应状态。任何提交后的读取失败都保持未知；只读核对不会再次调用写接口。
- 权限会在运行时重新核对；令牌身份或应用不匹配、非自有目标、权限不足都会阻止提交。限流持久化账号冷却。平台错误只输出安全类型，不输出原始凭据或响应正文。
- 本机排期要求电脑与发送进程运行。现有账号、目标、收件人及任务配额仍会限制实际数量；不能将创建批次数量当作发布成功数量。

## 本地接口

| 路径 | 用途 |
| --- | --- |
| `GET /api/tiktok/app` | 读取非敏感应用配置 |
| `PUT /api/tiktok/app` | 在本机保存应用配置 |
| `POST /api/tiktok/authorize` | 生成有时效的账号授权链接 |
| `POST /api/tiktok/authorize/complete` | 完成一次性回调授权 |
| `POST /api/tiktok/webhooks` | 显式注册 VIDEO 或 COMMENT 回调 |
| `POST /api/tiktok/accounts/{account_id}/videos` | 分页读取自有内容 |
| `POST /api/tiktok/accounts/{account_id}/comments` | 分页读取并可选导入评论 |
| `POST /api/tiktok/accounts/{account_id}/insights` | 读取可用账号统计 |

以上路径都使用工作台现有本机鉴权和跨站请求边界。内容发送继续使用任务 API，未提供可绕过任务的直接发送接口。`login tiktok` 命令提示使用工作台 OAuth；不会误用 X／Reddit 浏览器登录。

MCP 新增 `tiktok_configuration_status`、`tiktok_list_videos`、`tiktok_read_comments`、`tiktok_account_insights`。发送使用已有 `create_task`、`set_task_items`、`start_task` 和 `verify_task_item`。MCP 服务重启后更新工具目录。

## 验收

离线覆盖包括一次性授权、防跨站和错误账号、令牌轮换、最新权限检查、素材边界、视频／图文不同请求结构、评论归属与 owner、异步发布、回执读取失败、禁止重发、Webhook 验签与去重、日志脱敏，以及任务从草稿到明确启动的流程。页面以隔离数据库和模拟账号验证，不触发真实平台写入。

拿到已获批应用后，依次连接一个测试账号、只读核对身份和视频、准备一个精确发布任务、明确启动、核对平台帖子与回执，再测试自有视频下的一条评论回复。扩展号池之前完成该真实验收。

官方依据：[Accounts API 授权](https://business-api.tiktok.com/portal/docs/accounts-api-authorization/v1.3)、[令牌管理](https://business-api.tiktok.com/portal/docs/accounts-api-authentication/v1.3)、[视频发布](https://business-api.tiktok.com/portal/docs/publish-a-public-video-post-to-an-owned-account/v1.3)、[图文发布](https://business-api.tiktok.com/portal/docs/publish-a-photo-post-to-an-owned-account/v1.3)、[发布状态](https://business-api.tiktok.com/portal/docs/get-the-publishing-status-of-a-tiktok-post/v1.3)、[Webhook 验签](https://business-api.tiktok.com/portal/docs/webhook-verification/v1.3)。
