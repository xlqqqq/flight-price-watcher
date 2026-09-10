# Kiwi.com 公开优惠来源

2026-09-09 核验。`flightwatch.kiwi_source.KiwiDealsProvider` 使用 Kiwi.com 官方中文城市航线页中的真实、带日期优惠和公开城市目录，无须账户、API key、token、Cookie 或额外依赖。它读取有限日期的公开优惠缓存，不能查询任意日期的完整实时库存。

## 数据与城市身份

- [上海到东京官方优惠页](https://www.kiwi.com/cn/cheap-flights/shanghai-china/tokyo-japan/) 的 `script[type="application/ld+json"][data-test="FlightCollectionSchema"]` 提供 `CollectionPage.mainEntity.itemListElement[].item`。每条 `Offer` 含 `price`、`priceCurrency`、`validFrom`、单程日期链接以及起降机场、出发时间。
- [Kiwi 公开上海城市目录](https://api.skypicker.com/locations?term=SHA&location_types=city&locale=zh-CN&limit=100) 给出精确 `code=SHA`、`id=shanghai_cn`、英文 `slug_en=shanghai-china`、中文 `slug=上海市-中国`。
- [上海所属机场目录](https://api.skypicker.com/locations?type=subentity&term=shanghai_cn&location_types=airport&locale=zh-CN&limit=100) 返回 PVG、SHA，各机场的 `city.id` 和 `city.code` 都必须与所选城市匹配。`alternative_departure_points` 包含其他城市，程序完全不使用该字段。
- [布拉格目录](https://api.skypicker.com/locations?term=PRG&location_types=city&locale=zh-CN&limit=100) 返回 `code=PRG`、`id=prague_cz`、`slug_en=prague-czechia`、国家 CZ。只接受精确代码的唯一城市，不选择第一条模糊建议，不把冷门城市换成热门城市。

代码使用 `/cn/` 页面取得 CNY，逐条核验 `priceCurrency`，不做汇率换算。`/en/` 页即使添加 `?currency=CNY` 或 `?currency=cny`，本次验证仍返回 GBP，因此不依赖该参数。

## 真实请求结果

以下是 2026-09-09 匿名请求的参考价，并非未来的可售保证。

| 页面 | 出发日期 | 实际起降机场 | CNY 参考价 |
| --- | --- | --- | ---: |
| [上海到东京](https://www.kiwi.com/cn/cheap-flights/shanghai-china/tokyo-japan/) | 2026-10-07 | PVG → HND | 967 |
| [上海到东京](https://www.kiwi.com/cn/cheap-flights/shanghai-china/tokyo-japan/) | 2026-10-09 | PVG → HND | 960 |
| [伦敦到巴黎](https://www.kiwi.com/cn/cheap-flights/london-united-kingdom/paris-france/) | 2026-09-28 | SEN → CDG | 335 |
| [上海到布拉格](https://www.kiwi.com/cn/cheap-flights/shanghai-china/prague-czechia/) | 2026-09-16 | PVG → PRG | 2817 |

以上三条航线均已通过完整 provider 网络调用验证。布拉格页有两个完全相同的 `FlightCollectionSchema`，程序去重后读取；如果两个集合不同则报错。

## 价格口径与限制

**`price_basis="unknown"`，仅供参考，不参与最低总价或阈值提醒。** 这是有真实日期和机场的海外报价来源，但公开优惠页没有单独确认成人数、舱位以及税费口径。

[Kiwi 当前官方条款 6.2](https://www.kiwi.com/en/pages/content/legal/) 的原文为：“The Booking Price also includes all mandatory taxes applicable to the transaction.” 该段上下文讨论预订过程中的费用明细与 Booking Price，不能单凭它证明优惠页的所有 Offer 等于预订结算总价。

优惠页 HTML 内还包含全站通用翻译键 `booking.pricebox.total_description`，文案说明结算价格包含税费、附加费、Kiwi 服务费。但这只是预载翻译字典，不是本次优惠价格框的直接说明。正常匿名浏览器打开单条优惠的搜索详情页得到 HTTP 403，因此没有进一步验证结算口径，程序也不绕过防护。

另一个实际发现是：上海到布拉格 ¥2817 的可见优惠卡标为“2 次中转”，其 JSON-LD `description` 却写“直达航班（直飞）”。因此程序不使用该字段，保持 `stops=None`，并明确拒绝 `nonstop=True`。不以不可靠的字段冒充直飞筛选。

程序同时拒绝往返和指定非默认舱位；即便公开页混有往返价格，也只使用购票链接中明确 `no-return` 的优惠。日期必须同时匹配所选日期、链接日期和 `departureTime`。页面与优惠更新日期超过 2 天或异常则不使用；该时限只能排除明显陈旧缓存，不能保证实时库存。

## 请求与失败行为

构造参数为 `timeout=30, request_delay=1, max_requests=60, *, cancelled=None`。`search(route, today)` 返回标准 `SearchResult`，`Quote.provider="kiwi"`。

一条新航线最多使用 5 次请求：出发城市、出发城市机场、到达城市、到达城市机场、优惠页。城市及所属机场在 provider 实例内缓存 24 小时，最多 512 个城市。重复查询同一航线只重新请求 1 次优惠页。所有请求共享 `requests_used` 预算，请求前、等待间隔后检查取消回调。

缺少所选日期时返回明确的“暂无公开优惠”说明，不使用前后日期替代。币种、城市、机场归属或日期冲突的优惠被排除并提示；验证页、HTTP 错误、无效数据和冲突的页面身份报错，不重试绕过，不当作空结果成功。

本轮也调查了 [Wego 上海到东京](https://www.wego.com/flights/sha/tyo/cheapest-flights-from-shanghai-to-tokyo)：普通浏览器直接出现 Cloudflare 封禁页面，因此没有添加虚假的 Wego 来源。Trip.com 海外站首次响应为防护脚本，也没有据此假装接入。

## 验证

`python -m unittest tests.test_kiwi_source`：19 项通过，覆盖真实数据结构、日期与往返隔离、附近机场排除、冷门城市识别、币种不换算、缓存预算、取消、403 不重试、缓存过旧、异常价格和重复页面 schema。
