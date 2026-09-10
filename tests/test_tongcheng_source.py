"""Synthetic minimized regressions based on the verified 2026-09-08 public page."""

import http.client
import json
import threading
import unittest
import urllib.error
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from flightwatch.models import ProviderError, ProviderUnsupported, Route
from flightwatch.tongcheng_source import TongchengProvider, parse_nuxt_state


DAY = date(2026, 9, 22)
ROUTE = Route("bjs-sha", "北京到上海", "BJS", "SHA", "multi", dates=(DAY,))


def flight(**changes):
    row = dict(departureCityCode="BJS", arrivalCityCode="SHA",
               flyOffTime="2026-09-22 07:00", flightNo="MU5138",
               airCompanyName="东方航空", lcp=450, pt=50, ot=70,
               hasmt=False, stopNum=0, g5flag=0, g5pt=0)
    row.update(changes)
    return row


def state(rows=None, **changes):
    book = dict(Departure="BJS", Arrival="SHA", DepartureDate=DAY.isoformat(),
                hasReturn=False, flightLists=[flight()] if rows is None else rows,
                dataflag="some")
    book.update(changes)
    return dict(serverRendered=True, state=dict(book1=book))


def page(data):
    return '<html><script>window.__NUXT__=(function(){return ' + json.dumps(data) + '}());</script></html>'


class NuxtParserTests(unittest.TestCase):
    def test_minimized_actual_serializer_with_shared_arguments(self):
        html = '''<script>window.__NUXT__=(function(a,b,c,d){return {
          serverRendered:b,state:{book1:{Departure:"BJS",Arrival:"SHA",
          DepartureDate:"2026-09-22",hasReturn:c,flightLists:[{lcp:a,pt:50,ot:70}],
          unused:d}},data:["\\u002F"]}}(450,true,false,void 0));</script>'''
        parsed = parse_nuxt_state(html)
        self.assertEqual(parsed["state"]["book1"]["flightLists"][0]["lcp"], Decimal("450"))
        self.assertIsNone(parsed["state"]["book1"]["unused"])
        self.assertEqual(parsed["data"], ["/"])

    def test_end_to_end_literal_serialized_page(self):
        result = TongchengProvider()._parse(parse_nuxt_state(page(state())), ROUTE, DAY)
        self.assertEqual(result.quotes[0].price, Decimal("570"))

    def test_arbitrary_expressions_are_rejected(self):
        payloads = [
            '(function(){return {a:fetch("https://example.invalid")}}())',
            '(function(){return {a:globalThis.process}}())',
            '(function(){return {a:1+2}}())',
            '(function(){return {};alert(1)}())',
            '(function(a){return {a:a}}(console.log("x")))',
            '(function(a){return {a:a}}(1));alert(1)',
            '(function(){return {a:undeclared}}())',
            '(function(){return {a:NaN}}())',
            '(function(){return {a:Infinity}}())',
            '(function(){return {a:1,a:2}}())',
            '(function(a,a){return {a:a}}(1,2))',
            '(function(a){return {a:a}}())',
        ]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ProviderError):
                parse_nuxt_state('<script>window.__NUXT__=' + payload + ';</script>')

    def test_validation_page_or_duplicate_state_rejected(self):
        for html in ("请完成验证", page(state()) + page(state())):
            with self.subTest(html=html[:30]), self.assertRaises(ProviderError):
                parse_nuxt_state(html)

    def test_excessive_nested_data_rejected(self):
        html = '<script>window.__NUXT__=(function(){return {a:' + '[' * 70 + '0' + ']' * 70 + '}}());</script>'
        with self.assertRaises(ProviderError):
            parse_nuxt_state(html)


