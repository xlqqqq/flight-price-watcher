"""Live, anonymous Ctrip flight-city autocomplete with bounded memory caches.

Primary endpoint verified on 2026-09-08:
https://m.ctrip.com/restapi/soa2/19691/airportFuzzySearch
Public flight UI: https://m.ctrip.com/html5/flight/

Read-only live checks returned a CITY row with its zero-distance ``airports``
children for 上海 and 伦敦, and an exact AIRPORT row for query PVG.  We expose
both the owning city (all airports) and verified individual airports. COUNTRY,
nearby-city, attraction and other POI rows are never selectable.

This is an undocumented public website backend. An HTTP success is not enough:
both JSON layers and the service acknowledgement must be valid. Failures are
reported explicitly and are never cached as an empty successful lookup.
"""

from __future__ import annotations

from collections import OrderedDict
from http.client import HTTPException
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request

from .models import ProviderError
from .cities import airport_place, city_place


ENDPOINT = "https://m.ctrip.com/restapi/soa2/19691/airportFuzzySearch"
TIMEOUT = 10.0
REQUEST_INTERVAL = 0.6
CACHE_TTL = 24 * 60 * 60
MAX_QUERY_CACHE = 256
MAX_CITY_CACHE = 2048
MAX_RESPONSE_BYTES = 1_000_000
MAX_RESULTS = 50

_QUERY_CACHE: OrderedDict[str, tuple[float, list[dict[str, str]]]] = OrderedDict()
_CITY_CACHE: OrderedDict[str, tuple[float, dict[str, str]]] = OrderedDict()
_AIRPORT_CACHE: OrderedDict[str, tuple[float, dict[str, str]]] = OrderedDict()
_CACHE_LOCK = threading.RLock()
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST = 0.0


def _normalize_query(query: str) -> str:
    if not isinstance(query, str):
        raise ProviderError("城市搜索内容必须是文字")
    if len(query) > 120:
        raise ProviderError("城市搜索内容过长，请输入城市名或拼音")
    value = " ".join(unicodedata.normalize("NFKC", query).split())
    if len(value) > 60 or any(unicodedata.category(ch).startswith("C") for ch in value):
        raise ProviderError("请输入有效的城市名或拼音（最多 60 个字符）")
    return value


def _query_cache_get(key: str) -> list[dict[str, str]] | None:
    with _CACHE_LOCK:
        item = _QUERY_CACHE.get(key)
        if item is None:
            return None
        if time.monotonic() - item[0] >= CACHE_TTL:
            del _QUERY_CACHE[key]
            return None
        _QUERY_CACHE.move_to_end(key)
        return [dict(city) for city in item[1]]


def cached_city(code: str) -> dict[str, str] | None:
    """Return already retrieved city metadata without making a network request."""
    if not isinstance(code, str):
        return None
    key = code.strip().upper()
    with _CACHE_LOCK:
        item = _CITY_CACHE.get(key)
        if item is None:
            return None
        if time.monotonic() - item[0] >= CACHE_TTL:
            del _CITY_CACHE[key]
            return None
        _CITY_CACHE.move_to_end(key)
        return dict(item[1])


def cached_place(code: str, scope: str = "city", city_code: str | None = None) -> dict[str, str] | None:
    """Return a previously verified city or airport without network access."""
    if not isinstance(code, str) or scope not in {"city", "airport"}:
        return None
    key = code.strip().upper()
    cache = _CITY_CACHE if scope == "city" else _AIRPORT_CACHE
    with _CACHE_LOCK:
        item = cache.get(key)
        if item is None:
            return None
        if time.monotonic() - item[0] >= CACHE_TTL:
            del cache[key]
            return None
        place = item[1]
        if city_code is not None and place.get("city_code") != city_code.strip().upper():
            return None
        cache.move_to_end(key)
        return dict(place)


def _remember(key: str, cities: list[dict[str, str]]) -> None:
    stamp = time.monotonic()
    with _CACHE_LOCK:
        _QUERY_CACHE[key] = (stamp, [dict(city) for city in cities])
        _QUERY_CACHE.move_to_end(key)
        while len(_QUERY_CACHE) > MAX_QUERY_CACHE:
            _QUERY_CACHE.popitem(last=False)
        for city in cities:
            cache = _CITY_CACHE if city.get("scope", "city") == "city" else _AIRPORT_CACHE
            cache[city["code"]] = (stamp, dict(city))
            cache.move_to_end(city["code"])
        while len(_CITY_CACHE) > MAX_CITY_CACHE:
            _CITY_CACHE.popitem(last=False)
        while len(_AIRPORT_CACHE) > MAX_CITY_CACHE:
            _AIRPORT_CACHE.popitem(last=False)


def _request(query: str) -> dict:
    payload = {
        "data": json.dumps({"key": query}, ensure_ascii=False),
        "head": {"cid": "", "ctok": "", "cver": "1.0", "lang": "01", "sid": "",
                 "syscode": "09", "auth": "", "extension": []},
    }
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json",
                 "Accept": "application/json", "Referer": "https://m.ctrip.com/"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProviderError("城市搜索响应超过大小限制，请稍后重试")
        value = json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"城市搜索暂时不可用（HTTP {exc.code}），请稍后重试") from None
    except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as exc:
        raise ProviderError(f"城市搜索网络失败（{type(exc).__name__}），请检查网络后重试") from None
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ProviderError("城市搜索返回验证页面或无效数据，请稍后重试") from None
    if not isinstance(value, dict):
        raise ProviderError("城市搜索返回结构已变化，请稍后重试")
    return value


