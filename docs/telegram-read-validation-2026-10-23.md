# Telegram 读取降频补充：本地实施与验证

本报告覆盖账号阻断修复之后新增的读取优化，是本轮最新状态。所有修改仅保存在独立工作区 `<workspace>`，分支 `codex/fix-outbound-account-blocks-20261023`，基线 HEAD/origin/main 为 `40d270b2b2abfbe0be029782d1731a0bc995bcda`。本轮没有上传源码、推送、运行远端 CI、部署、请求 Cloudflare 线上、验证 Windows、启动生产任务、调用真实平台账号或发送消息。旧发布准备文档只保留历史证据，不代表当前执行授权或发布状态。旧 tree `ca2fe7caa1a25a74b0107c88257eca1c67463693` 和旧上传清单已失效。

## 实施内容

- 内置 Telegram backend 在实际 Telethon `_call` 前原子预留读取预算，覆盖身份、实体、群目录、资料、消息历史、成员/权限、反馈及回执读取。失败的逻辑调用也扣额；写 RPC 单列观测。额度不足不会跳过最终身份/权限检查，不会缩短 FloodWait，不会改账号/代理/通道继续执行。
- 同一账号目录内，各标签/进程使用稳定真实 identity 的私有 SQLite 账本。配置 CAS 版本更新、连接变化、窗口缩短、重启和旧 worker 的宽配置不会清空当前已用额度。显式提升上限仍保留 used；已经预留或在途的调用不能撤回。
- 群目录/实体/资料缓存按 identity、connection revision、查询及相关 options 隔离，TTL 0 禁用可选缓存。发送权限、身份和回执检查不由成功缓存替代。discover 的续页使用持久目录快照，搜索重排或 TTL 到期不会跳过尾部目标；缓存沿用原观测时间，不称为新数据。
- content 首次只导入请求的最近 N 条窗口。固定 head/floor 后升序分页，接着消费已保存 cursor 后的新消息；不是全历史导入。token 必须已在私有存储登记，绑定真实 identity、peer、query/limit 和窗口。连接变化只允许经 fresh identity 与 fresh peer 核验后，重签旧 token 的数字进度，绝不复用旧 TL 实体/access hash 或重新 bootstrap 丢掉积压。
- Campaigns 的机会整页与 cursor 同事务持久保存；Engine.discover 复用既有候选资格检查，候选整页与 cursor 同事务保存。账号 lane 覆盖取状态、读取、保存；提交时再检查 owner/租约、配置/身份及 expected-old cursor，包括首次空 cursor。崩溃、并发、页验证或存储失败均不越过未保存数据。
- feedback 的事件、覆盖状态和消费进度同事务保存，绑定已核 identity 与原生 peer。旧未绑定的历史数字游标不会被拿来跳过新 scope；必要时从原 publication parent 安全重读并按事件 ID 去重。无原 peer 且 parent 消失时保持不完整。已有 ID、unknown、attempt 和过期状态不重排、不解锁。
- Statistics 使用持久 round/progress 与 owner lease。冷却、部分页、预算或崩溃不提前写“整轮完成”；失败项目轮转，重启能继续尾部。verify 与 feedback 共用每轮剩余额度，缺计数则停止该账号本轮。Growth 的同轮多个 feed 同样共享账号剩余额度，部分读取保持 partial 并公平续读。
- 本地 UI/API 可配置六项读取策略；严格整数范围、If-Match revision、双击/取消/迟到响应保护。页面明确只配置本机内置执行器，云端独立配置统计，旧外部 MCP 通道未覆盖。默认额度为 120 次/3600 秒，每轮 40、每页 100、每轮群数 10、TTL 300 秒；可合法配置的极小预算可能不足以完成必需身份/实体检查，会明确预算不足并安全无进展，需要提高配置，不能为进度跳过检查。
- RPC 类别、计数来源、缓存旧观测时间、预算 authority/scope、冷却来源和运行时 fingerprint 穿过 backend、native bridge、结果持久化及脱敏 ZIP。非写入异常也保留安全的计数和来源；不可用值保持 null/unavailable，不能当作零。诊断不导出 cursor token、TL 实体、access hash 或凭据。

