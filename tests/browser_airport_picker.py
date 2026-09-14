"""Run with Playwright installed: python tests/browser_airport_picker.py.

All API calls are intercepted; no live fares, monitoring or messages are sent.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from flightwatch.cities import city_place, airport_place
from flightwatch.webapp import Dashboard, TZ, normalize_request


def main():
    static = Path(__file__).resolve().parents[1] / "flightwatch" / "static"
    places = [city_place("上海", "SHA", "中国", "domestic"),
              airport_place("浦东国际机场", "PVG", "上海", "SHA", "中国", "domestic"),
              airport_place("虹桥国际机场", "SHA", "上海", "SHA", "中国", "domestic"),
              city_place("东京", "TYO", "日本", "international"),
              airport_place("羽田机场", "HND", "东京", "TYO", "日本", "international"),
              airport_place("成田国际机场", "NRT", "东京", "TYO", "日本", "international")]
    with tempfile.TemporaryDirectory() as temp, sync_playwright() as pw:
        app = Dashboard(Path(temp))
        bootstrap = app.bootstrap()
        day = (datetime.now(TZ).date() + timedelta(days=7)).isoformat()
        bootstrap["defaults"] = normalize_request(dict(origin="SHA", destination="TYO",
            market="international", start_date=day, end_date=day, threshold=2000,
            mode="threshold", providers=["trip"], interval_minutes=10, notify="browser"))
        status = app.status()
        posts, errors = [], []

        def handle(route):
            url = urlsplit(route.request.url)
            if url.path == "/api/bootstrap": data = bootstrap
            elif url.path == "/api/status": data = status
            elif url.path == "/api/cities":
                query = parse_qs(url.query)["q"][0]
                owner = "SHA" if query in ("上海", "SHA", "PVG") else "TYO"
                data = dict(cities=[p for p in places if p["city_code"] == owner])
            elif url.path in ("/api/search", "/api/monitor/start"):
                posts.append(normalize_request(route.request.post_data_json))
                data = dict(ok=True)
            else:
                filename = "index.html" if url.path == "/" else url.path.removeprefix("/static/")
                target = static / filename
                if target.is_file():
                    route.fulfill(body=target.read_bytes(), content_type={".html":"text/html",
                        ".js":"text/javascript", ".css":"text/css"}[target.suffix])
                else: route.fulfill(status=404, body="missing")
                return
            route.fulfill(json=data)

        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport=dict(width=1440, height=1000))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("http://flightwatch.test/**", handle)
        page.goto("http://flightwatch.test/")
        expect(page.locator("#origin-airport option")).to_have_count(3)
        expect(page.locator("#destination-airport option")).to_have_count(3)
        expect(page.locator("#origin-airport")).to_have_value("city:SHA:SHA")
        expect(page.locator("#destination-airport")).to_have_value("city:TYO:TYO")

        def submit():
            prior = len(posts)
            page.locator("#search-button").click()
            expect(page.locator("#search-button")).to_be_enabled()
            assert len(posts) == prior + 1
            return posts[-1]

        result = submit()
        assert result["origin_scope"] == result["destination_scope"] == "city"
        # SHA city code and SHA airport must remain distinct identities.
        page.locator("#origin-airport").select_option("airport:SHA:SHA")
        page.locator("#destination-airport").select_option("airport:NRT:TYO")
        result = submit()
        assert (result["origin"], result["origin_scope"]) == ("SHA", "airport")
        assert (result["destination"], result["destination_scope"], result["destination_city_code"]) == ("NRT", "airport", "TYO")
        page.locator("#add-trip-button").click()

        page.locator("#origin-airport").select_option("city:SHA:SHA")
        expect(page.locator("#destination-airport")).to_have_value("airport:NRT:TYO")
        page.locator("#add-trip-button").click()
        result = submit()
        assert [trip["origin_scope"] for trip in result["trips"]] == ["airport", "city"]
        # Loading a saved trip and swapping endpoints retains exact scope.
        page.locator("#trip-list button", has_text="载入").first.click()
        expect(page.locator("#origin-airport")).to_have_value("airport:SHA:SHA")
        page.locator("#swap-cities").click()
        expect(page.locator("#origin-airport")).to_have_value("airport:NRT:TYO")
        expect(page.locator("#destination-airport")).to_have_value("airport:SHA:SHA")
        page.locator("#destination-clear").click()
        expect(page.locator("#destination-airport-field")).to_be_hidden()
        expect(page.locator("#origin-airport")).to_have_value("airport:NRT:TYO")
        page.locator("#trip-list button", has_text="载入").first.click()
        page.set_viewport_size(dict(width=390, height=844))
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        browser.close()
        print("PASS: all/airport choices on both sides, code collision, mixed scopes, multi-trip restore, swap, clear, mobile")


if __name__ == "__main__":
    main()
