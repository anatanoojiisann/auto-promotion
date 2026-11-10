# 无手机离线回执修复交接

日期：2026-11-10（北京时间）。本轮没有接入手机、调用真实平台读取器、对外发送、安装 APK、调用模型、修改运行服务或实际业务数据库。所有执行、基线、签名和回执测试均为明确的离线测试替身。

## 修复结果

1. Telegram / X 的真实平台时间有整秒精度并使用独立时钟。共享 `receipt_matches` 统一允许发布时间比本地提交早最多 **2 秒**，超过即拒绝。账号、目标、完整正文摘要、原基线排重和一次发送许可继续校验。Telegram 历史读取器去掉额外减 2 秒，避免容差叠加成 4 秒；120 秒创建上界保留。
2. 正常 `verify_task_item` 增加 X 的原执行只读再核验。业务侧保存任务映射，群控中心在独立持久化记录中排队；原节点利用自己的原命令日志恢复发送前基线，独立读回唯一新 ID、父帖、作者和全文并签名。新的节点认证可以查询已过期的原执行，不能恢复原发送许可。原执行、原结果摘要、设备占用和隔离均不被此操作改写。
3. 持久化明确拒绝原因，包括未找到、重复匹配、旧基线 ID、账号/目标/正文不符、时间超界、读取器不可用。冷却、网络异常或读取次数耗尽时可 GET 已保存结论，不能退回创建或发送。
4. 补齐所用手机分支缺少的主线修复 `39ba36f`：空的 `expected_draft_sha256` 不进入旧操作请求序列化，恢复历史请求指纹兼容性。这一问题在首次全量回归中复现，最终安装包全量回归已通过。

## 约束与接口

- 业务侧仅引用原 execution_id。`POST /internal/fleet/v1/executions/{id}/receipt-readback` 排队；同路径 GET 查询。
- 节点心跳传递原 command_id / nonce；当前节点认证通过 `POST /internal/fleet/v1/node/receipt-readbacks` 汇报签名证据。
- 本机受保护的 `POST /internal/fleet-runtime/v1/receipt-readback` 校验原命令指纹、账号绑定、真实来源标记、原提交和点击意图。只恢复原基线，不创建新的基线，不调用 Portal、ADB、Appium 或提交入口。
- 每个原执行最多 8 次只读核验，至少间隔 15 秒，请求有效期 120 秒；节点和本机分别最多并发两项。重复 nonce 复用持久化证据；等待不占设备执行锁或数据库事务。
- 中心复核当前节点凭证、签名、原上下文、原提交事实摘要、nonce、原命令、账号/设备归属、原基线时间、唯一新 ID 和原执行时间范围。结果单独保存，原 `unknown`、原结果摘要和隔离继续保留。业务结果可基于新证据确认为 confirmed。
- 使用现有 JSON 记录存储，不修改已发布迁移或直接读写另一服务数据库；公开任务和原执行 DTO 未增加字段。

## 最终验证

以下均使用独立环境中安装的正式 wheel；已断言运行模块来自该环境的 site-packages，未通过 PYTHONPATH 或源目录替代安装包。

| 范围 | 通过 | 跳过 | 秒 |
| --- | ---: | ---: | ---: |
| 群控全量 | 823；另 8 个子检查 | 5 | 159.51 |
| 调度与节点全量 | 102 | 6 | 22.46 |
| AP 手机、正常核验和 Telegram 读取器相关回归 | 147 | 0 | 142.85 |

合计 **1072 项通过、8 个子检查通过、11 项跳过**。AP 仅运行所列相关回归，不表示 AP 全仓所有用例均已运行。跳过的是未提供临时 PostgreSQL、SeaweedFS、Keycloak 或 Temporal 测试资源的项目，不记为通过。真机和真实平台复验本轮均为 0。

新增用例覆盖 2 秒边界、错误账号/目标/正文、原基线缺失或过晚、过期原执行、签名和 nonce 错配、跨租户、重复查询、节点换会话后的核验、查询不阻塞心跳、读取限额和网络异常，以及原执行/发送次数/设备隔离不变。

静态检查覆盖全部改动 Python 文件。依赖检查通过。5 个 wheel 中的 **257 份源码/资源文件**逐字节与最终工作树核对，均一致。单包构建未联网取依赖。配套包为 contracts 1.2.0、protocol 0.2.1.dev1、control 0.5.0.dev18、fleet 0.2.0.dev19、AP 0.1.1.dev5。

## 复跑命令

群控工作目录：`D:/Projects/phone-agent-platform/.worktrees/offline-receipt-repair-20261110`。

```powershell
.local/offline-receipt-wheel-venv/Scripts/python.exe -m pytest control-system/tests -q --tb=short --disable-warnings --maxfail=3 --junitxml=.local/offline-receipt-wheel-control.xml
```

调度工作目录：`D:/Projects/phone-agent-platform/.worktrees/offline-receipt-repair-20261110/control-system/fleet-management`。

