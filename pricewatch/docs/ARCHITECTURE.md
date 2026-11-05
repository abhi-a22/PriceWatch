# Architecture

PriceWatch is one Python process. Data flows in one direction through five stages, and
each stage is a small module with one job.

```
                         ┌──────────────────────── one process ───────────────────────┐
                         │                                                            │
 Provider ──► FeedRunner ──► PriceStore ──► PartitionedBus ──► RuleEngine ──► SQLite  │
 (simulated / (thread,       (latest +      (N threads, one     (cached rules,  (WAL) │
  CoinGecko)   backoff)       history)       queue each)         idempotent)    │     │
                         │        ▲                                   │         │     │
                         │        │ reads                             ▼         │     │
                         │   Flask API ◄──────────────────────── WebhookDispatcher    │
                         │   + dashboard                          (own queue + pool)  │
                         └────────────────────────────────────────────────────────────┘
```

## Data flow, step by step

1. **Provider** (`feed.py`). A provider has one method, `fetch()`, returning
   `{symbol: price}`. `SimulatedProvider` is a seedable random walk with a 2% chance per
   tick of a 1–2% shock. `CoinGeckoProvider` calls the free public API; its HTTP function
   is injected so tests use fakes.
2. **FeedRunner** (`feed.py`). A thread calls `fetch()` every `PRICEWATCH_TICK_INTERVAL`
   seconds and hands the result to `Services.ingest`. On any failure it waits
   1 s, 2 s, 4 s … up to 60 s, and returns to the normal interval after the first success.
   `poll_once()` contains all the logic, so tests drive it without threads or sleeping.
3. **PriceStore** (`store.py`). Per symbol: the latest price and a time-bounded history
   kept as two sorted lists (timestamps, prices). It refuses stale or duplicate
   timestamps. `reference_price(symbol, t)` uses `bisect` to find the price at or just
   before `t`, and returns `None` until history reaches back that far. Pruning always
   keeps one point at or before the cutoff so the full 600 s rule window stays answerable.
4. **PartitionedBus** (`bus.py`). Each tick goes to partition `crc32(symbol) % N`. Every
   partition has its own bounded queue and its own thread, so one symbol is always
   handled by one thread. That gives per-symbol ordering for free and means the rule
   state for a symbol needs no locks. `submit` never blocks: a full queue drops the tick
   and increments a counter.
5. **RuleEngine** (`engine.py`). On each tick it looks up the active rules for that symbol
   (cached in memory), evaluates them, and fires matches. Firing is one atomic
   `BEGIN IMMEDIATE` transaction: `INSERT OR IGNORE` the event, then update the alert.
   `UNIQUE(alert_id, tick_ts_ms)` makes replays harmless.
6. **WebhookDispatcher** (`webhooks.py`). Fired events with a webhook URL go on a separate
   queue with its own worker threads. Delivery signs the body, retries with exponential
   backoff, and records the outcome on the event row.
7. **API and dashboard** (`app.py`, `static/`). Flask serves the REST API and the
   single-page dashboard, which polls the API.

## Why these choices

| Decision | Reason | Cost |
|---|---|---|
| One process, in-memory feed/store/bus | The simplest design that is actually correct. No broker to run, no cross-process ordering to reason about. | Cannot scale horizontally as is. A restart loses price history (alerts and events are in SQLite and survive). |
| Partition by symbol hash | Per-symbol ordering and lock-free per-symbol state. | Throughput for one very hot symbol is capped by one thread. |
| `crc32`, not `hash()` | `hash()` of a str is randomized per process, so assignments would change on every restart. | None. |
| Bounded queues that drop | A slow consumer must never stall the feed, and memory must stay bounded. A dropped tick is replaced by the next one a second later. | Under extreme overload some ticks are skipped (counted in `/api/health`). |
| Cooldown in tick time | Tests and replays are deterministic, and it does not depend on how fast the machine processed the tick. | Cooldown follows feed timestamps, not wall-clock time. |
| `UNIQUE(alert_id, tick_ts_ms)` + `INSERT OR IGNORE` | Idempotency enforced by the database, not by application checks that could race. | One extra index. |
| Webhooks on a separate pool | A dead endpoint (5 s timeout × 3 retries) must not delay rule evaluation. | Delivery state lives in the DB, so a crash mid-delivery leaves an event `pending` (see limitations). |
| SQLite in WAL mode, one connection per thread | Readers do not block the single writer, no shared-connection locking, zero setup. | One writer at a time. Fine at this scale. |
| JWT (HS256) with `algorithms=["HS256"]` pinned | Stateless sessions. Pinning the algorithm is what rejects `alg: none`. | Tokens cannot be revoked before they expire (24 h). |
| Dashboard polls the API | Works through any proxy, trivial to reason about. | ~1 s update granularity and a request per second per client. |

