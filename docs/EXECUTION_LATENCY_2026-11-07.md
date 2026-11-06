# 执行耗时、无效重试与 App 独立推进

日期：2026-11-07。工作区：`D:/Projects/auto-promotion-phone-integration-20261106`；分支：`codex/integrate-phone-updates-20261106`。本次接续账号控制一致性提交 `850fe0c1f772c86b3a77d2570e3883ef11b9e274`，只修改本地整合代码，没有启用正式服务或真实发送。

## 问题与解决方案

| 问题 | 解决方案 |
| --- | --- |
| 明确缺少控制配置仍每隔几十秒重试 | 在原队列任务记录 `control_wait`。`CloudControlConfigurationMissing`、`LocalChannelBindingMismatch`、`LocalAuthorizationTaskScopeMismatch` 等已明确列入的配置/绑定错误等待相关配置变化，期间不重新领取；变更后只重新检查原任务。其他账号的离线状态变化不会唤醒它。 |
| 暂时不可用仍固定间隔重试 | 仅对账号控制阶段、尚未尝试提交的故障，采用 30/60/120/240/300 秒退避，服务返回的更长 `retry_after` 优先。下一次机会超出原截止时间时停止安排重试。 |
| 队列期限比轮次允许发送的期限长 | 新建任务及恢复原待执行任务时，取任务原期限和本轮 `submit_deadline` 的较早值，只收紧，不重置倒计时。已尝试和未知结果保留原身份与查询流程。 |
| 组合预检等待最慢服务，健康平台无法先推进 | 能力服务逐个返回逐个准入；一个服务超时只阻塞其成员，已准入的其他平台可由原调度器继续启动。暂停、全局停止、任务变更、时间预算在每次准入时重新校验。 |
| 手机状态轮询频繁、内容未变仍写库 | 轮询从 0.2 秒逐步延长至最多 2 秒，同时受业务截止时间和单调时钟预算约束，系统时间回拨不会延长本次轮询。相同快照不重复写入；通信从异常恢复时仍更新关联状态。新的过期请求在能力查询前及创建意图前阻断；原已创建请求继续只读查询，不再次 POST。 |
| 无法区分耗时发生在哪个阶段 | 记录账号控制、手机身份/准备/执行和回执核验的开始/结束与单调时钟耗时；预检记录各成员控制与总预检耗时；轮次详情显示排队、执行、授权前后耗时以及失败原因和下一次检查时间。 |
| 耗时记录本身拖慢执行、结束后排队时长持续增长 | 耗时审计采用零锁等待的尽力写入；阶段日志加局部索引，详情每任务最多读取 500 条事件；调度 tick 不读取阶段明细。未提交成员的排队耗时在轮次结束/发送期限处停止增长。 |

## 含义与限制

- 只对确定尚未尝试的账号控制错误安排重试。已经提交、可能已经提交、结果未知、已暂停、已过期的任务不会因配置变化而重放。
- 耗时中的 `returned` 只表示调用返回，不等于推广成功；真实成功仍需要原任务中的独立平台回执。
- `draft_wait_seconds` 仅指轮次记录创建到第一次启动的等待，不包含创建轮次之前的人工作业，不能当作用户全流程耗时。`stage_timings.coverage=recorded_stages` 表示只有确实记录的阶段；旧任务或丢失的诊断不会补造耗时。
- 配置变化检查保留原任务、版本、账号、设备和停止条件。环境变量变化需要新进程加载；令牌文件替换可按文件版本被识别。诊断不暴露凭证或配置摘要。
- 各 App 的准入和队列独立推进；同一台手机的前台点击仍必须串行，避免互相切换界面。共享服务自身不可用时，该服务承载的平台仍会受到影响。
- 逻辑等待到期不保证底层已在途网络请求瞬间中断；HTTP 自身仍有超时。已有提交不能为了速度取消后重新创建。
- 新增的是日志查询索引，没有业务数据搬迁；目前只在隔离测试数据库中应用。

## 修改文件

- `src/auto_promotion/execution_waits.py`：配置等待、配置变化唤醒、控制故障退避、发送截止时间。
- `src/auto_promotion/execution_timing.py`：阶段计时和有界查询。
- `src/auto_promotion/queue_store.py`：原队列中的等待、期限恢复与可见阻塞原因。
- `src/auto_promotion/task_channels.py`：控制阶段计时和故障来源标记。
- `src/auto_promotion/phone_round_readiness.py`：按能力服务逐批返回预检结果及计时。
- `src/auto_promotion/phone_rounds.py`：逐批准入、状态和阶段耗时展示。
- `src/auto_promotion/phone_reddit.py`：共享手机调度的有界轮询、快照去重、过期检查及回执计时。
- `src/auto_promotion/phone_social.py`：社交 App 回执计时，复用共享手机调度。
- `src/auto_promotion/store.py`：阶段审计查询索引。
- `src/auto_promotion/runtime_identity.py`：新增执行模块纳入实际加载版本指纹。
- `tests/test_execution_latency.py`：配置恢复、退避、期限、跨 App 并发、停止、轮询与计时边界测试。
- `docs/EXECUTION_LATENCY_2026-11-07.md`：本交接文档。

