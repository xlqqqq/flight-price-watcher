from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
import unittest
from unittest.mock import patch
import urllib.error

from flightwatch.kiwi_source import KiwiDealsProvider, PAGE_ROOT
from flightwatch.models import ProviderError, ProviderUnsupported, Route


TODAY = date(2026, 9, 9)
DAY = date(2026, 10, 7)
ROUTE = Route(id="tokyo", name="上海到东京", origin="SHA", destination="TYO",
              provider="kiwi", market="international", dates=(DAY,))
ORIGIN = {"id": "shanghai_cn", "code": "SHA", "slug": "shanghai-china",
          "local_slug": "上海市-中国", "country": "CN", "airports": frozenset({"SHA", "PVG"})}
DESTINATION = {"id": "tokyo_jp", "code": "TYO", "slug": "tokyo-japan",
               "local_slug": "东京都-日本", "country": "JP", "airports": frozenset({"NRT", "HND"})}
PAGE_URL = PAGE_ROOT + "shanghai-china/tokyo-japan/"


def offer(day="2026-10-07", price=967):
    # Minimized shape of Kiwi's public FlightCollectionSchema, checked live
    # 2026-09-09. It does not establish the tax/passenger or stops basis.
    return {"@type": "ListItem", "item": {
        "@type": "Offer", "url": f"https://www.kiwi.com/cn/search/results/上海市-中国/东京都-日本/{day}/no-return/",
        "price": price, "priceCurrency": "CNY", "validFrom": "2026-09-09",
        "availability": "https://schema.org/InStock", "itemOffered": {
            "@type": "Trip", "itinerary": {"@type": "ItemList", "itemListElement": [{
                "@type": "Flight", "departureAirport": {"iataCode": "PVG"},
                "arrivalAirport": {"iataCode": "HND"}, "departureTime": day + "T19:10:00",
                "description": "直达航班（直飞）",
            }]},
        },
    }}


def schema(*offers):
    return {"@type": "CollectionPage", "url": PAGE_URL, "dateModified": "2026-09-09",
            "mainEntity": {"itemListElement": list(offers or [offer()])}}


def html(data):
    return '<html><script type="application/ld+json" data-test="FlightCollectionSchema">' + json.dumps(data, ensure_ascii=False) + '</script></html>'


def city_payload(city):
    return json.dumps({"locations": [{
        "type": "city", "active": True, "id": city["id"], "code": city["code"],
        "slug_en": city["slug"], "slug": city["local_slug"], "country": {"code": city["country"]},
    }]})


def airports_payload(city):
    return json.dumps({"locations": [{"type": "airport", "active": True, "code": code,
                                      "city": {"id": city["id"], "code": city["code"]}}
                                     for code in city["airports"]]})


