# 高频互动：实现、接入与运维

## 官方 X API 自动扫描路线（本轮选定）

自动路线固定使用 `scan.source=x_api`，第一个所选账号读 recent search；全部所选真实账号
均须为官方 API 连接。HF 不回落到旧网页搜索或 Xtf。`AP_X_BACKEND` 精确等于 `legacy`
会改变底层派发，所以启用检查、每轮扫描及最终提交均阻止此配置，返回
`x_api_builtin_backend_required`；由操作者核对运行服务的环境并选 `builtin`，代码不自动改环境。
工作台从 `GET /api/interactions/accounts` 读取本机能力，禁用不兼容的未选账号并显示修正建议；
已选错误账号仍可取消勾选。保存设置保留原账号选择顺序，再追加新选项，避免按名称排序
静默更换首个 reader；显式取消首个账号才改用下一位。服务端也检查所有账号，不能只让首个 scanner 通过。

现场最小配置清单（本轮仅假客户端验证，以下现场值和权限均**未验证**）：

| 项目 | 代码已有 | 操作者需核对/配置 |
| --- | --- | --- |
| 官方连接 | `backends/common.py:profile_path/read_profile` 按 `AP_ACCOUNTS_DIR`，默认 DB 同级 `accounts/x/<backend_label 的 SHA256 前32位>/credentials.json` 读取私有文件 | 由既有安全接入流程配置 `label`（匹配账号 `backend_label`）、`identity`、`connection_type=x_api`、`user_id`、OAuth 用户 `access_token`；发布的应用证据另需 `app_id`。不要把内容发到聊天、策略或日志。现有 X 浏览器登录表单不支持 API 凭据录入，本轮不新增凭据表单或创建令牌。 |
| 账号登记与身份 | 原账号 DB、账号检查及 `GET /2/users/me` | 账号中心登记对应 `backend_label` 后，在另行授权的现场验收中核对实际 API 身份与 recent-search 权限。已配置不等于读取成功。 |
| 扫描 | 官方 `/2/tweets/search/recent`、固定 UTC 窗口、字符串 ID、next_token 分页 | 选 `x_api`、正确账号/查询/读预算；保留重叠回看。当前 upper 仍是轮开始时间，不 sleep、不加 since_id；真实 API 时间窗接受情况尚未验收。 |
| 模型 | `DraftService` 本地格式检查及分类/拟稿/校验调用 | 服务进程的 `AP_DRAFT_ENDPOINT`、`AP_DRAFT_MODEL`，供应商需要时由操作者安全配置 `AP_DRAFT_API_KEY`；本轮不证明外部模型可用。 |
| 后台 | 原 CLI `serve --port 8780` 默认启动 worker，持久阶段队列和原发送池 | 按既有运维步骤启动，不能带 `--no-worker`/`--reports-only`；核对阶段协程与执行池，不只看端口/父心跳。此文档命令不授予当前任务启动服务或外发权限。 |
| 策略与写保护 | 默认停用/dry_run；P0/P1 合格后沿原任务启动来源自动推进 | 操作者选择账号、事实和时间窗后显式启用正式路线。真实提交仍需 `AUTO_PROMOTION_LIVE_WRITES=1`、`AP_X_OWNER=auto-promotion`、global stop 未开启、账号健康、原云端授权或有效本地 authority、配额和冷却。代码不会为启用策略改这些开关。 |
| 回复资格 | API 预检与最终提交沿用 `api_plan_evidence`、`automation_approval`、`recipient_intents` 绑定和有效期 | 现场提供真实合法证据；读取搜索不要求某目标的回复同意，搜索成功也不授予回复资格。缺写资格保持受阻，不改走浏览器、不加逐条审批。 |

只有明确合法的 `meta.result_count`、匹配数量的 data、合法游标才接受搜索页；合法空中间页
仍跟随 next_token，最后一页后才提交该查询水位。`{}`、缺 meta、畸形 data、部分 errors、
后页失败均不能算完整空轮/P3；已存候选/分类工作保留。官方 `/i/web/status/<数字ID>`
原帖链接可进入原发布队列，同时拒绝伪域、userinfo、query、fragment 和其他路径。

