# 同程国际具体机场查询

2026-09-14 接入；仅使用公开匿名查询，不发送微信、不创建订单、不索取登录凭据。

官方代码来自 `https://file.40017.cn/iflight/iflight/app.061a10b4a76a72d0efc4.js`。
Book1 先 POST `https://www.ly.com/miflightapi/ts/preload`，JSON 为 `{search: 查询参数}`，再 POST `https://www.ly.com/miflightapi/ts/list`。列表用 `tid` 和 `done` 续查。官网用 `dants[0].ac` / `aants[-1].ac` 过滤机场，以 `fdate` 核对日期，以 `tp` 排含税价、`sp` 排票价。`pc-token: 1`、`t-token: 1` 是公开前端常量渠道标识，不是账户 token。

本程序保留所属城市查询参数，选择1成人、单程经济舱，再按实际机场过滤。城市/机场混合路线先用公开城市目录核实城市所属机场。仅完成且可核验的列表参与比价；预加载若无缓存不能推断没有航班，会继续请求实际列表。每日期最多1次预加载、4次列表请求；首日失败即停止余下日期，不使用城市日历兜底。购票链接中的城市 `para` 与 `departAirportCode` / `arriveAirportCode` 独立保存，全部机场的一端不添加具体机场过滤。

本机实际检查 PVG → CJU、2026-09-21：预加载成功但没有缓存航班，实际列表返回 HTTP 405，2次请求约1.98秒。官方页面还显示登录/安全验证，因此目前不能声称已成功取得该平台的国际机场价。测试夹具为官方 JS 字段约定的最小合成数据，未当作真实报价发布。
