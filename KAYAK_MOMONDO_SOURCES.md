# KAYAK 与 momondo 匿名查询边界

2026-09-14 分别核验了 KAYAK 和 momondo 官网。两个 provider 会访问各自的精确日期搜索页，不共用或复制报价：

- KAYAK：`https://www.kayak.com/flights/SHA-TYO/2026-10-01?sort=price_a&currency=CNY`
- momondo：`https://www.momondo.com/flight-search/SHA-TYO/2026-10-01?sort=price_a&currency=CNY`

上述页面都可以在不登录、没有用户 Token、没有请求 Cookie 的条件下返回 HTTP 200。程序不配置 Cookie jar，因此即使响应带有 `Set-Cookie`，也不会在后续请求中保存或发送。

## 官网当前实际返回

两站的 HTML 都包含官方 `jsonData_R9DataStorage` 启动数据。实测数据可以确认以下搜索身份：

- `tripType=oneway`；
- 1 名成人，没有儿童、婴儿、青年、学生或老年乘客；
- 经济舱、精确日期、未启用附近机场；
- 出发地 `SHA`、目的地 `TYO`、日期 `2026-10-01`；
- 请求币种 `CNY`，按 `price_a` 从低到高排序。

但两个响应的 `searchState.status` 均为 `NOT_STARTED`，`FlightResultItem` 为空，没有票价。运行官网 JavaScript 的临时无登录 Firefox 会话仍停留在结果骨架屏，没有生成可读取的航班卡片。

[KAYAK 官网当前前端模块](https://content.r9cdn.net/frontier/assets/BsE8KPaiHz.js)声明实时结果路径为 `/i/api/search/dynamic/flights/poll`，旧路径为 `/horizon/flights/results/FlightSearchPollAction`。对两个域名分别发出的无 Cookie、无 Token 空请求均返回 HTTP 401 和 `INVALID_SESSION`。前端代码还会读取页面生成的 form token 和 session id。因此实时结果不是一个可独立调用的无会话公开接口。

## 程序行为

`KayakProvider` 和 `MomondoProvider` 是可选的真实官网探测源。每个源都：

1. 生成带精确路线、日期、CNY 和价格排序的官方 HTTPS 地址；
2. 独立请求自己的域名；
3. 校验最终 URL、HTML MIME、品牌、页面组件、路线、日期、单程、1 成人、经济舱、附近机场状态、币种请求和排序；
4. 只解析唯一的 JSON 数据脚本，不执行远程 JavaScript；
5. 遇到动态搜索壳、验证码、401/403/429、错误路线、结构变化、重复参数或无法确认的金额口径时明确失败。

当前匿名 HTML 没有可以验证的总价，所以 provider 不会从骨架占位、SEO 历史优惠、页面翻译文本或另一平台报价拼出价格。KAYAK 和 momondo 的路线 SEO 页展示过去用户找到的稀疏缓存优惠，日期不一定是用户指定日期，也不是该日完整搜索，不能纳入最低价比较。

选中多天时，预计每个可正常返回结果的日期需要一次官网请求。按当前官网行为，第一个日期确认是动态壳或验证页后会立即停止，不会对 31 个日期重复发出相同的失败请求。以后如果官网直接在匿名 HTML 中返回完成结果，仍必须先核实币种、含税总价和官方购票链接的数据结构，才会允许报价进入比较。

## 一手页面

- [KAYAK 机票搜索](https://www.kayak.com/flights)
- [KAYAK 搜索与发现帮助](https://www.kayak.com/c/help/search/)
- [momondo 机票搜索](https://www.momondo.com/flight-search/SHA-TYO/2026-10-01?sort=price_a&currency=CNY)
- [KAYAK 开发者文档](https://developers.kayak.com/)

