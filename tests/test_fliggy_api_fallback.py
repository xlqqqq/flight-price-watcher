from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

from flightwatch.fliggy_source import FliggyProvider
from flightwatch.models import ProviderError, Quote, Route, SearchResult
from flightwatch.sources import MultiSourceProvider, make_public_provider


DAY = date(2026, 9, 21)
ROUTE = Route('pvg-cju', '浦东济州', 'PVG', 'CJU', 'fliggy', dates=(DAY,),
    market='international', origin_scope='airport', destination_scope='airport',
    origin_city_code='SHA', destination_city_code='CJU')
QUOTE = Quote('PVG', 'CJU', DAY, Decimal(390), 'CNY', '飞猪 FlyAI',
    provider='fliggy', origin_airport='PVG', destination_airport='CJU',
    price_basis='unknown', url='https://router.feizhu.com/ws/first')


class FlyaiFallbackTests(unittest.TestCase):
    def setUp(self):
        self.provider = FliggyProvider(request_delay=0, use_flyai=True)
        self.web = patch.object(self.provider, '_search_international_airport_days').start()
        self.availability = patch('flightwatch.flyai_source.available', return_value=True).start()
        self.api = patch('flightwatch.flyai_source.search_flyai', return_value=SearchResult([QUOTE], ['税费未确认'])).start()
        self.addCleanup(patch.stopall)

    def test_official_api_fallback_keeps_price_unknown_and_actual_airports(self):
        self.web.side_effect = ProviderError('官网要求验证')
        result = self.provider._search_international_airports(ROUTE, [DAY])
        self.assertEqual(result.quotes, [QUOTE])
        self.assertFalse(result.quotes[0].comparable)
        self.assertIn('官网要求验证', result.warnings)
        self.assertIn('税费未确认', result.warnings)

    def test_completed_website_prices_do_not_consume_api_quota(self):
        full = replace(QUOTE, price_basis='total')
        self.web.return_value = SearchResult([full], [])
        self.assertEqual(self.provider._search_international_airports(ROUTE, [DAY]).quotes, [full])
        self.api.assert_not_called()

    def test_partial_website_only_requests_missing_dates_and_keeps_totals(self):
        tomorrow = DAY + timedelta(days=1)
        full = replace(QUOTE, price_basis='total')
        reference = replace(QUOTE, departure_date=tomorrow)
        self.web.return_value = SearchResult([full], ['后续日期失败'])
        self.api.return_value = SearchResult([reference], [])
        result = self.provider._search_international_airports(ROUTE, [DAY, tomorrow])
        self.api.assert_called_once_with(self.provider, ROUTE, [tomorrow])
        self.assertEqual(result.quotes, [full, reference])

    def test_missing_dependency_or_two_failures_preserve_true_failure(self):
        self.web.side_effect = ProviderError('官网验证')
        self.availability.return_value = False
        with self.assertRaisesRegex(ProviderError, '官网验证'):
            self.provider._search_international_airports(ROUTE, [DAY])
        self.api.assert_not_called()
        self.availability.return_value = True
        self.api.side_effect = ProviderError('额度不足')
        with self.assertRaisesRegex(ProviderError, '官网验证.*额度不足'):
            self.provider._search_international_airports(ROUTE, [DAY])

    def test_cancelled_website_does_not_start_another_query(self):
        self.web.side_effect = ProviderError('已取消')
        self.provider.cancelled = lambda: True
        with self.assertRaises(ProviderError):
            self.provider._search_international_airports(ROUTE, [DAY])
        self.api.assert_not_called()

    def test_factory_enables_installed_official_client(self):
        self.assertTrue(make_public_provider('fliggy').use_flyai)

    def test_source_card_link_matches_lowest_reference_even_on_later_date(self):
        later = DAY + timedelta(days=1)
        cheapest = replace(QUOTE, departure_date=later, price=Decimal(350),
                           url='https://router.feizhu.com/ws/cheapest')
        provider = Mock()
        provider.search.return_value = SearchResult([QUOTE, cheapest], [])
        route = replace(ROUTE, provider='multi', sources=('fliggy',), dates=(DAY, later))
        result = MultiSourceProvider(providers={'fliggy': provider}).search(route, DAY)
        self.assertIsNone(result.sources[0]['lowest_price'])
        self.assertEqual(result.sources[0]['search_url'], cheapest.url)


if __name__ == '__main__':
    unittest.main()
