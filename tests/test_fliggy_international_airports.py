"""Official public template-shaped fixtures; these are not live fare captures."""

import json
import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from flightwatch.fliggy_source import FliggyProvider, MAX_INTERNATIONAL_POLLS, _VerificationRequired
from flightwatch.models import ProviderError, Route


DAY = date(2026, 9, 21)
ROUTE = Route("pvg-cju", "浦东济州", "PVG", "CJU", "multi", dates=(DAY,),
              market="international", origin_scope="airport", destination_scope="airport",
              origin_city_code="SHA", destination_city_code="CJU")


def segment(dep="PVG", arr="CJU", dep_city="SHA", arr_city="CJU", number="9C8573"):
    return {"depCityCode": dep_city, "arrCityCode": arr_city,
            "depAirportCode": dep, "arrAirportCode": arr,
            "depTimeStr": f"{DAY} 12:00:00", "marketingFlightNo": number}


def item(segments=None, fare=200):
    return {"flightInfo": [{"flightSegments": segments or [segment()], "mainAirlineName": "春秋航空"}],
            "adultPrice": fare, "adultTax": 180, "totalAdultPrice": fare + 180,
            "quantity": 9, "promotionShowInfos": [], "priceDesc": ""}


def data(items=None, complete=True):
    return {"status": 200, "data": {"isContinue": not complete,
            "flightItems": [item()] if items is None else items}}


