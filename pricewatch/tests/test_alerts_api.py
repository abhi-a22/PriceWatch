import socket

import pytest
from conftest import T0, signup

from pricewatch.app import create_app


@pytest.fixture
def auth(client):
    return signup(client)


def make_alert(client, auth, **kw):
    body = {"symbol": "AAPL", "rule_type": "above", "threshold": 200}
    body.update(kw)
    return client.post("/api/alerts", json=body, headers=auth)


def test_create_each_rule_type_and_defaults(client, auth):
    r = make_alert(client, auth, symbol="aapl")
    assert r.status_code == 201
    a = r.get_json()
    assert a["symbol"] == "AAPL" and a["rule_type"] == "above" and a["threshold"] == 200.0
    assert a["repeating"] is False and a["cooldown_seconds"] == 0 and a["active"] is True
    assert a["window_seconds"] is None and a["webhook_url"] is None
    assert a["last_triggered_at"] is None and a["created_at"]

    r = make_alert(client, auth, rule_type="pct_rise", threshold=1.5, window_seconds=60,
                   repeating=True, cooldown_seconds=300)  # fmt: skip
    assert r.status_code == 201 and r.get_json()["window_seconds"] == 60
    assert make_alert(client, auth, rule_type="below", threshold=10.5).status_code == 201
    r = make_alert(client, auth, rule_type="pct_drop", threshold=100, window_seconds=600)
    assert r.status_code == 201


BAD_CREATES = [
    ({"symbol": "NOPE"}, "symbol"),
    ({"symbol": 5}, "symbol"),
    ({"symbol": None}, "symbol"),
    ({"rule_type": "sideways"}, "rule_type"),
    ({"rule_type": None}, "rule_type"),
    ({"threshold": True}, "threshold"),  # booleans are not numbers
    ({"threshold": "200"}, "threshold"),
    ({"threshold": None}, "threshold"),
    ({"threshold": 0}, "threshold"),
    ({"threshold": -5}, "threshold"),
    ({"threshold": 1e10}, "threshold"),
    ({"rule_type": "pct_drop", "threshold": 101, "window_seconds": 60}, "threshold"),
    ({"rule_type": "pct_rise", "threshold": 5}, "window_seconds"),  # window required
    ({"rule_type": "pct_rise", "threshold": 5, "window_seconds": 9}, "window_seconds"),
    ({"rule_type": "pct_rise", "threshold": 5, "window_seconds": 601}, "window_seconds"),
    ({"rule_type": "pct_rise", "threshold": 5, "window_seconds": 60.5}, "window_seconds"),
    ({"rule_type": "pct_rise", "threshold": 5, "window_seconds": True}, "window_seconds"),
    ({"rule_type": "pct_rise", "threshold": 5, "window_seconds": "60"}, "window_seconds"),
    ({"window_seconds": 60}, "window_seconds"),  # window on a price rule
    ({"repeating": "yes"}, "repeating"),
    ({"repeating": 1}, "repeating"),
    ({"cooldown_seconds": -1}, "cooldown_seconds"),
    ({"cooldown_seconds": 86401}, "cooldown_seconds"),
    ({"cooldown_seconds": True}, "cooldown_seconds"),
    ({"cooldown_seconds": 1.5}, "cooldown_seconds"),
    ({"webhook_url": 123}, "webhook_url"),
    ({"webhook_url": "ftp://example.com/x"}, "http"),
    ({"webhook_url": "http://user:pw@127.0.0.1/x"}, "credentials"),
    ({"surprise": 1}, "unknown field"),
    ({"user_id": 2}, "unknown field"),
]


@pytest.mark.parametrize(("override", "fragment"), BAD_CREATES)
def test_create_validation(client, auth, override, fragment):
    r = make_alert(client, auth, **override)
    assert r.status_code == 400, r.get_json()
    assert fragment in r.get_json()["error"]


def test_nan_and_infinity_thresholds_rejected(client, auth):
    for raw in ('{"symbol":"AAPL","rule_type":"above","threshold":NaN}',
                '{"symbol":"AAPL","rule_type":"above","threshold":Infinity}'):  # fmt: skip
        r = client.post("/api/alerts", data=raw, content_type="application/json", headers=auth)
        assert r.status_code == 400


def test_missing_threshold(client, auth):
    r = client.post("/api/alerts", json={"symbol": "AAPL", "rule_type": "above"}, headers=auth)
    assert r.status_code == 400 and "threshold" in r.get_json()["error"]


