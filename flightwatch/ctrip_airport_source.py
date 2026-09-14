"""Ctrip's public mobile SSR flight list, inspected on 2026-09-14.

The SEO page embeds the flights actually displayed in ``__INITIAL_STATE__``.
It is a bounded public list, not an exhaustive inventory search. City search
parameters are followed by strict checks on the returned flight endpoints.
An offset URL is never evidence of the requested date: the page and each fare
must echo that date. No cookies, credentials or challenge solving are used.
"""

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import ProviderError, Quote, Route, SearchResult


_LIMIT = 8_000_000
_CODE = re.compile(r"[A-Z]{3}\Z")
_STATE = re.compile(r"\bwindow\.__INITIAL_STATE__\s*=\s*")
_NOTE = (
    "携程公开航班列表的1成人经济舱单程含税参考价，已按实际起降机场筛选；"
    "只覆盖页面本次返回的航班，缓存、行李和最终可售价请在对应航班购票页确认"
)


class _Blocked(ProviderError):
    pass


class _Cancelled(ProviderError):
    pass


class CtripAirportProvider:
    def __init__(self, timeout=20, request_delay=0, max_requests=60, *, cancelled=None):
        self.timeout = timeout
        self.request_delay = request_delay
        self.max_requests = max_requests
        self.requests_used = 0
        self._lock = threading.Lock()
        self._last_request = None
        self._blocked = threading.Event()
        self.cancelled = cancelled

    def _check_cancelled(self):
        if self.cancelled is not None and self.cancelled():
            raise _Cancelled("携程查询已取消")

    def _request(self, url):
        self._check_cancelled()
        with self._lock:
            self._check_cancelled()
            if self._blocked.is_set():
                raise _Blocked("携程本轮访问受限，已停止剩余日期请求")
            if self.requests_used >= self.max_requests:
                raise ProviderError("携程查询已达到本轮请求上限")
            if self._last_request is not None:
                wait = self.request_delay - (time.monotonic() - self._last_request)
                while wait > 0:
                    self._check_cancelled()
                    time.sleep(min(wait, 0.1))
                    wait = self.request_delay - (time.monotonic() - self._last_request)
            self._check_cancelled()
            self.requests_used += 1
            self._last_request = time.monotonic()
        # Ctrip's public SSR currently accepts HTTP/2 but rejects the same
        # anonymous request over HTTP/1.1 (432). Use the installed curl HTTP/2
        # transport without a shell or user-provided command arguments.
        curl = shutil.which("curl")
        try:
            if curl:
                response = subprocess.run(
                    [curl, "--disable", "--silent", "--show-error", "--compressed",
                     "--max-time", str(max(1, self.timeout)), "--max-filesize", str(_LIMIT),
                     "--proto", "=https", "--write-out", "\n%{http_code}", url],
                    capture_output=True, timeout=self.timeout + 2, check=False,
                )
                if response.returncode:
                    raise ProviderError("携程航班列表网络失败或响应超过大小限制")
                body, _, status = response.stdout.rpartition(b"\n")
                code = int(status) if status.isdigit() else 0
                if code in (401, 403, 429, 430, 432):
                    self._blocked.set()
                    raise _Blocked(f"携程航班列表访问受限（HTTP {code}），本轮无法取得机场报价")
                if code != 200:
                    raise ProviderError(f"携程航班列表返回 HTTP {code}，未取得有效报价")
            else:
                with urllib.request.urlopen(url, timeout=self.timeout) as response:
                    if response.geturl() != url:
                        raise ProviderError("携程航班列表跳转到其他页面，未读取报价")
                    body = response.read(_LIMIT + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429, 430, 432):
                self._blocked.set()
                raise _Blocked(
                    f"携程航班列表访问受限（HTTP {exc.code}）；安装支持 HTTP/2 的 curl 后可重试"
                ) from None
            raise ProviderError(f"携程航班列表返回 HTTP {exc.code}") from None
        except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
            raise ProviderError(f"携程航班列表网络失败（{type(exc).__name__}）") from None
        if len(body) > _LIMIT:
            raise ProviderError("携程航班列表响应超过大小限制")
        self._check_cancelled()
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderError("携程航班列表编码异常") from None

    def search(self, route: Route, today: date) -> SearchResult:
        self._check_cancelled()
        days = route.departure_dates(today)
        if not days:
            return SearchResult([], ["配置中没有尚未过期的出发日期"])
        if len(days) > 31:
            raise ProviderError("携程机场航班查询每轮最多31个出发日期")
        for side in ("origin", "destination"):
            if not _CODE.fullmatch(route.city_code(side)):
                raise ProviderError("携程机场查询缺少所属城市的三字码")
        self._blocked.clear()

        def query(day):
            self._check_cancelled()
            offset = (day - today).days
            url = ("https://m.ctrip.com/html5/flight/"
                   f"{route.city_code('origin').lower()}-{route.city_code('destination').lower()}"
                   f"-day-{offset}.html")
            return self._parse(self._request(url), route, day)

        # Fail once on an inaccessible/mismatched page before queuing a month.
        first = query(days[0])
        quotes, warnings = list(first.quotes), list(first.warnings)
        if len(days) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(days) - 1)) as pool:
                pending = iter(days[1:])
                tasks = {}

                def fill():
                    while len(tasks) < 4:
                        self._check_cancelled()
                        day = next(pending, None)
                        if day is None:
                            break
                        tasks[pool.submit(query, day)] = day

                try:
                    fill()
                    while tasks:
                        self._check_cancelled()
                        completed, _ = wait(tasks, timeout=0.1, return_when=FIRST_COMPLETED)
                        for task in completed:
                            day = tasks.pop(task)
                            try:
                                result = task.result()
                                quotes.extend(result.quotes)
                                warnings.extend(result.warnings)
                            except _Cancelled:
                                raise
                            except ProviderError as exc:
                                warnings.append(f"{day}：{exc}")
                        if self._blocked.is_set():
                            warnings.append("携程访问受限，已停止本轮剩余日期请求")
                            break
                        fill()
                finally:
                    for task in tasks:
                        task.cancel()
        self._check_cancelled()
        quotes.sort(key=lambda q: (q.departure_date, q.price, q.flight_number))
        warnings.append("携程机场筛选已接入；最低价仅代表公开航班列表本次返回的匹配航班")
        return SearchResult(quotes, list(dict.fromkeys(warnings)))

    @staticmethod
    def _parse(html: str, route: Route, day: date) -> SearchResult:
        match = _STATE.search(html)
        if not match:
            raise ProviderError("携程页面未返回公开航班数据，可能需要验证或页面结构已变化")
        try:
            state, _ = json.JSONDecoder(parse_float=Decimal).raw_decode(html[match.end():])
            data = state["listData"]
            valid = (isinstance(data, dict) and data.get("dcode") == route.city_code("origin")
                     and data.get("acode") == route.city_code("destination")
                     and data.get("ddate") == day.isoformat()
                     and type(data.get("triptype")) is int and data["triptype"] == 1)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ProviderError("携程公开航班数据结构异常，未读取含糊报价") from None
        if not valid:
            raise ProviderError("携程航班页面回显日期、城市或单程条件不一致，未使用缓存的其他行程")
        if data.get("errorMsg") or not isinstance(data.get("flights"), list):
            raise ProviderError("携程公开页面未确认航班列表查询成功")
        quotes = []
        rejected = 0
        references = 0
        for row in data["flights"]:
            try:
                item = row["flightItem"]
                legs = item["flights"]
                if not isinstance(legs, list) or not legs:
                    raise ValueError()
                for i, leg in enumerate(legs):
                    if (type(leg["segment"]) is not int or leg["segment"] != 1
                            or type(leg["sequence"]) is not int or leg["sequence"] != i + 1):
                        raise ValueError()
                    for port in (leg["dport"], leg["aport"]):
                        if not _CODE.fullmatch(port["code"]) or not _CODE.fullmatch(port["cityCode"]):
                            raise ValueError()
                    if not re.fullmatch(r"[A-Z0-9]{2}\d{1,5}[A-Z]?", leg["flightNo"]):
                        raise ValueError()
                    datetime.strptime(leg["dtime"], "%Y-%m-%d %H:%M:%S")
                    datetime.strptime(leg["atime"], "%Y-%m-%d %H:%M:%S")
                first, last = legs[0], legs[-1]
                if (first["dtime"][:10] != day.isoformat()
                        or first["dport"]["cityCode"] != route.city_code("origin")
                        or last["aport"]["cityCode"] != route.city_code("destination")):
                    raise ValueError()
                if any(route.airport_code(side) and route.airport_code(side) != actual
                       for side, actual in (("origin", first["dport"]["code"]),
                                            ("destination", last["aport"]["code"]))):
                    continue
                if route.nonstop and (len(legs) != 1 or first.get("stops")):
                    continue
                policies = item["pl"]
                if not isinstance(policies, list):
                    raise ValueError()
                for policy in policies:
                    if (policy.get("departDate") != day.isoformat() or policy.get("returnDate") != ""
                            or policy.get("className") != "经济舱" or policy.get("currency") != "CNY"
                            or policy.get("specialChannelPrice") is not False):
                        rejected += 1
                        continue
                    url = policy["jumpUrl"]
                    parsed = urllib.parse.urlsplit(url)
                    if (parsed.scheme != "https" or parsed.hostname != "m.ctrip.com"
                            or parsed.port not in (None, 443) or parsed.username or parsed.password
                            or parsed.fragment):
                        raise ValueError()
                    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                    if any(len(v) != 1 for v in params.values()):
                        raise ValueError()
                    params = {k: v[0] for k, v in params.items()}
                    total = policy.get("isContainsTax") is True
                    if route.market == "international":
                        expected = {"type": "1-0-0", "triptype": "1",
                                    "acode": route.city_code("destination"),
                                    "dcode": ".".join(l["dport"]["cityCode"] for l in legs),
                                    "ddate": ".".join(l["dtime"][:10] for l in legs),
                                    "dfltno": ".".join(l["flightNo"] for l in legs)}
                        if (parsed.path != "/html5/flight/swift/international/cabinlist"
                                or any(params.get(k) != v for k, v in expected.items())):
                            raise ValueError()
                    else:
                        # Domestic SSR prices omit tax and its opaque booking
                        # key does not independently establish passenger count.
                        # Display those as references, never as alert totals.
                        total = False
                        expected = {"dcode": route.city_code("origin"),
                                    "acode": route.city_code("destination"),
                                    "ddate": day.isoformat(), "tripType": "ONE_WAY",
                                    "regionType": "DOMESTIC"}
                        if (parsed.path != "/html5/flight/pages/middle"
                                or any(params.get(k) != v for k, v in expected.items())):
                            raise ValueError()
                    if isinstance(policy["price"], bool):
                        raise ValueError()
                    price = Decimal(str(policy["price"]))
                    if not price.is_finite() or price <= 0 or price > 10_000_000:
                        raise ValueError()
                    if not total:
                        references += 1
                    quotes.append(Quote(
                        origin=route.origin, destination=route.destination,
                        departure_date=day, price=price, currency="CNY",
                        source="携程机场航班", provider="ctrip",
                        airline=" / ".join(dict.fromkeys(l["airline"]["name"] for l in legs)),
                        flight_number=" / ".join(l["flightNo"] for l in legs),
                        origin_airport=first["dport"]["code"], destination_airport=last["aport"]["code"],
                        url=url, price_basis="total" if total else "base",
                        price_note=_NOTE if total else (
                            "携程公开航班的机场匹配参考票价；未确认完整税费及成人购票口径，"
                            "不参与含税最低价比较或微信价格提醒，请到航班购票页确认"
                        ),
                    ))
            except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
                rejected += 1
        warnings = []
        if rejected:
            warnings.append(f"{day}：携程已排除 {rejected} 条日期、航段或购票口径无法确认的报价")
        if references:
            warnings.append(f"{day}：{references} 条机场匹配票价未确认完整税费，仅作参考，不触发价格提醒")
        if not quotes:
            warnings.append(f"{day}：携程公开列表未返回所选机场的有效报价，不能据此判断无航班或售罄")
        return SearchResult(quotes, warnings)
