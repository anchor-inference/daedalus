"""The terminal client's devices: pairing by device code, one token per device, and each device's scope.

Pairing follows the device authorization grant (RFC 8628). The client starts a request and is handed a
secret device code, which it polls with, and a short user code, which it shows beside a QR code of the
approval link. The operator approves in the app or in Telegram, comparing the code with the one on
their terminal; the next poll makes the device and hands its token over, once. Only the device code's
hash is kept, and the token is made at that poll rather than at the approval, so a plain token exists
in exactly one answer and nothing stored could produce it again.

A token is kept as its SHA-256. It is looked up by that hash: the token is 32 random bytes, so knowing
how long a lookup took tells a guesser nothing about a preimage. It stops working when the device is
revoked or after :data:`DEVICE_IDLE` unused.

A device's scope is what it may drive (``cli_device_sessions``): the sessions it started (``home``: their
tools run on it) and the ones the operator opened from it with ``/resume`` (``resumed``), plus the
subagents of either. The door (:mod:`daedalus.remote.door`) asks :meth:`Devices.in_scope` before any
session route runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from daedalus.browser.user import CODE_ALPHABET, CODE_LENGTH, Attempts, normal_code
from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "dcl_"
PAIRING_TTL = timedelta(minutes=10)
POLL_INTERVAL = 3
"""Seconds between two polls of a pending request. Short, because the operator is looking at the
terminal waiting for it to go on."""
POLL_SLACK = 1.0
"""How much earlier than the interval a poll may come before it is told to slow down: a client that
polls every three seconds over a network arrives at 2.9 now and then."""
PENDING_MAX = 5
"""Requests that may wait for the operator at once. The start is a public door; past this a flood of
requests is refused instead of filling the operator's Telegram."""
DEVICE_IDLE = timedelta(days=90)
"""A device not seen for this long pairs again: its token stops."""
TOUCH_EVERY = timedelta(minutes=1)
"""A device's last use is written at most this often; every request of a client would otherwise be a
write to the database."""
KEEP_DECIDED = timedelta(days=1)
"""How long a decided or expired request is kept, for the approval card that opens it late."""
SCOPE_DEPTH = 3
"""How many ``subagent_of`` links a session may be away from a session in scope and still be in it:
a subagent of a subagent of the operator's conversation, and no further."""
GUARDS = ("ask", "auto", "plan")
"""The local guard levels a client reports (``docs/architecture/cli.md``, Local guards)."""

Guard = Literal["ask", "auto", "plan"]
Via = Literal["app", "telegram"]
Publish = Callable[[str, dict[str, Any]], Awaitable[Any]]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _instant(text: Any) -> datetime:
    try:
        at = datetime.fromisoformat(str(text))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _clip(value: Any, most: int) -> str:
    return " ".join(str(value or "").split())[:most]


def shown_code(code: str) -> str:
    """A user code as the terminal and the card show it: two halves and a dash."""
    half = len(code) // 2
    return f"{code[:half]}-{code[half:]}"


class Refused(Exception):
    """A request the store will not do, with the status and code the API answers it with."""

    status = 400
    code = "refused"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class NotFound(Refused):
    status, code = 404, "not_found"


class Conflict(Refused):
    status, code = 409, "conflict"


class TooMany(Refused):
    status, code = 429, "too_many"


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    name: str
    hostname: str
    os: str
    arch: str
    client_version: str
    paired_at: str
    token_issued_at: str
    """When the token in use was handed out: the pairing, or the last rotation."""
    approved_via: str
    last_seen_at: str
    last_address: str
    revoked_at: str | None

    @property
    def expires_at(self) -> datetime:
        return _instant(self.last_seen_at) + DEVICE_IDLE

    def live(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now

    def view(self, *, connected: bool = False, sessions: int = 0) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "hostname": self.hostname, "os": self.os, "arch": self.arch,
            "client_version": self.client_version, "paired_at": self.paired_at, "token_issued_at": self.token_issued_at,
            "approved_via": self.approved_via,
            "last_seen_at": self.last_seen_at, "last_address": self.last_address,
            "expires_at": self.expires_at.isoformat(), "revoked_at": self.revoked_at,
            "connected": connected, "sessions": sessions,
        }


