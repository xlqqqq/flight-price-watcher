import copy
import io
import json
import unittest
import urllib.error
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from flightwatch.models import ProviderError, Route
from flightwatch.public_source import CtripCalendarProvider, calendar_date


FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 9, 8)


class CtripCalendarTests(unittest.TestCase):
    def setUp(self):
        self.domestic = json.loads((FIXTURES / "ctrip_domestic.json").read_text())
        self.international = json.loads((FIXTURES / "ctrip_international.json").read_text())
        self.route = Route(
            id="sha-bjs", name="上海到北京", origin="SHA", destination="BJS",
            provider="ctrip", dates=(date(2026, 9, 9), date(2026, 9, 10)),
        )
        self.provider = CtripCalendarProvider(request_delay=0)

    def test_live_domestic_fixture_uses_total_with_fees_and_exact_dates(self):
        with patch.object(self.provider, "_request", return_value=self.domestic) as request:
            result = self.provider.search(self.route, TODAY)
        self.assertEqual([quote.price for quote in result.quotes], [Decimal("480"), Decimal("530")])
        self.assertEqual([quote.departure_date for quote in result.quotes], list(self.route.dates))
        self.assertEqual(result.warnings, [])
        payload = request.call_args.args[0]
        self.assertEqual(payload["searchType"], 1)
        self.assertEqual(payload["startDate"], "2026-09-09")
        self.assertEqual(payload["head"]["auth"], "")
        self.assertEqual(payload["passengerList"], [{"passengerCount": 1, "passengerType": "Adult"}])

    def test_live_international_fixture_uses_total_and_ignores_negative_return_placeholder(self):
        route = replace(self.route, destination="TYO", market="international")
        with patch.object(self.provider, "_request", return_value=self.international) as request:
            result = self.provider.search(route, TODAY)
        self.assertEqual([quote.price for quote in result.quotes], [Decimal("1086.0"), Decimal("962.0")])
        self.assertTrue(all(quote.return_date is None for quote in result.quotes))
        self.assertEqual(request.call_args.args[0]["searchType"], 2)
        self.assertIn("默认舱位参考", result.quotes[0].price_note)

    def test_zero_is_unavailable_not_a_free_flight(self):
        self.domestic["priceList"][1]["totalPrice"] = 0
        with patch.object(self.provider, "_request", return_value=self.domestic):
            result = self.provider.search(self.route, TODAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertIn("1 个", result.warnings[0])

    def test_missing_total_never_falls_back_to_base_fare(self):
        del self.domestic["priceList"][1]["totalPrice"]
        with patch.object(self.provider, "_request", return_value=self.domestic):
            with self.assertRaises(ProviderError):
                self.provider.search(self.route, TODAY)

    def test_nonfinite_boolean_negative_and_nonnumeric_prices_rejected(self):
        for value in (float("nan"), float("inf"), True, -10, None, "CALL"):
            with self.subTest(value=value):
                data = copy.deepcopy(self.domestic)
                data["priceList"][1]["totalPrice"] = value
                with patch.object(self.provider, "_request", return_value=data):
                    with self.assertRaises(ProviderError):
                        self.provider.search(self.route, TODAY)

    def test_response_errors_schema_changes_and_real_return_dates_rejected(self):
        invalid = []
        data = copy.deepcopy(self.domestic)
        data["responseStatus"]["Ack"] = "Failure"
        invalid.append(data)
        invalid.append({"responseStatus": {"Ack": "Success"}, "priceList": {}})
        data = copy.deepcopy(self.domestic)
        data["priceList"][1]["returnDate"] = "/Date(1788883200000+0800)/"
        invalid.append(data)
        data = copy.deepcopy(self.domestic)
        data["priceList"][1]["departDate"] = "09/09/2026"
        invalid.append(data)
        for data in invalid:
            with self.subTest(data=data):
                with patch.object(self.provider, "_request", return_value=data):
                    with self.assertRaises(ProviderError):
                        self.provider.search(self.route, TODAY)

    def test_unsupported_filters_fail_before_network(self):
        changes = [dict(currency="USD"), dict(stay_nights=3), dict(nonstop=True),
                   dict(travel_class=3), dict(market="unknown"), dict(origin="SHA,PVG")]
        with patch.object(self.provider, "_request") as request:
            for fields in changes:
                with self.subTest(fields=fields):
                    with self.assertRaises(ProviderError):
                        self.provider.search(replace(self.route, **fields), TODAY)
            request.assert_not_called()

    def test_http_block_and_html_challenge_report_provider_error(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                "https://m.ctrip.com/", 432, "blocked", {}, None)):
            with self.assertRaisesRegex(ProviderError, "HTTP 432"):
                self.provider.search(self.route, TODAY)
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"<title>Challenge Validation</title>")):
            with self.assertRaisesRegex(ProviderError, "JSON"):
                self.provider.search(self.route, TODAY)

    def test_request_limit_shared_across_routes_in_same_cycle(self):
        provider = CtripCalendarProvider(max_requests=1, request_delay=0)
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(self.domestic).encode())) as request:
            provider.search(self.route, TODAY)
            with self.assertRaisesRegex(ProviderError, "请求上限"):
                provider.search(replace(self.route, id="second"), TODAY)
            self.assertEqual(request.call_count, 1)

    def test_explicit_timezone_does_not_shift_chinese_departure_day(self):
        self.assertEqual(calendar_date("/Date(1788796800000+0800)/"), date(2026, 9, 8))
        self.assertEqual(calendar_date("/Date(1788796800000+0000)/"), date(2026, 9, 7))
        with self.assertRaises(ValueError):
            calendar_date("/Date(1788796800000+0860)/")


if __name__ == "__main__":
    unittest.main()
