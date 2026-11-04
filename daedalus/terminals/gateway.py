"""The app's way to a terminal: which frames a terminal's socket carries, and its tickets and audit.

The tickets, the Origin rule and the relay are shared with the browser's live view
(``daedalus.gateway``); what is a terminal's own is the frame check below — the browser frames of
``docs/architecture/terminals.md`` — and the audit of a host terminal's attachments.

Nothing here imports the web framework (only the API extension may); the routes hand this module a
``FrameSocket`` and it does the rest.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

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
    SocketGone,
    TicketBook,
    allowed_origin,
    close_reason,
    relay,
    ticket_who,
)
from daedalus.terminals import wire
from daedalus.terminals.model import EnvUnavailable, NotFound, TerminalError

if TYPE_CHECKING:
    from daedalus.terminals.service import Attachment, Terminals

logger = logging.getLogger(__name__)

TERMINAL_WS_MAX_BYTES = 1 << 20
"""The largest WebSocket message a terminal socket takes, held there by the API's socket limits
(``daedalus/extensions/socket_limits.py``, whose default it is). The largest frame a browser legitimately sends is
32 KiB of typing; the daemon's frames to the browser are bounded by its own 1 MiB framing."""

MAX_INPUT_BYTES = 32 << 10
MAX_ATTACH_JSON_BYTES = 4 << 10
FIXED_SIZES = {wire.RESIZE: 9, wire.ACK: 9}
"""Frames whose length is their definition, type byte included."""

AUDITED_ENVS = frozenset({"host"})
"""Where attaching and detaching are written to the audit: a terminal on the operator's own machine,
which is always marked and always accounted for. A container terminal's attachments are ordinary
use of a sandboxed shell and would only bury the host's entries."""


def check_frame(read_only: bool) -> Callable[[bytes, RelayResult], bytes | None]:
    """The terminal's judgement of what a browser sends: typing (counted, dropped when read-only), a
    resize, an acknowledgement and the attach, each at its size; anything else is refused."""

    def check(frame: bytes, result: RelayResult) -> bytes | None:
        kind = frame[0]
        if kind == wire.INPUT:
            if len(frame) - 1 > MAX_INPUT_BYTES:
                raise Refused(CLOSE_TOO_BIG, f"INPUT carries at most {MAX_INPUT_BYTES} bytes")
            if read_only:
                result.input_dropped += len(frame) - 1
                return None
            result.bytes_typed += len(frame) - 1
            return frame
        if kind in FIXED_SIZES:
            if len(frame) != FIXED_SIZES[kind]:
                raise Refused(CLOSE_POLICY, f"frame 0x{kind:02x} is {FIXED_SIZES[kind]} bytes")
            if kind == wire.ACK:
                try:
                    wire.decode_browser(frame)
                except wire.FrameError as exc:
                    raise Refused(CLOSE_POLICY, str(exc)) from None
            return frame
        if kind == wire.ATTACH:
            if len(frame) - 1 > MAX_ATTACH_JSON_BYTES:
                raise Refused(CLOSE_TOO_BIG, f"ATTACH carries at most {MAX_ATTACH_JSON_BYTES} bytes")
            try:
                decoded = wire.decode_browser(frame)
            except wire.FrameError as exc:
                raise Refused(CLOSE_POLICY, f"ATTACH: {exc}") from None
            if read_only and decoded.json is not None and decoded.json.get("readOnly") is not True:
                # The daemon already has the client as read-only; saying it in the frame as well keeps a
                # browser from believing, even for a moment, that it may type.
                return wire.encode_browser(wire.BrowserFrame("attach", json={**decoded.json, "readOnly": True}))
            return frame
        raise Refused(CLOSE_POLICY, f"frame type 0x{kind:02x} is not one a browser sends")

    return check


@dataclass(slots=True)
class Gateway:
    """Tickets and sockets for one application. ``service`` is looked up per call, because the
    terminals extension may be missing (not installed, failed to start) and the routes must say so."""

    service: Callable[[], Terminals | None]
    public_url: Callable[[], str]
    ticket_ttl: Callable[[], float]
    tickets: TicketBook = field(default_factory=TicketBook)

    async def issue(self, terminal_id: str, *, read_only: bool, who: dict[str, Any]) -> dict[str, Any]:
        """A ticket for a terminal that exists and whose environment answers; raises the refusal."""
        service = self.service()
        if service is None:
            raise EnvUnavailable("the terminals subsystem is not running")
        await service.attachable(terminal_id)
        ttl = float(self.ticket_ttl())
        return {"ticket": self.tickets.issue(terminal_id, read_only, who, ttl=ttl), "expires_in": int(ttl)}

    async def serve(self, sock: FrameSocket, terminal_id: str, *, ticket: str, origin: str | None, host: str | None, address: str) -> RelayResult | None:
        """One accepted socket from its first check to its close; ``None`` when it was refused.

        The socket is accepted before it is judged: a WebSocket refused during the handshake reaches
        the browser as a bare 1006, and the app needs the code to know whether to try again.
        """
        if not allowed_origin(origin, host, self.public_url()):
            await sock.close(CLOSE_ORIGIN, "this origin may not open terminals")
            return None
        held = self.tickets.take(ticket, terminal_id)
        if held is None:
            await sock.close(CLOSE_TICKET, "the ticket is unknown, spent or expired")
            return None
        service = self.service()
        if service is None:
            await sock.close(CLOSE_UNAVAILABLE, "the terminals subsystem is not running")
            return None
        via = str(held.who.get("via") or "")
        try:
            attachment = await service.attach(terminal_id, read_only=held.read_only, label=str(held.who.get("user_agent") or "")[:256], via=via)
        except NotFound:
            await sock.close(CLOSE_UNKNOWN, "no such terminal")
            return None
        except EnvUnavailable as exc:
            await sock.close(CLOSE_UNAVAILABLE, close_reason(exc.message))
            return None
        except TerminalError as exc:
            await sock.close(CLOSE_INTERNAL, close_reason(exc.message))
            return None
        started = time.monotonic()
        await self._audit(service, attachment, "attach", {**held.who, "socket_address": address, "read_only": held.read_only, "client_id": attachment.client_id})
        result = RelayResult(code=CLOSE_INTERNAL, reason="the relay was interrupted", ended_by="service")
        try:
            result = await relay(sock, attachment.channel, check=check_frame(held.read_only), service_alive=lambda: attachment.service_alive)
        finally:
            await self._audit(
                service,
                attachment,
                "detach",
                {
                    "client_id": attachment.client_id,
                    "bytes_typed": result.bytes_typed,
                    "input_dropped": result.input_dropped,
                    "bytes_out": result.bytes_out,
                    "seconds": round(time.monotonic() - started, 1),
                    "ended_by": result.ended_by,
                    "code": result.code,
                },
            )
        return result

    @staticmethod
    async def _audit(service: Terminals, attachment: Attachment, action: str, detail: dict[str, Any]) -> None:
        if attachment.env not in AUDITED_ENVS:
            return
        try:
            await service.audit(attachment.terminal_id, attachment.env, "operator", action, detail)
        except Exception:  # noqa: BLE001 — a failed audit write must not end or strand a person's terminal
            logger.exception("could not write the %s of terminal %s to the audit", action, attachment.terminal_id)


__all__ = [
    "AUDITED_ENVS",
    "TERMINAL_WS_MAX_BYTES",
    "FrameSocket",
    "Gateway",
    "RelayResult",
    "SocketGone",
    "TicketBook",
    "allowed_origin",
    "check_frame",
    "ticket_who",
]
