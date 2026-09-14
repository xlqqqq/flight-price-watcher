"""Integration checks for alert decisions and persisted delivery state.

The real monitor, policy and SQLite State run together. Only the external
flight source and notification transport are mocked; no live messages or
network requests are sent.
"""

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from flightwatch.config import Settings
from flightwatch.models import ProviderError, Quote, Route, SearchResult
from flightwatch.monitor import run_cycle
from flightwatch.notifier import NotificationError
from flightwatch.state import State


NOW = datetime(2026, 9, 8, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DEPARTURE = date(2026, 10, 1)


def monitored_route(**changes):
    values = dict(id="pek-sha", name="北京到上海", origin="PEK", destination="SHA",
                  provider="serpapi", currency="CNY", mode="threshold",
                  threshold=Decimal("1000"), dates=(DEPARTURE,))
    values.update(changes)
    return Route(**values)


def quote(price="900", route=None, **changes):
    route = route or monitored_route()
    values = dict(origin=route.origin, destination=route.destination, departure_date=DEPARTURE,
                  price=Decimal(price), currency=route.currency, source="mock fixture source",
                  return_date=route.return_on(DEPARTURE), airline="Fixture airline", flight_number="FX 100",
                  url="https://www.google.com/travel/flights",
                  origin_airport="PEK", destination_airport="PVG")
    values.update(changes)
    return Quote(**values)


class MonitorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "nested" / "prices.sqlite3"
        self.state = State(self.database)
        self.route = monitored_route()
        self.settings = Settings(
            routes=(self.route,), timezone=ZoneInfo("Asia/Shanghai"), interval_minutes=60,
            digest_hours=24, repeat_hours=12, min_drop=Decimal("10"), timeout_seconds=30,
            request_delay_seconds=1.0, max_requests_per_cycle=60, database=self.database,
        )
        self.source = Mock()
        self.source.search.return_value = SearchResult([quote()], [])
        self.notifier = Mock()
        self.notifier.send.return_value = "accepted-receipt-123"

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def run_cycle(self, now=NOW, settings=None, *, dry_run=False):
        return run_cycle(settings or self.settings, {"serpapi": self.source}, self.state,
                         self.notifier, now=now, dry_run=dry_run)

    def reopen(self):
        self.state.close()
        self.state = State(self.database)

    def content(self):
        return self.notifier.send.call_args.args[1]

    def test_equality_and_above_threshold_do_not_trigger(self):
        for price in ("1000", "1200"):
            with self.subTest(price=price):
                self.source.search.return_value = SearchResult([quote(price)], [])
                self.assertEqual(self.run_cycle(), 0)
        self.notifier.send.assert_not_called()
        self.assertIsNone(self.state.last_alert(self.route.state_key(), "threshold"))

    def test_city_reference_below_threshold_cannot_notify(self):
        self.source.search.return_value = SearchResult([], [], city_references=[
            quote("1", price_basis="unknown", origin_airport="", destination_airport="")])
        self.run_cycle()
        self.notifier.send.assert_not_called()
        self.assertIsNone(self.state.last_alert(self.route.state_key(), "threshold"))

    def test_airport_monitor_never_alerts_for_cheaper_other_airport(self):
        route = monitored_route(origin="PEK", destination="PVG", origin_scope="airport",
            destination_scope="airport", origin_city_code="BJS", destination_city_code="SHA")
        settings = replace(self.settings, routes=(route,))
        self.source.search.return_value = SearchResult([
            quote("1", route, origin_airport="PKX"),
            quote("2", route, destination_airport="SHA"),
            quote("3", route, origin_airport=""),
            quote("1200", route)], [])
        self.run_cycle(settings=settings)
        self.notifier.send.assert_not_called()
        self.source.search.return_value = SearchResult([quote("800", route)], [])
        self.run_cycle(settings=settings)
        self.assertIn("800.00", self.content())
        self.assertIn("PEK → PVG", self.content())

    def test_strictly_under_threshold_sends_cheapest_and_persists_receipt(self):
        self.source.search.return_value = SearchResult([quote("950"), quote("800"), quote("900")], [])
        self.assertEqual(self.run_cycle(), 0)
        self.notifier.send.assert_called_once()
        self.assertIn("CNY 800.00", self.content())
        self.assertIn("低于目标价", self.content())
        self.assertIn("严格低于 CNY 1000.00", self.content())
        self.assertNotIn("最低价汇总", self.content())
        self.assertIn("实际起降机场：PEK → PVG", self.content())
        self.assertIn("按本行程继续购买：https://www.google.com/travel/flights", self.content())
        self.assertEqual(self.state.last_alert(self.route.state_key(), "threshold"), (Decimal("800"), NOW))
        stored = self.state.connection.execute("SELECT receipt FROM alerts").fetchone()
        self.assertEqual(stored[0], "accepted-receipt-123")

    def test_successful_alert_is_suppressed_after_database_reopen(self):
        self.assertEqual(self.run_cycle(), 0)
        self.reopen()
        self.assertEqual(self.run_cycle(NOW + timedelta(hours=1)), 0)
        self.notifier.send.assert_called_once()
        observations = self.state.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        self.assertEqual(observations, 2)
        self.assertEqual(self.state.last_alert(self.route.state_key(), "threshold"), (Decimal("900"), NOW))

    def test_additional_drop_uses_last_sent_price_and_minimum_drop(self):
        self.run_cycle()
        self.source.search.return_value = SearchResult([quote("895")], [])
        self.run_cycle(NOW + timedelta(hours=1))
        self.assertEqual(self.notifier.send.call_count, 1)
        self.source.search.return_value = SearchResult([quote("890")], [])
        self.run_cycle(NOW + timedelta(hours=2))
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertIn("CNY 890.00", self.content())
        self.assertIn("此前本脚本观察到的最低价：CNY 895.00", self.content())

    def test_unchanged_price_repeats_at_configured_interval(self):
        self.run_cycle()
        self.run_cycle(NOW + timedelta(hours=12) - timedelta(seconds=1))
        self.assertEqual(self.notifier.send.call_count, 1)
        self.run_cycle(NOW + timedelta(hours=12))
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertEqual(self.state.last_alert(self.route.state_key(), "threshold")[1], NOW + timedelta(hours=12))

    def test_price_above_threshold_does_not_repeat_after_interval(self):
        self.run_cycle()
        self.source.search.return_value = SearchResult([quote("1001")], [])
        self.run_cycle(NOW + timedelta(hours=24))
        self.assertEqual(self.notifier.send.call_count, 1)

    def test_lowest_digest_has_its_own_interval_and_no_threshold_requirement(self):
        route = replace(self.route, mode="lowest", threshold=None)
        settings = replace(self.settings, routes=(route,))
        self.source.search.return_value = SearchResult([quote("1500", route)], [])
        self.assertEqual(self.run_cycle(settings=settings), 0)
        self.assertIn("最低价汇总", self.content())
        self.assertNotIn("低于目标价", self.content())
        self.source.search.return_value = SearchResult([quote("1400", route)], [])
        self.run_cycle(NOW + timedelta(hours=23), settings)
        self.assertEqual(self.notifier.send.call_count, 1)
        self.run_cycle(NOW + timedelta(hours=24), settings)
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertIn("CNY 1400.00", self.content())

    def test_both_modes_coalesce_message_but_keep_independent_intervals(self):
        route = replace(self.route, mode="both")
        settings = replace(self.settings, routes=(route,))
        self.run_cycle(settings=settings)
        self.assertIn("最低价汇总 / 低于目标价", self.content())
        self.notifier.send.assert_called_once()
        self.source.search.return_value = SearchResult([quote("850")], [])
        self.run_cycle(NOW + timedelta(hours=1), settings)
        self.assertNotIn("最低价汇总", self.content())
        self.assertEqual(self.state.last_alert(route.state_key(), "lowest")[1], NOW)
        self.assertEqual(self.state.last_alert(route.state_key(), "threshold")[1], NOW + timedelta(hours=1))

    def test_failed_notification_is_not_marked_and_identical_price_retries_after_reopen(self):
        self.notifier.send.side_effect = NotificationError("模拟发送失败")
        with self.assertLogs("flightwatch.monitor", level="ERROR") as logged:
            self.assertEqual(self.run_cycle(), 1)
        self.assertIn("下轮将重试", logged.output[0])
        self.assertIsNone(self.state.last_alert(self.route.state_key(), "threshold"))
        self.reopen()
        self.notifier.send.side_effect = None
        self.assertEqual(self.run_cycle(NOW + timedelta(minutes=1)), 0)
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertEqual(self.state.last_alert(self.route.state_key(), "threshold")[0], Decimal("900"))

    def test_one_source_failure_does_not_stop_another_route(self):
        other_route = monitored_route(id="sha-can", name="上海到广州", origin="SHA", destination="CAN", provider="ctrip")
        settings = replace(self.settings, routes=(self.route, other_route))
        self.source.search.side_effect = ProviderError("模拟数据源故障")
        other_source = Mock()
        other_source.search.return_value = SearchResult([quote("500", other_route)], [])
        with self.assertLogs("flightwatch.monitor", level="ERROR"):
            status = run_cycle(settings, {"serpapi": self.source, "ctrip": other_source}, self.state,
                               self.notifier, now=NOW)
        self.assertEqual(status, 1)
        other_source.search.assert_called_once_with(other_route, NOW.date())
        self.notifier.send.assert_called_once()
        self.assertIn("上海到广州", self.content())
        self.assertNotIn("北京到上海", self.content())
        self.assertIsNone(self.state.last_alert(self.route.state_key(), "threshold"))
        self.assertEqual(self.state.last_alert(other_route.state_key(), "threshold")[0], Decimal("500"))

    def test_partial_results_are_labeled_and_cycle_reports_warning(self):
        self.source.search.return_value = SearchResult([quote()], ["2026-10-02：模拟超时"])
        with self.assertLogs("flightwatch.monitor", level="WARNING"):
            self.assertEqual(self.run_cycle(), 1)
        self.notifier.send.assert_called_once()
        self.assertIn("成功查询部分中", self.content())
        self.assertIn("不代表完整日期窗口的最低价", self.content())

    def test_wrong_currency_departure_and_return_dates_are_excluded(self):
        invalid = [quote("1", currency="USD"),
                   quote("2", departure_date=DEPARTURE + timedelta(days=1)),
                   quote("3", return_date=DEPARTURE + timedelta(days=7))]
        self.source.search.return_value = SearchResult(invalid + [quote("900")], [])
        self.assertEqual(self.run_cycle(), 0)
        self.assertIn("CNY 900.00", self.content())
        self.assertEqual(self.state.last_alert(self.route.state_key(), "threshold")[0], Decimal("900"))

    def test_all_mismatched_quotes_do_not_send_or_mark_alert(self):
        self.source.search.return_value = SearchResult([quote("1", currency="USD")], [])
        with self.assertLogs("flightwatch.monitor", level="WARNING"):
            self.assertEqual(self.run_cycle(), 1)
        self.notifier.send.assert_not_called()
        self.assertIsNone(self.state.last_alert(self.route.state_key(), "threshold"))

    def test_base_fare_cannot_trigger_total_price_threshold(self):
        self.source.search.return_value = SearchResult([
            replace(quote("1"), price_basis="base"), quote("1200")], [])
        self.assertEqual(self.run_cycle(), 0)
        self.notifier.send.assert_not_called()
        self.assertIsNone(self.state.last_alert(self.route.state_key(), "threshold"))

    def test_multisource_notification_includes_each_platform_and_failure(self):
        self.source.search.return_value = SearchResult([quote("900")], ["同程：网络失败"], [
            dict(id="ctrip", name="携程", lowest_price=900, message="查询成功"),
            dict(id="tongcheng", name="同程", lowest_price=None, message="网络失败")])
        with self.assertLogs("flightwatch.monitor", level="WARNING"):
            self.assertEqual(self.run_cycle(), 1)
        self.assertIn("有可比报价的平台：1/2", self.content())
        self.assertIn("同程：网络失败", self.content())

    def test_roundtrip_uses_total_and_requires_matching_return_date(self):
        route = replace(self.route, stay_nights=7, threshold=Decimal("4000"))
        settings = replace(self.settings, routes=(route,))
        self.source.search.return_value = SearchResult([quote("100", route, return_date=None), quote("3000", route)], [])
        self.assertEqual(self.run_cycle(settings=settings), 0)
        self.assertIn("返程 2026-10-08 | 1 成人往返总价", self.content())
        self.assertIn("CNY 3000.00", self.content())

    def test_threshold_change_resets_deduplication_state(self):
        self.run_cycle()
        changed = replace(self.route, threshold=Decimal("950"))
        self.assertNotEqual(changed.state_key(), self.route.state_key())
        self.reopen()
        self.assertEqual(self.run_cycle(NOW + timedelta(minutes=1), replace(self.settings, routes=(changed,))), 0)
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertEqual(self.state.last_alert(self.route.state_key(), "threshold")[1], NOW)
        self.assertEqual(self.state.last_alert(changed.state_key(), "threshold")[1], NOW + timedelta(minutes=1))

    def test_display_name_change_keeps_deduplication_state(self):
        self.run_cycle()
        renamed = replace(self.route, name="另一种显示名称")
        self.run_cycle(NOW + timedelta(minutes=1), replace(self.settings, routes=(renamed,)))
        self.assertEqual(self.notifier.send.call_count, 1)

    def test_dry_run_previews_without_sending_or_consuming_alert(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.run_cycle(dry_run=True), 0)
        self.assertIn("预览，不发送微信", output.getvalue())
        self.notifier.send.assert_not_called()
        self.assertIsNone(self.state.last_alert(self.route.state_key(), "threshold"))
        self.assertEqual(self.run_cycle(NOW + timedelta(minutes=1)), 0)
        self.notifier.send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
