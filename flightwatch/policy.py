from datetime import datetime, timedelta
from decimal import Decimal

from .config import Settings
from .models import Route
from .state import State


def alert_kinds(route: Route, price: Decimal, state: State, settings: Settings,
                now: datetime) -> list[str]:
    kinds = []
    key = route.state_key()
    if route.mode in {"lowest", "both"}:
        last = state.last_alert(key, "lowest")
        if last is None or now - last[1] >= timedelta(hours=settings.digest_hours):
            kinds.append("lowest")
    # Strictly below the configured price; equality does not trigger.
    if route.mode in {"threshold", "both"} and route.threshold is not None and price < route.threshold:
        last = state.last_alert(key, "threshold")
        if (last is None or last[0] - price >= settings.min_drop
                or now - last[1] >= timedelta(hours=settings.repeat_hours)):
            kinds.append("threshold")
    return kinds