## 范围与计数含义

Python 计数是 instrumented Telethon `_call` 逻辑尝试（批请求逐项），云端是 `client.invoke` 逻辑尝试；都不是消息数、实际网络包数或 SDK 内部重试数。没有观测能力时明确 unavailable/null。Telegram 真正返回的冷却仍优先，读取预算耗尽不冒充 FloodWait。

本机账本 authority 是同一 accounts 根目录的 SQLite；云端 authority 是各账号 Durable Object，二者并未构成 Mac/Windows/Cloud 跨端总额。`AP_TELEGRAM_BACKEND=legacy` 的旧外部 MCP 路径没有证明具备新增账本；仅保留既有 Engine 全入口冷却，缺少指标时显示 unavailable_legacy，不切换或禁用该通道。

云 inspect/read_context 保留原有 recent 100/replies 50 有界观察样本，仅增加 TTL 复用与预算，不宣称完整消费游标或全历史增量。`read_complete` 是该次所需样本读取成功；样本截断与 scope 独立显示。具体合同及 128/63 Worker 测试见 [云端本地验证](telegram-cloud-read-validation-2026-10-23.md)。

新表为附加结构，不删除已有任务、attempt 或 unknown。正常重启保证要求中央消费状态与私有预算/快照同时保留；删除或损坏账本无法继续声称原预算与游标保证。旧程序不认识这些约束，不应与新写入程序混跑，也不能用旧程序回滚来解除阻断。生产更新、混跑与 Windows 原实例更新均未在本轮执行。

## 同 fixture 前后比较

`python scripts/telegram_read_compare.py` 使用隔离 synthetic client、同一公开群、两条查询、五条消息，实际执行旧/新代码各两次 discover。旧源码固定 git blob `c1f699b100954c0e1c9025c5baf4cd65427e7782`，不随最终 stage 改变。

| 逻辑调用尝试 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 第一次 discover | 6 | 6 |
| 第二次 discover | 6 | 2 |
| 两次合计 | 12 | 8 |
| 每次 fresh 身份相关尝试 | 2 | 2 |

输出 `/tmp/ap-telegram-read-python-comparison.json`。旧 blob 是该独立工作区内前轮已审修复的未提交对象，新 clone 未必含该对象；脚本支持 `--baseline-file`，仅接受 git blob hash 完全匹配的旧源文件，缺失会明确停止。该脚本不加入 CI，也不把 main `40d270b` 冒充前轮修复源码。云的固定旧 blob `65cb5866853f6cbaf773d11c2ac32eb53a640460` 同 fixture 两次 inspect 为 10→6，详见云报告。合成结果不能推导真实 Telegram 的请求降幅，也不能证明先前现场限流的根因。

## 红测、回归与构建

行为红测包括：无效第二条反馈仍留下首事件；冷却时提前标记统计 slot；真实 Engine→native 的新增 transport metadata 破坏私有 final receipt hash；discover 调用丢失续页状态；目录重排漏目标；同轮 verify 未扣预算；新 worker cursor 被旧首空页覆盖；发送高级方法内预算拒绝和 teardown 覆盖确定未提交；SDK 吞 FloodWait 误判登录。修复及独立故障复验均使用合成 client/临时库，没有真实平台调用。

首轮完整 Python：856 项，851 通过、2 失败、3 Windows 场景 skip，317.804 秒。失败为旧 synthetic fixture 未报告读数（新规则正确停止该账号）和在运行中扩充 fingerprint 清单导致已导入常量与磁盘不同；旧 fixture 已显式报告其合成尝试，最终整套在功能冻结后重新运行。一次直接沙箱内的定向 Windows collector HTTP 测试因 loopback EPERM 未运行成功；最终隔离 runner 使用批准的本机 loopback 路径覆盖，不把环境错误算通过。

