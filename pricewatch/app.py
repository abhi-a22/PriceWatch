"""Flask application: REST API and dashboard hosting."""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import time
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from . import __version__
from .engine import PCT_TYPES, RULE_TYPES
from .security import (
    InvalidToken,
    UnsafeURL,
    decode_token,
    hash_password,
    issue_token,
    validate_webhook_url,
    verify_password,
)
from .services import Services

log = logging.getLogger("pricewatch.api")

STATIC_DIR = Path(__file__).parent / "static"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
CREATE_FIELDS = {
    "symbol", "rule_type", "threshold", "window_seconds",
    "repeating", "cooldown_seconds", "webhook_url",
}  # fmt: skip
PATCH_FIELDS = {"active", "threshold", "repeating", "cooldown_seconds", "webhook_url"}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ------------------------------------------------------------------ validation
def _is_number(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def _int_in_range(v, name: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ApiError(f"{name} must be an integer")
    if not lo <= v <= hi:
        raise ApiError(f"{name} must be between {lo} and {hi}")
    return v


def _bool(v, name: str) -> bool:
    if not isinstance(v, bool):
        raise ApiError(f"{name} must be true or false")
    return v


def _threshold(v, rule_type: str) -> float:
    if not _is_number(v):
        raise ApiError("threshold must be a number")
    limit = {"above": 1e9, "below": 1e9, "pct_rise": 1000.0, "pct_drop": 100.0}[rule_type]
    if not 0 < v <= limit:
        raise ApiError(f"threshold must be greater than 0 and at most {limit:g} for {rule_type}")
    return float(v)


def _credentials(body) -> tuple[str, str]:
    email, password = body.get("email"), body.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        raise ApiError("email and password are required strings")
    email = email.strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise ApiError("email is not valid")
    if not 8 <= len(password) <= 128:
        raise ApiError("password must be 8 to 128 characters")
    return email, password


# ------------------------------------------------------------------- the app
def create_app(svc: Services) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = svc.cfg.max_body_bytes
    app.json.sort_keys = False
    app.svc = svc  # type: ignore[attr-defined]
    cfg, db = svc.cfg, svc.db

    # -- plumbing ------------------------------------------------------------
    def body_json() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ApiError("request body must be a JSON object")
        return data

    def current_user_id() -> int:
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise ApiError("authentication required", 401)
        try:
            uid = decode_token(token.strip(), cfg.jwt_secret)
        except InvalidToken:
            raise ApiError("invalid or expired token", 401) from None
        if db.conn().execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is None:
            raise ApiError("invalid or expired token", 401)
        return uid

    def auth(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            g.user_id = current_user_id()
            return fn(*args, **kwargs)

        return wrapper

    def rate_limited():
        if not svc.limiter.allow(request.remote_addr or "unknown"):
            raise ApiError("too many attempts, try again later", 429)

    @app.errorhandler(ApiError)
    def on_api_error(e: ApiError):
        resp = jsonify({"error": e.message})
        resp.status_code = e.status
        if e.status == 429:
            resp.headers["Retry-After"] = str(int(cfg.rate_limit_window))
        return resp

    @app.errorhandler(HTTPException)
    def on_http_error(e: HTTPException):
        msg = {
            404: "not found",
            405: "method not allowed",
            413: f"request body too large (max {cfg.max_body_bytes} bytes)",
        }.get(e.code or 500, e.description or e.name)
        resp = jsonify({"error": msg})
        resp.status_code = e.code or 500
        return resp

    @app.errorhandler(Exception)
    def on_unexpected(e: Exception):
        log.exception("unhandled error")
        resp = jsonify({"error": "internal server error"})
        resp.status_code = 500
        return resp

    @app.before_request
    def reject_oversized():
        if request.content_length is not None and request.content_length > cfg.max_body_bytes:
            raise ApiError(f"request body too large (max {cfg.max_body_bytes} bytes)", 413)

    @app.after_request
    def security_headers(resp):
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.teardown_request
    def release_connection(_exc):
        # Werkzeug serves each request on its own short-lived thread, so close that
        # thread's connection instead of leaking one per request.
        db.close_thread_connection()

    # -- dashboard -----------------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    # -- auth ----------------------------------------------------------------
    def auth_response(user_id: int, email: str, status: int):
        token = issue_token(user_id, cfg.jwt_secret, cfg.jwt_ttl_seconds)
        return jsonify({"token": token, "user": {"id": user_id, "email": email}}), status

    @app.post("/api/auth/register")
    def register():
        rate_limited()
        email, password = _credentials(body_json())
        pw_hash = hash_password(password, cfg.pbkdf2_iterations)
        try:
            cur = db.conn().execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, pw_hash, datetime.now(UTC).isoformat(timespec="seconds")),
            )
        except sqlite3.IntegrityError:
            raise ApiError("an account with this email already exists", 409) from None
        return auth_response(cur.lastrowid, email, 201)

    @app.post("/api/auth/login")
    def login():
        rate_limited()
        body = body_json()
        email, password = body.get("email"), body.get("password")
        if not isinstance(email, str) or not isinstance(password, str):
            raise ApiError("email and password are required strings")
        row = (
            db.conn()
            .execute(
                "SELECT id, email, password_hash FROM users WHERE email = ?",
                (email.strip().lower(),),
            )
            .fetchone()
        )
        # Always run exactly one PBKDF2 verification, against a dummy hash for
        # unknown emails, and return one error for both failure modes.
        ok = verify_password(password, row["password_hash"] if row else svc.dummy_hash)
        if row is None or not ok:
            raise ApiError("invalid email or password", 401)
        return auth_response(row["id"], row["email"], 200)

    # -- prices --------------------------------------------------------------
    @app.get("/api/prices")
    def prices():
        spark = request.args.get("spark")
        spark_seconds = None
        if spark is not None:
            try:
                spark_seconds = int(spark)
            except ValueError:
                raise ApiError("spark must be an integer number of seconds") from None
            if not 1 <= spark_seconds <= 600:
                raise ApiError("spark must be between 1 and 600")
        out = []
        for symbol in svc.store.symbols:
            latest = svc.store.latest(symbol)
            if latest is None:
                continue
            item = {
                "symbol": symbol,
                "price": latest[0],
                "ts_ms": latest[1],
                "change_pct_1m": svc.store.change_pct(symbol, 60),
            }
            if spark_seconds:
                item["spark"] = svc.store.sparkline(symbol, spark_seconds)
            out.append(item)
        return jsonify({"provider": svc.provider.name, "prices": out})

    @app.get("/api/prices/<symbol>/history")
    def price_history(symbol):
        symbol = symbol.upper()
        if symbol not in svc.store.symbols:
            raise ApiError("unknown symbol", 404)
        try:
            seconds = int(request.args.get("seconds", "300"))
        except ValueError:
            raise ApiError("seconds must be an integer") from None
        if not 1 <= seconds <= cfg.history_seconds:
            raise ApiError(f"seconds must be between 1 and {cfg.history_seconds}")
        last = svc.store.latest(symbol)
        points = svc.store.history(symbol, last[1] - seconds * 1000) if last else []
        if len(points) > 600:
            step = (len(points) - 1) / 599
            points = [points[round(i * step)] for i in range(600)]
        return jsonify({"symbol": symbol, "points": [[t, p] for t, p in points]})

    # -- alerts --------------------------------------------------------------
    def alert_json(r) -> dict:
        return {
            "id": r["id"],
            "symbol": r["symbol"],
            "rule_type": r["rule_type"],
            "threshold": r["threshold"],
            "window_seconds": r["window_seconds"],
            "repeating": bool(r["repeating"]),
            "cooldown_seconds": r["cooldown_seconds"],
            "webhook_url": r["webhook_url"],
            "active": bool(r["active"]),
            "last_triggered_at": r["last_triggered_at"],  # tick time, epoch ms
            "created_at": r["created_at"],
        }

    def check_webhook(url):
        if url is None:
            return None
        kwargs = {"allow_private": cfg.allow_private_webhooks}
        if svc.resolver is not None:
            kwargs["resolver"] = svc.resolver
        try:
            return validate_webhook_url(url, **kwargs)
        except UnsafeURL as exc:
            raise ApiError(str(exc)) from None

    def fetch_alert(alert_id: int):
        row = (
            db.conn()
            .execute("SELECT * FROM alerts WHERE id = ? AND user_id = ?", (alert_id, g.user_id))
            .fetchone()
        )
        if row is None:
            raise ApiError("alert not found", 404)
        return row

    @app.get("/api/alerts")
    @auth
    def list_alerts():
        rows = db.conn().execute("SELECT * FROM alerts WHERE user_id = ? ORDER BY id", (g.user_id,))
        return jsonify({"alerts": [alert_json(r) for r in rows]})

    @app.post("/api/alerts")
    @auth
    def create_alert():
        body = body_json()
        unknown = set(body) - CREATE_FIELDS
        if unknown:
            raise ApiError(f"unknown field(s): {', '.join(sorted(unknown))}")
        symbol = body.get("symbol")
        if not isinstance(symbol, str) or symbol.strip().upper() not in svc.store.symbols:
            raise ApiError(f"symbol must be one of: {', '.join(svc.store.symbols)}")
        symbol = symbol.strip().upper()
        rule_type = body.get("rule_type")
        if rule_type not in RULE_TYPES:
            raise ApiError(f"rule_type must be one of: {', '.join(RULE_TYPES)}")
        if "threshold" not in body:
            raise ApiError("threshold is required")
        threshold = _threshold(body["threshold"], rule_type)
        window = body.get("window_seconds")
        if rule_type in PCT_TYPES:
            if window is None:
                raise ApiError("window_seconds is required for percent rules")
            window = _int_in_range(window, "window_seconds", 10, 600)
        elif window is not None:
            raise ApiError("window_seconds only applies to percent rules")
        repeating = _bool(body.get("repeating", False), "repeating")
        cooldown = _int_in_range(body.get("cooldown_seconds", 0), "cooldown_seconds", 0, 86400)
        webhook = check_webhook(body.get("webhook_url"))

        conn = db.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE user_id = ?", (g.user_id,)
            ).fetchone()
            if count >= cfg.max_alerts_per_user:
                conn.execute("ROLLBACK")
                raise ApiError(f"alert limit reached ({cfg.max_alerts_per_user} per user)", 409)
            cur = conn.execute(
                "INSERT INTO alerts (user_id, symbol, rule_type, threshold, window_seconds, "
                "repeating, cooldown_seconds, webhook_url, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    g.user_id, symbol, rule_type, threshold, window, int(repeating),
                    cooldown, webhook, datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )  # fmt: skip
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        svc.engine.invalidate()
        return jsonify(alert_json(fetch_alert(cur.lastrowid))), 201

    @app.get("/api/alerts/<int:alert_id>")
    @auth
    def get_alert(alert_id):
        return jsonify(alert_json(fetch_alert(alert_id)))

    @app.patch("/api/alerts/<int:alert_id>")
    @auth
    def patch_alert(alert_id):
        row = fetch_alert(alert_id)
        body = body_json()
        unknown = set(body) - PATCH_FIELDS
        if unknown:
            raise ApiError(
                f"cannot change: {', '.join(sorted(unknown))} "
                f"(allowed: {', '.join(sorted(PATCH_FIELDS))})"
            )
        if not body:
            raise ApiError("no fields to update")
        updates: dict = {}
        if "active" in body:
            updates["active"] = int(_bool(body["active"], "active"))
        if "threshold" in body:
            updates["threshold"] = _threshold(body["threshold"], row["rule_type"])
        if "repeating" in body:
            updates["repeating"] = int(_bool(body["repeating"], "repeating"))
        if "cooldown_seconds" in body:
            updates["cooldown_seconds"] = _int_in_range(
                body["cooldown_seconds"], "cooldown_seconds", 0, 86400
            )
        if "webhook_url" in body:
            updates["webhook_url"] = check_webhook(body["webhook_url"])
        sets = ", ".join(f"{k} = ?" for k in updates)  # keys come from the fixed set above
        db.conn().execute(
            f"UPDATE alerts SET {sets} WHERE id = ? AND user_id = ?",  # noqa: S608
            (*updates.values(), alert_id, g.user_id),
        )
        svc.engine.invalidate()
        return jsonify(alert_json(fetch_alert(alert_id)))

    @app.delete("/api/alerts/<int:alert_id>")
    @auth
    def delete_alert(alert_id):
        fetch_alert(alert_id)
        db.conn().execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, g.user_id))
        svc.engine.invalidate()
        return "", 204

    # -- events --------------------------------------------------------------
    def event_json(r) -> dict:
        return {k: r[k] for k in r.keys() if k != "user_id"}  # noqa: SIM118

    @app.get("/api/events")
    @auth
    def list_events():
        try:
            limit = int(request.args.get("limit", "50"))
            before = request.args.get("before_id")
            before_id = int(before) if before is not None else None
        except ValueError:
            raise ApiError("limit and before_id must be integers") from None
        if not 1 <= limit <= 200:
            raise ApiError("limit must be between 1 and 200")
        sql = "SELECT * FROM alert_events WHERE user_id = ?"
        args: list = [g.user_id]
        if before_id is not None:
            sql += " AND id < ?"
            args.append(before_id)
        rows = db.conn().execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return jsonify(
            {
                "events": [event_json(r) for r in rows],
                "next_before_id": rows[-1]["id"] if len(rows) == limit else None,
            }
        )

    @app.post("/api/events/<int:event_id>/retry")
    @auth
    def retry_event(event_id):
        conn = db.conn()
        row = conn.execute(
            "SELECT * FROM alert_events WHERE id = ? AND user_id = ?", (event_id, g.user_id)
        ).fetchone()
        if row is None:
            raise ApiError("event not found", 404)
        if row["delivery_status"] != "failed":
            raise ApiError("only failed deliveries can be retried", 409)
        conn.execute(
            "UPDATE alert_events SET delivery_status = 'pending', delivery_attempts = 0, "
            "delivery_error = NULL WHERE id = ?",
            (event_id,),
        )
        if not svc.webhooks.enqueue(event_id):
            raise ApiError("delivery queue is full, try again shortly", 503)
        fresh = conn.execute("SELECT * FROM alert_events WHERE id = ?", (event_id,)).fetchone()
        return jsonify(event_json(fresh)), 202

    # -- health --------------------------------------------------------------
    @app.get("/api/health")
    def health():
        feed = svc.feed.health()
        return jsonify(
            {
                "status": "ok" if feed["status"] in ("ok", "starting") else "degraded",
                "version": __version__,
                "uptime_seconds": round(time.time() - svc.started_at, 1),
                "feed": feed,
                "bus": svc.bus.stats(),
                "webhooks": svc.webhooks.stats(),
            }
        )

    return app