def _country(row: dict) -> str:
    names = row.get("names")
    if isinstance(names, list) and len(names) >= 2 and isinstance(names[-2], str):
        return names[-2].strip()
    return ""


def _clean_city_name(row: dict) -> str:
    names = row.get("names")
    if isinstance(names, list) and names and isinstance(names[0], str) and names[0].strip():
        return names[0].strip()
    value = row.get("cityName")
    return value.strip() if isinstance(value, str) else ""


def _validated_city(row: dict) -> dict[str, str] | None:
    if row.get("cityCodeType") != "CityCode":
        return None
    code, name, international = row.get("cityCode"), _clean_city_name(row), row.get("isIntl")
    if not isinstance(code, str) or not isinstance(international, bool):
        raise ProviderError("城市搜索缺少城市名称、代码或国内国际分类")
    code = code.upper().strip()
    if not re.fullmatch(r"[A-Z]{3}", code):
        return None
    if not name or len(name) > 120:
        raise ProviderError("城市搜索返回无效城市名")
    return city_place(name, code, _country(row), "international" if international else "domestic")


def _validated_airport(row: dict, city: dict[str, str], *, child: bool = False) -> dict[str, str] | None:
    code, name = row.get("airportCode"), row.get("airportName")
    owner, international = row.get("cityCode"), row.get("isIntl")
    if child and row.get("distance") != 0:
        return None
    if not isinstance(code, str) or not isinstance(name, str) or not isinstance(owner, str):
        if child:
            return None
        raise ProviderError("机场搜索缺少机场名称、代码或所属城市")
    code, owner = code.upper().strip(), owner.upper().strip()
    if not re.fullmatch(r"[A-Z]{3}", code) or owner != city["city_code"]:
        return None
    if not name.strip() or len(name) > 120:
        raise ProviderError("机场搜索返回无效机场名")
    if not isinstance(international, bool) or international != (city["market"] == "international"):
        if child:
            return None
        raise ProviderError("机场搜索的国内国际分类与所属城市不一致")
    return airport_place(name.strip(), code, city["city_name"], city["city_code"],
                         city["country"], city["market"])


def _parse(payload: dict) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        raise ProviderError("城市搜索返回结构已变化")
    status = payload.get("ResponseStatus")
    if not isinstance(status, dict) or status.get("Ack") != "Success" or status.get("Errors"):
        raise ProviderError("城市搜索未成功，未将错误响应当作无匹配城市")
    raw = payload.get("data")
    if not isinstance(raw, str):
        raise ProviderError("城市搜索缺少有效 data 字段，接口可能已变化")
    try:
        rows = json.loads(raw)
    except (ValueError, RecursionError):
        raise ProviderError("城市搜索 data 字段解析失败") from None
    if not isinstance(rows, list):
        raise ProviderError("城市搜索结果不是列表，接口可能已变化")
    if any(not isinstance(row, dict) for row in rows):
        raise ProviderError("城市搜索结果字段已变化")
    found: dict[tuple[str, str, str], dict[str, str]] = {}

    def add(place: dict[str, str] | None) -> None:
        if place is None or len(found) >= MAX_RESULTS:
            return
        identity = (place["scope"], place["code"], place["city_code"])
        found.setdefault(identity, place)

    for row in rows:
        kind = row.get("poiType")
        if kind not in {"CITY", "AIRPORT"}:
            continue  # Countries are headings only; other POIs cannot become routes.
        if "cityCodeType" not in row:
            raise ProviderError("城市搜索缺少城市代码类型，停止解析以避免误选机场")
        city = _validated_city(row)
        if city is None:
            continue
        add(city)
        if kind == "CITY":
            children = row.get("airports", [])
            if children is not None and not isinstance(children, list):
                raise ProviderError("城市搜索的机场列表结构已变化")
            for child in children or []:
                if not isinstance(child, dict):
                    raise ProviderError("城市搜索的机场列表结构已变化")
                add(_validated_airport(child, city, child=True))
        else:
            add(_validated_airport(row, city))
        if len(found) >= MAX_RESULTS:
            break
    return list(found.values())


def search_cities(query: str) -> list[dict[str, str]]:
    """Search supported flight cities by Chinese name, pinyin, or airport/code.

    Empty input returns an empty list immediately. The UI may display its own
    popular cities then. Concurrent identical lookups share their completed
    cached response; cache reads never wait behind a remote network operation.
    """
    global _LAST_REQUEST
    query = _normalize_query(query)
    if not query:
        return []
    key = query.casefold()
    cities = _query_cache_get(key)
    if cities is not None:
        return cities
    if not _REQUEST_LOCK.acquire(timeout=TIMEOUT + 2):
        raise ProviderError("城市搜索正在处理其他请求，请稍后重试")
    try:
        cities = _query_cache_get(key)
        if cities is not None:
            return cities
        remaining = REQUEST_INTERVAL - (time.monotonic() - _LAST_REQUEST)
        if remaining > 0:
            time.sleep(remaining)
        _LAST_REQUEST = time.monotonic()
        cities = _parse(_request(query))
        _remember(key, cities)
        return [dict(city) for city in cities]
    finally:
        _REQUEST_LOCK.release()
