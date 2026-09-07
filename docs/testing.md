# 测试说明

本仓库的默认测试不连接社交平台、不需要真实账号，也不依赖相邻后端目录。

## Python

```sh
python -m pip install -e '.[dev]'
ruff check src tests
ruff format --check src tests
python -m unittest discover -s tests -v
python -m build
```

当前共 65 项独立测试，覆盖：

- 四平台批次、13 种外发动作和三种发现流程的模拟执行。
- 精确审批持久化、重复确认幂等、清单与媒体哈希校验。
- 8 个并发 worker 的单次领取、进程恢复、未知结果不重发。
- 计划时间、逾期跳过、原生排期独立回执及投递日额度预留。
- 全局/任务停止、账号冷却、用户和目标限额、旧后端控制读取。
- 候选资格、单次入群、待管理员批准和只读成员核对。
- API 认证、跨站边界、静态界面、人工结果与取消状态。
- Telegram/Reddit 桥接契约、Telegram 回执与兼容辅助模块。

平台调用使用合成数据或 mock。兼容模块单独保存在 `integrations/`，测试从本仓库读取，不从开发者电脑上的其他工程读取。

## 浏览器

```sh
npm ci
npx playwright install chromium
npm run format:check
AP_UI_SKIP_SCREENSHOTS=1 npm run test:ui
```

脚本启动独立 Python 服务，使用系统分配的端口和临时 SQLite 数据库；强制关闭真实写入并将后端路径指向隔离目录。无论成功或失败，都会关闭测试进程并清理临时数据库。

验收包括三个中心、任务创建、泛化批准拒绝、两阶段精确审批、清单编辑和冻结、模拟执行、回执、全局停止及移动布局，共 11 类检查。无效批准导致的单次 HTTP 409 是预期行为；其他浏览器错误会使测试失败。

结果保存到忽略提交的 `var/ui-test-result.json`。省略 `AP_UI_SKIP_SCREENSHOTS` 时，截图写入 `output/playwright/`。不会连接已有工作台或修改其数据库。

## 验证边界

通过上述测试代表控制层和模拟流程工作正常，不代表真实平台登录、权限、媒体处理、审核或投递已通过验收。真实环境应按 [平台接入说明](platforms.md) 逐个平台完成账号检查与实际批准批次的回执验证。

GitHub Actions 在 Python 3.11 / 3.14 上执行自动检查与构建，并另行运行浏览器验收。具体运行结果以仓库 Actions 记录为准。
