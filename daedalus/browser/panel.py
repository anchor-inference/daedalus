"""The side panel of the Daedalus extension: its credential, and what the host tells it.

The panel is the host's own app framed inside the extension (``/app/panel``). It signs in with a
credential of its own rather than the operator's cookie or the installation's token, because it is
handed to a page inside another program and must be able to do no more than the panel shows: the
conversations of that Chrome, and nothing else of the installation.

- A panel token is minted for one Chrome (a paired device on a server, a Chrome profile of this
  computer natively: the daemon's name for it either way) and only ever delivered over that Chrome's
  relay, which is already authenticated: the host asks the ``user`` daemon to pass it on (``relay.call
  panel.grant``, naming the Chrome). No HTTP endpoint hands one out, and a token one Chrome holds opens
  nothing of another's.
- Only its SHA-256 is kept, it is compared in constant time against every live one, and it lapses
  after twelve hours unused. Unpairing the device, or the operator ending every browser session,
  revokes it at once.
- What it opens is decided by ``daedalus.extensions.api_panel``, the one door that accepts it.

The same relay carries the panel's news: a conversation in this Chrome needs the operator, or
finished (``relay.call panel.event``), which the extension turns into a notification and the badge.

Which conversations are this Chrome's (:func:`follows_in_chrome`): of those that belong to it or to no
Chrome yet (``UserChoices.bind``), the ones the operator made for it (the side panel, *Ask Daedalus
about this page*, a Browser chat from the app), and any other while it is controlling this Chrome now
(:mod:`daedalus.host.control_marks`). Any conversation the operator ever gave their Chrome used to
count, for good, so the main chat that once opened a page was announced in Chrome after every turn it
took.

The news is not a second notifier. It is a channel of the app's notifications (:class:`ChromeChannel`):
the same router decides whether a turn's end or a question is worth a sound at all — the operator's
matrix, quiet hours, mutes, what they are looking at — and an entry the extension shows is not pushed
or raised by the desktop as well. The extension used to be told of every turn's end by a hook of its
own while the app announced the same end through its channels, and the operator heard each one twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable, Iterable
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


Call = Callable[[str, dict[str, Any], str], Awaitable[Any]]
"""``(op, args, chrome)`` passed to that Chrome through the user daemon's ``relay.call``."""


class PanelHerald:
    """Hands each connected Chrome its panel token, and tells it a conversation's news.

    ``chromes`` names the Chromes connected now; ``base`` is the address of the app as a Chrome reaches
    it, which a paired Chrome replaces with the address it was paired with. Neither the herald nor
    anything it calls waits for an answer that matters: a Chrome that went away between the check and
    the call misses one grant, and gets the next when it connects again.
    """

    def __init__(self, grants: PanelGrants, *, call: Call, chromes: Callable[[], list[str]], base: Callable[[], str]) -> None:
        self.grants = grants
        self.call = call
        self.chromes = chromes
        self.base = base
        self._lock = asyncio.Lock()

    async def grant(self, chrome: str, *, force: bool = True) -> bool:
        """Mint a token for one Chrome and deliver it down that Chrome's relay. Without ``force`` only
        when its newest one runs out soon. Whether one was delivered."""
        base = self.base().rstrip("/")
        if not chrome:
            return False
        async with self._lock:
            if not force:
                left = await self.grants.expires(chrome)
                if left is not None and left - self.grants.now() > REFRESH_AHEAD:
                    return False
            token, expires = await self.grants.mint(chrome)
            try:
                await self.call("panel.grant", {"base": base, "token": token, "expires": expires.isoformat().replace("+00:00", "Z")}, chrome)
            except Exception as exc:  # noqa: BLE001 — a Chrome that went away gets the next grant when it comes back
                logger.info("the side panel's grant did not reach Chrome: %s", exc)
                return False
        return True

    async def grant_all(self, *, force: bool = True) -> None:
        for chrome in self.chromes():
            await self.grant(chrome, force=force)

    async def event(self, chrome: str, session_id: str, kind: str, title: str, *, waiting: int, tab: int | None = None) -> None:
        args: dict[str, Any] = {"session": session_id, "kind": kind, "title": title[:120], "waiting": max(0, waiting)}
        if tab is not None:
            args["tab"] = tab
        with contextlib.suppress(Exception):
            await self.call("panel.event", args, chrome)

    async def keep_fresh(self, *, first: float = 15.0, every: float = 600.0) -> None:
        """Runs for the life of the host: each connected Chrome's token is renewed before it lapses. The
        first round is soon after the start and hands one over whatever a Chrome holds: a Chrome that
        connected before the host listened never had a connection to be handed a token on."""
        delay, force = first, True
        while True:
            await asyncio.sleep(delay)
            try:
                await self.grant_all(force=force)
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


