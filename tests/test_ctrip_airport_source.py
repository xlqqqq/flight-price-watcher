import copy
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch

from flightwatch.ctrip_airport_source import CtripAirportProvider
from flightwatch.models import ProviderError, Route
from flightwatch.public_source import CtripCalendarProvider


TODAY = date(2026, 9, 14)
DAY = date(2026, 9, 21)
ROUTE = Route("pvg-cju", "浦东到济州", "PVG", "CJU", "ctrip", dates=(DAY,),
              market="international", origin_scope="airport", destination_scope="airport",
              origin_city_code="SHA", destination_city_code="CJU")
FIXTURES = Path(__file__).parent / "fixtures"


def html(data):
    return "<script>window.__INITIAL_STATE__=" + json.dumps(data) + ";</script>"


class CtripAirportTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((FIXTURES / "ctrip_airport_international.json").read_text())
        self.domestic = json.loads((FIXTURES / "ctrip_airport_domestic.json").read_text())
        self.provider = CtripAirportProvider(request_delay=0)

    def test_live_international_fixture_has_exact_airports_tax_and_flight_booking_link(self):
        with patch.object(self.provider, "_request", return_value=html(self.data)) as request:
            result = self.provider.search(ROUTE, TODAY)
        request.assert_called_once_with("https://m.ctrip.com/html5/flight/sha-cju-day-7.html")
        self.assertEqual(len(result.quotes), 2)
        q = result.quotes[0]
        self.assertEqual((q.price, q.origin_airport, q.destination_airport),
                         (Decimal(280), "PVG", "CJU"))
        self.assertTrue(q.comparable)
        self.assertIn("dfltno=9C8573", q.url)
        self.assertIn("type=1-0-0", q.url)
        self.assertEqual(result.quotes[1].flight_number, "7C8352 / LJ569")

    def test_city_and_specific_airport_mix_and_sha_collision(self):
        for route, expected in [
            (replace(ROUTE, origin="SHA", origin_scope="city"), 2),
            (replace(ROUTE, destination_scope="city"), 2),
            (replace(ROUTE, origin="SHA"), 0),
            (replace(ROUTE, destination="ICN"), 0),
        ]:
            with self.subTest(route=route):
                result = self.provider._parse(html(self.data), route, DAY)
                self.assertEqual(len(result.quotes), expected)

    def test_exact_airport_filter_does_not_publish_cheaper_other_airport(self):
        self.data["listData"]["flights"][0]["flightItem"]["flights"][0]["dport"]["code"] = "SHA"
        result = self.provider._parse(html(self.data), ROUTE, DAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertEqual(result.quotes[0].price, Decimal(817))

    def test_wrong_echo_date_city_triptype_and_missing_data_rejected(self):
        for key, value in [("ddate", "2026-09-20"), ("dcode", "BJS"),
                           ("acode", "TYO"), ("triptype", 2), ("triptype", True)]:
            with self.subTest(key=key):
                data = copy.deepcopy(self.data)
                data["listData"][key] = value
                with self.assertRaises(ProviderError):
                    self.provider._parse(html(data), ROUTE, DAY)
        with self.assertRaises(ProviderError):
            self.provider._parse("<html>verification</html>", ROUTE, DAY)

    def test_price_passenger_date_and_booking_identity_are_validated(self):
        changes = [
            ("price", True), ("price", "NaN"), ("currency", "USD"),
            ("departDate", "2026-09-20"), ("returnDate", "2026-09-25"),
            ("className", "商务舱"), ("specialChannelPrice", True),
        ]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                data = copy.deepcopy(self.data)
                data["listData"]["flights"] = data["listData"]["flights"][:1]
                data["listData"]["flights"][0]["flightItem"]["pl"][0][key] = value
                self.assertEqual(self.provider._parse(html(data), ROUTE, DAY).quotes, [])
        for before, after in [("type=1-0-0", "type=2-0-0"),
                              ("triptype=1", "triptype=2"),
                              ("dfltno=9C8573", "dfltno=MU1234"),
                              ("m.ctrip.com", "m.ctrip.com.evil.example"),
                              ("ddate=2026-09-21", "ddate=2026-09-20")]:
            with self.subTest(after=after):
                data = copy.deepcopy(self.data)
                data["listData"]["flights"] = data["listData"]["flights"][:1]
                policy = data["listData"]["flights"][0]["flightItem"]["pl"][0]
                policy["jumpUrl"] = policy["jumpUrl"].replace(before, after)
                self.assertEqual(self.provider._parse(html(data), ROUTE, DAY).quotes, [])

    def test_unknown_tax_never_enters_total_price_or_notifications(self):
        self.data["listData"]["flights"][0]["flightItem"]["pl"][0]["isContainsTax"] = False
        result = self.provider._parse(html(self.data), ROUTE, DAY)
        self.assertFalse(result.quotes[0].comparable)
        self.assertIn("不触发", result.warnings[0])

    def test_domestic_endpoints_are_filtered_without_inventing_tax(self):
        route = replace(ROUTE, origin="SHA", destination="PEK", origin_city_code="SHA",
                        destination_city_code="BJS", market="domestic")
        result = self.provider._parse(html(self.domestic), route, DAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertEqual(result.quotes[0].origin_airport, "SHA")
        self.assertEqual(result.quotes[0].destination_airport, "PEK")
        self.assertFalse(result.quotes[0].comparable)

    def test_first_failure_does_not_launch_remaining_month(self):
        route = replace(ROUTE, dates=(DAY, date(2026, 9, 22)))
        with patch.object(self.provider, "_request", side_effect=ProviderError("HTTP 432")) as request:
            with self.assertRaisesRegex(ProviderError, "432"):
                self.provider.search(route, TODAY)
        self.assertEqual(request.call_count, 1)

    def test_later_date_failure_retains_valid_first_date(self):
        route = replace(ROUTE, dates=(DAY, date(2026, 9, 22)))
        with patch.object(self.provider, "_request", side_effect=[html(self.data), ProviderError("网络失败")]):
            result = self.provider.search(route, TODAY)
        self.assertEqual(len(result.quotes), 2)
        self.assertIn("2026-09-22", " ".join(result.warnings))

    def test_public_provider_dispatches_airport_queries_without_calendar(self):
        provider = CtripCalendarProvider(request_delay=0)
        with patch.object(CtripAirportProvider, "_request", return_value=html(self.data)), \
                patch.object(provider, "_request") as calendar:
            result = provider.search(ROUTE, TODAY)
        calendar.assert_not_called()
        self.assertEqual(len(result.quotes), 2)

    def test_http2_transport_bounded_and_access_block_stops_request_queue(self):
        response = subprocess.CompletedProcess([], 0, b"access denied\n432", b"")
        with patch("shutil.which", return_value="/usr/bin/curl"), \
                patch("subprocess.run", return_value=response) as run:
            with self.assertRaisesRegex(ProviderError, "432"):
                self.provider._request("https://m.ctrip.com/html5/flight/sha-cju-day-7.html")
            with self.assertRaisesRegex(ProviderError, "停止"):
                self.provider._request("https://m.ctrip.com/html5/flight/sha-cju-day-8.html")
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertIn("--max-filesize", command)
        self.assertIn("--max-time", command)
        self.assertNotIn("--location", command)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_cancelled_calendar_or_airport_query_never_starts_network(self):
        for route in [ROUTE, replace(ROUTE, origin="SHA", origin_scope="city", destination_scope="city")]:
            provider = CtripCalendarProvider(cancelled=lambda: True)
            with patch("subprocess.run") as process, patch("urllib.request.urlopen") as network:
                with self.assertRaisesRegex(ProviderError, "取消"):
                    provider.search(route, TODAY)
            process.assert_not_called()
            network.assert_not_called()

    def test_cancellation_after_first_date_prevents_remaining_date_jobs(self):
        stopped = threading.Event()
        provider = CtripAirportProvider(cancelled=stopped.is_set)
        route = replace(ROUTE, dates=(DAY, date(2026, 9, 22), date(2026, 9, 23)))

        def first(_url):
            stopped.set()
            return html(self.data)

        with patch.object(provider, "_request", side_effect=first) as request:
            with self.assertRaisesRegex(ProviderError, "取消"):
                provider.search(route, TODAY)
        self.assertEqual(request.call_count, 1)

    def test_request_budget_is_shared_by_the_calendar_wrapper(self):
        provider = CtripCalendarProvider(request_delay=0, max_requests=1)
        response = subprocess.CompletedProcess([], 0, html(self.data).encode() + b"\n200", b"")
        with patch("shutil.which", return_value="/usr/bin/curl"), \
                patch("subprocess.run", return_value=response) as run:
            self.assertEqual(len(provider.search(ROUTE, TODAY).quotes), 2)
            with self.assertRaisesRegex(ProviderError, "请求上限"):
                provider.search(ROUTE, TODAY)
        self.assertEqual(provider.requests_used, 1)
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
