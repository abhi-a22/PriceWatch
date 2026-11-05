import base64
import json

import pytest
from conftest import signup

from pricewatch import app as app_module
from pricewatch.app import create_app
from pricewatch.security import issue_token


def register(client, email="alice@example.com", password="correct-horse-1"):
    return client.post("/api/auth/register", json={"email": email, "password": password})


def login(client, email="alice@example.com", password="correct-horse-1"):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_register_returns_token_and_user(client):
    r = register(client)
    assert r.status_code == 201
    body = r.get_json()
    assert body["user"]["email"] == "alice@example.com" and body["token"]
    assert (
        client.get("/api/alerts", headers={"Authorization": f"Bearer {body['token']}"}).status_code
        == 200
    )


def test_password_is_stored_hashed(client, svc):
    register(client)
    stored = svc.db.conn().execute("SELECT password_hash FROM users").fetchone()[0]
    assert "correct-horse-1" not in stored and stored.startswith("pbkdf2_sha256$1000$")


def test_duplicate_email_rejected_case_insensitively(client):
    assert register(client).status_code == 201
    assert register(client).status_code == 409
    assert register(client, email="ALICE@Example.com").status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": "a@b.co"},
        {"password": "longenough1"},
        {"email": "not-an-email", "password": "longenough1"},
        {"email": "a@b", "password": "longenough1"},
        {"email": "a b@c.com", "password": "longenough1"},
        {"email": "a@b.co", "password": "short"},
        {"email": "a@b.co", "password": "x" * 129},
        {"email": 123, "password": "longenough1"},
        {"email": "a@b.co", "password": 12345678},
        {"email": True, "password": True},
        {"email": ["a@b.co"], "password": "longenough1"},
        {"email": "a" * 250 + "@b.co", "password": "longenough1"},
    ],
)
def test_register_validation(client, payload):
    r = client.post("/api/auth/register", json=payload)
    assert r.status_code == 400 and "error" in r.get_json()


def test_register_requires_json_object(client):
    assert client.post("/api/auth/register", data="nope").status_code == 400
    assert client.post("/api/auth/register", json=[1, 2]).status_code == 400


def test_login_success(client):
    register(client)
    r = login(client)
    assert r.status_code == 200 and r.get_json()["token"]
    assert login(client, email="ALICE@example.com").status_code == 200


def test_login_gives_identical_errors_for_unknown_email_and_wrong_password(client):
    register(client)
    wrong_pw = login(client, password="wrong-password-1")
    unknown = login(client, email="nobody@example.com")
    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.get_json() == unknown.get_json()


def test_login_runs_one_hash_verification_for_unknown_emails(client, svc, monkeypatch):
    register(client)
    calls = []
    real = app_module.verify_password

    def spy(password, stored):
        calls.append(stored)
        return real(password, stored)

    monkeypatch.setattr(app_module, "verify_password", spy)
    login(client, email="nobody@example.com")
    assert calls == [svc.dummy_hash]  # the dummy hash, so timing matches a real check
    calls.clear()
    login(client, password="wrong-password-1")
    assert len(calls) == 1 and calls[0] != svc.dummy_hash


def test_login_validation(client):
    assert client.post("/api/auth/login", json={"email": 1, "password": "x"}).status_code == 400
    assert client.post("/api/auth/login", json={}).status_code == 400


# ----------------------------------------------------------- token validation
def b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def test_missing_or_malformed_authorization_header(client):
    assert client.get("/api/alerts").status_code == 401
    for header in ("", "Bearer", "Bearer ", "Basic abc", "abc"):
        assert client.get("/api/alerts", headers={"Authorization": header}).status_code == 401


def test_forged_token_rejected(client, cfg):
    signup(client)
    forged = issue_token(1, "some-other-secret-" + "z" * 30, 3600)
    r = client.get("/api/alerts", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401 and r.get_json() == {"error": "invalid or expired token"}


def test_expired_token_rejected(client, cfg):
    signup(client)
    expired = issue_token(1, cfg.jwt_secret, 60, now=1_000_000)
    assert (
        client.get("/api/alerts", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    )


def test_alg_none_token_rejected(client):
    signup(client)
    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': '1', 'exp': 9999999999})}."
    assert (
        client.get("/api/alerts", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    )


def test_valid_token_for_deleted_user_rejected(client, svc):
    headers = signup(client)
    svc.db.conn().execute("DELETE FROM users")
    assert client.get("/api/alerts", headers=headers).status_code == 401


# --------------------------------------------------------------- rate limiting
def test_login_and_register_are_rate_limited_per_ip(make_services):
    svc = make_services(rate_limit_attempts=3)
    c = create_app(svc).test_client()
    codes = [login(c).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    r = login(c)
    assert r.get_json()["error"].startswith("too many") and "Retry-After" in r.headers
    # registration shares the limiter, and a different IP is unaffected
    assert register(c).status_code == 429
    other = c.post(
        "/api/auth/login",
        json={"email": "a@b.co", "password": "x"},
        environ_overrides={"REMOTE_ADDR": "10.9.9.9"},
    )
    assert other.status_code == 401
