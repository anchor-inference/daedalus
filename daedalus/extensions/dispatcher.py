"""The main orchestrator: one session the operator opens to say "in project X, do Y".

``app.extensions["dispatcher"]`` holds the office of the one main orchestrator of the installation,
its tools' operations, what wakes it and what it sees. It is a router, not a worker: it turns the
operator's words into a dispatch for a project (:mod:`daedalus.extensions.dispatches`) and follows
it; it never touches files, staff or a board, and never blocks on a question.

The office works like a project orchestrator's: the kv entry names the one session that holds it and
only a compare-and-set under a lock changes it, so a replaced main orchestrator whose engine still
runs is refused at its first tool call, and the successor's first state names its predecessor.

It is woken by three things only — a dispatch closed by its project, a progress report on a dispatch,
a dispatch gone quiet — plus the operator's answer to a confirmation it asked for. Nothing it does
wakes it, and nothing a project does outside a report on a dispatch does either.

Questions a project puts to the operator about a dispatch are shown in its chat without waking its
model: the app draws them from the request rows (:meth:`Dispatcher.view`). Its ``Answer`` passes on
an answer the operator gave in its chat, and only by quoting the operator's latest message.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from protocore.contracts.types import MessageRole, TextBlock

from daedalus.extensions.dispatcher_telegram import DispatcherTelegram
from daedalus.extensions.dispatches import SETUP_BY, Dispatches
from daedalus.extensions.notifications import Draft
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.peek import PeekRefused
from daedalus.host.wake_queue import Batch, TargetState, Wake, WakeQueue
from daedalus.stores.dispatches import Dispatch, DispatchError
from daedalus.stores.files import HANDOVER_MAX_FILES, MAIN, FileRefused, StoredFile, human_size, refs_line
from daedalus.stores.projects import BRIEF_SECTIONS, Project, ProjectError
from daedalus.stores.staff import Ask, StaffError
from daedalus.tools.vision import VisionUnavailable, kept_body

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.host.session_runner import SessionManager, SessionState

logger = logging.getLogger(__name__)

SESSION_KEY = "dispatcher.session"
"""kv: the id of the session that holds the main orchestrator's office."""
CURSOR_KEY = "dispatcher.cursor"
PROJECT_KIND = "dispatcher"
PROJECT_DIR = "main"
TITLE = "Main"
WAKE_TYPES = ("dispatch.closed", "dispatch.stalled", "dispatch.message", "ask.answered")
PROGRESS_KINDS = frozenset({"progress", "decision"})
ANSWERED_KEEP_HOURS = 24
"""How long an answered card stays in the chat as its one line, so the operator sees where it went."""
QUOTE_MIN = 2
OPERATOR_TAIL = 60
"""How far back the latest operator message is looked for; a turn never runs that long without one."""



