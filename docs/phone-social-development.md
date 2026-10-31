# 五 App Android 手机后端开发候选

2026-11-01，适配 A `0.5.0.dev14`、B `0.2.0.dev14`、内部包 `0.1.2`。需要配套升级，原 AP 服务不自动启用此路由。

实现为 Telegram send_message、X reply、IG/Facebook/TikTok 顶层 text comment。依赖 `.[phone]`，使用现有正常队列和 claim/manifest/一次许可/额度/停止/去重；不是直接驱动手机发送的工具。所有实际提交保持关闭，TikTok Lite 不包含。

`create_app(phone_social_config=...)` 显式注册私有 API；未配置时 phone 路由继续拒绝外发。register 不需要目标，只接受已有受信 device/platform/identity 绑定并从 A/B 核验新鲜真实账号。prepare 接收原未尝试 item 的冻结上下文，默认清空，不启动 task、不消耗 attempt、不回填假成功。原 browser/API 连接不能覆盖。

真正 execute 只能由原正常发送 JobQueue 的有效 lease/claim 进入，再查明确启动、开发任务白名单、通道与节点、停止、manifest、账号及一次状态。最终 guard 使用单独 runtime token、请求哈希、设备、执行与 native ID 绑定，CAS 使用一次，重新核对所有停止状态，授权有效期不超过原 claim。

已准备规则的 SHA-256 进入提交请求；A 拒绝更换规则后沿用旧准备结果。创建响应丢失后只 GET 原映射/原执行，404 不触发第二次 create，unknown 不自动重发。只有真实非模拟的独立原生回执才传给原 Engine.finish；按钮点击、空编辑器或准备成功都不是 confirmed。

API：

- `POST /api/phone-social/accounts/{id}/register`：空 body，登记账号；不授权发送。
- `POST /api/phone-social/items/{id}/prepare`：target_id / target_author / target_text / discard_after_prepare。
- `POST /internal/phone-social/v1/authorize`：单独认证的 runtime guard；不是外发入口。

PhoneSocialConfig 为 development-only，enabled 和 submit_enabled 默认 false，runtime_token 至少 24 字符，authorized_task_ids 默认空。worker 通过显式 `AP_PHONE_SOCIAL_CONFIG` 接入且校验数据库绝对路径，配置不传给其他资源池。需要独立保护的配置与原生凭证库；不得复制浏览器凭证或清除原 stop。

手机规则、权限与证据说明见群控 `control-system/docs/FIVE_APP_PHONE_ADAPTERS_2026-11-01.md`。当前包内规则尚未声明独立回执能力；没有真实评论或消息发布。五 App 编辑器的完整真机准备验收仍待系统输入法初始化及 Telegram 目标等输入，不作正式可外发结论。
