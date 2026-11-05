# PriceWatch walkthrough (for explaining it in an interview)

Read this top to bottom once, then open the file next to each section and find the code it
describes. If you can do that, you can explain the project.

## The 30-second pitch

"PriceWatch is a price alert service. Users sign up, set rules like 'tell me when NVDA is
above 130' or 'when BTC drops 2% in one minute', and watch live prices on a dashboard. When a
rule triggers they see it in the dashboard and, optionally, get a signed webhook. Prices flow
through a partitioned event bus so each symbol is processed by exactly one thread, which keeps
ordering correct without locks. I built it with Flask and SQLite, wrote 246 tests, and
benchmarked it."

(Check the test count and the benchmark numbers against the README before you say them.)

## The life of one price tick

1. The **feed thread** asks the provider for prices.
2. Each price is saved in the **price store**.
3. The tick is put on the **bus**, on the queue for that symbol's partition.
4. A **bus worker thread** takes it and asks the **rule engine** to check every active rule
   for that symbol.
5. If a rule matches, the engine writes an **event** to the database in one transaction.
6. If the alert has a webhook, the event id goes to the **webhook pool**, which POSTs it.
7. Meanwhile the **dashboard** polls the API and shows prices, alerts, and events.

Everything below explains one of these steps and why it looks the way it does.

---

## 1. Price feed (`pricewatch/feed.py`)

**What it is.** A *provider* is anything with a `fetch()` method that returns
`{symbol: price}`. There are two: a simulator and CoinGecko (live crypto).

**Simulator.** Each tick multiplies every price by `1 + small random number` (a random walk).
With a 2% chance it adds a 1–2% jump. *Why the jumps:* real prices rarely move 2% in a few
seconds, so without them a demo would never trigger a percent alert. *Why seedable:* the same
seed gives the same prices, so tests are repeatable.

**CoinGecko.** Calls the free API and validates the response (missing symbol, non-positive
price, bad HTTP status all raise `ProviderError`). The HTTP function is a constructor
argument so tests pass a fake, which is called *dependency injection*. Interviewers like
hearing that you did this on purpose. Free-tier rate limits are why the code never polls
faster than every 10 seconds for CoinGecko.

**FeedRunner.** A thread that calls `fetch()` in a loop. If it fails it waits 1, 2, 4, 8 …
seconds up to 60 (*exponential backoff*), so a broken provider is not hammered. After one
success it goes back to normal. It tracks health, which `/api/health` shows.
*Design detail worth mentioning:* all the logic is in `poll_once()`, which returns "how long
to wait". The thread just calls it in a loop. That is why the backoff test needs no real
sleeping.

## 2. Price store (`pricewatch/store.py`)

**What it is.** In-memory prices: the latest per symbol plus a rolling history (default 11
minutes).

**Key ideas.**
* **Thread-safe**: one lock around the data, held only briefly.
* **Rejects stale/duplicate timestamps**, so replayed or out-of-order ticks cannot corrupt
  history.
* **`reference_price(symbol, t)`**: "what was the price at or just before time `t`?" The
  history is a sorted list, so it uses `bisect` (binary search), O(log n). Percent rules use
  this: "rose 2% in 60 s" means compare now with the price 60 s ago.
* It returns **`None` until history is long enough**. A percent rule must stay quiet until
  it has a full window, otherwise it would compare against a price from only 5 seconds ago
  and fire wrongly.
* **Pruning keeps one point older than the cutoff**, so the oldest window is always
  answerable. This is an easy off-by-one to get wrong, and there is a test for it.

## 3. Partitioned event bus (`pricewatch/bus.py`)

**The problem.** One thread evaluating everything is simple but slow; a pool of threads that
all take any tick can process two ticks for the same symbol out of order, and then you need
locks around rule state.

**The solution.** N worker threads, each with its *own* queue. A symbol is mapped to a
queue by `crc32(symbol) % N`. So all ticks for AAPL always go to the same thread, in order.
That means:
* per-symbol **ordering is guaranteed** by construction;
* per-symbol state (like "when did this rule last fire") is only ever touched by one thread,
  so it needs **no locks**.

**Why `crc32` and not `hash()`.** Python randomizes `hash()` of strings every run, so a
symbol would land on a different thread after each restart. `crc32` is stable. (This is the
same idea Kafka uses with message keys, which is a good thing to say.)

**Backpressure.** Queues are bounded. If one is full, `submit` drops the tick and counts it
instead of blocking. *Why:* the feed must never be stalled by a slow consumer, and unbounded
queues eat memory until the process dies. Losing one tick is fine because the next one
arrives a second later.

**Robustness.** An exception in a handler is logged and counted, and the worker carries on.
`stop()` puts a sentinel behind queued work so everything already accepted is processed
before the threads exit (a *graceful drain*).

**Latency stats.** Each item is timestamped at enqueue; the worker records the elapsed time
after the handler finishes. `/api/health` reports p50/p95/p99 of that (nearest-rank
percentiles).

## 4. Rule engine (`pricewatch/engine.py`)

