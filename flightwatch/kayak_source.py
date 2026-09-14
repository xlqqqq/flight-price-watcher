"""Strict cookieless probe for KAYAK's official flight-search page.

KAYAK currently returns an SSR search shell to a plain anonymous GET.  The
shell accurately carries the requested route, date, passengers and cabin, but
its ``searchState.status`` is ``NOT_STARTED`` and it contains no fares.  The
official frontend then POSTs to ``/i/api/search/dynamic/flights/poll`` with a
page-created form token and a session; a cookieless request to that endpoint
returns ``401 INVALID_SESSION``.  We deliberately do not replay those session
credentials, run browser automation, solve challenges or turn SEO/history
prices into a quote for an arbitrary requested date.

This provider still performs a real request to the exact official search URL.
It verifies the full bootstrap identity and fails clearly when only the
dynamic shell is available.  That makes KAYAK selectable without ever
inventing a price, and leaves a strict parser boundary if KAYAK later renders a
complete, independently verifiable result in the anonymous response.

Official sources checked 2026-09-14:
https://www.kayak.com/flights
https://www.kayak.com/flights/SHA-TYO/2026-10-01?sort=price_a&currency=CNY
https://content.r9cdn.net/frontier/assets/BsE8KPaiHz.js
"""

from __future__ import annotations

from dataclasses import dataclass
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

from .models import ProviderError, ProviderUnsupported, Route, SearchResult


_MAX_RESPONSE_BYTES = 3_000_000
_MAX_BOOTSTRAP_BYTES = 2_500_000
_CODE = re.compile(r"[A-Z]{3}\Z")
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
_TRAVELERS = {
    "adults": 1,
    "seniors": 0,
    "students": 0,
    "youth": 0,
    "child": 0,
    "seatInfant": 0,
    "lapInfant": 0,
    "childAges": [],
}


class _AccessBlocked(ProviderError):
    """The official site returned a verification/access-control page."""


class _DynamicSearchRequired(ProviderError):
    """The anonymous document is valid but contains only a search shell."""


@dataclass(frozen=True)
class _Site:
    provider_id: str
    name: str
    host: str
    path_prefix: str
    required_brand: str


KAYAK_SITE = _Site("kayak", "KAYAK", "www.kayak.com", "/flights", "kayak")


