# X 的独立 Chrome 自动回复连接

用户明确要求补齐 Milo 的自动发送通道后，新增了 `chrome` 浏览器选项。
它使用已安装的 Google Chrome 和账号目录内的 `browser-chrome` 独立会话，
与旧的 `browser`、`browser-msedge` 目录隔离。不会读取、复制或导出日常 Chrome 的凭据。

通过工作台的 X 连接窗口选择 Google Chrome，或运行：

```
python -m auto_promotion.cli login x --account <原后端标签> --browser-channel chrome
```

在新窗口完成登录后核验用户名。同一个本机会话用于后台的健康检查和发送。
连接成功本身不启用自动回复；只有用户明确要求自动发送的账号，才能把其
`automatic_actions` 配置为 `["reply"]`，并记录账号范围与变更审计。
原 `browser_use` 身份观察连接继续只允许人工交接，不能靠修改该标志自动发送。

自动回复仍走 `x.playwright` 持久执行池、账号锁、队列租约、冻结清单、限速、
配额和一次提交回执。今天的独立本地模式不需要服务器初始化。
不能把旧人工交接记录改成未尝试；重新排期必须创建新任务，保留旧任务证据。
切换前需确认旧项目未提交、没有发布链接且没有结果未知。
