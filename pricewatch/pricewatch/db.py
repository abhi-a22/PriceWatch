"""SQLite access: WAL mode, one connection per thread, explicit transactions."""

from __future__ import annotations

import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol            TEXT NOT NULL,
    rule_type         TEXT NOT NULL
                      CHECK (rule_type IN ('above', 'below', 'pct_rise', 'pct_drop')),
    threshold         REAL NOT NULL,
    window_seconds    INTEGER,
    repeating         INTEGER NOT NULL DEFAULT 0,
    cooldown_seconds  INTEGER NOT NULL DEFAULT 0,
    webhook_url       TEXT,
    active            INTEGER NOT NULL DEFAULT 1,
    last_triggered_at INTEGER,          -- tick timestamp (epoch ms) of the last firing
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_symbol_active ON alerts(symbol, active);
CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id, id);

CREATE TABLE IF NOT EXISTS alert_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id          INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol            TEXT NOT NULL,
    price             REAL NOT NULL,
    message           TEXT NOT NULL,
    tick_ts_ms        INTEGER NOT NULL,
    fired_at          TEXT NOT NULL,
    delivery_status   TEXT NOT NULL DEFAULT 'none'
                      CHECK (delivery_status IN ('none', 'pending', 'delivered', 'failed')),
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivery_error    TEXT,
    UNIQUE (alert_id, tick_ts_ms)
);
CREATE INDEX IF NOT EXISTS idx_events_user ON alert_events(user_id, id);
"""


class Database:
    """Hands out one SQLite connection per thread.

    Connections run in autocommit mode (isolation_level=None) so every
    transaction is explicit (BEGIN IMMEDIATE ... COMMIT) and visible in the code.
    """

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=10000")
            self._local.conn = c
        return c

    def close_thread_connection(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def init_schema(self) -> None:
        self.conn().executescript(SCHEMA)
