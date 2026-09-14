"""Minimized real-shape Fliggy response tests; no live network in the suite."""

import gzip
import http.client
import json
import threading
import unittest
import urllib.error
import urllib.parse
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from flightwatch.fliggy_source import FliggyProvider, parse_jsonp
from flightwatch.models import ProviderError, ProviderUnsupported, Route


DAY = date(2026, 9, 23)
ROUTE = Route("test", "北京上海", "BJS", "SHA", "multi", dates=(DAY,))
INTL_DAY = date(2026, 10, 1)
INTL_ROUTE = Route(
    "international", "上海东京", "SHA", "TYO", "multi",
    dates=(INTL_DAY,), market="international",
)


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


def calendar_row(day=INTL_DAY, **changes):
    row = {
        "arrCityCode": "TYO", "depCityCode": "SHA", "discount": 0.0,
        "leaveDate": day.isoformat(), "price": 642, "tax": 1244,
        "url": ("//sijipiao.fliggy.com/ie/flight_search_result.htm?"
                f"depCityName=上海&depCityCode=SHA&arrCityName=东京&arrCityCode=TYO&"
                f"tripType=0&depDate={day.isoformat()}&arrDate=null&searchBy=1281"),
    }
    row.update(changes)
    return row


def calendar_response(rows=None):
    return {"success": True, "result": [calendar_row()] if rows is None else rows}


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


class FliggyInternationalParserTests(unittest.TestCase):
    def setUp(self):
        self.provider = FliggyProvider(request_delay=0)

    def parse(self, data=None, wanted=None, first_day=INTL_DAY):
        return self.provider._parse_month_calendar(
            calendar_response() if data is None else data,
            INTL_ROUTE,
            {INTL_DAY} if wanted is None else wanted,
            first_day,
        )

    def test_official_calendar_shape_adds_returned_fare_and_tax(self):
        quote = self.parse().quotes[0]
        self.assertEqual(quote.price, Decimal("1886"))
        self.assertEqual(quote.currency, "CNY")
        self.assertEqual(quote.price_basis, "total")
        self.assertEqual(quote.provider, "fliggy")
        self.assertEqual(quote.source, "飞猪国际最低价日历")
        self.assertEqual(quote.departure_date, INTL_DAY)
        self.assertFalse(quote.flight_number)
        self.assertIn("票价 642 + 税费 1244", quote.price_note)
        self.assertIn("sijipiao.fliggy.com/ie/flight_search_result.htm", quote.url)
        self.assertIn("depDate=2026-10-01", quote.url)

    def test_only_requested_dates_are_returned_from_month(self):
        rows = [calendar_row(INTL_DAY + timedelta(days=offset), price=600 + offset)
                for offset in range(31)]
        wanted = {INTL_DAY, INTL_DAY + timedelta(days=15), INTL_DAY + timedelta(days=30)}
        result = self.parse(calendar_response(rows), wanted)
        self.assertEqual([quote.departure_date for quote in result.quotes], sorted(wanted))

    def test_route_date_and_booking_url_must_all_match(self):
        cases = [
            calendar_row(depCityCode="BJS"),
            calendar_row(arrCityCode="OSA"),
            calendar_row(date(2026, 11, 1)),
            calendar_row(url="https://evil.example/ie/flight_search_result.htm?depCityCode=SHA&arrCityCode=TYO&depDate=2026-10-01"),
            calendar_row(url="//sijipiao.fliggy.com/ie/flight_search_result.htm?depCityCode=BJS&arrCityCode=TYO&depDate=2026-10-01"),
            calendar_row(url="//sijipiao.fliggy.com/ie/flight_search_result.htm?depCityCode=SHA&arrCityCode=TYO&depDate=2026-10-02"),
            calendar_row(url="//sijipiao.fliggy.com/ie/flight_search_result.htm?depCityCode=SHA&arrCityCode=TYO&depDate=2026-10-01"),
            calendar_row(url="//sijipiao.fliggy.com/ie/flight_search_result.htm?depCityCode=SHA&arrCityCode=TYO&depDate=2026-10-01&tripType=1"),
            calendar_row(url="//sijipiao.fliggy.com/ie/flight_search_result.htm?depCityCode=SHA&arrCityCode=TYO&depDate=2026-10-01&tripType=0&tripType=0"),
        ]
        for row in cases:
            with self.subTest(row=row), self.assertRaises(ProviderError):
                self.parse(calendar_response([row]))

    def test_duplicate_dates_are_rejected_instead_of_picking_an_ambiguous_price(self):
        with self.assertRaisesRegex(ProviderError, "重复日期"):
            self.parse(calendar_response([calendar_row(price=642), calendar_row(price=300)]))

    def test_invalid_price_or_tax_is_rejected(self):
        for field in ("price", "tax"):
            for value in (None, True, "NaN", "Infinity", -1, "abc", 1_000_001):
                with self.subTest(field=field, value=value), self.assertRaises(ProviderError):
                    self.parse(calendar_response([calendar_row(**{field: value})]))

    def test_zero_fare_and_missing_day_are_warnings_not_zero_quotes(self):
        result = self.parse(calendar_response([calendar_row(price=0, tax=1244)]))
        self.assertFalse(result.quotes)
        self.assertTrue(any("暂无正数报价" in warning for warning in result.warnings))

    def test_failed_or_changed_envelope_is_rejected(self):
        for data in ({}, {"success": False, "result": []},
                     {"success": True, "failure": True, "result": []},
                     {"success": True, "result": {}},
                     {"success": True, "result": ["bad"]}):
            with self.subTest(data=data), self.assertRaises(ProviderError):
                self.parse(data)

    def test_month_parser_requires_first_day_of_month(self):
        with self.assertRaisesRegex(ProviderError, "自然月第一天"):
            self.parse(first_day=date(2026, 10, 2))

    def test_week_parser_requires_every_row_inside_exact_seven_day_window(self):
        wanted = {INTL_DAY, INTL_DAY + timedelta(days=6)}
        rows = [calendar_row(INTL_DAY), calendar_row(INTL_DAY + timedelta(days=6))]
        result = self.provider._parse_week_calendar(
            calendar_response(rows), INTL_ROUTE, wanted, INTL_DAY
        )
        self.assertEqual([quote.departure_date for quote in result.quotes], sorted(wanted))
        with self.assertRaisesRegex(ProviderError, "七日窗口"):
            self.provider._parse_week_calendar(
                calendar_response([calendar_row(INTL_DAY + timedelta(days=7))]),
                INTL_ROUTE, wanted, INTL_DAY,
            )

    def test_week_parser_keeps_route_trip_type_and_url_validation(self):
        invalid = [
            calendar_row(depCityCode="BJS"),
            calendar_row(url="//sijipiao.fliggy.com/ie/flight_search_result.htm?depCityCode=SHA&arrCityCode=TYO&depDate=2026-10-01&tripType=1"),
            calendar_row(url="https://evil.example/ie/flight_search_result.htm?depCityCode=SHA&arrCityCode=TYO&depDate=2026-10-01&tripType=0"),
        ]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ProviderError):
                self.provider._parse_week_calendar(
                    calendar_response([row]), INTL_ROUTE, {INTL_DAY}, INTL_DAY
                )


