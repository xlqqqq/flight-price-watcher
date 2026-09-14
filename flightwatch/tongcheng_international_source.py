"""Official anonymous international list adapter with exact airport filtering.

Contract: file.40017.cn/iflight/iflight/app.061a10b4a76a72d0efc4.js
Book1 calls POST ts/preload {search: ...}, then POST ts/list until done != 0.
The page's airport filters use dants[0].ac / aants[-1].ac; tax-inclusive
sorting uses tp, and flight dates use fdate. No calendar fallback is allowed.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from datetime import date
from decimal import Decimal
from http.client import HTTPException

from .models import ProviderError, Quote, SearchResult
from .tongcheng_source import _amount

_ROOT = "https://www.ly.com/miflightapi/ts/"
_LIMIT = 5_000_000
_POLLS = 4


def request_list(provider, operation, payload):
    if operation not in {"preload", "list"}:
        raise ProviderError("同程国际列表操作无效")
    provider._reserve_request()
    request = urllib.request.Request(_ROOT + operation,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={
            "User-Agent": "Mozilla/5.0", "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json", "Origin": "https://www.ly.com",
            "Referer": "https://www.ly.com/iflight/book1.html",
            # Public constant channel identifiers from the official PC JS,
            # not account credentials or user-supplied API tokens.
            "pc-token": "1", "t-token": "1",
        })
    try:
        with urllib.request.urlopen(request, timeout=provider.timeout) as response:
            body = response.read(_LIMIT + 1)
        if len(body) > _LIMIT:
            raise ProviderError("同程国际航班列表超过大小限制")
        data = json.loads(body.decode("utf-8"), parse_float=Decimal)
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"同程国际航班列表返回 HTTP {exc.code}，匿名请求未获准；未改用城市日历价") from None
    except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as exc:
        raise ProviderError(f"同程国际航班列表网络失败（{type(exc).__name__}）") from None
    except (UnicodeError, ValueError):
        raise ProviderError("同程国际航班列表返回验证页面或非 JSON 数据，未取得指定机场价格") from None
    if (not isinstance(data, dict) or type(data.get("code")) is not int
            or data["code"] != 200 or not isinstance(data.get("data"), dict)):
        raise ProviderError("同程国际航班列表未确认成功，可能需要在官网完成验证")
    return data["data"]


def allowed_airports(provider, route, side):
    if getattr(route, side + "_scope") == "airport":
        return {getattr(route, side)}
    from .cities import resolve_city
    from .city_search import cached_city, search_cities
    code = route.city_code(side)
    city = cached_city(code) or resolve_city(code)
    provider._reserve_request()
    places = search_cities(city["name"] if city else code)
    airports = {place["code"] for place in places
                if place.get("scope") == "airport" and place.get("city_code") == code}
    if not airports:
        raise ProviderError(f"同程国际查询未能核验城市 {code} 的机场目录")
    return airports


def parse_list(data, route, day, origins, destinations, url):
    if (not isinstance(data, dict) or data.get("result") is not True
            or type(data.get("done")) is not int or data["done"] != 1
            or not isinstance(data.get("res"), list)):
        raise ProviderError("同程国际航班列表尚未完整返回或结构变化，未使用未完成价格")
    quotes, skipped = [], 0
    for row in data["res"]:
        if not isinstance(row, dict):
            raise ProviderError("同程国际航班条目结构发生变化")
        departures, arrivals, dates, carriers = (row.get(key) for key in ("dants", "aants", "fdate", "acs"))
        if (not all(isinstance(value, list) and value for value in (departures, arrivals, dates, carriers))
                or not len(departures) == len(arrivals) == len(dates) == len(carriers)):
            raise ProviderError("同程国际航班缺少完整机场、日期或航段信息")
        if any(not isinstance(place, dict) or place.get("t") != "a"
               or not isinstance(place.get("ac"), str) or not re.fullmatch(r"[A-Z]{3}", place["ac"])
               for place in departures + arrivals):
            skipped += 1  # train/bus mixed products are not flight-only fares
            continue
        try:
            flight_dates = [date.fromisoformat(value) for value in dates]
        except (TypeError, ValueError):
            raise ProviderError("同程国际航段日期无效") from None
        if flight_dates[0] != day:
            raise ProviderError("同程国际列表回显出发日期与请求不一致，拒绝旧查询价格")
        start, end = departures[0]["ac"], arrivals[-1]["ac"]
        if start not in origins or end not in destinations:
            skipped += 1
            continue
        if any(flight_dates[i] > flight_dates[i+1] or arrivals[i]["ac"] != departures[i+1]["ac"]
               for i in range(len(departures)-1)):
            skipped += 1
            continue
        if row.get("isU") or row.get("isUnion") or row.get("isMember") or row.get("isStudent"):
            skipped += 1
            continue
        if row.get("currency", "CNY") != "CNY":
            raise ProviderError("同程国际列表币种不是 CNY")
        sale, total = _amount(row.get("sp"), "国际销售价 sp"), _amount(row.get("tp"), "国际含税总价 tp")
        if total < sale:
            raise ProviderError("同程国际列表总价低于票价，不能确认税费")
        if not total:
            continue
        numbers = []
        for carrier in carriers:
            if (not isinstance(carrier, dict) or not isinstance(carrier.get("an"), str)
                    or not re.fullmatch(r"[A-Z0-9]{2,3}", carrier["an"])
                    or not re.fullmatch(r"[0-9]{1,5}[A-Z]?", str(carrier.get("ac", "")))):
                raise ProviderError("同程国际列表航班号字段不完整")
            numbers.append(carrier["an"] + str(carrier["ac"]))
        quotes.append(Quote(route.origin, route.destination, day, total, "CNY", "同程国际航班列表",
            flight_number="/".join(numbers), stops=None, url=url, provider="tongcheng",
            origin_airport=start, destination_airport=end,
            price_note=f"同程匿名国际列表1成人单程参考：销售价 {sale} + 税费 {total-sale} 元；仅比较已返回且机场匹配的航班，最终可售价格、行李和经停以预订页为准"))
    warnings = []
    if skipped:
        warnings.append(f"同程国际已排除 {skipped} 条机场不匹配、非纯航班或受限条目")
    if not quotes:
        warnings.append("同程国际本次完成的列表未返回匹配机场的可比航班，不代表没有航班")
    return SearchResult([min(quotes, key=lambda q: q.price)] if quotes else [], warnings)


def search_airports(provider, route, days):
    origins = allowed_airports(provider, route, "origin")
    destinations = allowed_airports(provider, route, "destination")
    quotes, warnings = [], []
    for index, day in enumerate(days):
        provider._check_cancelled()
        params = dict(tt=0, dc=route.city_code("origin"), ac=route.city_code("destination"),
            dt=day.isoformat(), at="1900-01-01", an=1, cn=0, baby=0, cabin="Y", tit=1,
            increaseType=1, isNewVoucherAndLj=True, ext={"guid": uuid.uuid4().hex})
        try:
            # Preload is a legitimate separate public endpoint. An empty
            # preload only means no cached search: always try the actual list.
            preload = request_list(provider, "preload", {"search": params}).get("search")
            if isinstance(preload, dict) and preload.get("res") and preload.get("done") == 1:
                result = parse_list(preload, route, day, origins, destinations,
                                    provider._international_page_url(route, day))
                result.warnings.append("同程国际使用官网预加载的缓存航班列表，非实时可售保证")
            else:
                data = None
                for _ in range(_POLLS):
                    data = request_list(provider, "list", params)
                    if data.get("done") == 1:
                        break
                    if (data.get("result") is not True or data.get("done") != 0
                            or not isinstance(data.get("tid"), str) or not data["tid"] or len(data["tid"]) > 256):
                        raise ProviderError("同程国际列表未提供有效搜索会话或完成状态")
                    params.update(tid=data["tid"], done=0)
                result = parse_list(data, route, day, origins, destinations,
                                    provider._international_page_url(route, day))
            quotes.extend(result.quotes)
            warnings.extend(result.warnings)
        except ProviderError as exc:
            if index == 0:
                raise ProviderError(f"{day}：{exc}；首日失败，停止重复请求其余日期") from None
            warnings.append(f"{day}：{exc}；后续日期未查询")
            break
    return SearchResult(quotes, warnings)
