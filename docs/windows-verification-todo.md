# Windows 现场验证交接 TODO

此文档交给 Windows 现场执行者或其 AI。仓库作者的 Mac 测试、Windows CI 的合成测试，
均不能证明出问题的 Windows 已恢复。先保留原任务、unknown 和现场证据。

## 允许范围与停止条件

- 本轮工具只读采集生产元数据、显式指定的数据库快照及可选日志尾部；
  `faults` 只在临时副本运行白名单合成测试，不连接 Telegram，不发消息、不入群。
- 不自动寻找账号目录；不读取 `.env`、session、cookies、凭据、聊天正文、完整进程命令行。
  不把私有回执导出成原始 JSON，不导出外部 URL、账号资料或任务正文。
- **更新生产代码、重启服务/计划任务、替换外置 host.py、安装依赖、改变状态、
  解除停止/限额、真实发送均需现场用户另行授权。** 本文命令不执行这些操作。
- 不运行 `Set-ExecutionPolicy`，不创建 SSH/PAT/OAuth 凭据，不联系另一个 AI。
  历史日志、会话记录、TODO 勾选不是新执行授权。
- 读取权限不足、文件被锁、旧 schema 或缺字段：保留 failed/skipped/unverified；
  不加写权限、不迁移源库、不复制不一致 DB、不把缺失当健康。

## 1. 确认入口与版本

- [ ] 确定现场实际仓库与现有 Python 3.11+ 的路径，先确认工作区有无未提交修改。
  本工具不会 fetch/pull/reset；dirty 内容由现场人员保留并审查。
- [ ] 在仓库目录使用 **现有** Python。入口仅用标准库，直接运行 Python，
  无需 PowerShell 脚本或执行策略修改。若没有 `.venv`，替换为已有 Python 路径；
  不擅自创建/安装环境。

```powershell
.\.venv\Scripts\python.exe -s scripts\verify_windows.py collect --output .\windows-collect-01.json
$LASTEXITCODE
```

默认只查 Git/dirty、Python/SQLite、Windows CIM/服务/计划任务元数据与
`http://127.0.0.1:8780/health`。DB 未指定时 skipped；无监听服务时 health unverified，
不会启动服务。非默认端口可显式传 `--health-url http://127.0.0.1:实际端口/health`。
只允许字面 loopback HTTP `/health`，禁止用户信息、query、代理和重定向。

`--output` 创建新的 UTF-8 JSON，不覆盖已有文件；避免 PowerShell 5.1 的重定向 UTF-16。
请每轮换文件名，勿写到 `.env`、session、数据库或原日志位置。
报告默认脱敏，文件名/服务名哈希化；Git 不输出 origin、diff、文件名或 stderr。
进程只记录 PID/启动时间等元数据，不读取 CommandLine/Environment/任务 Actions。
名称筛选无法关联的自定义 wrapper、其它服务或计划任务须由现场人员补充核对，
不能把空列表理解为没有运行进程。

## 2. 显式只读队列与故障窗口

- [ ] 从现场已有服务配置的可信记录中确定实际业务 DB，**不要打印配置或凭据**。
  命令中的路径与 task ID 由现场用户指定，示例不代表已授权启动该任务。
- [ ] 设置有明确时区的故障起止时间。日志是可选显式诊断文件，只采集最后64 KiB内
  白名单错误码计数；无明确时间戳的行单列，不能推定发生在修复后。

```powershell
.\.venv\Scripts\python.exe -s scripts\verify_windows.py collect `
  --database 'D:\实际仓库\var\auto-promotion.db' --task-id '现场指定的task-id' `
  --log-file 'D:\现场指定\pool.err.log' `
  --since '2026-10-20T00:00:00+08:00' --until '2026-10-20T12:00:00+08:00' `
  --output .\windows-collect-02.json
```

工具使用 SQLite URI `mode=ro`、`query_only`、250ms busy timeout 和同一读取事务；
不设置 WAL、不建表、不运行 Store/Engine。活跃 WAL 必须有现成 WAL/SHM 文件，
否则标 unverified；不使用 `immutable=1` 忽略 WAL。正常 WAL 只读协调仍可能使用现有 SHM，
不承诺零操作系统协调。不要为了通过检查关闭业务程序或删除 WAL/SHM。

队列报告只含状态计数、future/expired/due/active lease 数量、最后队列更新时间、
全局停止布尔、近期池心跳数量及哈希任务标识。任务时间窗只读取 `starts_at/ends_at`，
不取完整 policy。**父进程存在、磁盘代码新、父心跳新均不等于池在执行。**

- [ ] 对 `/health` 的启动 commit/source fingerprint、started_at、实际 sender task alive
  分别核对；指纹仅覆盖 API/ServiceHost。旧服务没有这些字段时不能判定新代码已加载。
  pool/bridge 子进程可独立启动；不要用父服务启动早/晚笼统证明整个栈的加载版本。