最终完整 Python 使用隔离 HOME/profiles、临时数据库、本机 HTTP/Miniflare 和拦截 Chromium 网络：**862 项，859 通过、3 skip、0 失败/错误，368.693 秒**。日志 `/tmp/ap-telegram-read-full-python-final.log`；runner 实际 exit 0。skip 仅两个 Windows DPAPI 和一个实际 Windows PowerShell/CIM。最后发送内预算+teardown 窄修在整套启动后收敛，最终又运行当前源码定向组合 **121/121 通过，31.624 秒**（预算、增量、真实 consumer/native、限制、反馈、读取健康），日志 `/tmp/ap-telegram-read-final-target.log`。全套未把旧 Windows 现场冒充已验。

| 最终范围 | 实际结果 | 证据 |
| --- | --- | --- |
| control Worker 全套 | 128/128 通过，0 skip | `/tmp/auto-promotion-cloud-read-control-test.log` |
| Telegram executor 全套 | 63/63 通过，0 skip | `/tmp/auto-promotion-cloud-read-telegram-test.log` |
| UI/API | main45、pools完整模拟链路、interactions22、faults64、blocks11、读取设置11；API10，全部通过 | UI 验证报告及 `/tmp/ap-telegram-read-validation-20261023/` |
| 全 Python/脚本 Ruff lint、format | 通过；125 文件格式正确 | `/tmp/ap-telegram-read-lint-final.log` |
| Prettier、git diff 空白检查 | 通过 | `/tmp/ap-telegram-read-format-final.log` |
| Python sdist/wheel | `build --no-isolation` 通过，wheel 包含两个新增读取模块 | `/tmp/ap-telegram-read-build-final.log`、`/tmp/ap-telegram-read-dist/` |
| 两 Worker 本地 dry-run 打包 | 均 exit 0；没有远端发布 | `/tmp/auto-promotion-cloud-read-control-build.log`、`/tmp/auto-promotion-cloud-read-telegram-build.log` |
| 独立审查 | 已复现 P1/P2 全闭环，无已确认阻断项；独立 Python161/50/48/26、Node14/7/router1/UI11 均通过，彼此重叠不相加 | 独立审查报告 |

最终功能源码 fingerprint 为 `35bddbf5037f241bb143bf3ff1f5c4093b94a8b0d0b8bff8f19ae7e9772e9284`；HEAD 仍是上述 main 基线，这不代表未提交功能已发布。Python 3.14.2、Node24.13.0；Python 浏览器测试使用已安装 Chromium1208/Chrome145.0.7632.6，UI suite 使用 Chrome155.0.8059.86，均为本机隔离 fixture。新增模型、私有读取 helper、bridge/adapter、consumer 及缓存边界均纳入 runtime fingerprint。最终本地 tree 与所有文件/blob 清单记录在 `/tmp/telegram-reads-local-tree-manifest.json`（仅本地复核用途，不是上传许可），旧上传 manifest 不复用。

同 fixture 旧 Python 源文件另保存在 `/tmp/ap-telegram-read-baseline-c1f699b.py`，git blob 与固定 `c1f699b100954c0e1c9025c5baf4cd65427e7782` 一致。可运行 `python scripts/telegram_read_compare.py --baseline-file /tmp/ap-telegram-read-baseline-c1f699b.py`；它不会连接平台。最终 wheel SHA-256：`63d287eea2e6fbd570903d6b16eee6069251cbac3f40ef495bd9d749f93a82e1`，sdist SHA-256：`2a430b6f52dfa3c692e50f8d3e240147610a6d72bee2561bc8497da7f0957b3e`。

独立审查：[读取安全审查](telegram-read-independent-security-review-2026-10-23.md)。UI 精确结果及环境：[UI/API 验证](telegram-read-ui-validation-2026-10-23.md)。原账号阻断修复及审查仍保留在各 outbound-account-block 文档内。
