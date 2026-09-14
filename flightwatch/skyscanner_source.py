"""Anonymous exact-date Skyscanner Radar flight search.

The regional public day-view resolves a user-facing IATA city code to the
city entity used by Skyscanner's current Acorn frontend.  The frontend's
official ``web-unified-search`` endpoint then returns incremental results.  We
send no account token, API key, Cookie, JHA value or session header, wait for a
``complete`` search context, and only then compare route/date-checked fares.

Observed official sources (2026-09-14):
https://www.skyscanner.com.sg/transport/flights/
https://www.skyscanner.com.sg/g/radar/api/v2/web-unified-search/
https://js.skyscnr.com/sttc/nx/web-platform/banana/static/js/FlightsDayView.42cd6b7f.chunk.js.map
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
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
import uuid

from .models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult


HOST = "www.skyscanner.com.sg"
ORIGIN = "https://" + HOST
RADAR_ENDPOINT = ORIGIN + "/g/radar/api/v2/web-unified-search/"
_RADAR_PATH = "/g/radar/api/v2/web-unified-search/"
_MAX_HTML_BYTES = 3_000_000
_MAX_JSON_BYTES = 8_000_000
_MAX_INTERNAL_BYTES = 1_500_000
_MAX_DATES = 31
_MAX_POLLS = 4
_DATE_WORKERS = 4
_CODE = re.compile(r"[A-Z]{3}\Z")
_ENTITY_ID = re.compile(r"[0-9]{1,20}\Z")
_SESSION_ID = re.compile(r"[A-Za-z0-9_=-]{1,1200}\Z")
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
PRICE_NOTE = (
    "Skyscanner 匿名公开搜索的1成人经济舱单程价格，页面说明价格含税费；"
    "部分行李、支付方式及供应商费用可能另计，成交价请到购票页确认"
)


class _AccessBlocked(ProviderError):
    """An access-control response should cancel queued searches."""


@dataclass(frozen=True)
class _Context:
    origin_entity: str
    destination_entity: str
    origin_country: str
    destination_country: str
    origin_raw: str
    destination_raw: str
    view_id: str
    origin_city_entity: str = ""
    destination_city_entity: str = ""

    def city_entity(self, side: str) -> str:
        value = getattr(self, f"{side}_city_entity")
        return value or getattr(self, f"{side}_entity")


class _ScriptParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_script = False
        self.scripts: list[str] = []
        self._parts: list[str] = []
        self.text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, _attrs):
        if tag in {"script", "style"}:
            self._skip += 1
        if tag == "script":
            self.in_script = True
            self._parts = []

    def handle_endtag(self, tag):
        if tag == "script" and self.in_script:
            self.scripts.append("".join(self._parts))
            self.in_script = False
            self._parts = []
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)

    def handle_data(self, value):
        if self.in_script:
            self._parts.append(value)
        elif not self._skip and value.strip():
            self.text.append(value.strip())


def _dict(value, message: str) -> dict:
    if not isinstance(value, dict):
        raise ProviderError(message)
    return value


def _list(value, message: str) -> list:
    if not isinstance(value, list):
        raise ProviderError(message)
    return value


def _number(value, message: str) -> Decimal:
    if type(value) not in {int, float, Decimal}:
        raise ProviderError(message)
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ProviderError(message) from None
    if not result.is_finite() or result <= 0 or result > Decimal("10000000"):
        raise ProviderError(message)
    return result


def _iso_time(value, message: str) -> datetime:
    if not isinstance(value, str):
        raise ProviderError(message)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ProviderError(message) from None
    if parsed.tzinfo is not None:
        raise ProviderError(message)
    return parsed


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _json_object(raw: str | bytes, message: str) -> dict:
    def reject_constant(_value):
        raise ValueError("invalid number")

    try:
        data = json.loads(raw, parse_float=Decimal, parse_constant=reject_constant,
                          object_pairs_hook=_unique_object)
    except (ValueError, UnicodeDecodeError, RecursionError, InvalidOperation):
        raise ProviderError(message) from None
    if not isinstance(data, dict):
        raise ProviderError(message)
    return data


def _extract_object(script: str, marker: str) -> str:
    """Extract one brace-balanced JS object; strings are never executed."""
    positions = [match.end() for match in re.finditer(re.escape(marker), script)]
    if len(positions) != 1:
        raise ProviderError("Skyscanner 页面缺少唯一查询上下文")
    start = positions[0]
    while start < len(script) and script[start].isspace():
        start += 1
    if start >= len(script) or script[start] != "{":
        raise ProviderError("Skyscanner 页面查询上下文结构已变化")
    depth, quote, escaped = 0, None, False
    for index in range(start, len(script)):
        char = script[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return script[start:index + 1]
            if depth < 0:
                break
    raise ProviderError("Skyscanner 页面查询上下文未完整结束")


def _replace_undefined_values(raw: str) -> str:
    """Replace only bare object values used by the server's JSON-like blob."""
    output, index, quote, escaped = [], 0, None, False
    while index < len(raw):
        char = raw[index]
        if quote is not None:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            output.append(char)
            index += 1
            continue
        if raw.startswith("undefined", index):
            before = "".join(output).rstrip()
            after = index + len("undefined")
            following = raw[after:].lstrip()
            if before.endswith(":") and following.startswith((",", "}")):
                output.append("null")
                index = after
                continue
            raise ProviderError("Skyscanner 页面包含无法安全解析的脚本值")
        output.append(char)
        index += 1
    return "".join(output)