async def hold_in_chrome(service: Any, session_id: str, chrome: str) -> None:
    """The operator holds this conversation from this Chrome's side panel: it belongs to that Chrome,
    and stays there. What it is talking about is the page beside the panel, on that computer."""
    if chrome:
        name = next((str(c.get("profile_name") or "") for c in service.user_chromes() if c["chrome"] == chrome), "")
        await service.choices.bind(session_id, chrome, name, held=True)


def follows_in_chrome(kind: str, controlling: Iterable[str], *, bound: str = "", chrome: str = "") -> bool:
    """Whether a conversation given the operator's Chrome is one this Chrome's side panel lists and
    the extension announces: made for their Chrome, or controlling it now, and not another Chrome's.
    ``bound`` is the Chrome the conversation belongs to, ``chrome`` the one asking; a conversation of
    none yet is listed by each, since either may turn out to be it."""
    if bound and chrome and bound != chrome:
        return False
    return kind == "chrome" or "chrome" in controlling


def waits_for_operator(state: Any) -> bool:
    """A loaded conversation that waits for the operator: a question, or an action they must approve."""
    return state is not None and (state.pending is not None or bool(state.metadata.get("policy_pending")))


class ChromeChannel:
    """The app's notifications, out through the extension: its notification and its badge.

    Registered with the notification service as ``chrome``. ``covers`` says whether an entry's
    conversation is one a Chrome connected now follows; the router then gives the entry to this channel
    instead of the push and the desktop, which on the same computer would be the same sound twice. A
    conversation of a Chrome that is away is left to those: told to nobody, the operator would not hear
    of it at all, and told to the other computer it would be news from the wrong machine. ``held`` is
    what the conversation controlled when the thing happened, for an entry posted after the control
    ended: a turn's end is posted once the turn's tabs are already closed.
    """

    name = "chrome"

    def __init__(self, herald: PanelHerald, *, manager: Any, using: Callable[..., Awaitable[list[str]]], bound: Callable[[str], Awaitable[str]], offered: Callable[[], bool]) -> None:
        self.herald = herald
        self.manager = manager
        self.using = using
        self.bound = bound
        self.offered = offered

    def available(self) -> bool:
        return bool(self.herald.chromes())

    async def _kind(self, session_id: str) -> str:
        state = self.manager.live_state(session_id)
        return str(state.kind) if state is not None else await self.manager.sessions.kind(session_id)

    async def _targets(self, session_id: str) -> list[str]:
        """The connected Chromes that a conversation's news is for: its own when it has one, else every
        one, since each lists it."""
        live = self.herald.chromes()
        bound = await self.bound(session_id)
        return ([bound] if bound in live else []) if bound else list(live)

    async def covers(self, session_id: str, held: Iterable[str] = ()) -> bool:
        if not self.offered() or session_id not in await self.using() or not await self._targets(session_id):
            return False
        return follows_in_chrome(await self._kind(session_id), [*self.manager.control_marks.of(session_id), *held])

    async def waiting(self, chrome: str) -> int:
        """How many of this Chrome's conversations wait for the operator: its badge's number."""
        ids = await self.using(chrome)
        kinds = await self.manager.sessions.kinds(ids)
        return sum(
            1 for sid in ids
            if follows_in_chrome(kinds.get(sid, "chat"), self.manager.control_marks.of(sid)) and waits_for_operator(self.manager.live_state(sid))
        )

    async def deliver(self, view: Any, *, more: int) -> Any:
        session_id = str(view.get("session_id") or "")
        if not session_id:
            return "skipped: no conversation"
        # The extension words its notification itself, in the operator's language, with two sentences:
        # it waits for them, or it is done. What it is named by is the conversation's own title.
        kind = "needs_you" if view.get("needs_you") or view.get("category") in ("question", "permission") else "done"
        state = self.manager.live_state(session_id)
        title = str(state.session.title) if state is not None else str(view.get("title") or "")
        for chrome in await self._targets(session_id):
            await self.herald.event(chrome, session_id, kind, title, waiting=await self.waiting(chrome))
        return "sent"


__all__ = [
    "CHROME_CHAT_LINE",
    "EXTENSION_ID",
    "IDLE",
    "PANEL_LINE",
    "PANEL_SESSION_KEY",
    "ChromeChannel",
    "PanelGrants",
    "PanelHerald",
    "follows_in_chrome",
    "hold_in_chrome",
    "mark_panel_session",
    "waits_for_operator",
]
