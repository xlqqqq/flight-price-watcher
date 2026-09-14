import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from flightwatch import exchange
from flightwatch.airasia_source import AirAsiaProvider, ROOT
from flightwatch.models import ProviderError, ProviderUnsupported, Route


TODAY = date(2026, 9, 14)
DAY = date(2026, 10, 15)
ROUTE = Route("aa", "吉隆坡→曼谷", "KUL", "DMK", "airasia",
              market="international", dates=(DAY,))
RATE = {"amount": 1, "base": "MYR", "date": "2026-09-14", "rates": {"CNY": 1.7132}}


def payload(day=DAY):
    rows = [
        {"carrier": {"code": "AK", "name": "AirAsia"}, "flightNumber": "AK896",
         "departure": {"time": "19:40", "date": day.isoformat()},
         "arrival": {"time": "20:50", "date": day.isoformat()}, "duration": 7800,
         "stops": 0, "layover": {"stops": 0, "details": []},
         "price": {"amount": 376, "currency": "MYR"}},
        {"carrier": {"code": "AK", "name": "AirAsia"}, "flightNumber": "AK888",
         "departure": {"time": "18:00", "date": day.isoformat()},
         "arrival": {"time": "19:20", "date": day.isoformat()}, "duration": 8400,
         "stops": 0, "layover": {"stops": 0, "details": []},
         "price": {"amount": 387, "currency": "MYR"}},
    ]
    return {"success": True, "data": {"flights": rows, "actualPrice": 376,
        "timestamp": datetime.now(timezone.utc).isoformat(), "cacheSource": "live", "fromCache": False,
        "meta": {"cheapestPrice": {"amount": 376, "currency": "MYR"},
                 "searchParams": {"origin": "KUL", "destination": "DMK",
                    "departureDate": day.isoformat(), "sortBy": "cheapest", "limit": 6,
                    "currency": "MYR", "language": "en-gb"}}}}


def rsc(value):
    return ("0:{\"a\":\"$@1\"}\n1:" + json.dumps(value, separators=(",", ":")) + "\n").encode()