def _query_dict(url: str) -> dict[str, str]:
    try:
        pairs = urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query,
                                      keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ProviderError("Skyscanner 官网链接参数无效") from None
    if len(pairs) != len({key for key, _value in pairs}):
        raise ProviderError("Skyscanner 官网链接含重复参数")
    return dict(pairs)


class SkyscannerProvider:
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

    @staticmethod
    def _date_code(departure: date) -> str:
        return departure.strftime("%y%m%d")

    @classmethod
    def search_url(cls, route: Route, departure: date, origin_raw: str | None = None,
                   destination_raw: str | None = None) -> str:
        origin = origin_raw or route.origin.lower()
        destination = destination_raw or route.destination.lower()
        path = f"/transport/flights/{origin}/{destination}/{cls._date_code(departure)}/"
        query = urllib.parse.urlencode({
            "adultsv2": "1", "cabinclass": "economy", "rtn": "0", "currency": "CNY",
        })
        return urllib.parse.urlunsplit(("https", HOST, path, query, ""))

    @classmethod
    def official_url(cls, route: Route, departure: date) -> str:
        return cls.search_url(route, departure)

    def _check_cancelled(self) -> None:
        batch = getattr(self._request_context, "cancelled", None)
        if ((self.cancelled is not None and self.cancelled())
                or (batch is not None and batch())):
            raise _AccessBlocked("监控已停止，取消 Skyscanner 后续查询")

    def _before_request(self) -> None:
        self._check_cancelled()
        with self._request_lock:
            self._check_cancelled()
            if self.requests_used >= self.max_requests:
                raise _AccessBlocked("Skyscanner 查询达到本轮请求上限")
            if self._last_request is not None:
                remaining = self.request_delay - (time.monotonic() - self._last_request)
                while remaining > 0:
                    time.sleep(min(remaining, 0.1))
                    self._check_cancelled()
                    remaining = self.request_delay - (time.monotonic() - self._last_request)
            self._check_cancelled()
            self.requests_used += 1
            self._last_request = time.monotonic()

    @staticmethod
    def _verify_response_url(actual: str, expected_kind: str) -> None:
        try:
            parsed = urllib.parse.urlsplit(actual)
            base_ok = (parsed.scheme == "https" and parsed.hostname == HOST
                       and parsed.port in (None, 443) and parsed.username is None
                       and parsed.password is None and not parsed.fragment)
            if expected_kind == "html":
                path_ok = bool(re.fullmatch(r"/transport/flights/[a-z0-9-]+/[a-z0-9-]+/[0-9]{6}/", parsed.path))
                query_ok = _query_dict(actual) == {
                    "adultsv2": "1", "cabinclass": "economy", "rtn": "0", "currency": "CNY",
                }
            elif expected_kind == "post":
                path_ok, query_ok = parsed.path == _RADAR_PATH, not parsed.query
            else:
                path_ok = parsed.path.startswith(_RADAR_PATH) and parsed.path != _RADAR_PATH
                query_ok = not parsed.query
            if not (base_ok and path_ok and query_ok):
                raise ValueError("wrong url")
        except (ValueError, ProviderError):
            raise _AccessBlocked("Skyscanner 查询跳转到登录、验证或无法核对的地址，本轮停止") from None

    def _open(self, request: urllib.request.Request, kind: str, limit: int,
              content_type: str) -> bytes:
        self._before_request()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self._verify_response_url(response.url, kind)
                if response.headers.get_content_type() != content_type:
                    raise _AccessBlocked("Skyscanner 未返回预期搜索数据，可能要求验证，本轮停止")
                body = response.read(limit + 1)
            if len(body) > limit:
                raise ProviderError("Skyscanner 搜索响应超过大小限制")
            self._check_cancelled()
            return body
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 429}:
                raise _AccessBlocked(f"Skyscanner 返回 HTTP {exc.code}，本轮停止且不绕过验证") from None
            raise ProviderError(f"Skyscanner 返回 HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as exc:
            raise ProviderError(f"Skyscanner 网络查询失败（{type(exc).__name__}）") from None

    def _request_page(self, url: str) -> str:
        request = urllib.request.Request(url, headers={
            "User-Agent": _USER_AGENT, "Accept": "text/html",
            "Accept-Language": "en-GB,en;q=0.9", "Accept-Encoding": "identity",
        })
        raw = self._open(request, "html", _MAX_HTML_BYTES, "text/html")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderError("Skyscanner 页面编码异常") from None

    @staticmethod
    def _api_headers(view_id: str) -> dict[str, str]:
        return {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "X-Skyscanner-ChannelId": "website",
            "X-Skyscanner-Consent-Adverts": "false",
            "X-Skyscanner-Market": "SG",
            "X-Skyscanner-Locale": "en-GB",
            "X-Skyscanner-Currency": "CNY",
            "X-Skyscanner-ViewId": view_id,
            # The current official defaultHeaders transform assigns both
            # headers from the same page viewId for the whole search session.
            "X-Skyscanner-TrustedFunnelId": view_id,
        }

    def _request_json(self, url: str, view_id: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(url, data=body,
                                         method="GET" if body is None else "POST",
                                         headers=self._api_headers(view_id))
        kind = "poll" if body is None else "post"
        raw = self._open(request, kind, _MAX_JSON_BYTES, "application/json")
        return _json_object(raw, "Skyscanner Radar 返回无效 JSON")

    def _parse_context(self, html: str, route: Route, departure: date) -> _Context:
        parser = _ScriptParser()
        try:
            parser.feed(html)
            parser.close()
        except (ValueError, RecursionError):
            raise ProviderError("Skyscanner HTML 结构异常") from None
        visible = " ".join(parser.text).lower()
        if any(term in visible for term in ("verify you are human", "access denied", "captcha", "not a robot")):
            raise _AccessBlocked("Skyscanner 要求人机验证，本轮停止且不绕过")
        marker = 'window["__internal"] = '
        scripts = [script for script in parser.scripts if marker in script]
        if len(scripts) != 1:
            raise _AccessBlocked("Skyscanner 匿名页未返回唯一搜索上下文，可能要求验证")
        raw = _extract_object(scripts[0], marker)
        if len(raw.encode("utf-8")) > _MAX_INTERNAL_BYTES:
            raise ProviderError("Skyscanner 页面查询上下文超过大小限制")
        state = _json_object(_replace_undefined_values(raw), "Skyscanner 页面查询上下文不是有效 JSON")
        culture = _dict(state.get("culture"), "Skyscanner 缺少地区与币种信息")
        user = _dict(state.get("userInfo"), "Skyscanner 缺少匿名用户状态")
        if (culture.get("currency") != "CNY" or culture.get("market") != "SG"
                or culture.get("locale") != "en-GB" or culture.get("tld") != "com.sg"
                or user.get("isLoggedIn") is not False):
            raise ProviderError("Skyscanner 未确认匿名 CNY 搜索环境")
        params = _dict(state.get("searchParams"), "Skyscanner 缺少搜索条件回显")
        legs = _list(params.get("legs"), "Skyscanner 缺少搜索航段回显")
        if (params.get("outboundDate") != departure.isoformat()
                or params.get("tripType") != "one-way" or params.get("cabinClass") != "economy"
                or params.get("adultsV2") != 1 or params.get("originalAdults") != 1
                or params.get("childrenV2") != [] or params.get("preferDirects") is not False
                or params.get("outboundAlts") is not False or params.get("inboundAlts") is not False
                or params.get("fareAttributes") != [] or len(legs) != 1):
            raise ProviderError("Skyscanner 回显日期、单程、舱位或乘客数与查询不符")
        origin = _dict(params.get("origin"), "Skyscanner 缺少出发地点身份")
        destination = _dict(params.get("destination"), "Skyscanner 缺少到达地点身份")
        leg = _dict(legs[0], "Skyscanner 搜索航段结构已变化")
        resolved = []
        for side, place in (("origin", origin), ("destination", destination)):
            entity, city_entity = place.get("entityId"), place.get("geoContainerId")
            kind, country, city_id = place.get("type"), place.get("countryId"), place.get("cityId")
            if (place.get("rawLocationId") != getattr(route, side).lower()
                    or not isinstance(entity, str) or not _ENTITY_ID.fullmatch(entity)
                    or not isinstance(city_entity, str) or not _ENTITY_ID.fullmatch(city_entity)
                    or kind not in {"City", "Airport"}
                    or not isinstance(country, str) or not country
                    or not isinstance(city_id, str) or not re.fullmatch(r"[A-Z0-9-]{3,12}", city_id)
                    or leg.get(f"{side}EntityId") != entity
                    or leg.get(f"{side}GeoContainerId") != city_entity
                    or leg.get(f"{side}Type") != kind
                    or leg.get(f"{side}CountryId") != country
                    or leg.get(f"{side}CityId") != city_id
                    or params.get(f"{side}Type") != kind
                    or params.get(f"{side}CityId") != city_id):
                raise ProviderError("Skyscanner 页面回显路线或地点身份不一致")
            if getattr(route, f"{side}_scope") == "airport":
                if (kind != "Airport" or place.get("airportId") != getattr(route, side)
                        or place.get("id") != getattr(route, side)
                        or params.get(f"{side}IataCode") != getattr(route, side)):
                    raise ProviderError("Skyscanner 未回显所选具体机场，拒绝使用城市范围价格")
                query_entity = entity
            else:
                # A city code can collide with an airport (SHA is both
                # Shanghai-all-airports and Hongqiao).  The official page may
                # resolve the raw URL to that airport, so city scope explicitly
                # promotes the request to its verified geoContainerId.
                query_entity = city_entity
            resolved.append((query_entity, city_entity, country, city_id.lower()))
        if leg.get("date") != departure.isoformat():
            raise ProviderError("Skyscanner 页面回显路线或日期与查询不一致")
        origin_entity, origin_city_entity, origin_country, origin_raw = resolved[0]
        destination_entity, destination_city_entity, destination_country, destination_raw = resolved[1]
        if (route.market == "domestic") != (origin_country == "CN" and destination_country == "CN"):
            raise ProviderUnsupported("Skyscanner 城市所属国家地区与航线类型不符")
        view_id = state.get("viewId")
        try:
            if str(uuid.UUID(view_id)) != view_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ProviderError("Skyscanner 页面缺少有效搜索关联标识") from None
        return _Context(origin_entity, destination_entity, origin_country, destination_country,
                        origin_raw, destination_raw, view_id,
                        origin_city_entity, destination_city_entity)

    @staticmethod
    def _payload(context: _Context, departure: date) -> dict:
        return {
            "cabinClass": "economy", "childAges": [], "adults": 1,
            "legs": [{
                "legOrigin": {"@type": "entity", "entityId": context.origin_entity},
                "legDestination": {"@type": "entity", "entityId": context.destination_entity},
                "dates": {"@type": "date", "year": str(departure.year),
                          "month": f"{departure.month:02d}", "day": f"{departure.day:02d}"},
            }],
        }

    @staticmethod
    def _merge_delta(data: dict, merged: dict[str, dict]) -> tuple[str, str | None]:
        context = _dict(data.get("context"), "Skyscanner Radar 缺少搜索状态")
        status, session_id = context.get("status"), context.get("sessionId")
        if status not in {"incomplete", "complete"}:
            raise ProviderError("Skyscanner Radar 搜索失败或状态未知")
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ProviderError("Skyscanner Radar 缺少有效搜索会话身份")
        itineraries = _dict(data.get("itineraries"), "Skyscanner Radar 缺少航班结果")
        results = _list(itineraries.get("results"), "Skyscanner Radar 航班列表结构已变化")
        for result in results:
            if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
                raise ProviderError("Skyscanner Radar 航班结果缺少唯一身份")
            merged[result["id"]] = result
        return status, session_id

    def _query_day(self, route: Route, departure: date, context: _Context) -> SearchResult:
        data = self._request_json(RADAR_ENDPOINT, context.view_id, self._payload(context, departure))
        merged: dict[str, dict] = {}
        status, session_id = self._merge_delta(data, merged)
        polls = 0
        while status != "complete" and polls < _MAX_POLLS:
            self._check_cancelled()
            encoded = urllib.parse.quote(session_id, safe="")
            data = self._request_json(RADAR_ENDPOINT + encoded, context.view_id)
            status, session_id = self._merge_delta(data, merged)
            polls += 1
        if status != "complete":
            raise ProviderError("Skyscanner Radar 在限定轮询内未完成，未使用不完整搜索价格")
        return self._parse_results(list(merged.values()), route, departure, context)

    def search(self, route: Route, today: date) -> SearchResult:
        self._check_cancelled()
        if route.currency != "CNY":
            raise ProviderUnsupported("Skyscanner 当前适配仅核验 CNY 人民币报价")
        if (route.origin_scope not in {"city", "airport"}
                or route.destination_scope not in {"city", "airport"}
                or not isinstance(route.origin, str) or not _CODE.fullmatch(route.origin)
                or not isinstance(route.destination, str) or not _CODE.fullmatch(route.destination)
                or not _CODE.fullmatch(route.city_code("origin"))
                or not _CODE.fullmatch(route.city_code("destination"))
                or route.city_code("origin") == route.city_code("destination")):
            raise ProviderUnsupported("Skyscanner 需要两个不同地点及其有效三字城市代码")
        if route.stay_nights is not None or route.nonstop or route.travel_class != 1:
            raise ProviderUnsupported("Skyscanner 当前适配仅支持1成人经济舱单程，暂不支持往返或直飞过滤")
        if route.market not in {"domestic", "international"}:
            raise ProviderUnsupported("Skyscanner 需要明确国内或国际航线")
        wanted = route.departure_dates(today)
        if not wanted:
            return SearchResult([], ["配置中没有尚未过期的出发日期"])
        if len(wanted) > _MAX_DATES:
            raise ProviderUnsupported("Skyscanner 单次最多查询31个出发日期")
        first_page_url = self.search_url(route, wanted[0])
        context = self._parse_context(self._request_page(first_page_url), route, wanted[0])
        stop = threading.Event()

        def query(departure):
            self._request_context.cancelled = stop.is_set
            try:
                result = self._query_day(route, departure, context)
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

        first = query(wanted[0])
        if isinstance(first, ProviderError):
            stop.set()
            raise first
        outcomes = {wanted[0]: first}
        remaining = wanted[1:]
        if remaining:
            futures = {}
            with ThreadPoolExecutor(max_workers=min(_DATE_WORKERS, len(remaining)),
                                    thread_name_prefix="skyscanner-date") as pool:
                for departure in remaining:
                    futures[pool.submit(query, departure)] = departure
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
        warnings.append("Skyscanner 仅在匿名 Radar 搜索完整结束后比较路线、日期和购票链接均通过核对的报价")
        return SearchResult(quotes, warnings)

    @staticmethod
    def _deep_link(value: str, agent: str, price: Decimal) -> str:
        if not isinstance(value, str) or len(value) > 12_000:
            raise ProviderError("Skyscanner 缺少有效官方购票链接")
        parsed = urllib.parse.urlsplit(value)
        parts = parsed.path.split("/")
        if (parsed.scheme or parsed.netloc or parsed.fragment
                or len(parts) < 11 or parts[:7] != ["", "transport_deeplink", "4.0", "SG", "en-GB", "CNY", agent]
                or parts[7] != "1" or parts[-3] != "air"
                or parts[-2] not in {"trava", "airli"} or parts[-1] != "flights"):
            raise ProviderError("Skyscanner 返回的购票链接不是本次官方 CNY 单程入口")
        query = _query_dict(value)
        if (query.get("passengers") != "1" or query.get("channel") != "website"
                or query.get("cabin_class") != "economy"
                or query.get("client_id") != "skyscanner_website"):
            raise ProviderError("Skyscanner 购票链接的乘客数、舱位或渠道不一致")
        ticket_text = query.get("ticket_price")
        if not isinstance(ticket_text, str) or not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,2})?", ticket_text):
            raise ProviderError("Skyscanner 购票链接缺少一致价格")
        try:
            ticket = Decimal(ticket_text)
        except InvalidOperation:
            raise ProviderError("Skyscanner 购票链接缺少一致价格") from None
        if ticket != price:
            raise ProviderError("Skyscanner 购票链接价格与报价不一致")
        return ORIGIN + value

    def _parse_results(self, rows: list, route: Route, departure: date,
                       context: _Context) -> SearchResult:
        valid, excluded, restricted = [], 0, 0
        for row_value in rows:
            row = _dict(row_value, "Skyscanner 航班行结构已变化")
            legs = _list(row.get("legs"), "Skyscanner 航班行缺少航段")
            options = _list(row.get("pricingOptions"), "Skyscanner 航班行缺少购票选项")
            if len(legs) != 1:
                raise ProviderError("Skyscanner 返回非单程航班结果")
            leg = _dict(legs[0], "Skyscanner 航段结构已变化")
            segments = _list(leg.get("segments"), "Skyscanner 航班缺少分段")
            stop_count = leg.get("stopCount")
            route_ok = bool(segments) and type(stop_count) is int and stop_count == len(segments) - 1
            parsed_segments = []
            previous_end, previous_entity = None, None
            for segment_value in segments:
                segment = _dict(segment_value, "Skyscanner 航班分段结构已变化")
                origin = _dict(segment.get("origin"), "Skyscanner 航班缺少起飞机场")
                destination = _dict(segment.get("destination"), "Skyscanner 航班缺少到达机场")
                start = _iso_time(segment.get("departure"), "Skyscanner 航班起飞时间无效")
                end = _iso_time(segment.get("arrival"), "Skyscanner 航班到达时间无效")
                # Segment endpoints use each airport's local clock.  A flight
                # across time zones/date line can arrive at an earlier local
                # time; only connection times at the same airport are directly
                # comparable.
                if ((previous_end is not None and start < previous_end)
                        or (previous_entity is not None and origin.get("entityId") != previous_entity)):
                    route_ok = False
                carrier = _dict(segment.get("marketingCarrier"), "Skyscanner 航班缺少承运人")
                if not isinstance(segment.get("flightNumber"), str) or not segment["flightNumber"]:
                    route_ok = False
                parsed_segments.append((segment, origin, destination, carrier, start, end))
                previous_end, previous_entity = end, destination.get("entityId")
            if parsed_segments:
                first, last = parsed_segments[0], parsed_segments[-1]
                first_parent = _dict(first[1].get("parent"), "Skyscanner 缺少出发机场所属城市")
                last_parent = _dict(last[2].get("parent"), "Skyscanner 缺少到达机场所属城市")
                origin_ok = (
                    first_parent.get("entityId") == context.city_entity("origin")
                    and first_parent.get("displayCode") == route.city_code("origin")
                )
                destination_ok = (
                    last_parent.get("entityId") == context.city_entity("destination")
                    and last_parent.get("displayCode") == route.city_code("destination")
                )
                if route.airport_code("origin"):
                    origin_ok &= (first[1].get("entityId") == context.origin_entity
                                  and first[1].get("displayCode") == route.airport_code("origin"))
                if route.airport_code("destination"):
                    destination_ok &= (last[2].get("entityId") == context.destination_entity
                                       and last[2].get("displayCode") == route.airport_code("destination"))
                route_ok &= (
                    first[4].date() == departure
                    and origin_ok and destination_ok
                    and first[1].get("countryId") == context.origin_country
                    and last[2].get("countryId") == context.destination_country
                    and _dict(leg.get("origin"), "Skyscanner 航段缺少起点").get("entityId") == first[1].get("entityId")
                    and _dict(leg.get("destination"), "Skyscanner 航段缺少终点").get("entityId") == last[2].get("entityId")
                    and leg.get("departure") == first[0].get("departure")
                    and leg.get("arrival") == last[0].get("arrival")
                )
            if not route_ok:
                excluded += len(options) or 1
                continue
            tags = row.get("tags")
            if (row.get("fareAttributes") not in (None, {})
                    or (tags is not None and
                        (not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags)))
                    or (isinstance(tags, list) and any(
                        word in tag.upper() for tag in tags
                        for word in ("MEMBER", "LOGIN", "LOYALTY", "SUBSCRIPTION")
                    ))):
                restricted += len(options) or 1
                continue
            top_price = _dict(row.get("price"), "Skyscanner 航班缺少最低价格")
            raw_top = _number(top_price.get("raw"), "Skyscanner 返回无效航班价格")
            matching = []
            exact_options = [option for option in options
                             if isinstance(option, dict)
                             and option.get("pricingOptionId") == top_price.get("pricingOptionId")]
            if len(exact_options) != 1:
                raise ProviderError("Skyscanner 航班最低价缺少唯一对应购票选项")
            for option_value in exact_options:
                option = _dict(option_value, "Skyscanner 购票选项结构已变化")
                option_price = _dict(option.get("price"), "Skyscanner 购票选项缺少价格")
                amount = _number(option_price.get("amount"), "Skyscanner 返回无效购票价格")
                # `pending` is a provisional provider check.  Even after the
                # outer search completes it is not strong enough for a price
                # alert; only the frontend's `current` option is comparable.
                if option_price.get("updateStatus") != "current":
                    restricted += 1
                    continue
                if amount != raw_top:
                    raise ProviderError("Skyscanner 航班最低价与对应购票选项不一致")
                items = _list(option.get("items"), "Skyscanner 购票选项缺少明细")
                if len(items) != 1 or option.get("fareAttributes") not in (None, {}):
                    restricted += 1
                    continue
                item = _dict(items[0], "Skyscanner 购票明细结构已变化")
                item_price = _dict(item.get("price"), "Skyscanner 购票明细缺少价格")
                if (_number(item_price.get("amount"), "Skyscanner 返回无效购票明细价格") != amount
                        or item_price.get("updateStatus") != "current"
                        or item.get("bookingProposition") != "PBOOK"):
                    raise ProviderError("Skyscanner 购票明细与最低总价不一致")
                agent = item.get("agentId")
                if not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", agent):
                    raise ProviderError("Skyscanner 购票供应商身份无效")
                matching.append(self._deep_link(item.get("url"), agent, amount))
            if not matching:
                restricted += 1
                continue
            flights, airlines = [], []
            for segment, _origin, _destination, carrier, _start, _end in parsed_segments:
                code = carrier.get("displayCode") or carrier.get("alternateId")
                if not isinstance(code, str) or not code:
                    code = ""
                if code and code not in airlines:
                    airlines.append(code)
                flights.append(code + segment["flightNumber"] if code else segment["flightNumber"])
            valid.append(Quote(
                origin=route.origin, destination=route.destination,
                departure_date=departure, price=raw_top, currency="CNY",
                source="Skyscanner 匿名公开搜索", airline="/".join(airlines),
                flight_number="/".join(flights), stops=stop_count,
                url=matching[0], price_note=PRICE_NOTE, provider="skyscanner", price_basis="total",
                origin_airport=first[1]["displayCode"],
                destination_airport=last[2]["displayCode"],
            ))
        if not valid:
            if rows and excluded:
                raise ProviderError("Skyscanner 所有带价航班均未通过路线、日期或中转链核对")
            note = "仅返回无法核实的受限或多票组合报价" if restricted else "暂无可核实报价"
            return SearchResult([], [f"{departure.isoformat()}：Skyscanner {note}"])
        notes = []
        if excluded:
            notes.append(f"{departure.isoformat()}：Skyscanner 已排除 {excluded} 条路线、日期或中转链不匹配的报价")
        if restricted:
            notes.append(f"{departure.isoformat()}：Skyscanner 已排除 {restricted} 条受限、组合或缺少唯一官方购票链接的报价")
        return SearchResult([min(valid, key=lambda quote: quote.price)], notes)
