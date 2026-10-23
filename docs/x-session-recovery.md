# X 后台独立会话恢复

日常浏览器与后台账号目录是两个独立会话。日常浏览器中的正常登录，不能替代
后台通过账号菜单读取到的用户名。健康检查仍需与原绑定身份完全一致。

当后台无界面 Edge 返回空的 HTTP 403，而同一账号目录的普通窗口能够返回
HTTP 200 时，可以在该账号的本机配置中显式设置 `browser_headless: false`。
默认仍为无界面模式；只有 JSON 布尔值 `false` 启用普通窗口。该设置作用于
同一独立账号目录，要求本机有可用桌面，不复制日常浏览器的登录资料。

若专用窗口显示 X 的未登录首页，应在工作台重新连接原用户名、原后端标签和
原浏览器。登录完成后关闭连接窗口，由后台重新打开同一账号目录核验身份。
同一身份、同一浏览器的重新登录会保留 `browser_headless` 与既有
`automatic_actions`；新连接或切换浏览器不会因此自动获得动作权限。

健康检查区分以下状态：

- `BrowserNavigationAccessDenied`：主页文档返回 HTTP 401/403，尚未核验登录身份。
- `BrowserLoginRequired`：身份控件未出现，且页面明确显示 X 未登录首页或登录流程。
- `BrowserRateLimited`：HTTP 429，继续沿用原有冷却处理。

即使空响应导致 Playwright 报下载导航错误，也应保留文档状态码。子资源或
子框架的拒绝响应不能替代主页状态。导航超时、下载和服务错误分别保留安全错误码，
不直接判定未登录；其他请求的 HTTP 403 继续使用 `BrowserAccessDenied`。
恢复过程只运行连接和健康检查，不创建或启动发送任务。

## Google 第三方登录拒绝受控窗口

Google 可能拒绝由自动化软件控制的浏览器。出现 “This browser or app may not
be secure” 时，先结束该次连接并退出受控的专用窗口，再用正常安装版 Edge
在相同 `browser-msedge` 目录手动登录 X。登录期间不连接 Playwright、远程
调试器，不修改安全设置或浏览器标识。用户完成登录并关闭专用窗口后，后台
才能重新打开同一账号目录核验身份。Edge 的启动进程可能立即退出并移交给
子进程，应等待实际持有该账号目录的浏览器进程退出。

Google 官方说明：https://support.google.com/accounts/answer/7675428?hl=en

若普通窗口中选择 Google 账号后弹窗关闭，仍应以后台实际读取的 X 身份为准，
不能将弹窗关闭视为登录成功。用户可使用 X 官方的“忘记密码”流程，为仅使用
Google/Apple SSO 的账号设置独立 X 密码，再通过用户名和密码登录专用窗口。
设置密码及邮箱验证由用户本人操作，不能把密码或验证码写入诊断日志。
X 说明重置密码会退出已有 X 会话，操作前应明确告知用户这一影响。

X SSO 说明：https://help.x.com/en/managing-your-account/sso

X 密码重置说明：https://help.x.com/en/managing-your-account/forgotten-or-lost-password-reset
