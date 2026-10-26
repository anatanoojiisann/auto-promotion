# Telegram 读取降频：独立安全审查

审查时间：2026-10-23。工作区：`<workspace>`。
本地 HEAD 为 `40d270b2b2abfbe0be029782d1731a0bc995bcda`；审查包含此工作区的未提交实现差异，不将 HEAD 当作这些实现已经发布的证明。

范围为本轮 Telegram 读取策略、私有预算/缓存/游标、原生桥、Campaigns/Growth/Statistics 消费者、配置 API/UI，以及 Cloudflare 代码的本地离线 fixture。审查者未修改功能文件，未运行真实账号、发送消息、访问生产任务、认证、操作验证码、上传、推送或部署；Windows 现场未验证。

## 审查结果

在以下明确范围与限制内，本轮已复现的 P1/P2 均已修复并独立复验，未剩已确认的阻断发现。此结论是本地代码安全审查，不是生产发布或平台限流效果的现场签核。完整项目测试、打包与 lint 的最终汇总由实施报告另列；本报告仅将实际独立执行的检查记为通过。

| 已发现问题 | 修复与独立复验 |
| --- | --- |
| 给 execute claim 增加读取配置破坏不可变回执摘要，重启后不能证明已完成发送 | 发送 claim 保持原样；原生桥在摘要计算后建立 backend transport 副本。真实 Engine→native payload 与暂停后恢复用例通过，保持 once-only。 |
| discover 用可变搜索目录的数字 index 续页，目录重排后跳过目标 | 当前轮目录持久快照绑定 token，跨页保留 visited。独立 A/B 重排脚本修复前 A→A 且 complete，修复后 A→B 且 complete。 |
| Engine.discover 丢弃 backend cursor，下一次永远从第一组开始 | 候选页与 cursor 同事务持久化，入口保留页完成状态并续用游标；现有资格、任务范围与候选上限继续生效。 |
| Statistics 轮预算不包含 unknown/scheduled 的 reconcile | 核对先获得剩余额度，其实际读量从同轮扣除；读量未知停止该账号本轮工作。独立 cap=4 脚本从 verify4+feedback4 变为仅 verify4，实际与报告均为4。 |
| 初始无 cursor 的 reader 在失去 lane 后能覆盖另一 worker 的新 cursor | 页提交事务检查 owner/过期时间，初始不存在也做 CAS，首次绑定 identity 后检查最终 key。两条独立故障注入分别验证失去 lane、保持 lane 但出现竞争 cursor，均拒绝提交并保留新 worker 的 last=5。 |
| feedback 新 peer 字段的 string/int 合同不一致；既有 cursor 未绑定身份/peer | 使用规范十进制 peer 字符串，原生回执保存 peer，fresh identity/peer 与持久反馈绑定核对；事件、绑定和 checked cursor 同事务。真实 Backend→Native→Core 的反馈持久化用例通过。 |
| 云观察接口全历史扫描后仅返回最后100/50条却推进高水位 | 收敛为原有上限内的 TTL 观察样本，不建立消费游标或全历史高水位。冷读有界，旧 ID 新置顶可在 TTL 过期后出现。 |
| 云 reader tick 的响应丢失/JSON异常可能把已消费读取当作零成本 | 未知计数及异常均耗尽本轮剩余额度，记录 unavailable/null，不继续下一个计划。独立 control-reader fixture 通过。 |
| SDK 高级发送调用内部的读取被预算拒绝，实际 mutation 尚未派发却被记为 unknown | 仅在可信 `_call` 计数、mutation 未派发且没有既有回执时记录 failed_known/confirmed_not_submitted。独立内部解析预算拒绝脚本修复前 unknown，修复后 failed_known/sends=[]；同时抛出 disconnect 错误时仍保留原预算原因。mutation 已派发无 ID、既有 ID/引用的 unknown 继续保留，attempt 不重新排队。 |

## 独立执行证据

