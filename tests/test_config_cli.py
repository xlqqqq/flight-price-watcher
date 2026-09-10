"""Configuration and preflight regressions; no external network or messages."""
import contextlib
import io
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from flightwatch.cli import main, make_providers, process_lock
from flightwatch.config import load_config, load_env
from flightwatch.models import ConfigError, ProviderError, Quote, SearchResult


BASE = '''
[monitor]
max_requests_per_cycle = 2
[[routes]]
id = "test"
origin = "PVG"
destination = "NRT"
provider = "serpapi"
market = "international"
mode = "threshold"
threshold = 1000
start_offset_days = 7
end_offset_days = 9
'''


class ConfigAndCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(BASE, encoding="utf-8")

    def test_budget_rejected_before_any_api_request(self):
        with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}, clear=True), \
                patch("flightwatch.cli.make_providers") as factory, \
                self.assertLogs(level="ERROR") as logs:
            code = main(["--config", str(self.path), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("超过上限", " ".join(logs.output))
        factory.assert_not_called()

    def test_push_test_does_not_require_flight_key_or_flight_budget(self):
        with patch.dict(os.environ, {"PUSHPLUS_TOKEN": "test-token"}, clear=True), \
                patch("flightwatch.cli.PushPlusNotifier") as notifier, \
                contextlib.redirect_stdout(io.StringIO()):
            notifier.return_value.send.return_value = "accepted:test-receipt"
            code = main(["--config", str(self.path), "--test-push", "--notifier", "pushplus"])
        self.assertEqual(code, 0)
        notifier.return_value.send.assert_called_once()

    def test_invalid_price_dates_and_unknown_options_rejected(self):
        mutations = [
            BASE.replace("threshold = 1000", "threshold = nan"),
            BASE.replace("threshold = 1000", "threshold = -1"),
            BASE.replace("end_offset_days = 9", "end_offset_days = 6"),
            BASE.replace("end_offset_days = 9", "end_offset_days = 40"),
            BASE + '\ndates = ["2030-01-01"]\n',
            BASE + '\nnonstop = "false"\n',
            BASE + '\nunknown_filter = true\n',
            BASE.replace('origin = "PVG"', 'origin = "NRT"'),
        ]
        for text in mutations:
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_ctrip_rejects_silent_filter_downgrades(self):
        raw = BASE.replace('provider = "serpapi"', 'provider = "ctrip"')
        for option in ('nonstop = true', 'stay_nights = 5', 'travel_class = 3', 'currency = "USD"'):
            with self.subTest(option=option):
                self.path.write_text(raw + "\n" + option, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_env_is_literal_and_environment_wins(self):
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text('TOKEN="$(echo secret)"\nALREADY=file-value\n', encoding="utf-8")
        with patch.dict(os.environ, {"ALREADY": "env-value"}, clear=True):
            load_env(env_path)
            self.assertEqual(os.environ["TOKEN"], "$(echo secret)")
            self.assertEqual(os.environ["ALREADY"], "env-value")

    def test_multi_config_keeps_selected_platforms_and_needs_no_key(self):
        raw = BASE.replace('provider = "serpapi"', 'provider = "multi"\nsources = ["tongcheng", "ctrip"]')
        self.path.write_text(raw, encoding="utf-8")
        route = load_config(self.path).routes[0]
        self.assertEqual(route.sources, ("ctrip", "tongcheng"))
        with patch.dict(os.environ, {}, clear=True):
            from flightwatch.cli import readiness
            self.assertEqual(readiness(load_config(self.path), push=False), [])

    def test_invalid_platform_selection_rejected(self):
        raw = BASE.replace('provider = "serpapi"', 'provider = "multi"')
        for selection in ('[]', '["invented"]', '["ctrip", "ctrip"]'):
            with self.subTest(selection=selection):
                self.path.write_text(raw + '\nsources = ' + selection, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_shared_source_budget_counts_multi_and_single_routes_together(self):
        raw = BASE.replace('provider = "serpapi"', 'provider = "multi"\nsources = ["ctrip"]')
        extra = '\n[[routes]]\nid = "extra%d"\norigin = "SHA"\ndestination = "BJS"\nprovider = "ctrip"\nmode = "lowest"\n'
        self.path.write_text(raw + extra % 1 + extra % 2, encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True), patch("flightwatch.cli.make_providers") as factory, \
                self.assertLogs(level="ERROR") as logs:
            self.assertEqual(main(["--config", str(self.path), "--dry-run"]), 2)
        factory.assert_not_called()
        self.assertIn("ctrip 每轮需要 3 次查询", " ".join(logs.output))

    def test_qunar_budget_includes_both_city_lookups(self):
        self.path.write_text(BASE.replace('provider = "serpapi"', 'provider = "qunar"'), encoding="utf-8")
        with patch("flightwatch.cli.make_providers") as factory, self.assertLogs(level="ERROR") as logs:
            self.assertEqual(main(["--config", str(self.path), "--dry-run"]), 2)
        factory.assert_not_called()
        self.assertIn("qunar 每轮需要 3 次查询", " ".join(logs.output))

    def test_cli_source_constructor_failure_keeps_other_source_results(self):
        raw = BASE.replace('provider = "serpapi"', 'provider = "multi"\nsources = ["ctrip", "qunar"]')
        self.path.write_text(raw, encoding="utf-8")
        settings = load_config(self.path)
        route = settings.routes[0]
        today = date(2026, 9, 8)
        quote = Quote(route.origin, route.destination, route.departure_dates(today)[0],
                      Decimal("500"), "CNY", "fixture")
        good = Mock()
        good.search.return_value = SearchResult([quote], [])
        def factory(name, *args, **kwargs):
            if name == "qunar":
                raise RuntimeError("fixture construction failure")
            return good
        with patch("flightwatch.sources.make_public_provider", side_effect=factory) as build:
            providers = make_providers(settings, False)
            build.assert_not_called()
            result = providers["multi"].search(route, today)
        self.assertEqual([q.price for q in result.quotes], [Decimal("500")])
        self.assertEqual([s["status"] for s in result.sources], ["ok", "error"])
        good.search.assert_called_once()

    def test_cli_multi_and_single_routes_share_actual_source_budget(self):
        settings = load_config(self.path)
        multi = replace(settings.routes[0], provider="multi", sources=("ctrip",))
        single = replace(multi, id="single", provider="ctrip", sources=())
        settings = replace(settings, routes=(multi, single))
        class LimitedSource:
            def __init__(self):
                self.requests_used = 0
            def search(self, route, today):
                if self.requests_used >= 2:
                    raise ProviderError("fixture request budget exhausted")
                self.requests_used += 1
                return SearchResult([], [])
        source = LimitedSource()
        with patch("flightwatch.sources.make_public_provider", return_value=source) as factory:
            providers = make_providers(settings, False)
            self.assertIs(providers["multi"], providers["ctrip"])
            providers["multi"].search(multi, date(2026, 9, 8))
            providers["ctrip"].search(single, date(2026, 9, 8))
            third = providers["multi"].search(multi, date(2026, 9, 8))
        factory.assert_called_once()
        self.assertEqual(source.requests_used, 2)
        self.assertEqual(third.sources[0]["status"], "error")
        self.assertIn("budget exhausted", third.sources[0]["message"])

    def test_process_lock_released_after_error(self):
        path = Path(self.tmp.name) / "data" / "lock"
        with self.assertRaisesRegex(RuntimeError, "test exit"):
            with process_lock(path):
                with self.assertRaises(ConfigError):
                    with process_lock(path):
                        self.fail("duplicate process acquired lock")
                raise RuntimeError("test exit")
        with process_lock(path):
            pass


if __name__ == "__main__":
    unittest.main()
