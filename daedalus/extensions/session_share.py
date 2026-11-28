"""Live links to one session: whoever holds the link drives that session in the app.

This is not the read-only page of :mod:`daedalus.extensions.dialog_share`. A visitor here gets the
session's own screen — the conversation, the composer, the panel beside it, its terminals and its
browser — and what they send runs with the operator's tools and the operator's powers, with no review
in between: the operator asked for exactly that, and the share dialog says so in as many words. What
the link does not reach is everything around the session: another conversation, the settings, the
secrets, the models' configuration, a new session. The door that enforces it is
:mod:`daedalus.extensions.api_session_share`; this module keeps the links and their live connections.

A link is a long random token and nothing else. It is served on the services door's origin, never on
the app's, so a visitor's browser holds nothing of the operator's and the operator's cookie never
meets a page a visitor opened. Revoking a link ends every request it has in flight at once, the
streams and the terminals' sockets among them: a revoked visitor must not keep watching the agent
through a stream opened a minute before.

A link may also carry a password, a name the operator gave it and a policy of what its visitor may pick
and spend (:mod:`daedalus.extensions.share_policy`). The password is kept as an scrypt hash; a visitor
who typed it gets a pass, an HMAC of the link's id keyed by that hash. The pass is therefore nothing the
token tells, opens no other link, and stops opening anything the moment the password changes (a new
hash, with a new salt even for the same words) or the link ends. Guesses are limited per link, not per
address: the link is one secret, and only a limit all its guessers share stops a crowd of them.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from daedalus import public_address
from daedalus.extensions import share_policy

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

EXTENSION_KEY = "session_shares"
TOKEN_BYTES = 32
"""43 characters of base64url: not guessable, and short enough to read off a phone's screen."""
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{40,64}")
VISITOR_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
NAME_MAX = 60
EXPIRY_HOURS_MAX = 24 * 90
TOUCH_SECONDS = 20.0
"""How often one visitor's activity is written: every request would be a write per keystroke of a
streamed answer's reads, and the header only says "a minute ago"."""
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
LABEL_MAX = 80
PASSWORD_MAX = 256
SCRYPT = {"n": 1 << 14, "r": 8, "p": 1}
"""About 16 MiB and a few tens of milliseconds a try: slow enough that a stolen row is not a dictionary
attack's afternoon, cheap enough for one host to answer its own visitors."""
FREE_TRIES = 5
DELAY_MAX = 900.0
"""After :data:`FREE_TRIES` wrong passwords each further one waits twice as long as the last, up to a
quarter of an hour: a visitor who mistyped barely notices, and a guesser gets a few hundred tries a day."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, maxmem=64 << 20, **SCRYPT)
    return "scrypt${n}${r}${p}$".format(**SCRYPT) + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def check_password(stored: str, password: str) -> bool:
    """Whether ``password`` is the one ``stored`` was made from, compared in constant time."""
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        found = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=32, n=int(n), r=int(r), p=int(p), maxmem=64 << 20)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(found, base64.b64decode(digest))


def pass_for(row: dict[str, Any]) -> str:
    """The pass a visitor of this link holds once they typed its password; "" for a link with none."""
    stored = str(row.get("password_hash") or "")
    if not stored:
        return ""
    mac = hmac.new(stored.encode(), b"live-link-pass:" + str(row["id"]).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def pass_valid(row: dict[str, Any], presented: str) -> bool:
    expected = pass_for(row)
    return bool(expected) and hmac.compare_digest(expected.encode(), (presented or "").encode())


def clean_label(raw: str) -> str:
    return " ".join("".join(ch for ch in raw if ch.isprintable()).split())[:LABEL_MAX]


def check_new_password(password: str) -> str:
    if not password or len(password) > PASSWORD_MAX:
        raise ValueError(f"password: from 1 to {PASSWORD_MAX} characters")
    return password


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def live(row: dict[str, Any], at: datetime | None = None) -> bool:
    """Whether a link still opens anything: not revoked, and not past its expiry."""
    if row.get("revoked_at"):
        return False
    expires = _parse(row.get("expires_at"))
    return expires is None or expires > (at or datetime.now(UTC))


def seconds_left(row: dict[str, Any]) -> float | None:
    """Until the link expires; None for one that does not."""
    expires = _parse(row.get("expires_at"))
    return None if expires is None else max(0.0, (expires - datetime.now(UTC)).total_seconds())


def clean_name(raw: str) -> str:
    """A visitor's name as they typed it, made fit for a label: one line, bounded, no controls."""
    return " ".join("".join(ch for ch in raw if ch.isprintable()).split())[:NAME_MAX]


