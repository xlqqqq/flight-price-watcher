"""Live, anonymous Ctrip flight-city autocomplete with bounded memory caches.

Primary endpoint verified on 2026-09-08:
https://m.ctrip.com/restapi/soa2/19691/airportFuzzySearch
Public flight UI: https://m.ctrip.com/html5/flight/

Read-only live checks returned 喀什/kashi -> KHG, 阿勒泰 -> AAT,
布拉格 -> PRG (捷克), chengdu -> CTU (with CTU/TFU airports), 东京 -> TYO,
haikou -> HAK, 景洪 -> JHG, and airport query PVG -> city SHA. The service
returns a JSON-string ``data`` array with CITY, AIRPORT, NEAR_CITY and other
POIs. We expose CITY and AIRPORT's owning city only, never numeric CityID,
nearby-city substitutions, attractions or individual-airport codes.

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


def _remember(key: str, cities: list[dict[str, str]]) -> None:
    stamp = time.monotonic()
    with _CACHE_LOCK:
        _QUERY_CACHE[key] = (stamp, [dict(city) for city in cities])
        _QUERY_CACHE.move_to_end(key)
        while len(_QUERY_CACHE) > MAX_QUERY_CACHE:
            _QUERY_CACHE.popitem(last=False)
        for city in cities:
            _CITY_CACHE[city["code"]] = (stamp, dict(city))
            _CITY_CACHE.move_to_end(city["code"])
        while len(_CITY_CACHE) > MAX_CITY_CACHE:
            _CITY_CACHE.popitem(last=False)


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
    found: dict[str, dict[str, str]] = {}
    # CITY metadata is preferred to duplicate airport entries even when the
    # upstream result order starts with an airport (e.g. an airport-code query).
    ordered = [row for row in rows if row.get("poiType") == "CITY"]
    ordered.extend(row for row in rows if row.get("poiType") == "AIRPORT")
    for row in ordered:
        if "cityCodeType" not in row:
            raise ProviderError("城市搜索缺少城市代码类型，停止解析以避免误选机场")
        if row["cityCodeType"] != "CityCode":
            continue
        code, name, international = row.get("cityCode"), row.get("cityName"), row.get("isIntl")
        if not isinstance(code, str) or not isinstance(name, str) or not isinstance(international, bool):
            raise ProviderError("城市搜索缺少城市名称、代码或国内国际分类")
        code = code.upper().strip()
        if not re.fullmatch(r"[A-Z]{3}", code):
            continue  # The current calendar accepts three-letter flight city codes.
        if not name.strip() or len(name) > 120:
            raise ProviderError("城市搜索返回无效城市名")
        names = row.get("names")
        country = ""
        # Verified CITY names=[city, country, code] and AIRPORT names=[city,
        # airport, country, code], with occasional null province before country.
        if isinstance(names, list) and len(names) >= 2 and isinstance(names[-2], str):
            country = names[-2].strip()
        found.setdefault(code, {"name": name.strip(), "code": code, "country": country,
                                "market": "international" if international else "domestic"})
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