`401` 报 `XAPIAuthenticationRejected`，`403` 报 `XAPIAccessDenied`，`429` 报
`XAPIRateLimited`（可能是速率或用量上限）。API 中心和轮次均记录脱敏错误，不假报成功。
搜索 429 的 `Retry-After`（秒或 HTTP-date）与 `x-rate-limit-reset` 分别解析后取较晚值，
缺有效头时至少退避900秒，真实恢复时间未知。该 reader 的只读退避保存在原 meta 表，
跨策略/服务重建和每一页边界复核，不换号绕过；到期不保证用量上限已恢复，也不自动付费。
写请求结果、unknown、发送次数及回复资格合同不因读取错误分类而改变。

`imported_results` 仅为已有结果的辅助人工交接，始终覆盖未知，不是本轮自动扫描来源。
回归入口：`node scripts/test_offline.cjs test_interaction_x_api.py`；假 HTTP/模型与临时 DB
覆盖官方响应到分类队列、原发布队列及一次冻结等待、分页失败/429/重启、最终渠道变更和
unknown 一次性保护。没有真实 API、模型、登录、发送或 Windows 现场验收。

## 2026-10-22 接入检查、人工来源与排期补充

以下是代码行为与本地隔离验证范围，不代表现场账号已接通或真实回复已验收。

- 未启用草稿和停用操作仍可保存。真实账号正式启用时检查所选扫描来源、
  `AP_DRAFT_ENDPOINT` 的本地 URL 格式、非空 `AP_DRAFT_MODEL`，以及当前服务中 sender、
  HF 协调和各阶段协程是否存活。缺项返回具体 blocker；探测异常为 `worker_unverified`。
  `configured` 只说明本地配置存在，`model_connectivity=unverified` 不等于远端健康检查通过。
  每轮 API 扫描再次检查；服务重启后丢失模型配置，即使 API 返回空页，也不能计完整空轮/P3。
  P3评估也重新检查当前就绪状态，不凭配置失效前的五轮旧记录发出新评估/通知。
  既有 `interaction-once` 在实际消费阶段队列的调用范围内标记 `worker=one_shot`，
  调用结束即撤销；它不是持续后台健康证明，真正发送仍需要原持久发送器。
  不会为检查而启用 worker、修改凭据、放开全局停止或平台 owner/live 开关。
- `scan.source=x_api` 保持原官方 API 的时间窗、分页和完整覆盖要求。
  `scan.source=imported_results` 是**已有采集结果人工导入**，不是实时浏览器扫描适配。
  不会调用浏览器、按十分钟刷新旧网页来源或把有限搜索卡片当作完整覆盖。
  旧浏览器发现代码仍存在，但本次没有开放网站自动化绕过 API/平台门禁。
- 工作台选择人工导入来源后，点击“导入已有采集候选”，选择同项目已保存的公开 X 候选，
  补充作者公开 ID、原帖 UTC 发布时间和上下文状态。正文与目标复用原记录，不能跨项目借用。
  时间字段明确按 UTC 解析；未知原帖时间不能填写当前发现时间替代。
  直接导入接口为 `POST /api/interactions/{policy_id}/imports`，请求 `sources` 最多100条；
  公开数据包含字符串 `id`、`url`、`text`、`author_id`、有限 UTC `source_event_at`、
  `context/context_status/language`，或 `legacy_candidate_id` 加原帖元数据。
  未来时间、非有限值、数值事件 ID、权限/批准/分类/模拟覆盖字段均拒绝。
  导入记录始终 `coverage_incomplete=1`，复用原分类和发布队列，但不更新 API 水位、
  不挣完整空轮、不触发 P3；切换来源递增 count_epoch 并清零连续空轮。
  导入不证明渠道、作者接收意愿或发送账号资格；P0/P1 仍须可信渠道重新核验后自动推进，
  P2/P3 保留人工选择，不增加新的逐条审批程序。
