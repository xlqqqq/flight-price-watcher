"""KAYAK/momondo probes must fail closed when anonymous HTML has no fares."""

import json
import unittest
import urllib.error
from dataclasses import replace
from datetime import date
from email.message import Message
from unittest.mock import MagicMock, patch

from flightwatch.kayak_source import KayakProvider
from flightwatch.models import ProviderError, ProviderUnsupported, Route
from flightwatch.momondo_source import MomondoProvider


DAY = date(2026, 10, 1)
ROUTE = Route(
    "international", "上海→东京", "SHA", "TYO", "kayak",
    dates=(DAY,), market="international",
)


def bootstrap(provider, *, route=ROUTE, status="NOT_STARTED", response=None, changes=None):
    path = f"{provider.site.path_prefix}/{route.origin}-{route.destination}/2026-10-01"
    params = {
        "t": f"{route.origin}-{route.destination}/2026-10-01",
        "display": "RP",
        "currency": "CNY",
        "vertical": "flights",
        "attributes": {},
        "id": "frp4",
        "sort": "price_a",
    }
    origin = {
        "locations": [{"display": route.origin, "airportCode": route.origin,
                       "metroForCodes": [route.origin]}],
        "isNearby": False,
    }
    destination = {
        "locations": [{"display": route.destination, "airportCode": route.destination,
                       "metroForCodes": [route.destination]}],
        "isNearby": False,
        "nearby": False,
    }
    data = {
        "brands": [provider.site.required_brand],
        "componentName": "ui/omni/page/OmniFlightsResultPage",
        "props": {"originalUri": path, "reqParams": dict(params)},
        "serverData": {
            "FlightSearchStatus": {
                "tripType": "oneway",
                "bags": {"carryon": 0, "checked": 0},
                "flexMode": "EXACT",
                "isUsingFlexDates": False,
                "isOpenFlex": False,
                "travelers": {
                    "adults": 1, "seniors": 0, "students": 0, "youth": 0,
                    "child": 0, "seatInfant": 0, "lapInfant": 0,
                    "childAges": [],
                },
                "legs": [{
                    "origin": origin, "departure": dict(origin),
                    "destination": destination, "date": "2026-10-01",
                    "flexDate": "EXACT_DATES", "cabin": "e",
                }],
            },
            "serverRequestState": {"params": dict(params)},
            "locale": {"currency": {"code": "CNY", "symbol": "¥"}},
            "searchState": {
                "pageNumber": 1, "filters": {}, "status": status,
                "currentSortMode": "price_a",
                **({"response": response} if response is not None else {}),
            },
        },
    }
    if changes:
        changes(data)
    return (
        "<!doctype html><html><head><title>Flight Search</title></head><body>"
        '<script id="jsonData_R9DataStorage" type="application/json">'
        + json.dumps(data) + "</script></body></html>"
    )


class FakeResponse:
    def __init__(self, body, url, content_type="text/html; charset=UTF-8"):
        self.body = body.encode() if isinstance(body, str) else body
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self.body[:limit] if limit >= 0 else self.body

    def geturl(self):
        return self.url


