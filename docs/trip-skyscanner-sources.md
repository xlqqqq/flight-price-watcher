# Trip.com 与 Skyscanner 匿名报价契约

核验日期为 2026-09-14。两个提供方都只访问平台官网，不需要用户账号、API key、登录 Cookie 或长期 token。价格接口、字段和防护策略是网页内部实现，平台改版后可能失效；失效时提供方会报错，不会把入口链接、历史价或未完成搜索当成实时最低价。

## Trip.com

查询使用官方 `POST https://www.trip.com/restapi/soa2/27015/FlightListSearchSSE`。请求体固定为一个成人、无儿童婴儿、经济舱、单程、CNY，并把 `studentsSelectedStatus` 固定为 `false`。请求头不含 Cookie、Authorization 或 API key，`head.auth`、`ctok` 和客户端身份字段为空。

`studentsSelectedStatus` 不能沿用网页样本里的 `true`。同一时间对上海 `SHA` 至东京 `TYO`、2026-10-01 的匿名请求做了对照：`true` 返回 70 条、最低 ¥1,902；`false` 返回 65 条、最低 ¥1,916。字段名和结果差异都表明 `true` 可能开启学生资格范围，所以普通成人提供方使用 `false`，避免把资格价推送给所有人。

2026-09-14 的 `false` 实测返回 HTTP 200、`text/event-stream`，响应 296,572 字节。响应回显了 `SHA`、`TYO`、2026-10-01、`CN-JP` 和 `CNY`，最低普通价格为 ¥1,916，其中税费 ¥1,244，航班为 BR705/BR108。解析器同时核对：

- `ResponseStatus.Errors=[]` 和 `head.retCode=SUCCESS`；
- 只有一段查询行程，城市、日期、国家地区和城市机场清单一致；
- 每个可用政策的全部航段都是 `grade=1` 且带 `ECONOMY` 标志；
- 一成人 `salePrice + tax - discount = totalPrice`，成人总价、平均价和汇总总价一致；
- 航段时间递增、机场衔接、起终城市和出发日期一致；
- 排除带会员、登录、学生、新客、特定银行卡、积分、订阅等资格标志，或带未知非空限制说明的政策。

核价页按日期构造为：

```text
https://www.trip.com/flights/showfarefirst?dcity=sha&acity=tyo&ddate=2026-10-01&triptype=ow&class=y&lowpricesource=searchform
```

每个日期使用 1 次请求，所以 N 个日期估算为 N 次。首日先同步校验；成功后其余日期最多 4 路并发，请求开始时间仍遵守 `request_delay`。首日任何网络、格式或验证失败都会立即结束该来源；401、403、429、跳转验证和取消信号会停止已排队日期。

## Skyscanner

第一步访问官方新加坡站的精确日期页，例如：

```text
https://www.skyscanner.com.sg/transport/flights/sha/tyo/261001/?adultsv2=1&cabinclass=economy&rtn=0&currency=CNY
```

页面公开的 `window["__internal"]` 回显一成人、经济舱、单程、日期、CNY、未登录状态，并把三字代码解析到城市实体。上海 `SHA` 会解析为城市实体 `27546079` 和核价路径代码 `CSHA`，因此 Radar 搜索同时覆盖虹桥与浦东，不会因 `SHA` 与虹桥机场代码重名而只查一个机场。

第二步按 Skyscanner 当前官方 source map 的 Acorn 实现，向 `POST https://www.skyscanner.com.sg/g/radar/api/v2/web-unified-search/` 发送城市实体和日期，随后以响应的 `context.sessionId` 轮询同一路径。这个 sessionId 只是该次匿名搜索的短期游标，不是用户 token；程序不读取或回传服务端设置的 session Cookie，也不使用页面的 JHA、UTID 或登录身份。`X-Skyscanner-ViewId` 与 `X-Skyscanner-TrustedFunnelId` 均使用页面公开的同一个 viewId，符合官方 `defaultHeaders` 实现。

2026-09-14 对 `SHA` 至 `TYO`、2026-10-01 的端到端实测使用 4 次请求（1 个日期页、1 次 POST、2 次 GET），Radar 最终为 `context.status=complete`。经城市实体、日期、航段、当前购票选项和 deeplink 核对后的最低价为 CNY 1,917.16，航班 BR705/BR108，经停 1 次。真实购票链接位于官方 `www.skyscanner.com.sg/transport_deeplink/4.0/...` 域名路径。

只有 Radar 完成后才会解析价格。`pricingOption.price.updateStatus` 和唯一 item 都必须是 `current`；`pending` 即使出现在结果里也不参与比较。购票 option ID 必须等于航班行的最低 option ID，option、item、顶层金额和 deeplink 的 `ticket_price` 必须相等。会员属性、组合 item、未知链接类型、非 `PBOOK` 选项以及路线或日期不符的结果均排除。

Skyscanner 官方英文网页翻译资源包含 “Prices include taxes and charges.” 和 “Total cost …” 的航班卡说明；预订说明进一步称价格包含所有强制税费的估算，并要求在供应商网站确认最终价格。来源是官方静态资源：

```text
https://js.skyscnr.com/sttc/nx/web-platform/banana/static/js/translations/translation.release.6a28e852584c0d34a34c.en-gb.js
```

因此报价标为可比较总价，同时 `price_note` 明确保留行李、支付方式、供应商费用和最终成交价差异。

N 个日期的请求估算为 `1 + N` 至 `1 + 5N`：1 次城市上下文页；每个日期 1 次 POST，最多 4 次 GET 轮询。首日先完整结束，成功后剩余日期最多 4 路并发。若 4 次轮询后仍未完成、价格仍为 `pending`、页面触发验证或响应结构不再可核验，来源会明确失败且不返回价格。
