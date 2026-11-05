"""PriceWatch benchmarks.

    python scripts/loadtest.py engine   # rule engine throughput and latency
    python scripts/loadtest.py api      # HTTP API throughput and latency
    python scripts/loadtest.py all      # both (default)

Engine benchmark: 500 active alerts spread across the 6 symbols; ticks go through the
real store -> partitioned bus -> rule engine path. Latency is measured by the bus from
enqueue to handler completion (i.e. until rule evaluation is complete).

  * saturation: push ticks as fast as the bus accepts them (a full queue makes the
    producer wait; nothing is dropped). Gives maximum sustained ticks/s. Latency here
    includes time spent waiting in a full queue, so it is not a "per-tick cost".
  * paced: push at a fixed target rate for a fixed time. This is the realistic
    latency measurement: "at N ticks/s, p95 is X ms".

API benchmark: starts `python -m pricewatch` as a separate process and drives it from
several client processes, so load generation does not share a GIL with the server.

Every figure printed comes from this run. Nothing is estimated.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import random
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from pricewatch.bus import PartitionedBus, percentile  # noqa: E402
from pricewatch.config import Config  # noqa: E402
from pricewatch.db import Database  # noqa: E402
from pricewatch.engine import RuleEngine  # noqa: E402
from pricewatch.feed import START_PRICES  # noqa: E402
from pricewatch.store import PriceStore  # noqa: E402

SYMBOLS = ("AAPL", "MSFT", "NVDA", "TSLA", "BTC", "ETH")
WINDOWS = (10, 30, 60, 120, 300, 600)


# ------------------------------------------------------------------- machine info
def describe_machine() -> dict:
    cpu = platform.processor() or platform.machine()
    mem_gb = None
    try:
        if sys.platform == "darwin":
            cpu = subprocess.check_output(  # noqa: S603
                ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], text=True
            ).strip()
            mem = int(subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.memsize"], text=True))  # noqa: S603
        else:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
            mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        mem_gb = round(mem / 2**30)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
        "memory_gb": mem_gb,
        "os": platform.platform(),
        "python": platform.python_version(),
    }


# --------------------------------------------------------------- engine benchmark
class TickSource:
    """Endless round-robin random-walk ticks; simulated time advances 1 s per round."""

    def __init__(self, start_ts_ms: int, seed: int = 7):
        self.rng = random.Random(seed)  # noqa: S311
        self.prices = {s: START_PRICES[s] for s in SYMBOLS}
        self.round = 0
        self.start = start_ts_ms
        self._gen = self._iter()

    def _iter(self):
        while True:
            ts = self.start + self.round * 1000
            for s in SYMBOLS:
                self.prices[s] = max(0.01, self.prices[s] * (1 + self.rng.gauss(0, 0.0006)))
                yield s, self.prices[s], ts
            self.round += 1

    def __next__(self):
        return next(self._gen)


def build_engine_env(n_rules: int, firing_fraction: float, tmpdir: str, partitions: int):
    cfg = Config(db_path=f"{tmpdir}/bench.db", partitions=partitions)
    db = Database(cfg.db_path)
    db.init_schema()
    conn = db.conn()
    conn.execute("INSERT INTO users (email, password_hash, created_at) VALUES ('b@b.co','x','now')")
    n_firing = int(n_rules * firing_fraction)
    rows = []
    for i in range(n_rules):
        symbol = SYMBOLS[i % len(SYMBOLS)]
        if i < n_firing:  # always true; repeating with a 60 s tick-time cooldown
            rows.append((1, symbol, "above", 0.01, None, 1, 60))
            continue
        kind = i % 4
        window = WINDOWS[(i // 4) % len(WINDOWS)]
        if kind == 0:
            rows.append((1, symbol, "above", 1e9, None, 0, 0))
        elif kind == 1:
            rows.append((1, symbol, "below", 0.0001, None, 0, 0))
        elif kind == 2:
            rows.append((1, symbol, "pct_rise", 1000.0, window, 0, 0))
        else:
            rows.append((1, symbol, "pct_drop", 100.0, window, 0, 0))
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO alerts (user_id, symbol, rule_type, threshold, window_seconds, repeating, "
        "cooldown_seconds, active, created_at) VALUES (?,?,?,?,?,?,?,1,'now')",
        rows,
    )
    conn.execute("COMMIT")
    store = PriceStore(SYMBOLS, history_seconds=cfg.history_seconds)
    # Pre-fill 11 minutes of history so percent rules do real bisect lookups, not early exits.
    base = 1_700_000_000_000
    rng = random.Random(1)  # noqa: S311 - benchmark data, not security
    price = dict(START_PRICES)
    for i in range(661):
        for s in SYMBOLS:
            price[s] *= 1 + rng.gauss(0, 0.0006)
            store.update(s, price[s], base + i * 1000)
    engine = RuleEngine(db, store)
    source = TickSource(base + 662 * 1000)
    source.prices = dict(price)
    return cfg, db, store, engine, source


def run_engine_case(engine, store, source, partitions, queue_size, *, rate=None, seconds=0.0,
                    ticks=0) -> dict:  # fmt: skip
    """rate=None -> saturation for `ticks` ticks; otherwise paced at `rate`/s for `seconds`."""
    bus = PartitionedBus(
        engine.on_tick, partitions=partitions, queue_size=queue_size, latency_samples=3_000_000
    )
    bus.start()
    submitted = backpressure = lost = 0
    t0 = time.perf_counter()
    if rate is None:
        while submitted < ticks:
            symbol, price, ts = next(source)
            store.update(symbol, price, ts)
            while not bus.submit(symbol, (price, ts)):
                backpressure += 1
                time.sleep(0.0002)  # queue full: wait, never drop
            submitted += 1
    else:
        # Pace in 10 ms batches and *sleep* between them. Busy-waiting here would hog the
        # GIL and starve the worker threads, which would measure the harness, not PriceWatch
        # (the real feed thread also sleeps between polls).
        batch_interval = 0.010
        per_batch = max(1, round(rate * batch_interval))
        next_t = t0
        deadline = t0 + seconds
        while time.perf_counter() < deadline:
            for _ in range(per_batch):
                symbol, price, ts = next(source)
                store.update(symbol, price, ts)
                if not bus.submit(symbol, (price, ts)):
                    lost += 1
                submitted += 1
            next_t += batch_interval * per_batch / (rate * batch_interval)
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
    t_submit_done = time.perf_counter()
    bus.stop(timeout=120)  # drains everything queued
    t_done = time.perf_counter()
    st = bus.stats()
    return {
        "mode": "saturation" if rate is None else "paced",
        "target_rate": rate,
        "ticks_submitted": submitted,
        "ticks_processed": st["processed"],
        # Paced runs never retry, so a full queue loses the tick. Saturation runs retry every
        # rejected submit (the rejections are counted as queue_full_rejections), losing none.
        "ticks_lost": lost,
        "queue_full_rejections": st["dropped"],
        "errors": st["errors"],
        "backpressure_waits": backpressure,
        "offered_rate_per_s": round(submitted / (t_submit_done - t0)),
        "sustained_rate_per_s": round(st["processed"] / (t_done - t0)),
        "seconds": round(t_done - t0, 2),
        "latency_ms": {k: st["latency_ms"][k] for k in ("p50", "p95", "p99")},
    }


def count_events(db) -> int:
    return db.conn().execute("SELECT COUNT(*) FROM alert_events").fetchone()[0]


def engine_benchmark(args) -> dict:
    results = {"rules": args.rules, "partitions": args.partitions, "scenarios": []}
    rates = [int(r) for r in args.rates.split(",")]
    scenarios = [
        ("evaluation only (no rule fires)", 0.0),
        ("10% of rules fire (cooldown 60 s)", 0.10),
    ]
    for name, firing in scenarios:
        print(f"\n== Engine: {args.rules} active rules, {name} ==", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            cfg, db, store, engine, source = build_engine_env(
                args.rules, firing, tmp, args.partitions
            )
            cases = []
            for rate in [None, *rates]:
                if rate is None:
                    r = run_engine_case(engine, store, source, args.partitions, cfg.queue_size,
                                        ticks=args.ticks)  # fmt: skip
                else:
                    r = run_engine_case(engine, store, source, args.partitions, cfg.queue_size,
                                        rate=rate, seconds=args.seconds)  # fmt: skip
                cases.append(r)
                label = "saturation" if rate is None else f"paced {rate}/s"
                lat = r["latency_ms"]
                print(
                    f"  {label:<16} sustained {r['sustained_rate_per_s']:>6}/s  "
                    f"p50 {lat['p50']:>8.2f}  p95 {lat['p95']:>8.2f}  p99 {lat['p99']:>8.2f} ms  "
                    f"lost {r['ticks_lost']}  errors {r['errors']}",
                    flush=True,
                )
            events = count_events(db)
            print(f"  alert events written during this scenario: {events}")
            assert all(c["errors"] == 0 for c in cases), "handler errors during benchmark"
            for c in cases:  # every accepted tick must have been processed after the drain
                assert c["ticks_processed"] == c["ticks_submitted"] - c["ticks_lost"], c
            if firing == 0.0:
                assert events == 0, "non-firing scenario unexpectedly fired alerts"
            else:
                assert events > 0, "firing scenario never fired"
            results["scenarios"].append(
                {"name": name, "firing_fraction": firing, "events_written": events, "cases": cases}
            )
            db.close_thread_connection()
    return results


# ------------------------------------------------------------------ API benchmark
def _client_worker(url: str, headers: dict, start_at: float, n_requests: int):
    session = requests.Session()
    lats, errors = [], 0
    while time.time() < start_at:
        time.sleep(0.001)
    for _ in range(n_requests):
        t = time.perf_counter()
        try:
            r = session.get(url, headers=headers, timeout=10)
            ok = r.status_code == 200
            r.content  # noqa: B018 - read the body so timing includes the full response
        except requests.RequestException:
            ok = False
        lats.append((time.perf_counter() - t) * 1000)
        errors += not ok
    return lats, errors, time.time()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def api_benchmark(args) -> dict:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            **os.environ,
            "PRICEWATCH_PORT": str(port),
            "PRICEWATCH_DB": f"{tmp}/api.db",
            "PRICEWATCH_SEED": "7",
            "PRICEWATCH_JWT_SECRET": "benchmark-secret-" + "x" * 40,
        }
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "pricewatch"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )  # fmt: skip
        try:
            for _ in range(200):
                try:
                    requests.get(base + "/api/health", timeout=1)
                    break
                except requests.RequestException:
                    time.sleep(0.1)
            creds = {"email": "bench@example.com", "password": "benchmark-pass-1"}
            token = requests.post(base + "/api/auth/register", json=creds, timeout=30).json()[
                "token"
            ]
            headers = {"Authorization": "Bearer " + token}
            for i in range(20):
                body = {"symbol": SYMBOLS[i % 6], "rule_type": "above", "threshold": 1e6 + i}
                assert requests.post(
                    base + "/api/alerts", json=body, headers=headers, timeout=10
                ).ok
            time.sleep(2)  # let the feed produce some history
            print(
                f"\n== API: {args.clients} client processes x {args.requests} requests each ==\n"
                "   (Werkzeug closes the connection after every response, so each request pays "
                "for a TCP connect;\n    a fixed request count and a cooldown keep the load "
                "generator inside the OS ephemeral-port budget)"
            )
            endpoints = [
                ("GET /api/prices?spark=60", "/api/prices?spark=60", {}),
                ("GET /api/alerts (authenticated, 20 alerts)", "/api/alerts", headers),
                ("GET /api/health", "/api/health", {}),
            ]
            results = {
                "clients": args.clients,
                "requests_per_client": args.requests,
                "endpoints": [],
            }
            for i, (name, path, hdrs) in enumerate(endpoints):
                if i:
                    time.sleep(args.cooldown)  # let TIME_WAIT sockets drain
                start_at = time.time() + 1.0
                with concurrent.futures.ProcessPoolExecutor(max_workers=args.clients) as pool:
                    futs = [
                        pool.submit(_client_worker, base + path, hdrs, start_at, args.requests)
                        for _ in range(args.clients)
                    ]
                    outs = [f.result() for f in futs]
                lats = sorted(x for o in outs for x in o[0])
                errors = sum(o[1] for o in outs)
                elapsed = max(o[2] for o in outs) - start_at
                assert errors == 0, f"{errors} failed requests: results would be invalid"
                row = {
                    "endpoint": name,
                    "requests": len(lats),
                    "errors": errors,
                    "requests_per_s": round(len(lats) / elapsed),
                    "latency_ms": {
                        "p50": round(percentile(lats, 50), 2),
                        "p95": round(percentile(lats, 95), 2),
                        "p99": round(percentile(lats, 99), 2),
                    },
                }
                results["endpoints"].append(row)
                lat = row["latency_ms"]
                print(
                    f"  {name:<44} {row['requests_per_s']:>6} req/s  p50 {lat['p50']:>7.2f}  "
                    f"p95 {lat['p95']:>7.2f}  p99 {lat['p99']:>7.2f} ms  errors {errors}"
                )
            return results
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("which", nargs="?", default="all", choices=["engine", "api", "all"])
    ap.add_argument("--rules", type=int, default=500)
    ap.add_argument("--partitions", type=int, default=4)
    ap.add_argument("--ticks", type=int, default=60_000, help="ticks for the saturation run")
    ap.add_argument("--rates", default="1000,2000,5000", help="paced target rates, ticks/s")
    ap.add_argument("--seconds", type=float, default=15.0, help="duration of each paced run")
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--requests", type=int, default=1000, help="API requests per client process")
    ap.add_argument("--cooldown", type=float, default=35.0, help="seconds between API endpoints")
    ap.add_argument("--json", help="write full results to this file")
    args = ap.parse_args()

    out: dict = {"machine": describe_machine(), "date": time.strftime("%Y-%m-%d")}
    print("Machine:", json.dumps(out["machine"]))
    if args.which in ("engine", "all"):
        out["engine"] = engine_benchmark(args)
    if args.which in ("api", "all"):
        out["api"] = api_benchmark(args)
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2) + "\n")
        print("\nwrote", args.json)


if __name__ == "__main__":
    main()
