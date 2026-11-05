"""Password hashing, JWT sessions, SSRF validation and rate limiting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import socket
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

import jwt

# --------------------------------------------------------------------- passwords


def hash_password(password: str, iterations: int) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    salt_b64 = base64.b64encode(salt).decode()
    digest_b64 = base64.b64encode(digest).decode()
    return f"pbkdf2_sha256${iterations}${salt_b64}${digest_b64}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. The iteration count comes from the stored hash so
    raising the configured count never locks out existing users."""
    try:
        scheme, iters, salt_b64, digest_b64 = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# -------------------------------------------------------------------------- JWT


class InvalidToken(Exception):
    pass


def issue_token(user_id: int, secret: str, ttl_seconds: int, now: float | None = None) -> str:
    issued = int(time.time() if now is None else now)
    payload = {"sub": str(user_id), "iat": issued, "exp": issued + ttl_seconds}
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str, now: float | None = None) -> int:
    """Return the user id or raise InvalidToken.

    Pinning algorithms=["HS256"] is what rejects `alg: none` and algorithm
    confusion attacks; requiring `exp` rejects tokens that never expire.
    """
    try:
        # PyJWT checks exp against the real clock; a `now` override is only for tests,
        # so we do the expiry comparison ourselves in that case.
        options = {"require": ["exp", "sub"], "verify_exp": now is None}
        claims = jwt.decode(token, secret, algorithms=["HS256"], options=options)
        if now is not None and claims["exp"] <= now:
            raise InvalidToken("token expired")
        return int(claims["sub"])
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
        raise InvalidToken(str(exc)) from exc


# ------------------------------------------------------------------------- SSRF


class UnsafeURL(ValueError):
    pass


def validate_webhook_url(url, allow_private: bool = False, resolver=socket.getaddrinfo) -> str:
    """Return the URL if it is safe to call, otherwise raise UnsafeURL.

    Checks scheme, embedded credentials, and that every address the hostname
    resolves to is a public one. `resolver` is injectable so tests need no DNS.
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise UnsafeURL("webhook_url must be a non-empty string of at most 2048 characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL("webhook_url is not a valid URL") from exc
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("webhook_url must use http or https")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURL("webhook_url must not contain credentials")
    host = parts.hostname
    if not host:
        raise UnsafeURL("webhook_url must include a hostname")
    try:
        default_port = 443 if parts.scheme == "https" else 80
        infos = resolver(host, port or default_port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise UnsafeURL("webhook_url hostname could not be resolved") from exc
    if not infos:
        raise UnsafeURL("webhook_url hostname could not be resolved")
    if allow_private:
        return url
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is still loopback
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
            or not ip.is_global
        ):
            raise UnsafeURL("webhook_url resolves to a non-public address")
    return url


# ----------------------------------------------------------------- rate limiting


class RateLimiter:
    """Sliding-window limiter keyed by client (here: IP). In-memory, single process."""

    def __init__(self, max_attempts: int, window_seconds: float, clock=time.monotonic):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.max_attempts:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:  # bound memory under address churn
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window]:
                    del self._hits[k]
            return True
