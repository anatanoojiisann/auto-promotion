# AIgram + Aurax 联合 P0/P1 补充功能交付

日期：2026-10-22；显示时区 Asia/Shanghai，数据库 UTC。
实施基线：`main`，`01da083a4bbc94fa7ed5f4069b896b9c3cea8d11`。
依据：用户提供的《Auto Promotion 补充开发指令：AIgram + Aurax AI 联合搜索与即时派单》v1.1。
交付为本地代码、迁移、接口、页面与运行文档；没有启动真实推广任务。

## 已实现

- 新建 v2 联合模板，默认停用、dry-run；旧 v1 配置与在途快照继续读取。
- 每轮运行两产品的启用查询组，按页交错提供读取机会。来源、可见范围、产品、查询 ID/语义版本形成持久分区；普通错误独立记录，水位仅在完整分页后推进。
- 默认整数秒均匀随机 3000–4200 秒，从实际启动时间计算下一轮；轮次账号、样本、配置、时间窗口和下次计划一次保存。普通保存、状态读取和重启不重排。
- 搜索账号固定轮换，按平台身份严格切换；等待替代账号时不采样、不推进游标。重试复用原账号与进度，手动扫描有幂等键，显式重排有审计。
- 429、Retry-After 和接口重置时间持久约束共享读取，不换号重放；读取预算、运行时段、队列租约和旧提交 fencing 继续有效。
- 分类保存原文证据、未解决需求、产品事实/限制及唯一主产品。P0 根据原生提及/回复与已发布会话关联确认归属，公开付费需求仍按 P1 核对。P0 事实不足可建等待任务；P1 核心事实不足不建自动处理任务。
- 合格时同事务写入真实 `HFP-*` 处理任务与分配阶段事件，不等待其他查询、下一轮、P2 批次或媒体。独立分配阶段选择产品允许的执行账号，缺账号显示 WAITING_ACCOUNT，已分配缺资格/事实显示 WAITING_CAPABILITY/WAITING_FACTS。
- 拟稿使用执行账号人设；事实按产品、发行状态、平台/版本和有效期过滤，生成后及最终提交再次核对。人工正文保留，相关事实变化会使未提交模型草稿失效。
- 可执行 P0 优先；等待 300 秒的任务按先到顺序处理，P1 不会永久饥饿；受阻任务延期，释放其他就绪任务的处理机会。
- 同事件多查询、多产品、多策略和多账号发现合并；跨历史项目支持明确共享去重映射。旧活动发送任务可复用，旧关闭/跳过与未知结果不复活。
- 联合模板不启用 P2/P3 推广、五小时批次或空轮回退。旧策略的原配置和行为继续工作。
- 页面提供双产品设置、轮换顺序、实际计划/延期、分产品覆盖、处理任务/发送任务、各账号角色、分配时间、草稿、受阻与回执，以及当前页产品筛选。

## 修改文件

| 范围 | 文件 |
| --- | --- |
| 新运行模块 | `src/auto_promotion/interaction_joint.py`、`interaction_schedule.py` |
| 模型与迁移 | `interaction_models.py`、`interaction_schema.py`、`store.py` |
| 工作流、队列与连接 | `interactions.py`、`interaction_queue.py`、`interaction_connectors.py` |
| 原发送边界 | `interaction_intents.py`、`core.py`、`readiness.py`、`native_bridge.py`、`backends/x_api.py` |
| API 与页面 | `api.py`、`web/interactions.js`、`web/app.js` |
| 测试 | 新增 `tests/test_interaction_joint.py`；扩展 `tests/test_interaction_api.py`、`tests/test_x_api.py`、`scripts/hf_ui_test.cjs` |
| 文档与示例 | `README.md`、`docs/high-frequency-interactions.md`、本报告、`examples/aigram-aurax-joint.json`、`MANIFEST.in` |

表中省略前缀的 Python 文件均位于 `src/auto_promotion/`。
开发前工作区已有浏览器、账号连接、X 原创发布和 Telegram 相关未提交改动；这些实现保留，没有重置工作区、回退提交或启动部署。

## 迁移和兼容结果