def reach(base: str) -> str:
    """Who a link on ``base`` can be opened by: ``outside`` (anyone on the internet), ``network`` (a
    machine on the same private network), ``machine`` (this computer alone) or ``none`` (no door).

    Read from the address alone. A tunnel's address is public by construction and an operator's own name
    for the door is trusted to be one; a loopback or private address is not reachable from outside
    whatever else is true, which is the thing the share dialog must not get wrong."""
    if not base:
        return "none"
    host = (urlsplit(base).hostname or "").lower()
    if host in LOOPBACK_HOSTS:
        return "machine"
    try:
        parsed = ip_address(host)
    except ValueError:
        # A name: a tunnel's, or the operator's own for the door. A single label (no dot) is a machine
        # on a home network, not a name the internet resolves.
        return "outside" if "." in host else "network"
    if parsed.is_loopback:
        return "machine"
    return "outside" if parsed.is_global else "network"


class SessionShares:
    def __init__(self, app: Application) -> None:
        self.app = app
        self._cuts: dict[str, set[asyncio.Event]] = {}
        self._touched: dict[tuple[str, str], float] = {}
        self._attempts: dict[str, tuple[int, float]] = {}
        self._unlocking: dict[str, asyncio.Lock] = {}

    # -- the links ---------------------------------------------------------------------------

    @property
    def base(self) -> str:
        """Where a link points: the services door's public address. Read each time, since a tunnel's
        address arrives while the process runs."""
        return public_address.services_base(self.app.settings)

    def url(self, token: str) -> str:
        base = self.base
        return f"{base}/v/{token}/" if base else ""

    async def create(self, session_id: str, *, expires_hours: float | None = None, label: str = "", password: str | None = None,
                     policy: dict[str, Any] | None = None) -> dict[str, Any]:
        """A new link. ``policy`` is one :func:`daedalus.extensions.share_policy.validate` already checked."""
        found = await self.app.db.fetchone("SELECT id FROM sessions WHERE id = ?", (session_id,))
        if found is None:
            raise KeyError(session_id)
        if expires_hours is not None and not 0 < expires_hours <= EXPIRY_HOURS_MAX:
            raise ValueError(f"a link expires within {EXPIRY_HOURS_MAX} hours, or never")
        hashed = await asyncio.to_thread(hash_password, check_new_password(password)) if password is not None else None
        share_id = "shr_" + secrets.token_hex(8)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        created = datetime.now(UTC)
        expires = (created + timedelta(hours=expires_hours)).isoformat(timespec="seconds") if expires_hours else None
        await self.app.db.execute(
            "INSERT INTO session_shares (id, session_id, token, created_at, expires_at, label, password_hash, policy) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (share_id, session_id, token, created.isoformat(timespec="seconds"), expires, clean_label(label), hashed, json.dumps(policy or {})),
        )
        listed = await self.for_session(session_id)
        return next(item for item in listed["links"] if item["id"] == share_id)

    async def update(self, session_id: str, share_id: str, *, label: str | None = None, password: str | None = None, clear_password: bool = False,
                     policy: dict[str, Any] | None = None, expires_hours: float | None = None) -> dict[str, Any]:
        """Change a live link. A new or removed password ends every pass and every open connection at once;
        a new policy or expiry also cuts the connections, so a stream opened under the old one does not
        go on under it. The door reads the row on every request, so the next one meets the change."""
        row = await self.app.db.fetchone("SELECT * FROM session_shares WHERE id = ? AND session_id = ?", (share_id, session_id))
        if row is None:
            raise KeyError(share_id)
        if not live(dict(row)):
            raise ValueError("this link has ended; make a new one")
        if expires_hours is not None and not 0 < expires_hours <= EXPIRY_HOURS_MAX:
            raise ValueError(f"a link expires within {EXPIRY_HOURS_MAX} hours, or never")
        if password is not None and clear_password:
            raise ValueError("password: either a new one or clear_password, not both")
        changes: dict[str, Any] = {}
        if label is not None:
            changes["label"] = clean_label(label)
        if password is not None:
            changes["password_hash"] = await asyncio.to_thread(hash_password, check_new_password(password))
        elif clear_password:
            changes["password_hash"] = None
        if policy is not None:
            changes["policy"] = json.dumps(policy)
        if expires_hours is not None:
            changes["expires_at"] = (datetime.now(UTC) + timedelta(hours=expires_hours)).isoformat(timespec="seconds")
        if changes:
            assignments = ", ".join(f"{name} = ?" for name in changes)
            await self.app.db.execute(f"UPDATE session_shares SET {assignments} WHERE id = ?", (*changes.values(), share_id))  # noqa: S608 — names from this function
            if "password_hash" in changes:
                self._attempts.pop(share_id, None)
            if set(changes) - {"label"}:
                self.cut(share_id)
        listed = await self.for_session(session_id)
        return next(item for item in listed["links"] if item["id"] == share_id)

    async def revoke(self, session_id: str, share_id: str) -> bool:
        row = await self.app.db.fetchone("SELECT revoked_at FROM session_shares WHERE id = ? AND session_id = ?", (share_id, session_id))
        if row is None:
            return False
        if not row["revoked_at"]:
            await self.app.db.execute("UPDATE session_shares SET revoked_at = ? WHERE id = ?", (now_iso(), share_id))
        self.cut(share_id)
        return True

    async def by_token(self, token: str) -> dict[str, Any] | None:
        """The link a token names, live or not; None for a token that was never handed out."""
        if not TOKEN_RE.fullmatch(token or ""):
            return None
        row = await self.app.db.fetchone("SELECT * FROM session_shares WHERE token = ?", (token,))
        if row is None:
            return None
        # The token is the whole secret: compared in constant time even after the index found it.
        return dict(row) if secrets.compare_digest(str(row["token"]), token) else None

    async def for_session(self, session_id: str) -> dict[str, Any]:
        """The session's links, newest first, with their visitors: what the header and the dialog show."""
        rows = await self.app.db.fetchall("SELECT * FROM session_shares WHERE session_id = ? ORDER BY created_at DESC, id DESC", (session_id,))
        now = datetime.now(UTC)
        links = []
        for raw in rows:
            row = dict(raw)
            visitors = await self.app.db.fetchall(
                "SELECT visitor, name, first_seen_at, last_seen_at FROM session_share_visitors WHERE share_id = ? ORDER BY last_seen_at DESC",
                (row["id"],),
            )
            active = live(row, now)
            links.append({
                "id": row["id"],
                # A dead link's token is of no use to anyone and is not handed out again.
                "url": self.url(row["token"]) if active else "",
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "revoked_at": row["revoked_at"],
                "active": active,
                "last_seen_at": row["last_seen_at"],
                "label": str(row.get("label") or ""),
                "has_password": bool(row.get("password_hash")),
                "policy": share_policy.parse(row.get("policy")),
                "spent": {"usd": float(row.get("spent_usd") or 0), "weekly_pct": float(row.get("spent_weekly_pct") or 0)},
                "connections": len(self._cuts.get(row["id"], ())),
                "visitors": [{"name": str(v["name"] or ""), "first_seen_at": v["first_seen_at"], "last_seen_at": v["last_seen_at"]} for v in visitors],
            })
        base = self.base
        return {"links": links, "reach": reach(base), "base": base}

    async def by_id(self, share_id: str) -> dict[str, Any] | None:
        row = await self.app.db.fetchone("SELECT * FROM session_shares WHERE id = ?", (share_id,))
        return dict(row) if row is not None else None

    # -- what a visitor does -----------------------------------------------------------------

    async def unlock(self, share_id: str, password: str) -> tuple[str, float]:
        """Try a visitor's password: ``("ok", 0)`` with the pass to hand out read by :func:`pass_for`,
        ``("wrong", 0)``, or ``("slow", seconds)`` while the link waits out its guessers.

        Tries are taken one at a time per link, and the wait is checked before the hash is computed:
        a hundred guesses sent at once must not all be judged before the first failure is counted, and
        must not cost the host a hundred hashes either."""
        lock = self._unlocking.setdefault(share_id, asyncio.Lock())
        async with lock:
            failures, until = self._attempts.get(share_id, (0, 0.0))
            now = time.monotonic()
            if until > now:
                return "slow", until - now
            row = await self.by_id(share_id)
            stored = str((row or {}).get("password_hash") or "")
            # Bounded before it is hashed: scrypt reads the whole of whatever it is given.
            ok = bool(stored) and len(password) <= PASSWORD_MAX and await asyncio.to_thread(check_password, stored, password)
            if ok:
                self._attempts.pop(share_id, None)
                return "ok", 0.0
            failures += 1
            wait = 0.0 if failures < FREE_TRIES else min(DELAY_MAX, 2.0 ** (failures - FREE_TRIES + 1))
            self._attempts[share_id] = (failures, now + wait)
            return "wrong", wait

    async def touch(self, share_id: str, visitor: str, name: str) -> None:
        """Record that a visitor used the link; at most every :data:`TOUCH_SECONDS` per visitor."""
        key = (share_id, visitor)
        now = time.monotonic()
        if now - self._touched.get(key, -TOUCH_SECONDS) < TOUCH_SECONDS:
            return
        self._touched[key] = now
        at = now_iso()
        try:
            await self.app.db.execute("UPDATE session_shares SET last_seen_at = ? WHERE id = ?", (at, share_id))
            if visitor:
                await self.app.db.execute(
                    "INSERT INTO session_share_visitors (share_id, visitor, name, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (share_id, visitor) DO UPDATE SET last_seen_at = excluded.last_seen_at, "
                    "name = CASE WHEN excluded.name != '' THEN excluded.name ELSE session_share_visitors.name END",
                    (share_id, visitor, name, at, at),
                )
        except Exception:  # noqa: BLE001 — a lost count must not cost the visitor the request
            logger.warning("could not record a visit to share %s", share_id, exc_info=True)

    # -- live connections --------------------------------------------------------------------

    def hold(self, share_id: str) -> asyncio.Event:
        """An event set when the link is revoked; the door ends the request it was taken for."""
        event = asyncio.Event()
        self._cuts.setdefault(share_id, set()).add(event)
        return event

    def release(self, share_id: str, event: asyncio.Event) -> None:
        held = self._cuts.get(share_id)
        if held is not None:
            held.discard(event)
            if not held:
                del self._cuts[share_id]

    def cut(self, share_id: str) -> int:
        """End every request the link has in flight; answers how many there were."""
        held = list(self._cuts.get(share_id, ()))
        for event in held:
            event.set()
        return len(held)

    async def cut_session(self, session_id: str) -> None:
        """A session that is deleted takes its links with it, and their open connections too."""
        rows = await self.app.db.fetchall("SELECT id FROM session_shares WHERE session_id = ?", (session_id,))
        for row in rows:
            self.cut(str(row["id"]))


async def install(app: Application) -> list[Any]:
    app.extensions[EXTENSION_KEY] = SessionShares(app)
    return []


__all__ = [
    "EXTENSION_KEY", "TOKEN_RE", "VISITOR_RE", "SessionShares", "check_password", "clean_name", "hash_password", "install", "live",
    "pass_for", "pass_valid", "reach", "seconds_left",
]