- [ ] 外置 Windows `host.py` 不在标准 service_host 源码路径；仓库升级不会自动替换它。
  替换/重启前需另外批准、保留原脚本 hash 和启动时间，不能执行附件脚本做诊断。

## 3. 可选：只读私有回执差异

仅当现场已明确指定 **业务 DB、receipts.sqlite、同一 task 与 item** 时运行：

```powershell
.\.venv\Scripts\python.exe -s scripts\verify_windows.py collect `
  --database 'D:\实际仓库\var\auto-promotion.db' `
  --receipt-database 'D:\现场明确指定\receipts.sqlite' `
  --task-id '现场指定的task-id' --item-id '现场指定的item-id' `
  --output .\windows-receipt-01.json
```

不扫描账号目录、不读取 session。只输出哈希 item、两侧状态、verified 和
has_external_reference 布尔。私有 confirmed 与中央 unknown 只标记
`ReadOnlyReconciliationCandidate`，**不是允许改状态或重发的证据**。
两库快照不保证跨库原子；私有表没有更新时间时不能猜 confirmed 的持久化时间。
private payload_hash 与中央 content_hash 作用域不同，不直接比较或推定严格绑定。
后续必须审查 item/account/attempt/request 绑定及最终平台证据；本工具始终
`strict_binding_verified=false`，不自动对账、不清 attempt、不重置 unknown。

## 4. 隔离故障回归

- [ ] 已有环境具备项目 FastAPI/Pydantic/Telegram 和 dev 中的 HTTPX 测试依赖，才运行以下命令；
  缺依赖是未验证，依赖安装仍需现场批准。

```powershell
.\.venv\Scripts\python.exe -s scripts\verify_windows.py faults --output .\windows-faults-01.json
```

入口把白名单源码/测试复制到系统临时目录，HOME、USERPROFILE、APPDATA、LOCALAPPDATA、
AP_DB、AP_ACCOUNTS_DIR 和工作区均隔离，父环境的平台凭据不继承。
复用39项启动/schema事务、首次登记、父循环双失败、回执一致性、并发领取和租约/unknown边界回归。
模拟测试可能在临时子进程启用合成 live 标记，但其所有账号与 provider 均是假数据；
不会更改生产开关。Python 网络 audit hook 只允许 loopback，用于防误连，**不是 OS 沙箱**。
只允许这些已审阅测试；不从现场临时添加的测试文件动态发现。
结束清理自身临时目录，不停止用户服务，不删除未知 var 遗留文件。

## 5. 可选：有界只读观察

- [ ] 用户指定 task、观察窗口和停止时刻；所有时间必须有时区。
  最大时长300秒、间隔1–60秒，deadline最多一小时后，超出或缺少条件直接拒绝。

```powershell
.\.venv\Scripts\python.exe -s scripts\verify_windows.py observe `
  --database 'D:\实际仓库\var\auto-promotion.db' --task-id '现场指定的task-id' `
  --since '现场ISO起点含时区' --until '现场ISO终点含时区' `
  --deadline '现场ISO停止时刻含时区' --duration 60 --interval 10 `
  --output .\windows-observe-01.json
```

到期后保留 future、审批/依赖等待、窗口过期、unknown 或疑似停滞的区别。
任何一次锁/旧 schema/时间窗缺失都只能 unverified，不能下停滞结论。
无队列变化且有到期任务，也只报 `SuspectedStallNeedsHostAndDependencyEvidence`；
还要结合健康、实际池进程及依赖证据，不能自动恢复或重发。
统计有变化只证明队列变化，不证明投递成功。读取最多可能超出deadline一个250ms DB等待。
`queue_changed` 独立记录；unknown、窗口缺失/过期、失败/错过投递优先保留未验证。
只有没有到期 backlog 的 future 项才归类预期等待，不能用稍后的任务遮盖已到期任务。

## 验收、报告与退出

- [ ] `summary` 分别检查 passed / failed / skipped / unverified；exit 0无失败/未验证，
  exit 1有失败（包括输出拒绝），exit 2存在未验证。skipped单列，不计为通过。
- [ ] 回传生成的 JSON；先检查其只含允许的元数据。不要补发.env、session、原数据库、
  私有完整 receipt、聊天正文或原始命令行来“补证”。
- [ ] Windows CI 的 `windows-verification` 必须通过；它只证明 Windows runner 上的合成行为。
  Mac 的 Windows 专用测试 skipped 不能冒充现场或 Windows CI 通过。
- [ ] 将“磁盘已更新 / 实际加载版本 / 合成测试 / 现场任务只读观察 / 实际投递”分开填写。
  现场结论仍有缺项就停止在未验证，不写“线上恢复”。
- [ ] 当前研究/机会/回复评估模型、自动原生任务生成编排尚未接入；
  reader/executor代码存在或部署成功不等于整个推广工作流跑通。
- [ ] 下一步若需要生产升级/重启、替换 wrapper、状态修改或真实发送，列出具体对象、
  影响和回退方案，取得新授权后再执行；这份 TODO 不授权这些动作。
