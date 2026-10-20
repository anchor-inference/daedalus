"""The operator's own Chrome: which conversations use it, the sites they may open there, the Chromes
paired with a server, and the relay that carries a paired Chrome to the ``user`` daemon.

The ``user`` daemon launches no browser. Natively the desktop launcher starts it, and Chrome reaches
it through ``browserd bridge`` (native messaging) without the host in between. On a server Chrome is
on the operator's laptop, so the host is the one thing that terminates its connection: the extension
opens ``wss://<server>/api/browser/relay``, presents a device token in its first frame, and the host
opens the relay on the daemon's socket (``relay.open``) and copies frames both ways, split and joined
exactly as the bridge does. The daemon never listens on the network.

A device token is made by spending a one-time code the operator reads in the app. Codes and tokens
are kept as their SHA-256 only; a token is compared in constant time against every paired device, and
the pairing and the relay's first frame share one limit on failed attempts per address and overall,
because both are reachable by anyone who can reach the server. Nothing here logs a code, a token or
an address.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from daedalus.browser import wire
from daedalus.browser.model import USER, Conflict, EnvUnavailable, Forbidden, InvalidRequest
from daedalus.host.policy import APPROVAL_MODES
from daedalus.terminals.client import PtydClient, Unavailable
from daedalus.terminals.wire import MAX_PAYLOAD

if TYPE_CHECKING:
    from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

AGENT = "agent"
"""A conversation's browser when it is not the operator's Chrome: the agent's own, wherever it runs."""
CHOICES = (AGENT, USER)

SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "nic", "mil", "ltd", "plc", "spb", "msk"})
"""Labels that, under a two-letter country domain, are a registry's own (``co.uk``, ``com.au``,
``spb.ru``) rather than a site. Without a public suffix list in the dependencies this is the part of it
that matters for granting a site: ``shop.example.co.uk`` is granted as ``example.co.uk``, never as the
whole of ``co.uk``."""


def site_of(host_or_url: str) -> str:
    """The site a host belongs to, as a grant names it: the registrable domain, lower case."""
    text = (host_or_url or "").strip().lower()
    if "://" in text:
        text = urlsplit(text).hostname or ""
    host = text.split("/")[0].split(":")[0].strip(".")
    labels = [p for p in host.split(".") if p]
    if len(labels) <= 2 or all(p.isdigit() for p in labels):
        return host
    if len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def site_granted(host: str, sites: list[str] | tuple[str, ...] | set[str]) -> bool:
    """Whether a host is on one of the sites (a site covers its subdomains)."""
    host = (host or "").lower().strip(".")
    return bool(host) and any(host == s or host.endswith("." + s) for s in sites if s)


# -- per conversation ------------------------------------------------------------------------------


CONSENT = (
    "The agent will work in your own Chrome, signed in as you. It uses only the tabs of its own tab group, "
    "in a window of its own; it cannot see your other tabs, your history or your cookies' values. Every new "
    "site asks you first, and a purchase, a message sent, a deletion or a sign-in always asks. A page it "
    "opens can do what a page you open can do."
)
"""The consent summary the app shows on a conversation's first switch, and the API answers with."""


class ConsentNeeded(Conflict):
    """The first switch of a conversation to the operator's Chrome, without the operator's yes."""

    code = "consent_needed"


class SkipNeedsConfirm(Conflict):
    """The first "skip all approvals" on this installation, without the operator's yes to it."""

    code = "skip_confirm_needed"


SKIP_CONFIRMED_KEY = "browser.user.skip_confirmed"
SKIP_WARNING = (
    "With approvals skipped, the agent acts in your signed-in Chrome without asking: it opens any site and "
    "buys, sends and deletes on its own. Refusals still hold, and every action stays in the conversation and the "
    "action log."
)


