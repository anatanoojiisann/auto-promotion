# 测试说明

Windows现场只读采集、隔离故障入口和交接清单见
[Windows 验证 TODO](windows-verification-todo.md)。`scripts/verify_windows.py` 的
collect/observe仅用标准库，不import项目Store/Engine；Windows专用合成CI不能替代现场验收。

本仓库的默认测试不连接社交平台、不需要真实账号，也不依赖相邻后端目录。

## Python

```sh
python -m pip install -e '.[dev,telegram,mcp]'
ruff check src tests
ruff format --check src tests
python -m unittest discover -s tests -v
python -m build
```

独立测试（本机 Chromium 契约测试需显式启用），覆盖：

- 六个平台批次、外发动作和候选发现流程的模拟执行。
- 直接启动持久化、重复确认幂等、清单与媒体哈希校验。
- 8 个并发 worker 的单次领取、进程恢复、未知结果不重发。
- 计划时间、逾期跳过、原生排期独立回执及投递日额度预留。
- 全局/任务停止、账号冷却、用户和目标限额、旧后端控制读取。
- 候选资格、单次入群、待管理员批准和只读成员核对。
- 候选说明中的精确 `non-adult` 否定标注不会误命中 `adult`；标题、目标和其他风险信号仍拒绝。
- Telegram 入群任务冻结和启动发送前，核对本轮候选、已核验入群回执、发言权限与实际等待截止时间。
- 执行窗口过期、超过现有15分钟投递容差或原生排期时间已过时，不能启动发送。
- 执行和入群跳过原因持久保存并去重；不改清单哈希、不生成发送尝试，工作台显示执行前检查。
- API 认证、跨站边界、静态界面、人工结果与取消状态。
- Telegram/Reddit 桥接契约、Telegram 回执与兼容辅助模块。
- 内置后端直接调用的启动状态复核、私密账号隔离、Windows 账号锁、持久化回执与断线后不重发。
- Telegram 身份变化、管理员审核、原生排期的两次独立查询、回复目标与文件回执。
- 真实 Telethon 请求处理中的手机号数据中心迁移、登录断线恢复上限、人工输入时的网络保活和推广发送不重试。
- Instagram 自有目标、身份及正文核验；Reddit 回复的作者和父评论核验。
- X 离线浏览器的唯一目标、实际作者、回复关联、单次发送及刷新后点赞状态。
- IG/X 网页连接的身份匹配、只读核验、自动登记、取消、并发边界和敏感字段脱敏。
- Discord Bot 身份、角色与频道权限覆盖顺序、私密/NSFW 过滤、公开频道目录、字符串消息 ID、文件上传、提及关闭、独立回执核验、断线不重发、限流冷却及本机定时执行。
- 平台身份去重、账号资料编辑、跨账号任务间隔和独立账号执行顺序。
- MCP 实际 stdio 握手、六个平台工具目录、任务草稿、直接启动与停止限制、单次执行和错误脱敏。

平台调用使用合成数据或 mock。兼容模块单独保存在 `integrations/`，测试从本仓库读取，不从开发者电脑上的其他工程读取。

Windows 启用全部内置后端及离线 Chromium 测试：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[all,dev]'
.\.venv\Scripts\python.exe -m playwright install chromium
$env:AP_TEST_BROWSER_BACKENDS='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

离线浏览器测试拦截所有页面与请求，使用合成 X 页面，不访问真实账号。

MCP 安装及本机只读连接检查见 [MCP 安装说明](mcp.md)。

## 浏览器

```sh
npm ci
npx playwright install chromium
npm run format:check
AP_UI_SKIP_SCREENSHOTS=1 npm run test:ui
```

脚本启动独立 Python 服务，使用系统分配的端口和临时 SQLite 数据库；强制关闭真实写入并将后端路径指向隔离目录。无论成功或失败，都会关闭测试进程并清理临时数据库。

