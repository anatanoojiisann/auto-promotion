# 执行占用、重复启动与同步耗时修复：本地验收

本轮基于本机提交 `7c9ee153e128964fd10c96ffc7c3555cf8abcd44`，工作区为 `<workspace>`。本报告的离线验收阶段尚未提交、上传、推送、运行远端 CI 或部署；没有读取真实账号、迁移生产预留、重放 unknown、发送测试消息或修改凭据/安全设置。无关私有评估材料保留在本机且不纳入公开提交。

两包日志已证实旧占用导致无 attempt 的重复昂贵预检、同代交接重复及对话整轮耗时，但没有提供 sender 启动 SHA。附件中的磁盘源码片段（包括新增启动 helper）不等于实际运行版本；未获得完整现场 helper 源码。本轮针对可取得的 7c9 源码与离线可重现行为修复，不能认定现场已加载或已经修好。对话耗时、Start API 耗时、到期调度延迟分开看待；具体现场 until 的形成原因仍未证实。

## 修改及行为证据

- `interaction_intents.py:intent_blocker` 只读复用最终 claim 原有判断：项目 alias、历史 accepted/unknown、人工证据、同候选明确未提交重试例外不变。`core.py` 在 health/preflight 前拦截已知冲突，仍在最终写事务调用 atomic claim。冲突不清除、迁移或抢占他人预留。
- `queue_store.py` 为无 attempt 的冲突保存 owner 引用和至少 60 秒的持久等待；重启保持，原占用证据变化可重新评估。重新评估仍受到期、过期、暂停/取消、attempt/unknown、账号 pacing 和最终授权约束。旧 worker 的 settle 必须匹配租约 owner；迟到清理必须匹配 intent 当前 item，不能删除新 owner 的预留。
- 已 active 的重复 Start 返回当前任务及 `already_started=true`、空 processed，不把重复按钮当调度 tick，也不重复 health/sync/dispatch。仍只读核验冻结内容、当前配置、连接身份与自动化授权。首次 Start、真正发送和 Telegram membership 继续执行必要检查。并发首次启动只产生一次调度；单调 stop audit revision 与 payload/policy/manifest 校验阻止旧 Start 吞掉较晚 Pause 或新草稿。同一个 fake-clock tick 也不会失效，明确对 paused 再次 Start 仍可恢复。
- X 账号读取 Retry-After 在账号 lane 内落库，每个后续入口及最终 native authorize 再查。有效长等待和既有更晚 deadline 取最大，不缩为后备 900 秒。资源失败不当作主搜索限流；未证实 scope 的既有联合扫描全平台保守 guard 保留，不以切账号继续读取规避平台限额。
- 通道交接保存完整 account/node/store/task/generation/channel/until tuple，状态/ACK/成功失败回调均检查 transition owner 和 CAS。同代完整 ACK 恢复不重复开始审计，旧成功/旧失败不覆盖新代状态；显式同代续期只允许同 tuple 单调延长。legacy grant 撤回/过期/换代在最终 guard 失效。outbox、配额、未知回执及共享锁保留。
- 常规云调度传播全局 metadata 后按活跃 task 同步 job definitions；显式 full sync 与 frozen record 不可变拒绝保留。任务范围的近期同步健康独立保存、绑定任务/配置/连接/节点/store，防迟到结果覆盖。旧全局错误仍作为历史展示，不用覆盖 frozen record 来“修复”同步。
- UI 按 task 合并 pending Start，跨重绘禁重；Start/GET/poll/编辑的迟到响应检查 route/panel/request 代次。排期与服务重试分别显示，占用任务/item 引用可见；新错误、账号阻断、全局停止和 quota 保持优先。已受理请求不会因关面板被假称取消。
- Playwright/仓库生成的 Tabbit 脚本识别可信主 frame 搜索请求的 429、搜索后 challenge，校验候选 URL host/scheme，采用共同 deadline 和主搜索请求预算；媒体/子 frame/无关 GraphQL 分开计数。未完成 Tabbit 回执沿用稳定任务名；清理失败不覆盖已检测阻断。Browser Use 仍只报告原人工观测，不伪装实时扫描。
- 新只读元字段贯穿 native → Engine → task/export。主搜索请求计数是受观察的子集，不是全部 RPC/网络次数；旧外部回执缺少时间/计数则为未知。返回可见样本不代表完整搜索覆盖，不前移完整覆盖 watermark。运行 source fingerprint 扩入本轮关键控制文件。

最初 `test_execution_waits.py` 行为红测重现：同一已知冲突两轮仍执行 health/preflight、active 重复 Start 仍调用 provider、队列仍领取冲突 job。后续红测重现晚 Pause 被旧 Start 吞掉、迟到清理删除新 owner，以及陈旧草稿冻结。修后 19 项 focused 全绿，另有真实 Native→Engine→导出及最终授权竞态测试。日志保留 `/tmp/execution-waits-*-20261026.log`、`/tmp/start-pause-race-red-20261026.log`、`/tmp/intent-cleanup-start-payload-red-20261026.log`。这些 fixture 只使用临时数据库、fake clock/barrier 和合成 provider。

## 同类检查范围

| 路径/模式 | 结论 |
| --- | --- |
| intent 预判/最终 claim、迟到 cleanup | 修复预判位置与 owner CAS；最终原子竞争保护保留 |
| queue send/join、settle、到期/过期、attempt/unknown | 冲突 bounded waiting；旧 settle owner 拒绝；终态不重领 |
| Start/resume、暂停并发、配置/身份/opt-in | 重复 Start 幂等；首次与最终检查保留；较晚停止优先 |
| 读取/强制健康、Retry-After | 账号持久 gate 与最终授权重查；有效长等待不缩短；未知平台 scope 保守 guard 保留 |
| 云 full/scoped sync、健康迟到、future 状态 | 新任务观测与历史全局记录分离；拒绝 frozen 变更不弱化 |
| handoff/renew、legacy grant、stale generation | 完整 tuple、同代恢复、双向 stale CAS 与撤回校验 |
| UI/API 重复与迟到响应 | pending 合并、route/panel 代次、当前 gate 文案 |
| 高频搜索限流/资源错误、deadline/预算 | 主请求可信分类；不完整样本/未知计数不冒充完整或零 |

