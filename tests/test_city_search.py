import copy
from http.client import IncompleteRead
import io
import json
import threading
import unittest
import urllib.error
from unittest.mock import patch

from flightwatch import city_search
from flightwatch.models import ProviderError


def payload(rows):
    return {"ResponseStatus": {"Ack": "Success", "Errors": []},
            "data": json.dumps(rows, ensure_ascii=False)}


# Sanitized fields from the live official endpoint, checked 2026-09-08.
KASHI = [
    {"poiType": "CITY", "cityCode": "KHG", "cityName": "喀什", "isIntl": False,
     "names": ["喀什", "中国", "KHG"], "cityCodeType": "CityCode"},
    {"poiType": "AIRPORT", "cityCode": "KHG", "cityName": "喀什", "isIntl": False,
     "airportCode": "KHG", "airportName": "喀什徕宁国际机场",
     "names": ["喀什", "喀什徕宁国际机场", "中国", "KHG"], "cityCodeType": "CityCode"},
    {"poiType": "CITY", "cityCode": "RAK", "cityName": "马拉喀什", "isIntl": True,
     "names": ["马拉喀什", "摩洛哥", "RAK"], "cityCodeType": "CityCode"},
]
PRAGUE = [
    {"poiType": "CITY", "cityCode": "PRG", "cityName": "布拉格(捷克)", "isIntl": True,
     "names": ["布拉格", None, "捷克", "PRG"], "cityCodeType": "CityCode"},
    {"poiType": "NEAR_CITY", "cityCode": "FOB", "cityName": "布拉格堡(美国)",
     "isIntl": True, "names": ["布拉格堡", "加利福尼亚州", "美国", "FOB"],
     "cityCodeType": "CityCode", "airports": [{"cityCode": "STS"}]},
]
PUDONG = [
    {"poiType": "AIRPORT", "cityCode": "SHA", "cityName": "上海", "isIntl": False,
     "airportCode": "PVG", "airportName": "浦东国际机场",
     "names": ["上海", "浦东国际机场", "中国", "PVG"], "cityCodeType": "CityCode"},
]
SHANGHAI = [
    {"poiType": "CITY", "cityCode": "SHA", "cityName": "上海", "isIntl": False,
     "names": ["上海", "中国", "SHA"], "cityCodeType": "CityCode",
     "airports": [
         {"cityCode": "SHA", "cityName": "上海", "airportCode": "PVG",
          "airportName": "浦东国际机场", "distance": 0, "isIntl": False},
         {"cityCode": "SHA", "cityName": "上海", "airportCode": "SHA",
          "airportName": "虹桥国际机场", "distance": 0, "isIntl": False},
         {"cityCode": "HGH", "cityName": "杭州", "airportCode": "HGH",
          "airportName": "萧山国际机场", "distance": 166, "isIntl": False},
     ]},
]


