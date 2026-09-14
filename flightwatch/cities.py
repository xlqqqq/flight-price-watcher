"""Curated Ctrip flight *city* codes for the local place selector.

Source pages checked 2026-09-08 (catalogue, not real-time route availability):
https://flights.ctrip.com/booking/hot-city-flights-sitemap.html
https://flights.ctrip.com/international/schedule/

The city names and city-code links were cross-checked against Ctrip's own
booking/schedule pages. Examples that must not be replaced with airport codes:
https://flights.ctrip.com/booking/TYO-BJS-day-1.html
https://flights.ctrip.com/booking/SIA-SEL-day-1.html
https://flights.ctrip.com/booking/PAR-OSA-day-1.html
https://flights.ctrip.com/international/schedule/LON-in.html
https://flights.ctrip.com/international/schedule/NYC-in.html
https://flights.ctrip.com/international/schedule/YTO-in.html
https://flights.ctrip.com/booking/ctu-chengdu-flights.html

Further domestic pair checks used KMG-FOC, TSN-TAO, NGB-HFE, URC-CGQ,
TNA-CGO booking pages and international/Schedule/nkg-lhw.html. These codes
select a city in Ctrip; they do not promise a particular departure airport.
``market`` selects the calendar service, not political geography: Hong Kong,
Macao and Taipei use the international/China regional service and country=中国.
This is a finite starter catalogue. Unknown input is never guessed.  Place
records deliberately distinguish a city (all airports) from one airport.  A
country is display/filter metadata only and is never a queryable place.
"""

from __future__ import annotations

import re
import unicodedata


_DOMESTIC = (
    ("北京", "BJS"),
    ("上海", "SHA"),
    ("广州", "CAN"),
    ("深圳", "SZX"),
    ("成都", "CTU"),
    ("杭州", "HGH"),
    ("武汉", "WUH"),
    ("西安", "SIA"),
    ("重庆", "CKG"),
    ("青岛", "TAO"),
    ("长沙", "CSX"),
    ("南京", "NKG"),
    ("厦门", "XMN"),
    ("昆明", "KMG"),
    ("大连", "DLC"),
    ("天津", "TSN"),
    ("郑州", "CGO"),
    ("三亚", "SYX"),
    ("济南", "TNA"),
    ("福州", "FOC"),
    ("沈阳", "SHE"),
    ("长春", "CGQ"),
    ("哈尔滨", "HRB"),
    ("乌鲁木齐", "URC"),
    ("兰州", "LHW"),
    ("南宁", "NNG"),
    ("贵阳", "KWE"),
    ("宁波", "NGB"),
    ("合肥", "HFE"),
)

_INTERNATIONAL = (
    ("香港", "HKG", "中国"),
    ("澳门", "MFM", "中国"),
    ("台北", "TPE", "中国"),
    ("东京", "TYO", "日本"),
    ("大阪", "OSA", "日本"),
    ("福冈", "FUK", "日本"),
    ("首尔", "SEL", "韩国"),
    ("曼谷", "BKK", "泰国"),
    ("普吉岛", "HKT", "泰国"),
    ("新加坡", "SIN", "新加坡"),
    ("吉隆坡", "KUL", "马来西亚"),
    ("巴厘岛", "DPS", "印度尼西亚"),
    ("河内", "HAN", "越南"),
    ("胡志明市", "SGN", "越南"),
    ("迪拜", "DXB", "阿联酋"),
    ("伦敦", "LON", "英国"),
    ("巴黎", "PAR", "法国"),
    ("纽约", "NYC", "美国"),
    ("洛杉矶", "LAX", "美国"),
    ("旧金山", "SFO", "美国"),
    ("悉尼", "SYD", "澳大利亚"),
    ("墨尔本", "MEL", "澳大利亚"),
    ("温哥华", "YVR", "加拿大"),
    ("多伦多", "YTO", "加拿大"),
)

_COUNTRY_CODES = {
    "中国": "CN", "日本": "JP", "韩国": "KR", "泰国": "TH", "新加坡": "SG",
    "马来西亚": "MY", "印度尼西亚": "ID", "越南": "VN", "阿联酋": "AE",
    "英国": "GB", "法国": "FR", "美国": "US", "澳大利亚": "AU", "加拿大": "CA",
    "捷克": "CZ", "摩洛哥": "MA",
}


def country_code_for(country: str) -> str:
    """Return a verified ISO alpha-2 code, or an empty value when unknown."""
    if not isinstance(country, str):
        return ""
    return _COUNTRY_CODES.get(unicodedata.normalize("NFKC", country).strip(), "")


def city_place(name: str, code: str, country: str, market: str) -> dict[str, str]:
    """Build the common, backwards-compatible place record for a whole city."""
    return {
        "scope": "city",
        "country": country,
        "country_code": country_code_for(country),
        "city_name": name,
        "city_code": code,
        "iata": code,
        "name": name,
        "code": code,
        "market": market,
        "label": f"{name}（{code} · 全部机场）",
    }


def airport_place(name: str, code: str, city_name: str, city_code: str,
                  country: str, market: str) -> dict[str, str]:
    """Build a selectable airport record while retaining its owning city."""
    return {
        "scope": "airport",
        "country": country,
        "country_code": country_code_for(country),
        "city_name": city_name,
        "city_code": city_code,
        "airport_name": name,
        "airport_code": code,
        "iata": code,
        "name": name,
        "code": code,
        "market": market,
        "label": f"{city_name} · {name}（{code}）",
    }


CITIES: list[dict[str, str]] = [
    city_place(name, code, "中国", "domestic") for name, code in _DOMESTIC
] + [
    city_place(name, code, country, "international")
    for name, code, country in _INTERNATIONAL
]

_BY_CODE = {city["code"]: city for city in CITIES}
_BY_NAME = {city["name"]: city for city in CITIES}
_ALIASES = {
    "北京市": "北京", "上海市": "上海", "重庆市": "重庆", "天津市": "天津",
    "中国香港": "香港", "香港特别行政区": "香港",
    "中国澳门": "澳门", "澳门特别行政区": "澳门",
    "中国台北": "台北", "台北市": "台北",
    "胡志明": "胡志明市",
}


def resolve_city(value: str) -> dict[str, str] | None:
    """Resolve a catalogue name, code, or matching ``name CODE`` label.

    Lookup accepts surrounding whitespace, lowercase codes and datalist labels
    such as ``上海 SHA`` or ``上海 (SHA)``. A mismatched ``北京 SHA`` is rejected
    rather than silently selecting the city implied by just one half. Returns a
    copy so callers cannot accidentally mutate the shared UI catalogue.
    """
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value).strip()
    if not text or len(text) > 100:
        return None
    text = " ".join(text.split())
    name = _ALIASES.get(text, text)
    city = _BY_NAME.get(name) or _BY_CODE.get(text.upper())
    if city:
        return dict(city)
    for candidate in CITIES:
        if text == unicodedata.normalize("NFKC", candidate["label"]):
            return dict(candidate)
    match = re.fullmatch(r"(.+?)\s+([A-Za-z]{3})", text)
    if not match:
        match = re.fullmatch(r"(.+?)\s*\(([A-Za-z]{3})\)", text)
    if not match:
        return None
    name = match.group(1).strip()
    name = _ALIASES.get(name, name)
    city = _BY_CODE.get(match.group(2).upper())
    if city and city["name"] == name:
        return dict(city)
    return None
