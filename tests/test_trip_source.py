"""Trip.com provider contract tests; no test accesses the network."""

import copy
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from email.message import Message
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from flightwatch.models import ProviderError, ProviderUnsupported, Route
from flightwatch.trip_source import ENDPOINT, TripComProvider, _AccessBlocked


DAY = date(2026, 10, 1)
TODAY = date(2026, 9, 14)
ROUTE = Route("trip-test", "上海东京", "SHA", "TYO", "trip",
              dates=(DAY,), market="international")


def policy(total, tax, flight_count=1):
    total, tax = Decimal(str(total)), Decimal(str(tax))
    native = lambda value: int(value) if value == value.to_integral_value() else float(value)
    return {
        "gradeInfoList": [
            {"segmentNo": index, "journeyNo": 1, "grade": 1}
            for index in range(1, flight_count + 1)
        ],
        "policyFlags": ["ECONOMY"],
        "price": {
            "totalPrice": native(total),
            "averagePrice": native(total),
            "totalTax": native(tax),
            "adult": {
                "salePrice": native(total - tax),
                "tax": native(tax),
                "discount": 0,
                "totalPrice": native(total),
            },
        },
        "tagList": [],
        "noteList": [],
        "preJourneyTagList": [],
        "sold": {},
        "cnyExchangeRate": 1,
    }


def row(day=DAY, total=1902, tax=1246, destination="TYO"):
    return {
        "journeyList": [{
            "journeyNo": 1,
            "transSectionList": [{
                "segmentNo": 1,
                "transportType": "FLIGHT",
                "departDateTime": f"{day.isoformat()} 11:45:00",
                "arriveDateTime": f"{day.isoformat()} 15:55:00",
                "departPoint": {"cityCode": "SHA", "airportCode": "SHA"},
                "arrivePoint": {"cityCode": destination, "airportCode": "HND"},
                "flightInfo": {"flightNo": "NH970", "airlineCode": "NH"},
            }],
        }],
        "policies": [policy(total, tax)],
    }


def response(day=DAY, rows=None, currency="CNY", origin="SHA", destination="TYO",
             origin_region="CN", destination_region="JP"):
    rows = [row(day)] if rows is None else rows
    prices = [item["price"] for result in rows for item in result.get("policies", [])]
    cheapest = min(prices, key=lambda value: value["totalPrice"]) if prices else None
    result = {
        "ResponseStatus": {"Errors": []},
        "head": {"retCode": "SUCCESS"},
        "basicInfo": {
            "recordCount": len(rows),
            "originalCount": len(rows),
            "currency": currency,
            "regionRoute": f"{origin_region}-{destination_region}",
            "searchCondition": {
                "orderBy": "Price", "direction": True, "selectJourneyList": [],
                "searchJourneys": [{
                    "journeyNo": 1, "departDate": day.isoformat(),
                    "departCity": {"code": origin, "region": origin_region,
                                   "airportList": ["SHA", "PVG"]},
                    "arriveCity": {"code": destination, "region": destination_region,
                                   "airportList": ["HND", "NRT"]},
                }],
            },
        },
        "itineraryList": rows,
    }
    if cheapest:
        result["basicInfo"]["lowestPrice"] = {
            "totalPrice": cheapest["totalPrice"], "totalTax": cheapest["totalTax"],
        }
    return result


def sse(data):
    return "event: message\ndata:" + json.dumps(data, default=str, separators=(",", ":")) + "\n\n"


