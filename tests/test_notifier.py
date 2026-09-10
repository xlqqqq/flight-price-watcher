"""Mocked transport checks: these tests never send a live message."""

import io
import json
import traceback
import unittest
from http.client import IncompleteRead
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from flightwatch.notifier import NotificationError, PushPlusNotifier, _NoRedirect


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status


class PushPlusNotifierTests(unittest.TestCase):
    token = "private-token-do-not-log-123456"
    receipt = "3cbc5eab19fe512e80677540fbde332a"

    def setUp(self):
        self.opener = Mock()
        self.factory = patch("flightwatch.notifier.build_opener", return_value=self.opener)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.notifier = PushPlusNotifier(self.token)

    def respond(self, value, status=200):
        self.opener.open.return_value = Response(json.dumps(value).encode(), status)

    def assert_safe_error(self):
        try:
            self.notifier.send("低价机票", "上海至东京 699 元")
        except NotificationError as exc:
            formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self.assertNotIn(self.token, str(exc))
            self.assertNotIn(self.token, formatted)
            self.assertNotIn("https://", str(exc))
            self.assertNotIn("unsafe-remote-message", str(exc))
            return str(exc)
        self.fail("Expected a sanitized NotificationError")

    def test_posts_utf8_json_and_returns_accepted_receipt_only(self):
        self.respond({"code": 200, "data": self.receipt})
        receipt = self.notifier.send("国际航班低价", "上海 → 东京\n最低 699 元")
        self.assertEqual(receipt, "accepted:" + self.receipt)
        self.opener.open.assert_called_once()
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.pushplus.plus/send")
        self.assertEqual(request.get_method(), "POST")
        self.assertNotIn(self.token, request.full_url)
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 20)
        self.assertEqual(json.loads(request.data), {
            "token": self.token,
            "title": "国际航班低价",
            "content": "上海 → 东京\n最低 699 元",
            "channel": "wechat",
            "template": "txt",
        })

    def test_rejected_api_response_does_not_echo_remote_message(self):
        self.respond({"code": 600, "msg": "unsafe-remote-message " + self.token})
        self.assertIn("拒绝", self.assert_safe_error())

    def test_rejects_malformed_response_shapes(self):
        for value in (None, [], "ok", {}, {"code": "200"}, {"code": True}):
            with self.subTest(value=value):
                self.respond(value)
                self.assert_safe_error()

    def test_rejects_missing_or_unsafe_receipts(self):
        for receipt in (None, 1, {}, "", "a\nb", "https://example.com", self.token):
            with self.subTest(receipt_type=type(receipt).__name__):
                self.respond({"code": 200, "data": receipt})
                self.assert_safe_error()

    def test_malformed_json_and_invalid_utf8_do_not_leak_body(self):
        for body in (self.token.encode(), b"\xff" + self.token.encode()):
            with self.subTest(body_type="malformed"):
                self.opener.open.return_value = Response(body)
                self.assert_safe_error()

    def test_rejects_excessive_response(self):
        self.opener.open.return_value = Response(b" " * 65_537)
        self.assertIn("响应过大", self.assert_safe_error())

    def test_http_errors_hide_url_reason_and_body(self):
        self.opener.open.side_effect = HTTPError(
            "https://example.com/" + self.token,
            500,
            "unsafe-remote-message " + self.token,
            {},
            io.BytesIO(self.token.encode()),
        )
        self.assert_safe_error()

    def test_network_errors_and_timeouts_have_unknown_outcome_without_retry(self):
        for error in (URLError(self.token), TimeoutError(self.token), OSError(self.token)):
            with self.subTest(error_type=type(error).__name__):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = error
                self.assertIn("受理结果未知", self.assert_safe_error())
                self.opener.open.assert_called_once()

    def test_non_200_http_status_is_rejected_even_with_accepted_body(self):
        self.respond({"code": 200, "data": self.receipt}, status=503)
        self.assert_safe_error()

    def test_truncated_http_response_is_sanitized(self):
        response = Mock()
        response.status = 200
        response.read.side_effect = IncompleteRead(self.token.encode(), 500)
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        self.opener.open.return_value = context
        self.assertIn("受理结果未知", self.assert_safe_error())

    def test_empty_token_and_invalid_timeouts_do_not_send(self):
        for token in (None, "", "   "):
            with self.subTest(token=token), self.assertRaises(NotificationError):
                PushPlusNotifier(token)
        for timeout in (True, 0, -1, float("nan"), float("inf"), "20", 10**1000):
            with self.subTest(timeout_type=type(timeout).__name__), self.assertRaises(NotificationError):
                PushPlusNotifier(self.token, timeout=timeout)
        self.opener.open.assert_not_called()

    def test_invalid_message_does_not_send(self):
        for title, content in ((None, "valid"), ("valid", ""), ("valid", "  "), ("valid", None)):
            with self.subTest(title=title), self.assertRaises(NotificationError):
                self.notifier.send(title, content)
        self.opener.open.assert_not_called()

    def test_redirects_are_blocked_before_credentials_can_be_forwarded(self):
        handler = _NoRedirect()
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.assertIsNone(handler.redirect_request(None, None, status, "", {}, "http://example.com"))


if __name__ == "__main__":
    unittest.main()
