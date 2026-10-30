# 手机执行后端：观察与草稿验收开发接入

2026-10-30 更新：兼容 B 0.2.0.dev6 和共享内部协议包 0.1.0。AP→B→HTTP A→模拟 Portal 的观察/草稿回读联调已通过；真机和真实社交身份尚待验收。

原开发记录：2026-10-29；独立分支 `codex/phone-fleet-backend`，基线 `88c0277924273e5f55abbdb23bd8ed4f481550c2`。代码不在原 Auto Promotion 主目录运行，未使用其未提交 browser 修改、凭证、数据库或推广任务。

## 当前能力

已增加 `execution_method=inherit/api/browser/phone` 和 `execution_node_os=any/windows/macos` 政策字段。原 `execution_channel=inherit/cloud/local` 语义保留，未迁移任何旧政策或 frozen hash。可信账号配置中的 `connection_type=phone_fleet` 和独立 phone_fleet 绑定可路由到 `<platform>.phone` 资源池，执行方式与节点系统分开表示。其他账号的 route.public 保持原字段与取值。

真实 phone 后端外发、入群、候选采集均拒绝，preflight 明确不可提交；verify 返回待业务证据的 unknown。尚未通过手机账号健康、目标和提交验收，因此不允许用户把这个路由视为可生产外发。Discord 无已安装验收，TikTok Lite 不在本轮范围。

开发准备任务通过 `PhonePreparations` 使用 AP 原 `execution_jobs` 持久化表，kind=`phone_prepare`、pool=`phone.prepare`，映射表为 `phone_fleet_preparations`。旧 send/join Worker 显式排除此 kind；运行独立准备 Worker。它检查原任务 active/ready、`task.started` 审计、manifest 完整性、账号限制、期限、开发任务 allowlist 和通道；不会新增候选、限额、去重或业务调度逻辑。

准备操作只有 observe / draft_validate：后者读取现有编辑内容并比较冻结文本和账号摘要，尚不填写或提交正文。冻结内容摘要、task/item/account、群控 execution/command/device/node 均有持久映射。所有准备结果都带 `publication_verified=false`；原 item、attempts、配额和去重不会被准备结果改为发布成功。

## 安装和离线演示

两个服务分别安装共享协议包与 B 的 `phone-fleet-management==0.2.0.dev6` wheel。开发时可从本轮群控副本安装；不要依赖原生产副本：

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -e '.[dev]'
.venv/Scripts/python -m pip install <shared-contracts-wheel> <phone-fleet-protocol-wheel> <phone-fleet-management-wheel>
.venv/Scripts/python -m auto_promotion.phone_fleet_demo
```

演示使用 `var/phone-fleet-demo` 下随机临时数据库和合成账号，从 AP 正常创建/启动 API 核心进入持久准备队列，经独立 Fleet FastAPI、模拟节点、模拟观察和草稿回读，再查询/消费事件。不会访问手机、ADB、社交网站或原数据库。验收输出要求：两项 preparation succeeded，simulated=true、outbound_attempts=0、原 item=not_attempted、manifest_unchanged=true。这些是模拟结果。

测试：`pytest tests/test_phone_fleet.py tests/test_persistent_pools.py tests/test_task_channels.py tests/test_execution_channels.py tests/test_channel_recovery_faults.py -q`。

## 显式开发服务配置

配置放忽略的 `var/phone-fleet/config.json`；不提交真实 token/账号。格式：

```json
{
  "mode": "development", "enabled": true,
  "fleet_url": "http://127.0.0.1:18340",
  "service_token": "PRIVATE_DIFFERENT_BUSINESS_ROLE_TOKEN",
  "authorized_task_ids": ["EXPLICITLY_STARTED_DEVELOPMENT_TASK"],
  "channel": "local",
  "bindings": {
    "AP_ACCOUNT_ID": {"device_id": "TRUSTED_FLEET_DEVICE_ID", "identity_sha256": "64_LOWERCASE_HEX_DIGEST", "node_os": "windows"}
  }
}
```

正常 API 的 `create_app(phone_fleet_config=<private-config>)` 显式传参后才注册有效准备服务。未传配置时，新端点返回 disabled，不读取环境来隐式启用；不会随原 sender 启动。业务 API：

- `POST /api/tasks/{task_id}/items/{item_id}/phone-preparations/{observe|draft_validate}`：记录开发准备请求；不启动原任务，不外发。
- `GET /api/phone-preparations/{job_id}`：查询持久映射。
- `POST /api/phone-preparations/{job_id}/{pause|resume|cancel}`：控制已映射执行；unknown 不覆盖。

这些端点继承原 API 的本机/令牌/同源/CSRF 边界。群控控制台没有推广创建入口。准备 Worker 使用明确路径：

```powershell
.venv/Scripts/python -m auto_promotion.phone_fleet --config var/phone-fleet/config.json --database var/PRIVATE_DEVELOPMENT_DATABASE.db
```

需要显式准备请求且原任务已明确启动。不要给本命令传原运行数据库。本轮没有运行持久 Worker 或配置原服务。

## 重启、结果和通道边界

发送前持久记录 sending；POST 响应丢失或进程恢复，只按原 task/item/op 查询，不再 create。404 不能证明延迟请求不会到达，保持 uncertain 等待核查。恢复原映射后消费 revision；事件 ID/cursor 持久化，仅作为重新查询提示。真实 native unknown 在 B 保留，AP 已映射 unknown 不因暂停、取消或后续快照消失。结果上传可以幂等重复，原生命令不能重复。

配置 channel 必须匹配原规则解析的通道；失败不改成另一通道，不改旧任务字段/默认策略，不迁移原任务或创建云端 checkpoint。本地观察可以连本地 B 独立开发服务。**本轮准备操作不消费任何外发授权**；云端写入、独立本地写入授权、原 TaskChannelRouter/一次许可/额度/去重及原生业务回执仍属于后续真实提交集成，不能用 preparation 成功代替。

## 集成交接

修改：models.py、routing.py、adapters.py、api.py、queue_store.py、pyproject.toml；新增 phone 后端、phone_fleet 准备队列、离线 demo、本说明和测试。没有修改 browser.py 或 test_browser_contracts.py，没有推送/合并/部署。

需先接收 B 协议包，再实现 A 的 fenced runtime 与不可变证据导入。公共 `auto-promotion-mcp` 原 PhoneFleetProvider 仍 observe-only，另有显式开发 `InternalPhoneFleetProvider`；不能描述为真实 AP 手机推广已经打通。真正开放 phone 账号还需要 ConnectionManager 的正式登记、账号观察、TaskChannelRouter 最终授权、发布许可、平台逐项验收和独立原生业务回执。目前全部真实互动提交关闭。
