"""Anonymous exact-date Trip.com flight search.

The public Trip.com flight-list frontend posts the user's search to the
official SSE endpoint below.  The request needs no account, API key, cookie or
browser-created token.  We accept a fare only after the response itself
confirms the city pair, departure date, one-way journey, one adult economy
pricing, CNY currency and a coherent tax-inclusive total.

Observed official sources (2026-09-14):
https://www.trip.com/flights/
https://www.trip.com/restapi/soa2/27015/FlightListSearchSSE
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult


ENDPOINT = "https://www.trip.com/restapi/soa2/27015/FlightListSearchSSE"
_HOST = "www.trip.com"
_PATH = "/restapi/soa2/27015/FlightListSearchSSE"
_MAX_RESPONSE_BYTES = 6_000_000
_MAX_DATES = 31
_DATE_WORKERS = 4
_CODE = re.compile(r"[A-Z]{3}\Z")
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
PRICE_NOTE = (
    "Trip.com 公开搜索的1成人经济舱单程含税总价；已核对票价、税费与总价。"
    "行李、可选服务和支付方式费用以最终预订页为准"
)


class _AccessBlocked(ProviderError):
    """Access control should stop queued date requests."""


def _dict(value, message: str) -> dict:
    if not isinstance(value, dict):
        raise ProviderError(message)
    return value


def _list(value, message: str) -> list:
    if not isinstance(value, list):
        raise ProviderError(message)
    return value


def _number(value, message: str, *, allow_zero: bool = False) -> Decimal:
    if type(value) not in {int, float, Decimal}:
        raise ProviderError(message)
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ProviderError(message) from None
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0) or result > Decimal("10000000"):
        raise ProviderError(message)
    return result


def _timestamp(value, message: str) -> datetime:
    if not isinstance(value, str):
        raise ProviderError(message)
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise ProviderError(message) from None


def _same_endpoint(actual: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(actual)
        return (
            parsed.scheme == "https"
            and parsed.hostname == _HOST
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path == _PATH
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _decode_sse(value: str) -> dict:
    """Decode SSE data fields without executing the returned page/script."""
    frames = []
    for block in value.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        parts = []
        for line in block.split("\n"):
            if line.startswith("data:"):
                parts.append(line[5:].lstrip(" "))
            elif line and not line.startswith(":") and not line.startswith(("event:", "id:", "retry:")):
                raise ProviderError("Trip.com SSE 响应包含未知字段，未解析含糊数据")
        if parts:
            raw = "\n".join(parts)

            def reject_constant(_value):
                raise ValueError("invalid number")

            def unique_object(pairs):
                result = {}
                for key, item_value in pairs:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = item_value
                return result

            try:
                item = json.loads(raw, parse_float=Decimal, parse_constant=reject_constant,
                                  object_pairs_hook=unique_object)
            except (ValueError, RecursionError, InvalidOperation):
                raise ProviderError("Trip.com SSE 返回无效 JSON，接口可能已变化") from None
            if not isinstance(item, dict):
                raise ProviderError("Trip.com SSE 数据根节点不是对象")
            frames.append(item)
    if not frames:
        raise ProviderError("Trip.com 未返回完整 SSE 航班数据")
    return frames[-1]


class TripComProvider:
    def __init__(self, timeout: float = 30, request_delay: float = 1.0,
                 max_requests: int = 60, *, cancelled=None):
        self.timeout = timeout
        self.request_delay = request_delay
        self.max_requests = max_requests
        self.cancelled = cancelled
        self.requests_used = 0
        self._last_request: float | None = None
        self._request_lock = threading.Lock()
        self._request_context = threading.local()

    @classmethod
    def official_url(cls, route: Route, departure: date) -> str:
        """Build the official one-way economy search page used for rechecking."""
        values = {
            "dcity": route.city_code("origin").lower(),
            "acity": route.city_code("destination").lower(),
            "ddate": departure.isoformat(),
            "triptype": "ow",
            "class": "y",
            "lowpricesource": "searchform",
        }
        if route.airport_code("origin"):
            values["dairport"] = route.airport_code("origin").lower()
        if route.airport_code("destination"):
            values["aairport"] = route.airport_code("destination").lower()
        query = urllib.parse.urlencode(values)
        return "https://www.trip.com/flights/showfarefirst?" + query

    @staticmethod
    def _payload(route: Route, departure: date) -> dict:
        return {
            "mode": 0,
            "searchCriteria": {
                "grade": 1,
                "realGrade": 1,
                "tripType": 1,
                "journeyNo": 1,
                "passengerInfoType": {"adultCount": 1, "childCount": 0, "infantCount": 0},
                "journeyInfoTypes": [{
                    "journeyNo": 1,
                    "departDate": departure.isoformat(),
                    "departCode": route.city_code("origin"),
                    "arriveCode": route.city_code("destination"),
                    "departAirport": route.airport_code("origin"),
                    "arriveAirport": route.airport_code("destination"),
                }],
                "policyId": None,
            },
            "sortInfoType": {"direction": True, "orderBy": "Price", "topList": []},
            "tagList": [],
            "flagList": ["NEED_RESET_SORT"],
            "filterType": {
                # Explicitly keep the student selector off.  The same anonymous
                # request with true returned a lower, smaller result set in a
                # live comparison, so treating true as a normal adult search
                # could leak an eligibility fare into the alert.
                "filterFlagTypes": [], "queryItemSettings": [], "studentsSelectedStatus": False,
            },
            "abtList": [],
            "head": {
                "cid": "", "ctok": "", "cver": "3", "lang": "01", "sid": "8888",
                "syscode": "40", "auth": "", "xsid": "",
                "extension": [
                    {"name": "source", "value": "ONLINE"},
                    {"name": "sotpGroup", "value": "Trip"},
                    {"name": "sotpLocale", "value": "zh-CN"},
                    {"name": "sotpCurrency", "value": "CNY"},
                    {"name": "useDistributionType", "value": "1"},
                ],
                "Locale": "zh-CN", "Language": "zh", "Currency": "CNY",
                "ClientID": "", "appid": "700020",
            },
        }

    def _check_cancelled(self) -> None:
        batch = getattr(self._request_context, "cancelled", None)
        if ((self.cancelled is not None and self.cancelled())
                or (batch is not None and batch())):
            raise _AccessBlocked("监控已停止，取消 Trip.com 后续查询")

    def _request(self, payload: dict) -> str:
        self._check_cancelled()
        with self._request_lock:
            self._check_cancelled()
            if self.requests_used >= self.max_requests:
                raise _AccessBlocked("Trip.com 查询达到本轮请求上限")
            if self._last_request is not None:
                remaining = self.request_delay - (time.monotonic() - self._last_request)
                while remaining > 0:
                    time.sleep(min(remaining, 0.1))
                    self._check_cancelled()
                    remaining = self.request_delay - (time.monotonic() - self._last_request)
            self._check_cancelled()
            self.requests_used += 1
            self._last_request = time.monotonic()
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/event-stream",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if not _same_endpoint(response.url):
                    raise _AccessBlocked("Trip.com 查询跳转到登录或验证地址，本轮停止")
                content_type = response.headers.get_content_type()
                if content_type != "text/event-stream":
                    raise _AccessBlocked("Trip.com 未返回 SSE 航班数据，可能要求验证，本轮停止")
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ProviderError("Trip.com 航班响应超过大小限制")
            self._check_cancelled()
            return raw.decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Trip.com also uses the non-standard 430 status for automated
            # access throttling.  Treat it like the other access-control
            # responses so a multi-date search stops after its first probe.
            if exc.code in {401, 403, 429, 430}:
                raise _AccessBlocked(f"Trip.com 返回 HTTP {exc.code}，本轮停止且不绕过验证") from None
            raise ProviderError(f"Trip.com 返回 HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as exc:
            raise ProviderError(f"Trip.com 网络查询失败（{type(exc).__name__}）") from None
        except UnicodeDecodeError:
            raise ProviderError("Trip.com SSE 响应编码异常") from None

    def search(self, route: Route, today: date) -> SearchResult:
        self._check_cancelled()
        if route.currency != "CNY":
            raise ProviderUnsupported("Trip.com 当前适配仅核验 CNY 人民币报价")
        if (route.origin_scope not in {"city", "airport"}
                or route.destination_scope not in {"city", "airport"}
                or not isinstance(route.origin, str) or not _CODE.fullmatch(route.origin)
                or not isinstance(route.destination, str) or not _CODE.fullmatch(route.destination)
                or not _CODE.fullmatch(route.city_code("origin"))
                or not _CODE.fullmatch(route.city_code("destination"))
                or route.city_code("origin") == route.city_code("destination")):
            raise ProviderUnsupported("Trip.com 需要两个不同地点及其有效三字城市代码")
        if route.stay_nights is not None or route.nonstop or route.travel_class != 1:
            raise ProviderUnsupported("Trip.com 当前适配仅支持1成人经济舱单程，暂不支持往返或直飞过滤")
        if route.market not in {"domestic", "international"}:
            raise ProviderUnsupported("Trip.com 需要明确国内或国际航线")
        wanted = route.departure_dates(today)
        if not wanted:
            return SearchResult([], ["配置中没有尚未过期的出发日期"])
        if len(wanted) > _MAX_DATES:
            raise ProviderUnsupported("Trip.com 单次最多查询31个出发日期")

        stop = threading.Event()

        def query_day(departure: date):
            self._request_context.cancelled = stop.is_set
            try:
                payload = self._payload(route, departure)
                result = self._parse_response(self._request(payload), route, departure,
                                              self.official_url(route, departure))
                self._check_cancelled()
                return result
            except (_AccessBlocked, ProviderUnsupported):
                stop.set()
                raise
            except ProviderError as exc:
                return exc
            finally:
                try:
                    del self._request_context.cancelled
                except AttributeError:
                    pass

        # Validate one date before opening a bounded parallel batch.  A block,
        # route mismatch or market mismatch therefore launches no extra dates.
        first = query_day(wanted[0])
        # A malformed/error response on the validation probe is endpoint-wide
        # until proven otherwise.  Avoid repeating the same slow failure for
        # every requested date.  A legitimate empty day is a SearchResult and
        # still permits the remaining exact dates to run.
        if isinstance(first, ProviderError):
            stop.set()
            raise first
        outcomes = {wanted[0]: first}
        remaining = wanted[1:]
        if remaining:
            futures = {}
            with ThreadPoolExecutor(max_workers=min(_DATE_WORKERS, len(remaining)),
                                    thread_name_prefix="trip-date") as pool:
                for departure in remaining:
                    futures[pool.submit(query_day, departure)] = departure
                try:
                    for future in as_completed(futures):
                        outcomes[futures[future]] = future.result()
                except BaseException:
                    stop.set()
                    for future in futures:
                        future.cancel()
                    raise

        quotes, warnings, completed = [], [], 0
        for departure in wanted:
            result = outcomes[departure]
            if isinstance(result, ProviderError):
                warnings.append(f"{departure.isoformat()}：{result}")
                continue
            quotes.extend(result.quotes)
            warnings.extend(result.warnings)
            completed += 1
        if not completed:
            raise ProviderError("；".join(warnings))
        warnings.append("Trip.com 每个日期仅返回已通过路线、日期、舱位、乘客数、币种和含税总价核对的最低报价")
        return SearchResult(quotes, warnings)

    def _parse_response(self, raw: str, route: Route, departure: date, url: str) -> SearchResult:
        data = _decode_sse(raw)
        status = _dict(data.get("ResponseStatus"), "Trip.com 缺少响应状态")
        errors = status.get("Errors")
        head = _dict(data.get("head"), "Trip.com 缺少响应头状态")
        if errors != [] or head.get("retCode") != "SUCCESS":
            raise ProviderError("Trip.com 查询未成功，未将错误响应当作无航班")
        basic = _dict(data.get("basicInfo"), "Trip.com 缺少查询与币种信息")
        if basic.get("currency") != "CNY":
            raise ProviderError("Trip.com 未确认报价币种为 CNY")
        condition = _dict(basic.get("searchCondition"), "Trip.com 缺少查询条件回显")
        journeys = _list(condition.get("searchJourneys"), "Trip.com 缺少查询行程回显")
        if (condition.get("orderBy") != "Price" or condition.get("direction") is not True
                or len(journeys) != 1 or condition.get("selectJourneyList") != []):
            raise ProviderError("Trip.com 未确认单程价格排序查询")
        query = _dict(journeys[0], "Trip.com 查询行程结构已变化")
        origin = _dict(query.get("departCity"), "Trip.com 缺少出发城市回显")
        destination = _dict(query.get("arriveCity"), "Trip.com 缺少到达城市回显")
        if (query.get("journeyNo") != 1 or query.get("departDate") != departure.isoformat()
                or origin.get("code") != route.city_code("origin")
                or destination.get("code") != route.city_code("destination")):
            raise ProviderError("Trip.com 回显路线或日期与本次查询不一致")
        for side, key in (("origin", "departAirport"), ("destination", "arriveAirport")):
            expected_airport = route.airport_code(side)
            echoed = query.get(key)
            if expected_airport:
                if not isinstance(echoed, dict) or echoed.get("code") != expected_airport:
                    raise ProviderError("Trip.com 未回显所选具体机场，拒绝使用城市范围价格")
            elif echoed not in (None, {}):
                raise ProviderError("Trip.com 意外回显机场筛选，查询范围与路线不一致")
        origin_region, destination_region = origin.get("region"), destination.get("region")
        if (not isinstance(origin_region, str) or not origin_region
                or not isinstance(destination_region, str) or not destination_region
                or basic.get("regionRoute") != f"{origin_region}-{destination_region}"):
            raise ProviderError("Trip.com 缺少一致的航线国家地区信息")
        domestic = origin_region == "CN" and destination_region == "CN"
        if (route.market == "domestic") != domestic:
            raise ProviderUnsupported("Trip.com 城市所属国家地区与航线类型不符")
        origin_airports = origin.get("airportList")
        destination_airports = destination.get("airportList")
        if (not isinstance(origin_airports, list) or not origin_airports
                or not all(isinstance(code, str) and _CODE.fullmatch(code) for code in origin_airports)
                or not isinstance(destination_airports, list) or not destination_airports
                or not all(isinstance(code, str) and _CODE.fullmatch(code) for code in destination_airports)):
            raise ProviderError("Trip.com 未返回城市所属机场，无法核对票价路线")

        rows = _list(data.get("itineraryList"), "Trip.com 缺少航班报价列表")
        count = basic.get("recordCount")
        original_count = basic.get("originalCount")
        if (type(count) is not int or count < 0 or count != len(rows)
                or type(original_count) is not int or original_count != count):
            raise ProviderError("Trip.com 航班数量与响应汇总不一致")
        if not rows:
            return SearchResult([], [f"{departure.isoformat()}：Trip.com 暂无可核实报价"])

        valid: list[Quote] = []
        all_totals: list[Decimal] = []
        excluded = 0
        restricted = 0
        for row in rows:
            row = _dict(row, "Trip.com 航班行结构已变化")
            row_journeys = _list(row.get("journeyList"), "Trip.com 航班行缺少行程")
            policies = _list(row.get("policies"), "Trip.com 航班行缺少票价政策")
            if len(row_journeys) != 1 or not policies:
                raise ProviderError("Trip.com 未确认每条结果为单程且带价格")
            journey = _dict(row_journeys[0], "Trip.com 航班行程结构已变化")
            segments = _list(journey.get("transSectionList"), "Trip.com 航班缺少分段信息")
            route_ok = bool(segments) and journey.get("journeyNo") == 1
            parsed_segments = []
            previous_end = None
            previous_airport = None
            for index, segment_value in enumerate(segments, 1):
                segment = _dict(segment_value, "Trip.com 航班分段结构已变化")
                start_point = _dict(segment.get("departPoint"), "Trip.com 航班缺少起飞机场")
                end_point = _dict(segment.get("arrivePoint"), "Trip.com 航班缺少到达机场")
                info = _dict(segment.get("flightInfo"), "Trip.com 航班缺少承运信息")
                start = _timestamp(segment.get("departDateTime"), "Trip.com 航班起飞时间无效")
                end = _timestamp(segment.get("arriveDateTime"), "Trip.com 航班到达时间无效")
                start_airport, end_airport = start_point.get("airportCode"), end_point.get("airportCode")
                if (segment.get("segmentNo") != index or segment.get("transportType") != "FLIGHT"
                        or not isinstance(start_airport, str) or not _CODE.fullmatch(start_airport)
                        or not isinstance(end_airport, str) or not _CODE.fullmatch(end_airport)
                        # Flight timestamps are local to different airports;
                        # crossing time zones or the date line can make the
                        # arrival clock earlier than departure.  Connection
                        # ordering remains comparable at the same airport.
                        or (previous_end is not None and start < previous_end)
                        or (previous_airport is not None and start_airport != previous_airport)):
                    route_ok = False
                if not isinstance(info.get("flightNo"), str) or not info["flightNo"].strip():
                    route_ok = False
                parsed_segments.append((segment, start_point, end_point, info, start, end))
                previous_end, previous_airport = end, end_airport
            if segments:
                first, last = parsed_segments[0], parsed_segments[-1]
                route_ok &= (
                    first[4].date() == departure
                    and first[1].get("cityCode") == route.city_code("origin")
                    and first[1].get("airportCode") in origin_airports
                    and (not route.airport_code("origin")
                         or first[1].get("airportCode") == route.airport_code("origin"))
                    and last[2].get("cityCode") == route.city_code("destination")
                    and last[2].get("airportCode") in destination_airports
                    and (not route.airport_code("destination")
                         or last[2].get("airportCode") == route.airport_code("destination"))
                )
            if not route_ok:
                excluded += len(policies)
                continue

            for policy_value in policies:
                policy = _dict(policy_value, "Trip.com 票价政策结构已变化")
                grades = _list(policy.get("gradeInfoList"), "Trip.com 票价缺少舱位回显")
                if (len(grades) != len(segments)
                        or {grade.get("segmentNo") for grade in grades if isinstance(grade, dict)}
                        != set(range(1, len(segments) + 1))
                        or any(not isinstance(grade, dict) or grade.get("journeyNo") != 1
                               or grade.get("grade") != 1 for grade in grades)
                        or not isinstance(policy.get("policyFlags"), list)
                        or "ECONOMY" not in policy["policyFlags"]):
                    raise ProviderError("Trip.com 票价未确认所有航段为经济舱")
                price_data = _dict(policy.get("price"), "Trip.com 票价缺少总价")
                adult = _dict(price_data.get("adult"), "Trip.com 票价缺少成人明细")
                total = _number(price_data.get("totalPrice"), "Trip.com 返回无效总价")
                average = _number(price_data.get("averagePrice"), "Trip.com 返回无效成人均价")
                total_tax = _number(price_data.get("totalTax"), "Trip.com 返回无效税费", allow_zero=True)
                sale = _number(adult.get("salePrice"), "Trip.com 返回无效成人票价", allow_zero=True)
                tax = _number(adult.get("tax"), "Trip.com 返回无效成人税费", allow_zero=True)
                discount = _number(adult.get("discount"), "Trip.com 返回无效优惠", allow_zero=True)
                adult_total = _number(adult.get("totalPrice"), "Trip.com 返回无效成人总价")
                if (total != average or total != adult_total or total_tax != tax
                        or sale + tax - discount != total):
                    raise ProviderError("Trip.com 成人票价、税费、优惠与含税总价不一致")
                all_totals.append(total)
                # The request itself is anonymous.  Still exclude any policy
                # that declares an account/payment/promotion eligibility rule;
                # otherwise a new-user, member, student or card-only fare could
                # incorrectly win the public minimum.  Unknown nonempty note
                # structures are also excluded instead of interpreted.
                flags = policy.get("policyFlags")
                tags = policy.get("tagList")
                if not isinstance(tags, list) or any(not isinstance(tag, dict) for tag in tags):
                    raise ProviderError("Trip.com 票价标签结构已变化")
                eligibility_words = (
                    "MEMBER", "LOGIN", "STUDENT", "YOUTH", "NEW_USER", "NEW_GUEST",
                    "FIRST_ORDER", "CREDIT_CARD", "BANK_CARD", "COUPON", "POINTS",
                    "LOYALTY", "SUBSCRIPTION",
                )
                labels = [str(value).upper() for value in flags]
                labels.extend(str(tag.get("key", "")).upper() for tag in tags)
                constrained = any(word in label for label in labels for word in eligibility_words)
                constrained |= any(policy.get(field) not in (None, [], {})
                                   for field in ("noteList", "preJourneyTagList", "sold"))
                constrained |= price_data.get("priceNoteList") not in (None, [])
                exchange_rate = policy.get("cnyExchangeRate")
                constrained |= (exchange_rate is not None and
                                (type(exchange_rate) not in {int, float, Decimal}
                                 or Decimal(str(exchange_rate)) != 1))
                if constrained:
                    restricted += 1
                    continue
                flights = [part[3]["flightNo"].strip() for part in parsed_segments]
                airlines = []
                for part in parsed_segments:
                    airline = part[3].get("airlineCode")
                    if isinstance(airline, str) and airline and airline not in airlines:
                        airlines.append(airline)
                valid.append(Quote(
                    origin=route.origin, destination=route.destination,
                    departure_date=departure, price=total, currency="CNY",
                    source="Trip.com 公开航班搜索", airline="/".join(airlines),
                    flight_number="/".join(flights), stops=len(segments) - 1,
                    url=url, price_note=PRICE_NOTE, provider="trip", price_basis="total",
                    origin_airport=first[1]["airportCode"],
                    destination_airport=last[2]["airportCode"],
                ))

        if not valid:
            if restricted:
                return SearchResult([], [f"{departure.isoformat()}：Trip.com 仅返回需资格或条件的票价，未参与最低价比较"])
            if route.airport_code("origin") or route.airport_code("destination"):
                return SearchResult([], [f"{departure.isoformat()}：Trip.com 未返回所选具体机场组合的可核实报价"])
            if excluded:
                raise ProviderError("Trip.com 所有带价航班均未通过路线、日期或中转链核对")
            return SearchResult([], [f"{departure.isoformat()}：Trip.com 暂无可核实报价"])
        lowest = min(valid, key=lambda quote: quote.price)
        summary = _dict(basic.get("lowestPrice"), "Trip.com 缺少最低价汇总")
        summary_total = _number(summary.get("totalPrice"), "Trip.com 最低价汇总无效")
        summary_tax = _number(summary.get("totalTax"), "Trip.com 最低税费汇总无效", allow_zero=True)
        airport_filtered = bool(route.airport_code("origin") or route.airport_code("destination"))
        if not all_totals:
            raise ProviderError("Trip.com 未找到可核对的路线票价")
        if not airport_filtered and min(all_totals) != summary_total:
            raise ProviderError("Trip.com 报价列表最低总价与响应汇总不一致")
        # There can be two policies with the same total and different taxes.
        # Require at least one matching lowest policy to carry the summary tax.
        matching_taxes = []
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("policies"), list):
                for policy in row["policies"]:
                    price = policy.get("price") if isinstance(policy, dict) else None
                    if isinstance(price, dict) and price.get("totalPrice") == summary.get("totalPrice"):
                        matching_taxes.append(price.get("totalTax"))
        if (not airport_filtered and not any(type(value) in {int, float, Decimal}
                                             and Decimal(str(value)) == summary_tax
                                             for value in matching_taxes)):
            raise ProviderError("Trip.com 最低价税费与报价明细不一致")
        notes = []
        if excluded:
            notes.append(f"{departure.isoformat()}：Trip.com 已排除 {excluded} 条路线、日期或中转链不匹配的报价")
        if restricted:
            notes.append(f"{departure.isoformat()}：Trip.com 已排除 {restricted} 条需会员、登录、特定身份、支付方式或其他资格的票价")
        return SearchResult([lowest], notes)
