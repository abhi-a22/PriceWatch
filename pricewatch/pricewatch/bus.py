"""Partitioned event bus.

N worker threads, each with its own bounded queue. A symbol always hashes to
the same partition, so one thread handles all events for that symbol:
per-symbol ordering is guaranteed and per-symbol state (rule cooldowns) needs no locks.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import zlib
from collections import deque
from collections.abc import Callable

log = logging.getLogger("pricewatch.bus")

_STOP = object()


def partition_for(symbol: str, partitions: int) -> int:
    # crc32, not hash(): Python's str hash is randomized per process.
    return zlib.crc32(symbol.encode()) % partitions


def percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already sorted list."""
    if not sorted_values:
        return 0.0
    rank = max(1, -(-len(sorted_values) * pct // 100))  # ceil
    return sorted_values[int(rank) - 1]


class PartitionedBus:
    def __init__(
        self,
        handler: Callable[[str, object], None],
        partitions: int = 4,
        queue_size: int = 5000,
        latency_samples: int = 50_000,
    ):
        self._handler = handler
        self.partitions = partitions
        self._queues = [queue.Queue(maxsize=queue_size) for _ in range(partitions)]
        self._threads: list[threading.Thread] = []
        # Each counter has a single writer (its worker), so no lock is needed.
        self._processed = [0] * partitions
        self._errors = [0] * partitions
        self._latencies = [deque(maxlen=latency_samples) for _ in range(partitions)]
        self._dropped = 0
        self._drop_lock = threading.Lock()
        self._accepting = False

    def start(self) -> None:
        self._accepting = True
        for i in range(self.partitions):
            t = threading.Thread(target=self._worker, args=(i,), name=f"bus-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def submit(self, symbol: str, payload: object) -> bool:
        """Enqueue without ever blocking. Returns False (and counts a drop) if the
        partition's queue is full or the bus is stopping."""
        if not self._accepting:
            return False
        q = self._queues[partition_for(symbol, self.partitions)]
        try:
            q.put_nowait((time.perf_counter(), symbol, payload))
            return True
        except queue.Full:
            with self._drop_lock:
                self._dropped += 1
            return False

    def _worker(self, idx: int) -> None:
        q = self._queues[idx]
        while True:
            item = q.get()
            if item is _STOP:
                return
            enqueued, symbol, payload = item
            try:
                self._handler(symbol, payload)
            except Exception:  # noqa: BLE001 - a bad handler must never kill the worker
                self._errors[idx] += 1
                log.exception("handler failed for %s", symbol)
            self._processed[idx] += 1
            self._latencies[idx].append((time.perf_counter() - enqueued) * 1000.0)

    def stop(self, timeout: float = 10.0) -> None:
        """Stop accepting work, let workers finish everything already queued, then exit."""
        self._accepting = False
        for q in self._queues:
            q.put(_STOP)  # FIFO: the sentinel sits behind all queued work
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self._threads.clear()

    def stats(self) -> dict:
        samples = sorted(x for d in self._latencies for x in list(d))
        return {
            "partitions": self.partitions,
            "processed": sum(self._processed),
            "dropped": self._dropped,
            "errors": sum(self._errors),
            "queue_depths": [q.qsize() for q in self._queues],
            "latency_ms": {
                "p50": round(percentile(samples, 50), 3),
                "p95": round(percentile(samples, 95), 3),
                "p99": round(percentile(samples, 99), 3),
                "samples": len(samples),
            },
        }
