"""Local UI and scheduler with optional server-based WeChat notifications."""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .config import Settings, get_timezone, integer, money
from . import __version__
from .models import ConfigError, ProviderError, Route, SearchResult
from .monitor import run_cycle
from .notifier import NotificationError
from .sources import DEFAULT_SOURCES, PROVIDERS, MultiSourceProvider, normalize_sources
from .state import State

ROOT = Path(__file__).resolve().parent.parent
TZ = get_timezone("Asia/Shanghai")
LOG = logging.getLogger(__name__)
MAX_WEB_TRIPS = 10
TRIP_FIELDS = {"origin", "destination", "market", "start_date", "end_date",
               "threshold", "mode", "providers",
               "origin_scope", "destination_scope",
               "origin_city_code", "destination_city_code",
               "origin_label", "destination_label"}
GLOBAL_FIELDS = {"interval_minutes", "notify"}


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def known_city(value):
    from .cities import resolve_city
    from .city_search import cached_city
    return resolve_city(value) or cached_city(value)


def known_place(value, scope="city", city_code=None):
    """Resolve cached official place metadata without doing network I/O."""
    from .city_search import cached_place
    if scope == "city":
        return known_city(value)
    return cached_place(value, scope="airport", city_code=city_code)


def _safe_place_label(value, field):
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{field}地点名称无效")
    label = " ".join(value.split())
    if not label or len(label) > 160 or any(ord(ch) < 32 for ch in label):
        raise ConfigError(f"{field}地点名称无效")
    return label


def _normalize_trip(raw: dict, today: date) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("请输入航线和出发日期")
    if set(raw) - TRIP_FIELDS:
        raise ConfigError("请求包含不支持的选项")
    data = {}
    places = []
    for field in ("origin", "destination"):
        value = raw.get(field, "")
        if not isinstance(value, str):
            raise ConfigError("请选择出发地和目的地")
        scope = raw.get(f"{field}_scope", "city")
        if scope not in {"city", "airport"}:
            raise ConfigError("地点范围只能选择城市（全部机场）或具体机场")
        supplied_city_code = raw.get(f"{field}_city_code", "")
        if not isinstance(supplied_city_code, str):
            raise ConfigError("所属城市代码无效")
        supplied_city_code = supplied_city_code.strip().upper()
        if supplied_city_code and not re.fullmatch(r"[A-Z]{3}", supplied_city_code):
            raise ConfigError("所属城市代码必须是三字码")
        place = known_place(value.strip(), scope, supplied_city_code or None)
        code = place["code"] if place else value.strip().upper()
        if not re.fullmatch("[A-Z]{3}", code):
            raise ConfigError("请选择城市（全部机场）或具体机场")
        city_code = place.get("city_code", "") if place else supplied_city_code
        if scope == "city":
            if city_code and city_code != code:
                raise ConfigError("城市范围的查询码与所属城市码不一致")
            city_code = code
        elif not city_code:
            raise ConfigError("选择具体机场时必须保留其所属城市码，请重新搜索并选择机场")
        label = (place or {}).get("label") or _safe_place_label(raw.get(f"{field}_label"), field)
        if not label:
            name = (place or {}).get("name", code)
            label = f"{name}（{code} · 全部机场）" if scope == "city" else f"{name}（{code}）"
        data[field] = code
        data[f"{field}_scope"] = scope
        data[f"{field}_city_code"] = city_code
        data[f"{field}_label"] = label
        places.append(place)
    if data["origin_city_code"] == data["destination_city_code"]:
        raise ConfigError("出发地和目的地不能相同或属于同一城市")
    market = raw.get("market", "domestic")
    if market not in ("domestic", "international"):
        raise ConfigError("请选择国内或国际航线")
    if all(places):
        expected = "domestic" if all(c["market"] == "domestic" for c in places) else "international"
        if market != expected:
            raise ConfigError("国内/国际选择与城市不匹配，请调整航线类型")
    data["market"] = market
    try:
        start = date.fromisoformat(raw.get("start_date", ""))
        end = date.fromisoformat(raw.get("end_date", ""))
    except (TypeError, ValueError):
        raise ConfigError("请选择有效的开始和结束日期") from None
    if start < today or end < start or end > today + timedelta(days=365):
        raise ConfigError("日期必须从今天起，结束不早于开始，且在未来一年内")
    if (end - start).days > 30:
        raise ConfigError("一次最多查询连续 31 天，请缩小日期范围")
    data.update(start_date=start.isoformat(), end_date=end.isoformat())
    mode = raw.get("mode", "both")
    if mode not in ("lowest", "threshold", "both"):
        raise ConfigError("请选择最低价汇总、低于目标价或两者")
    value = raw.get("threshold")
    threshold = None if value in (None, "") else money(value, "目标价")
    if mode != "lowest" and threshold is None:
        raise ConfigError("此提醒模式需要填写目标价")
    if threshold is not None and threshold > 1000000:
        raise ConfigError("目标价不能超过 1000000 元")
    data.update(mode=mode, threshold=float(threshold) if threshold is not None else None)
    data["providers"] = list(normalize_sources(raw.get("providers", DEFAULT_SOURCES)))
    return data


