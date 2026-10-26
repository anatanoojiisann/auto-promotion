# 2026-10-26 耗时与交接链修复：独立安全审查

## 结论

独立审查在本地冻结代码中未留下已确认、未修复的 P1/P2。审查中实际复现的启动竞态、通道旧回调污染、legacy 授权撤回遗漏及任务同步失败展示问题，均已由实施人员修复并复验。此结论限于本仓库本机源码、离线合成数据库、fake clock/barrier、隔离浏览器及 Worker fixtures；不构成生产部署或真实账号验收。

基线 HEAD：`7c9ee153e128964fd10c96ffc7c3555cf8abcd44`。审查对象为该基线之上的当前未提交改动及新增测试/模块；不是已发布版本。工作区为 `<workspace>`。审查者没有修改功能文件、stage、commit、推送、远端 CI、部署、真实账号调用、凭据更改、线上预留迁移或 unknown 重放。无关私有评估材料不纳入公开交付。

最终完整 Python 由实施线程运行，审查者已读取日志结尾确认：928 项、925 通过、3 个 Windows 专项跳过，215.526 秒，exit 0。下表另列审查者独立运行的检查。组合可能存在导入继承重复，不将数量称为唯一场景数。

## 已闭环发现与防护复验

1. **确定的 interaction intent 冲突仍先做外部预检。** 独立脚本原复现两轮 health/preflight 均被调用、attempt 为零且最终仍报占用；修复后两轮外部调用为空、attempt 为零、原 owner 不变。`interaction_intents.py:56/112` 共用 blocker 与最终原子 claim；`queue_store.py:354/373/528` 以持久等待减少热循环，owner 变化后才允许重查。等待没有将 attempted、unknown 或过期项目重领。
2. **早期 Start 可覆盖稍后 Pause。** 在真实 start_task 校验中注入 Pause，旧代码最终重新 active；修复后抛 Conflict，保持 paused、attempt 为零。`core.py:1101/1161/1170/1458` 将幂等快返、单调停止 audit ID、清单/策略/内容摘要二次事务检查结合，保留本地授权检查，避免把重复 Start 当执行 tick。
3. **迟到通道失败可污染同代新 owner / 跨代新通道。** 分别通过 Router 及真实 run_pool 外层异常路径复现。修复后保留新 owner/generation/ready、error=None、retry=0。`task_channels.py:200/215` 和 pool runtime 共用 owner、租约、generation、handoff、transition CAS；未取得 context 不猜当前 generation。成功方向与完整 ACK tuple 的回归也通过。
4. **legacy 本地授权撤回后旧 context 仍可发送。** 独立 synthetic configure_independent→prepare→use_cloud→final guard 原放行；修复后拒绝旧 context。`task_channels.py:67/152/182` 最终核对原 grant 的 generation/account/node/store/任务范围/期限，cloud_acknowledged 则保持独立授权语义。
5. **晚到的旧 intent cleanup 可清掉新 owner。** `interactions.py:1054` 改为 candidate+expected item 所有者 CAS，退休操作也检查当前 owner，保留已有 attempt/receipt 与未知终态约束。
6. **scope 成功/失败与全局观测展示混淆。** 新成功仅能解释严格较旧的全局 FrozenJobChanged；全局 stop、quota、账号限制继续优先。最后独立复现的 fresh exact-bound 本任务失败＋旧 global connected 原被显示成排期待执行，现显示本任务同步等待及自身 retry，同时保留原排期；错误/过期/无效 scope 不误阻断，合法本地 fallback 继续可用并展示云失败观测。`cloud_control.py:59/466/530` 生成有绑定和期限的只读观测，`readiness.py:99/118/293` 只解释状态，不代替 claim/final authorize。
7. **UI 异步旧响应与重复 Start。** 同任务开始请求涵盖 POST→refresh→render 的完整 pending 合并；路由、panel、render 代次阻止迟到成功/失败覆盖新页面。修改 execution settings 自身增加 panel 代次时，当前 GET 错误仍可见；过期响应仍抑制。全局停止/占用 owner/计划时间/服务 retry 分开显示。`web/app.js:1067/1101/1275/1311`。
8. **X 主读取 429 与资源错误混计、长等待被缩短。** 仅可信 X 主 frame/精确搜索主请求触发只读 rate limit；媒体/第三方/子 frame 故障独立计数。真实有效长 Retry-After 不截为固定等待，持久账号 max 冷却在入口继续拒绝；不换账号、代理或 API 绕过。`read_cooldowns.py:20/34/68/98` 的观测与冷却连入 native/core/export，旧 unknown 计数保持未知。