def test_auth_required_for_alerts(client):
    assert client.get("/api/alerts").status_code == 401
    assert client.post("/api/alerts", json={}).status_code == 401
    assert client.patch("/api/alerts/1", json={}).status_code == 401
    assert client.delete("/api/alerts/1").status_code == 401


def test_alert_limit_per_user(client, auth):
    for _ in range(50):
        assert make_alert(client, auth).status_code == 201
    r = make_alert(client, auth)
    assert r.status_code == 409 and "limit" in r.get_json()["error"]
    other = signup(client, "bob@example.com")  # limit is per user
    assert make_alert(client, other).status_code == 201


def test_list_get_patch_delete(client, auth):
    a = make_alert(client, auth).get_json()
    url = f"/api/alerts/{a['id']}"
    assert [x["id"] for x in client.get("/api/alerts", headers=auth).get_json()["alerts"]] == [
        a["id"]
    ]
    assert client.get(url, headers=auth).get_json()["id"] == a["id"]

    r = client.patch(url, json={"active": False, "threshold": 250.5, "repeating": True,
                                "cooldown_seconds": 60}, headers=auth)  # fmt: skip
    assert r.status_code == 200
    p = r.get_json()
    assert (p["active"], p["threshold"], p["repeating"], p["cooldown_seconds"]) == (
        False, 250.5, True, 60)  # fmt: skip
    assert client.get(url, headers=auth).get_json()["threshold"] == 250.5

    assert client.delete(url, headers=auth).status_code == 204
    assert client.get(url, headers=auth).status_code == 404
    assert client.delete(url, headers=auth).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"symbol": "MSFT"},
        {"rule_type": "below"},
        {"window_seconds": 60},
        {"user_id": 9},
        {"id": 5},
        {"created_at": "x"},
        {"last_triggered_at": 0},
        {},
        {"active": "no"},
        {"active": 0},
        {"threshold": True},
        {"threshold": -1},
        {"repeating": None},
        {"cooldown_seconds": 90000},
        {"webhook_url": "javascript:alert(1)"},
    ],
)
def test_patch_rejects_immutable_and_invalid_fields(client, auth, payload):
    a = make_alert(client, auth).get_json()
    r = client.patch(f"/api/alerts/{a['id']}", json=payload, headers=auth)
    assert r.status_code == 400
    assert client.get(f"/api/alerts/{a['id']}", headers=auth).get_json() == a  # unchanged


def test_patch_threshold_uses_the_alerts_rule_type_bounds(client, auth):
    a = make_alert(client, auth, rule_type="pct_drop", threshold=5, window_seconds=60).get_json()
    assert (
        client.patch(f"/api/alerts/{a['id']}", json={"threshold": 150}, headers=auth).status_code
        == 400
    )
    assert (
        client.patch(f"/api/alerts/{a['id']}", json={"threshold": 50}, headers=auth).status_code
        == 200
    )


def test_webhook_url_can_be_set_and_cleared(client, auth):
    a = make_alert(client, auth).get_json()
    url = f"/api/alerts/{a['id']}"
    r = client.patch(url, json={"webhook_url": "http://127.0.0.1:9/hook"}, headers=auth)
    assert r.get_json()["webhook_url"] == "http://127.0.0.1:9/hook"
    r = client.patch(url, json={"webhook_url": None}, headers=auth)
    assert r.get_json()["webhook_url"] is None


def test_alert_ids_must_be_integers(client, auth):
    assert client.get("/api/alerts/abc", headers=auth).status_code == 404


# -------------------------------------------------------- cross-user isolation
def test_users_cannot_see_or_change_each_others_alerts(client):
    alice, bob = signup(client, "alice@example.com"), signup(client, "bob@example.com")
    a = make_alert(client, alice).get_json()
    url = f"/api/alerts/{a['id']}"
    assert client.get(url, headers=bob).status_code == 404
    assert client.patch(url, json={"active": False}, headers=bob).status_code == 404
    assert client.delete(url, headers=bob).status_code == 404
    assert client.get("/api/alerts", headers=bob).get_json() == {"alerts": []}
    assert client.get(url, headers=alice).get_json()["active"] is True  # untouched
    assert client.get(url, headers=alice).status_code == 200


# --------------------------------------------------------- SSRF at the API edge
def test_private_webhook_targets_rejected_when_protection_is_on(make_services):
    svc = make_services(allow_private_webhooks=False)
    c = create_app(svc).test_client()
    auth = signup(c)
    for url in (
        "http://127.0.0.1/x",
        "http://localhost/x",
        "http://[::1]/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/x",
        "http://192.168.0.1/x",
    ):
        r = make_alert(c, auth, webhook_url=url)
        assert r.status_code == 400, url
        assert "non-public" in r.get_json()["error"], url


