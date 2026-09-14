"""Spring Airlines' anonymous route page and exact-date fare calendar.

The route page supplies the airline's current city names and independently
echoes route, passenger, trip, currency and market fields.  A calendar price is
accepted only after that page validation and only from an exact matching date
requested with Spring's ``IsShowTaxprice=true`` switch.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult


ROOT = "https://flights.ch.com"
CALENDAR = ROOT + "/Flights/MinPriceTrends"
_HOST = "flights.ch.com"
_CODE = re.compile(r"[A-Z]{3}\Z")
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
_MAX_HTML = 2_000_000
_MAX_JSON = 1_000_000


class _AccessBlocked(ProviderError):
    """An access challenge must stop all remaining dates."""


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


class _RoutePage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return
        fields = dict(attrs)
        name = fields.get("name")
        if name:
            self.values.setdefault(name, []).append(fields.get("value", ""))

    def one(self, name: str) -> str:
        values = self.values.get(name)
        if not values or len(values) != 1 or not isinstance(values[0], str):
            raise ProviderError(f"春秋航空航线页缺少唯一 {name} 字段")
        return values[0]


def _amount(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ProviderError("春秋航空价格不是有效金额")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ProviderError("春秋航空价格不是有效金额") from None
    if not result.is_finite() or result < 0 or result > Decimal("10000000"):
        raise ProviderError("春秋航空价格超出有效范围")
    return result or None


class SpringAirlinesProvider:
    def __init__(self, timeout=30, request_delay=1.0, max_requests=60, *, cancelled=None):
        self.timeout = timeout
        self.request_delay = request_delay
        self.max_requests = max_requests
        self.cancelled = cancelled
        self.requests_used = 0
        self._last_request = None
        self._request_lock = threading.Lock()

    def _check_cancelled(self):
        if self.cancelled and self.cancelled():
            raise _AccessBlocked("监控已停止，取消春秋航空后续查询")

    def _request(self, url, *, data=None, headers=None, max_bytes=_MAX_JSON):
        self._check_cancelled()
        with self._request_lock:
            self._check_cancelled()
            if self.requests_used >= self.max_requests:
                raise _AccessBlocked("春秋航空已达到本轮请求上限")
            if self._last_request is not None:
                remaining = self.request_delay - (time.monotonic() - self._last_request)
                while remaining > 0:
                    time.sleep(min(remaining, .1))
                    self._check_cancelled()
                    remaining = self.request_delay - (time.monotonic() - self._last_request)
            self.requests_used += 1
            self._last_request = time.monotonic()
        request_headers = {
            "User-Agent": _USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        request_headers.update(headers or {})
        request = urllib.request.Request(
            url, data=data, headers=request_headers, method="POST" if data is not None else "GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                final_url = response.geturl()
                content_type = response.headers.get_content_type().lower()
                body = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 429}:
                raise _AccessBlocked(f"春秋航空返回 HTTP {exc.code} 访问保护，本轮停止") from None
            raise ProviderError(f"春秋航空返回 HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise ProviderError(f"春秋航空网络失败（{type(exc).__name__}）") from None
        if len(body) > max_bytes:
            raise ProviderError("春秋航空响应超过大小限制")
        try:
            parsed = urllib.parse.urlsplit(final_url)
            safe_authority = (parsed.username is None and parsed.password is None
                              and parsed.port in {None, 443} and not parsed.fragment)
        except ValueError:
            safe_authority = False
            parsed = None
        if not safe_authority or parsed.scheme != "https" or parsed.hostname != _HOST:
            raise _AccessBlocked("春秋航空跳转到非预期登录、验证或外部页面，本轮停止")
        return body, content_type, final_url

    @staticmethod
    def _route_url(route: Route, day: date) -> str:
        query = urllib.parse.urlencode({
            "Departure": route.origin,
            "Arrival": route.destination,
            "FDate": day.isoformat(),
            "ANum": 1,
            "CNum": 0,
            "INum": 0,
            "IfRet": "false",
            "SType": 0,
            "MType": 0,
            "IsNew": 1,
        })
        return f"{ROOT}/{route.origin}-{route.destination}.html?{query}"

    @staticmethod
    def official_url(route: Route, day: date) -> str:
        """Public booking page carrying the validated route and passenger filters."""
        return SpringAirlinesProvider._route_url(route, day)

    def _route_context(self, route: Route, day: date):
        expected_url = self._route_url(route, day)
        body, kind, final_url = self._request(
            expected_url, headers={"Accept": "text/html"}, max_bytes=_MAX_HTML
        )
        if kind != "text/html":
            raise ProviderError("春秋航空航线页未返回 HTML")
        parsed_url = urllib.parse.urlsplit(final_url)
        if parsed_url.path.upper() != f"/{route.origin}-{route.destination}.HTML":
            raise ProviderUnsupported("春秋航空未保留所选航线，未改查其他城市或机场")
        try:
            html = body.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderError("春秋航空航线页编码无效") from None
        parser = _RoutePage()
        try:
            parser.feed(html)
        except (ValueError, RecursionError):
            raise ProviderError("春秋航空航线页 HTML 无效") from None
        expected = {
            "oriCode": route.origin,
            "desCode": route.destination,
            "currency": "0",
            "departureDate": day.isoformat(),
            "returnDate": "",
            "ifRet": "false",
            "isIJFlight": "false",
            "isBg": "false",
            "ActId": "0",
            "isEmployee": "false",
            "saNum": "1",
            "scNum": "0",
            "siNum": "0",
            "sType": "0",
            "SpecTravTypeId": "0",
            "IsJC": "false",
            "IsInternational": "true" if route.market == "international" else "false",
        }
        for name, value in expected.items():
            if parser.one(name).casefold() != value.casefold():
                raise ProviderUnsupported(
                    f"春秋航空未准确回显所选航线、日期、1 成人单程、币种或国内国际市场（{name}）"
                )
        departure, arrival = parser.one("departure").strip(), parser.one("arrival").strip()
        if not departure or not arrival or len(departure) > 100 or len(arrival) > 100:
            raise ProviderError("春秋航空航线页城市名称无效")
        return departure, arrival, expected_url

    @staticmethod
    def _parse_calendar(payload, day: date) -> Decimal | None:
        code = payload.get("Code") if isinstance(payload, dict) else None
        if not ((type(code) is int and code == 0) or code == "0"):
            raise ProviderError("春秋航空最低价日历查询未成功")
        rows = payload.get("PriceTrends")
        if not isinstance(rows, list) or len(rows) > 400 or any(not isinstance(row, dict) for row in rows):
            raise ProviderError("春秋航空最低价日历结构无效")
        matches = []
        for row in rows:
            raw_day = row.get("Date")
            if not isinstance(raw_day, str) or len(raw_day) > 40:
                raise ProviderError("春秋航空最低价日期字段无效")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?", raw_day):
                raise ProviderError("春秋航空最低价日期字段无效") from None
            try:
                row_day = date.fromisoformat(raw_day[:10])
            except ValueError:
                raise ProviderError("春秋航空最低价日期字段无效") from None
            price = _amount(row.get("Price"))
            if row_day == day and price is not None:
                matches.append(price)
        if len(matches) > 1:
            raise ProviderError("春秋航空同一日期返回多个含糊最低价")
        return matches[0] if matches else None

    def _calendar(self, route: Route, day: date, departure: str, arrival: str, referer: str):
        fields = {
            "Currency": 0,
            "DepartureDate": day.isoformat(),
            "IsShowTaxprice": "true",
            "Departure": departure,
            "Arrival": arrival,
            "SType": 0,
            "IsIJFlight": "false",
            "Days": 0,
            "IfRet": "false",
            "ActId": 0,
            "IsReturn": "false",
            "IsUM": "false",
            "SpecTravTypeId": 0,
            "IsEmployee": "false",
            "IsJC": "false",
            "IsBg": "false",
        }
        body, kind, final_url = self._request(
            CALENDAR,
            data=urllib.parse.urlencode(fields).encode("ascii"),
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": ROOT,
                "Referer": referer,
                "X-Requested-With": "XMLHttpRequest",
            },
            max_bytes=_MAX_JSON,
        )
        if urllib.parse.urlsplit(final_url).path != "/Flights/MinPriceTrends":
            raise _AccessBlocked("春秋航空最低价接口跳转到验证页面，本轮停止")
        if kind not in {"application/json", "text/json"}:
            raise ProviderError("春秋航空最低价接口未返回 JSON")
        try:
            payload = json.loads(
                body.decode("utf-8"), parse_float=Decimal,
                object_pairs_hook=_strict_object,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (UnicodeDecodeError, ValueError, RecursionError, InvalidOperation):
            raise ProviderError("春秋航空最低价接口返回无效 JSON 或验证页") from None
        return self._parse_calendar(payload, day)

    def search(self, route: Route, today: date):
        for side, label in (("origin", "出发地"), ("destination", "目的地")):
            scope = getattr(route, f"{side}_scope")
            if scope not in {"city", "airport"}:
                raise ProviderUnsupported(f"春秋航空{label}范围必须是城市全部机场或具体机场")
            if scope == "airport":
                city = route.city_code(side)
                if not isinstance(city, str) or not _CODE.fullmatch(city):
                    raise ProviderUnsupported(
                        f"春秋航空指定机场 {getattr(route, side)} 缺少有效所属城市代码"
                    )
        if route.currency != "CNY" or route.stay_nights or route.nonstop or route.travel_class != 1:
            raise ProviderUnsupported("春秋航空仅查询 1 成人经济舱单程 CNY 票价")
        if not all(isinstance(code, str) and _CODE.fullmatch(code) for code in (route.origin, route.destination)):
            raise ProviderUnsupported("春秋航空需要明确的三字城市/机场代码")
        if route.origin == route.destination:
            raise ProviderUnsupported("春秋航空出发地和目的地不能相同")
        wanted = route.departure_dates(today)
        if not wanted:
            return SearchResult([], ["没有尚未过期的出发日期"])
        if self.requests_used + 1 + len(wanted) > self.max_requests:
            raise ProviderError("春秋航空完整日期查询超过本轮请求上限")
        departure, arrival, referer = self._route_context(route, wanted[0])
        quotes, failures = [], []
        for index, day in enumerate(wanted):
            try:
                price = self._calendar(route, day, departure, arrival, referer)
                if price is None:
                    continue
                quotes.append(Quote(
                    route.origin, route.destination, day, price, "CNY", "春秋航空官网",
                    airline="春秋航空", stops=None, provider="spring", price_basis="total",
                    original_price=price, original_currency="CNY",
                    origin_airport=route.airport_code("origin"),
                    destination_airport=route.airport_code("destination"),
                    url=self._route_url(route, day),
                    price_note=(
                        "春秋航空官网最低价日历，1 成人经济舱单程；"
                        "请求已启用官网含税价开关并严格匹配航线和日期。"
                        "行李、选座、保险等可选服务及最终库存请到官网确认"
                    ),
                ))
            except _AccessBlocked:
                raise
            except ProviderError as exc:
                if index == 0:
                    raise ProviderError(f"{day.isoformat()}：{exc}") from None
                failures.append(f"{day.isoformat()}：{exc}")
        if not quotes and failures:
            raise ProviderError("；".join(failures))
        warnings = ["春秋航空官网日历未给出具体航班号/经停字段；官网访问保护可能限制自动查询频率"]
        warnings.extend(failures)
        missing = len(wanted) - len(quotes)
        if missing:
            warnings.append(f"{missing} 个日期未返回可严格核验的春秋航空价格，不代表没有航班")
        return SearchResult(quotes, warnings)