class SharedProbeContract:
    provider_class = KayakProvider

    def setUp(self):
        self.provider = self.provider_class(request_delay=0)

    def test_exact_official_url_keeps_route_day_currency_and_price_sort(self):
        url = self.provider.search_url(ROUTE, DAY)
        self.assertEqual(
            url,
            f"https://{self.provider.site.host}{self.provider.site.path_prefix}/"
            "SHA-TYO/2026-10-01?sort=price_a&currency=CNY",
        )

    def test_current_live_shape_is_explicit_dynamic_error_not_a_quote(self):
        with self.assertRaisesRegex(ProviderError, "动态搜索外壳.*未提供可核验"):
            self.provider._parse(bootstrap(self.provider), ROUTE, DAY)

    def test_airport_scope_keeps_exact_path_and_requires_exact_response_echo(self):
        route = replace(
            ROUTE, origin="PVG", destination="NRT",
            origin_scope="airport", destination_scope="airport",
            origin_city_code="SHA", destination_city_code="TYO",
        )
        self.assertIn("/PVG-NRT/2026-10-01", self.provider.search_url(route, DAY))
        with self.assertRaisesRegex(ProviderError, "动态搜索外壳"):
            self.provider._parse(bootstrap(self.provider, route=route), route, DAY)
        with self.assertRaises(ProviderError):
            self.provider._parse(
                bootstrap(
                    self.provider, route=route,
                    changes=lambda data: data["serverData"]["FlightSearchStatus"]
                    ["legs"][0]["origin"]["locations"][0].update(airportCode="SHA"),
                ),
                route, DAY,
            )

    def test_airport_scope_without_owner_stops_before_request(self):
        route = replace(
            ROUTE, origin="PVG", origin_scope="airport", origin_city_code="",
        )
        with patch.object(self.provider, "_request") as request, \
                self.assertRaises(ProviderUnsupported):
            self.provider.search(route, DAY)
        request.assert_not_called()

    def test_completed_empty_search_is_not_a_zero_price(self):
        result = self.provider._parse(
            bootstrap(self.provider, status="COMPLETED", response={
                "availableResultsCount": 0, "totalResultCount": 0, "results": [],
            }), ROUTE, DAY,
        )
        self.assertEqual(result.quotes, [])
        self.assertTrue(result.warnings)

    def test_completed_unknown_offer_shape_is_never_guessed(self):
        with self.assertRaisesRegex(ProviderError, "含税总价与官方购票链接"):
            self.provider._parse(
                bootstrap(self.provider, status="COMPLETED", response={
                    "availableResultsCount": 1, "totalResultCount": 1,
                    "results": [{"price": 1}],
                }), ROUTE, DAY,
            )

    def test_route_date_passengers_cabin_and_currency_are_bound(self):
        mutations = [
            lambda d: d["props"].update(originalUri="/wrong"),
            lambda d: d["props"]["reqParams"].update(t="CAN-TYO/2026-10-01"),
            lambda d: d["serverData"]["serverRequestState"]["params"].update(currency="USD"),
            lambda d: d["serverData"]["FlightSearchStatus"]["travelers"].update(adults=2),
            lambda d: d["serverData"]["FlightSearchStatus"]["travelers"].update(adults=True),
            lambda d: d["serverData"]["FlightSearchStatus"]["bags"].update(carryon=False),
            lambda d: d["serverData"]["FlightSearchStatus"]["legs"][0].update(date="2026-10-02"),
            lambda d: d["serverData"]["FlightSearchStatus"]["legs"][0].update(cabin="b"),
            lambda d: d["serverData"]["FlightSearchStatus"]["legs"][0]["origin"]["locations"][0].update(airportCode="CAN"),
            lambda d: d["serverData"]["FlightSearchStatus"]["legs"][0]["destination"].update(isNearby=True),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ProviderError):
                self.provider._parse(
                    bootstrap(self.provider, changes=mutation), ROUTE, DAY
                )

    def test_brand_component_duplicate_script_and_invalid_json_are_rejected(self):
        wrong_brand = bootstrap(
            self.provider, changes=lambda d: d.update(brands=["somewhere-else"])
        )
        with self.assertRaisesRegex(ProviderError, "品牌或组件"):
            self.provider._parse(wrong_brand, ROUTE, DAY)
        duplicate = bootstrap(self.provider).replace(
            "</body>",
            '<script id="jsonData_R9DataStorage" type="application/json">{}</script></body>',
        )
        with self.assertRaisesRegex(ProviderError, "唯一"):
            self.provider._parse(duplicate, ROUTE, DAY)
        invalid = (
            '<html><script id="jsonData_R9DataStorage" type="application/json">'
            '{"price":NaN}</script></html>'
        )
        with self.assertRaisesRegex(ProviderError, "有效 JSON"):
            self.provider._parse(invalid, ROUTE, DAY)
        duplicate_key = bootstrap(self.provider).replace(
            '"brands":', '"brands":["wrong"],"brands":', 1
        )
        with self.assertRaisesRegex(ProviderError, "有效 JSON"):
            self.provider._parse(duplicate_key, ROUTE, DAY)

    def test_challenge_is_reported_without_attempting_to_solve_it(self):
        for html in (
            "<html><title>Please verify that you are a real user</title></html>",
            '<html><script src="/px-captcha.js"></script></html>',
        ):
            with self.subTest(html=html), self.assertRaisesRegex(
                ProviderError, "不会.*绕过|不会处理"
            ):
                self.provider._parse(html, ROUTE, DAY)

    def test_request_sends_no_cookie_authorization_or_form_token(self):
        url = self.provider.search_url(ROUTE, DAY)
        response = FakeResponse(bootstrap(self.provider), url)
        seen = {}

        def open_request(request, timeout):
            seen["request"] = request
            seen["timeout"] = timeout
            return response

        with patch("urllib.request.urlopen", side_effect=open_request):
            text = self.provider._request(url)
        request = seen["request"]
        self.assertIn("jsonData_R9DataStorage", text)
        self.assertIsNone(request.get_header("Cookie"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Formtoken"))
        self.assertEqual(seen["timeout"], 30)

    def test_redirect_mime_and_network_failures_are_not_parsed(self):
        url = self.provider.search_url(ROUTE, DAY)
        cases = [
            FakeResponse(bootstrap(self.provider), "https://evil.example/flights"),
            FakeResponse("{}", url, "application/json"),
            urllib.error.URLError("private detail"),
            urllib.error.HTTPError(url, 403, "forbidden", {}, None),
        ]
        for outcome in cases:
            with self.subTest(outcome=outcome), patch(
                "urllib.request.urlopen",
                side_effect=outcome if isinstance(outcome, BaseException) else None,
                return_value=None if isinstance(outcome, BaseException) else outcome,
            ), self.assertRaises(ProviderError):
                self.provider._request(url)

    def test_dynamic_shell_stops_after_first_selected_date(self):
        route = Route(
            "many", "上海→东京", "SHA", "TYO", self.provider.site.provider_id,
            dates=(DAY, date(2026, 10, 2)), market="international",
        )
        with patch.object(
            self.provider, "_request", return_value=bootstrap(self.provider)
        ) as request, self.assertRaisesRegex(ProviderError, "2026-10-01"):
            self.provider.search(route, DAY)
        request.assert_called_once()

    def test_unsupported_filters_and_invalid_codes_never_make_a_request(self):
        variants = [
            Route("x", "x", "SHA", "TYO", "x", dates=(DAY,), currency="USD"),
            Route("x", "x", "SHA", "TYO", "x", dates=(DAY,), stay_nights=2),
            Route("x", "x", "SHA", "TYO", "x", dates=(DAY,), travel_class=2),
            Route("x", "x", "SHA", "TYO", "x", dates=(DAY,), nonstop=True),
        ]
        for route in variants:
            with self.subTest(route=route), patch.object(self.provider, "_request") as request:
                with self.assertRaises(ProviderUnsupported):
                    self.provider.search(route, DAY)
                request.assert_not_called()
        for origin, destination in (("sha", "TYO"), ("SHA", "SHA"), ("XXXX", "TYO")):
            with self.subTest(origin=origin, destination=destination), self.assertRaises(ProviderError):
                self.provider.search(
                    Route("x", "x", origin, destination, "x", dates=(DAY,)), DAY
                )

    def test_cancellation_and_budget_are_enforced_before_transport(self):
        url = self.provider.search_url(ROUTE, DAY)
        cancelled = self.provider_class(request_delay=0, cancelled=lambda: True)
        with patch("urllib.request.urlopen") as open_request, self.assertRaisesRegex(
            ProviderError, "取消"
        ):
            cancelled._request(url)
        open_request.assert_not_called()
        exhausted = self.provider_class(request_delay=0, max_requests=0)
        with patch("urllib.request.urlopen") as open_request, self.assertRaisesRegex(
            ProviderError, "请求上限"
        ):
            exhausted._request(url)
        open_request.assert_not_called()


class KayakProviderTests(SharedProbeContract, unittest.TestCase):
    provider_class = KayakProvider


class MomondoProviderTests(SharedProbeContract, unittest.TestCase):
    provider_class = MomondoProvider

    def test_momondo_does_not_reuse_kayak_host_or_path(self):
        momondo = self.provider.search_url(ROUTE, DAY)
        kayak = KayakProvider.search_url(ROUTE, DAY)
        self.assertIn("www.momondo.com/flight-search/", momondo)
        self.assertIn("www.kayak.com/flights/", kayak)
        self.assertNotEqual(momondo, kayak)


if __name__ == "__main__":
    unittest.main()
