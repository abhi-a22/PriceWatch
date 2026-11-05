import hashlib
import hmac
import json
import socket
import threading
from dataclasses import replace

import pytest
from conftest import T0, add_alert, add_user
from stubs import Stub

from pricewatch.webhooks import WebhookDispatcher, sign


@pytest.fixture
def stub_factory():
    stubs = []

    def make(statuses):
        s = Stub(statuses)
        stubs.append(s)
        return s

    yield make
    for s in stubs:
        s.close()


def make_event(db, url, **alert_kw):
    n = db.conn().execute("SELECT COUNT(*) FROM users").fetchone()[0]
    uid = add_user(db, f"user{n}@example.com")
    aid = add_alert(
        db, uid, threshold=100.0, webhook_url=url, rule_type="above", repeating=1, **alert_kw
    )
    cur = db.conn().execute(
        "INSERT INTO alert_events (alert_id, user_id, symbol, price, message, tick_ts_ms, "
        "fired_at, delivery_status) VALUES (?, ?, 'AAPL', 101.5, 'AAPL is up', ?, "
        "'2023-11-14T22:13:20.000+00:00', 'pending')",
        (aid, uid, T0),
    )
    return aid, cur.lastrowid


def row(db, event_id):
    return db.conn().execute("SELECT * FROM alert_events WHERE id = ?", (event_id,)).fetchone()


def dispatcher(db, cfg, **kw):
    sleeps = []
    d = WebhookDispatcher(db, cfg, sleep=sleeps.append, **kw)
    d.sleeps = sleeps
    return d


def test_success_marks_delivered(db, cfg, stub_factory):
    stub = stub_factory([200])
    _, ev = make_event(db, stub.url)
    assert dispatcher(db, cfg).deliver(ev) == "delivered"
    r = row(db, ev)
    assert (r["delivery_status"], r["delivery_attempts"], r["delivery_error"]) == (
        "delivered",
        1,
        None,
    )
    assert len(stub.requests) == 1


