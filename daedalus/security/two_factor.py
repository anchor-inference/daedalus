"""Two-factor sign-in: a time-based one-time code (RFC 6238) asked for after the first way in.

The first factor is what already signs a browser in — Telegram's Login Widget or a pairing link. With
two-factor on, that step no longer hands out the session cookie: it hands out a short-lived pending
one, and the six digits from the operator's authenticator app (or one of the recovery codes) turn it
into a session. The parameters are the ones every authenticator app supports without being told:
SHA-1, six digits, thirty seconds, so the enrolment URI carries them only to be explicit.

The secret is sealed with AES-GCM under the key the operator's secrets use, kept in the
installation's secrets directory where the agent's own tools can neither read nor write; the
database alone holds no usable secret. Recovery codes are stored as SHA-256 digests: each carries 60
random bits, which no table of digests can be searched for.

A code is good once. The step it was accepted for is remembered, and a code for that step or an
earlier one is refused, so a code read over the operator's shoulder (or replayed from a proxy log)
is spent by the time anyone else types it. One step of clock skew either way is accepted, as the RFC
recommends for a code the operator types by hand.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from daedalus.security.operator_secrets import load_key

ISSUER = "Daedalus"
DIGITS = 6
PERIOD = 30
SKEW = 1
"""Steps either side of now that still count: a phone a few seconds off, or a code typed as it rolled over."""
SECRET_BYTES = 20
"""160 bits, the HMAC-SHA1 block the RFC's reference secret uses; 32 base32 characters to type by hand."""
RECOVERY_COUNT = 10
RECOVERY_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
"""Thirty-two symbols with no 0/o or 1/l to misread from a sheet of paper."""
RECOVERY_GROUPS = 3
RECOVERY_GROUP_LENGTH = 4
SEAL_CONTEXT = b"daedalus two-factor secret"
"""Associated data of the sealed secret, so a value sealed for an operator secret does not open here."""


class TwoFactorError(ValueError):
    """A request refused in words the operator can act on."""


def new_secret() -> str:
    """A fresh shared secret, base32 without padding: what the QR code carries and the app shows."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _key_bytes(secret: str) -> bytes:
    cleaned = secret.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def code_at(secret: str, step: int) -> str:
    """The code for one thirty-second step: HOTP (RFC 4226) over the step counter."""
    digest = hmac.new(_key_bytes(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def step_of(when: float) -> int:
    return int(when // PERIOD)


def matching_step(secret: str, code: str, *, now: float, after: int = 0) -> int | None:
    """The step a code belongs to, within the skew and later than ``after``; None when it matches none."""
    current = step_of(now)
    for step in range(current - SKEW, current + SKEW + 1):
        if step > after and hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def enrolment_uri(secret: str, label: str) -> str:
    """The ``otpauth://`` URI an authenticator app reads from the QR code."""
    account = quote(f"{ISSUER}:{label}", safe=":@")
    query = urlencode({"secret": secret, "issuer": ISSUER, "algorithm": "SHA1", "digits": DIGITS, "period": PERIOD})
    return f"otpauth://totp/{account}?{query}"


def qr_rows(text: str) -> list[str]:
    """The QR code of ``text`` as rows of ``0`` and ``1``, with no quiet zone: the app draws the squares.

    Drawn here rather than by a script the page would fetch, and rather than sent to any service:
    the URI carries the secret, so it must not leave the host for anything but the operator's screen."""
    import segno  # Lazy: only an enrolment draws a code

    symbol = segno.make(text, error="m", micro=False)
    return ["".join("1" if cell else "0" for cell in row) for row in symbol.matrix]


def new_recovery_codes() -> list[str]:
    def group() -> str:
        return "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_GROUP_LENGTH))

    return ["-".join(group() for _ in range(RECOVERY_GROUPS)) for _ in range(RECOVERY_COUNT)]


def normalise_recovery(code: str) -> str:
    """A recovery code as typed — any case, with or without its dashes or spaces — in its stored spelling."""
    return "".join(ch for ch in code.lower() if ch.isalnum())


def recovery_digest(code: str) -> str:
    return hashlib.sha256(normalise_recovery(code).encode()).hexdigest()


def is_totp_code(code: str) -> bool:
    return len(code) == DIGITS and code.isdigit()


class Attempts:
    """Failed codes across every place one is checked, for the whole installation.

    There is one operator, so there is no account to key this by; whoever holds a first factor and
    guesses at the second is stopped here whichever route they guess through. Five wrong codes in five
    minutes close the door until the oldest of them ages out: with three codes valid at any moment,
    that leaves a guesser months of trying for one hit, and the operator a short wait after typos."""

    def __init__(self, *, limit: int = 5, window: float = 300.0, clock: Any = time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self._failures: deque[float] = deque()

    def _prune(self) -> None:
        horizon = self.clock() - self.window
        while self._failures and self._failures[0] <= horizon:
            self._failures.popleft()

    def wait(self) -> int:
        """Seconds until another code may be tried; zero when one may be tried now."""
        self._prune()
        if len(self._failures) < self.limit:
            return 0
        return max(1, int(self._failures[0] + self.window - self.clock()) + 1)

    def failed(self) -> None:
        self._failures.append(self.clock())

    def clear(self) -> None:
        self._failures.clear()


@dataclass(slots=True)
class Status:
    enabled: bool
    enrolling: bool
    """A secret was handed out for scanning and no code has confirmed it yet."""
    recovery_left: int
    enabled_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "enrolling": self.enrolling, "recovery_left": self.recovery_left, "enabled_at": self.enabled_at}


