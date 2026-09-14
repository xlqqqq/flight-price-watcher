"""Minimized real public-page contracts; these tests never access Google."""

import copy
import io
import json
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from flightwatch.google_flights_source import GoogleFlightsProvider, ENDPOINT
from flightwatch.models import ProviderError, ProviderUnsupported, Route, SearchResult


DAY = date(2026, 9, 29)
TODAY = date(2026, 9, 9)
ROUTE = Route("google-test", "上海东京", "SHA", "TYO", "google_flights",
              dates=(DAY,), market="international")
FIXTURE = Path(__file__).parent / "fixtures" / "google_flights_public_sample.json"


def sample():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def html(data=None, currency="CNY", taxes=True):
    data = data or sample()
    return (
        '<html><button aria-label="Currency ' + currency + '"></button>'
        + ('<p>Prices include required taxes + fees for 1 adult.</p>' if taxes else '')
        + "<script class=\"ds:0\">AF_initDataCallback({key:'ds:0',data:"
        + json.dumps(data["query"]) + ",sideChannel:{}});</script>"
        + "<script class=\"ds:1\">AF_initDataCallback({key:'ds:1',data:"
        + json.dumps(data["data"]) + ",sideChannel:{}});</script></html>"
    )


class GooglePageTests(unittest.TestCase):
    def parse(self, page=None, route=ROUTE):
        return GoogleFlightsProvider()._parse_page(page or html(), route, DAY, ENDPOINT)

    def test_real_snapshot_returns_inclusive_cny_minimum(self):
        result = self.parse()
        self.assertEqual(len(result.quotes), 1)
        quote = result.quotes[0]
        self.assertEqual(quote.price, Decimal("2102"))
        self.assertEqual((quote.origin, quote.destination, quote.departure_date), ("SHA", "TYO", DAY))
        self.assertEqual(quote.currency, "CNY")
        self.assertTrue(quote.comparable)
        self.assertEqual(quote.flight_number, "BR705/BR108")
        self.assertEqual(quote.stops, 1)
        self.assertIn("未公布价格", result.warnings[0])

    def test_price_history_does_not_influence_quote_minimum(self):
        data = sample()
        data["data"][5] = [None, [None, 1]]
        self.assertEqual(self.parse(html(data)).quotes[0].price, Decimal("2102"))

    def test_missing_or_wrong_currency_is_not_silently_converted(self):
        for page in [html(currency="USD"), html().replace('aria-label="Currency CNY"', ''), html() + '<button aria-label="Currency USD"></button>']:
            with self.subTest(page=page[:100]), self.assertRaisesRegex(ProviderError, "币种"):
                self.parse(page)

    def test_explicit_tax_inclusion_required(self):
        with self.assertRaisesRegex(ProviderError, "含税"):
            self.parse(html(taxes=False))

    def test_changed_query_date_rejected(self):
        data = sample()
        data["query"][1][1][13][0][6] = "2026-09-30"
        with self.assertRaisesRegex(ProviderError, "日期"):
            self.parse(html(data))

    def test_round_trip_cabin_and_passenger_mismatches_rejected(self):
        for index, value in [(2, 1), (5, 3), (5, True), (6, [True, 0, 0, 0]), (6, [2, 0, 0, 0]), (6, [1, 1, 0, 0])]:
            data = sample()
            data["query"][1][1][index] = value
            with self.subTest(index=index, value=value), self.assertRaises(ProviderError):
                self.parse(html(data))
        data = sample()
        data["query"][1][1][13].append(copy.deepcopy(data["query"][1][1][13][0]))
        with self.assertRaises(ProviderError):
            self.parse(html(data))

    def test_wrong_city_and_airport_only_searches_rejected(self):
        for path, value in [((2, 0, 2, 5), "PEK"), ((2, 0, 0, 1), 0)]:
            data = sample()
            target = data["query"]
            for index in path[:-1]:
                target = target[index]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ProviderUnsupported):
                self.parse(html(data))

    def test_price_payload_city_must_match_query_echo(self):
        data = sample()
        data["data"][1][0][1][0][0][0] = "/m/other-city"
        with self.assertRaisesRegex(ProviderError, "城市不一致"):
            self.parse(html(data))

    def test_market_mismatch_rejected(self):
        with self.assertRaises(ProviderUnsupported):
            self.parse(route=replace(ROUTE, market="domestic"))

    def test_nearby_airport_price_excluded(self):
        data = sample()
        data["data"][3][0][0][0][3] = "HGH"
        result = self.parse(html(data))
        self.assertEqual(result.quotes[0].price, Decimal("2538"))
        self.assertTrue(any("已排除 1 条" in message for message in result.warnings))

    def test_mismatched_flight_day_excluded(self):
        data = sample()
        data["data"][3][0][0][0][4] = [2026, 9, 30]
        self.assertEqual(self.parse(html(data)).quotes[0].price, Decimal("2538"))

    def test_wrong_segment_date_and_broken_connection_excluded(self):
        for index, value in [(20, [2026, 9, 28]), (3, "BKK")]:
            data = sample()
            data["data"][3][0][0][0][2][1][index] = value
            self.assertEqual(self.parse(html(data)).quotes[0].price, Decimal("2538"))

    def test_all_priced_rows_invalid_is_an_error(self):
        data = sample()
        for row in data["data"][3][0][:2]:
            row[0][4] = [2026, 9, 30]
        with self.assertRaisesRegex(ProviderError, "所有带价航班"):
            self.parse(html(data))

    def test_non_numeric_negative_zero_and_nonfinite_prices_rejected(self):
        for price in [True, "2102", -1, 0, float("nan"), float("inf")]:
            data = sample()
            data["data"][3][0][0][1][0][1] = price
            with self.subTest(price=price), self.assertRaises(ProviderError):
                self.parse(html(data))

    def test_no_price_rows_are_empty_not_fake_zero(self):
        data = sample()
        data["data"][3][0] = data["data"][3][0][-1:]
        self.assertEqual(self.parse(html(data)).quotes, [])

    def test_empty_response_not_confused_with_malformed_structure(self):
        data = sample()
        data["data"][3][0] = None
        self.assertEqual(self.parse(html(data)).quotes, [])
        data["data"][3] = None
        with self.assertRaises(ProviderError):
            self.parse(html(data))

    def test_missing_airport_provenance_rejected(self):
        data = sample()
        data["data"][17] = None
        with self.assertRaisesRegex(ProviderError, "机场所属城市"):
            self.parse(html(data))

    def test_duplicate_script_and_invalid_json_rejected(self):
        for page in [html() + '<script class="ds:0">data:[]</script>', html().replace('data:', 'data:broken(', 1)]:
            with self.assertRaises(ProviderError):
                self.parse(page)

    def test_access_challenges_rejected_even_with_embedded_stale_prices(self):
        for challenge in ["Our systems have detected unusual traffic", "Before you continue to Google", "Verify you are human"]:
            with self.subTest(challenge=challenge), self.assertRaisesRegex(ProviderError, "验证或同意"):
                self.parse(html() + '<p>' + challenge + '</p>')

    def test_server_error_payload_is_not_no_flights(self):
        with self.assertRaisesRegex(ProviderError, "完整查询数据"):
            self.parse(html().replace('sideChannel:{}', 'errorHasStatus:true', 1))