@dataclass(frozen=True, slots=True)
class Pairing:
    id: str
    user_code: str
    name: str
    hostname: str
    os: str
    arch: str
    client_version: str
    address: str
    created_at: str
    expires_at: str
    stored_state: str
    """As stored: ``pending``, ``approved``, ``denied`` or ``delivered``. A request that ran out is
    still stored as it was; :meth:`state` says ``expired``."""
    polled_at: str | None
    device_id: str | None

    def state(self, now: datetime) -> str:
        if self.stored_state in ("pending", "approved") and _instant(self.expires_at) <= now:
            return "expired"
        return self.stored_state

    def view(self, now: datetime) -> dict[str, Any]:
        return {
            "id": self.id, "user_code": shown_code(self.user_code), "name": self.name, "hostname": self.hostname,
            "os": self.os, "arch": self.arch, "client_version": self.client_version, "address": self.address,
            "created_at": self.created_at, "expires_at": self.expires_at, "state": self.state(now),
        }

    def event(self, now: datetime) -> dict[str, Any]:
        """The ``device.pairing`` payload."""
        return {"id": self.id, "user_code": shown_code(self.user_code), "name": self.name, "hostname": self.hostname,
                "os": self.os, "address": self.address, "expires_at": self.expires_at, "state": self.state(now)}


@dataclass(frozen=True, slots=True)
class Binding:
    device_id: str
    session_id: str
    role: str
    """``home``: the session's tools run on this device; ``resumed``: opened from it with ``/resume``."""
    cwd: str
    created_at: str


def _device(row: Any) -> Device:
    return Device(
        id=row["id"], name=row["name"], hostname=row["hostname"], os=row["os"], arch=row["arch"],
        client_version=row["client_version"], paired_at=row["paired_at"], token_issued_at=row["token_issued_at"], approved_via=row["approved_via"],
        last_seen_at=row["last_seen_at"], last_address=row["last_address"], revoked_at=row["revoked_at"],
    )


def _pairing(row: Any) -> Pairing:
    return Pairing(
        id=row["id"], user_code=row["user_code"], name=row["name"], hostname=row["hostname"], os=row["os"],
        arch=row["arch"], client_version=row["client_version"], address=row["address"], created_at=row["created_at"],
        expires_at=row["expires_at"], stored_state=row["state"], polled_at=row["polled_at"], device_id=row["device_id"],
    )


def _binding(row: Any) -> Binding:
    return Binding(device_id=row["device_id"], session_id=row["session_id"], role=row["role"], cwd=row["cwd"], created_at=row["created_at"])


DEVICE_COLUMNS = "id, name, hostname, os, arch, client_version, paired_at, token_issued_at, approved_via, last_seen_at, last_address, revoked_at"
PAIRING_COLUMNS = "id, user_code, name, hostname, os, arch, client_version, address, created_at, expires_at, state, polled_at, device_id"