class FliggyTransportTests(unittest.TestCase):
    def test_unsupported_filters_do_not_access_network(self):
        provider = FliggyProvider()
        for changes in (dict(market="unknown"), dict(stay_nights=7), dict(currency="USD"),
                        dict(travel_class=2), dict(nonstop=True)):
            with self.subTest(changes=changes), \
                    patch.object(provider, "_request") as request, \
                    patch.object(provider, "_request_month_calendar") as calendar, \
                    patch.object(provider, "_request_week_calendar") as week_calendar:
                with self.assertRaises(ProviderUnsupported):
                    provider.search(replace(ROUTE, **changes), DAY)
                request.assert_not_called()
                calendar.assert_not_called()
                week_calendar.assert_not_called()

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

    def test_international_calendar_transport_uses_no_credentials(self):
        web_response = MagicMock()
        web_response.__enter__.return_value.read.return_value = encode(calendar_response())
        with patch("flightwatch.fliggy_source.urllib.request.urlopen", return_value=web_response) as open_url:
            result = FliggyProvider(request_delay=0).search(INTL_ROUTE, INTL_DAY)
        self.assertEqual(result.quotes[0].price, Decimal("1886"))
        request = open_url.call_args.args[0]
        self.assertIsNone(request.get_header("Cookie"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(request.get_header("Referer"), FliggyProvider._booking_url(INTL_ROUTE, INTL_DAY))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query,
                                     keep_blank_values=True)
        self.assertEqual(query["depCityCode"], ["SHA"])
        self.assertEqual(query["arrCityCode"], ["TYO"])
        self.assertEqual(query["leaveDate"], ["2026-10-01"])
        self.assertEqual(query["calendarType"], ["1"])
        self.assertEqual(query["callback"], ["flightwatch"])
        self.assertNotIn("token", {key.lower() for key in query})

    def test_international_dates_are_batched_by_natural_month(self):
        dates = tuple(INTL_DAY + timedelta(days=offset) for offset in range(40))
        route = replace(INTL_ROUTE, dates=dates)
        first = calendar_response([calendar_row(day) for day in dates[:31]])
        second = calendar_response([calendar_row(
            day,
            url=("//sijipiao.fliggy.com/ie/flight_search_result.htm?"
                 f"depCityCode=SHA&arrCityCode=TYO&depDate={day.isoformat()}&tripType=0"),
        ) for day in dates[31:]])
        provider = FliggyProvider(request_delay=0)
        with patch.object(provider, "_request_month_calendar", side_effect=[first, second]) as request:
            result = provider.search(route, INTL_DAY)
        self.assertEqual(len(result.quotes), 40)
        self.assertEqual([call.args[1] for call in request.call_args_list],
                         [INTL_DAY, date(2026, 11, 1)])

    def test_explicit_month_service_refusal_falls_back_to_seven_day_windows(self):
        dates = tuple(INTL_DAY + timedelta(days=offset) for offset in range(10))
        route = replace(INTL_ROUTE, dates=dates)
        weeks = [
            calendar_response([calendar_row(day) for day in dates[:7]]),
            calendar_response([calendar_row(day) for day in dates[7:]]),
        ]
        for refusal in ({"success": False}, {"failure": True}):
            provider = FliggyProvider(request_delay=0)
            with self.subTest(refusal=refusal), \
                    patch.object(provider, "_request_month_calendar", return_value=refusal) as month, \
                    patch.object(provider, "_request_week_calendar", side_effect=weeks) as week:
                result = provider.search(route, INTL_DAY)
            month.assert_called_once_with(route, INTL_DAY)
            self.assertEqual([call.args[1] for call in week.call_args_list],
                             [INTL_DAY, INTL_DAY + timedelta(days=7)])
            self.assertEqual(len(result.quotes), 10)
            self.assertTrue(any("自动改用七日" in warning for warning in result.warnings))

    def test_live_transport_path_counts_month_and_week_requests_and_calendar_types(self):
        dates = tuple(INTL_DAY + timedelta(days=offset) for offset in range(10))
        route = replace(INTL_ROUTE, dates=dates)
        payloads = {
            ("1", "2026-10-01"): {"success": False},
            ("0", "2026-10-01"): calendar_response(
                [calendar_row(day) for day in dates[:7]]
            ),
            ("0", "2026-10-08"): calendar_response(
                [calendar_row(day) for day in dates[7:]]
            ),
        }
        requests = []

        def open_url(request, **_):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query,
                                         keep_blank_values=True)
            key = (query["calendarType"][0], query["leaveDate"][0])
            requests.append(request)
            web_response = MagicMock()
            web_response.__enter__.return_value.read.return_value = encode(payloads[key])
            return web_response

        provider = FliggyProvider(request_delay=0)
        with patch("flightwatch.fliggy_source.urllib.request.urlopen", side_effect=open_url):
            result = provider.search(route, INTL_DAY)
        self.assertEqual(len(result.quotes), 10)
        self.assertEqual(provider.requests_used, 3)
        self.assertEqual([
            urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["calendarType"][0]
            for request in requests
        ], ["1", "0", "0"])

    def test_week_fallback_partial_failure_preserves_successful_window(self):
        dates = tuple(INTL_DAY + timedelta(days=offset) for offset in range(10))
        route = replace(INTL_ROUTE, dates=dates)
        provider = FliggyProvider(request_delay=0)
        with patch.object(provider, "_request_month_calendar", return_value={"success": False}), \
                patch.object(provider, "_request_week_calendar", side_effect=[
                    calendar_response([calendar_row(day) for day in dates[:7]]),
                    ProviderError("周历网络失败"),
                ]):
            result = provider.search(route, INTL_DAY)
        self.assertEqual([quote.departure_date for quote in result.quotes], list(dates[:7]))
        self.assertTrue(any("周历网络失败" in warning for warning in result.warnings))

    def test_network_schema_route_url_and_amount_errors_never_trigger_fallback(self):
        bad_results = [
            ProviderError("网络失败"),
            {},
            calendar_response([calendar_row(depCityCode="BJS")]),
            calendar_response([calendar_row(price="invalid")]),
            calendar_response([calendar_row(url="https://evil.example/")]),
        ]
        for bad_result in bad_results:
            provider = FliggyProvider(request_delay=0)
            kwargs = ({"side_effect": bad_result} if isinstance(bad_result, Exception)
                      else {"return_value": bad_result})
            with self.subTest(bad_result=bad_result), \
                    patch.object(provider, "_request_month_calendar", **kwargs), \
                    patch.object(provider, "_request_week_calendar") as week, \
                    self.assertRaises(ProviderError):
                provider.search(INTL_ROUTE, INTL_DAY)
            week.assert_not_called()

    def test_fallback_respects_request_budget_after_month_request(self):
        provider = FliggyProvider(request_delay=0, max_requests=1)
        with patch.object(provider, "_open_jsonp", return_value={"success": False}), \
                patch.object(provider, "_request_week_calendar") as week, \
                self.assertRaisesRegex(ProviderError, "请求上限"):
            provider.search(INTL_ROUTE, INTL_DAY)
        self.assertEqual(provider.requests_used, 1)
        week.assert_not_called()

    def test_fallback_budget_stops_before_second_week_and_keeps_first(self):
        dates = tuple(INTL_DAY + timedelta(days=offset) for offset in range(10))
        route = replace(INTL_ROUTE, dates=dates)
        provider = FliggyProvider(request_delay=0, max_requests=2)
        with patch.object(provider, "_open_jsonp", side_effect=[
            {"success": False},
            calendar_response([calendar_row(day) for day in dates[:7]]),
        ]) as load:
            result = provider.search(route, INTL_DAY)
        self.assertEqual(provider.requests_used, 2)
        self.assertEqual(load.call_count, 2)
        self.assertEqual([quote.departure_date for quote in result.quotes], list(dates[:7]))
        self.assertTrue(any("请求上限" in warning for warning in result.warnings))

    def test_fallback_respects_cancellation_after_month_refusal(self):
        stopped = threading.Event()
        provider = FliggyProvider(request_delay=0, cancelled=stopped.is_set)

        def refuse(*_):
            stopped.set()
            return {"success": False}

        with patch.object(provider, "_request_month_calendar", side_effect=refuse), \
                patch.object(provider, "_request_week_calendar") as week, \
                self.assertRaisesRegex(ProviderError, "已停止"):
            provider.search(INTL_ROUTE, INTL_DAY)
        week.assert_not_called()

    def test_fallback_cancellation_stops_before_second_week_and_keeps_first(self):
        dates = tuple(INTL_DAY + timedelta(days=offset) for offset in range(10))
        route = replace(INTL_ROUTE, dates=dates)
        stopped = threading.Event()
        provider = FliggyProvider(request_delay=0, cancelled=stopped.is_set)

        def first_week(*_):
            stopped.set()
            return calendar_response([calendar_row(day) for day in dates[:7]])

        with patch.object(provider, "_request_month_calendar", return_value={"success": False}), \
                patch.object(provider, "_request_week_calendar", side_effect=first_week) as week:
            result = provider.search(route, INTL_DAY)
        week.assert_called_once_with(route, INTL_DAY)
        self.assertEqual([quote.departure_date for quote in result.quotes], list(dates[:7]))
        self.assertTrue(any("已停止" in warning for warning in result.warnings))

    def test_week_fallback_transport_sets_calendar_type_without_credentials(self):
        web_response = MagicMock()
        web_response.__enter__.return_value.read.return_value = encode(calendar_response())
        provider = FliggyProvider(request_delay=0)
        with patch("flightwatch.fliggy_source.urllib.request.urlopen", return_value=web_response) as open_url:
            provider._request_week_calendar(INTL_ROUTE, INTL_DAY)
        request = open_url.call_args.args[0]
        self.assertIsNone(request.get_header("Cookie"))
        self.assertIsNone(request.get_header("Authorization"))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query,
                                     keep_blank_values=True)
        self.assertEqual(query["calendarType"], ["0"])
        self.assertEqual(query["tripType"], ["0"])
        self.assertEqual(query["leaveDate"], ["2026-10-01"])

    def test_international_partial_month_failure_preserves_other_days(self):
        dates = (INTL_DAY, date(2026, 11, 1))
        route = replace(INTL_ROUTE, dates=dates)
        provider = FliggyProvider(request_delay=0)
        with patch.object(provider, "_request_month_calendar",
                          side_effect=[calendar_response(), ProviderError("网络失败")]):
            result = provider.search(route, INTL_DAY)
        self.assertEqual([quote.departure_date for quote in result.quotes], [INTL_DAY])
        self.assertTrue(any("网络失败" in warning for warning in result.warnings))

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
