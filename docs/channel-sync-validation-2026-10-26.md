# 通道交接、同步范围与耗时链修复（本地验证）

本轮基线为本机 `7c9ee153e128964fd10c96ffc7c3555cf8abcd44`。依据两份已完成的只读诊断开展修复；这些诊断没有给出实际 sender 启动 SHA，不能把本机源码或本轮测试当作现场运行版本。没有修改、迁移真实任务预留，没有重放 unknown、清理线上占用、访问真实账号、上传、提交、远端 CI、部署或 Windows 现场验证。无关私有评估材料不纳入公开提交。

## 变更

- `TaskChannelRouter` 持久记录账号通道原授权的 account/node/store/task/generation/channel/until，以及交接 owner、job、transition。状态查询与 ACK 必须完整匹配；同 generation 的完整旧 ACK 可以恢复，不重复记录 transition-start 审计。
- 相同 generation、相同完整授权 tuple 的较短有效 fence 可经显式 CAS 单调续期；不能缩短、换账号/node/store/task 或越过任务期限。Worker 状态与 ACK 包含持久 store binding，成熟 schema 只补该字段。
- Router 的成功、失败回调重查当前 owner、有效租约、generation、handoff 阶段和 transition。旧请求迟到时不覆盖已恢复的新 owner、ready 状态、错误或退避。`run_pool` 外层异常路径使用同一受限记录函数，未取得 context 不猜测当前 generation。既有 queue settle owner CAS 保留。
- legacy 派生的本地授权在最终 guard 再查原 grant generation、账号范围、node/store、期限与 through-task 范围；撤回、换代或过期立即失效。`cloud_acknowledged` 独立账号 fence 不依赖旧全局 legacy grant。
- 持久 outbox 未清不能换 generation。已知正有限 Retry-After 不缩短为固定 30 秒；同一交接在退避期内不再发网络请求。
- 常规调度同步先传播所有账号、任务状态和全局近期 quota ledger 的 metadata，再按活跃任务发送 job definitions，减少无关历史 job 重传。legacy 全局本地通道返回云端仍先完成原有全量回执/配额对账，不提前释放 fence。显式 full sync 的旧不可变冲突继续拒绝。
- 新任务同步观测单独持久为 `cloud_task_health:<task_id>`，带当前 policy/manifest、账号配置/连接修订、destination/node/store 绑定及观察有效期。成功/失败均以观察 token CAS 落库；旧响应不能覆盖新观测。无网络的 unchanged sync 不刷新服务器观察时间。该数据只供状态解释，不代替 claim/final authorize。
- `FrozenJobChanged` 继续拒绝不一致的 version/account/task/reference/pool/kind，保留 unknown 和 attempt。新增诊断只含批次 index、变更字段名、scope/revision/section、客户端计算的 record hash，不返回旧值、新值、正文或 token。

## 验证

- Python 通道/同步/runtime 组合：74 项，70 通过、4 个既有 Cloudflare HTTP emulator 项未启用而跳过。日志：`/tmp/ap-channel-cloud-python-20261026.log`。
- 新通道故障测试 8 项覆盖：完整 ACK、防重复审计、同代旧 owner 迟到失败、真实 run_pool 跨代迟到失败/成功、legacy 撤回、长 Retry-After、同代单调续期。使用 fake clock 和合成客户端，无真实等待或平台发送。
- 新 scope 观测测试 6 项覆盖：范围成功与历史全局冲突分离、配置失效、成功/失败两方向 stale CAS、重启退避、metadata 全账号/任务传播、冲突诊断脱敏。
- Control-plane Worker：130/130 通过；Telegram executor：63/63 通过。日志：`/tmp/ap-cloud-control-full-20261026.log`、`/tmp/ap-telegram-executor-full-20261026.log`。
- 两个 Worker 本机 dry-run 打包通过。日志：`/tmp/ap-control-build-20261026.log`、`/tmp/ap-telegram-build-20261026.log`。仅本地打包，不是 Cloudflare 部署或线上验收。
- 所改 Python 文件 Ruff check/format 通过，`git diff --check` 通过。最终全套 Python/UI 与独立审查由主执行线程合并报告。

## 保留边界

全局停止、quota、账号限制、最终身份/权限、租约 fencing 和 unknown once-only 继续有效。metadata 仍传播全量 task headers；历史 `FrozenTaskChanged` 若在 header 阶段发生，继续 fail closed。本轮针对已证实的历史 job 同步冲突，不声称隔离了所有 task/header 冲突，更不通过覆盖 frozen records 解决它们。

现场同 generation 重试日志不能单独证明具体底层错误。本次是对可离线复现的错误 tuple、迟到回调和同步范围问题的修复；没有宣称那份现场日志已加载或验证这些改动。
