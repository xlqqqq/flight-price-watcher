"""Qunar response and identity contracts; tests do not make network requests."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from flightwatch.models import ProviderError, ProviderUnsupported, Route
from flightwatch.qunar_source import (
    ENDPOINT, SUGGEST_ENDPOINT, INTERNATIONAL_FLIGHTS_ENDPOINT,
    DOMESTIC_FLIGHTS_ENDPOINT, QunarCalendarProvider,
)


TODAY = date(2026, 9, 8)
DOMESTIC_DAY = date(2026, 10, 15)
INTERNATIONAL_DAY = date(2026, 9, 29)


def city(code, name, international=False, country="中国", **extra):
    row = {"code": code, "type": 1, "canSearch": True, "makeGray": False,
           "suggestSearch": {"searchParam": name, "isInter": international,
                             "displayName": name, "countryName": country}}
    row.update(extra)
    return row


def suggest(*cities):
    return {"ret": True, "code": 0, "data": {"code": 0, "suggestData": {"places": list(cities)}}}


def calendar(*rows, origin="上海", destination="北京", market=1):
    return {"bstatus": {"code": 0}, "data": {"scity": origin, "ecity": destination,
        "flightType": market, "gflights": list(rows), "bflights": []}}


def row(day=DOMESTIC_DAY, price="414", flight="MU5185", **extra):
    value = {"date": day.isoformat(), "price": price, "code": flight, "backDate": "", "disabled": False}
    value.update(extra)
    return value


class QunarParsingTests(unittest.TestCase):
    def setUp(self):
        self.provider = QunarCalendarProvider(request_delay=0)
        self.route = Route("sha-bjs", "上海北京", "SHA", "BJS", "qunar", dates=(DOMESTIC_DAY,))

    def parse(self, payload, wanted=None):
        return self.provider._parse(payload, self.route, wanted or {DOMESTIC_DAY}, "上海", "北京")

    def test_live_domestic_shape_is_base_fare_excluded_from_comparison(self):
        result = self.parse(calendar(row()))
        self.assertEqual(result.quotes[0].price, Decimal("414"))
        self.assertEqual(result.quotes[0].flight_number, "MU5185")
        self.assertEqual(result.quotes[0].price_basis, "base")
        self.assertFalse(result.quotes[0].comparable)
        self.assertIn("未含税", result.quotes[0].price_note)
        self.assertIn("阈值", result.warnings[0])

    def test_live_international_shape_is_tax_inclusive_reference(self):
        route = replace(self.route, destination="TYO", market="international", dates=(INTERNATIONAL_DAY,))
        payload = calendar(row(INTERNATIONAL_DAY, "1748", "BR721/BR196"), destination="东京", market=2)
        result = self.provider._parse(payload, route, {INTERNATIONAL_DAY}, "上海", "东京")
        quote = result.quotes[0]
        self.assertEqual(quote.price, Decimal("1748"))
        self.assertTrue(quote.comparable)
        self.assertEqual(quote.provider, "qunar")
        self.assertIn("含税参考总价", quote.price_note)
        self.assertIsNone(quote.stops)  # Calendar code does not establish stops.

    def test_selected_dates_only_and_missing_cache_is_explicit(self):
        next_day = date(2026, 10, 16)
        result = self.parse(calendar(row(), row(date(2026, 10, 14), "199"), row(next_day, "")),
                            {DOMESTIC_DAY, next_day})
        self.assertEqual([q.departure_date for q in result.quotes], [DOMESTIC_DAY])
        self.assertTrue(any("1 个" in warning for warning in result.warnings))
        self.assertTrue(any("不代表没有航班" in warning for warning in result.warnings))

    def test_wrong_route_market_or_return_date_is_rejected(self):
        for payload in (calendar(row(), destination="广州"), calendar(row(), market=2),
                        calendar(row(backDate="2026-10-20"))):
            with self.subTest(payload=payload), self.assertRaises(ProviderError):
                self.parse(payload)

    def test_empty_zero_and_disabled_quotes_are_not_invented(self):
        for sample in (row(price=""), row(price="0"), row(disabled=True)):
            with self.subTest(sample=sample):
                result = self.parse(calendar(sample))
                self.assertEqual(result.quotes, [])
                self.assertTrue(result.warnings)

    def test_invalid_prices_dates_and_unacknowledged_response_fail(self):
        for price in (None, True, "NaN", "Infinity", "-1", "¥414"):
            with self.subTest(price=price), self.assertRaises(ProviderError):
                self.parse(calendar(row(price=price)))
        for value in ("2026-2-01", "2026-10-32", None):
            sample = row(); sample["date"] = value
            with self.subTest(date=value), self.assertRaises(ProviderError):
                self.parse(calendar(sample))
        for value in ({}, {"bstatus": {"code": False}}, {"bstatus": {"code": 1}},
                      {"bstatus": {"code": 0}, "data": {"gflights": "bad"}}):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                self.parse(value)

    def test_duplicate_day_uses_lowest_same_basis_quote(self):
        result = self.parse(calendar(row(price="450"), row(price="414")))
        self.assertEqual(len(result.quotes), 1)
        self.assertEqual(result.quotes[0].price, Decimal("414"))


class QunarIdentityTests(unittest.TestCase):
    def setUp(self):
        self.provider = QunarCalendarProvider(request_delay=0)
        self.provider._request = Mock()

    def test_bjs_and_sia_aliases_use_verified_city_name_not_first_result(self):
        for code, qcode, name in (("BJS", "PEK", "北京"), ("SIA", "XIY", "西安")):
            with self.subTest(code=code):
                self.provider._request.return_value = suggest(city("BOJ", "布加斯", True, "保加利亚"), city(qcode, name))
                resolved = self.provider._resolve_city(code)
                self.assertEqual(resolved["name"], name)
                self.assertEqual(resolved["code"], qcode)

    def test_dynamic_prague_metadata_preserves_identity_via_code(self):
        self.provider._request.return_value = suggest(city("PRG", "布拉格", True, "捷克"))
        with patch("flightwatch.qunar_source.cached_city", return_value={
            "code": "PRG", "name": "布拉格(捷克)", "country": "捷克", "market": "international",
        }):
            self.assertEqual(self.provider._resolve_city("PRG")["name"], "布拉格")

    def test_nearby_country_and_airport_matches_are_not_cities(self):
        for sample in (city("BOJ", "布加斯", True, "保加利亚"), city("BJS", "北京首都", type=2),
                       city("BJS", "北京", type=7), city("BJS", "北京", canSearch=False)):
            self.provider._request.return_value = suggest(sample)
            with self.subTest(sample=sample), self.assertRaises(ProviderUnsupported):
                self.provider._resolve_city("BJS")

    def test_ambiguous_and_wrong_market_city_fail_closed(self):
        self.provider._request.return_value = suggest(city("BJS", "北京"), city("PEK", "北京"))
        with self.assertRaises(ProviderUnsupported):
            self.provider._resolve_city("BJS")
        self.provider._request.return_value = suggest(city("SHA", "上海", True))
        with self.assertRaises(ProviderUnsupported):
            self.provider._resolve_city("SHA")

    def test_successful_city_resolution_is_cached(self):
        self.provider._request.return_value = suggest(city("SHA", "上海"))
        self.provider._resolve_city("SHA")
        self.provider._resolve_city("SHA")
        self.provider._request.assert_called_once()

    def test_search_uses_names_market_and_strict_selected_dates(self):
        route = Route("test", "上海北京", "SHA", "BJS", "qunar", dates=(DOMESTIC_DAY,))
        self.provider._request.side_effect = [suggest(city("SHA", "上海")), suggest(city("PEK", "北京")), calendar(row())]
        result = self.provider.search(route, TODAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertEqual(self.provider._request.call_args.args,
                         (ENDPOINT, {"dep": "上海", "arr": "北京", "days": "", "priceType": 1}))

    def test_unsupported_filters_and_expired_dates_make_no_requests(self):
        route = Route("test", "上海北京", "SHA", "BJS", "qunar", dates=(DOMESTIC_DAY,))
        for modified in (replace(route, currency="USD"), replace(route, nonstop=True),
                         replace(route, stay_nights=3), replace(route, travel_class=2)):
            with self.subTest(route=modified), self.assertRaises(ProviderUnsupported):
                self.provider.search(modified, TODAY)
        self.assertEqual(self.provider.search(replace(route, dates=(date(2026, 1, 1),)), TODAY).quotes, [])
        self.provider._request.assert_not_called()


class QunarTransportTests(unittest.TestCase):
    def test_cancellation_before_request_or_after_delay_sends_nothing(self):
        for during_delay in (False, True):
            with self.subTest(during_delay=during_delay):
                event = threading.Event()
                provider = QunarCalendarProvider(request_delay=1, cancelled=event.is_set)
                if during_delay:
                    provider._last_request = 100
                else:
                    event.set()
                with patch("flightwatch.qunar_source.urllib.request.urlopen") as opener, \
                        patch("flightwatch.qunar_source.time.monotonic", return_value=100), \
                        patch("flightwatch.qunar_source.time.sleep", side_effect=lambda _: event.set()):
                    with self.assertRaisesRegex(ProviderError, "监控已停止"):
                        provider._request(ENDPOINT, {})
                self.assertEqual(provider.requests_used, 0)
                opener.assert_not_called()

    def test_public_request_has_no_cookies_token_or_callback_and_counts_budget(self):
        provider = QunarCalendarProvider(request_delay=0, max_requests=1)
        with patch("flightwatch.qunar_source.urllib.request.urlopen", return_value=io.BytesIO(b'{"ok":true}')) as opener:
            self.assertEqual(provider._request(SUGGEST_ENDPOINT, {"queryWord": "SHA"}), {"ok": True})
        request = opener.call_args.args[0]
        self.assertEqual(parse_qs(urlparse(request.full_url).query), {"queryWord": ["SHA"]})
        self.assertFalse(request.has_header("Cookie"))
        self.assertIsNone(request.data)
        self.assertEqual(provider.requests_used, 1)
        with self.assertRaisesRegex(ProviderError, "请求上限"):
            provider._request(ENDPOINT, {})

    def test_captcha_or_invalid_root_is_not_successful_empty_data(self):
        for body in (b"<html>captcha</html>", b"[]", b"null", b"\xff"):
            provider = QunarCalendarProvider(request_delay=0)
            with self.subTest(body=body), patch("flightwatch.qunar_source.urllib.request.urlopen", return_value=io.BytesIO(body)):
                with self.assertRaises(ProviderError):
                    provider._request(ENDPOINT, {})


class QunarAirportFlightTests(unittest.TestCase):
    """Contracts from Qunar's current public list JavaScript, not live fares."""

    def setUp(self):
        self.provider = QunarCalendarProvider(request_delay=0)
        self.origin = {"name": "上海", "code": "SHA", "is_international": False,
                       "airports": {"PVG": "浦东机场", "SHA": "虹桥机场"}}
        self.destination = {"name": "济州岛", "code": "CJU", "is_international": True,
                            "airports": {"CJU": "济州国际机场"}}
        self.route = Route("pvg-cju", "浦东济州", "PVG", "CJU", "qunar",
                           market="international", dates=(DOMESTIC_DAY,),
                           origin_scope="airport", destination_scope="airport",
                           origin_city_code="SHA", destination_city_code="CJU")
        self.provider._resolve_city = Mock(side_effect=[self.origin, self.destination])

    def flight(self, dep="PVG", arr="CJU", day=DOMESTIC_DAY, amount=600, **price_fields):
        segment = {"depAirportCode": dep, "arrAirportCode": arr,
                   "depCityCode": "SHA", "arrCityCode": "CJU", "depDate": day.isoformat(),
                   "carrierShortName": "测试航空"}
        return {"journey": {"code": "9C0000", "flightCode": f"9C0000|{dep}-{arr}|{day}",
                            "journeyType": "ONEWAY", "ticketInsufficient": False,
                            "trips": [{"flightSegments": [segment]}]},
                "price": {"lowTotalPrice": amount, "currencyCode": "CNY",
                          "totalTaxType": 1, **price_fields}}

    def response(self, *rows, complete=True, query="test-query", dep="上海", arr="济州岛"):
        return {"status": 0, "result": {"ctrlInfo": {"queryId": query,
            "completed": complete, "interval": 0, "dep": {"cityZh": dep},
            "arr": {"cityZh": arr}}, "flightPrices": {str(i): item for i, item in enumerate(rows)}}}

    def test_airport_search_uses_owner_cities_and_official_filters(self):
        self.provider._request = Mock(return_value=self.response(self.flight()))
        result = self.provider.search(self.route, TODAY)
        self.assertEqual(self.provider._resolve_city.call_args_list[0].args, ("SHA",))
        endpoint, params = self.provider._request.call_args.args
        self.assertEqual(endpoint, INTERNATIONAL_FLIGHTS_ENDPOINT)
        self.assertEqual((params["depAirport"], params["arrAirport"]), ("PVG", "CJU"))
        self.assertEqual((params["depCity"], params["arrCity"]), ("上海", "济州岛"))
        self.assertEqual((params["adultNum"], params["childNum"]), (1, 0))
        self.assertNotIn("retDate", params)
        self.assertEqual(result.quotes[0].origin_airport, "PVG")
        self.assertEqual(result.quotes[0].destination_airport, "CJU")
        self.assertEqual(result.quotes[0].price_basis, "total")
        link = parse_qs(urlparse(result.quotes[0].url).query)
        self.assertEqual(link["searchDepartureTime"], [DOMESTIC_DAY.isoformat()])
        self.assertIn("PVG-CJU", link["filterFlightCode"][0])

    def test_mixed_city_and_airport_only_sets_selected_side_filter(self):
        self.provider._request = Mock(return_value=self.response(self.flight(dep="SHA")))
        route = replace(self.route, origin="SHA", origin_scope="city")
        result = self.provider.search(route, TODAY)
        params = self.provider._request.call_args.args[1]
        self.assertNotIn("depAirport", params)
        self.assertEqual(params["arrAirport"], "CJU")
        self.assertEqual(result.quotes[0].origin_airport, "SHA")

    def test_only_matching_airport_and_departure_date_prices_survive(self):
        rows = [self.flight(), self.flight(dep="SHA", amount=100),
                self.flight(day=date(2026, 10, 14), amount=90), self.flight(arr="ICN", amount=70)]
        result = self.provider._parse_international_flights(rows, self.route, DOMESTIC_DAY,
                                                            self.origin, self.destination)
        self.assertEqual([q.price for q in result.quotes], [Decimal(600)])
        self.assertIn("3 条", result.warnings[0])

    def test_round_trip_unknown_tax_other_currency_and_missing_airports(self):
        wrong_trip = self.flight(); wrong_trip["journey"]["journeyType"] = "ROUNDTRIP"
        rows = [wrong_trip, self.flight(currencyCode="USD"), self.flight(totalTaxType=0)]
        result = self.provider._parse_international_flights(rows, self.route, DOMESTIC_DAY,
                                                            self.origin, self.destination)
        self.assertEqual(len(result.quotes), 1)
        self.assertFalse(result.quotes[0].comparable)
        wrong_airport = self.flight(); wrong_airport["journey"]["trips"][0]["flightSegments"][0].pop("depAirportCode")
        with self.assertRaisesRegex(ProviderError, "机场代码"):
            self.provider._parse_international_flights([wrong_airport], self.route, DOMESTIC_DAY,
                                                        self.origin, self.destination)

    def test_delta_poll_replaces_old_cheaper_price(self):
        self.provider._request = Mock(side_effect=[self.response(self.flight(amount=500), complete=False),
                                                  self.response(self.flight(amount=700))])
        result = self.provider.search(self.route, TODAY)
        self.assertEqual([q.price for q in result.quotes], [Decimal(700)])
        self.assertEqual(self.provider._request.call_args.args[1]["queryId"], "test-query")

    def test_wrong_route_cache_live_shape_stops_all_other_dates(self):
        route = replace(self.route, dates=(DOMESTIC_DAY, date(2026, 10, 16)))
        self.provider._request = Mock(return_value=self.response(complete=False, dep="北京", arr="名古屋"))
        with self.assertRaisesRegex(ProviderError, "回显了其他城市"):
            self.provider.search(route, TODAY)
        self.provider._request.assert_called_once()

    def test_incomplete_or_different_query_never_publishes_partial_minimum(self):
        self.provider._request = Mock(return_value=self.response(self.flight(), complete=False))
        with self.assertRaisesRegex(ProviderError, "未完成"):
            self.provider._international_day(self.route, DOMESTIC_DAY, self.origin, self.destination)
        self.assertEqual(self.provider._request.call_count, 4)
        self.provider._request = Mock(side_effect=[self.response(self.flight(), complete=False),
                                                  self.response(query="other-query")])
        with self.assertRaisesRegex(ProviderError, "标识改变"):
            self.provider._international_day(self.route, DOMESTIC_DAY, self.origin, self.destination)

    def test_login_slider_and_invalid_status_fail_before_polling(self):
        for payload in ({"needLogin": True}, {"needSlider": True}, {"isLimit": True}, {"status": False}):
            self.provider._request = Mock(return_value=payload)
            with self.subTest(payload=payload), self.assertRaises(ProviderError):
                self.provider._international_day(self.route, DOMESTIC_DAY, self.origin, self.destination)
            self.provider._request.assert_called_once()

    def domestic_flight(self, airport="浦东机场", price=500, **extra):
        return {"flightType": "list", "minPrice": price, "code": "MU0000",
                "binfo": {"depCity": "上海", "arrCity": "北京", "depAirport": airport,
                          "arrAirport": "首都机场", "depDate": DOMESTIC_DAY.isoformat(), **extra}}

    def test_domestic_actual_airport_names_are_verified_before_minimum(self):
        destination = {"name": "北京", "code": "PEK", "is_international": False,
                       "airports": {"PEK": "首都机场", "PKX": "大兴机场"}}
        route = replace(self.route, market="domestic", destination="PEK", destination_city_code="BJS")
        payload = {"ret": True, "data": {"flights": [self.domestic_flight(),
                                                       self.domestic_flight("虹桥机场", 100)]}}
        result = self.provider._parse_domestic_flights(payload, route, DOMESTIC_DAY, self.origin, destination)
        self.assertEqual([q.price for q in result.quotes], [Decimal(500)])
        self.assertEqual(result.quotes[0].origin_airport, "PVG")
        self.assertEqual(result.quotes[0].destination_airport, "PEK")
        self.assertFalse(result.quotes[0].comparable)

    def test_domestic_empty_anonymous_live_response_is_not_no_inventory(self):
        self.provider._resolve_city = Mock(side_effect=[self.origin,
            {"name": "北京", "code": "PEK", "is_international": False}])
        self.provider._request = Mock(return_value={"ret": True, "code": -1, "data": {
            "allFilter": [], "min_flight": {}, "flights": [], "total": 0, "geographyInfo": {}}})
        route = replace(self.route, market="domestic", destination="PEK", destination_city_code="BJS")
        with self.assertRaisesRegex(ProviderError, "未返回可验证"):
            self.provider.search(route, TODAY)
        self.assertEqual(self.provider._request.call_args.args[0], DOMESTIC_FLIGHTS_ENDPOINT)
        self.assertTrue(self.provider._request.call_args.kwargs["post"])

    def test_missing_owning_city_cannot_substitute_airport_as_city(self):
        with self.assertRaisesRegex(ProviderUnsupported, "所属城市"):
            self.provider.search(replace(self.route, origin_city_code=""), TODAY)
        self.provider._resolve_city.assert_not_called()

    def test_domestic_post_preserves_request_budget_without_token(self):
        provider = QunarCalendarProvider(request_delay=0)
        with patch("flightwatch.qunar_source.urllib.request.urlopen", return_value=io.BytesIO(b'{"ret":true}')) as opener:
            provider._request(DOMESTIC_FLIGHTS_ENDPOINT, {"departureCity": "上海"}, post=True)
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(parse_qs(request.data.decode()), {"departureCity": ["上海"]})
        self.assertFalse(request.has_header("Cookie"))
        self.assertEqual(provider.requests_used, 1)


if __name__ == "__main__":
    unittest.main()
