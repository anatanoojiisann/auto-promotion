# 任务结束后的浏览器标签清理

X 的 Tabbit 自动发帖和回复在独立核验成功、保存回执后，自动关闭该操作创建的标签。
后续任务按原流程创建页面，浏览器账号和登录会话不被删除。

清理使用当前任务的 `finish --discard`，不遍历或关闭浏览器中的其他标签。
该执行路径不通过 `--tab` 或 `--group` 获取用户已有页面；以后若引入这类获取行为，
必须同步调整清理逻辑，不能再关闭全部任务所属页面。

- 正在运行、排队或通信状态不明确的请求：保留任务与页面。
- 未核验成功、需要继续查回执、发送前中止或留下草稿：保留页面。
- 清理失败：保留已核验的发布结果，返回 `browser_cleanup_state=incomplete`。
- 清理完成：返回 `browser_cleanup_state=closed`。

独立 Playwright 后端原本就在调用结束后关闭浏览器上下文；Tabbit 健康检查和采集
原本已关闭自建临时页。TikTok、Meta、Browser Use 借用的现有用户标签不在本次清理范围。
历史遗留标签也不会按“不在前台”一刀切关闭，因为这不能证明它们已经无用。

离线验证：`python -m unittest discover -s tests -p test_browser_tab_cleanup.py -v`。
