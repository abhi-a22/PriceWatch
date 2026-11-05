import threading
import time

from pricewatch.bus import PartitionedBus, partition_for, percentile

SYMBOLS = ["AAPL", "MSFT", "NVDA", "TSLA", "BTC", "ETH", "SOL", "DOGE"]


def test_partition_is_stable_and_in_range():
    # crc32 is deterministic across processes; these pinned values would change
    # if someone swapped in Python's randomized hash().
    assert partition_for("AAPL", 4) == partition_for("AAPL", 4)
    assert [partition_for(s, 4) for s in ("AAPL", "BTC", "ETH")] == [0, 2, 3]
    assert all(0 <= partition_for(s, 4) < 4 for s in SYMBOLS)


def test_percentile_nearest_rank():
    data = list(map(float, range(1, 101)))
    assert percentile(data, 50) == 50.0
    assert percentile(data, 95) == 95.0
    assert percentile(data, 99) == 99.0
    assert percentile([], 95) == 0.0
    assert percentile([7.0], 99) == 7.0


def test_per_symbol_ordering_and_single_thread_per_symbol_under_load():
    seen = {s: [] for s in SYMBOLS}
    threads = {s: set() for s in SYMBOLS}
    lock = threading.Lock()

    def handler(symbol, payload):
        with lock:
            seen[symbol].append(payload)
            threads[symbol].add(threading.current_thread().name)

    bus = PartitionedBus(handler, partitions=4, queue_size=100_000)
    bus.start()
    n = 2000
    for i in range(n):
        for s in SYMBOLS:
            assert bus.submit(s, i)
    bus.stop()
    for s in SYMBOLS:
        assert seen[s] == list(range(n)), f"{s} out of order"
        assert len(threads[s]) == 1, f"{s} handled by {threads[s]}"
    assert bus.stats()["processed"] == n * len(SYMBOLS)


def test_queue_overflow_drops_and_never_blocks():
    gate = threading.Event()
    handled = []

    def slow(symbol, payload):
        gate.wait(5)
        handled.append(payload)

    bus = PartitionedBus(slow, partitions=1, queue_size=3)
    bus.start()
    t0 = time.monotonic()
    results = [bus.submit("AAPL", i) for i in range(20)]
    assert time.monotonic() - t0 < 1.0  # producer was never blocked
    # one item may already be in the handler, so 3 or 4 are accepted
    assert results.count(True) in (3, 4)
    stats = bus.stats()
    assert stats["dropped"] == results.count(False) >= 16
    gate.set()
    bus.stop()
    assert len(handled) == results.count(True)
    assert bus.stats()["dropped"] == stats["dropped"]


def test_crashing_handler_is_counted_and_worker_survives():
    handled = []

    def handler(symbol, payload):
        if payload % 2 == 0:
            raise RuntimeError("boom")
        handled.append(payload)

    bus = PartitionedBus(handler, partitions=1)
    bus.start()
    for i in range(10):
        bus.submit("AAPL", i)
    bus.stop()
    assert handled == [1, 3, 5, 7, 9]
    stats = bus.stats()
    assert stats["errors"] == 5 and stats["processed"] == 10


def test_stop_drains_queued_work_before_exiting():
    handled = []

    def handler(symbol, payload):
        time.sleep(0.002)
        handled.append(payload)

    bus = PartitionedBus(handler, partitions=2, queue_size=1000)
    bus.start()
    for i in range(100):
        bus.submit("AAPL" if i % 2 else "BTC", i)
    bus.stop()
    assert sorted(handled) == list(range(100))
    assert all(depth == 0 for depth in bus.stats()["queue_depths"])


def test_submit_after_stop_is_rejected():
    bus = PartitionedBus(lambda s, p: None, partitions=1)
    bus.start()
    bus.stop()
    assert bus.submit("AAPL", 1) is False


def test_stats_report_latency_percentiles():
    bus = PartitionedBus(lambda s, p: time.sleep(0.001), partitions=2)
    bus.start()
    for i in range(50):
        bus.submit("AAPL" if i % 2 else "BTC", i)
    bus.stop()
    lat = bus.stats()["latency_ms"]
    assert lat["samples"] == 50
    assert 0 < lat["p50"] <= lat["p95"] <= lat["p99"]
