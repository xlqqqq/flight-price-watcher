"""Mock fixtures follow https://serpapi.com/google-flights-api (2026-09-08).

These tests validate documented request/response handling, not live inventory
or availability: no paid API key is used or required by the test suite.
"""

import copy
import io
import json
import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from flightwatch.models import ProviderError, Route
from flightwatch.serpapi_source import MAX_RESPONSE_BYTES, SerpApiProvider


TODAY = date(2026, 9, 8)
DAY = date(2026, 9, 20)
SECRET = "private-test-key-do-not-print"


def route(**overrides):
    values = dict(id="pek-sha", name="北京到上海", origin="PEK", destination="SHA",
                  provider="serpapi", dates=(DAY,))
    values.update(overrides)
    return Route(**values)


def offer(price=680, origin="PEK", destination="SHA", day=DAY, trip_type="One way"):
    return {
        "flights": [{
            "departure_airport": {"id": origin, "time": f"{day.isoformat()} 08:30"},
            "arrival_airport": {"id": destination, "time": f"{day.isoformat()} 10:40"},
            "airline": "China Eastern", "flight_number": "MU 5102",
        }],
        "price": price, "type": trip_type,
    }


def payload(best=None, other=None):
    return {
        "search_metadata": {"status": "Success", "google_flights_url": "https://www.google.com/travel/flights?hl=en"},
        "search_parameters": {"currency": "CNY", "outbound_date": DAY.isoformat()},
        "best_flights": best if best is not None else [offer()],
        "other_flights": other or [],
        "price_insights": {"lowest_price": 1, "price_history": [[0, 1]]},
    }


def response(data):
    return io.BytesIO(json.dumps(data).encode("utf-8"))


