"""Configuration: environment variables with safe defaults."""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field

DEFAULT_SYMBOLS = ("AAPL", "MSFT", "NVDA", "TSLA", "BTC", "ETH")
DEFAULT_JWT_SECRET = "dev-only-insecure-secret-change-me-0123456789"  # noqa: S105


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    db_path: str = "pricewatch.db"
    jwt_secret: str = DEFAULT_JWT_SECRET
    webhook_secret: str | None = None  # derived from jwt_secret when None
    feed: str = "simulated"
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    tick_interval: float = 1.0
    seed: int | None = None
    partitions: int = 4
    host: str = "127.0.0.1"
    port: int = 8000
    allow_private_webhooks: bool = False

    # Less commonly changed knobs (tests override these).
    pbkdf2_iterations: int = 600_000
    jwt_ttl_seconds: int = 24 * 3600
    queue_size: int = 5000
    history_seconds: int = 660  # must cover the 600 s maximum rule window
    max_alerts_per_user: int = 50
    webhook_workers: int = 2
    webhook_queue_size: int = 1000
    webhook_attempts: int = 3
    webhook_backoff_base: float = 1.0
    webhook_timeout: float = 5.0
    rate_limit_attempts: int = 10
    rate_limit_window: float = 60.0
    max_body_bytes: int = 64 * 1024
    feed_max_backoff: float = 60.0
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.symbols = tuple(s.strip().upper() for s in self.symbols if s.strip())
        if not self.symbols:
            raise ValueError("at least one symbol is required")
        if self.feed not in {"simulated", "coingecko"}:
            raise ValueError("feed must be 'simulated' or 'coingecko'")
        if self.partitions < 1:
            raise ValueError("partitions must be >= 1")
        if self.tick_interval <= 0:
            raise ValueError("tick_interval must be > 0")
        if self.webhook_attempts < 1:
            raise ValueError("webhook_attempts must be >= 1")

    @property
    def signing_key(self) -> bytes:
        """Key used for the X-PriceWatch-Signature header."""
        if self.webhook_secret:
            return self.webhook_secret.encode()
        return hmac.new(self.jwt_secret.encode(), b"pricewatch-webhook", hashlib.sha256).digest()

    @property
    def uses_default_secret(self) -> bool:
        return self.jwt_secret == DEFAULT_JWT_SECRET

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        e = os.environ if env is None else env
        kwargs: dict = {}
        if "PRICEWATCH_DB" in e:
            kwargs["db_path"] = e["PRICEWATCH_DB"]
        if "PRICEWATCH_JWT_SECRET" in e:
            kwargs["jwt_secret"] = e["PRICEWATCH_JWT_SECRET"]
        if "PRICEWATCH_WEBHOOK_SECRET" in e:
            kwargs["webhook_secret"] = e["PRICEWATCH_WEBHOOK_SECRET"]
        if "PRICEWATCH_FEED" in e:
            kwargs["feed"] = e["PRICEWATCH_FEED"].strip().lower()
        if "PRICEWATCH_SYMBOLS" in e:
            kwargs["symbols"] = tuple(e["PRICEWATCH_SYMBOLS"].split(","))
        if "PRICEWATCH_TICK_INTERVAL" in e:
            kwargs["tick_interval"] = float(e["PRICEWATCH_TICK_INTERVAL"])
        if e.get("PRICEWATCH_SEED"):
            kwargs["seed"] = int(e["PRICEWATCH_SEED"])
        if "PRICEWATCH_PARTITIONS" in e:
            kwargs["partitions"] = int(e["PRICEWATCH_PARTITIONS"])
        if "PRICEWATCH_HOST" in e:
            kwargs["host"] = e["PRICEWATCH_HOST"]
        if "PRICEWATCH_PORT" in e:
            kwargs["port"] = int(e["PRICEWATCH_PORT"])
        if "PRICEWATCH_ALLOW_PRIVATE_WEBHOOKS" in e:
            kwargs["allow_private_webhooks"] = _bool(e["PRICEWATCH_ALLOW_PRIVATE_WEBHOOKS"])
        if "PRICEWATCH_PBKDF2_ITERATIONS" in e:
            kwargs["pbkdf2_iterations"] = int(e["PRICEWATCH_PBKDF2_ITERATIONS"])
        return cls(**kwargs)
