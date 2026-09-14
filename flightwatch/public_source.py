"""Anonymous Ctrip mobile low-price calendar, checked live on 2026-09-08.

Primary source: https://m.ctrip.com/html5/flight/
The public website backend below returned real domestic SHA-BJS, international
SHA-TYO, and SHA-HKG calendars without cookies, tokens, or login. This is an
undocumented website endpoint, not a supported partner API. Its internal
calendarSelections enum has no public contract; the observed request is kept
as-is, and no cabin/nonstop/round-trip filtering is advertised. Calendar totals
are indicative: they are not a fresh bookable flight-offer response.
"""

from __future__ import annotations

import json
import http.client
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult


ENDPOINT = (
    "https://m.ctrip.com/restapi/soa2/15380/bjjson/"
    "FlightIntlAndInlandLowestPriceSearch"
)
PRICE_NOTE = (
    "携程低价日历参考总价（totalPrice），1 成人单程、平台默认舱位参考；"
    "缓存可能过期，舱位、税费、行李及最终可售价格请到购票页确认"
)
_CST = timezone(timedelta(hours=8))
_MS_DATE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")
_MAX_RESPONSE_BYTES = 2_000_000


def calendar_date(value: object) -> date:
    """Read the service's Microsoft JSON date using its explicit UTC offset."""
    if not isinstance(value, str):
        raise ValueError("日期不是字符串")
    match = _MS_DATE.fullmatch(value)
    if not match:
        raise ValueError("日期格式发生变化")
    offset = match.group(2)
    tz = _CST
    if offset:
        hours, minutes = int(offset[1:3]), int(offset[3:5])
        if hours > 23 or minutes > 59:
            raise ValueError("无效日期时区")
        sign = 1 if offset[0] == "+" else -1
        tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        return datetime.fromtimestamp(int(match.group(1)) / 1000, timezone.utc).astimezone(tz).date()
    except (OverflowError, OSError) as exc:
        raise ValueError("日期超出有效范围") from exc