```powershell
../../.local/offline-receipt-wheel-venv/Scripts/python.exe -m pytest tests -q --tb=short --disable-warnings --maxfail=3 --junitxml=../../.local/offline-receipt-wheel-fleet.xml
```

AP 工作目录：`D:/Projects/auto-promotion-offline-receipt-repair-20261110`。

```powershell
D:\Projects\phone-agent-platform\.worktrees\offline-receipt-repair-20261110\.local\offline-receipt-wheel-venv\Scripts\python.exe -m pytest tests/test_phone_receipt_readback.py tests/test_phone_protocol_receipt_gateway.py tests/test_phone_social.py tests/test_telegram_phone_observer.py -q --tb=short --disable-warnings --maxfail=3 --junitxml=.local/offline-receipt-wheel-promotion.xml
```

精确静态检查参数、JUnit、wheel SHA-256 和源文件一致性清单在外部输出目录 `C:/Users/22246/Documents/ChatGPT/群控系统/outputs/offline-receipt-repair-20261110`；原始 JUnit 与 wheel 还在手机工作树 `.local/`、AP 工作树 `.local/`。运行证据和环境未提交到仓库。

## 文件与集成归属

手机工作树：`D:/Projects/phone-agent-platform/.worktrees/offline-receipt-repair-20261110`，分支 `codex/offline-receipt-repair-20261110`，基线 5ef7f2e。最终提交 SHA 记录在外部 summary.json 与聊天交接。仅本聊天专属分支有改动；未推送或合并。

- `control-system/fleet-management/pyproject.toml`
- `control-system/fleet-management/requirements-dev.lock`
- `control-system/fleet-management/src/phone_fleet/__init__.py`
- `control-system/fleet-management/src/phone_fleet/agent.py`
- `control-system/fleet-management/src/phone_fleet/api.py`
- `control-system/fleet-management/src/phone_fleet/receipt_readback.py`
- `control-system/fleet-management/src/phone_fleet/runtime.py`
- `control-system/fleet-management/src/phone_fleet/service.py`
- `control-system/fleet-management/tests/test_agent_receipt_readback.py`
- `control-system/fleet-management/tests/test_receipt_readback.py`
- `control-system/pyproject.toml`
- `control-system/requirements-integration.lock`
- `control-system/src/phone_control/__init__.py`
- `control-system/src/phone_control/api.py`
- `control-system/src/phone_control/data/x_reply_reader.js`
- `control-system/src/phone_control/fleet_runtime.py`
- `control-system/src/phone_control/operator_actions.py`
- `control-system/src/phone_control/receipt_readback.py`
- `control-system/src/phone_control/x_reply_reader.py`
- `control-system/tests/test_receipt_clock.py`
- `control-system/tests/test_x_readback_runtime.py`
- `control-system/tests/test_x_reply_reader.py`
- `docs/development/OFFLINE_RECEIPT_REPAIR_2026-11-10.md`
- `packages/phone-fleet-protocol/pyproject.toml`
- `packages/phone-fleet-protocol/src/phone_fleet_protocol/receipt_readback.py`
- `packages/phone-fleet-protocol/src/phone_fleet_protocol/social.py`

AP 工作树：`D:/Projects/auto-promotion-offline-receipt-repair-20261110`，同名分支，基线 508f891。

- `docs/OFFLINE_PHONE_RECEIPT_REPAIR_20261110.md`
- `pyproject.toml`
- `src/auto_promotion/phone_receipt_readback.py`
- `src/auto_promotion/phone_social.py`
- `src/auto_promotion/telegram_phone_observer.py`
- `tests/test_phone_receipt_readback.py`
- `tests/test_phone_social.py`

共享协议的时间匹配器、签名只读核验模型及版本改动已随本修复分支准备，须由集成聊天接收并配套安装新版本；其他聊天的工作树和运行环境未改动。根目录 CI、发布配置及 `auto-promotion-mcp/pyproject.toml` 的旧 fleet 可选依赖锁仍由集成方更新，本轮未越过这些目录修改。

## 尚未验收

- 本轮未自动对真实 X / Telegram 历史任务执行对账，原线上 unknown 保持原样；真实只读核验需要加载配套服务后另行通过正常业务入口完成。
- 没有原节点命令日志、原基线、原提交事实或当前账号归属不再匹配时，保持 unknown；不能补造基线或解除隔离来通过。
- 新签名核验队列已完成 SQLite 测试替身的并发和重启测试；专用 PostgreSQL、真实浏览器/平台刷新、真实节点网络中断仍需环境验收。
- Facebook / TikTok 等账号与界面适配、手机连接恢复、输入法和实际提交仍需真机复验。未把原分支已有界面适配修复算作本轮新增真机验收。

若集成基线已经包含 39ba36f，operator_actions.py 的空草稿摘要序列化规则保留主线等效实现即可；不要重复改写历史请求。
