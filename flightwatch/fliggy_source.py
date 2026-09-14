"""Anonymous Fliggy domestic and international flight prices.

Official sources:
https://www.fliggy.com/
https://g.alicdn.com/trip/rc-pc-home/1.1.26/index.js
https://g.alicdn.com/trip/flight-searchow/0.4.77/global/config-min.js
https://g.alicdn.com/trip/flight-searchow/0.4.77/mods/flight-listing/flightItem-min.js
https://g.alicdn.com/trip/flight-searchow/0.4.77/global/cabinManager-min.js
https://sijipiao.fliggy.com/ie/flight_search_result.htm
https://g.alicdn.com/trip/iflight-search/1.10.94/global/conf/index-min.js
https://g.alicdn.com/trip/iflight-search/1.10.94/mods/week-price/oneway-min.js

The homepage links to sjipiao.fliggy.com/flight_search_result.htm, which
redirects to /homeow/trip_flight_search.htm. Its config and loader explicitly
use /searchow/search.htm. The verified anonymous GET needs no cookie, login,
API key, session token or generated browser fingerprint. Remote JS is only
read as documentation, never executed by this provider.

The official flight model computes tax = oilPrice + buildPrice. We add that
returned tax to cabin.price, NOT ticketPrice or conditional bestPrice.
Some real responses combine all tax in buildPrice, so the UI reports the
combined amount without assigning misleading component names. The official
cabin manager maps cabinClass=2 to economy. Age-restricted notices, membership,
application fares, special fares and flagged packages are excluded.

This is an undocumented public website backend, not a supported partner API;
prices remain booking-page references, not a guarantee of available seats.

For international routes, the official page uses the anonymous
``r.fliggy.com/cheapestCalendar/pc`` endpoint for its price calendar.  With
``calendarType=1`` and a month-start ``leaveDate``, the same anonymous endpoint
returns the selected natural month in one response.
The page template renders ``price`` as the fare and ``price + tax`` when the
"total price" display is selected.  We therefore use only rows that exactly
match the requested route and date, and add the returned tax.  One request per
natural month replaces a per-day search, so when the month view succeeds even
a year-long range needs only about thirteen requests.  The calendar does not
return a flight number or fare rules;
its result is a current platform low-price reference that must be confirmed on
the linked result page.  The detailed international listing endpoint currently
returns Fliggy's slide challenge to anonymous server requests, and this module
does not attempt to evade that access control.

The month view occasionally responds with an explicit ``success:false`` while
the same official seven-day view remains available.  Only for that explicit
service refusal do we fall back to non-overlapping seven-day windows.  Network,
schema, route, link and amount validation failures are never hidden by a
fallback.
"""

from __future__ import annotations

import gzip
import http.client
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from .models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult


ENDPOINT = "https://sjipiao.fliggy.com/searchow/search.htm"
INTERNATIONAL_CALENDAR_ENDPOINT = "https://r.fliggy.com/cheapestCalendar/pc"
_CALLBACK = "flightwatch"
_MAX_RESPONSE_BYTES = 8_000_000
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class _CalendarServiceRejected(ProviderError):
    """The endpoint explicitly declined this calendar shape, not bad data."""


def _city_query_code(route: Route, side: str) -> str:
    scope = getattr(route, f"{side}_scope")
    selected = getattr(route, side)
    if scope == "city":
        return selected
    if scope == "airport":
        city = route.city_code(side)
        if not isinstance(city, str) or not re.fullmatch(r"[A-Z]{3}", city):
            raise ProviderUnsupported(f"飞猪指定机场 {selected} 缺少有效所属城市代码")
        return city
    raise ProviderUnsupported("飞猪地点范围必须是城市全部机场或具体机场")