class CtripCalendarProvider:
    def __init__(self, timeout: float = 30, request_delay: float = 1.0,
                 max_requests: int = 60):
        self.timeout = timeout
        self.request_delay = request_delay
        self.max_requests = max_requests
        self.requests_used = 0
        self._last_request: float | None = None

    def _request(self, payload: dict) -> dict:
        if self.requests_used >= self.max_requests:
            raise ProviderError("携程查询已达到本轮请求上限")
        if self._last_request is not None:
            wait = self.request_delay - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        self.requests_used += 1
        self._last_request = time.monotonic()
        request = urllib.request.Request(
            ENDPOINT,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Referer": "https://m.ctrip.com/",
                "x-ctx-currency": "CNY",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ProviderError("携程响应超过大小限制，可能接口已变化")
            data = json.loads(body.decode("utf-8"), parse_float=Decimal)
        except urllib.error.HTTPError as exc:
            raise ProviderError(
                f"携程返回 HTTP {exc.code}；可能触发网站防护，请稍后重试或切换数据源"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise ProviderError(f"携程查询网络失败（{type(exc).__name__}）") from None
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise ProviderError("携程返回非 JSON 内容，可能是验证页面或接口已变化") from None
        if not isinstance(data, dict):
            raise ProviderError("携程响应根节点不是对象，接口可能已变化")
        return data

    def search(self, route: Route, today: date) -> SearchResult:
        if route.origin_scope == "airport" or route.destination_scope == "airport":
            raise ProviderUnsupported(
                "本程序接入的携程日历只有城市最低价，尚未接入携程航班机场筛选；不代表携程不支持该机场"
            )
        if route.currency != "CNY":
            raise ProviderError("携程日历仅支持 CNY，不会将其他币种当作人民币")
        if route.stay_nights is not None:
            raise ProviderError("携程日历目前仅支持单程；往返请使用 serpapi 数据源")
        if route.nonstop:
            raise ProviderError("携程日历不支持可靠的直飞筛选；请使用 serpapi 数据源")
        if route.travel_class != 1:
            raise ProviderError("携程日历不支持舱位筛选；请使用 serpapi 数据源")
        if route.market not in {"domestic", "international"}:
            raise ProviderError("携程 market 只能为 domestic 或 international")
        if not all(re.fullmatch(r"[A-Z]{3}", value)
                   for value in (route.origin, route.destination)):
            raise ProviderError("携程日历须使用单个三字母城市代码（例如 SHA、BJS、TYO）")
        dates = route.departure_dates(today)
        if not dates:
            return SearchResult([], ["配置中没有尚未过期的出发日期"])
        payload = {
            "searchType": 1 if route.market == "domestic" else 2,
            "departNewCityCode": route.origin,
            "arriveNewCityCode": route.destination,
            "passengerList": [{"passengerCount": 1, "passengerType": "Adult"}],
            "grade": 1,
            # This exact website request was verified for both markets. The
            # enum is undocumented; do not infer cabin or nonstop guarantees.
            "calendarSelections": [{"selectionType": 8, "selectionContent": ["1"]}],
            "startDate": min(dates).isoformat(),
            "flag": 0,
            "channelName": "MobileH5",
            "lowPriceExtendInfo": [],
            "head": {"cid": "", "ctok": "", "cver": "1.0", "lang": "01",
                     "sid": "", "syscode": "09", "auth": "", "extension": []},
        }
        return self._parse(self._request(payload), route, set(dates))

    def _parse(self, data: dict, route: Route, wanted: set[date]) -> SearchResult:
        status = data.get("responseStatus")
        if not isinstance(status, dict) or status.get("Ack") != "Success" or status.get("Errors"):
            raise ProviderError("携程接口未确认查询成功，未将响应当作有效票价")
        rows = data.get("priceList")
        if not isinstance(rows, list):
            raise ProviderError("携程响应缺少 priceList 列表，接口可能已变化")
        quotes_by_day: dict[date, Quote] = {}
        missing = set(wanted)
        for row in rows:
            if not isinstance(row, dict) or "departDate" not in row or "totalPrice" not in row:
                raise ProviderError("携程票价字段发生变化，停止解析以避免误报")
            try:
                day = calendar_date(row["departDate"])
            except ValueError as exc:
                raise ProviderError(f"携程出发日期解析失败：{exc}") from None
            if day not in wanted:
                continue
            raw_price = row["totalPrice"]
            if isinstance(raw_price, bool) or raw_price is None:
                raise ProviderError("携程返回了无效总价，未使用税前价格替代")
            try:
                price = Decimal(str(raw_price))
            except InvalidOperation:
                raise ProviderError("携程总价不是数字，停止解析以避免误报") from None
            if not price.is_finite() or price < 0:
                raise ProviderError("携程返回了无效总价，停止解析以避免误报")
            # The service encodes unavailable calendar dates as totalPrice=0.
            if price == 0:
                continue
            return_value = row.get("returnDate")
            if return_value:
                match = _MS_DATE.fullmatch(str(return_value))
                # Live one-way international responses carry negative year-1
                # placeholders. A positive return date would change semantics.
                if not match or int(match.group(1)) >= 0:
                    raise ProviderError("携程单程响应出现实际返程日期，停止解析以避免航程误报")
            url = (
                "https://flights.ctrip.com/online/list/oneway-"
                f"{route.origin.lower()}-{route.destination.lower()}?"
                + urllib.parse.urlencode({"depdate": day.isoformat(), "adult": 1})
            )
            quote = Quote(
                origin=route.origin, destination=route.destination,
                departure_date=day, price=price, currency="CNY",
                source="携程低价日历", airline=str(row.get("airLine") or ""),
                flight_number=str(row.get("flightNo") or ""),
                url=url, price_note=PRICE_NOTE,
            )
            if day not in quotes_by_day or price < quotes_by_day[day].price:
                quotes_by_day[day] = quote
            missing.discard(day)
        warnings = []
        if missing:
            warnings.append(
                f"{len(missing)} 个出发日期暂无有效日历总价；"
                "可能尚无缓存、无航班或超出日历范围，不能据此判断售罄"
            )
        return SearchResult([quotes_by_day[day] for day in sorted(quotes_by_day)], warnings)
