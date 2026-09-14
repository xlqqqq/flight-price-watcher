"""AirAsia's anonymous exact-date flight action.

The public route page loads exact-date cards through a Next.js server action.
The action identifier is discovered from the current official JavaScript build;
it is never treated as a credential.  AirAsia MOVE does not state the tax basis
in this response, so returned prices are deliberately non-comparable.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .exchange import cny_rate
from .models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult


ROOT = "https://www.airasia.com/flights/"
BOOTSTRAP = ROOT + "from-kuala-lumpur-kul-to-bangkok-don-mueang-dmk/"
BOOKING = "https://www.airasia.com/v2/flights/search/"
_HOST = "www.airasia.com"
_CODE = re.compile(r"[A-Z]{3}\Z")
_CHUNK = re.compile(r"/flights/_next/static/chunks/[A-Za-z0-9_.~\-]+\.js")
_ACTION = re.compile(
    r'createServerReference\)\("([0-9a-f]{40,64})"[^}]{0,600}"fetchFlightsForDate"'
)
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
_MAX_HTML = 2_000_000
_MAX_SCRIPT = 3_000_000
_MAX_ACTION = 2_000_000
_MAX_JSON = 1_000_000


class _AccessBlocked(ProviderError):
    """An access challenge must stop the complete date loop."""


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _finite_amount(value, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ProviderError(f"AirAsia {label}不是有效金额")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ProviderError(f"AirAsia {label}不是有效金额") from None
    if not amount.is_finite() or not Decimal("0") < amount <= Decimal("10000000"):
        raise ProviderError(f"AirAsia {label}超出有效范围")
    return amount


class AirAsiaProvider:
    def __init__(self, timeout=30, request_delay=1.0, max_requests=60, *, cancelled=None):
        self.timeout = timeout
        self.request_delay = request_delay
        self.max_requests = max_requests
        self.cancelled = cancelled
        self.requests_used = 0
        self._last_request = None
        self._request_lock = threading.Lock()
        self._action_id = None

    def _check_cancelled(self):
        if self.cancelled and self.cancelled():
            raise _AccessBlocked("监控已停止，取消 AirAsia 后续查询")

    def _request(self, url, *, data=None, headers=None, max_bytes=_MAX_JSON,
                 expected_host=_HOST, expected_path="/flights/"):
        self._check_cancelled()
        with self._request_lock:
            self._check_cancelled()
            if self.requests_used >= self.max_requests:
                raise _AccessBlocked("AirAsia 已达到本轮请求上限")
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
            "Accept-Language": "en-GB,en;q=0.9",
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
                raise _AccessBlocked(f"AirAsia 返回 HTTP {exc.code} 访问保护，本轮停止") from None
            raise ProviderError(f"AirAsia 返回 HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise ProviderError(f"AirAsia 网络失败（{type(exc).__name__}）") from None
        if len(body) > max_bytes:
            raise ProviderError("AirAsia 响应超过大小限制")
        try:
            parsed = urllib.parse.urlsplit(final_url)
            safe_authority = (parsed.username is None and parsed.password is None
                              and parsed.port in {None, 443} and not parsed.fragment)
        except ValueError:
            safe_authority = False
            parsed = None
        if (not safe_authority or parsed.scheme != "https" or parsed.hostname != expected_host
                or not parsed.path.startswith(expected_path)):
            raise _AccessBlocked("AirAsia 跳转到非预期登录、验证或外部页面，本轮停止")
        return body, content_type, final_url

    def _json_request(self, url):
        body, content_type, _ = self._request(
            url, headers={"Accept": "application/json"}, max_bytes=_MAX_JSON,
            expected_host="api.frankfurter.dev", expected_path="/v1/latest",
        )
        if content_type not in {"application/json", "text/json"}:
            raise ProviderError("AirAsia/汇率服务没有返回 JSON")
        try:
            return json.loads(
                body.decode("utf-8"), parse_float=Decimal,
                object_pairs_hook=_strict_object,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (UnicodeDecodeError, ValueError, RecursionError, InvalidOperation):
            raise ProviderError("AirAsia/汇率服务返回无效 JSON") from None

    @staticmethod
    def _candidate_chunks(html: str) -> list[str]:
        # The component that receives ``initialFlights`` lists its dependent
        # chunks immediately before that property in the React stream.  Put
        # those first, then retain all page chunks as a bounded fallback.
        anchors = [m.start() for m in re.finditer(r"initialFlights", html)]
        preferred = []
        for position in anchors:
            preferred.extend(_CHUNK.findall(html[max(0, position - 5000):position]))
        all_chunks = _CHUNK.findall(html)
        result = []
        for path in preferred + all_chunks:
            if path not in result:
                result.append(path)
        return result

    def _discover_action_id(self) -> str:
        if self._action_id:
            return self._action_id
        body, content_type, final_url = self._request(
            BOOTSTRAP, headers={"Accept": "text/html"}, max_bytes=_MAX_HTML
        )
        if content_type != "text/html" or urllib.parse.urlsplit(final_url).path != urllib.parse.urlsplit(BOOTSTRAP).path:
            raise ProviderError("AirAsia 官网航线页未返回预期 HTML")
        try:
            html = body.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderError("AirAsia 官网航线页编码无效") from None
        chunks = self._candidate_chunks(html)
        if not chunks or len(chunks) > 80:
            raise ProviderError("AirAsia 官网未列出有效脚本，无法发现精确日期接口")
        for path in chunks:
            body, kind, _ = self._request(
                urllib.parse.urljoin(ROOT, path),
                headers={"Accept": "application/javascript, text/javascript;q=0.9"},
                max_bytes=_MAX_SCRIPT,
            )
            if kind not in {"application/javascript", "text/javascript", "application/x-javascript"}:
                continue
            try:
                script = body.decode("utf-8")
            except UnicodeDecodeError:
                continue
            found = set(_ACTION.findall(script))
            if len(found) == 1:
                self._action_id = found.pop()
                return self._action_id
            if len(found) > 1:
                raise ProviderError("AirAsia 精确日期接口标识不唯一，停止查询")
        raise ProviderError("AirAsia 官网脚本结构已变化，未找到精确日期接口")

    @staticmethod
    def _rsc_payload(text: str):
        values = []
        decoder = json.JSONDecoder(
            parse_float=Decimal,
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        for line in text.splitlines():
            match = re.fullmatch(r"\d+:(.*)", line)
            if not match or not match.group(1).startswith("{"):
                continue
            try:
                value, end = decoder.raw_decode(match.group(1))
            except (ValueError, RecursionError, InvalidOperation):
                continue
            if not match.group(1)[end:].strip() and isinstance(value, dict):
                values.append(value)
        payloads = [value for value in values if "success" in value and "data" in value]
        if len(payloads) != 1:
            raise ProviderError("AirAsia 精确日期响应结构已变化")
        return payloads[0]

    @staticmethod
    def _flight_number(value) -> str:
        if isinstance(value, dict):
            value = value.get("primary")
        if not isinstance(value, str):
            raise ProviderError("AirAsia 航班号字段无效")
        number = re.sub(r"\s+", "", value.upper())
        if not re.fullmatch(r"[A-Z0-9]{2,3}\d{1,4}[A-Z]?", number):
            raise ProviderError("AirAsia 航班号字段无效")
        return number

    def _parse_action(self, raw: bytes, route: Route, day: date, *, include_freshness=False):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderError("AirAsia 精确日期响应编码无效") from None
        payload = self._rsc_payload(text)
        if payload.get("success") is not True or not isinstance(payload.get("data"), dict):
            raise ProviderError("AirAsia 精确日期查询未成功")
        data = payload["data"]
        timestamp, cache_source, from_cache = (data.get("timestamp"), data.get("cacheSource"),
                                                data.get("fromCache"))
        if (not isinstance(timestamp, str) or len(timestamp) > 50
                or not isinstance(cache_source, str) or not cache_source or len(cache_source) > 50
                or type(from_cache) is not bool):
            raise ProviderError("AirAsia 缺少有效报价时间或缓存来源")
        try:
            stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("timezone")
            stamp = stamp.astimezone(timezone.utc)
        except ValueError:
            raise ProviderError("AirAsia 报价时间字段无效") from None
        now = datetime.now(timezone.utc)
        if stamp > now + timedelta(minutes=5):
            raise ProviderError("AirAsia 报价时间来自未来，未采用")
        needs_refresh = cache_source != "live" and now - stamp > timedelta(minutes=10)
        meta = data.get("meta")
        params = meta.get("searchParams") if isinstance(meta, dict) else None
        expected = {
            "origin": route.origin,
            "destination": route.destination,
            "departureDate": day.isoformat(),
            "sortBy": "cheapest",
            "limit": 6,
            "currency": "MYR",
            "language": "en-gb",
        }
        if not isinstance(params, dict) or any(params.get(key) != value for key, value in expected.items()):
            raise ProviderError("AirAsia 未准确回显所选航线、日期、币种及最低价排序")
        rows = data.get("flights")
        if not isinstance(rows, list) or len(rows) > 6 or any(not isinstance(row, dict) for row in rows):
            raise ProviderError("AirAsia 航班列表结构无效")
        parsed = []
        last_amount = None
        for row in rows:
            carrier = row.get("carrier")
            departure, arrival, price = row.get("departure"), row.get("arrival"), row.get("price")
            if not all(isinstance(value, dict) for value in (carrier, departure, arrival, price)):
                raise ProviderError("AirAsia 航班、日期或价格字段缺失")
            if departure.get("date") != day.isoformat():
                raise ProviderError("AirAsia 返回了错误出发日期")
            try:
                arrival_day = date.fromisoformat(arrival["date"])
            except (KeyError, TypeError, ValueError):
                raise ProviderError("AirAsia 到达日期字段无效") from None
            if not day <= arrival_day <= day + timedelta(days=2):
                raise ProviderError("AirAsia 到达日期超出有效范围")
            currency = price.get("currency")
            if currency != "MYR":
                raise ProviderError("AirAsia 报价币种与查询币种不一致")
            amount = _finite_amount(price.get("amount"), "报价")
            if last_amount is not None and amount < last_amount:
                raise ProviderError("AirAsia 未按最低价顺序返回，无法安全选最低价")
            last_amount = amount
            stops = row.get("stops")
            layover = row.get("layover")
            if type(stops) is not int or stops < 0 or stops > 8 or not isinstance(layover, dict) or layover.get("stops") != stops:
                raise ProviderError("AirAsia 经停字段无效")
            code, name = carrier.get("code"), carrier.get("name")
            number = self._flight_number(row.get("flightNumber"))
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]{2,3}", code):
                raise ProviderError("AirAsia 承运人代码无效")
            if not isinstance(name, str) or not name.strip() or len(name) > 100:
                raise ProviderError("AirAsia 承运人名称无效")
            parsed.append((amount, code, name.strip(), number, stops))
        if rows:
            actual = _finite_amount(data.get("actualPrice"), "实际最低价")
            cheapest = meta.get("cheapestPrice")
            if (actual != parsed[0][0] or not isinstance(cheapest, dict)
                    or cheapest.get("currency") != "MYR"
                    or _finite_amount(cheapest.get("amount"), "最低价") != parsed[0][0]):
                raise ProviderError("AirAsia 响应内的最低价字段互相矛盾")
        # A connection card does not expose every segment carrier.  Restrict
        # this airline provider to direct cards whose carrier name and flight
        # number both independently identify AirAsia.
        owned = [item for item in parsed if item[4] == 0 and "airasia" in item[2].casefold()
                 and item[3].startswith(item[1])]
        result = min(owned, default=None, key=lambda item: item[0])
        return (result, needs_refresh) if include_freshness else result

    @staticmethod
    def _booking_url(route: Route, day: date) -> str:
        return BOOKING + "?" + urllib.parse.urlencode({
            "origin": route.origin,
            "destination": route.destination,
            "departDate": day.strftime("%d/%m/%Y"),
            "tripType": "O",
            "adult": 1,
            "child": 0,
            "infant": 0,
            "locale": "en-gb",
            "currency": "MYR",
            "cabinClass": "economy",
            "isAirasiaFlightOnly": "true",
        })

    @staticmethod
    def official_url(route: Route, day: date) -> str:
        """Public purchase page carrying the same one-way passenger filters."""
        return AirAsiaProvider._booking_url(route, day)

    def search(self, route: Route, today: date):
        for side, label in (("origin", "出发地"), ("destination", "目的地")):
            scope = getattr(route, f"{side}_scope")
            if scope not in {"city", "airport"}:
                raise ProviderUnsupported(f"AirAsia {label}范围必须是城市全部机场或具体机场")
            if scope == "airport":
                city = route.city_code(side)
                if not isinstance(city, str) or not _CODE.fullmatch(city):
                    raise ProviderUnsupported(
                        f"AirAsia 指定机场 {getattr(route, side)} 缺少有效所属城市代码"
                    )
        if route.currency != "CNY" or route.stay_nights or route.travel_class != 1:
            raise ProviderUnsupported("AirAsia 仅查询 1 成人经济舱单程，并折算为 CNY")
        if route.market != "international":
            raise ProviderUnsupported("AirAsia 不提供中国国内航班，只查询其自营国际/海外航线")
        if not all(isinstance(code, str) and _CODE.fullmatch(code) for code in (route.origin, route.destination)):
            raise ProviderUnsupported("AirAsia 需要明确的三字城市/机场代码")
        if route.origin == route.destination:
            raise ProviderUnsupported("AirAsia 出发地和目的地不能相同")
        wanted = route.departure_dates(today)
        if not wanted:
            return SearchResult([], ["没有尚未过期的出发日期"])
        discovery_floor = 0 if self._action_id else 2  # one HTML page and at least one JS chunk
        if self.requests_used + discovery_floor + 2 * len(wanted) + 1 > self.max_requests:
            raise ProviderError("AirAsia 完整日期查询及可能的实时刷新超过本轮请求上限")
        action_id = self._discover_action_id()
        # Reserve two actions per date (the public UI refreshes stale cache)
        # plus one dated exchange-rate request before starting the date loop.
        # This prevents returning only the first part of a requested date set.
        if self.requests_used + 2 * len(wanted) + 1 > self.max_requests:
            raise ProviderError("AirAsia 当前官网脚本发现开销过高，剩余预算不足以完整查询所有日期")
        raw_quotes, failures = [], []
        for index, day in enumerate(wanted):
            self._check_cancelled()
            request_data = [{
                "origin": route.origin,
                "destination": route.destination,
                "date": day.isoformat(),
                "currency": "MYR",
                "language": "en-gb",
                "sortBy": "cheapest",
                "limit": 6,
                "geoId": "MY",
            }]
            try:
                body, kind, _ = self._request(
                    ROOT,
                    data=json.dumps(request_data, separators=(",", ":")).encode("utf-8"),
                    headers={
                        "Accept": "text/x-component",
                        "Content-Type": "text/plain;charset=UTF-8",
                        "Next-Action": action_id,
                        "Origin": "https://www.airasia.com",
                    },
                    max_bytes=_MAX_ACTION,
                )
                if kind != "text/x-component":
                    raise ProviderError("AirAsia 精确日期接口未返回组件数据")
                item, stale = self._parse_action(body, route, day, include_freshness=True)
                if stale:
                    request_data[0]["forceRefresh"] = True
                    body, kind, _ = self._request(
                        ROOT,
                        data=json.dumps(request_data, separators=(",", ":")).encode("utf-8"),
                        headers={
                            "Accept": "text/x-component",
                            "Content-Type": "text/plain;charset=UTF-8",
                            "Next-Action": action_id,
                            "Origin": "https://www.airasia.com",
                        },
                        max_bytes=_MAX_ACTION,
                    )
                    if kind != "text/x-component":
                        raise ProviderError("AirAsia 实时刷新接口未返回组件数据")
                    item, stale = self._parse_action(body, route, day, include_freshness=True)
                    if stale:
                        raise ProviderError("AirAsia 实时刷新后仍只返回过期缓存，未采用")
                if item:
                    raw_quotes.append((day, item))
            except _AccessBlocked:
                raise
            except ProviderError as exc:
                if index == 0:
                    raise ProviderError(f"{day.isoformat()}：{exc}") from None
                failures.append(f"{day.isoformat()}：{exc}")
        if not raw_quotes and failures:
            raise ProviderError("；".join(failures))
        quotes = []
        if raw_quotes:
            rate, rate_day = cny_rate("MYR", today, self._json_request)
            for day, (amount, code, name, number, stops) in raw_quotes:
                cny = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                quotes.append(Quote(
                    route.origin, route.destination, day, cny, "CNY", "AirAsia 官网",
                    airline=name, flight_number=number, stops=stops, provider="airasia",
                    price_basis="unknown", original_price=amount, original_currency="MYR",
                    exchange_rate=rate, exchange_date=rate_day,
                    origin_airport=route.airport_code("origin"),
                    destination_airport=route.airport_code("destination"),
                    url=self._booking_url(route, day),
                    price_note=(
                        f"AirAsia 自营直飞 {number}，官网精确日期最低展示价 MYR {amount}；"
                        f"按 {rate_day} Frankfurter 参考汇率 1 MYR={rate} CNY 折算。"
                        "接口未说明税费口径，不参与最低含税总价/阈值；"
                        "行李、选座、支付费用及最终可售总价请到官网确认"
                    ),
                ))
        warnings = [
            "AirAsia 每日接口按价格仅返回前 6 条，再筛选其中由 AirAsia 承运的直飞；"
            "未命中不代表没有航班；税费口径未说明，折算价仅展示、不参与含税最低价"
        ]
        warnings.extend(failures)
        missing = len(wanted) - len(quotes)
        if missing:
            warnings.append(f"{missing} 个日期未返回可严格核验的 AirAsia 自营直飞，不代表没有航班")
        return SearchResult(quotes, warnings)
