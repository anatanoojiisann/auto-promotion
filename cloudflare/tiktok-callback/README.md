# Cloudflare TikTok 回调页

## 当前部署

- Worker：`auto-promotion-tiktok-callback`
- 回调地址：`https://auto-promotion-tiktok-callback.lolulu834.workers.dev/oauth/tiktok/`
- 健康检查：`https://auto-promotion-tiktok-callback.lolulu834.workers.dev/health`
- 2026-10-16 已核对公网 HTTP 200、`manual_handoff` 模式、无查询参数回显、`no-store`、`no-referrer`，并在部署后重新确认 Logs 与 Traces 关闭。
- 本机账号中心已预填回调地址和 `mira339019`，尚未保存应用配置，等待真实 App ID、App secret 和官方账号持有人授权 URL。
- R2 尚未启用，未开通任何新增付费订阅；账号尚未完成 TikTok OAuth 连接。
- TikTok 开发者注册表及应用 `Auto Promotion` 已由用户提交，包含 346 字符英文说明、`TikTok accounts` 权限、两个回调字段以及 `app-logo.png`（512×512，沿用工作台图标）。2026-10-16 早先显示 `Pending`；同日最新核对显示应用 `Rejected`，页面同时提示开发者资料申请被拒，App ID 和 Secret 尚不可用，账号仍未连接。
- 资料此前因官网 `aurax.one` 与通信邮箱 `hala.finance` 域名不一致被拒。2026-10-16 按用户指示将公司名称更正为 `AlphaDecentral Inc.`、官网改为 `https://www.hala.finance/en-US/home`，核对现有企业联系邮箱，并补充 HALA／Aurax 同属该公司的用途说明后重新提交。页面已显示 **Your profile is currently under review**；未触发额外邮箱验证码，未上传证件。开发者资料正在审核，应用／Accounts／私信权限不能视为已获批。
- 用户提供的两份 PDF 已逐页核对：内容对应美国科罗拉多州的 `AlphaDecentral Inc.`，分别为州公司设立文件及 IRS CP 575 A 通知，名称与地址对应。此前关于中国大陆主体的准备建议已被文件内容修正；未在仓库保存税号、完整证件或证件截图。当前仅核对文件类型、内容一致性及可读性，未完成签发机构在线核验。
- 选择 `TikTok accounts` 后，创建表单会额外显示 **TikTok account holder redirect URL** 和 **App logo**；真正的账号授权需使用该账号回调字段。当前广告主和账号持有人两个回调字段均填入上方已部署地址。
- 平台提示申请 Accounts API 前还需完成 [Accounts API Access Application Form](https://bytedance.sg.larkoffice.com/share/base/form/shrlgu4WEvtSXpEDLcCw56u4Rfc)。此表未由代理代填或提交，是否已由用户提交尚待确认。

## 工作方式

提供 `/oauth/tiktok/` HTTPS 回调页面，配合本机工作台现有的手动回填流程使用。浏览器保留完整回调 URL，用户将其粘贴回工作台；一次性 state、账号身份和令牌交换仍由本机处理。

- `/health` 返回服务版本和 `manual_handoff` 模式。
- 页面不读取、回显、存储或转发查询参数，不加载第三方资源。
- 未提供 Webhook 接收、媒体托管或自动账号登记，不要将 `/webhooks/tiktok` 配到此 Worker。
- 不需要 TikTok 应用密钥、Cloudflare KV、数据库、R2 或付费 Workers 计划。
- 保持 Workers Logs / Observability 关闭；授权链接含一次性授权码，不应采集请求 URL。Wrangler 配置已关闭该选项，通过控制台部署时需检查设置。

部署后，将实际的 `https://<worker>.<account>.workers.dev/oauth/tiktok/` 填入 TikTok 的 **TikTok account holder redirect URL** 和本机「连接 TikTok」表单，二者须完全一致。保留末尾 `/`。

创建应用及 Accounts API 权限仍需通过 TikTok 审核。此服务只提供回调地址。

Workers 的 `workers.dev` 地址用于先完成联调；Cloudflare 建议正式业务使用自定义域名或 Worker 路由。后续如启用 R2，应另外配置素材路径和 TikTok URL 前缀验证。
