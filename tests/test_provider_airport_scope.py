"""No provider may substitute a city low fare for an exact airport request."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import unittest
from unittest.mock import Mock

from flightwatch.models import Quote, Route, SearchResult
from flightwatch.sources import MultiSourceProvider, estimate_requests

DAY = date(2026, 10, 15)
ROUTE = Route("airport", "浦东济州", "PVG", "CJU", "multi", dates=(DAY,),
    market="international", origin_scope="airport", destination_scope="airport",
    origin_city_code="SHA", destination_city_code="CJU")


class AirportScopeTests(unittest.TestCase):
    def test_each_new_adapter_must_return_actual_selected_airports(self):
        valid = Quote("PVG", "CJU", DAY, Decimal(380), "CNY", "测试", origin_airport="PVG", destination_airport="CJU")
        for name in ("ctrip", "qunar", "tongcheng", "fliggy"):
            provider = Mock()
            provider.search.return_value = SearchResult([
                replace(valid, price=Decimal(1), origin_airport="SHA"),
                replace(valid, price=Decimal(2), destination_airport="ICN"),
                replace(valid, price=Decimal(3), origin_airport=""), valid], [])
            found = MultiSourceProvider(providers={name: provider}).search(replace(ROUTE, sources=(name,)), DAY)
            self.assertEqual([q.price for q in found.quotes], [Decimal(380)])
            self.assertTrue(any("实际机场" in warning for warning in found.warnings))

    def test_airport_list_budgets_cover_dates_and_polls(self):
        days = [DAY, date(2026, 10, 16)]
        expected = {"ctrip":2, "qunar":10, "tongcheng":12, "fliggy":12}
        for name, count in expected.items():
            self.assertEqual(estimate_requests(name, ROUTE, days), count)


if __name__ == "__main__":
    unittest.main()