class KiwiDealsTests(unittest.TestCase):
    def parse(self, data, route=ROUTE):
        return KiwiDealsProvider()._parse(html(data), route, TODAY, set(route.departure_dates(TODAY)), ORIGIN, DESTINATION, PAGE_URL)

    def test_exact_date_offer_is_reference_and_never_assumed_nonstop(self):
        result = self.parse(schema(offer("2026-10-09", 960), offer()))
        self.assertEqual(len(result.quotes), 1)
        quote = result.quotes[0]
        self.assertEqual((quote.origin, quote.destination, quote.departure_date), ("SHA", "TYO", DAY))
        self.assertEqual(quote.price, Decimal("967"))
        self.assertEqual(quote.currency, "CNY")
        self.assertEqual(quote.price_basis, "unknown")
        self.assertFalse(quote.comparable)
        self.assertIsNone(quote.stops)
        self.assertIn("不参与", " ".join(result.warnings))

    def test_lowest_matching_day_is_kept(self):
        result = self.parse(schema(offer(price=1050), offer(price=967)))
        self.assertEqual([quote.price for quote in result.quotes], [Decimal("967")])

    def test_roundtrip_price_not_used_for_single_trip(self):
        row = offer(price=1)
        row["item"]["url"] = row["item"]["url"].replace("no-return", "2026-10-14")
        self.assertEqual(self.parse(schema(row)).quotes, [])

    def test_nearby_airport_is_rejected(self):
        row = offer()
        row["item"]["itemOffered"]["itinerary"]["itemListElement"][0]["arrivalAirport"]["iataCode"] = "IBR"
        result = self.parse(schema(row))
        self.assertEqual(result.quotes, [])
        self.assertIn("机场归属", " ".join(result.warnings))

    def test_different_city_or_external_purchase_link_is_rejected(self):
        for part, replacement in [("东京都-日本", "大阪市-日本"), ("www.kiwi.com", "evil.example")]:
            with self.subTest(replacement=replacement):
                row = offer()
                row["item"]["url"] = row["item"]["url"].replace(part, replacement)
                self.assertEqual(self.parse(schema(row)).quotes, [])

    def test_schema_date_must_match_purchase_link(self):
        row = offer()
        row["item"]["itemOffered"]["itinerary"]["itemListElement"][0]["departureTime"] = "2026-10-08T19:10:00"
        self.assertEqual(self.parse(schema(row)).quotes, [])

    def test_no_currency_conversion_or_assumed_cny(self):
        row = offer()
        row["item"]["priceCurrency"] = "GBP"
        self.assertEqual(self.parse(schema(row)).quotes, [])

    def test_other_route_and_stale_page_raise(self):
        for field, value in [("url", PAGE_ROOT + "london-united-kingdom/paris-france/"), ("dateModified", "2026-08-01")]:
            with self.subTest(field=field):
                data = schema()
                data[field] = value
                with self.assertRaises(ProviderError):
                    self.parse(data)

    def test_missing_requested_date_is_explicit_not_replaced(self):
        result = self.parse(schema(offer("2026-10-09", 960)))
        self.assertEqual(result.quotes, [])
        self.assertIn("1 个出发日期暂无", " ".join(result.warnings))

    def test_invalid_prices_fail_closed(self):
        for value in [None, True, "NaN", "Infinity", 0, -1, "unavailable"]:
            with self.subTest(value=value), self.assertRaises(ProviderError):
                self.parse(schema(offer(price=value)))

    def test_unsupported_filters_do_not_make_requests(self):
        provider = KiwiDealsProvider()
        with patch.object(provider, "_request") as request:
            for updates in [{"nonstop": True}, {"stay_nights": 7}, {"travel_class": 2}, {"currency": "GBP"}]:
                with self.subTest(updates=updates), self.assertRaises(ProviderUnsupported):
                    provider.search(replace(ROUTE, **updates), TODAY)
        request.assert_not_called()

    def test_cities_and_airports_cached_five_then_one_requests(self):
        provider = KiwiDealsProvider(request_delay=0)
        responses = [city_payload(ORIGIN), airports_payload(ORIGIN), city_payload(DESTINATION), airports_payload(DESTINATION), html(schema()), html(schema())]
        with patch.object(provider, "_request", side_effect=responses) as request:
            first = provider.search(ROUTE, TODAY)
            second = provider.search(ROUTE, TODAY)
        self.assertEqual(len(first.quotes), 1)
        self.assertEqual(first.quotes, second.quotes)
        self.assertEqual(request.call_count, 6)
        self.assertIn("type=subentity", request.call_args_list[1].args[0])
        self.assertEqual(request.call_args_list[-1].args[0], PAGE_URL)

    def test_cold_city_requires_exact_code_not_first_suggestion(self):
        provider = KiwiDealsProvider()
        prague = {"id": "prague_cz", "code": "PRG", "slug": "prague-czechia", "local_slug": "布拉格-捷克", "country": "CZ", "airports": frozenset({"PRG"})}
        payload = json.loads(city_payload(prague))
        wrong = deepcopy(payload["locations"][0])
        wrong.update(code="PVD", id="providence_ri_us")
        payload["locations"].insert(0, wrong)
        with patch.object(provider, "_request", side_effect=[json.dumps(payload), airports_payload(prague)]):
            resolved = provider._resolve_city("PRG")
        self.assertEqual(resolved["id"], "prague_cz")
        self.assertEqual(resolved["airports"], frozenset({"PRG"}))

    def test_unknown_city_and_foreign_airport_parent_fail(self):
        provider = KiwiDealsProvider()
        with patch.object(provider, "_request", return_value=city_payload(ORIGIN)), self.assertRaises(ProviderUnsupported):
            provider._resolve_city("PRG")
        with patch.object(provider, "_request", side_effect=[city_payload(ORIGIN), airports_payload(DESTINATION)]), self.assertRaises(ProviderUnsupported):
            provider._resolve_city("SHA")

    def test_cancelled_request_never_contacts_site_or_uses_budget(self):
        provider = KiwiDealsProvider(cancelled=lambda: True)
        with patch("flightwatch.kiwi_source.urllib.request.urlopen") as request, self.assertRaisesRegex(ProviderError, "监控已停止"):
            provider._request(PAGE_URL)
        request.assert_not_called()
        self.assertEqual(provider.requests_used, 0)

    def test_cancellation_during_interval_prevents_next_request(self):
        state = {"cancelled": False}
        provider = KiwiDealsProvider(cancelled=lambda: state["cancelled"])
        provider._last_request = 100.0
        with patch("flightwatch.kiwi_source.time.monotonic", return_value=100.1), patch("flightwatch.kiwi_source.time.sleep", side_effect=lambda _: state.update(cancelled=True)), patch("flightwatch.kiwi_source.urllib.request.urlopen") as request, self.assertRaisesRegex(ProviderError, "监控已停止"):
            provider._request(PAGE_URL)
        request.assert_not_called()
        self.assertEqual(provider.requests_used, 0)

    def test_budget_and_http_protection_do_not_retry(self):
        provider = KiwiDealsProvider(max_requests=0)
        with patch("flightwatch.kiwi_source.urllib.request.urlopen") as request, self.assertRaisesRegex(ProviderError, "请求上限"):
            provider._request(PAGE_URL)
        request.assert_not_called()
        provider = KiwiDealsProvider()
        error = urllib.error.HTTPError(PAGE_URL, 403, "Forbidden", {}, None)
        with patch("flightwatch.kiwi_source.urllib.request.urlopen", side_effect=error) as request, self.assertRaisesRegex(ProviderError, "HTTP 403"):
            provider._request(PAGE_URL)
        self.assertEqual(request.call_count, 1)

    def test_challenge_page_without_schema_is_not_empty_success(self):
        with self.assertRaisesRegex(ProviderError, "验证页面"):
            KiwiDealsProvider()._parse("<html>Verify you are human</html>", ROUTE, TODAY, {DAY}, ORIGIN, DESTINATION, PAGE_URL)

    def test_identical_repeated_schema_allowed_but_conflicting_schema_rejected(self):
        provider = KiwiDealsProvider()
        page = html(schema())
        result = provider._parse(page + page, ROUTE, TODAY, {DAY}, ORIGIN, DESTINATION, PAGE_URL)
        self.assertEqual(len(result.quotes), 1)
        with self.assertRaises(ProviderError):
            provider._parse(page + html(schema(offer(price=100))), ROUTE, TODAY, {DAY}, ORIGIN, DESTINATION, PAGE_URL)


if __name__ == "__main__":
    unittest.main()