## Concurrency model

* **Feed thread** (1): writes `PriceStore`, calls `bus.submit`.
* **Bus workers** (N, default 4): read `PriceStore`, own all rule state for their symbols,
  write to SQLite, call `webhooks.enqueue`.
* **Webhook workers** (default 2): read and update event rows, make HTTP calls.
* **Request threads** (Werkzeug starts one per request): read `PriceStore`, read/write SQLite.
  Their database connection is closed at the end of each request.

Shared structures: `PriceStore` is guarded by one lock held only for tiny critical
sections. Bus counters have a single writer each. The rule cache is replaced wholesale
(never mutated in place) under a lock when it is invalidated.

## Failure behaviour

| Failure | What happens |
|---|---|
| Price provider down | Backoff to 60 s, `/api/health` reports `degraded` with the last error, recovers automatically. |
| Alert deleted while being evaluated | The foreign key rejects the event insert, the rule is skipped, the worker continues. |
| Handler raises | Logged and counted in `errors`; the worker keeps running. |
| Queue full | Tick dropped and counted; the feed is never blocked. |
| Webhook endpoint dead/slow | Only the webhook pool is affected; the event ends `failed` and can be retried from the dashboard. |
| SIGINT / SIGTERM | HTTP server stops, feed stops, bus drains queued ticks, webhook pool finishes queued deliveries, then exit. |

## Security boundaries

* Every alert and event query includes `AND user_id = ?`; another user's id returns 404.
* Webhook URLs are checked (scheme, credentials, resolved addresses) when saved **and**
  again immediately before every delivery attempt; redirects are never followed.
* The dashboard's Content-Security-Policy forbids inline script/style and all external
  origins, and the JavaScript never assigns HTML strings.

## Upgrade paths (not implemented)

* **PostgreSQL**: the data layer is `db.py` plus the SQL strings in `app.py`, `engine.py`
  and `webhooks.py`. Move to a connection pool (psycopg), replace `INSERT OR IGNORE` with
  `INSERT ... ON CONFLICT DO NOTHING`, and replace `BEGIN IMMEDIATE` with a normal
  transaction (the unique constraint already provides the idempotency). That removes the
  single-writer limit and allows several API processes.
* **Kafka (or similar)**: replace `PartitionedBus` with a topic keyed by symbol. The
  partitioning idea is the same one Kafka uses (same key, same partition, ordered), so the
  rule engine's "one consumer owns a symbol" assumption carries over unchanged. Consumers
  could then run as separate processes, and ticks could be replayed after a crash. The
  idempotent `INSERT OR IGNORE` is exactly what makes at-least-once delivery safe.
* **Push instead of polling**: Server-Sent Events from the API for prices and events.
* **Multiple instances**: needs the two upgrades above plus a shared rate limiter (Redis).

## Known limitations

* Single process; do not run several copies against one database.
* A DNS-rebinding race remains between the delivery-time URL check and the HTTP library's
  own name lookup. Closing it needs connecting to the checked IP while keeping the
  original Host header/SNI.
* All webhooks are signed with one server-wide secret (`PRICEWATCH_WEBHOOK_SECRET`), not a
  per-user secret.
* Events stuck in `pending` after a crash are not re-queued on startup.
* The login rate limiter is per IP, in memory, and keyed on the socket address; behind a
  reverse proxy every client appears as the proxy.
* Registration reveals whether an email already exists (it must, to report a duplicate);
  login does not.
* Event rows are never pruned.
