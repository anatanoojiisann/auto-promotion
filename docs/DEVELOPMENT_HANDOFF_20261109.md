# 六 App 回执与内容生产开发交接

日期：2026-11-09。仅本地隔离开发与离线验收；未切换实际服务、未操作真实手机、未调用真实模型生成或发布社交内容。

## 代码归属

- 群控工作树：`D:\Projects\pap-r9`；提交 `08d372fb0f0ce4ae1b03b132b72502ff22d7ce5c`。
- AP 工作树：`D:\Projects\auto-promotion-receipt-content-20261109`；本文件随本轮 AP 提交保存，最终 SHA 以该工作树 `git rev-parse HEAD` 为准。
- 两仓库开发分支均为 `codex/receipt-content-integration-20261109`。main 仍分别为群控 `39ba36f8b0866c12ec6b7f49b498d69d2a891e38`、AP `fd5cef52cc2fe0118d977c6b2c76364c0661dcb0`。未合并或推送本轮分支。

## 实现与兼容范围

- 六平台 `publication.receipt/2`、平台原生引用验证、发送前后证据和原任务绑定；旧回执继续接受，未使用 v2 的原 JSON 指纹不变。Fleet 和 AP 消费同一协议包，原授权、一次提交、设备/命令关联和 unknown 保留逻辑继续生效。
- 通用内容生产支持六平台机会记录，复用现有模板和 DraftService；不要求发布账号、手机或任务启动。来源与产品资料快照、幂等键、逐项租约、部分重试、内容和映射同事务保存。
- X 高频自动生产可创建 content_queue_only 新策略；旧策略模式不可暗中切换。分类、编辑、生成、定时唤醒均进入内容队列，不触碰执行分配或发送。活动 phone_queue_v1 路径保留原 task/assignment ID；普通活动仍走旧路径。
- 复用现有任务队列、SQLite/SQLAlchemy、Pydantic/JSON Schema、pytest、官方 MCP SDK 和 GitHub Actions；没有建立跨产品共享数据库。

## 实际验证结果

| 检查 | 结果 |
| --- | --- |
| 配对 wheel 集成：protocol | 26 通过，0 失败/错误/跳过 |
| 配对 wheel 集成：fleet | 52 通过，0 失败/错误/跳过 |
| 配对 wheel 集成：promotion | 339 通过，0 失败/错误/跳过 |
| 手机控制全量回归 | 731 通过、6 环境相关跳过，另有 8 个 subtest 通过 |
| Fleet 全量回归 | 113 通过、6 环境相关跳过；随后增加的 24 个真实报告形态证据缺失用例全部通过，回执模块最终 48 通过 |
| 包锁校验工具 | 2 通过 |
| 本轮修改文件 Ruff / Git diff | 通过 |

各组存在重叠，不直接相加。所有设备和平台返回均为显式测试替身或 synthetic 样例，真实发布验收仍是 0/6。
配对 manifest：`D:\Projects\pap-r9\.local\ap-ci\6967ba27cf\manifest.json`。JUnit 为同目录 protocol.xml、fleet.xml、promotion.xml；记录了包指纹及实际依赖版本。`pip check` 和独立安装位置断言通过。

## 可复跑命令

使用 Python 3.12，在 AP 工作树执行：

```powershell
python scripts/verify_phone_integration.py --phone-source D:/Projects/pap-r9 --work-root D:/Projects/pap-r9/.local/ap-ci

# 发布前可追加 --full-legacy，包含全部旧活动和高频策略测试

```
群控包验证：`python tools/verify_package.py --package packages/phone-fleet-protocol`。本轮控制/Fleet 全量回归使用 `.local/receipt-control-environment.json` 中的测试解释器，并将 control-system/src、control-system/fleet-management/src、packages/phone-fleet-protocol/src 加入 PYTHONPATH，再运行对应 tests 目录。完整命令和测试结果均不涉及真实服务。
完整控制/Fleet 回归的执行目录为 `D:/Projects/pap-r9`，命令如下（测试解释器由包验证工具生成）：

```powershell
$env:PYTHONPATH='D:\Projects\pap-r9\control-system\src;D:\Projects\pap-r9\control-system\fleet-management\src;D:\Projects\pap-r9\packages\phone-fleet-protocol\src'
& .local\pv\phone_co-00beeaea24d5\venv\Scripts\python.exe -m pytest control-system\tests -q --tb=line --junitxml=.local\control-final.xml
& .local\pv\phone_co-00beeaea24d5\venv\Scripts\python.exe -m pytest control-system\fleet-management\tests -q --tb=line --junitxml=.local\fleet-final.xml
& .local\pv\phone_co-00beeaea24d5\venv\Scripts\python.exe -m pytest control-system\fleet-management\tests\test_publication_receipt_integration.py -q --tb=line --junitxml=.local\fleet-v2-final.xml
& .local\pv\phone_co-00beeaea24d5\venv\Scripts\python.exe -m unittest discover -s tools\tests -v
```

## 尚未通过的全仓检查及环境限制