class SerpApiTests(unittest.TestCase):
    def run_search(self, data, monitored=None, **options):
        provider = SerpApiProvider(SECRET, request_delay=0, **options)
        with patch("flightwatch.serpapi_source.urllib.request.urlopen", return_value=response(data)) as opened:
            result = provider.search(monitored or route(), TODAY)
        return result, opened

    def test_domestic_and_other_flights_cheapest_no_price_insights(self):
        result, opened = self.run_search(payload([offer(900)], [offer(480)]), route(nonstop=True, travel_class=3))
        self.assertEqual([item.price for item in result.quotes], [Decimal(480), Decimal(900)])
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.quotes[0].stops, 0)
        request = opened.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).scheme, "https")
        self.assertEqual(urlsplit(request.full_url).netloc, "serpapi.com")
        params = parse_qs(urlsplit(request.full_url).query)
        expected = dict(engine="google_flights", departure_id="PEK", arrival_id="SHA", currency="CNY",
                        hl="zh-cn", gl="us", type="2", sort_by="2", stops="1", adults="1",
                        travel_class="3", deep_search="true", show_hidden="true", api_key=SECRET)
        for key, value in expected.items():
            self.assertEqual(params[key], [value])
        self.assertNotIn("return_date", params)
        self.assertEqual(opened.call_args.kwargs["timeout"], 30)

    def test_international_roundtrip_uses_itinerary_total(self):
        data = payload([offer(2800, destination="NRT", trip_type="Round trip")])
        result, opened = self.run_search(data, route(destination="NRT", stay_nights=7))
        quote = result.quotes[0]
        self.assertEqual(quote.price, Decimal(2800))
        self.assertEqual(quote.return_date, DAY + timedelta(days=7))
        self.assertIn("往返总价", quote.price_note)
        self.assertIn("回程航班", quote.price_note)
        params = parse_qs(urlsplit(opened.call_args.args[0].full_url).query)
        self.assertEqual(params["type"], ["1"])
        self.assertEqual(params["return_date"], ["2026-09-27"])
        self.assertNotIn("stops", params)

    def test_international_connection_and_decimal_price(self):
        item = offer(1400.50, destination="HKG")
        item["flights"].append(offer(origin="HKG", destination="SIN")["flights"][0])
        result, _ = self.run_search(payload([], [item]), route(destination="SIN"))
        self.assertEqual(result.quotes[0].stops, 1)
        self.assertEqual(result.quotes[0].price, Decimal("1400.5"))

    def test_reject_zero_negative_nan_infinite_null_boolean_and_bad_price(self):
        invalid = [0, -5, float("nan"), float("inf"), None, True, "free"]
        result, _ = self.run_search(payload([offer(value) for value in invalid] + [offer(500)]))
        self.assertEqual([item.price for item in result.quotes], [Decimal(500)])
        self.assertIn("7 条", result.warnings[0])

    def test_reject_wrong_departure_route_and_trip_type(self):
        invalid = [offer(day=DAY + timedelta(days=1)), offer(origin="PVG"), offer(destination="NRT"),
                   offer(trip_type="Round trip"), {"price": 99, "flights": []}]
        result, _ = self.run_search(payload(invalid + [offer()]))
        self.assertEqual(len(result.quotes), 1)
        self.assertIn("5 条", result.warnings[0])

    def test_valid_empty_lists_and_missing_best_group(self):
        result, _ = self.run_search(payload([], []))
        self.assertEqual(result.quotes, [])
        self.assertEqual(result.warnings, [])
        data = payload([], [offer(480)])
        del data["best_flights"]
        result, _ = self.run_search(data)
        self.assertEqual(result.quotes[0].price, Decimal(480))

    def test_malformed_responses_and_api_error_are_not_no_tickets(self):
        cases = [[], {}, {"search_metadata": {"status": "Processing"}}, {"best_flights": None},
                 {"error": f"URL https://serpapi.com/search.json?api_key={SECRET}"},
                 payload([offer(None)]), {"price_insights": {"lowest_price": 500}}]
        for data in cases:
            with self.subTest(data_type=type(data).__name__):
                with self.assertRaises(ProviderError) as raised:
                    self.run_search(data)
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertNotIn("https://serpapi.com", str(raised.exception))

    def test_currency_and_return_date_must_match_query(self):
        data = payload([offer(trip_type="Round trip")])
        for field, value in [("currency", "USD"), ("return_date", "2026-10-01")]:
            changed = copy.deepcopy(data)
            changed["search_parameters"][field] = value
            with self.subTest(field=field), self.assertRaises(ProviderError):
                self.run_search(changed, route(stay_nights=7))

    def test_missing_key_makes_no_network_request(self):
        with patch("flightwatch.serpapi_source.urllib.request.urlopen") as opened:
            with self.assertRaisesRegex(ProviderError, "SERPAPI_API_KEY"):
                SerpApiProvider("  ").search(route(), TODAY)
            opened.assert_not_called()

    def test_network_errors_do_not_expose_urls_keys_or_exception_chains(self):
        secret_url = f"https://serpapi.com/search.json?api_key={SECRET}"
        for error in [HTTPError(secret_url, 401, SECRET, {}, None),
                      HTTPError(secret_url, 429, SECRET, {}, None), URLError(secret_url), TimeoutError(SECRET)]:
            with self.subTest(error_type=type(error).__name__):
                with patch("flightwatch.serpapi_source.urllib.request.urlopen", side_effect=error):
                    with self.assertRaises(ProviderError) as raised:
                        SerpApiProvider(SECRET).search(route(), TODAY)
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertNotIn("https://serpapi.com", str(raised.exception))

    def test_request_limit_is_shared_across_searches_and_counts_failures(self):
        provider = SerpApiProvider(SECRET, request_delay=0, max_requests=2)
        days = (DAY, DAY + timedelta(days=1), DAY + timedelta(days=2))
        with patch("flightwatch.serpapi_source.urllib.request.urlopen",
                   side_effect=[response(payload()), URLError(SECRET)]) as opened:
            result = provider.search(route(dates=days), TODAY)
            self.assertEqual(opened.call_count, 2)
            self.assertEqual(len(result.quotes), 1)
            self.assertTrue(any("上限" in warning for warning in result.warnings))
            with self.assertRaisesRegex(ProviderError, "上限"):
                provider.search(route(), TODAY)
            self.assertEqual(opened.call_count, 2)

    def test_rate_limit_applies_between_routes(self):
        provider = SerpApiProvider(SECRET, request_delay=1.5)
        with patch("flightwatch.serpapi_source.time.sleep") as sleep, patch(
                "flightwatch.serpapi_source.urllib.request.urlopen", side_effect=[response(payload()), response(payload())]):
            provider.search(route(), TODAY)
            provider.search(route(), TODAY)
            sleep.assert_called_once_with(1.5)

    def test_partial_date_failure_preserves_success_and_date_warning(self):
        days = (DAY, DAY + timedelta(days=1))
        with patch("flightwatch.serpapi_source.urllib.request.urlopen", side_effect=[response(payload()), URLError(SECRET)]):
            result = SerpApiProvider(SECRET, request_delay=0).search(route(dates=days), TODAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertIn("2026-09-21", result.warnings[0])
        self.assertNotIn(SECRET, str(result.warnings))

    def test_invalid_json_and_response_size_bound(self):
        for data in [b"<html>not JSON</html>", b"x" * (MAX_RESPONSE_BYTES + 1)]:
            with patch("flightwatch.serpapi_source.urllib.request.urlopen", return_value=io.BytesIO(data)):
                with self.assertRaises(ProviderError):
                    SerpApiProvider(SECRET).search(route(), TODAY)

    def test_public_link_rejects_private_endpoint_or_secret(self):
        for url in [f"https://serpapi.com/search.json?api_key={SECRET}",
                    f"https://www.google.com/travel/flights?api_key={SECRET}", "javascript:alert(1)"]:
            data = payload()
            data["search_metadata"]["google_flights_url"] = url
            result, _ = self.run_search(data)
            self.assertNotIn(SECRET, result.quotes[0].url)
            self.assertEqual(urlsplit(result.quotes[0].url).hostname, "www.google.com")

    def test_past_dates_do_not_query(self):
        with patch("flightwatch.serpapi_source.urllib.request.urlopen") as opened:
            result = SerpApiProvider(SECRET).search(route(dates=(TODAY - timedelta(days=1),)), TODAY)
        self.assertEqual(result.quotes, [])
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
