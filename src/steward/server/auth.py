"""Steward pairing and session auth: a 6-digit code, stateless HMAC tokens.

The daemon exposes only /health and /v1/bundle without a token. Everything
else is gated behind Authorization: Bearer with a session token won by
pairing. The flow, on the same LAN as the daemon:

    1. The daemon prints a fresh 6-digit pairing code and the pairing QR
       payload on startup. The code is also served at GET /pair/code, but
       only to loopback clients, so it cannot be scraped from the LAN.
    2. A client sends POST /v1/pair {"code": "...", "device": "Name"} and
       gets a signed session token back.
    3. The token is sent as Authorization: Bearer on every gated route.

Tokens are stateless HMAC-SHA256: ``sv1.<exp>.<device>.<nonce>.<sig>``. The
signing key is 32 random bytes persisted once under <data>/db/pairing.key
(owner-only), so tokens survive daemon restarts and re-pairing is never
required after a reboot. The device label travels inside the token for
read-only identification; nothing secret is embedded.

Pairing is rate-limited per client IP: 5 failed attempts per 5 minutes, after
which the code is refused until the oldest failing attempt ages out of the
window. The pattern follows /home/user/internal-service/api/auth.py (proven on the
InternalDash appliance) but is reimplemented for this stdlib-only server: no
password hashing (the code is short-lived and single-use by design), no
cookie transport, tokens instead of a session store.

No third-party imports. Security notes:
    * Code comparison uses a constant-time compare.
    * Timing-safe signature check via hmac.compare_digest.
    * The key file is created 0o600 in the data dir, never world-readable.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import time
from pathlib import Path

TOKEN_PREFIX = "sv1"
SESSION_TTL_SEC = 30 * 24 * 3600  # 30 days
PAIR_LEN = 6
RATE_LIMIT = 5
RATE_WINDOW_SEC = 5 * 60  # 5 per 5 minutes per IP
DEVICE_MAXLEN = 64


# -- base64url helpers --------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


# -- pairing code --------------------------------------------------------------


def generate_pair_code() -> str:
    """A fresh 6-digit pairing code, digits only, CSPRNG-derived."""
    return f"{secrets.randbelow(10 ** PAIR_LEN):0{PAIR_LEN}d}"


def code_matches(given: str, expected: str) -> bool:
    """Constant-time compare; a wrong code is just wrong, never observable."""
    return hmac.compare_digest(given.strip(), expected)


# -- signing key ---------------------------------------------------------------


def load_or_create_secret(path: Path) -> bytes:
    """Return the persisted 32-byte signing key, creating it once 0o600.

    Creating on first run means every daemon restart signs with the same key:
    tokens issued before a restart stay valid after it.
    """
    if path.exists():
        try:
            raw = path.read_bytes()
            if len(raw) >= 16:
                return raw
        except OSError:
            pass  # unreadable key: regenerate below (still safe, invalidates tokens)
    raw = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    return raw


# -- tokens --------------------------------------------------------------------


def issue_token(key: bytes, device: str, now: float | None = None) -> tuple[str, int]:
    """Return (token, exp_epoch) signed with key for the given device label."""
    exp = int((now if now is not None else time.time()) + SESSION_TTL_SEC)
    device = device.strip()[:DEVICE_MAXLEN] or "device"
    nonce = _b64e(secrets.token_bytes(9))
    body = f"{TOKEN_PREFIX}.{exp}.{_b64e(device.encode('utf-8'))}.{nonce}"
    sig = hmac.new(key, body.encode("utf-8"), "sha256").digest()
    return f"{body}.{_b64e(sig)}", exp


def split_token(token: str) -> list[str]:
    return token.split(".")


def check_token(token: str | None, key: bytes, now: float | None = None) -> bool:
    """Verify signature and expiry. Constant-time on the signature."""
    if not token or not key:
        return False
    parts = split_token(token)
    if len(parts) != 5 or parts[0] != TOKEN_PREFIX:
        return False
    try:
        exp = int(parts[1])
    except ValueError:
        return False
    if exp <= int(now if now is not None else time.time()):
        return False
    body = ".".join(parts[:4])
    try:
        given = _b64d(parts[4])
    except Exception:
        return False
    want = hmac.new(key, body.encode("utf-8"), "sha256").digest()
    return hmac.compare_digest(given, want)


def device_label(token: str) -> str:
    """Best-effort device label from an already-validated token, else empty."""
    parts = split_token(token)
    if len(parts) != 5 or parts[0] != TOKEN_PREFIX:
        return ""
    try:
        return _b64d(parts[2]).decode("utf-8")
    except Exception:
        return ""


# -- rate limiting (in-process, single-node daemon) ----------------------------


class PairRateLimiter:
    """Per-IP failure window: refuse when 5 failed attempts sit inside 5 min.

    Stores only timestamps of failed attempts, pruned on access, so the table
    cannot grow without bound on a quiet daemon.
    """

    limit = RATE_LIMIT
    window = RATE_WINDOW_SEC

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}  # client ip -> failure timestamps

    def _prune(self, ip: str, now: float) -> list[float]:
        fails = self._fails.setdefault(ip, [])
        while fails and now - fails[0] > self.window:
            fails.pop(0)
        return fails

    def blocked(self, ip: str, now: float | None = None) -> int:
        """Seconds still blocked, or 0 when this IP may try again."""
        now = now if now is not None else time.time()
        fails = self._prune(ip, now)
        if len(fails) < self.limit:
            return 0
        oldest_in_window = fails[0]
        return int(oldest_in_window + self.window - now)

    def note_failure(self, ip: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self._fails.setdefault(ip, []).append(now)
        self._prune(ip, now)

    def clear(self, ip: str) -> None:
        self._fails.pop(ip, None)


def bearer_token(authorization: str | None) -> str | None:
    """Extract the Bearer token from an Authorization header, or None."""
    if not authorization:
        return None
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = rest.strip()
    return token or None
