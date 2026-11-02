"""Remote conversations in the session manager: whose hands a session has, and turns that wait for them.

A conversation's hands are on a device when the pairing's store says it is that device's home
(``cli_device_sessions``, role ``home``, with the folder), or when it is a subagent of one that is:
the subagent works in the same folder of the same machine. The manager asks
:meth:`RemoteTurns.home_of` while it builds a session's services, which is synchronous, so the homes
are read once at start (:meth:`RemoteTurns.load`) and kept here; whatever binds a session or moves its
hands afterwards tells :meth:`RemoteTurns.bind`, which also points a loaded session there at once.

When the device is away a tool call parks (``daedalus.remote.hands``) and the run stops waiting on
an ``external_call`` interrupt. The manager records the wait like a question (one
``pending_questions`` row, kind ``external_call``, so a restart restores it) and reports it to the
control book (:meth:`RemoteTurns.waiting`), which announces ``remote.waiting`` and answers the session
view. When the device connects, :meth:`RemoteTurns.device_connected` asks the manager
to go on with every turn of that device that waits; the operator can end one with Stop instead.

The timeout policy is: **none**. A parked turn waits until its terminal comes back or the operator
stops it, however long that takes. It holds nothing open on the server meanwhile (the run ended,
parked, and its snapshot is on disk), it is announced, the session view and the session list say so,
and a restart keeps it. A clock would fail a turn the operator may well come back to tomorrow, which
is the case this exists for; forgetting it is prevented by showing it, not by ending it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.remote.hands import DeviceExecBackend
from daedalus.remote.journal import Journal
from daedalus.remote.link import DeviceLink
from daedalus.remote.relay import DeviceHub

if TYPE_CHECKING:
    from daedalus.host.services import SessionServices
    from daedalus.host.session_runner import SessionManager, SessionState

logger = logging.getLogger(__name__)

EXTERNAL_CALL = "external_call"
"""The kind of a ``pending_questions`` row that is a turn waiting for its terminal, not a question."""
SUBAGENT_DEPTH = 3

PLAN_LINE = (
    "- The operator's terminal is in plan mode: it refuses writes and commands that change things on their machine. "
    "Read, investigate and plan; say what you would change instead of trying to change it."
)
ASK_LINE = "- The operator's terminal asks them before each write and each command; a refusal is their answer, not an error."


Homes = Callable[[], Awaitable[dict[str, tuple[str, str]]]]
"""Every session's home, as ``{session_id: (device_id, cwd)}``: the pairing's store, read at start."""


class RemoteTurns:
    def __init__(self, manager: SessionManager, hub: DeviceHub, journal: Journal, *, homes: Homes | None = None) -> None:
        self.manager = manager
        self.hub = hub
        self.journal = journal
        self._read_homes = homes
        self._homes: dict[str, tuple[str, str]] = {}
        self._output_tails: dict[str, asyncio.Task[None]] = {}
        self._rebinds: set[asyncio.Task[None]] = set()
        hub.on_connect.append(self.device_connected)

    async def load(self) -> None:
        """Read every home from the pairing's store, before any session is loaded."""
        if self._read_homes is not None:
            self._homes = dict(await self._read_homes())

    # -- whose hands -----------------------------------------------------------------------------

    def home_of(self, state: SessionState) -> dict[str, str] | None:
        """``{device_id, cwd}`` of the session's hands, its own or its leader's, or ``None``."""
        session_id = state.session.id
        metadata: dict[str, Any] = dict(state.session.metadata)
        for _ in range(SUBAGENT_DEPTH + 1):
            home = self._homes.get(session_id)
            if home is not None:
                return {"device_id": home[0], "cwd": home[1]}
            session_id = str(metadata.get("subagent_of") or "")
            parent = self.manager.live_state(session_id) if session_id else None
            if parent is None:
                return None
            metadata = dict(parent.session.metadata)
        return None

    def device_of(self, session_id: str) -> str | None:
        """The device a session's own hands are on, by id alone (no leader is looked up)."""
        home = self._homes.get(session_id)
        return home[0] if home is not None else None

    def drive(self, services: SessionServices, state: SessionState, home: dict[str, str]) -> None:
        """Point a session's tools at its device, as a host-folder session's are pointed at the host:
        commands and files through the relay, relative paths under the bound folder, no walls of this
        process. The terminal's own jail and guard are what fence it in, and the server cannot widen them."""
        session_id = state.session.id
        link = self.hub.link_of(home["device_id"])
        services.exec_backend = DeviceExecBackend(
            self.hub,
            self.journal,
            device_id=home["device_id"],
            device_name=link.name if link is not None else "",
            session_id=session_id,
            root=home["cwd"],
            turn_active=self._turn_active,
            on_output=self.output,
        )
        services.workspace_dir = Path(home["cwd"])
        services.extra["local_home"] = state.workspace
        services.walls = None
        services.writable = []
        services.log_root = None
        services.remote_readonly = False

    def _turn_active(self, session_id: str) -> bool:
        state = self.manager.live_state(session_id)
        if state is None:
            return False
        if state.running:
            return True
        # A subagent's tools run inside its leader's turn.
        leader = str(state.session.metadata.get("subagent_of") or "")
        parent = self.manager.live_state(leader) if leader else None
        return parent is not None and parent.running

    def bind(self, session_id: str, device_id: str, cwd: str) -> None:
        """``device_id`` and its folder ``cwd`` are now the hands of a session: told by whatever wrote
        the home into the pairing's store (a new remote conversation, or one continued on another
        machine). A loaded session is pointed there at once; its next call goes to the new device."""
        if not cwd.strip():
            raise ValueError("a remote conversation needs the folder it works in")
        before = self._homes.get(session_id)
        self._homes[session_id] = (device_id, cwd)
        state = self.manager.live_state(session_id)
        if state is not None:
            self.manager.register_services(state)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # bound before the host runs: the links carry it from their hello
            return
        for device in {device_id, *((before[0],) if before is not None else ())}:
            task = loop.create_task(self.hub.rebound(device), name=f"relay-rebound:{device}")
            self._rebinds.add(task)
            task.add_done_callback(self._rebinds.discard)

    async def bound_to(self, device_id: str) -> list[dict[str, str]]:
        """The conversations whose own hands are on ``device_id``, for ``device.welcome``."""
        return [{"session_id": session_id, "cwd": cwd} for session_id, (device, cwd) in self._homes.items() if device == device_id]

    # -- waiting for the terminal ----------------------------------------------------------------

    async def waiting(self, session_id: str, pending_payload: dict[str, Any], *, waiting: bool) -> None:
        """Tell the control book a turn waits for its terminal (with its calls), or no longer does."""
        device_id = str(pending_payload.get("device_id") or "")
        calls = [dict(call) for call in pending_payload.get("calls") or []] if waiting else None
        try:
            await self.hub.control.set_waiting(session_id, device_id, calls, since=str(pending_payload.get("since") or ""))
        except Exception:  # noqa: BLE001 — the wait is recorded in the session; only its announcement failed
            logger.warning("session %s: could not announce its wait", session_id, exc_info=True)

    @staticmethod
    def parked(pending_payload: dict[str, Any] | None, call: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        """The wait's record with one more parked call: a batch parks every call of the message."""
        record = dict(pending_payload or {})
        record.setdefault("device_id", str(call.get("device_id") or ""))
        record.setdefault("device_name", str(call.get("device_name") or ""))
        record.setdefault("since", datetime.now(UTC).isoformat())
        calls = [dict(item) for item in record.get("calls") or [] if item.get("tool_call_id") != tool_call_id]
        calls.append({"tool_call_id": tool_call_id, "tool": str(call.get("tool") or ""), "delivery": str(call.get("delivery") or "unknown")})
        record["calls"] = calls
        return record

    async def device_connected(self, link: DeviceLink) -> None:
        """A device came back: every turn that waits for it goes on, in no particular order between
        conversations and in the model's order within one."""
        for session_id, state in list(self.manager.loaded_states()):
            pending = state.pending
            if pending is None or pending.kind != EXTERNAL_CALL or state.running:
                continue
            home = self.home_of(state)
            if home is None or home["device_id"] != link.device_id:
                continue
            try:
                await self.manager.resume_remote(session_id)
            except Exception:  # noqa: BLE001 — one conversation that cannot go on must not hold back the rest
                logger.exception("session %s: could not go on after its terminal reconnected", session_id)

    # -- what the terminal says while a command runs ----------------------------------------------

    def output(self, session_id: str, tool_call_id: str, text: str) -> None:
        """A running command's output, to the session's open screens, in the order it came. Live only:
        the tool's result carries the output that counts, and a reconnecting screen does not replay it."""
        previous = self._output_tails.get(session_id)
        task = asyncio.get_running_loop().create_task(self._emit_output(previous, session_id, tool_call_id, text))
        self._output_tails[session_id] = task
        task.add_done_callback(lambda done, sid=session_id: self._output_tails.pop(sid, None) if self._output_tails.get(sid) is done else None)

    async def _emit_output(self, previous: asyncio.Task[None] | None, session_id: str, tool_call_id: str, text: str) -> None:
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        try:
            await self.manager.remote_output(session_id, tool_call_id, text)
        except Exception:  # noqa: BLE001 — a screen that misses a line still gets the result
            logger.debug("session %s: could not pass on output", session_id, exc_info=True)

    # -- the guard --------------------------------------------------------------------------------

    def guard_of(self, state: SessionState) -> str:
        home = self.home_of(state)
        return self.hub.guard(home["device_id"], state.session.id) if home is not None else ""

    async def guard_line(self, state: SessionState) -> str:
        """The turn context's line on the operator's local guard: nothing under ``auto``. The same
        words while the guard stays, so it costs no cache miss."""
        guard = self.guard_of(state)
        if guard == "plan":
            return PLAN_LINE
        if guard == "ask":
            return ASK_LINE
        return ""


def subscribe(manager: SessionManager, remote: RemoteTurns) -> Callable[[], None]:
    """Wire ``remote`` into ``manager``; the returned callable undoes it (for the tests)."""
    manager.remote = remote
    manager.turn_line_hooks.append(remote.guard_line)

    def undo() -> None:
        manager.remote = None
        if remote.guard_line in manager.turn_line_hooks:
            manager.turn_line_hooks.remove(remote.guard_line)

    return undo


__all__ = ["ASK_LINE", "EXTERNAL_CALL", "PLAN_LINE", "RemoteTurns", "subscribe"]
