# 飞猪公开国内航班来源

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

当前仅接入国内成人单程经济舱；中转组合的税费口径尚未完整核实，暂不纳入。国际、往返、其他舱位和直飞筛选明确返回不支持。通过返回的城市代码及每条航班日期检查查询身份。票价不是全平台穷尽搜索结果，行李、适用条件和最终可售总价仍以购票页为准。
