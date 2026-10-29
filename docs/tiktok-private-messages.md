# TikTok 网页私信

2026-10-26 按用户要求接入现有 TikTok 网页账号的私信采集、主动文字私信和私信回复。使用登记过的 Tabbit / Chrome / Edge 会话，不复制 Cookie、不调用网页内部私有接口。原官方 API 路由仍保留自己的权限与自有视频规则。

## 采集与隐私

桌面辅助脚本为 `web/tiktok-messages-desktop.js`。它识别普通 `/messages` 页面，以及实机观察到的 Business Suite `/business-suite/messages` 中的 `/messages?scene=business` iframe。

- 从 `[data-conv-id]` 读取一对一会话原生 ID，从私信头部用户名和主页链接核实对方身份。
- 从 `more-action-icon-msg-<数字ID>` 读取原生消息 ID，从消息头像主页识别收发方向。缺失 ID 保持未核验，不按列表位置补造。
- `read(page, connection, identityProof)` 读取当前会话；`collectVisible(..., limit)` 串行读取最多 20 个当前可见会话，始终标记 partial，不声称覆盖全部收件箱或历史。
- 身份 proof 来自同一登记浏览器近期的已登录导航及主页核验；不得用会话中出现的本账号头像代替登录核验。
- 原生时间分隔符可解析时保存平台时间，否则保持 null。未读角标不当作互动数。
- 正文仅保存在 `tiktok_dm_messages`，不导入共享内容池，不写入通知或普通审计。审计只含账号、条数和覆盖状态。
- 采集不会自动回复、接受请求、关注或创建发送任务。打开会话可能由平台标为已读。

本机表为 `tiktok_dm_collections / tiktok_dm_conversations / tiktok_dm_messages`；重复采集按账号、会话和消息原生 ID 去重。原生消息不能转移到另一会话，已核验的作者与方向不能变化。

## 发送任务

先采集并核实账号的真实收件箱，再启用该账号的私信任务能力。启用能力不改变全局开关，不解除账号停止，也不启动任务。

工作台「TikTok 网页账号 → 私信收件箱」可查看本机采集记录、准备回复或主动私信草稿。保存草稿不会开始发送。也可使用正常 create_task / set_task_items / start_task，选择 local 通道。

动作与冻结字段：

- `send_direct_message`：target 为收件人 TikTok HTTPS 主页；`recipient_id` 必须为匹配的用户名。已有会话可带 `tiktok_conversation_id`，新收件人待页面打开后核实。
- `reply_direct_message`：另需已采集的 `tiktok_conversation_id` 和对方原生 `tiktok_message_id`。它表示对此条私信的回应上下文，不冒充平台原生引用关系。
- 首版只支持 1–1000 字符精确文字，不混入视频、评论、媒体或其他平台字段。
- 主动私信对同一收件人跨任务去重；回复按对方原消息 ID 区分。未知发送默认阻塞该账号的新外发。用户明确要求跳过某条未知结果并继续时，可通过原归属的正式回执接口记录 `UserSkippedUnknownSubmission`；这只结束该条的工作流等待，保留发送结果未知、原尝试和目标去重。之后仍可针对原条目只读核验并登记独立原生回执。

`openRecipient(page, target)` 只打开指定主页，并在页面提供明确 Message 按钮时打开会话；无按钮或平台不允许时停止。不会接受消息请求或改变隐私设置。

`prepare` 核对当前收件人和会话，保留无关草稿，填写精确文字后重新读取发送控件。只有页面实际提供唯一的发送控件才可继续。`proof` 绑定消息 ID 快照、输入全文及接收方；会话变化后不能沿用旧许可。

之后使用原 `tiktok_browser_begin / tiktok_browser_guard`：领取唯一尝试、提交前重查账号／任务／全局停止、额度和过期状态。`submit` 只消费一次许可，点击返回 unknown；超时不重试，也不按输入框清空判成功。

## 回执与播报

`verify` 独立重读已绑定会话，核对新增原生消息 ID、方向、完整正文、对方及账号。已有消息、对方发来的同文消息、缺失 ID 或多个候选均不能确认成功。

confirmed receipt 包含通用回执字段，以及 `recipient_id, recipient_verified=true, conversation_id, message_id, message_id_verified=true, direction='outbound', independent_read=true, parent_message_id`。external_reference 使用实际观察到的私信页面地址，不补造消息直链。原生 ID 另行持久保存并唯一绑定发送条目。

未知结果只读核验，最短核验间隔 60 秒，绝不重新发送。私信正文不进入播报；核验后的本系统外发消息进入原三小时汇总“私信互动”项。历史收件箱导入不增加发送或成功数。平台发布时间未知仍保持 null。

## 本机入口

旧 MCP 会话需刷新工具目录。以下 CLI 使用相同实现，可独立于旧会话工作：

```powershell
.venv/Scripts/python.exe scripts/tiktok_browser.py dm-collect --input var/private-snapshot.json
.venv/Scripts/python.exe scripts/tiktok_browser.py dm-inbox --input outputs/account.json
.venv/Scripts/python.exe scripts/tiktok_browser.py dm-messages --input outputs/conversation.json
.venv/Scripts/python.exe scripts/tiktok_browser.py dm-enable --input outputs/account.json
```

collect 输入为 `{account_id, snapshot}`，inbox 输入为 `{account_id}`，messages 另带 conversation_id 与可选 limit，enable 另带可选 enabled（默认 true）。私信快照应放在本机私密目录，不打包进公开诊断附件。

新 MCP 工具有 `tiktok_dm_collect / tiktok_dm_inbox / tiktok_dm_messages / tiktok_dm_enable`，API 在 `/api/tiktok-browser/accounts/{id}/dm/` 下，沿用工作台身份验证。

该通道需要 Codex 桌面执行端在线。本轮开发和采集不代表已发送真实私信；端到端发送验收需要明确指定的收件人、完整文案和对应任务启动指令。

## 真实外发启用记录

2026-10-26 用户明确批准“启用 TikTok 真实外发，仅执行明确启动的任务”。已为登记账号 `@mira339019` 设置 `AP_TIKTOK_OWNER=auto-promotion`，将该账号与 `tiktok.desktop` 加入原执行节点，并仅同步账号元数据，没有上传任务或发送内容。正式工作台已重载并报告 TikTok 外发启用；其他平台开关、全局停止、额度和冷却保持原值。

本次启用没有创建或启动任何 TikTok 任务，没有发送私信。每次外发仍须明确收件人和完整文案、启动对应任务，再完成现场身份与上下文检查；消息请求、平台限制和未知回执仍按原边界处理。
