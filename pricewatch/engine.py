"""Rule engine: evaluates alert rules against each price tick."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .db import Database
from .store import PriceStore

log = logging.getLogger("pricewatch.engine")

RULE_TYPES = ("above", "below", "pct_rise", "pct_drop")
PCT_TYPES = ("pct_rise", "pct_drop")


@dataclass
class Rule:
    id: int
    user_id: int
    symbol: str
    rule_type: str
    threshold: float
    window_seconds: int | None
    repeating: bool
    cooldown_seconds: int
    webhook_url: str | None
    active: bool
    last_triggered_ms: int | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Rule:
        return cls(
            id=r["id"],
            user_id=r["user_id"],
            symbol=r["symbol"],
            rule_type=r["rule_type"],
            threshold=r["threshold"],
            window_seconds=r["window_seconds"],
            repeating=bool(r["repeating"]),
            cooldown_seconds=r["cooldown_seconds"],
            webhook_url=r["webhook_url"],
            active=bool(r["active"]),
            last_triggered_ms=r["last_triggered_at"],
        )


def _fmt(x: float) -> str:
    return f"{x:,.4f}".rstrip("0").rstrip(".")


def evaluate(rule: Rule, price: float, ref_price: float | None) -> str | None:
    """Return the alert message if the rule triggers, else None. Pure function.

    Thresholds are inclusive: a price exactly equal to the target fires.
    Percent rules return None when there is no reference price (history too short).
    """
    t = rule.rule_type
    now_at = f"{rule.symbol} is at {_fmt(price)}, at or"
    target = f"your target {_fmt(rule.threshold)}"
    if t == "above":
        return f"{now_at} above {target}" if price >= rule.threshold else None
    if t == "below":
        return f"{now_at} below {target}" if price <= rule.threshold else None
    if ref_price is None or ref_price <= 0:
        return None
    change = (price - ref_price) / ref_price * 100.0
    if t == "pct_rise" and change >= rule.threshold:
        return (
            f"{rule.symbol} rose {change:.2f}% in {rule.window_seconds}s "
            f"({_fmt(ref_price)} to {_fmt(price)}), threshold {_fmt(rule.threshold)}%"
        )
    if t == "pct_drop" and -change >= rule.threshold:
        return (
            f"{rule.symbol} fell {-change:.2f}% in {rule.window_seconds}s "
            f"({_fmt(ref_price)} to {_fmt(price)}), threshold {_fmt(rule.threshold)}%"
        )
    return None


class RuleEngine:
    """Consumes ticks (called from the bus workers) and fires alerts.

    Active rules are cached in memory grouped by symbol. Because the bus sends a
    given symbol to exactly one thread, the mutable per-rule state (last fire time,
    active flag) is only touched by one thread and needs no lock. The cache itself
    is rebuilt lazily after any create/update/delete (`invalidate`).
    """

    def __init__(
        self,
        db: Database,
        store: PriceStore,
        on_fired: Callable[[int], None] | None = None,
    ):
        self._db = db
        self._store = store
        self._on_fired = on_fired  # called with the new event id (webhook dispatch)
        self._cache: dict[str, list[Rule]] | None = None
        self._cache_lock = threading.Lock()
        self.cache_loads = 0  # exposed for tests/diagnostics

    # -- cache ---------------------------------------------------------------
    def invalidate(self) -> None:
        with self._cache_lock:
            self._cache = None

    def _rules_for(self, symbol: str) -> list[Rule]:
        cache = self._cache
        if cache is None:
            with self._cache_lock:
                if self._cache is None:
                    rows = self._db.conn().execute("SELECT * FROM alerts WHERE active = 1")
                    fresh: dict[str, list[Rule]] = {}
                    for row in rows:
                        fresh.setdefault(row["symbol"], []).append(Rule.from_row(row))
                    self._cache = fresh
                    self.cache_loads += 1
                cache = self._cache
        return cache.get(symbol, [])

    # -- evaluation ----------------------------------------------------------
    def on_tick(self, symbol: str, payload: tuple[float, int]) -> None:
        """Bus handler. payload = (price, tick_ts_ms)."""
        price, ts_ms = payload
        for rule in self._rules_for(symbol):
            if not rule.active:
                continue
            if (
                rule.last_triggered_ms is not None
                and rule.repeating
                and ts_ms - rule.last_triggered_ms < rule.cooldown_seconds * 1000
            ):
                continue
            ref = None
            if rule.rule_type in PCT_TYPES:
                ref = self._store.reference_price(symbol, ts_ms - rule.window_seconds * 1000)
            message = evaluate(rule, price, ref)
            if message is not None:
                self._fire(rule, price, ts_ms, message)

    def _fire(self, rule: Rule, price: float, ts_ms: int, message: str) -> None:
        """Atomically record the event and update the alert.

        UNIQUE(alert_id, tick_ts_ms) + INSERT OR IGNORE makes this idempotent: a
        replayed tick inserts nothing, so we change nothing and send nothing.
        """
        conn = self._db.conn()
        fired_at = datetime.fromtimestamp(ts_ms / 1000, UTC).isoformat(timespec="milliseconds")
        status = "pending" if rule.webhook_url else "none"
        one_time = not rule.repeating
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO alert_events "
                    "(alert_id, user_id, symbol, price, message, tick_ts_ms, fired_at, "
                    "delivery_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (rule.id, rule.user_id, rule.symbol, price, message, ts_ms, fired_at, status),
                )
                if cur.rowcount == 0:
                    conn.execute("ROLLBACK")
                    return
                event_id = cur.lastrowid
                conn.execute(
                    "UPDATE alerts SET last_triggered_at = ?, active = ? WHERE id = ?",
                    (ts_ms, 0 if one_time else 1, rule.id),
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        except sqlite3.IntegrityError:
            # The alert (or its user) was deleted while we were evaluating it:
            # the foreign key rejected the insert. Skip the rule, never crash the worker.
            log.info("alert %s vanished during evaluation; skipping", rule.id)
            rule.active = False
            return
        rule.last_triggered_ms = ts_ms
        if one_time:
            rule.active = False
        if rule.webhook_url and self._on_fired is not None:
            self._on_fired(event_id)
