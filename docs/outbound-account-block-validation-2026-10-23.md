# Telegram 发言受限与 X 验证后空跑：本地修复验收

## 工作区与范围

- 独立 clone：`<workspace>`。
- 分支：`codex/fix-outbound-account-blocks-20261023`。
- 在线重新 fetch 核实基线 main：`40d270b2b2abfbe0be029782d1731a0bc995bcda`。
- 本报告记录的是最初实施验收时的独立工作区未提交变更，彼时未推送、部署或操作生产任务。用户后续授权推送、CI 和发布后的准备/阻塞记录见 `outbound-account-block-release-2026-10-23.md`。两阶段均未授权真实测试消息、验证码处理或注册凭据。
- 旧工作区仍仅有原先未跟踪的 `scripts/telegram_login_handoff.py`；未运行该脚本，未覆盖旧 `var` 或凭据，未修改本机记忆。
- 本次没有读取两份 Library ZIP；诊断背景使用委托中已经核实的证据，不能声称已独立复读附件或确认历史运行 SHA。

## 最终行为

Telegram 的最终发送 RPC 明确返回 `UserBannedInChannelError`、又无接受证据时，结果为 `failed_known`，独立记录 `telegram_speaking_restricted`。已有 ID/引用后再遇到拒绝仍保留 `unknown` 和回执；发送超时/断线仍不重发。15 分钟内连续、不同目标的两次明确拒绝暂停账号外发，范围交给人工确认，不解释成永久单群封禁，也不切换账号或代理。确认发送成功或显式恢复会重置拒绝序列。无 durable ID 的 Telegram verify 继续零客户端构造、零网络。

X 在首页/身份、目标导航/查找、编辑器、提交前后与回执阶段检查可靠 URL、可见专用组件和结构化响应错误。普通推文正文或一般错误文本含 captcha 不触发验证分类。HTTP 200 的结构化错误、普通 403/429、登录失效、账号限制、网络失败及 DOM 异常分别记录；明确未提交或明确服务端拒绝为已知失败，提交后无回执继续为未知。三次页面异常在 15 分钟窗口触发 15 分钟熔断，标为页面异常。

`account_blocks` 独立于任务结果，贯穿账号 lane、队列 claim、最终 native authorize 和云端 claim/authorize/standby/offline gate。中央数据库落库失败时，独立私有事件 journal 仍保护账号；事件文件互不覆盖，清旧事件不能删新事件。部分/已 fsync 但未 rename 的 staging 文件、权限错误和损坏证据均停止外发。journal 自身失败时继续尝试中央数据库，保存失败不覆盖平台原因。两条专用持久通路都不可写时返回 `account_guard_unavailable` 与 `guard_persistence_failed`；所有持久介质都不可用时无法保证跨重启的证据保存，必须修复存储后再恢复。

恢复需要先执行当前账号的只读身份健康检查，再显式恢复当前 generation。失败缓存不能替代这次探针；身份不符、检查超过 5 分钟、连接配置变化或新证据会拒绝旧恢复授权。恢复不会重排 unknown、已尝试或过期任务，不自动切换 API 通道。UI 展示原因、时间和受影响任务，并处理重复点击、取消及迟到响应。云端阻断通过已有响应/心跳按账号身份导入本地；远端健康证据不能直接授予本机恢复权限，旧快照也不能清掉新阻断。

新增诊断包含失败类别、阻断原因/时间/generation、RPC 类别与计数来源、冷却来源，以及进程启动时冻结的 Git SHA、源码指纹和启动时间，并有实际任务导出断言。源码指纹用于辨别未提交工作树；Git SHA 本身不能代表这些未提交修改。RPC 计数来自 Telethon `_call` 的分类，不能把消息样本数当 RPC 数，也不能由本次合成测试推断历史限流根因。

## 验证状态

最终全套 Python acceptance 已通过：808 tests，805 通过、3 个平台限定跳过，0 失败/错误，耗时 184.641 秒。

