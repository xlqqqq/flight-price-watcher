import base64
import json
import ssl
import struct
import unittest
from email.message import Message
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode

from flightwatch.serverchan_binding import ServerChanBinding, _CHECK, _SIGNIN


TOKEN = "anonymous_session_token_0123456789"
TICKET = "temporary_qr_ticket+value="
SENDKEY = "SCT1234567890abcdefTESTONLY"
QR_URL = "https://mp.weixin.qq.com/cgi-bin/showqrcode?" + urlencode({"ticket": TICKET})
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 100, 100) + b"\x08\x02\x00\x00\x00TEST"


def signin(**changes):
    data = {
        "ticket": TICKET, "token": TOKEN, "sid": "1234567", "expire_seconds": 600,
        "url": "https://weixin.qq.com/q/abcdefghijklmnop", "qr_url": QR_URL,
    }
    data.update(changes)
    return {"code": 0, "data": data}


class Response:
    def __init__(self, value, *, content_type="application/json", url=None, status=200):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.url = url
        self.status = status

    def geturl(self):
        return self.url

    def read(self, size):
        return self.raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class ServerChanBindingTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        clock = patch("flightwatch.serverchan_binding.time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.responses = []
        self.opener = Mock()
        self.opener.open.side_effect = self.open
        factory = patch("flightwatch.serverchan_binding.build_opener", return_value=self.opener)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.binding = ServerChanBinding()

    def open(self, request, **kwargs):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if response.url is None:
            response.url = request.full_url
        return response

    def start(self, **changes):
        self.responses.extend([Response(signin(**changes)), Response(PNG, content_type="image/png")])
        return self.binding.start()

    def confirm(self, result):
        self.responses.append(Response({"code": 0, "data": {"result": result}}))
        return self.binding.confirm()

    def assert_safe(self):
        public = json.dumps(self.binding.status())
        for secret in (TOKEN, TICKET, SENDKEY):
            self.assertNotIn(secret, public)
        self.assertNotIn("sendkey", self.binding.status())
        self.assertNotIn("token", self.binding.status())

    def test_initial_status_and_confirm_are_offline(self):
        self.assertEqual(self.binding.status()["state"], "idle")
        self.assertIsNone(self.binding.confirm())
        self.opener.open.assert_not_called()

    def test_start_exposes_image_but_no_authorization_session(self):
        state = self.start()
        self.assertEqual(state["state"], "waiting")
        self.assertEqual(state["expires_in"], 600)
        self.assertEqual(state["qr_image"], "data:image/png;base64," + base64.b64encode(PNG).decode())
        calls = self.opener.open.call_args_list
        self.assertEqual([call.args[0].full_url for call in calls], [_SIGNIN, QR_URL])
        self.assertEqual([call.args[0].method for call in calls], ["GET", "GET"])
        self.assert_safe()

    def test_official_http_scan_target_is_validated_but_never_fetched(self):
        result = self.start(url="http://weixin.qq.com/q/abcdefghijklmnop")
        self.assertEqual(result["state"], "waiting")
        requests = [call.args[0] for call in self.opener.open.call_args_list]
        self.assertEqual([request.full_url for request in requests], [_SIGNIN, QR_URL])
        self.assertTrue(all(request.full_url.startswith("https://") for request in requests))
        self.assert_safe()

    def test_unscanned_result_is_one_manual_check_and_preserves_session(self):
        self.start()
        for result in (None, False, {"level": 0}, {"level": "0"}):
            self.assertIsNone(self.confirm(result))
            self.assertEqual(self.binding.status()["state"], "waiting")
        self.assertEqual(self.opener.open.call_count, 6)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, _CHECK)
        self.assertEqual(request.method, "POST")
        self.assertEqual(parse_qs(request.data.decode()), {"token": [TOKEN]})
        self.assertIsNone(request.get_header("Authorization"))
        self.assert_safe()

    def test_completed_authorization_returns_only_valid_sendkey_and_drops_session(self):
        self.start()
        self.assertEqual(self.confirm({"level": 1, "first_guide_done": 1, "sendkey": SENDKEY}), SENDKEY)
        self.assertEqual(self.binding.status()["state"], "bound")
        self.assertIsNone(self.binding.status()["qr_image"])
        self.assertIsNone(self.binding._token)
        self.assertIsNone(self.binding.confirm())
        self.assertEqual(self.opener.open.call_count, 3)
        self.assert_safe()

    def test_canonical_string_level_is_supported(self):
        self.start()
        self.assertEqual(self.confirm({"level": "1", "first_guide_done": "1", "sendkey": SENDKEY}), SENDKEY)

    def test_incomplete_first_setup_does_not_bind_or_keep_session(self):
        for result in ({"level": 1}, {"level": 1, "sendkey": "bad"},
                       {"level": 1, "first_guide_done": 0, "sendkey": SENDKEY}):
            with self.subTest(result=result):
                self.start()
                self.assertIsNone(self.confirm(result))
                self.assertEqual(self.binding.status()["state"], "error")
                self.assertIn("首次开通", self.binding.status()["message"])
                self.assertIsNone(self.binding._token)
                self.assert_safe()

    def test_invalid_authorization_levels_never_bind(self):
        for level in (True, 1.2, "yes", {}, [], -1, "1e2", "1.0", None):
            with self.subTest(level=level):
                self.start()
                self.assertIsNone(self.confirm({"level": level, "sendkey": SENDKEY}))
                self.assertEqual(self.binding.status()["state"], "error")
                self.assert_safe()

    def test_missing_login_result_is_not_reported_as_waiting(self):
        self.start()
        self.responses.append(Response({"code": 0, "data": {}}))
        self.assertIsNone(self.binding.confirm())
        self.assertEqual(self.binding.status()["state"], "error")
        self.assertIsNone(self.binding._token)

    def test_expired_session_never_contacts_check(self):
        self.start(expire_seconds=20)
        self.now += 20
        self.assertEqual(self.binding.status()["state"], "expired")
        self.assertIsNone(self.binding.confirm())
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertIsNone(self.binding._token)
        self.assertIsNone(self.binding.status()["qr_image"])

    def test_authorization_response_arriving_after_expiration_is_discarded(self):
        self.start(expire_seconds=1)
        original = self.opener.open.side_effect

        def delayed(request, **kwargs):
            self.now += 2
            return original(request, **kwargs)

        self.opener.open.side_effect = delayed
        self.assertIsNone(self.confirm({"level": 1, "sendkey": SENDKEY}))
        self.assertEqual(self.binding.status()["state"], "expired")
        self.assertIsNone(self.binding._token)

    def test_duration_is_capped_at_ten_minutes(self):
        self.assertEqual(self.start(expire_seconds=8000)["expires_in"], 600)

    def test_invalid_expiry_rejects_before_image_fetch(self):
        for duration in (0, -1, True, "600", 1.2, 10**100):
            with self.subTest(duration=duration):
                self.responses.append(Response(signin(expire_seconds=duration)))
                before = self.opener.open.call_count
                self.assertEqual(self.binding.start()["state"], "error")
                self.assertEqual(self.opener.open.call_count, before + 1)

    def test_cancel_clears_image_and_session_without_network(self):
        self.start()
        self.binding.cancel()
        self.assertEqual(self.binding.status()["state"], "idle")
        self.assertIsNone(self.binding._token)
        self.assertIsNone(self.binding.confirm())
        self.assertEqual(self.opener.open.call_count, 2)

    def test_new_start_drops_old_session_even_when_new_request_fails(self):
        self.start()
        self.responses.append(URLError(TOKEN))
        self.assertEqual(self.binding.start()["state"], "error")
        self.assertIsNone(self.binding._token)
        self.assert_safe()

    def test_network_errors_do_not_expose_response_or_request_secrets(self):
        for error in (URLError(TOKEN), OSError(TOKEN),
                      HTTPError(_CHECK + "?token=" + TOKEN, 403, SENDKEY, {}, None)):
            with self.subTest(error_type=type(error).__name__):
                self.start()
                self.responses.append(error)
                self.assertIsNone(self.binding.confirm())
                self.assertEqual(self.binding.status()["state"], "error")
                self.assert_safe()

    def test_invalid_json_and_business_errors_have_safe_messages(self):
        for value in (b"not json", {"code": True, "data": {}}, {"code": 403, "message": TOKEN},
                      {"code": 0, "data": SENDKEY}, [], b" " * 65537):
            with self.subTest(value_type=type(value).__name__):
                self.responses.append(Response(value))
                self.assertEqual(self.binding.start()["state"], "error")
                self.assert_safe()

    def test_untrusted_qr_urls_never_trigger_image_download(self):
        for url in (
            "http://mp.weixin.qq.com/cgi-bin/showqrcode?" + urlencode({"ticket": TICKET}),
            "https://mp.weixin.qq.com.evil.test/cgi-bin/showqrcode?ticket=x",
            "https://evil.test@mp.weixin.qq.com/cgi-bin/showqrcode?ticket=x",
            "https://mp.weixin.qq.com:444/cgi-bin/showqrcode?ticket=x",
            "https://mp.weixin.qq.com/cgi-bin/other?ticket=x",
            QR_URL + "&token=" + TOKEN,
            QR_URL + "&ticket=x", QR_URL + "#fragment", QR_URL.replace("ticket=", "other="),
            "https://127.0.0.1/cgi-bin/showqrcode?ticket=x",
        ):
            with self.subTest(url=url):
                self.responses.append(Response(signin(qr_url=url)))
                before = self.opener.open.call_count
                self.assertEqual(self.binding.start()["state"], "error")
                self.assertEqual(self.opener.open.call_count, before + 1)
                self.assert_safe()

    def test_qr_ticket_must_match_and_target_must_be_official(self):
        for changes in ({"ticket": "different"}, {"url": "https://evil.test/q/abc"},
                        {"url": "https://weixin.qq.com/q/abc?other=1"}, {"sid": []},
                        {"token": "insecure token"}, {"ticket": "x\nvalue"}):
            with self.subTest(field=list(changes)):
                self.responses.append(Response(signin(**changes)))
                self.assertEqual(self.binding.start()["state"], "error")

    def test_image_must_be_small_png_or_jpeg_not_html_or_svg(self):
        for raw, media_type in ((b"<svg/>", "image/svg+xml"), (b"<html/>", "text/html"),
                                (PNG, "text/html"), (b"not_png", "image/png"),
                                (b"x" * 524289, "image/png"),
                                (PNG[:16] + struct.pack(">II", 99999, 10) + PNG[24:], "image/png")):
            with self.subTest(media_type=media_type, size=len(raw)):
                self.responses.extend([Response(signin()), Response(raw, content_type=media_type)])
                self.assertEqual(self.binding.start()["state"], "error")
                self.assertIsNone(self.binding._token)

    def test_official_jpg_alias_becomes_standard_jpeg_data_url(self):
        jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"test-image-bytes" + b"\xff\xd9"
        self.responses.extend([Response(signin()), Response(jpeg, content_type="image/jpg")])
        state = self.binding.start()
        self.assertEqual(state["state"], "waiting")
        self.assertEqual(state["qr_image"], "data:image/jpeg;base64," + base64.b64encode(jpeg).decode())
        self.assert_safe()

    def test_jpg_alias_still_requires_jpeg_signature_and_end_marker(self):
        for raw in (b"<svg/>", b"\xff\xd8\xffbroken", b"no-start\xff\xd9"):
            with self.subTest(raw=raw):
                self.responses.extend([Response(signin()), Response(raw, content_type="image/jpg")])
                self.assertEqual(self.binding.start()["state"], "error")
                self.assertIsNone(self.binding._token)

    def test_redirect_handler_is_installed_and_final_url_is_checked(self):
        handlers = self.factory.call_args.args
        self.assertIsNone(handlers[0].redirect_request(None, None, 302, "redirect", {}, "https://evil.test"))
        self.responses.append(Response(signin(), url="https://evil.test/"))
        self.assertEqual(self.binding.start()["state"], "error")
        self.assertEqual(self.opener.open.call_count, 1)

    def test_tls_verifies_certificate_and_hostname(self):
        context = self.factory.call_args.args[1]._context
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_timeout_validation(self):
        for value in (0, -1, True, "10", float("inf"), float("nan"), 121, 10**1000):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    ServerChanBinding(value)


if __name__ == "__main__":
    unittest.main()
