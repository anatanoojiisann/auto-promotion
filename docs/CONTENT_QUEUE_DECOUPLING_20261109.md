# 内容队列解耦与手机流程简化 第一批交付

日期：2026-11-09。开发分支：`codex/content-queue-decoupling-20261109`。工作区：`D:/Projects/auto-promotion-content-queue-20261109`。

本批实现内容持久保存、幂等交接到原任务、版本化投递期限、延后身份观察和六状态结果查询。全部改动位于独立工作区，没有切换运行服务或迁移实际业务数据库。

## 验收范围

按用户最新指示，本轮真机验收包含 Facebook、Telegram、TikTok、X、Reddit、Instagram 六个平台。Discord 暂不纳入。内容模型继续支持已有七个平台，不代表七个平台发送适配已完成。

当前真实手机互动及回执验收为 **0/6，未完成**。离线测试不能替代该验收。

## 已实现

1. 内容池独立于账号绑定、手机、模型调用和执行时间。保存目标、确定正文、素材引用、上下文和不可变摘要；普通保存不会建立发送任务。
2. 同一幂等键重试返回原内容；相同键正文变化被拒绝。批次事务先绑定内容与待交接请求，创建任务时原子关联批次；断线后恢复同一任务和执行项。
3. 内容批次仅建立手机通道草稿，继续沿用原有 `start_task`、冻结清单、原发送队列、额度、租约和提交许可，不增加第二条发送路径。
4. 新策略 `dispatch_policy=phone_queue_v1` 只允许 `execution_method=phone`、`execution_channel=local`。统一入队、预检和领取的迟到判断，取消普通互动项的隐含15分钟淘汰，按明确的任务截止时间处理。
5. 新策略明确启动时不接触手机或重复桌面健康探测；实际执行继续使用既有新观察和提交前检查。真实账号仍须存在手机绑定，停用账号拒绝启动；全局停止、冷却、账号权限和原许可不被清除。
6. 六状态结果为待执行、执行中、待核验、已发布、需处理、已取消。没有已验证回执、原生标识和发布引用的 confirmed 不直接显示已发布；未知提交即使停止后续动作仍待核验。
7. 修复明确选择手机方式的 TikTok 任务误走网页/API的目标所有权或字符校验；使用既有受限手机文字动作校验，不扩大允许动作。
8. 已绑定的内容任务禁止通过普通修改执行项接口覆盖正文。内容改变须生成新内容记录并重新建立批次。

## 使用入口

MCP 新增三个工具：`save_interaction_content`、`list_interaction_content`、`materialize_content_batch`。保存和交接工具均为本地准备，不直接对外提交；随后使用原 `start_task` 明确启动一次。

已认证本地 HTTP 接口：

- `POST /api/content-queue` 保存内容。
- `GET /api/content-queue` 按上限读取内容。
- `GET /api/content-queue/{content_id}` 查询内容和绑定。
- `POST /api/content-queue/batches` 幂等建立原任务草稿。
- `GET /api/content-queue/tasks/{task_id}/status` 读取控制状态及六状态投影。

正式 DTO 示例由 `ContentDraft`、`ContentBatch` 的 JSON Schema 描述。本批没有修改群控的正式执行协议。

## 数据与兼容性

新增内容、批次和绑定三张表，沿用原 Store 的加法初始化及 schema 检查。旧任务不自动升级策略，不改变原截止时间、冻结摘要、未知结果或原生预约发送语义。

历史状态和尝试记录保留。绑定关系与原任务在同一事务内建立；写入执行项在原事务内检查关联批次和原内容，不因响应丢失另建任务或发送项。

新内容生产可以长期保存；本批任务侧仍沿用现有 **必填 ends_at** 契约。可选业务截止时间、永久等待的新版正式契约尚待后续开发，不能把这一部分记为已完成。

## 尚需集成与开发

- 配套加载新的 API、MCP 和发送进程，先在隔离数据库验证 schema 和恢复，再由集成负责运行切换及历史数据核对。
- Temporal 的事件通知和条件等待尚未修改，仍须实现可靠通知、低频对账及复核与任务终态同步。
- 自动定位、键盘与布局变化、锁屏唤醒、旧草稿恢复仍属群控侧后续开发，未因本批队列代码而完成。
- 六平台的实际账号绑定、提交适配和独立回执逐项核验。只读配置检查发现，控制侧发布适配登记名单目前仅含 Telegram，Reddit 提交关闭；候选配置中的账号映射和界面适配不等于真实身份或发布通过。
- 前端暂使用既有界面；六状态已从任务查询及专用接口返回，批次管理和统一展示仍需接入。

## 运行基线

以手机发送服务的 `36fd668ffe5958ebafceef906c6427be62d92dc8` 为开发基线。确认当前 MCP 的 `2653b507d0ce3b649ace1ba0374515f240025734` 是其祖先，因此没有丢弃该 MCP 版本的既有修复。

Python 3.12 独立虚拟环境。手机契约、协议及 Fleet 依赖使用手机代码 `41731b8fc139e05cdbe8074b10e95516a4040200` 的包构建副本，仅存入本工作区忽略的 `.local`；未改其他工作树。

## 验证入口

```powershell
.venv/Scripts/python.exe -m pytest tests/test_content_queue.py tests/test_mcp.py tests/test_task_readiness.py tests/test_readiness_scope_display.py tests/test_task_tools.py tests/test_task_channels.py tests/test_persistent_pools.py tests/test_api.py tests/test_start_api_idempotency.py tests/test_phone_round_readiness.py tests/test_phone_social.py tests/test_workflow_unification.py tests/test_async_visual_preparation.py -q --disable-warnings --maxfail=3 --junitxml=.local/content-queue-final.xml
.venv/Scripts/python.exe -m pip check
git diff --check
```

测试覆盖内容无手机保存、重启、并发去重、同键冲突、原任务恢复、冻结内容保护、HTTP认证、MCP工具、旧策略兼容、明确截止、停止及账号停用、原手机准备和提交流程。全部使用明确标记的离线替身或模拟数据库，不发送社交消息。

## 本批验证结果

最终组合回归：283 passed，46 subtests passed，2 warnings，用时264.82秒。子测试不与主测试数量重复相加。JUnit保存在忽略的 .local/content-queue-final.xml。静态检查、pip依赖一致性和git差异检查通过；wheel构建成功且包含新增模块，未混入运行数据库、截图或环境文件。

六App真机发布和回执仍为0/6。未将单元测试中标记为模拟的设备、协议回执替身或健康接口成功算作真机验收。

## 精确文件清单

- src/auto_promotion/api.py
- src/auto_promotion/core.py
- src/auto_promotion/mcp_server.py
- src/auto_promotion/models.py
- src/auto_promotion/phone_round_readiness.py
- src/auto_promotion/queue_store.py
- src/auto_promotion/readiness.py
- src/auto_promotion/store.py
- tests/test_mcp.py
- docs/CONTENT_QUEUE_DECOUPLING_20261109.md
- docs/CONTENT_QUEUE_TEST_ENVIRONMENT_20261109.json
- docs/PHONE_ACCEPTANCE_20261109.md
- src/auto_promotion/content_queue.py
- src/auto_promotion/content_schema.py
- src/auto_promotion/delivery_policy.py
- tests/test_content_queue.py