def parse_jsonp(text: str) -> dict:
    """Parse the documented callback as JSON data; reject extra executable text."""
    match = re.fullmatch(r"\s*flightwatch\((.*)\)\s*;?\s*", text, re.DOTALL)
    if not match:
        raise ProviderError("飞猪未返回预期的票价数据，可能是验证页或接口已变化")

    def reject_constant(value):
        raise ValueError("非法数字常量")

    try:
        result = json.loads(match.group(1), parse_float=Decimal, parse_constant=reject_constant)
    except (ValueError, RecursionError, InvalidOperation):
        raise ProviderError("飞猪返回的票价数据不是有效 JSON") from None
    if not isinstance(result, dict):
        raise ProviderError("飞猪票价数据根节点不是对象")
    return result


def _amount(value, field: str) -> Decimal:
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, Decimal)):
        raise ProviderError(f"飞猪缺少有效 {field}，不能确认含税总价")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ProviderError(f"飞猪 {field} 不是有效金额") from None
    if not amount.is_finite() or amount < 0 or amount > 1_000_000:
        raise ProviderError(f"飞猪 {field} 超出有效金额范围")
    return amount


class FliggyProvider:
    def __init__(self, timeout: float = 30, request_delay: float = 1.0,
                 max_requests: int = 60, *, cancelled: Callable[[], bool] | None = None):
        self.timeout = timeout
        self.request_delay = request_delay
        self.max_requests = max_requests
        self.requests_used = 0
        self._last_request: float | None = None
        self.cancelled = cancelled

    def _check_cancelled(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise ProviderError("飞猪查询已停止，剩余日期未查询")

    @staticmethod
    def _booking_url(route: Route, day: date) -> str:
        root = ("https://sijipiao.fliggy.com/ie/flight_search_result.htm"
                if route.market == "international"
                else "https://sjipiao.fliggy.com/flight_search_result.htm")
        return root + "?" + urllib.parse.urlencode({
            "tripType": "0", "depCity": _city_query_code(route, "origin"),
            "arrCity": _city_query_code(route, "destination"),
            "depDate": day.isoformat(),
        })

    def _before_request(self) -> None:
        """Apply the shared per-provider budget and start-time spacing."""
        self._check_cancelled()
        if self.requests_used >= self.max_requests:
            raise ProviderError("飞猪查询已达到本轮请求上限")
        if self._last_request is not None:
            wait = self.request_delay - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        self._check_cancelled()
        self.requests_used += 1
        self._last_request = time.monotonic()

    def _open_jsonp(self, request: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ProviderError("飞猪响应超过大小限制")
            if body.startswith(b"\x1f\x8b"):
                # The website sometimes compresses without Accept-Encoding.
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                    body = compressed.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise ProviderError("飞猪解压响应超过大小限制")
            return parse_jsonp(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"飞猪返回 HTTP {exc.code}，可能需要稍后重试") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, EOFError) as exc:
            raise ProviderError(f"飞猪查询网络失败（{type(exc).__name__}）") from None
        except UnicodeDecodeError:
            raise ProviderError("飞猪返回无法识别的文本，可能是验证页") from None

    def _request(self, route: Route, day: date) -> dict:
        origin = _city_query_code(route, "origin")
        destination = _city_query_code(route, "destination")
        self._before_request()
        # These are the official search page's anonymous defaults. Conditions
        # in returned offers are still checked; member prices are not alerts.
        params = {
            "tripType": "0", "depCity": origin, "depCityName": "",
            "arrCity": destination, "arrCityName": "", "depDate": day.isoformat(),
            "searchSource": "99", "sKey": "", "qid": "", "needMemberPrice": "true",
            "_input_charset": "utf-8", "ua": "", "itemId": "", "openCb": "false",
            "callback": _CALLBACK,
        }
        request = urllib.request.Request(ENDPOINT + "?" + urllib.parse.urlencode(params), headers={
            "User-Agent": _USER_AGENT,
            "Referer": "https://sjipiao.fliggy.com/homeow/trip_flight_search.htm?" + urllib.parse.urlencode({
                "depCity": origin, "arrCity": destination,
                "depDate": day.isoformat(), "tripType": "0",
            }),
        })
        return self._open_jsonp(request)

    def _request_calendar(self, route: Route, first_day: date, calendar_type: str) -> dict:
        self._before_request()
        params = {
            "bizType": "1", "searchBy": "", "depCityCode": route.origin,
            "arrCityCode": route.destination, "leaveDate": first_day.isoformat(),
            "agentId": "-1", "calendarType": calendar_type, "tripType": "0",
            "b2g": "0", "formNo": "-1", "callback": _CALLBACK,
        }
        request = urllib.request.Request(
            INTERNATIONAL_CALENDAR_ENDPOINT + "?" + urllib.parse.urlencode(params),
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/javascript, application/json, */*;q=0.8",
                "Referer": self._booking_url(route, first_day),
            },
        )
        return self._open_jsonp(request)

    def _request_month_calendar(self, route: Route, first_day: date) -> dict:
        if first_day.day != 1:
            raise ProviderError("飞猪国际月历请求必须从自然月第一天开始")
        return self._request_calendar(route, first_day, "1")

    def _request_week_calendar(self, route: Route, first_day: date) -> dict:
        return self._request_calendar(route, first_day, "0")

    def search(self, route: Route, today: date) -> SearchResult:
        if route.market not in {"domestic", "international"}:
            raise ProviderUnsupported("飞猪需要明确国内或国际航线")
        if route.currency != "CNY":
            raise ProviderUnsupported("飞猪公开航班页只提供 CNY 价格")
        if route.stay_nights is not None:
            raise ProviderUnsupported("飞猪公开航班数据源目前仅支持单程")
        if route.travel_class != 1:
            raise ProviderUnsupported("飞猪公开航班数据源目前只比较普通经济舱")
        if route.nonstop:
            raise ProviderUnsupported("飞猪公开航班数据源目前不支持直飞筛选")
        if not all(isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code)
                   for code in (route.origin, route.destination)):
            raise ProviderError("飞猪查询须使用三字母城市或机场代码")
        if route.market == "international" and (
                route.origin_scope == "airport" or route.destination_scope == "airport"):
            raise ProviderUnsupported(
                "飞猪国际最低价日历不返回实际机场，无法核实指定机场；本次未发起网络查询"
            )
        # Validate scope and required owning-city metadata before any request.
        _city_query_code(route, "origin")
        _city_query_code(route, "destination")
        dates = route.departure_dates(today)
        if not dates:
            return SearchResult([], ["配置中没有尚未过期的出发日期"])
        if route.market == "international":
            return self._search_international(route, dates)
        return self._search_domestic(route, dates)

    def _search_domestic(self, route: Route, dates: list[date]) -> SearchResult:
        quotes, warnings, failures = [], [], []
        succeeded = 0
        for day in dates:
            try:
                self._check_cancelled()
            except ProviderError as exc:
                failures.append(str(exc))
                break
            if self.requests_used >= self.max_requests:
                failures.append("飞猪查询已达到本轮请求上限，部分日期未查询")
                break
            try:
                result = self._parse(self._request(route, day), route, day)
                succeeded += 1
                quotes.extend(result.quotes)
                warnings.extend(result.warnings)
            except ProviderError as exc:
                failures.append(f"{day}：{exc}")
                if not succeeded:
                    failures.append("首日查询未成功，暂停该平台剩余日期，避免重复等待；下轮可重试")
                    break
        if not succeeded:
            raise ProviderError("；".join(failures) or "飞猪未取得有效票价数据")
        return SearchResult(quotes, list(dict.fromkeys(warnings + failures)))

    def _search_international(self, route: Route, dates: list[date]) -> SearchResult:
        months: dict[date, set[date]] = {}
        for day in dates:
            months.setdefault(day.replace(day=1), set()).add(day)
        quotes, warnings, failures = [], [], []
        succeeded = 0
        for first_day, wanted in sorted(months.items()):
            last_day = first_day.replace(day=monthrange(first_day.year, first_day.month)[1])
            try:
                self._check_cancelled()
            except ProviderError as exc:
                failures.append(str(exc))
                break
            if self.requests_used >= self.max_requests:
                failures.append("飞猪查询已达到本轮请求上限，部分日期未查询")
                break
            try:
                try:
                    result = self._parse_month_calendar(
                        self._request_month_calendar(route, first_day), route, wanted, first_day
                    )
                except _CalendarServiceRejected as rejected:
                    warnings.append(
                        f"{first_day:%Y-%m} 飞猪月度日历暂时拒绝本次请求，已自动改用七日低价日历"
                    )
                    try:
                        result = self._search_week_fallback(route, wanted)
                    except ProviderError as fallback_error:
                        raise ProviderError(
                            f"{rejected}；七日低价日历降级也未完成：{fallback_error}"
                        ) from None
                succeeded += 1
                quotes.extend(result.quotes)
                warnings.extend(result.warnings)
            except ProviderError as exc:
                failures.append(f"{first_day} 至 {last_day}：{exc}")
        if not succeeded:
            raise ProviderError("；".join(failures) or "飞猪国际最低价日历未取得有效数据")
        warnings.append(
            "飞猪国际报价来自官网最低价日历；该接口不返回具体航班号或票价规则，"
            "点击购票页后请确认当前可售价格及行李条件"
        )
        return SearchResult(quotes, list(dict.fromkeys(warnings + failures)))

    def _search_week_fallback(self, route: Route, wanted: set[date]) -> SearchResult:
        pending = set(wanted)
        quotes, warnings, failures = [], [], []
        succeeded = 0
        while pending:
            first_day = min(pending)
            last_day = first_day + timedelta(days=6)
            window = {day for day in pending if first_day <= day <= last_day}
            try:
                self._check_cancelled()
            except ProviderError as exc:
                failures.append(str(exc))
                break
            if self.requests_used >= self.max_requests:
                failures.append("飞猪查询已达到本轮请求上限，部分日期未查询")
                break
            try:
                result = self._parse_week_calendar(
                    self._request_week_calendar(route, first_day), route, window, first_day
                )
                succeeded += 1
                quotes.extend(result.quotes)
                warnings.extend(result.warnings)
            except ProviderError as exc:
                failures.append(f"{first_day} 至 {last_day}：{exc}")
            pending.difference_update(window)
        if not succeeded:
            raise ProviderError("；".join(failures) or "飞猪七日低价日历未取得有效数据")
        return SearchResult(quotes, list(dict.fromkeys(warnings + failures)))

    def _parse_month_calendar(
        self, response: dict, route: Route, wanted: set[date], first_day: date
    ) -> SearchResult:
        if first_day.day != 1:
            raise ProviderError("飞猪国际月历解析必须使用自然月第一天")
        return self._parse_calendar(
            response, route, wanted,
            lambda departure: (departure.year == first_day.year
                               and departure.month == first_day.month),
            "月度", "月份",
        )

    def _parse_week_calendar(
        self, response: dict, route: Route, wanted: set[date], first_day: date
    ) -> SearchResult:
        last_day = first_day + timedelta(days=6)
        return self._parse_calendar(
            response, route, wanted,
            lambda departure: first_day <= departure <= last_day,
            "七日", "七日窗口",
        )

    def _parse_calendar(
        self, response: dict, route: Route, wanted: set[date], date_is_valid: Callable[[date], bool],
        calendar_name: str, date_scope_name: str,
    ) -> SearchResult:
        if response.get("success") is False or response.get("failure") is True:
            # The remote message is neither needed to choose this narrow
            # fallback nor safe to echo without a documented size/format.
            raise _CalendarServiceRejected("飞猪国际最低价日历明确拒绝本次请求")
        if response.get("success") is not True:
            raise ProviderError("飞猪国际最低价日历缺少明确成功状态")
        rows = response.get("result")
        if not isinstance(rows, list):
            raise ProviderError("飞猪国际最低价日历字段发生变化")
        by_day: dict[date, Quote] = {}
        seen: set[date] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ProviderError("飞猪国际最低价日历包含无效条目")
            if row.get("depCityCode") != route.origin or row.get("arrCityCode") != route.destination:
                raise ProviderError("飞猪国际最低价日历城市与所选城市不一致")
            raw_day = row.get("leaveDate")
            try:
                departure = date.fromisoformat(raw_day) if isinstance(raw_day, str) else None
            except ValueError:
                departure = None
            if departure is None or not date_is_valid(departure):
                raise ProviderError(f"飞猪国际最低价日历日期与请求{date_scope_name}不一致")
            if departure in seen:
                raise ProviderError("飞猪国际最低价日历返回重复日期")
            seen.add(departure)
            fare = _amount(row.get("price"), "国际成人票价 price")
            tax = _amount(row.get("tax"), "国际税费 tax")
            raw_url = row.get("url")
            if not isinstance(raw_url, str) or not raw_url.strip():
                raise ProviderError("飞猪国际最低价日历缺少购票链接")
            link = "https:" + raw_url if raw_url.startswith("//") else raw_url
            parsed = urllib.parse.urlsplit(link)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

            def one_of(*names: str) -> str | None:
                values = []
                for name in names:
                    values.extend(query.get(name, []))
                return values[0] if len(values) == 1 else None

            if (parsed.scheme != "https" or parsed.hostname != "sijipiao.fliggy.com"
                    or parsed.path != "/ie/flight_search_result.htm"
                    or one_of("depCityCode", "depCity") != route.origin
                    or one_of("arrCityCode", "arrCity") != route.destination
                    or one_of("depDate") != departure.isoformat()
                    or one_of("tripType") != "0"):
                raise ProviderError("飞猪国际最低价日历购票链接与路线或日期不一致")
            if departure not in wanted or fare == 0:
                continue
            total = fare + tax
            if total <= 0:
                continue
            by_day[departure] = Quote(
                origin=route.origin, destination=route.destination,
                departure_date=departure, price=total, currency="CNY",
                source="飞猪国际最低价日历", url=self._booking_url(route, departure),
                provider="fliggy", price_basis="total",
                price_note=(f"飞猪官网{calendar_name}最低价日历参考总价：票价 {fare} + 税费 {tax} 元；"
                            "日历接口不返回具体航班、会员限制或行李条件，"
                            "最终可售价格请到飞猪国际机票页确认"),
            )
        warnings = [
            f"{day} 飞猪国际最低价日历暂无正数报价，不能据此判断售罄"
            for day in sorted(wanted - set(by_day))
        ]
        return SearchResult([by_day[day] for day in sorted(by_day)], warnings)

    def _parse(self, response: dict, route: Route, day: date) -> SearchResult:
        if response.get("errorMsg") or response.get("success") is False:
            raise ProviderError("飞猪未确认查询成功，未将错误响应当作有效票价")
        if "status" in response and response["status"] != 200:
            raise ProviderError("飞猪返回了查询失败状态")
        data = response.get("data")
        if not isinstance(data, dict):
            raise ProviderError("飞猪响应缺少航班数据，可能需要验证或接口已变化")
        origin_city = _city_query_code(route, "origin")
        destination_city = _city_query_code(route, "destination")
        if data.get("depCityCode") != origin_city or data.get("arrCityCode") != destination_city:
            raise ProviderError("飞猪响应城市与所选城市不一致")
        rows, airlines = data.get("flight"), data.get("aircodeNameMap")
        if not isinstance(rows, list) or not isinstance(airlines, dict):
            raise ProviderError("飞猪航班列表或航空公司字段发生变化")
        candidates, restricted, connections, airport_mismatches = [], 0, 0, 0
        for row in rows:
            if not isinstance(row, dict):
                raise ProviderError("飞猪航班字段发生变化")
            transfer = row.get("isTransfer", False)
            if transfer is True:
                connections += 1
                continue
            if transfer is not False:
                raise ProviderError("飞猪中转标记格式发生变化")
            try:
                departure = datetime.strptime(row["depTime"], "%Y-%m-%d %H:%M")
            except (KeyError, TypeError, ValueError):
                raise ProviderError("飞猪航班出发时间格式发生变化") from None
            if departure.date() != day:
                raise ProviderError("飞猪航班日期与所选日期不一致")
            cabin = row.get("cabin")
            if not isinstance(cabin, dict):
                raise ProviderError("飞猪航班缺少舱位报价")
            notices = cabin.get("notices")
            if not isinstance(notices, list):
                raise ProviderError("飞猪报价缺少预订限制信息，不能确认普通成人适用")
            if (notices or cabin.get("cabinClass") != 2 or cabin.get("priceType") != 0
                    or cabin.get("sprodType") != 0
                    or any(cabin.get(flag) is not False for flag in
                           ("hasMemberPrice", "vip", "specialSale", "fSpecialSale"))):
                restricted += 1
                continue
            fare = _amount(cabin.get("price"), "成人票价 cabin.price")
            oil = _amount(row.get("oilPrice"), "税费 oilPrice")
            build = _amount(row.get("buildPrice"), "税费 buildPrice")
            if not fare:
                continue
            stop = _amount(row.get("stop"), "经停次数")
            if stop != stop.to_integral_value() or stop > 20:
                raise ProviderError("飞猪经停次数无效")
            flight_number = row.get("flightNo")
            airline_code = row.get("airlineCode")
            origin_airport = row.get("depAirport")
            destination_airport = row.get("arrAirport")
            if not isinstance(flight_number, str) or not flight_number or not isinstance(airline_code, str):
                raise ProviderError("飞猪航班号或航空公司字段发生变化")
            if (not isinstance(origin_airport, str) or not re.fullmatch(r"[A-Z]{3}", origin_airport)
                    or not isinstance(destination_airport, str)
                    or not re.fullmatch(r"[A-Z]{3}", destination_airport)):
                raise ProviderError("飞猪实际起降机场字段发生变化")
            if ((route.origin_scope == "airport" and origin_airport != route.origin)
                    or (route.destination_scope == "airport"
                        and destination_airport != route.destination)):
                airport_mismatches += 1
                continue
            airline = airlines.get(airline_code, airline_code)
            if not isinstance(airline, str):
                raise ProviderError("飞猪航空公司名称格式发生变化")
            candidates.append(Quote(
                origin=route.origin, destination=route.destination, departure_date=day,
                price=fare + oil + build, currency="CNY", source="飞猪公开航班搜索",
                airline=airline, flight_number=flight_number, stops=int(stop),
                url=self._booking_url(route, day), provider="fliggy", price_basis="total",
                origin_airport=origin_airport, destination_airport=destination_airport,
                price_note=(f"飞猪返回普通成人单程经济舱参考价：票价 {fare} + 税费 {oil + build} 元；"
                            "已排除返回数据注明的限年龄、会员及特殊产品，未查询中转报价；"
                            "行李及最终可售价格请到购票页确认"),
            ))
        warnings = []
        if restricted:
            warnings.append(f"{day} 飞猪已排除 {restricted} 条附预订限制、非经济舱或特殊产品报价")
        if connections:
            warnings.append(f"{day} 飞猪 {connections} 条中转组合未参与比较，当前仅核实单航班税费")
        if airport_mismatches:
            warnings.append(
                f"{day} 飞猪已排除 {airport_mismatches} 条实际起降机场与所选机场不一致的报价"
            )
        if not candidates:
            warnings.append(f"{day} 飞猪暂无可确认的普通成人含税报价，不能据此判断售罄")
        return SearchResult([min(candidates, key=lambda quote: quote.price)] if candidates else [], warnings)
