# 本机 MCP 安装

本项目提供一个真正的 MCP 服务 `auto-promotion`，通过标准 stdio 协议连接
Telegram、Reddit、Instagram、X、Discord 五个平台的内置后端。
它不是旧电脑上的四个独立工程；无需寻找或安装那些旧工程。
使用[官方 Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)。

## 新电脑安装

Windows 在项目目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

脚本安装平台依赖及 MCP、下载 Chromium，并通过真实 MCP 握手和只读调用检查。
配置生成在 `var/mcp-client-config.json`，可填入支持 stdio 的 MCP 客户端。
其中使用当前电脑的 Python 和项目绝对路径，不包含账号密码、令牌或会话。

macOS / Linux：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[all]'
.venv/bin/python -m playwright install chromium
.venv/bin/python -m auto_promotion.mcp_server --print-config
.venv/bin/python scripts/check_mcp.py
```

`-SkipBrowser` 仅跳过下载；如果本机没有项目需要的 Chromium，完整检查会报告未就绪。

## 接入 Codex

安装后使用项目 Python 的完整路径注册，例如 Windows：

```powershell
codex mcp add auto-promotion -- "$PWD\.venv\Scripts\python.exe" -m auto_promotion.mcp_server --project-dir "$PWD"
```

注册后，在 Codex MCP 设置中重新连接；若当前聊天未加载新服务，重启客户端或新建聊天。
其他客户端读取生成配置中的 `command` 和 `args`。服务由客户端按需启动，无需常驻另一进程。
参见 [OpenAI 官方 MCP 配置说明](https://developers.openai.com/codex/mcp)。

## 工具与账号

服务提供状态、五个平台账号目录与连接检查、Discord 频道目录、任务草稿、候选发现、
候选筛选、清单编辑与冻结、批准后的入群/执行，以及回执与成员核对。

账号登录继续在[本机工作台或登录命令](platforms.md)中完成。
MCP 安装完成不代表每个平台账号已登录，也不代表具备全部发布权限。
服务读取项目 `.env` 并使用与工作台相同的数据库，不自动改变执行开关。
任务的两阶段精确审批由人在工作台完成，MCP 不提供审批、开启真实写入或解除全局停止的工具。
执行工具沿用任务完整性、限额、冷却、一次尝试和未知结果核对规则。

服务源码、安装脚本与依赖声明属于项目代码；提交到 GitHub 后同事才可拉取。
虚拟环境、生成的本机配置、账号会话及运行数据库不会上传。
