# 飞猪国际机场明细查询

2026-09-14 接入官方国际航班明细路径。任意一端选择具体机场时，按其所属城市查询实际航班列表，再按第一航段起飞机场和最后航段到达机场筛选；两端均为全部机场时继续使用原有月度低价日历。

## 公开来源和字段依据

- [国际搜索页及内嵌模板](https://sijipiao.fliggy.com/ie/flight_search_result.htm)：`J_FlightItemsTmpl` 使用 `flightInfo[].flightSegments[]`；`J_FlightDetailTmpl` 展示 `depAirportCode`、`arrAirportCode`、`depTimeStr` 和 `marketingFlightNo`。含税展示使用 `totalAdultPrice`，票价与税费分别是 `adultPrice`、`adultTax`。解析要求三者相加一致。
- [官方查询 URL 配置](https://g.alicdn.com/trip/iflight-search/1.10.94/global/conf/index-min.js)：`DEP` 指向 `https://sijipiao.fliggy.com/ie/flight_search_result_poller.do`，使用单程 `searchJourney`。
- [官方舱位选择](https://g.alicdn.com/trip/iflight-search/1.10.94/mods/filter/index-min.js)：`searchCabinType=1` 是经济舱，`0` 是所有舱位。实现明确请求经济舱、零儿童、零婴儿，关闭会员价请求。
- [官方轮询器](https://g.alicdn.com/trip/iflight-search/1.10.94/widgets/loader/index-min.js)：以 `status=200`、`data.isContinue=false` 确认完成；后续请求只使用当前匿名响应返回的 `iesToken` 和 `queryRecordId`。它们是短期查询续页信息，不需要用户提供或保存任何账号/API Token。
- [官方金额格式化器](https://g.alicdn.com/trip/iflight-search/1.10.94/global/util/juicer-fn-min.js)：国际明细的 `price` 展示过滤器除以 100。`adultPrice`、`adultTax` 和 `totalAdultPrice` 的单位均为整数分，解析后转换成元；月历和国内接口的金额处理不变。普通浏览器实测初始数据 `18600 + 18000 = 36600` 对应页面 `¥366`，相关回归测试防止发生 100 倍金额错误。

查询最多每日期 4 次请求，只发布完成后的快照，遇验证码立即停止该平台后续日期。不执行验证脚本、不处理滑块、不伪造设备信息、不使用登录 Cookie。第一日任何失败也停止剩余日期，避免 15 个日期重复等待同一个失败。

每次比价前核对日期、城市及实际机场；不把中转机场误当最终目的地。缺少机场、负数/缺失税费、币种不符、票价加税费与总价不一致、含促销条件/价格说明/会员标记、特殊拼接产品或余票不明的报价不进入比较。平台规则仍需购票页最终确认。

单航段报价链接使用公开搜索页支持的 `pcOtaMode=1`、`pcLeaveFlightNo`，让官网选择返回的航班并展开商家；连接航班使用带对应日期的结果页。链接不创建订单，不保证预订时价格不变。

## 本次实际验证的结果与边界

匿名请求 `SHA → CJU`（用于筛选 `PVG → CJU`）、`2026-09-21` 的官方国际明细端点返回了安全验证 HTML，未取得真实可比较明细。程序现已发出明细查询并显示该真实原因，不能宣称飞猪国际在当前服务器已取价成功。

另外验证 `depCityCode=PVG` 的国际低价日历：接口原样回显机场代码，但七天价格全部为零且没有实际机场字段。这不能证明接口执行机场筛选，因此没有采用这种方式，也不会用上海全机场价格冒充浦东价格。国内 `searchow/search.htm` 用国际目的地返回未找到航班的错误，亦未作为国际机场报价。

新增测试使用官方模板字段构造，明确不是实测报价。覆盖混合范围、双机场、较便宜的其他机场排除、中转目的地判定、字段/日期/金额验证、完整轮询和验证码仅请求一次、日历不回退以及城市查询性能路径。

## 后续普通匿名浏览器验证

2026-09-14 通过全新普通 Chromium 会话直接打开同一官网行程，曾取得一份真实的初始航班列表，包含 `PVG → CJU` 的航段、日期和分单位成人含税金额。但该快照 `isContinue=true`，因此不把它发布为查询完成的最低价。进一步完整查询时官网返回安全验证，不能把初始有价等同于持续查询稳定可用。

补齐官网 `agentId=-1`、正常城市名和缓存时间戳的普通 HTTP 请求仍返回验证页，说明当前情况不是仅少一个查询参数。可选的 `flightwatch/fliggy_browser.py` 让官网在隔离的无账号浏览器会话中自然执行查询，读取符合所选城市/日期、经济舱、零儿童/婴儿及关闭会员价格条件的响应，并严格等到 `isContinue=false` 后才进入已有机场筛选器。

浏览器模式最多同时运行 2 个会话，首日包含启动等待的查询时限 25 秒，每日期最多 4 次明细请求；官网要求安全验证就关闭页面，不进行验证码操作或换身份重试。响应时间戳必须在 15 分钟以内。实际适配器验收用时 17.32 秒，1 次请求后遇到安全验证退出，未发布中间价格。

该模式默认关闭，需要可选依赖及环境配置：

```bash
.venv/bin/python -m pip install playwright
PLAYWRIGHT_BROWSERS_PATH="$PWD/.runtime/browsers" .venv/bin/python -m playwright install chromium
```

运行服务时设置 `FLIGHTWATCH_FLIGGY_BROWSER=1` 和同一个绝对路径的 `PLAYWRIGHT_BROWSERS_PATH`。不使用用户浏览器配置、登录 Cookie 或持久化个人会话。未安装或未启用浏览器的环境继续使用普通 HTTP 查询；启用后遇验证不会再换 HTTP 重试。
