"""Price feed: providers and the FeedRunner polling thread."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Protocol

import requests

log = logging.getLogger("pricewatch.feed")

START_PRICES = {
    "AAPL": 190.0,
    "MSFT": 420.0,
    "NVDA": 120.0,
    "TSLA": 250.0,
    "BTC": 65000.0,
    "ETH": 3200.0,
}


class ProviderError(Exception):
    pass


class Provider(Protocol):
    name: str

    def fetch(self) -> dict[str, float]:
        """Return {symbol: price}. Raise ProviderError on failure."""


class SimulatedProvider:
    """Seedable random walk with an occasional 1-2% shock so alerts fire in demos."""

    name = "simulated"

    def __init__(
        self,
        symbols,
        seed: int | None = None,
        volatility: float = 0.0006,
        shock_probability: float = 0.02,
    ):
        self._rng = random.Random(seed)  # noqa: S311 - simulation, not security
        self._prices = {s: START_PRICES.get(s, 100.0) for s in symbols}
        self._vol = volatility
        self._shock_p = shock_probability

    def fetch(self) -> dict[str, float]:
        for symbol, price in self._prices.items():
            ret = self._rng.gauss(0.0, self._vol)
            if self._rng.random() < self._shock_p:
                shock = self._rng.uniform(0.01, 0.02)
                ret += shock if self._rng.random() < 0.5 else -shock
            self._prices[symbol] = max(0.01, round(price * (1.0 + ret), 4))
        return dict(self._prices)


COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "DOGE": "dogecoin",
    "ADA": "cardano",
    "XRP": "ripple",
}
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"


class CoinGeckoProvider:
    """Live crypto prices from CoinGecko's free public API.

    `http_get` is injectable (default requests.get) so tests use mocks.
    """

    name = "coingecko"

    def __init__(self, symbols, http_get: Callable = requests.get, timeout: float = 10.0):
        unsupported = [s for s in symbols if s not in COINGECKO_IDS]
        if unsupported:
            raise ValueError(
                f"CoinGecko provider does not support: {', '.join(unsupported)} "
                f"(supported: {', '.join(COINGECKO_IDS)})"
            )
        self._ids = {s: COINGECKO_IDS[s] for s in symbols}
        self._get = http_get
        self._timeout = timeout

    def fetch(self) -> dict[str, float]:
        params = {"ids": ",".join(self._ids.values()), "vs_currencies": "usd"}
        try:
            resp = self._get(COINGECKO_URL, params=params, timeout=self._timeout)
            if resp.status_code != 200:
                raise ProviderError(f"CoinGecko returned HTTP {resp.status_code}")
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"CoinGecko request failed: {exc}") from exc
        prices: dict[str, float] = {}
        for symbol, cg_id in self._ids.items():
            try:
                price = float(data[cg_id]["usd"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError(f"CoinGecko response missing price for {symbol}") from exc
            if price <= 0:
                raise ProviderError(f"CoinGecko returned non-positive price for {symbol}")
            prices[symbol] = price
        return prices


def backoff_delay(failures: int, base: float, cap: float) -> float:
    """Delay after `failures` consecutive failures: base, 2*base, 4*base ... capped."""
    return min(cap, base * (2 ** max(0, failures - 1)))


class FeedRunner:
    """Polls a provider on a thread. Backs off exponentially on failure and
    recovers by itself. `poll_once` holds all the logic so tests need no threads or sleeps."""

    def __init__(
        self,
        provider: Provider,
        on_prices: Callable[[dict[str, float], int], None],
        interval: float = 1.0,
        max_backoff: float = 60.0,
        clock: Callable[[], float] = time.time,
    ):
        self.provider = provider
        self._on_prices = on_prices
        self.interval = interval
        self.max_backoff = max_backoff
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._failures = 0
        self._last_error: str | None = None
        self._last_success_ms: int | None = None
        self._polls_ok = 0
        self._started = False

    def poll_once(self) -> float:
        """Fetch once, publish, and return how long to wait before the next poll."""
        try:
            prices = self.provider.fetch()
            ts_ms = int(self._clock() * 1000)
            self._on_prices(prices, ts_ms)
        except Exception as exc:  # noqa: BLE001 - any provider/handler failure triggers backoff
            with self._lock:
                self._failures += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                delay = backoff_delay(self._failures, max(self.interval, 1.0), self.max_backoff)
            log.warning("feed poll failed (%s); retrying in %.0fs", exc, delay)
            return delay
        with self._lock:
            if self._failures:
                log.info("feed recovered after %d failures", self._failures)
            self._failures = 0
            self._last_error = None
            self._last_success_ms = ts_ms
            self._polls_ok += 1
        return self.interval

    def _run(self) -> None:
        while not self._stop.is_set():
            delay = self.poll_once()
            self._stop.wait(delay)

    def start(self) -> None:
        self._started = True
        self._thread = threading.Thread(target=self._run, name="feed", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def health(self) -> dict:
        with self._lock:
            if self._stop.is_set():
                status = "stopped"
            elif self._failures:
                status = "degraded"
            elif self._polls_ok:
                status = "ok"
            else:
                status = "starting"
            return {
                "provider": self.provider.name,
                "status": status,
                "consecutive_failures": self._failures,
                "last_error": self._last_error,
                "last_success_ms": self._last_success_ms,
                "polls_ok": self._polls_ok,
                "interval_seconds": self.interval,
            }
