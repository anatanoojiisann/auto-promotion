# 只读限流与连接探测

这些改动不恢复暂停任务、不补发 missed/unknown，也不自动登录或更换连接。
`unknown` 只能根据准确回执进行只读核验。一次 FloodWait 是服务端指定的调用等待时间，
不能据此推断每日消息配额、首次限流原因或“每次重试都会延长一天”。

## Telegram

- Engine 在既有 AccountLanes 内重新读取账号的禁用、连接和冷却状态。
  verify、health 等读取同样受本地账号冷却约束；首次服务端 FloodWait 在释放 lane 前持久化。
  后续批次项目不会继续连接该账号，其他可用账号仍可读取。
- 本地跳过返回 `AccountCooldown` / `AccountDisabled`，不伪装为新 FloodWait。
  同一返回结果携带固定 `account_retry_at`；调用方重复记录不会重新从当前时间延长等待。
  已有二维码登录冷却属于独立云端流程，本补丁不修改它。
- native bridge 先加载私有持久回执，再由 Telegram backend 检查非空、正整数 message_ids。
  verify 没有这些 ID 时直接 `unknown/no_durable_receipt`，不创建 client、不连接、不搜索猜测。
  私有回执有 ID 而公共结果尚无 ID 时，仍可只读核验；无法证实则保持 unknown。

## 账号中心与执行门禁

真实账号展示探测成功保留 15 秒；失败按 30、60、120、240、300 秒退避。
缓存/退避保存在账号 health 中，可跨进程读取。缓存命中只读，不重复写检查审计或申请 lane。
实际探测复用现有跨进程 AccountLanes 与 provider OS 锁，锁后再次读取最新状态。
连接目录、配置文件 inode/ctime/mtime/size、后端选择和绑定身份参与不可逆摘要，
连接替换后旧缓存和在途结果不能验证新连接；不读取 session 内容来构造摘要。

`connection_state` 区分 healthy、cooldown、disabled、busy、temporary_unavailable、
navigation_blocked、authorization_required、unverified。短时网络/本地数据库故障不等于退出登录；
`last_verified_at` 仅表示过去成功核验的时间，不证明目前可以发送。
临时失败仍禁止新发布；不会依赖最近成功状态放行。

发送和入群入口显式强制一次新 health 探测，并同时检查本次返回值及最新数据库状态，
不复用展示成功缓存。强制探测不绕过失败退避、禁用、冷却或数据库错误。
既有发送前目标权限、身份、清单、租约、配额、窗口和一次性提交保护保持有效。

## 浏览器诊断

后台 home 导航的 401/403 为 `BrowserNavigationAccessDenied`，触发下载为
`BrowserNavigationDownload`，导航超时为 `BrowserNavigationTimeout`。
记录 `home_navigation` 和安全错误码，不记录 URL、响应正文或原始异常消息。
尚未进入 identity_check 的故障不能推断 Cookie 失效；另一个可见窗口已登录也不能证明
后台独立浏览器 profile 已登录。这里没有重试导航/点击、绕过 403 或切换浏览器。

health 返回保留安全 `execution_step` / `exception_type`。实际 `account.checked` 审计记录
account_id、connection_key 摘要、probe=performed、reason、probe_state、next_check_at；
缓存返回 `health.probe_cached=true`，不新增重复审计。请只导出这些字段及状态/时间，
不要导出 profile、凭据、完整命令行、原始异常或页面内容。

API / ServiceHost 的启动 source_fingerprint 纳入 core、health_checks 和 Telegram/browser backend。
这是该组件启动时的磁盘源码证据，不能替代子进程独立的运行版本，也不自动升级外置 Windows host。
现场 collector 只用 AST 读取同一字面量源码清单，不执行项目模块；旧版缺少清单时保留未验证状态。

## 验证与限制

新增离线契约：`python -m unittest discover -s tests -p test_read_health_resilience.py -v`。
全套隔离入口：`node scripts/test_offline.cjs`；UI：`node scripts/test_ui_offline.cjs main pools`。
依赖及测试 Chromium 路径按 `docs/testing.md` 配置，测试仅使用临时数据库和合成客户端。
测试通过不等于现场已恢复：首次 Telegram 限流根因、Tabbit 真实连接故障及后台 403 的外部原因
仍需同一执行进程、同一连接与准确时间窗的现场证据；不能重放历史消息验证。
历史 SQLite 堆栈中重复 schema 初始化和旧 BEGIN 路径已由既有修复覆盖，本轮不延长锁预算。
