"""No browser install or network is needed for transport guard regressions."""

import json
import time
import unittest
from datetime import date
from unittest.mock import MagicMock
from urllib.parse import urlencode

from flightwatch.fliggy_browser import FliggyBrowserSession, _matching_request, _response_data
from flightwatch.fliggy_source import FliggyProvider
from flightwatch.models import ProviderError, Route


DAY = date(2026, 9, 21)
ROUTE = Route("pvg-cju", "浦东济州", "PVG", "CJU", "multi", dates=(DAY,),
              market="international", origin_scope="airport", destination_scope="airport",
              origin_city_code="SHA", destination_city_code="CJU")


def query_url(**changes):
    params = {"tripType": "0", "searchCabinType": "1", "childPassengerNum": "0",
              "infantPassengerNum": "0", "needMemberPrice": "false", "callback": "jsonp742",
              "searchJourney": json.dumps([{"depCityCode": "SHA", "arrCityCode": "CJU",
                  "depDate": str(DAY), "selectedFlights": []}])}
    params.update(changes)
    return "https://sijipiao.fliggy.com/ie/flight_search_result_poller.do?" + urlencode(params)


def body(complete=False, **changes):
    data = {"timestamp": int(time.time() * 1000), "isContinue": not complete, "flightItems": []}
    data.update(changes)
    return ("jsonp742(" + json.dumps({"status": 200, "success": True, "data": data}) + ");").encode()


class FliggyBrowserTests(unittest.TestCase):
    def test_request_matches_owner_city_economy_and_adult_only(self):
        self.assertTrue(_matching_request(query_url(), ROUTE, DAY))
        for change in ({"searchCabinType": "0"}, {"childPassengerNum": "1"},
                       {"needMemberPrice": "true"}, {"tripType": "1"},
                       {"searchJourney": "[]"}):
            with self.subTest(change=change):
                self.assertFalse(_matching_request(query_url(**change), ROUTE, DAY))
        self.assertFalse(_matching_request(query_url().replace("2026-09-21", "2026-09-22"), ROUTE, DAY))
        self.assertFalse(_matching_request(query_url().replace("sijipiao.fliggy.com", "example.com"), ROUTE, DAY))
        self.assertFalse(_matching_request(query_url() + "&searchJourney=[]", ROUTE, DAY))

    def test_challenge_is_reported_without_executing_scripts(self):
        with self.assertRaisesRegex(ProviderError, "安全验证"):
            _response_data(b'<script>location="/_____tmd_____/punish"</script>')

    def test_dynamic_jsonp_is_data_only_and_requires_current_timestamp(self):
        self.assertFalse(_response_data(body(complete=True))["isContinue"])
        for value in (None, True, 1, int((time.time() - 901) * 1000)):
            with self.subTest(value=value), self.assertRaisesRegex(ProviderError, "时间"):
                _response_data(body(timestamp=value))
        with self.assertRaises(ProviderError):
            _response_data(body() + b'alert(1);')

    def fake_session(self, first, final=None):
        provider = FliggyProvider(request_delay=0)
        session = FliggyBrowserSession(provider)
        session.started = time.monotonic()
        session.context = MagicMock()
        page = session.context.new_page.return_value

        def emit(payload):
            response = MagicMock()
            response.url = query_url()
            response.body.return_value = payload
            page.on.call_args.args[1](response)

        page.goto.side_effect = lambda *args, **kwargs: emit(first)
        if final is not None:
            page.wait_for_timeout.side_effect = lambda *args, **kwargs: emit(final)
        return session, page

    def test_browser_waits_for_final_snapshot_before_parser(self):
        session, page = self.fake_session(body(), body(complete=True))
        result = session.search_day(ROUTE, DAY)
        self.assertEqual(result.quotes, [])
        self.assertEqual(page.wait_for_timeout.call_count, 1)
        self.assertEqual(session.provider.requests_used, 1)  # The route HTML; fake responses do not issue polls.
        page.close.assert_called_once()

    def test_incomplete_snapshot_never_becomes_a_result_on_timeout(self):
        session, page = self.fake_session(body())
        session.started = time.monotonic() - 30
        with self.assertRaisesRegex(ProviderError, "未在本轮完成"):
            session.search_day(ROUTE, DAY)
        page.close.assert_called_once()

    def test_browser_stops_when_initial_response_is_challenge(self):
        session, page = self.fake_session(b'<script>location="/_____tmd_____/punish"</script>')
        with self.assertRaisesRegex(ProviderError, "安全验证"):
            session.search_day(ROUTE, DAY)
        page.wait_for_timeout.assert_not_called()
        page.close.assert_called_once()