**Rule types.** `above`, `below` (inclusive: a price exactly equal to the target fires),
`pct_rise`, `pct_drop` (compare with the price one window ago).

**Rule cache.** Active rules are loaded from SQLite once and grouped by symbol, so each tick
is a dictionary lookup, not a database query. Creating, editing, or deleting an alert calls
`invalidate()`, which drops the cache so it is reloaded on the next tick.

**One-time vs repeating, cooldown.** One-time alerts switch themselves off after firing.
Repeating alerts can fire again after a cooldown. Cooldown is measured in **tick time** (the
timestamp on the price), not the wall clock, which makes the behavior deterministic and
testable with fixed timestamps.

**Idempotent firing — the most interview-worthy part.** Firing does, in one transaction
(`BEGIN IMMEDIATE`):
1. `INSERT OR IGNORE` into `alert_events`;
2. if a row was actually inserted, update the alert (`last_triggered_at`, maybe `active=0`).

The table has `UNIQUE(alert_id, tick_ts_ms)`, so the same alert cannot fire twice for the
same tick. If a tick is replayed (after a crash, or by a future Kafka consumer redelivering
it), the insert is ignored and nothing else happens. The *database* guarantees this, not an
`if` statement that two threads could race through. That property is called **idempotency**
and it is what makes "at-least-once delivery" safe.

**Deleted-while-evaluating.** The engine works from a cached copy, so a user can delete an
alert after the engine loaded it. The foreign key on `alert_events.alert_id` then rejects the
insert. The engine catches that, skips the rule, and keeps going. There is a test that
deletes an alert behind the engine's back.

## 5. Webhooks (`pricewatch/webhooks.py`)

**Why a separate pool.** A customer's server might hang for 5 seconds on each of 3 retries.
If rule evaluation did the HTTP call, one bad webhook would delay every alert. So evaluation
just puts the event id on a queue (instant) and a different set of threads does the slow work.

**What gets sent.** JSON with event id, alert id, symbol, price, message, `fired_at`, and the
rule. The header `X-PriceWatch-Signature: sha256=<hex>` is an **HMAC-SHA256** of the exact
body bytes using a server secret. The receiver recomputes it; if it matches, the request
really came from PriceWatch and was not modified. (Comparison should be constant-time:
`hmac.compare_digest`.)

**Reliability.** 2xx means delivered. Anything else is retried with exponential backoff (1 s,
2 s, …) up to `webhook_attempts`. After the last failure the event is marked `failed`, the
last error is stored, and the dashboard shows a Retry button that calls
`POST /api/events/<id>/retry`.

**Safety.** Redirects are never followed (a redirect could point at an internal address).
There is a request timeout. The URL is re-validated before *every* attempt (see SSRF below).

## 6. Security (`pricewatch/security.py`, `pricewatch/app.py`)

**Passwords.** PBKDF2-HMAC-SHA256 with a random 16-byte salt and 600,000 iterations (the
iteration count is stored inside each hash, so it can be raised later without locking anyone
out). Why slow on purpose: if the database leaks, each guess costs an attacker real time.
Why a salt: identical passwords get different hashes and precomputed tables are useless.
Comparison uses `hmac.compare_digest`, which takes the same time no matter where a mismatch
is.

**Sessions.** A JWT (JSON Web Token) signed with HS256, valid 24 hours. The server pins
`algorithms=["HS256"]` when decoding. That single argument is what stops the classic
`alg: none` attack (a token that claims it needs no signature) and algorithm-confusion
attacks. Tokens with a wrong secret or past expiry are rejected, and tests forge all three.

**No user enumeration.** Login returns the same message for "no such email" and "wrong
password". It also runs a password hash against a dummy value when the email is unknown, so
the response *time* does not reveal which emails exist either.

**Access control.** Every alert and event query includes `AND user_id = ?`, and "not yours"
looks identical to "does not exist" (404). Otherwise an attacker could probe for valid ids.

**SSRF (server-side request forgery).** Webhooks make the *server* call a URL the *user*
chose. A malicious user could point it at `http://169.254.169.254/` (the cloud metadata
service, which hands out credentials) or at `localhost` services. The validator allows only
http/https, rejects credentials in the URL, resolves the hostname, and rejects private,
loopback, link-local, multicast, reserved, and unspecified addresses (including IPv6 and
IPv4-mapped IPv6). It runs when the alert is saved *and* before each delivery, because DNS
answers can change in between (*DNS rebinding*). Honest limitation: a tiny race remains
between our check and the HTTP library's own lookup. It is listed in the architecture doc.

**Input validation.** Every field is type- and range-checked. Booleans are rejected where
numbers are expected, because in Python `True` is an `int` and `isinstance(True, int)` is
true. That is a classic bug. NaN/Infinity are rejected. Unknown fields are rejected. Bodies
over 64 KB get a 413. Each user can have 50 alerts.

**Headers.** `Content-Security-Policy` allows scripts and styles only from our own origin (no
inline), plus `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy`, and `Cache-Control: no-store` on API responses.

**Rate limiting.** A sliding window per IP on login and register slows password guessing.

## 7. Database (`pricewatch/db.py`)

