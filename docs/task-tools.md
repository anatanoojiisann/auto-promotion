# 每次任务的固定工具

不是每次任务都需要执行研究目录中的全部脚本。常用流程已经使用参数化的工作台、API 和 MCP 入口；历史脚本保留各次研究、批次和故障调查的记录。

| 常用步骤 | 固定入口 |
| --- | --- |
| 账号身份、限额和冷却检查 | 账号中心、任务执行前检查 |
| 发现公开候选及筛选 | 任务详情中的搜索／录入候选，MCP `discover_candidates`／`record_candidate` |
| Telegram 内容只读采集 | 共享内容池、定时采集来源，MCP `collect_telegram_content` |
| 人设匹配、草稿、排期和执行批次 | 推广活动，MCP `plan_campaign`／`create_campaign_batches` |
| 直接启动、按期执行和结果核对 | 任务详情，MCP `start_task`／`verify_task_item` |
| 互动汇总和定时报表 | 互动统计、MCP `interaction_statistics`／`statistical_reports` |
| 复用常用发现规则和执行节奏 | 任务模板 |
| 完整正文、账号、媒体和时间的审阅快照 | 任务详情 → 下载审阅清单 |
| 任务结果与全部关联日志 | 任务详情 → 导出任务日志 |

公开目录网页研究、商店页面检查和浏览器编辑器探测仍是按需调查工具。它们不是每次发送任务的必经步骤，也不会在后台自动启动。真实 X、Reddit 互动继续使用人工清单。

## 使用任务模板

1. 打开一个草稿、进行中或已结束任务，点击“保存为模板”，填写名称和用途。
2. 下次进入“任务模板”，点击“按模板创建任务”。
3. 重新选择本次账号、执行开始与结束时间，以及可选的目标白名单。
4. 创建新的草稿，准备发现范围与发送清单，点击开始发送。

模板保存查询主题、必需／排除关键词、语言、公开边界、筛选门槛、候选／入群上限、时区、间隔、顺序和任务限额。它不保存旧日期、账号选择、目标白名单、正文、媒体、候选资格、已尝试结果、额度快照或任何审批。新任务使用当前账号资料和当前服务端额度快照，并得到独立任务编号。模板按平台和真实／模拟模式区分。

模板是保存时的设置快照。修改来源任务或账号不会改写模板。需要不同规则时，可由另一个任务保存新的模板。当前模板页面支持保存和使用，不提供模板编辑或删除。

## 审阅清单与日志包

审阅清单为独立 HTML 文件，含任务政策、账号限额、历史审批记录、候选筛选依据、逐项完整正文／标题、动作、原消息、媒体文件名及哈希、明确时区的计划时间、顺序和当前结果。草稿和已冻结清单有不同标记。HTML 对来源文字转义，不运行页面脚本；下载不会固定或启动任务。

ZIP 包包含 JSON 结果、完整的任务关联审计日志、候选、发送／入群尝试、平台调用、该任务专用进程记录、投递时间证据、回复事件及采集覆盖、HTML 审阅文件、CSV 汇总及 SHA-256 校验文件。CSV 的公式前缀被转义。

所有数据从同一次数据库读取事务取得，不限定为页面上最近150条日志。包内只包含所选任务的关联记录：全局开关日志、共享发送进程、其他任务和旧研究目录中的额外文件不自动混入。使用两个任务编号分别导出，可以保存前后批次的独立记录。

导出字段经过筛选，不读取登录资料或账号健康原始内容，不打包数据库、凭据、媒体文件或媒体本机路径。未知、失败、排期和确认状态保留原值；已观察回复数不是完整覆盖的回复率。清单哈希来自原任务，导出是经过字段筛选的证据快照，不能作为恢复执行或重新发布的输入。

## 接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/task-templates` | 模板列表 |
| `POST /api/task-templates` | 从 `source_task_id` 保存模板名称和说明 |
| `POST /api/task-templates/{id}/tasks` | 使用明确的账号、名称、开始／结束时间及可选目标创建草稿 |
| `GET /api/tasks/{id}/evidence` | 读取完整任务证据 JSON |
| `GET /api/tasks/{id}/export?format=zip` | 下载日志包，也支持 `json`／`html` |

所有接口沿用本机认证和跨站检查。MCP 提供 `list_task_templates`、`save_task_template`、`create_task_from_template`、`task_evidence` 和 `task_review`。创建与保存工具只写本地草稿／模板；证据与审阅工具只读。

命令行提供 `template-list`、`template-save`、`task-from-template` 和 `task-export`，均支持全局 `--db`。以下示例中的任务和模板编号需要替换成实际值：

```powershell
auto-promotion template-save --task AP-EXAMPLE --name "AI 视频讨论"
auto-promotion template-list
auto-promotion task-from-template --template tpl-example --name "下一轮" --account acc-example --starts "2026-10-15T09:00:00+08:00" --ends "2026-10-15T20:00:00+08:00"
auto-promotion task-export --task AP-EXAMPLE --format zip --output ./exports/task.zip
```

命令行导出不会覆盖已有文件。新页面和接口需要重启工作台；新增 MCP 工具需要重启或重新连接 MCP 服务。
