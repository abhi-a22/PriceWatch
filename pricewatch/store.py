"""In-memory, thread-safe price store with bounded time-based history."""

from __future__ import annotations

import threading
from bisect import bisect_right


class PriceStore:
    """Latest price plus a time-bounded history per symbol.

    History is two parallel sorted lists (timestamps in epoch ms, prices) so a
    reference price can be found with bisect in O(log n).
    """

    def __init__(self, symbols, history_seconds: int = 660):
        self._history_ms = history_seconds * 1000
        self._ts: dict[str, list[int]] = {s: [] for s in symbols}
        self._px: dict[str, list[float]] = {s: [] for s in symbols}
        self._lock = threading.Lock()

    @property
    def symbols(self) -> list[str]:
        return list(self._ts)

    def update(self, symbol: str, price: float, ts_ms: int) -> bool:
        """Record a tick. Returns False (and stores nothing) for an unknown symbol
        or a stale/duplicate timestamp."""
        with self._lock:
            ts = self._ts.get(symbol)
            if ts is None:
                return False
            if ts and ts_ms <= ts[-1]:
                return False
            ts.append(ts_ms)
            px = self._px[symbol]
            px.append(price)
            # Drop history older than the window but always keep ONE point at or
            # before the cutoff, so a reference price still exists for the full window.
            i = bisect_right(ts, ts_ms - self._history_ms) - 1
            if i > 0:
                del ts[:i]
                del px[:i]
            return True

    def latest(self, symbol: str) -> tuple[float, int] | None:
        with self._lock:
            ts = self._ts.get(symbol)
            if not ts:
                return None
            return self._px[symbol][-1], ts[-1]

    def reference_price(self, symbol: str, at_ts_ms: int) -> float | None:
        """Price at or just before `at_ts_ms`; None until history reaches that far back."""
        with self._lock:
            ts = self._ts.get(symbol)
            if not ts:
                return None
            i = bisect_right(ts, at_ts_ms) - 1
            if i < 0:
                return None
            return self._px[symbol][i]

    def history(self, symbol: str, since_ts_ms: int | None = None) -> list[tuple[int, float]]:
        with self._lock:
            ts = self._ts.get(symbol)
            if ts is None:
                return []
            start = 0 if since_ts_ms is None else bisect_right(ts, since_ts_ms - 1)
            return list(zip(ts[start:], self._px[symbol][start:], strict=True))

    def sparkline(self, symbol: str, seconds: int = 60, points: int = 30) -> list[float]:
        """Prices over the last `seconds` (relative to the newest tick), thinned
        to at most `points` values."""
        last = self.latest(symbol)
        if last is None:
            return []
        data = self.history(symbol, since_ts_ms=last[1] - seconds * 1000)
        prices = [p for _, p in data]
        if len(prices) <= points:
            return prices
        step = (len(prices) - 1) / (points - 1)
        return [prices[round(i * step)] for i in range(points)]

    def change_pct(self, symbol: str, seconds: int = 60) -> float | None:
        last = self.latest(symbol)
        if last is None:
            return None
        ref = self.reference_price(symbol, last[1] - seconds * 1000)
        if ref is None or ref == 0:
            return None
        return (last[0] - ref) / ref * 100.0