class AirAsiaTests(unittest.TestCase):
    def setUp(self):
        exchange._CACHE.clear()
        self.provider = AirAsiaProvider(request_delay=0)

    def test_exact_route_date_currency_and_airline_are_parsed(self):
        item = self.provider._parse_action(rsc(payload()), ROUTE, DAY)
        self.assertEqual(item, (Decimal("376"), "AK", "AirAsia", "AK896", 0))

    def test_wrong_query_echo_or_departure_date_is_rejected(self):
        for mutate in ("origin", "date"):
            value = payload()
            if mutate == "origin":
                value["data"]["meta"]["searchParams"]["origin"] = "PEN"
            else:
                value["data"]["flights"][0]["departure"]["date"] = "2026-10-16"
            with self.subTest(mutate=mutate), self.assertRaises(ProviderError):
                self.provider._parse_action(rsc(value), ROUTE, DAY)

    def test_duplicate_json_key_and_nonfinite_constant_are_rejected(self):
        duplicate = b'1:{"success":true,"success":true,"data":{}}\n'
        nonfinite = b'1:{"success":true,"data":{"actualPrice":NaN}}\n'
        for raw in (duplicate, nonfinite):
            with self.subTest(raw=raw), self.assertRaises(ProviderError):
                self.provider._parse_action(raw, ROUTE, DAY)

    def test_unsorted_or_internally_conflicting_prices_are_rejected(self):
        value = payload()
        value["data"]["flights"][1]["price"]["amount"] = 300
        with self.assertRaisesRegex(ProviderError, "最低价顺序"):
            self.provider._parse_action(rsc(value), ROUTE, DAY)
        value = payload()
        value["data"]["actualPrice"] = 1
        with self.assertRaisesRegex(ProviderError, "互相矛盾"):
            self.provider._parse_action(rsc(value), ROUTE, DAY)

    def test_non_airasia_or_connection_is_not_called_self_operated(self):
        value = payload()
        value["data"]["flights"][0]["carrier"] = {"code": "OD", "name": "Batik Air"}
        value["data"]["flights"][1]["stops"] = 1
        value["data"]["flights"][1]["layover"]["stops"] = 1
        self.assertIsNone(self.provider._parse_action(rsc(value), ROUTE, DAY))

    def test_search_converts_original_price_but_keeps_unknown_basis(self):
        route = Route(
            **{**ROUTE.__dict__, "origin_scope": "airport",
               "destination_scope": "airport", "origin_city_code": "KUL",
               "destination_city_code": "BKK"}
        )
        calls = []
        def request(url, **kwargs):
            calls.append((url, kwargs))
            if url == ROOT:
                return rsc(payload()), "text/x-component", ROOT
            self.assertEqual(urlsplit(url).hostname, "api.frankfurter.dev")
            return json.dumps(RATE).encode(), "application/json", url
        with patch.object(self.provider, "_discover_action_id", return_value="a" * 40), \
             patch.object(self.provider, "_request", side_effect=request):
            result = self.provider.search(route, TODAY)
        quote = result.quotes[0]
        self.assertEqual(quote.price, Decimal("644.16"))
        self.assertEqual((quote.original_price, quote.original_currency), (Decimal("376"), "MYR"))
        self.assertFalse(quote.comparable)
        self.assertEqual(quote.flight_number, "AK896")
        self.assertEqual((quote.origin_airport, quote.destination_airport), ("KUL", "DMK"))
        query = parse_qs(urlsplit(quote.url).query)
        self.assertEqual(query["departDate"], ["15/10/2026"])
        self.assertEqual(query["isAirasiaFlightOnly"], ["true"])
        action_body = json.loads(calls[0][1]["data"])
        self.assertEqual(action_body[0]["date"], DAY.isoformat())
        self.assertEqual(action_body[0]["limit"], 6)

    def test_dynamic_action_is_discovered_from_current_page_chunk(self):
        # The current production build uses a 42-hex-character action ID.
        action = "1" * 42
        html = ('<script src="/flights/_next/static/chunks/shared.js"></script>'
                'I[123,[\\"/flights/_next/static/chunks/flight.js\\"],\\"default\\"]'
                '"initialFlights"').encode()
        script = f'let x=(0,t.createServerReference)("{action}",t.callServer,0,0,"fetchFlightsForDate")'.encode()
        with patch.object(self.provider, "_request", side_effect=[
            (html, "text/html", "https://www.airasia.com/flights/from-kuala-lumpur-kul-to-bangkok-don-mueang-dmk/"),
            (script, "application/javascript", "https://www.airasia.com/flights/_next/static/chunks/flight.js"),
        ]) as request:
            self.assertEqual(self.provider._discover_action_id(), action)
        self.assertEqual(request.call_count, 2)

    def test_old_nonlive_cache_is_force_refreshed_before_use(self):
        stale = payload()
        stale["data"]["timestamp"] = "2026-01-01T00:00:00Z"
        stale["data"]["cacheSource"] = "gcs"
        fresh = payload()
        action_calls = []
        def request(url, **kwargs):
            if url == ROOT:
                action_calls.append(json.loads(kwargs["data"])[0])
                value = stale if len(action_calls) == 1 else fresh
                return rsc(value), "text/x-component", ROOT
            return json.dumps(RATE).encode(), "application/json", url
        with patch.object(self.provider, "_discover_action_id", return_value="a" * 40), \
             patch.object(self.provider, "_request", side_effect=request):
            result = self.provider.search(ROUTE, TODAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertNotIn("forceRefresh", action_calls[0])
        self.assertIs(action_calls[1]["forceRefresh"], True)

    def test_first_protocol_failure_stops_before_later_dates(self):
        route = copy.copy(ROUTE)
        route = Route(**{**route.__dict__, "dates": (DAY, DAY + timedelta(days=1))})
        with patch.object(self.provider, "_discover_action_id", return_value="a" * 40), \
             patch.object(self.provider, "_request", return_value=(b"<html>", "text/html", ROOT)) as request, \
             self.assertRaisesRegex(ProviderError, DAY.isoformat()):
            self.provider.search(route, TODAY)
        request.assert_called_once()

    def test_domestic_and_unsupported_filters_never_request(self):
        variants = [
            Route(**{**ROUTE.__dict__, "market": "domestic"}),
            Route(**{**ROUTE.__dict__, "currency": "USD"}),
            Route(**{**ROUTE.__dict__, "stay_nights": 2}),
            Route(**{**ROUTE.__dict__, "travel_class": 2}),
        ]
        for route in variants:
            with self.subTest(route=route), patch.object(self.provider, "_discover_action_id") as discover, \
                 self.assertRaises(ProviderUnsupported):
                self.provider.search(route, TODAY)
            discover.assert_not_called()

    def test_airport_scope_without_owner_never_discovers_or_requests(self):
        route = Route(
            **{**ROUTE.__dict__, "origin_scope": "airport", "origin_city_code": ""}
        )
        with patch.object(self.provider, "_discover_action_id") as discover, \
                patch.object(self.provider, "_request") as request, \
                self.assertRaises(ProviderUnsupported):
            self.provider.search(route, TODAY)
        discover.assert_not_called()
        request.assert_not_called()

    def test_complete_date_budget_is_checked_before_discovery(self):
        provider = AirAsiaProvider(max_requests=4)
        with patch.object(provider, "_discover_action_id") as discover, self.assertRaisesRegex(ProviderError, "请求上限"):
            provider.search(ROUTE, TODAY)
        discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