- 当前主线遗留的全仓格式检查仍未通过。群控未改动文件中有 11 个 lint 问题，以及 19 个待格式化文件；例如 async_submit_review.py、social_flow.py、test_async_visual_review.py。AP 也有本轮范围外的旧格式问题，例如 account_control_state.py。本轮所有改动文件检查通过，不能据此宣称原全仓 CI 全绿。
- GitHub 上的配对工作流尚未运行；需要先让群控配对提交在远端可读取。私有跨仓库检出需要具备既有读取权限的 PHONE_PLATFORM_READ_TOKEN；本轮没有配置凭据。
- 契约没有自动让任何 App 的 receipt_capable/commissioned 变为真。现有手机适配器继续旧回执，v2 新原生读取器需要手机场景校准后接入。Instagram/Facebook/TikTok 的候选链接形态不能当作已验证原生链接。
- 高频自动发现仍限原有 X 路径，其余平台可使用导入机会与通用内容生产，不宣称六平台自动找目标均已实现。
- 业务结束时间仍保留；可选业务期限、前端六状态接线及 Temporal 事件等待属于后续项。运行版本与生产数据库未切换，旧任务不迁移、不重发。
- 手机接入后逐个验收 Telegram、Facebook、Instagram、X、TikTok、Reddit：真实一次互动、同一任务原生 ID/作者/父目标/正文独立读回、证据归档，以及未知/断线/取消不重发。Discord 不属于此次新增验收，原能力保留。

## 合并与部署交接

集成方先检查两仓库分支和配对清单，再接收群控提交与 AP 提交。各产品独立构建/安装；发布前使用数据库副本备份并复核在途任务，按配对版本升级，回退保留新增表和已有内容。真机验收与最终服务切换另行执行。

## 精确文件清单


### 群控


- `D:\Projects\pap-r9\.github\workflows\ci.yml`
- `D:\Projects\pap-r9\auto-promotion-mcp\pyproject.toml`
- `D:\Projects\pap-r9\control-system\fleet-management\contracts\fleet.schema.json`
- `D:\Projects\pap-r9\control-system\fleet-management\contracts\openapi.json`
- `D:\Projects\pap-r9\control-system\fleet-management\pyproject.toml`
- `D:\Projects\pap-r9\control-system\fleet-management\requirements-dev.lock`
- `D:\Projects\pap-r9\control-system\fleet-management\src\phone_fleet\__init__.py`
- `D:\Projects\pap-r9\control-system\fleet-management\src\phone_fleet\publication_receipts.py`
- `D:\Projects\pap-r9\control-system\fleet-management\src\phone_fleet\service.py`
- `D:\Projects\pap-r9\control-system\fleet-management\tests\test_publication_receipt_integration.py`
- `D:\Projects\pap-r9\control-system\pyproject.toml`
- `D:\Projects\pap-r9\control-system\requirements-integration.lock`
- `D:\Projects\pap-r9\control-system\src\phone_control\__init__.py`
- `D:\Projects\pap-r9\control-system\src\phone_control\fleet_runtime.py`
- `D:\Projects\pap-r9\docs\PUBLICATION_RECEIPTS_V2.md`
- `D:\Projects\pap-r9\packages\phone-fleet-protocol\pyproject.toml`
- `D:\Projects\pap-r9\packages\phone-fleet-protocol\requirements-test.lock`
- `D:\Projects\pap-r9\packages\phone-fleet-protocol\src\phone_fleet_protocol\__init__.py`
- `D:\Projects\pap-r9\packages\phone-fleet-protocol\src\phone_fleet_protocol\receipts.py`
- `D:\Projects\pap-r9\packages\phone-fleet-protocol\src\phone_fleet_protocol\schemas\publication-receipt-v2.schema.json`
- `D:\Projects\pap-r9\packages\phone-fleet-protocol\src\phone_fleet_protocol\schemas\receipt-v2.synthetic.json`
- `D:\Projects\pap-r9\packages\phone-fleet-protocol\src\phone_fleet_protocol\social.py`
- `D:\Projects\pap-r9\packages\phone-fleet-protocol\tests\test_publication_receipts.py`
- `D:\Projects\pap-r9\tools\verify_package.py`

### Auto Promotion


- `D:\Projects\auto-promotion-receipt-content-20261109\.github\workflows\phone-integration.yml`
- `D:\Projects\auto-promotion-receipt-content-20261109\docs\CONTENT_PRODUCTION_INTEGRATION.md`
- `D:\Projects\auto-promotion-receipt-content-20261109\docs\DEVELOPMENT_HANDOFF_20261109.md`
- `D:\Projects\auto-promotion-receipt-content-20261109\docs\integrations\phone-pair.json`
- `D:\Projects\auto-promotion-receipt-content-20261109\pyproject.toml`
- `D:\Projects\auto-promotion-receipt-content-20261109\requirements-phone-test.txt`
- `D:\Projects\auto-promotion-receipt-content-20261109\scripts\verify_phone_integration.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\src\auto_promotion\api.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\src\auto_promotion\campaign_content_queue.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\src\auto_promotion\campaigns.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\src\auto_promotion\content_production.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\src\auto_promotion\content_queue.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\src\auto_promotion\content_schema.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\src\auto_promotion\interaction_models.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\src\auto_promotion\interactions.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\src\auto_promotion\mcp_server.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\src\auto_promotion\phone_reddit.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\src\auto_promotion\phone_social.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\src\auto_promotion\publication_receipts.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\tests\test_content_production.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\tests\test_hf_content_queue.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\tests\test_mcp.py`
- `D:\Projects\auto-promotion-receipt-content-20261109\tests\test_publication_receipt_integration.py`

## 复用的官方资料


- [Pydantic serialization](https://docs.pydantic.dev/latest/concepts/serialization/)
- [pytest usage](https://docs.pytest.org/en/stable/how-to/usage.html)
- [MCP SDK architecture](https://modelcontextprotocol.io/docs/learn/architecture)
- [GitHub checkout and cross-repository access](https://github.com/actions/checkout)
- [GitHub setup-python](https://github.com/actions/setup-python)


## 后续内容解耦修复

AP `0.1.1.dev4` 的来源作者分离、候选执行关联及按版本回执回流，见 [两处修复交接](CONTENT_ORIGIN_LINKS_20261109.md)。本文件前述数字是前一轮结果，新增验证以该交接为准；群控配对版本保持不变。
