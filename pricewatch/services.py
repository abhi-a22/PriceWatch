"""Wires the components together. Everything here lives in one process."""

from __future__ import annotations

import logging
import time

import requests

from .bus import PartitionedBus
from .config import Config
from .db import Database
from .engine import RuleEngine
from .feed import CoinGeckoProvider, FeedRunner, Provider, SimulatedProvider
from .security import RateLimiter, hash_password
from .store import PriceStore
from .webhooks import WebhookDispatcher

log = logging.getLogger("pricewatch")

COINGECKO_MIN_INTERVAL = 10.0  # the free API is rate limited


def build_provider(cfg: Config) -> Provider:
    if cfg.feed == "coingecko":
        return CoinGeckoProvider(cfg.symbols)
    return SimulatedProvider(cfg.symbols, seed=cfg.seed)


class Services:
    def __init__(
        self,
        cfg: Config,
        provider: Provider | None = None,
        http_post=requests.post,
        resolver=None,
    ):
        self.cfg = cfg
        self.resolver = resolver  # None means the real DNS resolver
        self.started_at = time.time()
        self.db = Database(cfg.db_path)
        self.db.init_schema()
        self.store = PriceStore(cfg.symbols, history_seconds=cfg.history_seconds)
        self.webhooks = WebhookDispatcher(self.db, cfg, http_post=http_post, resolver=resolver)
        self.engine = RuleEngine(self.db, self.store, on_fired=self.webhooks.enqueue)
        self.bus = PartitionedBus(
            self.engine.on_tick, partitions=cfg.partitions, queue_size=cfg.queue_size
        )
        self.provider = provider or build_provider(cfg)
        interval = cfg.tick_interval
        if self.provider.name == "coingecko":
            interval = max(interval, COINGECKO_MIN_INTERVAL)
        self.feed = FeedRunner(
            self.provider, self.ingest, interval=interval, max_backoff=cfg.feed_max_backoff
        )
        self.limiter = RateLimiter(cfg.rate_limit_attempts, cfg.rate_limit_window)
        # Verified against when a login names an unknown email, so both paths cost
        # one PBKDF2 and response time does not reveal which emails exist.
        self.dummy_hash = hash_password("pricewatch-dummy-password", cfg.pbkdf2_iterations)

    def ingest(self, prices: dict[str, float], ts_ms: int) -> None:
        """Feed callback: store each tick, then hand it to the bus."""
        for symbol, price in prices.items():
            if self.store.update(symbol, price, ts_ms):
                self.bus.submit(symbol, (price, ts_ms))

    def start(self, feed: bool = True) -> None:
        self.webhooks.start()
        self.bus.start()
        if feed:
            self.feed.start()

    def stop(self) -> None:
        """Graceful shutdown, in data-flow order so nothing is lost mid-pipeline."""
        self.feed.stop()
        self.bus.stop()  # drains queued ticks (which may enqueue webhooks)
        self.webhooks.stop()  # then finishes queued deliveries
        self.db.close_thread_connection()
