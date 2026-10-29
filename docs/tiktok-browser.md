# TikTok 网页互动首版

2026-10-26 开发。沿用现有 Tabbit / Chrome / Edge 登录会话，通过统一持久任务队列执行指定视频的文字评论和指定评论回复。桌面执行端必须在线；后台发送进程不会操作网页，也不会在没有桌面执行端时把入队计为成功。

## 能力与边界

- 网页账号独立登记为 `tiktok_desktop`，执行池为 `tiktok.desktop`，任务通道必须为 `local`。
- 原 `tiktok.organic` API 路由、自有视频限制和 OAuth 配置保持各自规则，不会被网页连接覆盖。
- 首版只接入文字评论／回复，采取本机 150 字符上限；页面更严格的限制仍以实际页面为准。
- 文字私信采集、主动发送及私信回复已扩展，见 [私信接入说明](tiktok-private-messages.md)。仍无关键词自动发现、点赞、关注和无人值守桌面派发服务。
- 仅接受 `https://www.tiktok.com/@作者/video/视频ID`。短链接先在浏览器解析。回复链接必须带唯一 `comment_id`，并与 `tiktok_comment_id` 一致。
- 同一视频跨账号、跨任务共享去重占用。已提交、结果未知或正在领取的互动不能再次发送。
- 登录、验证码、账号限制、页面访问失败和未知结果必须保存原状态；不得绕过或重新提交。

## 连接账号

工作台「账号中心 → TikTok 网页账号」填写用户名、本机标签、浏览器及实例 ID。
Tabbit 实例 ID 从稳定启动器实际输出读取。登记仅保存身份和连接标签，不读取或复制 Cookie、令牌、浏览器配置目录，也不改变真实发送开关、全局停止、已有任务或 API 连接。

默认日额度 5、小时额度 2、最小间隔 600 秒。重复登记不会重置额度、冷却或禁用状态。
更换身份或浏览器连接使用新标签；同一已登记网页身份不能再创建第二个执行账号。

桌面执行端必须从已登录导航中的 Profile 链接和个人主页共同核实身份。
单独打开某个公开个人主页不能证明登录身份。身份观察有效期为五分钟。

## 桌面工作流

使用正常 `create_task / set_task_items / start_task`。只有人明确启动指定任务或点击开始按钮才能外发。开发、连接、拟稿和读取不启动任务。

1. 读取 `tiktok_browser_jobs`，只处理已启动、到期且没有阻塞的条目。
2. 在登记浏览器和实例中打开目标视频，读取身份、视频、评论、输入框及平台限制。
3. 填写精确文字。回复时必须独立核实父评论 ID 和作者，并定位该评论的行内回复框。
4. 调用 `tiktok_browser_begin` 领取唯一尝试；此时仍没有提交。
5. 重读当前页面，立即调用 `tiktok_browser_guard`，传入新的 proof。许可只使用一次，有效期受 180 秒租约及任务窗口限制。
6. 许可成功后点击对应提交按钮一次。超时或断线一律视为未知，禁止重试。
7. 重新打开／读取评论线程，独立核对作者、全文、视频、父评论和新评论 ID，再登记 `tiktok_browser_result`。

`web/tiktok-desktop.js` 是桌面浏览器辅助脚本，不加载进运营工作台：

- 在同一个浏览器任务中加载脚本；`tiktokDesktop.read(page)` 只读取 DOM。
- `prepareComment(page, item, connection)` 准备顶层评论，不提交；保留无关草稿。
- `prepareReply(page, item, connection, parentProof)` 需要独立核验过的父评论凭据，然后准备该行内回复。
- `submit(page, item, connection, permit)` 在当前页面再次核对身份、视频、全文和行内回复归属，再使用 guard 返回的许可点击一次。
- `item` 为 `{item_id, payload_hash, payload}`；`connection` 包含 `identity, browser, instance_id`。
- 点击返回 `unknown`，不能把按钮动作或输入框清空当作平台成功回执。重载辅助脚本仍保留该浏览器任务内已消费的许可。

父评论凭据包含 `comment_id, target, text, author_profile, id_verified=true, independent_read=true, evidence, observed_at`。
`author_profile` 使用实际作者主页路径，不能用显示名替代用户名。

## 观察与回执契约

身份 proof：`identity, profile_url, browser, instance_id, identity_verified=true, authenticated_menu_verified=true, observed_at, evidence`。

页面受阻时可以登记 `blocked_reason` 和新观察时间，使旧的正常身份观察失效。支持登录、验证、限制、限流和访问受阻状态。

begin 和 guard proof 还需要：

- `payload_hash, target, video_id, parent_comment_id, editor_text`，与冻结清单逐项一致。
- `context_verified=true, can_reply=true, no_duplicate=true, source_quote`。
- 回复另需 `parent_id_verified=true, parent_author_profile`。没有父评论 ID 依据时保持未尝试。

confirmed receipt 需要：

- `status=confirmed, verified=true, identity, observed_at, observed_text, evidence`。
- `external_reference`：已核实的新评论链接，视频 ID 一致且带新 `comment_id`。
- `video_id, comment_id, parent_comment_id, parent_target`，其中新评论 ID 不得等于父评论 ID。
- `author_verified=true, parent_verified=true, independent_read=true`。
- `published_at` 只使用实际平台时间，取不到则为 `null`；统一统计单列“已确认但缺少平台发布时间”，不补造时间或回复率。

2026-10-26 实机只读核验发现：当前视频评论 DOM 可读正文与作者，但未必暴露评论 ID。
读取器将这种 ID 保留 `null / id_verified=false`，不会从列表顺序或其他视频通知推断 ID。
若无法取得独立的评论 ID／回执依据，回复保持等待，已提交结果保留 unknown。

## 本机入口与界面

旧 MCP 会话需重新加载工具目录。也可以使用同一实现的 CLI：

```powershell
.venv/Scripts/python.exe scripts/tiktok_browser.py status
.venv/Scripts/python.exe scripts/tiktok_browser.py connect --input outputs/connect.json
.venv/Scripts/python.exe scripts/tiktok_browser.py jobs
.venv/Scripts/python.exe scripts/tiktok_browser.py observe --input outputs/observe.json
.venv/Scripts/python.exe scripts/tiktok_browser.py begin --input outputs/begin.json
.venv/Scripts/python.exe scripts/tiktok_browser.py guard --input outputs/guard.json
.venv/Scripts/python.exe scripts/tiktok_browser.py result --input outputs/result.json
```

connect 输入为连接对象；其余输入以方法参数名包装。guard 需要 `item_id, owner, proof`；result 需要 `item_id, owner, receipt`。
这些入口都不登录、不启动任务、不启用发送开关。

结果进入原 `items / attempts / publication_evidence / feedback_events`，复用统一统计、三小时汇总与诊断导出。
API 前缀为 `/api/tiktok-browser/`。上线前重新加载本机服务和 MCP；本次开发不自动重启既有任务执行服务。
