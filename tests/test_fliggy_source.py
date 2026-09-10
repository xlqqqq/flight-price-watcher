"""Minimized real-shape Fliggy response tests; no live network in the suite."""

import gzip
import http.client
import json
import threading
import unittest
import urllib.error
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from flightwatch.fliggy_source import FliggyProvider, parse_jsonp
from flightwatch.models import ProviderError, ProviderUnsupported, Route


DAY = date(2026, 9, 23)
ROUTE = Route("test", "北京上海", "BJS", "SHA", "multi", dates=(DAY,))


def flight(**changes):
    row = {
        "airlineCode": "MU", "flightNo": "MU5231", "depTime": "2026-09-23 22:00",
        "arrTime": "2026-09-24 00:10", "depAirport": "PKX", "arrAirport": "PVG",
        "oilPrice": 0, "buildPrice": 120, "stop": 0,
        "cabin": {"price": 410, "bestPrice": 400, "ticketPrice": 420, "notices": [],
                  "cabinClass": 2, "priceType": 0, "sprodType": 0,
                  "hasMemberPrice": False, "vip": False, "specialSale": False,
                  "fSpecialSale": False},
    }
    row.update(changes)
    return row


def response(rows=None):
    return {"data": {"depCityCode": "BJS", "arrCityCode": "SHA",
                     "aircodeNameMap": {"MU": "东航"},
                     "flight": [flight()] if rows is None else rows}}


def encode(data):
    return ("flightwatch(" + json.dumps(data) + ")").encode()


