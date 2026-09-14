# 去哪儿机场查询

2026-09-14 检查。选择某个机场时已改为请求逐航班列表；两端均选“全部机场”仍使用原来的快速城市日历。

## 国际与港澳台

官网当前 [国际单程页面脚本](https://q.qunarzz.com/flight_qzz/prd/inter_oneway@6db14243cbf78cdedebe.js) 的 `SEARCH` 指向 `https://flight.qunar.com/touch/api/inter/wwwsearch`。筛选器把 `depAirport`、`arrAirport` 原样传入列表请求。程序使用该接口，请求所属城市、确切出发日期、1 成人、0 儿童，仅在用户指定的一端加入机场筛选。

响应必须确认成功、回显对应的出发与到达城市、轮询标识一致且 `ctrlInfo.completed=true`，然后逐条检查 `journeyType=ONEWAY`、单个 trip、首段起飞日期、首段出发机场和末段到达机场。城市日历和页面上的邻近推荐均不参与机场结果。

只在 `currencyCode=CNY` 且 `totalTaxType` 为 `GeneralTax=1` 或 `ContainTax=2` 时，将 `lowTotalPrice` 作为含税参考总价；官网枚举 `NoTax=0`、`ConsultTax=3` 及未知类型仅作参考。不对过期、错误航线、登录要求或验证响应生成报价。

每日期最多轮询 4 次，增量价格按航班身份更新，等待完成后才发布。首次响应失败就停止剩余日期，避免同一验证或错误缓存造成几十次无用请求。预算上限为 2 次所属城市识别 + 4 × 日期数；成功城市识别会缓存在 provider 中。

## 国内

官网 [国内单程页面脚本](https://q.qunarzz.com/flight_qzz/prd/domestic_oneway_new@1e3350c8ebf823f5308f.js) 使用 `POST https://flight.qunar.com/touch/api/domestic/wbdflightlist`，参数是 `departureCity`、`arrivalCity`、`departureDate`。程序读取其 `flights`，仅接受已确定航班的 `list` 和 `listMore`，核验首末段所属城市、日期和实际机场。

国内列表中的机场名称通过官网 [地点建议接口](https://m.flight.qunar.com/touch/api/suggest?queryWord=SHA) 的同城市包含机场进行唯一映射；不通过“上海”推断浦东，也不使用邻近城市机场。`minPrice` 没有明确含税证明，所以国内仍是票面参考价，不用于总价比较或微信阈值提醒。预算为 2 次城市识别 + 每日期 1 次列表请求。

## 实际验证结果与限制

本次匿名实测 `PVG → CJU`、2026-09-21 国际请求获得 HTTP 200，但官网接口回显“北京 → 名古屋”、`completed=false`，并无可用报价；程序将其报告为错航线或过期缓存。无缓存请求参数未改变该结果。

国内 `PVG → PEK` 同日返回 `ret=true`、`code=-1`、空 `flights`、空 `geographyInfo`，没有可验证的路线或日期，程序报告查询失败，不显示为“没有航班”。两种实测均使用 3 次请求即停止后续日期。

因此本次交付是机场查询请求、逐航班验证与错误处理接入，**不等于当前网络已成功获得去哪儿机场报价**。新增测试根据官网公开字段建立契约测试，并覆盖上述真实失败形态；测试中的示例票价不是实测票价。

购票入口包含原路线、确切日期、成人数及官网的 `filterFlightCode` 参数。官网可能重新搜索或调整展示，出票前仍须核对实际机场、航班与最终售价。