class FliggyInternationalAirportTests(unittest.TestCase):
    def setUp(self):
        self.provider = FliggyProvider(request_delay=0)

    def parse(self, items, route=ROUTE):
        return self.provider._parse_international_listing(data(items)["data"], route, DAY)

    def test_filters_real_endpoints_before_comparing_price(self):
        wrong = item([segment(dep="SHA")], fare=1)
        quote = self.parse([wrong, item()]).quotes[0]
        self.assertEqual(quote.price, Decimal("380"))
        self.assertEqual((quote.origin_airport, quote.destination_airport), ("PVG", "CJU"))
        self.assertEqual(quote.flight_number, "9C8573")
        self.assertEqual(quote.price_basis, "total")
        link = parse_qs(urlsplit(quote.url).query)
        self.assertEqual(link["pcLeaveFlightNo"], ["9C8573"])
        self.assertEqual(link["depDate"], [str(DAY)])

    def test_city_scope_keeps_all_airports_and_mixed_scope_filters_one_end(self):
        route = replace(ROUTE, origin="SHA", origin_scope="city")
        quote = self.parse([item([segment(dep="SHA")], fare=100), item()], route).quotes[0]
        self.assertEqual(quote.origin, "SHA")
        self.assertEqual(quote.origin_airport, "SHA")
        self.assertEqual(quote.price, Decimal("280"))
        wrong_destination = item([segment(arr="GMP")], fare=1)
        self.assertFalse(self.parse([wrong_destination], route).quotes)

    def test_connection_matches_last_arrival_not_intermediate_airport(self):
        flights = [segment(arr="CJU"), segment(dep="CJU", arr="ICN", dep_city="CJU", arr_city="SEL", number="KE1001")]
        route = replace(ROUTE, destination_city_code="SEL")
        self.assertFalse(self.parse([item(flights)], route).quotes)
        route = replace(route, destination="ICN")
        quote = self.parse([item(flights)], route).quotes[0]
        self.assertEqual(quote.destination_airport, "ICN")
        self.assertNotIn("pcLeaveFlightNo", quote.url)

    def test_missing_airports_dates_or_wrong_city_are_not_inferred(self):
        for field, value in [("depAirportCode", None), ("arrAirportCode", ""),
                             ("depCityCode", "BJS"), ("depTimeStr", "2026-09-22 12:00:00")]:
            with self.subTest(field=field):
                bad = item()
                bad["flightInfo"][0]["flightSegments"][0][field] = value
                with self.assertRaises(ProviderError):
                    self.parse([bad])

    def test_unknown_or_mismatched_tax_is_rejected(self):
        for change in ({"adultTax": -1}, {"adultTax": None}, {"totalAdultPrice": 1}, {"currency": "USD"}):
            with self.subTest(change=change), self.assertRaises(ProviderError):
                self.parse([dict(item(), **change)])

    def test_restricted_or_unavailable_offers_do_not_alert(self):
        for change in ({"priceDesc": "限两人"}, {"promotionShowInfos": [{"tag": "会员专享"}]},
                       {"hasMemberPrice": True}, {"quantity": 0}, {"quantity": True},
                       {"fareSource": 19}):
            with self.subTest(change=change):
                self.assertFalse(self.parse([dict(item(), **change)]).quotes)

    def test_transport_uses_official_city_search_economy_without_credentials(self):
        with patch.object(self.provider, "_open_jsonp", return_value=data()) as transport:
            self.provider._request_international_listing(ROUTE, DAY)
        request = transport.call_args.args[0]
        query = parse_qs(urlsplit(request.full_url).query)
        journey = json.loads(query["searchJourney"][0])[0]
        self.assertEqual((journey["depCityCode"], journey["arrCityCode"]), ("SHA", "CJU"))
        self.assertEqual(query["searchCabinType"], ["1"])
        self.assertEqual(query["childPassengerNum"], ["0"])
        self.assertEqual(query["infantPassengerNum"], ["0"])
        self.assertEqual(query["needMemberPrice"], ["false"])
        self.assertFalse(any(key.lower() in {"authorization", "cookie"} for key in request.headers))
        self.assertNotIn("iesToken", query)

    def test_waits_for_complete_snapshot_not_cheaper_intermediate_price(self):
        partial = data([item(fare=1)], complete=False)
        partial["data"].update(iesToken="public-response-continuation", queryRecordId="query-1")
        with patch.object(self.provider, "_request_international_listing", side_effect=[partial, data()]) as request:
            quote = self.provider.search(ROUTE, DAY).quotes[0]
        self.assertEqual(quote.price, Decimal("380"))
        self.assertEqual(request.call_args.args[2]["count"], "1")

    def test_incomplete_queries_are_bounded_and_never_use_calendar(self):
        partial = data(complete=False)
        partial["data"].update(iesToken="public-response-continuation")
        with patch.object(self.provider, "_request_international_listing", return_value=partial) as request, \
                patch.object(self.provider, "_request_month_calendar") as calendar, \
                self.assertRaisesRegex(ProviderError, "未完成"):
            self.provider.search(ROUTE, DAY)
        self.assertEqual(request.call_count, MAX_INTERNATIONAL_POLLS)
        calendar.assert_not_called()

    def test_verification_page_stops_remaining_dates_without_publishing_prices(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'<script>location="/_____tmd_____/punish"</script>'
        route = replace(ROUTE, dates=tuple(DAY + timedelta(days=i) for i in range(15)))
        with patch("urllib.request.urlopen", return_value=response) as transport, \
                self.assertRaisesRegex(ProviderError, "官网要求安全验证"):
            self.provider.search(route, DAY)
        self.assertEqual(transport.call_count, 1)

    def test_completed_empty_result_is_honest_not_sold_out(self):
        result = self.parse([])
        self.assertEqual(result.quotes, [])
        self.assertIn("不能据此判断售罄", result.warnings[0])

    def test_later_verification_preserves_success_and_stops_remaining_dates(self):
        route = replace(ROUTE, dates=tuple(DAY + timedelta(days=i) for i in range(4)))
        with patch.object(self.provider, "_international_day",
                          side_effect=[self.parse([item()]), _VerificationRequired("官网要求安全验证")]) as request:
            result = self.provider.search(route, DAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertEqual(request.call_count, 2)
        self.assertTrue(any("安全验证" in warning for warning in result.warnings))

    def test_budget_exhaustion_stops_dates_after_completed_result(self):
        self.provider.max_requests = 1
        route = replace(ROUTE, dates=(DAY, DAY + timedelta(days=1)))
        with patch.object(self.provider, "_open_jsonp", return_value=data()) as request:
            result = self.provider.search(route, DAY)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(result.quotes), 1)
        self.assertTrue(any("请求上限" in warning for warning in result.warnings))

    def test_city_only_international_routes_keep_fast_calendar(self):
        route = replace(ROUTE, origin="SHA", origin_scope="city", destination_scope="city")
        with patch.object(self.provider, "_search_international") as calendar, \
                patch.object(self.provider, "_request_international_listing") as listing:
            self.provider.search(route, DAY)
        calendar.assert_called_once_with(route, [DAY])
        listing.assert_not_called()