验收包括三个中心、IG/X/Discord 连接表单、任务创建、无审批表单、直接启动、清单编辑和冻结、模拟执行、回执、全局停止及移动布局，以及 Discord 频道选择、完整消息 ID、图片上传和模拟回复，共 18 类检查。浏览器错误会使测试失败。

结果保存到忽略提交的 `var/ui-test-result.json`。省略 `AP_UI_SKIP_SCREENSHOTS` 时，截图写入 `output/playwright/`。不会连接已有工作台或修改其数据库。

如需复用 Python Playwright 已安装的 Chromium，可将 `AP_UI_CHROMIUM_PATH` 设置为其可执行文件绝对路径。Windows 测试脚本默认使用项目 `.venv\Scripts\python.exe`。

## 验证边界

任务工具测试覆盖模板跨轮次创建独立草稿、重新捕获额度和账号快照、真实／模拟隔离、重启保存、任务证据隔离和完整审计、诊断字段筛选、HTML 转义、CSV 公式处理、ZIP 校验文件、接口认证与跨站检查，以及命令行不覆盖文件。MCP 和主界面验收增加保存模板、按模板创建草稿及下载审阅／日志包。

业务增长新增14项测试，覆盖匿名事件并发去重、冲突整批回滚、跨来源交易去重、试用成熟队列、退款金额与币种、地区筛选、全球／分地区数据避免重复、商店修订与事件分开、活动实际投递指标、回复待办与新任务草稿、来源停用与刷新保留核对结果、数据库增量建表及接口认证。主界面测试增加目标设置、渠道、事件／商店导入、手机布局、回复待办入口、只读来源和内容核对。所有记录为隔离合成数据。

号池单元测试新增30账号/1000内容规划、账号与人设版本、业务内容去重、跨账号并发、跨任务串行、活动暂停及完成、额度预留争用、AI草稿不自动发布和API直接启动路径。`npm run test:ui:pools` 默认创建独立模拟服务，也可指向 `AP_POOL_UI_URL` 指定的模拟服务，覆盖建池、人设、导入、分配、草稿编辑、直接启动、模拟回执、额度设置及桌面/移动截图；遇到任何真实账号立即拒绝运行。详见 [号池说明](account-pools.md)。

通过上述测试代表控制层和模拟流程工作正常，不代表真实平台登录、权限、媒体处理、审核或投递已通过验收。真实环境应按 [平台接入说明](platforms.md) 逐个平台完成账号检查与实际启动批次的回执验证。

GitHub Actions 在 Python 3.11 / 3.14 上执行自动检查与构建，并另行运行浏览器验收。具体运行结果以仓库 Actions 记录为准。

## 执行可靠性回归

`python -m unittest discover -s tests -p test_runtime_reliability.py -v` 使用临时数据库，
覆盖真实 SQLite 写锁竞争、锁释放恢复、池心跳及领取失败后的存活、回执保存失败后不重发、
快照并发创建的一致性、历史孤立引用的明确阻塞，以及跨时间的无变化同步。
`test_native_backends.py` 另测提交标记保存失败时零发送、发送后回执保存失败或连接中断时只发送一次。
`node scripts/test_offline.cjs test_private_receipt_recovery.py` 使用合成账号验证平台返回后
调用日志锁失败仍保留原结果、最终私有回执与中央落库失败后的只读收敛，以及活跃租约、
错账号/尝试/请求/清单绑定、缺失最终标记和损坏回执均不重发。新回执在执行时写入
`native_execute_claim_v1` 绑定，最终 envelope 校验覆盖绑定、结果和完成时间；这不是防篡改签名。
旧导出缺少完整绑定或最终证据时保持 unknown，不根据当前任务补造字段。
仅 confirmed/failed_known 最终结果可自动收敛；scheduled 仍按原排期核验流程处理。
锁重试仅发生在连接准备和 `BEGIN IMMEDIATE` 阶段；5 次、每次至多 2 秒等待，
加上总计 1.6 秒退避，共用 11.6 秒总预算，WAL 首次切换也受同一预算限制。
事务体、平台提交及发送结果不做自动重试。

