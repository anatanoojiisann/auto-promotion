# 账号控制与手机预检一致性优化

日期：2026-11-07。范围：本聊天的独立 Auto Promotion 整合工作区。无正式数据库迁移、服务切换或真实发送。

## 问题与实现

- 原任务控制路由把无账号范围的 offline_only 判断用于本地任务，导致另一个账号的离线意图影响当前账号。改为按当前账号判断；已有任务专属本地协调记录继续限制当前账号和原任务，不自动续期或转云端。
- 手机轮次预检遗漏实际执行器的账号控制条件。抽取 control_plan、control_snapshot、local_fence_source；预检和执行器共用同一逻辑。实际领取后仍在事务内复核，不把预检结果用作发送许可。
- 控制配置明确缺失时，在轮次准入之前返回原因，不创建交接记录，也不请求无效的 Fleet 能力检查。错误保留在对应平台，其他合格成员可以继续。选中多个平台的一次组合预检仍有有界等待；要求即时独立推进时可分别调用已有平台级启动入口，共享的设备操作仍由原设备锁串行。
- 正常任务的显式 start 仍记录用户启动意图。没有改变为“授权前必须启动、启动前必须取得授权”的循环依赖；账号控制检查在队列执行前生效，手机轮次另外在准入前检查。需要显式协调时，继续使用已有的任务级授权接口。
- 任务级本地授权支持实际绑定且身份新鲜的手机路线，使用手机资源而非原桌面资源检查占用。原账号身份、别名、未确认结果、控制权代次、任务清单和截止时间保护保留。未扩大非手机 Telegram 等旧渠道的授权能力。
- 移除 routing 中两段不可达的配置重复读取；将原执行器中的重复控制判断移至公共函数，避免维护两份条件。代码总体增加来自公共接口、错误隔离及测试，不能用净行数减少衡量本次效果。

## 状态含义与性能证据

预检不向云端申请控制权，也不创建任务、发送尝试或单次许可。account_control 区分已有有效本地证明、可用的本地控制条件和仍需云端协调；coordination_required 不代表已取得授权，执行器仍必须取得有效确认。远端故障、配置变化、设备状态变化仍可能阻止后续执行。

针对性测试覆盖：其他账号当前及过期离线设置、当前账号过期意图不得自动联网、任务专属授权不可借用、预检只读、预检后配置变化重新阻断、一平台受阻而其他五平台准入、逐平台启动不等待另一个平台的慢响应、全体控制缺失时零 Fleet 请求、手机控制协调后由原队列领取且零提交。

测试中的速度改善证据是调用数与并发关系：全体缺控制时能力请求由两个降到零；仅 Reddit 控制受阻时只请求仍可运行的社交服务；慢平台未返回时另一平台已获准入。尚未测量真机发布耗时，不能宣称真实发布提速百分比。

## 部署与边界

此分支不是运行中的正式版本。当前运行候选还包含原工作区其他未提交改动；后续启用必须将本次提交与这些改动做完整三方整合，核对实际加载指纹，再按获授权的空闲切换流程操作。不能用本分支直接覆盖运行候选。

保留原任务、未确认结果、配额、停止状态和全部服务数据。该补丁不重试本次已过期任务，不代表任何新平台发布已经成功。

相关回归结果和提交号在最终交接中记录；JUnit 结果保存在工作区忽略目录 `.local/account-control-regression.xml`。


## 修改文件

- src/auto_promotion/task_channels.py：统一控制条件，隔离账号范围，任务状态列表复用实际授权路由。
- src/auto_promotion/local_authorization.py：明确选择手机的任务使用已绑定且身份新鲜的手机资源，保留全部原控制保护。
- src/auto_promotion/phone_round_readiness.py：准入前检查控制条件，按平台返回原因，跳过已知无效的远端查询。
- src/auto_promotion/routing.py：删除两段不可达的重复配置读取。
- tests/test_account_control_readiness.py：新增控制一致性、跨 App 独立推进与真实适配器接口替身测试。
- docs/ACCOUNT_CONTROL_READINESS_2026-11-07.md：本交接。

## 验证命令

工作目录：D:/Projects/auto-promotion-phone-integration-20261106。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_account_control_readiness.py tests/test_task_channels.py tests/test_local_authorization.py tests/test_execution_channels.py tests/test_channel_recovery_faults.py tests/test_phone_round_preflight.py tests/test_phone_round_readiness.py tests/test_phone_platform_independence.py tests/test_phone_shared_connections.py tests/test_phone_social.py tests/test_phone_reddit.py tests/test_phone_rounds.py tests/test_phone_round_resilience.py tests/test_persistent_pools.py -q --tb=short --junitxml=.local/account-control-regression.xml
.venv/Scripts/python.exe -m pytest tests/test_account_control_readiness.py tests/test_task_channels.py tests/test_local_authorization.py tests/test_phone_social.py tests/test_phone_round_preflight.py tests/test_phone_runtime_identity.py -q --tb=short --junitxml=.local/account-control-final-status.xml
.venv/Scripts/python.exe -m ruff check src/auto_promotion/task_channels.py src/auto_promotion/local_authorization.py src/auto_promotion/phone_round_readiness.py src/auto_promotion/routing.py tests/test_account_control_readiness.py
.venv/Scripts/python.exe -m pip check
git diff --check
```

扩展回归：329 passed、26 subtests passed，204.51 秒。该轮在最终账号状态列表对齐前完成；随后运行上面第二组针对最终改动的回归，两组存在重叠，不应相加。测试服务给出的 publication 字段均为合成测试数据，不是真实平台发布。扩展测试出现一项已有 Starlette/httpx 弃用提示，未失败。

静态检查、依赖一致性与 diff 检查通过。未更改正式数据库、服务配置、实际账号或任务。

最终账号状态对齐后的受影响回归：146 passed、30 subtests passed，123.59 秒；JUnit 为 `.local/account-control-final-status.xml`。上述测试均已完成，代码静态检查、依赖检查和最终差异检查通过。