- 随机入队等待默认 `publishing.enqueue_delay_min_seconds=600`、
  `enqueue_delay_max_seconds=900`，可显式配置，上限不得小于下限。
  起点是候选首次向原发布队列转交、保留发布意图的写事务时刻；
  `hf_enqueue_times` 保存一次抽样及最终 `not_before`，重复路由、配置改变、重启不重抽。
  原 item 的 `scheduled_at/due_at` 使用冻结值，900秒投递容差从这个 due 起算；
  恢复不会用当前时间滚动延长窗口。事务提交后、task/items/link 之间中断可找回同名原草稿，
  明确尚未尝试且已过窗时关闭为 `enqueue_window_elapsed`；已存在尝试/unknown 优先保留核验。
  已冻结的旧清单不重新排期。跨过候选 TTL 时关闭未提交项，不创建发送尝试或额度预留。
  Engine/native 最终提交保护也读取 not_before；账号节奏、冷却、额度、全局停止及渠道检查照常有效，
  最早排期不承诺精确发送时间。扫描600秒、入队600–900秒、账号最小间隔是三个独立参数。
  PDF 的600–900秒要求是原 MD 之外的补充，本次明确采用以上默认值。
- 批次 `completed` 表示生命周期结束，不表示全部投递成功。
  后端 `delivery_summary` 与工作台分别显示真实核验 confirmed、模拟、missed、unknown、
  failed_known 和证据未完整。历史分类使用冻结 task.mode 与回执/引用，不依赖当前账号模式。
  unknown 不解锁意图、不换号补发，继续原只读核验；旧 completed 任务不会自动转为持续策略。

本轮回归：`tests/test_interaction_readiness.py`、`tests/test_interaction_api.py`；
可用 `node scripts/test_offline.cjs test_interaction_readiness.py` 运行合成故障回归，
完整方法见 [测试说明](testing.md)。原合成流程显式把等待设为0测试原规则，新增假时钟用例独立覆盖
默认600–900秒边界、重启、孤儿任务、过窗、unknown、额度与冷却。
真实 API/模型健康、授权证据、媒体服务及效果归因仍需现场独立验收。

2026-10-21；业务显示时区 Asia/Shanghai；数据库时间为 UTC 秒。

## 实际架构检查

本功能遵循根目录 `AGENTS.md`、`README.md`、`docs/testing.md`、
`docs/cloudflare-execution.md` 和 `docs/execution-channels.md`。
开发前工作区已有 X 原创发布等未提交改动，本功能保留这些改动。

| 范围 | 现有位置 | 复用方式与发现 |
| --- | --- | --- |
| 服务与调度 | `api.py`、`cli.py`、`workers.py` | 同一 FastAPI 服务与生命周期；协调、扫描、分类、生成、通知、核验各自后台推进 |
| 任务与持久队列 | `core.py`、`queue_store.py`、`pool_runtime.py` | 阶段任务使用原 `execution_jobs` 表；公开提交仍由原发送器和平台执行池负责 |
| 数据库 | `store.py` | SQLite WAL、FULL 同步、BEGIN IMMEDIATE、事务迁移、唯一键；没有另建数据库服务 |
| 账号与人设 | `accounts`、`pools.py`、`account_profiles`、`backends/common.py` | 复用账号、号池、语言、目标边界、身份、账号锁、额度和冷却 |
| X 发现与发布 | `backends/browser.py`、`native_bridge.py`、`routing.py` | 原配置为浏览器连接，不能证明完整分页或官方自动回复资格；新增官方 API 文本连接作为同一适配器的分支 |
| 原发布证据 | `items`、`attempts`、`publication_evidence`、私有 `Receipts` | 与新旧任务共享；不知道是否已提交时保留未知，只读核验 |
| 模型 | `drafts.py` | 扩展现有 `AP_DRAFT_*` HTTP 传输，增加结构化分类、拟稿和事实校验用途 |
| 媒体 | `Media`、`Engine.verify_media`、原媒体上传与哈希校验 | 复用资产结构及校验；源码依赖树没有 Factory 生成客户端或素材检索服务 |
| 通知 | `statistics.py`、工作台持久报告 | 没有现成通用外部推送服务；新增工作台内持久通知与可选 Webhook outbox，不冒充聊天消息已送达 |
| 页面与测试 | `web/`、`tests/`、`scripts/*ui_test.cjs` | 沿用原网页、会话鉴权、CSRF/Host/Origin 边界、unittest、Ruff、Playwright 和打包方式 |

