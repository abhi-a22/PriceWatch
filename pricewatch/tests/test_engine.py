import pytest
from conftest import SYMBOLS, T0, add_alert, add_user, events

from pricewatch.engine import RuleEngine
from pricewatch.store import PriceStore


@pytest.fixture
def setup(db):
    store = PriceStore(SYMBOLS)
    fired = []
    engine = RuleEngine(db, store, on_fired=fired.append)
    uid = add_user(db)
    return db, store, engine, uid, fired


def tick(store, engine, symbol, price, ts):
    """What the feed pipeline does: store the tick, then evaluate it."""
    assert store.update(symbol, price, ts)
    engine.on_tick(symbol, (price, ts))


def alert_row(db, alert_id):
    return db.conn().execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()


# ------------------------------------------------------------ price thresholds
def test_above_is_inclusive(setup):
    db, store, engine, uid, _ = setup
    a = add_alert(db, uid, rule_type="above", threshold=100.0, repeating=1)
    tick(store, engine, "AAPL", 99.99, T0)
    assert events(db, a) == []
    tick(store, engine, "AAPL", 100.0, T0 + 1000)  # exactly at the threshold
    assert len(events(db, a)) == 1


def test_below_is_inclusive(setup):
    db, store, engine, uid, _ = setup
    a = add_alert(db, uid, rule_type="below", threshold=50.0, repeating=1)
    tick(store, engine, "AAPL", 50.01, T0)
    assert events(db, a) == []
    tick(store, engine, "AAPL", 50.0, T0 + 1000)
    assert len(events(db, a)) == 1


def test_event_contents(setup):
    db, store, engine, uid, _ = setup
    a = add_alert(db, uid, threshold=100.0)
    tick(store, engine, "AAPL", 101.5, T0)
    (e,) = events(db, a)
    assert (e["user_id"], e["symbol"], e["price"], e["tick_ts_ms"]) == (uid, "AAPL", 101.5, T0)
    assert e["fired_at"] == "2023-11-14T22:13:20.000+00:00"
    assert "101.5" in e["message"] and e["delivery_status"] == "none"


def test_other_symbols_do_not_trigger(setup):
    db, store, engine, uid, _ = setup
    a = add_alert(db, uid, symbol="AAPL", threshold=1.0)
    tick(store, engine, "MSFT", 500.0, T0)
    assert events(db, a) == []


# ------------------------------------------------- one-time vs repeating, cooldown
def test_one_time_alert_fires_once_and_deactivates(setup):
    db, store, engine, uid, _ = setup
    a = add_alert(db, uid, threshold=100.0, repeating=0)
    tick(store, engine, "AAPL", 101.0, T0)
    tick(store, engine, "AAPL", 102.0, T0 + 1000)
    assert len(events(db, a)) == 1
    row = alert_row(db, a)
    assert row["active"] == 0 and row["last_triggered_at"] == T0


def test_repeating_without_cooldown_fires_every_tick(setup):
    db, store, engine, uid, _ = setup
    a = add_alert(db, uid, threshold=100.0, repeating=1, cooldown_seconds=0)
    for i in range(3):
        tick(store, engine, "AAPL", 101.0, T0 + i * 1000)
    assert len(events(db, a)) == 3
    assert alert_row(db, a)["active"] == 1


def test_cooldown_is_measured_in_tick_time(setup):
    db, store, engine, uid, _ = setup
    a = add_alert(db, uid, threshold=100.0, repeating=1, cooldown_seconds=60)
    tick(store, engine, "AAPL", 101.0, T0)
    tick(store, engine, "AAPL", 101.0, T0 + 59_999)  # 1 ms short of the cooldown
    assert len(events(db, a)) == 1
    tick(store, engine, "AAPL", 101.0, T0 + 60_000)  # exactly cooled down
    assert len(events(db, a)) == 2
    tick(store, engine, "AAPL", 101.0, T0 + 60_001)
    assert len(events(db, a)) == 2


# ------------------------------------------------------------- percent rules
def test_pct_rise_silent_until_history_covers_window(setup):
    db, store, engine, uid, _ = setup
    a = add_alert(db, uid, rule_type="pct_rise", threshold=5.0, window_seconds=60, repeating=1)
    tick(store, engine, "AAPL", 100.0, T0)
    tick(store, engine, "AAPL", 150.0, T0 + 30_000)  # +50% but only 30 s of history
    assert events(db, a) == []
    tick(store, engine, "AAPL", 106.0, T0 + 60_000)  # window now covered; vs 100 -> +6%
    assert len(events(db, a)) == 1
    assert "rose 6.00%" in events(db, a)[0]["message"]


def test_pct_rise_boundary_is_inclusive(setup):
    db, store, engine, uid, _ = setup
    a = add_alert(db, uid, rule_type="pct_rise", threshold=5.0, window_seconds=10)
    tick(store, engine, "AAPL", 100.0, T0)
    tick(store, engine, "AAPL", 104.99, T0 + 10_000)
    assert events(db, a) == []
    # The reference for T0+11_000 is the price at or before T0+1_000, i.e. 100.0 -> exactly +5%.
    tick(store, engine, "AAPL", 105.0, T0 + 11_000)
    assert len(events(db, a)) == 1


