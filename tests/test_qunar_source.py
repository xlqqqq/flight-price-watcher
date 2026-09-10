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
from flightwatch.qunar_source import ENDPOINT, SUGGEST_ENDPOINT, QunarCalendarProvider


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


if __name__ == "__main__":
    unittest.main()