## 文件与数据迁移

新增：

- `interaction_models.py`：所有策略、时间、预算和人工操作的校验模型。
- `interaction_schema.py`：事务内增量迁移，标识 `hf_schema_version=1`。
- `interaction_queue.py`：在现有队列表领取阶段任务，租约、心跳、过期恢复和旧租约写入隔离。
- `interactions.py`：扫描、分类、分流、人工选择、批次、通知、生成、发布衔接、核验及效果观察。
- `interaction_connectors.py`：账号、模型、发现、通知、媒体与指标的接入合同及真实缺口。
- `interaction_intents.py`：新旧任务共享的跨账号事件去重和最终提交前检查。
- `interaction_simulation.py`：独立临时数据库、模拟连接和可替换时钟的可执行验收。
- `backends/x_api.py`：官方 X API 搜索、读取、身份核对、资格检查、文本回复及只读回执核验。
- `web/interactions.js`：真实接口驱动的设置、候选、通知、运行和结果页面。
- `tests/test_interactions.py`、`test_interaction_api.py`、`test_x_api.py`、`scripts/hf_ui_test.cjs`。

修改原有的 `store.py`、`models.py`、`quotas.py`、`core.py`、`queue_store.py`、
`drafts.py`、`api.py`、`cli.py`、`routing.py`、`native_bridge.py`、
`backends/common.py`、`backends/browser.py`、`readiness.py`，以及网页入口、样式和 npm 测试入口。
现有 Cloudflare 控制层的执行池白名单增加 `x.api`；使用在线执行通道时须通过原部署流程升级控制层并更新节点池范围。
离线测试入口增加可配置的 `AP_TEST_TIMEOUT_MS`，用于这台 Windows 电脑上的完整回归。

新增表按功能分组：

| 数据 | 表 |
| --- | --- |
| 配置 | `hf_policies`、`hf_config_versions` |
| 扫描与进度 | `hf_scan_runs`、`hf_query_progress`、`hf_watermarks`、`hf_scan_candidates`、`hf_admissions` |
| 候选与选择 | `hf_candidates`、`hf_decisions` |
| 批次与摘要 | `hf_batches`、`hf_batch_members`、`hf_assessments` |
| 通知与尝试 | `hf_notices`、`hf_notice_attempts` |
| 生成、消耗与观察 | `hf_generation`、`hf_usage`、`hf_observations` |
| 发布去重与确证未提交重试 | `interaction_intents`、`hf_publish_retries` |

原有任务、冻结清单、凭据、发布记录、回执和历史审批不重写。
旧任务缺少 `project_id` 时按 `default` 项目参与去重。已有任务不会因部署而自动成为高频策略。
迁移会在正常服务启动时随 `Store` 自动执行，可重复运行；无需手工执行 SQL。

## 确定性规则

1. **默认十分钟扫描**。作用域为项目、平台、受众/查询策略标识；同策略多个账号共享发现。
   固定每轮上界、每查询下界和分页游标。默认首次回看 1800 秒，水位回扫重叠 1200 秒。
   官方 API 时间窗口不带 `since_id`，字符串事件 ID 保持精度。
2. **完整采集与完整业务轮分开**。每页原始候选、分类任务和分页进度在同一事务保存。
   只有该查询全部分页可靠落库才提交自己的水位。失败、截断、页数/读取预算耗尽不会变成空轮。
   分类未决保留待判定，超出轮次期限后本轮失败清零；分类任务可按原引用有限重试恢复。