def test_pct_drop(setup):
    db, store, engine, uid, _ = setup
    a = add_alert(db, uid, rule_type="pct_drop", threshold=2.0, window_seconds=10)
    tick(store, engine, "AAPL", 100.0, T0)
    tick(store, engine, "AAPL", 99.0, T0 + 5000)  # not enough history yet
    tick(store, engine, "AAPL", 98.5, T0 + 10_000)  # -1.5% vs 100
    assert events(db, a) == []
    tick(store, engine, "AAPL", 97.9, T0 + 11_000)  # ref still 100.0 -> -2.1%
    assert len(events(db, a)) == 1
    assert "fell" in events(db, a)[0]["message"]


def test_pct_rule_does_not_fire_on_opposite_move(setup):
    db, store, engine, uid, _ = setup
    rise = add_alert(db, uid, rule_type="pct_rise", threshold=1.0, window_seconds=10)
    drop = add_alert(db, uid, rule_type="pct_drop", threshold=1.0, window_seconds=10)
    tick(store, engine, "AAPL", 100.0, T0)
    tick(store, engine, "AAPL", 90.0, T0 + 10_000)
    assert events(db, rise) == [] and len(events(db, drop)) == 1


# ---------------------------------------------------- pausing / cache invalidation
def test_pause_and_rearm(setup):
    db, store, engine, uid, _ = setup
    a = add_alert(db, uid, threshold=100.0, repeating=1)
    tick(store, engine, "AAPL", 101.0, T0)
    assert len(events(db, a)) == 1

    db.conn().execute("UPDATE alerts SET active = 0 WHERE id = ?", (a,))
    engine.invalidate()
    tick(store, engine, "AAPL", 101.0, T0 + 1000)
    assert len(events(db, a)) == 1  # paused: nothing new

    db.conn().execute("UPDATE alerts SET active = 1 WHERE id = ?", (a,))
    engine.invalidate()
    tick(store, engine, "AAPL", 101.0, T0 + 2000)
    assert len(events(db, a)) == 2  # re-armed


def test_rearming_a_fired_one_time_alert(setup):
    db, store, engine, uid, _ = setup
    a = add_alert(db, uid, threshold=100.0, repeating=0)
    tick(store, engine, "AAPL", 101.0, T0)
    assert alert_row(db, a)["active"] == 0
    db.conn().execute("UPDATE alerts SET active = 1 WHERE id = ?", (a,))
    engine.invalidate()
    tick(store, engine, "AAPL", 101.0, T0 + 1000)
    assert len(events(db, a)) == 2


def test_rules_are_cached_until_invalidated(setup):
    db, store, engine, uid, _ = setup
    tick(store, engine, "AAPL", 90.0, T0)  # loads the (empty) cache
    loads = engine.cache_loads
    late = add_alert(db, uid, threshold=100.0)  # created behind the engine's back
    tick(store, engine, "AAPL", 101.0, T0 + 1000)
    assert events(db, late) == [] and engine.cache_loads == loads  # still cached

    engine.invalidate()
    tick(store, engine, "AAPL", 101.0, T0 + 2000)
    assert len(events(db, late)) == 1 and engine.cache_loads == loads + 1


# ----------------------------------------------------------------- idempotency
def test_replaying_the_same_tick_creates_no_duplicate(setup):
    db, store, engine, uid, fired = setup
    a = add_alert(db, uid, threshold=100.0, repeating=1, cooldown_seconds=0, webhook_url="http://x")
    tick(store, engine, "AAPL", 101.0, T0)
    engine.on_tick("AAPL", (101.0, T0))  # replay, same engine
    assert len(events(db, a)) == 1
    assert len(fired) == 1  # and no second webhook dispatch

    fresh = RuleEngine(db, store, on_fired=fired.append)  # replay after a "restart"
    fresh.on_tick("AAPL", (101.0, T0))
    assert len(events(db, a)) == 1 and len(fired) == 1


def test_replay_does_not_corrupt_alert_state(setup):
    db, store, engine, uid, _ = setup
    a = add_alert(db, uid, threshold=100.0, repeating=1, cooldown_seconds=30)
    tick(store, engine, "AAPL", 101.0, T0)
    engine.on_tick("AAPL", (101.0, T0))
    assert alert_row(db, a)["last_triggered_at"] == T0


# --------------------------------------------------------------- deleted alerts
def test_alert_deleted_mid_evaluation_is_skipped_not_fatal(setup):
    db, store, engine, uid, fired = setup
    a = add_alert(db, uid, threshold=100.0, repeating=1)
    keep = add_alert(db, uid, threshold=100.0, repeating=1)
    tick(store, engine, "AAPL", 90.0, T0)  # cache now holds both rules
    db.conn().execute("DELETE FROM alerts WHERE id = ?", (a,))  # engine cache is stale
    tick(store, engine, "AAPL", 101.0, T0 + 1000)  # FK violation for `a` must not raise
    assert events(db, a) == []
    assert len(events(db, keep)) == 1  # the other rule still fired
    assert not db.conn().in_transaction  # no transaction left open
    tick(store, engine, "AAPL", 102.0, T0 + 2000)  # engine keeps working afterwards
    assert len(events(db, keep)) == 2


def test_on_fired_only_called_for_alerts_with_a_webhook(setup):
    db, store, engine, uid, fired = setup
    plain = add_alert(db, uid, threshold=100.0)
    hooked = add_alert(db, uid, threshold=100.0, webhook_url="http://example.com/h")
    tick(store, engine, "AAPL", 101.0, T0)
    assert fired == [events(db, hooked)[0]["id"]]
    assert events(db, hooked)[0]["delivery_status"] == "pending"
    assert events(db, plain)[0]["delivery_status"] == "none"