class TripParseTests(unittest.TestCase):
    def parse(self, data=None, route=ROUTE, day=DAY):
        return TripComProvider()._parse_response(
            sse(data or response(day)), route, day, TripComProvider.official_url(route, day)
        )

    def test_exact_anonymous_total_price_contract(self):
        data = response(rows=[row(total=2450, tax=800), row(total=1902, tax=1246)])
        quote = self.parse(data).quotes[0]
        self.assertEqual(quote.price, Decimal("1902"))
        self.assertEqual(quote.currency, "CNY")
        self.assertTrue(quote.comparable)
        self.assertEqual(quote.flight_number, "NH970")
        self.assertEqual(quote.stops, 0)
        self.assertEqual(quote.provider, "trip")
        self.assertEqual((quote.origin_airport, quote.destination_airport), ("SHA", "HND"))
        params = parse_qs(urlsplit(quote.url).query)
        self.assertEqual(params["dcity"], ["sha"])
        self.assertEqual(params["acity"], ["tyo"])
        self.assertEqual(params["ddate"], [DAY.isoformat()])
        self.assertEqual(params["triptype"], ["ow"])
        self.assertEqual(params["class"], ["y"])

    def test_wrong_currency_route_date_and_market_are_rejected(self):
        cases = [
            (response(currency="USD"), ROUTE),
            (response(origin="BJS"), ROUTE),
            (response(day=DAY + timedelta(days=1)), ROUTE),
            (response(), Route(**{**ROUTE.__dict__, "market": "domestic"})),
        ]
        for data, route in cases:
            with self.subTest(data=data["basicInfo"].get("currency"), route=route.market), \
                    self.assertRaises(ProviderError):
                self.parse(data, route)

    def test_incoherent_price_tax_or_summary_is_rejected(self):
        for mutation in ("adult_total", "tax", "summary"):
            data = response()
            if mutation == "adult_total":
                data["itineraryList"][0]["policies"][0]["price"]["adult"]["totalPrice"] = 1901
            elif mutation == "tax":
                data["itineraryList"][0]["policies"][0]["price"]["adult"]["tax"] = 1
            else:
                data["basicInfo"]["lowestPrice"]["totalPrice"] = 1
            with self.subTest(mutation=mutation), self.assertRaises(ProviderError):
                self.parse(data)

    def test_mismatched_airport_chain_is_excluded_not_used_as_cheapest(self):
        bad = row(total=1, tax=0)
        bad["journeyList"][0]["transSectionList"][0]["departPoint"]["airportCode"] = "HGH"
        data = response(rows=[bad, row(total=1902, tax=1246)])
        # The official summary agrees with the valid route row; the stray row
        # must not replace it merely because its number is lower.
        data["basicInfo"]["lowestPrice"] = {"totalPrice": 1902, "totalTax": 1246}
        result = self.parse(data)
        self.assertEqual(result.quotes[0].price, Decimal("1902"))
        self.assertTrue(any("已排除" in warning for warning in result.warnings))

    def test_all_route_mismatches_fail_instead_of_returning_foreign_price(self):
        with self.assertRaisesRegex(ProviderError, "所有带价航班"):
            self.parse(response(rows=[row(destination="OSA")]))

    def test_earlier_arrival_local_clock_across_time_zones_is_allowed(self):
        value = row()
        segment = value["journeyList"][0]["transSectionList"][0]
        segment["arriveDateTime"] = f"{DAY.isoformat()} 08:00:00"
        self.assertEqual(self.parse(response(rows=[value])).quotes[0].price, Decimal("1902"))

    def test_round_trip_non_economy_and_wrong_passenger_price_contract_fail(self):
        data = response()
        data["itineraryList"][0]["journeyList"].append(copy.deepcopy(data["itineraryList"][0]["journeyList"][0]))
        with self.assertRaisesRegex(ProviderError, "单程"):
            self.parse(data)
        data = response()
        data["itineraryList"][0]["policies"][0]["gradeInfoList"][0]["grade"] = 2
        with self.assertRaisesRegex(ProviderError, "经济舱"):
            self.parse(data)

    def test_empty_success_is_not_a_zero_price(self):
        result = self.parse(response(rows=[]))
        self.assertEqual(result.quotes, [])
        self.assertTrue(result.warnings)

    def test_bad_sse_and_nonfinite_json_are_rejected(self):
        for value in ["<html>challenge</html>", "data:{broken}\n\n", "data:{\"x\":NaN}\n\n",
                      "data:{\"x\":1,\"x\":2}\n\n"]:
            with self.subTest(value=value), self.assertRaises(ProviderError):
                TripComProvider()._parse_response(value, ROUTE, DAY, "https://example.invalid")