class TwoFactor:
    """The installation's one TOTP secret, its recovery codes, and the check of a code against them."""

    def __init__(self, db: Any, key_path: Path, *, clock: Any = time.time) -> None:
        self.db = db
        self.key_path = key_path
        self.clock = clock
        self._aead: AESGCM | None = None

    def _cipher(self) -> AESGCM:
        if self._aead is None:
            self._aead = AESGCM(load_key(self.key_path))
        return self._aead

    def _seal(self, secret: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._cipher().encrypt(nonce, secret.encode("ascii"), SEAL_CONTEXT)

    def _open(self, sealed: bytes) -> str:
        try:
            return self._cipher().decrypt(sealed[:12], sealed[12:], SEAL_CONTEXT).decode("ascii")
        except Exception as exc:  # noqa: BLE001 — a key file replaced since enrolment is the one way this fails
            raise TwoFactorError("the two-factor secret cannot be opened with this installation's key; turn two-factor off with a recovery code") from exc

    async def _row(self) -> Any:
        return await self.db.fetchone("SELECT sealed, enabled_at, last_step FROM two_factor WHERE id = 1")

    async def enabled(self) -> bool:
        row = await self._row()
        return row is not None and row["enabled_at"] is not None

    async def status(self) -> Status:
        row = await self._row()
        left = await self.db.fetchone("SELECT count(*) c FROM two_factor_recovery WHERE used_at IS NULL")
        on = row is not None and row["enabled_at"] is not None
        return Status(enabled=on, enrolling=row is not None and not on, recovery_left=int(left["c"]) if on and left else 0,
                      enabled_at=row["enabled_at"] if on else None)

    async def begin(self) -> str:
        """A fresh secret to scan, replacing any enrolment nobody finished. Refused while two-factor is on:
        a second secret would have to be confirmed against the first, which is what turning it off and on again is."""
        if await self.enabled():
            raise TwoFactorError("two-factor sign-in is already on; turn it off first to enrol another authenticator")
        secret = new_secret()
        await self.db.execute(
            "INSERT INTO two_factor(id, sealed, enabled_at, last_step, created_at) VALUES (1, ?, NULL, 0, ?)"
            " ON CONFLICT(id) DO UPDATE SET sealed = excluded.sealed, enabled_at = NULL, last_step = 0, created_at = excluded.created_at",
            (self._seal(secret), datetime.now(UTC).isoformat()),
        )
        return secret

    async def confirm(self, code: str) -> list[str] | None:
        """Switch two-factor on with a code from the secret just scanned: the recovery codes, once, or None for a wrong code."""
        row = await self._row()
        if row is None or row["enabled_at"] is not None:
            raise TwoFactorError("there is no enrolment waiting for a code; start again")
        step = matching_step(self._open(bytes(row["sealed"])), code.strip(), now=self.clock())
        if step is None:
            return None
        codes = new_recovery_codes()
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE two_factor SET enabled_at = ?, last_step = ? WHERE id = 1", (datetime.now(UTC).isoformat(), step))
            await conn.execute("DELETE FROM two_factor_recovery")
            await conn.executemany("INSERT INTO two_factor_recovery(code_hash) VALUES (?)", [(recovery_digest(c),) for c in codes])
        return codes

    async def check(self, code: str) -> str | None:
        """Spend a code: ``"totp"`` or ``"recovery"`` for the kind that matched, None for a wrong one.

        Six digits are tried as a time-based code and anything else as a recovery code, so a recovery
        code can never be mistaken for a lucky guess at the six digits, nor the other way round."""
        typed = code.strip().replace(" ", "")
        # Read and spend in one transaction: two requests carrying the same code at the same moment
        # must not both find it unspent.
        async with self.db.transaction() as conn:
            row = await (await conn.execute("SELECT sealed, enabled_at, last_step FROM two_factor WHERE id = 1")).fetchone()
            if row is None or row["enabled_at"] is None:
                raise TwoFactorError("two-factor sign-in is off")
            if is_totp_code(typed):
                step = matching_step(self._open(bytes(row["sealed"])), typed, now=self.clock(), after=int(row["last_step"]))
                if step is None:
                    return None
                await conn.execute("UPDATE two_factor SET last_step = ? WHERE id = 1", (step,))
                return "totp"
            spent = await conn.execute(
                "UPDATE two_factor_recovery SET used_at = ? WHERE code_hash = ? AND used_at IS NULL",
                (datetime.now(UTC).isoformat(), recovery_digest(typed)),
            )
            return "recovery" if spent.rowcount == 1 else None

    async def regenerate(self) -> list[str]:
        """A new set of recovery codes; every earlier one stops working. The caller has checked a code first."""
        codes = new_recovery_codes()
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM two_factor_recovery")
            await conn.executemany("INSERT INTO two_factor_recovery(code_hash) VALUES (?)", [(recovery_digest(c),) for c in codes])
        return codes

    async def disable(self) -> None:
        """Forget the secret and the recovery codes. The caller has checked a code first."""
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM two_factor WHERE id = 1")
            await conn.execute("DELETE FROM two_factor_recovery")


__all__ = [
    "DIGITS", "ISSUER", "PERIOD", "SKEW", "Attempts", "Status", "TwoFactor", "TwoFactorError", "code_at", "enrolment_uri",
    "matching_step", "new_recovery_codes", "new_secret", "normalise_recovery", "qr_rows", "recovery_digest", "step_of",
]
