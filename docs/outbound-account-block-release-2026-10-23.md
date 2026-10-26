# 本次修复的发布准备与阻塞记录

> 历史阶段记录：后续 Telegram 读取优化仅获本地实施/测试授权，当前没有上传、CI 或发布许可。旧 tree/清单已失效；最新范围与结果见 [Telegram 读取验证](telegram-read-validation-2026-10-23.md)。本文的历史授权/测试/阻塞不代表当前已发布或当前可执行上传。

## 授权和源代码

用户在完成离线实施和审查后明确授权：直接推送 main、运行 CI、发布并做发布后验证。此次授权不包括真实发帖、验证码处理、新建凭据/设备或修改生产队列。GitHub 提交及 CI 由主协调代理负责；本记录形成时尚未产生本次发布提交 SHA，实际 SHA、CI 链接及操作结果以最终交接报告为准。

已从独立 Mac 工作区重新 fetch，main 仍为 `40d270b2b2abfbe0be029782d1731a0bc995bcda`，其 tree 是 `186458a981f445adf2c4517969cf490331e10097`。没有检测到需要合并的同事 main 新提交。原工作区未被替换，旧 `telegram_login_handoff.py` 保留且没有执行。

## 生产发布当前被拒绝

仅复用本机已有 Wrangler OAuth 做了只读核查，没有登录、复制 Windows 凭据或扩大访问权限。已登录身份只列出账号 `<existing-account-id>`，目标生产账号是 `<production-account-id>`。

以下两次只读动作各执行一次，均被 Cloudflare 明确拒绝：

| 动作 | 目标 | 实际结果 |
| --- | --- | --- |
| `wrangler deployments list --json` | 目标生产账号下 `auto-promotion-control` | exit 1，Authentication error，code `10000`，denied |
| `wrangler deployments list --json` | 目标生产账号下 `auto-promotion-telegram-executor` | exit 1，Authentication error，code `10000`，denied |

拒绝后没有重试替代 API、切换身份、代理或 UA。没有部署 Worker，也没有操作生产 stop、cron、队列、任务、租约、凭据或原节点。因为访问被拒绝，不能声称已实时核实当前 Worker 版本、额外线上配置、`global_stop` / `live_enabled` 或在途 lease。

委托提供的上次已知版本仅作为待核实参考：control `<previous-control-version>`，executor `<previous-executor-version>`。这些不代表本轮实时查询成功，也不代表本次已发布。

## 兼容性与后续发布条件

- 本地持久化仅增加 `account_blocks` / `account_block_events` 表。成熟库检测会发现缺表并迁移；不删除或改写既有 items、attempts、unknown 或回执。
- 账号阻断字段是附加字段。新控制层仍接受旧回执，同时对新字段实施 claim / final authorize / standby / offline gate；接收本地与远端状态时保留 generation 和人工恢复边界。
- 旧本地程序虽然可能打开增加了表的库，但不会实施新阻断语义，因此不能把旧 Windows 发送进程和新发送进程混跑，也不能通过回滚发送程序恢复已暂停账号。推送 main 或部署 Worker 都不等于原 Windows 实例已更新。本轮没有启动本地服务或 Windows 发送进程。
- 获取原有正确授权的生产访问后，必须先只读证明现有 `global_stop=true` 或 `live_enabled=false`，且没有可执行在途 lease。不能主动改生产 stop / 任务 / 配置来制造安全条件；无法证明时继续停止发布。
- 在该准确源 SHA 的必要 CI 全绿且上述条件成立后，先发布支持新字段与阻断 gate 的 control，再发布提供明确 Telegram 拒绝分类的 executor。发布前对比线上包/配置，保留仓库外的 cooldown 后激活逻辑、observability、服务绑定、秘密和原节点配置；不运行会覆盖 `OPERATOR_TOKEN` 的旧脚本。
- 回滚必须保持外发停止且无可执行 lease。旧 control / 旧本地程序不认识新阻断语义，不能把旧代码版本替换视作安全解锁；unknown 和已尝试状态不回退。需要记录并核对发布前后完整 Worker 版本、UTC 时间、源 SHA 和非秘密配置。
- 发布后只使用健康、鉴权拒绝或隔离的阻断闭环检查，不触发 cron/原生队列 tick、生产工作流或真实平台发送。Cloudflare 1010 明确拒绝不得通过修改 UA/代理或削弱策略绕过。

生产部署的下一步是恢复原有正确授权的执行环境（例如已持有既有 OAuth 的 Windows 节点上线）后做上述只读核对；本任务没有创建新凭据来替代它。

## 本地复验

本轮重新 fetch 后未修改功能代码，再次取得以下实际结果：

| 范围 | 结果 | 日志 |
| --- | --- | --- |
| 完整隔离 Python | 808 tests：805 通过、3 Windows 限定 skip；0 失败/错误，209.325 秒 | `/tmp/ap-outbound-release-python.log` |
| control Worker | 124/124 通过、0 skip | `/tmp/ap-outbound-release-control.log` |
| Telegram executor | 48/48 通过、0 skip | `/tmp/ap-outbound-release-executor.log` |
| 完整 UI runner | main 45、pools 模拟链路、interactions、faults 64、blocks 11 均通过 | `/tmp/ap-outbound-release-ui.log` |
| Ruff lint / format | 全范围通过；119 文件已格式化 | 最终命令输出 |
| Prettier / diff 空白 | 通过 | 最终命令输出 |
| Python build | sdist 和 wheel 通过，关闭 build isolation；未安装依赖或使用网络构建 | `/tmp/ap-outbound-release-build.log`、`/tmp/ap-outbound-release-dist/` |
| control / executor build | 两套 `wrangler deploy --dry-run` exit 0，未部署 | `/tmp/ap-outbound-release-control-build.log`、`/tmp/ap-outbound-release-executor-build.log` |

Python 浏览器与 HTTP 模拟器契约均显式启用，未把这些可运行项算成 skip。跳过仅为两个 Windows DPAPI 和一个实际 Windows PowerShell/CIM 场景；Windows 的实际 CI 结果需由准确提交对应的 GitHub Actions 提供。运行环境为 Python 3.14.2、Node v24.13.0、macOS 和已有 Chromium 1208（145.0.7632.6）。本机测试均使用临时数据库、隔离账号目录和合成/拦截请求。

此前完整离线验收和独立审查见 `outbound-account-block-validation-2026-10-23.md`、`outbound-account-block-independent-security-review-2026-10-23.md`；发布兼容、真实旧版迁移及回滚边界独立审查见 `outbound-account-block-release-safety-2026-10-23.md`。
