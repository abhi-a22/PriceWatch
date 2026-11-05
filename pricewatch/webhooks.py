"""Webhook delivery on its own queue and worker pool.

Rule evaluation only ever calls `enqueue`, which never blocks, so a slow or dead
endpoint cannot delay alert evaluation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import threading
import time
from collections.abc import Callable

import requests

from .config import Config
from .db import Database
from .security import UnsafeURL, validate_webhook_url

log = logging.getLogger("pricewatch.webhooks")
_STOP = object()


def sign(body: bytes, key: bytes) -> str:
    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


class WebhookDispatcher:
    def __init__(
        self,
        db: Database,
        cfg: Config,
        http_post: Callable = requests.post,
        resolver: Callable | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self._db = db
        self._cfg = cfg
        self._post = http_post
        self._resolver = resolver
        self._queue: queue.Queue = queue.Queue(maxsize=cfg.webhook_queue_size)
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._sleep = sleep or (lambda s: self._stopping.wait(s))
        self._lock = threading.Lock()
        self._counts = {"delivered": 0, "failed": 0, "queue_full": 0}

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        for i in range(self._cfg.webhook_workers):
            t = threading.Thread(target=self._worker, name=f"webhook-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 10.0) -> None:
        """Finish deliveries already queued, then exit. Retry sleeps are cut short."""
        self._stopping.set()
        for _ in self._threads:
            self._queue.put(_STOP)
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self._threads.clear()

    def enqueue(self, event_id: int) -> bool:
        try:
            self._queue.put_nowait(event_id)
            return True
        except queue.Full:
            with self._lock:
                self._counts["queue_full"] += 1
            self._set_status(event_id, "failed", error="delivery queue full")
            return False

    def stats(self) -> dict:
        with self._lock:
            return {**self._counts, "queued": self._queue.qsize()}

    # -- delivery ------------------------------------------------------------
    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            try:
                self.deliver(item)
            except Exception:  # noqa: BLE001 - never let a delivery bug kill the worker
                log.exception("unexpected error delivering event %s", item)

    def _set_status(self, event_id: int, status: str, attempts=None, error=None) -> None:
        conn = self._db.conn()
        if attempts is None:
            conn.execute(
                "UPDATE alert_events SET delivery_status = ?, delivery_error = ? WHERE id = ?",
                (status, error, event_id),
            )
        else:
            conn.execute(
                "UPDATE alert_events SET delivery_status = ?, delivery_attempts = ?, "
                "delivery_error = ? WHERE id = ?",
                (status, attempts, error, event_id),
            )

    def _load(self, event_id: int):
        return (
            self._db.conn()
            .execute(
                "SELECT e.*, a.webhook_url, a.rule_type, a.threshold, a.window_seconds, "
                "a.repeating, a.cooldown_seconds "
                "FROM alert_events e JOIN alerts a ON a.id = e.alert_id WHERE e.id = ?",
                (event_id,),
            )
            .fetchone()
        )

    def _payload(self, row) -> bytes:
        payload = {
            "event_id": row["id"],
            "alert_id": row["alert_id"],
            "symbol": row["symbol"],
            "price": row["price"],
            "message": row["message"],
            "fired_at": row["fired_at"],
            "rule": {
                "type": row["rule_type"],
                "threshold": row["threshold"],
                "window_seconds": row["window_seconds"],
                "repeating": bool(row["repeating"]),
                "cooldown_seconds": row["cooldown_seconds"],
            },
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()

    def deliver(self, event_id: int) -> str:
        """Deliver one event with retries. Returns the final delivery status."""
        row = self._load(event_id)
        if row is None:  # alert/event deleted while queued
            return "gone"
        url = row["webhook_url"]
        body = self._payload(row)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "PriceWatch-Webhook/1.0",
            "X-PriceWatch-Signature": sign(body, self._cfg.signing_key),
        }
        attempts = 0
        error = "no webhook configured"
        for attempt in range(1, self._cfg.webhook_attempts + 1):
            if not url:
                break
            # Re-validate on every attempt: DNS answers can change between
            # alert creation and delivery.
            try:
                kwargs = {"allow_private": self._cfg.allow_private_webhooks}
                if self._resolver is not None:
                    kwargs["resolver"] = self._resolver
                validate_webhook_url(url, **kwargs)
            except UnsafeURL as exc:
                error = f"blocked: {exc}"
                attempts = attempt
                break  # permanent: retrying cannot make an unsafe URL safe
            attempts = attempt
            try:
                resp = self._post(
                    url,
                    data=body,
                    headers=headers,
                    timeout=self._cfg.webhook_timeout,
                    allow_redirects=False,
                )
                if 200 <= resp.status_code < 300:
                    self._set_status(event_id, "delivered", attempts, None)
                    with self._lock:
                        self._counts["delivered"] += 1
                    return "delivered"
                if 300 <= resp.status_code < 400:
                    error = f"HTTP {resp.status_code} (redirects are not followed)"
                else:
                    error = f"HTTP {resp.status_code}"
            except requests.RequestException as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
            self._set_status(event_id, "pending", attempts, error)
            if attempt < self._cfg.webhook_attempts:
                self._sleep(self._cfg.webhook_backoff_base * 2 ** (attempt - 1))
                if self._stopping.is_set():
                    break
        self._set_status(event_id, "failed", attempts, error)
        with self._lock:
            self._counts["failed"] += 1
        return "failed"
