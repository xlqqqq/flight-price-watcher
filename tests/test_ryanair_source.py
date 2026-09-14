import copy
from datetime import date
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

from flightwatch import exchange
from flightwatch.models import ProviderError, ProviderUnsupported, Route
from flightwatch.ryanair_source import RyanairProvider

TODAY = date(2026, 9, 9)
DAY = date(2026, 9, 29)
RATE = {"amount": 1, "base": "GBP", "date": "2026-09-08", "rates": {"CNY": 9.0898}}
AIRPORTS = [{"code": "STN", "city": {"macCode": "LON"}},
            {"code": "LTN", "city": {"macCode": "LON"}}, {"code": "DUB", "city": {}}]
FARES = {"outbound": {"fares": [{"day": str(DAY), "departureDate": "2026-09-29T17:30:00",
         "price": {"value": 14.99, "currencyCode": "GBP"}, "soldOut": False, "unavailable": False}]}}


class RyanairTests(unittest.TestCase):
    def setUp(self):
        exchange._CACHE.clear()
        self.provider = RyanairProvider(request_delay=0)
        self.route = Route("test", "伦敦→都柏林", "LON", "DUB", "ryanair", market="international", dates=(DAY,))

    def test_metropolitan_airports_are_from_live_catalogue_not_fixed_places(self):
        with patch.object(self.provider, "_request", return_value=AIRPORTS):
            self.assertEqual(self.provider._city_airports("LON"), ["LTN", "STN"])
            self.assertEqual(self.provider._city_airports("DUB"), ["DUB"])
            with self.assertRaises(ProviderUnsupported):
                self.provider._city_airports("SHA")

    def test_foreign_fare_is_converted_with_dated_rate_and_not_used_for_threshold(self):
        with patch.object(self.provider, "_request", return_value=RATE):
            q = self.provider._parse(FARES, self.route, {DAY}, "STN", "DUB", TODAY)[0]
        self.assertEqual(q.price, Decimal("136.26"))
        self.assertEqual(q.currency, "CNY")
        self.assertFalse(q.comparable)
        self.assertEqual((q.origin_airport, q.destination_airport), ("STN", "DUB"))
        self.assertIn("GBP 14.99", q.price_note)
        self.assertIn("2026-09-08", q.price_note)
        self.assertIn("STN", q.url)
        self.assertEqual((q.original_price, q.original_currency), (Decimal("14.99"), "GBP"))
        self.assertEqual(q.exchange_date, date(2026, 9, 8))

    def test_wrong_departure_date_is_rejected(self):
        body = copy.deepcopy(FARES)
        body["outbound"]["fares"][0]["departureDate"] = "2026-09-30T17:30:00"
        with self.assertRaises(ProviderError):
            self.provider._parse(body, self.route, {DAY}, "STN", "DUB", TODAY)

    def test_unavailable_and_unselected_days_never_become_prices(self):
        body = copy.deepcopy(FARES)
        body["outbound"]["fares"][0].update(unavailable=True, price=None)
        self.assertEqual(self.provider._parse(body, self.route, {DAY}, "STN", "DUB", TODAY), [])
        self.assertEqual(self.provider._parse(FARES, self.route, {date(2026, 9, 30)}, "STN", "DUB", TODAY), [])

    def test_invalid_price_and_availability_are_rejected(self):
        for field, value in (("price", {"value": "NaN", "currencyCode": "GBP"}),
                             ("price", {"value": True, "currencyCode": "GBP"}), ("soldOut", 0)):
            body = copy.deepcopy(FARES)
            body["outbound"]["fares"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ProviderError):
                self.provider._parse(body, self.route, {DAY}, "STN", "DUB", TODAY)

    def test_all_airport_combinations_requested_and_one_failure_keeps_other_prices(self):
        def fetch(url):
            if "airports/en" in url:
                return AIRPORTS
            if "frankfurter" in url:
                return RATE
            if "/LTN/" in url:
                raise ProviderError("HTTP 404")
            self.assertIn("/STN/DUB/", url)
            return FARES
        with patch.object(self.provider, "_request", side_effect=fetch):
            result = self.provider.search(self.route, TODAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertTrue(any("LTN" in warning for warning in result.warnings))

    def test_airport_scope_requests_only_the_exact_catalogue_airport(self):
        route = Route(
            "exact", "斯坦斯特德→都柏林", "STN", "DUB", "ryanair",
            market="international", dates=(DAY,),
            origin_scope="airport", destination_scope="airport",
            origin_city_code="LON", destination_city_code="DUB",
        )
        self.provider._airports = AIRPORTS
        urls = []

        def fetch(url):
            urls.append(url)
            return RATE if "frankfurter" in url else FARES

        with patch.object(self.provider, "_request", side_effect=fetch):
            result = self.provider.search(route, TODAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertEqual(
            (result.quotes[0].origin, result.quotes[0].destination,
             result.quotes[0].origin_airport, result.quotes[0].destination_airport),
            ("STN", "DUB", "STN", "DUB"),
        )
        calendar_urls = [url for url in urls if "cheapestPerDay" in url]
        self.assertEqual(len(calendar_urls), 1)
        self.assertIn("/STN/DUB/", calendar_urls[0])
        self.assertFalse(any("/LTN/" in url for url in calendar_urls))

    def test_airport_scope_rejects_missing_or_wrong_owner_without_fare_request(self):
        self.provider._airports = AIRPORTS
        missing = Route(
            "exact", "机场", "STN", "DUB", "ryanair", market="international",
            dates=(DAY,), origin_scope="airport", origin_city_code="",
        )
        with patch.object(self.provider, "_request") as request, self.assertRaises(ProviderUnsupported):
            self.provider.search(missing, TODAY)
        request.assert_not_called()

        wrong = Route(
            "exact", "机场", "STN", "DUB", "ryanair", market="international",
            dates=(DAY,), origin_scope="airport", origin_city_code="MAN",
        )
        with patch.object(self.provider, "_request") as request, \
                self.assertRaisesRegex(ProviderUnsupported, "不属于"):
            self.provider.search(wrong, TODAY)
        request.assert_not_called()

    def test_all_source_failures_are_reported_as_error(self):
        self.provider._airports = AIRPORTS
        with patch.object(self.provider, "_request", side_effect=ProviderError("network")):
            with self.assertRaises(ProviderError):
                self.provider.search(self.route, TODAY)

    def test_cancellation_never_opens_network_or_uses_budget(self):
        self.provider.cancelled = lambda: True
        with patch("urllib.request.urlopen") as network, self.assertRaises(ProviderError):
            self.provider._request("https://www.ryanair.com/")
        network.assert_not_called()
        self.assertEqual(self.provider.requests_used, 0)


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        exchange._CACHE.clear()

    def test_recent_rate_cached_without_further_requests(self):
        fetch = Mock(return_value=RATE)
        self.assertEqual(exchange.cny_rate("GBP", TODAY, fetch), (Decimal("9.0898"), date(2026, 9, 8)))
        exchange.cny_rate("GBP", TODAY, fetch)
        fetch.assert_called_once()

    def test_wrong_currency_amount_or_stale_rates_never_silently_convert(self):
        for changes in ({"base": "EUR"}, {"amount": 100}, {"date": "2026-08-01"},
                        {"date": "2026-09-10"}, {"rates": {"CNY": 0}}, {"rates": {"CNY": "NaN"}}):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                exchange.cny_rate("GBP", TODAY, Mock(return_value={**RATE, **changes}))

    def test_failed_rate_not_cached_as_one_to_one(self):
        with self.assertRaises(ProviderError):
            exchange.cny_rate("GBP", TODAY, Mock(side_effect=ProviderError("unavailable")))
        self.assertNotIn("GBP", exchange._CACHE)


if __name__ == "__main__":
    unittest.main()