3. **空轮按调度顺序、每轮至多结算一次**。只有完整成功且无新有效 P0/P1/P2 才加一；
   重复采集不算新机会。新增高价值机会即使不能发送也打断空轮。
   查询、受众、语言、事实/分类版本或账号范围改变，递增 `count_epoch`；旧版本结果不修改新计数。
   正常扫描间隔不得大于长期中断阈值的一半。
4. **P0/P1 自动路线**。满足当前内容事实、账号、渠道、接收意愿、批准、有效期与额度后，
   自动拟稿并创建原有持久发送任务，启动来源记录为 `high_frequency_policy`。
   不创建逐条审批。全局停止、原账号限额和原执行通道保护继续生效。
5. **P2 固定批次**。首条有效 P2 打开 `opened_at` 与 `due_at=opened_at+18000`。
   新候选、重启和普通配置保存均不移动这个时间。首次通知尝试前封存；封存后的新 P2 打开下一批。
   送达和封存分别记录，发送失败不代表已提醒。
6. **P3 保持人工**。默认最近完整健康扫描带来连续五轮空轮后，至少间隔 3000 秒评估，最多三条。
   确定性排序不调用收费生成，不抢占待处理 P0/P1。未通知 P2 批次可附带 P3 而不改截止时间；
   已提醒但未处理的 P2 存在时不追加主动催促。相同 P3 不因冷却结束重复通知，也不因空轮升级。
7. **有效期**。从触发帖子/新追问的原始发布时间计算，时效/相关讨论默认四小时，明确未解决需求默认一天。
   首次发现不续期。临期提醒默认提前半小时，按候选、有效期、类型、通道唯一。
   P3 不走临期提醒；普通摘要过滤已提醒的 P2、关闭、选择、已发布和失效项。
8. **人工选择有版本**。仅提交明确选中的 ID，采用/编辑/跳过/暂缓都使用乐观版本检查并记录操作者。
   编辑账号或正文后原选择失效，人工正文不会被生成器覆盖。暂缓不能超过有效期。
   原帖实质改变时取消旧的未提交清单，重新分类；关闭、明确跳过和已提交事件不自动复活。
9. **发布唯一意图**。数据库唯一键为项目、平台、触发事件、动作；所有账号共享。
   原发送器在事务内检查新候选及历史已发布/未决记录。纯人工交接尚未发送不充当已提交证据，
   实际登记后的人工结果参与历史去重。
10. **未知结果不重发**。原 `attempts` 与私有回执先于外部提交保存。
    提交进程中断、返回不明均进入 `VERIFYING`；过期、停用和核验超时不会解除意图锁。
    到关注期限暂停自动轮询，记录一次工作台故障通知；仍可手动只读核验。
    只有明确 `confirmed_not_submitted=true` 的可信底层回执允许同一候选、同一账号、原内容按配置有限重试。
    旧尝试保留，新尝试仍经过原发送器，不换号、不把查询未发现当作未提交证明。
11. **通知幂等**。通知唯一键、候选快照、修订、每次尝试和实际消息 ID 持久化。
    明确未发送后重试会过滤当前有效项，必要时在同一通知下修订；结果未知时先查原回执，不另建摘要。
    对外正文突出前三条，完整候选列表在工作台；静默只影响通知，TTL 继续流逝。
12. **生成与成本**。默认文字。P2/P3 未选择前不能提交收费媒体任务。
    媒体使用项目共享槽位、计数/费用预留、持久供应商 ID、来源和版本；超时先查询原任务，
    不确定的供应商任务在本地租约或候选 TTL 结束后仍占用项目槽位，防止重复计费及并发超限。
    不明费用保留 null；缺少预算或媒体连接时明确显示原因，具备条件的文字流程可继续。
    外部示意素材没有真实产品输出证据时不会记为 Aurax 实测。
