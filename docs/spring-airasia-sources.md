# 春秋航空与 AirAsia 官网来源

核验日期：2026-09-14。两个来源只发起官网匿名 HTTPS 请求，不读取或保存用户 token、账号、登录态或 Cookie。官网响应可能下发临时 WAF/Cloudflare Cookie，但 Python 请求器没有 CookieJar，不会保存或回传它们。

## 春秋航空

官方入口是 `https://flights.ch.com/{出发代码}-{到达代码}.html`。来源先打开带完整条件的页面，例如：

```text
https://flights.ch.com/SHA-TYO.html?Departure=SHA&Arrival=TYO&FDate=2026-10-15&ANum=1&CNum=0&INum=0&IfRet=false&SType=0&MType=0&IsNew=1
```

页面必须逐项、唯一回显出发/到达三字码、所选日期、人民币、1 成人、0 儿童、0 婴儿、单程、普通旅客及国内/国际市场。城市名称取自这个已核验页面，不使用固定城市表或猜测附近机场。随后逐日 POST 官方 `/Flights/MinPriceTrends`，请求内再次携带该页面给出的出发/到达名称、精确日期、`Currency=0`、`IfRet=false`、`Days=0` 和 `IsShowTaxprice=true`。

含税口径的一手依据是官网当前脚本：

```text
https://ajax.springairlines.com/cache/js/modules/site5/main-search-new.js?vs=v2026091002
```

该脚本把 `IsShowTaxprice` 原样传给 `MinPriceTrends`，并在没有直飞结果时把它切换成“不含税”状态；开启时日历直接显示响应 `PriceTrends[].Price`。因此只有明确用 `IsShowTaxprice=true` 请求、响应 `Code=0` 且日期唯一精确匹配时，才建立 `price_basis=total` 的含税参考报价。

接口响应没有再次回显航线、乘客数、具体航班号或经停数。航线绑定来自同一 HTTPS 请求的 `Departure`/`Arrival` 参数以及请求前的官网页面回显；代码不会声称某个航班号或经停数，`stops=None`。若需要响应自身再次回显航线才能满足审计要求，应关闭此来源。

当前出口实测航线页可匿名访问，但 `MinPriceTrends` 返回 HTTP 429。代码会把 401、403、429 或意外跳转视为整轮访问保护并立即停止，不会逐日重试验证页。首日 MIME/JSON/协议失败也会停止后续日期。该限制意味着平台保护期间会明确报错，不会拿页面模板中的营销/占位价格充当机票价。

冷启动请求量是 `1 + 日期数`：一次航线页面验证，随后每个日期一次精确日历请求。官方核价链接由 `SpringAirlinesProvider.official_url(route, day)` 生成，和实际查询使用相同的航线、日期与乘客条件。

## AirAsia

AirAsia 官方航线页通过 Next.js server action 加载指定日期航班卡。action ID 是当前公开前端构建的路由标识，不是账号 token；代码每个 provider 实例从官方页面及其同源 JavaScript 动态发现它，不把临时构建 ID 固定在代码中。动作 POST 到稳定入口：

```text
https://www.airasia.com/flights/
```

请求包含 `origin`、`destination`、ISO 日期、`currency=MYR`、`sortBy=cheapest` 和 `limit=6`。响应必须再次准确回显航线、日期、币种、排序与条数；每条还要通过出发日期、价格、币种、承运人名称/代码、航班号和经停字段验证。只有承运人名称包含 AirAsia、航班号前缀等于承运人代码且 `stops=0` 的卡片会成为报价。这样由实时响应动态验证机场/城市覆盖，不维护固定航线表，也不会把 AirAsia MOVE 上的其他航空公司当作 AirAsia 自营。

2026-09-14 匿名实测 `KUL→DMK`、2026-10-15 返回：

```text
AK896  MYR 376
AK888  MYR 376
AK892  MYR 387
AK890  MYR 429
AK884  MYR 451
AK886  MYR 451
```

响应 meta 回显 `origin=KUL`、`destination=DMK`、`departureDate=2026-10-15`、`currency=MYR`、`sortBy=cheapest`、`limit=6`，最低字段也等于 MYR 376。接口只给按价格排序的前 6 条，代码再从这 6 条中筛 AirAsia；没有命中只能解释为“前 6 条没有可验证的 AirAsia 直飞”，不能解释为没有航班。

这次响应的 `timestamp=2026-09-08T19:59:35.144Z`、`cacheSource=gcs`。官网脚本会在非 live 数据超过 10 分钟时用 `forceRefresh=true` 再查；实测强制刷新仍返回同一过期时间。实现复现官网刷新逻辑，刷新后仍旧过期便拒绝该价格。因此上面的 MYR 376 是协议/字段实测证据，不会在当前过期状态下作为用户报价输出。

该 action 没有说明票价是否包含政府税费、机场费或支付费。任何通过新鲜度检查的报价也会标为 `price_basis=unknown`，只展示 MYR 原价和按有日期的 Frankfurter 参考汇率折算的 CNY，不参与最低含税总价或阈值提醒。Frankfurter 请求另行严格限制为 `https://api.frankfurter.dev/v1/latest`，不会经过 AirAsia 域名校验器。

官方核价/购买链接使用 AirAsia 自己的搜索页，并写入 `DD/MM/YYYY` 日期、1 成人单程、经济舱及 `isAirasiaFlightOnly=true`：

```text
https://www.airasia.com/v2/flights/search/?origin=KUL&destination=DMK&departDate=15%2F10%2F2026&tripType=O&adult=1&child=0&infant=0&locale=en-gb&currency=MYR&cabinClass=economy&isAirasiaFlightOnly=true
```

当前冷启动实测需要 1 次引导页和 4 次脚本请求来发现 action；每个日期 1 次动作，过期时再加 1 次强制刷新，有可用外币报价时再加 1 次汇率请求。因此当前保守估算为 `6 + 2 × 日期数`，实例复用且数据新鲜时通常接近 `日期数 + 1`。脚本结构改变时发现过程可能多取同一页面列出的候选 chunk，但仍受 `max_requests` 总预算约束。401、403、429、跳转、首日 MIME/动作结构错误会立即停止整个日期循环。

AirAsia 对本项目标为 `domestic` 的中国国内路线会在联网前返回不支持；国际/海外路线则由实际 action 响应动态决定是否有自营直飞。

## 验证

专项测试覆盖正确报价、错误航线/日期/币种、非 AirAsia 承运、含中转卡片、无穷金额、重复 JSON 键、action 动态发现、过期缓存强制刷新、首日协议失败停止、过滤条件、请求预算、官网核价参数和汇率折算：

```bash
python3 -m unittest -v tests.test_airasia_source tests.test_spring_source
```
