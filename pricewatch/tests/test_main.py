"""Run `python -m pricewatch` as a real process and check startup and graceful shutdown."""

import os
import signal
import socket
import subprocess
import sys
import time

import pytest
import requests

from pricewatch.config import Config


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_process_starts_serves_and_shuts_down_gracefully(tmp_path, sig):
    port = free_port()
    env = {
        **os.environ,
        "PRICEWATCH_PORT": str(port),
        "PRICEWATCH_DB": str(tmp_path / "main.db"),
        "PRICEWATCH_TICK_INTERVAL": "0.1",
        "PRICEWATCH_SEED": "3",
        "PRICEWATCH_JWT_SECRET": "t" * 48,
        "PRICEWATCH_PBKDF2_ITERATIONS": "1000",
    }
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "pricewatch"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        health = None
        while time.monotonic() < deadline:
            try:
                health = requests.get(base + "/api/health", timeout=1).json()
                if health["bus"]["processed"] > 0:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.1)
        assert health and health["bus"]["processed"] > 0, "server never produced ticks"
        assert health["feed"]["provider"] == "simulated"
        r = requests.post(
            base + "/api/auth/register",
            json={"email": "main@example.com", "password": "correct-horse-1"},
            timeout=5,
        )
        assert r.status_code == 201

        proc.send_signal(sig)
        output, _ = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, output
    assert "shutdown complete" in output
    assert (tmp_path / "main.db").exists()


def test_config_from_env_covers_every_variable():
    cfg = Config.from_env(
        {
            "PRICEWATCH_DB": "x.db",
            "PRICEWATCH_JWT_SECRET": "s" * 40,
            "PRICEWATCH_WEBHOOK_SECRET": "hook-secret",
            "PRICEWATCH_FEED": "CoinGecko",
            "PRICEWATCH_SYMBOLS": "btc,eth",
            "PRICEWATCH_TICK_INTERVAL": "2.5",
            "PRICEWATCH_PARTITIONS": "8",
            "PRICEWATCH_HOST": "0.0.0.0",  # noqa: S104
            "PRICEWATCH_PORT": "1234",
            "PRICEWATCH_ALLOW_PRIVATE_WEBHOOKS": "true",
            "PRICEWATCH_PBKDF2_ITERATIONS": "5000",
        }
    )
    assert (cfg.db_path, cfg.feed, cfg.symbols, cfg.tick_interval) == (
        "x.db", "coingecko", ("BTC", "ETH"), 2.5,
    )  # fmt: skip
    assert (cfg.partitions, cfg.host, cfg.port, cfg.pbkdf2_iterations) == (8, "0.0.0.0", 1234, 5000)  # noqa: S104
    assert cfg.allow_private_webhooks and not cfg.uses_default_secret
    assert cfg.signing_key == b"hook-secret"


def test_signing_key_derived_from_jwt_secret_when_unset():
    a = Config(jwt_secret="a" * 40).signing_key
    assert a == Config(jwt_secret="a" * 40).signing_key
    assert a != Config(jwt_secret="b" * 40).signing_key
    assert a != b"a" * 40  # derived, not the raw JWT secret


@pytest.mark.parametrize("kwargs", [{"symbols": ()}, {"tick_interval": 0}, {"webhook_attempts": 0}])
def test_config_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs)