- Python 定向组合：161 项通过，55.754 秒。包含预算、增量分页、消费者、配置 API、原生后端、Telegram 限制、私有回执恢复、Statistics、Campaigns、Growth。日志：`/tmp/telegram-reads-independent-python-final.log`。
- 后补 Python 最新定向组合：50/50 通过，6.933 秒。日志：`/tmp/telegram-reads-independent-latest-focused.log`。包含 Growth 每轮预算、feed 并发和重启续读、异常计数与新增 lane fencing。
- 最后发送边界、Telegram 限制、私有回执恢复组合：48/48 通过，6.581 秒。日志：`/tmp/telegram-reads-independent-submit-boundary-final.log`。另对内部预算拒绝加 disconnect 错误进行了独立故障注入。
- 最后 teardown 保留原因修复后的增量/TG限制组合：26/26 通过，5.659 秒。日志：`/tmp/telegram-reads-independent-final-teardown.log`；独立 `/tmp/review-budget-inside-send-disconnect.py` 同时确认无发送且保留 failed_known 和原预算原因。
- 云读取策略离线测试：14/14 通过。日志：`/tmp/telegram-reads-independent-cloud-policy-final.log`。最后增加同 operation 并行扫描额度预占，以及超过96KiB时跳过可选缓存但保留完整有界样本的用例。
- Control reader 离线测试：7/7 通过，包含同轮额度、未知计数和 transport/JSON 异常。日志：`/tmp/telegram-reads-independent-control-final.log`。
- 本地 Miniflare 真实 router + 合成 client：1/1 通过；并发账号 busy、持久预算、已有 ID 回执恢复、无 ID verify 零 RPC 均覆盖。
- 隔离 Chrome UI：11/11 通过；每个 HTTP 请求由 fixture 拦截，使用全新临时 profile。覆盖陈旧版本、重复保存、取消及迟到响应、错误与移动端。
- `git diff --check` 通过。独立故障脚本保存在 `/tmp/review-discover-reorder.py`、`/tmp/review-statistics-budget.py`、`/tmp/review-first-page-cas.py`、`/tmp/review-first-page-empty-cas.py`，均未连接平台。

首次 Miniflare 在沙箱内绑定 loopback 被 EPERM 阻止；默认 Playwright 浏览器路径也不存在。使用已安装 Chrome 的临时 profile，并按明确的离线测试理由获准运行后，两项已成功完成；没有安装浏览器或注册凭据。

## 保留的安全边界与解释

1. 新预算统计的是原生客户端逻辑调用尝试（批请求逐项计数）；失败尝试也扣额。它不是消息条数，也不代表 Telethon/teleproto 底层网络重试数。wire/retry 未观测时保持 unavailable/null，不据此声称已证明现场限流根因。
2. 本机预算由同一 accounts 根目录内、稳定 Telegram identity 的私有 SQLite 账本持久化。session/connection revision 与 label 不重置已用量。正常应用入口带账号 DB context 并读取当前策略；首次无 context 的 standalone profile 调用使用默认120，已有本机登记 authority 后不能用 raw 宽松 payload 放宽该 authority。云 executor 的 Durable Object 是另一 authority；两者不是跨设备、跨部署的全局 Telegram 配额，本机 UI 保存也不自动更改云配置。显式提高配置不会清空已用量，已预留或在途的 RPC 不能撤回。
3. 身份核验、发送前权限核验、receipt 核对不使用可选数据缓存替代。发送与入群仍执行最终授权；预算不足时停止必要检查和外发。发送后已有 durable ID 的结果保持 unknown，不变成可重发项。无 durable ID 的 verify 在连接前返回。
4. content 第一次仅覆盖调用方明确请求的最近 N 条窗口；它不是全历史导入。窗口 head/floor 固定后分页向前，只有完整转换、整页持久保存成功才推进消费 cursor；之后读取该 cursor 以后的增量，新增消息不扩大正在消费的旧窗口。
5. 目录/实体/元数据缓存保留原始观测时间。目录续页的持久快照可以早于当前 TTL，这用于保持同一轮集合稳定，不能称为重新实时观察。连接变化只允许已登记的 content 数字窗口在 fresh 身份和 fresh peer 一致时重新签名；旧 TL 实体/access hash 不跨连接复用。未完成的 discover 旧 scope 不静默 bootstrap。query/window/limit 语义变化是不同 scope，不能沿用旧消费高水位。
6. 云 inspect/read_context 是有界观察样本（100/50 的原有上限，并可被策略进一步降低），`read_complete` 仅表示本次样本读取成功，绝不表示全量消息已经消费；结果明确输出 sample/cursor scope 与截断信息。TTL 命中不是新样本，不增加 fresh samples。
7. 过小额度可能不足以完成身份、实体与必要检查；此时允许安全地无进展，不能跳过检查换取进度。预算耗尽属于本地读取策略，不伪称平台 FloodWait，不通过换账号或代理规避。
8. 重启安全依赖中央消费状态和私有账本/快照同时保留。损坏或不可写状态应拒绝读取并保持旧 cursor；手动删除这些数据不能保留原来的预算与分页保证。已保存消息/反馈事件与 cursor 的事务回滚、lane fencing、CAS 保护的是正常持久存储模型下的崩溃与并发。

## 适用范围与未运行事项

新增私有逻辑 RPC 预算适用于本轮 builtin Telegram backend 与云 executor。显式 `AP_TELEGRAM_BACKEND=legacy` 路径仍使用原有独立 MCP provider，并未在本报告证明具有相同 RPC 预算；不能将 builtin 的计数结果推广到该旧通道。现有 Engine 账号冷却仍适用。

未运行真实 Telegram/X 账号、Cloudflare 线上请求、生产升级/回滚、真实消息或 Windows 现场验证。合成 cold/warm 对照只能说明给定 fixture 的逻辑调用减少；不能保证未来 Telegram 不触发限流或人机验证。
