# 浏览器只读扫描契约与验证范围

本次覆盖仓库内 Playwright X 扫描、仓库生成的 Tabbit 扫描 JavaScript 及其回执解析。
不能据此认定现场对话调用的外部 Tabbit 执行器采用了这些代码，亦未验证真实账号、
Cloudflare 或 Windows。Browser Use 仍仅保存人工会话的身份观测，没有新增自动扫描或发送能力。

## 停止、预算与结果

- 主读取只认可信 X 主机上的主 frame 首页/搜索导航，以及精确的 SearchTimeline
  GraphQL 操作或已列出的搜索端点。媒体、其他第三方请求以及子 frame 导航的错误独立计数，
  不因其 429、ERR_ABORTED 或 ERR_FAILED 推断账号限流或验证码。
- 主读取 429 返回 `rate_limited`、实际可解析的 `Retry-After` 与不完整状态；停止后续主请求，
  丢弃可能残留的页面候选。没有有效等待时间时才使用后备值，不将有效长等待缩为 15 分钟。
  账号持久冷却与后续入口拦截由控制层处理，不切账号、代理或 API 通道继续查找。
- `timeout_seconds`（1–120）约束本次首页、身份控件、搜索和结果等待的共同读取截止时间。
  可选 `read_request_limit`（1–1000）在主请求发出前预留；它只限制上述主请求，
  不代表全部平台 RPC 或网络包数量。Tabbit 释放已完成任务有单独至多 5 秒的清理窗口。
- Tabbit 未完成的扫描使用稳定任务名，保留 pending 状态，不创建新名字绕过旧回执，
  不强制清理用户标签页。清理异常不能覆盖已观测到的账号阻断或 429。
- 浏览器扫描始终 `coverage_complete=false`、`next_cursor=null`。
  `read_page_complete=true` 只表示本次可见 DOM 样本已解析，不能证明搜索时间窗完整。
  `read_page_fingerprint` 是已返回候选标识的稳定摘要，可用于识别重复样本；
  它不是新的分页游标，也不能据此前移完整覆盖 watermark。

## 观测字段

`read_state` 区分部分样本、不可观测、超时、网络失败、预算不足、主读取限流及账号验证状态。
`read_primary_request_count` 统计扫描拦截器允许的主请求尝试；响应计数是监听窗口内观察值，
两者不保证相等，也不等于全部浏览器流量。`read_resource_failures` 只含资源类别计数。
诊断不保存请求 URL、正文、cookie 或响应头。

Playwright DOM 样本使用实际读取时间。仓库 Tabbit 脚本返回自己的观测时间，解析器保留它，
不以回执读取时间重新盖章；缺少该契约和时间戳的旧回执标为 `tabbit_unstamped_receipt`，
时间和计数为未知。Browser Use 返回原人工观测时间及 `live_probe=false`，
再次调用 health 不会把旧观测变成实时平台检查。

最终发送身份、权限、持久账号阻断及未知结果不重发规则保持。搜索页发现可靠 challenge
会提交新的账号阻断事件；Tabbit 身份观测不能在可见 challenge 仍存在时返回健康。

## 离线回归

新增 `tests/test_browser_read_observations.py`；其中浏览器用例需要显式
`AP_TEST_BROWSER_BACKENDS=1` 和可用测试 Chromium。所有页面、XHR 和资源都由 fixture 拦截。
`scripts/test_x_read_observations.cjs` 直接执行本仓库生成的 Tabbit JavaScript，
覆盖主搜索 429、资源 429/中止、子 frame、无关 GraphQL、challenge、预算和截止时间。
不启动 Tabbit launcher，不连接已登录浏览器。恢复了原本误放在 `__main__` 下、
不会被 unittest discover 执行的两个 Tabbit 高频扫描用例。

这些结果证明本仓库离线契约，不证明外部执行器、真实平台限流改善或现场版本已更新。