def _normalize_globals(raw: dict) -> dict:
    data = {
        "interval_minutes": integer(raw.get("interval_minutes", 360), "查询间隔（分钟）", 10, 10080),
    }
    channel = raw.get("notify", "wechat")
    if channel not in ("wechat", "browser", "serverchan"):
        raise ConfigError("请选择微信文件传输助手、微信服务号或网页提醒")
    data["notify"] = channel
    return data


def normalize_form(raw: dict, today: date | None = None) -> dict:
    """Normalize the original single-trip request without changing its schema."""
    today = today or datetime.now(TZ).date()
    if not isinstance(raw, dict):
        raise ConfigError("请输入航线和出发日期")
    if set(raw) - (TRIP_FIELDS | GLOBAL_FIELDS):
        raise ConfigError("请求包含不支持的选项")
    return {**_normalize_trip({key: value for key, value in raw.items() if key in TRIP_FIELDS}, today),
            **_normalize_globals(raw)}


def normalize_request(raw: dict, today: date | None = None) -> dict:
    """Accept either the original flat request or a multi-trip envelope."""
    today = today or datetime.now(TZ).date()
    if not isinstance(raw, dict):
        raise ConfigError("请输入航线和出发日期")
    if "trips" not in raw:
        return normalize_form(raw, today)
    if set(raw) - ({"trips"} | GLOBAL_FIELDS):
        raise ConfigError("多行程请求包含不支持的选项")
    values = raw.get("trips")
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_WEB_TRIPS:
        raise ConfigError(f"请添加 1～{MAX_WEB_TRIPS} 条行程")
    trips = []
    signatures = set()
    for index, value in enumerate(values, 1):
        try:
            trip = _normalize_trip(value, today)
        except ConfigError as exc:
            raise ConfigError(f"第 {index} 条行程：{exc}") from None
        signature = json.dumps(trip, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if signature in signatures:
            raise ConfigError(f"第 {index} 条行程与清单中的另一条完全重复")
        signatures.add(signature)
        trips.append(trip)
    return {"trips": trips, **_normalize_globals(raw)}


def _trips_in(form: dict) -> tuple[dict, ...]:
    return tuple(form["trips"]) if "trips" in form else (form,)


def settings_for(form: dict, data_dir: Path) -> Settings:
    trips = _trips_in(form)
    routes = []
    for trip in trips:
        start, end = date.fromisoformat(trip["start_date"]), date.fromisoformat(trip["end_date"])
        names = [trip.get(f"{key}_label") or (known_city(trip[key]) or {"name": trip[key]})["name"]
                 for key in ("origin", "destination")]
        if "trips" in form:
            identity = {key: value for key, value in trip.items() if not key.endswith("_label")}
            stable = json.dumps({"notify": form["notify"], **identity}, ensure_ascii=True,
                                sort_keys=True, separators=(",", ":"))
            route_id = f"dashboard-{form['notify']}-{hashlib.sha256(stable.encode()).hexdigest()[:20]}"
        else:
            # Preserve the historic state key for existing one-trip monitors.
            route_id = f"dashboard-{form['notify']}"
        routes.append(Route(
            id=route_id, name=" → ".join(names),
            origin=trip["origin"], destination=trip["destination"], provider="multi",
            market=trip["market"], mode=trip["mode"],
            threshold=Decimal(str(trip["threshold"])) if trip["threshold"] is not None else None,
            dates=tuple(start + timedelta(days=i) for i in range((end - start).days + 1)),
            sources=tuple(trip["providers"]),
            origin_scope=trip.get("origin_scope", "city"),
            destination_scope=trip.get("destination_scope", "city"),
            origin_city_code=trip.get("origin_city_code", trip["origin"]),
            destination_city_code=trip.get("destination_city_code", trip["destination"]),
            origin_label=trip.get("origin_label", ""),
            destination_label=trip.get("destination_label", ""),
        ))
    return Settings(tuple(routes), TZ, form["interval_minutes"], 24, 24, Decimal("1"),
                    20, 1.0, 160, data_dir / "dashboard.sqlite3")


def _quote_payload(quote, route_id: str) -> dict:
    return dict(
        route_id=route_id,
        origin=quote.origin,
        destination=quote.destination,
        origin_airport=getattr(quote, "origin_airport", ""),
        destination_airport=getattr(quote, "destination_airport", ""),
        departure_date=quote.departure_date.isoformat(),
        return_date=quote.return_date.isoformat() if quote.return_date else None,
        price=float(quote.price),
        currency=quote.currency,
        url=quote.url,
        price_note=quote.price_note,
        source=quote.source,
        provider=quote.provider,
        airline=quote.airline,
        flight_number=quote.flight_number,
        stops=quote.stops,
        price_basis=quote.price_basis,
        comparable=quote.comparable,
        original_price=float(quote.original_price) if quote.original_price is not None else None,
        original_currency=quote.original_currency,
        exchange_rate=str(quote.exchange_rate) if quote.exchange_rate is not None else None,
        exchange_date=quote.exchange_date.isoformat() if quote.exchange_date else None,
    )


def _trip_snapshot(trip: dict, route: Route, *, result=None, error: str | None = None,
                   queried_at: str, in_progress: bool = False) -> dict:
    quotes = [] if result is None else [_quote_payload(quote, route.id) for quote in result.quotes]
    quotes.sort(key=lambda quote: (quote["departure_date"], quote["price"], quote["provider"]))
    if not in_progress and error is None and not any(quote["comparable"] for quote in quotes):
        error = "所选平台暂未返回可比较的参考总价；请查看该行程的平台状态。"
    return dict(
        route_id=route.id,
        name=route.name,
        origin=route.origin,
        destination=route.destination,
        origin_scope=route.origin_scope,
        destination_scope=route.destination_scope,
        origin_city_code=route.city_code("origin"),
        destination_city_code=route.city_code("destination"),
        origin_label=route.origin_label,
        destination_label=route.destination_label,
        market=route.market,
        start_date=trip["start_date"],
        end_date=trip["end_date"],
        settings=dict(trip),
        queried_at=queried_at,
        quotes=quotes,
        warnings=[] if result is None else list(result.warnings),
        sources=[] if result is None else list(result.sources),
        error=error,
        in_progress=in_progress,
    )


def _combined_snapshot(form: dict, trips: list[dict], queried_at: str) -> dict:
    if len(trips) == 1:
        # Keep every original single-trip field at the top level.  New clients
        # can consistently consume ``trips`` while old clients remain valid.
        snapshot = dict(trips[0])
        snapshot.update(queried_at=queried_at, settings=form, trips=trips)
        return snapshot
    quotes = [quote for trip in trips for quote in trip["quotes"]]
    warnings = [f"{trip['name']}：{warning}" for trip in trips for warning in trip["warnings"]]
    unavailable = [trip for trip in trips if trip["error"]]
    error = None
    partial_error = None
    if len(unavailable) == len(trips):
        error = f"全部 {len(trips)} 条行程暂未返回可比较的参考总价，请分别查看平台状态。"
    elif unavailable:
        partial_error = f"{len(unavailable)}/{len(trips)} 条行程暂未返回可比较的参考总价。"
    return dict(
        queried_at=queried_at,
        settings=form,
        trips=trips,
        trip_count=len(trips),
        successful_trip_count=len(trips) - len(unavailable),
        quotes=quotes,
        warnings=warnings,
        sources=[],
        error=error,
        partial_error=partial_error,
        in_progress=any(trip.get("in_progress") for trip in trips),
    )


class Dashboard:
    def __init__(self, data_dir: Path | None = None):
        from .serverchan_binding import ServerChanBinding
        self.data_dir = data_dir or ROOT / "data"
        self.lock = threading.RLock()
        self.search_busy = False
        self.operation_id = None
        self.latest = None
        self.notifications = []
        self.monitor = dict(running=False, busy=False, next_run_at=None, settings=None, last_error=None)
        self.stop_event = threading.Event()
        self.thread = None
        self.last_query = 0.0
        self.operation_ready_at = 0.0
        self.serverchan_binding = ServerChanBinding()
        self._binding_operation = None
        self._binding_error = None

    def bootstrap(self):
        from .cities import CITIES
        from .desktop_notifier import desktop_status
        from .serverchan_settings import channel_status
        today = datetime.now(TZ).date()
        defaults = dict(origin="", destination="", market="auto",
                        origin_scope="city", destination_scope="city",
                        origin_city_code="", destination_city_code="",
                        origin_label="", destination_label="",
                        start_date=(today + timedelta(days=7)).isoformat(),
                        end_date=(today + timedelta(days=21)).isoformat(),
                        threshold=600, mode="both", interval_minutes=360, notify="wechat",
                        providers=list(DEFAULT_SOURCES))
        try:
            saved = json.loads((self.data_dir / "web-settings.json").read_text(encoding="utf-8"))
            defaults = normalize_request(saved, today)
        except (OSError, ValueError, TypeError):
            pass
        return dict(today=today.isoformat(), max_date=(today + timedelta(days=365)).isoformat(),
                    cities=CITIES, wechat=desktop_status(), defaults=defaults,
                    providers=list(PROVIDERS), serverchan=channel_status(self.data_dir),
                    serverchan_binding=self._binding_status(), version=__version__)

    def city_lookup(self, query: str):
        from .cities import CITIES
        from .city_search import search_cities
        if not isinstance(query, str) or len(query) > 60:
            raise ConfigError("城市搜索词最多 60 个字符")
        query = query.strip()
        if not query:
            return {"cities": CITIES[:12]}
        local = [c for c in CITIES if query.casefold() in c["name"].casefold()
                 or query.casefold() in c["code"].casefold()]
        try:
            remote = search_cities(query)
        except ProviderError as exc:
            return {"cities": local[:20], "warning": f"{exc}；暂时显示匹配的常用城市，可稍后重试。"}
        by_code = {(city.get("scope", "city"), city["code"], city.get("city_code", city["code"])): city
                   for city in remote}
        for city in local:
            key = (city.get("scope", "city"), city["code"], city.get("city_code", city["code"]))
            by_code.setdefault(key, city)
        return {"cities": list(by_code.values())[:30]}

    def status(self):
        from .serverchan_settings import channel_status
        with self.lock:
            return json.loads(json.dumps(dict(monitor=self.monitor, latest=self.latest,
                     notifications=self.notifications, search_busy=self.search_busy,
                     serverchan=channel_status(self.data_dir), serverchan_binding=self._binding_status())))

    def _binding_status(self):
        with self.lock:
            if self._binding_operation:
                return dict(state=self._binding_operation, qr_image=None,
                            message="正在准备扫码绑定…" if self._binding_operation == "creating" else "正在确认你主动授权的绑定…")
            if self._binding_error:
                return dict(state="error", qr_image=None, message=self._binding_error)
            return self.serverchan_binding.status()

    def _claim(self):
        if self.search_busy:
            raise ConfigError("查询或微信操作正在进行，请稍等完成")
        self.search_busy = True
        self.operation_id = uuid.uuid4().hex
        # Queue a quick second click briefly instead of rejecting a valid new
        # route immediately after the previous result appeared.
        self.operation_ready_at = max(time.monotonic(), self.last_query + 3)
        self.last_query = self.operation_ready_at
        return self.operation_id

    def _release(self, operation_id):
        if self.operation_id == operation_id:
            self.search_busy = False
            self.operation_id = None

    def search(self, raw):
        form = normalize_request(raw)
        with self.lock:
            operation_id = self._claim()
        threading.Thread(target=self._query, args=(form, False, None, operation_id), daemon=True).start()

    def start(self, raw):
        from .desktop_notifier import desktop_status
        form = normalize_request(raw)
        if form["notify"] == "wechat":
            status = desktop_status()
            if not status["available"]:
                raise ConfigError(status["message"])
        with self.lock:
            if self.monitor["running"] or (self.thread and self.thread.is_alive()):
                raise ConfigError("已有监控任务，请先停止再修改")
            if form["notify"] == "serverchan":
                from .serverchan_settings import channel_status
                status = channel_status(self.data_dir)
                if not status["available"]:
                    raise ConfigError(status["message"])
            operation_id = self._claim()
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                target = self.data_dir / "web-settings.json"
                temporary = target.with_suffix(".tmp")
                temporary.write_text(json.dumps(form, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, target)
            except OSError:
                self._release(operation_id)
                raise ConfigError("无法保存选择，请检查 data 目录权限") from None
            self.stop_event = threading.Event()
            self.monitor.update(running=True, busy=True, next_run_at=None, settings=form, last_error=None)
            self.thread = threading.Thread(target=self._loop, args=(form, self.stop_event, operation_id), daemon=True)
            self.thread.start()

    def stop(self):
        with self.lock:
            self.stop_event.set()
            self.monitor.update(running=False, next_run_at=None)

    def _loop(self, form, event, operation_id):
        try:
            while not event.is_set():
                self._query(form, True, event, operation_id)
                operation_id = None
                if event.is_set():
                    break
                due = datetime.now(TZ) + timedelta(minutes=form["interval_minutes"])
                with self.lock:
                    self.monitor["next_run_at"] = due.isoformat(timespec="seconds")
                if event.wait(form["interval_minutes"] * 60):
                    break
                while not event.is_set():
                    with self.lock:
                        try:
                            operation_id = self._claim()
                            self.monitor.update(busy=True, next_run_at=None)
                            break
                        except ConfigError:
                            pass
                    event.wait(1)
        finally:
            with self.lock:
                self.monitor.update(running=False, busy=False, next_run_at=None)
                # A pre-query stop still has to release the initial claim.
                if operation_id is not None:
                    self._release(operation_id)

    def _record_notification(self, title, content, channel):
        item = dict(id=uuid.uuid4().hex, title=title, content=content,
                    created_at=now_iso(), channel=channel)
        with self.lock:
            self.notifications.insert(0, item)
            del self.notifications[50:]
        return item["id"]

    def _query(self, form, monitor: bool, event, operation_id=None):
        snapshot = None
        try:
            if event is not None and event.is_set():
                return
            with self.lock:
                delay = max(0, self.operation_ready_at - time.monotonic()) if operation_id is not None else 0
            if delay:
                if event is not None:
                    if event.wait(delay):
                        return
                else:
                    time.sleep(delay)
            settings = settings_for(form, self.data_dir)
            trip_forms = _trips_in(form)
            today = datetime.now(TZ).date()
            queried_at = now_iso()
            items = list(zip(trip_forms, settings.routes))
            progress = {}
            for trip, route in items:
                pending = SearchResult([], [], [dict(id=name, name=name, status="pending", quote_count=0,
                    lowest_price=None, message="等待查询", search_url="") for name in route.sources])
                progress[route.id] = _trip_snapshot(trip, route, result=pending,
                    queried_at=queried_at, in_progress=True)
            with self.lock:
                self.latest = _combined_snapshot(form, list(progress.values()), queried_at)

            def publish(trip, route, result=None, error=None, in_progress=True):
                current = _trip_snapshot(trip, route, result=result, error=error,
                    queried_at=queried_at, in_progress=in_progress)
                with self.lock:
                    if event is None or not event.is_set():
                        progress[route.id] = current
                        self.latest = _combined_snapshot(form,
                            [progress[r.id] for r in settings.routes], queried_at)
                return current

            def query_trip(item):
                trip, route = item
                if event is not None and event.is_set():
                    error = ProviderError("监控已停止，取消该行程查询")
                    return error, _trip_snapshot(trip, route, error=str(error), queried_at=queried_at)
                try:
                    result = MultiSourceProvider(
                        timeout=settings.timeout_seconds,
                        request_delay=settings.request_delay_seconds,
                        max_requests=settings.max_requests_per_cycle,
                        cancelled=event.is_set if event is not None else None,
                        on_progress=lambda result: publish(trip, route, result=result),
                    ).search(route, today)
                    return result, publish(trip, route, result=result, in_progress=False)
                except (ProviderError, ConfigError) as exc:
                    error = ProviderError(str(exc))
                except Exception as exc:
                    error = ProviderError(f"该行程查询未完成（{type(exc).__name__}），请稍后重试")
                return error, publish(trip, route, error=str(error), in_progress=False)

            if len(items) == 1:
                outcomes = [query_trip(items[0])]
            else:
                # A small route-level pool prevents one slow itinerary from
                # blocking every other one without multiplying source threads
                # without bound.
                with ThreadPoolExecutor(max_workers=min(3, len(items)),
                                        thread_name_prefix="dashboard-trip") as pool:
                    outcomes = list(pool.map(query_trip, items))
            cached = {route.id: outcome for route, (outcome, _trip) in zip(settings.routes, outcomes)}
            trip_snapshots = [trip for _outcome, trip in outcomes]
            snapshot = _combined_snapshot(form, trip_snapshots, queried_at)
            with self.lock:
                self.latest = snapshot
                if monitor:
                    self.monitor["last_error"] = snapshot.get("partial_error") or snapshot["error"]
            if monitor and not event.is_set():
                app = self
                class CachedProvider:
                    def search(self, route, *_args):
                        result = cached[route.id]
                        if isinstance(result, ProviderError):
                            raise result
                        return result
                class Notifier:
                    def send(self, title, content):
                        if event.is_set():
                            raise NotificationError("监控已停止，本次提醒取消")
                        if form["notify"] == "wechat":
                            from .desktop_notifier import DesktopWeChatNotifier
                            try:
                                receipt = DesktopWeChatNotifier().send(title, content)
                            except NotificationError as exc:
                                with app.lock:
                                    app.monitor["last_error"] = str(exc)
                                raise
                            app._record_notification(title, content, "wechat")
                            return receipt
                        if form["notify"] == "serverchan":
                            from .serverchan_settings import make_notifier
                            try:
                                receipt = make_notifier(app.data_dir).send(title, content)
                            except NotificationError as exc:
                                with app.lock:
                                    app.monitor["last_error"] = str(exc)
                                raise
                            app._record_notification(title, content, "serverchan")
                            return receipt
                        return "browser:" + app._record_notification(title, content, "browser")
                from .cli import process_lock
                with process_lock(settings.database.with_suffix(".lock")):
                    state = State(settings.database)
                    try:
                        run_cycle(settings, {"multi": CachedProvider()}, state, Notifier(), cancelled=event.is_set)
                    finally:
                        state.close()
        except (ProviderError, ConfigError, NotificationError) as exc:
            with self.lock:
                # A history/notification failure happens after a valid query.
                # Preserve its prices and platform reports for the user.
                if snapshot is None:
                    queried_at = now_iso()
                    settings = settings_for(form, self.data_dir)
                    failed = [_trip_snapshot(trip, route, error=str(exc), queried_at=queried_at)
                              for trip, route in zip(_trips_in(form), settings.routes)]
                    self.latest = _combined_snapshot(form, failed, queried_at)
                if monitor:
                    self.monitor["last_error"] = str(exc)
        except Exception as exc:
            with self.lock:
                if snapshot is None:
                    message = f"本次查询未完成（{type(exc).__name__}），请检查网络或稍后重试。"
                    queried_at = now_iso()
                    try:
                        settings = settings_for(form, self.data_dir)
                        failed = [_trip_snapshot(trip, route, error=message, queried_at=queried_at)
                                  for trip, route in zip(_trips_in(form), settings.routes)]
                        self.latest = _combined_snapshot(form, failed, queried_at)
                    except Exception:
                        self.latest = dict(queried_at=queried_at, settings=form, trips=[],
                                           quotes=[], warnings=[], sources=[], error=message)
                else:
                    message = f"报价已查询，但监控记录或提醒未完成（{type(exc).__name__}），请检查 data 目录及微信状态。"
                self.monitor["last_error"] = message
        finally:
            with self.lock:
                self._release(operation_id)
                if monitor:
                    self.monitor["busy"] = False

    def test_wechat(self):
        from .desktop_notifier import desktop_status, DesktopWeChatNotifier
        status = desktop_status()
        if not status["available"]:
            raise ConfigError(status["message"])
        with self.lock:
            operation_id = self._claim()
            self.monitor["last_error"] = None
        def send():
            try:
                title, content = "机票监控 · 免 token 测试", "这是一条发给文件传输助手的测试消息，非机票报价。"
                DesktopWeChatNotifier().send(title, content)
                self._record_notification(title, content, "wechat")
            except NotificationError as exc:
                with self.lock:
                    self.monitor["last_error"] = str(exc)
            except Exception:
                with self.lock:
                    self.monitor["last_error"] = "微信测试未完成，请检查已登录的客户端。"
            finally:
                with self.lock:
                    self._release(operation_id)
        threading.Thread(target=send, daemon=True).start()

    def configure_serverchan(self, raw: dict, *, clear: bool = False):
        from .serverchan_settings import save_sendkey, clear_sendkey
        if (clear and raw) or (not clear and set(raw) != {"sendkey"}):
            raise ConfigError("微信服务号配置格式无效")
        with self.lock:
            if self.monitor["running"] or self.search_busy or (self.thread and self.thread.is_alive()):
                raise ConfigError("请先停止监控并等待当前操作完成，再修改微信服务号配置")
            try:
                if clear:
                    clear_sendkey(self.data_dir)
                else:
                    save_sendkey(self.data_dir, raw["sendkey"])
                self.serverchan_binding.cancel()
                self._binding_error = None
            except NotificationError as exc:
                raise ConfigError(str(exc)) from None

    def bind_serverchan(self, action: str):
        from .serverchan_settings import save_sendkey
        with self.lock:
            if self.monitor["running"] or self.search_busy or (self.thread and self.thread.is_alive()):
                raise ConfigError("请先停止监控并等待当前操作完成，再绑定微信服务号")
            if action == "cancel":
                self.serverchan_binding.cancel()
                self._binding_error = None
                return
            if action not in ("start", "confirm"):
                raise ConfigError("绑定操作无效")
            if action == "confirm" and self.serverchan_binding.status()["state"] != "waiting":
                raise ConfigError("请先生成有效二维码并使用手机微信扫码授权")
            operation_id = self._claim()
            self._binding_operation = "creating" if action == "start" else "checking"
            self._binding_error = None
        def bind():
            try:
                if action == "start":
                    self.serverchan_binding.start()
                else:
                    key = self.serverchan_binding.confirm()
                    if key is not None:
                        save_sendkey(self.data_dir, key)
            except NotificationError as exc:
                with self.lock:
                    self._binding_error = str(exc)
                self.serverchan_binding.cancel()
            except Exception:
                with self.lock:
                    self._binding_error = "扫码绑定未完成，请重新生成二维码，或使用下方的一次性 SendKey 配置。"
                self.serverchan_binding.cancel()
            finally:
                with self.lock:
                    self._binding_operation = None
                    self._release(operation_id)
        threading.Thread(target=bind, daemon=True).start()

    def test_serverchan(self):
        from .serverchan_settings import channel_status, make_notifier
        with self.lock:
            if self.monitor["running"] or (self.thread and self.thread.is_alive()):
                raise ConfigError("请先停止监控，再发送微信服务号测试消息")
            status = channel_status(self.data_dir)
            if not status["available"]:
                raise ConfigError(status["message"])
            if status["quota"]["remaining"] <= 0:
                raise ConfigError("今日 5 次免费发送预算已用完，明天自动恢复；测试消息也占用次数")
            operation_id = self._claim()
            self.monitor["last_error"] = None
        def send():
            try:
                title = "机票提醒 · 微信服务号测试"
                content = "这是一条来自机票监控服务器的测试消息，非机票报价。无需桌面微信或定期回复。此测试占用今日 5 次发送预算中的 1 次。"
                make_notifier(self.data_dir).send(title, content)
                self._record_notification(title, content, "serverchan")
            except NotificationError as exc:
                with self.lock:
                    self.monitor["last_error"] = str(exc)
            except Exception:
                with self.lock:
                    self.monitor["last_error"] = "微信服务号测试未确认受理，请查看服务状态和本地配置。"
            finally:
                with self.lock:
                    self._release(operation_id)
        threading.Thread(target=send, daemon=True).start()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = f"Flightwatch/{__version__}"
    def log_message(self, *args):
        pass

    def _reply(self, status, payload, content_type="application/json; charset=utf-8"):
        body = json.dumps(payload, ensure_ascii=False).encode() if isinstance(payload, dict) else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _local(self):
        port = self.server.server_address[1]
        allowed = {f"localhost:{port}", f"127.0.0.1:{port}"}
        if self.headers.get("Host", "").lower() not in allowed:
            self._reply(403, {"error": "仅允许通过本机地址访问"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed}:
            self._reply(403, {"error": "不接受其他网站的操作请求"})
            return False
        return True

    def do_GET(self):
        if not self._local():
            return
        path = urlsplit(self.path).path
        if path == "/api/bootstrap":
            self._reply(200, self.server.app.bootstrap())
        elif path == "/api/cities":
            try:
                query = parse_qs(urlsplit(self.path).query, max_num_fields=5).get("q", [""])[0]
                self._reply(200, self.server.app.city_lookup(query))
            except (ConfigError, ValueError):
                self._reply(400, {"error": "城市搜索词格式无效，最多 60 个字符"})
        elif path == "/api/status":
            self._reply(200, self.server.app.status())
        else:
            static = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8"),
                      "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/static/style.css": ("style.css", "text/css; charset=utf-8")}
            if path not in static:
                self._reply(404, {"error": "页面不存在"})
                return
            name, mime = static[path]
            self._reply(200, (Path(__file__).parent / "static" / name).read_bytes(), mime)

    def do_POST(self):
        if not self._local():
            return
        if self.headers.get("X-Flightwatch") != "1" or self.headers.get_content_type() != "application/json":
            self._reply(403, {"error": "请通过本机机票页面操作"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                self._reply(413, {"error": "请求大小无效"})
                return
            raw = json.loads(self.rfile.read(length))
            if not isinstance(raw, dict):
                raise ConfigError("请求内容必须是对象")
            path = urlsplit(self.path).path
            app = self.server.app
            if path == "/api/search":
                app.search(raw)
            elif path == "/api/monitor/start":
                app.start(raw)
            elif path == "/api/monitor/stop":
                app.stop()
            elif path == "/api/wechat/test":
                app.test_wechat()
            elif path == "/api/serverchan/save":
                app.configure_serverchan(raw)
            elif path == "/api/serverchan/clear":
                app.configure_serverchan(raw, clear=True)
            elif path == "/api/serverchan/test":
                if raw:
                    raise ConfigError("测试请求不接受额外参数")
                app.test_serverchan()
            elif path in ("/api/serverchan/bind/start", "/api/serverchan/bind/confirm", "/api/serverchan/bind/cancel"):
                if raw:
                    raise ConfigError("绑定请求不接受额外参数")
                app.bind_serverchan(path.rsplit("/", 1)[-1])
            else:
                self._reply(404, {"error": "接口不存在"})
                return
            self._reply(202, {"ok": True})
        except (ValueError, TypeError, RecursionError) as exc:
            message = str(exc) if isinstance(exc, ConfigError) else "请求格式无效"
            self._reply(400, {"error": message})
        except Exception:
            self._reply(500, {"error": "操作未完成，请稍后重试"})


def serve(port: int = 8765, *, open_browser: bool = True):
    app = Dashboard()
    server = LocalServer(("127.0.0.1", port), app)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"机票页面已启动：{url}\n查价无需 API key；在页面选择行程及通知方式；Ctrl+C 停止。", flush=True)
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        server.server_close()
    return 0