13. **效果记录**。仅按真实已确认发布时间创建 24/72 小时观察任务并去重。
    保存发现/发布与人工等待时间、各等级/状态、生成及读取消耗。
    没有 X 指标或归因来源时观看、相关对话、访问、安装和首次生成均为 null，不冒充零或获客成功。

## 设置、启停与恢复

正常启动原工作台：

```powershell
.\start.cmd serve --port 8780
```

进入侧栏“高频互动”。新策略默认停用、dry-run。选择项目/受众策略、X 账号或已有号池、
查询、语言和当前核实的产品事实。设置页高级配置覆盖所有 TTL、静默与运行时段、
生成预算/时限、发布频率、重试和核验参数；账号额度为 null 时解析原账号配置并显示有效值。
收费媒体预算为 null 时禁用该分支。

先以 dry-run 检查范围和阻断原因。切换正式执行并启用，是对该策略下未来合格 P0/P1 的持续执行授权；
无需逐条点选。P2/P3 始终需要人工采用。正式外发仍依赖原来的
`AUTO_PROMOTION_LIVE_WRITES`、平台 owner、全局停止、账号授权、执行通道与平台资格。
代码部署不会自动切换这些开关，也不会自动启用任何项目。

使用设置页“停用策略”阻止新生成/提交；已经提交的操作继续记录和只读核验。
全局停止仍使用原工作台按钮。不要删除队列、回执或未决记录来恢复任务。
服务重启后使用原数据库即可恢复水位、游标、批次时间、阶段队列、供应商 ID 与发布核验。

`serve --no-worker` 只启动网页与接口，扫描按钮仅入队；不会运行任何阶段。
`serve --reports-only` 不运行高频策略。需要单步推进时：

```powershell
.\start.cmd interaction-once
.\start.cmd interaction-simulate
```

`interaction-once` 推进阶段队列，把发布交给现有发送队列；原平台发送器仍需运行。
`interaction-simulate` 使用独立临时数据库和可替换时钟，不操作当前任务，不访问外部模型、社交账号或媒体服务。
网页“运行隔离模拟”使用相同验收入口。

配置修改默认作用于后续扫描、分类和新批次。选择“应用到已有未封存批次”后，
按原打开时间重算截止时间；已过新截止时间的批次只补通知一次。
从 dry-run 切换正式路线时，仅 dry-run 未提交的模拟意图重新评估；实际已提交/已核验任务不重播。

## 接口