def test_public_webhook_target_accepted_when_protection_is_on(make_services):
    def public(host, port, proto=0):
        if host == "hooks.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", ("93.184.216.34", port))]
        return socket.getaddrinfo(host, port, proto=proto)  # IP literals resolve locally

    svc = make_services(allow_private_webhooks=False, services_kwargs={"resolver": public})
    c = create_app(svc).test_client()
    auth = signup(c)
    r = make_alert(c, auth, webhook_url="https://hooks.example.com/pw")
    assert r.status_code == 201
    assert client_patch(c, auth, r.get_json()["id"], "http://10.1.1.1/x").status_code == 400


def client_patch(c, auth, alert_id, url):
    return c.patch(f"/api/alerts/{alert_id}", json={"webhook_url": url}, headers=auth)


# ------------------------------------------------------ request-level protection
def test_oversized_body_rejected_with_json_error(client, auth):
    big = {"symbol": "AAPL", "rule_type": "above", "threshold": 1, "webhook_url": "x" * 70_000}
    r = client.post("/api/alerts", json=big, headers=auth)
    assert r.status_code == 413
    assert r.get_json() == {"error": "request body too large (max 65536 bytes)"}


def test_body_just_under_the_cap_is_parsed(client, auth):
    r = make_alert(client, auth, webhook_url="http://127.0.0.1/" + "a" * 3000)
    assert r.status_code == 400 and "2048" in r.get_json()["error"]  # reached validation


def test_errors_are_json(client, auth):
    for r in (
        client.get("/api/nope"),
        client.get("/nope"),
        client.put("/api/alerts", headers=auth),
        client.post("/api/alerts", data="x", headers=auth),
    ):
        assert r.is_json and set(r.get_json()) == {"error"}
    assert client.get("/api/nope").status_code == 404
    assert client.put("/api/alerts", headers=auth).status_code == 405


def test_security_headers(client, auth):
    r = client.get("/api/alerts", headers=auth)
    csp = r.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "script-src 'self'" in csp and "style-src 'self'" in csp and "default-src 'none'" in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Cache-Control"] == "no-store"
    assert client.get("/api/health").headers["Cache-Control"] == "no-store"
    assert client.post("/api/auth/login", json={}).headers["Cache-Control"] == "no-store"


# ----------------------------------- API changes reach the running engine (cache)
def test_created_alert_fires_and_pause_delete_take_effect(svc, client, auth):
    svc.start(feed=False)
    a = make_alert(client, auth, threshold=100, repeating=True).get_json()
    svc.ingest({"AAPL": 99.0}, T0)
    svc.ingest({"AAPL": 101.0}, T0 + 1000)
    _drain(svc)
    events = client.get("/api/events", headers=auth).get_json()["events"]
    assert len(events) == 1 and events[0]["alert_id"] == a["id"] and events[0]["price"] == 101.0


def _drain(svc):
    svc.bus.stop()
    svc.webhooks.stop()


def test_pausing_through_the_api_stops_firing(svc, client, auth):
    svc.start(feed=False)
    a = make_alert(client, auth, threshold=100, repeating=True).get_json()
    svc.ingest({"AAPL": 101.0}, T0)
    _wait_for(lambda: svc.bus.stats()["processed"] == 1)
    client.patch(f"/api/alerts/{a['id']}", json={"active": False}, headers=auth)
    svc.ingest({"AAPL": 102.0}, T0 + 1000)
    _wait_for(lambda: svc.bus.stats()["processed"] == 2)
    client.patch(f"/api/alerts/{a['id']}", json={"active": True}, headers=auth)  # re-arm
    svc.ingest({"AAPL": 103.0}, T0 + 2000)
    _drain(svc)
    prices = [e["price"] for e in client.get("/api/events", headers=auth).get_json()["events"]]
    assert prices == [103.0, 101.0]  # nothing fired while paused


def test_deleting_through_the_api_stops_firing(svc, client, auth):
    svc.start(feed=False)
    a = make_alert(client, auth, threshold=100, repeating=True).get_json()
    svc.ingest({"AAPL": 101.0}, T0)
    _wait_for(lambda: svc.bus.stats()["processed"] == 1)
    client.delete(f"/api/alerts/{a['id']}", headers=auth)
    svc.ingest({"AAPL": 102.0}, T0 + 1000)
    _drain(svc)
    assert client.get("/api/events", headers=auth).get_json()["events"] == []  # cascade-deleted


def _wait_for(cond, timeout=3.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.005)
    raise AssertionError("condition not met in time")
