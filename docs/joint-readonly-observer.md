# AIgram + Aurax 分账号只读观察

联合策略支持每个 X 账号独立观察公开帖子。每个策略只绑定一个搜索账号，按实际启动时间随机抽样下一次扫描间隔，默认 50–70 分钟。账号之间共享项目级原帖去重，避免给同一帖子重复建立回复任务。

观察组按 P0、P1、P2 顺序读取：P0 是针对该账号的实际求助，P1 是明确且未解决的产品需求，P2 是能提供具体帮助的相关讨论。若前一级已有通过本地过滤的帖子，低一级查询先延后；人工核查发现不合适时可继续做一次更宽泛的只读搜索。产品优先级在同一 P 级内生效，例如 Aurax 优先、AIgram 补充。浏览器仅记录主搜索栏可见的公开内容，无法证明完整时间覆盖；轮次标记为 `partial`，不推进完整覆盖水位。

Edge、Chrome 和 Tabbit 已有账号连接可用于只读发现。Tabbit 路线先核对已登录 X 身份，再从主搜索栏提取原帖 ID、作者、正文和时间；临时标签页在成功读取后关闭。连接失效、结果不可见、时间或身份无法核实时，记录能力缺口，不把它当作“没有需求”。

只读轮次只产生待核查候选，不自动分类、派发或发布。每账号每轮最多准备一条真实相关的 `draft` 回复任务，正文、账号、目标和原帖需逐项核对。执行项使用 `execution_mode=manual`；用户明确启动具体任务后，由人完成 X 网页发布并登记实际回复链接。没有相关帖子时记录 `no_match`，不能用无关内容凑数。旧策略的 P2 五小时批次和 P3 回退不适用于这组分账号观察策略。

运维入口：`scripts/joint_readonly_once.py` 只推进指定策略的到期只读扫描；`scripts/joint_account_review.py` 记录每轮人工筛选结果，防止重复登记或把另一个账号、另一个原帖的回复任务关联进来。定时器应逐个策略执行，并严格遵守数据库保存的 `next_scan_at`、预算、停止和账号冷却。

策略模型、调度与去重位于 `interaction_models.py`、`interaction_joint.py`、`interaction_schedule.py`、`interaction_observer.py`；Tabbit 读取位于 `backends/tabbit.py`。离线验收见 `tests/test_interaction_joint.py`、`tests/test_tabbit_connection.py`、`tests/test_browser_contracts.py`，页面验收见 `scripts/hf_ui_test.cjs`。
