"""Bounded flight search through Alibaba's official FlyAI CLI.

The CLI owns authentication (including its official restricted demo mode).
No credentials or signing implementation are embedded here. The observed
ticketPrice response does not establish tax inclusion, so these are references
only, never threshold-alert totals. Contract: alibaba-flyai/flyai-skill,
skills/flyai/references/search-flight.md, inspected 2026-09-14.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .models import ProviderError, ProviderUnsupported, Quote, Route, SearchResult


_LIMIT = 8_000_000
_WORKERS = 2
_CODE = re.compile(r"[A-Z]{3}\Z")
_BUNDLE = (Path(__file__).resolve().parent.parent / ".runtime" / "flyai" /
           "node_modules" / "@fly-ai" / "flyai-cli" / "dist" / "flyai-bundle.cjs")
_REFERENCE = (
    "飞猪 FlyAI 返回航班参考价；ticketPrice 未提供可核验税费口径，"
    "不参与含税最低价及阈值提醒；价格、行李和可售条件以对应航班预订页为准"
)


def cli_command() -> list[str] | None:
    configured = os.environ.get("FLIGHTWATCH_FLYAI_CLI", "").strip()
    path = Path(configured).expanduser() if configured else _BUNDLE
    if path.is_file():
        if path.suffix in {".js", ".cjs", ".mjs"}:
            explicit_node = os.environ.get("FLIGHTWATCH_FLYAI_NODE", "").strip()
            if explicit_node:
                executable = Path(explicit_node)
                # systemd does not normally inherit interactive shell PATH.
                # An explicit runtime must be an absolute executable file;
                # never interpret it as shell text or silently replace it.
                if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
                    return None
                node = str(executable)
            else:
                node = shutil.which("node")
            return [node, str(path)] if node else None
        return [str(path)] if os.access(path, os.X_OK) else None
    if configured:
        found = shutil.which(configured)
        return [found] if found else None
    found = shutil.which("flyai")
    return [found] if found else None


def available() -> bool:
    return cli_command() is not None


def _place_name(route: Route, side: str) -> str:
    from .cities import resolve_city
    from .city_search import cached_place
    code, scope = getattr(route, side), getattr(route, side + "_scope")
    city_code = route.city_code(side)
    if not _CODE.fullmatch(code) or not _CODE.fullmatch(city_code):
        raise ProviderUnsupported("飞猪 FlyAI 查询需要有效地点及所属城市三字码")
    place = cached_place(code, scope, city_code)
    if place:
        name = place.get("name", code)
        city_name = place.get("city_name", "")
        if scope == "airport" and city_name and not name.startswith(city_name):
            name = city_name + name
        return name
    label = getattr(route, side + "_label")
    if label:
        name = re.sub(r"[（(].*?[）)]", "", label).replace("·", "").strip()
        if name and len(name) <= 120 and not any(ord(ch) < 32 for ch in name):
            return name
    city = resolve_city(city_code) if scope == "city" else None
    return city["name"] if city else code


def _booking_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) <= 32 for c in value):
        raise ProviderError("飞猪 FlyAI 航班购买链接无效")

    def valid(url):
        try:
            parsed = urlsplit(url)
            return (parsed.scheme == "https" and parsed.hostname == "router.feizhu.com"
                    and parsed.port in (None, 443) and not parsed.username and not parsed.password
                    and not parsed.fragment and "\\" not in url
                    and not any(ord(char) <= 32 for char in url))
        except ValueError:
            return False

    if not valid(value):
        raise ProviderError("飞猪 FlyAI 航班购买链接不是官方安全地址")
    parsed = urlsplit(value)
    if parsed.path == "/multi/webview":
        query = parse_qs(parsed.query, keep_blank_values=True)
        nested = query.get("url", [])
        if (set(query) != {"url"} or len(nested) != 1 or not valid(nested[0])
                or not re.fullmatch(r"/ws/[A-Za-z0-9]+", urlsplit(nested[0]).path)
                or urlsplit(nested[0]).query):
            raise ProviderError("飞猪 FlyAI 内层购买链接不是已核实的官方航班入口")
    elif not re.fullmatch(r"/ws/[A-Za-z0-9]+", parsed.path) or parsed.query:
        raise ProviderError("飞猪 FlyAI 购买链接路径发生变化")
    return value


def parse_flyai(payload: object, route: Route, day: date) -> SearchResult:
    if not isinstance(payload, dict) or type(payload.get("status")) is not int or payload["status"] != 0:
        # Never echo arbitrary CLI errors: they may contain credentials or URLs.
        raise ProviderError("飞猪 FlyAI 查询未成功，可能为体验额度、授权或服务限制；已停止后续日期")
    data = payload.get("data")
    rows = data.get("itemList") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > 500:
        raise ProviderError("飞猪 FlyAI 未返回有效航班列表")
    quotes, skipped = [], 0
    for row in rows:
        if not isinstance(row, dict):
            raise ProviderError("飞猪 FlyAI 航班条目结构异常")
        journeys = row.get("journeys")
        if not isinstance(journeys, list) or len(journeys) != 1 or not isinstance(journeys[0], dict):
            raise ProviderError("飞猪 FlyAI 未返回可核验的单程行程")
        journey = journeys[0]
        segments = journey.get("segments")
        if not isinstance(segments, list) or not 1 <= len(segments) <= 8:
            raise ProviderError("飞猪 FlyAI 缺少完整航段")
        dep_times, arr_times, numbers, airlines = [], [], [], []
        for segment in segments:
            if not isinstance(segment, dict) or any(
                not isinstance(segment.get(key), str) or not _CODE.fullmatch(segment[key])
                for key in ("depCityCode", "arrCityCode", "depStationCode", "arrStationCode")
            ):
                raise ProviderError("飞猪 FlyAI 航段缺少实际机场及城市代码")
            if segment.get("transportType") != "飞机" or segment.get("seatClassName") != "经济舱":
                break
            try:
                dep_times.append(datetime.strptime(segment["depDateTime"], "%Y-%m-%d %H:%M:%S"))
                arr_times.append(datetime.strptime(segment["arrDateTime"], "%Y-%m-%d %H:%M:%S"))
            except (KeyError, TypeError, ValueError):
                raise ProviderError("飞猪 FlyAI 航段日期时间不完整") from None
            number = segment.get("marketingTransportNo")
            if not isinstance(number, str) or not re.fullmatch(r"[A-Z0-9]{2,3}[0-9]{1,5}[A-Z]?", number):
                raise ProviderError("飞猪 FlyAI 航班号无效")
            numbers.append(number)
            name = segment.get("marketingTransportName", "")
            if not isinstance(name, str) or len(name) > 100:
                raise ProviderError("飞猪 FlyAI 承运人字段无效")
            airlines.append(name)
        else:
            first, last = segments[0], segments[-1]
            if (first["depCityCode"] != route.city_code("origin")
                    or last["arrCityCode"] != route.city_code("destination")
                    or dep_times[0].date() != day):
                raise ProviderError("飞猪 FlyAI 返回日期或城市与请求不一致，拒绝其他行程价格")
            if ((route.origin_scope == "airport" and first["depStationCode"] != route.origin)
                    or (route.destination_scope == "airport" and last["arrStationCode"] != route.destination)):
                skipped += 1
                continue
            if any(segments[i]["arrStationCode"] != segments[i+1]["depStationCode"]
                   or segments[i]["arrCityCode"] != segments[i+1]["depCityCode"]
                   or arr_times[i] > dep_times[i+1] for i in range(len(segments)-1)):
                skipped += 1
                continue
            direct = (len(segments) == 1 and journey.get("journeyType") == "直达"
                      and not first.get("stopInfos"))
            if route.nonstop and not direct:
                skipped += 1
                continue
            value = row.get("ticketPrice")
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
                raise ProviderError("飞猪 FlyAI 缺少已核实的 ticketPrice 参考价")
            try:
                price = Decimal(value)
            except InvalidOperation:
                raise ProviderError("飞猪 FlyAI 参考价无效") from None
            if not 0 < price <= 1_000_000 or row.get("currency", "CNY") != "CNY":
                raise ProviderError("飞猪 FlyAI 参考价或币种无法确认")
            quotes.append(Quote(route.origin, route.destination, day, price, "CNY", "飞猪 FlyAI 航班查询",
                airline="/".join(dict.fromkeys(airlines)), flight_number="/".join(numbers),
                stops=0 if direct else None, url=_booking_url(row.get("jumpUrl")),
                provider="fliggy", price_basis="unknown", price_note=_REFERENCE,
                origin_airport=first["depStationCode"], destination_airport=last["arrStationCode"]))
            continue
        skipped += 1  # A non-flight or non-economy segment.
    warnings = [_REFERENCE, "飞猪 FlyAI 仅展示本次返回的匹配航班，不能代表平台全部航班最低价"]
    system_message = payload.get("systemMessage")
    if (isinstance(system_message, str)
            and any(word in system_message.lower() for word in ("体验", "受限", "demo", "trial"))):
        warnings.append("飞猪 FlyAI 当前为官方免费体验，部分搜索结果或调用额度可能受限；正式服务需平台 API Key")
    if skipped:
        warnings.append(f"飞猪 FlyAI 已排除 {skipped} 条机场、交通方式、舱等或中转条件不匹配的条目")
    if not quotes:
        warnings.append("飞猪 FlyAI 本次未返回符合条件的参考航班，不代表该日没有航班")
    return SearchResult(sorted(quotes, key=lambda quote: quote.price), warnings)


def _terminate(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def _cli_environment():
    env = os.environ.copy()
    # Official CLI debugging redirects must never divert a production key.
    env.pop("DEBUG_FLYAI_MCP_URL", None)
    env.pop("DEBUG_FLYAI_API_KEY", None)
    # Supported by recent Node; older supported runtimes ignore this env var
    # instead of exiting on an unrecognized --use-env-proxy command flag.
    env["NODE_USE_ENV_PROXY"] = "1"
    if env.get("FLYAI_API_KEY", "").strip():
        return env
    # Read only this app's optional FlyAI key. Do not import unrelated dotenv
    # settings, interpolate values, execute shell text, or mutate parent env.
    path = Path(__file__).resolve().parent.parent / ".env"
    try:
        with path.open("rb") as stream:
            body = stream.read(16_385)
    except FileNotFoundError:
        return env
    except OSError:
        raise ProviderError("无法读取项目中的飞猪 FlyAI Key 配置") from None
    if len(body) > 16_384:
        raise ProviderError("项目 .env 超过 16KB，未读取 FlyAI Key")
    try:
        lines = body.decode("utf-8-sig").splitlines()
    except UnicodeError:
        raise ProviderError("项目 .env 编码无效，未读取 FlyAI Key") from None
    for line in lines:
        match = re.fullmatch(r"\s*(?:export\s+)?FLYAI_API_KEY\s*=\s*(.*?)\s*", line)
        if not match:
            continue
        value = match.group(1)
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ProviderError("项目 FlyAI Key 引号格式无效")
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if len(value) > 4096 or any(ord(char) < 32 for char in value):
            raise ProviderError("项目 FlyAI Key 格式无效")
        if value:
            env["FLYAI_API_KEY"] = value
        break
    return env


def _run_cli(command, parent, stop: threading.Event):
    parent._check_cancelled()
    if stop.is_set():
        raise ProviderError("飞猪 FlyAI 本轮已停止")
    env = _cli_environment()
    # API keys are inherited only as environment variables. Do not log stderr.
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.DEVNULL, env=env)
        except OSError:
            raise ProviderError("飞猪 FlyAI CLI 无法启动，请检查官方 CLI 和 Node.js 安装") from None
        deadline = time.monotonic() + max(1, parent.timeout)
        try:
            while process.poll() is None:
                parent._check_cancelled()
                if stop.is_set():
                    raise ProviderError("飞猪 FlyAI 本轮已停止，剩余日期未查询")
                if time.monotonic() >= deadline:
                    raise ProviderError("飞猪 FlyAI 单日查询超时，已停止后续日期")
                if os.fstat(output.fileno()).st_size > _LIMIT:
                    raise ProviderError("飞猪 FlyAI 返回数据超过大小限制")
                time.sleep(0.05)
            parent._check_cancelled()
            if process.returncode:
                raise ProviderError("飞猪 FlyAI CLI 查询失败，可能为网络、体验额度或授权限制")
            output.seek(0)
            body = output.read(_LIMIT + 1)
            if len(body) > _LIMIT:
                raise ProviderError("飞猪 FlyAI 返回数据超过大小限制")
            try:
                return json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                raise ProviderError("飞猪 FlyAI 未返回有效 JSON 航班数据") from None
        finally:
            _terminate(process)


def search_flyai(parent, route: Route, days: list[date]) -> SearchResult:
    parent._check_cancelled()
    if route.stay_nights is not None or route.travel_class != 1 or route.currency != "CNY":
        raise ProviderUnsupported("飞猪 FlyAI 目前仅接入单程经济舱人民币参考航班")
    if not days:
        return SearchResult([], ["没有尚未过期的出发日期"])
    if len(days) > 31:
        raise ProviderUnsupported("飞猪 FlyAI 每轮最多查询 31 个出发日期")
    prefix = cli_command()
    if not prefix:
        raise ProviderUnsupported("飞猪 FlyAI 官方 CLI 尚未安装或不可运行")
    args = ["search-flight", "--origin", _place_name(route, "origin"),
            "--destination", _place_name(route, "destination"),
            "--seat-class-name", "经济舱", "--sort-type", "3"]
    if route.nonstop:
        args += ["--journey-type", "1"]
    stop, lock = threading.Event(), threading.Lock()

    def query(day):
        with lock:
            parent._check_cancelled()
            if stop.is_set():
                raise ProviderError("飞猪 FlyAI 本轮已停止")
            reserve = getattr(parent, "_reserve_request", None) or parent._before_request
            reserve()
        try:
            return parse_flyai(_run_cli(prefix + args + ["--dep-date", day.isoformat()], parent, stop), route, day)
        except BaseException:
            stop.set()
            raise

    first = query(days[0])  # An inaccessible service does not consume a month of demo requests.
    quotes, warnings = list(first.quotes), list(first.warnings)
    failed = False
    pending_days = iter(days[1:])
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        tasks = {}

        def fill():
            while len(tasks) < _WORKERS and not stop.is_set():
                parent._check_cancelled()
                day = next(pending_days, None)
                if day is None:
                    break
                tasks[pool.submit(query, day)] = day

        try:
            fill()
            while tasks:
                parent._check_cancelled()
                complete, _ = wait(tasks, timeout=0.1, return_when=FIRST_COMPLETED)
                for task in complete:
                    day = tasks.pop(task)
                    try:
                        result = task.result()
                        quotes.extend(result.quotes)
                        warnings.extend(result.warnings)
                    except ProviderError as exc:
                        failed = True
                        stop.set()
                        warnings.append(f"{day}：{exc}")
                fill()
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
    parent._check_cancelled()
    if failed:
        warnings.append("飞猪 FlyAI 已保留完成日期的参考结果，未继续请求其余日期")
    quotes.sort(key=lambda quote: (quote.departure_date, quote.price, quote.flight_number))
    return SearchResult(quotes, list(dict.fromkeys(warnings)))
