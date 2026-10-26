# 外发账号阻断：发布兼容与回滚审查

审查日期：2026-10-23。审查工作区：`auto-promotion-fix`，旧版本基线为
`40d270b2b2abfbe0be029782d1731a0bc995bcda`。本轮仅静态检查和隔离合成测试，
未部署、登录平台、发送消息、读取凭据或操作生产任务。

## 当前结论与发布阻塞

本地代码、SQLite 数据结构和既有未知结果的兼容检查通过，但**不能签发生产发布安全结论**。
实施代理报告：既有 Mac Wrangler 认证仅能访问另一账号，对生产账号
`<production-account-id>` 下的 `auto-promotion-control` 和
`auto-promotion-telegram-executor` 各执行一次只读 `deployments list --json`，
均返回 Cloudflare `10000 Authentication error`。状态为 **denied**；未部署、未重新认证，
审查者未重试。当前生产版本、实际变量/绑定、全局停止和在途租约均未取得。

因此本轮可交付提交、推送和 CI，部署保持 pending，不能将 dry-run 打包或公开健康页称为发布成功。
用户本轮未授权真实发帖；不能通过切换 Cloudflare 账号、重新注册凭据、换新项目/对象，
或临时开启外发来排除认证问题。

## 数据结构与兼容性

- 本地新增 `account_blocks` 和 `account_block_events` 两张表；没有删除或重命名旧列，
  没有把 `items`、`attempts`、`candidates`、任务清单或既有回执改回待执行状态。
  `Store` 的新增表创建仍在原有写事务内；既有全局停止值使用 `INSERT OR IGNORE` 保留。
- 本地 `schema_version` / `store_schema_revision` 未提升到旧版拒绝打开的值。
  旧版可以打开新库，但不理解新的阻断语义，不能据此继续发送。
- 云端没有新增 SQL schema 或 Durable Object migration tag；账号阻断存在既有
  `accounts.payload` 的 `outbound_block_state` 中。两 Worker 的对象类、绑定名称及
  receipt key 保持原样，不应为发布创建新的对象或清空存储。
- 新 control 能接收旧客户端省略阻断字段的快照，并保留已有阻断；清除状态需要更新的
  generation 和本地明确恢复的健康证据。旧 receipt 和未知终态不会因此重领。
- 新 executor 的明确拒绝分类仍以已获 ID/回执优先；已有未知回执保持未知，不因升级重新提交。

### 独立隔离验证

使用 `git archive` 从旧 main 提取代码到临时目录，使用旧版真实 `Store/Engine`
创建合成模拟账号、已冻结任务、发送 attempt 及 `unknown` 项，再由新版迁移并由旧版重开。
没有使用生产数据库或账号。

| 检查 | 实际结果 |
| --- | --- |
| 旧本地库 → 新版 `Store` 初始化 | 原有所有表的每行数据完全不变 |
| 新版添加阻断后旧版重开 | 数据可读，`unknown` 与 attempt 保留，旧版重开未改行 |
| 同一合成账号的新旧外发可写判定 | 新版 `writable=false`；旧版 `writable=true`，确认旧发送进程不执行新守卫 |
| 新旧 control kernel 共用临时 SQLite | 旧版可重开，未知 job 与 attempt 不变 |
| 新云阻断 → 旧 control 接收旧客户端快照 | 旧 `sync` 会整段覆盖账号 payload，移除新阻断字段；确认不能安全热回滚旧 control |

证据：`/tmp/ap-release-compat-xzaz0w59/result.json`、
`/tmp/ap-release-compat-xzaz0w59/cloud-result.json`。
首次本地验证脚本在显式新增审计后将 `sqlite_sequence` 变化误纳入“迁移无变化”比较，
断言失败；修正为先独立比较迁移前后，再单独验证阻断与旧版重开，最终结果如表。
该脚本问题未计为通过。

## 新旧版本混跑与部署顺序

以下是认证恢复后仍须满足的前提与顺序，**本轮未执行这些生产步骤**：

1. 先只读确认生产全局停止/真实写入关闭、相关在途提交已结束或保留未知回执，并确认
   旧本地 sender/pool/provider 进程已停止。不能仅依据父进程心跳或 `/health`。
   不能取消/改写已尝试项来“清空”在途状态。
2. 保持外发关闭，先发布 **control Worker**，再发布 **Telegram executor Worker**。
   新 executor 在旧 control 上运行时会产出细分限制字段，而旧 control 不会据此暂停后续目标；
   先升级 control 才能先具备最终授权守卫。
3. 两 Worker 完成后，再更新完整本地 Python/UI/provider 组件，并重启全部相关进程。
   不允许旧 Engine、旧 queue 与新 provider 长期混跑；旧 Engine 即使被新 provider 挡住，
   也可能继续为不同后续条目创建不必要的 attempt。
4. 仅用版本、绑定、队列状态及安全配置的只读检查验收；保持原有停止状态，不启动任务、
   不恢复账号、不开启发送、不切换执行通道。验证通过不构成真实发帖授权。

旧 executor 配新 control 的过渡期也不是完整修复：旧 executor 仍把发言限制归为较宽的
账号风险，可能设置旧式 disabled/cooldown。故整个过渡期应保持不执行，而不是依赖混跑运行。

## 静态部署配置风险

仓库的 control 配置包含每 15 分钟 cron、Telegram executor service binding、
既有 fallback 设备/数据库绑定及历史 `LOCAL_CHANNEL_UNTIL`；executor 配置包含反向
control service binding、既有账号约束和 `TG_ACCOUNT_NOT_BEFORE`。配置文件没有本轮修改。

但静态仓库文件不能证明它们等于当前线上配置。control 的 cron 在现有 executor 绑定和
`TG_QUEUE_BRIDGE` 可用时会调用 native queue tick；该路径检查全局停止/执行授权。
部署本身不创建新任务，但可能让既有已启动任务继续运行，所以本轮“不真实发帖”的边界
仍需要先确认线上执行关闭。

恢复认证后应比较部署元数据与非敏感配置，保留当前 service/DO/fallback/account 绑定、
停止与生效时间约束，以及现有秘密绑定；不能把仓库历史时间值当成当前线上事实覆盖。
不要导出或输出秘密值，不要为此次升级复制/替换 Session、重新扫码或注册凭据。

## 回滚边界

- 优先在外发保持关闭时前向修复。若必须回滚，也应先验证没有发送进程或云租约继续执行，
  并保留现有状态；本轮没有授权审查者执行生产停止或回滚。
- 不可在执行开启时回滚旧 control：它不检查 `account_blocks` / `outbound_block_state`，
  且旧同步可移除新云阻断。旧本地 sender 同样忽略新表和私有 guard journal。
- 回滚 executor 时应保留新 control 守卫且维持外发关闭；旧 executor 的限制分类仍较宽，
  不能据“代码能启动”宣布行为安全。
- 保留新表、generation/tombstone、账号私有 `outbound-stops`、receipt 数据库、任务清单、
  attempts、未知结果、排期 ID 及云队列证据。不要 drop 新表、删除 guard 文件、清空云对象，
  或恢复较旧数据库快照来配合代码版本；这样可能丢掉已提交证据并造成重复发送。
- 旧 UI 不展示新的恢复状态，不可用旧 UI 的普通健康检查/启用按钮替代新版明确恢复流程。

本报告的兼容通过仅涵盖隔离合成数据与代码路径；生产账号授权、实际配置和发布前执行状态
仍未验证，发布继续受上述认证阻塞约束。
