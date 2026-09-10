from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .models import Quote


class State:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=10)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, rule_key TEXT NOT NULL,
                observed_at TEXT NOT NULL, quote TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS observations_time ON observations(observed_at);
            CREATE TABLE IF NOT EXISTS best (
                rule_key TEXT PRIMARY KEY, price TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alerts (
                rule_key TEXT NOT NULL, kind TEXT NOT NULL,
                price TEXT NOT NULL, sent_at TEXT NOT NULL, receipt TEXT NOT NULL,
                PRIMARY KEY(rule_key, kind)
            );
        """)

    def close(self):
        self.connection.close()

    def observe(self, key: str, quote: Quote, now: datetime) -> Decimal | None:
        row = self.connection.execute("SELECT price FROM best WHERE rule_key=?", (key,)).fetchone()
        previous = Decimal(row[0]) if row else None
        with self.connection:
            self.connection.execute(
                "INSERT INTO observations(rule_key,observed_at,quote) VALUES(?,?,?)",
                (key, now.isoformat(), json.dumps(asdict(quote), ensure_ascii=False, default=str)),
            )
            if previous is None or quote.price < previous:
                self.connection.execute("INSERT OR REPLACE INTO best VALUES(?,?)", (key, str(quote.price)))
        return previous

    def last_alert(self, key: str, kind: str) -> tuple[Decimal, datetime] | None:
        row = self.connection.execute("SELECT price,sent_at FROM alerts WHERE rule_key=? AND kind=?",
                                      (key, kind)).fetchone()
        return (Decimal(row[0]), datetime.fromisoformat(row[1])) if row else None

    def mark_alert(self, key: str, kind: str, price: Decimal, now: datetime, receipt: str):
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO alerts VALUES(?,?,?,?,?)",
                                    (key, kind, str(price), now.isoformat(), receipt))

    def prune(self, now: datetime):
        # Keep one year of observations; all-time observed minimum remains in best.
        with self.connection:
            self.connection.execute("DELETE FROM observations WHERE observed_at < ?",
                                    ((now - timedelta(days=365)).isoformat(),))
