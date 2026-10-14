# 号池与推广活动

工作台增加“号池与人设”“共享内容池”“推广活动”三个入口。Campaign 管理大批内容和账号；Task 继续负责两阶段精确审批和一次性执行。生成计划、编辑草稿、创建批次均不会批准或发布。

## 使用流程

1. 在账号中心登记并核验账号，创建号池，并配置真实角色、语言、主题、受众、语气、产品事实、模板和工作时段。
2. 导入 JSON/CSV 内容，或只读采集公开 Telegram 群消息。读取不会入群或回复，内容的互动许可默认为待核实。
3. 选择内容创建活动，指定号池、期限、语言、主题、账号间隔和额度。
4. 查看分配理由、未分配原因、精确草稿和时间，可编辑草稿，然后创建审批批次。
5. 打开每个批次批准发现政策，计划清单自动载入。核对清单，冻结，再批准外发。若载入中断，可调用 `POST /api/tasks/{id}/populate` 恢复。
6. 按排期执行，活动页汇总任务、账号和人设效果。未知结果只核对，不重发或换号。

批次按账号分组，最多100项。活动最多10000条内容，号池最多1000个账号。执行默认并发6个账号，上限50；这些是工作台容量，实际执行还受各级额度和平台许可限制。

## 内容格式

JSON 使用数组或 `{"items":[...]}`，一次最多5000条。CSV 首行为字段名，数组字段 `topics`、`audience`、`permission_account_ids` 使用 `|` 分隔或 JSON 数组，布尔字段使用 `true/false`，媒体使用 JSON 数组。

```json
[
  {
    "platform": "telegram",
    "external_id": "123",
    "native_target_id": "-1001234567890",
    "target": "@your_public_group",
    "action": "reply",
    "reply_to": 123,
    "author_id": "456",
    "title": "视频导出问题",
    "context": "原消息上下文",
    "language": "zh",
    "topics": ["视频导出"],
    "confidence": 0.95,
    "value": 0.8,
    "permission": "allowed",
    "permission_evidence": "真实的群规则或管理员许可来源",
    "permission_account_ids": ["acc-your-authorized-account"],
    "expires_at": "2026-10-28T20:00:00+08:00",
    "draft_text": "待审核的精确正文"
  }
]
```

Telegram 回复需要 `reply_to`，Discord 回复需要 `discord_reply_to`。Telegram 原生群ID和消息ID用于去重，未提供群ID时使用目标标识；X按帖子ID、Reddit按帖子/评论ID、Instagram按内容短码合并链接变体。`author_id` 用于跨帖子作者额度。

同一平台内容默认由一个活动保留。取消只释放未尝试的内容和额度，已提交、已排期及未知结果继续保留。

## 人设和 AI 草稿

草稿优先使用 `draft_text`，否则使用人设 `reply_template`。模板支持 `{title}`、`{context}`、`{topic}`、`{brand}`、`{role}`、`{facts}`、`{author}`；禁止属性访问、格式指令和代码执行。

新版本人设只用于后续计划。已生成批次保留快照，冻结清单包含完整正文和人设版本，发送时不会重新生成。MCP 的 `get_campaign`、`edit_campaign_draft` 可用于 AI 辅助阅读和写入草稿。

可选草稿服务在 `.env` 配置 `AP_DRAFT_ENDPOINT`、`AP_DRAFT_API_KEY`、`AP_DRAFT_MODEL`。需要实现本项目的 JSON 契约：接收 `POST {model,instructions,profile,opportunity}`，返回 `{text,title}`；不能直接填写响应格式不同的模型接口。地址允许 HTTPS 或本机 HTTP，禁止重定向，密钥和异常响应不返回页面。

“AI生成本页前20条草稿”仅在点击时调用服务。服务失败、活动版本变化或文本不符合动作约束时，不覆盖该次请求原草稿。未配置服务时，导入正文和人设模板仍可使用。

## 资格、调度和额度

先检查平台动作、公开边界、语言、关键词、置信度、目标许可、账号健康、人设限制和有效期，再按主题、受众、内容价值和负载评分，结合工作时段和剩余容量排期。有效期内无法执行的内容保留未分配原因。

日额度按UTC自然日计算，小时额度为滚动60分钟；页面时间显示北京时间。全局、平台、号池、活动、Task、账号、目标和作者限额共同生效。默认全局100次/日、20次/小时，在活动页“调度与额度”调整。

新任务绑定额度快照；调高配置不提高旧任务已批准的上限，更严格的当前限制立即生效。活动批次冻结时原子预留投递额度，领取时转为已消耗，防止批次超卖。

旧任务保留任务间隔和严格顺序语义。新活动按账号间隔执行。同一账号跨任务共用可续期执行权，检查、发送、核验和采集不会重叠；不同账号有界并发。占用账号或并发槽位不足时保留未尝试状态。

活动暂停在事务内更新子任务，后端提交前再次检查停止状态。完成必须等待子任务结束，未知、排期待核验、待人工均不当作完成。

## 平台范围和统计

Telegram、Discord 自动执行需要目标许可及证据，发送前继续核对身份和权限。Instagram 自有媒体发布沿用现有能力，第三方评论使用人工清单。

当前 X、Reddit 真实互动采用人工清单，执行入口和后端阻止真实浏览器自动互动，X点赞不进入活动自动分配。模拟账号继续支持已有动作。参考 [X自动化规则](https://help.x.com/en/rules-and-policies/x-automation) 和 [Reddit互动规则](https://support.reddithelp.com/hc/en-us/articles/360043504051-Spam)。

活动页区分确认、排期、未知和人工状态，账号效果关联人设版本。缺少完整反馈覆盖时回复率为 `null`，点赞和转发不进入分母。当前自动反馈采集仍是 Telegram，其他平台空数据不被报告成零回复率。

## API 和测试

接口包括 `/api/pools`、`/api/accounts/{id}/profile`、`/api/opportunities/import`、`/api/accounts/{id}/content`、`/api/campaigns/{id}/plan`、`/materialize`、`/drafts`、`/api/assignments/{id}`、`/api/execution/settings`；详情见本机 OpenAPI。

`test_campaigns.py` 覆盖30账号/1000内容、分配理由、并发/串行、额度争用、版本、暂停、未知和幂等；`test_campaign_api.py` 覆盖认证和两阶段审批。`scripts/pool_ui_test.cjs` 验证完整界面，发现真实账号立即拒绝运行。

```powershell
$env:AP_POOL_UI_URL='http://127.0.0.1:8795'
npm run test:ui:pools
```
