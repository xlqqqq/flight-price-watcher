"""Skyscanner anonymous Radar provider tests; all transport is mocked."""

import copy
import json
from datetime import date, timedelta
from decimal import Decimal
from email.message import Message
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from flightwatch.models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult
from flightwatch.skyscanner_source import (
    RADAR_ENDPOINT, SkyscannerProvider, _Context,
)


DAY = date(2026, 10, 1)
TODAY = date(2026, 9, 14)
ROUTE = Route("sky-test", "上海东京", "SHA", "TYO", "skyscanner",
              dates=(DAY,), market="international")
CONTEXT = _Context("27546079", "27542089", "CN", "JP", "csha", "tyoa",
                   "de624805-7b84-49a0-b87a-9f8de9421071", "27546079", "27542089")


def context_state(day=DAY, origin_raw="sha", destination_raw="tyo",
                  origin_country="CN", destination_country="JP"):
    state = {
        "culture": {"currency": "CNY", "market": "SG", "locale": "en-GB", "tld": "com.sg"},
        "userInfo": {"isLoggedIn": False, "utid": "unused-and-never-sent"},
        "viewId": CONTEXT.view_id,
        "searchParams": {
            "outboundDate": day.isoformat(), "tripType": "one-way",
            "cabinClass": "economy", "adultsV2": 1, "originalAdults": 1,
            "childrenV2": [], "preferDirects": False, "outboundAlts": False,
            "inboundAlts": False, "fareAttributes": [],
            "origin": {"rawLocationId": origin_raw, "geoContainerId": CONTEXT.origin_entity,
                       "countryId": origin_country, "cityId": "CSHA"},
            "destination": {"rawLocationId": destination_raw,
                            "geoContainerId": CONTEXT.destination_entity,
                            "countryId": destination_country, "cityId": "TYOA"},
            "legs": [{"originGeoContainerId": CONTEXT.origin_entity,
                      "destinationGeoContainerId": CONTEXT.destination_entity,
                      "date": day.isoformat()}],
        },
    }
    # Include the complete official location identity echo used to distinguish
    # a city search from an identically named airport search.
    params = state["searchParams"]
    for side in ("origin", "destination"):
        location = params[side]
        location.update(entityId=location["geoContainerId"], type="City")
        params[side + "Type"] = "City"
        params[side + "CityId"] = location["cityId"]
        for suffix, value in (("EntityId", location["entityId"]), ("Type", "City"),
                              ("CountryId", location["countryId"]), ("CityId", location["cityId"])):
            params["legs"][0][side + suffix] = value
    return state


def page(state=None):
    raw = json.dumps(state or context_state(), separators=(",", ":"))
    # The real server object has two bare undefined object values.  Exercise
    # the narrow safe replacement without introducing executable JavaScript.
    raw = raw[:-1] + ',"homepageAds":undefined}'
    return '<html><script>window["__internal"] = ' + raw + ';</script></html>'


def place(airport, entity, city_entity, city_code, country):
    return {
        "entityId": entity, "flightPlaceId": airport, "displayCode": airport,
        "parent": {"entityId": city_entity, "flightPlaceId": "C" + city_code,
                   "displayCode": city_code, "type": "City"},
        "type": "Airport", "countryId": country,
    }


def itinerary(day=DAY, amount=1803, origin_code="SHA", destination_code="TYO"):
    origin = place("PVG", "128667077", CONTEXT.origin_entity, origin_code, "CN")
    destination = place("NRT", "128668889", CONTEXT.destination_entity,
                        destination_code, "JP")
    start, end = f"{day.isoformat()}T20:05:00", f"{day.isoformat()}T23:55:00"
    option_id = "price-option"
    link = (
        "/transport_deeplink/4.0/SG/en-GB/CNY/agent/1/15641.14788."
        + day.isoformat()
        + "/air/trava/flights?passengers=1&channel=website&cabin_class=economy"
          "&client_id=skyscanner_website&ticket_price=" + f"{amount:.2f}"
    )
    return {
        "id": "flight-" + day.isoformat(),
        "fareAttributes": {},
        "price": {"raw": amount, "formatted": f"¥{amount}", "pricingOptionId": option_id},
        "legs": [{
            "origin": {"entityId": origin["entityId"]},
            "destination": {"entityId": destination["entityId"]},
            "departure": start, "arrival": end, "stopCount": 0,
            "segments": [{
                "origin": origin, "destination": destination,
                "departure": start, "arrival": end, "flightNumber": "970",
                "marketingCarrier": {"displayCode": "NH", "alternateId": "NH"},
            }],
        }],
        "pricingOptions": [{
            "pricingOptionId": option_id, "fareAttributes": {},
            "price": {"updateStatus": "current", "amount": amount},
            "items": [{
                "price": {"updateStatus": "current", "amount": amount},
                "bookingProposition": "PBOOK", "agentId": "agent", "url": link,
            }],
        }],
    }


