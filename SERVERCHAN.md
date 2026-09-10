# Linux 微信通知：Server酱 Turbo 可选通道

Server酱 Turbo 的微信服务号通道可在 Linux 后台调用，无须登录电脑微信；按目前官方说明，无须每隔 24 小时回复机器人激活。免费用户每天最多发送 5 条。它需要一次性获取、保存免费的 **SendKey**；SendKey 是授权凭证，因此这个通道不是完全免凭证方案。当前桌面微信免 Token 方式仍可使用。

一次性准备优先使用本项目的手机微信扫码绑定，授权后由程序获取并保存 SendKey；这省去手动抄写密钥，后台仍然持有授权凭证。也可手动登录 [Server酱 Turbo](https://sct.ftqq.com/)，按控制台指引绑定/关注**方糖服务号**，在 SendKey 页面复制以 `SCT` 开头的密钥，并在本项目通知设置中保存。随后选择 Server酱通知并发送一条测试消息；测试也占一天的免费额度。此模块不支持推送到独立 App 的 `sctp`/Server酱³ 密钥，不会混淆两种落点。

发送时固定指定官方的**方糖服务号（通道 9）**，不跟随账户中可更改的默认通道，避免无意间切到需互动的 ClawBot 或独立 App。请确保这个服务号已关注并允许接收通知；手机扫码登录账户与开通接收通道是两个步骤。

免费版微信卡片只显示标题，因此标题应直接包含航线、价格、日期；完整详情可在消息落地页查看，官方免费内容保留 1 天。接口受理成功不等于微信已送达，应以手机收到消息确认首次配置。服务政策可能变动，本文核对日期为 2026-09-10。

## 本项目的额度与回执

- 同一数据目录、同一个 SendKey 每个北京时间自然日最多 **5 次发送尝试**。测试、拒绝、HTTP 错误、超时和无效回执均占一次，以避免未知受理状态下反复发送耗尽免费额度。
- 限额存于数据目录的 `serverchan-quota.sqlite3`，在网络请求前使用 SQLite 事务登记；并发实例、进程重启共用额度。库里只有密钥的 SHA-256 指纹、日期和计数，不保存明文密钥、消息正文或服务返回的阅读密钥。
- 达到上限后停止提交网络请求，明确报告本次没有发送；不要删除额度库来继续发送。同一个账号在别的软件发送的消息不在本地计数范围内，服务端可能更早达到其额度。
- 超时或受理结果未知时，**同一次发送调用不会立即重试**，也不会把未知结果记为已提醒。现有监控策略会在后续轮次继续检查条件，仍满足条件时可能再次提交；如果前一次其实已受理，可能收到重复消息。后续每次提交仍受每天 5 次预算约束。请在 Server酱后台确认未知请求的结果；需要避免重复时先暂停监控。
- 只有 HTTP 200、整数 `code=0`、整数 `data.errno=0`、`data.error=SUCCESS` 及有效正数 `pushid` 全部满足时，才返回 `accepted:serverchan:<pushid>`。这只是服务受理回执，不宣称已送达或已读。
- 标题必须为 1～32 个字符，不含换行/控制字符。正文按 UTF-8 完整发送，不截断。输入校验失败、本地额度用尽或计数库异常均不会发送。

开发接口（仅说明，不在导入、初始化、查询额度时发送）：

```python
from flightwatch.serverchan_notifier import ServerChanNotifier, quota_status

# sendkey 从本地秘密配置读取，不要写死在源代码或打印到日志。
notifier = ServerChanNotifier(sendkey, data_dir, timeout=20)
status = quota_status(sendkey, data_dir)
# status: {"today": "YYYY-MM-DD", "used": 0, "remaining": 5, "limit": 5}
# 明确需要发消息时才调用 notifier.send(title, content)。
```

所有请求只发往 `https://sctapi.ftqq.com/<SENDKEY>.send`，使用 POST 表单 `title`、`desp`、`noip=1`、`channel=9`。不跟随 HTTP 重定向、不回显服务错误正文或含密钥的请求 URL，同次调用不立即重试；后续监控轮次的重试规则见上文。HTTPS 同时验证证书链和主机名；在系统 CA 文件存在时补充加载系统信任链，保持默认信任，不关闭 TLS 校验。测试使用模拟传输，没有实际给微信发送消息。

## 官方资料

- [通道说明](https://sct.ftqq.com/docs/getting-started/channels/)：微信服务号、测试号、企业微信应用通道的区别及免费卡片显示能力。
- [免费额度和常见问题](https://sct.ftqq.com/docs/getting-started/faq/)：注册即免费，每天 5 条；订阅及其他产品是独立选择。
- [获取 SendKey](https://sct.ftqq.com/docs/getting-started/sendkey/)：扫码登录后复制凭证，不需要付费购买免费额度。
- [Server酱官方 SDK](https://github.com/easychen/serverchan-sdk)：Turbo 接口、参数及 `code=0` 受理协议。
- [方糖官方 Check酱调用示例](https://ft07.com/fxd-app-check-chan/)：返回结构包括 `data.pushid`、`data.error=SUCCESS`、`data.errno=0`。
- [官网当前加载的前端脚本](https://fox.ftqq.com/sct/static/js/main.7ca8c580.chunk.js)：`channel_options_suggest` 中 `方糖服务号` 对应 `value:9`，`send_test` 请求传递 `channel`。渠道编号以本次读取官网资产核实，未猜测账户默认值。

对比时不要把 PushPlus 的“免费额度”等同于新用户完全零付费：其[实名认证流程](https://pushplus.plus/doc/function/verify.html)要求支付认证费用或购买会员。ClawBot 的周期互动限制也不适用于这里的微信公众号模板消息通道。
