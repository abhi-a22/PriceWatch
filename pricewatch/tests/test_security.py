import base64
import json
import socket

import jwt
import pytest

from pricewatch.config import Config
from pricewatch.security import (
    InvalidToken,
    RateLimiter,
    UnsafeURL,
    decode_token,
    hash_password,
    issue_token,
    validate_webhook_url,
    verify_password,
)

SECRET = "x" * 40


# ------------------------------------------------------------------ passwords
def test_hash_is_salted_and_verifies():
    a = hash_password("correct horse", 1000)
    b = hash_password("correct horse", 1000)
    assert a != b  # different salts
    assert a.startswith("pbkdf2_sha256$1000$")
    assert verify_password("correct horse", a)
    assert not verify_password("wrong", a)


def test_verify_uses_stored_iteration_count():
    old = hash_password("pw-pw-pw-pw", 1000)
    assert verify_password("pw-pw-pw-pw", old)  # still valid after config changes


@pytest.mark.parametrize("bad", ["", "garbage", "md5$1$a$b", "pbkdf2_sha256$x$y$z"])
def test_verify_rejects_malformed_hashes(bad):
    assert not verify_password("pw", bad)


# ------------------------------------------------------------------------ JWT
def test_token_roundtrip():
    tok = issue_token(7, SECRET, 60, now=1000)
    assert decode_token(tok, SECRET, now=1010) == 7


def test_expired_token_rejected():
    tok = issue_token(7, SECRET, 60, now=1000)
    with pytest.raises(InvalidToken):
        decode_token(tok, SECRET, now=1060)


def test_expired_token_rejected_with_real_clock():
    tok = issue_token(7, SECRET, 60, now=1000)  # expired in 1970
    with pytest.raises(InvalidToken):
        decode_token(tok, SECRET)


def test_wrong_secret_rejected():
    tok = issue_token(7, "y" * 40, 60, now=1000)
    with pytest.raises(InvalidToken):
        decode_token(tok, SECRET, now=1010)


def _b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def test_alg_none_rejected():
    forged = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'sub': '7', 'exp': 9999999999})}."
    with pytest.raises(InvalidToken):
        decode_token(forged, SECRET)


def test_other_algorithm_rejected():
    tok = jwt.encode({"sub": "7", "exp": 9999999999}, "z" * 64, algorithm="HS512")
    with pytest.raises(InvalidToken):
        decode_token(tok, SECRET)


def test_token_without_exp_rejected():
    tok = jwt.encode({"sub": "7"}, SECRET, algorithm="HS256")
    with pytest.raises(InvalidToken):
        decode_token(tok, SECRET)


def test_garbage_token_rejected():
    with pytest.raises(InvalidToken):
        decode_token("not.a.jwt", SECRET)


# ----------------------------------------------------------------------- SSRF
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "http://localhost/hook",
        "http://[::1]/hook",
        "http://[::ffff:127.0.0.1]/hook",
        "http://169.254.169.254/latest/meta-data/",  # AWS/GCP/Azure metadata
        "http://[fd00:ec2::254]/latest/meta-data/",  # AWS IPv6 metadata
        "http://10.0.0.5/",
        "http://172.16.3.4/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",  # carrier-grade NAT
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://240.0.0.1/",
    ],
)
def test_private_addresses_rejected(url):
    with pytest.raises(UnsafeURL):
        validate_webhook_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "http://user:pw@example.com/",
        "http://user@example.com/",
        "http:///nohost",
        "http://example.com:99999/",
        "",
        None,
        123,
        "http://" + "a" * 3000,
    ],
)
def test_bad_shapes_rejected(url):
    with pytest.raises(UnsafeURL):
        validate_webhook_url(url)


def _fake_resolver(*addresses):
    def resolve(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (a, port)) for a in addresses]

    return resolve


def test_public_address_accepted():
    url = "https://hooks.example.com/x"
    assert validate_webhook_url(url, resolver=_fake_resolver("93.184.216.34")) == url


def test_hostname_resolving_to_private_rejected():
    with pytest.raises(UnsafeURL):
        validate_webhook_url("http://evil.example/", resolver=_fake_resolver("10.1.2.3"))


def test_one_private_address_among_public_rejected():
    resolver = _fake_resolver("93.184.216.34", "127.0.0.1")
    with pytest.raises(UnsafeURL):
        validate_webhook_url("http://rebind.example/", resolver=resolver)


def test_unresolvable_host_rejected():
    def boom(*a, **k):
        raise socket.gaierror("nope")

    with pytest.raises(UnsafeURL):
        validate_webhook_url("http://nope.invalid/", resolver=boom)


def test_allow_private_flag_permits_loopback():
    assert validate_webhook_url("http://127.0.0.1:9/x", allow_private=True)


# ---------------------------------------------------------------- rate limiter
def test_rate_limiter_blocks_then_recovers():
    now = [0.0]
    rl = RateLimiter(3, 10, clock=lambda: now[0])
    assert [rl.allow("ip") for _ in range(4)] == [True, True, True, False]
    assert rl.allow("other-ip")
    now[0] = 10.5
    assert rl.allow("ip")


# --------------------------------------------------------------------- config
def test_config_from_env():
    cfg = Config.from_env(
        {
            "PRICEWATCH_SYMBOLS": "aapl, btc",
            "PRICEWATCH_PARTITIONS": "2",
            "PRICEWATCH_PORT": "9000",
            "PRICEWATCH_ALLOW_PRIVATE_WEBHOOKS": "1",
            "PRICEWATCH_SEED": "5",
        }
    )
    assert cfg.symbols == ("AAPL", "BTC")
    assert cfg.partitions == 2 and cfg.port == 9000 and cfg.seed == 5
    assert cfg.allow_private_webhooks is True


def test_config_defaults_and_validation():
    cfg = Config()
    assert cfg.symbols == ("AAPL", "MSFT", "NVDA", "TSLA", "BTC", "ETH")
    assert cfg.uses_default_secret and cfg.allow_private_webhooks is False
    with pytest.raises(ValueError):
        Config(feed="nope")
    with pytest.raises(ValueError):
        Config(partitions=0)