class FliggyParserTests(unittest.TestCase):
    def setUp(self):
        self.provider = FliggyProvider(request_delay=0)

    def test_actual_response_shape_without_status_field(self):
        quote = self.provider._parse(parse_jsonp(encode(response()).decode()), ROUTE, DAY).quotes[0]
        self.assertEqual(quote.price, Decimal("530"))
        self.assertEqual(quote.price_basis, "total")
        self.assertEqual(quote.provider, "fliggy")
        self.assertEqual(quote.currency, "CNY")
        self.assertEqual(quote.airline, "东航")
        self.assertEqual(quote.flight_number, "MU5231")
        self.assertIn("票价 410 + 税费 120", quote.price_note)

    def test_conditional_cheapest_is_excluded(self):
        restricted = flight(flightNo="KN5977")
        restricted["cabin"].update(price=387, notices=["限16-25周岁乘客可订"])
        result = self.provider._parse(response([restricted, flight()]), ROUTE, DAY)
        self.assertEqual(result.quotes[0].price, Decimal("530"))
        self.assertEqual(result.quotes[0].flight_number, "MU5231")
        self.assertTrue(any("预订限制" in warning for warning in result.warnings))

    def test_membership_bundles_application_and_other_cabins_excluded(self):
        cases = [{"hasMemberPrice": True}, {"vip": True}, {"specialSale": True},
                 {"fSpecialSale": True}, {"sprodType": 64}, {"priceType": 3},
                 {"cabinClass": 0}, {"notices": ["需要学生认证"]}]
        for changes in cases:
            row = flight()
            row["cabin"].update(changes)
            with self.subTest(changes=changes):
                self.assertFalse(self.provider._parse(response([row]), ROUTE, DAY).quotes)

    def test_missing_restriction_information_is_not_assumed_unrestricted(self):
        row = flight()
        del row["cabin"]["notices"]
        with self.assertRaisesRegex(ProviderError, "预订限制"):
            self.provider._parse(response([row]), ROUTE, DAY)

    def test_missing_member_marker_excludes_quote(self):
        row = flight()
        del row["cabin"]["hasMemberPrice"]
        self.assertFalse(self.provider._parse(response([row]), ROUTE, DAY).quotes)

    def test_minimum_compares_actual_totals(self):
        first, second = flight(), flight(flightNo="MU9999", oilPrice=10, buildPrice=20)
        second["cabin"]["price"] = 450
        quote = self.provider._parse(response([first, second]), ROUTE, DAY).quotes[0]
        self.assertEqual(quote.price, Decimal("480"))
        self.assertEqual(quote.flight_number, "MU9999")

    def test_missing_or_invalid_taxes_rejected(self):
        for field in ("oilPrice", "buildPrice"):
            for value in (None, True, "NaN", "Infinity", "-1", "abc", "1000001"):
                with self.subTest(field=field, value=value), self.assertRaises(ProviderError):
                    self.provider._parse(response([flight(**{field: value})]), ROUTE, DAY)

    def test_invalid_fare_rejected_and_zero_ignored(self):
        for value in (None, True, "NaN", "-1", "abc"):
            row = flight()
            row["cabin"]["price"] = value
            with self.subTest(value=value), self.assertRaises(ProviderError):
                self.provider._parse(response([row]), ROUTE, DAY)
        row["cabin"]["price"] = 0
        self.assertFalse(self.provider._parse(response([row]), ROUTE, DAY).quotes)

    def test_route_and_date_must_match_request(self):
        for field, value in (("depCityCode", "CAN"), ("arrCityCode", "CAN")):
            data = response()
            data["data"][field] = value
            with self.subTest(field=field), self.assertRaises(ProviderError):
                self.provider._parse(data, ROUTE, DAY)
        with self.assertRaisesRegex(ProviderError, "日期"):
            self.provider._parse(response([flight(depTime="2026-09-24 22:00")]), ROUTE, DAY)

    def test_transfer_total_is_not_guessed_from_first_leg(self):
        data = response([{"isTransfer": True, "transferFlight": [{"totalInfo": {"lowestPrice": 1}}]}, flight()])
        result = self.provider._parse(data, ROUTE, DAY)
        self.assertEqual(result.quotes[0].price, Decimal("530"))
        self.assertTrue(any("中转" in warning for warning in result.warnings))

    def test_error_envelopes_and_changed_schema_rejected(self):
        for data in ({}, {"errorMsg": "请验证", **response()}, {"status": 403, **response()},
                     {"success": False, **response()}, {"data": {"flight": []}}):
            with self.subTest(data=data), self.assertRaises(ProviderError):
                self.provider._parse(data, ROUTE, DAY)

    def test_empty_results_are_not_a_zero_price(self):
        result = self.provider._parse(response([]), ROUTE, DAY)
        self.assertFalse(result.quotes)
        self.assertTrue(result.warnings)

    def test_jsonp_accepts_only_json_in_exact_callback(self):
        self.assertEqual(parse_jsonp(' flightwatch({"data": {}}); '), {"data": {}})
        for payload in ('other({})', 'flightwatch({});alert(1)', 'flightwatch(fetch("x"))',
                        'flightwatch({"price":NaN})', 'flightwatch({"price":Infinity})',
                        '<html>验证码</html>', 'flightwatch([])'):
            with self.subTest(payload=payload), self.assertRaises(ProviderError):
                parse_jsonp(payload)


