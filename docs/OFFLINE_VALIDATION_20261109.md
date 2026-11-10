# 六 App 离线验证结果

日期：2026-11-09。结果：**本轮离线验证闭环通过**。真实手机、真实社交发布和真实模型生成均未执行，真机验收仍为 0/6。

## 验证方式

使用真实的内容生产、SQLite 存储、内容队列、任务启动、账号/通道许可、回执消费及结果持久化代码；模型输出和 Fleet/手机 HTTP 响应用显式测试替身。并另外运行协议、Fleet 事务和 Android 控制代码的独立测试。这里的六 App 通过指软件业务链路，不代表六个真实 App 的界面、按钮、键盘、登录或原生回执已通过。

## 六平台串联结果

| App | 正常回执 | 停止、重启后只读确认 | 错绑回执 | 回执响应丢失 |
| --- | --- | --- | --- | --- |
| Facebook | 通过 | 同一执行确认 | 保留待核验 | 原执行只读恢复 |
| Telegram | 通过 | 同一执行确认 | 保留待核验 | 原执行只读恢复 |
| TikTok | 通过 | 同一执行确认 | 保留待核验 | 原执行只读恢复 |
| X | 通过 | 同一执行确认 | 保留待核验 | 原执行只读恢复 |
| Reddit | 通过 | 同一执行确认 | 保留待核验 | 原执行只读恢复 |
| Instagram | 通过 | 同一执行确认 | 保留待核验 | 原执行只读恢复 |

24 条串联场景均验证：模型替身只调用一次、只创建一次提交尝试、原任务与 item/execution/manifest 关联保持一致、批次重入不生成新任务、未重发、已有独立任务不被改写。另有 2 个保护用例验证：没有内容队列绑定不能绕过旧活动规则，内容队列也不能替换真实活动的原分配。

## 本轮发现并修复的问题

| 问题 | 原因及修复 |
| --- | --- |
| 内容生成并入队后，无法转成执行项 | 生产出口漏传原机会记录的 content_key。现在快照保存原标识并传给内容队列；旧待生成快照可从冻结来源按现有规则恢复标识，不改写已冻结任务。 |
| 独立内容队列被旧活动规则拒绝 | 旧代码把所有 opportunity_id 都视为活动分配。现在只有经校验的独立内容批次使用自身绑定；活动任务和普通旧入口继续验证原分配。 |
| 结果已确认，状态投影仍待核验 | 保存结果的字段清单过滤掉 native_id 等回执信息。现在保存原生 ID、契约版本、读取来源和证据指纹，保留原成功判据。 |

AP 修复版本为 0.1.1.dev3，协议/Fleet 包和群控源码没有变更。复用现有模型、队列、许可和 SDK，没有切换运行服务或实际业务数据库。

## 回归结果与记录

- 修复后 AP 有效结果：577 项通过，另 22 个子检查通过、0 跳过，未遗留业务失败；包含新增 26 个用例。
- 协议：26 通过。
- Fleet：136 通过、7 跳过；跳过项需要外部服务或可选存储依赖。
- 控制端：731 通过、6 跳过，另有 8 个 subtest 通过。
- 本轮代码 Ruff check、Git diff 检查通过；原有全仓格式问题仍按此前交接保留。

**保留原始记录：** AP 全量运行有 1 项测试样例失败：负例使用的目标本就不在任务允许范围内，因此没有走到预期的“活动分配拒绝”分支。已修正为允许范围内的同一目标、不同来源/回复 ID，并使用相同修复后发布包复跑通过。只是修正测试数据，未放宽业务规则。报告的有效结果按每个用例的最新结果统计；原始失败 JUnit 和独立复跑 JUnit 均在证据目录，不隐藏历史结果。

## 验证耗时

AP 广回归用时约 14 分 20 秒；其中 30 账号/1000 内容的压力案例约 157 秒，Reddit 错绑回执负例约 33 秒。这些是离线测试耗时，不能作为真机发布耗时。后续快速复核可只跑新增的 test_content_phone_pipeline.py，完整发布前再跑广回归。

## 证据文件

本地证据目录：`C:\Users\22246\.codex\visualizations\2026\11\08\01a1bbfe-26b0-7602-942f-4be738a80ba9\Offline-六App-20261109`。JSON 和 JUnit 保存在该目录，不提交运行证据到仓库。

- summary.json：本轮有效结果、发布包指纹及源码指纹。
- chain-cases.json：24 条模拟链路的任务、内容、执行 ID 与次数。
- protocol-junit.xml / fleet-junit.xml / control-junit.xml：分层结果。
- promotion-raw-junit.xml / campaign-guard-recheck-junit.xml：AP 原始全量结果和测试数据修正后的复跑。
- tested-wheel-manifest.json：实际安装的 AP 发布包与源码哈希。该原始清单保留首次全量运行的状态；最终闭环结果以 summary.json 及本报告的复跑说明为准。

## 真机仍需确认

手机接入、六个 App 的真实账号和目标定位、输入与提交、独立读回原生回执、断线/锁屏/待审核恢复及对应读取器校准。此次未提升 App 发布能力开关，未把模拟记录转为真实业务记录。

## 开发交接与复跑

本轮代码文件：

- `src/auto_promotion/content_production.py`：保存并传递冻结来源的 content_key。
- `src/auto_promotion/core.py`：独立内容批次的绑定交接；原生回执字段持久化。
- `tests/test_content_phone_pipeline.py`：六 App × 4 场景，以及 2 个旧规则保护用例。
- `scripts/verify_phone_integration.py`：将串联用例加入常规配对检查。
- `pyproject.toml`：AP 修复包版本 0.1.1.dev3。
- `docs/OFFLINE_VALIDATION_20261109.md`：本交接记录。

Python 3.12 的常规复跑命令：

```powershell
python scripts/verify_phone_integration.py --phone-source D:/Projects/pap-r9 --work-root D:/Projects/pap-r9/.local/ap-ci --full-legacy
python -m pytest tests/test_engine.py tests/test_execution_channels.py tests/test_task_channels.py tests/test_outbound_text_integrity.py -q
```

本轮实际使用的 AP 全量测试文件清单和发布包指纹保存在 tested-wheel-manifest.json；实际测试解释器为 `D:/Projects/pap-r9/.local/ap-ci/6967ba27cf/venv/Scripts/python.exe`。新增串联用例也可单独运行 `python -m pytest tests/test_content_phone_pipeline.py -q`。上述命令均需已安装本轮配对包及测试依赖的独立环境。

本轮修复只保存在开发分支；没有推送、合并 main、切换实际服务或迁移实际业务数据。群控提交保持 08d372fb0f0ce4ae1b03b132b72502ff22d7ce5c。集成时接收本轮 AP 提交及既有群控配对提交即可，不需要删除或改写历史任务。
