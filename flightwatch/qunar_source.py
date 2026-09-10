"""Anonymous Qunar city suggestions and public flight-price calendar.

Checked against live requests and the JavaScript loaded by
https://flight.qunar.com/site/ on 2026-09-08. The home page calls priceCalendar
with Chinese city searchParam values, priceType=1 domestic / 2 international,
days="" and reads data.gflights[].price. It labels international calendar
values as tax-inclusive, and domestic values as tax-exclusive. These are cached
indicative prices, not currently bookable quotes. We preserve that distinction.

Qunar and Ctrip city codes are not identical (北京 BJS -> Qunar PEK; 西安 SIA
-> Qunar XIY). Suggestions must identify the same city before calendar lookup;
we never blindly select the first suggest result or substitute a nearby city.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from .cities import resolve_city
from .city_search import cached_city
from .models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult


ENDPOINT = "https://gw.flight.qunar.com/api/f/priceCalendar"
SUGGEST_ENDPOINT = "https://m.flight.qunar.com/touch/api/suggest"
_MAX_RESPONSE_BYTES = 2_000_000
_CITY_CACHE_TTL = 24 * 60 * 60
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_CODE = re.compile(r"[A-Z]{3}\Z")
BASE_PRICE_NOTE = (
    "去哪儿国内低价日历未含税票面价；不参与最低总价或阈值提醒。"
    "1 成人单程参考，机建、燃油、行李及最终可售价请到购票页确认"
)
TOTAL_PRICE_NOTE = (
    "去哪儿国际/港澳台低价日历含税参考总价，1 成人单程；"
    "日历缓存可能过期，行李及最终可售价格请到购票页确认"
)


def _city_name(value: str, country: str = "") -> str:
    name = unicodedata.normalize("NFKC", value).strip()
    # Known city metadata may include its country for disambiguation. Remove
    # only that exact suffix, never an arbitrary bracketed district or state.
    if country:
        suffix = "(" + unicodedata.normalize("NFKC", country).strip() + ")"
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip()
    return name


class QunarCalendarProvider:
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

    def _request(self, endpoint: str, params: dict) -> dict:
        if self.cancelled is not None and self.cancelled():
            raise ProviderError("监控已停止，取消去哪儿后续查询")
        with self._request_lock:
            if self.cancelled is not None and self.cancelled():
                raise ProviderError("监控已停止，取消去哪儿后续查询")
            if self.requests_used >= self.max_requests:
                raise ProviderError("去哪儿查询已达到本轮请求上限")
            if self._last_request is not None:
                wait = self.request_delay - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
            if self.cancelled is not None and self.cancelled():
                raise ProviderError("监控已停止，取消去哪儿后续查询")
            self.requests_used += 1
            self._last_request = time.monotonic()
            url = endpoint + "?" + urllib.parse.urlencode(params)
            request = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Accept": "application/json",
                "Referer": "https://flight.qunar.com/",
            })
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise ProviderError("去哪儿响应超过大小限制，接口可能已变化")
                payload = json.loads(body.decode("utf-8"), parse_float=Decimal)
            except urllib.error.HTTPError as exc:
                raise ProviderError(
                    f"去哪儿返回 HTTP {exc.code}，可能触发网站防护；请稍后再试"
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as exc:
                raise ProviderError(f"去哪儿查询网络失败（{type(exc).__name__}）") from None
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise ProviderError("去哪儿返回验证页面或无效 JSON，本次未使用其票价") from None
        if not isinstance(payload, dict):
            raise ProviderError("去哪儿响应结构已变化，本次未使用其票价")
        return payload

    def _resolve_city(self, code: str) -> dict:
        cached = self._cities.get(code)
        if cached and time.monotonic() - cached[0] < _CITY_CACHE_TTL:
            return dict(cached[1])
        known = cached_city(code) or resolve_city(code)
        payload = self._request(SUGGEST_ENDPOINT, {"queryWord": code})
        data = payload.get("data")
        if payload.get("ret") is not True or type(payload.get("code")) is not int or payload["code"] != 0:
            raise ProviderError("去哪儿城市识别请求失败，未继续查询错误城市")
        if not isinstance(data, dict) or type(data.get("code")) is not int or data["code"] != 0:
            raise ProviderError("去哪儿城市识别请求失败，未继续查询错误城市")
        suggestions = data.get("suggestData")
        if not isinstance(suggestions, dict) or not isinstance(suggestions.get("places"), list):
            raise ProviderError("去哪儿城市建议结构已变化")
        matches = []
        for row in suggestions["places"]:
            if not isinstance(row, dict):
                raise ProviderError("去哪儿城市建议结构已变化")
            search = row.get("suggestSearch")
            if (
                type(row.get("type")) is not int or row["type"] != 1
                or row.get("canSearch") is not True or row.get("makeGray") is True
                or not isinstance(search, dict) or search.get("airportCode")
            ):
                continue
            qcode, name, inter = row.get("code"), search.get("searchParam"), search.get("isInter")
            country = search.get("countryName") or row.get("countryName") or ""
            if (
                not isinstance(qcode, str) or not _CODE.fullmatch(qcode)
                or not isinstance(name, str) or not name.strip() or len(name) > 80
                or not isinstance(inter, bool) or not isinstance(country, str)
            ):
                raise ProviderError("去哪儿城市代码、名称或分类字段无效")
            same_code = qcode == code
            same_name = bool(known) and _city_name(name, country) == _city_name(known["name"], known.get("country", ""))
            same_country = not known or not known.get("country") or not country or known["country"] == country
            if not same_code and not (same_name and same_country):
                continue
            if known and (known["market"] == "international") != inter:
                raise ProviderUnsupported("去哪儿城市分类与所选城市不一致，未替换目的地")
            matches.append({"name": name, "code": qcode, "is_international": inter})
        unique = {(item["name"], item["code"], item["is_international"]): item for item in matches}
        if len(unique) != 1:
            raise ProviderUnsupported(f"去哪儿未能唯一识别城市 {code}，本次不查询替代城市")
        result = next(iter(unique.values()))
        if len(self._cities) >= 512:
            self._cities.pop(next(iter(self._cities)))
        self._cities[code] = (time.monotonic(), result)
        return dict(result)

    def search(self, route: Route, today: date) -> SearchResult:
        if route.currency != "CNY":
            raise ProviderUnsupported("去哪儿日历仅支持 CNY")
        if route.stay_nights is not None or route.nonstop or route.travel_class != 1:
            raise ProviderUnsupported("去哪儿日历仅支持默认舱位单程，无法可靠筛选直飞或往返")
        if route.market not in {"domestic", "international"}:
            raise ProviderUnsupported("去哪儿须明确国内或国际/港澳台航线")
        if not all(isinstance(code, str) and _CODE.fullmatch(code) for code in (route.origin, route.destination)):
            raise ProviderUnsupported("去哪儿查询须使用已选择的三字母城市代码")
        wanted = set(route.departure_dates(today))
        if not wanted:
            return SearchResult([], ["配置中没有尚未过期的出发日期"])
        origin, destination = self._resolve_city(route.origin), self._resolve_city(route.destination)
        international = origin["is_international"] or destination["is_international"]
        if international != (route.market == "international"):
            raise ProviderUnsupported("去哪儿识别的国内国际范围与所选航线不一致")
        payload = self._request(ENDPOINT, {
            "dep": origin["name"], "arr": destination["name"],
            "days": "", "priceType": 2 if international else 1,
        })
        return self._parse(payload, route, wanted, origin["name"], destination["name"])

    def _parse(self, payload: dict, route: Route, wanted: set[date], origin_name: str,
               destination_name: str) -> SearchResult:
        status, data = payload.get("bstatus"), payload.get("data")
        if not isinstance(status, dict) or type(status.get("code")) is not int or status["code"] != 0:
            raise ProviderError("去哪儿日历未确认查询成功，未把错误当作有效票价")
        if not isinstance(data, dict) or not isinstance(data.get("gflights"), list):
            raise ProviderError("去哪儿日历缺少去程价格列表，接口可能已变化")
        international = route.market == "international"
        if (
            data.get("scity") != origin_name or data.get("ecity") != destination_name
            or type(data.get("flightType")) is not int
            or data["flightType"] != (2 if international else 1)
        ):
            raise ProviderError("去哪儿回显航线或市场不匹配，未使用其他航线票价")
        quotes = {}
        for row in data["gflights"]:
            if not isinstance(row, dict) or "date" not in row or "price" not in row:
                raise ProviderError("去哪儿日历价格字段已变化")
            raw_date = row["date"]
            try:
                if not isinstance(raw_date, str) or not _DATE.fullmatch(raw_date):
                    raise ValueError("invalid date")
                departure = date.fromisoformat(raw_date)
            except ValueError:
                raise ProviderError("去哪儿日历出发日期无效") from None
            if departure not in wanted or row.get("disabled") is True or row["price"] == "":
                continue
            if row.get("backDate"):
                raise ProviderError("去哪儿单程响应出现返程日期，未使用往返价格")
            raw_price = row["price"]
            if raw_price is None or isinstance(raw_price, bool):
                raise ProviderError("去哪儿返回无效价格")
            try:
                price = Decimal(str(raw_price))
            except InvalidOperation:
                raise ProviderError("去哪儿返回非数字价格") from None
            if not price.is_finite() or price < 0:
                raise ProviderError("去哪儿返回无效价格")
            if price == 0:
                continue
            url = "https://flight.qunar.com/twell/flight/Search.jsp?" + urllib.parse.urlencode({
                "fromCity": origin_name, "toCity": destination_name,
                "fromDate": departure.isoformat(), "searchType": "OnewayFlight",
            })
            quote = Quote(
                origin=route.origin, destination=route.destination, departure_date=departure,
                price=price, currency="CNY", source="去哪儿低价日历", provider="qunar",
                price_basis="total" if international else "base",
                price_note=TOTAL_PRICE_NOTE if international else BASE_PRICE_NOTE,
                flight_number=str(row.get("code") or ""), url=url,
            )
            if departure not in quotes or price < quotes[departure].price:
                quotes[departure] = quote
        warnings = []
        if not international and quotes:
            warnings.append("去哪儿国内日历为未含税票面价，仅供参考，不参与最低总价及阈值提醒")
        missing = wanted - set(quotes)
        if missing:
            warnings.append(f"{len(missing)} 个出发日期暂无去哪儿日历报价；可能尚无缓存，不代表没有航班")
        return SearchResult([quotes[day] for day in sorted(quotes)], warnings)
