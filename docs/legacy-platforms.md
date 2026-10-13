# 可选旧工程兼容模式

默认后端已内置到本项目。只有仍保存原工程、原数据库和原账号环境时才使用本页。
显式设置 `AP_<PLATFORM>_BACKEND=legacy` 后，才会调用下列旧工程。
Windows 可使用 `AP_<PLATFORM>_PYTHON` 指定 `.venv/Scripts/python.exe`。
独立安装与新账号配置见 [内置平台接入](platforms.md)。

模拟模式无需任何后端。真实模式通过固定操作桥接已有工程；**这些后端并未包含在仓库中**，不能仅凭源码目录存在就视为连接成功。

## 目录与运行环境

`AUTO_PROMOTION_WORKSPACE` 指向包含下列目录的路径：

```text
promotion-backends/
  telegram-mcp/
  reddit-engagement-workflow/
  meta-social-engagement/
  x-engagement/
```

每个后端默认使用自己的 `.venv/bin/python`，可通过 `AP_<PLATFORM>_PYTHON` 指定其他解释器。桥接器在隔离子进程中读取对应后端的 `.env`，只返回允许的状态字段和错误类型。原始日志、平台凭据和完整身份响应不会传给工作台。

### Telegram

需要一个兼容本工作区 Telegram MCP 协议的服务，默认 `AP_TELEGRAM_MCP_URL=http://127.0.0.1:8765/mcp`。后端 Python 环境需要 `mcp` 客户端包。

账号标签必须与服务中的标签一致。桥接器使用：

- `search_public_chats`：发现公开候选。
- `promotion_account_health`：核对身份、账号可用性和冷却。
- `join_public_promotion_group`、`verify_promotion_membership`：已批准入群和只读成员核对。
- `send_message`、`reply_to_message`、`send_file`：调用时传 `verify_receipt=true`。
- `send_scheduled_message`、`send_scheduled_file`、`get_scheduled_messages`：原生排期及独立查询。
- `verify_sent_message`：只读消息回执核对。

`integrations/telegram/` 提供本项目需要的小范围兼容代码。它适用于已有 `telegram_mcp.runtime` 和 `task_executor` 的后端，**不是通用 Telegram SDK**。安装前核对版本和已有任务，使用 `git apply --check` 检查补丁上下文，再复制两个模块并应用补丁：

```sh
# 以下从 auto-promotion 仓库根目录执行；示例路径指向相邻后端。
git -C ../telegram-mcp apply --check ../auto-promotion/integrations/telegram/receipt-tools.patch
cp integrations/telegram/message_receipt.py ../telegram-mcp/telegram_mcp/message_receipt.py
cp integrations/telegram/receipts.py ../telegram-mcp/telegram_mcp/tools/receipts.py
git -C ../telegram-mcp apply ../auto-promotion/integrations/telegram/receipt-tools.patch
```

若补丁已存在，反向检查 `git apply --reverse --check` 可确认，不要重复覆盖。若上下文不匹配，需要人工移植；不要强制应用。受控重启服务后，核对工具允许列表是否包含上述接口。媒体目录也必须处于原 Telegram 服务允许的文件范围内。

候选缺少语言、活跃度或可信度等必要证据时会被保守拒绝。`pending_admin_approval` 不计为已入群；未知结果不重新入群。

### Reddit

需要 `reddit-engagement-workflow` 中的 `mcp_bridge`、`reddit_browser` 和 `rate_limit` 模块，以及独立登录的浏览器。`REDDIT_BROWSER_CDP_URL` 默认 `http://127.0.0.1:9223`，应与原工作流使用的浏览器保持一致。

账号标签使用 Reddit 用户名。健康检查核对浏览器头部可见的个人账号链接；无法确认身份时保持不可用。发布沿用原浏览器的文字帖子/回复实现和持久化限流。发现结果仍须经过中央资格门槛。

### Instagram

需要 `meta-social-engagement` 的认证 API、`meta_social` Python 模块和原数据库。桥接器使用原 Meta `.env` 中的操作令牌、OAuth、加密密钥及发布配置，不复制凭据数据库。

- `AP_INSTAGRAM_URL` 可覆盖服务地址；若原配置有本机 TLS 证书，默认连接 `https://localhost:8090` 并验证证书。
- `AP_INSTAGRAM_ACCOUNT` 必须显式填写允许发布的用户名，且与原后端允许账号配置一致。
- 发布目标使用原平台账号的 external account ID，并勾选自有目标；媒体必须有可供平台获取的 HTTPS 来源地址。
- 平台图片、视频、Reels、轮播处理和结果核验由原适配器承担。
- 第三方评论只进入人工核对与结果登记，不调用自动评论接口。

中央执行前读取原 `system_settings` 停止开关、`account_action_limits` 和已提交量。缺失设置、冷却或用尽额度均会阻止执行。

### X

需要 `x-engagement` 的读取模块、HTTP action provider、原数据库和独立浏览器动作服务。

- `X_ACTION_BASE_URL` 默认 `http://127.0.0.1:3000`。
- `X_ACTION_TOKEN` 必须在私密配置中填写，与动作服务 `MCP_AUTH_TOKEN` 一致。
- 账号标签使用不带 `@` 的用户名。
- 动作服务需提供受相同 Bearer 认证保护的 `/health`、`/identity` 和 `/actions`。

`integrations/x/identity.patch` 为现有 `services/x-action-service/src/mcp.ts` 增加只读 `/identity`。从项目根目录执行 `git -C ../x-engagement apply --check ../auto-promotion/integrations/x/identity.patch`，确认匹配后再应用、构建并受控重启。接口核对当前浏览器账号，不执行帖子、点赞或关注操作。

原 `system_control`、`action_limits`、`action_pacing` 及已领取记录仍约束真实执行。原创帖子与 Thread 不在当前中央动作目录中。

## 接管顺序

1. 部署兼容后端，完成原平台登录/OAuth，并运行 `auto-promotion check-accounts --live-only`。
2. 确认同账号旧 worker 和自动回复已停止，核对尚未投递的原生排期；保留原有限额和冷却。
3. 设置对应 `AP_<PLATFORM>_OWNER=auto-promotion`，需要真实执行时再开启 `AUTO_PROMOTION_LIVE_WRITES=1`。
4. 为实际批次创建新任务，展示明确目标、内容、媒体和时间，分别获取两阶段精确批准。
5. 按任务逐项检查回执。排期、已发布、待管理员批准、未知和人工登记是不同结果，不能互相替代。

配置开关不是平台授权，也不是任务批准。首次真实发布需要单独验收；本仓库模拟测试不证明某个账号具有当前发布权限。
