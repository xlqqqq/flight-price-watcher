"""Playwright progress check; all API traffic is intercepted."""
import sys
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from flightwatch.webapp import Dashboard, TZ, normalize_request, settings_for, _trip_snapshot, _combined_snapshot
from flightwatch.models import Quote, SearchResult


def main():
    static = Path(__file__).resolve().parents[1] / 'flightwatch' / 'static'
    with tempfile.TemporaryDirectory() as temp, sync_playwright() as pw:
        app = Dashboard(Path(temp))
        boot, status = app.bootstrap(), app.status()
        day = (datetime.now(TZ).date() + timedelta(days=7)).isoformat()
        form = normalize_request(dict(origin='SHA',destination='TYO', market='international',
            start_date=day,end_date=day,threshold=2000,mode='both',providers=['trip','skyscanner'],
            notify='browser',interval_minutes=10))
        route = settings_for(form, Path(temp)).routes[0]
        quote = Quote('SHA','TYO',route.dates[0],Decimal('1200'),'CNY','Trip.com',provider='trip')
        result = SearchResult([quote], [], [dict(id='trip',name='Trip.com',status='ok',quote_count=1,
            lowest_price=1200,elapsed_seconds=0.3),dict(id='skyscanner',name='Skyscanner',status='pending',quote_count=0)])
        stamp = datetime.now(TZ).isoformat()
        trip = _trip_snapshot(form,route,result=result,queried_at=stamp,in_progress=True)
        status.update(latest=_combined_snapshot(form,[trip],stamp),search_busy=True)
        def handle(request):
            path = urlsplit(request.request.url).path
            if path == '/api/bootstrap': request.fulfill(json=boot)
            elif path == '/api/status': request.fulfill(json=status)
            else:
                file = static / ('index.html' if path == '/' else path.removeprefix('/static/'))
                if file.is_file(): request.fulfill(body=file.read_bytes(),content_type={
                    '.html':'text/html','.js':'text/javascript','.css':'text/css'}[file.suffix])
                else: request.fulfill(status=404,body='missing')
        browser = pw.chromium.launch(headless=True,args=['--no-sandbox'])
        page = browser.new_page()
        errors = []
        page.on('pageerror',lambda error: errors.append(str(error)))
        page.route('http://flightwatch.test/**',handle)
        page.goto('http://flightwatch.test/')
        expect(page.locator('#monitor-detail')).to_contain_text('已完成 1/2')
        expect(page.locator('.fare-amount strong')).to_have_text('1,200')
        expect(page.locator('[data-provider=skyscanner] .source-status')).to_have_text('查询中')
        expect(page.locator('.trip-result-heading')).to_contain_text('报价更新中')
        # Final failure must preserve the successful fare and end progress.
        result.sources[1].update(status='error',message='模拟超时',elapsed_seconds=20)
        trip = _trip_snapshot(form,route,result=result,queried_at=stamp)
        status.update(latest=_combined_snapshot(form,[trip],stamp),search_busy=False)
        expect(page.locator('.trip-result-heading')).to_contain_text('查询完成')
        expect(page.locator('[data-provider=skyscanner] .source-status')).to_have_text('查询失败')
        expect(page.locator('.fare-amount strong')).to_have_text('1,200')
        # Airport-unverified city references stay in collapsed source details;
        # a cheaper reference must never replace the comparable best fare.
        result.sources.append(dict(id='qunar',name='去哪儿',status='reference',quote_count=0,
            lowest_price=None,message='机场未确认',city_reference_quotes=[dict(
                departure_date=day,price=655,currency='CNY',flight_number='9C8573',
                url=f'https://flight.qunar.com/site/oneway_list_inter.htm?searchDepartureTime={day}&filterFlightCode=9C8573')]))
        trip = _trip_snapshot(form,route,result=result,queried_at=stamp)
        status.update(latest=_combined_snapshot(form,[trip],stamp))
        card = page.locator('[data-provider=qunar]')
        expect(card.locator('.source-status')).to_have_text('仅城市参考')
        expect(card.locator('.source-reason')).to_contain_text('不参与比价或提醒')
        expect(card.locator('.city-references')).not_to_have_attribute('open','')
        expect(page.locator('.fare-amount strong')).to_have_text('1,200')
        card.locator('.city-references summary').click()
        expect(card.locator('.city-references')).to_contain_text('9C8573')
        expect(card.locator('.city-references a')).to_have_attribute('href',
            f'https://flight.qunar.com/site/oneway_list_inter.htm?searchDepartureTime={day}&filterFlightCode=9C8573')
        assert not errors, errors
        browser.close()
        print('PASS: partial fare visible, completed/total progress, pending status, final update keeps fare')


if __name__ == '__main__':
    main()
