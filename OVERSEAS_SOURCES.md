# 海外来源与核验范围（2026-09-14）

网页现在把 13 家平台都作为独立自动来源：携程、同程、去哪儿、飞猪、Trip.com、Skyscanner、Google Flights、Kiwi.com、KAYAK、momondo、瑞安航空、春秋航空和 AirAsia。每家都请求自己的官网，不把某个平台的报价复制到另一个平台名下，也不把核价链接当作成功报价。

Trip.com 和 Skyscanner 已能匿名返回精确日期结果，并在路线、日期、1 成人经济舱单程、CNY、价格口径和购票入口全部通过校验后参与最低含税总价。春秋航空接入其含税最低价日历，但当前出口可能收到 429；AirAsia 接入精确日期自营直飞卡，但税费口径未说明，只显示原币及折算参考价。KAYAK 和 momondo 会各自真实打开精确日期搜索页，当前匿名 HTML 仅有需要浏览器会话继续轮询的动态外壳，因此快速报告失败而不生成报价。

这些查价路径不要求用户申请 API key、登录平台账号或提供 Cookie。平台返回的匿名搜索会话标识只用于完成当次同平台请求，不是用户凭证，也不会持久化。详细审计见 [Trip.com / Skyscanner](docs/trip-skyscanner-sources.md)、[KAYAK / momondo](KAYAK_MOMONDO_SOURCES.md)、[春秋 / AirAsia](docs/spring-airasia-sources.md)、[同程](TONGCHENG_SOURCE.md) 和 [飞猪](SOURCES_FLIGGY.md)。

## 瑞安航空

官方[低价搜索页](https://www.ryanair.com/gb/en/cheap-flights/london-to-dublin)加载的 [frontend 脚本](https://www.ryanair.com/etc/designs/ryanair/frontend/js/frontend-6fb8f31263.js)定义 `FareFinderApiConfig` 和 `cheapestPerDay`。实测公开接口：

- `https://www.ryanair.com/api/views/locate/5/airports/en/active`：224 个机场。按每个机场自身 `city.macCode` 识别多机场城市，或按精确机场码匹配。例如伦敦 LON 包含 LGW、LTN、STN；没有固定城市候选范围，也不取附近坐标或国家默认机场。
- `https://www.ryanair.com/api/farfnd/v4/oneWayFares/STN/DUB/cheapestPerDay?outboundMonthOfDate=2026-09-01`：按日返回出发日期、价格、原币种及无缓存/售罄标记，程序只取所选日期。
- `oneWayFares` 日期区间接口额外交叉核实 LGW→DUB 2026-09-29 为 GBP14.99，与月历相符。月历本身不回显机场，属于该来源的字段限制；机场对来自请求路径和已验证目录。

伦敦→都柏林 2026-09-29～30 实测 GBP14.99，按 2026-09-08 的 1 GBP=9.0898 CNY 折算 ¥136.26。这是当次参考价，不是当前可购承诺。

汇率使用[Frankfurter v1](https://frankfurter.dev/v1/)匿名日参考数据，校验基准币、金额1、CNY正有限汇率及日期不晚于查询日/不超过7天，缓存6小时。所有实际目录、月历、汇率请求均计入平台预算；多机场会查询完整机场组合，预算不足则明确报告，不悄悄缩成单机场。

瑞安月历没有单列税费；官方条款对票价与政府税的描述也不足以证明该日历就是用户最终应付总价。因此标为 `unknown`，不参与总价排名或微信阈值，原币和折算日期保留在报价中。其业务主要覆盖欧洲及周边，不能查询中国国内航班。

## 六个平台的当前自动化结果

| 平台 | 自动查询行为 | 最低价资格 |
| --- | --- | --- |
| Trip.com | 官方匿名 SSE 逐日搜索；排除学生、会员、新客、卡类等资格价 | 普通成人含税总价通过完整校验后参与 |
| Skyscanner | 从精确日期匿名页取得城市实体，再 POST 并轮询 Radar 至 `complete` | 价格与单人经济舱 CNY deeplink 一致且无受限属性时参与 |
| KAYAK | 请求自己的精确日期页并校验路线、日期、乘客、舱位及币种状态 | 当前只有 `NOT_STARTED` 动态壳，明确失败 |
| momondo | 与 KAYAK 完全分开请求及校验自己的域名 | 当前只有动态壳，明确失败，不复制 KAYAK |
| 春秋航空 | 先核对官网航线页，再逐日请求 `IsShowTaxprice=true` 日历 | 精确日期含税价参与；429 时整源失败 |
| AirAsia | 动态发现当前官网 action，查询前 6 条并筛 AirAsia 自营直飞 | 税费未知，只在平台卡展示，不参与排名/提醒 |

页面不再显示单独的“更多平台核价入口”。六家都和原有来源一样显示为平台选择项及状态卡；状态详情默认收起。KAYAK、momondo 或访问保护失败时仍保留带本次航线和日期的官网核价链接。

## 仍未接入的站点

Wego 在本次普通访问中返回 Cloudflare 限制页；东航官网返回访问保护内容；南航公开趋势接口仍缺少足以绑定城市机场和最终税费口径的字段。这些站点没有被计入平台数量。程序没有尝试绕过登录、验证码或访问限制，也没有安装付费代理或验证码服务。后续接口变化时会继续明确显示错误，不用模拟价或旧价补成成功。