class FliggyTransportTests(unittest.TestCase):
    def test_unsupported_filters_do_not_access_network(self):
        provider = FliggyProvider()
        for changes in (dict(market="international"), dict(stay_nights=7), dict(currency="USD"),
                        dict(travel_class=2), dict(nonstop=True)):
            with self.subTest(changes=changes), patch.object(provider, "_request") as request:
                with self.assertRaises(ProviderUnsupported):
                    provider.search(replace(ROUTE, **changes), DAY)
                request.assert_not_called()

    def test_bad_city_code_rejected_before_network(self):
        provider = FliggyProvider()
        with patch.object(provider, "_request") as request, self.assertRaises(ProviderError):
            provider.search(replace(ROUTE, origin="../"), DAY)
        request.assert_not_called()

    def test_transport_handles_gzip_and_uses_no_credentials(self):
        web_response = MagicMock()
        web_response.__enter__.return_value.read.return_value = gzip.compress(encode(response()))
        with patch("flightwatch.fliggy_source.urllib.request.urlopen", return_value=web_response) as open_url:
            result = FliggyProvider(request_delay=0).search(ROUTE, DAY)
        self.assertEqual(result.quotes[0].price, Decimal("530"))
        request = open_url.call_args.args[0]
        self.assertIsNone(request.get_header("Cookie"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIn("depDate=2026-09-23", request.full_url)
        self.assertIn("callback=flightwatch", request.full_url)

    def test_partial_failure_preserves_other_days(self):
        provider = FliggyProvider(request_delay=0)
        route = replace(ROUTE, dates=(DAY, date(2026, 9, 24)))
        with patch.object(provider, "_request", side_effect=[response(), ProviderError("网络失败")]):
            result = provider.search(route, DAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertTrue(any("2026-09-24" in warning for warning in result.warnings))

    def test_all_failed_raises_provider_error(self):
        provider = FliggyProvider()
        with patch.object(provider, "_request", side_effect=ProviderError("验证页面")):
            with self.assertRaises(ProviderError):
                provider.search(ROUTE, DAY)

    def test_cancellation_stops_remaining_dates(self):
        stopped = threading.Event()
        provider = FliggyProvider(request_delay=0, cancelled=stopped.is_set)
        route = replace(ROUTE, dates=(DAY, date(2026, 9, 24)))

        def first_response(route, day):
            stopped.set()
            return response()

        with patch.object(provider, "_request", side_effect=first_response) as request:
            result = provider.search(route, DAY)
        request.assert_called_once()
        self.assertEqual(len(result.quotes), 1)
        self.assertTrue(any("已停止" in warning for warning in result.warnings))

    def test_cancellation_callback_can_be_replaced_for_reused_provider(self):
        provider = FliggyProvider(request_delay=0, cancelled=lambda: True)
        provider.cancelled = lambda: False
        with patch.object(provider, "_request", return_value=response()):
            self.assertEqual(len(provider.search(ROUTE, DAY).quotes), 1)
        provider.cancelled = lambda: True
        with patch.object(provider, "_request") as request, self.assertRaisesRegex(ProviderError, "已停止"):
            provider.search(ROUTE, DAY)
        request.assert_not_called()

    def test_cancellation_during_throttle_prevents_network(self):
        stopped = threading.Event()
        provider = FliggyProvider(request_delay=1, cancelled=stopped.is_set)
        provider._last_request = 0
        with patch("flightwatch.fliggy_source.time.monotonic", return_value=0), \
                patch("flightwatch.fliggy_source.time.sleep", side_effect=lambda _: stopped.set()), \
                patch("flightwatch.fliggy_source.urllib.request.urlopen") as open_url:
            with self.assertRaisesRegex(ProviderError, "已停止"):
                provider._request(ROUTE, DAY)
        open_url.assert_not_called()
        self.assertEqual(provider.requests_used, 0)

    def test_request_limit_shared_across_routes(self):
        provider = FliggyProvider(request_delay=0, max_requests=1)
        web_response = MagicMock()
        web_response.__enter__.return_value.read.return_value = encode(response())
        with patch("flightwatch.fliggy_source.urllib.request.urlopen", return_value=web_response) as open_url:
            provider.search(ROUTE, DAY)
            with self.assertRaisesRegex(ProviderError, "请求上限"):
                provider.search(ROUTE, DAY)
        self.assertEqual(open_url.call_count, 1)

    def test_truncated_response_becomes_provider_error(self):
        web_response = MagicMock()
        web_response.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b"x", 20)
        with patch("flightwatch.fliggy_source.urllib.request.urlopen", return_value=web_response):
            with self.assertRaisesRegex(ProviderError, "IncompleteRead"):
                FliggyProvider().search(ROUTE, DAY)

    def test_http_error_is_not_a_fare(self):
        error = urllib.error.HTTPError("https://sjipiao.fliggy.com/", 403, "Forbidden", {}, None)
        with patch("flightwatch.fliggy_source.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(ProviderError, "HTTP 403"):
                FliggyProvider().search(ROUTE, DAY)


if __name__ == "__main__":
    unittest.main()
