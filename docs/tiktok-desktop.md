# TikTok 视频桌面通道

本地新增的通道复用既有 Tabbit 连接，执行入口为持久队列的 `tiktok.tabbit` pool。调用链是 Engine → native_bridge → backends.tiktok.call → backends.tiktok_desktop.call → 已有 Tabbit CLI；不是独立校验 helper。官方 API 的读取工具仍只面向自有内容，不自动切换通道。

## 输入与定位

已绑定账号配置需包含 `browser_channel=tabbit`、固定 `identity`／`username`、16 位大写十六进制 `tabbit_instance`；可指定 `tabbit_tab_id`。此修复不会创建连接、登录或凭据。多个可用 TikTok 标签而没有唯一绑定时拒绝猜测。其他浏览器通道尚未实现此适配。

评论项可只填写 `tiktok_video_id`，在 `set_task_items`／Engine.set_items 中省略 `target`。系统先从当前任务、当前账号的 qualified 候选查找原生视频链接，或从明确授权给该账号且仍有效的共享内容查找；不跨账号或跨任务复用候选，不使用没有账号范围的共享记录。同一 ID 对应不同作者路径、来源 URL 与声明 ID 不一致、缺少记录或读取预算耗尽都会明确阻塞，不猜作者路径。解析只读本地记录，不请求 TikTok。

解析出的 URL 和来源引用／内容哈希进入执行项及冻结清单。准备、预检前、原子 claim 和 native 最终授权都会复核来源账号／任务、权限、有效期、动作、父评论、视频和哈希；来源变化不自动选另一条记录，也不改写冻结目标。候选查找上限 2000 条，共享内容查找上限 200 条；超过时阻塞而非从截断集合任意选择。回复不能借用其他父评论或顶层评论的共享许可。

MCP 的 `tiktok_check_interaction_input(account_id, action, video_id, task_id)` 可预览解析状态，但不授权发送。ID 入口保存时继续省略 `target`，不要只复制预览的 `resolved_target`；Engine 会在保存时重新解析并冻结证明。直接显式 URL 的旧入口仍可用，须同时提供匹配的 ID，它不会自动继承某个共享内容的权限证明。

若数据库没有该账号的候选链接或明确授权来源，只有一个裸 ID，当前可用读取通道无法安全推导作者与原生 URL：最少需先通过现有 `record_candidate` 或带账号范围的 `import_opportunities` 登记一次实际原生链接。不是每次建单重复手填 URL；本轮不会从私密日志自动导入或恢复旧批次。

定位器随后打开已解析／显式验证的原生 `https://www.tiktok.com/@作者/video/ID`（或 photo）链接，重新核对当前 URL、canonical、账号身份和唯一可见视频，再选择评论编辑器。已有草稿、重定向、相邻视频、可见 iframe／对话框、编辑器歧义会停止。

`comment` 是顶层评论，不带父评论 ID。`reply` 必须额外提供 `tiktok_comment_id`，并从同一视频的原生评论行／精确 permalink 证明父评论绑定。父评论缺失、属于其他视频或回复编辑器不在该父评论行中时拒绝执行；不会降级成顶层评论，也不会无限滚动或点击猜测坐标。

## 执行与回执

准备和输入检查不会发评论。健康预检不填正文；真正执行在持久 claim、账号／浏览器锁和停止检查下准备正文，再在提交前调用最终授权。普通 UI 点击至多一次；不直接请求私有发布 API。共享 Tabbit 实例的 X 与 TikTok 使用相同资源锁键。

只观察本次点击之后产生的同页原生提交请求及对应响应。提交侧 comment ID 仍不足以确认成功：须独立导航读取并精确核对账号、视频、父评论、正文、ID 和 permalink。回执绑定 task/item/attempt/identity 与冻结内容，跨任务回执不能确认当前项。未收到回执、断线、提交后 403 或独立读取证据不足保持 `unknown`，不重发。缺少新 binding 的旧 API 回执保守保持 unknown，不能解锁旧任务。

实际页面若未暴露适配器要求的原生评论属性／permalink，结果仍是阻塞或 unknown。离线合成页面通过不表示日志中旧候选已经获得有效父评论 ID，也不证明真实页面已兼容。

## 访问失败和发布边界

主页面导航 HTTP 403 标为 `TikTokAccessDenied`，与缺执行器和限流分开；429 保持限流分类。按 Retry-After（桌面默认至少 900 秒）持久冷却，期间读操作也不再请求。第三方媒体失败不能冒充主页面 403。验证页只安全停止，交给人工处理；不解决验证码，不更换身份、代理或指纹绕过限制。

`tiktok_check_interaction_input` 是无网络准备检查，`needs_desktop_preflight` 不代表发送权限。私信不走此视频校验与执行链；本轮没有验证或新增私信 provider。

本次仅在 Mac 本地实现并用隔离 fixture 验证。线上 Worker 需要未来同步支持新 pool，常驻执行进程也需加载相同代码。共享资源键规范化可能使旧冻结 job 绑定不匹配：保持 FrozenJobChanged 保护，不改写旧绑定，不迁移或重放 unknown。部署前应停止新领取、排空／核对持有的租约并一致更新控制端与节点；本轮未部署或操作生产队列。Windows、真实 TikTok DOM、账号权限与真实发送均未验收，403 的现场原因仍未知。
