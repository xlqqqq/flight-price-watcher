"""Google Flights prices through SerpApi's documented JSON API.

Schema: https://serpapi.com/google-flights-api
An instance owns one polling cycle's request allowance; create a new instance
for the next cycle. Returned prices are itinerary totals for one adult, and
round-trip offers require selecting a return flight on the booking site.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import ProviderError, Quote, Route, SearchResult


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
ENDPOINT = "https://serpapi.com/search.json"


class SerpApiProvider:
    def __init__(
        self,
        api_key: str,
        timeout: float = 30,
        request_delay: float = 1.0,
        max_requests: int = 60,
    ):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout 必须是有限正数")
        if not math.isfinite(request_delay) or request_delay < 0:
            raise ValueError("request_delay 必须是有限非负数")
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
            raise ValueError("max_requests 必须是正整数")
        self._api_key = api_key.strip() if isinstance(api_key, str) else ""
        self.timeout = timeout
        self.request_delay = request_delay
        self.max_requests = max_requests
        self._requests_used = 0

    def search(self, route: Route, today: date) -> SearchResult:
        if not self._api_key:
            raise ProviderError("缺少 SERPAPI_API_KEY，请在 .env 中配置后查询实时机票")
        days = route.departure_dates(today)
        quotes: list[Quote] = []
        warnings: list[str] = []
        successes = 0
        for index, day in enumerate(days):
            if self._requests_used >= self.max_requests:
                warnings.append(f"SerpApi 本轮请求上限已达 {self.max_requests}，跳过 {len(days) - index} 个日期")
                break
            try:
                payload = self._request(route, day)
                found, rejected = self._parse(payload, route, day)
            except ProviderError as exc:
                warnings.append(f"{day.isoformat()}：{exc}")
                continue
            successes += 1
            quotes.extend(found)
            if rejected:
                warnings.append(f"{day.isoformat()}：已忽略 {rejected} 条价格或航班信息无效的结果")
        if days and successes == 0:
            raise ProviderError("SerpApi 本次未能成功查询任何日期；" + "；".join(warnings))
        # Both groups are considered: Google's 'best' group is not necessarily cheapest.
        quotes.sort(key=lambda item: (item.price, item.departure_date, item.flight_number))
        return SearchResult(quotes, warnings)

    def _request(self, route: Route, departure: date) -> dict[str, Any]:
        returning = route.return_on(departure)
        params: dict[str, Any] = {
            "engine": "google_flights",
            "api_key": self._api_key,
            "departure_id": route.origin,
            "arrival_id": route.destination,
            "outbound_date": departure.isoformat(),
            "type": 1 if returning else 2,
            "currency": route.currency,
            "hl": "zh-cn",
            "gl": "us",
            "adults": 1,
            "travel_class": route.travel_class,
            "sort_by": 2,
            "deep_search": "true",
            "show_hidden": "true",
        }
        if returning:
            params["return_date"] = returning.isoformat()
        if route.nonstop:
            params["stops"] = 1
        if self._requests_used and self.request_delay:
            time.sleep(self.request_delay)
        # Count failed attempts too: they still consume network/API resources.
        self._requests_used += 1
        request = urllib.request.Request(
            ENDPOINT + "?" + urllib.parse.urlencode(params),
            headers={"Accept": "application/json", "User-Agent": "flight-price-watcher/1.0"},
        )
        try:
            # urllib's default HTTPS handler verifies certificates and hostnames.
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Never interpolate exception text, response bodies or request URLs: the
            # documented endpoint carries the API key in its query parameters.
            if exc.code in (401, 403):
                message = "SerpApi 鉴权失败，请检查 API Key 和账户权限"
            elif exc.code == 429:
                message = "SerpApi 请求过于频繁或配额不足，请降低频率并检查账户额度"
            else:
                message = f"SerpApi HTTP 请求失败（状态码 {exc.code}）"
            raise ProviderError(message) from None
        except (OSError, http.client.HTTPException, ValueError):
            raise ProviderError("SerpApi 网络请求失败，请检查网络、证书和超时设置") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("SerpApi 响应超过大小上限")
        try:
            payload = json.loads(raw, parse_float=Decimal)
        except (ValueError, UnicodeError, RecursionError):
            raise ProviderError("SerpApi 返回的内容不是有效 JSON") from None
        if not isinstance(payload, dict):
            raise ProviderError("SerpApi 返回的 JSON 结构异常")
        return payload

    def _parse(self, payload: dict[str, Any], route: Route, departure: date) -> tuple[list[Quote], int]:
        metadata = payload.get("search_metadata", {})
        if not isinstance(metadata, dict):
            raise ProviderError("SerpApi 查询状态结构异常")
        if payload.get("error"):
            raise ProviderError("SerpApi 返回查询错误，请检查 API Key、额度及航线日期参数")
        if metadata.get("status") not in (None, "Success"):
            raise ProviderError("SerpApi 查询未成功完成，请稍后重试")
        groups = [payload[key] for key in ("best_flights", "other_flights") if key in payload]
        if not groups or any(not isinstance(group, list) for group in groups):
            raise ProviderError("SerpApi 缺少有效航班列表，不能将异常响应视为无票")
        parameters = payload.get("search_parameters", {})
        if not isinstance(parameters, dict):
            raise ProviderError("SerpApi 查询参数结构异常")
        if parameters.get("currency", route.currency) != route.currency:
            raise ProviderError("SerpApi 响应币种与请求不符，已拒绝比较价格")
        if parameters.get("outbound_date", departure.isoformat()) != departure.isoformat():
            raise ProviderError("SerpApi 响应日期与请求不符")
        returning = route.return_on(departure)
        if returning and parameters.get("return_date", returning.isoformat()) != returning.isoformat():
            raise ProviderError("SerpApi 响应回程日期与请求不符")
        url = self._public_url(metadata.get("google_flights_url"), route, departure)
        quotes: list[Quote] = []
        rejected = 0
        for group in groups:
            for offer in group:
                quote = self._quote(offer, route, departure, url)
                if quote is None:
                    rejected += 1
                else:
                    quotes.append(quote)
        if rejected and not quotes:
            raise ProviderError("SerpApi 航班列表中没有价格、日期及行程信息均有效的报价")
        return quotes, rejected

    def _quote(self, offer: Any, route: Route, departure: date, url: str) -> Quote | None:
        if not isinstance(offer, dict) or isinstance(offer.get("price"), bool):
            return None
        try:
            price = Decimal(str(offer.get("price")))
        except (InvalidOperation, ValueError):
            return None
        if not price.is_finite() or price <= 0:
            return None
        flights = offer.get("flights")
        if not isinstance(flights, list) or not flights or not all(isinstance(flight, dict) for flight in flights):
            return None
        first = flights[0].get("departure_airport")
        last = flights[-1].get("arrival_airport")
        if not isinstance(first, dict) or not isinstance(last, dict):
            return None
        if (not isinstance(first.get("id"), str)
                or not re.fullmatch(r"[A-Z]{3}", first["id"])
                or not isinstance(last.get("id"), str)
                or not re.fullmatch(r"[A-Z]{3}", last["id"])):
            return None
        raw_time = first.get("time")
        if not isinstance(raw_time, str):
            return None
        try:
            actual_day = date.fromisoformat(raw_time[:10])
        except ValueError:
            return None
        if actual_day != departure:
            return None
        # Configured airport codes must match the itinerary. Location kgmids do
        # not identify individual airports and cannot be compared this way.
        if not self._airport_matches(route.origin, first.get("id")):
            return None
        if not self._airport_matches(route.destination, last.get("id")):
            return None
        returning = route.return_on(departure)
        expected_type = "Round trip" if returning else "One way"
        if offer.get("type", expected_type) != expected_type:
            return None
        stops = len(flights) - 1
        if route.nonstop and stops:
            return None
        airlines = dict.fromkeys(flight["airline"] for flight in flights if isinstance(flight.get("airline"), str))
        numbers = [flight["flight_number"] for flight in flights if isinstance(flight.get("flight_number"), str)]
        note = "平台显示的 1 位成人单程总价；税费、行李及最终可售价格请到购票页确认"
        if returning:
            note = "平台显示的 1 位成人往返总价；当前航班详情为去程，回程航班、行李及最终可售价格请到购票页确认"
        return Quote(
            origin=route.origin, destination=route.destination,
            departure_date=departure, return_date=returning,
            price=price, currency=route.currency, source="SerpApi / Google Flights",
            airline=" / ".join(airlines), flight_number=" / ".join(numbers),
            stops=stops, url=url, price_note=note,
            origin_airport=first["id"], destination_airport=last["id"],
        )

    @staticmethod
    def _airport_matches(configured: str, actual: Any) -> bool:
        ids = configured.split(",")
        if any(item.startswith(("/m/", "/g/")) for item in ids):
            return isinstance(actual, str) and bool(actual)
        return actual in ids

    def _public_url(self, candidate: Any, route: Route, departure: date) -> str:
        if isinstance(candidate, str) and self._api_key not in candidate:
            try:
                parsed = urllib.parse.urlsplit(candidate)
                keys = {key.lower() for key, _ in urllib.parse.parse_qsl(parsed.query)}
                if (parsed.scheme == "https" and parsed.hostname in {"google.com", "www.google.com", "flights.google.com"}
                        and parsed.username is None and parsed.password is None
                        and parsed.path.startswith(("/travel/flights", "/flights"))
                        and not keys.intersection({"api_key", "key", "token", "access_token"})):
                    return candidate
            except ValueError:
                pass
        query = f"Flights from {route.origin} to {route.destination} on {departure.isoformat()}"
        returning = route.return_on(departure)
        query += f" returning {returning.isoformat()}" if returning else " one way"
        return "https://www.google.com/travel/flights?" + urllib.parse.urlencode(
            {"hl": "zh-CN", "curr": route.currency, "q": query})
