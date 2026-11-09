# 内容生产、内容队列及配对验收

日期：2026-11-09。当前交付离线开发；手机和现有运行服务均未切换。

## 流程和边界

导入/发现互动目标 → 使用现有模板或 DraftService 产出内容 → 不可变内容队列 → 指定账号和时间，形成原发布任务草稿 → 明确启动 → 手机一次互动 → 原执行独立读取回执。

生产入口不要求发布账号或手机，不创建任务、提交许可或发送作业。目标上下文仅作为数据，不执行其中的指令。生成与执行解耦不代表随意更换已冻结的目标、正文或账号。未知提交仍保留原身份，只能查询回执。

复用现有 DraftService/已配置模型连接、模板渲染、SQLite 事务、原发送器及 MCP 官方 SDK。没有增加第二个消息中间件、重写模型 SDK 或绕过原发布器。新表全部为增量表；不修改旧任务状态、指纹或授权。

## 通用六平台生产入口

先通过现有 `POST /api/opportunities/import`导入机会记录，再调用已鉴权的 `POST /api/content-production`：

```json
{
  "idempotency_key": "weekly-content-20261109",
  "opportunity_ids": ["原机会记录ID"],
  "mode": "template",
  "profile": {"reply_template": "{facts}", "facts": "已确认的产品事实"}
}
```

`existing_draft` 复用来源草稿，`template` 渲染模板，`model` 调用当前配置的 DraftService。开发和测试没有调用真实模型。无配置或生成失败时保存可重试原因，不伪造输出。

`GET /api/content-production/{run_id}` 查询进度。MCP 对应 `produce_interaction_content`、`get_content_production`；已有 `save_interaction_content`、内容列表、批次交接与 `start_task` 继续使用。生产完成返回 `execution_started:false`。

相同幂等键和请求重用第一次的来源/产品资料快照及已保存内容；修改请求需新键。每个来源独立租约，生成调用在事务外；内容、生产映射和 ready 状态在同一事务落库。部分失败再次提交原请求只补缺失项。来源原文、版本、时间和指纹随内容保存，后续来源更新不覆盖已生产结果。

使用现有 `/api/content-queue/batches` 绑定内容与账号后仅生成草稿。启动复用原授权、停止、额度和设备占用规则。新手机队列取消旧的“计划时间后15分钟”隐式过期，但仍保留用户设置的业务结束时间；本次没有取消或延长既有期限。

## 现有自动发现及活动接入

高频策略新增 `content_queue_only:true`，默认关闭且旧策略序列化不增加默认字段。须创建新 policy_key，禁止在已有策略上切换该模式，防止影响在途任务。新模式复用现有发现、分类、产品事实和文字生成；跳过发布账号健康/冷却与执行分配，生成结果仅写内容队列，不建立发送或媒体生成作业。停用、全局停止及内容质量规则仍生效。现有高频发现范围仍是 X；六平台来源都可经通用机会导入后生产，并不代表六平台自动发现均已实现。

活动仅在既有 `dispatch_policy=phone_queue_v1` 时接入。生成草稿即入内容队列；materialize 绑定原活动任务与 assignment ID，不另建任务。崩溃后再次 materialize 恢复原空草稿；已冻结、已尝试或有未知结果的任务不得重新收编。旧 dispatch_policy 仍走原路径。

## 配对集成检查

`docs/integrations/phone-pair.json` 固定群控仓库提交及协议/Fleet 包版本。Python 3.12 执行：

```powershell
python scripts/verify_phone_integration.py --phone-source D:/Projects/pap-r9 --work-root D:/Projects/pap-r9/.local/ap-ci
```

群控源必须是清洁的配对提交。默认运行完整新增测试、手机/MCP 消费测试及旧流程关键边界；发布前可加 `--full-legacy` 运行全部旧活动与高频策略回归。脚本构建并安装独立 wheel，使用群控依赖约束和官方 SDK/test 约束，清除真实业务配置环境，使用独立目录与临时数据库，运行契约/Fleet/AP 三层测试并输出 manifest/JUnit。样例和替身均非真机验收。

GitHub 工作流同样读取配对清单。群控提交须先在远端可读取；若仓库私有，需要已有权限的 `PHONE_PLATFORM_READ_TOKEN`。本次不推送、不创建凭据，也不宣称云端工作流已运行。

## 升级和保留历史

备份原数据库和运行版本；停在途领取后按配对升级各服务；对副本运行初始化和本脚本；比对原 task/item/attempt/manifest/unknown 记录；再切换实际服务。回退程序时保留新增表及生产结果，不删除或重新发送旧未知项目。实际生产数据库、凭据和运行服务本轮均未改动。

后续仍需：手机六 App 登录与允许目标、真实 App 适配和回执读取形态校准、逐 App 真实一次互动及独立回执、运行版本切换。界面六状态接线、Temporal 事件等待、可选业务期限属于后续流程项，本次不据此宣称全流程发布已通过。
