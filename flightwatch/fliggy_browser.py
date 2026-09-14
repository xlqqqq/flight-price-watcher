"""Optional ordinary anonymous browser transport for Fliggy's public listing.

No persistent profile, account cookies, stealth patches or challenge solving.
The page issues its own normal requests; only completed, matching responses are
passed to the same strict flight parser used by the HTTP transport.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date
from urllib.parse import parse_qs, urlencode, urlsplit

from .models import ProviderError, Route, SearchResult


_BROWSER_SLOTS = threading.BoundedSemaphore(2)
_LIST_PATH = "/ie/flight_search_result_poller.do"
_CHALLENGE_MARKERS = ("_____tmd_____", "__baxia__", "punishFlowType")


def _matching_request(url: str, route: Route, day: date) -> bool:
    parsed = urlsplit(url)
    if parsed.hostname != "sijipiao.fliggy.com" or parsed.path != _LIST_PATH:
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    expected = {"tripType": "0", "searchCabinType": "1", "childPassengerNum": "0",
                "infantPassengerNum": "0", "needMemberPrice": "false"}
    if any(query.get(key) != [value] for key, value in expected.items()):
        return False
    if len(query.get("searchJourney", [])) != 1:
        return False
    try:
        journeys = json.loads(query["searchJourney"][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    return (isinstance(journeys, list) and len(journeys) == 1
            and isinstance(journeys[0], dict)
            and journeys[0].get("depCityCode") == route.city_code("origin")
            and journeys[0].get("arrCityCode") == route.city_code("destination")
            and journeys[0].get("depDate") == day.isoformat()
            and not journeys[0].get("selectedFlights"))


def _response_data(body: bytes) -> dict:
    from .fliggy_source import _VerificationRequired, parse_jsonp

    if len(body) > 8_000_000:
        raise ProviderError("飞猪浏览器航班明细超过大小限制")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ProviderError("飞猪浏览器未返回有效航班文本") from None
    if any(marker in text for marker in _CHALLENGE_MARKERS):
        raise _VerificationRequired("飞猪官网要求安全验证；普通匿名浏览器已停止，不会操作验证码")
    match = re.fullmatch(r"\s*(?:jsonp\d+|flightwatch)\((.*)\)\s*;?\s*", text, re.DOTALL)
    if not match:
        raise ProviderError("飞猪浏览器未返回预期的航班明细")
    response = parse_jsonp("flightwatch(" + match.group(1) + ")")
    if response.get("status") != 200 or response.get("success") is False:
        raise ProviderError("飞猪浏览器航班查询未成功")
    data = response.get("data")
    if not isinstance(data, dict) or type(data.get("isContinue")) is not bool:
        raise ProviderError("飞猪浏览器航班明细缺少明确完成状态")
    timestamp = data.get("timestamp")
    if (not isinstance(timestamp, int) or isinstance(timestamp, bool)
            or abs(time.time() - timestamp / 1000) > 900):
        raise ProviderError("飞猪浏览器航班响应时间无法核实或已经过期")
    return data


class FliggyBrowserSession:
    def __init__(self, provider):
        self.provider = provider
        self.playwright = self.browser = self.context = None
        self.acquired = False
        self.started = 0.0
        self.days_started = 0

    def __enter__(self):
        self.started = time.monotonic()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ProviderError("飞猪普通浏览器查询已启用，但尚未安装 Playwright 浏览器依赖") from None
        for _ in range(10):
            self.provider._check_cancelled()
            if _BROWSER_SLOTS.acquire(timeout=0.5):
                self.acquired = True
                break
        if not self.acquired:
            raise ProviderError("其他飞猪行程正在查询，本轮普通浏览器并发等待超时")
        try:
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=True, timeout=10000)
            self.context = self.browser.new_context(locale="zh-CN")
        except Exception:
            self.__exit__(None, None, None)
            raise ProviderError("飞猪普通浏览器未能启动，请检查 Chromium 安装") from None
        return self

    def __exit__(self, *args):
        for resource in (self.context, self.browser, self.playwright):
            if resource is not None:
                try:
                    resource.stop() if resource is self.playwright else resource.close()
                except Exception:
                    pass
        if self.acquired:
            _BROWSER_SLOTS.release()
            self.acquired = False

    def search_day(self, route: Route, day: date) -> SearchResult:
        from .fliggy_source import MAX_INTERNATIONAL_POLLS, _VerificationRequired

        page = self.context.new_page()
        state = {"error": None, "data": None, "requests": 0}
        deadline = time.monotonic() + 25
        if self.days_started == 0:
            deadline = min(deadline, self.started + 25)
        self.days_started += 1

        def request_handler(intercept):
            if state["error"] or state["data"] is not None:
                intercept.abort()
                return
            if not _matching_request(intercept.request.url, route, day):
                # A page's initial unrestricted-cabin request may race with its
                # economy filter. Never consume or publish that unrelated data.
                intercept.abort()
                return
            try:
                if state["requests"] >= MAX_INTERNATIONAL_POLLS:
                    raise ProviderError("飞猪普通浏览器航班查询达到单日轮询上限，未发布中间报价")
                self.provider._before_request()
                state["requests"] += 1
                intercept.continue_()
            except ProviderError as exc:
                state["error"] = exc
                intercept.abort()

        def on_response(response):
            parsed = urlsplit(response.url)
            if parsed.hostname != "sijipiao.fliggy.com":
                return
            if "_____tmd_____" in parsed.path:
                state["error"] = _VerificationRequired("飞猪官网要求安全验证；普通匿名浏览器已停止，不会操作验证码")
                return
            if parsed.path == "/ie/flight_search_result.htm":
                try:
                    body = response.body()
                    if any(marker.encode() in body for marker in _CHALLENGE_MARKERS):
                        state["error"] = _VerificationRequired("飞猪官网要求安全验证；普通匿名浏览器已停止，不会操作验证码")
                except Exception:
                    pass
                return
            if not _matching_request(response.url, route, day):
                return
            try:
                data = _response_data(response.body())
                if data["isContinue"] is False:
                    state["data"] = data
            except ProviderError as exc:
                state["error"] = exc
            except Exception:
                state["error"] = ProviderError("飞猪普通浏览器读取航班响应失败")

        page.route("**/ie/flight_search_result_poller.do?**", request_handler)
        page.on("response", on_response)
        url = self.provider._booking_url(route, day) + "&" + urlencode({
            "needMemberPrice": "false", "cabinClass": "1",
        })
        try:
            # Count the route-result HTML as one business request as well as
            # each listing poll; incidental page assets are not flight queries.
            self.provider._before_request()
            try:
                page.goto(url, wait_until="domcontentloaded",
                          timeout=max(1, min(15000, int((deadline - time.monotonic()) * 1000))))
            except Exception:
                if not state["error"] and state["data"] is None:
                    raise ProviderError("飞猪普通浏览器打开所选行程超时或失败") from None
            while state["error"] is None and state["data"] is None:
                self.provider._check_cancelled()
                if time.monotonic() >= deadline:
                    raise ProviderError("飞猪普通浏览器查询未在本轮完成，未发布中间报价")
                page.wait_for_timeout(200)
            if state["error"]:
                raise state["error"]
            return self.provider._parse_international_listing(state["data"], route, day)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("飞猪普通浏览器航班查询中断，请稍后重试") from None
        finally:
            try:
                page.close()
            except Exception:
                pass
