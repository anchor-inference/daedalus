"""The app's way to a browser's live view: which frames the socket carries, and the view's audit.

The tickets, the Origin rule and the relay are shared with the terminals (``daedalus.gateway``). What
is the browser's own is the frame check — from the app only ATTACH, VIEW, ACK and a person's INPUT, at
their sizes (``docs/architecture/browser.md``, The host's relay) — and the audit, which records who
watched and how many inputs of each kind a person sent, never what they typed.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from daedalus.browser import wire
from daedalus.browser.model import BrowserError, EnvUnavailable, NotFound
from daedalus.gateway import (
    CLOSE_INTERNAL,
    CLOSE_ORIGIN,
    CLOSE_POLICY,
    CLOSE_TICKET,
    CLOSE_TOO_BIG,
    CLOSE_UNAVAILABLE,
    CLOSE_UNKNOWN,
    FrameSocket,
    Refused,
    RelayResult,
    TicketBook,
    allowed_origin,
    close_reason,
    relay,
)

if TYPE_CHECKING:
    from daedalus.browser.service import Attachment, Browsers

logger = logging.getLogger(__name__)

BROWSER_WS_MAX_BYTES = 1 << 20
"""The largest WebSocket message the server takes from the app; what it legitimately sends is at most
a few kilobytes, and the daemon's frames to it are bounded by the socket framing."""
INPUT_KINDS = ("mouse", "wheel", "key", "text", "touch", "nav", "copy")
"""The kinds the audit counts by name. A copy is the operator reading the page's selection into their
own clipboard: the daemon answers it on this socket alone, and the audit counts it, never its text."""


def _json_object(frame: bytes, name: str, limit: int) -> dict[str, Any]:
    if len(frame) - 1 > limit:
        raise Refused(CLOSE_TOO_BIG, f"{name} carries at most {limit} bytes")
    try:
        value = json.loads(frame[1:].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Refused(CLOSE_POLICY, f"{name} is not JSON") from None
    if not isinstance(value, dict):
        raise Refused(CLOSE_POLICY, f"the JSON of {name} is an object")
    return value


def check_frame(read_only: bool, on_view: Callable[[dict[str, Any]], None] | None = None) -> Callable[[bytes, RelayResult], bytes | None]:
    """The view's judgement of what the app sends. INPUT from a read-only socket is dropped here,
    before the daemon sees it (the daemon also has the client as a watcher, and drops it again).
    ``on_view`` hears every ATTACH and VIEW: whether the view is in view is watch mode's question."""

    def check(frame: bytes, result: RelayResult) -> bytes | None:
        kind = frame[0]
        if kind == wire.ACK:
            if len(frame) != wire.ACK_SIZE:
                raise Refused(CLOSE_POLICY, f"ACK is {wire.ACK_SIZE} bytes")
            return frame
        if kind in (wire.ATTACH, wire.VIEW):
            value = _json_object(frame, "ATTACH" if kind == wire.ATTACH else "VIEW", wire.MAX_VIEW_JSON)
            if on_view is not None:
                on_view(value)
            return frame
        if kind == wire.INPUT:
            value = _json_object(frame, "INPUT", wire.MAX_INPUT_JSON)
            what = value.get("t")
            if not isinstance(what, str):
                raise Refused(CLOSE_POLICY, "an INPUT names its kind in t")
            if read_only:
                result.input_dropped += 1
                return None
            name = what if what in INPUT_KINDS else "other"
            result.counts[name] = result.counts.get(name, 0) + 1
            return frame
        raise Refused(CLOSE_POLICY, f"frame type 0x{kind:02x} is not one the app sends to a browser")

    return check


@dataclass(slots=True)
class BrowserGateway:
    """Tickets and sockets for the browsers' live views. ``service`` is looked up per call: the
    browser may not be installed here, and the routes must say so."""

    service: Callable[[], Browsers | None]
    public_url: Callable[[], str]
    ticket_ttl: Callable[[], float]
    tickets: TicketBook = field(default_factory=TicketBook)

    async def issue(self, group: str, *, read_only: bool, who: dict[str, Any]) -> dict[str, Any]:
        service = self.service()
        if service is None:
            raise EnvUnavailable("the browser is not installed in this installation")
        await service.attachable(group)
        ttl = float(self.ticket_ttl())
        return {"ticket": self.tickets.issue(group, read_only, who, ttl=ttl), "expires_in": int(ttl)}

    async def serve(self, sock: FrameSocket, group: str, *, ticket: str, origin: str | None, host: str | None, address: str) -> RelayResult | None:
        """One accepted socket from its first check to its close; ``None`` when it was refused. The
        socket is accepted before it is judged, so the app gets the close code (see the terminals')."""
        if not allowed_origin(origin, host, self.public_url()):
            await sock.close(CLOSE_ORIGIN, "this origin may not watch browsers")
            return None
        held = self.tickets.take(ticket, group)
        if held is None:
            await sock.close(CLOSE_TICKET, "the ticket is unknown, spent or expired")
            return None
        service = self.service()
        if service is None:
            await sock.close(CLOSE_UNAVAILABLE, "the browser is not installed in this installation")
            return None
        try:
            attachment = await service.attach(group, read_only=held.read_only, label=str(held.who.get("user_agent") or "")[:256], via=str(held.who.get("via") or ""))
        except NotFound:
            await sock.close(CLOSE_UNKNOWN, "no such browser")
            return None
        except EnvUnavailable as exc:
            await sock.close(CLOSE_UNAVAILABLE, close_reason(exc.message))
            return None
        except BrowserError as exc:
            await sock.close(CLOSE_INTERNAL, close_reason(exc.message))
            return None
        started = time.monotonic()
        await self._audit(service, attachment, "view.attach", {**held.who, "socket_address": address, "read_only": held.read_only, "client_id": attachment.client_id})
        result = RelayResult(code=CLOSE_INTERNAL, reason="the relay was interrupted", ended_by="service")
        token = service.watch(group)

        def seen(view: dict[str, Any]) -> None:
            # A view the app hides (its page is in the background) says so; one that says nothing is
            # in view, as a freshly opened one is.
            if "hidden" in view:
                service.set_watch(group, token, not bool(view.get("hidden")))

        try:
            result = await relay(
                sock,
                attachment.channel,
                check=check_frame(held.read_only, seen),
                service_alive=lambda: attachment.service_alive,
                target="view",
                closed_text="the browser closed this view",
                gone_text="the browser service went away",
            )
        finally:
            service.unwatch(group, token)
            await self._audit(
                service,
                attachment,
                "view.detach",
                {
                    "client_id": attachment.client_id,
                    "inputs": dict(result.counts),
                    "input_dropped": result.input_dropped,
                    "frames_out": result.frames_out,
                    "bytes_out": result.bytes_out,
                    "seconds": round(time.monotonic() - started, 1),
                    "ended_by": result.ended_by,
                    "code": result.code,
                },
            )
        return result

    @staticmethod
    async def _audit(service: Browsers, attachment: Attachment, action: str, detail: dict[str, Any]) -> None:
        try:
            await service.audit(attachment.group_id, attachment.env, "operator", action, detail)
        except Exception:  # noqa: BLE001 — a failed audit write must not end or strand a person's view
            logger.exception("could not write the %s of browser %s to the audit", action, attachment.group_id)


__all__ = ["BROWSER_WS_MAX_BYTES", "BrowserGateway", "check_frame"]
