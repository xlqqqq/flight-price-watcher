from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal


class ConfigError(ValueError):
    pass


class ProviderError(RuntimeError):
    pass


class ProviderUnsupported(ProviderError):
    """A source cannot reliably query the requested market or filters."""


@dataclass(frozen=True)
class Route:
    id: str
    name: str
    origin: str
    destination: str
    provider: str
    currency: str = "CNY"
    mode: str = "both"
    threshold: Decimal | None = None
    dates: tuple[date, ...] = ()
    start_offset_days: int = 7
    end_offset_days: int = 9
    stay_nights: int | None = None
    nonstop: bool = False
    travel_class: int = 1
    market: str = "domestic"
    sources: tuple[str, ...] = ()
    # ``origin``/``destination`` remain the selected query code for backwards
    # compatibility: a city code for all-airports scope, an airport code for
    # airport scope.  The owning city is kept separately for providers that
    # need both values in their request or response validation.
    origin_scope: str = "city"
    destination_scope: str = "city"
    origin_city_code: str = ""
    destination_city_code: str = ""
    origin_label: str = ""
    destination_label: str = ""

    def city_code(self, side: str) -> str:
        if side not in {"origin", "destination"}:
            raise ValueError("地点方向必须是 origin 或 destination")
        code = getattr(self, f"{side}_city_code")
        if code:
            return code
        return getattr(self, side) if getattr(self, f"{side}_scope") == "city" else ""

    def airport_code(self, side: str) -> str:
        if side not in {"origin", "destination"}:
            raise ValueError("地点方向必须是 origin 或 destination")
        return getattr(self, side) if getattr(self, f"{side}_scope") == "airport" else ""

    def departure_dates(self, today: date) -> list[date]:
        if self.dates:
            return sorted({day for day in self.dates if day >= today})
        return [today + timedelta(days=n)
                for n in range(self.start_offset_days, self.end_offset_days + 1)]

    def return_on(self, departure: date) -> date | None:
        return departure + timedelta(days=self.stay_nights) if self.stay_nights else None

    def state_key(self) -> str:
        # A changed route/filter/budget is a new monitor, never reuse stale alert state.
        fields = dict(self.__dict__)
        fields.pop("name")
        # Keep historic monitor keys stable for Routes made by older configs.
        fields.pop("origin_label")
        fields.pop("destination_label")
        for key in ("origin_city_code", "destination_city_code"):
            if not fields[key] or (fields[key.replace("_city_code", "_scope")] == "city"
                                   and fields[key] == fields[key.replace("_city_code", "")]):
                fields.pop(key)
        if fields["origin_scope"] == fields["destination_scope"] == "city":
            fields.pop("origin_scope")
            fields.pop("destination_scope")
        return hashlib.sha256(json.dumps(fields, default=str, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Quote:
    origin: str
    destination: str
    departure_date: date
    price: Decimal
    currency: str
    source: str
    return_date: date | None = None
    airline: str = ""
    flight_number: str = ""
    stops: int | None = None
    url: str = ""
    price_note: str = "平台显示价格；税费、行李及最终可售价格请到购票页确认"
    provider: str = ""
    price_basis: str = "total"
    original_price: Decimal | None = None
    original_currency: str = ""
    exchange_rate: Decimal | None = None
    exchange_date: date | None = None
    # These are the actual first and last airports returned by a provider.
    # They stay empty for calendar-only sources that do not identify a flight.
    origin_airport: str = ""
    destination_airport: str = ""

    def __post_init__(self):
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("机票价格必须是有限正数")
        if self.price_basis not in {"total", "base", "unknown"}:
            raise ValueError("未知票价口径")
        for code in (self.origin_airport, self.destination_airport):
            if code and (len(code) != 3 or not code.isascii() or not code.isalpha()
                         or code.upper() != code):
                raise ValueError("实际起降机场必须是大写 IATA 三字码")

    @property
    def comparable(self) -> bool:
        return self.price_basis == "total"


@dataclass
class SearchResult:
    quotes: list[Quote]
    warnings: list[str]
    sources: list[dict] = field(default_factory=list)