| 范围 | 已取得结果 | 证据 |
| --- | --- | --- |
| 完整 Python 隔离回归 | 808 tests：805 通过、3 skip；0 失败/错误 | `/tmp/ap-outbound-python-acceptance.log` |
| 账号阻断/恢复与存储故障 | 20 tests 通过 | focused unittest 输出 |
| native、Telegram、X 历史事件与执行监控 | 修复 journal OSError 后 74 tests 通过；随后新增两个线程/缓存回归单测通过 | focused unittest 输出 |
| Cloudflare 控制层 | 124/124 通过，0 skip | `/tmp/auto-promotion-cloud-block-worker-test.log` |
| Telegram 云执行器 | 48/48 通过，0 skip | `/tmp/auto-promotion-cloud-block-telegram-test.log` |
| 云客户端独立回归 | 30 tests：26 通过、4 个需 HTTP 模拟器的既有 skip | `/tmp/auto-promotion-cloud-block-python-test.log` |
| UI | main 45、interactions 22、faults 64、blocks 11 和 pools 模拟链路全部通过 | `/tmp/ap-outbound-validation-20261023/ui-full.log` |
| API | 新增 3 + 既有 10 通过 | 同目录 `api-blocks.log` / `api-existing.log` |
| Ruff lint / format | 全范围通过；119 文件已格式化 | 最终命令输出 |
| Prettier / diff 空白检查 | `npm run format:check`、`git diff --check` 通过 | 最终命令输出 |
| Worker / Telegram dry-run build | 两套 exit 0，未部署 | `/tmp/auto-promotion-cloud-block-control-build.log`、`/tmp/auto-promotion-cloud-block-telegram-build.log` |
| Python 包构建 | 最终快照 sdist + wheel 通过，06:30:25 UTC 验证；wheel 四个关键文件与源码逐字节一致 | `/tmp/ap-outbound-validation-20261023/build-final.log`、`dist-final/` |
| 独立安全审查 | 通过；9项已发现问题闭环，无未解决 P1/P2；独立 focused 69、X 最终21、云客户端26、Worker59、云 Telegram11、UI11 均通过 | `docs/outbound-account-block-independent-security-review-2026-10-23.md` |

失败/修正记录保留：新增 Telegram 测试在旧代码中复现原生异常逃逸、native 把明确拒绝写成 unknown，日志 `/tmp/telegram-restrictions-red.log`。首次完整 runner 因默认 Chromium 路径不存在造成浏览器初始化失败和事件循环连锁错误，不计为通过。显式选择已有 Chromium 后第二轮完整执行 803 tests，唯一失败为旧监控断言仍期待预提交 X 超时抛出异常；新行为应返回 `failed_known` 和 `confirmed_not_submitted`，断言已修正并 focused 复验通过。最后一轮重新执行已全部通过，日志为 `/tmp/ap-outbound-python-acceptance.log`。3 个跳过项分别是两个 Windows DPAPI 场景和一个真实 Windows PowerShell/CIM 元数据场景；本轮本机 Chromium 和本机 HTTP 模拟器契约测试均启用，没有把这些 opt-in 场景计作 skip。

## 环境与复现

本机使用 macOS、Python 3.14.2、Node v24.13.0；完整 Python 浏览器回归显式使用缓存 Chromium 1208（Google Chrome for Testing 145.0.7632.6）。测试复用旧工作区的依赖，但测试代码、临时数据库和账号目录均隔离，所有社交网络请求是 mock 或被浏览器拦截。UI 的浏览器版本和命令见独立 UI 报告。

```sh
AUTO_PROMOTION_PYTHON=/absolute/path/to/python \
AP_BROWSER_EXECUTABLE=/absolute/path/to/test-chromium \
AP_TEST_TIMEOUT_MS=900000 node scripts/test_offline.cjs
python -m ruff check src tests scripts/telegram_online_replies.py
python -m ruff format --check src tests scripts/telegram_online_replies.py
npm run format:check
```

本次检测与审查覆盖已诊断的内置 Playwright X 通路，不证明独立第三方浏览器通道具有同样的验证检测覆盖。

没有执行 Python 3.11、Windows 现场、真实平台 challenge/登录或真实消息投递验收。不能把 Mac 合成测试当作 Windows 现场验证。原有全局停止、冷却 max 写入、锁后重查、冻结清单、账号身份与权限检查、租约和 unknown 防重发保护保留。
