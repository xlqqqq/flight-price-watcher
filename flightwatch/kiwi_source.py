"""Kiwi's anonymous Chinese public deal pages; limited cached offers, not inventory.

Locations are identified by exact city code and airport parent city. The page's
dated JSON-LD offers are filtered by route, date and one-way URL. The same
schema incorrectly labels connecting trips as nonstop (verified 2026-09-09),
so stops are deliberately unknown. The deal page also does not establish the
tax/passenger basis: these prices are references, never comparable totals.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
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

from .models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult


LOCATIONS_ENDPOINT = "https://api.skypicker.com/locations"
PAGE_ROOT = "https://www.kiwi.com/cn/cheap-flights/"
_CODE = re.compile(r"[A-Z]{3}\Z")
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_ID = re.compile(r"[a-z0-9_-]+\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_MAX_RESPONSE_BYTES = 6_000_000
_CACHE_TTL = 24 * 60 * 60
PRICE_NOTE = (
    "Kiwi 官方公开单程优惠参考，日期有限且可能是缓存；"
    "优惠页未单列税费、成人数和舱位口径，不参与最低总价或阈值提醒；"
    "行李、转机及最终可售价请到购票页确认"
)


class _Schemas(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.fragments: list[str] = []
        self.schemas: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") == "application/ld+json" and attrs.get("data-test") == "FlightCollectionSchema":
            self.active = True
            self.fragments = []

    def handle_data(self, data):
        if self.active:
            self.fragments.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.active = False
            self.schemas.append("".join(self.fragments))


def _day(value: object) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("invalid date")
    return date.fromisoformat(value)


def _path_part(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 180 and all(
        character.isalnum() or character in "-_" for character in value
    )


class KiwiDealsProvider:
    def __init__(self, timeout: float = 30, request_delay: float = 1.0,
                 max_requests: int = 60, *, cancelled=None):
        self.timeout = timeout
        self.request_delay = request_delay
        self.max_requests = max_requests
        self.requests_used = 0
        self.cancelled = cancelled
        self._last_request: float | None = None
        self._request_lock = threading.Lock()
        self._cities: dict[str, tuple[float, dict]] = {}

    def _check_cancelled(self):
        if self.cancelled is not None and self.cancelled():
            raise ProviderError("监控已停止，取消 Kiwi 后续查询")

    def _request(self, url: str) -> str:
        self._check_cancelled()
        with self._request_lock:
            self._check_cancelled()
            if self.requests_used >= self.max_requests:
                raise ProviderError("Kiwi 查询已达到本轮请求上限")
            if self._last_request is not None:
                wait = self.request_delay - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
            self._check_cancelled()
            self.requests_used += 1
            self._last_request = time.monotonic()
            request = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Accept": "text/html,application/json",
            })
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise ProviderError("Kiwi 响应超过大小限制，网页可能已变化")
                return body.decode("utf-8")
            except urllib.error.HTTPError as exc:
                raise ProviderError(f"Kiwi 返回 HTTP {exc.code}，可能触发网站防护；本次不重试") from None
            except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as exc:
                raise ProviderError(f"Kiwi 查询网络失败（{type(exc).__name__}）") from None
            except UnicodeDecodeError:
                raise ProviderError("Kiwi 返回无效网页编码") from None

    def _locations(self, **params) -> list[dict]:
        raw = self._request(LOCATIONS_ENDPOINT + "?" + urllib.parse.urlencode({
            **params, "locale": "zh-CN", "limit": 100,
        }))
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            raise ProviderError("Kiwi 城市服务返回验证页面或无效 JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("locations"), list) or not all(isinstance(row, dict) for row in data["locations"]):
            raise ProviderError("Kiwi 城市服务结构已变化")
        return data["locations"]

    def _resolve_city(self, code: str) -> dict:
        cached = self._cities.get(code)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL:
            return cached[1]
        candidates = self._locations(term=code, location_types="city")
        matches = [row for row in candidates if row.get("type") == "city" and row.get("active") is True and row.get("code") == code]
        if len(matches) != 1:
            raise ProviderUnsupported(f"Kiwi 未能唯一识别城市 {code}，未替换为附近城市")
        city = matches[0]
        identity, slug, local_slug, country = city.get("id"), city.get("slug_en"), city.get("slug"), city.get("country")
        if not isinstance(identity, str) or not _ID.fullmatch(identity) or not isinstance(slug, str) or not _SLUG.fullmatch(slug) or not _path_part(local_slug) or not isinstance(country, dict) or not isinstance(country.get("code"), str) or not re.fullmatch(r"[A-Z]{2}", country["code"]):
            raise ProviderError("Kiwi 城市身份字段无效，未继续查询")
        airports = self._locations(type="subentity", term=identity, location_types="airport")
        codes = set()
        for airport in airports:
            parent = airport.get("city")
            airport_code = airport.get("code")
            if airport.get("type") != "airport" or airport.get("active") is not True:
                continue
            if not isinstance(parent, dict) or parent.get("id") != identity or parent.get("code") != code:
                continue
            if not isinstance(airport_code, str) or not _CODE.fullmatch(airport_code):
                raise ProviderError("Kiwi 机场代码无效")
            codes.add(airport_code)
        if not codes:
            raise ProviderUnsupported(f"Kiwi 未能核验城市 {code} 的机场归属")
        result = {"id": identity, "code": code, "slug": slug, "local_slug": local_slug,
                  "country": country["code"], "airports": frozenset(codes)}
        if len(self._cities) >= 512:
            self._cities.pop(next(iter(self._cities)))
        self._cities[code] = (time.monotonic(), result)
        return result

    @staticmethod
    def _query_city_code(route: Route, side: str) -> str:
        scope = getattr(route, f"{side}_scope")
        selected = getattr(route, side)
        if scope == "city":
            return selected
        if scope == "airport":
            city = route.city_code(side)
            if not isinstance(city, str) or not _CODE.fullmatch(city):
                raise ProviderUnsupported(
                    f"Kiwi 指定机场 {selected} 缺少可核验的所属城市代码"
                )
            return city
        raise ProviderUnsupported("Kiwi 地点范围必须是城市全部机场或具体机场")

    @staticmethod
    def _restrict_airports(place: dict, route: Route, side: str) -> dict:
        if getattr(route, f"{side}_scope") != "airport":
            return place
        selected = getattr(route, side)
        if selected not in place["airports"]:
            raise ProviderUnsupported(
                f"Kiwi 未确认机场 {selected} 属于所选城市 {place['code']}，未改查附近机场"
            )
        return {**place, "airports": frozenset({selected})}

    def search(self, route: Route, today: date) -> SearchResult:
        if route.currency != "CNY":
            raise ProviderUnsupported("Kiwi 中文公开优惠页仅支持 CNY，本次不换算币种")
        if route.stay_nights is not None or route.nonstop or route.travel_class != 1:
            raise ProviderUnsupported("Kiwi 公开优惠页仅支持默认舱位单程参考，无法可靠筛选直飞、往返或指定舱位")
        if not all(isinstance(code, str) and _CODE.fullmatch(code) for code in (route.origin, route.destination)):
            raise ProviderUnsupported("Kiwi 查询须使用已选择的三字母城市或机场代码")
        origin_city = self._query_city_code(route, "origin")
        destination_city = self._query_city_code(route, "destination")
        wanted = set(route.departure_dates(today))
        if not wanted:
            return SearchResult([], ["配置中没有尚未过期的出发日期"])
        origin = self._restrict_airports(self._resolve_city(origin_city), route, "origin")
        destination = self._restrict_airports(
            self._resolve_city(destination_city), route, "destination"
        )
        market = "domestic" if origin["country"] == destination["country"] == "CN" else "international"
        if route.market != market:
            raise ProviderUnsupported("Kiwi 识别的国内国际范围与所选航线不一致")
        page_url = PAGE_ROOT + origin["slug"] + "/" + destination["slug"] + "/"
        return self._parse(self._request(page_url), route, today, wanted, origin, destination, page_url)

    def _parse(self, html: str, route: Route, today: date, wanted: set[date],
               origin: dict, destination: dict, page_url: str) -> SearchResult:
        parser = _Schemas()
        parser.feed(html)
        # Some landing-page templates render the identical schema twice.
        # Accept exact duplicates; conflicting collections remain an error.
        schemas = list(dict.fromkeys(parser.schemas))
        if len(schemas) != 1:
            raise ProviderError("Kiwi 未返回可核验的优惠列表，可能是验证页面、暂无公开优惠或网页已变化")
        try:
            schema = json.loads(schemas[0], parse_float=Decimal)
        except (ValueError, RecursionError):
            raise ProviderError("Kiwi 优惠数据无效") from None
        if not isinstance(schema, dict) or schema.get("@type") != "CollectionPage" or schema.get("url") != page_url:
            raise ProviderError("Kiwi 回显航线与所选城市不一致，未使用其他航线价格")
        try:
            updated = _day(schema.get("dateModified"))
        except ValueError:
            raise ProviderError("Kiwi 优惠缺少有效更新日期") from None
        if not today - timedelta(days=2) <= updated <= today + timedelta(days=1):
            raise ProviderError("Kiwi 公开优惠更新日期过旧或异常，本次不使用其价格")
        entity = schema.get("mainEntity")
        if not isinstance(entity, dict) or not isinstance(entity.get("itemListElement"), list):
            raise ProviderError("Kiwi 优惠列表结构已变化")
        quotes = {}
        skipped = 0
        for entry in entity["itemListElement"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("item"), dict):
                raise ProviderError("Kiwi 优惠字段已变化")
            offer = entry["item"]
            if offer.get("@type") != "Offer" or offer.get("availability") != "https://schema.org/InStock":
                continue
            url = offer.get("url")
            if not isinstance(url, str):
                raise ProviderError("Kiwi 优惠缺少日期查询链接")
            parsed = urllib.parse.urlsplit(url)
            parts = urllib.parse.unquote(parsed.path).strip("/").split("/")
            if parsed.scheme != "https" or parsed.netloc != "www.kiwi.com" or parsed.query or parsed.fragment or len(parts) != 7 or parts[:3] != ["cn", "search", "results"]:
                skipped += 1
                continue
            if parts[3] not in {origin["slug"], origin["local_slug"]} or parts[4] not in {destination["slug"], destination["local_slug"]}:
                skipped += 1
                continue
            if parts[6] != "no-return":
                continue
            try:
                departure = _day(parts[5])
            except ValueError:
                raise ProviderError("Kiwi 优惠链接日期无效") from None
            if departure not in wanted:
                continue
            trip = offer.get("itemOffered")
            itinerary = trip.get("itinerary") if isinstance(trip, dict) else None
            flights = itinerary.get("itemListElement") if isinstance(itinerary, dict) else None
            if not isinstance(flights, list) or len(flights) != 1 or not isinstance(flights[0], dict):
                skipped += 1
                continue
            flight = flights[0]
            dep_airport, arr_airport = flight.get("departureAirport"), flight.get("arrivalAirport")
            if flight.get("@type") != "Flight" or not isinstance(dep_airport, dict) or not isinstance(arr_airport, dict) or dep_airport.get("iataCode") not in origin["airports"] or arr_airport.get("iataCode") not in destination["airports"]:
                skipped += 1
                continue
            try:
                flight_day = datetime.fromisoformat(flight["departureTime"]).date()
                valid_from = _day(offer.get("validFrom"))
            except (KeyError, TypeError, ValueError):
                raise ProviderError("Kiwi 优惠的航班日期或更新日期无效") from None
            if flight_day != departure or not today - timedelta(days=2) <= valid_from <= today + timedelta(days=1):
                skipped += 1
                continue
            if offer.get("priceCurrency") != route.currency:
                skipped += 1
                continue
            raw_price = offer.get("price")
            if raw_price is None or isinstance(raw_price, bool):
                raise ProviderError("Kiwi 返回无效价格")
            try:
                price = Decimal(str(raw_price))
            except InvalidOperation:
                raise ProviderError("Kiwi 返回非数字价格") from None
            if not price.is_finite() or price <= 0:
                raise ProviderError("Kiwi 返回无效价格")
            quote = Quote(origin=route.origin, destination=route.destination,
                          departure_date=departure, price=price, currency=route.currency,
                          source="Kiwi.com 公开优惠", provider="kiwi", price_basis="unknown",
                          price_note=PRICE_NOTE, url=url,
                          origin_airport=dep_airport["iataCode"],
                          destination_airport=arr_airport["iataCode"])
            if departure not in quotes or price < quotes[departure].price:
                quotes[departure] = quote
        warnings = []
        if quotes:
            warnings.append("Kiwi 优惠页未单列税费及成人数口径，价格仅供参考，不参与最低总价和阈值提醒")
        if skipped:
            warnings.append(f"已跳过 {skipped} 条币种、日期或城市机场归属无法匹配的 Kiwi 优惠")
        missing = wanted - set(quotes)
        if missing:
            warnings.append(f"{len(missing)} 个出发日期暂无 Kiwi 公开优惠；该页面日期有限，不代表没有航班")
        return SearchResult([quotes[day] for day in sorted(quotes)], warnings)