另外独立双 worker barrier 在双方都通过早期预检后竞争同一事件，结果只有一个 attempt、一个合成 send；证明早期 gate 没有替代最终原子 claim。

## 独立执行证据

全部为本机离线执行，未调用真实平台发送：

| 检查 | 实际结果 | 日志 |
|---|---|---|
| Core/queue/channel/scope/blocks/private receipts/interactions/pools 组合 | 161 tests，OK，45.492s | `<local-evidence>/diagnostic-fixes-independent-python-20261026.log` |
| Playwright/TabbitJS 只读观测及既有 X safety/contracts | 53 tests，OK，48.838s | `<local-evidence>/diagnostic-fixes-independent-browser-20261026.log` |
| task start UI 最新冻结代码 | 16/16，failures=[]，exit 0 | `<local-evidence>/diagnostic-fixes-independent-ui-final-20261026.log` |
| UI broad faults（390/1440 宽度、迟到成功/失败、当前错误） | 64/64，failures=[]，exit 0 | `<local-evidence>/diagnostic-fixes-independent-ui-faults-20261026.log` |
| 最后 scope failure / local fallback 复验 | 10 tests，OK | `<local-evidence>/diagnostic-fixes-independent-readiness-final-20261026.log` |
| Worker task-channel + isolated HTTP fixture | 9/9，exit 0 | `<local-evidence>/diagnostic-fixes-independent-worker-20261026.log` |
| `git diff --check` | 通过 | 审查工具输出 |

独立故障脚本保留 `<local-evidence>/preflight-intent-review-red.py`、`<local-evidence>/review-start-pause-race.py`、`<local-evidence>/review-stale-channel-error.py`、`<local-evidence>/review-pool-stale-channel-error.py`、`<local-evidence>/review-legacy-channel-revocation.py`、`<local-evidence>/review-intent-final-race.py`。它们仅使用临时数据库/合成 provider。脚本修复后结果已反馈实施线程，不读取生产状态。

实施人员另报告 Worker 130/130、Telegram executor 63/63、全 UI/浏览器及本地打包通过；完整结果由主线程依据实际日志汇总。本报告不把他人运行结果当作审查者独立运行；已读取最终完整 Python、Ruff format 133 文件及 sdist/wheel 成功日志。

## 保留边界与待现场条件

- metadata 同步仍传播全量 task headers，历史 `FrozenTaskChanged` 在该阶段继续 fail closed。此次隔离的是已证明的历史 job definitions 冲突，不承诺所有历史 task/header 冲突都自动恢复；不覆盖 frozen 记录消冲突。
- 旧通道缺少新完整 authority tuple 时需新交接，不能凭新 UI 状态获得发送权。没有线上迁移/混跑/重启真实服务验收。
- 日志未证明实际 sender 启动 SHA。现场磁盘源码片段、导出版本、本机基线与本轮源码均不能当运行时 SHA。无法据此断言所有现场延迟都由本轮缺陷造成；未覆盖时间也不是纯推理耗时。
- 外部对话使用的 Tabbit 执行器不一定执行本仓库生成脚本；本轮只能证明本仓库适配器契约。Browser Use 仍保存原人工身份观测并标 live_probe=false，没有升级成自动实时身份验证。
- X 搜索始终是有界 DOM 样本：coverage_complete=false、next_cursor=null。fingerprint 不是游标或全时间窗水位。主请求计数不等全部网络包/RPC；资源 ERR_ABORTED/ERR_FAILED 不等检索失败次数。
- 共同扫描 deadline 和请求预算限制读取流程，不应被解释为包含浏览器启动、调度、IPC/系统阻塞的整个工具硬实时承诺；Tabbit 完成后的清理另有至多 5 秒窗口。
- 没有真实账号、Cloudflare 线上、Windows、验证码解除或消息可见性验收。没有为了性能绕过身份、权限、全局停止、冷却或 once-only，也没有 replay unknown。

## 最终判定

允许交付本次本地修复及独立审查结果，无已确认未闭环的阻断发现。用户后续明确授权将两批相关修复上传公开 main 并运行 CI；审查者仅整理本地公开文档，没有执行上传。公开前复核：15 个已记录关键源码文件的 SHA-256 与独立审查快照一致；交付清单排除附件、压缩包、原始日志、私有评估材料及凭据。相关公开文档以占位符表示本机目录和部署账号，未发现本轮文档含现场账号、消息正文或现场 URL。实际上传 SHA 与 CI 状态以协调线程的后续结果为准。未授权部署，生产发布/现场效果不在此签核范围。