class TripRequestTests(unittest.TestCase):
    def test_payload_is_one_adult_one_way_economy_cny_without_credentials(self):
        payload = TripComProvider._payload(ROUTE, DAY)
        criteria = payload["searchCriteria"]
        self.assertEqual((criteria["tripType"], criteria["grade"], criteria["realGrade"]), (1, 1, 1))
        self.assertEqual(criteria["passengerInfoType"],
                         {"adultCount": 1, "childCount": 0, "infantCount": 0})
        self.assertEqual(criteria["journeyInfoTypes"], [{
            "journeyNo": 1, "departDate": DAY.isoformat(), "departCode": "SHA",
            "arriveCode": "TYO", "departAirport": "", "arriveAirport": "",
        }])
        self.assertEqual(payload["head"]["auth"], "")
        self.assertEqual(payload["head"]["ctok"], "")
        self.assertEqual(payload["head"]["Currency"], "CNY")
        self.assertIs(payload["filterType"]["studentsSelectedStatus"], False)

    def test_transport_sends_no_cookie_token_or_authorization(self):
        headers = Message()
        headers.add_header("Content-Type", "text/event-stream; charset=UTF-8")

        class Response:
            url = ENDPOINT
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return b"data:{}\n\n"
            @property
            def headers(self): return headers

        provider = TripComProvider(request_delay=0)
        with patch("urllib.request.urlopen", return_value=Response()) as opened:
            provider._request(provider._payload(ROUTE, DAY))
        request = opened.call_args.args[0]
        lowered = {key.lower(): value for key, value in request.header_items()}
        self.assertNotIn("cookie", lowered)
        self.assertNotIn("authorization", lowered)
        self.assertNotIn("x-api-key", lowered)
        sent = json.loads(request.data)
        self.assertEqual(sent["head"]["auth"], "")

    def test_unsupported_filters_and_bad_codes_do_not_request(self):
        mutations = [
            {"currency": "USD"}, {"stay_nights": 2}, {"nonstop": True},
            {"travel_class": 2}, {"origin": "上海"}, {"destination": "SHA"},
        ]
        for mutation in mutations:
            provider = TripComProvider()
            route = Route(**{**ROUTE.__dict__, **mutation})
            with self.subTest(mutation=mutation), patch.object(provider, "_request") as request, \
                    self.assertRaises(ProviderUnsupported):
                provider.search(route, TODAY)
            request.assert_not_called()

    def test_cancel_and_budget_stop_before_network(self):
        for provider in [TripComProvider(cancelled=lambda: True), TripComProvider(max_requests=0)]:
            with self.subTest(provider=provider), patch("urllib.request.urlopen") as request, \
                    self.assertRaises(ProviderError):
                provider.search(ROUTE, TODAY)
            request.assert_not_called()

    def test_http_access_control_stops_after_first_probe(self):
        route = Route(**{**ROUTE.__dict__, "dates": (DAY, DAY + timedelta(days=1))})
        for status in [401, 403, 429, 430]:
            provider = TripComProvider(request_delay=0)
            error = HTTPError(ENDPOINT, status, "blocked", {}, io.BytesIO())
            with self.subTest(status=status), patch("urllib.request.urlopen", side_effect=error) as request, \
                    self.assertRaisesRegex(ProviderError, "本轮停止"):
                provider.search(route, TODAY)
            self.assertEqual(request.call_count, 1)

    def test_any_first_probe_error_does_not_repeat_for_later_dates(self):
        route = Route(**{**ROUTE.__dict__, "dates": (DAY, DAY + timedelta(days=1))})
        provider = TripComProvider(request_delay=0)
        with patch.object(provider, "_request", side_effect=ProviderError("结构变化")) as request, \
                self.assertRaisesRegex(ProviderError, "结构变化"):
            provider.search(route, TODAY)
        self.assertEqual(request.call_count, 1)

    def test_multiple_dates_overlap_and_never_cross_assign_results(self):
        days = tuple(DAY + timedelta(days=index) for index in range(5))
        route = Route(**{**ROUTE.__dict__, "dates": days})
        provider = TripComProvider(request_delay=0)
        barrier = threading.Barrier(4, timeout=2)
        lock = threading.Lock()
        calls = 0

        def request(payload):
            nonlocal calls
            day = date.fromisoformat(payload["searchCriteria"]["journeyInfoTypes"][0]["departDate"])
            with lock:
                calls += 1
                current = calls
            if current > 1:
                barrier.wait()
            return sse(response(day=day, rows=[row(day=day, total=1900 + day.day, tax=1000)]))

        with patch.object(provider, "_request", side_effect=request):
            result = provider.search(route, TODAY)
        self.assertEqual(calls, 5)
        self.assertEqual([quote.departure_date for quote in result.quotes], list(days))
        self.assertEqual([quote.price for quote in result.quotes],
                         [Decimal(1900 + day.day) for day in days])

    def test_parallel_access_block_cancels_queued_work(self):
        days = tuple(DAY + timedelta(days=index) for index in range(10))
        route = Route(**{**ROUTE.__dict__, "dates": days})
        provider = TripComProvider(request_delay=0.01)
        calls = 0
        lock = threading.Lock()

        def request(payload):
            nonlocal calls
            day = date.fromisoformat(payload["searchCriteria"]["journeyInfoTypes"][0]["departDate"])
            with lock:
                calls += 1
                current = calls
            if current == 1:
                return sse(response(day=day, rows=[row(day=day)]))
            if current == 2:
                raise _AccessBlocked("Trip.com 返回 HTTP 429，本轮停止")
            # Give the blocker time to set the shared cancellation event.
            threading.Event().wait(0.03)
            return sse(response(day=day, rows=[row(day=day)]))

        with patch.object(provider, "_request", side_effect=request), \
                self.assertRaisesRegex(ProviderError, "429"):
            provider.search(route, TODAY)
        self.assertLess(calls, len(days))


if __name__ == "__main__":
    unittest.main()
