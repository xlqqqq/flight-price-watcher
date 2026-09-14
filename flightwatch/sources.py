"""Query independent public providers and retain failures alongside prices."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
import threading
import urllib.parse

from .models import ConfigError, ProviderError, ProviderUnsupported, Route, SearchResult


PROVIDERS = (
    {"id": "ctrip", "name": "携程", "group": "国内旅行平台", "description": "国内/国际低价日历参考总价"},
    {"id": "tongcheng", "name": "同程", "group": "国内旅行平台", "description": "国内含税参考价；国际可按所选路线和日期直达官网核价"},
    {"id": "qunar", "name": "去哪儿", "group": "国内旅行平台", "description": "国内/国际公开低价日历；国内未含税价单独展示"},
    {"id": "fliggy", "name": "飞猪", "group": "国内旅行平台", "description": "国内普通成人含税价；国际可按所选路线和日期直达官网核价"},
    {"id": "google_flights", "name": "Google Flights", "group": "海外旅行平台", "description": "逐日搜索，1 成人经济舱单程含税参考价"},
    {"id": "kiwi", "name": "Kiwi.com", "group": "海外旅行平台", "description": "公开单程优惠，日期覆盖有限、票价口径未确认，仅供参考"},
    {"id": "ryanair", "name": "瑞安航空 Ryanair", "group": "航空公司官网", "description": "海外自营航线日历，外币折算并显示原价；税费未确认，仅供参考"},
)
DEFAULT_SOURCES = tuple(item["id"] for item in PROVIDERS)
NAMES = {item["id"]: item["name"] for item in PROVIDERS}


def _city_name(code: str) -> str:
    """Use already-known metadata only; building a link must never make a request."""
    from .cities import resolve_city
    from .city_search import cached_city
    city = cached_city(code) or resolve_city(code)
    return city["name"] if city else code


def platform_search_url(name: str, route: Route, day: date) -> str:
    """Return an official page carrying the exact route and departure date."""
    origin, destination = route.origin.upper(), route.destination.upper()
    if name == "tongcheng":
        if route.market == "international":
            params = {
                "advanced": "false", "departureCity": _city_name(origin),
                "arrivalCity": _city_name(destination), "departAirportCode": origin,
                "arriveAirportCode": destination,
                "para": f"{origin}*{destination}*{day.isoformat()}**OW*1_0_0*Y|S|C|F",
            }
            return "https://www.ly.com/iflight/book1.html?" + urllib.parse.urlencode(params)
        return f"https://www.ly.com/flights/itinerary/oneway/{origin}-{destination}?" + urllib.parse.urlencode({"date": day.isoformat()})
    if name == "fliggy":
        root = "https://sijipiao.fliggy.com/ie/flight_search_result.htm" if route.market == "international" else "https://sjipiao.fliggy.com/flight_search_result.htm"
        return root + "?" + urllib.parse.urlencode({
            "tripType": "0", "depCity": origin, "depCityName": _city_name(origin),
            "arrCity": destination, "arrCityName": _city_name(destination),
            "depDate": day.isoformat(),
        })
    return ""


def additional_platform_links(route: Route, day: date) -> list[dict[str, str]]:
    """Exact-date official search links for sites that block unattended reads.

    These are intentionally separate from price providers: opening a search is
    useful, but it is not evidence that a price was returned to this program.
    """
    origin, destination = route.origin.upper(), route.destination.upper()
    compact_day = day.strftime("%y%m%d")
    encoded_pair = f"{origin}-{destination}"
    return [
        {"id": "trip", "name": "Trip.com", "url": "https://www.trip.com/flights/showfarefirst?" + urllib.parse.urlencode({
            "dcity": origin.lower(), "acity": destination.lower(), "ddate": day.isoformat(),
            "triptype": "ow", "class": "y", "lowpricesource": "searchform",
        })},
        {"id": "skyscanner", "name": "Skyscanner", "url": f"https://www.skyscanner.com/transport/flights/{origin.lower()}/{destination.lower()}/{compact_day}/?" + urllib.parse.urlencode({
            "adultsv2": 1, "cabinclass": "economy", "rtn": 0,
        })},
        {"id": "kayak", "name": "KAYAK", "url": f"https://www.kayak.com/flights/{encoded_pair}/{day.isoformat()}?sort=bestflight_a"},
        {"id": "momondo", "name": "momondo", "url": f"https://www.momondo.com/flight-search/{encoded_pair}/{day.isoformat()}?sort=bestflight_a"},
        {"id": "spring", "name": "春秋航空", "url": f"https://flights.ch.com/{encoded_pair}.html?" + urllib.parse.urlencode({
            "FDate": day.isoformat(), "MType": 0, "SType": 0,
        })},
        {"id": "airasia", "name": "AirAsia", "url": "https://www.airasia.com/flights/search/?" + urllib.parse.urlencode({
            "origin": origin, "destination": destination, "departDate": day.isoformat(),
            "tripType": "O", "adult": 1, "child": 0, "infant": 0,
        })},
    ]


def estimate_requests(name, route, days):
    if not days:
        return 0
    if name in {"tongcheng", "fliggy"} and route.market != "domestic":
        return 0
    if name == "ctrip":
        return 1
    if name == "qunar":
        return 3
    if name == "kiwi":
        return 5
    if name == "ryanair":
        # A base airport pair plus catalogue/rate. The live catalogue may
        # expand city airports; the provider prechecks that complete matrix.
        return 0 if route.market == "domestic" else 2 + len({d.replace(day=1) for d in days})
    return len(days)


def normalize_sources(value) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ConfigError("请至少选择一个查询平台")
    if any(not isinstance(item, str) or item not in NAMES for item in value):
        raise ConfigError("包含不支持的查询平台")
    if len(value) != len(set(value)):
        raise ConfigError("查询平台不能重复")
    # Canonical ordering keeps saved alert state stable when boxes are clicked
    # in a different order.
    return tuple(item for item in DEFAULT_SOURCES if item in value)


def make_public_provider(name, timeout=30, request_delay=1.0, max_requests=60, *, cancelled=None):
    if name == "ctrip":
        from .public_source import CtripCalendarProvider
        cls = CtripCalendarProvider
    elif name == "tongcheng":
        from .tongcheng_source import TongchengProvider
        cls = TongchengProvider
    elif name == "qunar":
        from .qunar_source import QunarCalendarProvider
        cls = QunarCalendarProvider
    elif name == "fliggy":
        from .fliggy_source import FliggyProvider
        cls = FliggyProvider
    elif name == "google_flights":
        from .google_flights_source import GoogleFlightsProvider
        cls = GoogleFlightsProvider
    elif name == "kiwi":
        from .kiwi_source import KiwiDealsProvider
        cls = KiwiDealsProvider
    elif name == "ryanair":
        from .ryanair_source import RyanairProvider
        cls = RyanairProvider
    else:
        raise ConfigError("未知公开数据源")
    kwargs = {"cancelled": cancelled} if name != "ctrip" else {}
    return cls(timeout=timeout, request_delay=request_delay, max_requests=max_requests, **kwargs)


class MultiSourceProvider:
    def __init__(self, timeout=30, request_delay=1.0, max_requests=60, *, providers=None, cancelled=None):
        self.providers = providers if providers is not None else {}
        self._use_factory = providers is None
        self._options = (timeout, request_delay, max_requests)
        self.cancelled = cancelled

    def search(self, route: Route, today: date) -> SearchResult:
        names = normalize_sources((route.sources or DEFAULT_SOURCES) if route.provider == "multi"
                                  else (route.provider,))
        wanted = set(route.departure_dates(today))
        interrupted = threading.Event()

        def is_cancelled():
            return interrupted.is_set() or bool(self.cancelled and self.cancelled())

        def query(name):
            report = dict(id=name, name=NAMES[name], status="error", quote_count=0,
                          lowest_price=None, message="", search_url="")
            warnings = []
            try:
                report["search_url"] = platform_search_url(name, route, min(wanted)) if wanted else ""
                if is_cancelled():
                    raise ProviderError("监控已停止，取消后续查询")
                if name not in self.providers and self._use_factory:
                    self.providers[name] = make_public_provider(name, *self._options, cancelled=is_cancelled)
                if hasattr(self.providers[name], "cancelled"):
                    # Providers are reused across routes, but the interrupt
                    # event belongs to this search only.
                    self.providers[name].cancelled = is_cancelled
                result = self.providers[name].search(replace(route, provider=name), today)
                quotes = [replace(q, provider=name) for q in result.quotes
                          if q.origin == route.origin and q.destination == route.destination
                          and q.currency == route.currency and q.departure_date in wanted
                          and q.return_date == route.return_on(q.departure_date)]
                if len(quotes) != len(result.quotes):
                    warnings.append("已排除与航线、日期、币种或行程不匹配的报价")
                warnings.extend(result.warnings)
                comparable = [q for q in quotes if q.comparable]
                report.update(status="ok" if quotes else "empty", quote_count=len(quotes),
                    lowest_price=float(min(q.price for q in comparable)) if comparable else None)
                if quotes and quotes[0].url:
                    report["search_url"] = quotes[0].url
                if quotes and not comparable:
                    warnings.append("未获得可核实总价；展示报价但不参与最低总价及阈值提醒")
                if not quotes:
                    warnings.append("这些日期未查到有效报价，不代表没有航班")
                report["message"] = "；".join(warnings) or "查询成功"
                return quotes, warnings, report
            except ProviderUnsupported as exc:
                report.update(status="unsupported", message=str(exc))
            except ProviderError as exc:
                report["message"] = str(exc)
            except Exception as exc:
                # One changed website must not discard another provider's
                # valid result. Do not expose raw response or credentials.
                report["message"] = f"查询未完成（{type(exc).__name__}），请稍后重试"
            return [], [report["message"]], report

        quotes, warnings, reports = [], [], []
        with ThreadPoolExecutor(max_workers=min(4, len(names)), thread_name_prefix="fare-source") as pool:
            # map preserves selected provider order despite parallel requests.
            try:
                for name, (items, notes, report) in zip(names, pool.map(query, names)):
                    quotes.extend(items)
                    warnings.extend(f"{NAMES[name]}：{note}" for note in notes)
                    reports.append(report)
            except BaseException:
                interrupted.set()
                raise
        quotes.sort(key=lambda q: (q.departure_date, q.price, q.provider))
        return SearchResult(quotes, warnings, reports)