均使用原 Bearer 或 HttpOnly 会话与写入来源检查：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/interactions`、`/defaults` | 策略与完整默认配置 |
| POST | `/api/interactions` | 保存 config、expected_version、apply_timing_to_open_batches |
| GET | `/api/interactions/{id}` | 有效额度、能力缺口、轮次/覆盖、批次、通知、费用及结果 |
| GET | `/api/interactions/{id}/candidates` | 按 offset/limit 查看持久候选 |
| POST | `/api/interactions/{id}/scan` | 合并或排入一次扫描 |
| POST | `/api/interactions/actions` | adopt、edit、skip、defer、verify、verify_media，显式 ID→版本映射 |
| POST | `/api/interactions/simulate` | 隔离端到端验收 |

## 真实连接核实与缺口

2026-10-21 PDF 的历史现场快照记录五个真实 X 账号使用浏览器连接，未记录官方 API
token、套餐证据、AI 回复机器人书面批准或绑定目标的接收意愿证据，且当时
`AP_DRAFT_ENDPOINT` 未配置。这不是对当前现场配置的确认；源码没有 Factory 客户端。
本轮没有读取或改变现场凭据，没有向真实用户发送测试回复，
没有调用收费媒体，也没有把这些能力报告为已接通。

官方连接代码使用已有私有账号配置 `credentials.json`，选择 `connection_type: x_api`，
需要 OAuth 用户 access_token、identity、user_id、app_id。密钥不写入策略或前端。
`health` 实际读取 `/2/users/me` 核对身份；搜索与发布还需开发者应用的真实接口权限及读取预算。
现有浏览器账号不会被自动改为 API 账号。

资格证据必须绑定实际账号与目标，保存在私有适配器资料中，不能由策略页面的布尔开关取代：

- `api_plan_evidence`：plan=self_serve/enterprise、source_reference、verified_by、verified_at、valid_until。
- `automation_approval`：上述证据字段及 account_user_id、app_id、真实书面批准 document_path/document_sha256。
- `recipient_intents[event_id]`：上述证据字段、author_id、account_user_id、willing_to_receive、opted_out、opt_out_checked_at。
- 这些是有来源的本地核实记录，不是平台自动签发的凭证。维护者须依据真实资料录入；
  当前没有这些证据，状态保持未知。Enterprise 不免除批准、接收意愿或退出检查。

模型复用原 `AP_DRAFT_ENDPOINT`、`AP_DRAFT_API_KEY`、`AP_DRAFT_MODEL`。
服务需要支持 `purpose=interaction_classify/interaction_draft/interaction_validate` 的结构化协议；
HTTP 请求包括 model、instructions、data，分类 schema 随请求提供。
该协议通过模拟合同测试，尚无已配置真实服务的验收结果。

工作台通知已能真实落库并在页面查看；消息引用为 `workbench://...`，不称为外部聊天送达。
Webhook 可配置 `AP_INTERACTION_NOTIFICATION_URL`（HTTPS）和可选 token，
接收方须返回 `{status: "sent", message_id: "..."}`；请求携带稳定的通知版本 Idempotency-Key。
当前没有外部 Webhook 回执查询合同，结果未知时停止自动重发，保留原快照等待核实。

媒体真实缺口是 Factory/素材库连接、计费估算与供应商查询合同，以及该官方 X 连接的媒体上传能力。
现有官方分支只支持文字回复，有媒体附件时明确返回 `x_api_media_upload_unavailable`。
媒体生命周期、预算、并发和来源保护可被注入适配器测试；没有实际供应商接入就不会产生伪造素材或成功回执。
需要真实 Factory 实现 `media_status/media_estimate/submit_media/query_media` 并返回可校验资产后再验收媒体发布。
缺少这些外部条件只影响对应媒体/真实执行分支，人工选择、扫描状态、工作台通知及文字模拟可独立运行。

官方规则在开发开始时重新读取核实：

- [X Manage Posts integration guide](https://docs.x.com/x-api/posts/manage-tweets/integrate)：自助 API 回复须由原作者提及或引用发送账号。
- [X API changelog](https://docs.x.com/changelog)：2026-02-23 变更记录。
- [X Automation rules](https://help.x.com/en/rules-and-policies/x-automation)：AI 回复机器人书面批准、接收意愿与退出约束；网站脚本自动化不作为本策略的发送路径。
- [X Search Posts](https://docs.x.com/x-api/posts/search/introduction)：近期搜索、分页与访问范围。

## 验收入口

```powershell
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m ruff format --check src tests
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_interactions.py" -v
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_interaction_api.py" -v
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_x_api.py" -v
.venv\Scripts\python.exe -m build --no-isolation
npm run format:check
npm run test:ui:interactions
```

完整离线回归沿用 `node scripts/test_offline.cjs`；慢速 Windows 环境可设置
`AP_TEST_TIMEOUT_MS=900000`、`AP_BROWSER_EXECUTABLE` 和 `AP_UI_CHROMIUM_PATH` 为已安装的测试浏览器。
浏览器测试要求本机回环端口可用。受限执行环境可能不允许创建 asyncio 回环套接字，
需在允许本机测试端口的环境重跑，不能把这种超时当作业务验收成功。

T01–T41 对应测试名保留在 `test_interactions.py` 中，另含租约隔离、迁移幂等、
事实过期后的提交保护、原帖改变后的旧清单撤销、明确未提交的同账号有限重试，
以及超过关注期限/TTL 后的人工只读核验。
模拟结果、检查计数与完整测试/构建结果见同目录的交付验收记录。