数据库版本为 **`hf_schema_version=2`**。增加处理任务、分配事件、发现关系、分类历史、共享去重项目、手动请求与读取冷却表；向已有策略、轮次、查询进度、候选及阶段队列表增量加列。启动检测同时检查版本和实际字段，不只修改建表文本。

测试验证既有 v1 在途轮次和阶段 job 在重复迁移后继续推进；原发送任务、冻结清单、回执及 unknown 不被改成待发送。原有 48 项高频行为回归覆盖 P2/P3 批次、人工编辑、未知结果和原队列；联合专项另测迁移、跨项目意图、旧任务复用和最终执行账号一致性。

`processing_task_id` 始终是独立处理任务；`task_id/item_id` 始终是原 Engine 发送任务/执行项。创建发送任务时原子保存与候选的关联，中断后复用同一任务。发布仍由原持久发送器和平台池完成，不增加另一套发送入口。ASSIGNED、READY 与 PUBLISHED 分别保留，真实发布必须有可信回执。

## 验证记录

所有行为验收使用临时数据库、合成账号、模拟连接、可替换时钟和随机源。真实用户发送与收费媒体调用为零。

| 检查 | 结果 |
| --- | --- |
| 互动新旧策略与 API | 85 项通过，含原 48 项、联合 32 项及 API 5 项 |
| X 官方连接契约 | 12 项通过，覆盖原生提及/回复身份及 429 响应头保存 |
| 桌面/手机页面 | 16 项通过；包括真实后端设置、草稿、稳定计划、处理任务、产品筛选、幂等扫描、无 P2/P3 审核入口与移动无溢出 |
| 完整离线回归 | 696 项通过（392.435 秒）；包含隔离 Cloudflare 控制层和 Chromium 契约测试 |
| Ruff lint | 全仓 `src`、`tests`、`scripts/telegram_online_replies.py` 通过 |
| 格式与差异 | 互动相关文件格式、网页 Prettier、JavaScript 语法、`git diff --check` 通过；全仓已有格式偏差见下文 |
| 源码包/安装包 | `python -m build --no-isolation` 可构建 sdist 和 wheel，示例纳入源码包 |

交付时，全仓 Ruff format 提示开发前已有改动/新增的 11 个文件存在格式偏差，当时未为本次功能改写这些片段：
`backends/browser.py`、`backends/tabbit.py`、`connections.py`、`core.py`、`execution_diagnostics.py`，
以及 `tests/test_browser_health_diagnostics.py`、`test_browser_navigation_denial.py`、
`test_browser_session_reconnect.py`、`test_owned_post_quota_policy.py`、`test_tabbit_connection.py`、`test_x_post_browser.py`。
新模块、互动相关测试和其余本次接口/页面改动已单独通过格式检查；`core.py` 的本次新增块遵守格式，原有片段保留。

2026-10-23 整合本地代码时已统一这些格式；当前全仓 Ruff lint、format 与网页 Prettier 检查均通过。

## 外部能力与启用

本次验证不代表官方 X 搜索/发布、实际模型服务或媒体服务已在用户账号接通。首版实际接入范围为已有 X API 链路；其他平台及账号私有来源未新增读取能力。公共相同查询目前独立执行，不宣称共享物理读取。

产品事实未替用户编造。示例中的事实和账号保持空，公开提及查询需填实际用户名；用户提供的 AIgram 手册与后续发行证据、Aurax 的真实能力/版本/限制须作为事实来源录入。未核实、未发行、失效或不适用事实不得进入自动承诺。

模型复用原 `AP_DRAFT_*` 配置。缺少服务时保留待判定/受阻；现有代码没有 Factory 生成服务连接，媒体建议仍按原能力缺口处理。指标、归因或费用未接通的数据保持未知。

重启到新代码后进入“高频互动 → 联合 P0/P1 模板”，按[配置示例](../examples/aigram-aurax-joint.json)填写原项目、查询、搜索/执行账号与事实。启用正式策略继续使用原持续授权路线，原全局停止、真实写入开关、账号/平台资格、额度和回执保护仍生效。详细步骤见[运行文档](high-frequency-interactions.md)。
