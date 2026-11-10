# 内容解耦两处修复交接

日期：2026-11-09。Auto Promotion 版本 `0.1.1.dev4`；工作树 `D:\Projects\auto-promotion-receipt-content-20261109`，分支 `codex/receipt-content-integration-20261109`。群控配对提交仍为 `08d372fb0f0ce4ae1b03b132b72502ff22d7ce5c`，本轮没有修改群控代码或协议。

## 问题、原因和修复

| 问题 | 原因 | 修复 |
| --- | --- | --- |
| 高频自动内容、活动草稿不能进入手机发送 | 原帖作者写进 `recipient_id`，被当作实际收件人，触发手机顶层回复限制 | 手机路径新增可选 `source_author_id` 保存来源作者；`recipient_id` 继续表示实际收件人，手机限制继续校验 |
| 原候选一直显示等待绑定，无法反映发送结果 | 只有内容到批次、任务、执行项的映射，缺少从原执行项回到候选的结果投影 | 沿已有 producer link / batch / member / item 读取结果并写回原候选；不新建第二套发送器或调度器 |
| 关联候选后仍无法领取任务 | 旧候选检查把搜索策略允许的账号及原搜索账号的 API 能力当成发送授权 | `content_queue_only` 的执行交给冻结后的内容任务管理账号、时间和权限；普通高频直发策略仍运行全部原检查 |

## 具体契约与行为

- `ItemInput.source_author_id` 非空才进入序列化结果。老请求不增加默认字段，旧 payload、manifest 及内容指纹不因新增字段变化。实际收件人优先，其次来源作者，再以目标计算用户限额；历史 `recipient_id=作者` 的用量继续计入同一用户。
- 新手机活动草稿写来源作者；非手机旧活动保持原字段行为。通用内容生产和 X 高频内容生产保存来源作者。此元数据留在 AP，不作为手机收件人下发，不改变 `fleet.internal/0.2` 或 `publication.receipt/2`。
- 高频新内容 revision 加 `hf-content/2` 标记，来源 context 保存 `candidate_version`。已有内容和执行快照不回写、不重算哈希。旧未绑定错误内容可生成新修订；已有未知结果必须继续查询原执行。
- 内容批次 materialize 后保留原 task/item ID，补齐候选执行关联；`owner_account_id` 保留搜索/人设归属，`execution_account_id` 表示实际发送者。再次 materialize 重用原任务。
- `Engine.finish` 在保存回执的同一事务更新候选；周期 `sync_results` 仅补充读取。只使用内容 hash、批次 request hash、执行 payload hash、账号、平台、来源/正文版本全部匹配的关联。发现非本次执行的关联、歧义、损坏来源时跳过投影，保留业务回执。
- 对当前候选版本投影：草稿/排队 → READY；执行 → PUBLISHING；未知/待验证 → VERIFYING；有独立验证、原生 ID 和引用 → PUBLISHED；模拟回执 → SIMULATED；确定失败/错过/取消未提交 → CLOSED。不会仅因点击、许可、模型结束或排队成功而标记发布成功。
- 候选新版本不被旧回执覆盖；详情新增可选 `content_executions` 历史，列出每个内容修订、版本、原 task/item ID、关联验证情况及结果。没有此类执行的旧候选响应不增加该字段。
- 内容模式不进入旧 `hf_publish_retries` / `hf_verify` 自动调度路径。遗留 publish 作业只重用已绑定任务读取结果，不重建内容或重复发送。停止后可以只读补回执，不恢复发送。

## 验证

新增定向测试已完成：

- `tests/test_content_origin_links.py`：18 通过。覆盖旧序列化/哈希、跨旧新任务用户限额、真实收件人限制、同任务绑定、状态幂等、未知保留、新旧版本隔离、外国/损坏关联、陈旧 publish 作业和搜索账号授权解耦。
- `tests/test_automatic_content_phone.py`：29 通过。X 高频入口 4 场景，六 App 活动入口各 4 场景，另 1 项损坏来源不回滚原生回执。四场景为正常回执、停止重启后查回执、错配 item 回执、回执响应丢失；每条链仅一个提交请求和一次 attempt。

