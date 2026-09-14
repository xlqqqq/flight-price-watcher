"""Cross-platform prices must keep tax basis, route identity and failures."""
import threading
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from flightwatch.models import ConfigError, ProviderError, ProviderUnsupported, Quote, Route, SearchResult
from flightwatch.sources import (DEFAULT_SOURCES, MultiSourceProvider, estimate_requests,
                                 normalize_sources, platform_search_url)


DAY = date(2026, 10, 1)


class MultiSourceTests(unittest.TestCase):
    def setUp(self):
        self.route = Route("test", "北京→上海", "BJS", "SHA", "multi", dates=(DAY,),
                           sources=("ctrip", "tongcheng"))
        self.a, self.b = Mock(), Mock()
        self.a.search.return_value = SearchResult([self.quote("600")], [])
        self.b.search.return_value = SearchResult([self.quote("500")], [])
        self.provider = MultiSourceProvider(providers={"ctrip": self.a, "tongcheng": self.b})

    def quote(self, price, **kwargs):
        return Quote("BJS", "SHA", DAY, Decimal(price), "CNY", "测试来源", **kwargs)

    def test_two_real_provider_results_remain_distinguishable(self):
        result = self.provider.search(self.route, DAY)
        self.assertEqual([q.provider for q in result.quotes], ["tongcheng", "ctrip"])
        self.assertEqual([s["lowest_price"] for s in result.sources], [600, 500])
        self.assertEqual(self.a.search.call_args.args[0].provider, "ctrip")
        self.assertEqual(self.b.search.call_args.args[0].provider, "tongcheng")

    def test_one_failure_does_not_discard_other_result_or_claim_success(self):
        self.b.search.side_effect = ProviderError("HTTP 403")
        result = self.provider.search(self.route, DAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertEqual(result.sources[1]["status"], "error")
        self.assertIsNone(result.sources[1]["lowest_price"])
        self.assertIn("同程：HTTP 403", result.warnings)

    def test_base_fare_kept_visible_but_has_no_comparable_minimum(self):
        self.b.search.return_value = SearchResult([self.quote("1", price_basis="base")], [])
        result = self.provider.search(self.route, DAY)
        self.assertEqual(result.quotes[0].price, 1)
        self.assertFalse(result.quotes[0].comparable)
        self.assertIsNone(result.sources[1]["lowest_price"])
        self.assertTrue(result.sources[1]["message"])

    def test_unexpected_parse_bug_is_isolated_and_sanitized(self):
        self.b.search.side_effect = ValueError("private raw response")
        result = self.provider.search(self.route, DAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertIn("ValueError", result.sources[1]["message"])
        self.assertNotIn("private", str(result.sources))

    def test_incompatible_market_is_distinct_from_network_error(self):
        self.b.search.side_effect = ProviderUnsupported("尚不支持此航线")
        result = self.provider.search(self.route, DAY)
        self.assertEqual(result.sources[1]["status"], "unsupported")
        self.assertIn("ly.com/flights/itinerary/oneway/BJS-SHA", result.sources[1]["search_url"])

    def test_international_tongcheng_and_fliggy_links_keep_route_and_date(self):
        route = replace(self.route, origin="SHA", destination="TYO", market="international")
        tongcheng = platform_search_url("tongcheng", route, DAY)
        fliggy = platform_search_url("fliggy", route, DAY)
        self.assertNotIn("departAirportCode", tongcheng)
        self.assertNotIn("arriveAirportCode", tongcheng)
        self.assertIn("2026-10-01", tongcheng)
        self.assertIn("sijipiao.fliggy.com/ie/", fliggy)
        self.assertIn("depCity=SHA", fliggy)
        self.assertIn("arrCity=TYO", fliggy)
        self.assertIn("depDate=2026-10-01", fliggy)

    def test_new_provider_links_are_exact_date_https_searches(self):
        route = replace(self.route, origin="SHA", destination="TYO", market="international")
        links = {name: platform_search_url(name, route, DAY) for name in (
            "trip", "skyscanner", "kayak", "momondo", "spring", "airasia",
        )}
        self.assertTrue(all(link.startswith("https://") for link in links.values()))
        self.assertTrue(all("SHA" in link.upper() and "TYO" in link.upper()
                            for link in links.values()))
        self.assertEqual(parse_qs(urlsplit(links["trip"]).query)["ddate"], ["2026-10-01"])
        self.assertIn("/261001/", urlsplit(links["skyscanner"]).path)
        self.assertIn("/2026-10-01", urlsplit(links["kayak"]).path)
        self.assertIn("/2026-10-01", urlsplit(links["momondo"]).path)
        self.assertEqual(parse_qs(urlsplit(links["spring"]).query)["FDate"], ["2026-10-01"])
        self.assertEqual(parse_qs(urlsplit(links["airasia"]).query)["departDate"], ["01/10/2026"])

    def test_wrong_city_and_currency_never_enter_comparison(self):
        self.b.search.return_value = SearchResult([
            replace(self.quote("1"), origin="CAN"), replace(self.quote("2"), currency="USD"),
            replace(self.quote("3"), departure_date=date(2026, 10, 2)),
            replace(self.quote("4"), return_date=date(2026, 10, 8))], [])
        result = self.provider.search(self.route, DAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertEqual(result.sources[1]["status"], "empty")

    def test_unselected_source_is_not_requested(self):
        self.provider.search(replace(self.route, sources=("ctrip",)), DAY)
        self.b.search.assert_not_called()

    def test_only_selected_sources_are_constructed(self):
        with patch("flightwatch.sources.make_public_provider", return_value=self.a) as factory:
            source = MultiSourceProvider()
            factory.assert_not_called()
            source.search(replace(self.route, sources=("ctrip",)), DAY)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(factory.call_args.args[0], "ctrip")

    def test_constructor_failure_is_isolated_to_one_platform(self):
        def factory(name, *args, **kwargs):
            if name == "tongcheng":
                raise ImportError("provider unavailable")
            return self.a
        with patch("flightwatch.sources.make_public_provider", side_effect=factory):
            result = MultiSourceProvider().search(self.route, DAY)
        self.assertEqual(len(result.quotes), 1)
        self.assertEqual(result.sources[1]["status"], "error")

    def test_single_source_route_uses_only_that_source(self):
        result = self.provider.search(replace(self.route, provider="tongcheng", sources=()), DAY)
        self.a.search.assert_not_called()
        self.assertEqual([s["id"] for s in result.sources], ["tongcheng"])

    def test_pre_cancelled_search_never_queries_any_source(self):
        self.provider.cancelled = lambda: True
        result = self.provider.search(self.route, DAY)
        self.a.search.assert_not_called()
        self.b.search.assert_not_called()
        self.assertEqual(result.quotes, [])

    def test_cancellation_reaches_multirequest_provider(self):
        event = threading.Event()
        self.provider.cancelled = event.is_set
        def search(*args):
            self.assertFalse(self.b.cancelled())
            event.set()
            self.assertTrue(self.b.cancelled())
            return SearchResult([], ["已停止"])
        self.b.search.side_effect = search
        self.provider.search(replace(self.route, sources=("tongcheng",)), DAY)
        self.b.search.assert_called_once()

    def test_sources_start_concurrently(self):
        barrier = threading.Barrier(2, timeout=3)
        def search(route, today):
            barrier.wait()
            return SearchResult([self.quote("500")], [])
        self.a.search.side_effect = self.b.search.side_effect = search
        result = self.provider.search(self.route, DAY)
        self.assertEqual([s["status"] for s in result.sources], ["ok", "ok"])

    def test_fast_source_progress_does_not_wait_for_first_slow_source(self):
        release = threading.Event()
        partial = threading.Event()
        snapshots = []
        def slow(*args):
            self.assertTrue(release.wait(3))
            return SearchResult([self.quote("500")], [])
        def progress(result):
            snapshots.append(result)
            if result.sources[0]["status"] == "pending" and result.sources[1]["status"] == "ok":
                partial.set()
                release.set()
        self.a.search.side_effect = slow
        self.provider.on_progress = progress
        result = self.provider.search(self.route, DAY)
        self.assertTrue(partial.is_set())
        self.assertEqual([s["id"] for s in result.sources], ["ctrip", "tongcheng"])
        self.assertTrue(all(s["status"] == "pending" for s in snapshots[0].sources))
        self.assertEqual([s["status"] for s in result.sources], ["ok", "ok"])
        self.assertTrue(all(s["elapsed_seconds"] >= 0 for s in result.sources))

    def test_all_selected_platforms_start_without_four_source_queue(self):
        names = DEFAULT_SOURCES
        barrier = threading.Barrier(len(names), timeout=3)
        providers = {}
        for name in names:
            provider = Mock()
            provider.search.side_effect = lambda *_args: (
                barrier.wait(), SearchResult([], [])
            )[1]
            providers[name] = provider
        route = replace(self.route, sources=names)
        result = MultiSourceProvider(providers=providers).search(route, DAY)
        self.assertEqual([item["id"] for item in result.sources], list(names))
        self.assertTrue(all(item["status"] == "empty" for item in result.sources))

    def test_international_calendar_request_estimates_are_batched(self):
        route = replace(self.route, market="international")
        days = [date(2026, 10, 1), date(2026, 10, 31), date(2026, 11, 1)]
        self.assertEqual(estimate_requests("tongcheng", route, days), 1)
        self.assertEqual(estimate_requests("fliggy", route, days), 2)
        self.assertEqual(estimate_requests("trip", route, days), 3)
        self.assertEqual(estimate_requests("skyscanner", route, days), 16)
        self.assertEqual(estimate_requests("spring", route, days), 4)
        self.assertEqual(estimate_requests("airasia", route, days), 12)
        self.assertEqual(estimate_requests("airasia", replace(route, market="domestic"), days), 0)

    def test_source_order_canonicalized_and_changes_reset_alert_state(self):
        self.assertEqual(normalize_sources(["tongcheng", "ctrip"]), ("ctrip", "tongcheng"))
        self.assertEqual(
            normalize_sources(["airasia", "trip", "spring", "kayak"]),
            ("trip", "kayak", "spring", "airasia"),
        )
        self.assertNotEqual(self.route.state_key(), replace(self.route, sources=("ctrip",)).state_key())
        for value in ([], ["fake"], ["ctrip", "ctrip"], "ctrip", [1]):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                normalize_sources(value)


if __name__ == "__main__":
    unittest.main()
