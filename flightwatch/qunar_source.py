"""Anonymous Qunar city suggestions and public flight-price calendar.

Checked against live requests and the JavaScript loaded by
https://flight.qunar.com/site/ on 2026-09-08. The home page calls priceCalendar
with Chinese city searchParam values, priceType=1 domestic / 2 international,
days="" and reads data.gflights[].price. It labels international calendar
values as tax-inclusive, and domestic values as tax-exclusive. These are cached
indicative prices, not currently bookable quotes. We preserve that distinction.

Airport-scoped searches use the official domestic and international flight
lists instead of the city calendar. Their contracts and live access limitations
were checked against the site's JavaScript on 2026-09-14; see
docs/qunar-airport-source.md. No login token or anti-bot signature is generated.

Qunar and Ctrip city codes are not identical (北京 BJS -> Qunar PEK; 西安 SIA
-> Qunar XIY). Suggestions must identify the same city before calendar lookup;
we never blindly select the first suggest result or substitute a nearby city.
"""

from __future__ import annotations

from dataclasses import replace
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
INTERNATIONAL_FLIGHTS_ENDPOINT = "https://flight.qunar.com/touch/api/inter/wwwsearch"
DOMESTIC_FLIGHTS_ENDPOINT = "https://flight.qunar.com/touch/api/domestic/wbdflightlist"
_MAX_FLIGHT_POLLS = 4
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

    def _request(self, endpoint: str, params: dict, *, post: bool = False) -> dict:
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
            encoded = urllib.parse.urlencode(params)
            url = endpoint if post else endpoint + "?" + encoded
            request = urllib.request.Request(url, data=encoded.encode() if post else None, headers={
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
        # The same official suggest response identifies a city's included
        # airports. Names are useful for the domestic list, which sometimes
        # returns airport names instead of IATA codes. Do not use nearby rows.
        airports = {}
        for row in suggestions["places"]:
            search = row.get("suggestSearch") or {}
            if not isinstance(search, dict):
                continue
            airport = search.get("airportCode")
            if (row.get("type") in (2, 7) and row.get("canSearch") is True
                    and row.get("makeGray") is not True
                    and search.get("searchParam") == result["name"]
                    and search.get("isInter") is result["is_international"]
                    and (not known or not known.get("country")
                         or search.get("countryName") == known["country"])
                    and isinstance(airport, str) and _CODE.fullmatch(airport)
                    and row.get("code") == airport
                    and isinstance(row.get("airportName"), str) and row["airportName"]):
                airports[airport] = row["airportName"]
        result["airports"] = airports
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
        airport_scope = route.origin_scope == "airport" or route.destination_scope == "airport"
        origin_code = route.city_code("origin") if airport_scope else route.origin
        destination_code = route.city_code("destination") if airport_scope else route.destination
        if not origin_code or not destination_code:
            raise ProviderUnsupported("去哪儿机场查询需要保留机场所属城市，请重新选择城市和机场")
        origin, destination = self._resolve_city(origin_code), self._resolve_city(destination_code)
        international = origin["is_international"] or destination["is_international"]
        if international != (route.market == "international"):
            raise ProviderUnsupported("去哪儿识别的国内国际范围与所选航线不一致")
        if airport_scope:
            try:
                return self._search_airports(route, sorted(wanted), origin, destination)
            except ProviderError as exc:
                if not international or (self.cancelled and self.cancelled()):
                    raise
                # One public calendar request covers every selected date. Keep
                # its city scope explicit, outside the airport quote channel.
                try:
                    fallback = self._city_references(route, wanted, origin, destination)
                except ProviderError as fallback_error:
                    raise ProviderError(f"{exc}；城市参考查询也未取得结果：{fallback_error}") from None
                if not fallback.city_references:
                    raise ProviderError(f"{exc}；所选日期也暂无城市日历参考报价") from None
                fallback.warnings.insert(0, str(exc))
                return fallback
        payload = self._request(ENDPOINT, {
            "dep": origin["name"], "arr": destination["name"],
            "days": "", "priceType": 2 if international else 1,
        })
        return self._parse(payload, route, wanted, origin["name"], destination["name"])

    def _city_references(self, route: Route, wanted: set[date], origin: dict,
                         destination: dict) -> SearchResult:
        city_route = replace(route, origin=route.city_code("origin"),
                             destination=route.city_code("destination"),
                             origin_scope="city", destination_scope="city")
        payload = self._request(ENDPOINT, {
            "dep": origin["name"], "arr": destination["name"], "days": "", "priceType": 2,
        })
        parsed = self._parse(payload, city_route, wanted, origin["name"], destination["name"])
        note = ("去哪儿城市日历含税参考价，1 成人单程；未确认所选机场，"
                "不参与本行程最低价或微信提醒；缓存与最终可售价可能不同")
        references = [replace(q, price_basis="unknown", price_note=note,
                              url=self._flight_url(city_route, q.departure_date, origin,
                                                   destination, q.flight_number))
                      for q in parsed.quotes]
        return SearchResult([], [note, *parsed.warnings], city_references=references)

    @staticmethod
    def _flight_url(route: Route, departure: date, origin: dict, destination: dict,
                    flight_code: str) -> str:
        # The list's selected-flight parameter preserves the provider journey
        # identifier. Final inventory and checkout price are confirmed there.
        params = {"searchDepartureAirport": origin["name"],
                  "searchArrivalAirport": destination["name"],
                  "searchDepartureTime": departure.isoformat(),
                  "fromCode": origin["code"], "toCode": destination["code"],
                  "adultNum": 1, "childNum": 0, "startSearch": "true",
                  "filterFlightCode": flight_code}
        suffix = "_inter" if route.market == "international" else ""
        return (f"https://flight.qunar.com/site/oneway_list{suffix}.htm?"
                + urllib.parse.urlencode(params))

    def _search_airports(self, route: Route, wanted: list[date], origin: dict,
                         destination: dict) -> SearchResult:
        quotes, warnings = [], []
        for departure in wanted:
            try:
                if route.market == "international":
                    result = self._international_day(route, departure, origin, destination)
                else:
                    payload = self._request(DOMESTIC_FLIGHTS_ENDPOINT, {
                        "departureCity": origin["name"], "arrivalCity": destination["name"],
                        "departureDate": departure.isoformat(), "ex_track": "3w",
                    }, post=True)
                    result = self._parse_domestic_flights(payload, route, departure, origin, destination)
            except ProviderError as exc:
                if not quotes:
                    # A blocked/malformed first response will not improve by
                    # sending the same request for another 30 dates.
                    raise
                warnings.append(f"{departure}：{exc}；其余日期未继续查询")
                break
            quotes.extend(result.quotes)
            warnings.extend(result.warnings)
        return SearchResult(quotes, warnings)

    def _international_day(self, route: Route, departure: date, origin: dict,
                           destination: dict) -> SearchResult:
        params = {"depCity": origin["name"], "arrCity": destination["name"],
                  "depDate": departure.isoformat(), "adultNum": 1, "childNum": 0,
                  "ex_track": "3w", "from": "fi_re_search"}
        if route.airport_code("origin"):
            params["depAirport"] = route.airport_code("origin")
        if route.airport_code("destination"):
            params["arrAirport"] = route.airport_code("destination")
        rows = {}
        query_id = None
        for attempt in range(_MAX_FLIGHT_POLLS):
            payload = self._request(INTERNATIONAL_FLIGHTS_ENDPOINT, params)
            if any(payload.get(key) is True for key in ("isLimit", "needLogin", "needSlider")):
                raise ProviderError("去哪儿航班列表要求登录或验证，匿名查询本次未取得报价")
            if type(payload.get("status")) is not int or payload["status"] != 0:
                raise ProviderError("去哪儿航班列表未确认查询成功")
            result = payload.get("result")
            control = result.get("ctrlInfo") if isinstance(result, dict) else None
            if not isinstance(control, dict):
                raise ProviderError("去哪儿航班列表缺少查询状态，未使用其价格")
            for side, expected in (("dep", origin), ("arr", destination)):
                echoed = control.get(side)
                if not isinstance(echoed, dict) or echoed.get("cityZh") != expected["name"]:
                    raise ProviderError("去哪儿机场列表回显了其他城市，已拒绝错航线或过期缓存报价")
            current_id = control.get("queryId")
            if not isinstance(current_id, str) or not current_id or len(current_id) > 300:
                raise ProviderError("去哪儿航班列表缺少有效查询标识")
            if query_id is not None and current_id != query_id:
                raise ProviderError("去哪儿航班列表轮询标识改变，未合并不同查询价格")
            query_id = current_id
            flights = result.get("flightPrices")
            if isinstance(flights, list):
                flights = {str(index): item for index, item in enumerate(flights)}
            if not isinstance(flights, dict) or not all(isinstance(item, dict) for item in flights.values()):
                raise ProviderError("去哪儿逐航班价格列表结构已变化")
            # Later deltas replace updated journey prices, never keep a
            # superseded cheaper price for the same flight.
            for item in flights.values():
                journey = item.get("journey")
                if not isinstance(journey, dict) or not isinstance(journey.get("flightCode"), str):
                    raise ProviderError("去哪儿逐航班缺少行程身份")
                rows[journey["flightCode"]] = item
            if control.get("completed") is True:
                return self._parse_international_flights(list(rows.values()), route, departure,
                                                         origin, destination)
            if control.get("completed") is not False:
                raise ProviderError("去哪儿航班列表缺少完成状态")
            params["queryId"] = query_id
            interval = control.get("interval", 1000)
            if type(interval) not in (int, float) or not 0 <= interval <= 5000:
                raise ProviderError("去哪儿航班列表轮询间隔无效")
            if self.cancelled is not None and self.cancelled():
                raise ProviderError("监控已停止，取消去哪儿后续查询")
            if attempt + 1 < _MAX_FLIGHT_POLLS:
                time.sleep(interval / 1000)
        raise ProviderError("去哪儿机场列表在本轮时限内未完成，未把部分价格当作最低价")

    @staticmethod
    def _money(value) -> Decimal:
        if value is None or isinstance(value, bool):
            raise ProviderError("去哪儿航班价格字段无效")
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            raise ProviderError("去哪儿航班价格字段无效") from None
        if not amount.is_finite() or amount <= 0:
            raise ProviderError("去哪儿航班价格字段无效")
        return amount

    def _parse_international_flights(self, rows: list[dict], route: Route, departure: date,
                                     origin: dict, destination: dict) -> SearchResult:
        quotes, excluded = [], 0
        for row in rows:
            journey, price = row.get("journey"), row.get("price")
            if not isinstance(journey, dict) or not isinstance(price, dict):
                raise ProviderError("去哪儿国际航班行程或价格结构已变化")
            trips = journey.get("trips")
            if journey.get("journeyType") != "ONEWAY" or not isinstance(trips, list) or len(trips) != 1:
                excluded += 1
                continue
            trip = trips[0]
            segments = trip.get("flightSegments") if isinstance(trip, dict) else None
            if not isinstance(segments, list) or not segments or not all(isinstance(s, dict) for s in segments):
                raise ProviderError("去哪儿国际航班缺少可验证的逐段机场")
            first, last = segments[0], segments[-1]
            dep, arr = first.get("depAirportCode"), last.get("arrAirportCode")
            if not all(isinstance(code, str) and _CODE.fullmatch(code) for code in (dep, arr)):
                raise ProviderError("去哪儿国际航班机场代码无效")
            if (first.get("depDate") != departure.isoformat()
                    or first.get("depCityCode") != origin["code"]
                    or last.get("arrCityCode") != destination["code"]
                    or (route.airport_code("origin") and dep != route.airport_code("origin"))
                    or (route.airport_code("destination") and arr != route.airport_code("destination"))):
                excluded += 1
                continue
            if journey.get("ticketInsufficient") is True:
                excluded += 1
                continue
            # The website labels totalTaxType 1/2 as tax-inclusive; unknown
            # tax types are not silently promoted to comparable totals.
            total_tax_type = price.get("totalTaxType")
            basis = "total" if type(total_tax_type) is int and total_tax_type in (1, 2) else "unknown"
            if price.get("currencyCode") != "CNY":
                excluded += 1
                continue
            amount = self._money(price.get("lowTotalPrice"))
            quotes.append(Quote(
                origin=route.origin, destination=route.destination, departure_date=departure,
                price=amount, currency="CNY", provider="qunar", source="去哪儿国际航班列表",
                flight_number=str(journey.get("code") or ""),
                airline=" / ".join(dict.fromkeys(str(s.get("carrierShortName") or "") for s in segments)),
                origin_airport=dep, destination_airport=arr, price_basis=basis,
                price_note=("去哪儿逐航班含税参考总价，1 成人单程；已核验起降机场和出发日期，最终售价及行李以购票页为准"
                            if basis == "total" else "去哪儿逐航班价格未确认含税，不参与最低总价及阈值提醒"),
                url=self._flight_url(route, departure, origin, destination, journey.get("flightCode", "")),
            ))
        warnings = []
        if excluded:
            warnings.append(f"{departure}：去哪儿已排除 {excluded} 条机场、日期、单程或币种不匹配的报价")
        if not quotes:
            warnings.append(f"{departure}：去哪儿本次列表暂无符合所选机场的可验证报价，不代表没有航班")
        return SearchResult(quotes, warnings)

    def _parse_domestic_flights(self, payload: dict, route: Route, departure: date,
                               origin: dict, destination: dict) -> SearchResult:
        if any(payload.get(key) is True for key in ("needLogin", "needSlider", "isLimit")):
            raise ProviderError("去哪儿国内航班列表要求登录或验证，匿名查询本次未取得报价")
        data = payload.get("data")
        if payload.get("ret") is not True or not isinstance(data, dict) or not isinstance(data.get("flights"), list):
            raise ProviderError("去哪儿国内逐航班列表结构已变化")
        if not data["flights"]:
            # Observed anonymous response has ret:true, code:-1, empty data.
            # There is no city/date echo; it does NOT prove zero inventory.
            raise ProviderError("去哪儿国内机场列表未返回可验证的航线和航班，可能需要网站验证；不能据此判断没有航班")
        quotes, excluded = [], 0
        for row in data["flights"]:
            if not isinstance(row, dict):
                raise ProviderError("去哪儿国内逐航班数据无效")
            if row.get("flightType") == "list":
                first = last = row.get("binfo")
            elif row.get("flightType") == "listMore":
                first, last = row.get("binfo1"), row.get("binfo2")
            else:
                excluded += 1
                continue
            if not isinstance(first, dict) or not isinstance(last, dict):
                raise ProviderError("去哪儿国内逐航班缺少机场和日期")
            if (first.get("depDate") != departure.isoformat() or first.get("depCity") != origin["name"]
                    or last.get("arrCity") != destination["name"]):
                excluded += 1
                continue
            # Airport names must be an exact, unique match in the official
            # city suggestions; never infer an airport from its city code.
            dep_codes = [code for code, name in origin.get("airports", {}).items() if name == first.get("depAirport")]
            arr_codes = [code for code, name in destination.get("airports", {}).items() if name == last.get("arrAirport")]
            if len(dep_codes) != 1 or len(arr_codes) != 1:
                excluded += 1
                continue
            dep, arr = dep_codes[0], arr_codes[0]
            if ((route.airport_code("origin") and dep != route.airport_code("origin"))
                    or (route.airport_code("destination") and arr != route.airport_code("destination"))):
                excluded += 1
                continue
            amount = self._money(row.get("minPrice"))
            quotes.append(Quote(
                origin=route.origin, destination=route.destination, departure_date=departure,
                price=amount, currency="CNY", provider="qunar", source="去哪儿国内航班列表",
                flight_number=str(row.get("code") or ""), origin_airport=dep, destination_airport=arr,
                price_basis="base", price_note="去哪儿国内逐航班票面价，1 成人单程；已按实际机场筛选，未确认税费，不参与最低总价及阈值提醒",
                url=self._flight_url(route, departure, origin, destination, str(row.get("code") or "")),
            ))
        warnings = [f"{departure}：去哪儿国内列表未确认含税总价，仅参考展示，不参与最低总价及阈值提醒"]
        if excluded:
            warnings.append(f"{departure}：去哪儿已排除 {excluded} 条无法确认日期或机场的记录")
        return SearchResult(quotes, warnings)

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
