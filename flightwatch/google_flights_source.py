"""Anonymous Google Flights HTML search, with checked query/price provenance.

The public page embeds AF_initDataCallback JSON. We read that data without
executing scripts, using keys observed on 2026-09-09. Every response must echo
the requested cities, date, one adult, economy and one-way trip, and visibly
confirm CNY and required taxes/fees. See docs/google-flights-source.md.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from http.client import HTTPException
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .cities import resolve_city
from .city_search import cached_city
from .models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult


ENDPOINT = "https://www.google.com/travel/flights"
_MAX_RESPONSE_BYTES = 8_000_000
_CODE = re.compile(r"[A-Z]{3}\Z")
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
PRICE_NOTE = (
    "Google Flights 公开搜索含税参考总价，1 成人经济舱单程；"
    "仅比较本次网页返回的航班，可能含中转，并非全部可售航班。"
    "行李、可选服务及部分支付方式可能另收费，成交价请到预订页核实"
)


class _AccessBlocked(ProviderError):
    """Do not continue a date loop after an access challenge."""


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts: dict[str, str] = {}
        self._script = None
        self._skip = 0
        self.text = []
        self.currencies = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style"}:
            self._skip += 1
        if tag == "script":
            keys = set((attrs.get("class") or "").split()) & {"ds:0", "ds:1"}
            if keys:
                key = next(iter(keys))
                if key in self.scripts:
                    raise ProviderError("Google Flights 出现重复查询数据，未使用含糊票价")
                self._script = key
                self.scripts[key] = ""
        if tag == "button":
            match = re.fullmatch(r"Currency ([A-Z]{3})", attrs.get("aria-label") or "")
            if match:
                self.currencies.add(match[1])

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
        if tag == "script":
            self._script = None

    def handle_data(self, value):
        if self._script is not None:
            self.scripts[self._script] += value
        elif not self._skip and value.strip():
            self.text.append(value.strip())

    def payload(self, key):
        script = self.scripts.get(key, "")
        if "errorHasStatus" in script:
            raise ProviderError("Google Flights 页面未返回完整查询数据，请稍后重试")
        try:
            match = re.search(r"\bdata\s*:\s*", script)
            if not match:
                raise ValueError("missing data")
            payload, end = json.JSONDecoder(parse_float=Decimal).raw_decode(script[match.end():])
            if not isinstance(payload, list):
                raise ValueError("invalid data")
            return payload
        except (ValueError, RecursionError, InvalidOperation):
            raise ProviderError("Google Flights 页面数据结构已变化，未解析未知格式") from None


def _get(value, *indices):
    try:
        for index in indices:
            if not isinstance(value, list):
                return None
            value = value[index]
        return value
    except IndexError:
        return None


def _day(value):
    if not isinstance(value, list) or len(value) != 3 or any(type(part) is not int for part in value):
        return None
    try:
        return date(*value)
    except ValueError:
        return None


def _city_entity(value, wanted_code):
    if (
        not isinstance(value, list)
        or _get(value, 0, 1) != 4
        or _get(value, 2, 5) != wanted_code
        or not isinstance(_get(value, 0, 0), str)
        or not _get(value, 0, 0).startswith(("/m/", "/g/"))
        or _get(value, 2, 0) != _get(value, 0, 0)
    ):
        raise ProviderUnsupported(
            f"Google Flights 未准确回显所选城市 {wanted_code}，未使用其他城市或单个机场的票价"
        )
    return _get(value, 0, 0)


class GoogleFlightsProvider:
    def __init__(self, timeout: float = 30, request_delay: float = 1.0,
                 max_requests: int = 60, *, cancelled=None):
        self.timeout = timeout
        self.request_delay = request_delay
        self.max_requests = max_requests
        self.cancelled = cancelled
        self.requests_used = 0
        self._last_request = None
        self._request_lock = threading.Lock()

    def _check_cancelled(self):
        if self.cancelled is not None and self.cancelled():
            raise _AccessBlocked("监控已停止，取消 Google Flights 后续查询")

    def _request(self, url):
        self._check_cancelled()
        with self._request_lock:
            self._check_cancelled()
            if self.requests_used >= self.max_requests:
                raise _AccessBlocked("Google Flights 查询达到本轮请求上限")
            if self._last_request is not None:
                remaining = self.request_delay - (time.monotonic() - self._last_request)
                while remaining > 0:
                    time.sleep(min(remaining, 0.1))
                    self._check_cancelled()
                    remaining = self.request_delay - (time.monotonic() - self._last_request)
            self._check_cancelled()
            self.requests_used += 1
            self._last_request = time.monotonic()
            request = urllib.request.Request(url, headers={
                "User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html", "Accept-Encoding": "identity",
            })
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    location = urllib.parse.urlsplit(response.url)
                    if location.scheme != "https" or location.hostname != "www.google.com" or not location.path.startswith("/travel/flights"):
                        raise _AccessBlocked("Google Flights 跳转到登录、同意或验证页面，本轮已停止")
                    if "unsupported" in location.path:
                        raise ProviderError("Google Flights 暂不接受当前客户端，未读取不受支持页面的价格")
                    body = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise ProviderError("Google Flights 页面超过大小限制")
                self._check_cancelled()
                return body.decode("utf-8")
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403, 429}:
                    raise _AccessBlocked(f"Google Flights 返回 HTTP {exc.code}，可能要求验证，本轮停止且不绕过") from None
                raise ProviderError(f"Google Flights 返回 HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as exc:
                raise ProviderError(f"Google Flights 网络查询失败（{type(exc).__name__}）") from None
            except UnicodeDecodeError:
                raise ProviderError("Google Flights 页面编码异常") from None

    def search(self, route: Route, today: date) -> SearchResult:
        self._check_cancelled()
        if route.currency != "CNY":
            raise ProviderUnsupported("Google Flights 当前适配仅核实 CNY 人民币报价")
        if route.stay_nights is not None or route.nonstop or route.travel_class != 1:
            raise ProviderUnsupported("Google Flights 当前适配仅支持1成人经济舱单程，暂不支持往返或直飞过滤")
        if route.market not in {"domestic", "international"}:
            raise ProviderUnsupported("Google Flights 需要明确国内或国际航线")
        names = []
        for code in (route.origin, route.destination):
            known = (cached_city(code) or resolve_city(code)) if isinstance(code, str) and _CODE.fullmatch(code) else None
            if not known or not isinstance(known.get("name"), str) or not known["name"].strip():
                raise ProviderUnsupported("Google Flights 需要已搜索选定的城市名称，无法确认未知代码对应的城市")
            names.append(known["name"].strip())
        wanted = route.departure_dates(today)
        if not wanted:
            return SearchResult([], ["配置中没有尚未过期的出发日期"])
        quotes, warnings, completed = [], [], 0
        for departure in wanted:
            self._check_cancelled()
            query = f"One way flights from {names[0]} to {names[1]} on {departure.isoformat()} for 1 adult in economy"
            url = ENDPOINT + "?" + urllib.parse.urlencode({"q": query, "hl": "en", "curr": "CNY"})
            try:
                result = self._parse_page(self._request(url), route, departure, url)
                self._check_cancelled()
                quotes.extend(result.quotes)
                warnings.extend(result.warnings)
                completed += 1
            except _AccessBlocked:
                raise
            except ProviderUnsupported:
                raise
            except ProviderError as exc:
                warnings.append(f"{departure.isoformat()}：{exc}")
        if not completed:
            raise ProviderError("；".join(warnings))
        warnings.append("Google Flights 仅比较各日期网页初始返回且有有效价格的航班，不代表全部航班；报价可能与最终预订页不同")
        return SearchResult(quotes, warnings)

    def _parse_page(self, html, route: Route, departure: date, url: str) -> SearchResult:
        page = _Page()
        try:
            page.feed(html)
            page.close()
        except (ValueError, RecursionError):
            raise ProviderError("Google Flights HTML 结构异常") from None
        visible = " ".join(page.text)
        if any(text in visible.lower() for text in (
            "unusual traffic", "not a robot", "before you continue to google", "verify you are human",
        )):
            raise _AccessBlocked("Google Flights 要求验证或同意，本轮停止且不绕过")
        if page.currencies != {"CNY"}:
            raise ProviderError("Google Flights 未确认页面显示币种为 CNY，未混用其他币种")
        if "Prices include required taxes + fees for 1 adult" not in visible:
            raise ProviderError("Google Flights 未确认1成人含税总价口径，未用于价格提醒")
        query, payload = page.payload("ds:0"), page.payload("ds:1")
        settings = _get(query, 1, 1)
        legs = _get(settings, 13)
        passengers = _get(settings, 6)
        if (
            type(_get(settings, 2)) is not int or _get(settings, 2) != 2
            or type(_get(settings, 5)) is not int or _get(settings, 5) != 1
            or passengers != [1, 0, 0, 0] or any(type(value) is not int for value in passengers)
            or not isinstance(legs, list) or len(legs) != 1
            or _get(legs, 0, 6) != departure.isoformat()
        ):
            raise ProviderError("Google Flights 回显日期、单程、舱位或乘客数与查询不符")
        origin = _get(query, 2, 0)
        destination = _get(query, 2, 1)
        origin_id = _city_entity(origin, route.origin)
        destination_id = _city_entity(destination, route.destination)
        if (
            _get(legs, 0, 0) != [[[origin_id, 4]]]
            or _get(legs, 0, 1) != [[[destination_id, 4]]]
            or _get(payload, 1, 0, 0) is None or _get(payload, 1, 0, 1) is None
            or _get(payload, 1, 0, 0, 0, 0) != [origin_id, 4]
            or _get(payload, 1, 0, 1, 0, 0) != [destination_id, 4]
        ):
            raise ProviderError("Google Flights 查询与报价回显城市不一致")
        if (route.market == "domestic") != (_get(origin, 4) == "CN" and _get(destination, 4) == "CN"):
            raise ProviderUnsupported("Google Flights 城市所属国家地区与航线类型不符")
        rows = _get(payload, 3, 0)
        if rows is None and isinstance(_get(payload, 3), list):
            return SearchResult([], [f"{departure}：Google Flights 暂无可报价航班"])
        if not isinstance(rows, list):
            raise ProviderError("Google Flights 缺少报价列表，页面格式可能已变化")
        airports = _get(payload, 17)
        if not isinstance(airports, list):
            raise ProviderError("Google Flights 未提供机场所属城市，未混入附近机场票价")
        airport_cities = {}
        for entity in airports:
            code = _get(entity, 0, 0)
            if isinstance(code, str) and _CODE.fullmatch(code) and _get(entity, 0, 1) == 0 and _get(entity, 5) == 0:
                airport_cities[code] = _get(entity, 2, 0)
        valid, excluded, unpriced = [], 0, 0
        for row in rows:
            if not isinstance(row, list) or len(row) < 2 or not isinstance(row[0], list):
                raise ProviderError("Google Flights 航班行结构已变化")
            raw_price = _get(row, 1, 0, 1)
            if raw_price is None:
                unpriced += 1
                continue
            if type(raw_price) not in {int, float, Decimal}:
                raise ProviderError("Google Flights 返回非数字价格")
            price = Decimal(str(raw_price))
            if not price.is_finite() or price <= 0:
                raise ProviderError("Google Flights 返回非正数或无效价格")
            flight = _get(row, 0)
            segments = _get(flight, 2)
            if (
                not isinstance(segments, list) or not segments
                or _day(_get(flight, 4)) != departure
                or airport_cities.get(_get(flight, 3)) != origin_id
                or airport_cities.get(_get(flight, 6)) != destination_id
                or _get(segments, 0, 3) != _get(flight, 3)
                or _get(segments, len(segments) - 1, 6) != _get(flight, 6)
                or _day(_get(segments, 0, 20)) != departure
            ):
                excluded += 1
                continue
            consistent = True
            for index, segment in enumerate(segments):
                start, end = _day(_get(segment, 20)), _day(_get(segment, 21))
                if start is None or end is None or end < start:
                    consistent = False
                if index and (
                    _get(segments, index - 1, 6) != _get(segment, 3)
                    or start is None or _day(_get(segments, index - 1, 21)) is None
                    or start < _day(_get(segments, index - 1, 21))
                ):
                    consistent = False
            if not consistent:
                excluded += 1
                continue
            airlines = _get(flight, 1)
            airline = "/".join(airlines) if isinstance(airlines, list) and all(isinstance(v, str) for v in airlines) else ""
            flight_numbers = []
            for segment in segments:
                carrier, number = _get(segment, 22, 0), _get(segment, 22, 1)
                if isinstance(carrier, str) and isinstance(number, str):
                    flight_numbers.append(carrier + number)
            valid.append(Quote(
                origin=route.origin, destination=route.destination, departure_date=departure,
                price=price, currency="CNY", source="Google Flights 公开航班页",
                airline=airline, flight_number="/".join(flight_numbers), stops=len(segments) - 1,
                url=url, price_note=PRICE_NOTE, provider="google_flights", price_basis="total",
            ))
        notes = []
        if excluded:
            notes.append(f"{departure}：Google Flights 已排除 {excluded} 条日期、城市机场或中转链不匹配的报价")
        if unpriced:
            notes.append(f"{departure}：Google Flights 有 {unpriced} 条航班未公布价格，未参与比较")
        if excluded and not valid:
            raise ProviderError("Google Flights 所有带价航班均未通过日期、城市机场或中转链核对，未用于提醒")
        if not valid:
            notes.append(f"{departure}：Google Flights 暂无可核实报价")
        return SearchResult([min(valid, key=lambda quote: quote.price)] if valid else [], notes)
