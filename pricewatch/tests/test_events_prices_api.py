import time

import pytest
from conftest import T0, add_alert, signup

from pricewatch.app import create_app


def user_id(client, headers):
    # tests create users through the API; look the id up via the DB-backed token owner
    return (
        client.application.svc.db.conn()
        .execute("SELECT id FROM users ORDER BY id DESC LIMIT 1")
        .fetchone()[0]
    )


def seed_events(svc, uid, n, status="none", symbol="AAPL"):
    aid = add_alert(svc.db, uid, symbol=symbol, repeating=1)
    ids = []
    for i in range(n):
        cur = svc.db.conn().execute(
            "INSERT INTO alert_events (alert_id, user_id, symbol, price, message, tick_ts_ms, "
            "fired_at, delivery_status) VALUES (?, ?, ?, ?, 'm', ?, 'f', ?)",
            (aid, uid, symbol, 100.0 + i, T0 + i, status),
        )
        ids.append(cur.lastrowid)
    return aid, ids


# ---------------------------------------------------------------------- events
def test_events_newest_first_with_cursor_pagination(svc, client):
    auth = signup(client)
    _, ids = seed_events(svc, user_id(client, auth), 7)
    page1 = client.get("/api/events?limit=3", headers=auth).get_json()
    assert [e["id"] for e in page1["events"]] == ids[::-1][:3]
    assert page1["next_before_id"] == ids[::-1][2]
    page2 = client.get(f"/api/events?limit=3&before_id={page1['next_before_id']}", headers=auth)
    page2 = page2.get_json()
    assert [e["id"] for e in page2["events"]] == ids[::-1][3:6]
    page3 = client.get(f"/api/events?limit=3&before_id={page2['next_before_id']}", headers=auth)
    page3 = page3.get_json()
    assert [e["id"] for e in page3["events"]] == ids[::-1][6:]
    assert page3["next_before_id"] is None  # short page: no more data


def test_event_fields_hide_internal_user_id(svc, client):
    auth = signup(client)
    seed_events(svc, user_id(client, auth), 1)
    e = client.get("/api/events", headers=auth).get_json()["events"][0]
    assert set(e) == {
        "id", "alert_id", "symbol", "price", "message", "tick_ts_ms", "fired_at",
        "delivery_status", "delivery_attempts", "delivery_error",
    }  # fmt: skip


@pytest.mark.parametrize("qs", ["limit=0", "limit=201", "limit=abc", "before_id=x", "limit=-1"])
def test_event_pagination_validation(client, qs):
    auth = signup(client)
    assert client.get(f"/api/events?{qs}", headers=auth).status_code == 400


def test_events_are_scoped_to_the_user(svc, client):
    alice = signup(client, "alice@example.com")
    alice_id = user_id(client, alice)
    bob = signup(client, "bob@example.com")
    _, ids = seed_events(svc, alice_id, 2, status="failed")
    assert client.get("/api/events", headers=bob).get_json()["events"] == []
    assert len(client.get("/api/events", headers=alice).get_json()["events"]) == 2
    assert client.post(f"/api/events/{ids[0]}/retry", headers=bob).status_code == 404


def test_retry_unknown_event_and_requires_auth(client):
    auth = signup(client)
    assert client.post("/api/events/999/retry", headers=auth).status_code == 404
    assert client.post("/api/events/1/retry").status_code == 401


def test_only_failed_deliveries_can_be_retried(svc, client):
    auth = signup(client)
    for status in ("none", "pending", "delivered"):
        _, ids = seed_events(svc, user_id(client, auth), 1, status=status)
        r = client.post(f"/api/events/{ids[0]}/retry", headers=auth)
        assert r.status_code == 409


def test_retry_requeues_a_failed_delivery_and_it_succeeds(make_services):
    posted = []

    class Ok:
        status_code = 200

    def fake_post(url, **kwargs):
        posted.append(url)
        return Ok()

    svc = make_services(services_kwargs={"http_post": fake_post})
    client = create_app(svc).test_client()
    auth = signup(client)
    uid = user_id(client, auth)
    aid = add_alert(svc.db, uid, webhook_url="http://127.0.0.1:9/hook", repeating=1)
    cur = svc.db.conn().execute(
        "INSERT INTO alert_events (alert_id, user_id, symbol, price, message, tick_ts_ms, "
        "fired_at, delivery_status, delivery_attempts, delivery_error) "
        "VALUES (?, ?, 'AAPL', 1, 'm', ?, 'f', 'failed', 3, 'HTTP 500')",
        (aid, uid, T0),
    )
    svc.start(feed=False)
    r = client.post(f"/api/events/{cur.lastrowid}/retry", headers=auth)
    assert r.status_code == 202
    assert r.get_json()["delivery_status"] == "pending" and r.get_json()["delivery_error"] is None
    end = time.monotonic() + 3
    while time.monotonic() < end and not posted:
        time.sleep(0.01)
    svc.webhooks.stop()
    e = client.get("/api/events", headers=auth).get_json()["events"][0]
    assert posted == ["http://127.0.0.1:9/hook"]
    assert e["delivery_status"] == "delivered" and e["delivery_attempts"] == 1