class CitySearchTests(unittest.TestCase):
    def setUp(self):
        with city_search._CACHE_LOCK:
            city_search._QUERY_CACHE.clear()
            city_search._CITY_CACHE.clear()
            city_search._AIRPORT_CACHE.clear()
        city_search._LAST_REQUEST = 0
        self.interval = patch.object(city_search, "REQUEST_INTERVAL", 0)
        self.interval.start()
        self.addCleanup(self.interval.stop)

    def test_live_chinese_fixture_keeps_same_code_city_and_airport_distinct(self):
        with patch.object(city_search, "_request", return_value=payload(KASHI)) as request:
            rows = city_search.search_cities("喀什")
        self.assertEqual([(row["scope"], row["code"], row["city_code"]) for row in rows],
                         [("city", "KHG", "KHG"), ("airport", "KHG", "KHG"),
                          ("city", "RAK", "RAK")])
        self.assertEqual(rows[0]["country_code"], "CN")
        self.assertEqual(rows[1]["name"], "喀什徕宁国际机场")
        self.assertIn("全部机场", rows[0]["label"])
        request.assert_called_once_with("喀什")
        self.assertEqual(city_search.cached_city("khg"), rows[0])
        self.assertEqual(city_search.cached_place("KHG", "airport", "KHG"), rows[1])

    def test_pinyin_query_and_casefold_cache_do_not_require_curated_city(self):
        with patch.object(city_search, "_request", return_value=payload(KASHI[:2])) as request:
            rows = city_search.search_cities("kashi")
            self.assertEqual(city_search.search_cities(" KASHI "), rows)
            request.assert_called_once_with("kashi")
        rows[0]["code"] = "XXX"
        self.assertEqual(city_search.cached_city("KHG")["code"], "KHG")
        self.assertEqual(city_search.search_cities("kashi")[0]["code"], "KHG")

    def test_international_country_handles_null_province_and_nearby_towns_are_not_substituted(self):
        rows = city_search._parse(payload(PRAGUE))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["name"], rows[0]["code"], rows[0]["country_code"]),
                         ("布拉格", "PRG", "CZ"))

    def test_airport_search_returns_owning_city_and_exact_airport(self):
        rows = city_search._parse(payload(PUDONG))
        self.assertEqual([(row["scope"], row["code"], row["city_code"]) for row in rows],
                         [("city", "SHA", "SHA"), ("airport", "PVG", "SHA")])

    def test_city_search_expands_only_verified_same_city_airports(self):
        rows = city_search._parse(payload(SHANGHAI))
        self.assertEqual([(row["scope"], row["code"]) for row in rows],
                         [("city", "SHA"), ("airport", "PVG"), ("airport", "SHA")])
        self.assertEqual(rows[2]["label"], "上海 · 虹桥国际机场（SHA）")

    def test_country_row_is_never_a_selectable_route(self):
        country = {"poiType": "COUNTRY", "countryName": "中国", "names": ["中国"],
                   "cities": [{"cityCode": "BJS"}, {"cityCode": "SHA"}]}
        self.assertEqual(city_search._parse(payload([country])), [])

    def test_china_regional_city_is_not_misclassified_as_mainland(self):
        hong_kong = {"poiType": "CITY", "cityCode": "HKG", "cityName": "中国香港",
                     "isIntl": True, "names": ["中国香港", "中国", "HKG"], "cityCodeType": "CityCode"}
        self.assertEqual(city_search._parse(payload([hong_kong]))[0]["market"], "international")

    def test_empty_query_never_requests_upstream_and_real_empty_result_is_cached(self):
        with patch.object(city_search, "_request", return_value=payload([])) as request:
            self.assertEqual(city_search.search_cities(" \t "), [])
            request.assert_not_called()
            self.assertEqual(city_search.search_cities("zzzzzzzzzzzz"), [])
            self.assertEqual(city_search.search_cities("zzzzzzzzzzzz"), [])
            request.assert_called_once()

    def test_invalid_queries_never_make_network_requests(self):
        with patch.object(city_search, "_request") as request:
            for value in (None, 42, {}, "a" * 61, "\ud800", "上海\x00"):
                with self.subTest(value=repr(value)):
                    with self.assertRaises(ProviderError):
                        city_search.search_cities(value)
            request.assert_not_called()

    def test_failed_response_is_not_cached_as_no_matches(self):
        with patch.object(city_search, "_request", side_effect=[ProviderError("network"), payload(KASHI)]) as request:
            with self.assertRaises(ProviderError):
                city_search.search_cities("喀什")
            self.assertTrue(city_search.search_cities("喀什"))
            self.assertEqual(request.call_count, 2)

    def test_false_like_strings_missing_schema_and_invalid_inner_json_are_rejected(self):
        row = copy.deepcopy(KASHI[0])
        row["isIntl"] = "false"
        invalid = [payload([row]), {"ResponseStatus": {"Ack": "Warning"}},
                   {"ResponseStatus": {"Ack": "Success"}, "data": []},
                   {"ResponseStatus": {"Ack": "Success"}, "data": "oops"},
                   payload([None]), payload({"a": "b"})]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ProviderError):
                    city_search._parse(value)

    def test_expired_cache_refetches_and_city_lookup_never_networks(self):
        with patch.object(city_search.time, "monotonic", return_value=10):
            city_search._remember("kashi", city_search._parse(payload(KASHI)))
        with patch.object(city_search.time, "monotonic", return_value=10 + city_search.CACHE_TTL + 1):
            self.assertIsNone(city_search.cached_city("KHG"))
            with patch.object(city_search, "_request", return_value=payload(KASHI)) as request:
                city_search.search_cities("kashi")
                request.assert_called_once()

    def test_memory_caches_are_bounded(self):
        with patch.object(city_search, "MAX_QUERY_CACHE", 2), patch.object(city_search, "MAX_CITY_CACHE", 2):
            for key, code in (("one", "AAA"), ("two", "BBB"), ("three", "CCC")):
                city_search._remember(key, [{"name": key, "code": code, "country": "中国", "market": "domestic"}])
        self.assertEqual(list(city_search._QUERY_CACHE), ["two", "three"])
        self.assertIsNone(city_search.cached_city("AAA"))
        self.assertEqual(len(city_search._CITY_CACHE), 2)

    def test_concurrent_identical_searches_share_one_remote_request(self):
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        def remote(query):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test timed out")
            return payload(KASHI)
        def run():
            try:
                results.append(city_search.search_cities("喀什"))
            except Exception as exc:
                errors.append(exc)
        with patch.object(city_search, "_request", side_effect=remote) as request:
            first = threading.Thread(target=run)
            second = threading.Thread(target=run)
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            release.set()
            first.join(2)
            second.join(2)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            request.assert_called_once()

    def test_public_request_has_empty_auth_and_decodes_both_json_layers(self):
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload(KASHI)).encode())) as opener:
            rows = city_search.search_cities("喀什")
        request = opener.call_args.args[0]
        sent = json.loads(request.data)
        self.assertEqual(json.loads(sent["data"]), {"key": "喀什"})
        self.assertEqual(sent["head"]["auth"], "")
        self.assertEqual(request.full_url, city_search.ENDPOINT)
        self.assertEqual(rows[0]["code"], "KHG")

    def test_http_block_or_html_challenge_is_an_error_not_empty_results(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                city_search.ENDPOINT, 432, "blocked", {}, None)):
            with self.assertRaisesRegex(ProviderError, "432"):
                city_search.search_cities("喀什")
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"<html>Challenge</html>")):
            with self.assertRaises(ProviderError):
                city_search.search_cities("喀什")

    def test_truncated_http_response_is_reported_and_not_cached_as_no_matches(self):
        class TruncatedResponse(io.BytesIO):
            def read(self, *args):
                raise IncompleteRead(b'{"ResponseStatus":', 100)
        with patch("urllib.request.urlopen", return_value=TruncatedResponse()):
            with self.assertRaisesRegex(ProviderError, "网络失败（IncompleteRead）"):
                city_search.search_cities("喀什")
        self.assertIsNone(city_search._query_cache_get("喀什"))
        with patch.object(city_search, "_request", return_value=payload(KASHI)) as request:
            self.assertEqual(city_search.search_cities("喀什")[0]["code"], "KHG")
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
