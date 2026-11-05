"""End to end through the real HTTP server, feed thread, bus, rule engine and webhooks."""

import hashlib
import hmac
import time

import requests
from conftest import RampProvider
from stubs import Stub


def wait_for(fn, timeout=10.0, interval=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("timed out waiting for condition")


def test_signup_alert_fires_and_signed_webhook_arrives(start_live, cfg):
    receiver = Stub([200])
    try:
        live = start_live(provider=RampProvider(), tick_interval=0.05)
        base = live.url

        # the dashboard and live prices are served over real HTTP
        page = requests.get(base + "/", timeout=5)
        assert page.status_code == 200 and "PriceWatch" in page.text
        prices = wait_for(
            lambda: requests.get(base + "/api/prices?spark=60", timeout=5).json()["prices"]
        )
        assert prices[0]["symbol"] == "AAPL" and prices[0]["price"] >= 100.0

        # sign up, create an alert with a webhook
        r = requests.post(
            base + "/api/auth/register",
            json={"email": "e2e@example.com", "password": "correct-horse-1"},
            timeout=10,
        )
        assert r.status_code == 201
        headers = {"Authorization": "Bearer " + r.json()["token"]}
        r = requests.post(
            base + "/api/alerts",
            json={
                "symbol": "AAPL",
                "rule_type": "above",
                "threshold": 101.0,
                "webhook_url": receiver.url,
            },
            headers=headers,
            timeout=5,
        )
        assert r.status_code == 201
        alert_id = r.json()["id"]

        # the ramp crosses 101 within a few polls: the alert fires and the webhook is delivered
        event = wait_for(
            lambda: next(
                (
                    e
                    for e in requests.get(base + "/api/events", headers=headers, timeout=5).json()[
                        "events"
                    ]
                    if e["delivery_status"] == "delivered"
                ),
                None,
            )
        )
        assert event["alert_id"] == alert_id and event["price"] >= 101.0
        assert event["delivery_attempts"] == 1

        # one-time alert deactivated itself and fired exactly once
        alert = requests.get(f"{base}/api/alerts/{alert_id}", headers=headers, timeout=5).json()
        assert alert["active"] is False and alert["last_triggered_at"] is not None
        time.sleep(0.3)  # more ticks arrive, but a one-time alert must not fire again
        events = requests.get(base + "/api/events", headers=headers, timeout=5).json()["events"]
        assert len(events) == 1 and len(receiver.requests) == 1

        # the receiver can verify the HMAC signature over the raw body
        _, req_headers, body = receiver.requests[0]
        expected = "sha256=" + hmac.new(cfg.signing_key, body, hashlib.sha256).hexdigest()
        assert req_headers["X-PriceWatch-Signature"] == expected

        # health reflects a working pipeline
        health = requests.get(base + "/api/health", timeout=5).json()
        assert health["status"] == "ok" and health["feed"]["provider"] == "ramp"
        assert health["bus"]["processed"] > 0 and health["bus"]["errors"] == 0
        assert health["bus"]["latency_ms"]["p95"] >= 0
        assert health["webhooks"]["delivered"] == 1
    finally:
        receiver.close()


def test_failed_webhook_can_be_retried_over_http(start_live):
    flaky = Stub([500])
    try:
        live = start_live(provider=RampProvider(), tick_interval=0.05, webhook_attempts=1)
        base = live.url
        r = requests.post(
            base + "/api/auth/register",
            json={"email": "retry@example.com", "password": "correct-horse-1"},
            timeout=10,
        )
        headers = {"Authorization": "Bearer " + r.json()["token"]}
        requests.post(
            base + "/api/alerts",
            json={
                "symbol": "AAPL",
                "rule_type": "above",
                "threshold": 100.5,
                "webhook_url": flaky.url,
            },
            headers=headers,
            timeout=5,
        )
        failed = wait_for(
            lambda: next(
                (
                    e
                    for e in requests.get(base + "/api/events", headers=headers, timeout=5).json()[
                        "events"
                    ]
                    if e["delivery_status"] == "failed"
                ),
                None,
            )
        )
        assert failed["delivery_error"] == "HTTP 500"

        flaky.statuses[:] = [200]  # the receiver recovers
        r = requests.post(f"{base}/api/events/{failed['id']}/retry", headers=headers, timeout=5)
        assert r.status_code == 202
        wait_for(
            lambda: (
                requests.get(base + "/api/events", headers=headers, timeout=5).json()["events"][0][
                    "delivery_status"
                ]
                == "delivered"
            )
        )
    finally:
        flaky.close()
