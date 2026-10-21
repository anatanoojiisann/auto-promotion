# 准备内容后线上发布

本流程采用“我们准备目标与正文，服务器自动排队发布”。无需额外模型 API，不开通付费套餐，不复制本机 Telegram 会话，不使用本机平台执行器。

## 两个组成部分

1. `telegram-prepared-replies.mjs`：保存精确正文与审阅记录，消费研究、机会、草稿、发布前审核四种工作项。草稿阶段原样保留已准备正文，不重新生成文案。真实聊天证据必须来自指定账号的线上认证读取。发布前重新读取群规、原消息和回复；新增回答、正文变化、截断规则、禁止自动化、未验收产品推荐、相似重复内容均不能被自动放行。
2. `telegram-task-builder.mjs`：在前置条件通过后原子创建冻结任务、正式原生作业和工作流连接。账号、目标、正文、Reply 消息、时间窗、策略、清单哈希都保存在服务器；复用原有签名授权、租约、账户串行、回执和只读核对。子任务共享父任务的每日额度、最大入群数和节奏，不会因拆分任务而增加额度。

服务器不能仅凭文字自动判定问题是否解决或产品是否合适。操作者须阅读原文，提交正确的语义判断与对应引用；服务器验证引用确实存在于当前认证证据中，并在提交前检查上下文是否改变。

## 接口

全部为操作员认证的 POST；四个修改接口同时要求已绑定数据库的控制签名。状态只返回任务状态及数量，不返回密钥、二维码或会话。

| 接口 | 用途 |
| --- | --- |
| `/v1/workflow/replies/start` | 针对已显式启动、严格在线的原任务启用消费；传 `plan_id`、`reference_task_id`、`source` |
| `/v1/workflow/replies/stop` | 暂停该流程，同时阻止已生成子任务获得新的原生授权 |
| `/v1/workflow/replies/prepare` | 保存 `target`、`question_message_id`、精确 `text` 与 `review`，返回包 ID 与审阅哈希 |
| `/v1/workflow/replies/reconfirm` | 上下文变化后明确更新审阅证据；必须给旧审阅哈希，不能修改已冻结正文，也不能重试已尝试发送 |
| `/v1/workflow/replies/status` | 查看准备数量、生成数量及等待原因 |

`prepare` 不会启动一个未启用的流程；启用后的流程在服务器定时调度中消费。`/v1/workflow/status` 同时返回 `prepared_reply_pipeline` 和实际子任务回执。队列有内容、子任务存在、发送成功、100 群目标完成分别记录。

## 准备记录

`review.language` 为 `en` 或 `ru`。必须明确填写布尔字段：`topic_allowed`、`free_real_identity`、`automation_allowed`、`rules_allow_replies`、`question_unresolved`、`claims_verified`、`language_matches`、`support_only`、`requires_ai_disclosure`。

审阅引用放在 `review.citations`，对应决定名称为键，每项为 `{ "source": "about", "quote": "实际原文" }`。来源可以是 `about`、`pin:消息ID`、`recent:消息ID`、`context:source`、`context:reply:消息ID`。引用必须出现在当前线上证据中。必需引用对应主题、加入条件、自动化规则、答疑规则、问题未解决、事实正确及语言匹配等决定。禁止伪造肯定断言来弥补缺失证据。

正文为一到两段，30–90 个英文词或相近俄语信息量，最多一个主要追问。当前仅支持对公开讨论群内指定原消息的纯文字 Reply，不支持私信、脱离上下文的群公告或媒体投递。

AIgram 推荐仍需要工作流冻结的目标 Android 版本与验收记录、可推荐的群规及关系披露。链接最多一个且须为冻结事实清单中的精确官方入口。缺少这些条件不妨碍准备普通相关答疑。

## 项目客户端

使用已配置的受保护凭据；不要将令牌写入内容文件或命令参数：

```powershell
.venv/Scripts/python.exe scripts/telegram_online_replies.py status --plan-id WF-你的活动
.venv/Scripts/python.exe scripts/telegram_online_replies.py start --plan-id WF-你的活动 --task-id AP-原任务
.venv/Scripts/python.exe scripts/telegram_online_replies.py prepare --file 已审阅回复.json
```

`start` 只同步指定原任务并登记线上消费，不切换其他账号通道，不解除全局停止。账号授权、账号独立的云端归属和认证读取调度仍需完成；在这些条件不足时，内容留在服务器等待，不改为本机执行。

## 验证边界

隔离测试覆盖：精确正文端到端生成并派发、回执独立计数、并发构建去重、重启恢复、冻结内容保护、相似重复、冷却与登录、原任务暂停及每日额度、最大入群限制、上下文变化、审阅重新确认、产品限制、Cloudflare 实际运行环境和控制签名。测试使用合成账号、群和传输，不会发送真实 Telegram 消息。

真实平台发言仍以独立回执为准，不能用测试通过或任务入队代替验收。
