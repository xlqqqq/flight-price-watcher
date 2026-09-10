"""Ryanair's own anonymous airport catalogue and daily fare calendar.

Checked 2026-09-09 at https://www.ryanair.com/gb/en/cheap-flights/london-to-dublin
and its frontend-6fb8f31263.js (FareFinderApiConfig cheapestPerDay).
Only Ryanair's served airport groups are searched. Original-currency fares
are converted using dated Frankfurter reference rates, not card settlement
rates. Website calendars are indicative and exclude optional extras.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from itertools import product
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .exchange import cny_rate
from .models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult

AIRPORTS = "https://www.ryanair.com/api/views/locate/5/airports/en/active"
CALENDAR = "https://www.ryanair.com/api/farfnd/v4/oneWayFares"


class RyanairProvider:
    def __init__(self, timeout=30, request_delay=1.0, max_requests=60, *, cancelled=None):
        self.timeout, self.request_delay, self.max_requests = timeout, request_delay, max_requests
        self.cancelled = cancelled
        self.requests_used = 0
        self._last_request = None
        self._airports = None

    def _request(self, url):
        if self.cancelled and self.cancelled():
            raise ProviderError("监控已停止，取消瑞安航空后续查询")
        if self.requests_used >= self.max_requests:
            raise ProviderError("瑞安航空已达到本轮请求上限")
        if self._last_request is not None:
            time.sleep(max(0, self.request_delay - (time.monotonic() - self._last_request)))
        if self.cancelled and self.cancelled():
            raise ProviderError("监控已停止，取消瑞安航空后续查询")
        self._last_request = time.monotonic()
        self.requests_used += 1
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise ProviderError("瑞安航空/汇率响应过大，停止解析")
            return json.loads(body.decode("utf-8"), parse_float=Decimal)
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"瑞安航空/汇率服务返回 HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise ProviderError(f"瑞安航空/汇率网络失败（{type(exc).__name__}）") from None
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ProviderError("瑞安航空/汇率返回无效 JSON 或验证页") from None

    def _city_airports(self, code):
        if self._airports is None:
            rows = self._request(AIRPORTS)
            if not isinstance(rows, list) or not 1 <= len(rows) <= 2000:
                raise ProviderError("瑞安航空机场目录结构变化")
            if any(not isinstance(row, dict) or not isinstance(row.get("city"), dict)
                   or not re.fullmatch(r"[A-Z]{3}", str(row.get("code", ""))) for row in rows):
                raise ProviderError("瑞安航空机场代码/城市字段无效")
            self._airports = rows
        # macCode is the airline's own metropolitan IATA identity. Do not
        # choose airports using nearest coordinates or a fixed popular list.
        grouped = [r for r in self._airports if r["city"].get("macCode") == code]
        selected = grouped or [r for r in self._airports if r["code"] == code]
        if not selected:
            raise ProviderUnsupported(f"瑞安航空目录未覆盖城市 {code}；不会改查附近城市")
        return sorted({r["code"] for r in selected})

    def search(self, route: Route, today: date):
        if route.currency != "CNY" or route.stay_nights or route.nonstop or route.travel_class != 1:
            raise ProviderUnsupported("瑞安航空仅提供单程默认票价并折算 CNY，不能指定往返/舱位/直飞")
        if route.market != "international":
            raise ProviderUnsupported("瑞安航空不提供中国国内航班，只查询其海外航线")
        if not all(isinstance(c, str) and re.fullmatch(r"[A-Z]{3}", c) for c in (route.origin, route.destination)):
            raise ProviderUnsupported("瑞安航空需要明确的城市三字码")
        wanted = set(route.departure_dates(today))
        if not wanted:
            return SearchResult([], ["没有尚未过期的出发日期"])
        origins, destinations = self._city_airports(route.origin), self._city_airports(route.destination)
        pairs = [(a, b) for a, b in product(origins, destinations) if a != b]
        if not pairs:
            raise ProviderUnsupported("瑞安航空的出发与到达机场集合重叠")
        months = sorted({day.replace(day=1) for day in wanted})
        # Avoid an unannounced partial city search when multiple airports map
        # to each metropolitan area. Reserve a rate request for each origin.
        if len(pairs) * len(months) + len(origins) + self.requests_used > self.max_requests:
            raise ProviderError("瑞安航空完整城市机场组合超过本轮预算，请缩小日期范围或提高请求上限")
        quotes, failures, succeeded = [], [], 0
        warnings = ["瑞安航空只覆盖自营海外航线；日历税费口径未单独确认，外币折算仅展示，不参与总价阈值"]
        for origin, destination in pairs:
            for month in months:
                if self.cancelled and self.cancelled():
                    warnings.append("监控已停止，后续机场/日期未查询")
                    return SearchResult(quotes, warnings)
                url = f"{CALENDAR}/{origin}/{destination}/cheapestPerDay?" + urllib.parse.urlencode({"outboundMonthOfDate": month.isoformat()})
                try:
                    quotes.extend(self._parse(self._request(url), route, wanted, origin, destination, today))
                    succeeded += 1
                except ProviderError as exc:
                    failures.append(f"{origin}→{destination} {month:%Y-%m}：{exc}")
        if not succeeded:
            raise ProviderError("；".join(failures) or "瑞安航空未返回有效日历")
        warnings.extend(failures)
        by_day = {}
        for q in quotes:
            if q.departure_date not in by_day or q.price < by_day[q.departure_date].price:
                by_day[q.departure_date] = q
        missing = wanted - set(by_day)
        if missing:
            warnings.append(f"{len(missing)} 个日期未查到瑞安航空参考票价，可能没有该航线或日历缓存")
        return SearchResult([by_day[d] for d in sorted(by_day)], warnings)

    def _parse(self, data, route, wanted, origin, destination, today):
        outbound = data.get("outbound") if isinstance(data, dict) else None
        rows = outbound.get("fares") if isinstance(outbound, dict) else None
        if not isinstance(rows, list):
            raise ProviderError("瑞安航空缺少单程日历字段")
        quotes = []
        for row in rows:
            try:
                day = date.fromisoformat(row["day"])
                if day not in wanted:
                    continue
                if type(row.get("unavailable")) is not bool or type(row.get("soldOut")) is not bool:
                    raise ValueError("availability")
                if row["unavailable"] or row["soldOut"]:
                    continue
                if datetime.fromisoformat(row["departureDate"]).date() != day:
                    raise ValueError("date")
                raw = row["price"]["value"]
                currency = row["price"]["currencyCode"]
                if isinstance(raw, bool):
                    raise ValueError("amount")
                price = Decimal(str(raw))
                if not price.is_finite() or not 0 < price <= 1000000:
                    raise ValueError("price")
            except (KeyError, TypeError, ValueError, InvalidOperation):
                raise ProviderError("瑞安航空日历日期、可售状态或价格字段无效") from None
            rate, rate_day = cny_rate(currency, today, self._request)
            total = (price * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            query = urllib.parse.urlencode({"adults": 1, "teens": 0, "children": 0, "infants": 0,
                    "dateOut": day.isoformat(), "isReturn": "false", "originIata": origin, "destinationIata": destination})
            quotes.append(Quote(route.origin, route.destination, day, total, "CNY", "瑞安航空官网日历",
                airline="Ryanair", provider="ryanair", price_basis="unknown",
                original_price=price, original_currency=currency, exchange_rate=rate, exchange_date=rate_day,
                url="https://www.ryanair.com/gb/en/trip/flights/select?" + query,
                price_note=f"瑞安航空 {origin}→{destination}，1 成人单程参考票价；原价 {currency} {price}，"
                           f"按 {rate_day} Frankfurter 参考汇率 1 {currency}={rate} CNY 折算；"
                           "日历未单列税费，含税口径未确认，不参与最低总价/阈值；"
                           "支付汇率、行李及最终可售总价请在官网确认"))
        return quotes