class UserChoices:
    """Per conversation: which browser it uses, whether the operator consented, the sites granted.

    Kept in one ``kv`` row, bounded, and dropped with the session: a grant is the operator's yes for
    one conversation, and a new one asks again. The agent has no way to write here — only the
    operator's switch and the operator's answer to a site question do.
    """

    KEY = "browser.user.sessions"
    KEEP = 500

    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = asyncio.Lock()

    async def _all(self) -> dict[str, dict[str, Any]]:
        saved = await self.db.kv_get(self.KEY, {})
        return saved if isinstance(saved, dict) else {}

    async def get(self, session_id: str) -> dict[str, Any]:
        entry = (await self._all()).get(session_id) or {}
        env = entry.get("env") if entry.get("env") in CHOICES else AGENT
        approvals = entry.get("approvals") if entry.get("approvals") in APPROVAL_MODES else "manual"
        return {"env": env, "consented": bool(entry.get("consented_at")), "sites": [str(s) for s in entry.get("sites") or []], "approvals": approvals}

    async def env(self, session_id: str | None) -> str:
        if not session_id:
            return AGENT
        return str((await self.get(session_id))["env"])

    async def using_user(self) -> list[str]:
        """The conversations the operator gave their Chrome, newest switch last: what the extension's
        side panel lists, and the only ones its credential may open."""
        return [sid for sid, entry in (await self._all()).items() if isinstance(entry, dict) and entry.get("env") == USER]

    async def _update(self, session_id: str, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        async with self._lock:
            entries = await self._all()
            entry = dict(entries.pop(session_id, None) or {})
            change(entry)
            entries[session_id] = entry
            # The newest last; the oldest go first past the bound.
            await self.db.kv_set(self.KEY, dict(list(entries.items())[-self.KEEP:]))
        return await self.get(session_id)

    async def switch(self, session_id: str, env: str, *, consent: bool) -> dict[str, Any]:
        """The operator's switch. The first one to the operator's Chrome in a conversation needs their
        yes to the consent summary; after that it switches back and forth without asking."""
        if env not in CHOICES:
            raise InvalidRequest(f"a conversation's browser is one of {', '.join(CHOICES)}, not {env!r}")
        current = await self.get(session_id)
        if env == USER and not current["consented"] and not consent:
            raise ConsentNeeded("the first switch to your Chrome in a conversation asks you once", consent=CONSENT)

        def change(entry: dict[str, Any]) -> None:
            entry["env"] = env
            if env == USER and not entry.get("consented_at"):
                entry["consented_at"] = datetime.now(UTC).isoformat()

        return await self._update(session_id, change)

    async def set_approvals(self, session_id: str, mode: str, *, confirm: bool = False) -> dict[str, Any]:
        """The operator's approval mode for one conversation in their Chrome. The first ``skip`` on this
        installation needs their yes to what it means, asked once, as the consent is."""
        if mode not in APPROVAL_MODES:
            raise InvalidRequest(f"an approval mode is one of {', '.join(APPROVAL_MODES)}, not {mode!r}")
        if mode == "skip" and not confirm and not await self.db.kv_get(SKIP_CONFIRMED_KEY, False):
            raise SkipNeedsConfirm("the agent will act in your signed-in Chrome without asking; say yes once", consent=SKIP_WARNING)

        def change(entry: dict[str, Any]) -> None:
            entry["approvals"] = mode

        if mode == "skip":
            await self.db.kv_set(SKIP_CONFIRMED_KEY, True)
        return await self._update(session_id, change)

    async def grant_site(self, session_id: str, site: str) -> None:
        def change(entry: dict[str, Any]) -> None:
            sites = [s for s in entry.get("sites") or [] if s != site]
            entry["sites"] = (sites + [site])[-200:]

        await self._update(session_id, change)

    async def forget(self, session_id: str) -> None:
        async with self._lock:
            entries = await self._all()
            if entries.pop(session_id, None) is not None:
                await self.db.kv_set(self.KEY, entries)


# -- failed attempts -------------------------------------------------------------------------------


class Attempts:
    """Failed attempts at the public doors (a pairing code, a relay's token), per address and overall.

    Past either bound every attempt is refused until the window moves on, the right one included: a
    guess that waits its turn costs the guesser minutes, a code lives ten of them, and the operator who
    mistyped makes a new code. Only failures count, so a Chrome that reconnects all day is never held.
    """

    def __init__(self, *, per_address: int = 5, overall: int = 30, window: float = 600.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_address = per_address
        self.overall = overall
        self.window = window
        self.clock = clock
        self._all: deque[float] = deque()
        self._by: dict[str, deque[float]] = {}

    def _trim(self, q: deque[float], now: float) -> None:
        while q and now - q[0] > self.window:
            q.popleft()

    def blocked(self, address: str) -> bool:
        now = self.clock()
        self._trim(self._all, now)
        mine = self._by.get(address)
        if mine is not None:
            self._trim(mine, now)
            if not mine:
                self._by.pop(address, None)
        return len(self._all) >= self.overall or (mine is not None and len(mine) >= self.per_address)

    def failed(self, address: str) -> None:
        now = self.clock()
        self._all.append(now)
        self._by.setdefault(address, deque()).append(now)
        if len(self._by) > 4096:
            # A flood from many addresses: the overall bound holds it; the table must not grow for ever.
            for key in list(self._by)[:2048]:
                self._by.pop(key, None)


# -- pairing a Chrome with a server ------------------------------------------------------------------


CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
"""No 0/O, 1/I/L: the code is read off one screen and typed into another."""
CODE_LENGTH = 8
CODE_TTL = timedelta(minutes=10)
DEVICE_IDLE = timedelta(days=90)
"""A paired Chrome not seen for this long has to pair again: its token stops opening the relay."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normal_code(code: str) -> str:
    """A code as typed — any case, with or without its dash or spaces — in the form it was minted."""
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


class PairingRefused(Forbidden):
    """A code that is wrong, spent or expired; or too many wrong ones."""


class TooMany(PairingRefused):
    status, code = 429, "too_many"


@dataclass(slots=True)
class Device:
    id: str
    name: str
    profile_id: str
    ext_version: str
    chrome_version: str
    paired_at: str
    last_seen: str

    def view(self, *, connected: bool = False) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "profile_id": self.profile_id, "ext_version": self.ext_version, "chrome_version": self.chrome_version,
                "paired_at": self.paired_at, "last_seen": self.last_seen, "connected": connected}


class Pairing:
    """One-time codes and the device tokens they become, in ``kv``, hashed."""

    CODES = "browser.user.codes"
    DEVICES = "browser.user.devices"

    def __init__(self, db: Database, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.db = db
        self.now = now
        self.attempts = Attempts()
        self._lock = asyncio.Lock()

    async def _codes(self) -> list[dict[str, Any]]:
        saved = await self.db.kv_get(self.CODES, [])
        now = self.now().isoformat()
        return [c for c in saved if isinstance(c, dict) and str(c.get("expires_at") or "") > now] if isinstance(saved, list) else []

    async def _devices(self) -> list[dict[str, Any]]:
        saved = await self.db.kv_get(self.DEVICES, [])
        return [d for d in saved if isinstance(d, dict) and d.get("id") and d.get("token_hash")] if isinstance(saved, list) else []

    async def mint(self) -> dict[str, Any]:
        """A fresh code, returned once; only its digest is kept. Older outstanding codes stay valid
        until they expire or one is spent."""
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        expires = self.now() + CODE_TTL
        async with self._lock:
            codes = await self._codes()
            codes.append({"hash": _digest(raw), "expires_at": expires.isoformat()})
            await self.db.kv_set(self.CODES, codes[-10:])
        return {"code": f"{raw[:4]}-{raw[4:]}", "expires_at": expires.isoformat().replace("+00:00", "Z")}

    async def redeem(self, code: str, *, address: str, profile_id: str, profile_name: str, ext_version: str, chrome_version: str) -> str:
        """Spend a code for a device token, returned once. Spending one revokes every other outstanding
        code: a code that did its job leaves no second copy working on a screen or in a paste buffer."""
        if self.attempts.blocked(address):
            raise TooMany("too many wrong codes; wait a few minutes and make a new code in the app")
        wanted = _digest(normal_code(code))
        async with self._lock:
            codes = await self._codes()
            match = False
            for entry in codes:
                # Every outstanding code is compared, in constant time, whichever matches.
                match = hmac.compare_digest(wanted, str(entry.get("hash") or "")) or match
            if not match:
                self.attempts.failed(address)
                raise PairingRefused("the code is wrong or has expired; make a new one in the app (Settings → Environments → Your Chrome)")
            await self.db.kv_set(self.CODES, [])
            token = secrets.token_urlsafe(32)
            stamp = self.now().isoformat()
            devices = [d for d in await self._devices() if not (profile_id and d.get("profile_id") == profile_id)]
            device = {
                "id": secrets.token_hex(6), "token_hash": _digest(token), "name": (profile_name or "Chrome").strip()[:80] or "Chrome",
                "profile_id": profile_id[:200], "ext_version": ext_version[:64], "chrome_version": chrome_version[:64], "paired_at": stamp, "last_seen": stamp,
            }
            devices.append(device)
            await self.db.kv_set(self.DEVICES, devices[-20:])
        return token

    async def device_of(self, token: str, *, address: str) -> Device | None:
        """The paired device a token is, or None for a wrong, revoked or long-unused one. A wrong one
        counts against the address."""
        if not token or self.attempts.blocked(address):
            return None
        wanted = _digest(token)
        found: dict[str, Any] | None = None
        for entry in await self._devices():
            if hmac.compare_digest(wanted, str(entry["token_hash"])):
                found = entry
        if found is None or self._idle(found):
            self.attempts.failed(address)
            return None
        return self._device(found)

    def _idle(self, entry: dict[str, Any]) -> bool:
        try:
            seen = datetime.fromisoformat(str(entry.get("last_seen") or entry.get("paired_at")))
        except ValueError:
            return True
        return self.now() - seen > DEVICE_IDLE

    @staticmethod
    def _device(entry: dict[str, Any]) -> Device:
        return Device(str(entry["id"]), str(entry.get("name") or "Chrome"), str(entry.get("profile_id") or ""), str(entry.get("ext_version") or ""),
                      str(entry.get("chrome_version") or ""), str(entry.get("paired_at") or ""), str(entry.get("last_seen") or ""))

    async def devices(self) -> list[Device]:
        return [self._device(d) for d in await self._devices() if not self._idle(d)]

    async def seen(self, device_id: str, *, name: str = "", ext_version: str = "", chrome_version: str = "") -> None:
        async with self._lock:
            devices = await self._devices()
            for d in devices:
                if d["id"] == device_id:
                    d["last_seen"] = self.now().isoformat()
                    d["name"] = (name or d.get("name") or "Chrome")[:80]
                    d["ext_version"] = (ext_version or d.get("ext_version") or "")[:64]
                    d["chrome_version"] = (chrome_version or d.get("chrome_version") or "")[:64]
            await self.db.kv_set(self.DEVICES, devices)

    async def revoke(self, device_id: str) -> bool:
        async with self._lock:
            devices = await self._devices()
            kept = [d for d in devices if d["id"] != device_id]
            if len(kept) == len(devices):
                return False
            await self.db.kv_set(self.DEVICES, kept)
        return True


# -- the relay over a WebSocket ----------------------------------------------------------------------


PIECE_LAST, PIECE_MORE = 0, 1
"""Every socket frame on the relay channel starts with one byte: the relay frame ends in it, or goes
on in the next one (``browserd/internal/relay/pieces.go``)."""
MAX_FROM_EXTENSION = 64 << 20
MAX_TO_EXTENSION = 1 << 20
HELLO_SECONDS = 10.0

CLOSE_UNAUTHORIZED = 4401
"""The pairing expired or was revoked, or the token is wrong: the extension asks to pair again."""
CLOSE_BUSY = 4409
"""Another Chrome is connected to this installation; the reason names it."""
CLOSE_BAD_FRAME = 4400
CLOSE_UNAVAILABLE = 4503


def split(frame: bytes, limit: int = MAX_PAYLOAD) -> list[bytes]:
    """One relay frame as the payloads of socket frames of at most ``limit`` bytes, marker included."""
    room = limit - 1
    out = []
    while len(frame) > room:
        out.append(bytes([PIECE_MORE]) + frame[:room])
        frame = frame[room:]
    out.append(bytes([PIECE_LAST]) + frame)
    return out


class Joiner:
    """Relay frames put back together from socket-frame payloads."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.buf = bytearray()

    def add(self, payload: bytes) -> bytes | None:
        if not payload or payload[0] not in (PIECE_LAST, PIECE_MORE):
            raise ValueError("a relay piece without its marker")
        if len(self.buf) + len(payload) - 1 > self.limit:
            raise ValueError("a relay frame larger than the relay carries")
        self.buf.extend(payload[1:])
        if payload[0] == PIECE_MORE:
            return None
        out = bytes(self.buf)
        self.buf.clear()
        return out


class RelaySocket(Protocol):
    """The framework's WebSocket as the relay needs it."""

    async def receive(self) -> bytes | str | None: ...

    async def send_text(self, text: str) -> None: ...

    async def close(self, code: int, reason: str = "") -> None: ...


@dataclass(slots=True)
class LiveRelay:
    device: Device
    profile_id: str
    name: str
    socket: RelaySocket
    since: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    ended: asyncio.Event = field(default_factory=asyncio.Event)


class RelayDesk:
    """The host's end of ``wss /api/browser/relay``: one relay at a time, proxied to the user daemon."""

    def __init__(self, pairing: Pairing, run_dir: Callable[[], Path | None], *, install_name: Callable[[], str] = lambda: "Daedalus") -> None:
        self.pairing = pairing
        self.run_dir = run_dir
        self.install_name = install_name
        self.current: LiveRelay | None = None
        self._lock = asyncio.Lock()

    async def _bye(self, sock: RelaySocket, reason: str, message: str, code: int) -> None:
        with contextlib.suppress(Exception):
            await sock.send_text(json.dumps({"t": "bye", "reason": reason, "message": message}))
        await sock.close(code, message[:120])

    async def serve(self, sock: RelaySocket, *, address: str) -> None:
        """Carry one Chrome's relay until either side ends it."""
        try:
            first = await asyncio.wait_for(sock.receive(), HELLO_SECONDS)
        except TimeoutError:
            await sock.close(CLOSE_BAD_FRAME, "no hello")
            return
        hello = _frame(first)
        if hello is None or hello.get("t") != "hello":
            await sock.close(CLOSE_BAD_FRAME, "the first frame must be hello")
            return
        token = str(hello.pop("token", "") or "")
        device = await self.pairing.device_of(token, address=address)
        if device is None:
            await self._bye(sock, "unauthorized", "pairing expired or revoked; pair this Chrome again in the app", CLOSE_UNAUTHORIZED)
            return
        profile_id = str(hello.get("profile_id") or "")
        name = str(hello.get("profile_name") or device.name or "Chrome")[:80]
        run_dir = self.run_dir()
        if run_dir is None:
            await self._bye(sock, "unavailable", "this installation has no browser for your Chrome", CLOSE_UNAVAILABLE)
            return
        live = LiveRelay(device, profile_id, name, sock)
        async with self._lock:
            old = self.current
            if old is not None and not old.ended.is_set():
                if not profile_id or old.profile_id != profile_id:
                    await self._bye(sock, "another_relay", f"another Chrome is already connected: {old.name}", CLOSE_BUSY)
                    return
                # The same Chrome profile again: its worker restarted, or its old socket is half dead.
                await self._bye(old.socket, "replaced", "this Chrome connected again", 1000)
                old.ended.set()
            self.current = live
        try:
            await self._carry(live, hello, run_dir)
        finally:
            live.ended.set()
            if self.current is live:
                self.current = None
            with contextlib.suppress(Exception):
                await self.pairing.seen(device.id, name=name, ext_version=str(hello.get("ext_version") or ""), chrome_version=str(hello.get("chrome_version") or ""))

    async def _carry(self, live: LiveRelay, hello: dict[str, Any], run_dir: Path) -> None:
        sock = live.socket
        client = PtydClient(USER, run_dir, label="browser service for your Chrome", outdated="update Daedalus", lock=wire.LOCK_FILE)
        try:
            await client.connect()
            opened = await client.call("relay.open", {"transport": "server", "install_name": self.install_name()[:200]})
        except (Unavailable, wire.RpcError) as exc:
            detail = exc.detail if isinstance(exc, Unavailable) else exc.message
            await client.close()
            await self._bye(sock, "unavailable", f"Daedalus cannot reach the browser service for your Chrome: {detail}"[:300], CLOSE_UNAVAILABLE)
            return
        channel = client.channel(int(opened["channel"]))
        await self.pairing.seen(live.device.id, name=live.name)
        busy = asyncio.Event()

        async def to_daemon() -> None:
            await _send_pieces(channel, json.dumps(hello, separators=(",", ":")).encode())
            while not live.ended.is_set():
                message = await sock.receive()
                if message is None:
                    return
                data = message.encode() if isinstance(message, str) else message
                if len(data) > MAX_FROM_EXTENSION:
                    await sock.close(CLOSE_BAD_FRAME, "frame too large")
                    return
                await _send_pieces(channel, data)

        async def to_chrome() -> None:
            join = Joiner(MAX_TO_EXTENSION)
            while True:
                payload = await channel.recv()
                if payload is None:
                    return
                frame = join.add(payload)
                if frame is None:
                    continue
                await sock.send_text(frame.decode("utf-8", errors="replace"))
                parsed = _frame(frame)
                if parsed is not None and parsed.get("t") == "bye":
                    if parsed.get("reason") == "another_relay":
                        busy.set()
                    return

        tasks = [asyncio.create_task(to_daemon()), asyncio.create_task(to_chrome()), asyncio.create_task(live.ended.wait())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (Unavailable, ValueError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await channel.close()
            await client.close()
            if busy.is_set():
                # The daemon has a relay of its own already (a Chrome through the bridge on the
                # server itself): refused as the host refuses a second WebSocket.
                await sock.close(CLOSE_BUSY, "another Chrome is already connected")
            else:
                await sock.close(1000, "relay ended")

    async def revoked(self, device_id: str) -> None:
        """A device was revoked: its live relay ends now, with the code that says pair again."""
        live = self.current
        if live is not None and live.device.id == device_id and not live.ended.is_set():
            await self._bye(live.socket, "unauthorized", "this Chrome's pairing was revoked", CLOSE_UNAUTHORIZED)
            live.ended.set()

    def connected_device(self) -> str:
        live = self.current
        return live.device.id if live is not None and not live.ended.is_set() else ""


async def _send_pieces(channel: Any, frame: bytes) -> None:
    for piece in split(frame):
        await channel.send(piece)


def _frame(data: bytes | str | None) -> dict[str, Any] | None:
    if data is None:
        return None
    try:
        value = json.loads(data)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def not_connected(reason: str) -> EnvUnavailable:
    return EnvUnavailable(f"your Chrome is not connected to Daedalus: {reason or 'the extension has not connected'}", env=USER, reason="chrome_not_connected")


__all__ = [
    "AGENT", "CHOICES", "CLOSE_BUSY", "CLOSE_UNAUTHORIZED", "CONSENT", "Attempts", "ConsentNeeded", "Device", "Joiner", "Pairing", "PairingRefused",
    "RelayDesk", "RelaySocket", "TooMany", "UserChoices", "normal_code", "not_connected", "site_granted", "site_of", "split",
]
