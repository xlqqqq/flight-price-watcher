from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import load_config, load_env
from .models import ConfigError, ProviderError
from .monitor import DemoProvider, run_cycle
from .notifier import NotificationError, PushPlusNotifier
from .state import State

ROOT = Path(__file__).resolve().parent.parent
LOG = logging.getLogger(__name__)


@contextmanager
def process_lock(path: Path):
    """OS releases the lock on exit/crash; the empty lock file can safely remain."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ConfigError("已有监控进程使用此数据库，请勿重复启动。") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def make_providers(settings, demo: bool):
    from .sources import MultiSourceProvider
    # Share lazy provider instances across multi and individual public routes.
    # Construction failures remain isolated inside the aggregator, and each
    # source's request count still covers the complete monitoring cycle.
    public = None if demo else MultiSourceProvider(
        settings.timeout_seconds, settings.request_delay_seconds,
        settings.max_requests_per_cycle,
    )
    result = {}
    for name in {r.provider for r in settings.routes}:
        if demo:
            result[name] = DemoProvider()
        elif name == "multi":
            result[name] = public
        elif name == "serpapi":
            from .serpapi_source import SerpApiProvider
            result[name] = SerpApiProvider(os.environ.get("SERPAPI_API_KEY", ""),
                settings.timeout_seconds, settings.request_delay_seconds,
                settings.max_requests_per_cycle)
        else:
            result[name] = public
    return result


def make_notifier(channel: str, timeout: float):
    if channel == "pushplus":
        return PushPlusNotifier(os.environ.get("PUSHPLUS_TOKEN", ""), timeout)
    if channel == "serverchan":
        from .serverchan_notifier import ServerChanNotifier
        return ServerChanNotifier(serverchan_key(), ROOT / "data", timeout=timeout)
    from .desktop_notifier import DesktopWeChatNotifier
    return DesktopWeChatNotifier(timeout)


def serverchan_key():
    from .serverchan_settings import read_sendkey
    return os.environ.get("SERVERCHAN_SENDKEY", "").strip() or read_sendkey(ROOT / "data")


def readiness(settings, *, push: bool, channel: str = "wechat") -> list[str]:
    missing = []
    if push:
        if channel == "pushplus" and not os.environ.get("PUSHPLUS_TOKEN", "").strip():
            missing.append("兼容 PushPlus 模式需要 PUSHPLUS_TOKEN；免 token 请使用默认 wechat 模式")
        elif channel == "serverchan":
            from .serverchan_notifier import validate_sendkey
            try:
                validate_sendkey(serverchan_key())
            except NotificationError as exc:
                missing.append(str(exc))
        elif channel == "wechat":
            from .desktop_notifier import desktop_status
            status = desktop_status()
            if not status["available"]:
                missing.append(status["message"])
    if any(r.provider == "serpapi" for r in settings.routes) and not os.environ.get("SERPAPI_API_KEY", "").strip():
        missing.append("SERPAPI_API_KEY：仅 serpapi 数据源需要；默认 multi 多平台无需机票 API key")
    return missing


def execute(args) -> int:
    if args.web:
        from .webapp import serve
        return serve(args.port, open_browser=not args.no_browser)
    config_path = Path(args.config).resolve()
    load_env(Path(args.env).resolve() if args.env else config_path.parent / ".env")
    settings = load_config(config_path)
    preview = args.dry_run or args.demo
    if args.demo:
        print("=== 离线演示：所有机票价格均为模拟，不联网、不推送、不写入真实历史 ===")
    if not args.demo:
        today = datetime.now(settings.timezone).date()
        from .sources import DEFAULT_SOURCES, estimate_requests
        counts = {}
        for route in settings.routes:
            days = route.departure_dates(today)
            if not days:
                continue
            names = (route.sources or DEFAULT_SOURCES) if route.provider == "multi" else (route.provider,)
            for name in names:
                count = estimate_requests(name, route, days)
                counts[name] = counts.get(name, 0) + count
        if not args.test_push:
            for provider, count in counts.items():
                if count > settings.max_requests_per_cycle:
                    raise ConfigError(f"{provider} 每轮需要 {count} 次查询，超过上限 {settings.max_requests_per_cycle}；请缩小日期范围或调整上限，避免尾部航线永远漏查。")
        if args.test_push:
            from dataclasses import replace
            missing = readiness(replace(settings, routes=()), push=True, channel=args.notifier)
        else:
            missing = readiness(settings, push=not args.dry_run, channel=args.notifier)
        if args.check_config:
            requests = sum(counts.values())
            print(f"配置有效：{len(settings.routes)} 条航线，每 {settings.interval_minutes} 分钟一轮。")
            print(f"每轮基础请求估计 {requests} 次（城市缓存命中可减少），各数据源上限 {settings.max_requests_per_cycle} 次。")
            if "ryanair" in counts:
                print("瑞安航空多机场城市会增加查询，完整机场组合及剩余预算在运行时核验。")
            print(f"历史数据库：{settings.database}")
            if any(r.provider == "serpapi" for r in settings.routes):
                print("SerpApi 请求会消耗账户额度；按配置的日期数逐日查询。")
            if not missing:
                print("当前模式运行条件已满足（dry-run 不检查微信客户端）。")
        if missing:
            for message in missing:
                LOG.error("%s", message)
            return 2
    if args.check_config:
        return 0
    if args.test_push:
        receipt = make_notifier(args.notifier, settings.timeout_seconds).send(
            "机票监控 · 连通性测试", "这是机票监控脚本的微信推送测试，非机票报价。")
        print(f"已提交微信测试消息：{receipt}；请在微信确认是否收到。")
        return 0

    def run():
        state = State(":memory:" if preview else settings.database)
        try:
            notifier = None if preview else make_notifier(args.notifier, settings.timeout_seconds)
            while True:
                started = time.monotonic()
                providers = make_providers(settings, args.demo)
                result = run_cycle(settings, providers, state, notifier, dry_run=preview)
                if not args.loop:
                    return result
                delay = max(1, settings.interval_minutes * 60 - (time.monotonic() - started))
                LOG.info("本轮结束，约 %.0f 分钟后继续查询。", delay / 60)
                time.sleep(delay)
        finally:
            state.close()

    if preview:
        return run()
    with process_lock(settings.database.with_suffix(".lock")):
        return run()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="国内/国际机票最低参考价 → 微信提醒")
    parser.add_argument("--config", default=str(ROOT / "config.toml"), help="TOML 配置文件")
    parser.add_argument("--env", help="本地凭证文件；默认配置文件同目录的 .env")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="查询一轮后退出（默认）")
    mode.add_argument("--loop", action="store_true", help="按配置间隔持续运行")
    mode.add_argument("--check-config", action="store_true", help="检查配置及所需凭证，不联网")
    mode.add_argument("--test-push", action="store_true", help="发送一条微信连通性测试消息")
    mode.add_argument("--web", action="store_true", help="打开可选择地点和日期的免 token 页面")
    parser.add_argument("--port", type=int, default=8765, help="本机网页端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="启动网页时不自动打开浏览器")
    parser.add_argument("--notifier", choices=("wechat", "serverchan", "pushplus"), default="wechat",
                        help="wechat 为桌面微信；serverchan 为一次性免费 SendKey 的服务号推送（每天最多5次）；pushplus 兼容旧版")
    parser.add_argument("--dry-run", action="store_true", help="查询真实机票，仅打印提醒，不推送、不写真实历史")
    parser.add_argument("--demo", action="store_true", help="模拟数据离线演示，不联网、不推送")
    args = parser.parse_args(argv)
    if args.web and (args.dry_run or args.demo):
        parser.error("网页直接提供真实查询，不与 --dry-run / --demo 一起使用")
    if not 1024 <= args.port <= 65535:
        parser.error("端口应为 1024～65535")
    if argv is None and not sys.argv[1:]:
        args.web = True
    if args.test_push and (args.dry_run or args.demo):
        parser.error("--test-push 不能与 --dry-run / --demo 一起使用")
    if args.demo and (args.loop or args.check_config):
        parser.error("--demo 是单轮离线演示，不能与 --loop / --check-config 一起使用")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return execute(args)
    except (ConfigError, ProviderError, NotificationError) as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.info("监控已停止。")
        return 0
    except Exception as exc:
        # Avoid exposing credential-bearing URLs in unexpected exception messages.
        LOG.error("运行失败（%s），请检查配置、目录权限及本地数据库。", type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