这 47 项全部使用临时数据库和显式模拟 transport。报告中的非模拟协议形态仅用于测试生产消费者，不能算真机成功。活动测试按规划时间推进模拟时钟，未修改计划和过期规则。

安装包回归已完成：pytest 为 **624 项通过、22 项子测试通过**；21 个测试模块，JUnit 记录 646 项（含子测试）、失败 0、错误 0、跳过 0，耗时 1056.49 秒。pytest 的主测试及 subtest 数量以原始输出和 JUnit 为准。

- 环境：Python 3.12、AP `0.1.1.dev4`、Pydantic `2.12.5`、protocol `0.2.1`、Fleet `0.2.0.dev19`；`pip check` 通过。新增 47 项还在 Pydantic `2.14.3` 的开发环境通过。
- 已确认 wheel、安装目录和当前源码的 145 份 Python 文件逐字节一致。wheel SHA-256：`971b95964ada552254de08bbb89c25ff2f60ffa988a3af7b239a9127662ed1e9`。
- 回归包括旧活动、高频/联合策略、原任务引擎、发送通道、正文完整性、MCP、内容生产/队列、六 App 手机消费者、回执契约消费和明确未提交处理。本轮未重新运行无改动的 Android/Fleet 全量测试，前轮数字仅供历史参考。
- 修改 Python 文件 Ruff 检查通过；除保留原格式的旧 `core.py` 外，修改文件格式检查通过；`git diff --check` 通过。
- 原始 manifest/JUnit/汇总分别位于工作树 `.local/origin-v4-regressions/manifest.json`、`promotion.xml`、`summary.json`；测试数据和数据库不提交。

实际执行：开发环境分别运行 `python -m pytest tests/test_content_origin_links.py -q`、`python -m pytest tests/test_automatic_content_phone.py -q`；构建 `python -m pip wheel --no-deps . -w .local/origin-v4-validation-b/wheels`，向隔离配对环境安装该 wheel 后执行 `python -m pip check`；在清除真实业务配置的隔离环境用该解释器运行 `python -m pytest`，完整 21 模块参数记录在上述 manifest 的 `tests` 数组，结果输出为上述 JUnit。


复现新增集成及既有边界：

```powershell
python scripts/verify_phone_integration.py --phone-source D:/Projects/pap-r9 --work-root .local/phone-integration --full-legacy
```

该脚本已纳入两份新测试及任务引擎、执行通道、正文完整性回归。使用 Python 3.12 和配对依赖；从 wheel 安装，清除真实账号/模型/云配置环境，使用隔离目录。运行日志中的真实网络服务、模型、手机均未启用。

## 文件及整合需要

本轮源代码：`models.py`、`quotas.py`、`planner.py`、`content_production.py`、`content_queue.py`、`content_origins.py`（新增）、`core.py`、`interaction_intents.py`、`interactions.py`，均位于 `src/auto_promotion/`。

测试：`tests/test_content_origin_links.py`、`tests/test_automatic_content_phone.py`。配对检查：`scripts/verify_phone_integration.py`。版本：`pyproject.toml`。文档：本文件及 `docs/CONTENT_PRODUCTION_INTEGRATION.md`、`docs/DEVELOPMENT_HANDOFF_20261109.md`。

整合时升级 AP 生产者和执行工作进程至同一版本，旧版本可能不认识新增可选字段；无需群控数据库/协议变更。本轮无新表、无生产迁移。提交仅限本开发分支，由整合会话决定合入；未推送、合并、部署或切换运行服务。

真机六 App（Telegram、Facebook、Instagram、X、TikTok、Reddit）验收仍为 **0/6**。本轮没有扩展六 App 自动搜索、界面状态接线、云端内容队列同步或 Temporal 编排。这些能力不能从本次离线通过推断为已完成。
