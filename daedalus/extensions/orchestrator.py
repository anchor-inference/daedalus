"""A project's orchestrator: its session, its office, what it sees and what wakes it.

``app.extensions["orchestrator"]`` switches a project's orchestrator on and off and replaces it,
answers "is this session the project's current orchestrator" for every one of its tools, writes the
project's state into the turn context of each of its turns, and wakes it with batches of the
project's events (:class:`daedalus.host.wake_queue.WakeQueue`).

Being the orchestrator is an office, not a property of a session. The project's settings name the one
session that holds it, and only a compare-and-set changes that name; enabling, replacing and
disabling also hold a lock per project. Every tool starts by asking :meth:`Orchestrators.current`,
so a replaced orchestrator whose engine is still running cannot act for the project it left: two
orchestrators can never act at once, and a replacement is never silent — the journal says who
replaced whom and why, and the successor's first state block names its predecessor.

An orchestrator never blocks on the operator. Its questions (``AskOperator``) are request rows that
return at once; the answer arrives as an ``ask.answered`` event in a later wake-up.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from protocore.contracts.types import OPERATOR_WORDS_METADATA_KEY, Message, MessageRole, TextBlock

from daedalus.config import NoModelConfigured
from daedalus.extensions import orchestrator_ops, orchestrator_team, wakeups
from daedalus.extensions.notifications import Draft, ProjectNotifyPolicy
from daedalus.extensions.project_usage import ProjectUsage
from daedalus.extensions.watches import describe as describe_watch
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.peek import BridgedFolderAccess, FolderAccess, LocalFolderAccess, UnreachableFolder
from daedalus.host.session_runner import HOME_KEY, WorkspaceUnreachable, home_of
from daedalus.host.wake_queue import Batch, TargetState, Wake, WakeQueue
from daedalus.staff_runtime import ReadRequest
from daedalus.stores.files import refs_line
from daedalus.stores.projects import BRIEF_SECTIONS, OrchestratorSettings, Project, ProjectError, ProjectFolder
from daedalus.stores.staff import ACTIVE_STATUSES, HARNESS_NAMES, Ask, Staff

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.host.session_runner import SessionManager, SessionState

logger = logging.getLogger(__name__)

CURSOR_KEY = "orchestrator_cursor:{project_id}"
"""The kv key of a project's wake-up cursor: the last event its orchestrator was given."""
STUCK_KEY = "orchestrator_stuck:{project_id}"
"""The kv key of why a project's orchestrator cannot be woken, once the operator has been told: kept
across a restart, so the same reason is not announced again by every start."""
HOME_NAME = "project-{project_id}"
"""The directory under the workspaces root an orchestrator runs in when its project's primary folder
is out of this process's reach."""

WAKE_TYPES = (
    "staff.status",
    "staff.report",
    "staff.channel",
    "ask.pending",
    "ask.answered",
    "permission.pending",
    "permission.resolved",
    "task.",
    "run.started",
    "schedule.fired",
    "watch.fired",
    "dispatch.created",
    "dispatch.message",
    "ask.batch",
)
"""What an orchestrator's queue subscribes to; :meth:`Orchestrators.classify` decides which of them wake it.
The two ``dispatch`` types are the main orchestrator's hand-overs to a project; they wake at once."""

STAFF_WAKE_STATUSES = frozenset({"turn_done_unseen", "error", "exited", "no_signal"})
URGENT_REPORTS = frozenset({"needs_input", "stuck"})
TASK_WAKES = frozenset({"task.created", "task.moved", "task.assigned", "task.accepted", "task.merge_failed"})
SELF_EVENTS_ALLOWED = frozenset({"ask.answered", "permission.resolved"})
"""Events that carry the orchestrator's own session id but are someone else's news: the operator
answering what it asked."""
ANSWER_EVENTS = frozenset({"ask.answered", "permission.resolved", "ask.batch"})
"""The events that bring the operator's answers."""
NEVER_FOLDED = ANSWER_EVENTS | {"staff.report"}
"""What a wake-up never folds into "… N more": the operator's answers and the members' reports. The cap
is for news the tools list again; a report folded away reached the orchestrator as a count, and in
the one place it looks it saw nothing of what the member had answered."""
IMPLICIT_REPORT_CHARS = 1000
"""How much of a turn's last message stands in for a report the member did not make: 300 characters
cut a member's answer mid-sentence, the whole turn is one ReadStaff away."""

QUESTION_MAX = 2000
OPTION_MAX = 200
HEADLINE_CHARS = 200
BRIEF_SECTION_CHARS = 400
TEAM_LINES = 12
OPEN_TASK_LINES = 12
ASK_LINES = 8
JOURNAL_LINES = 5
QUEUE_LINES = 6
READ_TIMEOUT_SECONDS = 5.0
SECTION_LABELS = {
    "goals": "goals",
    "constraints": "constraints",
    "preferences": "preferences",
    "done_when": "done when",
    "allowed_without_operator": "allowed without the operator",
    "notes": "notes",
}


class NotCurrent(RuntimeError):
    """This session does not hold its project's orchestrator office (any more); the message says who does."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _one_line(text: str, limit: int) -> str:
    flat = " / ".join(line.strip() for line in (text or "").strip().splitlines() if line.strip())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _verbatim(text: str) -> str:
    """The operator's words for an event line, whole and with their line breaks.

    Never clipped: an answer is the operator's instruction, and a 600-character cut once handed the
    orchestrator half a numbered list of remarks ending on "Такж…", which it could only report as
    broken. The size is bounded where the words are taken, by the answer routes' own limits. Lines
    after the first are indented so they stay with their event: a line of the operator's that began
    with "- 12:30" would otherwise read as an event of its own.
    """
    lines = [line.rstrip() for line in (text or "").strip().splitlines()]
    return "\n  ".join(lines)


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


def _tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)


def who(member: Staff | None) -> str:
    """A member as a line names them: their name, and the harness when it is not Daedalus's own."""
    if member is None:
        return "a staff member"
    return member.name if member.harness == "daedalus" else f"{member.name} ({HARNESS_NAMES.get(member.harness, member.harness)})"


Section = tuple[list[str], str]
"""Lines of the state block, and where the orchestrator finds what a cut left out."""

OPERATIONS = {**orchestrator_ops.OPS, **orchestrator_team.OPS}
"""Everything the orchestrator's tools can ask of this extension: the project tools and the team tools."""

CUT_NOTE = "\n[… the state is cut here; the tools show the rest]"


def _section(title: str, items: list[str], *, cap: int, more: str) -> Section:
    """A titled list of at most ``cap`` items, with what was left out counted and where to find it."""
    shown = items[:cap]
    rest = len(items) - len(shown)
    lines = [f"{title}:", *(f"  - {line}" for line in shown)] if shown else []
    if rest > 0:
        lines.append(f"  - … {rest} more ({more})")
    return lines, more


def fit(sections: list[Section], limit: int) -> str:
    """Join the sections, cutting the longest lists first until the whole fits ``limit`` characters.

    A section keeps its title and a "… N more (where)" line as it shrinks, so the orchestrator always
    sees that something is there and which tool shows the rest.
    """
    blocks = [(list(lines), more) for lines, more in sections if lines]

    def size() -> int:
        return sum(len(line) + 1 for lines, _ in blocks for line in lines)

    def items(lines: list[str]) -> int:
        return sum(1 for line in lines if line.startswith("  - ") and not line.startswith("  - … "))

    while size() > limit:
        candidates = [b for b in blocks if items(b[0]) > 0]
        if not candidates:
            break
        lines, more = max(candidates, key=lambda b: sum(len(line) for line in b[0]))
        dropped = 0
        if lines[-1].startswith("  - … "):
            try:
                dropped = int(lines[-1].split()[2])
            except (IndexError, ValueError):
                dropped = 0
            lines.pop()
        lines.pop()
        lines.append(f"  - … {dropped + 1} more ({more})")
    text = "\n".join(line for lines, _ in blocks for line in lines)
    return text if len(text) <= limit else text[: max(0, limit - len(CUT_NOTE))] + CUT_NOTE


