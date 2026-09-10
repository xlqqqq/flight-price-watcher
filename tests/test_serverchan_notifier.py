"""No live delivery: transports are mocked, including subprocess checks."""

import io
import json
import os
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from urllib.request import BaseHandler, ProxyHandler, build_opener
from urllib.response import addinfourl

from flightwatch.notifier import NotificationError, _NoRedirect
from flightwatch.serverchan_notifier import ServerChanNotifier, _today, quota_status, validate_sendkey


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status


class ServerChanNotifierTests(unittest.TestCase):
    key = "SCT1234567890privateKEYdonotlog"
    success = {"code": 0, "message": "", "data": {
        "errno": 0, "error": "SUCCESS", "pushid": "851777", "readkey": "private-read-key",
    }}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.opener = Mock()
        self.opener.open.side_effect = lambda *args, **kwargs: Response(json.dumps(self.success).encode())
        factory = patch("flightwatch.serverchan_notifier.build_opener", return_value=self.opener)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.notifier = ServerChanNotifier(self.key, self.directory)

    def safe_error(self, notifier=None):
        try:
            (notifier or self.notifier).send("上海→东京 ¥699", "机票完整详情")
        except NotificationError as error:
            formatted = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            for secret in (self.key, "private-read-key", "unsafe-remote-message", "https://"):
                self.assertNotIn(secret, str(error))
                self.assertNotIn(secret, formatted)
            return str(error)
        self.fail("Expected a sanitized NotificationError")

    def respond(self, value, status=200):
        self.opener.open.side_effect = lambda *args, **kwargs: Response(json.dumps(value).encode(), status)

    def test_success_posts_full_utf8_content_and_acceptance_only(self):
        content = "上海 → 东京\n最低 699 元\n\n|日期|含税价格|\n|09-20|699|\nhttps://example.com/?a=1&b=2"
        self.assertEqual(self.notifier.send("上海→东京 ¥699 09-20", content), "accepted:serverchan:851777")
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, f"https://sctapi.ftqq.com/{self.key}.send")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(parse_qs(request.data.decode()), {
            "title": ["上海→东京 ¥699 09-20"], "desp": [content], "noip": ["1"], "channel": ["9"],
        })
        self.opener.open.assert_called_once()
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 20)
        self.assertEqual(quota_status(self.key, self.directory)["used"], 1)

    def test_constructor_and_status_never_send_or_consume(self):
        status = quota_status(self.key, self.directory)
        self.assertEqual(status, {"today": _today(), "used": 0, "remaining": 5, "limit": 5})
        self.opener.open.assert_not_called()

    def test_https_requires_certificate_and_hostname_verification(self):
        handler = self.factory.call_args.args[1]
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(handler._context.check_hostname)

    def test_constructor_adds_system_ca_without_replacing_default_trust(self):
        context = Mock()
        with patch("flightwatch.serverchan_notifier.ssl.create_default_context", return_value=context), \
             patch("flightwatch.serverchan_notifier.Path.is_file", return_value=True):
            ServerChanNotifier(self.key, self.directory)
        context.load_verify_locations.assert_called_once_with(cafile="/etc/ssl/certs/ca-certificates.crt")
        self.opener.open.assert_not_called()

    def test_bad_trust_store_is_safe_and_never_disables_tls(self):
        with patch("flightwatch.serverchan_notifier.ssl.create_default_context", side_effect=ssl.SSLError(self.key)):
            try:
                ServerChanNotifier(self.key, self.directory)
            except NotificationError as error:
                self.assertNotIn(self.key, "".join(traceback.format_exception(error)))
            else:
                self.fail("Invalid trust store must stop construction")
        self.opener.open.assert_not_called()

    def test_five_calls_max_including_tests_and_restart(self):
        for index in range(5):
            ServerChanNotifier(self.key, self.directory).send("微信测试消息", f"测试 {index}")
        self.assertIn("5 次", self.safe_error(ServerChanNotifier(self.key, self.directory)))
        self.assertEqual(self.opener.open.call_count, 5)
        self.assertEqual(quota_status(self.key, self.directory)["remaining"], 0)

    def test_failed_requests_and_timeouts_consume_budget_without_retry(self):
        failures = [URLError(self.key), TimeoutError(self.key), OSError(self.key),
                    IncompleteRead(self.key.encode(), 500), URLError("unsafe-remote-message")]
        for count, error in enumerate(failures, 1):
            self.opener.open.side_effect = error
            self.assertIn("受理结果未知", self.safe_error())
            self.assertEqual(self.opener.open.call_count, count)
            self.assertEqual(quota_status(self.key, self.directory)["used"], count)
        self.assertIn("5 次", self.safe_error())
        self.assertEqual(self.opener.open.call_count, 5)

    def test_service_rejection_does_not_echo_response_or_return_receipt(self):
        self.respond({"code": 40001, "message": "unsafe-remote-message " + self.key})
        self.assertIn("拒绝", self.safe_error())
        self.assertEqual(quota_status(self.key, self.directory)["used"], 1)

    def test_requires_strict_outer_and_inner_success(self):
        variants = [None, [], {}, {"code": "0"}, {"code": False}, {"code": 0},
                    {"code": 0, "data": []}, {"code": 0, "data": {"errno": False}},
                    {"code": 0, "data": {"errno": "0"}},
                    {"code": 0, "data": {"errno": 1, "error": "SUCCESS", "pushid": "1"}},
                    {"code": 0, "data": {"errno": 0, "error": "FAILURE", "pushid": "1"}}]
        for index, value in enumerate(variants):
            with self.subTest(index=index):
                self.respond(value)
                self.safe_error(ServerChanNotifier(self.key, self.directory / str(index)))

    def test_rejects_invalid_or_secret_pushids(self):
        for index, pushid in enumerate([None, True, 0, -1, 1.2, {}, "", "0", "01", "a\nb", self.key, "9" * 65]):
            with self.subTest(index=index):
                value = {"code": 0, "data": {"errno": 0, "error": "SUCCESS", "pushid": pushid}}
                self.respond(value)
                self.safe_error(ServerChanNotifier(self.key, self.directory / str(index)))

    def test_accepts_positive_integer_pushid(self):
        self.respond({"code": 0, "data": {"errno": 0, "error": "SUCCESS", "pushid": 123}})
        self.assertEqual(self.notifier.send("机票提醒", "完整正文"), "accepted:serverchan:123")

    def test_malformed_or_oversize_response_is_unknown_and_counted(self):
        for index, body in enumerate([self.key.encode(), b"\xff", b" " * 65_537]):
            with self.subTest(index=index):
                self.opener.open.side_effect = lambda *args, **kwargs: Response(body)
                self.safe_error()
        self.assertEqual(quota_status(self.key, self.directory)["used"], 3)

    def test_http_error_hides_key_and_counts_attempt(self):
        self.opener.open.side_effect = HTTPError(
            f"https://sctapi.ftqq.com/{self.key}.send", 500,
            "unsafe-remote-message " + self.key, {}, io.BytesIO(self.key.encode()),
        )
        self.assertIn("HTTP", self.safe_error())
        self.assertEqual(quota_status(self.key, self.directory)["used"], 1)

    def test_non_200_http_even_with_success_body_is_rejected(self):
        self.respond(self.success, status=503)
        self.assertIn("HTTP", self.safe_error())

    def test_malicious_redirect_cannot_forward_credentials(self):
        self.assertIsInstance(self.factory.call_args.args[0], _NoRedirect)
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                requests = []
                key = self.key

                class SimulatedRedirect(BaseHandler):
                    handler_order = 100

                    def https_open(self, request):
                        requests.append(request.full_url)
                        if len(requests) > 1:
                            raise AssertionError("The redirect must never be followed")
                        response = addinfourl(
                            io.BytesIO(), {"location": "https://evil.example/" + key},
                            request.full_url, code,
                        )
                        response.msg = "unsafe-remote-message " + key
                        return response

                # Exercise urllib's real HTTP-error/redirect handler chain with
                # an in-memory HTTPS response; no socket can be opened here.
                self.notifier._opener = build_opener(ProxyHandler({}), _NoRedirect(), SimulatedRedirect())
                self.assertIn("HTTP", self.safe_error())
                self.assertEqual(requests, [f"https://sctapi.ftqq.com/{self.key}.send"])
        self.assertEqual(quota_status(self.key, self.directory)["used"], 5)

    def test_rejects_other_services_and_invalid_keys_without_exposing_input(self):
        for key in (None, "", "SCT", "sctp123tSecret", "https://example.com/", self.key + "/x", self.key + "\nfoo"):
            with self.subTest(kind=type(key).__name__), self.assertRaises(NotificationError):
                ServerChanNotifier(key, self.directory)
        self.assertEqual(validate_sendkey(" \n" + self.key + " \n"), self.key)
        self.opener.open.assert_not_called()

    def test_invalid_timeout_is_rejected_without_network(self):
        for timeout in (True, 0, -1, float("nan"), float("inf"), "20", 10**1000):
            with self.subTest(kind=type(timeout).__name__), self.assertRaises(NotificationError):
                ServerChanNotifier(self.key, self.directory, timeout=timeout)
        self.opener.open.assert_not_called()

    def test_invalid_message_does_not_consume_budget_and_32_character_title_works(self):
        for title, content in [(None, "x"), ("", "x"), ("x" * 33, "x"), ("a\nb", "x"),
                               ("a\rb", "x"), ("x", None), ("x", "  "), ("x", "\ud800")]:
            with self.subTest(kind=type(title).__name__), self.assertRaises(NotificationError):
                self.notifier.send(title, content)
        self.assertEqual(quota_status(self.key, self.directory)["used"], 0)
        self.opener.open.assert_not_called()
        self.notifier.send("中" * 32, "完整正文")

    def test_same_key_normalization_shares_budget_but_different_key_has_own_counter(self):
        self.notifier.send("提醒", "第一次")
        ServerChanNotifier(" " + self.key + " ", self.directory).send("提醒", "第二次")
        other_key = "SCTdifferentKEY12345"
        ServerChanNotifier(other_key, self.directory).send("提醒", "另一账号")
        self.assertEqual(quota_status(self.key, self.directory)["used"], 2)
        self.assertEqual(quota_status(other_key, self.directory)["used"], 1)

    def test_database_contains_hash_only_and_is_private(self):
        self.notifier.send("提醒", "不可存储正文的内容")
        path = self.directory / "serverchan-quota.sqlite3"
        raw = path.read_bytes()
        for secret in (self.key, "private-read-key", "不可存储正文的内容"):
            self.assertNotIn(secret.encode(), raw)
        with sqlite3.connect(path) as conn:
            key_hash, day, used = conn.execute("SELECT key_hash, day, used FROM daily_attempts").fetchone()
        self.assertEqual(len(key_hash), 64)
        self.assertEqual(used, 1)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_shanghai_midnight_boundary_and_next_day_resets(self):
        for moment, expected in [
            (datetime(2026, 9, 9, 15, 59, 59, tzinfo=timezone.utc), "2026-09-09"),
            (datetime(2026, 9, 9, 16, 0, 0, tzinfo=timezone.utc), "2026-09-10"),
        ]:
            with patch("flightwatch.serverchan_notifier.datetime") as clock:
                clock.now.side_effect = lambda zone: moment.astimezone(zone)
                self.assertEqual(_today(), expected)
        with patch("flightwatch.serverchan_notifier._today", return_value="2026-09-09"):
            for _ in range(5):
                self.notifier.send("提醒", "正文")
            self.assertIn("5 次", self.safe_error())
        with patch("flightwatch.serverchan_notifier._today", return_value="2026-09-10"):
            self.assertEqual(quota_status(self.key, self.directory)["remaining"], 5)
            self.notifier.send("提醒", "正文")
            self.assertEqual(quota_status(self.key, self.directory)["used"], 1)

    def test_concurrent_instances_cannot_overrun_quota(self):
        def attempt(index):
            try:
                return ServerChanNotifier(self.key, self.directory).send("并发测试", str(index))
            except NotificationError:
                return None
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(attempt, range(20)))
        self.assertEqual(sum(result is not None for result in results), 5)
        self.assertEqual(self.opener.open.call_count, 5)
        self.assertEqual(quota_status(self.key, self.directory)["used"], 5)

    def test_concurrent_processes_and_restart_share_durable_budget(self):
        script = """
import sys
from flightwatch.serverchan_notifier import _quota
from flightwatch.notifier import NotificationError
for _ in range(5):
    try:
        _quota(sys.argv[1], sys.argv[2], consume=True)
        print('reserved')
    except NotificationError:
        pass
"""
        project = Path(__file__).resolve().parents[1]
        processes = [subprocess.Popen([sys.executable, "-c", script, self.key, str(self.directory)],
                                     cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(4)]
        outputs = [process.communicate(timeout=20) for process in processes]
        self.assertTrue(all(process.returncode == 0 for process in processes), outputs)
        self.assertEqual(sum(stdout.count("reserved") for stdout, _ in outputs), 5)
        self.assertEqual(quota_status(self.key, self.directory)["remaining"], 0)
        self.assertIn("5 次", self.safe_error(ServerChanNotifier(self.key, self.directory)))
        self.opener.open.assert_not_called()

    def test_storage_failure_blocks_network_and_hides_exception_detail(self):
        with patch("flightwatch.serverchan_notifier.sqlite3.connect", side_effect=sqlite3.OperationalError(self.key)):
            self.assertIn("本次没有发送", self.safe_error())
        self.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
