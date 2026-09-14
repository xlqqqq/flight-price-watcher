from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal

from .config import Settings
from .models import ProviderError, Quote, Route, SearchResult
from .notifier import NotificationError
from .policy import alert_kinds
from .state import State

LOG = logging.getLogger(__name__)


def format_quote(route: Route, quote: Quote, kinds: list[str], previous: Decimal | None,
                 partial: bool = False) -> str:
    labels = {"lowest": "最低价汇总", "threshold": "低于目标价"}
    lines = [f"【{' / '.join(labels[k] for k in kinds)}】{route.name}",
             f"{quote.origin} → {quote.destination} | 出发 {quote.departure_date}"]
    if quote.return_date:
        lines.append(f"返程 {quote.return_date} | 1 成人往返总价")
    else:
        lines.append("1 成人单程参考价")
    lines.append(f"本次{'成功查询部分中' if partial else '查到的'}最低价：{quote.currency} {quote.price:.2f}")
    if route.threshold is not None:
        lines.append(f"目标：严格低于 {route.currency} {route.threshold:.2f}")
    if previous is not None:
        lines.append(f"此前本脚本观察到的最低价：{quote.currency} {previous:.2f}")
    if quote.airline or quote.flight_number:
        lines.append(f"航司/航班：{quote.airline} {quote.flight_number}".strip())
    if quote.origin_airport and quote.destination_airport:
        lines.append(f"实际起降机场：{quote.origin_airport} → {quote.destination_airport}")
    lines.append(f"中转次数：{quote.stops}" if quote.stops is not None else "直飞/中转请在购票页确认")
    lines.extend([f"来源：{quote.source}", quote.price_note])
    if quote.url:
        lines.append(f"按本行程继续购买：{quote.url}")
    if partial:
        lines.append("部分平台或日期未获得可比报价，本次价格不代表完整日期窗口的最低价。")
    return "\n".join(lines)


def run_cycle(settings: Settings, providers: dict, state: State, notifier=None,
              *, dry_run: bool = False, now: datetime | None = None, cancelled=None) -> int:
    now = now or datetime.now(settings.timezone)
    pending, failed = [], False
    for route in settings.routes:
        if cancelled is not None and cancelled():
            return 0
        if not route.departure_dates(now.date()):
            LOG.warning("%s：配置的日期均已过期，请更新日期。", route.name)
            failed = True
            continue
        try:
            result = providers[route.provider].search(route, now.date())
        except ProviderError as exc:
            LOG.error("%s：%s", route.name, exc)
            failed = True
            continue
        for warning in result.warnings:
            LOG.warning("%s：%s", route.name, warning)
        failed |= bool(result.warnings)
        dates = set(route.departure_dates(now.date()))
        valid = [q for q in result.quotes if q.comparable and q.currency == route.currency
                 and q.origin == route.origin and q.destination == route.destination
                 and (route.origin_scope != "airport" or q.origin_airport == route.origin)
                 and (route.destination_scope != "airport" or q.destination_airport == route.destination)
                 and q.departure_date in dates and q.return_date == route.return_on(q.departure_date)]
        if not valid:
            LOG.warning("%s：未查到匹配条件的有效价格；这不代表没有航班。", route.name)
            failed = True
            continue
        quote = min(valid, key=lambda q: (q.price, q.departure_date, q.flight_number))
        LOG.info("%s：本次最低 %s %.2f，出发 %s（%d 条报价）。",
                 route.name, quote.currency, quote.price, quote.departure_date, len(valid))
        key = route.state_key()
        previous = state.observe(key, quote, now)
        kinds = alert_kinds(route, quote.price, state, settings, now)
        if kinds:
            block = format_quote(route, quote, kinds, previous, bool(result.warnings))
            if result.sources:
                lines = []
                for source in result.sources:
                    price = source.get("lowest_price")
                    outcome = (f"{route.currency} {price:.2f}" if price is not None
                               else source.get("message", "未获得可比总价"))
                    lines.append(f"{source['name']}：{outcome}")
                count = sum(source.get("lowest_price") is not None for source in result.sources)
                block += f"\n有可比报价的平台：{count}/{len(result.sources)}\n" + "\n".join(lines)
            pending.append((route, quote, kinds, block))
        else:
            LOG.info("%s：未达到提醒条件，或仍在重复提醒间隔内。", route.name)
    # Split only between routes; each accepted chunk gets its own persistent state.
    chunks, chunk, size = [], [], 0
    for entry in pending:
        block_size = len(entry[3].encode("utf-8")) + 8
        if chunk and size + block_size > 9000:
            chunks.append(chunk)
            chunk, size = [], 0
        chunk.append(entry)
        size += block_size
    if chunk:
        chunks.append(chunk)
    for chunk in chunks:
        if cancelled is not None and cancelled():
            return 0
        title = f"机票低价提醒 · {len(chunk)} 条航线"
        if len(chunk) == 1:
            route, quote, _, _ = chunk[0]
            # Free service-account cards show only the title. Put the fare
            # and departure date there instead of making every alert identical.
            amount = format(quote.price, ".2f")
            if "." in amount:
                amount = amount.rstrip("0").rstrip(".")
            title = f"{route.origin}→{route.destination} {quote.departure_date:%m-%d} {quote.currency}{amount}"
        content = (f"查询时间：{now.strftime('%Y-%m-%d %H:%M %Z')}\n"
                   "价格为本次数据源返回结果，非全网最低价保证。\n\n"
                   + "\n\n────────\n\n".join(entry[3] for entry in chunk))
        if dry_run:
            print(f"\n[预览，不发送微信] {title}\n{content}\n")
            continue
        try:
            if notifier is None:
                raise NotificationError("未配置微信推送器")
            receipt = notifier.send(title, content)
        except NotificationError as exc:
            LOG.error("微信推送未确认受理：%s；未记录为已提醒，下轮将重试。", exc)
            failed = True
            continue
        for route, quote, kinds, _ in chunk:
            for kind in kinds:
                state.mark_alert(route.state_key(), kind, quote.price, now, receipt)
        if receipt.startswith("accepted:"):
            LOG.info("推送服务已受理 %d 条航线提醒（这不是微信送达回执）。", len(chunk))
        elif receipt.startswith("browser:"):
            LOG.info("已生成 %d 条航线的网页提醒。", len(chunk))
        else:
            LOG.info("微信客户端已提交 %d 条航线提醒；请在文件传输助手确认。", len(chunk))
    state.prune(now)
    return 1 if failed else 0


class DemoProvider:
    """Explicit offline fixtures; never used as a live source fallback."""

    def search(self, route: Route, today: date) -> SearchResult:
        dates = route.departure_dates(today)
        price = (route.threshold or Decimal("1000")) * Decimal("0.8")
        return SearchResult([Quote(
            route.origin, route.destination, day, price + n * Decimal("20"),
            route.currency, "演示数据（非真实机票报价）", route.return_on(day),
            price_note="仅用于检查提醒格式和规则，请勿用于订票。",
        ) for n, day in enumerate(dates)], [])
