# 云 Telegram 读取降频：本地离线验证

本次只修改本地代码和运行离线 fixtures。没有上传源码、运行远端 CI、调用 Cloudflare 服务、部署、发送 Telegram 消息或验证 Windows 现场。

## 实际范围

- 云执行器通过持久事务在 `client.invoke` 前预留账号读取额度，包括身份、目录/实体、群资料、历史样本、成员检查、回执验证和发送内部必要读取。失败的 invoke 也计入；预算拒绝时尚未调用的 invoke 不计入。只精确排除执行器支持的 `messages.SendMessage`、`channels.JoinChannel` 两种写 RPC，并单列 `mutation` 计数。
- 账号预算键不含策略、身份或连接版本；当前窗口的使用量和上限不能通过换策略、缩短窗口、重建对象或重新登录清零。缓存键另含账号、已核身份、登录 generation、查询和读取 options。多 plan 共享同一账号 Durable Object 的预算。
- `TelegramWorkflowReader.tick` 公平轮转 plan，并把同一轮剩余 RPC 上限递减传给后续请求。缺少计数、响应丢失、JSON 解析失败均视为读量未知，停止本轮后续读取，记录 `rpc_counts: null`。
- inspect/replies 保持原有 recent 100、replies 50 的有界观察样本语义。目录、实体、白名单群资料、source 和样本可持久 TTL 复用；TTL 0 明确禁用缓存。到期读取当前窗口，因此旧 ID 的新置顶消息不会被高水位跳过。
- **云观察 API 没有消费增量游标**。返回 `read_cursor_scope: bounded_sample_no_consumption_cursor`、`read_cursor_next: null`，不回扫全历史，也不把仅保存在执行器中的页当作用户已消费数据。真正可消费的持久增量游标属于本轮 Python content 路径，另行验证。
- `read_complete` 仅表示所需有界观察源成功返回；`read_sample_truncated`、各 stream 的样本上限和数量另行展示，不代表全群历史完整。缓存命中保留原始 `read_observed_at`、`read_fresh_samples: 0`，不会冒充新样本。
- 过大的有界样本（序列化超过 96KB）直接返回新鲜结果并跳过可选缓存，明确报告 `read_cache_skipped_oversize`，避免超过 Durable Object 单值限制。它仍受同一账号和每轮读取预算约束。
- 身份检查始终实时；成员状态、发送权限和回执从不走可选缓存。预算不足不会跳过检查，不会被标成 FloodWait。最终发送前已确定无写 RPC的预算失败为已知未提交；已有 ID 或写后回执读取受阻保持 unknown，durable receipt 禁止重发。没有有效 durable ID 的 verify 不连接 Telegram。

## 配置和统计边界

代码支持可信执行器/reader 配置 `TG_READ_POLICY_JSON`，没有修改现有部署配置。本机 UI 配置和云配置属于独立权威，本轮不自动传播。默认：`requests_per_window=120`、`window_seconds=3600`、`scan_rpc_limit=40`、`messages_per_page=100`、`groups_per_scan=10`、`cache_ttl_seconds=300`。调用方的 `read_policy` 只能收紧执行器上限；window 由账号权威配置决定，最短 60 秒；page/group 最大 100；TTL 可为 0。响应另有 `read_policy_effective`、`read_budget_state`，新配置不能重置旧窗口账本。

`rpc_counts` 是逻辑 `client.invoke` 尝试的类别计数，不是样本数，也不是抓包得到的物理网络 RPC。SDK 内部重试和网络调用次数不能由该计数证明，因此 `read_rpc_network_attempts`、`read_rpc_retries` 为 null。`read_budget_scope=cloud_executor_authority`，不承诺把 Python SQLite、Mac、Windows 与云端不同账本合成一个跨端总额度。Telegram 返回的真实等待时间仍优先，既有账号锁、身份检查、平台冷却和最终授权保留。

## 旧版同 fixture 实测

运行：`node scripts/telegram_cloud_read_compare.mjs`。脚本固定读取已校验的旧 adapter blob，以同一 synthetic client、五条消息、两次相同 inspect 实际调用旧/新代码；后续 stage 不会改变基准。

固定基准 blob：`65cb5866853f6cbaf773d11c2ac32eb53a640460`。它来自本地未提交对象，目前仅保证此工作区存在，fresh clone 或 Git 清理后可能不存在。可使用 `node scripts/telegram_cloud_read_compare.mjs --baseline-file /path/to/old-telegram-adapter.mjs` 提供同 hash 的旧源码；脚本验证 Git blob hash，缺失或不匹配会明确停止。没有把旧源码复制进仓库，也没有向远端上传该旧对象。

| 指标 | 旧版 | 新版 |
| --- | ---: | ---: |
| 两次 inspect 的逻辑 invoke 尝试 | 10 | 6 |
| 第二次 inspect 的必需身份尝试 | 1 | 1 |
| 第二次 inspect 新鲜消息样本 | 5 | 0（明确 cache_hit） |

新版 cold inspect 仍为 5 次；warm inspect 为身份 1 次。另有 1000 条历史 fixture 证明 cold 仍只取既有 100 条样本，不增加全历史遍历。以上不能推导真实 Telegram RPC 降幅，也不能证明原现场限流根因。

## 验证结果

- `cloudflare/control-plane`: `node --test test/*.test.mjs`，128/128 通过。
- `cloudflare/telegram-executor`: `node --test test/*.test.mjs`，63/63 通过。
- 新增读取 policy 14 条离线测试，覆盖缓存、TTL 0、身份/连接隔离、查询、并发原子预算、单轮并行预留、策略/窗口变化、坏样本、缓存写失败、超大样本跳缓存、发送内预算、已获 ID、旧 ID 新置顶和请求数比较。
- 实际 Worker router 在本机 Miniflare 搭配 synthetic Telegram client 验证：并发账号锁、durable 额度、无 ID verify 零 RPC、额度耗尽后保留已有 durable receipt ID。它不是 Cloudflare 线上验证。
- 控制端覆盖共享每轮剩余额度、未知计数停止、transport/JSON 故障停止、公平轮转、未完成结果不能通过研究检查、本地预算不污染平台冷却。
- 两个 Worker 本机 dry-run 打包通过；新增文件 Prettier 和 `git diff --check` 通过。这些均不是远端部署或线上验证。

本机日志：`/tmp/auto-promotion-cloud-read-control-test.log`、`/tmp/auto-promotion-cloud-read-telegram-test.log`、`/tmp/auto-promotion-cloud-read-baseline-comparison.json`。远端 CI、Cloudflare 线上和 Windows 现场验证均未运行。
