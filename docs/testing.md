# 测试说明

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

- 五个平台批次、外发动作和候选发现流程的模拟执行。
- 精确审批持久化、重复确认幂等、清单与媒体哈希校验。
- 8 个并发 worker 的单次领取、进程恢复、未知结果不重发。
- 计划时间、逾期跳过、原生排期独立回执及投递日额度预留。
- 全局/任务停止、账号冷却、用户和目标限额、旧后端控制读取。
- 候选资格、单次入群、待管理员批准和只读成员核对。
- API 认证、跨站边界、静态界面、人工结果与取消状态。
- Telegram/Reddit 桥接契约、Telegram 回执与兼容辅助模块。
- 内置后端直接调用的审批复核、私密账号隔离、Windows 账号锁、持久化回执与断线后不重发。
- Telegram 身份变化、管理员审核、原生排期的两次独立查询、回复目标与文件回执。
- 真实 Telethon 请求处理中的手机号数据中心迁移、登录断线恢复上限、人工输入时的网络保活和推广发送不重试。
- Instagram 自有目标、身份及正文核验；Reddit 回复的作者和父评论核验。
- X 离线浏览器的唯一目标、实际作者、回复关联、单次发送及刷新后点赞状态。
- IG/X 网页连接的身份匹配、只读核验、自动登记、取消、并发边界和敏感字段脱敏。
- Discord Bot 身份、角色与频道权限覆盖顺序、私密/NSFW 过滤、公开频道目录、字符串消息 ID、文件上传、提及关闭、独立回执核验、断线不重发、限流冷却及本机定时执行。
- 平台身份去重、账号资料编辑、跨账号任务间隔和独立账号执行顺序。
- MCP 实际 stdio 握手、五个平台工具目录、任务草稿、审批与停止限制、单次执行和错误脱敏。

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

验收包括三个中心、IG/X/Discord 连接表单、任务创建、泛化批准拒绝、两阶段精确审批、清单编辑和冻结、模拟执行、回执、全局停止及移动布局，以及 Discord 频道选择、完整消息 ID、图片上传和模拟回复，共 18 类检查。无效批准导致的单次 HTTP 409 是预期行为；其他浏览器错误会使测试失败。

结果保存到忽略提交的 `var/ui-test-result.json`。省略 `AP_UI_SKIP_SCREENSHOTS` 时，截图写入 `output/playwright/`。不会连接已有工作台或修改其数据库。

如需复用 Python Playwright 已安装的 Chromium，可将 `AP_UI_CHROMIUM_PATH` 设置为其可执行文件绝对路径。Windows 测试脚本默认使用项目 `.venv\Scripts\python.exe`。

## 验证边界

通过上述测试代表控制层和模拟流程工作正常，不代表真实平台登录、权限、媒体处理、审核或投递已通过验收。真实环境应按 [平台接入说明](platforms.md) 逐个平台完成账号检查与实际批准批次的回执验证。

GitHub Actions 在 Python 3.11 / 3.14 上执行自动检查与构建，并另行运行浏览器验收。具体运行结果以仓库 Actions 记录为准。
