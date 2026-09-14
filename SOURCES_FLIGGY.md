# 飞猪公开国内航班与国际低价日历来源

## 国际低价日历

2026-09-14 读取并实测飞猪国际机票页及其官方静态代码：

- [飞猪国际机票搜索页](https://sijipiao.fliggy.com/ie/flight_search_result.htm?tripType=0&depCity=SHA&arrCity=TYO&depDate=2026-10-01)
- [国际搜索配置](https://g.alicdn.com/trip/iflight-search/1.10.94/global/conf/index-min.js)
- [单程低价日历逻辑](https://g.alicdn.com/trip/iflight-search/1.10.94/mods/week-price/oneway-min.js)

国际页通过 `r.fliggy.com/cheapestCalendar/pc` 读取低价日历。匿名 HTTPS 请求使用 `bizType=1`、`tripType=0`、`calendarType=1` 和自然月第一天，一次返回该月各日期，实测无需 Cookie、账号、API key 或动态 token。程序按自然月批量查询，因此正常情况下，网页允许的连续 31 天范围最多发出两次飞猪国际请求。

若官网月历明确返回失败状态，程序会自动改用同一官方接口的 `calendarType=0` 七日低价日历，并以互不重叠的七日窗口覆盖所选日期。这个备用路径仍共用限速、请求上限、取消控制及下面全部数据校验；已经核实的窗口会保留，其余窗口会明确报告失败。网络错误、响应结构变化、错误航线或日期、异常购票链接和金额问题不会触发备用路径，避免把完整性错误误当成临时月历故障。月历和七日历均被官网拒绝时，页面会如实显示本次飞猪查询失败。

官网模板在票面价模式显示 `price`，切换总价时显示 `price + tax`。程序逐行要求 `depCityCode`、`arrCityCode`、`leaveDate` 与用户选择一致，并再次校验返回链接必须是飞猪国际 HTTPS 页面且携带同一航线和日期；只有有限非负的 `price`、`tax` 相加后为正数才进入含税总价比较。重复日期、跨月数据、错误路线、异常链接、验证页和失败状态都会被拒绝，0 元不会被当成免费机票。

月历不提供具体航班号、会员限制、行李或余票，属于缓存最低价参考，最终可售条件需打开对应日期的飞猪页面确认。完整国际航班列表接口在匿名服务器请求中会返回滑块验证；程序不执行远程脚本、不处理验证码，也不把受保护列表中的不完整内容拼成报价。

## 国内航班

2026-09-09 核实。`FliggyProvider` 无需用户申请 API key、token，也不需要登录或读取浏览器 cookies；每个出发日期一次普通匿名 GET。接口是网站自身使用的未公开保证稳定性的后台，验证页或字段变化会明确报错。

## 一手依据

- [飞猪官网](https://www.fliggy.com/) 加载的 [首页脚本](https://g.alicdn.com/trip/rc-pc-home/1.1.26/index.js) 生成 `https://sjipiao.fliggy.com/flight_search_result.htm` 查询链接，参数为 `depCity`、`arrCity`、`depDate`、`tripType=0`，页面正常跳转到 `/homeow/trip_flight_search.htm`。
- [查询页地址配置](https://g.alicdn.com/trip/flight-searchow/0.4.77/global/config-min.js) 和 [航班加载模块](https://g.alicdn.com/trip/flight-searchow/0.4.77/mods/flight-listing/loader-min.js) 使用 `/searchow/search.htm` 返回航班列表。
- [官方航班模型](https://g.alicdn.com/trip/flight-searchow/0.4.77/mods/flight-listing/flightItem-min.js) 明确计算 `tax = oilPrice + buildPrice`。实际返回中可能把合并税费放在 `buildPrice`，因此界面只写“税费合计”，不强行把各字段解释为单项机建或燃油。
- [官方舱位模型](https://g.alicdn.com/trip/flight-searchow/0.4.77/global/cabinManager-min.js) 将 `cabinClass=2` 映射为经济舱，并区分 `price`、`bestPrice`、特殊产品与补贴。程序采用 `cabin.price + oilPrice + buildPrice`，排除有预订限制通知、会员标记、申请票价、特殊产品及非经济舱报价。

只按严格 JSON 解析 `flightwatch(...)` 回调数据，不执行网页 JavaScript。已验证的请求允许 `ua`、`sKey`、`qid` 为空，没有生成防护指纹或绕过验证码。

## 实测结果

下列价格为核验当时的网页参考总价，随库存变化；不是当前仍可购买的保证。

| 出发日期 | 城市 | 返回范围内可确认的普通成人总价 |
| --- | --- | --- |
| 2026-09-23 | 北京 BJS → 上海 SHA | MU5231，410 元票价 + 120 元税费 = 530 元 |
| 2026-09-24 | 北京 BJS → 上海 SHA | MF8561，490 + 120 = 610 元 |
| 2026-09-23 | 上海 SHA → 广州 CAN | AQ1006，299 + 120 = 419 元 |
| 2026-09-23 | 上海 SHA → 喀什 KHG | CZ6830，2020 + 120 = 2140 元 |

北京到上海 9 月 23 日返回的较低 387 元票面报价有“限 16–25 周岁”限制，已排除。不会用这种条件价触发普通成人低价提醒。

国内接入成人单程经济舱；中转组合的税费口径尚未完整核实，暂不纳入。往返、其他舱位和直飞筛选明确返回不支持。通过返回的城市代码及每条航班日期检查查询身份。票价不是全平台穷尽搜索结果，行李、适用条件和最终可售总价仍以购票页为准。国际航线使用上面的月度低价日历，不会套用国内航班响应。