def test_payload_contents_and_signature_verify(db, cfg, stub_factory):
    stub = stub_factory([204])
    aid, ev = make_event(db, stub.url, cooldown_seconds=30)
    dispatcher(db, cfg).deliver(ev)
    _, headers, body = stub.requests[0]
    # A receiver verifies the signature with the shared secret over the raw body.
    expected = "sha256=" + hmac.new(cfg.signing_key, body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(headers["X-PriceWatch-Signature"], expected)
    assert expected == sign(body, cfg.signing_key)
    payload = json.loads(body)
    assert payload["event_id"] == ev and payload["alert_id"] == aid
    assert payload["symbol"] == "AAPL" and payload["price"] == 101.5
    assert payload["message"] == "AAPL is up"
    assert payload["fired_at"] == "2023-11-14T22:13:20.000+00:00"
    assert payload["rule"] == {
        "type": "above",
        "threshold": 100.0,
        "window_seconds": None,
        "repeating": True,
        "cooldown_seconds": 30,
    }
    assert headers["Content-Type"] == "application/json"


def test_tampered_body_fails_signature(db, cfg, stub_factory):
    stub = stub_factory([200])
    _, ev = make_event(db, stub.url)
    dispatcher(db, cfg).deliver(ev)
    _, headers, body = stub.requests[0]
    forged = sign(body + b" ", cfg.signing_key)
    assert forged != headers["X-PriceWatch-Signature"]
    assert sign(body, b"another-secret") != headers["X-PriceWatch-Signature"]


def test_retry_then_success_with_exponential_backoff(db, cfg, stub_factory):
    cfg = replace(cfg, webhook_backoff_base=1.0, webhook_attempts=3)
    stub = stub_factory([500, 503, 200])
    _, ev = make_event(db, stub.url)
    d = dispatcher(db, cfg)
    assert d.deliver(ev) == "delivered"
    assert len(stub.requests) == 3
    assert d.sleeps == [1.0, 2.0]
    r = row(db, ev)
    assert r["delivery_status"] == "delivered" and r["delivery_attempts"] == 3


def test_exhausted_retries_mark_failed_with_last_error(db, cfg, stub_factory):
    cfg = replace(cfg, webhook_attempts=3)
    stub = stub_factory([500, 500, 502])
    _, ev = make_event(db, stub.url)
    assert dispatcher(db, cfg).deliver(ev) == "failed"
    r = row(db, ev)
    assert r["delivery_status"] == "failed" and r["delivery_attempts"] == 3
    assert r["delivery_error"] == "HTTP 502"
    assert len(stub.requests) == 3


def test_connection_refused_is_retried_then_failed(db, cfg):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    _, ev = make_event(db, f"http://127.0.0.1:{port}/hook")
    assert dispatcher(db, cfg).deliver(ev) == "failed"
    r = row(db, ev)
    assert r["delivery_attempts"] == cfg.webhook_attempts
    assert "ConnectionError" in r["delivery_error"]


def test_redirects_are_not_followed(db, cfg, stub_factory):
    stub = stub_factory([302])
    _, ev = make_event(db, stub.url)
    assert dispatcher(db, cfg).deliver(ev) == "failed"
    assert {path for path, _, _ in stub.requests} == {"/hook"}  # /redirected never requested
    assert "redirects are not followed" in row(db, ev)["delivery_error"]


def test_url_revalidated_at_delivery_blocks_private_target(db, cfg, stub_factory):
    stub = stub_factory([200])
    _, ev = make_event(db, stub.url)  # saved while private addresses were allowed
    strict = replace(cfg, allow_private_webhooks=False)
    assert dispatcher(db, strict).deliver(ev) == "failed"
    assert stub.requests == []  # the request was never made
    r = row(db, ev)
    assert r["delivery_error"].startswith("blocked:") and r["delivery_attempts"] == 1


def test_dns_rebinding_between_save_and_delivery_is_caught(db, cfg):
    strict = replace(cfg, allow_private_webhooks=False)
    calls = []

    def rebinding_resolver(host, port, proto=0):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", ("169.254.169.254", port))]

    def post(*a, **k):
        raise AssertionError("must not be called")

    _, ev = make_event(db, "http://hooks.example.com/x")
    d = WebhookDispatcher(db, strict, http_post=post, resolver=rebinding_resolver)
    assert d.deliver(ev) == "failed"
    assert calls == ["hooks.example.com"]
    assert "blocked" in row(db, ev)["delivery_error"]


def test_request_uses_timeout_and_disables_redirects(db, cfg):
    seen = {}

    class R:
        status_code = 200

    def post(url, **kwargs):
        seen.update(kwargs)
        return R()

    _, ev = make_event(db, "http://127.0.0.1:1/x")
    WebhookDispatcher(db, cfg, http_post=post).deliver(ev)
    assert seen["allow_redirects"] is False and seen["timeout"] == cfg.webhook_timeout


def test_event_deleted_while_queued(db, cfg):
    assert dispatcher(db, cfg).deliver(99999) == "gone"


def test_worker_pool_delivers_queued_events_and_drains_on_stop(db, cfg, stub_factory):
    stub = stub_factory([200])
    events_ = [make_event(db, stub.url)[1] for _ in range(5)]
    d = WebhookDispatcher(db, cfg)
    d.start()
    for ev in events_:
        assert d.enqueue(ev)
    d.stop()
    assert all(row(db, ev)["delivery_status"] == "delivered" for ev in events_)
    assert d.stats()["delivered"] == 5


def test_full_queue_marks_event_failed_without_blocking(db, cfg):
    small = replace(cfg, webhook_queue_size=1)
    d = WebhookDispatcher(db, small)  # workers not started, so the queue fills
    _, e1 = make_event(db, "http://127.0.0.1:1/a")
    _, e2 = make_event(db, "http://127.0.0.1:1/b")
    assert d.enqueue(e1) is True
    assert d.enqueue(e2) is False
    assert row(db, e2)["delivery_status"] == "failed"
    assert row(db, e2)["delivery_error"] == "delivery queue full"
    assert d.stats()["queue_full"] == 1


def test_slow_endpoint_does_not_block_enqueue(db, cfg):
    gate = threading.Event()

    class R:
        status_code = 200

    def slow_post(*a, **k):
        gate.wait(5)
        return R()

    _, ev = make_event(db, "http://127.0.0.1:1/x")
    d = WebhookDispatcher(db, cfg, http_post=slow_post)
    d.start()
    import time

    t0 = time.monotonic()
    d.enqueue(ev)
    assert time.monotonic() - t0 < 0.5  # the caller (rule evaluation) never waits
    gate.set()
    d.stop()