# ---------------------------------------------------------------------- prices
def feed(svc, n=90, symbol="AAPL"):
    for i in range(n):
        svc.store.update(symbol, 100.0 + i, T0 + i * 1000)


def test_prices_empty_before_any_tick(client):
    body = client.get("/api/prices").get_json()
    assert body == {"provider": "scripted", "prices": []}


def test_prices_with_change_and_sparkline(svc, client):
    feed(svc)
    body = client.get("/api/prices?spark=60").get_json()
    (p,) = body["prices"]
    assert p["symbol"] == "AAPL" and p["price"] == 189.0 and p["ts_ms"] == T0 + 89_000
    assert p["change_pct_1m"] == pytest.approx((189 - 129) / 129 * 100)
    assert 2 <= len(p["spark"]) <= 30 and p["spark"][-1] == 189.0
    assert "spark" not in client.get("/api/prices").get_json()["prices"][0]


def test_price_change_is_null_until_a_minute_of_history(svc, client):
    feed(svc, n=10)
    assert client.get("/api/prices").get_json()["prices"][0]["change_pct_1m"] is None


@pytest.mark.parametrize("spark", ["abc", "0", "601", "-5"])
def test_prices_spark_validation(client, spark):
    assert client.get(f"/api/prices?spark={spark}").status_code == 400


def test_price_history(svc, client):
    feed(svc, n=100)
    body = client.get("/api/prices/aapl/history?seconds=30").get_json()
    assert body["symbol"] == "AAPL"
    assert body["points"][0] == [T0 + 69_000, 169.0] and body["points"][-1] == [T0 + 99_000, 199.0]
    assert client.get("/api/prices/NOPE/history").status_code == 404
    assert client.get("/api/prices/AAPL/history?seconds=0").status_code == 400
    assert client.get("/api/prices/AAPL/history?seconds=9999").status_code == 400
    assert client.get("/api/prices/AAPL/history?seconds=x").status_code == 400
    assert client.get("/api/prices/MSFT/history").get_json()["points"] == []


def test_price_history_is_downsampled(svc, client):
    for i in range(660):
        svc.store.update("AAPL", float(i), T0 + i * 1000)
    points = client.get("/api/prices/AAPL/history?seconds=660").get_json()["points"]
    assert len(points) == 600 and points[-1][1] == 659.0


def test_prices_do_not_require_auth(client):
    assert client.get("/api/prices").status_code == 200


# ---------------------------------------------------------------------- health
def test_health_reports_feed_and_bus(svc, client):
    h = client.get("/api/health").get_json()
    assert h["status"] == "ok" and h["feed"]["provider"] == "scripted"
    assert h["feed"]["status"] == "starting"
    assert set(h["bus"]) >= {"processed", "dropped", "errors", "queue_depths", "latency_ms"}
    assert set(h["bus"]["latency_ms"]) >= {"p50", "p95", "p99"}
    assert "delivered" in h["webhooks"] and h["uptime_seconds"] >= 0


def test_health_degrades_when_the_feed_is_failing(svc, client):
    class Down:
        name = "down"

        def fetch(self):
            raise RuntimeError("provider down")

    svc.feed.provider = Down()
    svc.feed.poll_once()
    h = client.get("/api/health").get_json()
    assert h["status"] == "degraded" and h["feed"]["consecutive_failures"] == 1
    assert "provider down" in h["feed"]["last_error"]


def test_dashboard_static_files_served(client):
    r = client.get("/")
    assert r.status_code == 200 and b"PriceWatch" in r.data
    assert "unsafe-inline" not in r.headers["Content-Security-Policy"]
    r.close()
    for path, ctype in (("/static/app.js", "javascript"), ("/static/style.css", "css")):
        r = client.get(path)
        assert r.status_code == 200 and ctype in r.content_type
        r.close()