class GoogleRequestTests(unittest.TestCase):
    def test_request_uses_city_names_date_one_adult_cny_without_tokens(self):
        provider = GoogleFlightsProvider(request_delay=0)
        with patch.object(provider, "_request", return_value=html()) as request:
            result = provider.search(ROUTE, TODAY)
        params = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual(set(params), {"q", "hl", "curr"})
        self.assertEqual(params["curr"], ["CNY"])
        self.assertIn("上海", params["q"][0])
        self.assertIn("东京", params["q"][0])
        self.assertIn("2026-09-29", params["q"][0])
        self.assertEqual(result.quotes[0].price, Decimal("2102"))

    def test_unsupported_filters_do_not_request(self):
        for changes in [dict(currency="USD"), dict(stay_nights=2), dict(nonstop=True), dict(travel_class=2), dict(origin="ZZZ")]:
            provider = GoogleFlightsProvider()
            with self.subTest(changes=changes), patch.object(provider, "_request") as request, self.assertRaises(ProviderUnsupported):
                provider.search(replace(ROUTE, **changes), TODAY)
            request.assert_not_called()

    def test_cancellation_prevents_network(self):
        provider = GoogleFlightsProvider(cancelled=lambda: True)
        with patch("urllib.request.urlopen") as request, self.assertRaisesRegex(ProviderError, "已停止"):
            provider.search(ROUTE, TODAY)
        request.assert_not_called()

    def test_no_future_dates_make_no_requests(self):
        provider = GoogleFlightsProvider()
        with patch.object(provider, "_request") as request:
            self.assertEqual(provider.search(ROUTE, DAY + timedelta(days=1)).quotes, [])
        request.assert_not_called()

    def test_limit_stops_before_network(self):
        provider = GoogleFlightsProvider(max_requests=0)
        with patch("urllib.request.urlopen") as request, self.assertRaisesRegex(ProviderError, "请求上限"):
            provider.search(ROUTE, TODAY)
        request.assert_not_called()

    def test_redirect_to_consent_is_not_followed_by_more_date_queries(self):
        response = Mock()
        response.url = "https://consent.google.com/m"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        provider = GoogleFlightsProvider(request_delay=0)
        with patch("urllib.request.urlopen", return_value=response) as request, self.assertRaisesRegex(ProviderError, "登录、同意或验证"):
            provider.search(replace(ROUTE, dates=(DAY, DAY + timedelta(days=1))), TODAY)
        self.assertEqual(request.call_count, 1)
        response.read.assert_not_called()

    def test_http_protection_stops_whole_date_loop(self):
        for status in [401, 403, 429]:
            provider = GoogleFlightsProvider(request_delay=0)
            error = HTTPError(ENDPOINT, status, "blocked", {}, io.BytesIO())
            with self.subTest(status=status), patch("urllib.request.urlopen", side_effect=error) as request, self.assertRaisesRegex(ProviderError, "本轮停止"):
                provider.search(replace(ROUTE, dates=(DAY, DAY + timedelta(days=1))), TODAY)
            self.assertEqual(request.call_count, 1)

    def test_failed_day_preserves_valid_other_day(self):
        provider = GoogleFlightsProvider(request_delay=0)
        with patch.object(provider, "_request", side_effect=[html(), ProviderError("网络失败")]) as request:
            result = provider.search(replace(ROUTE, dates=(DAY, DAY + timedelta(days=1))), TODAY)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(result.quotes), 1)
        self.assertTrue(any("2026-09-30：网络失败" in message for message in result.warnings))

    def test_all_failed_days_report_error(self):
        provider = GoogleFlightsProvider()
        with patch.object(provider, "_request", side_effect=ProviderError("网络失败")), self.assertRaisesRegex(ProviderError, "网络失败"):
            provider.search(ROUTE, TODAY)

    def test_remaining_dates_overlap_after_successful_first_day_probe(self):
        days = tuple(DAY + timedelta(days=index) for index in range(5))
        route = replace(ROUTE, dates=days)
        provider = GoogleFlightsProvider(request_delay=0)
        barrier = threading.Barrier(4, timeout=2)
        lock = threading.Lock()
        calls = 0

        def request(_url):
            nonlocal calls
            with lock:
                calls += 1
                current = calls
            if current > 1:
                barrier.wait()
            return "page"

        with patch.object(provider, "_request", side_effect=request), \
                patch.object(provider, "_parse_page", return_value=SearchResult([], [])):
            result = provider.search(route, TODAY)
        self.assertEqual(calls, 5)
        self.assertEqual(result.quotes, [])

    def test_parallel_completion_keeps_date_order_in_messages(self):
        days = tuple(DAY + timedelta(days=index) for index in range(4))
        route = replace(ROUTE, dates=days)
        provider = GoogleFlightsProvider(request_delay=0)

        def parse(_page, _route, departure, _url):
            time.sleep((days[-1] - departure).days * 0.01)
            return SearchResult([], [f"完成 {departure.isoformat()}"])

        with patch.object(provider, "_request", return_value="page"), \
                patch.object(provider, "_parse_page", side_effect=parse):
            result = provider.search(route, TODAY)
        self.assertEqual(result.warnings[:4], [f"完成 {day.isoformat()}" for day in days])

    def test_download_time_does_not_hold_request_spacing_lock(self):
        provider = GoogleFlightsProvider(request_delay=0)
        barrier = threading.Barrier(2, timeout=2)

        class Response:
            url = ENDPOINT

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                barrier.wait()
                return b"ok"

        with patch("urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: Response()):
            with ThreadPoolExecutor(max_workers=2) as pool:
                pages = list(pool.map(provider._request, [ENDPOINT, ENDPOINT]))
        self.assertEqual(pages, ["ok", "ok"])
        self.assertEqual(provider.requests_used, 2)

    def test_parallel_request_starts_keep_configured_spacing(self):
        provider = GoogleFlightsProvider(request_delay=0.04)
        starts = []
        lock = threading.Lock()

        class Response:
            url = ENDPOINT

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b"ok"

        def open_page(*_args, **_kwargs):
            with lock:
                starts.append(time.monotonic())
            return Response()

        with patch("urllib.request.urlopen", side_effect=open_page):
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(provider._request, [ENDPOINT] * 3))
        starts.sort()
        self.assertTrue(all(right - left >= 0.03 for left, right in zip(starts, starts[1:])))

    def test_later_access_block_cancels_dates_waiting_for_request_slot(self):
        days = tuple(DAY + timedelta(days=index) for index in range(8))
        route = replace(ROUTE, dates=days)
        provider = GoogleFlightsProvider(request_delay=0.04)
        calls = 0
        lock = threading.Lock()

        class Response:
            url = ENDPOINT

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b"page"

        def open_page(*_args, **_kwargs):
            nonlocal calls
            with lock:
                calls += 1
                current = calls
            if current == 2:
                raise HTTPError(ENDPOINT, 429, "blocked", {}, io.BytesIO())
            return Response()

        with patch("urllib.request.urlopen", side_effect=open_page), \
                patch.object(provider, "_parse_page", return_value=SearchResult([], [])), \
                self.assertRaisesRegex(ProviderError, "HTTP 429"):
            provider.search(route, TODAY)
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
