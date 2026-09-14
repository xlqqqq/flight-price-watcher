"""Anonymous Fliggy domestic flight search, verified 2026-09-09.

Official sources:
https://www.fliggy.com/
https://g.alicdn.com/trip/rc-pc-home/1.1.26/index.js
https://g.alicdn.com/trip/flight-searchow/0.4.77/global/config-min.js
https://g.alicdn.com/trip/flight-searchow/0.4.77/mods/flight-listing/flightItem-min.js
https://g.alicdn.com/trip/flight-searchow/0.4.77/global/cabinManager-min.js

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
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult


ENDPOINT = "https://sjipiao.fliggy.com/searchow/search.htm"
_CALLBACK = "flightwatch"
_MAX_RESPONSE_BYTES = 8_000_000


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
        return "https://sjipiao.fliggy.com/flight_search_result.htm?" + urllib.parse.urlencode({
            "tripType": "0", "depCity": route.origin, "arrCity": route.destination,
            "depDate": day.isoformat(),
        })

    def _request(self, route: Route, day: date) -> dict:
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
        # These are the official search page's anonymous defaults. Conditions
        # in returned offers are still checked; member prices are not alerts.
        params = {
            "tripType": "0", "depCity": route.origin, "depCityName": "",
            "arrCity": route.destination, "arrCityName": "", "depDate": day.isoformat(),
            "searchSource": "99", "sKey": "", "qid": "", "needMemberPrice": "true",
            "_input_charset": "utf-8", "ua": "", "itemId": "", "openCb": "false",
            "callback": _CALLBACK,
        }
        request = urllib.request.Request(ENDPOINT + "?" + urllib.parse.urlencode(params), headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://sjipiao.fliggy.com/homeow/trip_flight_search.htm?" + urllib.parse.urlencode({
                "depCity": route.origin, "arrCity": route.destination,
                "depDate": day.isoformat(), "tripType": "0",
            }),
        })
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

    def search(self, route: Route, today: date) -> SearchResult:
        if route.market != "domestic":
            raise ProviderUnsupported("飞猪国际搜索会触发滑块验证，后台无法稳定匿名读取报价；可打开本次路线和日期到飞猪核价")
        if route.currency != "CNY":
            raise ProviderUnsupported("飞猪国内公开航班页只提供 CNY 价格")
        if route.stay_nights is not None:
            raise ProviderUnsupported("飞猪公开航班数据源目前仅支持单程")
        if route.travel_class != 1:
            raise ProviderUnsupported("飞猪公开航班数据源目前只比较普通经济舱")
        if route.nonstop:
            raise ProviderUnsupported("飞猪公开航班数据源目前不支持直飞筛选")
        if not all(re.fullmatch(r"[A-Z]{3}", code) for code in (route.origin, route.destination)):
            raise ProviderError("飞猪查询须使用三字母城市代码")
        dates = route.departure_dates(today)
        if not dates:
            return SearchResult([], ["配置中没有尚未过期的出发日期"])
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
            raise ProviderError("；".join(failures) or "飞猪未取得有效票价数据")
        return SearchResult(quotes, list(dict.fromkeys(warnings + failures)))

    def _parse(self, response: dict, route: Route, day: date) -> SearchResult:
        if response.get("errorMsg") or response.get("success") is False:
            raise ProviderError("飞猪未确认查询成功，未将错误响应当作有效票价")
        if "status" in response and response["status"] != 200:
            raise ProviderError("飞猪返回了查询失败状态")
        data = response.get("data")
        if not isinstance(data, dict):
            raise ProviderError("飞猪响应缺少航班数据，可能需要验证或接口已变化")
        if data.get("depCityCode") != route.origin or data.get("arrCityCode") != route.destination:
            raise ProviderError("飞猪响应城市与所选城市不一致")
        rows, airlines = data.get("flight"), data.get("aircodeNameMap")
        if not isinstance(rows, list) or not isinstance(airlines, dict):
            raise ProviderError("飞猪航班列表或航空公司字段发生变化")
        candidates, restricted, connections = [], 0, 0
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
            if not isinstance(flight_number, str) or not flight_number or not isinstance(airline_code, str):
                raise ProviderError("飞猪航班号或航空公司字段发生变化")
            airline = airlines.get(airline_code, airline_code)
            if not isinstance(airline, str):
                raise ProviderError("飞猪航空公司名称格式发生变化")
            candidates.append(Quote(
                origin=route.origin, destination=route.destination, departure_date=day,
                price=fare + oil + build, currency="CNY", source="飞猪公开航班搜索",
                airline=airline, flight_number=flight_number, stops=int(stop),
                url=self._booking_url(route, day), provider="fliggy", price_basis="total",
                price_note=(f"飞猪返回普通成人单程经济舱参考价：票价 {fare} + 税费 {oil + build} 元；"
                            "已排除返回数据注明的限年龄、会员及特殊产品，未查询中转报价；"
                            "行李及最终可售价格请到购票页确认"),
            ))
        warnings = []
        if restricted:
            warnings.append(f"{day} 飞猪已排除 {restricted} 条附预订限制、非经济舱或特殊产品报价")
        if connections:
            warnings.append(f"{day} 飞猪 {connections} 条中转组合未参与比较，当前仅核实单航班税费")
        if not candidates:
            warnings.append(f"{day} 飞猪暂无可确认的普通成人含税报价，不能据此判断售罄")
        return SearchResult([min(candidates, key=lambda quote: quote.price)] if candidates else [], warnings)
