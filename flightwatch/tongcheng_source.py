"""Anonymous Tongcheng domestic and international web fares.

Primary sources (read as text; no remote JavaScript is executed):
https://www.ly.com/flights/itinerary/oneway/BJS-SHA?date=2026-09-22
https://file.40017.cn/assets/flightpc/e9a826dca4d7610c2ac4.js
https://file.40017.cn/assets/flightpc/aea8b2dd28cdc40b6ea5.js
https://file.40017.cn/assets/flightpc/2b97560e26aa0cce5f04.js

The public page's Nuxt state.book1.flightLists contains adult fare ``lcp``.
The official flightPriceShow filter displays lcp for one-way travel. The
checkout asset maps flight.pt to adultPt (机建), flight.ot to adultOt (燃油),
and computes adultPrice + adultPt + adultOt; g5flag with nonzero g5pt overrides
pt. We use those per-flight returned amounts, never fixed estimated taxes.
The list asset labels g5flag=1 as 华夏联程; stopNum alone therefore does
not establish a nonstop journey, and nonstop requests are unsupported.

SSR returns an INITIAL subset (dataflag="some"), not an exhaustive search.
These are indicative adult one-way totals, excluding optional extras, and do
not certify current seat availability. The domestic URL is not an
international provider. Its structure is undocumented and can change.

The official international site was verified live on 2026-09-14:
https://www.ly.com/iflight/
https://www.ly.com/miflightapi/ts/calendar?D=SHA&A=TYO&DD=2026-10-01&AD=&TT=OW&ST=1
https://file.40017.cn/iflight/iflight/app.061a10b4a76a72d0efc4.js

Its anonymous low-price calendar accepts a city pair and returns about 90
dated rows in one request. The site's current code maps the lowest list ``sp``
to calendar ``P`` and the lowest list ``tp`` to calendar ``TP``; its default
tax-inclusive sorting uses ``tp``. We therefore use only a positive ``TP`` for
an exact requested date. The calendar is cached and does not echo the route,
so every quote records that limitation and links back to the exact HTTPS
search. No cookie, login, API credential, dynamic signature, browser execution
or CAPTCHA handling is used.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from .models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult


_MAX_HTML_BYTES = 5_000_000
_MAX_JSON_BYTES = 2_000_000
_MAX_STATE_CHARS = 2_000_000
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_SUBSET_WARNING = "同程仅比较公开网页初始返回的航班，未覆盖该平台全部航班；最终可售总价请到购票页确认"
_INTERNATIONAL_ENDPOINT = "https://www.ly.com/miflightapi/ts/calendar"
_INTERNATIONAL_WARNING = (
    "同程国际价格来自公开低价日历缓存，不是实时可售航班列表；"
    "响应不回显航线，程序仅按请求的官方 HTTPS 航线和精确日期取值，最终价格请到购票页确认"
)


@dataclass(frozen=True)
class _Reference:
    name: str


class _LiteralReader:
    """A deliberately small data grammar, not a JavaScript interpreter.

    Only objects, arrays, JSON strings, finite numbers, booleans, null,
    ``void 0`` and named serializer arguments are accepted. Calls, property
    access, operators, assignments and executable function bodies fail.
    """

    def __init__(self, text: str, position: int):
        self.text = text
        self.position = position
        self.nodes = 0
        self.decoder = json.JSONDecoder()

    def whitespace(self) -> None:
        while self.position < len(self.text) and self.text[self.position].isspace():
            self.position += 1

    def consume(self, token: str) -> None:
        self.whitespace()
        if not self.text.startswith(token, self.position):
            raise ValueError("网页数据使用了不支持的表达式")
        self.position += len(token)

    def peek(self) -> str:
        self.whitespace()
        return self.text[self.position:self.position + 1]

    def value(self, depth: int = 0, references: bool = True):
        self.nodes += 1
        if depth > 64 or self.nodes > 200_000:
            raise ValueError("网页数据超过解析复杂度限制")
        token = self.peek()
        if token == '"':
            value, self.position = self.decoder.raw_decode(self.text, self.position)
            return value
        if token == "{":
            self.consume("{")
            result = {}
            if self.peek() == "}":
                self.consume("}")
                return result
            while True:
                if self.peek() == '"':
                    key, self.position = self.decoder.raw_decode(self.text, self.position)
                else:
                    match = _IDENTIFIER.match(self.text, self.position)
                    if not match:
                        raise ValueError("网页对象字段格式变化")
                    key = match.group()
                    self.position = match.end()
                if key in result:
                    raise ValueError("网页数据含重复字段")
                self.consume(":")
                result[key] = self.value(depth + 1, references)
                if self.peek() == "}":
                    self.consume("}")
                    return result
                self.consume(",")
        if token == "[":
            self.consume("[")
            result = []
            if self.peek() == "]":
                self.consume("]")
                return result
            while True:
                result.append(self.value(depth + 1, references))
                if self.peek() == "]":
                    self.consume("]")
                    return result
                self.consume(",")
        match = _NUMBER.match(self.text, self.position)
        if match:
            self.position = match.end()
            value = Decimal(match.group())
            if not value.is_finite() or abs(value.adjusted()) > 100:
                raise ValueError("网页数字超出合理范围")
            return value
        match = _IDENTIFIER.match(self.text, self.position)
        if not match:
            raise ValueError("网页数据出现非字面量表达式")
        self.position = match.end()
        name = match.group()
        if name in {"true", "false", "null", "undefined"}:
            return {"true": True, "false": False, "null": None, "undefined": None}[name]
        if name == "void":
            self.consume("0")
            return None
        if not references:
            raise ValueError("网页参数出现非字面量引用")
        return _Reference(name)


class _ScriptCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.in_script = False
        self.parts: list[str] = []
        self.candidates: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
            self.parts = []

    def handle_data(self, data):
        if self.in_script:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_script:
            text = "".join(self.parts).strip()
            if text.startswith("window.__NUXT__"):
                self.candidates.append(text)
            self.parts = []
            self.in_script = False


def parse_nuxt_state(html: str) -> dict:
    """Extract one verified serializer wrapper, rejecting executable syntax."""
    try:
        collector = _ScriptCollector()
        collector.feed(html)
        if len(collector.candidates) != 1:
            raise ValueError("网页未提供唯一的航班数据，可能是验证页")
        text = collector.candidates[0]
        if len(text) > _MAX_STATE_CHARS:
            raise ValueError("网页航班数据过大")
        wrapper = re.match(
            r"window\.__NUXT__\s*=\s*\(function\(([^()]*)\)\s*\{\s*return\s+", text)
        if not wrapper:
            raise ValueError("网页航班数据包装格式变化")
        names = [name.strip() for name in wrapper.group(1).split(",") if name.strip()]
        if (len(names) != len(set(names)) or len(names) > 10_000
                or any(not _IDENTIFIER.fullmatch(name) for name in names)):
            raise ValueError("网页数据参数格式变化")
        reader = _LiteralReader(text, wrapper.end())
        value = reader.value()
        reader.consume("}")
        reader.consume("(")
        arguments = []
        if reader.peek() != ")":
            while True:
                arguments.append(reader.value(references=False))
                if reader.peek() == ")":
                    break
                reader.consume(",")
        reader.consume(")")
        reader.consume(")")
        if reader.peek() == ";":
            reader.consume(";")
        if reader.peek() or len(names) != len(arguments):
            raise ValueError("网页数据含额外代码或参数不匹配")
        bindings = dict(zip(names, arguments))

        def resolve(item):
            if isinstance(item, _Reference):
                if item.name not in bindings:
                    raise ValueError("网页数据出现未声明参数")
                return bindings[item.name]
            if isinstance(item, dict):
                return {key: resolve(child) for key, child in item.items()}
            if isinstance(item, list):
                return [resolve(child) for child in item]
            return item

        result = resolve(value)
        if not isinstance(result, dict):
            raise ValueError("网页航班数据根节点不是对象")
        return result
    except (ValueError, InvalidOperation, RecursionError) as exc:
        raise ProviderError(f"同程网页数据解析失败：{exc}") from None


def _amount(value, field: str) -> Decimal:
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, Decimal)):
        raise ProviderError(f"同程缺少有效 {field}，不能确认含税总价")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ProviderError(f"同程 {field} 不是金额") from None
    if not amount.is_finite() or amount < 0 or amount > 1_000_000:
        raise ProviderError(f"同程 {field} 金额无效")
    return amount


class TongchengProvider:
    def __init__(self, timeout: float = 30, request_delay: float = 1.0,
                 max_requests: int = 60, *, cancelled: Callable[[], bool] | None = None):
        self.timeout = timeout
        self.request_delay = request_delay
        self.max_requests = max_requests
        self.requests_used = 0
        self._last_request: float | None = None
        self._cancelled = cancelled

    @property
    def cancelled(self):
        return self._cancelled

    @cancelled.setter
    def cancelled(self, callback):
        self._cancelled = callback

    def _check_cancelled(self) -> None:
        if self._cancelled is not None and self._cancelled():
            raise ProviderError("同程查询已停止，剩余日期未查询")

    @staticmethod
    def _url(route: Route, day: date) -> str:
        return (f"https://www.ly.com/flights/itinerary/oneway/{route.origin}-{route.destination}?"
                + urllib.parse.urlencode({"date": day.isoformat()}))

    @staticmethod
    def _international_page_url(route: Route, day: date) -> str:
        params = {
            "advanced": "false",
            "departAirportCode": route.origin,
            "arriveAirportCode": route.destination,
            "para": (
                f"{route.origin}*{route.destination}*{day.isoformat()}**"
                "OW*1_0_0*Y|S|C|F"
            ),
        }
        return "https://www.ly.com/iflight/book1.html?" + urllib.parse.urlencode(params)

    @staticmethod
    def _international_calendar_url(route: Route, day: date) -> str:
        return _INTERNATIONAL_ENDPOINT + "?" + urllib.parse.urlencode({
            "D": route.origin,
            "A": route.destination,
            "DD": day.isoformat(),
            "AD": "",
            "TT": "OW",
            "ST": 1,
        })

    def _reserve_request(self) -> None:
        self._check_cancelled()
        if self.requests_used >= self.max_requests:
            raise ProviderError("同程查询已达到本轮请求上限")
        if self._last_request is not None:
            wait = self.request_delay - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        self._check_cancelled()
        self.requests_used += 1
        self._last_request = time.monotonic()

    def _request(self, url: str) -> str:
        self._reserve_request()
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Accept": "text/html",
            "Accept-Language": "zh-CN,zh;q=0.9", "Referer": "https://www.ly.com/flights/",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(_MAX_HTML_BYTES + 1)
            if len(body) > _MAX_HTML_BYTES:
                raise ProviderError("同程网页超过大小限制，停止解析")
            return body.decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"同程返回 HTTP {exc.code}，可能需要稍后重试") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise ProviderError(f"同程查询网络失败（{type(exc).__name__}）") from None
        except UnicodeDecodeError:
            raise ProviderError("同程网页编码发生变化") from None

    def _request_international_calendar(self, route: Route, day: date) -> dict:
        self._reserve_request()
        page_url = self._international_page_url(route, day)
        request = urllib.request.Request(
            self._international_calendar_url(route, day),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": page_url,
                # These are the international website's fixed channel headers,
                # not user credentials or configurable API tokens.
                "pc-token": "1",
                "t-token": "1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                location = urllib.parse.urlsplit(response.url)
                final_query = urllib.parse.parse_qs(
                    location.query, keep_blank_values=True,
                )
                expected_query = {
                    "D": route.origin,
                    "A": route.destination,
                    "DD": day.isoformat(),
                    "AD": "",
                    "TT": "OW",
                    "ST": "1",
                }
                try:
                    trusted_location = (
                        location.scheme == "https"
                        and location.hostname == "www.ly.com"
                        and location.port in (None, 443)
                        and location.username is None
                        and location.password is None
                        and location.path == "/miflightapi/ts/calendar"
                        and not location.fragment
                        and all(final_query.get(key) == [value]
                                for key, value in expected_query.items())
                    )
                except ValueError:
                    trusted_location = False
                content_type = response.headers.get("Content-Type", "")
                if not trusted_location:
                    raise ProviderError("同程国际日历跳转到非预期地址，未读取其价格")
                if content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise ProviderError("同程国际日历返回类型不是 JSON，可能是验证页")
                body = response.read(_MAX_JSON_BYTES + 1)
            if len(body) > _MAX_JSON_BYTES:
                raise ProviderError("同程国际日历响应超过大小限制，可能接口已变化")

            def reject_constant(_value):
                raise ValueError("非标准数字常量")

            data = json.loads(
                body.decode("utf-8"), parse_float=Decimal,
                parse_constant=reject_constant,
            )
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"同程国际日历返回 HTTP {exc.code}，可能需要稍后重试") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise ProviderError(f"同程国际日历查询网络失败（{type(exc).__name__}）") from None
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ProviderError("同程国际日历返回非 JSON 内容，可能是验证页或接口已变化") from None
        if not isinstance(data, dict):
            raise ProviderError("同程国际日历响应根节点不是对象")
        return data

    def search(self, route: Route, today: date) -> SearchResult:
        if route.origin_scope == "airport" or route.destination_scope == "airport":
            raise ProviderUnsupported(
                "同程当前公开数据只核实到城市，无法核实指定机场；本次未发起网络查询"
            )
        if route.currency != "CNY":
            raise ProviderUnsupported("同程公开网页只提供 CNY 价格")
        if route.stay_nights is not None:
            raise ProviderUnsupported("同程公开网页数据源目前仅支持单程")
        if route.travel_class != 1:
            raise ProviderUnsupported("同程公开网页不支持可靠的舱位筛选")
        if route.nonstop:
            raise ProviderUnsupported("同程公开网页不支持可靠的直飞筛选")
        if not all(re.fullmatch(r"[A-Z]{3}", code)
                   for code in (route.origin, route.destination)):
            raise ProviderError("同程查询须使用三字母城市代码")
        dates = route.departure_dates(today)
        if not dates:
            return SearchResult([], ["配置中没有尚未过期的出发日期"])
        if route.market == "international":
            data = self._request_international_calendar(route, min(dates))
            return self._parse_international(data, route, set(dates))
        if route.market != "domestic":
            raise ProviderUnsupported("同程 market 只能为 domestic 或 international")
        quotes, warnings, failures = [], [_SUBSET_WARNING], []
        succeeded = 0
        for day in dates:
            try:
                self._check_cancelled()
            except ProviderError as exc:
                failures.append(str(exc))
                break
            if self.requests_used >= self.max_requests:
                failures.append("同程查询已达到本轮请求上限，部分日期未查询")
                break
            try:
                result = self._parse(parse_nuxt_state(self._request(self._url(route, day))), route, day)
                succeeded += 1
                quotes.extend(result.quotes)
                warnings.extend(result.warnings)
            except ProviderError as exc:
                failures.append(f"{day.isoformat()}：{exc}")
                if not succeeded:
                    failures.append("首日查询未成功，暂停该平台剩余日期，避免重复等待；下轮可重试")
                    break
        if not succeeded:
            raise ProviderError("；".join(failures) or "同程未能取得有效网页数据")
        warnings.extend(failures)
        return SearchResult(quotes, list(dict.fromkeys(warnings)))

    def _parse_international(
            self, data: dict, route: Route, wanted: set[date]) -> SearchResult:
        payload = data.get("data")
        if (data.get("code") != 200 or not isinstance(payload, dict)
                or payload.get("R") != "0"):
            raise ProviderError("同程国际日历未确认查询成功，未将响应当作有效票价")
        rows = payload.get("RD")
        if not isinstance(rows, list):
            raise ProviderError("同程国际日历缺少 RD 日期列表")

        # LP/TP describe the whole returned calendar rather than the requested
        # date. Validate their shape, but never substitute them for a dated row.
        _amount(payload.get("LP"), "国际日历最低销售价 LP")
        _amount(payload.get("TP"), "国际日历最低总价 TP")

        quotes_by_day: dict[date, Quote] = {}
        seen: set[date] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ProviderError("同程国际日历日期行格式发生变化")
            raw_day = row.get("DD")
            if not isinstance(raw_day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_day):
                raise ProviderError("同程国际日历出发日期格式发生变化")
            try:
                day = date.fromisoformat(raw_day)
            except ValueError:
                raise ProviderError("同程国际日历包含无效出发日期") from None
            if day in seen:
                raise ProviderError("同程国际日历包含重复出发日期，无法唯一匹配报价")
            seen.add(day)
            if row.get("RD") != "":
                raise ProviderError("同程单程国际日历出现返程日期，停止解析以避免行程误报")
            sale_price = _amount(row.get("P"), "国际日历销售价 P")
            total_price = _amount(row.get("TP"), "国际日历总价 TP")
            if total_price < sale_price:
                raise ProviderError("同程国际日历总价低于销售价，价格口径可能已变化")
            if day not in wanted or total_price == 0:
                continue
            quotes_by_day[day] = Quote(
                origin=route.origin,
                destination=route.destination,
                departure_date=day,
                price=total_price,
                currency="CNY",
                source="同程国际低价日历",
                url=self._international_page_url(route, day),
                provider="tongcheng",
                price_basis="total",
                price_note=(
                    f"同程国际低价日历缓存参考：销售价 P={sale_price} 元，"
                    f"采用官网总价 TP={total_price} 元；1 成人单程、平台默认舱位。"
                    "日历响应不回显航线且不代表实时可售，税费明细、行李和最终价格请到购票页确认"
                ),
            )

        missing = wanted - quotes_by_day.keys()
        warnings = [_INTERNATIONAL_WARNING]
        if missing:
            warnings.append(
                f"同程国际低价日历有 {len(missing)} 个所选日期暂无有效总价；"
                "可能尚无缓存、无航班或超出约 90 天日历范围，不能据此判断售罄"
            )
        return SearchResult(
            [quotes_by_day[day] for day in sorted(quotes_by_day)], warnings)

    def _parse(self, data: dict, route: Route, day: date) -> SearchResult:
        state = data.get("state")
        book = state.get("book1") if isinstance(state, dict) else None
        if data.get("serverRendered") is not True or not isinstance(book, dict):
            raise ProviderError("同程网页未返回航班状态，可能是验证页或页面已变化")
        if (book.get("Departure") != route.origin or book.get("Arrival") != route.destination
                or book.get("DepartureDate") != day.isoformat() or book.get("hasReturn") is not False):
            raise ProviderError("同程网页航线、日期或单程标记与请求不一致")
        rows = book.get("flightLists")
        if not isinstance(rows, list):
            raise ProviderError("同程网页缺少航班列表")
        candidates, skipped = [], 0
        for row in rows:
            if not isinstance(row, dict):
                raise ProviderError("同程航班字段发生变化")
            if (row.get("departureCityCode") != route.origin
                    or row.get("arrivalCityCode") != route.destination):
                raise ProviderError("同程航班城市与请求不一致")
            try:
                departure = datetime.strptime(row["flyOffTime"], "%Y-%m-%d %H:%M")
            except (KeyError, TypeError, ValueError):
                raise ProviderError("同程航班出发时间格式发生变化") from None
            if departure.date() != day:
                raise ProviderError("同程航班出发日期与请求不一致")
            if row.get("hasmt") is not False:
                # This marker is not publicly documented: do not extrapolate
                # the verified default flight row to differently marked rows.
                skipped += 1
                continue
            stop_value = _amount(row.get("stopNum"), "经停次数")
            if stop_value != stop_value.to_integral_value() or stop_value > 20:
                raise ProviderError("同程经停次数无效")
            stops = int(stop_value)
            fare = _amount(row.get("lcp"), "成人票价 lcp")
            if not fare:
                continue
            airport_fee = _amount(row.get("pt"), "机建 pt")
            fuel_fee = _amount(row.get("ot"), "燃油 ot")
            flag = row.get("g5flag")
            if isinstance(flag, bool) or flag not in (0, 1):
                raise ProviderError("同程特殊机建费标记无效")
            if flag == 1:
                special_fee = _amount(row.get("g5pt"), "特殊机建 g5pt")
                if special_fee:
                    airport_fee = special_fee
                stops = None  # The official list calls this 华夏联程.
            flight_number = row.get("flightNo")
            airline = row.get("airCompanyName")
            if not isinstance(flight_number, str) or not flight_number or not isinstance(airline, str):
                raise ProviderError("同程航班号或航空公司字段无效")
            candidates.append(Quote(
                origin=route.origin, destination=route.destination,
                departure_date=day, price=fare + airport_fee + fuel_fee,
                currency="CNY", source="同程公开航班页", airline=airline,
                flight_number=flight_number, stops=stops, url=self._url(route, day),
                provider="tongcheng", price_basis="total",
                price_note=(f"同程网页本次返回的 {len(rows)} 个航班范围内参考价；"
                            f"1 成人单程，票价 {fare} + 机建 {airport_fee} + 燃油 {fuel_fee} 元；"
                            "平台默认舱位，不含可选服务，最终可售价格及行李请到购票页确认"),
            ))
        warnings = []
        if skipped:
            warnings.append(f"{day} 同程 {skipped} 条含未支持标记或字段不完整的航班未参与总价比较")
        if not candidates:
            warnings.append(f"{day} 同程网页暂无可确认的含税价格，不能据此判断售罄")
        return SearchResult([min(candidates, key=lambda quote: quote.price)] if candidates else [], warnings)