class Devices:
    """The paired terminals, their pairing requests and their scopes, in ``cli_*`` tables."""

    def __init__(self, db: Database, *, now: Callable[[], datetime] = lambda: datetime.now(UTC), publish: Publish | None = None) -> None:
        self.db = db
        self.now = now
        self.publish = publish
        self.attempts = Attempts()
        self.revoked_hooks: list[Callable[[str], Awaitable[None]]] = []
        """Called with a device's id once it is revoked: the relay closes its link, the live marks
        forget what it showed. A hook that fails is logged; the revocation stands."""
        self._lock = asyncio.Lock()

    async def _announce(self, name: str, payload: dict[str, Any]) -> None:
        if self.publish is None:
            return
        try:
            await self.publish(name, payload)
        except Exception:  # noqa: BLE001 — the change is made; a front that missed it reads the list again
            logger.warning("could not announce %s", name, exc_info=True)

    # -- pairing -----------------------------------------------------------------------------------

    async def start_pairing(self, *, name: str = "", hostname: str = "", os: str = "", arch: str = "", client_version: str = "", address: str = "") -> tuple[Pairing, str]:
        """A new request and the device code the client polls with, returned once."""
        if self.attempts.blocked(address):
            raise TooMany("too many failed attempts from here; wait a few minutes")
        now = self.now()
        hostname, os, arch, client_version = _clip(hostname, 64), _clip(os, 64), _clip(arch, 64), _clip(client_version, 64)
        name = _clip(name, 80) or hostname or "terminal"
        device_code = secrets.token_urlsafe(32)
        async with self._lock:
            await self.db.execute("DELETE FROM cli_pairings WHERE expires_at < ?", ((now - KEEP_DECIDED).isoformat(),))
            pending = await self.db.fetchall("SELECT user_code FROM cli_pairings WHERE state = 'pending' AND expires_at > ?", (now.isoformat(),))
            if len(pending) >= PENDING_MAX:
                raise TooMany(f"{PENDING_MAX} terminals are already waiting for the operator; approve or deny them first")
            taken = {str(row["user_code"]) for row in pending}
            user_code = ""
            while not user_code or user_code in taken:
                user_code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            pairing_id = uuid.uuid4().hex[:12]
            await self.db.execute(
                "INSERT INTO cli_pairings(id, device_code_hash, user_code, name, hostname, os, arch, client_version, address, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pairing_id, _digest(device_code), user_code, name, hostname, os, arch, client_version, _clip(address, 64), now.isoformat(), (now + PAIRING_TTL).isoformat()),
            )
        found = await self._pairing_by("id", pairing_id)
        assert found is not None
        await self._announce("device.pairing", found.event(now))
        return found, device_code

    async def _pairing_by(self, column: Literal["id", "user_code"], value: str) -> Pairing | None:
        row = await self.db.fetchone(f"SELECT {PAIRING_COLUMNS} FROM cli_pairings WHERE {column} = ? ORDER BY created_at DESC LIMIT 1", (value,))  # noqa: S608 — the column is one of two literals
        return _pairing(row) if row is not None else None

    async def pairing(self, user_code: str) -> Pairing | None:
        """The newest request shown with this code, as typed or scanned; None for one that ran out."""
        found = await self._pairing_by("user_code", normal_code(user_code))
        return found if found is not None and found.state(self.now()) != "expired" else None

    async def pairing_by_id(self, pairing_id: str) -> Pairing | None:
        return await self._pairing_by("id", pairing_id)

    async def pending(self) -> list[Pairing]:
        rows = await self.db.fetchall(
            f"SELECT {PAIRING_COLUMNS} FROM cli_pairings WHERE state = 'pending' AND expires_at > ? ORDER BY created_at DESC",  # noqa: S608 — a constant
            (self.now().isoformat(),),
        )
        return [_pairing(row) for row in rows]

    async def decide(self, found: Pairing | None, *, approve: bool, via: Via, name: str = "") -> Pairing:
        """The operator's answer to a request. The device is made at the client's next poll."""
        now = self.now()
        if found is None or found.state(now) == "expired":
            raise NotFound("no such pairing request, or it ran out; start `daedalus-cli login` again")
        if found.stored_state != "pending":
            raise Conflict(f"this request was already {found.stored_state}")
        renamed = _clip(name, 80) or found.name
        async with self._lock:
            cursor_rows = await self.db.fetchall(
                "UPDATE cli_pairings SET state = ?, decided_via = ?, name = ? WHERE id = ? AND state = 'pending' RETURNING id",
                ("approved" if approve else "denied", via, renamed, found.id),
            )
        if not cursor_rows:
            raise Conflict("this request was decided meanwhile")
        decided = await self._pairing_by("id", found.id)
        assert decided is not None
        await self._announce("device.pairing", decided.event(now))
        return decided

    async def poll(self, device_code: str, *, address: str = "") -> dict[str, Any]:
        """The client's poll, answered as the contract's table says; the token in the one answer that
        finds the request approved."""
        if self.attempts.blocked(address):
            raise TooMany("too many failed attempts from here; wait a few minutes")
        row = await self.db.fetchone(f"SELECT {PAIRING_COLUMNS} FROM cli_pairings WHERE device_code_hash = ?", (_digest(device_code or ""),))  # noqa: S608 — a constant
        if row is None:
            self.attempts.failed(address)
            raise NotFound("no such pairing request", code="unknown")
        found = _pairing(row)
        now = self.now()
        state = found.state(now)
        if state in ("expired", "delivered"):
            return {"status": "expired"}
        if state == "denied":
            return {"status": "denied"}
        early = found.polled_at is not None and (now - _instant(found.polled_at)).total_seconds() < POLL_INTERVAL - POLL_SLACK
        await self.db.execute("UPDATE cli_pairings SET polled_at = ? WHERE id = ?", (now.isoformat(), found.id))
        if early:
            return {"status": "slow_down", "interval": POLL_INTERVAL * 2}
        if state == "pending":
            return {"status": "pending", "interval": POLL_INTERVAL}
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        device_id = uuid.uuid4().hex[:12]
        stamp = now.isoformat()
        async with self.db.transaction() as conn:
            # Claimed before the device is made: two polls racing each other see one row change, and
            # only that one hands out a token.
            claimed = await (await conn.execute("UPDATE cli_pairings SET state = 'delivered' WHERE id = ? AND state = 'approved' RETURNING decided_via", (found.id,))).fetchall()
            if not claimed:
                return {"status": "expired"}
            await conn.execute(
                "INSERT INTO cli_devices(id, name, hostname, os, arch, client_version, token_hash, token_issued_at, paired_at, approved_via, last_seen_at, last_address)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (device_id, found.name, found.hostname, found.os, found.arch, found.client_version, _digest(token), stamp, stamp,
                 claimed[0][0] or "app", stamp, _clip(address, 64) or found.address),
            )
            await conn.execute("UPDATE cli_pairings SET device_id = ? WHERE id = ?", (device_id, found.id))
        device = await self.get(device_id)
        assert device is not None
        await self._announce("device.pairing", {**found.event(now), "state": "delivered"})
        await self._announce("device.changed", {"device_id": device_id, "change": "paired"})
        return {"status": "approved", "token": token, "device": device.view()}

    # -- devices and tokens ------------------------------------------------------------------------

    async def check(self, token: str, *, address: str = "") -> Device | None:
        """The live device a token belongs to, or None. A use moves its last-seen along."""
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        row = await self.db.fetchone(f"SELECT {DEVICE_COLUMNS} FROM cli_devices WHERE token_hash = ? AND revoked_at IS NULL", (_digest(token),))  # noqa: S608 — a constant
        if row is None:
            return None
        device = _device(row)
        now = self.now()
        if not device.live(now):
            return None
        address = _clip(address, 64)
        if now - _instant(device.last_seen_at) >= TOUCH_EVERY or (address and address != device.last_address):
            await self.db.execute("UPDATE cli_devices SET last_seen_at = ?, last_address = COALESCE(NULLIF(?, ''), last_address) WHERE id = ?", (now.isoformat(), address, device.id))
        return device

    async def get(self, device_id: str) -> Device | None:
        row = await self.db.fetchone(f"SELECT {DEVICE_COLUMNS} FROM cli_devices WHERE id = ?", (device_id,))  # noqa: S608 — a constant
        return _device(row) if row is not None else None

    async def paired(self, *, everything: bool = False) -> list[Device]:
        """The paired devices, newest first; with ``everything`` the revoked and lapsed ones too."""
        rows = await self.db.fetchall(f"SELECT {DEVICE_COLUMNS} FROM cli_devices ORDER BY paired_at DESC")  # noqa: S608 — a constant
        now = self.now()
        found = [_device(row) for row in rows]
        return found if everything else [device for device in found if device.live(now)]

    async def rename(self, device_id: str, name: str) -> Device:
        name = _clip(name, 80)
        if not name:
            raise Refused("a device needs a name")
        device = await self.get(device_id)
        if device is None or device.revoked_at is not None:
            raise NotFound("no such device")
        await self.db.execute("UPDATE cli_devices SET name = ? WHERE id = ?", (name, device_id))
        await self._announce("device.changed", {"device_id": device_id, "change": "renamed"})
        renamed = await self.get(device_id)
        assert renamed is not None
        return renamed

    async def revoke(self, device_id: str) -> bool:
        """Unpair a device: its token stops at once, the conversations it resumed leave its scope, and
        the hooks close what it holds open. The row stays, so the conversations it started still name
        it. Whether there was a live device to revoke."""
        now = self.now().isoformat()
        async with self.db.transaction() as conn:
            changed = await (await conn.execute("UPDATE cli_devices SET token_hash = NULL, revoked_at = ? WHERE id = ? AND revoked_at IS NULL RETURNING id", (now, device_id))).fetchall()
            if changed:
                await conn.execute("DELETE FROM cli_device_sessions WHERE device_id = ? AND role = 'resumed'", (device_id,))
        if not changed:
            return False
        for hook in list(self.revoked_hooks):
            try:
                await hook(device_id)
            except Exception:  # noqa: BLE001 — the token is already dead; what a hook missed lapses on its own
                logger.warning("a hook failed on revoking device %s", device_id, exc_info=True)
        await self._announce("device.changed", {"device_id": device_id, "change": "revoked"})
        return True

    async def rotate(self, device_id: str) -> str:
        """A new token for a live device; the old one stops in the same write."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        rows = await self.db.fetchall(
            "UPDATE cli_devices SET token_hash = ?, token_issued_at = ? WHERE id = ? AND revoked_at IS NULL RETURNING id",
            (_digest(token), self.now().isoformat(), device_id),
        )
        if not rows:
            raise NotFound("no such device")
        return token

    # -- scope ------------------------------------------------------------------------------------

    async def bind_home(self, device_id: str, session_id: str, cwd: str) -> None:
        """A session started from this device: its tools run on it, in ``cwd``."""
        await self.db.execute(
            "INSERT INTO cli_device_sessions(device_id, session_id, role, cwd, created_at) VALUES (?, ?, 'home', ?, ?)",
            (device_id, session_id, cwd, self.now().isoformat()),
        )

    async def attach(self, device_id: str, session_id: str) -> None:
        """The operator opened a session from this device (``/resume``). A session already in its scope
        stays as it is."""
        await self.db.execute(
            "INSERT INTO cli_device_sessions(device_id, session_id, role, cwd, created_at) VALUES (?, ?, 'resumed', '', ?) ON CONFLICT DO NOTHING",
            (device_id, session_id, self.now().isoformat()),
        )

    async def detach(self, device_id: str, session_id: str) -> bool:
        """Drop a resumed session from the scope. A home session cannot be: its tools run here."""
        binding = await self.binding(device_id, session_id)
        if binding is None:
            return False
        if binding.role == "home":
            raise Conflict("this conversation was started here and its tools run here; it cannot be detached")
        await self.db.execute("DELETE FROM cli_device_sessions WHERE device_id = ? AND session_id = ?", (device_id, session_id))
        return True

    async def move_home(self, device_id: str, session_id: str, cwd: str) -> None:
        """Make this device and folder a session's home. The device that was its home keeps it in its
        scope as a resumed one, so the terminal there can still show it."""
        stamp = self.now().isoformat()
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE cli_device_sessions SET role = 'resumed', cwd = '' WHERE session_id = ? AND role = 'home' AND device_id != ?", (session_id, device_id))
            await conn.execute(
                "INSERT INTO cli_device_sessions(device_id, session_id, role, cwd, created_at) VALUES (?, ?, 'home', ?, ?)"
                " ON CONFLICT(device_id, session_id) DO UPDATE SET role = 'home', cwd = excluded.cwd",
                (device_id, session_id, cwd, stamp),
            )

    async def binding(self, device_id: str, session_id: str) -> Binding | None:
        row = await self.db.fetchone("SELECT * FROM cli_device_sessions WHERE device_id = ? AND session_id = ?", (device_id, session_id))
        return _binding(row) if row is not None else None

    async def home(self, session_id: str) -> Binding | None:
        row = await self.db.fetchone("SELECT * FROM cli_device_sessions WHERE session_id = ? AND role = 'home'", (session_id,))
        return _binding(row) if row is not None else None

    async def homes(self, session_ids: Iterable[str] | None = None) -> dict[str, Binding]:
        """The home of each of these sessions that has one, or of every session: one read per listing."""
        if session_ids is None:
            rows = await self.db.fetchall("SELECT * FROM cli_device_sessions WHERE role = 'home'")
        else:
            ids = list(session_ids)
            rows = []
            for start in range(0, len(ids), 500):
                part = ids[start:start + 500]
                if part:
                    rows += await self.db.fetchall(f"SELECT * FROM cli_device_sessions WHERE role = 'home' AND session_id IN ({','.join('?' for _ in part)})", part)  # noqa: S608 — placeholders only
        return {str(row["session_id"]): _binding(row) for row in rows}

    async def scope(self, device_id: str) -> list[Binding]:
        rows = await self.db.fetchall("SELECT * FROM cli_device_sessions WHERE device_id = ? ORDER BY created_at", (device_id,))
        return [_binding(row) for row in rows]

    async def session_counts(self) -> dict[str, int]:
        rows = await self.db.fetchall("SELECT device_id, count(*) AS n FROM cli_device_sessions GROUP BY device_id")
        return {str(row["device_id"]): int(row["n"]) for row in rows}

    async def in_scope(self, device_id: str, session_id: str) -> bool:
        """Whether the device may drive this session: bound to it, or a subagent (to
        :data:`SCOPE_DEPTH` links) of one that is."""
        current = session_id
        for _ in range(SCOPE_DEPTH + 1):
            if await self.db.fetchone("SELECT 1 FROM cli_device_sessions WHERE device_id = ? AND session_id = ?", (device_id, current)):
                return True
            row = await self.db.fetchone("SELECT metadata FROM sessions WHERE id = ?", (current,))
            if row is None:
                return False
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (TypeError, ValueError):
                return False
            parent = metadata.get("subagent_of") if isinstance(metadata, dict) else None
            if not parent or not isinstance(parent, str):
                return False
            current = parent
        return False


__all__ = [
    "DEVICE_IDLE", "GUARDS", "PAIRING_TTL", "PENDING_MAX", "POLL_INTERVAL", "TOKEN_PREFIX",
    "Binding", "Conflict", "Device", "Devices", "Guard", "NotFound", "Pairing", "Refused", "TooMany", "shown_code",
]
