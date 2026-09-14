# 飞猪国际机场明细查询

2026-09-14 接入官方国际航班明细路径。任意一端选择具体机场时，按其所属城市查询实际航班列表，再按第一航段起飞机场和最后航段到达机场筛选；两端均为全部机场时继续使用原有月度低价日历。

## 公开来源和字段依据

- [国际搜索页及内嵌模板](https://sijipiao.fliggy.com/ie/flight_search_result.htm)：`J_FlightItemsTmpl` 使用 `flightInfo[].flightSegments[]`；`J_FlightDetailTmpl` 展示 `depAirportCode`、`arrAirportCode`、`depTimeStr` 和 `marketingFlightNo`。含税展示使用 `totalAdultPrice`，票价与税费分别是 `adultPrice`、`adultTax`。解析要求三者相加一致。
- [官方查询 URL 配置](https://g.alicdn.com/trip/iflight-search/1.10.94/global/conf/index-min.js)：`DEP` 指向 `https://sijipiao.fliggy.com/ie/flight_search_result_poller.do`，使用单程 `searchJourney`。
- [官方舱位选择](https://g.alicdn.com/trip/iflight-search/1.10.94/mods/filter/index-min.js)：`searchCabinType=1` 是经济舱，`0` 是所有舱位。实现明确请求经济舱、零儿童、零婴儿，关闭会员价请求。
- [官方轮询器](https://g.alicdn.com/trip/iflight-search/1.10.94/widgets/loader/index-min.js)：以 `status=200`、`data.isContinue=false` 确认完成；后续请求只使用当前匿名响应返回的 `iesToken` 和 `queryRecordId`。它们是短期查询续页信息，不需要用户提供或保存任何账号/API Token。

查询最多每日期 4 次请求，只发布完成后的快照，遇验证码立即停止该平台后续日期。不执行验证脚本、不处理滑块、不伪造设备信息、不使用登录 Cookie。第一日任何失败也停止剩余日期，避免 15 个日期重复等待同一个失败。

每次比价前核对日期、城市及实际机场；不把中转机场误当最终目的地。缺少机场、负数/缺失税费、币种不符、票价加税费与总价不一致、含促销条件/价格说明/会员标记、特殊拼接产品或余票不明的报价不进入比较。平台规则仍需购票页最终确认。

单航段报价链接使用公开搜索页支持的 `pcOtaMode=1`、`pcLeaveFlightNo`，让官网选择返回的航班并展开商家；连接航班使用带对应日期的结果页。链接不创建订单，不保证预订时价格不变。

## 本次实际验证的结果与边界

匿名请求 `SHA → CJU`（用于筛选 `PVG → CJU`）、`2026-09-21` 的官方国际明细端点返回了安全验证 HTML，未取得真实可比较明细。程序现已发出明细查询并显示该真实原因，不能宣称飞猪国际在当前服务器已取价成功。

另外验证 `depCityCode=PVG` 的国际低价日历：接口原样回显机场代码，但七天价格全部为零且没有实际机场字段。这不能证明接口执行机场筛选，因此没有采用这种方式，也不会用上海全机场价格冒充浦东价格。国内 `searchow/search.htm` 用国际目的地返回未找到航班的错误，亦未作为国际机场报价。

新增测试使用官方模板字段构造，明确不是实测报价。覆盖混合范围、双机场、较便宜的其他机场排除、中转目的地判定、字段/日期/金额验证、完整轮询和验证码仅请求一次、日历不回退以及城市查询性能路径。
