import pytest

from pricewatch.config import Config
from pricewatch.db import Database

SYMBOLS = ("AAPL", "MSFT", "NVDA", "TSLA", "BTC", "ETH")
T0 = 1_700_000_000_000  # fixed epoch ms: every test uses fixed timestamps


@pytest.fixture
def cfg(tmp_path):
    return Config(
        db_path=str(tmp_path / "test.db"),
        jwt_secret="test-secret-" + "x" * 40,
        pbkdf2_iterations=1000,  # fast hashing in tests
        allow_private_webhooks=True,  # webhook tests use a local stub server
        webhook_backoff_base=0.0,
        webhook_timeout=2.0,
        rate_limit_attempts=1000,
        tick_interval=3600.0,  # real feed thread stays idle; tests inject ticks
        seed=1,
    )


@pytest.fixture
def db(cfg):
    d = Database(cfg.db_path)
    d.init_schema()
    yield d
    d.close_thread_connection()


def add_user(db, email="u@example.com"):
    cur = db.conn().execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?, 'x', 'now')", (email,)
    )
    return cur.lastrowid


def add_alert(db, user_id, symbol="AAPL", rule_type="above", threshold=100.0, **kw):
    fields = {
        "window_seconds": None,
        "repeating": 0,
        "cooldown_seconds": 0,
        "webhook_url": None,
        "active": 1,
        **kw,
    }
    cur = db.conn().execute(
        "INSERT INTO alerts (user_id, symbol, rule_type, threshold, window_seconds, repeating, "
        "cooldown_seconds, webhook_url, active, created_at) VALUES (?,?,?,?,?,?,?,?,?, 'now')",
        (
            user_id,
            symbol,
            rule_type,
            threshold,
            fields["window_seconds"],
            int(fields["repeating"]),
            fields["cooldown_seconds"],
            fields["webhook_url"],
            int(fields["active"]),
        ),
    )
    return cur.lastrowid


def events(db, alert_id=None):
    sql, args = "SELECT * FROM alert_events", ()
    if alert_id is not None:
        sql, args = sql + " WHERE alert_id = ?", (alert_id,)
    return db.conn().execute(sql + " ORDER BY id", args).fetchall()


# ---------------------------------------------------------------- API fixtures
class ScriptedProvider:
    name = "scripted"

    def fetch(self):
        return {"AAPL": 100.0}


@pytest.fixture
def make_services(cfg):
    """Factory so a test can tweak the config; every instance is stopped afterwards."""
    from dataclasses import replace

    from pricewatch.services import Services

    created = []

    def make(services_kwargs=None, **overrides):
        s = Services(
            replace(cfg, **overrides), provider=ScriptedProvider(), **(services_kwargs or {})
        )
        created.append(s)
        return s

    yield make
    for s in created:
        s.stop()


@pytest.fixture
def svc(make_services):
    return make_services()


@pytest.fixture
def client(svc):
    from pricewatch.app import create_app

    return create_app(svc).test_client()


def signup(client, email="alice@example.com", password="correct-horse-1"):
    r = client.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.get_json()
    return {"Authorization": "Bearer " + r.get_json()["token"]}


# ------------------------------------------------------- real HTTP server fixture
class RampProvider:
    """Deterministic feed: AAPL rises 0.5 per poll from 100."""

    name = "ramp"

    def __init__(self):
        self.price = 99.5

    def fetch(self):
        self.price += 0.5
        return {"AAPL": self.price}


class LiveServer:
    def __init__(self, svc, server, thread):
        self.svc, self.server, self.thread = svc, server, thread
        self.url = f"http://127.0.0.1:{server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.svc.stop()


@pytest.fixture
def start_live(cfg):
    """Start the real stack (feed thread, bus, webhooks, threaded Werkzeug server)."""
    import threading
    from dataclasses import replace

    from werkzeug.serving import make_server

    from pricewatch.app import create_app
    from pricewatch.services import Services

    started = []

    def start(provider=None, **overrides):
        from pricewatch.feed import SimulatedProvider

        c = replace(cfg, **overrides)
        svc = Services(c, provider=provider or SimulatedProvider(c.symbols, seed=c.seed))
        server = make_server("127.0.0.1", 0, create_app(svc), threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        svc.start()
        live = LiveServer(svc, server, thread)
        started.append(live)
        return live

    yield start
    for live in started:
        live.stop()