class TongchengPriceTests(unittest.TestCase):
    def setUp(self):
        self.provider = TongchengProvider(request_delay=0)

    def test_per_flight_taxes_and_source_are_explicit(self):
        quote = self.provider._parse(state(), ROUTE, DAY).quotes[0]
        self.assertEqual(quote.price, Decimal("570"))
        self.assertEqual(quote.provider, "tongcheng")
        self.assertEqual(quote.price_basis, "total")
        self.assertTrue(quote.comparable)
        self.assertIn("机建 50 + 燃油 70", quote.price_note)
        self.assertIn("本次返回的 1 个航班范围", quote.price_note)
        self.assertIn("BJS-SHA?date=2026-09-22", quote.url)

    def test_minimum_compares_totals_not_base_fares(self):
        rows = [flight(lcp=400, pt=50, ot=70), flight(lcp=430, pt=0, ot=0, flightNo="TEST2")]
        quote = self.provider._parse(state(rows), ROUTE, DAY).quotes[0]
        self.assertEqual(quote.price, Decimal("430"))
        self.assertEqual(quote.flight_number, "TEST2")

    def test_special_airport_fee_matches_official_mapping(self):
        for special, expected in ((20, "540"), (0, "570")):
            with self.subTest(special=special):
                quote = self.provider._parse(state([flight(g5flag=1, g5pt=special)]), ROUTE, DAY).quotes[0]
                self.assertEqual(quote.price, Decimal(expected))
                self.assertIsNone(quote.stops)

    def test_missing_taxes_are_never_filled_with_estimates(self):
        for field in ("pt", "ot", "lcp", "g5flag"):
            row = flight()
            del row[field]
            with self.subTest(field=field), self.assertRaises(ProviderError):
                self.provider._parse(state([row]), ROUTE, DAY)

    def test_invalid_amounts_fail_instead_of_becoming_cheap_fares(self):
        for field in ("lcp", "pt", "ot"):
            for value in (True, None, "NaN", "Infinity", -1, "abc", "1000001"):
                with self.subTest(field=field, value=value), self.assertRaises(ProviderError):
                    self.provider._parse(state([flight(**{field: value})]), ROUTE, DAY)

    def test_page_route_date_and_oneway_must_match(self):
        for changes in (dict(Departure="CAN"), dict(Arrival="CAN"),
                        dict(DepartureDate="2026-09-23"), dict(hasReturn=True)):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                self.provider._parse(state(**changes), ROUTE, DAY)

    def test_flight_route_and_date_must_match(self):
        for changes in (dict(departureCityCode="CAN"), dict(arrivalCityCode="CAN"),
                        dict(flyOffTime="2026-09-23 07:00"), dict(flyOffTime=None)):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                self.provider._parse(state([flight(**changes)]), ROUTE, DAY)

    def test_undocumented_marker_skipped_and_reported(self):
        result = self.provider._parse(state([flight(hasmt=True), flight()]), ROUTE, DAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertTrue(any("未支持标记" in text for text in result.warnings))

    def test_returned_stopover_is_preserved(self):
        quote = self.provider._parse(state([flight(stopNum=1)]), ROUTE, DAY).quotes[0]
        self.assertEqual(quote.stops, 1)

    def test_missing_or_fractional_stops_rejected(self):
        for value in (None, True, "0.5", "-1"):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                self.provider._parse(state([flight(stopNum=value)]), ROUTE, DAY)

    def test_zero_fare_and_empty_list_are_not_real_quotes(self):
        for rows in ([], [flight(lcp=0)]):
            result = self.provider._parse(state(rows), ROUTE, DAY)
            self.assertFalse(result.quotes)
            self.assertTrue(result.warnings)

    def test_invalid_state_rejected(self):
        for data in ({}, dict(serverRendered=False, state={}), state(flightLists=None)):
            with self.subTest(data=data), self.assertRaises(ProviderError):
                self.provider._parse(data, ROUTE, DAY)


class TongchengTransportTests(unittest.TestCase):
    def test_reused_provider_uses_current_cancellation_callback(self):
        provider = TongchengProvider(request_delay=0, cancelled=lambda: True)
        with self.assertRaises(ProviderError):
            provider.search(ROUTE, DAY)
        provider.cancelled = lambda: False
        with patch.object(provider, "_request", return_value=page(state())):
            result = provider.search(ROUTE, DAY)
        self.assertEqual(result.quotes[0].price, Decimal("570"))

    def test_unsupported_filters_make_no_network_request(self):
        provider = TongchengProvider()
        for changes in (dict(market="international"), dict(currency="USD"),
                        dict(stay_nights=7), dict(travel_class=2), dict(nonstop=True)):
            with self.subTest(changes=changes), patch.object(provider, "_request") as request:
                with self.assertRaises(ProviderUnsupported):
                    provider.search(replace(ROUTE, **changes), DAY)
                request.assert_not_called()

    def test_query_uses_one_get_per_day_and_exposes_subset_warning(self):
        provider = TongchengProvider(request_delay=0)
        with patch.object(provider, "_request", return_value=page(state())) as request:
            result = provider.search(ROUTE, DAY)
        request.assert_called_once_with("https://www.ly.com/flights/itinerary/oneway/BJS-SHA?date=2026-09-22")
        self.assertEqual(result.quotes[0].price, Decimal("570"))
        self.assertTrue(any("初始" in warning for warning in result.warnings))

    def test_partial_date_failure_preserves_other_quotes(self):
        provider = TongchengProvider(request_delay=0)
        route = replace(ROUTE, dates=(DAY, date(2026, 9, 23)))
        with patch.object(provider, "_request", side_effect=[page(state()), ProviderError("网络失败")]):
            result = provider.search(route, DAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertTrue(any("2026-09-23" in warning and "网络失败" in warning for warning in result.warnings))

    def test_all_dates_failed_is_provider_failure(self):
        provider = TongchengProvider(request_delay=0)
        with patch.object(provider, "_request", side_effect=ProviderError("验证页面")):
            with self.assertRaises(ProviderError):
                provider.search(ROUTE, DAY)

    def test_stop_does_not_query_remaining_dates(self):
        stopped = threading.Event()
        provider = TongchengProvider(request_delay=0, cancelled=stopped.is_set)
        route = replace(ROUTE, dates=(DAY, date(2026, 9, 23), date(2026, 9, 24)))

        def first_page(url):
            stopped.set()
            return page(state())

        with patch.object(provider, "_request", side_effect=first_page) as request:
            result = provider.search(route, DAY)
        request.assert_called_once()
        self.assertEqual(len(result.quotes), 1)
        self.assertTrue(any("已停止" in warning for warning in result.warnings))

    def test_stop_during_throttle_prevents_http_request(self):
        stopped = threading.Event()
        provider = TongchengProvider(request_delay=1, cancelled=stopped.is_set)
        provider._last_request = 0
        with patch("flightwatch.tongcheng_source.time.monotonic", return_value=0), \
                patch("flightwatch.tongcheng_source.time.sleep", side_effect=lambda seconds: stopped.set()), \
                patch("flightwatch.tongcheng_source.urllib.request.urlopen") as open_url:
            with self.assertRaisesRegex(ProviderError, "已停止"):
                provider._request(provider._url(ROUTE, DAY))
        open_url.assert_not_called()
        self.assertEqual(provider.requests_used, 0)

    def test_request_budget_is_shared_across_routes(self):
        provider = TongchengProvider(request_delay=0, max_requests=1)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = page(state()).encode()
        with patch("flightwatch.tongcheng_source.urllib.request.urlopen", return_value=response) as open_url:
            self.assertEqual(len(provider.search(ROUTE, DAY).quotes), 1)
            with self.assertRaisesRegex(ProviderError, "请求上限"):
                provider.search(ROUTE, DAY)
        self.assertEqual(open_url.call_count, 1)

    def test_truncated_http_response_becomes_provider_error(self):
        response = MagicMock()
        response.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b"<html>", 100)
        with patch("flightwatch.tongcheng_source.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(ProviderError, "IncompleteRead"):
                TongchengProvider().search(ROUTE, DAY)

    def test_http_error_is_not_parsed_as_fare(self):
        error = urllib.error.HTTPError("https://www.ly.com/", 403, "Forbidden", {}, None)
        with patch("flightwatch.tongcheng_source.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(ProviderError, "HTTP 403"):
                TongchengProvider().search(ROUTE, DAY)

    def test_invalid_city_code_does_not_become_url_path(self):
        provider = TongchengProvider()
        with patch.object(provider, "_request") as request, self.assertRaises(ProviderError):
            provider.search(replace(ROUTE, origin="../"), DAY)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
