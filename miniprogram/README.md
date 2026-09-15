# 微信小程序版

这是现有机票监控的原生微信小程序入口，面向**一个绑定微信、最多 10 条行程**。支持国内/国际、城市全部机场/具体机场、单日/日期范围、多个平台比较、后台定时监控与订阅消息。原网页服务仍可单独使用。

## 当前交付边界

客户端、鉴权后端、订阅消息发送和部署脚本已写好。仓库没有你的真实小程序 AppID、AppSecret、订阅模板或公网域名，**还不是已发布、可以扫码使用的小程序**。本地自动测试使用模拟微信接口，不代表微信真机已送达。

小程序不需要桌面微信在线。监控在 Python 服务器运行，手机关闭页面不影响监控；服务器关机或休眠会停止查询。软件未增加收费查价 API，但微信账号相关流程、域名和服务器是否产生费用，取决于实际已有资源与微信后台要求，不能保证部署总成本为零。

## 微信订阅限制

使用官方 `wx.requestSubscribeMessage` 弹窗订阅，只在用户点击“订阅一条提醒”时调用。一次性订阅不能视为永久推送权限：多行程分别发送，每个符合条件的行程需要相应可用授权。前端返回 `accept` 只是授权操作结果，程序不虚构剩余次数；微信返回 43101 时标记需要重新订阅，不记录为已提醒。一次授权不足以保证 10 个行程都收到卡片。

长期订阅及限频长期订阅仅限微信实际开放的行业、业务场景和模板。虽然本项目与机票有关，也不能据此宣称获得了交通类长期模板。若账号已获匹配业务的长期模板，可配置该模板，代码会使用同一发送接口；最终频率仍由微信控制。不要选择与机票提醒无关的设备或公共服务模板。

官方依据（2026-09-15 核查）：[订阅消息类型与长期订阅范围](https://developers.weixin.qq.com/miniprogram/dev/framework/open-ability/subscribe-message-overview.html)、[订阅消息发送、字段规则及 43101 错误](https://developers.weixin.qq.com/miniprogram/dev/server/API/mp-message-management/subscribe-message/api_sendmessage.html)、[微信登录 code2Session](https://developers.weixin.qq.com/miniprogram/dev/server/API/user-login/api_code2session.html)。

## 1. 准备小程序账号与匹配业务的模板

在微信公众平台创建小程序，取得自己的 AppID 和 AppSecret，在“订阅消息”中选择可用于本业务的价格提醒模板。必须确认实际模板至少含行程的 `thing` 字段与金额的 `amount` 字段；有日期或平台字段时也可以配置。没有合适模板时需按微信流程申请，不能凭空填写模板 ID。

只在服务器项目根目录 `.env` 添加下面的配置。**不要把 AppSecret 放进小程序、GitHub 或聊天消息。**

```dotenv
MINIPROGRAM_APP_ID=你的微信小程序AppID
MINIPROGRAM_APP_SECRET=仅保存在服务器的AppSecret
MINIPROGRAM_TEMPLATE_ID=微信后台实际获得的模板ID
# 以下编号只是示例，必须逐项对应你选用模板的实际字段。
MINIPROGRAM_TEMPLATE_FIELDS={"thing1":"route","amount2":"price","date3":"departure","thing4":"platform"}
MINIPROGRAM_STATE=trial
```

支持字段映射：`route`→`thingN`、`price`→`amountN`、`departure`→`dateN`、`platform`→`thingN`、`flight`→`thingN` 或 `character_stringN`。`N` 是微信模板提供的编号，不能自行编造。配置中的字段应覆盖实际模板全部必填项。开发/体验/正式消息分别配置 `developer` / `trial` / `formal`，需与真实上传版本对应。

后端自动获取并缓存微信 access_token；用户不需要手工复制或定期更新它。若微信后台要求服务器 IP 白名单，添加实际出口 IP。

## 2. 启动后端与首次绑定

在项目根目录执行（Python 3.11+，无需新增 Python 依赖）：

```bash
python -m flightwatch.mini_server --check
python -m flightwatch.mini_server --pair
python -m flightwatch.mini_server
```

`--check` 只检查配置格式，不调用微信。`--pair` 输出一次性首次绑定码，24 小时有效；在手机登录时填写，完成后失效。服务一经绑定只允许同一微信再次登录，不是开放注册的多用户平台。登录通过微信 `code2Session` 确认 OpenID，客户端不能指定消息接收人。

后端默认只监听 `127.0.0.1:8766`。用自有域名通过 HTTPS 反向代理到这个端口，例如 [Caddy 配置](../deploy/mini-api.Caddyfile.example)。在微信后台将该 HTTPS 域名配置为 request 合法域名。手机里的 localhost 指向手机自身，不能用电脑的 `localhost:8766` 代替服务器域名。

Linux 常驻安装：

```bash
bash deploy/install-mini-service.sh
```

该脚本先检查配置，再安装用户级 `flightwatch-mini.service`；退出登录后是否保持运行取决于服务器的用户服务配置。可选择已有云服务器常驻运行。不要把未鉴权的桌面网页端口 8765 反向代理到公网。

配置和提醒状态保存在 `data/miniprogram/`，不与桌面版混用。已启动的监控会在小程序服务重启时按保存的设置恢复；已过期或无效行程会显示恢复错误，需重新设置。点“停止监控”会清除自动恢复标记。

## 3. 导入、真机预览与发布

1. 在微信开发者工具导入本目录 `miniprogram/`，把项目 AppID 设置成自己的真实 AppID（仓库中的 `touristappid` 仅为导入占位）。
2. 编辑 `config.js` 中的 `apiBase`，填入已配置好的 HTTPS 后端域名。AppID 是公开标识，AppSecret 必须只在服务器。
3. 使用开发者工具“预览”，手机扫码。首次输入绑定码登录。
4. 选择城市“全部机场”或具体机场，设置日期和平台，加入行程清单；可继续加入其他行程，然后保存。
5. 点击“订阅一条提醒”，同意微信弹窗，再点击“监控全部行程”。一次性授权不足时需要再次主动订阅；这不等于定期保持客服会话。
6. 让真实报价满足条件后，确认微信收到相应行程卡片；点卡片可查看该行程完整内容。未确认含税价或实际机场的城市参考不会被推送。
7. 按微信要求完成账号资料、隐私保护指引、备案/审核等发布流程，上传版本并将服务端 `MINIPROGRAM_STATE` 改为 `formal` 后重启服务。项目不会代替你提交身份资料或凭空创建正式模板。

购票链接采用“复制购票链接”。第三方购票网站不属于本小程序控制的业务域名，不能假设可直接嵌入小程序 web-view；复制后在浏览器或对应平台核价购票。提醒中的价格仍只是查询时参考价。

隐私指引需按实际部署填写：后端保存微信 OpenID（账号绑定）、会话凭证摘要、用户选择的行程、监控价格及提醒详情；不读取微信聊天记录、手机通讯录或手机号。提醒详情最多保留 90 天，在下一次写入时清理；持有服务器权限者可停止服务后删除 `data/miniprogram/` 清除本小程序全部绑定和监控数据。

## 验证

```bash
python -m unittest discover -s tests -q
node tests/test_miniprogram_client.js
```

自动测试覆盖绑定隔离、会话过期、错误身份拒绝、多行程参数、微信错误码、消息详情、逐行程发送与失败不记为成功。发布前仍必须完成上述微信真机授权和接收验证。
