from datetime import date, timedelta
from decimal import Decimal
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from flightwatch.models import ProviderError, ProviderUnsupported, Route
from flightwatch.spring_source import CALENDAR, SpringAirlinesProvider


TODAY = date(2026, 9, 14)
DAY = date(2026, 10, 15)
ROUTE = Route("spring", "上海→东京", "SHA", "TYO", "spring",
              market="international", dates=(DAY,))


def route_html(day=DAY, *, origin="SHA", destination="TYO", international="true"):
    values = {
        "oriCode": origin, "desCode": destination, "departure": "上海", "arrival": "东京(羽田/成田)",
        "currency": "0", "departureDate": day.isoformat(), "returnDate": "", "ifRet": "false",
        "isIJFlight": "false", "isBg": "false", "ActId": "0", "isEmployee": "false",
        "saNum": "1", "scNum": "0", "siNum": "0", "sType": "0", "SpecTravTypeId": "0",
        "IsJC": "false", "IsInternational": international,
    }
    return ("<html><body>" + "".join(
        f'<input type="hidden" name="{key}" value="{value}">' for key, value in values.items()
    ) + "</body></html>").encode()


def calendar(day=DAY, price=829):
    return json.dumps({"Code": "0", "PriceTrends": [
        {"Date": day.isoformat() + "T00:00:00", "Price": price}
    ]}).encode()


class SpringAirlinesTests(unittest.TestCase):
    def setUp(self):
        self.provider = SpringAirlinesProvider(request_delay=0)

    def test_route_page_strictly_echoes_route_date_passenger_trip_and_market(self):
        final = self.provider._route_url(ROUTE, DAY)
        with patch.object(self.provider, "_request", return_value=(route_html(), "text/html", final)):
            departure, arrival, referer = self.provider._route_context(ROUTE, DAY)
        self.assertEqual((departure, arrival), ("上海", "东京(羽田/成田)"))
        self.assertEqual(referer, final)
        query = parse_qs(final.split("?", 1)[1])
        self.assertEqual(query["ANum"], ["1"])
        self.assertEqual(query["IfRet"], ["false"])

    def test_route_page_wrong_date_or_route_is_rejected(self):
        final = self.provider._route_url(ROUTE, DAY)
        for body in [route_html(DAY + timedelta(days=1)), route_html(origin="PVG")]:
            with self.subTest(body=body), patch.object(self.provider, "_request", return_value=(body, "text/html", final)), \
                 self.assertRaises(ProviderUnsupported):
                self.provider._route_context(ROUTE, DAY)

    def test_exact_calendar_day_and_positive_price_only(self):
        self.assertEqual(self.provider._parse_calendar(json.loads(calendar()), DAY), Decimal("829"))
        self.assertIsNone(self.provider._parse_calendar(
            {"Code": 0, "PriceTrends": [{"Date": DAY.isoformat(), "Price": 0}]}, DAY))
        self.assertIsNone(self.provider._parse_calendar(
            {"Code": 0, "PriceTrends": [{"Date": "2026-10-16", "Price": 1}]}, DAY))
        for invalid_code in (False, True, None):
            with self.subTest(code=invalid_code), self.assertRaises(ProviderError):
                self.provider._parse_calendar({"Code": invalid_code, "PriceTrends": []}, DAY)

    def test_ambiguous_or_invalid_calendar_price_is_rejected(self):
        for rows in [
            [{"Date": DAY.isoformat(), "Price": 1}, {"Date": DAY.isoformat(), "Price": 2}],
            [{"Date": DAY.isoformat(), "Price": "NaN"}],
            [{"Date": "not-a-date", "Price": 1}],
        ]:
            with self.subTest(rows=rows), self.assertRaises(ProviderError):
                self.provider._parse_calendar({"Code": "0", "PriceTrends": rows}, DAY)

    def test_search_requests_official_tax_inclusive_exact_day(self):
        final = self.provider._route_url(ROUTE, DAY)
        calls = []
        def request(url, **kwargs):
            calls.append((url, kwargs))
            if url == CALENDAR:
                return calendar(), "application/json", CALENDAR
            return route_html(), "text/html", final
        with patch.object(self.provider, "_request", side_effect=request):
            result = self.provider.search(ROUTE, TODAY)
        quote = result.quotes[0]
        self.assertEqual(quote.price, Decimal("829"))
        self.assertTrue(quote.comparable)
        self.assertIsNone(quote.stops)
        fields = parse_qs(calls[1][1]["data"].decode())
        self.assertEqual(fields["DepartureDate"], [DAY.isoformat()])
        self.assertEqual(fields["IsShowTaxprice"], ["true"])
        self.assertEqual(fields["Departure"], ["上海"])
        self.assertEqual(fields["Arrival"], ["东京(羽田/成田)"])
        self.assertEqual(fields["IfRet"], ["false"])

    def test_airport_scope_is_preserved_by_route_echo_and_quote(self):
        route = Route(
            "spring-airports", "浦东→成田", "PVG", "NRT", "spring",
            market="international", dates=(DAY,),
            origin_scope="airport", destination_scope="airport",
            origin_city_code="SHA", destination_city_code="TYO",
        )
        final = self.provider._route_url(route, DAY)

        def request(url, **kwargs):
            if url == CALENDAR:
                return calendar(), "application/json", CALENDAR
            return route_html(origin="PVG", destination="NRT"), "text/html", final

        with patch.object(self.provider, "_request", side_effect=request):
            quote = self.provider.search(route, TODAY).quotes[0]
        self.assertEqual((quote.origin, quote.destination), ("PVG", "NRT"))
        self.assertEqual((quote.origin_airport, quote.destination_airport), ("PVG", "NRT"))
        self.assertIn("/PVG-NRT.html", quote.url)

    def test_airport_scope_without_owner_stops_before_network(self):
        route = Route(
            **{**ROUTE.__dict__, "origin": "PVG", "origin_scope": "airport",
               "origin_city_code": ""}
        )
        with patch.object(self.provider, "_request") as request, \
                self.assertRaises(ProviderUnsupported):
            self.provider.search(route, TODAY)
        request.assert_not_called()

    def test_first_bad_mime_stops_before_later_dates(self):
        route = Route(**{**ROUTE.__dict__, "dates": (DAY, DAY + timedelta(days=1))})
        final = self.provider._route_url(route, DAY)
        with patch.object(self.provider, "_request", side_effect=[
            (route_html(), "text/html", final),
            (b"<html>", "text/html", CALENDAR),
        ]) as request, self.assertRaisesRegex(ProviderError, DAY.isoformat()):
            self.provider.search(route, TODAY)
        self.assertEqual(request.call_count, 2)

    def test_request_budget_and_filters_stop_before_network(self):
        with patch.object(self.provider, "_request") as request, self.assertRaisesRegex(ProviderError, "请求上限"):
            SpringAirlinesProvider(max_requests=1).search(ROUTE, TODAY)
        request.assert_not_called()
        for route in [Route(**{**ROUTE.__dict__, "currency": "USD"}),
                      Route(**{**ROUTE.__dict__, "stay_nights": 2}),
                      Route(**{**ROUTE.__dict__, "travel_class": 2}),
                      Route(**{**ROUTE.__dict__, "nonstop": True})]:
            with self.subTest(route=route), patch.object(self.provider, "_request") as request, \
                 self.assertRaises(ProviderUnsupported):
                self.provider.search(route, TODAY)
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