def radar(status, results, session="abc_DEF-123="):
    return {"context": {"status": status, "sessionId": session},
            "itineraries": {"results": results}}


class SkyscannerParseTests(unittest.TestCase):
    def test_page_context_promotes_airport_collision_to_city_entities(self):
        state = context_state()
        params = state["searchParams"]
        params["origin"].update(entityId="128667076", type="Airport", airportId="SHA", id="SHA")
        params.update(originType="Airport", originIataCode="SHA")
        params["legs"][0].update(originEntityId="128667076", originType="Airport")
        parsed = SkyscannerProvider()._parse_context(page(state), ROUTE, DAY)
        self.assertEqual(parsed, CONTEXT)
        payload = SkyscannerProvider._payload(parsed, DAY)
        self.assertEqual(payload["adults"], 1)
        self.assertEqual(payload["childAges"], [])
        self.assertEqual(payload["cabinClass"], "economy")
        self.assertEqual(payload["legs"][0]["legOrigin"]["entityId"], CONTEXT.origin_entity)
        self.assertEqual(payload["legs"][0]["dates"],
                         {"@type": "date", "year": "2026", "month": "10", "day": "01"})

    def test_complete_result_returns_exact_total_and_official_deep_link(self):
        provider = SkyscannerProvider()
        result = provider._parse_results([itinerary()], ROUTE, DAY, CONTEXT)
        quote = result.quotes[0]
        self.assertEqual(quote.price, Decimal("1803"))
        self.assertEqual(quote.currency, "CNY")
        self.assertTrue(quote.comparable)
        self.assertEqual(quote.flight_number, "NH970")
        self.assertEqual(quote.stops, 0)
        self.assertEqual((quote.origin_airport, quote.destination_airport), ("PVG", "NRT"))
        self.assertTrue(quote.url.startswith("https://www.skyscanner.com.sg/transport_deeplink/"))
        query = parse_qs(urlsplit(quote.url).query)
        self.assertEqual(query["passengers"], ["1"])
        self.assertEqual(query["cabin_class"], ["economy"])
        self.assertEqual(query["ticket_price"], ["1803.00"])

    def test_route_date_and_booking_price_mismatches_are_rejected(self):
        variants = []
        wrong_route = itinerary(origin_code="BJS")
        variants.append(wrong_route)
        wrong_day = itinerary(day=DAY + timedelta(days=1))
        variants.append(wrong_day)
        wrong_link = itinerary()
        wrong_link["pricingOptions"][0]["items"][0]["url"] = wrong_link["pricingOptions"][0]["items"][0]["url"].replace("1803.00", "1.00")
        variants.append(wrong_link)
        for value in variants:
            with self.subTest(value=value["id"]), self.assertRaises(ProviderError):
                SkyscannerProvider()._parse_results([value], ROUTE, DAY, CONTEXT)

    def test_earlier_arrival_local_clock_across_time_zones_is_allowed(self):
        value = itinerary()
        value["legs"][0]["arrival"] = f"{DAY.isoformat()}T08:00:00"
        value["legs"][0]["segments"][0]["arrival"] = f"{DAY.isoformat()}T08:00:00"
        result = SkyscannerProvider()._parse_results([value], ROUTE, DAY, CONTEXT)
        self.assertEqual(result.quotes[0].price, Decimal("1803"))

    def test_market_login_currency_and_query_echo_must_match(self):
        mutations = []
        state = context_state(); state["culture"]["currency"] = "USD"; mutations.append((state, ROUTE))
        state = context_state(); state["userInfo"]["isLoggedIn"] = True; mutations.append((state, ROUTE))
        state = context_state(day=DAY + timedelta(days=1)); mutations.append((state, ROUTE))
        domestic = Route(**{**ROUTE.__dict__, "market": "domestic"}); mutations.append((context_state(), domestic))
        for state, route in mutations:
            with self.subTest(state=state), self.assertRaises(ProviderError):
                SkyscannerProvider()._parse_context(page(state), route, DAY)

    def test_duplicate_context_json_and_script_values_are_rejected(self):
        duplicate = page().replace('"currency":"CNY"', '"currency":"CNY","currency":"CNY"')
        executable = page().replace("undefined", "doSomething()")
        for html in [duplicate, executable, "<html>captcha verify you are human</html>"]:
            with self.subTest(html=html[:80]), self.assertRaises(ProviderError):
                SkyscannerProvider()._parse_context(html, ROUTE, DAY)

    def test_member_or_multi_item_option_never_becomes_price(self):
        value = itinerary()
        value["pricingOptions"][0]["fareAttributes"] = {"member": True}
        result = SkyscannerProvider()._parse_results([value], ROUTE, DAY, CONTEXT)
        self.assertEqual(result.quotes, [])
        self.assertTrue(any("受限" in warning for warning in result.warnings))

    def test_pending_provider_price_is_not_comparable(self):
        value = itinerary()
        value["pricingOptions"][0]["price"]["updateStatus"] = "pending"
        value["pricingOptions"][0]["items"][0]["price"]["updateStatus"] = "pending"
        result = SkyscannerProvider()._parse_results([value], ROUTE, DAY, CONTEXT)
        self.assertEqual(result.quotes, [])


