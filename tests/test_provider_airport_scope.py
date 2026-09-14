from dataclasses import replace
from datetime import date
import unittest
from unittest.mock import patch

from flightwatch.google_flights_source import GoogleFlightsProvider
from flightwatch.models import ProviderUnsupported, Route
from flightwatch.public_source import CtripCalendarProvider
from flightwatch.qunar_source import QunarCalendarProvider
from flightwatch.tongcheng_source import TongchengProvider


DAY = date(2026, 10, 15)
ROUTE = Route(
    "airport", "浦东→首都", "PVG", "PEK", "multi", dates=(DAY,),
    origin_scope="airport", destination_scope="airport",
    origin_city_code="SHA", destination_city_code="BJS",
)


class UnsupportedAirportScopeTests(unittest.TestCase):
    def test_calendar_only_sources_reject_airport_scope_before_network(self):
        providers = [
            CtripCalendarProvider(request_delay=0),
            QunarCalendarProvider(request_delay=0),
            TongchengProvider(request_delay=0),
            GoogleFlightsProvider(request_delay=0),
        ]
        for provider in providers:
            for route in (
                ROUTE,
                replace(
                    ROUTE, origin="SHA", origin_scope="city",
                    origin_city_code="", market="international",
                ),
            ):
                with self.subTest(provider=type(provider).__name__, route=route), \
                        patch.object(provider, "_request") as request, \
                        patch("urllib.request.urlopen") as network, \
                        self.assertRaises(ProviderUnsupported):
                    provider.search(route, DAY)
                request.assert_not_called()
                network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
