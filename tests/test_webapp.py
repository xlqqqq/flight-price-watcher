import json
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from flightwatch.models import ConfigError, ProviderError, Quote, SearchResult
from flightwatch.notifier import NotificationError
from flightwatch.webapp import Dashboard, TZ, normalize_form, settings_for


def form(**changes):
    start = datetime.now(TZ).date() + timedelta(days=7)
    result = dict(origin="上海", destination="北京", market="domestic",
        start_date=start.isoformat(), end_date=(start + timedelta(days=2)).isoformat(),
        threshold=600, mode="both", interval_minutes=10, notify="browser")
    result.update(changes)
    return result


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Dashboard(Path(self.tmp.name))
        self.input = normalize_form(form())

    def prices(self):
        day = date.fromisoformat(self.input["start_date"])
        return SearchResult([Quote("SHA", "BJS", day, Decimal("500"), "CNY", "测试日历")], [])

    def query(self, **kwargs):
        self.app._query(self.input, kwargs.get("monitor", False),
                        kwargs.get("event", threading.Event()))

    def test_place_and_date_selection_maps_to_exact_query(self):
        chosen = normalize_form(form(destination="东京 TYO", market="international"))
        settings = settings_for(chosen, Path(self.tmp.name))
        route = settings.routes[0]
        self.assertEqual((route.origin, route.destination, route.market), ("SHA", "TYO", "international"))
        self.assertEqual(len(route.dates), 3)
        self.assertEqual(route.dates[0].isoformat(), chosen["start_date"])
        self.assertEqual(route.dates[-1].isoformat(), chosen["end_date"])

    def test_bad_dates_places_prices_and_channel_rejected(self):
        today = datetime.now(TZ).date()
        invalid = [dict(destination="上海"), dict(destination="北京 SHA"), dict(market="international"),
            dict(start_date=(today - timedelta(days=1)).isoformat()),
            dict(end_date=(today + timedelta(days=40)).isoformat()), dict(threshold=float("nan")),
            dict(threshold=True), dict(notify="pushplus"), dict(interval_minutes=1), dict(token="secret")]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ConfigError):
                normalize_form(form(**change))

    def test_lowest_without_target_is_valid(self):
        self.assertIsNone(normalize_form(form(mode="lowest", threshold=None))["threshold"])

    def test_new_page_does_not_preselect_fixed_places(self):
        defaults = self.app.bootstrap()["defaults"]
        self.assertEqual((defaults["origin"], defaults["destination"]), ("", ""))

    def test_platform_choices_default_to_all_and_persist_in_route(self):
        boot = self.app.bootstrap()
        self.assertGreaterEqual(len(boot["providers"]), 2)
        self.assertEqual(boot["defaults"]["providers"], [p["id"] for p in boot["providers"]])
        selected = normalize_form(form(providers=["tongcheng"]))
        route = settings_for(selected, Path(self.tmp.name)).routes[0]
        self.assertEqual(route.sources, ("tongcheng",))
        self.assertEqual(route.provider, "multi")
        with self.assertRaises(ConfigError):
            normalize_form(form(providers=[]))

    def test_query_exposes_source_status_and_price_basis(self):
        from dataclasses import replace
        result = self.prices()
        result.quotes = [replace(result.quotes[0], provider="tongcheng", price_basis="base")]
        result.sources = [dict(id="tongcheng", name="同程", status="ok", quote_count=1,
                               lowest_price=None, message="未含税")]
        with patch("flightwatch.webapp.MultiSourceProvider") as provider:
            provider.return_value.search.return_value = result
            self.query(monitor=True)
        self.assertEqual(self.app.latest["sources"], result.sources)
        self.assertFalse(self.app.latest["quotes"][0]["comparable"])
        self.assertTrue(self.app.latest["error"])
        self.assertFalse(self.app.notifications)

    def test_city_lookup_can_return_places_outside_popular_list(self):
        city = dict(name="喀什", code="KHG", country="中国", market="domestic")
        with patch("flightwatch.city_search.search_cities", return_value=[city]) as lookup:
            result = self.app.city_lookup("kashi")
        self.assertEqual(result["cities"], [city])
        lookup.assert_called_once_with("kashi")

    def test_city_lookup_failure_is_visible_and_keeps_local_matches(self):
        with patch("flightwatch.city_search.search_cities", side_effect=ProviderError("城市服务暂不可用")):
            result = self.app.city_lookup("上海")
        self.assertEqual(result["cities"][0]["code"], "SHA")
        self.assertIn("暂不可用", result["warning"])

    def test_dynamic_city_metadata_controls_market_and_display_name(self):
        city = dict(name="布拉格", code="PRG", country="捷克", market="international")
        with patch("flightwatch.city_search.cached_city", side_effect=lambda value: city if value == "PRG" else None):
            chosen = normalize_form(form(destination="PRG", market="international"))
            settings = settings_for(chosen, Path(self.tmp.name))
            with self.assertRaisesRegex(ConfigError, "不匹配"):
                normalize_form(form(destination="PRG", market="domestic"))
        self.assertEqual(settings.routes[0].name, "上海 → 布拉格")

    def test_no_tokens_required_to_query_or_bootstrap(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("flightwatch.webapp.MultiSourceProvider") as provider:
            provider.return_value.search.return_value = self.prices()
            self.query()
            boot = self.app.bootstrap()
        self.assertEqual(self.app.latest["quotes"][0]["price"], 500)
        self.assertFalse(self.app.notifications)
        self.assertTrue(boot["cities"])
        self.assertNotIn("token", json.dumps(boot["defaults"]))

    def test_no_desktop_does_not_silently_start_browser_instead(self):
        with patch("flightwatch.desktop_notifier.desktop_status", return_value={"available": False, "message": "缺少桌面"}):
            with self.assertRaisesRegex(ConfigError, "缺少桌面"):
                self.app.start(form(notify="wechat"))
        self.assertFalse(self.app.monitor["running"])

    def test_browser_start_persists_choices_without_desktop_check(self):
        with patch("flightwatch.webapp.threading.Thread"), \
                patch("flightwatch.desktop_notifier.desktop_status") as desktop:
            self.app.start(form())
        desktop.assert_not_called()
        saved = json.loads((Path(self.tmp.name) / "web-settings.json").read_text())
        self.assertEqual(saved["notify"], "browser")
        self.assertTrue(self.app.monitor["running"])
        self.app.stop()
        self.assertFalse(self.app.monitor["running"])

    def test_browser_alert_persistent_dedup_and_cheapest(self):
        with patch("flightwatch.webapp.MultiSourceProvider") as provider:
            provider.return_value.search.return_value = self.prices()
            self.query(monitor=True)
            self.query(monitor=True)
        self.assertEqual(len(self.app.notifications), 1)
        self.assertIn("500.00", self.app.notifications[0]["content"])
        self.assertEqual(provider.return_value.search.call_count, 2)

    def test_wechat_failure_not_acknowledged_then_retry(self):
        self.input["notify"] = "wechat"
        with patch("flightwatch.webapp.MultiSourceProvider") as provider, \
                patch("flightwatch.desktop_notifier.DesktopWeChatNotifier") as notifier:
            provider.return_value.search.return_value = self.prices()
            notifier.return_value.send.side_effect = NotificationError("未发送")
            self.query(monitor=True)
            self.assertEqual(self.app.notifications, [])
            self.assertEqual(self.app.monitor["last_error"], "未发送")
            notifier.return_value.send.side_effect = None
            notifier.return_value.send.return_value = "sent_to_client:文件传输助手:test"
            self.query(monitor=True)
        self.assertEqual(len(self.app.notifications), 1)

    def test_history_lock_failure_preserves_fresh_prices_and_source_reports(self):
        result = self.prices()
        result.sources = [dict(id="ctrip", name="携程", status="ok", quote_count=1,
                               lowest_price=500, message="查询成功")]
        with patch("flightwatch.webapp.MultiSourceProvider") as provider, \
                patch("flightwatch.cli.process_lock", side_effect=ConfigError("历史库正被其他进程使用")):
            provider.return_value.search.return_value = result
            self.query(monitor=True)
        self.assertEqual(self.app.latest["quotes"][0]["price"], 500)
        self.assertEqual(self.app.latest["sources"], result.sources)
        self.assertEqual(self.app.latest["origin"], "SHA")
        self.assertEqual(self.app.latest["start_date"], self.input["start_date"])
        self.assertIsNone(self.app.latest["error"])
        self.assertEqual(self.app.monitor["last_error"], "历史库正被其他进程使用")
        self.assertFalse(self.app.notifications)

    def test_history_open_failure_preserves_prices_and_reports_monitor_error(self):
        with patch("flightwatch.webapp.MultiSourceProvider") as provider, \
                patch("flightwatch.webapp.State", side_effect=OSError("fixture permissions failure")):
            provider.return_value.search.return_value = self.prices()
            self.query(monitor=True)
        self.assertEqual(self.app.latest["quotes"][0]["price"], 500)
        self.assertIsNone(self.app.latest["error"])
        self.assertIn("报价已查询", self.app.monitor["last_error"])
        self.assertNotIn("fixture", self.app.monitor["last_error"])
        self.assertFalse(self.app.notifications)

    def test_monitor_passes_cancellation_to_public_sources(self):
        event = threading.Event()
        with patch("flightwatch.webapp.MultiSourceProvider") as provider:
            provider.return_value.search.return_value = self.prices()
            self.query(monitor=True, event=event)
            callback = provider.call_args.kwargs["cancelled"]
            self.assertFalse(callback())
            event.set()
            self.assertTrue(callback())

    def test_stop_during_network_prevents_later_notification(self):
        event = threading.Event()
        def query(*args):
            event.set()
            return self.prices()
        with patch("flightwatch.webapp.MultiSourceProvider") as provider:
            provider.return_value.search.side_effect = query
            self.query(monitor=True, event=event)
        self.assertFalse(self.app.notifications)

    def test_old_monitor_cleanup_does_not_release_manual_query(self):
        # Stop a sleeping monitor after a newer manual operation acquired the slot.
        with self.app.lock:
            old_claim = self.app._claim()
            self.app._release(old_claim)
            self.app.last_query = 0
            new_claim = self.app._claim()
        event = threading.Event()
        event.set()
        self.app._loop(self.input, event, old_claim)
        self.assertTrue(self.app.search_busy)
        self.assertEqual(self.app.operation_id, new_claim)

    def test_quick_followup_query_is_queued_instead_of_rejected(self):
        with self.app.lock:
            first = self.app._claim()
            first_time = self.app.operation_ready_at
            self.app._release(first)
            second = self.app._claim()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(self.app.operation_ready_at, first_time + 3)

    def test_source_failure_does_not_leave_busy_or_old_prices(self):
        self.app.latest = {"quotes": [{"price": 1}]}
        with self.app.lock:
            claim = self.app._claim()
        with patch("flightwatch.webapp.MultiSourceProvider") as provider:
            provider.return_value.search.side_effect = ProviderError("暂不可用")
            self.app._query(self.input, False, None, claim)
        self.assertFalse(self.app.search_busy)
        self.assertEqual(self.app.latest["quotes"], [])
        self.assertEqual(self.app.latest["error"], "暂不可用")

    def test_empty_calendar_never_creates_alert(self):
        with patch("flightwatch.webapp.MultiSourceProvider") as provider:
            provider.return_value.search.return_value = SearchResult([], [])
            self.query(monitor=True)
        self.assertTrue(self.app.latest["error"])
        self.assertFalse(self.app.notifications)


if __name__ == "__main__":
    unittest.main()
