from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, timezone, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import ConfigError, Route


@dataclass(frozen=True)
class Settings:
    routes: tuple[Route, ...]
    timezone: tzinfo
    interval_minutes: int
    digest_hours: int
    repeat_hours: int
    min_drop: Decimal
    timeout_seconds: int
    request_delay_seconds: float
    max_requests_per_cycle: int
    database: Path


def get_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows does not ship an IANA database. Shanghai is UTC+8 for all
        # future departures this app supports; no extra package is necessary.
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), "Asia/Shanghai")
        raise


def load_env(path: Path) -> None:
    """Read KEY=VALUE without shell execution; existing environment wins."""
    if not path.exists():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ConfigError(f".env 第 {number} 行格式错误，应为 KEY=VALUE")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def integer(value, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{name} 必须是 {low}～{high} 的整数")
    return value


def money(value, name: str, zero: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ConfigError(f"{name} 必须是有效金额") from None
    if not result.is_finite() or result < 0 or (not zero and result == 0):
        raise ConfigError(f"{name} 必须是{'非负数' if zero else '正数'}")
    return result


def reject_unknown(raw: dict, known: set[str], name: str) -> None:
    if set(raw) - known:
        raise ConfigError(f"{name} 含不支持的配置项：{', '.join(sorted(set(raw) - known))}")


def load_config(path: Path) -> Settings:
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError:
        raise ConfigError(f"找不到配置文件：{path}") from None
    except tomllib.TOMLDecodeError:
        raise ConfigError("config.toml 格式错误，请检查引号、数字和表名") from None
    reject_unknown(raw, {"monitor", "routes"}, "配置文件")
    monitor = raw.get("monitor", {})
    if not isinstance(monitor, dict):
        raise ConfigError("monitor 必须是 TOML 表")
    reject_unknown(monitor, {
        "timezone", "interval_minutes", "digest_hours", "repeat_hours", "min_drop",
        "timeout_seconds", "request_delay_seconds", "max_requests_per_cycle", "database",
    }, "monitor")
    try:
        tz = get_timezone(monitor.get("timezone", "Asia/Shanghai"))
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ConfigError("timezone 无效；推荐 Asia/Shanghai") from None
    delay = monitor.get("request_delay_seconds", 1.0)
    if type(delay) not in (int, float) or not 0.5 <= delay <= 60:
        raise ConfigError("request_delay_seconds 必须在 0.5～60 之间")
    entries = raw.get("routes", [])
    if not isinstance(entries, list) or not 1 <= len(entries) <= 30:
        raise ConfigError("请配置 1～30 条 [[routes]] 航线")
    routes, ids = [], set()
    for item in entries:
        if not isinstance(item, dict):
            raise ConfigError("每条 routes 必须是 TOML 表")
        reject_unknown(item, {
            "id", "name", "origin", "destination", "provider", "currency", "mode",
            "threshold", "dates", "start_offset_days", "end_offset_days", "stay_nights",
            "nonstop", "travel_class", "market", "sources",
            "origin_scope", "destination_scope", "origin_city_code", "destination_city_code",
            "origin_label", "destination_label",
        }, "routes")
        rid = item.get("id", "")
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid) or rid in ids:
            raise ConfigError("每条航线 id 必须唯一，且只含 1～64 位字母、数字、下划线或短横线")
        ids.add(rid)
        codes = []
        for field in ("origin", "destination"):
            code = item.get(field, "")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z]{3}(,[A-Za-z]{3})*", code):
                raise ConfigError(f"{rid}.{field} 应为机场三字码；多个机场用英文逗号分隔")
            codes.append(code.upper())
        if set(codes[0].split(",")) & set(codes[1].split(",")):
            raise ConfigError(f"{rid} 的出发和到达机场不能重叠")
        locations = []
        for field, code in zip(("origin", "destination"), codes):
            scope = item.get(f"{field}_scope", "city")
            if scope not in {"city", "airport"}:
                raise ConfigError(f"{rid}.{field}_scope 仅支持 city 或 airport")
            city_code = item.get(f"{field}_city_code", code if scope == "city" and "," not in code else "")
            if not isinstance(city_code, str) or (city_code and not re.fullmatch(r"[A-Za-z]{3}", city_code)):
                raise ConfigError(f"{rid}.{field}_city_code 应为所属城市三字码")
            city_code = city_code.upper()
            if scope == "city" and "," not in code and city_code != code:
                raise ConfigError(f"{rid}.{field} 为城市范围时必须与 {field}_city_code 一致")
            if scope == "airport" and ("," in code or not city_code):
                raise ConfigError(f"{rid}.{field} 为机场范围时必须提供单个机场码和所属城市码")
            label = item.get(f"{field}_label", "")
            if not isinstance(label, str) or len(label) > 160 or any(ord(ch) < 32 for ch in label):
                raise ConfigError(f"{rid}.{field}_label 必须是最多 160 字的安全地点名称")
            locations.append((scope, city_code, label.strip()))
        if locations[0][1] and locations[0][1] == locations[1][1]:
            raise ConfigError(f"{rid} 的出发和到达地点不能属于同一城市")
        from .sources import DEFAULT_SOURCES, NAMES, normalize_sources
        provider = item.get("provider", "multi")
        if provider not in {*NAMES, "multi", "serpapi"}:
            raise ConfigError(f"{rid}.provider 不支持；免 token 多平台请选择 multi")
        sources = normalize_sources(item.get("sources", DEFAULT_SOURCES)) if provider == "multi" else ()
        if provider != "multi" and "sources" in item:
            raise ConfigError(f"{rid}.sources 仅用于 multi 多平台查询")
        market = item.get("market", "domestic")
        if market not in {"domestic", "international"}:
            raise ConfigError(f"{rid}.market 仅支持 domestic 或 international")
        currency = item.get("currency", "CNY")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ConfigError(f"{rid}.currency 应为大写三字币种，例如 CNY")
        mode = item.get("mode", "both")
        if mode not in {"lowest", "threshold", "both"}:
            raise ConfigError(f"{rid}.mode 仅支持 lowest、threshold、both")
        threshold = money(item["threshold"], f"{rid}.threshold") if "threshold" in item else None
        if mode in {"threshold", "both"} and threshold is None:
            raise ConfigError(f"{rid} 的 {mode} 模式必须配置 threshold")
        days = item.get("dates", [])
        if not isinstance(days, list) or len(days) > 31:
            raise ConfigError(f"{rid}.dates 必须是最多 31 个日期的列表")
        try:
            dates = tuple(sorted({date.fromisoformat(str(day)) for day in days}))
        except (ValueError, TypeError):
            raise ConfigError(f"{rid}.dates 应为 YYYY-MM-DD 日期") from None
        if dates and ("start_offset_days" in item or "end_offset_days" in item):
            raise ConfigError(f"{rid} 固定 dates 与滚动日期窗口只能选择一种")
        start = integer(item.get("start_offset_days", 7), f"{rid}.start_offset_days", 0, 365)
        end = integer(item.get("end_offset_days", 9), f"{rid}.end_offset_days", 0, 365)
        if end < start or end - start > 30:
            raise ConfigError(f"{rid} 滚动日期窗口应递增，且最多覆盖 31 天")
        nights = integer(item["stay_nights"], f"{rid}.stay_nights", 1, 90) if "stay_nights" in item else None
        nonstop = item.get("nonstop", False)
        if type(nonstop) is not bool:
            raise ConfigError(f"{rid}.nonstop 必须为 true 或 false")
        name = item.get("name", f"{codes[0]} → {codes[1]}")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ConfigError(f"{rid}.name 必须是 1～100 字的名称")
        travel_class = integer(item.get("travel_class", 1), f"{rid}.travel_class", 1, 4)
        if provider != "serpapi" and (currency != "CNY" or nights or nonstop or travel_class != 1
                                     or any("," in code for code in codes)):
            raise ConfigError(f"{rid} 公开来源仅支持 CNY、单程、默认舱位、单个城市代码，且不能筛选直飞")
        routes.append(Route(
            id=rid, name=name, origin=codes[0], destination=codes[1], provider=provider,
            currency=currency, mode=mode, threshold=threshold, dates=dates,
            start_offset_days=start, end_offset_days=end, stay_nights=nights,
            nonstop=nonstop, travel_class=travel_class, market=market, sources=sources,
            origin_scope=locations[0][0], destination_scope=locations[1][0],
            origin_city_code=locations[0][1], destination_city_code=locations[1][1],
            origin_label=locations[0][2], destination_label=locations[1][2],
        ))
    database = monitor.get("database", "data/prices.sqlite3")
    if not isinstance(database, str) or not database:
        raise ConfigError("database 应为文件路径")
    return Settings(
        tuple(routes), tz,
        integer(monitor.get("interval_minutes", 360), "interval_minutes", 10, 10080),
        integer(monitor.get("digest_hours", 24), "digest_hours", 1, 8760),
        integer(monitor.get("repeat_hours", 24), "repeat_hours", 1, 8760),
        money(monitor.get("min_drop", 1), "min_drop"),
        integer(monitor.get("timeout_seconds", 30), "timeout_seconds", 3, 120),
        float(delay),
        integer(monitor.get("max_requests_per_cycle", 320), "max_requests_per_cycle", 1, 1000),
        (path.parent / database).resolve(),
    )
