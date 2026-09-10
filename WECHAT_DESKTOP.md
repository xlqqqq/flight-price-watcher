# 无 token 的微信桌面通知

通知会通过本机已登录的 Windows 微信发送到自己的「文件传输助手」。无需 PushPlus、公众号接口、API key 或 token；仍需本人正常登录微信，并让运行监控的电脑保持开机、联网和桌面解锁。发送时程序会切换到文件传输助手，请在发送过程中暂停鼠标和键盘操作。

1. 在 Windows 10/11 x64 安装 Python 3.11–3.13，打开并登录简体中文微信。锁定依赖 `wxauto4==41.1.7` 的维护者标明适用微信 4.1.8。
2. 在本项目目录运行 `python -m pip install -r requirements-wechat.txt`。
3. 执行 `python -m flightwatch.desktop_notifier` 可只检查环境，不发送消息、不切换会话。网页里的环境检查使用同一个只读方法。
4. 在查询页面保存航线、日期和目标价后开启监控。推送目标固定为文件传输助手，不需要填写收件人或令牌。

依赖可安装在个人虚拟环境中；安装命令仅在 Windows 生效。Linux、macOS 和 WSL 可查询机票、管理监控条件，但此桌面通知方式必须由 Windows 原生 Python 运行。服务器没有已登录的微信桌面时不能凭空发送微信。

程序先检查登录，再精确打开文件传输助手并核对聊天名称。遇到同名群、找不到窗口、锁屏、库返回失败或异常时不会把结果登记为成功。每次发送使用独立进程并初始化 COM，超过默认 20 秒则结束该进程；超时可能发生在微信已经发送之后，因此结果会标为未知，不在同次调用中重试。

`sent_to_client:...` 只表示桌面自动化库返回“成功”，不等于手机已收到或弹出通知。微信版本升级可能改变窗口结构；更换版本后先查看环境状态和第一次实际发送结果。当前开发环境是无微信桌面的 Linux，只验证了模拟客户端、失败路径和接口合同，没有在真实 Windows 微信中验收发送。

依赖依据（2026-09-08 核验）：

- [维护者在 PyPI 发布的 wxauto4 41.1.7](https://pypi.org/project/wxauto4/41.1.7/)：Windows x64 轮子、版本要求及微信 4.1.8 说明。当前分发含编译模块，不能称为完全开源；不要安装名称不同、要求商业授权的 `wxautox4`。
- 读取该 PyPI 轮子的 `wxauto4/wx.pyi`，核验 `IsOnline()`、`ChatWith(who, exact=True, force=False)`、`ChatInfo()`、`SendMsg(msg, who, clear, exact)`；读取 `param.py`，核验 `WxResponse` 的成功条件为 `status == '成功'`。程序在实例化前关闭该包可选遥测和文件日志。
- [维护者的 Chat 接口文档](https://docs.wxauto.org/docs/class/Chat.html)及 [WeChat 接口文档](https://docs.wxauto.org/docs/class/WeChat.html)：作为接口说明补充；文档中部分示例属于 `wxautox4`，实际代码以锁定的免费 `wxauto4` 轮子声明为准。
