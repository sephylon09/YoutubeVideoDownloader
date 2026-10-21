"""Password login, signed session cookies and in-memory rate limiting.

Only used when the app is reachable from other machines (public mode). The
state lives in this process, which matches how the app is deployed: a single
process with in-memory jobs.

Session cookies are ``<session id>.<expiry>.<HMAC-SHA256>``. The signing key is
derived from ``MD_SECRET_KEY`` *and* the password, so changing either one signs
everybody out.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

from .logging_setup import get_logger

log = get_logger("security")

SESSION_COOKIE = "md_session"


def password_matches(given: str, expected: str) -> bool:
    """Constant-time comparison that doesn't leak the password length either."""
    return hmac.compare_digest(hashlib.sha256(given.encode("utf-8")).digest(),
                               hashlib.sha256(expected.encode("utf-8")).digest())


class SessionSigner:
    """Issues and verifies tamper-proof, expiring session tokens."""

    def __init__(self, secret_key: str | None, password: str, lifetime_seconds: float) -> None:
        if not secret_key:
            log.warning("MD_SECRET_KEY is not set: sessions are signed with a random key and end when the "
                        "server restarts.")
            secret_key = secrets.token_hex(32)
        self._key = hmac.new(secret_key.encode("utf-8"), b"md-session\0" + password.encode("utf-8"),
                             hashlib.sha256).digest()
        self._lifetime = lifetime_seconds
        self._revoked: dict[str, float] = {}  # session id -> expiry
        self._lock = threading.Lock()

    @property
    def lifetime_seconds(self) -> float:
        return self._lifetime

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        payload = f"{secrets.token_urlsafe(18)}.{int(time.time() + self._lifetime)}"
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str | None) -> str | None:
        """Return the session id of a valid token, else ``None``."""
        if not token or len(token) > 200:
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        session_id, expiry, signature = parts
        try:
            expires_at = int(expiry)
            expected = self._sign(f"{session_id}.{expiry}")
        except (ValueError, UnicodeEncodeError):
            return None
        if not hmac.compare_digest(expected, signature) or expires_at < time.time():
            return None
        with self._lock:
            if session_id in self._revoked:
                return None
        return session_id

    def revoke(self, token: str | None) -> None:
        session_id = self.verify(token)
        if session_id is None:
            return
        now = time.time()
        with self._lock:
            self._revoked = {sid: exp for sid, exp in self._revoked.items() if exp > now}
            self._revoked[session_id] = int(token.split(".")[1])  # type: ignore[union-attr]


class RateLimiter:
    """Sliding-window counters per key, e.g. ``"login:203.0.113.7"``."""

    MAX_KEYS = 10_000

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _window(self, key: str, window: float, now: float) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            if len(self._events) >= self.MAX_KEYS:
                self._prune(now)
            events = self._events[key] = deque()
        while events and events[0] <= now - window:
            events.popleft()
        return events

    def _prune(self, now: float, max_window: float = 24 * 3600) -> None:
        for key in [k for k, v in self._events.items() if not v or v[-1] <= now - max_window]:
            del self._events[key]

    def retry_after(self, key: str, limit: int, window: float) -> float:
        """Seconds until ``key`` may act again (0 = allowed now). Doesn't count an event."""
        now = time.monotonic()
        with self._lock:
            events = self._window(key, window, now)
            if len(events) < limit:
                return 0.0
            return max(1.0, events[0] + window - now)

    def record(self, key: str, window: float) -> None:
        now = time.monotonic()
        with self._lock:
            self._window(key, window, now).append(now)

    def hit(self, key: str, limit: int, window: float) -> float:
        """Count an event if allowed. Returns 0 when allowed, else seconds to wait."""
        now = time.monotonic()
        with self._lock:
            events = self._window(key, window, now)
            if len(events) >= limit:
                return max(1.0, events[0] + window - now)
            events.append(now)
            return 0.0

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