`node scripts/test_offline.cjs test_startup_resilience.py` 覆盖成熟 schema 无写打开、
DDL 整体回滚、未知 schema 版本拒绝、首次 worker 登记恢复、错误审计再次失败时父循环存活、
以及启动时冻结的运行指纹和调度 task 存活状态。外置生产 wrapper 不由这些库修复自动替换。

Telegram 只读权限失败按实际账号身份、目标和检查种类暂存：明确拒绝等待 5 分钟，
网络等不确定失败等待 30 秒；FloodWait 继续使用平台返回的账号冷却期。
资格变化后到期重新只读检查，已入群目标也可从界面或 MCP 核对当前权限，不重新入群，
不延长原入群等待时间。此检查仍只涵盖现有文字发言权限，不能替代媒体类型权限的真实验收。
`submission_started` 是提交前持久化的保守边界，不代表平台已接受或投递成功。

macOS 的临时目录可能有 `/var` 与 `/private/var` 别名，运行全套时可用
`TMPDIR=/private/tmp python -m unittest discover -s tests -v`，避免仅路径拼写导致的断言差异。

云控离线回归：在 `cloudflare/control-plane` 运行 `npm ci && npm test && npm run check`。
包含 schema 1→2 迁移、重新打开零写、重复授权/回执/快照零写和普通任务 6 次写入预算；
`check` 仅打包 dry-run，不部署。CI 同时执行这些检查和 `npm run test:ui:pools`。

Telegram 云端执行器离线回归：先安装 `cloudflare/control-plane` 的依赖，再在
`cloudflare/telegram-executor` 运行 `npm ci && npm test && npm run check`。
新增 CI 作业独立执行同样检查。云控测试覆盖任务级账号交接、工作流依赖与证据、公开资料采集、
登录后只读采集和原生队列授权；执行器测试覆盖真实依赖的离线构造、禁止自动重试、
回复上下文变化、提交前持久回执及未知结果不重发。测试不登录 Telegram，也不发起真实平台写入。

完整隔离执行器 `node scripts/test_offline.cjs` 会创建临时 HOME、账号目录和默认数据库路径，
强制关闭真实写入，并启动临时本机 Miniflare；使用的 operator token 是脚本内纯合成测试值。
需要已有 Python 测试依赖、根目录及云控目录的 npm 依赖和测试 Chromium。
可用 `AP_BROWSER_EXECUTABLE=/absolute/path/to/test-chromium node scripts/test_offline.cjs`
指定已有测试浏览器；脚本显式启用离线浏览器及本机云控 HTTP 契约测试，不能把普通
`unittest` 默认运行的跳过项计为通过。参数 `test_extreme_faults.py` 可只运行极端故障回归。
结束时在 finally 中关闭临时云控并清理本轮临时目录，不读取生产会话。

`AP_UI_CHROMIUM_PATH=/absolute/path/to/test-chromium node scripts/ui_fault_test.cjs`
使用独立模拟服务，覆盖桌面与手机各三轮迟到 GET/PUT 成功及失败响应：新草稿、仅路由切换、
按钮关闭、ESC 和同一事件内关闭重开；同时检查草稿/焦点、错误提示和实际保存值。
共 60 个迟到响应场景及 4 个当前错误提示场景。该测试只延迟响应，
不证明跨客户端或服务端写入顺序倒置的冲突已解决。所有服务、浏览器和临时目录在 finally 清理。
`node scripts/test_ui_offline.cjs` 使用受控环境依次运行主 UI 和号池 UI；
可追加 `main`、`pools` 或 `faults` 选择场景，并继承显式指定的 `AP_UI_CHROMIUM_PATH`。
该入口不会继承已有工作台 URL、账号路径或平台凭据。


TikTok 的授权配置、发布与自有评论管理、独立回调和真实验收见 [TikTok 接入说明](tiktok.md)。