## 验证

所有服务、账号、回执均为测试替身，未连接真实手机或平台。新增测试已经覆盖：配置不变零重复尝试、无关账号变更不唤醒、原任务原版本恢复、暂停/未知/过期不重放、暂时故障退避、另一账号仍能领取、原发送截止时间不延长、慢服务未返回前其他五个 App 启动、停止优先、慢服务超时隔离、阶段成功返回与抛错的耗时、过期前后禁止创建意图、轮询退避及快照只写一次。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_execution_latency.py -q --tb=short
.venv/Scripts/python.exe -m pytest tests/test_execution_latency.py tests/test_account_control_readiness.py tests/test_task_channels.py tests/test_local_authorization.py tests/test_execution_channels.py tests/test_channel_recovery_faults.py tests/test_phone_round_preflight.py tests/test_phone_round_readiness.py tests/test_phone_platform_independence.py tests/test_phone_shared_connections.py tests/test_phone_social.py tests/test_phone_reddit.py tests/test_phone_rounds.py tests/test_phone_round_resilience.py tests/test_persistent_pools.py tests/test_phone_runtime_identity.py tests/test_phone_protocol_upgrade.py tests/test_phone_protocol_receipt_gateway.py tests/test_phone_telegram_join.py tests/test_telegram_receipt_budget.py tests/test_queue_blockage_repair.py -q --tb=short --junitxml=.local/execution-latency-regression.xml
.venv/Scripts/python.exe -m pytest tests/test_execution_latency.py tests/test_phone_social.py tests/test_phone_reddit.py tests/test_phone_rounds.py tests/test_phone_round_preflight.py tests/test_persistent_pools.py tests/test_phone_round_resilience.py -q --tb=short --junitxml=.local/execution-latency-final.xml
.venv/Scripts/python.exe -m pytest tests/test_execution_latency.py tests/test_phone_social.py::test_lost_create_is_read_only_and_immutable_after_recovery tests/test_phone_reddit.py::test_lost_prepare_response_is_only_looked_up_never_reposted -q --tb=short --junitxml=.local/execution-latency-recovery.xml
.venv/Scripts/python.exe -m ruff check src/auto_promotion/execution_waits.py src/auto_promotion/execution_timing.py src/auto_promotion/phone_rounds.py src/auto_promotion/phone_round_readiness.py src/auto_promotion/task_channels.py src/auto_promotion/runtime_identity.py src/auto_promotion/store.py src/auto_promotion/queue_store.py src/auto_promotion/phone_reddit.py src/auto_promotion/phone_social.py tests/test_execution_latency.py
.venv/Scripts/python.exe -m pip check
git diff --check
```

扩展回归：398 passed、50 subtests passed，234.72 秒。随后补充单调时钟预算和实际重试时刻展示，受影响回归：155 passed，105.43 秒。最后补齐相同快照下的通信恢复状态更新，并修正新测试按单项选择时的样本注册，最终边界/恢复回归：24 passed，14.12 秒。新增文件最终有 18 项测试；以上结果有重叠，不能相加当作独立用例数。

静态检查、依赖一致性和差异检查通过。前两组回归各有一项现有 Starlette/httpx 弃用提示，未失败。早期测试样本中重复账号、严格请求字段和单项选择时的 fixture 注册错误均已修正。未进行真机或生产验收。

可复现的减负证据：模拟 4 秒等待仅查询原执行 4 次，相同快照只写入一次；时钟回拨仍在单调时钟 4 秒结束。缺失配置保持不变时零重复尝试，慢服务尚未返回时另五个平台已可入队。运行速度改善证据以调用次数、写入次数和并发先后关系为准，没有真机端到端耗时对比，不承诺实际提速百分比。

## 后续整合

正式运行候选仍包含原工作区未提交改动。后续加载本次更新前，须将本分支与保留的改动三方整合，确认原任务数据、服务配置和实际加载指纹；不能直接用这个工作区覆盖运行候选。未推送、未切换服务，也没有恢复历史未知或过期推广任务。