class _BootstrapParser(HTMLParser):
    """Extract only the named JSON bootstrap and page title; execute nothing."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self._in_bootstrap = False
        self._bootstrap_parts: list[str] = []
        self._title_parts: list[str] = []
        self.bootstrap_count = 0
        self.bootstrap_type_valid = True

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "script" and values.get("id") == "jsonData_R9DataStorage":
            self.bootstrap_count += 1
            self.bootstrap_type_valid &= values.get("type") == "application/json"
            self._in_bootstrap = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._in_bootstrap:
            self._in_bootstrap = False

    def handle_data(self, value):
        if self._in_title:
            self._title_parts.append(value)
        if self._in_bootstrap:
            self._bootstrap_parts.append(value)

    @property
    def title(self) -> str:
        return "".join(self._title_parts).strip()

    @property
    def bootstrap(self) -> str:
        return "".join(self._bootstrap_parts)


def _json_object(value: str, site_name: str) -> dict:
    if len(value.encode("utf-8")) > _MAX_BOOTSTRAP_BYTES:
        raise ProviderError(f"{site_name} 页面查询数据超过大小限制")

    def reject_constant(_value):
        raise ValueError("invalid number")

    def reject_duplicate_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    try:
        data = json.loads(
            value,
            parse_float=Decimal,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (ValueError, RecursionError, InvalidOperation):
        raise ProviderError(f"{site_name} 页面查询数据不是有效 JSON") from None
    if not isinstance(data, dict):
        raise ProviderError(f"{site_name} 页面查询数据根节点不是对象")
    return data


def _unique_query(url: str) -> dict[str, str]:
    try:
        pairs = urllib.parse.parse_qsl(
            urllib.parse.urlsplit(url).query, keep_blank_values=True, strict_parsing=True
        )
    except ValueError:
        raise ProviderError("官网搜索链接查询参数无效") from None
    if len(pairs) != len({key for key, _value in pairs}):
        raise ProviderError("官网搜索链接包含重复查询参数")
    return dict(pairs)


def _same_https_url(actual: str, expected: str, site_name: str) -> None:
    got, wanted = urllib.parse.urlsplit(actual), urllib.parse.urlsplit(expected)
    try:
        port = got.port
    except ValueError:
        raise ProviderError(f"{site_name} 搜索页跳转到了无法核验的地址") from None
    if (
        got.scheme != "https"
        or got.hostname != wanted.hostname
        or port not in (None, 443)
        or got.username is not None
        or got.password is not None
        or got.fragment
        or got.path != wanted.path
        or _unique_query(actual) != _unique_query(expected)
    ):
        raise ProviderError(f"{site_name} 搜索页跳转到了无法核验的地址")


def _require_dict(value, message: str) -> dict:
    if not isinstance(value, dict):
        raise ProviderError(message)
    return value


def _verify_location(value, expected_code: str, site_name: str, field: str) -> None:
    value = _require_dict(value, f"{site_name} 缺少有效{field}身份")
    locations = value.get("locations")
    if (
        not isinstance(locations, list)
        or len(locations) != 1
        or not isinstance(locations[0], dict)
        or locations[0].get("airportCode") != expected_code
        or value.get("isNearby") is not False
        or ("nearby" in value and value.get("nearby") is not False)
    ):
        raise ProviderError(f"{site_name} 返回的{field}与查询不一致或启用了附近机场")


def _validate_location_scopes(route: Route, site_name: str) -> None:
    for side, label in (("origin", "出发地"), ("destination", "目的地")):
        scope = getattr(route, f"{side}_scope")
        if scope not in {"city", "airport"}:
            raise ProviderUnsupported(f"{site_name} {label}范围必须是城市全部机场或具体机场")
        if scope == "airport":
            city = route.city_code(side)
            if not isinstance(city, str) or not _CODE.fullmatch(city):
                raise ProviderUnsupported(
                    f"{site_name} 指定机场 {getattr(route, side)} 缺少有效所属城市代码"
                )


class _R9AnonymousPageProvider:
    """Shared strict transport for two separately requested R9-owned sites."""

    site = KAYAK_SITE

    def __init__(self, timeout: float = 30, request_delay: float = 1.0,
                 max_requests: int = 60, *, cancelled=None):
        self.timeout = timeout
        self.request_delay = request_delay
        self.max_requests = max_requests
        self.cancelled = cancelled
        self.requests_used = 0
        self._last_request: float | None = None
        self._request_lock = threading.Lock()

    @classmethod
    def search_url(cls, route: Route, day: date) -> str:
        path = f"{cls.site.path_prefix}/{route.origin}-{route.destination}/{day.isoformat()}"
        return urllib.parse.urlunsplit((
            "https", cls.site.host, path,
            urllib.parse.urlencode({"sort": "price_a", "currency": "CNY"}), "",
        ))

    @classmethod
    def official_url(cls, route: Route, day: date) -> str:
        """Alias used by the source registry to build an exact-date link."""
        return cls.search_url(route, day)

    def _check_cancelled(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise ProviderError(f"监控已停止，取消 {self.site.name} 后续查询")

    def _request(self, url: str) -> str:
        self._check_cancelled()
        with self._request_lock:
            self._check_cancelled()
            if self.requests_used >= self.max_requests:
                raise ProviderError(f"{self.site.name} 查询已达到本轮请求上限")
            if self._last_request is not None:
                wait = self.request_delay - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
            self._check_cancelled()
            self.requests_used += 1
            self._last_request = time.monotonic()

        # urllib has no cookie jar here.  Set-Cookie may be received, but it is
        # discarded and never replayed; no Authorization/form token is sent.
        request = urllib.request.Request(url, headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8",
            "Cache-Control": "no-cache",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                final_url = response.geturl()
                content_type = response.headers.get_content_type()
                charset = response.headers.get_content_charset()
                body = response.read(_MAX_RESPONSE_BYTES + 1)
            _same_https_url(final_url, url, self.site.name)
            if content_type != "text/html":
                raise ProviderError(f"{self.site.name} 未返回可核验的 HTML 搜索页")
            if charset not in (None, "utf-8", "UTF-8"):
                raise ProviderError(f"{self.site.name} 搜索页编码无法核验")
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ProviderError(f"{self.site.name} 搜索页超过大小限制")
            return body.decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 429}:
                raise _AccessBlocked(
                    f"{self.site.name} 匿名搜索被官网拒绝（HTTP {exc.code}）；本次未绕过验证"
                ) from None
            raise ProviderError(f"{self.site.name} 返回 HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise ProviderError(
                f"{self.site.name} 查询网络失败（{type(exc).__name__}）"
            ) from None
        except UnicodeDecodeError:
            raise ProviderError(f"{self.site.name} 返回无法识别的页面编码") from None

    def _parse(self, html: str, route: Route, day: date) -> SearchResult:
        parser = _BootstrapParser()
        try:
            parser.feed(html)
            parser.close()
        except (ValueError, RecursionError):
            raise ProviderError(f"{self.site.name} 返回了无法解析的搜索页") from None

        title = parser.title.lower()
        challenge_markers = (
            "please verify that you are a real user",
            "verify you are human",
            "access denied",
            "captcha",
        )
        if any(marker in title for marker in challenge_markers):
            raise _AccessBlocked(
                f"{self.site.name} 返回了真人验证页；本程序不会处理或绕过验证码"
            )
        if parser.bootstrap_count != 1 or not parser.bootstrap_type_valid:
            lower = html.lower()
            if "g-recaptcha" in lower or "cf-chl-" in lower or "px-captcha" in lower:
                raise _AccessBlocked(
                    f"{self.site.name} 返回了访问验证页；本程序不会绕过网站防护"
                )
            raise ProviderError(f"{self.site.name} 搜索页缺少唯一的官方查询数据")

        data = _json_object(parser.bootstrap, self.site.name)
        brands = data.get("brands")
        if (
            not isinstance(brands, list)
            or self.site.required_brand not in brands
            or data.get("componentName") != "ui/omni/page/OmniFlightsResultPage"
        ):
            raise ProviderError(f"{self.site.name} 搜索页品牌或组件身份不匹配")

        expected_path = urllib.parse.urlsplit(self.search_url(route, day)).path
        expected_trip = f"{route.origin}-{route.destination}/{day.isoformat()}"
        props = _require_dict(data.get("props"), f"{self.site.name} 缺少页面请求身份")
        if props.get("originalUri") != expected_path:
            raise ProviderError(f"{self.site.name} 页面航线或日期与查询不一致")
        self._verify_request_params(props.get("reqParams"), expected_trip)

        server = _require_dict(data.get("serverData"), f"{self.site.name} 缺少搜索状态")
        server_request = _require_dict(
            server.get("serverRequestState"), f"{self.site.name} 缺少服务端请求身份"
        )
        self._verify_request_params(server_request.get("params"), expected_trip)
        self._verify_trip(server.get("FlightSearchStatus"), route, day)

        state = _require_dict(server.get("searchState"), f"{self.site.name} 缺少结果状态")
        if state.get("currentSortMode") != "price_a":
            raise ProviderError(f"{self.site.name} 未按价格从低到高查询")
        status = state.get("status")
        if status == "NOT_STARTED":
            raise _DynamicSearchRequired(
                f"{self.site.name} 官网匿名页面只返回动态搜索外壳，未提供可核验的 CNY 含税总价；"
                "实时结果需要官网浏览器会话轮询，本程序未使用 Cookie 或动态 token"
            )
        if status != "COMPLETED":
            raise ProviderError(f"{self.site.name} 返回了未知的搜索状态")

        locale = _require_dict(server.get("locale"), f"{self.site.name} 缺少币种状态")
        currency = _require_dict(locale.get("currency"), f"{self.site.name} 缺少币种身份")
        if currency.get("code") != route.currency:
            raise ProviderError(f"{self.site.name} 完成结果的币种与查询不一致")
        response = _require_dict(state.get("response"), f"{self.site.name} 完成状态缺少结果")
        available, total = response.get("availableResultsCount"), response.get("totalResultCount")
        if type(available) is int and type(total) is int and available == total == 0:
            return SearchResult([], [f"{self.site.name} 官网本次完成搜索未返回航班"])

        # Current anonymous HTML never reaches this branch.  Refuse evolving
        # result shapes until fare, taxes, currency and clickout URL can all be
        # tied to one result.  A visually rendered number alone is not enough.
        raise ProviderError(
            f"{self.site.name} 页面出现结果，但缺少已核验的含税总价与官方购票链接结构"
        )

    def _verify_request_params(self, value, expected_trip: str) -> None:
        params = _require_dict(value, f"{self.site.name} 请求参数结构无效")
        required = {
            "t": expected_trip,
            "display": "RP",
            "currency": "CNY",
            "vertical": "flights",
            "sort": "price_a",
        }
        if any(params.get(key) != expected for key, expected in required.items()):
            raise ProviderError(f"{self.site.name} 请求的航线、日期、币种或排序不一致")

    def _verify_trip(self, value, route: Route, day: date) -> None:
        status = _require_dict(value, f"{self.site.name} 行程状态结构无效")
        travelers = status.get("travelers")
        traveler_counts_valid = (
            isinstance(travelers, dict)
            and set(travelers) == set(_TRAVELERS)
            and all(
                type(travelers[key]) is int and travelers[key] == expected
                for key, expected in _TRAVELERS.items()
                if key != "childAges"
            )
            and travelers.get("childAges") == []
        )
        bags = status.get("bags")
        bags_valid = (
            isinstance(bags, dict)
            and set(bags) == {"carryon", "checked"}
            and type(bags.get("carryon")) is int
            and type(bags.get("checked")) is int
            and bags == {"carryon": 0, "checked": 0}
        )
        if (
            status.get("tripType") != "oneway"
            or not traveler_counts_valid
            or not bags_valid
            or status.get("flexMode") != "EXACT"
            or status.get("isUsingFlexDates") is not False
            or status.get("isOpenFlex") is not False
        ):
            raise ProviderError(f"{self.site.name} 未确认 1 成人经济舱精确日期单程条件")
        legs = status.get("legs")
        if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict):
            raise ProviderError(f"{self.site.name} 返回的行程段数无效")
        leg = legs[0]
        if (
            leg.get("date") != day.isoformat()
            or leg.get("flexDate") != "EXACT_DATES"
            or leg.get("cabin") != "e"
        ):
            raise ProviderError(f"{self.site.name} 返回的日期或舱位与查询不一致")
        _verify_location(leg.get("origin"), route.origin, self.site.name, "出发地")
        _verify_location(leg.get("destination"), route.destination, self.site.name, "目的地")
        if "departure" in leg:
            _verify_location(leg.get("departure"), route.origin, self.site.name, "出发地")

    def search(self, route: Route, today: date) -> SearchResult:
        _validate_location_scopes(route, self.site.name)
        if route.currency != "CNY":
            raise ProviderUnsupported(f"{self.site.name} 当前接入只核验 CNY 报价")
        if route.stay_nights is not None:
            raise ProviderUnsupported(f"{self.site.name} 当前接入只核验单程搜索")
        if route.travel_class != 1:
            raise ProviderUnsupported(f"{self.site.name} 当前接入只核验普通经济舱")
        if route.nonstop:
            raise ProviderUnsupported(f"{self.site.name} 当前接入尚未核验直飞筛选")
        if (
            not isinstance(route.origin, str)
            or not isinstance(route.destination, str)
            or not _CODE.fullmatch(route.origin)
            or not _CODE.fullmatch(route.destination)
            or route.origin == route.destination
        ):
            raise ProviderError(f"{self.site.name} 查询须使用两个不同的三字母城市或机场代码")

        dates = route.departure_dates(today)
        if not dates:
            return SearchResult([], ["配置中没有尚未过期的出发日期"])
        quotes, warnings = [], []
        completed = 0
        for day in dates:
            try:
                result = self._parse(self._request(self.search_url(route, day)), route, day)
            except ProviderError as exc:
                # Verification/session/schema failures apply to the access
                # path, so dozens of identical date requests add delay and
                # load without creating any trustworthy quote.
                raise ProviderError(f"{day.isoformat()}：{exc}") from None
            completed += 1
            quotes.extend(result.quotes)
            warnings.extend(result.warnings)
        if not completed:
            raise ProviderError(f"{self.site.name} 未完成任何日期查询")
        return SearchResult(quotes, list(dict.fromkeys(warnings)))


class KayakProvider(_R9AnonymousPageProvider):
    site = KAYAK_SITE
