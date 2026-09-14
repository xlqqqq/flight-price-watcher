"""Minimized official JS contracts; all fares/transport are synthetic fixtures."""
import copy
import json
import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from flightwatch.models import ProviderError, Route
from flightwatch.tongcheng_source import TongchengProvider
from flightwatch.tongcheng_international_source import parse_list, request_list, search_airports

DAY = date(2026, 9, 21)
ROUTE = Route("pvg-cju", "浦东济州", "PVG", "CJU", "tongcheng", market="international",
    dates=(DAY,), origin_scope="airport", destination_scope="airport",
    origin_city_code="SHA", destination_city_code="CJU")


def flight(**changes):
    row = dict(dants=[dict(t="a", ac="PVG", an="浦东")], aants=[dict(t="a", ac="CJU", an="济州")],
        fdate=[DAY.isoformat()], acs=[dict(an="9C", ac="8573")], sp=200, tp=380)
    row.update(changes)
    return row


def result(*rows):
    return dict(result=True, done=1, res=list(rows))


class InternationalAirportTests(unittest.TestCase):
    def parse(self, data):
        return parse_list(data, ROUTE, DAY, {"PVG"}, {"CJU"}, "https://www.ly.com/iflight/book1.html")

    def test_exact_airport_filter_before_minimum(self):
        other = flight(dants=[dict(t="a", ac="SHA")], sp=1, tp=2)
        quotes = self.parse(result(other, flight())).quotes
        self.assertEqual(len(quotes), 1)
        self.assertEqual((quotes[0].origin_airport, quotes[0].destination_airport), ("PVG", "CJU"))
        self.assertEqual(quotes[0].price, Decimal("380"))
        self.assertTrue(quotes[0].comparable)

    def test_wrong_date_never_becomes_quote(self):
        with self.assertRaisesRegex(ProviderError, "日期"):
            self.parse(result(flight(fdate=["2026-09-22"])))

    def test_missing_airport_total_or_incomplete_result_rejected(self):
        cases = [result(flight(dants=[])), result(flight(tp=None)), result(flight(tp=True)),
                 result(flight(tp=100)), dict(result=True, done=0, res=[flight()])]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ProviderError):
                self.parse(data)

    def test_member_and_train_products_excluded(self):
        self.assertEqual(self.parse(result(flight(isMember=True),
            flight(dants=[dict(t="t", ac="PVG")]))).quotes, [])

    def test_empty_preload_is_followed_by_real_list(self):
        provider = TongchengProvider(request_delay=0)
        replies = [dict(search=result()), result(flight())]
        with patch("flightwatch.tongcheng_international_source.request_list", side_effect=replies) as request:
            found = search_airports(provider, ROUTE, [DAY])
        self.assertEqual([call.args[1] for call in request.call_args_list], ["preload", "list"])
        payload = request.call_args_list[1].args[2]
        self.assertEqual((payload["dc"], payload["ac"], payload["dt"]), ("SHA", "CJU", DAY.isoformat()))
        self.assertEqual((payload["an"], payload["cn"], payload["baby"], payload["tt"], payload["cabin"]), (1,0,0,0,"Y"))
        self.assertEqual(found.quotes[0].price, Decimal("380"))

    def test_valid_preload_is_labeled_as_cached(self):
        with patch("flightwatch.tongcheng_international_source.request_list", return_value=dict(search=result(flight()))) as request:
            found = search_airports(TongchengProvider(request_delay=0), ROUTE, [DAY])
        request.assert_called_once()
        self.assertTrue(any("缓存" in warning for warning in found.warnings))

    def test_first_day_failure_stops_remaining_dates_without_calendar(self):
        provider = TongchengProvider(request_delay=0)
        with patch("flightwatch.tongcheng_international_source.request_list", side_effect=ProviderError("HTTP 405")) as request, \
                patch.object(provider, "_request_international_calendar") as calendar:
            with self.assertRaisesRegex(ProviderError, "首日失败"):
                search_airports(provider, ROUTE, [DAY, DAY+timedelta(days=1)])
        request.assert_called_once()
        calendar.assert_not_called()

    def test_incomplete_list_never_reports_empty_success(self):
        replies = [dict(search=result())] + [dict(result=True, done=0, tid="public-session", res=[])]*4
        with patch("flightwatch.tongcheng_international_source.request_list", side_effect=replies):
            with self.assertRaisesRegex(ProviderError, "未完整"):
                search_airports(TongchengProvider(request_delay=0), ROUTE, [DAY])

    def test_booking_link_retains_city_and_airport_separately(self):
        query = parse_qs(urlsplit(TongchengProvider._international_page_url(ROUTE, DAY)).query)
        self.assertTrue(query["para"][0].startswith("SHA*CJU*"))
        self.assertEqual(query["departAirportCode"], ["PVG"])
        city = replace(ROUTE, origin="SHA", origin_scope="city")
        self.assertNotIn("departAirportCode", parse_qs(urlsplit(TongchengProvider._international_page_url(city, DAY)).query))

    def test_transport_has_no_account_cookie_or_auth_and_uses_json(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(dict(code=200, data=result())).encode()
        with patch("urllib.request.urlopen", return_value=response) as network:
            request_list(TongchengProvider(request_delay=0), "list", {"an":1})
        request = network.call_args.args[0]
        headers = {key.lower():value for key,value in request.header_items()}
        self.assertNotIn("cookie", headers)
        self.assertNotIn("authorization", headers)
        self.assertEqual(json.loads(request.data), {"an":1})
        self.assertIn("application/json", headers["content-type"])

    def test_http_refusal_exposes_status_without_response_body(self):
        with patch("urllib.request.urlopen", side_effect=HTTPError("https://www.ly.com",405,"private body",None,None)):
            with self.assertRaisesRegex(ProviderError,"HTTP 405") as caught:
                request_list(TongchengProvider(request_delay=0), "list", {})
        self.assertNotIn("private", str(caught.exception))
