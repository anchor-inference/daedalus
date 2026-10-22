"""The side panel of the Daedalus extension: its credential, and what the host tells it.

The panel is the host's own app framed inside the extension (``/app/panel``). It signs in with a
credential of its own rather than the operator's cookie or the installation's token, because it is
handed to a page inside another program and must be able to do no more than the panel shows: the
conversations that use the paired Chrome, and nothing else of the installation.

- A panel token is minted for the Chrome connected now (a paired device on a server, the one Chrome
  of this computer natively) and only ever delivered over that Chrome's relay, which is already
  authenticated: the host asks the ``user`` daemon to pass it on (``relay.call panel.grant``). No
  HTTP endpoint hands one out.
- Only its SHA-256 is kept, it is compared in constant time against every live one, and it lapses
  after twelve hours unused. Unpairing the device, or the operator ending every browser session,
  revokes it at once.
- What it opens is decided by ``daedalus.extensions.api_panel``, the one door that accepts it.

The same relay carries the panel's news: a conversation in this Chrome needs the operator, or
finished (``relay.call panel.event``), which the extension turns into a notification and the badge.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from daedalus.host import tool_groups

if TYPE_CHECKING:
    from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

EXTENSION_ID = "lkhhklicnjlplokpadpccndipomikoaj"
"""The Daedalus extension's id, fixed by the public key in its manifest (``src/protocol.ts`` in the
daedalus-chrome-extension repository): the one origin allowed to frame the side panel's page."""
IDLE = timedelta(hours=12)
"""A panel token unused for this long stops working; each use moves the end along."""
REFRESH_AHEAD = timedelta(hours=2)
"""A connected Chrome whose newest token has less than this left is handed a fresh one, so a panel
left open overnight still opens in the morning without a reconnect."""
TOUCH_EVERY = timedelta(minutes=1)
"""A use is written down at most this often: every request of an open panel would otherwise be a
write to the database."""
NATIVE_DEVICE = "native"
"""The device a token is for when Chrome is on this computer, which pairs nothing."""
KEEP_PER_DEVICE = 2
"""The newest token and the one before: a panel that loaded just before a fresh one arrived keeps
working until it picks the new one up."""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PanelGrants:
    """Panel tokens in ``kv``, hashed, per device."""

    KEY = "browser.panel.grants"

    def __init__(self, db: Database, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.db = db
        self.now = now
        self._lock = asyncio.Lock()

    async def _all(self) -> list[dict[str, Any]]:
        saved = await self.db.kv_get(self.KEY, [])
        if not isinstance(saved, list):
            return []
        return [g for g in saved if isinstance(g, dict) and g.get("hash") and g.get("device")]

    def _expires(self, grant: dict[str, Any]) -> datetime:
        try:
            used = datetime.fromisoformat(str(grant.get("used_at") or grant.get("created_at")))
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)
        return used + IDLE

    async def mint(self, device: str) -> tuple[str, datetime]:
        """A new token for the device, returned once, and when it lapses if unused."""
        token = "pnl_" + secrets.token_urlsafe(32)
        stamp = self.now()
        async with self._lock:
            now = self.now()
            kept = [g for g in await self._all() if self._expires(g) > now]
            mine = [g for g in kept if g["device"] == device][-(KEEP_PER_DEVICE - 1):]
            others = [g for g in kept if g["device"] != device]
            mine.append({"hash": _digest(token), "device": device, "created_at": stamp.isoformat(), "used_at": stamp.isoformat()})
            await self.db.kv_set(self.KEY, (others + mine)[-40:])
        return token, stamp + IDLE

    async def check(self, token: str) -> str | None:
        """The device a live token is for, or None. A use moves its end along."""
        if not token:
            return None
        wanted = _digest(token)
        found: dict[str, Any] | None = None
        grants = await self._all()
        for grant in grants:
            # Every grant is compared, in constant time, whichever matches.
            if hmac.compare_digest(wanted, str(grant["hash"])):
                found = grant
        now = self.now()
        if found is None or self._expires(found) <= now:
            return None
        try:
            used = datetime.fromisoformat(str(found.get("used_at") or ""))
        except ValueError:
            used = now - TOUCH_EVERY
        if now - used >= TOUCH_EVERY:
            async with self._lock:
                fresh = await self._all()
                for grant in fresh:
                    if grant["hash"] == found["hash"]:
                        grant["used_at"] = now.isoformat()
                await self.db.kv_set(self.KEY, fresh)
        return str(found["device"])

    async def expires(self, device: str) -> datetime | None:
        """When the device's newest live token lapses, or None when it has none."""
        now = self.now()
        live = [self._expires(g) for g in await self._all() if g["device"] == device and self._expires(g) > now]
        return max(live) if live else None

    async def revoke(self, device: str) -> None:
        """The device was unpaired: every token of its stops at once."""
        async with self._lock:
            grants = await self._all()
            kept = [g for g in grants if g["device"] != device]
            if len(kept) != len(grants):
                await self.db.kv_set(self.KEY, kept)

    async def revoke_all(self) -> None:
        """The operator ended every browser session: the panels' end with them."""
        async with self._lock:
            await self.db.kv_set(self.KEY, [])


