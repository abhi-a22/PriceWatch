import pytest

from pricewatch.store import PriceStore

T0 = 1_700_000_000_000  # fixed epoch ms so every test is deterministic


def make(history_seconds=660):
    return PriceStore(["AAPL", "BTC"], history_seconds=history_seconds)


def test_latest_and_unknown_symbol():
    s = make()
    assert s.latest("AAPL") is None
    assert s.update("AAPL", 100.0, T0)
    assert s.latest("AAPL") == (100.0, T0)
    assert not s.update("NOPE", 1.0, T0)
    assert s.latest("NOPE") is None


def test_rejects_stale_and_duplicate_timestamps():
    s = make()
    assert s.update("AAPL", 100.0, T0 + 1000)
    assert not s.update("AAPL", 101.0, T0 + 1000)  # duplicate
    assert not s.update("AAPL", 102.0, T0 + 500)  # stale
    assert s.update("AAPL", 103.0, T0 + 1001)
    assert s.history("AAPL") == [(T0 + 1000, 100.0), (T0 + 1001, 103.0)]


def test_symbols_are_independent():
    s = make()
    assert s.update("AAPL", 1.0, T0 + 5000)
    assert s.update("BTC", 2.0, T0)  # older ts is fine for a different symbol


def test_reference_price_uses_bisect_semantics():
    s = make()
    for i in range(10):
        s.update("AAPL", 100.0 + i, T0 + i * 1000)
    assert s.reference_price("AAPL", T0 + 3000) == 103.0  # exact tick
    assert s.reference_price("AAPL", T0 + 3999) == 103.0  # just before the next tick
    assert s.reference_price("AAPL", T0 + 99_000) == 109.0  # after the newest tick


def test_reference_price_none_until_history_covers_window():
    s = make()
    s.update("AAPL", 100.0, T0 + 5000)
    assert s.reference_price("AAPL", T0 + 4999) is None
    assert s.reference_price("AAPL", T0 + 5000) == 100.0
    assert s.reference_price("BTC", T0) is None  # no data at all


def test_history_pruned_but_keeps_a_point_covering_the_window():
    s = make(history_seconds=10)
    for i in range(0, 61):  # one tick per second for a minute
        s.update("AAPL", float(i), T0 + i * 1000)
    hist = s.history("AAPL")
    assert hist[-1] == (T0 + 60_000, 60.0)
    assert hist[0][0] <= T0 + 60_000 - 10_000  # still covers a 10 s window
    assert len(hist) <= 12  # but bounded
    assert s.reference_price("AAPL", T0 + 50_000) == 50.0


def test_history_since_filter():
    s = make()
    for i in range(5):
        s.update("AAPL", float(i), T0 + i * 1000)
    assert [p for _, p in s.history("AAPL", since_ts_ms=T0 + 2000)] == [2.0, 3.0, 4.0]
    assert s.history("NOPE") == []


def test_sparkline_downsamples_to_requested_points():
    s = make()
    for i in range(300):
        s.update("AAPL", float(i), T0 + i * 1000)
    spark = s.sparkline("AAPL", seconds=60, points=30)
    assert len(spark) == 30
    assert spark[-1] == 299.0 and spark[0] == 239.0
    assert s.sparkline("NOPE") == []


def test_sparkline_short_history_returned_as_is():
    s = make()
    for i in range(3):
        s.update("AAPL", float(i), T0 + i * 1000)
    assert s.sparkline("AAPL") == [0.0, 1.0, 2.0]


def test_change_pct_needs_a_full_window():
    s = make()
    s.update("AAPL", 100.0, T0)
    s.update("AAPL", 110.0, T0 + 30_000)
    assert s.change_pct("AAPL", 60) is None
    s.update("AAPL", 120.0, T0 + 60_000)
    assert s.change_pct("AAPL", 60) == pytest.approx(20.0)