class Orchestrators:
    """Every project's orchestrator of this installation."""

    def __init__(self, app: Application) -> None:
        self.app = app
        assert app.manager is not None
        self.manager: SessionManager = app.manager
        self._locks: dict[str, asyncio.Lock] = {}
        self.queues: dict[str, WakeQueue] = {}
        self._operator_words: dict[str, list[str]] = {}
        """The operator's passages each rendered batch relays, by the batch's text, for its delivery
        to name them to compaction; a batch's text is only ever delivered as rendered."""
        self._models: dict[str, str] = {}
        """The model each enabled project's orchestrator runs, by project: read on every engine build,
        which is synchronous, so it is kept here and written with every change of the setting."""
        self.state_sections: list[Callable[[Project], Awaitable[Section]]] = []
        """Further sections of the state block, each ``project -> (lines, where the rest is)``: what a
        later feature (the dispatches a project was handed) wants its orchestrator to see every turn."""
        self.clock: Callable[[], float] | None = None
        """A fake monotonic clock for the wake queues, in tests."""
        self._replacing: set[str] = set()
        self._background: set[asyncio.Task[None]] = set()

    # -- lookups ---------------------------------------------------------------------------------

    def lock(self, project_id: str) -> asyncio.Lock:
        return self._locks.setdefault(project_id, asyncio.Lock())

    @property
    def team(self) -> Any:
        return self.app.extensions.get("staff")

    @property
    def board(self) -> Any:
        return self.app.extensions.get("board")

    async def project(self, project_id: str) -> Project:
        project = await self.manager.projects.get(project_id)
        if project is None:
            raise KeyError(project_id)
        return project

    async def current(self, session_id: str) -> tuple[Project, OrchestratorSettings]:
        """The project this session is the current orchestrator of; :class:`NotCurrent` otherwise."""
        state = await self.manager.get_state(session_id)
        metadata = state.metadata if state is not None else {}
        project_id = str(metadata.get("orchestrator_of") or metadata.get("orchestrator_retired_of") or "")
        if not project_id:
            raise NotCurrent("this session is not a project's orchestrator")
        project = await self.manager.projects.get(project_id)
        if project is None:
            raise NotCurrent("the project this orchestrator ran no longer exists")
        orchestrator = project.settings.orchestrator
        if orchestrator.session_id != session_id:
            if orchestrator.session_id:
                raise NotCurrent(f"this orchestrator was replaced by {orchestrator.session_id}; it no longer acts for {project.name}")
            raise NotCurrent(f"the orchestrator of {project.name} is switched off")
        if not orchestrator.enabled:
            raise NotCurrent(f"the orchestrator of {project.name} is switched off")
        return project, orchestrator

    def session_of(self, project: Project) -> str:
        orchestrator = project.settings.orchestrator
        return orchestrator.session_id if orchestrator.enabled else ""

    # -- the office --------------------------------------------------------------------------------

    async def enable(self, project_id: str, *, model: str | None = None, autonomy: str | None = None, concurrency_cap: int | None = None, by: str = "operator") -> Project:
        """Switch the project's orchestrator on: a session of its own, its settings, its wake-ups.

        Enabling a project that already has a working orchestrator changes only what was passed. Two
        enables racing each other make one session: the lock serialises them in this process and the
        compare-and-set on the settings decides between processes; the loser's session is removed and
        it answers with the winner.
        """
        async with self.lock(project_id):
            project = await self.project(project_id)
            orchestrator = project.settings.orchestrator
            if orchestrator.enabled and orchestrator.session_id and await self.manager.get_state(orchestrator.session_id) is not None:
                if model is None and autonomy is None and concurrency_cap is None:
                    return project
                return await self._update_locked(project, model=model, autonomy=autonomy, concurrency=None, concurrency_cap=concurrency_cap, by=by)
            if model is not None:
                self._check_model(model)
            config = self.manager.config.orchestrator
            first = await self._never_had_one(project_id)
            cap = concurrency_cap if concurrency_cap is not None else (config.default_concurrency_cap if first else orchestrator.concurrency_cap)
            self._check_cap(cap)
            concurrency = min(config.default_concurrency, cap) if first else min(orchestrator.concurrency, cap)
            project = await self.manager.projects.update_orchestrator(project_id, enabled=True, model=model, autonomy=autonomy, concurrency=concurrency, concurrency_cap=cap)
            stale = orchestrator.session_id
            try:
                session_id = await self._new_session(project, predecessor=stale, reason="its session could not be loaded" if stale else "")
            except (OSError, ValueError, KeyError) as exc:
                # Switched on with no session would be an orchestrator that exists only in the settings.
                await self.manager.projects.update_orchestrator(project_id, enabled=False)
                raise ProjectError(f"the orchestrator of {project.name} could not be started: {exc}") from exc
            if not await self.manager.projects.set_orchestrator(project_id, expect=stale, value=session_id):
                await self.manager.delete_session(session_id)
                return await self.project(project_id)
            if stale:
                await self._retire(stale, project_id, successor=session_id)
            project = await self.project(project_id)
            await self._took_office(project)
            await self.manager.projects.record(
                project_id, "system", "orchestrator",
                f"The orchestrator was switched on by the {by} (model {self.model_of(project)}, autonomy {project.settings.orchestrator.autonomy}, concurrency {project.settings.orchestrator.concurrency} of {project.settings.orchestrator.concurrency_cap}).",
                {"session_id": session_id},
            )
            await self._changed(project_id, "orchestrator.enabled", by)
            return project

    async def update(self, project_id: str, *, model: str | None = None, autonomy: str | None = None, concurrency: int | None = None, concurrency_cap: int | None = None, by: str = "operator") -> Project:
        async with self.lock(project_id):
            project = await self.project(project_id)
            if not project.settings.orchestrator.enabled:
                raise ProjectError(f"{project.name} has no orchestrator; switch it on first")
            return await self._update_locked(project, model=model, autonomy=autonomy, concurrency=concurrency, concurrency_cap=concurrency_cap, by=by)

    async def _update_locked(self, project: Project, *, model: str | None, autonomy: str | None, concurrency: int | None, concurrency_cap: int | None, by: str) -> Project:
        if model is not None:
            self._check_model(model)
        if concurrency_cap is not None:
            self._check_cap(concurrency_cap)
        before = project.settings.orchestrator
        updated = await self.manager.projects.update_orchestrator(project.id, model=model, autonomy=autonomy, concurrency=concurrency, concurrency_cap=concurrency_cap)
        after = updated.settings.orchestrator
        changes = []
        if model is not None and before.model != after.model:
            changes.append(f"model {self.model_of(updated)}")
        if before.autonomy != after.autonomy:
            changes.append(f"autonomy {after.autonomy}")
        if before.concurrency != after.concurrency or before.concurrency_cap != after.concurrency_cap:
            changes.append(f"concurrency {after.concurrency} of {after.concurrency_cap}")
        self._models[project.id] = after.model
        if model is not None:
            await self._sync_model(updated)
        if changes:
            await self.manager.projects.record(project.id, "system", "orchestrator", f"The {by} changed the orchestrator: {', '.join(changes)}.", {})
            await self._changed(project.id, "orchestrator.settings", by)
        if before.concurrency != after.concurrency and self.team is not None:
            self.team.queue.pump_soon(project.id)
        return updated

    async def disable(self, project_id: str, *, by: str = "operator") -> Project:
        """Switch the orchestrator off. Its session stays, with its history; what it was asked goes to the operator."""
        async with self.lock(project_id):
            project = await self.project(project_id)
            orchestrator = project.settings.orchestrator
            if not orchestrator.enabled and not orchestrator.session_id:
                return project
            await self.manager.projects.update_orchestrator(project_id, enabled=False)
            if orchestrator.session_id and await self.manager.projects.set_orchestrator(project_id, expect=orchestrator.session_id, value=""):
                await self._retire(orchestrator.session_id, project_id, successor="")
            await self._left_office(project_id)
            await self._hand_requests_to_operator(project_id, why="the orchestrator was switched off")
            await self.manager.projects.record(project_id, "system", "orchestrator", f"The orchestrator was switched off by the {by}.", {"session_id": orchestrator.session_id})
            await self._changed(project_id, "orchestrator.disabled", by)
            return await self.project(project_id)

    async def replace(self, project_id: str, reason: str, *, by: str = "operator") -> Project:
        """A new orchestrator session in place of the current one, linked both ways, never silently.

        The predecessor is retired (its tools refuse from now on, and a run it is in is stopped), the
        wake-ups it set are re-pointed at the successor, the journal records the replacement, and the
        successor's first state block names the predecessor and the reason.
        """
        reason = " ".join((reason or "").split())[:300] or "replaced"
        async with self.lock(project_id):
            project = await self.project(project_id)
            orchestrator = project.settings.orchestrator
            if not orchestrator.enabled:
                raise ProjectError(f"{project.name} has no orchestrator to replace; switch it on instead")
            old = orchestrator.session_id
            session_id = await self._new_session(project, predecessor=old, reason=reason)
            if not await self.manager.projects.set_orchestrator(project_id, expect=old, value=session_id):
                await self.manager.delete_session(session_id)
                return await self.project(project_id)
            if old:
                await self._retire(old, project_id, successor=session_id)
                await self.manager.db.execute("UPDATE schedules SET target_session = ? WHERE target_session = ?", (session_id, old))
            await wakeups.repoint(self.app, project_id, session_id)
            project = await self.project(project_id)
            await self._sync_model(project)
            await self.manager.projects.record(
                project_id, "system", "replacement",
                f"The orchestrator {old or '(none)'} was replaced by {session_id} ({by}): {reason}",
                {"predecessor": old, "successor": session_id},
            )
            await self._changed(project_id, "orchestrator.replaced", by)
            queue = self.queues.get(project_id)
            if queue is not None:
                queue.poke()
            return project

    async def _never_had_one(self, project_id: str) -> bool:
        """Whether no session of the project has ever been its orchestrator. Every project's settings
        carry the orchestrator block from the start, so the sessions are what remember one existed."""
        row = await self.manager.db.fetchone(
            "SELECT 1 FROM sessions WHERE project_id = ? AND (json_extract(metadata, '$.orchestrator_of') IS NOT NULL OR json_extract(metadata, '$.orchestrator_retired_of') IS NOT NULL) LIMIT 1",
            (project_id,),
        )
        return row is None

    def _check_cap(self, cap: int) -> None:
        ceiling = self.manager.config.orchestrator.max_concurrency_cap
        if not 1 <= int(cap) <= ceiling:
            raise ProjectError(f"the concurrency cap is between 1 and {ceiling}, not {cap}")

    def _check_model(self, model: str) -> None:
        if model and model not in self.manager.config.presets:
            raise ProjectError(f"no model preset {model!r}; the presets are in Settings → Models")

    def needs_home(self, project: Project) -> bool:
        """Whether the project's orchestrator must run in a directory of its own rather than in the
        project's primary folder: that folder lies where this process cannot go (a host folder, seen
        from the agent's container), so a run started there is refused before it begins. The
        orchestrator never writes files; it reads the folders through Peek, which goes through the
        host bridge, and the folders stay the project's for every other tool."""
        return not project.folders or not project.primary.local(self.manager.projects.local_env)

    async def _new_session(self, project: Project, *, predecessor: str, reason: str) -> str:
        metadata: dict[str, Any] = {"orchestrator_of": project.id, "telegram_detached": True}
        if self.needs_home(project):
            metadata[HOME_KEY] = HOME_NAME.format(project_id=project.id)
        if predecessor:
            metadata["predecessor"] = predecessor
            metadata["predecessor_reason"] = reason
        state = await self.manager.create_session(f"Orchestrator · {project.name}", metadata=metadata, project_id=project.id)
        return state.session.id

    async def _retire(self, session_id: str, project_id: str, *, successor: str) -> None:
        """The old session keeps its history and loses its office: it is marked, and a run it is in stops."""
        state = await self.manager.get_state(session_id)
        if state is None:
            return
        for metadata in (state.metadata, state.session.metadata):
            metadata.pop("orchestrator_of", None)
            metadata["orchestrator_retired_of"] = project_id
            if successor:
                metadata["successor"] = successor
        await self.manager.sessions.update_metadata(session_id, state.session.metadata)
        await self.manager.stop(session_id)

    async def _took_office(self, project: Project) -> None:
        self._models[project.id] = project.settings.orchestrator.model
        await self._sync_model(project)
        # Wake-ups set before the orchestrator was switched off wake whoever holds the office now.
        await wakeups.repoint(self.app, project.id, project.settings.orchestrator.session_id)
        # A new office starts from now: what happened to the project before it existed is not news.
        await self.manager.db.kv_set(CURSOR_KEY.format(project_id=project.id), self.manager.bus.head)
        await self.start_queue(project.id)

    async def _left_office(self, project_id: str) -> None:
        self._models.pop(project_id, None)
        await self.stop_queue(project_id)
        await self.manager.db.kv_set(CURSOR_KEY.format(project_id=project_id), None)

    async def _hand_requests_to_operator(self, project_id: str, *, why: str) -> None:
        team = self.team
        if team is None:
            return
        for ask in await self.manager.asks.open_for(project_id, routed_to="orchestrator"):
            await team.escalate(ask, why=why)

    async def _changed(self, project_id: str, change: str, by: str) -> None:
        try:
            await self.manager.bus.publish("project.changed", {"change": change, "actor": by}, project_id=project_id)
        except Exception:  # noqa: BLE001 — the change stands; the event is a courtesy
            logger.warning("could not publish project.changed for %s", project_id, exc_info=True)

    # -- the model ------------------------------------------------------------------------------------

    def model_of(self, project: Project) -> str:
        """The preset the project's orchestrator runs: its own choice, the Settings default, or the strongest."""
        return self.manager.config.orchestrator_preset(project.settings.orchestrator.model) or "(no model)"

    def preset_for(self, state: SessionState) -> str | None:
        """The manager's preset hook: an orchestrator runs its project's model, whatever the chat was set to."""
        project_id = str(state.metadata.get("orchestrator_of") or "")
        if not project_id or project_id not in self._models:
            return None
        return self.manager.config.orchestrator_preset(self._models[project_id])

    async def _sync_model(self, project: Project) -> None:
        """Write the effective preset on the session too, so the chat's model chip shows what it runs."""
        session_id = self.session_of(project)
        preset = self.manager.config.orchestrator_preset(project.settings.orchestrator.model)
        if not session_id or not preset:
            return
        try:
            await self.manager.set_model(session_id, preset=preset)
        except (KeyError, ValueError):
            logger.warning("could not set the model of orchestrator %s", session_id, exc_info=True)

    async def model_chosen(self, session_id: str, preset: str | None, *, clear: bool = False) -> bool:
        """The composer's model chip on an orchestrator's chat: the choice is the project's setting.

        Whether the session was a current orchestrator (and so the project changed). A cleared chip
        returns the project to the Settings default.
        """
        try:
            project, _ = await self.current(session_id)
        except NotCurrent:
            return False
        if not clear and not preset:
            return False
        await self.update(project.id, model="" if clear else preset)
        return True

    # -- what it sees ---------------------------------------------------------------------------------

    async def turn_notes(self, state: SessionState) -> str | None:
        """The manager's turn-notes hook: the project state in place of the workspace notes."""
        project_id = str(state.metadata.get("orchestrator_of") or state.metadata.get("orchestrator_retired_of") or "")
        if not project_id:
            return None
        try:
            project, _ = await self.current(state.session.id)
        except NotCurrent as exc:
            return f"\n- {exc}. Its tools refuse; tell the operator, and do nothing else."
        text = await self.project_state(project, session_id=state.session.id)
        if state.metadata.get("predecessor") and not state.metadata.get("predecessor_announced"):
            for metadata in (state.metadata, state.session.metadata):
                metadata["predecessor_announced"] = True
            await self.manager.sessions.update_metadata(state.session.id, state.session.metadata)
        return "\n" + text

    async def project_state(self, project: Project, *, session_id: str | None = None) -> str:
        """The state block: the project as it is now, bounded by ``orchestrator.state_max_chars``."""
        now = datetime.now(UTC)
        config = self.manager.config
        orchestrator = project.settings.orchestrator
        members = await self.manager.staff.list(project.id)
        live = await self.manager.staff.live_sessions(project.id)
        working = sum(1 for s in live.values() if s.status in ACTIVE_STATUSES)
        tasks = {r["id"]: dict(r) for r in await self.manager.db.fetchall("SELECT id, title, status, priority, assignee_staff_id, branch FROM board_tasks WHERE project_id = ? ORDER BY priority, updated_at DESC", (project.id,))}
        by_id = {m.id: m for m in members}

        head = [f"Project: {project.name} · default env {project.settings.default_env or self.manager.projects.local_env} · autonomy {orchestrator.autonomy} · concurrency {orchestrator.concurrency} of cap {orchestrator.concurrency_cap} · {working} working"]
        state = self.manager.live_state(session_id) if session_id else None
        if state is not None and state.metadata.get("predecessor") and not state.metadata.get("predecessor_announced"):
            head.append(f"You replace the orchestrator session {state.metadata['predecessor']}: {state.metadata.get('predecessor_reason') or 'replaced'}. Its journal entries are yours to read.")

        folders = [self._folder_line(f) for f in project.folders]
        brief = await self.manager.projects.brief(project.id)
        brief_parts = [f"{SECTION_LABELS[name]}: {_one_line(brief[name].body, BRIEF_SECTION_CHARS) or '(empty)'}" for name in BRIEF_SECTIONS]

        team_lines: list[str] = []
        one_offs: list[str] = []
        for member in members:
            line = self._member_line(member, live.get(member.id), tasks, now)
            (one_offs if member.one_off else team_lines).append(line)

        asks = await self.manager.asks.open_for(project.id)
        for_me = [self._ask_line(a, by_id, now) for a in asks if a.routed_to == "orchestrator"]
        for_operator = [self._ask_line(a, by_id, now) for a in asks if a.routed_to == "operator"]

        queue_lines: list[str] = []
        if self.team is not None:
            for entry in self.team.queue.queue(project.id):
                waiting = by_id.get(entry["staff_id"])
                queue_lines.append(f"{waiting.name if waiting else entry['staff_id']} → {entry['task_id']} ({entry['reason'] or 'waiting'}: {_one_line(entry['detail'], 120)})")

        counts: dict[str, int] = {}
        for task in tasks.values():
            counts[task["status"]] = counts.get(task["status"], 0) + 1
        open_tasks = [
            f"{t['id']} {_one_line(t['title'], 80)} ({by_id[t['assignee_staff_id']].name if t['assignee_staff_id'] in by_id else 'unassigned'}, {t['status']})"
            for t in tasks.values() if t["status"] not in ("done", "dropped")
        ]
        board_head = "Board: " + " · ".join(f"{name} {counts.get(name, 0)}" for name in ("doing", "review", "todo", "blocked", "done"))

        alarms = []
        for wakeup in (await wakeups.wakeups(self.app, project.id))[:10]:
            when = f"cron {wakeup['cron']} UTC" if wakeup["cron"] else self._moment(wakeup["next_run_at"] or "")
            alarms.append(f"[{wakeup['id']}] {when} \"{_one_line(wakeup['note'], 80)}\"")
        keeper = self.app.extensions.get("watches")
        watches = [
            f"[{w.id}] {_one_line(describe_watch(w), 120)}" + (f" ({_one_line(w.note, 60)})" if w.note else "")
            for w in (keeper.of_project(project.id, enabled_only=True) if keeper is not None else [])[:10]
        ]

        journal = [f"{self._clock(e.at)} {e.kind}: {_one_line(e.text, 160)}" for e in await self.manager.projects.journal(project.id, limit=JOURNAL_LINES)]

        board_lines = [board_head + (" — open:" if open_tasks else "")] + [f"  - {line}" for line in open_tasks[:OPEN_TASK_LINES]]
        if len(open_tasks) > OPEN_TASK_LINES:
            board_lines.append(f"  - … {len(open_tasks) - OPEN_TASK_LINES} more (Tasks)")
        team_section = _section("Team", team_lines, cap=TEAM_LINES, more="Team")
        sections: list[Section] = [
            (head, ""),
            (["Folders: " + " · ".join(folders)] if folders else [], ""),
            (["Brief — " + " · ".join(brief_parts)], ""),
            team_section if team_section[0] else (["Team: nobody yet"], ""),
            _section("One-off", one_offs, cap=TEAM_LINES, more="Team"),
            _section("Waiting for your answer", for_me, cap=ASK_LINES, more="the requests list"),
            _section("Needs the operator", for_operator, cap=ASK_LINES, more="the requests list"),
            _section("Launch queue", queue_lines, cap=QUEUE_LINES, more="Team"),
            (board_lines, "Tasks"),
            (["Wake-ups: " + (" · ".join(alarms) or "none") + " · Watches: " + (" · ".join(watches) or "none")], ""),
            _section("Journal (latest)", journal, cap=JOURNAL_LINES, more="Journal"),
            ([await self._spend_line(project.id, now)], ""),
        ]
        for extra in self.state_sections:
            try:
                sections.append(await extra(project))
            except Exception:  # noqa: BLE001 — one section that fails must not take the state away
                logger.exception("an orchestrator state section failed for %s", project.id)
        return fit(sections, config.orchestrator.state_max_chars)

    def _folder_line(self, folder: ProjectFolder) -> str:
        marks = [m for m in ("git" if folder.is_git else "", folder.env if folder.env != self.manager.projects.local_env else "", "read-only" if folder.readonly else "") if m]
        return f"[{folder.id}] {folder.label or Path(folder.path).name} {folder.path}" + (f" ({', '.join(marks)})" if marks else "")

    def _member_line(self, member: Staff, session: Any, tasks: dict[str, dict[str, Any]], now: datetime) -> str:
        role = f" — {_one_line(member.role, 60)}" if member.role else ""
        kind = HARNESS_NAMES.get(member.harness, member.harness) + (f" {member.agent}" if member.agent else "")
        where = member.isolation + (f" {session.branch}" if session is not None and session.branch else "")
        if session is None:
            return f"{member.name}{role}, {kind}, {where}: off"
        status = session.status.replace("_", " ")
        task = tasks.get(session.task_id or "")
        on = f" on \"{_one_line(task['title'], 60)}\" ({task['id']})" if task else ""
        waiting = f" — {session.waiting_for}" if session.waiting_for else ""
        return f"{member.name}{role}, {kind}, {where}: {status}{on}{waiting}, last signal {_age(session.last_signal_at or session.status_at, now)}"

    def _ask_line(self, ask: Ask, members: dict[str, Staff], now: datetime) -> str:
        asker = "you" if ask.origin == "orchestrator" else who(members.get(ask.staff_id or ""))
        suggestion = f" — your suggestion: {_one_line(ask.suggestion, 80)}" if ask.suggestion else ""
        # The title leads when there is one: it is what the operator sees in their list, so the
        # orchestrator can refer to a question the way the operator reads it.
        said = f"\"{_one_line(ask.title, 80)}\" {_one_line(ask.text, 120)}" if ask.title else _one_line(ask.text, 160)
        return f"[{ask.short_id}] {asker} ({ask.kind}): {said} ({_age(ask.created_at, now)}){suggestion}"

    async def _spend_line(self, project_id: str, now: datetime) -> str:
        """Today's spend, read by the same summary the app shows, so the orchestrator and the operator
        never see two different numbers for one project."""
        usage = await ProjectUsage(self.manager).summary(project_id, now=now)
        mine, other, total = usage["orchestrator"]["today"], usage["other"]["today"], usage["total"]["today"]
        staff = sum(member["today"]["usd"] for member in usage["staff"])
        extra = f" · other sessions ${other['usd']:.2f}" if other["usd"] else ""
        unpriced = f" · {total['unpriced']} unpriced" if total["unpriced"] else ""
        return f"Spend today: orchestrator ${mine['usd']:.2f} · staff ${staff:.2f}{extra} (tokens: {_tokens(total['tokens'])}){unpriced}"

    def _zone(self) -> tzinfo:
        """The operator's time zone, as their app last reported it: the times in a batch are theirs."""
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

    def _moment(self, at: str) -> str:
        """A time of day when it is today in the operator's zone, with the date when it is not."""
        try:
            moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            return at[:16]
        moment = (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(self._zone())
        today = datetime.now(UTC).astimezone(self._zone()).date()
        return moment.strftime("%H:%M" if moment.date() == today else "%Y-%m-%d %H:%M")

    # -- wake-ups -------------------------------------------------------------------------------------

    async def start_queue(self, project_id: str) -> WakeQueue:
        existing = self.queues.get(project_id)
        if existing is not None and not existing.closed:
            return existing
        key = CURSOR_KEY.format(project_id=project_id)

        async def load_cursor() -> int | None:
            value = await self.manager.db.kv_get(key)
            return int(value) if isinstance(value, int) else None

        async def save_cursor(seq: int) -> None:
            await self.manager.db.kv_set(key, int(seq))

        async def classify(event: AppEvent) -> Wake | None:
            return await self.classify(project_id, event)

        async def render(batch: Batch) -> str:
            return await self.render(project_id, batch)

        async def state() -> TargetState:
            return await self.target_state(project_id)

        async def deliver(text: str, steer: bool) -> None:
            await self.deliver(project_id, text, steer=steer)

        async def capped(wait_seconds: float) -> None:
            await self._capped(project_id, wait_seconds)

        async def stuck(reason: str) -> None:
            await self.stuck(project_id, reason)

        async def unstuck() -> None:
            await self.unstuck(project_id)

        extra: dict[str, Any] = {"clock": self.clock} if self.clock is not None else {}
        queue = WakeQueue(
            name=project_id,
            bus=self.manager.bus,
            flt=EventFilter(types=WAKE_TYPES, project_id=project_id),
            classify=classify,
            render=render,
            state=state,
            deliver=deliver,
            load_cursor=load_cursor,
            save_cursor=save_cursor,
            batch_seconds=lambda: float(self.manager.config.orchestrator.batch_seconds),
            max_wakes_per_hour=lambda: self.manager.config.orchestrator.max_wakes_per_hour,
            on_capped=capped,
            lasting=lasting,
            on_stuck=stuck,
            on_unstuck=unstuck,
            **extra,
        )
        # A queue that was stuck before a restart still owes the all-clear: without this, the first
        # delivery after the restart would leave the dispatches it blocked blocked for good.
        told = await self.manager.db.kv_get(STUCK_KEY.format(project_id=project_id))
        queue.stuck = told if isinstance(told, str) else ""
        self.queues[project_id] = queue
        await queue.start()
        return queue

    async def stop_queue(self, project_id: str) -> None:
        queue = self.queues.pop(project_id, None)
        if queue is not None:
            await queue.close()

    async def classify(self, project_id: str, event: AppEvent) -> Wake | None:
        """Whether an event of the project wakes its orchestrator, and whether it cannot wait.

        Its own doing is never news to it: an event it caused (``actor: orchestrator``, or its own
        session's) is dropped at the source, so a message it sends a member cannot wake it again.
        """
        project = await self.manager.projects.get(project_id)
        if project is None or not project.settings.orchestrator.enabled:
            return None
        mine = project.settings.orchestrator.session_id
        p = event.payload
        if p.get("actor") == "orchestrator" or (event.session_id and event.session_id == mine and event.type not in SELF_EVENTS_ALLOWED):
            return None
        kind = event.type
        if kind == "staff.status":
            status = str(p.get("status") or "")
            if status not in STAFF_WAKE_STATUSES or not event.staff_id:
                return None
            return Wake(f"staff:{event.staff_id}", urgent=status == "error")
        if kind == "staff.report":
            if p.get("implicit") and event.staff_id:
                # Made from a turn that ended with no report: it is that turn's end, and replaces the
                # plain "finished a turn" line rather than following it.
                return Wake(f"staff:{event.staff_id}", urgent=p.get("kind") in URGENT_REPORTS)
            return Wake(f"report:{event.seq}", urgent=p.get("kind") in URGENT_REPORTS)
        if kind == "staff.channel":
            return Wake(f"channel:{event.staff_id or event.seq}", urgent=False) if event.staff_id else None
        if kind in ("ask.pending", "permission.pending"):
            if not event.staff_id:
                return None
            routed = str(p.get("routed_to") or "")
            if not routed:
                routed = self.team.route(project, "permission" if kind == "permission.pending" else "question") if self.team is not None else "operator"
            # A request the operator decides is news to the orchestrator, not a call to act.
            return Wake(f"request:{p.get('request_ref') or event.seq}", urgent=routed == "orchestrator")
        if kind in ("ask.answered", "permission.resolved"):
            ask = await self._ask_for_event(p)
            if ask is None or ask.resolved_by == "system":
                # A request withdrawn because what it served is over has no answer to bring.
                return None
            if ask.resolution.get("batch"):
                # Answered with others from the operator's list: the batch's own event brings it,
                # with the rest, as one wake-up rather than one per answer.
                return None
            if not self._news_to_it(ask):
                return None
            return Wake(f"answer:{ask.id}", urgent=kind == "ask.answered")
        if kind == "ask.batch":
            asks = await self._batch_asks(p)
            if not asks:
                return None
            return Wake(f"answers:{p.get('batch_id') or event.seq}", urgent=any(a.kind != "permission" for a in asks))
        if kind in TASK_WAKES:
            return Wake(f"task:{p.get('task_id') or event.seq}", urgent=kind == "task.merge_failed")
        if kind == "run.started":
            if not event.staff_id or p.get("origin") != "operator":
                return None
            return Wake(f"wrote:{event.staff_id}")
        if kind == "schedule.fired":
            return Wake(f"schedule:{event.seq}", urgent=True) if p.get("kind") == "wake" else None
        if kind == "watch.fired":
            action = (p.get("action") or "wake") if isinstance(p.get("action"), str) else "wake"
            return Wake(f"watch:{event.seq}", urgent=True) if action == "wake" else None
        if kind == "dispatch.created":
            return Wake(f"dispatch:{event.seq}", urgent=True)
        if kind == "dispatch.message":
            # Only what the main orchestrator (or the operator) says on a dispatch is for this
            # orchestrator; its own progress reports are messages on the dispatch too, and waking on
            # them would be waking on its own doing.
            if p.get("author") not in ("dispatcher", "operator"):
                return None
            return Wake(f"dispatch:{event.seq}", urgent=True)
        return None

    @staticmethod
    def _news_to_it(ask: Ask) -> bool:
        """Whether an answer is the orchestrator's to hear: to what it asked, or to what it escalated."""
        own = ask.origin == "orchestrator"
        escalated = ask.routed_to == "operator" and ask.routed_at > ask.created_at and ask.resolved_by == "operator"
        return own or escalated

    async def _batch_asks(self, payload: Any) -> list[Ask]:
        """The answers of a batch the orchestrator hears of, in the order the operator gave them."""
        found: list[Ask] = []
        for ask_id in payload.get("ask_ids") or []:
            ask = await self.manager.asks.get(str(ask_id))
            if ask is not None and ask.resolved_by not in (None, "system") and self._news_to_it(ask):
                found.append(ask)
        return found

    async def _ask_for_event(self, payload: Any) -> Ask | None:
        ref = str(payload.get("request_ref") or "")
        if ref:
            row = await self.manager.db.fetchone("SELECT id FROM asks WHERE json_extract(detail_json, '$.event_ref') = ? ORDER BY created_at DESC LIMIT 1", (ref,))
            if row is not None:
                return await self.manager.asks.get(row["id"])
        request_id = str(payload.get("request_id") or "")
        if request_id.startswith("ask-"):
            return await self.manager.asks.get(request_id)
        return None

    async def render(self, project_id: str, batch: Batch) -> str:
        """One line per event, with the ids the orchestrator needs to act on it without a lookup."""
        project = await self.manager.projects.get(project_id)
        name = project.name if project is not None else project_id
        limit = self.manager.config.orchestrator.batch_max_lines
        events = batch.events
        # An answer of the operator's or a member's report is never one of the "… N more" (see
        # ``NEVER_FOLDED``); the rest of the batch is capped.
        shown = [event for index, event in enumerate(events) if index < limit or event.type in NEVER_FOLDED]
        lines: list[str] = []
        words: list[str] = []
        for event in shown:
            try:
                # A batch of answers is one event and one wake-up, but each answer is a line of its
                # own, worded as a single answer is: the orchestrator reads them the same way, and
                # the app's event card draws them the same way.
                said = await self.batch_lines(project, event) if event.type == "ask.batch" else [await self.line(project, event)]
                if event.type in ANSWER_EVENTS:
                    answered = await self._batch_asks(event.payload) if event.type == "ask.batch" else [await self._ask_for_event(event.payload)]
                    words += [passage for ask in answered if ask is not None for passage in self._answer_words(ask)]
            except Exception:  # noqa: BLE001 — an event that cannot be described is still named
                logger.warning("could not describe %s for the orchestrator", event.type, exc_info=True)
                said = [event.type]
            lines.extend(f"- {self._clock(event.at)} {line}" for line in said)
        if len(events) > len(shown):
            lines.append(f"- … and {len(events) - len(shown)} more (Team, Tasks)")
        first = self._clock(events[0].at) if events else ""
        text = f"[events · {name} · {len(lines)} since {first}]\n" + "\n".join(lines)
        if words:
            self._operator_words[text] = words
            while len(self._operator_words) > 64:  # a batch rendered and never delivered is not kept for ever
                self._operator_words.pop(next(iter(self._operator_words)))
        return text

    async def batch_lines(self, project: Project | None, event: AppEvent) -> list[str]:
        return [await self._answer_line(ask) for ask in await self._batch_asks(event.payload)]

    async def _answer_line(self, ask: Ask) -> str:
        answer = self._answer_text(ask)
        if ask.origin == "orchestrator":
            return f"the operator answered your request [{ask.short_id}] \"{_one_line(ask.heading, 120)}\": {answer}"
        member = await self.manager.staff.get(ask.staff_id) if ask.staff_id else None
        return f"the operator answered the request [{ask.short_id}] of {who(member)} you escalated: {answer}"

    async def line(self, project: Project | None, event: AppEvent) -> str:
        p = event.payload
        member = await self.manager.staff.get(event.staff_id) if event.staff_id else None
        task = await self._task_bit(p.get("task_id")) if p.get("task_id") else ""
        kind = event.type
        if kind == "staff.status":
            status = p.get("status")
            detail = _one_line(str(p.get("detail") or ""), 200)
            if status == "turn_done_unseen":
                headline = await self._headline(member)
                on = await self._on_task(member)
                quoted = f': "{headline}"' if headline else ""
                return f"{who(member)} finished a turn{on}{quoted} — ReadStaff(\"{member.name if member else ''}\") for the whole reply"
            if status == "error":
                return f"{who(member)} stopped with an error{await self._on_task(member)}: {detail or 'no detail'}"
            if status == "exited":
                return f"{who(member)}'s session ended{': ' + detail if detail else ''}"
            return f"{who(member)} has gone silent{await self._on_task(member)}{': ' + detail if detail else ''} (silent is not failed)"
        if kind == "staff.report":
            if p.get("implicit"):
                name = member.name if member else ""
                ending = "ended a turn with a question and no report" if p.get("kind") == "needs_input" else "finished a turn without a report"
                return f"{who(member)} {ending}{(' on ' + task) if task else ''}: \"{_one_line(str(p.get('text') or ''), IMPLICIT_REPORT_CHARS)}\" — ReadStaff(\"{name}\") for the whole turn"
            # A report is the member's answer to the orchestrator and arrives whole: cut at 300
            # characters, a detailed reply reached it as a stub, and no tool could show the rest.
            return f"{who(member)} reported {p.get('kind')}{(' on ' + task) if task else ''}: \"{_verbatim(str(p.get('text') or ''))}\"{refs_line(p.get('files'))}"
        if kind == "staff.channel":
            if p.get("team_tools") == "missing":
                return f"{who(member)}'s team tools are not connected: {_one_line(str(p.get('detail') or ''), 200)}. They keep working, but will not Report or AskOrchestrator; Tell and ReadStaff still work"
            return f"{who(member)}'s team tools are connected now"
        if kind in ("ask.pending", "permission.pending"):
            ask = await self._ask_for_event(p)
            short = f" [{ask.short_id}]" if ask is not None else ""
            routed = ask.routed_to if ask is not None else str(p.get("routed_to") or "")
            tail = " — yours to answer or escalate" if routed == "orchestrator" else " — the operator decides; you are told"
            if kind == "permission.pending":
                text = _one_line(str(p.get("text") or (ask.text if ask else "")), 200)
                tool = str(p.get("tool") or "")
                autonomy = project.settings.orchestrator.autonomy if project is not None else ""
                return f"{who(member)} needs permission{short}: {tool + ': ' if tool and not text.startswith(tool) else ''}{text} (autonomy {autonomy}){tail}"
            questions = p.get("questions") or []
            first = questions[0] if questions and isinstance(questions[0], dict) else {}
            text = _one_line(str(first.get("question") or (ask.text if ask else "")), 240)
            options = " / ".join(str(o.get("label") or "") for o in first.get("options") or [] if isinstance(o, dict))
            return f"{who(member)} asks{short}: {text}{' — options: ' + options if options else ''}{tail}"
        if kind in ("ask.answered", "permission.resolved"):
            ask = await self._ask_for_event(p)
            if ask is None:
                return kind
            return await self._answer_line(ask)
        if kind in TASK_WAKES:
            actor = self._actor(str(p.get("actor") or ""), member)
            title = f"\"{_one_line(str(p.get('title') or ''), 80)}\" ({p.get('task_id')})"
            if kind == "task.created":
                return f"{actor} added {title} to the board ({p.get('to') or 'todo'})"
            if kind == "task.moved":
                return f"{actor} moved {title} {p.get('from')} → {p.get('to')}"
            if kind == "task.assigned":
                assignee = await self.manager.staff.get(str(p.get("assignee_staff_id") or "")) if p.get("assignee_staff_id") else None
                error = f" ({_one_line(str(p.get('error')), 200)})" if p.get("error") else ""
                return f"{title} assigned to {assignee.name}{error}" if assignee else f"{title} is unassigned{error}"
            if kind == "task.accepted":
                return f"{actor} accepted {title}"
            return f"merging {title} failed: {_one_line(str(p.get('error') or 'conflicts'), 300)}"
        if kind == "run.started":
            return f"the operator wrote to {who(member)} directly"
        if kind == "schedule.fired":
            return f"your wake-up [{p.get('schedule_id')}] fired: {_one_line(str(p.get('note') or p.get('name') or ''), 200)}"
        if kind == "watch.fired":
            said = " — ".join(_one_line(str(p[key]), 240) for key in ("detail", "note") if p.get(key))
            return f"watch [{p.get('watch_id')}] fired{': ' + said if said else ''}"
        if kind in ("dispatch.created", "dispatch.message"):
            title = str(p.get("title") or "")
            text = _one_line(str(p.get("text") or ""), 600)
            if kind == "dispatch.message" and p.get("kind") == "cancelled":
                return f"[from the main orchestrator] dispatch {p.get('dispatch_id') or ''} is cancelled: {text}"
            what = "follow-up on dispatch" if kind == "dispatch.message" else "dispatch"
            number = f" #{p.get('seq')}" if kind == "dispatch.created" and p.get("seq") else ""
            return f"[from the main orchestrator] {what} {p.get('dispatch_id') or ''}{number}{': ' + title if title else ''}: {text}{refs_line(p.get('files'))}"
        return kind

    @staticmethod
    def _actor(actor: str, member: Staff | None) -> str:
        if actor == "staff" and member is not None:
            return member.name
        return {"operator": "the operator", "system": "the board", "agent": "an agent"}.get(actor, "someone")

    @staticmethod
    def _answer_words(ask: Ask) -> list[str]:
        """What the operator gave as an answer, each part as they gave it: the option they chose,
        the answer they typed, the note or reason beside it."""
        r = ask.resolution
        if ask.resolved_by in (None, "system", "orchestrator") or ask.kind == "folder":
            return []
        parts = [", ".join(str(s) for s in r.get("selected") or []) if ask.kind != "permission" else "", str(r.get("text") or ""), str(r.get("note") or "")]
        return [part.strip() for part in dict.fromkeys(parts) if part.strip()]

    @staticmethod
    def _answer_text(ask: Ask) -> str:
        r = ask.resolution
        if ask.kind == "permission":
            always = f"granted always ({r['rule']})" if r.get("rule") else "granted always"
            said = (always if r.get("always") else "granted") if r.get("allow") else "refused"
            return f"{said} — the operator said: {_verbatim(str(r['note']))}" if r.get("note") else said
        if ask.kind == "folder":
            if r.get("outcome") and str(r["outcome"]) != "added":
                # Said in full, with the advice after the dash: "approved" alone once sent the
                # orchestrator to ask again for a folder the host had refused.
                return f"{r['outcome']} — nothing was added; do not ask for it again unless that reason is gone"
            return "approved" if r.get("allow") or (r.get("selected") and r["selected"][0] == (ask.detail.get("options") or ["Add"])[0]) else "declined"
        parts = [", ".join(str(s) for s in r.get("selected") or []), str(r.get("text") or "")]
        said = " — ".join(p for p in parts if p)
        if r.get("note"):
            # The operator's words beside the option they chose: part of the answer, not a remark.
            said = f"{said} — note: {r['note']}" if said else str(r["note"])
        return _verbatim(said) or "(no text)"

    async def _task_bit(self, task_id: Any) -> str:
        row = await self.manager.db.fetchone("SELECT id, title FROM board_tasks WHERE id = ?", (str(task_id),))
        return f"\"{_one_line(row['title'], 80)}\" ({row['id']})" if row is not None else str(task_id)

    async def _on_task(self, member: Staff | None) -> str:
        if member is None:
            return ""
        session = await self.manager.staff.live(member.id)
        if session is None or not session.task_id:
            return ""
        return " on " + await self._task_bit(session.task_id)

    async def _headline(self, member: Staff | None) -> str:
        """The start of the member's last reply, so a finished turn can often be judged without reading it."""
        team = self.team
        if member is None or team is None:
            return ""
        try:
            live = await team.live_of(member)
            if live is None:
                return ""
            async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                page = await team.runtime(member).read(live, ReadRequest(what="last", max_chars=self.manager.config.staff.report_summary_chars))
        except Exception:  # noqa: BLE001 — the line stands without it
            return ""
        return _one_line(page.text, HEADLINE_CHARS)

    async def target_state(self, project_id: str) -> TargetState:
        project = await self.manager.projects.get(project_id)
        session_id = self.session_of(project) if project is not None else ""
        if not session_id:
            return "gone"
        state = await self.manager.get_state(session_id)
        if state is None:
            self._replace_soon(project_id, "its session could not be loaded")
            return "gone"
        if state.pending is not None:
            return "waiting"
        if state.running and not (state.engine is not None and state.engine.is_terminal):
            return "running"
        return "idle"

    def _replace_soon(self, project_id: str, reason: str) -> None:
        if project_id in self._replacing:
            return
        self._replacing.add(project_id)

        async def go() -> None:
            try:
                await self.replace(project_id, reason, by="system")
            except Exception:  # noqa: BLE001
                logger.exception("could not replace the orchestrator of %s", project_id)
            finally:
                self._replacing.discard(project_id)

        task = asyncio.create_task(go(), name=f"orchestrator-replace:{project_id}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def deliver(self, project_id: str, text: str, *, steer: bool) -> None:
        """Hand a batch to the orchestrator: a new turn when it is idle, a steer while it runs.

        A routine batch that finds a run started meanwhile waits for the end of that turn rather than
        interrupting it. A refusal (no model, a budget, the host still starting) is raised, and the
        queue keeps the events and tries again later.
        """
        project = await self.project(project_id)
        session_id = self.session_of(project)
        if not session_id:
            raise RuntimeError("the project has no orchestrator session")
        # The operator's answers among the events are named, so compaction quotes them whole and
        # takes the rest of the batch for the host's news it is.
        words = self._operator_words.pop(text, [])
        try:
            await self.manager.submit(session_id, text, origin="events", as_answer=False, steer=steer, follow_up=not steer, operator_words=words)
        except Exception:
            if words:
                self._operator_words[text] = words  # the queue delivers the same text again
            raise

    async def _capped(self, project_id: str, wait_seconds: float) -> None:
        notifications = self.app.notifications
        project = await self.manager.projects.get(project_id)
        if notifications is None or project is None:
            return
        limit = self.manager.config.orchestrator.max_wakes_per_hour
        await notifications.post(Draft(
            "orchestrator_report",
            f"{project.name}: the orchestrator reached {limit} wake-ups this hour",
            f"Routine events wait about {max(1, round(wait_seconds / 60))} min; questions, permissions and errors still wake it.",
            kind="orchestrator_capped",
            project_id=project_id,
            session_id=self.session_of(project) or None,
            dedupe_key=f"orchestrator-capped:{project_id}",
            source="orchestrator",
        ))

    async def stuck(self, project_id: str, reason: str) -> None:
        """The orchestrator cannot be woken, and waiting will not change that: say so once, where it shows.

        The operator gets a notification and a line in the orchestrator's chat (an empty chat said
        nothing), and every open dispatch of the project is marked blocked with the reason, which
        wakes the main orchestrator and turns its card from "in progress" to "blocked". The reason is
        kept, so a restart that meets the same refusal does not announce it a second time; a dispatch
        handed over while it stays stuck is still blocked, with a line of its own in the chat.
        """
        key = STUCK_KEY.format(project_id=project_id)
        told = await self.manager.db.kv_get(key) == reason
        project = await self.manager.projects.get(project_id)
        if project is None:
            return
        if not told:
            await self.manager.db.kv_set(key, reason)
        session_id = self.session_of(project)
        why = f"the orchestrator of {project.name} cannot run: {reason}"
        blocked: list[Any] = []
        dispatches: Any = self.app.extensions.get("dispatches")
        if dispatches is not None:
            try:
                blocked = await dispatches.block_open(project, why)
            except Exception:  # noqa: BLE001 — the operator is still told below
                logger.warning("could not mark the dispatches of %s blocked", project_id, exc_info=True)
        if told and not blocked:
            return
        clock = self._clock(_now())
        lines = [] if told else [f"- {clock} the orchestrator could not be woken; its turn stopped with an error before it began: {reason}"]
        lines += [f"- {clock} dispatch {d.id} (#{d.seq}) is marked as blocked: the orchestrator cannot run until this is fixed" for d in blocked]
        await self._note(session_id, f"[events · {project.name} · {len(lines)} since {clock}]\n" + "\n".join(lines))
        if told:
            return
        await self.manager.projects.record(project_id, "system", "orchestrator", f"The orchestrator cannot be woken: {reason}", {"session_id": session_id})
        notifications = self.app.notifications
        if notifications is not None:
            held = f" Dispatch {', '.join(d.id for d in blocked)} is marked blocked until then." if blocked else ""
            await notifications.post(Draft(
                "orchestrator_report",
                f"{project.name}: the orchestrator cannot run",
                f"{reason}.{held} It tries again by itself every so often, and at once when it is replaced.",
                kind="orchestrator_stuck",
                tone="error",
                project_id=project_id,
                session_id=session_id or None,
                dedupe_key=f"orchestrator-stuck:{project_id}",
                source="orchestrator",
            ))

    async def unstuck(self, project_id: str) -> None:
        """A wake-up went through again: the dispatches this blocked go back to open, and the chat says so."""
        key = STUCK_KEY.format(project_id=project_id)
        if await self.manager.db.kv_get(key) is None:
            return
        await self.manager.db.kv_set(key, None)
        project = await self.manager.projects.get(project_id)
        if project is None:
            return
        dispatches: Any = self.app.extensions.get("dispatches")
        reopened = await dispatches.unblock(project) if dispatches is not None else []
        await self.manager.projects.record(project_id, "system", "orchestrator", "The orchestrator can be woken again.", {})
        clock = self._clock(_now())
        lines = [f"- {clock} the orchestrator runs again"]
        lines += [f"- {clock} dispatch {d.id} (#{d.seq}) is open again" for d in reopened]
        await self._note(self.session_of(project), f"[events · {project.name} · {len(lines)} since {clock}]\n" + "\n".join(lines))

    async def _note(self, session_id: str, text: str) -> None:
        """A line in the orchestrator's chat that the model never reads: written to the transcript the
        app shows, not to the history its turns are built from. It reads like a batch of events, so the
        app draws it as one."""
        if not session_id:
            return
        note = Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": "events", "daedalus.notice": True, OPERATOR_WORDS_METADATA_KEY: False})
        try:
            await self.manager.sessions.append_transcript(session_id, [note])
        except Exception:  # noqa: BLE001 — the notification still says it
            logger.warning("could not write a note into the chat of %s", session_id, exc_info=True)

    # -- following the sessions ----------------------------------------------------------------------

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        state = self.manager.live_state(session_id)
        project_id = str(state.metadata.get("orchestrator_of") or "") if state is not None else ""
        queue = self.queues.get(project_id) if project_id else None
        if queue is not None:
            queue.poke()

    async def on_session_deleted(self, session_id: str) -> None:
        """The operator deleted the orchestrator's chat: the orchestrator is off, and the journal says so."""
        row = await self.manager.db.fetchone("SELECT id FROM projects WHERE json_extract(settings, '$.orchestrator.session_id') = ?", (session_id,))
        if row is None:
            return
        project_id = str(row["id"])
        async with self.lock(project_id):
            await self.manager.projects.update_orchestrator(project_id, enabled=False)
            await self.manager.projects.set_orchestrator(project_id, expect=session_id, value="")
            await self._left_office(project_id)
        await self._hand_requests_to_operator(project_id, why="the orchestrator's session was deleted")
        await self.manager.projects.record(project_id, "system", "orchestrator", f"The orchestrator's session {session_id} was deleted; the orchestrator is off.", {"session_id": session_id})
        await self._changed(project_id, "orchestrator.disabled", "operator")

    async def notification_policy(self, project_id: str) -> ProjectNotifyPolicy:
        """What the notification router holds for this project: a staff member's request waits for the
        orchestrator a little before the operator hears of it — not at all when the operator decides."""
        project = await self.manager.projects.get(project_id)
        if project is None or not project.settings.orchestrator.enabled:
            return ProjectNotifyPolicy()
        orchestrator = project.settings.orchestrator
        hold = 0 if orchestrator.autonomy == "ask" else self.manager.config.notifications.orchestrator_hold_seconds
        return ProjectNotifyPolicy(orchestrated=True, hold_seconds=hold, orchestrator_session_id=orchestrator.session_id or None)

    # -- its own requests -----------------------------------------------------------------------------

    async def open_request(self, project: Project, session_id: str, *, kind: str, text: str, options: list[str], detail: dict[str, Any], task_id: str | None = None, dispatch_id: str | None = None, title: str = "") -> Ask:
        """A request of the orchestrator's own to the operator: a row, and the event the router and the app show."""
        ask = await self.manager.asks.open(project.id, origin="orchestrator", kind=kind, title=title, text=text, routed_to="operator", task_id=task_id, detail={**detail, "options": options}, dispatch_id=dispatch_id)
        ref = f"orchestrator:{project.id}:{ask.id}"
        await self.manager.db.execute("UPDATE asks SET detail_json = json_set(detail_json, '$.event_ref', ?) WHERE id = ?", (ref, ask.id))
        ask = (await self.manager.asks.get(ask.id)) or ask
        run_id = ""
        state = self.manager.live_state(session_id)
        if state is not None:
            run_id = state.run_id or ""
        await self.manager.bus.publish(
            "ask.pending",
            {
                "request_id": ask.id,
                "request_ref": ref,
                "run_id": run_id,
                "title": f"{project.name} · orchestrator",
                # The title leads, so a notification or a lock screen names the decision before its detail.
                "questions": [{
                    "question": (f"{title}\n\n{text}" if title else text)[:QUESTION_MAX],
                    "options": [{"label": o, "description": ""} for o in options],
                    "multi": bool(detail.get("multi")),
                    # Words are an answer only to a question: a folder or a project is added or not.
                    "custom": kind == "question",
                }],
                "operator_facing": True,
                "telegram": False,
                "short_id": ask.short_id,
                "routed_to": "operator",
                "kind": kind,
                **({"dispatch_id": ask.dispatch_id} if ask.dispatch_id else {}),
            },
            project_id=project.id,
            session_id=session_id,
        )
        return ask

    async def deliver_own(self, ask: Ask, *, allow: bool | None, text: str | None, selected: list[str] | None) -> tuple[bool, str]:
        """The operator answered one of the orchestrator's own requests. A question needs nothing
        delivered — its answer wakes the orchestrator as an event — and an approved folder is added."""
        if ask.kind != "folder":
            return True, ""
        options = list(ask.detail.get("options") or [])
        approved = bool(allow) or bool(selected and options and selected[0] == options[0])
        if not approved:
            await self.manager.projects.record(ask.project_id, "system", "folder", f"The operator declined the folder {ask.detail.get('path')}.", {"ask_id": ask.id})
            return True, ""
        try:
            await self.manager.projects.add_folder(ask.project_id, str(ask.detail.get("path") or ""), label=str(ask.detail.get("label") or ""), env=str(ask.detail.get("env") or "") or None, readonly=bool(ask.detail.get("readonly")))
        except (ProjectError, KeyError) as exc:
            reason = str(exc) if isinstance(exc, ProjectError) else "the project is gone"
            await self._approval_failed(ask, f"the folder {ask.detail.get('path')}", reason)
            return False, reason
        await self._outcome(ask, "added")
        await self.manager.projects.record(ask.project_id, "system", "folder", f"The operator approved the folder {ask.detail.get('path')}; it was added.", {"ask_id": ask.id})
        await self.manager.reload_project(await self.manager.projects.get(ask.project_id), ask.project_id)
        await self._changed(ask.project_id, "folders", "operator")
        return True, ""

    async def _outcome(self, ask: Ask, outcome: str, error: str = "") -> None:
        """What came of an approved request, kept on its resolution: the wake-up line the orchestrator
        reads is written from it, after this, so the orchestrator learns what happened, not only
        what the operator pressed. ``error`` is the bare reason, which the card shows."""
        await self.manager.db.execute(
            "UPDATE asks SET resolution_json = json_set(resolution_json, '$.outcome', ?, '$.error', ?) WHERE id = ?", (outcome, error, ask.id)
        )

    async def _approval_failed(self, ask: Ask, what: str, reason: str) -> None:
        """The operator approved a request and carrying it out failed. They are told where they look —
        a notification and a line in the orchestrator's chat — and the orchestrator is told the exact
        reason in the wake-up the answer brings. The failure once went only into the journal: the
        orchestrator saw "approved", found nothing added, and asked again, and the operator approved
        twice without ever learning why nothing happened."""
        assert ask.project_id is not None
        await self._outcome(ask, f"approved, but {what} could not be added: {reason}", reason)
        await self.manager.projects.record(ask.project_id, "system", "folder", f"{what[0].upper()}{what[1:]} was approved but could not be added: {reason}", {"ask_id": ask.id})
        project = await self.manager.projects.get(ask.project_id)
        if project is None:
            return
        session_id = self.session_of(project)
        clock = self._clock(_now())
        await self._note(session_id, f"[events · {project.name} · 1 since {clock}]\n- {clock} you approved request [{ask.short_id}], but {what} could not be added: {reason}")
        notifications = self.app.notifications
        if notifications is not None:
            await notifications.post(Draft(
                "orchestrator_report",
                f"{project.name}: {what} could not be added",
                f"You approved request {ask.short_id}, and it failed: {reason}.",
                kind="orchestrator_request_failed",
                tone="error",
                project_id=project.id,
                link=f"/app/project/{project.id}",
                dedupe_key=f"orchestrator-request-failed:{ask.id}",
                source="orchestrator",
            ))

    # -- the tools' hook ------------------------------------------------------------------------------

    async def service(self, operation: str, /, **kwargs: Any) -> Any:
        """The tools' hook: every operation first checks that the calling session holds the office.
        The operation is positional because several tools have an argument called ``op`` of their own."""
        return await orchestrator_ops.dispatch(self, operation, OPERATIONS, **kwargs)

    def folder_access(self, project: Project, folder: ProjectFolder, session_id: str) -> FolderAccess:
        """How ``Peek`` reads a folder: directly when it is this process's, through the host bridge when
        it is a host folder seen from the container, otherwise with the reason it cannot."""
        if not folder.local(self.manager.projects.local_env):
            if folder.env != "host":
                return UnreachableFolder(
                    f"{folder.path} is a {folder.env} folder, which this process cannot read; staff that run there can, "
                    "so ask one to look, or ask the operator"
                )
            bridge: Any = self.app.extensions.get("host_bridge")
            if bridge is None:
                return UnreachableFolder(f"{folder.path} is on the host, and this installation has no host terminal bridge to read it through (bash deploy/setup.sh offers to install it)")
            if not bridge.available():
                return UnreachableFolder(f"{folder.path} is on the host, and the host terminal bridge is not answering now; ask the operator to check the host terminal (systemctl --user status daedalus-ptyd)")
            roots = [str(f.path) for f in project.folders if f.env == folder.env]
            return BridgedFolderAccess(str(folder.path), roots=roots, bridge=bridge, env=folder.env)
        services = self.manager.locator_services(session_id)
        if services is None:
            return UnreachableFolder("this session's services are not loaded; try again in its next turn")
        return LocalFolderAccess(Path(folder.path), services)

    def attach(self) -> None:
        manager = self.manager
        manager.service_hooks["orchestrator"] = self.service
        manager.turn_notes_hooks.append(self.turn_notes)
        manager.preset_hooks.append(self.preset_for)
        manager.on_finished(self.on_run_finished)
        manager.delete_hooks.append(self.on_session_deleted)
        notifications = self.app.notifications
        if notifications is not None and hasattr(notifications, "set_project_policy"):
            notifications.set_project_policy(self.notification_policy)
            if self.team is not None:
                # The operator answers the orchestrator's own questions from a notification the same
                # way as a staff member's: the request row is the same kind of row.
                notifications.register_resolver("orchestrator", self.team.resolve_action)
        if self.team is not None:
            self.team.own_requests = self

    async def resume(self) -> None:
        """Start the wake queues of every project that has an orchestrator, each from its cursor."""
        for project in await self.manager.projects.list():
            orchestrator = project.settings.orchestrator
            if orchestrator.enabled and orchestrator.session_id:
                self._models[project.id] = orchestrator.model
                try:
                    await self.give_home(project)
                except Exception:  # noqa: BLE001 — its queue still starts, and says why it cannot deliver
                    logger.exception("could not move the orchestrator of project %s into a directory of its own", project.id)
                try:
                    await self.start_queue(project.id)
                except Exception:  # noqa: BLE001 — one project's queue must not keep the others from starting
                    logger.exception("could not start the wake-ups of project %s", project.id)



    async def give_home(self, project: Project) -> bool:
        """Move a current orchestrator made before :meth:`needs_home` existed out of a folder it cannot
        run in. Whether it was moved.

        Such a session was created with the project's host folder as its working directory, and in
        the container every wake-up was refused ("the working directory … is not reachable") while
        the events waited. Once it has a home the waiting events are delivered by its queue as usual.
        This runs at every start and does nothing for a session that is already right, so it is the
        one-shot repair for the sessions stored before the fix and a no-op ever after.
        """
        session_id = project.settings.orchestrator.session_id
        if not session_id or not self.needs_home(project):
            return False
        row = await self.manager.db.fetchone("SELECT metadata FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            return False
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if home_of(metadata):
            return False
        await self.manager.set_home(session_id, HOME_NAME.format(project_id=project.id))
        logger.info("the orchestrator %s of %s runs in a directory of its own from now on", session_id, project.name)
        return True


def lasting(exc: BaseException) -> bool:
    """Whether a refused wake-up will be refused the same way until somebody acts: a working directory
    that is not there or not writable, no model configured. The host restarting, a budget, a
    maintenance window pass by themselves and are only retried."""
    return isinstance(exc, (WorkspaceUnreachable, NoModelConfigured))


async def install(app: Application) -> list[asyncio.Task[None]]:
    orchestrators = Orchestrators(app)
    app.extensions["orchestrator"] = orchestrators
    orchestrators.attach()
    await orchestrators.resume()
    return []


__all__ = ["CURSOR_KEY", "NotCurrent", "Orchestrators", "fit", "install", "who"]