SQLite in **WAL mode** (write-ahead log): readers do not block the writer. **One connection
per thread** (`threading.local`) because a SQLite connection should not be shared across
threads. Connections run in autocommit mode with explicit `BEGIN IMMEDIATE`, so every
transaction is visible in the code. `BEGIN IMMEDIATE` takes the write lock up front, which
avoids a class of "database is locked" errors when a read transaction tries to upgrade.
Foreign keys are switched on (SQLite has them off by default) with `ON DELETE CASCADE`, so
deleting a user or alert cleans up its children.

Tables: `users`, `alerts`, `alert_events`. Indexes exist for the two hot queries: "active
rules for a symbol" and "a user's events, newest first".

## 8. The API (`pricewatch/app.py`)

Flask app built by `create_app(services)`, so tests can build one with a temporary database.
All errors are JSON `{"error": "..."}`, including 404, 405, 413, and unexpected 500s.
`PATCH /api/alerts/<id>` accepts only `active`, `threshold`, `repeating`,
`cooldown_seconds`, `webhook_url` — you cannot change which symbol or rule type an alert is
after creating it, which keeps the cached rule and its history meaningful.
`/api/events` uses **cursor pagination** (`before_id`), not offsets, so new events arriving
while you page never cause skipped or repeated rows.

## 9. The dashboard (`pricewatch/static/`)

Plain HTML/CSS/JS. It polls `/api/prices` every second. Details:
* **No `innerHTML`.** Everything is built with `createElement` and `textContent`, so even
  hostile text from the server cannot become markup. A test scans the source for banned
  APIs.
* **Strict CSP**, so even if someone found an injection bug the browser would refuse to run
  injected scripts. That is why there are no inline styles; the sparkline colors come from
  CSS classes.
* The session token is kept in `sessionStorage` (cleared when the tab closes), not
  `localStorage`.
* Dark/light mode via `prefers-color-scheme`; responsive layout; a test checks there is no
  horizontal scroll at phone width.

## 10. Running it (`__main__.py`, `services.py`)

`Services` builds and connects everything. `__main__` starts the Werkzeug server in threaded
mode and handles SIGINT/SIGTERM. A subtle detail: `server.shutdown()` blocks until
`serve_forever()` returns, and `serve_forever()` is running on the same thread that the
signal handler interrupts, so calling it directly would deadlock; the handler starts a
separate thread to call it. Shutdown order follows the data flow: stop the feed, drain the
bus, drain the webhooks, close the database. A test starts the real process, sends it
SIGTERM and SIGINT, and checks it exits cleanly.

Because the feed, store, and bus are in memory, it must run as **one process**.

## 11. Tests (`tests/`)

Fixed timestamps everywhere, so tests are deterministic. They cover each component alone
(engine with a fake clock-free tick source, bus with threads, webhooks against a real local
stub HTTP server, SSRF with a fake DNS resolver), the API through Flask's test client, one
**end-to-end test** over a real socket (sign up → alert → price crosses → webhook arrives
with a valid signature), a **process test** (start, serve, SIGTERM), and a **browser test**
with Playwright that clicks through the dashboard. A good thing to know about the browser
test: an early version was flaky because re-arming an alert that was already true made it
fire again before the assertion ran; the fix was to test pause/re-arm on an alert that
cannot fire. That is a real story about race conditions in tests.

## 12. Benchmarks (`scripts/loadtest.py`)

Two benchmarks. The **engine** one puts 500 active rules in place and pushes ticks through
the real store → bus → engine path. It has two modes: *saturation* (as fast as possible,
gives maximum throughput) and *paced* (fixed rate, gives honest latency at that load). The
**API** one starts the server as its own process and hits it from several client processes.
The README has the numbers from a real run on a described machine. Know what each number
means before you quote it:
* **Throughput under saturation** is a capacity figure; its latency is dominated by queueing
  and is *not* a per-tick cost.
* **Paced latency** answers "at N ticks/s, how long does a tick wait until its rules are
  evaluated?"
* The benchmark runs the load generator and the engine in one Python process, so they share
  the GIL. Real throughput with a separate feed process could differ.

## Questions you should be ready for

* *Why threads and not asyncio?* Rule evaluation does SQLite calls and CPU work; both are
  simple with threads, and the GIL is released during SQLite and socket calls. Asyncio
  would add complexity without removing the GIL limit on CPU work.
* *What is the bottleneck?* Python's GIL for the evaluation itself; SQLite's single writer
  for firing. The benchmark shows both scenarios.
* *What happens if the server crashes?* Alerts and events are in SQLite and survive.
  Price history is in memory and is rebuilt as ticks arrive; percent rules stay silent
  until history refills. Events left `pending` are not retried automatically (a known gap).
* *How would you scale it?* Postgres, then Kafka keyed by symbol, then several consumer
  processes. See `docs/ARCHITECTURE.md`. The rule engine already assumes "one consumer owns
  a symbol", which is exactly Kafka's partition ownership.
* *What would you change?* Per-user webhook secrets, re-queueing `pending` events at startup,
  closing the DNS-rebinding race, SSE instead of polling.
