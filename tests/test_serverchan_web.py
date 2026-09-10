import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from flightwatch.models import ConfigError, Quote, SearchResult
from flightwatch.notifier import NotificationError
from flightwatch.serverchan_settings import (
    FILENAME, channel_status, clear_sendkey, read_sendkey, save_sendkey,
)
from flightwatch.webapp import Dashboard, TZ, normalize_form, settings_for


KEY = "SCT1234567890forOfflineTestsOnly"


def form(**changes):
    day = (datetime.now(TZ).date() + timedelta(days=7)).isoformat()
    result = dict(origin="SHA", destination="BJS", market="domestic",
                  start_date=day, end_date=day, threshold=600, mode="threshold",
                  interval_minutes=360, notify="serverchan", providers=["ctrip"])
    result.update(changes)
    return result


class ServerChanWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.app = Dashboard(self.directory)

    def finish(self):
        deadline = time.monotonic() + 3
        while self.app.status()["search_busy"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

    def test_credential_saved_privately_and_not_exposed(self):
        self.app.configure_serverchan({"sendkey": "  " + KEY + "  "})
        self.assertEqual(read_sendkey(self.directory), KEY)
        if os.name != "nt":
            self.assertEqual((self.directory / FILENAME).stat().st_mode & 0o777, 0o600)
        for public in (self.app.bootstrap(), self.app.status()):
            self.assertNotIn(KEY, json.dumps(public))
            self.assertTrue(public["serverchan"]["configured"])
            self.assertEqual(public["serverchan"]["quota"]["remaining"], 5)
        self.assertFalse((self.directory / "web-settings.json").exists())
        self.assertFalse(self.app.monitor["running"])
        self.assertEqual(self.app.notifications, [])

    def test_invalid_or_corrupt_credential_not_reflected(self):
        for value in ("sctp123SECRET", "https://evil.invalid/SECRET", KEY + "\nSECRET", None):
            with self.subTest(value=value), self.assertRaises(NotificationError) as caught:
                save_sendkey(self.directory, value)
            self.assertNotIn("SECRET", str(caught.exception))
        (self.directory / FILENAME).write_text('{"sendkey":"SECRET"}')
        status = channel_status(self.directory)
        self.assertFalse(status["available"])
        self.assertNotIn("SECRET", json.dumps(status))

    @unittest.skipIf(os.name == "nt", "POSIX symlink test")
    def test_credential_reader_rejects_symlink(self):
        other = self.directory / "unrelated.json"
        other.write_text(json.dumps({"sendkey": KEY}))
        (self.directory / FILENAME).symlink_to(other)
        with self.assertRaises(NotificationError):
            read_sendkey(self.directory)
        save_sendkey(self.directory, KEY)
        self.assertFalse((self.directory / FILENAME).is_symlink())
        self.assertEqual(read_sendkey(self.directory), KEY)

    def test_credentials_never_allowed_inside_trip_form(self):
        with self.assertRaises(ConfigError):
            normalize_form(form(sendkey=KEY))
        normalized = normalize_form(form())
        self.assertEqual(normalized["notify"], "serverchan")
        self.assertNotEqual(settings_for(normalized, self.directory).routes[0].state_key(),
                            settings_for(normalize_form(form(notify="browser")), self.directory).routes[0].state_key())

    def test_not_configured_rejects_monitor_without_desktop(self):
        with patch("flightwatch.desktop_notifier.desktop_status") as desktop:
            with self.assertRaises(ConfigError):
                self.app.start(form())
        desktop.assert_not_called()
        self.assertFalse(self.app.monitor["running"])

    def test_configured_server_monitor_start_needs_no_desktop_and_does_not_save_key_in_trip(self):
        save_sendkey(self.directory, KEY)
        with patch("flightwatch.webapp.threading.Thread"), patch("flightwatch.desktop_notifier.desktop_status") as desktop:
            self.app.start(form())
        desktop.assert_not_called()
        self.assertTrue(self.app.monitor["running"])
        self.assertNotIn(KEY, (self.directory / "web-settings.json").read_text())
        self.app.stop()

    def test_configuration_locked_during_queries_monitor_and_tests(self):
        save_sendkey(self.directory, KEY)
        for field in ("busy", "running"):
            self.app.search_busy = field == "busy"
            self.app.monitor["running"] = field == "running"
            for operation in (lambda: self.app.configure_serverchan({"sendkey": KEY}),
                              lambda: self.app.configure_serverchan({}, clear=True),
                              lambda: self.app.bind_serverchan("start")):
                with self.assertRaises(ConfigError):
                    operation()
        self.assertEqual(read_sendkey(self.directory), KEY)

    def test_binding_start_is_not_a_push_and_confirm_is_explicit(self):
        binding = self.app.serverchan_binding
        with patch.object(binding, "start") as start, patch.object(binding, "confirm") as confirm:
            self.app.bind_serverchan("start")
            self.finish()
        start.assert_called_once_with()
        confirm.assert_not_called()
        self.assertFalse(channel_status(self.directory)["configured"])
        self.assertFalse(self.app.notifications)

    def test_binding_confirm_only_saves_authorized_key(self):
        binding = self.app.serverchan_binding
        with patch.object(binding, "status", return_value={"state": "waiting", "message": "test", "qr_image": None}), \
                patch.object(binding, "confirm", return_value=KEY), \
                patch("flightwatch.serverchan_notifier.ServerChanNotifier.send") as send:
            self.app.bind_serverchan("confirm")
            self.finish()
        self.assertEqual(read_sendkey(self.directory), KEY)
        send.assert_not_called()
        self.assertFalse(self.app.notifications)
        self.assertFalse(self.app.monitor["running"])
        self.assertNotIn(KEY, json.dumps(self.app.status()))

    def test_pending_scan_does_not_overwrite_existing_key(self):
        save_sendkey(self.directory, KEY)
        binding = self.app.serverchan_binding
        with patch.object(binding, "status", return_value={"state": "waiting", "message": "test", "qr_image": None}), \
                patch.object(binding, "confirm", return_value=None):
            self.app.bind_serverchan("confirm")
            self.finish()
        self.assertEqual(read_sendkey(self.directory), KEY)

    def test_binding_network_does_not_block_status_or_hide_busy(self):
        entered, release = threading.Event(), threading.Event()
        def slow_start():
            entered.set()
            release.wait(3)
        self.addCleanup(release.set)
        with patch.object(self.app.serverchan_binding, "start", side_effect=slow_start):
            self.app.bind_serverchan("start")
            self.assertTrue(entered.wait(1))
            now = time.monotonic()
            status = self.app.status()
            self.assertLess(time.monotonic() - now, 0.5)
            self.assertTrue(status["search_busy"])
            self.assertEqual(status["serverchan_binding"]["state"], "creating")
            with self.assertRaises(ConfigError):
                self.app.configure_serverchan({"sendkey": KEY})
            release.set()
            self.finish()

    def test_binding_save_failure_is_visible_and_not_configured(self):
        binding = self.app.serverchan_binding
        with patch.object(binding, "status", return_value={"state": "waiting"}), \
                patch.object(binding, "confirm", return_value=KEY), \
                patch("flightwatch.serverchan_settings.save_sendkey", side_effect=NotificationError("无法保存配置")):
            self.app.bind_serverchan("confirm")
            self.finish()
        status = self.app.status()
        self.assertEqual(status["serverchan_binding"]["state"], "error")
        self.assertFalse(status["serverchan"]["configured"])

    def test_test_message_records_acceptance_and_consumes_shared_budget(self):
        from unittest.mock import MagicMock
        from flightwatch.serverchan_notifier import quota_status
        save_sendkey(self.directory, KEY)
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"code":0,"data":{"pushid":"12345","errno":0,"error":"SUCCESS"}}'
        response.__enter__.return_value = response
        with patch("urllib.request.OpenerDirector.open", return_value=response):
            self.app.test_serverchan()
            self.finish()
        self.assertEqual(quota_status(KEY, self.directory)["used"], 1)
        self.assertEqual(self.app.notifications[0]["channel"], "serverchan")
        self.assertNotIn(KEY, json.dumps(self.app.status()))

    def test_failed_notification_keeps_prices_and_remains_retryable(self):
        normalized = normalize_form(form())
        quote = Quote("SHA", "BJS", date.fromisoformat(normalized["start_date"]), Decimal("499.25"), "CNY", "测试平台")
        with patch("flightwatch.webapp.MultiSourceProvider") as provider, \
                patch("flightwatch.serverchan_settings.make_notifier") as notifier:
            provider.return_value.search.return_value = SearchResult([quote], [])
            notifier.return_value.send.side_effect = NotificationError("今日预算已用完")
            self.app._query(normalized, True, threading.Event())
            self.assertEqual(self.app.latest["quotes"][0]["price"], 499.25)
            self.assertEqual(self.app.monitor["last_error"], "今日预算已用完")
            self.assertFalse(self.app.notifications)
            notifier.return_value.send.side_effect = None
            notifier.return_value.send.return_value = "accepted:serverchan:12345"
            self.app._query(normalized, True, threading.Event())
            self.assertEqual(len(self.app.notifications), 1)
            title = notifier.return_value.send.call_args.args[0]
            self.assertIn("499.25", title)
            self.assertIn("SHA→BJS", title)
            self.assertLessEqual(len(title), 32)

    def test_clear_does_not_delete_daily_quota(self):
        save_sendkey(self.directory, KEY)
        channel_status(self.directory)
        clear_sendkey(self.directory)
        self.assertFalse(channel_status(self.directory)["configured"])
        self.assertTrue((self.directory / "serverchan-quota.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
