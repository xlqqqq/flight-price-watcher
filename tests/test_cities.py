import unittest

from flightwatch.cities import CITIES, resolve_city


class CityLookupTests(unittest.TestCase):
    def test_name_code_and_datalist_label_resolve_to_same_city(self):
        for value in ("上海", "SHA", "sha", " 上海 SHA ", "上海 (SHA)", "上海（ＳＨＡ）"):
            with self.subTest(value=value):
                city = resolve_city(value)
                self.assertEqual(city["name"], "上海")
                self.assertEqual(city["code"], "SHA")

    def test_multi_airport_city_codes_are_not_airport_substitutes(self):
        expected = {"北京": "BJS", "西安": "SIA", "成都": "CTU", "东京": "TYO",
                    "大阪": "OSA", "首尔": "SEL", "伦敦": "LON", "巴黎": "PAR",
                    "纽约": "NYC", "多伦多": "YTO"}
        for name, code in expected.items():
            self.assertEqual(resolve_city(name)["code"], code)
        for airport in ("PEK", "PKX", "PVG", "NRT", "HND", "ICN", "LHR", "CDG", "JFK", "YYZ", "TFU"):
            self.assertIsNone(resolve_city(airport))

    def test_unknown_mismatched_and_nonstring_inputs_are_rejected(self):
        for value in ("北京 SHA", "Tokyo TYO", "ZZZ", "", "北京BJS", None, 123,
                      "上海 SHA trailing", "上海<script>", "x" * 101):
            with self.subTest(value=value):
                self.assertIsNone(resolve_city(value))

    def test_china_regional_cities_select_international_calendar(self):
        for name, code in (("中国香港", "HKG"), ("澳门", "MFM"), ("台北", "TPE")):
            city = resolve_city(name)
            self.assertEqual(city["code"], code)
            self.assertEqual(city["country"], "中国")
            self.assertEqual(city["market"], "international")
        self.assertEqual(resolve_city("北京")["market"], "domestic")

    def test_catalogue_has_required_coverage_and_unambiguous_codes(self):
        self.assertGreaterEqual(sum(city["market"] == "domestic" for city in CITIES), 25)
        self.assertGreaterEqual(sum(city["market"] == "international" for city in CITIES), 20)
        self.assertEqual(len({city["code"] for city in CITIES}), len(CITIES))
        required = {"scope", "country", "country_code", "city_name", "city_code",
                    "iata", "name", "code", "market", "label"}
        self.assertTrue(all(required <= set(city) for city in CITIES))
        self.assertTrue(all(city["scope"] == "city" and city["city_code"] == city["code"]
                            and "全部机场" in city["label"] for city in CITIES))

    def test_new_city_label_resolves_without_losing_scope(self):
        city = resolve_city("上海（SHA · 全部机场）")
        self.assertEqual((city["scope"], city["city_code"], city["country_code"]),
                         ("city", "SHA", "CN"))

    def test_lookup_returns_copy(self):
        result = resolve_city("上海")
        result["code"] = "XXX"
        self.assertEqual(resolve_city("上海")["code"], "SHA")


if __name__ == "__main__":
    unittest.main()