这是对上述实际模块和模式的检查，不代表整个程序不存在其他缺陷。历史 `FrozenTaskChanged` 在 metadata header 阶段仍 fail closed；外部 Tabbit 执行器、未提供的现场代码、真实平台和 Windows 实例不在本机离线验收范围。

## 验证记录

首轮完整 Python 日志 `/tmp/execution-reliability-full-python-20261026.log`：925 项，921 通过、1 error、3 跳过，232.906 秒。唯一 error 是 MCP 旧 fixture 把重复 Start 当 worker tick，访问空 processed 导致 IndexError。已改为先验证幂等返回且 sends=0，再由隔离 fake worker 显式 tick；没有放宽生产授权。修后 MCP/相关 scope/API 27 项通过，日志 `/tmp/execution-reliability-final-followup-20261026.log`。首轮失败没有计为通过。

最终完整 Python：928 项，925 通过、3 个 Windows 专项跳过，215.526 秒，exit 0。日志 `/tmp/execution-reliability-final-python-20261026.log`；浏览器和本机 HTTP emulator 均显式启用。最终 Ruff check 通过、133 个文件 format 通过、配置 Prettier 通过、Python sdist/wheel 构建通过，日志 `/tmp/execution-reliability-final-{ruff,prettier,build}-20261026.log`。其他已完成结果：

- Control-plane Worker 130/130；Telegram executor 63/63。仅本机测试。两 Worker 本机 dry-run 打包通过，没有调用 Cloudflare 发布。
- 通道/scope/runtime focused 74 项：70 通过、4 个未启用 HTTP emulator 跳过；最终完整 runner 会启用隔离 emulator。
- 浏览器相关 67 项启用真实隔离 Chromium，通过；Tabbit 15 项通过（与组合有重叠，不相加）。新增观察契约 16 项含仓库生成 JavaScript 的 9 场景。
- UI main 45、pools 合成链、interactions 22；最后 focused remainder faults 64、blocks 11、Telegram reads 11、task Start 15 全通过。证据分两进程，不冒称单次整套全绿。纯 readiness 最终 10、实际 HTTP API 4、既有 task readiness 30、cloud workflow 8 通过。最后 scope 错误优先显示窄修的 UI 16 项也通过：本任务近期绑定失败不能被较旧 global connected 掩盖，合法独立 local scope 仍可继续等待排期，同时保留云错误观测。
- 本机环境：macOS arm64，Python 3.14.2、Node 24.13.0。Python 浏览器使用 Chromium cache 1208（Chrome 145），UI 使用 Chrome 155。未运行 Windows 现场和真实社交平台测试。

详细日志与复现范围见 `docs/channel-sync-validation-2026-10-26.md`、`docs/start-readiness-ui-validation-2026-10-26.md`、`docs/browser-read-observation-contract-2026-10-26.md`。各组件通过数存在交叠，不合计成唯一测试总数。

## 公开提交准备

在上述离线验收后，用户明确授权将两批修复上传公开 main 并监控 CI，不包含部署。公开文档已用占位符替换本机路径、部署账号与历史生产版本标识；无关私有材料不纳入清单。实际上传 SHA、远端 CI 和拒绝/成功状态以协调线程的后续结果为准，不能把本地 build 或测试当作 CI/部署成功。

## 离线复现

```sh
PYTHONPATH=src:tests TMPDIR=/private/tmp <python-env>/bin/python -m unittest test_execution_waits
AUTO_PROMOTION_PYTHON=<python-env>/bin/python \
AP_BROWSER_EXECUTABLE=<isolated-test-chromium> \
AP_TEST_TIMEOUT_MS=900000 TMPDIR=/private/tmp node scripts/test_offline.cjs
<python-env>/bin/ruff check src tests scripts/telegram_online_replies.py scripts/telegram_read_compare.py
<python-env>/bin/ruff format --check src tests scripts/telegram_online_replies.py scripts/telegram_read_compare.py
npm run format:check
<python-env>/bin/python -m build --no-isolation --outdir <temporary-artifact-directory>
```

这组命令需要已安装依赖和本机隔离浏览器，不读取用户 profile。测试 runner 创建临时 HOME、数据库、账号目录和 localhost emulator，禁止真实写入并在结束后清理。3 个跳过项为两个 Windows DPAPI 和一个真实 PowerShell/CIM 环境验证；准确远端提交的 Windows CI 结果需另行核验。最终可复核文件清单由提交准备时的逐 blob SHA 生成，旧基线 tree 不代表本轮源码快照。

## 独立审查

[独立安全审查](diagnostic-fixes-independent-security-review-2026-10-26.md) 已完成，无已确认、未闭环的 P1/P2。审查者独立运行 161 项控制链组合、53 项浏览器组合、16 项最终 task UI、64 项广泛 UI faults、10 项最终 scope 以及 9 项 Worker 通道/HTTP fixtures，均通过；这些组合存在重叠，不相加。15 个关键源码 SHA-256 与其审查快照一致。公开交付复核未发现相关文档中的本机个人路径、现场账号、消息正文或现场 URL；只上传源码、合成测试和必要脱敏文档，不含私有附件和原始日志。