class NotCurrent(RuntimeError):
    """This session does not hold the main orchestrator's office (any more)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _whole(text: str) -> str:
    """A project's report or result for an event line, whole and with its line breaks.

    Cut at 600 and 800 characters, an orchestrator's detailed answer reached the main agent as a stub
    it could only guess the rest of. Lines after the first are indented so they stay with their event.
    """
    return "\n  ".join(line.rstrip() for line in (text or "").strip().splitlines())


def _one_line(text: str, limit: int) -> str:
    flat = " / ".join(line.strip() for line in (text or "").strip().splitlines() if line.strip())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _normalised(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _age(at: str | None, now: datetime) -> str:
    if not at:
        return "never"
    try:
        then = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    then = then if then.tzinfo else then.replace(tzinfo=UTC)
    minutes = max(0, int((now - then).total_seconds() // 60))
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} d"


class Dispatcher:
    """The installation's main orchestrator."""

    def __init__(self, app: Application) -> None:
        self.app = app
        assert app.manager is not None
        self.manager: SessionManager = app.manager
        self._lock = asyncio.Lock()
        self.queue: WakeQueue | None = None
        self.clock: Callable[[], float] | None = None
        """A fake monotonic clock for the wake queue, in tests."""
        self._id: str | None = None

    @property
    def dispatches(self) -> Dispatches:
        found: Any = self.app.extensions.get("dispatches")
        if found is None:
            raise RuntimeError("dispatches are not running on this installation")
        return found  # type: ignore[no-any-return]

    @property
    def orchestrators(self) -> Any:
        return self.app.extensions.get("orchestrator")

    # -- the office ----------------------------------------------------------------------------

    async def session_id(self) -> str:
        """The session holding the office now; empty when there is none yet."""
        if self._id is None:
            self._id = str(await self.manager.db.kv_get(SESSION_KEY, "") or "")
        return self._id

    async def ensure(self) -> str:
        """The main orchestrator's session, made the first time anyone opens it."""
        async with self._lock:
            current = await self.session_id()
            if current and await self.manager.get_state(current) is not None:
                return current
            made = await self._new_session(predecessor=current, reason="its session could not be loaded" if current else "")
            if not await self._swap(current, made):
                await self.manager.delete_session(made)
                self._id = None
                return await self.session_id()
            if current:
                await self._retire(current, successor=made)
            await self._took_office()
            return made

    async def replace(self, reason: str, *, by: str = "operator") -> str:
        """A new main orchestrator session in place of the current one, linked both ways, never silently."""
        why = " ".join((reason or "").split())[:300] or "replaced"
        async with self._lock:
            old = await self.session_id()
            made = await self._new_session(predecessor=old, reason=why)
            if not await self._swap(old, made):
                await self.manager.delete_session(made)
                self._id = None
                return await self.session_id()
            if old:
                await self._retire(old, successor=made)
            await self._took_office()
            await self._publish("project.changed", {"change": "dispatcher.replaced", "actor": by}, None)
            return made

    async def current(self, session_id: str) -> None:
        """Nothing when this session holds the office; :class:`NotCurrent` saying who does otherwise."""
        holder = await self.session_id()
        if holder and holder == session_id:
            return
        state = await self.manager.get_state(session_id)
        was = state is not None and bool(state.metadata.get("dispatcher") or state.metadata.get("dispatcher_retired"))
        if was and holder:
            raise NotCurrent(f"this main orchestrator was replaced by {holder}; its tools no longer act")
        if was:
            raise NotCurrent("the main orchestrator's office is empty; the operator opens a new one from the app")
        raise NotCurrent("this session is not the main orchestrator")

    async def _swap(self, expect: str, value: str) -> bool:
        """Compare-and-set of the office in one statement: two starts cannot both win."""
        async with self.manager.db.transaction() as conn:
            if expect:
                cursor = await conn.execute("UPDATE kv SET value = ? WHERE key = ? AND value = ?", (json.dumps(value), SESSION_KEY, json.dumps(expect)))
            else:
                cursor = await conn.execute(
                    "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value WHERE kv.value IN ('\"\"', 'null')",
                    (SESSION_KEY, json.dumps(value)),
                )
            won = cursor.rowcount == 1
            await cursor.close()
        if won:
            self._id = value
        return won

    async def project(self) -> Project:
        """The installation's own project the main orchestrator's chat lives in; it has no files to work on."""
        return await self.manager.projects.ensure_system(PROJECT_KIND, name=TITLE, root=self.manager.settings.workspaces_dir / PROJECT_DIR)

    async def _new_session(self, *, predecessor: str, reason: str) -> str:
        metadata: dict[str, Any] = {"dispatcher": True}
        if not getattr(self.app, "front", None):
            # No bot, no Telegram window: nothing to deliver to, and nothing that could leak there later.
            metadata["telegram_detached"] = True
        if predecessor:
            metadata["predecessor"] = predecessor
            metadata["predecessor_reason"] = reason
        project = await self.project()
        state = await self.manager.create_session(TITLE, metadata=metadata, project_id=project.id)
        return state.session.id

    async def _retire(self, session_id: str, *, successor: str) -> None:
        state = await self.manager.get_state(session_id)
        if state is None:
            return
        for metadata in (state.metadata, state.session.metadata):
            metadata.pop("dispatcher", None)
            metadata["dispatcher_retired"] = True
            # A retired main orchestrator is off Telegram: its successor has General and the header.
            metadata["telegram_detached"] = True
            if successor:
                metadata["successor"] = successor
        await self.manager.sessions.update_metadata(session_id, state.session.metadata)
        await self.manager.stop(session_id)

    async def _took_office(self) -> None:
        await self._sync_model()
        # A new office starts from now: reports that arrived before it are in Progress, not news.
        await self.manager.db.kv_set(CURSOR_KEY, self.manager.bus.head)
        await self.start_queue()

    # -- the model -----------------------------------------------------------------------------

    def preset_for(self, state: SessionState) -> str | None:
        """The manager's preset hook: the main orchestrator runs the Settings choice, whatever the chat was set to."""
        if not state.metadata.get("dispatcher"):
            return None
        return self.manager.config.dispatcher_preset()

    async def _sync_model(self) -> None:
        """Write the effective preset on the session, so the chat's model chip shows what it runs; a
        change made in Settings reaches the chip at the next turn."""
        session_id = await self.session_id()
        preset = self.manager.config.dispatcher_preset()
        if not session_id or not preset:
            return
        if (await self.manager.live.load(session_id)).get("preset") == preset:
            return
        try:
            await self.manager.set_model(session_id, preset=preset)
        except (KeyError, ValueError):
            logger.warning("could not set the main orchestrator's model", exc_info=True)

    async def model_chosen(self, session_id: str, preset: str | None, *, clear: bool = False) -> bool:
        """The composer's model chip on the main chat writes the Settings choice; whether it was that chat."""
        state = await self.manager.get_state(session_id)
        if state is None or not state.metadata.get("dispatcher"):
            return False
        if not clear and not preset:
            return False
        if preset and preset not in self.manager.config.presets:
            raise ValueError(f"no such model preset {preset!r}")
        config = getattr(self.app, "config", None) or self.manager.config
        raw = config.model_dump(mode="json")
        raw["dispatcher"]["preset"] = "" if clear else str(preset)
        save = getattr(self.app, "save_config", None)
        if save is not None:
            await save(type(config).model_validate(raw))
        else:
            self.manager.config.dispatcher.preset = raw["dispatcher"]["preset"]
        await self._sync_model()
        return True

    # -- what it sees ----------------------------------------------------------------------------

    async def turn_notes(self, state: SessionState) -> str | None:
        """The manager's turn-notes hook: the open dispatches and the questions waiting, every turn."""
        if not (state.metadata.get("dispatcher") or state.metadata.get("dispatcher_retired")):
            return None
        try:
            await self.current(state.session.id)
        except NotCurrent as exc:
            return f"\n- {exc}. Tell the operator, and do nothing else."
        await self._sync_model()
        text = await self.state_text(state)
        if state.metadata.get("predecessor") and not state.metadata.get("predecessor_announced"):
            for metadata in (state.metadata, state.session.metadata):
                metadata["predecessor_announced"] = True
            await self.manager.sessions.update_metadata(state.session.id, state.session.metadata)
        return "\n" + text

    async def state_text(self, state: SessionState | None = None) -> str:
        now = _now()
        lines: list[str] = []
        if state is not None and state.metadata.get("predecessor") and not state.metadata.get("predecessor_announced"):
            lines.append(f"You replace the main orchestrator session {state.metadata['predecessor']}: {state.metadata.get('predecessor_reason') or 'replaced'}. Progress() shows what it handed over.")
        projects = {p.id: p for p in await self.manager.projects.list()}
        workable = [p for p in projects.values() if not p.settings.system and not p.settings.ephemeral]
        lines.append("Projects: " + (" · ".join(f"{p.name}{' (orchestrator on)' if p.settings.orchestrator.enabled else ''}{' (setting up)' if p.setup_by == SETUP_BY else ''}" for p in workable) or "none yet"))
        open_dispatches = await self.manager.dispatches.open_for()
        blocked = [d for d in await self.manager.dispatches.recent(limit=40) if d.status == "blocked"]
        shown = open_dispatches + blocked
        if shown:
            lines.append("Open dispatches:")
            for d in shown[:15]:
                name = projects[d.project_id].name if d.project_id in projects else d.project_id
                lines.append(f"  - [{d.id}] {name} #{d.seq} {d.status}, {_age(d.created_at, now)}: {_one_line(d.title or d.text, 120)}")
            if len(shown) > 15:
                lines.append(f"  - … {len(shown) - 15} more (Progress)")
        else:
            lines.append("Open dispatches: none")
        pending = [a for a in await self.mirrored(open_only=True)]
        if pending:
            lines.append("Questions waiting for the operator (shown to them as cards here; answer one only with Answer, quoting their latest message):")
            for ask in pending[:10]:
                name = projects[ask.project_id].name if ask.project_id in projects else "a new project"
                options = " / ".join(str(o) for o in ask.detail.get("options") or [])
                lines.append(f"  - [{ask.short_id}] {name} ({ask.kind}): {_one_line(ask.text, 200)}{' — options: ' + options if options else ''}")
        text = "\n".join(lines)
        limit = self.manager.config.dispatcher.state_max_chars
        return text if len(text) <= limit else text[: limit - 60] + "\n[… cut here; Progress() and Projects() show the rest]"

    # -- the questions shown in its chat ----------------------------------------------------------

    async def mirrored(self, *, open_only: bool) -> list[Ask]:
        """Every request shown in the main chat: its own confirmations, and the requests to the operator
        linked to a dispatch that is still going. Oldest first. With ``open_only`` false, requests
        answered in the last day come too, for the one line that says where they were answered."""
        going = [d.id for d in await self.manager.dispatches.recent(limit=200) if d.status in ("open", "blocked") or not open_only]
        asks = [a for a in await self.manager.asks.of_dispatches(going, open_only=open_only) if a.routed_to == "operator"]
        asks += await self.manager.asks.of_origin("dispatcher", open_only=open_only)
        if not open_only:
            horizon = _now().timestamp() - ANSWERED_KEEP_HOURS * 3600
            asks = [a for a in asks if a.open or _stamp(a.resolved_at) >= horizon]
        unique = {a.id: a for a in asks}
        return sorted(unique.values(), key=lambda a: a.created_at)

    async def is_mirrored(self, ask: Ask) -> bool:
        if ask.origin == "dispatcher":
            return True
        if not ask.dispatch_id or ask.routed_to != "operator":
            return False
        dispatch = await self.manager.dispatches.get(ask.dispatch_id)
        return dispatch is not None and dispatch.status in ("open", "blocked")

    async def host_level(self, ask: Ask) -> bool:
        """Whether a request acts on the host itself. The operator's rule: such a request is answered in
        the app alone, where the whole of it is shown, never from Telegram or a lock screen."""
        if ask.kind == "project":
            return any(str(f.get("env") or "") == "host" for f in ask.detail.get("folders") or [] if isinstance(f, dict))
        if ask.kind == "folder":
            return str(ask.detail.get("env") or "") == "host"
        if ask.kind != "permission" or not ask.staff_id:
            return False
        member = await self.manager.staff.get(ask.staff_id)
        if member is not None and member.env == "host":
            return True
        if ask.staff_session_id:
            row = await self.manager.db.fetchone(
                "SELECT f.env FROM staff_sessions s JOIN project_folders f ON f.id = s.folder_id WHERE s.id = ?", (ask.staff_session_id,)
            )
            return row is not None and row["env"] == "host"
        return False

    async def request_link(self, request_ref: str) -> str | None:
        """The notification router's link hook: a mirrored request opens the main chat."""
        ask = await self._ask_by_ref(request_ref)
        if ask is not None and await self.is_mirrored(ask):
            return "/app/main"
        return None

    async def _ask_by_ref(self, ref: str) -> Ask | None:
        if not ref:
            return None
        row = await self.manager.db.fetchone("SELECT id FROM asks WHERE json_extract(detail_json, '$.event_ref') = ? ORDER BY created_at DESC LIMIT 1", (ref,))
        return await self.manager.asks.get(row["id"]) if row is not None else None

    async def view(self) -> dict[str, Any]:
        """What the app draws of the main chat: the session, the dispatches, the cards."""
        session_id = await self.session_id()
        projects = {p.id: p for p in await self.manager.projects.list()}
        recent = await self.manager.dispatches.recent(limit=40)
        last = await self.manager.dispatches.last_messages([d.id for d in recent])
        asks = await self.mirrored(open_only=False)
        members: dict[str, str] = {}
        host: dict[str, bool] = {}
        for ask in asks:
            if ask.staff_id and ask.staff_id not in members:
                member = await self.manager.staff.get(ask.staff_id)
                members[ask.staff_id] = member.name if member is not None else ""
            host[ask.id] = await self.host_level(ask)

        def dispatch_view(d: Dispatch) -> dict[str, Any]:
            said = last.get(d.id)
            return {**d.view(), "project_name": projects[d.project_id].name if d.project_id in projects else "", "last": said.view() if said is not None else None}

        def ask_view(a: Ask) -> dict[str, Any]:
            return {
                **a.view(),
                "project_name": projects[a.project_id].name if a.project_id and a.project_id in projects else str(a.detail.get("name") or ""),
                "asker": "main" if a.origin == "dispatcher" else "orchestrator" if a.origin == "orchestrator" else members.get(a.staff_id or "", "") or "staff",
                "host": host.get(a.id, False),
            }

        return {
            "session_id": session_id,
            "dispatches": [dispatch_view(d) for d in recent],
            "asks": [ask_view(a) for a in asks],
            "questions": sum(1 for a in asks if a.open),
            "setup": [{"project_id": p.id, "name": p.name} for p in projects.values() if p.setup_by == SETUP_BY],
        }

    # -- wake-ups -------------------------------------------------------------------------------

    async def start_queue(self) -> WakeQueue:
        if self.queue is not None and not self.queue.closed:
            return self.queue

        async def load_cursor() -> int | None:
            value = await self.manager.db.kv_get(CURSOR_KEY)
            return int(value) if isinstance(value, int) else None

        async def save_cursor(seq: int) -> None:
            await self.manager.db.kv_set(CURSOR_KEY, int(seq))

        extra: dict[str, Any] = {"clock": self.clock} if self.clock is not None else {}
        queue = WakeQueue(
            name="dispatcher",
            bus=self.manager.bus,
            flt=EventFilter(types=WAKE_TYPES),
            classify=self.classify,
            render=self.render,
            state=self.target_state,
            deliver=self.deliver,
            load_cursor=load_cursor,
            save_cursor=save_cursor,
            batch_seconds=lambda: float(self.manager.config.dispatcher.batch_seconds),
            max_wakes_per_hour=lambda: self.manager.config.dispatcher.max_wakes_per_hour,
            on_capped=self._capped,
            **extra,
        )
        self.queue = queue
        await queue.start()
        return queue

    async def stop_queue(self) -> None:
        queue, self.queue = self.queue, None
        if queue is not None:
            await queue.close()

    async def classify(self, event: AppEvent) -> Wake | None:
        """What wakes the main orchestrator: a report on a dispatch, a dispatch gone quiet, and the
        operator's answer to its own confirmation. Its own doing never does."""
        p = event.payload
        if event.type.startswith("dispatch.") and (p.get("actor") in ("dispatcher", "operator") or p.get("by") in ("dispatcher", "operator")):
            # Its own doing, and what the operator did themselves (a dispatch cancelled from its card),
            # is nothing to tell the operator about.
            return None
        if event.type == "dispatch.closed":
            return Wake(f"dispatch:{p.get('dispatch_id')}", urgent=True)
        if event.type == "dispatch.stalled":
            return Wake(f"stalled:{p.get('dispatch_id')}", urgent=True)
        if event.type == "dispatch.message":
            if p.get("author") != "orchestrator" or p.get("kind") not in PROGRESS_KINDS:
                return None
            return Wake(f"progress:{event.seq}")
        if event.type == "ask.answered":
            ask = await self._ask_by_ref(str(p.get("request_ref") or ""))
            if ask is None or ask.origin != "dispatcher":
                return None
            return Wake(f"answer:{ask.id}", urgent=True)
        return None

    async def render(self, batch: Batch) -> str:
        lines = []
        for event in batch.events:
            try:
                lines.append(f"- {self._clock(event.at)} {await self.line(event)}")
            except Exception:  # noqa: BLE001 — an event that cannot be described is still named
                logger.warning("could not describe %s for the main orchestrator", event.type, exc_info=True)
                lines.append(f"- {event.type}")
        first = self._clock(batch.events[0].at) if batch.events else ""
        return f"[reports · {len(batch.events)} since {first}]\n" + "\n".join(lines)

    async def line(self, event: AppEvent) -> str:
        p = event.payload
        project = await self.manager.projects.get(event.project_id) if event.project_id else None
        name = project.name if project is not None else "a project"
        dispatch = f"dispatch {p.get('dispatch_id')}" + (f" \"{_one_line(str(p.get('title')), 80)}\"" if p.get("title") else "")
        if event.type == "dispatch.closed":
            status = str(p.get("status") or "")
            said = _whole(str(p.get("result") or ""))
            if status == "done" and p.get("kind") == "setup" and project is not None:
                brief = await self.manager.projects.brief(project.id)
                goals = _one_line(brief["goals"].body, 240) if "goals" in brief else ""
                return f"{name} finished its setup ({dispatch}): {said}" + (f" — goals: {goals}" if goals else "") + f" — the project is at /app/project/{project.id}"
            return f"{name} closed {dispatch} as {status}: {said}{refs_line(p.get('files'))}"
        if event.type == "dispatch.stalled":
            return f"{dispatch} of {name} has been quiet for {p.get('minutes')} min and nobody there is working — tell the operator; do not prod the project yourself"
        if event.type == "dispatch.message":
            return f"{name} reported {p.get('kind')} on {dispatch}: {_whole(str(p.get('text') or ''))}{refs_line(p.get('files'))}"
        if event.type == "ask.answered":
            ask = await self._ask_by_ref(str(p.get("request_ref") or ""))
            outcome = str((ask.resolution or {}).get("outcome") or "") if ask is not None else ""
            return f"the operator answered your confirmation [{ask.short_id if ask else ''}]: {outcome or 'answered'}"
        return event.type

    async def target_state(self) -> TargetState:
        session_id = await self.session_id()
        if not session_id:
            return "gone"
        state = await self.manager.get_state(session_id)
        if state is None:
            return "gone"
        if state.pending is not None:
            return "waiting"
        if state.running and not (state.engine is not None and state.engine.is_terminal):
            return "running"
        return "idle"

    async def deliver(self, text: str, steer: bool) -> None:
        session_id = await self.session_id()
        if not session_id:
            raise RuntimeError("there is no main orchestrator session")
        await self.manager.submit(session_id, text, origin="events", as_answer=False, steer=steer, follow_up=not steer)

    async def _capped(self, wait_seconds: float) -> None:
        notifications = self.app.notifications
        if notifications is None:
            return
        await notifications.post(Draft(
            "orchestrator_report",
            f"The main orchestrator reached {self.manager.config.dispatcher.max_wakes_per_hour} wake-ups this hour",
            f"Progress reports wait about {max(1, round(wait_seconds / 60))} min; closed and stalled dispatches still wake it.",
            kind="dispatcher_capped",
            session_id=await self.session_id() or None,
            dedupe_key="dispatcher-capped",
            source="dispatcher",
            link="/app/main",
        ))

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        if self.queue is not None and session_id == await self.session_id():
            self.queue.poke()

    async def on_session_deleted(self, session_id: str) -> None:
        """The operator deleted the main chat: the office is empty; opening the chat again makes a new one."""
        if session_id != await self.session_id():
            return
        async with self._lock:
            await self._swap(session_id, "")
            await self.stop_queue()

    def _zone(self) -> Any:
        tz = self.manager.presence.locale()[1]
        try:
            return ZoneInfo(tz) if tz else UTC
        except (ZoneInfoNotFoundError, ValueError):
            return UTC

    def _clock(self, at: str) -> str:
        try:
            moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            return at[:16]
        moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        return moment.astimezone(self._zone()).strftime("%H:%M")

    # -- its tools -------------------------------------------------------------------------------------

    async def service(self, operation: str, /, **kwargs: Any) -> str:
        """The tools' hook: each operation first checks that the calling session holds the office."""
        handler = OPERATIONS.get(operation)
        if handler is None:
            raise ValueError(f"unknown main orchestrator operation {operation!r}")
        session_id = str(kwargs.pop("session_id", "") or "")
        await self.current(session_id)
        try:
            return await handler(self, session_id, **kwargs)
        except (DispatchError, ProjectError, StaffError) as exc:
            raise ValueError(str(exc)) from exc

    async def find_project(self, ref: str) -> Project:
        wanted = (ref or "").strip()
        if not wanted:
            raise ValueError("name the project; Projects() lists them")
        projects = [p for p in await self.manager.projects.list() if not p.settings.system and not p.settings.ephemeral]
        for p in projects:
            if p.id == wanted:
                return p
        lowered = wanted.casefold()
        exact = [p for p in projects if p.name.casefold() == lowered]
        if len(exact) == 1:
            return exact[0]
        partial = [p for p in projects if lowered in p.name.casefold()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise ValueError(f"{wanted!r} could be {', '.join(p.name for p in partial)}; name one, or ask the operator which")
        raise ValueError(f"no project called {wanted!r}; Projects() lists them")

    async def find_dispatch(self, ref: str) -> Dispatch:
        dispatch = await self.manager.dispatches.get((ref or "").strip())
        if dispatch is None:
            raise ValueError(f"no dispatch {ref!r}; the state lists the open ones and Progress() every one")
        return dispatch

    # -- plumbing -----------------------------------------------------------------------------------------

    async def _publish(self, event_type: str, payload: dict[str, Any], project_id: str | None, *, session_id: str | None = None) -> None:
        try:
            await self.manager.bus.publish(event_type, payload, project_id=project_id, session_id=session_id)
        except Exception:  # noqa: BLE001 — the change stands; the event is how others hear of it
            logger.warning("could not publish %s", event_type, exc_info=True)

    def attach(self) -> None:
        manager = self.manager
        manager.service_hooks["dispatcher"] = self.service
        manager.turn_notes_hooks.append(self.turn_notes)
        manager.preset_hooks.append(self.preset_for)
        manager.on_finished(self.on_run_finished)
        manager.delete_hooks.append(self.on_session_deleted)
        notifications = self.app.notifications
        if notifications is not None and hasattr(notifications, "register_link"):
            notifications.register_link(self.request_link)

    async def resume(self) -> None:
        session_id = await self.session_id()
        if session_id and await self.manager.get_state(session_id) is not None:
            await self.start_queue()


def _stamp(at: str | None) -> float:
    if not at:
        return 0.0
    try:
        moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()


# -- the operations behind its tools ---------------------------------------------------------------------


async def op_projects(d: Dispatcher, session_id: str, *, project: str | None = None) -> str:
    manager = d.manager
    now = _now()
    if project:
        return await _project_detail(d, await d.find_project(project), now)
    projects = [p for p in await manager.projects.list() if not p.settings.system and not p.settings.ephemeral]
    if not projects:
        return "there are no projects yet; CreateProject makes one"
    teams = await manager.staff.team_counts()
    waiting = await manager.asks.open_counts("operator")
    board: dict[str, dict[str, int]] = {}
    for row in await manager.db.fetchall("SELECT project_id, status, COUNT(*) AS n FROM board_tasks WHERE project_id IS NOT NULL GROUP BY project_id, status"):
        board.setdefault(row["project_id"], {})[row["status"]] = int(row["n"])
    opened: dict[str, int] = {}
    for dispatch in await manager.dispatches.open_for():
        opened[dispatch.project_id] = opened.get(dispatch.project_id, 0) + 1
    reports: dict[str, Any] = {}
    for row in await manager.db.fetchall("SELECT j.project_id, j.at, j.refs_json FROM project_journal j JOIN (SELECT project_id, max(id) AS id FROM project_journal WHERE kind = 'report' GROUP BY project_id) last ON last.id = j.id"):
        reports[row["project_id"]] = row
    lines = []
    for p in projects:
        o = p.settings.orchestrator
        parts = [f"orchestrator {'on' if o.enabled else 'off'}" + (f" (autonomy {o.autonomy})" if o.enabled else "")]
        if p.setup_by == SETUP_BY:
            parts.append("being set up")
        parts.append(f"{opened.get(p.id, 0)} open dispatches")
        counts = board.get(p.id, {})
        parts.append(f"tasks doing {counts.get('doing', 0)}, review {counts.get('review', 0)}")
        team = teams.get(p.id, {})
        parts.append(f"staff {team.get('working', 0)} working of {team.get('staff', 0)}")
        parts.append(f"needs you {waiting.get(p.id, 0)}")
        report = reports.get(p.id)
        if report is not None:
            try:
                kind = json.loads(report["refs_json"] or "{}").get("kind", "report")
            except (TypeError, ValueError):
                kind = "report"
            parts.append(f"last report {kind} {_age(report['at'], now)} ago")
        lines.append(f"{p.name} [{p.id}] — " + " · ".join(parts))
    return "\n".join(lines)


async def _project_detail(d: Dispatcher, project: Project, now: datetime) -> str:
    manager = d.manager
    o = project.settings.orchestrator
    lines = [f"{project.name} [{project.id}] — orchestrator {'on, autonomy ' + o.autonomy if o.enabled else 'off'}" + (" — being set up" if project.setup_by == SETUP_BY else "")]
    lines.append("Folders: " + " · ".join(f"{f.path} ({f.env}{', git' if f.is_git else ''}{', read-only' if f.readonly else ''})" for f in project.folders))
    brief = await manager.projects.brief(project.id)
    for name in BRIEF_SECTIONS:
        body = brief[name].body.strip() if name in brief else ""
        if body:
            lines.append(f"Brief, {name.replace('_', ' ')}: {_one_line(body.splitlines()[0], 200)}")
    counts: dict[str, int] = {}
    for row in await manager.db.fetchall("SELECT status, COUNT(*) AS n FROM board_tasks WHERE project_id = ? GROUP BY status", (project.id,)):
        counts[row["status"]] = int(row["n"])
    lines.append("Board: " + " · ".join(f"{s} {counts.get(s, 0)}" for s in ("doing", "review", "todo", "blocked", "done")))
    members = await manager.staff.list(project.id)
    live = await manager.staff.live_sessions(project.id)
    if members:
        lines.append("Team: " + " · ".join(f"{m.name} ({live[m.id].status.replace('_', ' ') if m.id in live else 'off'})" for m in members))
    dispatches = [x for x in await manager.dispatches.recent(project_id=project.id, limit=10) if x.status in ("open", "blocked")]
    if dispatches:
        lines.append("Open dispatches: " + " · ".join(f"[{x.id}] #{x.seq} {x.status}: {_one_line(x.title or x.text, 80)}" for x in dispatches))
    entries = await manager.projects.journal(project.id, limit=8)
    reports = [e for e in entries if e.kind == "report"][:5]
    others = [e for e in entries if e.kind != "report"][:5]
    for e in reports:
        lines.append(f"Report {_age(e.at, now)} ago: {_one_line(e.text, 240)}")
    for e in others:
        lines.append(f"Journal {_age(e.at, now)} ago, {e.author}/{e.kind}: {_one_line(e.text, 200)}")
    return "\n".join(lines)


async def main_files(d: Dispatcher, refs: list[str] | None) -> list[StoredFile]:
    """The main orchestrator's files a hand-over names, each checked before anything is handed over."""
    wanted = [str(r).strip() for r in refs or [] if str(r or "").strip()]
    if len(wanted) > HANDOVER_MAX_FILES:
        raise ValueError(f"{len(wanted)} files in one hand-over; at most {HANDOVER_MAX_FILES}")
    found = []
    for ref in wanted:
        try:
            found.append(await d.manager.files.in_scope(ref, MAIN))
        except FileRefused as exc:
            raise ValueError(f"{exc}; Files() lists them") from exc
    return list({f.id: f for f in found}.values())


async def op_delegate(d: Dispatcher, session_id: str, *, project: str, text: str, title: str = "", dispatch_id: str | None = None, enable_orchestrator: bool = False, files: list[str] | None = None) -> str:
    body = (text or "").strip()
    if not body:
        raise ValueError("say what the project is to do")
    handed = await main_files(d, files)
    carried = f"; with {len(handed)} file{'s' if len(handed) > 1 else ''}, now the project's under the same handle{'s' if len(handed) > 1 else ''}" if handed else ""
    if dispatch_id:
        dispatch = await d.find_dispatch(dispatch_id)
        target = await d.find_project(project) if project else None
        if target is not None and target.id != dispatch.project_id:
            raise ValueError(f"dispatch {dispatch.id} belongs to another project")
        dispatch = await d.dispatches.follow_up(dispatch, body, files=handed)
        return f"added to dispatch {dispatch.id} (#{dispatch.seq}, {dispatch.status}){carried}; its orchestrator has it now"
    target = await d.find_project(project)
    if not target.settings.orchestrator.enabled:
        if not enable_orchestrator:
            raise ValueError(
                f"the orchestrator of {target.name} is off, so nobody there would take this. Switch it on only if the operator asked "
                "for that (enable_orchestrator=true), or tell them it is off"
            )
        if d.orchestrators is None:
            raise ValueError("orchestrators are not running on this installation")
        target = await d.orchestrators.enable(target.id, by="dispatcher")
    dispatch = await d.dispatches.create(target, text=body, title=title, from_session=session_id, files=handed)
    return f"dispatch {dispatch.id} (#{dispatch.seq}) handed to {target.name}{carried}; its orchestrator is woken with it. You are told when it reports; do not check on it."


async def op_files(d: Dispatcher, session_id: str, *, op: str = "list", file: str = "", offset: int = 1, limit: int = 200) -> str:
    """The main orchestrator's own files: the operator's attachments in its chat and what projects
    reported back. It reads them; it never opens a project's folders."""
    if op == "list":
        listed = await d.manager.files.listing(MAIN, limit=30)
        if not listed:
            return "No files yet. The operator's attachments in this chat and the files projects report back are kept here, each with a handle (att:…)."
        return "\n".join(["Your files, newest first (pass one to a project with Delegate(files=[…])):", *(f"- {f.handle} {f.name} ({f.mime}, {human_size(f.size)}; from the {f.origin})" for f in listed)])
    if op != "read":
        raise ValueError("op is list or read")
    if not file:
        raise ValueError("read needs file: a handle att:…")
    try:
        stored = await d.manager.files.in_scope(file, MAIN)
    except FileRefused as exc:
        raise ValueError(f"{exc}; Files() lists them") from exc
    try:
        body = await kept_body(d.manager, await d.manager.files.read(stored), stored, offset=offset, limit=limit)
    except (PeekRefused, VisionUnavailable) as exc:
        raise ValueError(str(exc)) from exc
    return f"{stored.handle} {stored.name} ({stored.mime}, {stored.size} bytes):\n{body}"


async def op_progress(d: Dispatcher, session_id: str, *, dispatch_id: str | None = None, project: str | None = None) -> str:
    manager = d.manager
    now = _now()
    names = {p.id: p.name for p in await manager.projects.list()}
    if dispatch_id:
        dispatch = await d.find_dispatch(dispatch_id)
        lines = [
            f"[{dispatch.id}] {names.get(dispatch.project_id, dispatch.project_id)} #{dispatch.seq} — {dispatch.status}, opened {_age(dispatch.created_at, now)} ago"
            + (f", closed {_age(dispatch.closed_at, now)} ago" if dispatch.closed_at else "")
            + (" (stalled)" if dispatch.stalled_at and dispatch.open else ""),
            f"Asked: {_one_line(dispatch.text, 600)}",
        ]
        for message in await manager.dispatches.messages(dispatch.id, limit=10):
            lines.append(f"- {_age(message.at, now)} ago, {message.author} {message.kind}: {_one_line(message.text, 400)}")
        if dispatch.result and not dispatch.open:
            lines.append(f"Result: {_one_line(dispatch.result, 800)}")
        asks = await manager.asks.of_dispatches([dispatch.id], open_only=True)
        for ask in asks:
            lines.append(f"Waiting for the operator: [{ask.short_id}] {_one_line(ask.text, 200)}")
        rows = await manager.db.fetchall(
            "SELECT t.id, t.title, t.status, m.name FROM board_tasks t LEFT JOIN staff m ON m.id = t.assignee_staff_id WHERE t.project_id = ? AND t.status IN ('doing', 'review') ORDER BY t.updated_at DESC LIMIT 8",
            (dispatch.project_id,),
        )
        if rows:
            lines.append("The project's work now: " + " · ".join(f"{r['title']} ({r['status']}, {r['name'] or 'unassigned'})" for r in rows))
        return "\n".join(lines)
    if project:
        target = await d.find_project(project)
        chosen = [x for x in await manager.dispatches.recent(project_id=target.id, limit=20) if x.status in ("open", "blocked")]
    else:
        chosen = [x for x in await manager.dispatches.recent(limit=60) if x.status in ("open", "blocked")]
    if not chosen:
        return "no open dispatches" + (" there" if project else "")
    last = await manager.dispatches.last_messages([x.id for x in chosen])
    lines = []
    for x in sorted(chosen, key=lambda item: item.created_at):
        said = last.get(x.id)
        tail = f" — last: {said.author} {said.kind} {_age(said.at, now)} ago: {_one_line(said.text, 160)}" if said is not None else " — no report yet"
        lines.append(f"[{x.id}] {names.get(x.project_id, x.project_id)} #{x.seq} {x.status}, {_age(x.created_at, now)}: {_one_line(x.title or x.text, 100)}{tail}")
    return "\n".join(lines)


async def op_cancel(d: Dispatcher, session_id: str, *, dispatch_id: str, reason: str = "") -> str:
    dispatch = await d.find_dispatch(dispatch_id)
    await d.dispatches.cancel(dispatch, reason=reason, by="dispatcher")
    return f"dispatch {dispatch.id} is cancelled; its orchestrator is told to stop that work"


async def op_answer(d: Dispatcher, session_id: str, *, ask_id: str, text: str = "", quote: str = "") -> str:
    ask = await d.manager.asks.get((ask_id or "").strip())
    if ask is None:
        raise ValueError(f"no request {ask_id!r}; the state lists the questions waiting")
    if not ask.open:
        raise ValueError(f"request {ask.short_id} is already answered")
    if not await d.is_mirrored(ask):
        raise ValueError(f"request {ask.short_id} is not shown in this chat")
    if ask.kind != "question":
        raise ValueError("only a question is answered in words; a permission, a folder or a new project is the operator's to press on its card")
    said, seq = await _latest_operator_message(d, session_id)
    wanted = (quote or text or "").strip()
    if len(wanted) < QUOTE_MIN:
        raise ValueError("quote the words of the operator's latest message that answer it")
    if not said or _normalised(wanted) not in _normalised(said):
        raise ValueError(
            "the answer must be the operator's own words: quote them from their latest message. If they did not say it, ask them "
            "— and if several questions are waiting and it is unclear which they meant, ask which"
        )
    team: Any = d.app.extensions.get("staff")
    if team is None:
        raise ValueError("the team is not running here")
    answer = (text or quote).strip()
    await team.answer(ask.id, text=answer, by="operator", via="dispatcher", extra={"quote_message_id": seq, "quote": wanted[:500]})
    return f"answered [{ask.short_id}] with the operator's words: {answer}"


async def _latest_operator_message(d: Dispatcher, session_id: str) -> tuple[str, int]:
    for message in reversed(await d.manager.transcript(session_id, tail=OPERATOR_TAIL)):
        if message.role is MessageRole.user and message.metadata.get("daedalus.origin") == "operator":
            text = "\n".join(b.text for b in message.content_blocks if isinstance(b, TextBlock))
            return text, int(message.metadata.get("daedalus.seq") or 0)
    return "", 0


OPERATIONS: dict[str, Any] = {
    "projects": op_projects,
    "delegate": op_delegate,
    "progress": op_progress,
    "cancel": op_cancel,
    "answer": op_answer,
    "files": op_files,
}


async def install(app: Application) -> list[asyncio.Task[None]]:
    dispatcher = Dispatcher(app)
    app.extensions["dispatcher"] = dispatcher
    dispatcher.attach()
    await dispatcher.resume()
    tasks: list[asyncio.Task[None]] = []
    front = getattr(app, "front", None)
    if front is not None:
        telegram = DispatcherTelegram(app, front, dispatcher)
        app.extensions["dispatcher_telegram"] = telegram
        tasks.append(telegram.attach())
    return tasks


__all__ = ["Dispatcher", "NotCurrent", "OPERATIONS", "install"]
