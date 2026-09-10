"""Daily reference exchange rates; no keys or silent stale-rate fallback."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import re
import threading
import time
from urllib.parse import urlencode

from .models import ProviderError

RATE_ENDPOINT = "https://api.frankfurter.dev/v1/latest"
_CACHE = {}
_LOCK = threading.Lock()


def cny_rate(currency: str, today: date, fetch) -> tuple[Decimal, date]:
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ProviderError("外币代码无效，未折算人民币")
    if currency == "CNY":
        return Decimal("1"), today
    # The caller supplies its budgeted, cancellable HTTPS reader.
    with _LOCK:
        cached = _CACHE.get(currency)
        if cached and time.monotonic() - cached[0] < 21600 and 0 <= (today - cached[2]).days <= 7:
            return cached[1], cached[2]
        data = fetch(RATE_ENDPOINT + "?" + urlencode({"base": currency, "symbols": "CNY"}))
        try:
            if not isinstance(data, dict) or data.get("base") != currency:
                raise ValueError("base")
            if isinstance(data.get("amount"), bool) or Decimal(str(data.get("amount"))) != 1:
                raise ValueError("amount")
            day = date.fromisoformat(data["date"])
            raw = data["rates"]["CNY"]
            if isinstance(raw, bool):
                raise ValueError("rate")
            rate = Decimal(str(raw))
            if not rate.is_finite() or not 0 < rate < 1000000 or not 0 <= (today - day).days <= 7:
                raise ValueError("freshness")
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ProviderError("汇率币种、金额或日期无效/超过7天，未将外币当成人民币") from None
        if len(_CACHE) >= 64:
            _CACHE.clear()
        _CACHE[currency] = (time.monotonic(), rate, day)
        return rate, day
