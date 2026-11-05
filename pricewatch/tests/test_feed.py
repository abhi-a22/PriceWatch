import pytest
import requests

from pricewatch.feed import (
    CoinGeckoProvider,
    FeedRunner,
    ProviderError,
    SimulatedProvider,
    backoff_delay,
)

SYMBOLS = ("AAPL", "MSFT", "NVDA", "TSLA", "BTC", "ETH")


def test_simulated_is_deterministic_for_a_seed():
    a, b = SimulatedProvider(SYMBOLS, seed=42), SimulatedProvider(SYMBOLS, seed=42)
    assert [a.fetch() for _ in range(50)] == [b.fetch() for _ in range(50)]


def test_simulated_differs_across_seeds():
    a, b = SimulatedProvider(SYMBOLS, seed=1), SimulatedProvider(SYMBOLS, seed=2)
    assert [a.fetch() for _ in range(5)] != [b.fetch() for _ in range(5)]


def test_simulated_prices_stay_positive_even_under_extreme_volatility():
    p = SimulatedProvider(SYMBOLS, seed=3, volatility=0.5, shock_probability=0.5)
    for _ in range(2000):
        assert all(v > 0 for v in p.fetch().values())


def test_simulated_produces_shocks_of_one_to_two_percent():
    p = SimulatedProvider(["AAPL"], seed=7, volatility=0.0, shock_probability=1.0)
    prev = 190.0
    for _ in range(100):
        cur = p.fetch()["AAPL"]
        assert 0.0099 <= abs(cur - prev) / prev <= 0.0201
        prev = cur


def test_simulated_covers_default_symbols():
    assert set(SimulatedProvider(SYMBOLS, seed=0).fetch()) == set(SYMBOLS)


# ------------------------------------------------------------------ CoinGecko
class FakeResponse:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


def test_coingecko_parses_response():
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params, timeout))
        return FakeResponse(payload={"bitcoin": {"usd": 64000.5}, "ethereum": {"usd": 3100}})

    prices = CoinGeckoProvider(["BTC", "ETH"], http_get=fake_get).fetch()
    assert prices == {"BTC": 64000.5, "ETH": 3100.0}
    assert calls[0][1] == {"ids": "bitcoin,ethereum", "vs_currencies": "usd"}


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(status=429),
        FakeResponse(status=500),
        FakeResponse(bad_json=True),
        FakeResponse(payload={}),
        FakeResponse(payload={"bitcoin": {}}),
        FakeResponse(payload={"bitcoin": {"usd": "abc"}}),
        FakeResponse(payload={"bitcoin": {"usd": -5}}),
        FakeResponse(payload=None),
    ],
)
def test_coingecko_bad_responses_raise_provider_error(response):
    provider = CoinGeckoProvider(["BTC"], http_get=lambda *a, **k: response)
    with pytest.raises(ProviderError):
        provider.fetch()


def test_coingecko_network_error_becomes_provider_error():
    def boom(*a, **k):
        raise requests.ConnectionError("down")

    with pytest.raises(ProviderError):
        CoinGeckoProvider(["BTC"], http_get=boom).fetch()


def test_coingecko_rejects_unsupported_symbols():
    with pytest.raises(ValueError, match="AAPL"):
        CoinGeckoProvider(["AAPL"])


# ----------------------------------------------------------------- FeedRunner
def test_backoff_delay_doubles_then_caps():
    assert [backoff_delay(n, 1.0, 60.0) for n in range(1, 9)] == [1, 2, 4, 8, 16, 32, 60, 60]


class Flaky:
    name = "flaky"

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ProviderError("boom")
        return {"AAPL": 100.0}


def test_runner_backs_off_exponentially_and_recovers():
    published = []
    provider = Flaky(fail_times=8)
    runner = FeedRunner(
        provider, lambda p, ts: published.append((p, ts)), interval=1.0, clock=lambda: 1000.0
    )
    delays = [runner.poll_once() for _ in range(8)]
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]
    assert runner.health()["status"] == "degraded"
    assert runner.health()["consecutive_failures"] == 8
    assert "boom" in runner.health()["last_error"]
    assert published == []

    assert runner.poll_once() == 1.0  # recovered: back to normal interval
    health = runner.health()
    assert health["status"] == "ok" and health["consecutive_failures"] == 0
    assert published == [({"AAPL": 100.0}, 1_000_000)]


def test_runner_treats_handler_exceptions_as_failures():
    def bad_handler(prices, ts):
        raise RuntimeError("downstream broke")

    runner = FeedRunner(Flaky(0), bad_handler, interval=1.0)
    assert runner.poll_once() == 1.0
    assert runner.health()["status"] == "degraded"


def test_runner_thread_polls_and_stops():
    import threading

    got = threading.Event()
    runner = FeedRunner(Flaky(0), lambda p, ts: got.set(), interval=0.01)
    assert runner.health()["status"] == "starting"
    runner.start()
    assert got.wait(2)
    runner.stop()
    assert runner.health()["status"] == "stopped"
