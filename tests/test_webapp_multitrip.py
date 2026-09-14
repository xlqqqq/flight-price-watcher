"""Exercise real monitor policy and SQLite state with mocked fares and delivery."""
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from flightwatch.models import ConfigError, ProviderError, Quote, SearchResult
from flightwatch.notifier import NotificationError
from flightwatch.webapp import Dashboard, TZ, normalize_request, settings_for


class MultiTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.app = Dashboard(self.directory)
        day = (datetime.now(TZ).date() + timedelta(days=7)).isoformat()
        base = dict(origin="SHA", origin_scope="city", origin_city_code="SHA",
                    destination_scope="city", market="international", start_date=day,
                    end_date=day, mode="threshold", providers=["trip"], threshold=600)
        self.raw = dict(trips=[dict(base, destination="TYO", destination_city_code="TYO"),
                               dict(base, destination="SEL", destination_city_code="SEL")],
                        notify="serverchan", interval_minutes=10)
        self.form = normalize_request(self.raw)
        self.prices = {"TYO": 500, "SEL": 450}
        self.failed = set()

    def fares(self, route, today):
        if route.destination in self.failed:
            raise ProviderError("该平台暂不可用")
        return SearchResult([Quote(route.origin, route.destination, route.dates[0],
            Decimal(self.prices[route.destination]), "CNY", "测试平台",
            url="https://example.com/booking")], [])

    def query(self):
        self.app._query(self.form, True, threading.Event())

    def test_both_trips_delivered_and_deduplicated_after_reload_and_reorder(self):
        with patch("flightwatch.webapp.MultiSourceProvider") as provider, \
                patch("flightwatch.serverchan_settings.make_notifier") as notifier:
            provider.return_value.search.side_effect = self.fares
            notifier.return_value.send.return_value = "accepted:test"
            self.query()
            content = notifier.return_value.send.call_args.args[1]
            self.assertIn("SHA → TYO", content)
            self.assertIn("SHA → SEL", content)
            self.assertEqual(len(self.app.latest["trips"]), 2)
            self.assertEqual(self.app.notifications[0]["channel"], "serverchan")
            self.app = Dashboard(self.directory)
            self.form["trips"].reverse()
            self.query()
            self.assertEqual(notifier.return_value.send.call_count, 1)

    def test_each_trip_uses_its_own_threshold(self):
        self.form["trips"][1]["threshold"] = 400
        with patch("flightwatch.webapp.MultiSourceProvider") as provider, \
                patch("flightwatch.serverchan_settings.make_notifier") as notifier:
            provider.return_value.search.side_effect = self.fares
            notifier.return_value.send.return_value = "accepted:test"
            self.query()
            content = notifier.return_value.send.call_args.args[1]
            self.assertIn("SHA → TYO", content)
            self.assertNotIn("SHA → SEL", content)
            self.prices["SEL"] = 350
            self.query()
            content = notifier.return_value.send.call_args.args[1]
            self.assertIn("SHA → SEL", content)
            self.assertNotIn("SHA → TYO", content)

    def test_failed_trip_does_not_block_other_trip_and_recovers(self):
        self.failed.add("TYO")
        with patch("flightwatch.webapp.MultiSourceProvider") as provider, \
                patch("flightwatch.serverchan_settings.make_notifier") as notifier:
            provider.return_value.search.side_effect = self.fares
            notifier.return_value.send.return_value = "accepted:test"
            self.query()
            self.assertIn("SHA → SEL", notifier.return_value.send.call_args.args[1])
            self.assertTrue(self.app.latest["trips"][0]["error"])
            self.failed.clear()
            self.query()
            self.assertIn("SHA → TYO", notifier.return_value.send.call_args.args[1])
            self.assertEqual(notifier.return_value.send.call_count, 2)

    def test_delivery_failure_retries_all_unacknowledged_trips(self):
        with patch("flightwatch.webapp.MultiSourceProvider") as provider, \
                patch("flightwatch.serverchan_settings.make_notifier") as notifier:
            provider.return_value.search.side_effect = self.fares
            notifier.return_value.send.side_effect = NotificationError("发送失败")
            self.query()
            self.assertFalse(self.app.notifications)
            notifier.return_value.send.side_effect = None
            notifier.return_value.send.return_value = "accepted:test"
            self.query()
            content = notifier.return_value.send.call_args.args[1]
            self.assertIn("SHA → TYO", content)
            self.assertIn("SHA → SEL", content)

    def test_labels_do_not_reset_alert_history(self):
        before = settings_for(self.form, self.directory).routes[0].state_key()
        self.form["trips"][0]["origin_label"] = "上海所有机场"
        self.assertEqual(before, settings_for(self.form, self.directory).routes[0].state_key())

    def test_duplicate_and_oversized_trip_list_rejected(self):
        for trips in ([self.raw["trips"][0]] * 2, self.raw["trips"] * 6, []):
            with self.assertRaises(ConfigError):
                normalize_request(dict(self.raw, trips=trips))

    def test_start_persists_every_trip_without_sending_messages(self):
        import json
        with patch("flightwatch.serverchan_settings.channel_status", return_value={"available": True}), \
                patch("flightwatch.webapp.threading.Thread"):
            self.app.start(self.raw)
        saved = json.loads((self.directory / "web-settings.json").read_text())
        self.assertEqual(len(saved["trips"]), 2)
        self.assertEqual(saved["notify"], "serverchan")
        self.assertEqual(len(self.app.bootstrap()["defaults"]["trips"]), 2)
        self.app.stop()