Call = Callable[[str, dict[str, Any]], Awaitable[Any]]
"""``(op, args)`` passed to the connected Chrome through the user daemon's ``relay.call``."""


class PanelHerald:
    """Hands the connected Chrome its panel token, and tells it a conversation's news.

    ``device`` names the Chrome connected now (empty when none is); ``base`` is the address of the
    app as that Chrome reaches it, which a paired Chrome replaces with the address it was paired with. Neither the herald nor anything it calls waits for an answer that
    matters: a Chrome that went away between the check and the call misses one grant, and gets the
    next when it connects again.
    """

    def __init__(self, grants: PanelGrants, *, call: Call, device: Callable[[], str], base: Callable[[], str]) -> None:
        self.grants = grants
        self.call = call
        self.device = device
        self.base = base
        self._lock = asyncio.Lock()

    async def grant(self, *, force: bool = True) -> bool:
        """Mint and deliver a token for the Chrome connected now. Without ``force`` only when its
        newest one runs out soon. Whether one was delivered."""
        device = self.device()
        base = self.base().rstrip("/")
        if not device:
            return False
        async with self._lock:
            if not force:
                left = await self.grants.expires(device)
                if left is not None and left - self.grants.now() > REFRESH_AHEAD:
                    return False
            token, expires = await self.grants.mint(device)
            try:
                await self.call("panel.grant", {"base": base, "token": token, "expires": expires.isoformat().replace("+00:00", "Z")})
            except Exception as exc:  # noqa: BLE001 — a Chrome that went away gets the next grant when it comes back
                logger.info("the side panel's grant did not reach Chrome: %s", exc)
                return False
        return True

    async def event(self, session_id: str, kind: str, title: str, *, waiting: int, tab: int | None = None) -> None:
        if not self.device():
            return
        args: dict[str, Any] = {"session": session_id, "kind": kind, "title": title[:120], "waiting": max(0, waiting)}
        if tab is not None:
            args["tab"] = tab
        with contextlib.suppress(Exception):
            await self.call("panel.event", args)

    async def keep_fresh(self, *, first: float = 15.0, every: float = 600.0) -> None:
        """Runs for the life of the host: a connected Chrome's token is renewed before it lapses. The
        first round is soon after the start and hands one over whatever the Chrome holds: a Chrome that
        connected before the host listened never had a connection to be handed a token on."""
        delay, force = first, True
        while True:
            await asyncio.sleep(delay)
            try:
                await self.grant(force=force)
            except Exception:  # noqa: BLE001 — the next round tries again
                logger.exception("renewing the side panel's grant failed")
            delay, force = every, False


PANEL_SESSION_KEY = "browser.panel"
"""Session metadata: the operator talks to this conversation from the Chrome side panel. Set when the
panel starts it or sends into it, and kept: the conversation is about the page beside it."""

PANEL_LINE = (
    "- Side panel: the operator is talking to you from the Chrome side panel beside the page they are on. For "
    "anything about pages, sites, accounts or actions on the web, work in their Chrome with the browser tools, "
    "starting from the current tab, rather than WebSearch or WebFetch; a plain knowledge question may use search."
)
"""The turn context's line for such a conversation. Without it the agent answered "find the cheapest
flight on this site" with a web search, as it would in the app; from the panel the operator means the
page in front of them. Fixed words, so it costs one line and never a cache miss."""


CHROME_CHAT_LINE = (
    "- Chrome chat: the operator made this a conversation for work in their own Chrome. For anything about pages, "
    "sites, accounts or actions on the web, work in their Chrome with the browser tools rather than WebSearch or "
    "WebFetch; a plain knowledge question may use search."
)
"""The side panel's browser-first rule for a Chrome conversation the operator holds from the app: the
same expectation, without the claim that they are looking at the page beside the panel."""


async def mark_panel_session(manager: Any, session_id: str) -> None:
    """Mark a conversation as one the operator holds from the side panel, and have its runs start with
    the browser's tools loaded: the group is on demand elsewhere, and an agent told to work in the
    browser should not have to search for the tools first. Written once; later calls change nothing."""
    state = await manager.get_state(session_id)
    if state is None:
        return
    carried = [str(g) for g in (state.metadata.get(tool_groups.CARRIED_KEY) or ()) if isinstance(g, str)]
    if state.metadata.get(PANEL_SESSION_KEY) and "browser" in carried:
        return
    for meta in (state.metadata, state.session.metadata):
        meta[PANEL_SESSION_KEY] = True
        meta[tool_groups.CARRIED_KEY] = list(dict.fromkeys([*carried, "browser"]))
    await manager.sessions.update_metadata(session_id, state.session.metadata)


__all__ = ["CHROME_CHAT_LINE", "EXTENSION_ID", "IDLE", "NATIVE_DEVICE", "PANEL_LINE", "PANEL_SESSION_KEY", "PanelGrants", "PanelHerald", "mark_panel_session"]