class SkyscannerRequestTests(unittest.TestCase):
    def test_search_waits_for_complete_delta_and_preserves_initial_results(self):
        provider = SkyscannerProvider(request_delay=0)
        responses = [radar("incomplete", [itinerary()]), radar("complete", [], "next_session")]
        with patch.object(provider, "_request_page", return_value=page()), \
                patch.object(provider, "_request_json", side_effect=responses) as request:
            result = provider.search(ROUTE, TODAY)
        self.assertEqual(result.quotes[0].price, Decimal("1803"))
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1].args[0], RADAR_ENDPOINT + "abc_DEF-123%3D")

    def test_incomplete_search_never_publishes_initial_price(self):
        provider = SkyscannerProvider(request_delay=0)
        with patch.object(provider, "_request_page", return_value=page()), \
                patch.object(provider, "_request_json",
                             return_value=radar("incomplete", [itinerary()])), \
                self.assertRaisesRegex(ProviderError, "未完成"):
            provider.search(ROUTE, TODAY)

    def test_api_requests_send_no_cookie_login_token_jha_or_auth(self):
        headers = Message(); headers.add_header("Content-Type", "application/json")

        class Response:
            url = RADAR_ENDPOINT
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return b"{}"
            @property
            def headers(self): return headers

        provider = SkyscannerProvider(request_delay=0)
        with patch("urllib.request.urlopen", return_value=Response()) as opened:
            provider._request_json(RADAR_ENDPOINT, CONTEXT.view_id,
                                   provider._payload(CONTEXT, DAY))
        request = opened.call_args.args[0]
        lowered = {key.lower(): value for key, value in request.header_items()}
        for forbidden in ("cookie", "authorization", "x-api-key", "jha",
                          "x-skyscanner-session-id-token"):
            self.assertNotIn(forbidden, lowered)
        self.assertEqual(lowered["x-skyscanner-market"], "SG")
        self.assertEqual(lowered["x-skyscanner-currency"], "CNY")
        self.assertEqual(lowered["x-skyscanner-viewid"], CONTEXT.view_id)
        self.assertEqual(lowered["x-skyscanner-trustedfunnelid"], CONTEXT.view_id)
        sent = json.loads(request.data)
        self.assertEqual(sent["adults"], 1)

    def test_unsupported_filters_cancel_and_budget_make_no_search_request(self):
        mutations = [
            {"currency": "USD"}, {"stay_nights": 2}, {"nonstop": True},
            {"travel_class": 2}, {"origin": "上海"}, {"destination": "SHA"},
        ]
        for mutation in mutations:
            provider = SkyscannerProvider()
            route = Route(**{**ROUTE.__dict__, **mutation})
            with self.subTest(mutation=mutation), patch.object(provider, "_request_page") as request, \
                    self.assertRaises(ProviderUnsupported):
                provider.search(route, TODAY)
            request.assert_not_called()
        for provider in [SkyscannerProvider(cancelled=lambda: True), SkyscannerProvider(max_requests=0)]:
            with self.subTest(provider=provider), patch("urllib.request.urlopen") as opened, \
                    self.assertRaises(ProviderError):
                provider.search(ROUTE, TODAY)
            opened.assert_not_called()

    def test_first_radar_failure_does_not_repeat_all_dates(self):
        route = Route(**{**ROUTE.__dict__, "dates": (DAY, DAY + timedelta(days=1))})
        provider = SkyscannerProvider(request_delay=0)
        with patch.object(provider, "_request_page", return_value=page()), \
                patch.object(provider, "_query_day", side_effect=ProviderError("结构变化")) as query, \
                self.assertRaisesRegex(ProviderError, "结构变化"):
            provider.search(route, TODAY)
        self.assertEqual(query.call_count, 1)

    def test_remaining_dates_overlap_and_results_keep_date_order(self):
        days = tuple(DAY + timedelta(days=index) for index in range(5))
        route = Route(**{**ROUTE.__dict__, "dates": days})
        provider = SkyscannerProvider(request_delay=0)
        barrier = threading.Barrier(4, timeout=2)
        lock = threading.Lock(); calls = 0

        def query(_route, departure, _context):
            nonlocal calls
            with lock:
                calls += 1; current = calls
            if current > 1:
                barrier.wait()
            return SearchResult([Quote("SHA", "TYO", departure,
                                      Decimal(1000 + departure.day), "CNY", "test")], [])

        with patch.object(provider, "_request_page", return_value=page()), \
                patch.object(provider, "_query_day", side_effect=query):
            result = provider.search(route, TODAY)
        self.assertEqual(calls, 5)
        self.assertEqual([quote.departure_date for quote in result.quotes], list(days))
        self.assertEqual([quote.price for quote in result.quotes],
                         [Decimal(1000 + day.day) for day in days])


if __name__ == "__main__":
    unittest.main()
