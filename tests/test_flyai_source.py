"""FlyAI parser uses a reduced real 2026-09-14 official demo response.

All transport tests are local mocks/subprocesses; no demo quota or WeChat sends.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from flightwatch import flyai_source as source
from flightwatch.fliggy_source import FliggyProvider
from flightwatch.models import ProviderError, ProviderUnsupported, Route


DAY = date(2026, 9, 21)
ROUTE = Route("pvg-cju", "浦东济州", "PVG", "CJU", "multi", dates=(DAY,),
              market="international", origin_scope="airport", destination_scope="airport",
              origin_city_code="SHA", destination_city_code="CJU",
              origin_label="上海 · 浦东国际机场（PVG）", destination_label="济州岛 · 济州国际机场（CJU）")
FIXTURE = Path(__file__).parent / "fixtures" / "flyai_airport_reference.json"


def payload():
    return json.loads(FIXTURE.read_text())


class FlyAIParserTests(unittest.TestCase):
    def test_real_reference_airports_flights_and_tax_uncertainty(self):
        result = source.parse_flyai(payload(), ROUTE, DAY)
        self.assertEqual(len(result.quotes), 2)
        quote = result.quotes[0]
        self.assertEqual(quote.price, Decimal("390.00"))
        self.assertEqual(quote.flight_number, "9C8573")
        self.assertEqual((quote.origin_airport, quote.destination_airport), ("PVG", "CJU"))
        self.assertEqual(quote.price_basis, "unknown")
        self.assertFalse(quote.comparable)
        self.assertEqual(quote.provider, "fliggy")
        self.assertTrue(any("体验" in warning for warning in result.warnings))
        self.assertTrue(quote.url.startswith("https://router.feizhu.com/multi/webview?url="))

    def test_wrong_specific_airport_is_filtered_even_if_cheaper(self):
        data = payload()
        wrong = deepcopy(data["data"]["itemList"][0])
        wrong["ticketPrice"] = "1.00"
        wrong["journeys"][0]["segments"][0]["depStationCode"] = "SHA"
        data["data"]["itemList"].insert(0, wrong)
        result = source.parse_flyai(data, ROUTE, DAY)
        self.assertEqual(result.quotes[0].price, Decimal("390"))
        self.assertTrue(any("排除 1" in warning for warning in result.warnings))
        city = replace(ROUTE, origin="SHA", origin_scope="city")
        self.assertEqual(source.parse_flyai(data, city, DAY).quotes[0].origin_airport, "SHA")

    def test_wrong_date_or_city_rejects_entire_stale_result(self):
        for field, value in [("depDateTime", "2026-09-22 20:05:00"), ("depCityCode", "BJS"),
                             ("arrCityCode", "SEL"), ("depStationCode", ""), ("arrDateTime", None)]:
            with self.subTest(field=field):
                data = payload()
                data["data"]["itemList"][0]["journeys"][0]["segments"][0][field] = value
                with self.assertRaises(ProviderError):
                    source.parse_flyai(data, ROUTE, DAY)

    def test_connection_continuity_and_direct_filter(self):
        result = source.parse_flyai(payload(), replace(ROUTE, nonstop=True), DAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertEqual(result.quotes[0].stops, 0)
        data = payload()
        data["data"]["itemList"][1]["journeys"][0]["segments"][1]["depStationCode"] = "ICN"
        self.assertEqual(len(source.parse_flyai(data, ROUTE, DAY).quotes), 1)

    def test_every_segment_must_be_economy_aircraft(self):
        for field, value in [("transportType", "火车"), ("seatClassName", "公务舱")]:
            with self.subTest(field=field):
                data = payload()
                data["data"]["itemList"][1]["journeys"][0]["segments"][1][field] = value
                self.assertEqual(len(source.parse_flyai(data, ROUTE, DAY).quotes), 1)

    def test_reject_return_journey(self):
        data = payload()
        data["data"]["itemList"][0]["journeys"] *= 2
        with self.assertRaises(ProviderError):
            source.parse_flyai(data, ROUTE, DAY)

    def test_missing_or_invalid_ticket_price_is_not_invented_from_other_fields(self):
        for value in [None, 390, True, "NaN", "Infinity", "-1", "0", "390.001", "1000001"]:
            with self.subTest(value=value):
                data = payload()
                data["data"]["itemList"][0]["ticketPrice"] = value
                data["data"]["itemList"][0]["adultPrice"] = "¥390.00"
                with self.assertRaises(ProviderError):
                    source.parse_flyai(data, ROUTE, DAY)

    def test_unexpected_currency_rejected(self):
        data = payload()
        data["data"]["itemList"][0]["currency"] = "USD"
        with self.assertRaises(ProviderError):
            source.parse_flyai(data, ROUTE, DAY)

    def test_error_payload_never_echoes_sensitive_service_text(self):
        with self.assertRaises(ProviderError) as raised:
            source.parse_flyai({"status": 429, "message": "fake-secret-key"}, ROUTE, DAY)
        self.assertNotIn("fake-secret-key", str(raised.exception))

    def test_success_status_is_strict_and_empty_results_are_not_no_flights(self):
        for status in [False, "0", None]:
            with self.subTest(status=status), self.assertRaises(ProviderError):
                source.parse_flyai({"status": status, "data": {"itemList": []}}, ROUTE, DAY)
        result = source.parse_flyai({"status": 0, "data": {"itemList": []}}, ROUTE, DAY)
        self.assertFalse(result.quotes)
        self.assertTrue(any("不代表" in warning for warning in result.warnings))

    def test_official_outer_domain_does_not_allow_malicious_redirect(self):
        for url in ["http://router.feizhu.com/ws/abc", "https://router.feizhu.com.evil.test/ws/abc",
                    "https://evil.test@router.feizhu.com/ws/abc", "https://router.feizhu.com:444/ws/abc",
                    "https://router.feizhu.com/multi/webview?url=https%3A%2F%2Fevil.test%2F",
                    "https://router.feizhu.com/multi/webview?url=https%3A%2F%2Frouter.feizhu.com%2Fws%2Fa&url=https%3A%2F%2Fevil.test",
                    "https://router.feizhu.com/ws/abc?url=https://evil.test", "https://router.feizhu.com/admin",
                    "https://router.feizhu.com/multi/webview?url=https%3A%2F%2Frouter.feizhu.com%2Fws%2Fa&url=",
                    "https://router.feizhu.com/multi/webview?url=https%3A%2F%2Frouter.feizhu.com%2Fws%2Fa%0a"]:
            with self.subTest(url=url), self.assertRaises(ProviderError):
                source._booking_url(url)


class FlyAIRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.parent = FliggyProvider(timeout=2, request_delay=0, max_requests=31)

    def test_bundle_path_is_one_argument_and_key_never_becomes_an_argument(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / "my bundle.cjs"
            bundle.write_text("")
            with patch.dict(os.environ, {"FLIGHTWATCH_FLYAI_CLI": str(bundle), "FLYAI_API_KEY": "test-secret"}), \
                    patch.object(source.shutil, "which", return_value="/usr/bin/node"):
                self.assertEqual(source.cli_command(), ["/usr/bin/node", str(bundle)])
                self.assertNotIn("test-secret", str(source.cli_command()))

    def test_missing_explicit_cli_does_not_silently_use_another_installation(self):
        with patch.dict(os.environ, {"FLIGHTWATCH_FLYAI_CLI": "/nonexistent/flyai.cjs"}), \
                patch.object(source.shutil, "which", return_value=None):
            self.assertFalse(source.available())

    def test_explicit_node_absolute_executable_works_without_service_path(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / "bundle.cjs"
            bundle.write_text("")
            node = Path(folder) / "node runtime"
            node.write_text("#!/bin/sh\n")
            node.chmod(0o700)
            with patch.dict(os.environ, {"FLIGHTWATCH_FLYAI_CLI": str(bundle),
                                         "FLIGHTWATCH_FLYAI_NODE": str(node)}), \
                    patch.object(source.shutil, "which", return_value=None) as find:
                self.assertEqual(source.cli_command(), [str(node), str(bundle)])
            find.assert_not_called()

    def test_invalid_explicit_node_does_not_silently_use_path_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / "bundle.cjs"
            bundle.write_text("")
            for runtime in ["node", "/nonexistent/node", str(bundle), folder]:
                with self.subTest(runtime=runtime), patch.dict(os.environ, {
                        "FLIGHTWATCH_FLYAI_CLI": str(bundle), "FLIGHTWATCH_FLYAI_NODE": runtime}), \
                        patch.object(source.shutil, "which", return_value="/other/node"):
                    self.assertFalse(source.available())

    def test_cli_args_keep_airport_selection_and_exact_single_day(self):
        with patch.object(source, "cli_command", return_value=["flyai"]), \
                patch.object(source, "_run_cli", return_value=payload()) as run, \
                patch("flightwatch.city_search.cached_place", return_value=None):
            result = source.search_flyai(self.parent, replace(ROUTE, nonstop=True), [DAY])
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--origin")+1].replace(" ", ""), "上海浦东国际机场")
        self.assertEqual(args[args.index("--dep-date")+1], str(DAY))
        self.assertEqual(args[args.index("--sort-type")+1], "3")
        self.assertEqual(args[args.index("--journey-type")+1], "1")
        self.assertEqual(self.parent.requests_used, 1)
        self.assertFalse(result.quotes[0].comparable)

    def test_first_day_quota_error_never_requests_other_days(self):
        with patch.object(source, "cli_command", return_value=["flyai"]), \
                patch.object(source, "_run_cli", return_value={"status": 429}) as run:
            with self.assertRaises(ProviderError):
                source.search_flyai(self.parent, ROUTE, [DAY+timedelta(days=n) for n in range(15)])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.parent.requests_used, 1)

    def test_later_failure_stops_at_most_two_inflight_and_keeps_first_result(self):
        def run(command, parent, stop):
            day = command[-1]
            if day == str(DAY):
                return payload()
            return {"status": 429}
        with patch.object(source, "cli_command", return_value=["flyai"]), \
                patch.object(source, "_run_cli", side_effect=run) as transport:
            result = source.search_flyai(self.parent, ROUTE, [DAY+timedelta(days=n) for n in range(15)])
        self.assertLessEqual(transport.call_count, 3)
        self.assertEqual(len(result.quotes), 2)
        self.assertTrue(any("保留" in warning for warning in result.warnings))

    def test_bounded_parallelism_and_each_date_requested_once(self):
        active = maximum = 0
        lock = threading.Lock()
        seen = []

        def run(command, parent, stop):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                seen.append(command[-1])
            time.sleep(0.03)
            data = payload()
            for row in data["data"]["itemList"]:
                for segment in row["journeys"][0]["segments"]:
                    for key in ("depDateTime", "arrDateTime"):
                        segment[key] = segment[key].replace(str(DAY), command[-1])
            with lock:
                active -= 1
            return data

        days = [DAY+timedelta(days=n) for n in range(5)]
        with patch.object(source, "cli_command", return_value=["flyai"]), \
                patch.object(source, "_run_cli", side_effect=run):
            result = source.search_flyai(self.parent, ROUTE, days)
        self.assertEqual(maximum, 2)
        self.assertEqual(sorted(seen), [str(day) for day in days])
        self.assertEqual(self.parent.requests_used, 5)
        self.assertEqual(len(result.quotes), 10)

    def test_unsupported_roundtrip_does_not_start_cli(self):
        with patch.object(source, "_run_cli") as run, self.assertRaises(ProviderUnsupported):
            source.search_flyai(self.parent, replace(ROUTE, stay_nights=3), [DAY])
        run.assert_not_called()

    def test_local_subprocess_output_success(self):
        with patch.object(source, "_cli_environment", return_value=os.environ.copy()):
            result = source._run_cli([sys.executable, "-c", "print('{\"status\": 0}')"], self.parent, threading.Event())
        self.assertEqual(result, {"status": 0})

    def test_cancel_terminates_running_local_subprocess(self):
        event = threading.Event()
        self.parent.cancelled = event.is_set
        real_popen = subprocess.Popen
        processes = []

        def start(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            event.set()
            return process

        with patch.object(source.subprocess, "Popen", side_effect=start), \
                patch.object(source, "_cli_environment", return_value=os.environ.copy()):
            with self.assertRaises(ProviderError):
                source._run_cli([sys.executable, "-c", "import time; time.sleep(30)"], self.parent, threading.Event())
        self.assertIsNotNone(processes[0].poll())

    def test_local_timeout_terminates_subprocess_without_stderr_leak(self):
        self.parent.timeout = 0.01
        started = time.monotonic()
        with patch.object(source, "_cli_environment", return_value=os.environ.copy()), self.assertRaises(ProviderError) as raised:
            source._run_cli([sys.executable, "-c", "import time,sys; print('fake-secret',file=sys.stderr); time.sleep(30)"],
                            self.parent, threading.Event())
        self.assertLess(time.monotonic()-started, 3)
        self.assertNotIn("fake-secret", str(raised.exception))

    def test_dotenv_reads_only_flyai_key_without_parent_mutation(self):
        body = b'OTHER_SECRET=must-not-import\nFLYAI_API_KEY="test-dotenv-key"\n'
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, "open", return_value=io.BytesIO(body)):
            env = source._cli_environment()
            self.assertEqual(env["FLYAI_API_KEY"], "test-dotenv-key")
            self.assertNotIn("OTHER_SECRET", env)
            self.assertNotIn("FLYAI_API_KEY", os.environ)

    def test_process_key_wins_and_does_not_read_dotenv(self):
        with patch.dict(os.environ, {"FLYAI_API_KEY": "process-key"}), patch.object(Path, "open") as read:
            self.assertEqual(source._cli_environment()["FLYAI_API_KEY"], "process-key")
        read.assert_not_called()

    def test_debug_endpoint_overrides_never_reach_cli(self):
        with patch.dict(os.environ, {"FLYAI_API_KEY": "process-key", "DEBUG_FLYAI_MCP_URL": "https://evil.test",
                                     "DEBUG_FLYAI_API_KEY": "debug-key"}):
            env = source._cli_environment()
        self.assertNotIn("DEBUG_FLYAI_MCP_URL", env)
        self.assertNotIn("DEBUG_FLYAI_API_KEY", env)
        self.assertEqual(env["FLYAI_API_KEY"], "process-key")
        self.assertEqual(env["NODE_USE_ENV_PROXY"], "1")

    def test_dotenv_errors_never_echo_key_and_size_is_bounded(self):
        for body in [b'FLYAI_API_KEY="fake-secret', b'x'*16385]:
            with self.subTest(size=len(body)), patch.dict(os.environ, {}, clear=True), \
                    patch.object(Path, "open", return_value=io.BytesIO(body)), self.assertRaises(ProviderError) as raised:
                source._cli_environment()
            self.assertNotIn("fake-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
