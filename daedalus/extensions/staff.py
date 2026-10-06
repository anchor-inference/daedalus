"""The team at work: assignments, the launch queue, statuses, requests and control of staff sessions.

``app.extensions["staff"]`` is the one place a staff member is started, told something, paused,
released or answered, whichever executor runs it. The runtimes (``runtimes[<harness>]``) do what only
they can — start a session, deliver a message, stop it — and report back through :class:`Ingress`,
which writes the staff tables and publishes the bus events in one place. Daedalus staff are run here;
the command-line harnesses register their runtimes when they are installed.

Two sources could say a member is waiting: the Daedalus session's own pending question and the
request row. The row is the one the team reads, and this module is its only writer: a question is
recorded from the session's pending event, and an answer given anywhere — the app's session view,
the request list, the orchestrator — resolves the row first, by compare-and-set, before it reaches the
session. Whoever updates the row delivers; everyone else is told who was first.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import posixpath
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.extensions.board import NOTES_MAX_CHARS
from daedalus.extensions.launch_controls import launch_resources
from daedalus.extensions.notifications import ActionConflict, ActionOutcome, ActionRefused, Draft
from daedalus.extensions.runtime_observations import admit_native_run, enter_runtime, observe_exit, observe_no_entry
from daedalus.extensions.staff_results import StaffReportService
from daedalus.extensions.task_context import ContextUnavailable, assemble_task_context, render_task_context
from daedalus.extensions.task_contract import RETURNED, Contracts
from daedalus.harness.capabilities import CAPABILITIES, RESTRICTIVE_MODES
from daedalus.harness.contract import mcp_server, standing_rule
from daedalus.harness.health import ChannelHealth, channel_health
from daedalus.host import prompts
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.handoff import Delivered, Handoff, box_name
from daedalus.host.launch_queue import Entry, LaunchQueue, MachineCapacity, TerminalsCapacity
from daedalus.host.staff_daedalus import DaedalusStaffRuntime
from daedalus.host.worktrees import (
    WORKTREES_DIR,
    StaffWorktrees,
    Worktree,
    WorktreeError,
    WorktreeRefused,
    branch_name,
    staff_slug,
)
from daedalus.staff_runtime import (
    AskRef,
    BoardTask,
    Decision,
    LiveSession,
    OutgoingMessage,
    Receipt,
    StaffRuntime,
    StartRequest,
    UsageSnapshot,
)
from daedalus.stores.capacity import claim_in, finish_in
from daedalus.stores.control import ControlDenied, Principal, one
from daedalus.stores.files import FileRefused, StoredFile
from daedalus.stores.projects import Project, ProjectFolder, ProjectSettings
from daedalus.stores.runtime_release import physical_exit_in
from daedalus.stores.staff import (
    ACTIVE_STATUSES,
    DAEDALUS_EFFORTS,
    HARNESS_NAMES,
    Ask,
    SetupFailed,
    Staff,
    StaffBusy,
    StaffError,
    StaffSession,
)
from daedalus.stores.staff_context import ContextPinRefused, pin_staff_context
from daedalus.stores.writer_leases import WriterLease, WriterLeases
from daedalus.terminals.bridge import HostBridge

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.host.session_runner import SessionManager
    from daedalus.terminals.service import Terminals

logger = logging.getLogger(__name__)

TICK_SECONDS = 30.0
"""How often silence, stale requests and the machine's capacity are looked at."""
FIRST_PUMP_SECONDS = 20.0
"""After a start, the queue waits this long before launching what was assigned before the restart:
the host first continues the runs it left behind and refuses new ones meanwhile."""
NOTE_MAX = 12000
RESULT_MAX = 1500
"""How much of a done report is kept on its card: the summary, not the whole transcript of the work."""
BASIS_MIN = 12
"""The least a quoted allowance may be: a line of the brief, not a word that happens to occur in it."""
STAFF_RULES_CHARS = 2000
"""How much of a brief the project's rules and constraints may take together."""
STAFF_CONSTRAINTS_MIN = 400

OPEN_TASK = ("todo", "blocked")
FINISHED_TASK = ("done", "dropped")
ABNORMAL = ("error", "no_signal")
SETTLED_TASK = ("review", "done", "dropped")
"""A task its member has handed in or that is over: whatever the member does now is not the task's
work, and its silence is nobody's concern."""
RECOVERED_KEY = "handoff:inbox_briefs_recovered"
INBOX_PATH_RE = re.compile(r"(/[^\s\"'`<>]*?/inbox/[^\s\"'`<>]+)")
"""A path to a file in an inbox, as a brief written before handles existed names it."""
SENT_BACK = "sent back by the "
"""How a rejection from review is written into a task's notes (see ``review.py``)."""


def no_worktree(member: Staff, task: BoardTask, why: Exception) -> str:
    """Why a member who works in a worktree of their own cannot take a task, and the two ways out."""
    return (
        f"{member.name} works in a git worktree of their own, and task {task.id} cannot have one: {why}. "
        f"Give the task a folder of the project that is a git repository, or set {member.name}'s isolation "
        f"to shared (they work in the folder itself) or read-only"
    )


class AlreadyAnswered(StaffError):
    """A request someone else answered first; the message says who."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _age_seconds(at: str | None, now: datetime) -> float:
    if not at:
        return 0.0
    try:
        then = datetime.fromisoformat(at)
    except ValueError:
        return 0.0
    then = then if then.tzinfo else then.replace(tzinfo=UTC)
    return (now - then).total_seconds()


def _task(row: Any) -> BoardTask:
    try:
        brief = json.loads(row["brief_json"] or "{}")
    except (TypeError, ValueError):
        brief = {}
    brief = brief if isinstance(brief, dict) else {}
    try:
        depends = tuple(str(d) for d in json.loads(row["depends_on"] or "[]"))
    except (TypeError, ValueError):
        depends = ()
    return BoardTask(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        priority=int(row["priority"]),
        objective=str(brief.get("objective") or ""),
        deliverable=str(brief.get("deliverable") or ""),
        boundaries=str(brief.get("boundaries") or ""),
        done_when=str(brief.get("done_when") or ""),
        project_id=row["project_id"],
        folder_id=row["folder_id"],
        assignee_staff_id=row["assignee_staff_id"],
        branch=row["branch"],
        depends_on=depends,
        sent_back=_sent_back(row),
        returned=_returned(row),
    )


def _sent_back(row: Any) -> str:
    """The latest "sent back" note of a task the operator rejected from review, else nothing."""
    if row["merge_state"] != "rejected":
        return ""
    for line in reversed(str(row["notes"] or "").splitlines()):
        if SENT_BACK in line:
            return line.split(SENT_BACK, 1)[1].partition(": ")[2].strip()
    return ""


def _returned(row: Any) -> str:
    """The orchestrator's latest note when it checked the work and returned it, else nothing."""
    if row["acceptance_state"] != "returned":
        return ""
    for line in reversed(str(row["notes"] or "").splitlines()):
        if RETURNED in line:
            return line.split(RETURNED, 1)[1].strip()
    return ""


async def claim_host_slot(app: Any, entry: Entry) -> str | None:
    """Take one of the machine's staff places for a launch, or say what it waits for.

    Each project's own limit let several projects' built-in workers together run past the one
    machine cap, and nothing kept a place free for a coordinator or reviewer. An entry without a
    durable attempt has no command to hold the place for, so it is not charged here.
    """
    if entry.attempt_id is None:
        return None
    async with app.db.transaction() as conn:
        generation = await app.executions._host(conn)
        return await claim_in(conn, attempt_id=entry.attempt_id, project_id=entry.project_id,
                              runtime_kind="cli" if entry.terminal else "daedalus",
                              role_class=entry.role_class, generation=generation,
                              cap=app.manager.config.terminals.running_cap)


async def finish_host_slot(app: Any, entry: Entry, started: bool) -> None:
    """Settle a launch's machine place: a failed start returns it, a started one stays charged
    until the worker's end is observed. A launch still waiting in the durable order needs no call:
    its claim is cancelled when its command settles."""
    if entry.attempt_id is None:
        return
    async with app.db.transaction() as conn:
        await finish_in(conn, entry.attempt_id, started=started)


class Team:
    """The project teams of this installation at work."""

    def __init__(self, app: Application, *, capacity: Any = None) -> None:
        self.app = app
        assert app.manager is not None
        self.manager: SessionManager = app.manager
        if self.manager.execution_store is None:
            self.manager.execution_store = app.executions
        elif self.manager.execution_store is not app.executions:
            raise RuntimeError("the staff runtime and model calls must share the same execution owner")
        self.runtimes: dict[str, StaffRuntime] = {"daedalus": DaedalusStaffRuntime(self.manager)}
        self._execution_locks: dict[str, asyncio.Lock] = {}
        # A host folder in Docker is worked in through the host terminal bridge; the service is
        # looked up per call, so a bridge installed after the start is used without a restart.
        self.host_bridge = HostBridge(lambda: cast("Terminals | None", app.extensions.get("terminals")))
        self.worktrees = StaffWorktrees(self.manager.projects.local_env, host=self.host_bridge)
        self.handoff = Handoff(self.manager.files, local_env=self.manager.projects.local_env, host=self._host_files)
        """Files handed to members before the brief that names them, and their artifacts taken back."""
        self.ingress = Ingress(self)
        self.manager.service_hooks["execution_run_admission"] = self.admit_run
        self._capacity = capacity
        """A fixed :class:`MachineCapacity` for tests; otherwise the terminals service is asked each time."""
        self.queue = LaunchQueue(
            concurrency=self._concurrency,
            active=self._active,
            ready=self._ready,
            free=self._free,
            launch=self._launch,
            capacity=self.capacity,
            stagger=lambda: self.manager.config.staff.launch_stagger_seconds,
            on_failure=self._launch_failed,
            reuses=self._reuses,
            check_reserved=self._reserved_capacity,
            active_in=self._active_in,
            concurrency_in=self._concurrency_in,
            claim_host=lambda entry: claim_host_slot(app, entry),
            finish_host=lambda entry, started: finish_host_slot(app, entry, started),
        )
        self._pause_commits: set[asyncio.Task[None]] = set()
        self.review: Any = None
        """Review and merge of staff branches (``review.py``), set at install."""
        self.own_requests: Any = None
        """The orchestrators, once installed: a request the orchestrator itself made (a question to the
        operator, a folder it wants) has no staff session to deliver the answer to, so they take it."""
        self.dispatcher_requests: Any = None
        """The main orchestrator, once installed: its confirmation before a project is created is a
        request of no project, which it settles itself."""
        self.contracts = Contracts(self.manager.db)
        """The cards' requirements, their deliveries to members, the checks and the acceptance."""
        self.report_hooks: list[Callable[[AppEvent], Awaitable[None]]] = []
        """Called with every member's report once it is published, before the call that made it
        returns: the orchestrators open the result as one to decide on, so a wake-up the report causes
        never finds it missing."""

    # -- lookups -----------------------------------------------------------------------------------

    def _host_files(self) -> Any:
        """The host bridge when this installation has a host terminal at all; ``None`` otherwise, so a
        refusal can say "there is none" rather than "it is not answering"."""
        terminals: Any = self.app.extensions.get("terminals")
        if terminals is None or not terminals.configured("host"):
            return None
        return self.host_bridge

    async def cwd_of(self, live: LiveSession) -> tuple[ProjectFolder, str]:
        """The folder a live session works in, and its working directory there: its worktree, else the
        folder. What a member was handed lies under it, in ``.agents/inbox/``."""
        project = await self.project(live.staff.project_id)
        folder = project.folder(live.session.folder_id) if live.session.folder_id else None
        folder = folder or self.folder_for(project, live.staff, None)
        state = self.manager.live_state(live.session.session_id) if live.session.session_id else None
        if state is not None and live.staff.harness == "daedalus":
            # Where its tools work: in a host folder that is the folder on the host, not the
            # directory of this process the session runs from.
            return folder, str(self.manager.work_dir(state))
        return folder, live.session.worktree_path or str(folder.path)

    async def hand_files(self, member: Staff, files: list[StoredFile], *, folder: ProjectFolder, cwd: str, task_id: str | None, by: str) -> list[Delivered]:
        """Put files where the member can open them; a :class:`StaffError` naming why when they cannot
        be, so nothing is sent that names a file the member does not have."""
        if not files:
            return []
        try:
            self.handoff.check_target(folder)
            return await self.handoff.deliver(files, env=folder.env, cwd=cwd, box=box_name(task_id), actor=by, member=member.name)
        except FileRefused as exc:
            raise StaffError(f"the files for {member.name} could not be delivered: {exc}") from exc

    async def brief_files(self, delivered: list[Delivered]) -> tuple[list[Delivered], list[Delivered]]:
        """A task's files split into those new to the member and those it was handed before.

        A task carries every file ever attached to it, and each new brief of the task listed them all
        again as "files handed to you": a follow-up assigned with no attachment arrived naming twenty
        screenshots from earlier rounds as if the operator had just sent them. The copies are still
        put in place (a new worktree has none), but only the new ones are named in the brief.
        """
        fresh: list[Delivered] = []
        earlier: list[Delivered] = []
        for d in delivered:
            (earlier if await self.manager.files.deliveries(d.file.id, d.path) > 1 else fresh).append(d)
        return fresh, earlier

    def capacity(self) -> MachineCapacity | None:
        if self._capacity is not None:
            return self._capacity  # type: ignore[no-any-return]
        terminals = self.app.extensions.get("terminals")
        return TerminalsCapacity(terminals) if terminals is not None else None

    async def project(self, project_id: str) -> Project:
        project = await self.manager.projects.get(project_id)
        if project is None:
            raise KeyError(project_id)
        return project

    async def member(self, staff_id: str) -> Staff:
        member = await self.manager.staff.get(staff_id)
        if member is None:
            raise KeyError(staff_id)
        return member

    async def live(self, staff_session_id: str) -> LiveSession | None:
        """A live session with its member; ``None`` once it has ended."""
        session = await self.manager.staff.session(staff_session_id)
        if session is None or not session.live:
            return None
        member = await self.manager.staff.get(session.staff_id)
        return LiveSession(member, session) if member is not None else None

    async def live_of(self, member: Staff) -> LiveSession | None:
        session = await self.manager.staff.live(member.id)
        return LiveSession(member, session) if session is not None else None

    async def live_for_session(self, session_id: str) -> LiveSession | None:
        """The live staff session a Daedalus session is, read from the session's own metadata so it
        works from the first event of the first run, before the start has recorded the session id."""
        state = self.manager.live_state(session_id)
        staff_session_id = str(state.metadata.get("staff_session_id") or "") if state is not None else ""
        if not staff_session_id:
            found = await self.manager.staff.by_session(session_id)
            staff_session_id = found.id if found is not None else ""
        return await self.live(staff_session_id) if staff_session_id else None

    def runtime(self, member: Staff) -> StaffRuntime:
        runtime = self.runtimes.get(member.harness)
        if runtime is None:
            raise StaffError(f"{HARNESS_NAMES.get(member.harness, member.harness)} staff cannot be started here yet: its runtime is not installed")
        return runtime

    async def silence_watched(self, session: StaffSession) -> bool:
        """Whether the member's silence means anything: only while a turn of it runs on a task that is
        still being worked. A member that finished its turn, has no task, or whose task is in review
        or over is quiet by right. Watching those once woke an orchestrator three times over to hear
        that members who had filed their reports and sat at their prompts had "gone silent"."""
        if session.status != "working" or not session.task_id:
            return False
        row = await self.manager.db.fetchone("SELECT status FROM board_tasks WHERE id = ?", (session.task_id,))
        return row is not None and row["status"] not in SETTLED_TASK

    async def health(self, live: LiveSession, now: datetime | None = None) -> ChannelHealth:
        """Whether the host still hears the member: the card, the staff view and ``Team(staff)`` all
        read this, so none of them can call a member reachable that another calls silent."""
        member = live.staff
        runtime = self.runtimes.get(member.harness)
        channel_of = getattr(runtime, "channel", None)
        channel = channel_of(live) if channel_of is not None else {}
        caps = CAPABILITIES.get(member.harness)
        config = self.manager.config
        # A command-line member is looked at by the screen reconcile after this long; a Daedalus
        # member is marked silent by the tick after its own, longer, time.
        silence = config.harness.no_signal_after_s if caps is not None else config.staff.silence_minutes * 60
        messages = [m for m in await self.manager.staff.messages(member.id, limit=10) if m.staff_session_id == live.id]
        return channel_health(
            status=live.session.status,
            team_tools=caps.team_tools if caps is not None else "builtin",
            channel=channel,
            last_signal_at=live.session.last_signal_at,
            messages=messages,
            now=now or datetime.now(UTC),
            silence_after_s=silence,
            # The same rule as the silence checks: a working member whose task is handed in is not
            # shown silent here when nothing would ever mark it so.
            expected=live.session.status != "working" or await self.silence_watched(live.session),
        )

    async def task(self, task_id: str) -> BoardTask | None:
        row = await self.manager.db.fetchone("SELECT * FROM board_tasks WHERE id = ?", (task_id,))
        return _task(row) if row is not None else None

    def folder_for(self, project: Project, member: Staff, task: BoardTask | None) -> ProjectFolder:
        for folder_id in ((task.folder_id if task else None), member.default_folder_id):
            if folder_id:
                folder = project.folder(folder_id)
                if folder is not None:
                    return folder
        if project.primary is None:
            raise StaffError(f"{project.name} has no folder to work in")
        return project.primary

    # -- publishing ----------------------------------------------------------------------------------

    async def publish(self, event_type: str, payload: dict[str, Any], *, member: Staff | None = None, project_id: str | None = None, session_id: str | None = None) -> AppEvent | None:
        """A bus event about the team; a bus that cannot write is logged, never the caller's failure."""
        try:
            return await self.manager.bus.publish(
                event_type,
                payload,
                project_id=project_id or (member.project_id if member else None),
                staff_id=member.id if member else None,
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001 — the change is written; the event is a courtesy to its subscribers
            logger.warning("could not publish %s", event_type, exc_info=True)
            return None

    async def _move_task(self, task: BoardTask, to: str, *, actor: str, actor_id: str | None = None, assignee: str | None = "", branch: str | None = None, folder_id: str | None = None, merge_state: str | None = None) -> BoardTask:
        """Move a task on the board and say so. ``assignee=""`` keeps the assignee; ``None`` clears it."""
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [to, _now()]
        if assignee != "":
            sets.append("assignee_staff_id = ?")
            params.append(assignee)
        if branch is not None:
            sets.append("branch = ?")
            params.append(branch)
        if folder_id is not None:
            sets.append("folder_id = ?")
            params.append(folder_id)
        if merge_state is not None:
            sets.append("merge_state = ?")
            params.append(merge_state)
        await self.manager.db.execute(f"UPDATE board_tasks SET {', '.join(sets)} WHERE id = ?", (*params, task.id))  # noqa: S608 — column names are this module's own
        moved = await self.task(task.id)
        assert moved is not None
        if to != task.status:
            payload: dict[str, Any] = {"task_id": task.id, "title": task.title, "from": task.status, "to": to, "actor": actor}
            if actor_id:
                payload["actor_id"] = actor_id
            if moved.assignee_staff_id:
                payload["assignee_staff_id"] = moved.assignee_staff_id
            await self.publish("task.moved", payload, project_id=moved.project_id)
        return moved

    async def hand_in_to(self, task: BoardTask, worktree: Worktree | None) -> str:
        """A report enters review until an exact result and verdict authorize completion."""
        return "review"

    async def record_result(self, task: BoardTask, member: Staff, note: str) -> None:
        """Keep what a member reported on its card, so the card says what came of the work and not only
        what was asked. The latest result is enough to read; the event log keeps every one."""
        await self.note_on_card(task.id, f"result from {member.name}: " + " ".join(note.split())[:RESULT_MAX])

    async def _set_assignee(self, task: BoardTask, staff_id: str | None, *, actor: str, error: str = "") -> None:
        await self.manager.db.execute("UPDATE board_tasks SET assignee_staff_id = ?, updated_at = ? WHERE id = ?", (staff_id, _now(), task.id))
        payload: dict[str, Any] = {"task_id": task.id, "title": task.title, "assignee_staff_id": staff_id or "", "actor": actor}
        if error:
            payload["error"] = error[:500]
        await self.publish("task.assigned", payload, project_id=task.project_id)

    # -- the launch queue's questions -----------------------------------------------------------------

    async def _concurrency(self, project_id: str) -> int:
        project = await self.manager.projects.get(project_id)
        return project.settings.orchestrator.concurrency if project is not None else 0

    async def _active(self, project_id: str) -> int:
        async with self.app.db.transaction() as conn:
            return await self._active_in(conn, project_id)

    async def _concurrency_in(self, conn: Any, project_id: str) -> int:
        row = await one(conn, 'SELECT settings FROM projects WHERE id = ?', (project_id,))
        return ProjectSettings.load(json.loads(row['settings'])).orchestrator.concurrency if row is not None else 0

    async def _active_in(self, conn: Any, project_id: str) -> int:
        """Count physical workers and promised pair slots in the caller's admission snapshot."""

        async with conn.execute("SELECT s.id,a.id AS attempt_id FROM staff_sessions s JOIN staff m ON m.id = s.staff_id"
                                " LEFT JOIN execution_attempts a ON a.staff_session_id = s.id"
                                " WHERE m.project_id = ? AND s.ended_at IS NULL AND s.status IN ("
                                + ','.join('?' for _ in ACTIVE_STATUSES) + ')', (project_id, *sorted(ACTIVE_STATUSES))) as cursor:
            rows = await cursor.fetchall()
        active = set()
        for row in rows:
            if row['attempt_id'] is None or not await physical_exit_in(conn, row['attempt_id']):
                active.add(row['id'])
        async with conn.execute("SELECT f.attempt_id,a.staff_session_id FROM comparison_funding_slots f"
                                " LEFT JOIN execution_attempts a ON a.id = f.attempt_id"
                                " WHERE f.project_id = ? AND f.state = 'held'", (project_id,)) as cursor:
            slots = await cursor.fetchall()
        held = 0
        for slot in slots:
            if slot['staff_session_id'] in active:
                continue
            if slot['attempt_id'] is not None and await physical_exit_in(conn, slot['attempt_id']):
                continue
            held += 1
        return len(active) + held

    async def _reserved_capacity(self, entry: Entry) -> bool:
        """Subtract only this entry's real unspent capacity claim, never an arbitrary slot ID."""

        async with self.app.db.transaction() as conn:
            generation = await self.app.executions._host(conn)
            slot = await one(conn, "SELECT f.*,g.state AS group_state,t.contract_revision AS current_contract"
                             " FROM comparison_funding_slots f JOIN comparison_groups g ON g.id = f.group_id"
                             " JOIN board_tasks t ON t.id = f.task_id WHERE f.id = ?", (entry.capacity_slot_id,))
            if (slot is None or slot['state'] != 'held' or slot['group_state'] not in ('planned','active')
                    or slot['project_id'] != entry.project_id or slot['staff_id'] != entry.staff_id
                    or slot['task_id'] != entry.task_id or slot['host_generation'] != generation
                    or slot['contract_revision'] != slot['current_contract'] or entry.terminal):
                return False
            return slot['attempt_id'] is None or not await physical_exit_in(conn, slot['attempt_id'])

    async def _ready(self, entry: Entry) -> str | None:
        task = await self.task(entry.task_id)
        if task is None:
            return "the task is gone"
        if task.status == "blocked":
            waiting = await self.open_dependencies(task)
            if waiting:
                return f"waits for {', '.join(waiting)} to finish"
            # Named for what it is: "waits for ec2179 to finish" was said of a card whose one
            # dependency had been done for six hours, and the orchestrator went to edit the
            # dependency away instead of moving the card it had set aside itself.
            return "is set aside as blocked, not waiting for any task; moving it to todo lets it start"
        return None

    async def open_dependencies(self, task: BoardTask) -> list[str]:
        """The tasks ``task`` waits on that are not finished yet."""
        waiting = []
        for dep in task.depends_on:
            row = await self.manager.db.fetchone("SELECT status FROM board_tasks WHERE id = ?", (dep,))
            if row is not None and row["status"] not in FINISHED_TASK:
                waiting.append(dep)
        return waiting

    async def _free(self, entry: Entry) -> str | None:
        """Why the member cannot take the entry now, or ``None``.

        Busy means the member's live session is working a task that is still being worked. It used to
        mean any live session whose status was one of the active ones, and a command-line session
        keeps the task it was started for and a status its screen last gave: three members idle at
        their prompts, their scouting tasks done an hour before, sat as "working on" those tasks
        while four new tasks waited for them and nothing was delivered.
        """
        live = await self._live_member(entry.staff_id)
        if live is None:
            return None
        live = await self.settle_stale(live)
        session = live.session
        current = await self.task(session.task_id) if session.task_id and session.task_id != entry.task_id else None
        if session.status in ACTIVE_STATUSES:
            # A Daedalus member's active status is a run of this host in progress: real work, with
            # or without a task, and a start would be refused until it ends.
            if (current is not None and current.status not in SETTLED_TASK) or session.kind != "cli":
                return f"{entry.staff_name} is still working" + (f" on {session.task_id}" if session.task_id else "")
            if not await self._reuses(entry):
                return f"{entry.staff_name} is finishing a turn; the next task starts when it ends"
        if session.pause_requested:
            return f"{entry.staff_name} is paused; a message or a new assignment resumes it"
        if current is not None and current.status == "doing":
            return f"{entry.staff_name}'s task {current.id} is still in doing; it has to be reported done, moved or released first"
        runtime = self.runtimes.get(live.staff.harness)
        continuity = getattr(runtime, "continuity", None)
        if session.kind == "cli" and continuity is not None and continuity(live) == "attaching":
            # Launching afresh now would end a session the host is about to take up again.
            return f"{entry.staff_name}'s session is being taken up again after the restart"
        return None

    async def _live_member(self, staff_id: str) -> LiveSession | None:
        member = await self.manager.staff.get(staff_id)
        return await self.live_of(member) if member is not None else None

    async def _reuses(self, entry: Entry) -> bool:
        """Whether the entry would go into its member's live session as the next message."""
        if entry.principal is not None:
            return False
        if entry.resume_from or await self.manager.db.kv_get(self._resume_key(entry.staff_id, entry.task_id)):
            return False
        live = await self._live_member(entry.staff_id)
        task = await self.task(entry.task_id)
        if live is None or task is None:
            return False
        project = await self.manager.projects.get(live.staff.project_id)
        if project is None:
            return False
        try:
            folder = self.folder_for(project, live.staff, task)
        except StaffError:
            return False
        return self._continues(live, folder)

    def _continues(self, live: LiveSession, folder: ProjectFolder) -> bool:
        """Whether a member's live command-line session takes a task in ``folder`` as a message.

        Only a session this host holds, past its start and not failed, and standing where the task
        is worked: a CLI cannot change the folder it runs in, and a task in a worktree of its own
        needs a CLI started in that worktree. Anything else is launched afresh, as before.
        """
        member, session = live.staff, live.session
        runtime = self.runtimes.get(member.harness)
        continuity = getattr(runtime, "continuity", None)
        if continuity is None or session.kind != "cli" or session.status == "error":
            return False
        if session.folder_id != folder.id or session.worktree_path or member.isolation == "worktree":
            return False
        return bool(continuity(live) == "live")

    async def settle_stale(self, live: LiveSession, now: datetime | None = None) -> LiveSession:
        """Settle a command-line session whose row says it works on a task that is over.

        A CLI's status is what its hooks and screen last said, and nothing said anything after an
        earlier host marked three idle members ``no_signal``: their rows stayed grey, "silent" on
        the health line, and counted as busy, long after their tasks were done. A row that says
        ``no_signal`` while its task is handed in or over, or ``working`` so with no signal for the
        silence time, is idle: whatever the CLI does now is not the task's work. A ``working`` row
        that still hears from its CLI is left alone: it may be a follow-up on the same task.
        """
        session = live.session
        if session.kind != "cli" or session.status not in ("working", "no_signal"):
            return live
        task = await self.task(session.task_id) if session.task_id else None
        if task is not None and task.status not in SETTLED_TASK:
            return live
        if session.status == "working" and _age_seconds(session.last_signal_at, now or datetime.now(UTC)) < self.manager.config.harness.no_signal_after_s:
            return live
        why = f"its task {task.id} is {task.status}" if task is not None else "it has no task"
        await self.ingress.status(live, "idle", detail=f"{why}; nothing is being worked")
        return (await self.live(live.id)) or live

    async def _launch(self, entry: Entry) -> None:
        member = await self.member(entry.staff_id)
        task = await self.task(entry.task_id)
        if task is None:
            raise StaffError(f"task {entry.task_id} is gone")
        source = None if entry.capacity_slot_id is not None else (
            entry.resume_from or await self.manager.db.kv_get(self._resume_key(entry.staff_id, entry.task_id)))
        if entry.principal is None or entry.check_authority is None:
            raise ControlDenied("a queued launch must retain its authenticated command")
        bound_resources = launch_resources.set(entry.resources)
        try:
            await self.start(member, task, principal=entry.principal, check_authority=entry.check_authority,
                             by=entry.by, resume_from=source, capacity_slot_id=entry.capacity_slot_id,
                             resources=entry.resources, source_head=entry.source_head)
        finally:
            launch_resources.reset(bound_resources)
        if entry.capacity_slot_id is None:
            await self.manager.db.execute("DELETE FROM kv WHERE key = ?", (self._resume_key(entry.staff_id, entry.task_id),))

    async def _launch_failed(self, entry: Entry, exc: BaseException) -> None:
        if entry.principal is not None:
            # The outbox preserves the command's failure or uncertainty. Removing the assignment
            # here used to hide a process whose start succeeded before its response was lost.
            return
        # An assignment that cannot start is taken back rather than retried by every pump: the task
        # stays on the board, unassigned, and the event says why.
        task = await self.task(entry.task_id)
        await self.manager.db.execute("DELETE FROM kv WHERE key = ?", (self._resume_key(entry.staff_id, entry.task_id),))
        if task is not None and task.assignee_staff_id == entry.staff_id:
            # On the card as well as in the event: the event wakes the orchestrator, the card is where
            # the operator looks when a task they saw assigned is unassigned again.
            await self.note_on_card(task.id, f"{entry.staff_name} could not start: {exc}")
            await self._set_assignee(task, None, actor="system", error=f"{entry.staff_name} could not start: {exc}")

    async def _set_up(self, member: Staff, task: BoardTask, project: Project, worktree: Worktree) -> None:
        """Run the project's setup command in a worktree that was just made, before the worker starts.

        Only a new worktree: a reused one keeps what was installed in it. A worktree whose last setup
        failed is set up again though, or one bad ``npm ci`` would leave it bare for good. The outcome
        and the tail of the output are kept under the worktree's key. A failure stops the launch: the
        card says why with the last lines of the output, and the task stays unstarted.
        """
        command = project.settings.setup_command
        if not command:
            return
        key = f"worktree_setup:{worktree.env}:{worktree.path}"
        previous = await self.manager.db.kv_get(key)
        if not worktree.created and (previous is None or previous.get("ok")):
            return
        try:
            result = await self.worktrees.run_setup(worktree, command)
        except WorktreeError as exc:
            raise SetupFailed(f"no setup for {member.name} in {worktree.cwd}: {exc}") from exc
        await self.manager.db.kv_set(key, {"ok": result.ok, "exit_code": result.exit_code,
                                           "command": command, "tail": result.tail, "at": _now()})
        if result.ok:
            return
        reason = result.reason(command)
        logger.warning("setup of %s's worktree %s failed: %s", member.name, worktree.path, reason)
        # The same words a launch the queue could not make leaves on the card, so the operator finds
        # this one where they look for the others; the event wakes the orchestrator.
        await self.note_on_card(task.id, f"{member.name} could not start: {reason}")
        current = await self.task(task.id)
        if current is not None and current.assignee_staff_id == member.id:
            await self._set_assignee(current, None, actor="system", error=f"{member.name} could not start: {reason}")
        raise SetupFailed(reason)

    # -- assigning and starting ---------------------------------------------------------------------

    @staticmethod
    def _resume_key(staff_id: str, task_id: str) -> str:
        return f"staff_resume:{staff_id}:{task_id}"

    @staticmethod
    def _effort_key(staff_id: str, task_id: str) -> str:
        return f"staff_effort:{staff_id}:{task_id}"

    async def resume_sessions(self, member: Staff, *, task_id: str | None = None, before: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Conversations in the member's launch folder, including those of dismissed namesakes."""
        if member.harness == "daedalus":
            return []
        task = await self.task(task_id) if task_id else None
        if task_id and (task is None or task.project_id != member.project_id):
            raise StaffError("the task is not in this project")
        project = await self.project(member.project_id)
        folder = self.folder_for(project, member, task)
        wanted_branch = branch_name(member.name, task.id, task.title) if task and member.isolation == "worktree" else None
        wanted_worktree = str(folder.path / WORKTREES_DIR / staff_slug(member.name)) if member.isolation == "worktree" else None
        rows = await self.manager.staff.resumable_sessions(
            project.id, member.harness, folder.id, str(folder.path),
            worktree_path=wanted_worktree, before=before, limit=limit,
        )
        busy_rows = await self.manager.db.fetchall(
            "SELECT DISTINCT s.cli_session_id FROM staff_sessions s JOIN staff m ON m.id = s.staff_id "
            "WHERE m.project_id = ? AND m.harness = ? AND s.ended_at IS NULL AND s.cli_session_id IS NOT NULL",
            (project.id, member.harness),
        )
        busy_refs = {row["cli_session_id"] for row in busy_rows}
        result = []
        for session, owner, title in rows:
            same_folder = member.isolation == "worktree" or not session.launch_cwd or session.launch_cwd == str(folder.path)
            busy = session.cli_session_id in busy_refs
            eligible = not busy and same_folder and (session.branch == wanted_branch and session.worktree_path == wanted_worktree if wanted_branch else not session.worktree_path and member.isolation != "worktree")
            view = session.view()
            view.update({"owner_name": owner, "task_title": title, "can_resume": eligible,
                         "resume_reason": "" if eligible else ("live" if busy else "folder")})
            result.append(view)
        return result

    async def _resume_source(self, member: Staff, task: BoardTask, folder: ProjectFolder, source_id: str) -> LiveSession:
        source = await self.manager.staff.session(source_id)
        owner = await self.manager.staff.get(source.staff_id) if source else None
        if source is None or owner is None or owner.project_id != member.project_id or owner.harness != member.harness:
            raise StaffError("the session is not from this project's selected harness")
        if source.kind != "cli" or not source.cli_session_id or source.live:
            raise StaffError("the selected CLI session has no ended conversation to resume")
        busy = await self.manager.db.fetchone(
            "SELECT s.id FROM staff_sessions s JOIN staff m ON m.id = s.staff_id "
            "WHERE m.project_id = ? AND m.harness = ? AND s.cli_session_id = ? AND s.ended_at IS NULL LIMIT 1",
            (member.project_id, member.harness, source.cli_session_id),
        )
        if busy:
            raise StaffBusy("the selected CLI conversation is already open in another session")
        if not source.launch_cwd and source.folder_id != folder.id:
            raise StaffError("the selected CLI session belongs to another launch folder")
        if source.launch_cwd and member.isolation != "worktree" and source.launch_cwd != str(folder.path):
            raise StaffError("the selected CLI session was launched from another folder path")
        if member.isolation == "worktree":
            expected = str(folder.path / WORKTREES_DIR / staff_slug(member.name))
            if source.worktree_path != expected or source.branch != branch_name(member.name, task.id, task.title):
                raise StaffError("the selected CLI session belongs to another worktree or branch")
        elif source.worktree_path:
            raise StaffError("the selected CLI session belongs to a worktree, not this launch folder")
        return LiveSession(owner, source)

    async def assign(self, member: Staff, task: str | dict[str, Any], *, principal: Principal,
                     client_operation_id: str, expected_entity_revision: int,
                     resume_from: str | None = None, effort: str | None = None) -> dict[str, Any]:
        """Commit an authenticated launch; capacity waiting survives without inferred operator rights."""
        from daedalus.extensions.task_launch import queue_launch  # Lazy: the durable handler also calls Team.

        if effort is not None and (member.harness != "daedalus" or effort not in DAEDALUS_EFFORTS[1:]):
            raise StaffError("an assignment's effort is a Daedalus effort: off, low, medium, high or xhigh")
        task_id = str(task["id"]) if isinstance(task, dict) else task
        return await queue_launch(self.app, task_id, principal, staff_id=member.id,
                                  client_operation_id=client_operation_id,
                                  expected_entity_revision=expected_entity_revision, resume_from=resume_from, effort=effort)

    async def _holder(self, task: BoardTask) -> tuple[str, str]:
        """Who holds a card in doing, and, when nobody works it any more, why not (else ``""``).

        A card is held only by a member still on the team whose live session is on it. Silence is
        not gone: a command-line member that says nothing may be thinking, and its card stays its
        own until its session ends or it is released.
        """
        member = await self.manager.staff.get(task.assignee_staff_id or "")
        if member is None:
            return "someone no longer on the team", "they are no longer on the team"
        if not member.active:
            return member.name, f"{member.name} was dismissed"
        live = await self.manager.staff.live(member.id)
        if live is not None and live.task_id == task.id:
            return member.name, ""
        if live is not None:
            return member.name, f"{member.name}'s session moved on to task {live.task_id or 'none'}"
        last = next((s for s in await self.manager.staff.sessions(member.id, limit=20) if s.task_id == task.id), None)
        if last is None:
            return member.name, f"{member.name} never started on it"
        return member.name, f"{member.name}'s session ended ({last.end_reason or last.status}) before the work was handed in"

    async def note_on_card(self, task_id: str, text: str) -> None:
        """A line of the host's in a card's notes, dated as the board dates its own."""
        line = f"[{_now()[:16].replace('T', ' ')}] {' '.join(text.split())}"
        await self.manager.db.execute(
            "UPDATE board_tasks SET notes = substr(CASE WHEN notes = '' THEN ? ELSE notes || char(10) || ? END, ?), updated_at = ? WHERE id = ?",
            (line, line, -NOTES_MAX_CHARS, _now(), task_id),
        )

    async def free_card(self, member: Staff, task_id: str | None, why: str, *, by: str = "system",
                        principal: Principal | None = None) -> BoardTask | None:
        """Put a card its member no longer works back to todo, unassigned, saying why on it; ``None``
        when there was no such card. What a release does for a live session, for one already over."""
        task = await self.task(task_id) if task_id else None
        if task is None or task.status != "doing" or task.assignee_staff_id != member.id:
            return None
        await self.note_on_card(task.id, f"{member.name} no longer works this card: {why}; it is back in todo, unassigned")
        return await self._move_task(task, "todo", actor=by,
                                     actor_id=principal.actor_id if principal else None, assignee=None)

    def execution_lock(self, staff_id: str) -> asyncio.Lock:
        """A stop and retasking share ownership: neither may overtake the other's external call."""
        return self._execution_locks.setdefault(staff_id, asyncio.Lock())

    async def start(self, member: Staff, task: BoardTask, *, principal: Principal,
                    check_authority: Callable[[], Awaitable[None]], by: str = "operator",
                    resume_from: str | None = None, capacity_slot_id: str | None = None,
                    resources: dict[str, Any] | None = None, source_head: str | None = None) -> LiveSession:
        async with self.execution_lock(member.id):
            # Strict CLI containment is the only runtime that can attest that escaped children
            # stopped. Claim before file handoff; an ordinary CLI or native worker makes no such claim.
            leases = WriterLeases(self.app.executions)
            if member.isolation != "readonly" and resources is None:
                # Preparation precedes its attempt row. Keep a contained claim from entering
                # between this check and the first durable attempt observation.
                async with leases.uncontained_start():
                    return await self._start(member, task, principal=principal, check_authority=check_authority,
                                             by=by, resume_from=resume_from, capacity_slot_id=capacity_slot_id,
                                             resources=resources, source_head=source_head, writer_lease=None)
            lease = await leases.acquire(member.project_id) if member.isolation != "readonly" else None
            try:
                return await self._start(member, task, principal=principal, check_authority=check_authority,
                                         by=by, resume_from=resume_from, capacity_slot_id=capacity_slot_id,
                                         resources=resources, source_head=source_head, writer_lease=lease)
            finally:
                if lease is not None:
                    await leases.release_if_safe(lease)

    async def _start(self, member: Staff, task: BoardTask, *, principal: Principal,
                     check_authority: Callable[[], Awaitable[None]], by: str,
                     resume_from: str | None, capacity_slot_id: str | None,
                     resources: dict[str, Any] | None = None, source_head: str | None = None,
                     writer_lease: WriterLease | None = None) -> LiveSession:
        """Start a session of ``member`` for ``task`` now; the launch queue calls this once it admits it."""
        if principal.origin_class != "operator" and not principal.grant_id:
            raise StaffError("a host-attested launch principal is required")
        if capacity_slot_id is not None and (member.harness != "daedalus" or member.isolation != "worktree"):
            raise StaffError("a funded comparison needs an isolated native worker")
        if capacity_slot_id is not None and resume_from is not None:
            raise StaffError("a comparison cannot reuse an earlier worker session")
        if resources is not None and member.harness == "daedalus":
            raise StaffError("in-process workers cannot use a strict attempt resource profile")
        profile = await self.app.db.fetchone("SELECT state FROM resource_profile_versions"
                                             " WHERE project_id = ? ORDER BY revision DESC LIMIT 1",
                                             (member.project_id,))
        if profile is not None and profile["state"] == "enabled" and resources is None:
            raise StaffError("a strict resource profile requires an exact queued CLI containment binding")
        await check_authority()
        permission_mode = self.launch_permission_mode(member)
        runtime = self.runtime(member)
        project = await self.project(member.project_id)
        folder = self.folder_for(project, member, task)
        source = await self._resume_source(member, task, folder, resume_from) if resume_from else None
        previous = await self.live_of(member)
        if resources is not None and resources.get("mode") == "writer":
            # Git preparation and initial file delivery have no contained process owner yet.
            # Refuse them before the claim crosses its uncertain preparation boundary.
            if member.isolation != "shared" or await self.manager.files.of_task(task.id):
                raise StaffError("writer containment needs a shared folder without initial file delivery")
        if writer_lease is not None:
            # The checks above are reads. From here, settling an old worker, preparing a worktree
            # or delivering files may leave a write in flight if this host loses the outcome.
            await WriterLeases(self.app.executions).begin_effects(writer_lease)
        uncontained_token = None
        if member.isolation != "readonly" and writer_lease is None:
            # If the host disappears during preparation, a later contained claim must see the
            # unresolved write even though no execution attempt was ever created.
            uncontained_token = await WriterLeases(self.app.executions).begin_uncontained_preparation(
                member.project_id, member.id)
            await WriterLeases(self.app.executions).enter_uncontained_preparation(
                uncontained_token, project_id=member.project_id, staff_id=member.id)
        if previous is not None:
            previous = await self.settle_stale(previous)
            # Every launch has its own session and attempt binding; reusing a CLI turn would let
            # the previous task's provider reference publish into the new task's contract.
        if previous is not None:
            if previous.session.status in ACTIVE_STATUSES:
                raise StaffBusy(f"{member.name} is still working")
            # A Daedalus member's next task is a new session, and the idle one it replaces ends
            # here; so is a command-line member's whose session cannot take it (see _continues).
            await self._end(previous, "next task", stop=True)
        predecessor = source.session if source else next((s for s in await self.manager.staff.sessions(member.id, limit=20) if s.task_id == task.id), None)
        worktree: Worktree | None = None
        if member.isolation == "worktree":
            # Never the folder itself instead. The start used to ask the stored `is_git`, which is
            # false for every host folder and for a subfolder of a repository, and went on without a
            # worktree when it was: a command-line member hired to work in a worktree of its own ran
            # in the operator's whole home directory, the live checkout included, as its sandbox's
            # writable root. Git is asked instead, and a folder with no worktree to give is refused.
            try:
                await self.worktrees.check(folder)
            except WorktreeRefused as exc:
                raise StaffError(no_worktree(member, task, exc)) from exc
            except WorktreeError as exc:
                raise StaffError(f"no worktree for {member.name} in {folder.path}: {exc}") from exc
            # What prepare refuses past that point (a worktree left with uncommitted changes) is the
            # worktree's state, not the folder's, and is said as such.
            try:
                worktree = await self.worktrees.prepare(folder, member.name, task.id, task.title, expected_head=source_head)
            except WorktreeError as exc:
                raise StaffError(f"no worktree for {member.name} in {folder.path}: {exc}") from exc
            if source and (str(worktree.path) != source.session.worktree_path or worktree.branch != source.session.branch):
                raise StaffError("the selected CLI session was launched in another worktree or branch")
            if source and source.session.launch_cwd and str(worktree.cwd) != source.session.launch_cwd:
                raise StaffError("the selected CLI session was launched from another worktree folder")
            await self._set_up(member, task, project, worktree)
        # Before the session exists: the brief names these copies, so a start whose files cannot be
        # put in place does not start at all.
        delivered = await self.hand_files(member, await self.manager.files.of_task(task.id), folder=folder, cwd=str(worktree.cwd if worktree else folder.path), task_id=task.id, by=by)
        try:
            context_packet = await assemble_task_context(self.app.db, task.id, role="worker",
                                                         role_hint=member.role)
        except ContextUnavailable as exc:
            raise StaffError(f"the task context cannot be pinned: {exc}") from exc
        context_text = render_task_context(context_packet)
        token = secrets.token_urlsafe(32)
        session = await self.manager.staff.claim_session(
            member.id,
            kind="daedalus" if member.harness == "daedalus" else "cli",
            task_id=task.id,
            folder_id=folder.id,
            launch_cwd=str(worktree.cwd if worktree else folder.path),
            worktree_path=str(worktree.path) if worktree else None,
            branch=worktree.branch if worktree else None,
            base_ref=worktree.base_ref if worktree else None,
            predecessor_id=predecessor.id if predecessor else None,
            team_token_hash=_hash(token),
        )
        try:
            await pin_staff_context(self.app.db, session.id, context_packet, role_hint=member.role)
        except ContextPinRefused as exc:
            await self.manager.staff.end_session(session.id, str(exc))
            raise StaffError(str(exc)) from exc
        context_text += (f"\nTo recover this exact packet after history is shortened, read "
                         f"http://127.0.0.1:{self.app.settings.api_port}/api/team/{session.id}/context "
                         "with your existing team token. Its source_current field says whether to refresh the task.")
        await self.publish("staff.status", {"status": "starting", "previous": None, "actor": by}, member=member)
        fresh, earlier = await self.brief_files(delivered)
        contract = await self.contract_block(member, task, session.id, delivered)
        first = self.first_message(member, task, folder, worktree, predecessor, by, fresh, earlier,
                                   rules=await self.rules_block(project.id), contract=contract)
        first += "\n\n" + context_text
        try:
            recorded = await self.manager.staff.add_message(member.id, first, origin=by, mode="after_turn", staff_session_id=session.id)
            first_id = recorded.id
        except StaffError:
            first_id = ""  # a brief longer than a message may be; it is still sent, just not receipted
        # On the board before the session starts: a quick worker's Report(done) must find the task in
        # doing, not be overtaken by this move.
        moved = task if capacity_slot_id is not None else await self._move_task(
            task, "doing", actor=by, assignee=member.id,
            branch=worktree.branch if worktree else None, folder_id=folder.id)
        request = StartRequest(
            staff=member,
            project=project,
            folder=folder,
            cwd=worktree.cwd if worktree else folder.path,
            worktree=worktree,
            task=moved,
            first_message=first,
            brief_text=(await self.brief(member, project, folder, worktree)) + "\n\n" + context_text,
            staff_session_id=session.id,
            env=folder.env,
            model=member.model,
            effort=(await self.manager.db.kv_get(self._effort_key(member.id, task.id))) or member.effort,
            agent=member.agent,
            permission_level=self.permission_level(project),
            permission_mode=permission_mode,
            team_url=f"http://127.0.0.1:{self.app.settings.api_port}/api/team/{session.id}",
            team_token=token,
            predecessor=LiveSession(member, predecessor) if predecessor else None,
            origin="orchestrator" if by == "orchestrator" else "operator",
            first_message_id=first_id,
            allow_rules=tuple(r["rule"] for r in await self.manager.staff.allow_rules(member.id)),
            resources=resources,
        )
        identity = None
        entered_provider = False
        try:
            from daedalus.extensions.launch_controls import (  # Lazy: attempt creation follows the claimed session.
                observe_bind,
                prepare_attempt,
            )

            identity = await prepare_attempt(self.app, principal, member, task, session, fence_token=token,
                                             capacity_slot_id=capacity_slot_id,
                                             uncontained_token=uncontained_token)
            if writer_lease is not None:
                await WriterLeases(self.app.executions).bind(writer_lease, identity.id)
            if resources is not None:
                request = replace(request, resources={**resources, "attempt_id": identity.id,
                                                      "host_generation": str(identity.host_generation)})
            await check_authority()
            current_context = await assemble_task_context(self.app.db, task.id, role="worker",
                                                          role_hint=member.role)
            if current_context["packet_hash"] != context_packet["packet_hash"]:
                raise StaffError("the task context changed before the worker started; retry the launch")
            from daedalus.stores.phase_clocks import PhaseClocks  # Lazy: attempt ownership is established first.

            async with self.app.db.transaction() as conn:
                await PhaseClocks(self.app.executions).advance(conn, identity, from_phase="prepare", to_phase="spawn")
            await enter_runtime(self.app, identity, capacity_slot_id=capacity_slot_id)
            entered_provider = True
            started = await runtime.resume(request, source) if source else await runtime.start(request)
            await self.manager.staff.started(session.id, session_id=started.session_id, terminal_id=started.terminal_id, cli_session_id=started.cli_session_id, transcript_ref=started.transcript_ref)
            await observe_bind(self.app, identity, session, started)
        except (Exception, asyncio.CancelledError) as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            if identity is not None and not entered_provider:
                # Cancellation can arrive after the boundary committed but before await returned.
                # Only the durable host observation can distinguish that from a refused launch.
                entered_provider = not await observe_no_entry(self.app, identity, staff_session_id=session.id,
                                                              reason=f"could not start: {exc}")
            if identity is not None:
                from daedalus.stores.attempt_faults import record_fault  # Lazy: launch ownership is established first.

                async with self.app.db.transaction() as conn:
                    await record_fault(conn, identity.id, "cancelled" if cancelled else "launch_error",
                                       cancelled_by="system" if cancelled else None)
                    if cancelled and not entered_provider:
                        # A pre-entry cancellation is a normal terminal outcome, even when the
                        # no-entry observation initially closed the attempt as a failed launch.
                        await conn.execute("UPDATE execution_attempts SET state = 'cancelled'"
                                           " WHERE id = ? AND state = 'failed'", (identity.id,))
            if entered_provider:
                # A provider can create the process before its response disappears. Keep its slot,
                # task and session owned until an observation establishes what actually happened.
                async with self.app.db.transaction() as conn:
                    cursor = await conn.execute("UPDATE execution_attempts SET state = 'recovering',updated_at = ?"
                                                " WHERE id = ? AND state IN ('queued','starting','running','waiting')",
                                                (_now(), identity.id))
                    changed = cursor.rowcount
                    await cursor.close()
                    if changed:
                        await conn.execute("UPDATE staff_sessions SET status = 'no_signal',status_at = ?"
                                           " WHERE id = ? AND ended_at IS NULL", (_now(), session.id))
                if changed:
                    await self.publish("staff.status", {"status": "no_signal", "previous": "starting",
                                       "detail": "the launch outcome is unknown; reconcile the original execution"}, member=member)
            else:
                await self.manager.staff.end_session(session.id, f"could not start: {exc}"[:500])
                await self.publish("staff.status", {"status": "exited", "previous": "starting", "detail": f"could not start: {exc}"[:500]}, member=member)
                if capacity_slot_id is None:
                    await self._move_task(moved, "todo", actor="system", assignee=None)
            raise
        refreshed = await self.manager.staff.session(session.id)
        if first_id:
            await self.ingress.message_state(first_id, "submitted")
        return LiveSession(member, refreshed or session)

    @staticmethod
    def launch_permission_mode(member: Staff) -> str:
        """The CLI mode a member starts in. A read-only member starts in its CLI's own no-write mode.

        Read-only used to live only in the brief, while Claude ran with acceptEdits and Codex with
        workspace-write, so a "read-only" reviewer could still edit the folder it was checking. A
        CLI with no such mode is refused rather than trusted to keep a promise in its prompt."""
        if member.isolation != "readonly" or member.harness == "daedalus":
            return member.permission_mode
        modes = RESTRICTIVE_MODES.get(member.harness)
        if not modes:
            raise StaffError(f"{member.harness} has no mode that keeps it from writing; give {member.name} "
                             "a shared folder or a worktree instead of read-only")
        return member.permission_mode if member.permission_mode in modes else min(modes)

    def permission_level(self, project: Project) -> str:
        """The permission level a command-line member starts with. ``full`` autonomy starts it exactly
        like ``normal``: the operator decided that full means the orchestrator answers the requests
        itself, not that the agent stops asking — a bypassed permission is one nobody sees."""
        autonomy = project.settings.orchestrator.autonomy if project.settings.orchestrator.enabled else "ask"
        return {"ask": "ask", "normal": "edits", "full": "edits"}.get(autonomy, "ask")

    async def brief(self, member: Staff, project: Project, folder: ProjectFolder, worktree: Worktree | None) -> str:
        if worktree is not None:
            where = prompts.STAFF_WORKTREE_CLAUSE.format(path=worktree.cwd, branch=worktree.branch, base=worktree.base_ref)
        elif member.isolation == "readonly":
            where = prompts.STAFF_READONLY_CLAUSE.format(path=folder.path)
        else:
            where = prompts.STAFF_SHARED_CLAUSE.format(path=folder.path)
        persona = ""
        if member.harness == "daedalus" and member.agent:
            text = self._persona(member.agent)
            if text:
                persona = f"\n\n[persona: {member.agent}]\n{text}"
        return prompts.STAFF_BRIEF.format(
            name=member.name,
            project=project.name,
            role=f" Your responsibility: {member.role}." if member.role else "",
            where=where,
            done_rule=" (a worktree with uncommitted changes is refused: commit first)" if worktree is not None else "",
            notes=f"\nYour notes from earlier sessions:\n{member.notes}\n" if member.notes else "",
            instructions=f"\nStanding instructions:\n{member.instructions}\n" if member.instructions else "",
            persona=persona,
        )

    def _persona(self, name: str) -> str | None:
        subagents = self.app.extensions.get("subagents")
        return subagents.persona(name) if subagents is not None else None

    async def rules_block(self, project_id: str) -> str:
        """The operator's rules in force and the brief's constraints, for a member's brief.

        The rules go whole: they are bounded where they are written. The constraints section is free
        text of any length and gets what is left of ``STAFF_RULES_CHARS``, and never less than
        ``STAFF_CONSTRAINTS_MIN``; the orchestrator holds the rest.
        """
        rules = [" ".join(r.text.split()) for r in await self.manager.projects.rules(project_id)]
        lines = [f"- {rule}" for rule in rules]
        constraints = " ".join((await self.manager.projects.brief(project_id))["constraints"].body.split())
        if constraints:
            room = max(STAFF_CONSTRAINTS_MIN, STAFF_RULES_CHARS - sum(len(line) + 1 for line in lines))
            if len(constraints) > room:
                constraints = constraints[:room].rstrip() + " … (the orchestrator has the rest)"
            lines.append(f"- The project's constraints: {constraints}")
        return prompts.STAFF_RULES.format(lines="\n".join(lines)) if lines else ""

    async def announce_rule(self, project_id: str, text: str, *, by: str) -> list[str]:
        """Tell each member at work on a task now of a rule made or lifted, when their turn ends; the
        names of those told. An idle member is not woken for it: its next brief carries the rules."""
        told: list[str] = []
        for staff_id, session in (await self.manager.staff.live_sessions(project_id)).items():
            task = await self.task(session.task_id) if session.task_id else None
            member = await self.manager.staff.get(staff_id)
            if task is None or task.status != "doing" or member is None or not member.active:
                continue
            try:
                await self.tell(member, text, when="after_turn", by=by)
            except Exception:  # noqa: BLE001 — one member who cannot be told must not keep the rule from the rest
                logger.warning("could not tell %s of a changed rule", member.name, exc_info=True)
                continue
            told.append(member.name)
        return told

    async def contract_block(self, member: Staff, task: BoardTask, staff_session_id: str, delivered: list[Delivered]) -> str:
        """The card's requirements and checks for a member's brief, each requirement recorded as given to
        this session. An input names the copy the member can open, which is what the host watches for."""
        requirements = await self.contracts.requirements(task.id)
        checks = await self.contracts.checks(task.id)
        paths = {d.file.id: d.path for d in delivered}
        cli = member.harness != "daedalus"
        lines: list[str] = []
        for requirement in requirements:
            path = paths.get(requirement.file_id or "", "")
            await self.contracts.delivered(requirement, staff_session_id=staff_session_id, staff_id=member.id, via="brief", path=path)
            text = requirement.text
            if requirement.kind == "input":
                confirm = prompts.STAFF_INPUT_CONFIRM.format(label=requirement.label) if cli else ""
                text += " — " + prompts.STAFF_INPUT_LINE.format(path=path or "the file of that name among those handed to you", confirm=confirm)
            lines.append(f"- {requirement.label} ({requirement.kind}, from {requirement.origin()}): {text}")
        block = prompts.STAFF_CONTRACT.format(lines="\n".join(lines)) if lines else ""
        if checks:
            block += prompts.STAFF_CHECKS.format(lines="\n".join(f"- C{i} {item['text']}" for i, item in enumerate(checks, start=1)))
        return block

    def first_message(self, member: Staff, task: BoardTask, folder: ProjectFolder, worktree: Worktree | None, predecessor: StaffSession | None, by: str, delivered: list[Delivered] | None = None, earlier: list[Delivered] | None = None, *, rules: str = "", contract: str = "") -> str:
        before = ""
        if predecessor is not None:
            why = predecessor.end_reason or predecessor.status
            before = (
                f"\n\nThis task was worked on before, in session {predecessor.id}, which ended: {why}. "
                "Look at what is already there before you start over."
            )
        if task.sent_back:
            before += f"\n\nThe operator sent this work back from review: {task.sent_back}\nChange it on the same branch, commit, and report done again."
        if task.returned:
            before += prompts.STAFF_RETURNED.format(text=task.returned)
        return prompts.STAFF_TASK.format(
            task_id=task.id,
            by="orchestrator" if by == "orchestrator" else "operator",
            title=task.title,
            objective=task.objective,
            deliverable=task.deliverable,
            boundaries=task.boundaries,
            done_when=task.done_when,
            rules=rules,
            contract=contract,
            folder=worktree.cwd if worktree is not None else folder.path,
            branch=f"\nBranch: {worktree.branch} (from {worktree.base_ref})" if worktree is not None else "",
            predecessor=before,
            files=(prompts.STAFF_FILES.format(lines="\n".join(d.line() for d in delivered)) if delivered else "") + (prompts.STAFF_EARLIER_FILES.format(n=len(earlier), where=posixpath.dirname(earlier[0].path)) if earlier else ""),
        )

    # -- control ---------------------------------------------------------------------------------------

    async def tell(self, member: Staff, text: str, *, when: str = "now", by: str = "operator",
                   files: list[StoredFile] | None = None, message_id: str | None = None) -> dict[str, Any]:
        """Say something to a member's live session; returns the message and its receipt. ``when`` is
        ``now`` (into the running turn), ``after_turn`` or ``interrupt``; ``now`` is the default
        because a message to someone at work is almost always about that work, and one that waited
        for the turn's end used to arrive after the work it was meant to change. ``files`` are put
        where the member can open them first, and the message ends with their paths; they also stay
        with the session's task, so a restart of it hands them over again."""
        live = await self.live_of(member)
        if live is None:
            raise StaffError(f"{member.name} has no live session; assign a task to start one")
        delivered: list[Delivered] = []
        if files:
            folder, cwd = await self.cwd_of(live)
            strict = await self.manager.db.fetchone(
                "SELECT b.attempt_id FROM attempt_resource_bindings b JOIN execution_attempts a"
                " ON a.id = b.attempt_id WHERE a.staff_session_id = ?"
                " UNION ALL SELECT b.attempt_id FROM writer_attempt_bindings b JOIN execution_attempts a"
                " ON a.id = b.attempt_id WHERE a.staff_session_id = ?", (live.id, live.id)
            )
            leases = WriterLeases(self.app.executions) if strict is not None else None
            handoff = await leases.begin_handoff(strict["attempt_id"]) if leases is not None else None
            delivered = await self.hand_files(member, files, folder=folder, cwd=cwd, task_id=live.session.task_id, by=by)
            if leases is not None and handoff is not None:
                await leases.finish_handoff(strict["attempt_id"], handoff)
            if live.session.task_id:
                await self.manager.files.attach_to_task(live.session.task_id, files, actor=by)
            text = text.rstrip() + "\n\n" + prompts.STAFF_FILES.format(lines="\n".join(d.line() for d in delivered)).strip()
        if live.session.pause_requested:
            await self.manager.staff.request_pause(live.id, False)
        message = await self.manager.staff.add_message(member.id, text, origin=by, mode=when,
                                                       staff_session_id=live.id, message_id=message_id)
        outgoing = OutgoingMessage(message.id, message.text, when, "orchestrator" if by == "orchestrator" else "operator")  # type: ignore[arg-type]
        try:
            receipt = await self.runtime(member).send(live, outgoing)
        except Exception as exc:  # noqa: BLE001 — a failed delivery is recorded on the message, not raised past it
            receipt = Receipt("failed", str(exc)[:500])
        await self.ingress.message_state(message.id, receipt.state, receipt.error)
        return {"message_id": message.id, "state": receipt.state, "error": receipt.error, "degraded_to": receipt.degraded_to, "files": [d.path for d in delivered]}

    async def interrupt(self, member: Staff) -> None:
        live = await self.live_of(member)
        if live is None:
            raise StaffError(f"{member.name} has no live session")
        await self.runtime(member).interrupt(live)

    async def pause(self, member: Staff) -> dict[str, Any]:
        """Let the turn finish, commit what is uncommitted, and start nothing new until told or assigned."""
        live = await self.live_of(member)
        if live is None:
            raise StaffError(f"{member.name} has no live session")
        await self.manager.staff.request_pause(live.id)
        if live.session.status not in ACTIVE_STATUSES:
            return {"paused": True, "commit": await self._settle_pause(live)}
        return {"paused": False, "note": f"{member.name} pauses when the current turn ends"}

    async def _settle_pause(self, live: LiveSession) -> str | None:
        commit = None
        worktree = await self.worktree_of(live.session)
        if worktree is not None:
            title = live.session.task_id or "task"
            try:
                commit = await self.worktrees.commit_wip(worktree, f"wip: {title} (paused)")
            except WorktreeError as exc:
                logger.warning("could not commit the paused work of %s: %s", live.staff.name, exc)
        await self.ingress.status(live, "idle", detail="paused" + (f"; work committed as {commit[:10]}" if commit else ""))
        return commit

    async def worktree_of(self, session: StaffSession) -> Worktree | None:
        """The worktree a session works in, rebuilt from its row: the folder it was made in is three
        levels up (``<folder>/.agents/worktrees/<name>``), and the folder's environment is the git's."""
        if not session.worktree_path or not session.branch:
            return None
        path = Path(session.worktree_path)
        env = self.manager.projects.local_env
        if session.folder_id:
            row = await self.manager.db.fetchone("SELECT env FROM project_folders WHERE id = ?", (session.folder_id,))
            env = str(row["env"]) if row is not None else env
        return Worktree(path=path, branch=session.branch, base_ref=session.base_ref or "HEAD", folder=path.parent.parent.parent, env=env)

    async def release(self, member: Staff, *, keep_worktree: bool = True, reason: str = "released",
                      by: str = "operator", principal: Principal | None = None,
                      check_authority: Callable[[], Awaitable[None]] | None = None) -> bool:
        """End the member's live session: its runtime stops it, the row ends, the task goes back to todo.

        The worktree stays unless asked otherwise, and even then an unmerged branch is kept: it is the
        only copy of the work. Returns whether there was a session to end.
        """
        live = await self.live_of(member)
        if live is None:
            return False
        if by == "orchestrator" and (principal is None or check_authority is None):
            raise StaffError("coordinator release requires a current approved grant")
        if check_authority is not None:
            await check_authority()
        await self._end(live, reason, stop=True, by=by, principal=principal)
        if live.session.task_id:
            task = await self.task(live.session.task_id)
            if task is not None and task.status == "doing":
                # Named by who released, so the orchestrator is not woken by its own release.
                await self._move_task(task, "todo", actor=by,
                                      actor_id=principal.actor_id if principal else None, assignee=None)
        if not keep_worktree:
            worktree = await self.worktree_of(live.session)
            if worktree is not None:
                try:
                    await self.worktrees.remove(worktree, delete_branch_if_merged=True)
                except WorktreeError as exc:
                    logger.warning("kept the worktree of %s: %s", member.name, exc)
        self.queue.pump_soon(member.project_id)
        return True

    async def _end(self, live: LiveSession, reason: str, *, stop: bool, by: str | None = None,
                   principal: Principal | None = None) -> None:
        if stop:
            try:
                await self.runtime(live.staff).stop(live)
            except Exception:  # noqa: BLE001 — the row ends whatever the process did; reconcile finds a survivor
                logger.exception("stopping %s's session failed", live.staff.name)
        await self._stop_services(live, reason)
        ended = await self.manager.staff.end_session(live.id, reason)
        if ended is not None:
            payload: dict[str, Any] = {"status": "exited", "previous": live.session.status, "detail": reason[:500]}
            if by:
                payload["actor"] = by
            if principal:
                payload["actor_id"] = principal.actor_id
            await self.publish("staff.status", payload, member=live.staff, session_id=live.session_id)
        for ask in await self._open_asks(live.id):
            if await self.manager.asks.resolve(ask.id, "system", {"closed": f"the session ended: {reason}"}):
                await self._withdrawn(ask)

    async def _stop_services(self, live: LiveSession, reason: str) -> None:
        """Stop the servers the worker's session started with ServiceStart. They belong to the session,
        and nothing else ever stopped them: a released worker's dev server kept its port and its memory
        until the operator found it in the services list. Only a Daedalus session has the tool."""
        services: Any = self.app.extensions.get("services")
        if services is None or not live.session.session_id:
            return
        try:
            stopped = await services.stop_session(live.session.session_id, f"its worker ended: {reason}")
        except Exception:  # noqa: BLE001 — the session ends whatever its servers did
            logger.exception("stopping the services of %s's session failed", live.staff.name)
            return
        if stopped:
            logger.info("stopped %d service(s) of %s's session: %s", stopped, live.staff.name, reason)

    async def _open_asks(self, staff_session_id: str) -> list[Ask]:
        rows = await self.manager.db.fetchall("SELECT id FROM asks WHERE staff_session_id = ? AND resolved_at IS NULL ORDER BY created_at", (staff_session_id,))
        out = []
        for row in rows:
            ask = await self.manager.asks.get(row["id"])
            if ask is not None:
                out.append(ask)
        return out

    async def seen(self, live: LiveSession) -> None:
        """Someone read the finished turn: it is no longer news."""
        if live.session.status == "turn_done_unseen":
            await self.ingress.status(live, "idle")

    # -- requests ----------------------------------------------------------------------------------------

    def route(self, project: Project, kind: str) -> str:
        """Who a staff request goes to first, by the project's autonomy (see ``answer``)."""
        orchestrator = project.settings.orchestrator
        if not orchestrator.enabled:
            return "operator"
        if kind == "permission" and orchestrator.autonomy == "ask":
            return "operator"
        return "orchestrator"

    async def answer(
        self,
        ref: str,
        *,
        allow: bool | None = None,
        always: bool = False,
        server: bool = False,
        text: str | None = None,
        selected: list[str] | None = None,
        note: str = "",
        by: str = "operator",
        basis: str = "",
        via: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Answer a request, once. The first answer to update the row delivers; a later one is refused.

        ``note`` is the operator's comment beside a chosen option ("Postgres — but keep SQLite for
        tests"), or their reason beside a refusal. It is kept apart from ``text`` on the resolution, so
        a window can show what was pressed and what was said; the asker is handed it as the words
        that came with the answer.

        The orchestrator answers within the project's autonomy. At ``ask`` its answer to a question
        becomes a suggestion the operator confirms, and a permission is the operator's. At ``normal``
        it may grant only by quoting, as ``basis``, a line of the brief's allowances — the section only
        the operator writes — so a grant is never consent read into a chat message. At ``full`` any
        stated reason will do. Denying is always allowed, and every grant is written to the journal.

        ``always`` with ``server`` grants every tool of the asked tool's MCP server. Where the member's
        CLI keeps standing rules, an "always" for an MCP tool is also kept as the member's rule and
        written into each later launch; it stays until the operator revokes it.
        """
        ask = await self.manager.asks.get(ref)
        if ask is None:
            raise KeyError(ref)
        if not ask.open:
            raise AlreadyAnswered(f"request {ask.short_id} was already answered by the {ask.resolved_by}")
        if ask.origin == "dispatcher":
            # The main orchestrator's own confirmation belongs to no project yet; it settles it itself.
            if self.dispatcher_requests is None:
                raise StaffError("the main orchestrator is not running to take the answer")
            if by != "operator":
                raise StaffError(f"request {ask.short_id} is the operator's to answer")
            settled: dict[str, Any] = await self.dispatcher_requests.answer_own(ask, allow=allow, text=text, selected=selected, by=by, via=via or "app")
            return settled
        assert ask.project_id is not None
        project = await self.project(ask.project_id)
        if by == "orchestrator":
            outcome = await self._orchestrator_may(ask, project, allow=allow, text=text, selected=selected, basis=basis)
            if outcome is not None:
                return outcome
        resolution: dict[str, Any] = {"allow": allow, "text": text or "", "selected": list(selected or []), "via": via or ("orchestrator" if by == "orchestrator" else "app")}
        note = (note or "").strip()
        if note:
            resolution["note"] = note
        if basis:
            resolution["basis"] = basis
        # "Always" is the operator's alone: a standing grant is a change to what the member may do,
        # which the brief's allowances give the orchestrator no say over.
        always = bool(always and allow and ask.kind == "permission" and by == "operator")
        rule: str | None = None
        if always:
            resolution["always"] = True
            rule = await self._standing_rule(ask, server=server)
            if rule is not None:
                resolution["rule"] = rule
        if extra:
            resolution.update({k: v for k, v in extra.items() if k not in resolution})
        if not await self.manager.asks.resolve(ask.id, by, resolution):
            current = await self.manager.asks.get(ask.id)
            raise AlreadyAnswered(f"request {ask.short_id} was already answered by the {current.resolved_by if current else 'someone else'}")
        answered = await self.manager.asks.get(ask.id)
        if answered is not None and answered.resolution.get("applies") is False:
            # The operator's words are recorded, but an old task contract must not receive a
            # permission or decision as if it answered the current version of the work.
            await self._announce_resolved(ask, allow=allow, by=by, via=resolution["via"])
            return {"state": "answered", "applied": False, "delivered": False,
                    "error": "the task contract changed after this request was asked", "ask": answered.view()}
        if rule is not None and ask.staff_id:
            # Kept before the delivery: the operator's "always" stands even when the session that
            # asked has ended by now, and its next launch is where the rule is read.
            await self.manager.staff.grant_rule(ask.staff_id, rule, by="operator")
        # The typed answer and the note beside it both go: ``text or note`` once dropped the note
        # whenever the operator had typed an answer as well.
        said = " — ".join(part for part in dict.fromkeys(((text or "").strip(), note)) if part)
        delivered, error = await self._deliver(ask, allow=allow, always=always, server=bool(server and rule), text=said or None, selected=selected, by=by)
        if ask.kind == "permission" and allow:
            who = "The orchestrator" if by == "orchestrator" else "The operator"
            member = await self.manager.staff.get(ask.staff_id) if ask.staff_id else None
            await self.manager.projects.record(
                project.id, "system", "grant",
                f"{who} granted {member.name if member else 'a staff member'}: {ask.text[:300]}" + (f" (always: {rule})" if rule else "") + (f" (basis: {basis})" if basis else ""),
                {"ask_id": ask.id, "staff_id": ask.staff_id or ""},
            )
        await self._announce_resolved(ask, allow=allow, by=by, via=resolution["via"])
        return {"state": "answered", "applied": True, "delivered": delivered,
                "error": error, "ask": answered.view() if answered else ask.view()}

    async def _standing_rule(self, ask: Ask, *, server: bool) -> str | None:
        """The rule an operator's "always" leaves for the member, if its CLI keeps rules and the tool
        is an MCP one. Asked for the whole server where there is none, it is refused rather than
        quietly narrowed to the one tool: the operator meant more than that."""
        member = await self.manager.staff.get(ask.staff_id) if ask.staff_id else None
        caps = CAPABILITIES.get(member.harness) if member is not None else None
        tool = str(ask.detail.get("tool") or "")
        keeps = caps is not None and caps.standing_rules
        if server and not (keeps and mcp_server(tool)):
            raise StaffError(f"request {ask.short_id} is not for a tool of an MCP server whose tools can be allowed together")
        return standing_rule(tool, "server" if server else "tool") if keeps else None

    async def _orchestrator_may(self, ask: Ask, project: Project, *, allow: bool | None, text: str | None, selected: list[str] | None, basis: str) -> dict[str, Any] | None:
        """The orchestrator's limits; a dict when the answer ends here (a suggestion), ``None`` to go on."""
        orchestrator = project.settings.orchestrator
        if not orchestrator.enabled:
            raise StaffError(f"{project.name} has no orchestrator")
        if ask.routed_to != "orchestrator":
            raise StaffError(f"request {ask.short_id} is the operator's to answer")
        if ask.kind == "question" and orchestrator.autonomy == "ask":
            suggestion = (text or "").strip() or ", ".join(selected or [])
            await self.manager.asks.route(ask.id, "operator", suggestion)
            await self._release_hold(ask)
            routed = await self.manager.asks.get(ask.id)
            return {"state": "suggested", "delivered": False, "error": "", "ask": routed.view() if routed else ask.view()}
        if ask.kind == "permission" and allow:
            if orchestrator.autonomy == "ask":
                raise StaffError(f"in {project.name} the operator decides permissions")
            if orchestrator.autonomy == "normal":
                quoted = " ".join(basis.split())
                if await self._granted_on_the_card(ask, quoted):
                    return None
                allowances = (await self.manager.projects.brief(project.id))["allowed_without_operator"].body
                lines = [" ".join(line.split()) for line in allowances.splitlines()]
                if len(quoted) < BASIS_MIN or not any(quoted in line for line in lines if line):
                    raise StaffError(
                        "a grant needs, as its basis, a line quoted verbatim from the brief's 'allowed without the operator' "
                        f"(at least {BASIS_MIN} characters), or the R… of the operator's own scope on the member's card; "
                        "otherwise escalate the request to the operator"
                    )
            elif not (basis.strip() or (text or "").strip()):
                raise StaffError("a grant states its reason")
        return None

    async def _granted_on_the_card(self, ask: Ask, basis: str) -> bool:
        """Whether the basis names what the operator allowed for the very work the member asks within:
        a scope requirement of theirs on its card. The operator allowed one test message for a mail
        check, and the orchestrator could only ask them to write it into the brief's allowances — the
        brief is for what holds everywhere, the card for this work."""
        if not ask.task_id or not re.fullmatch(r"R\d{1,3}", basis):
            return False
        requirement = await self.contracts.find(ask.task_id, basis)
        return requirement is not None and requirement.state == "active" and requirement.kind == "scope" and requirement.from_operator

    async def _deliver(self, ask: Ask, *, allow: bool | None, text: str | None, selected: list[str] | None, by: str, always: bool = False, server: bool = False) -> tuple[bool, str]:
        if ask.origin == "orchestrator":
            if self.own_requests is None:
                return False, "no orchestrator is installed to take the answer"
            delivered: tuple[bool, str] = await self.own_requests.deliver_own(ask, allow=allow, text=text, selected=selected)
            return delivered
        live = await self.live(ask.staff_session_id) if ask.staff_session_id else None
        if live is None:
            return False, "the session that asked has ended"
        decision = Decision(allow=allow, text=text, selected=list(selected or []), by="orchestrator" if by == "orchestrator" else "operator", always=always, server=server)
        try:
            await self.runtime(live.staff).answer(live, AskRef(ask.id, ask.kind, ask.request_ref), decision)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 — the answer is recorded; the failure to deliver it is reported beside it
            logger.warning("could not deliver the answer to %s: %s", ask.short_id, exc)
            return False, str(exc)[:500]
        await self._after_answer(live)
        return True, ""

    async def _after_answer(self, live: LiveSession) -> None:
        """The status once a request is answered: working again, unless another request of the
        session is still open — then it waits on that one, a permission before a question, since a
        permission holds the process itself.

        A runtime that already moved the session out of waiting decided better than "working": a
        command-line member whose turn ended on a question the hold gave up on is idle, and its
        answer goes to it as a message that waits for exactly that. Forcing "working" here left the
        message waiting for a turn that had already ended."""
        remaining = await self._open_asks(live.id)
        current = await self.live(live.id) or live
        if not remaining:
            if current.session.status in ("question", "permission"):
                await self.ingress.status(current, "working")
            return
        ask = next((a for a in remaining if a.kind == "permission"), remaining[0])
        if ask.kind == "permission":
            words = f"permission [{ask.short_id}]: {ask.detail.get('tool') or ask.text}"
        else:
            words = f"question [{ask.short_id}]: {ask.text}"
        await self.ingress.status(current, ask.kind, words)

    async def _announce_resolved(self, ask: Ask, *, allow: bool | None, by: str, via: str) -> None:
        """The pending events of a command-line member's request are this module's, so their answers are too,
        and so are those of the orchestrator's own requests. A Daedalus member's are the session's own,
        published when the session is answered or granted. The notification router closes the operator's
        copy from these events, and the orchestrator is woken by the answer to what it asked."""
        ref = str(ask.detail.get("event_ref") or "")
        if ref.startswith("orchestrator:"):
            await self.publish("ask.answered", {"request_id": ask.id, "request_ref": ref, "via": via, "by": by}, project_id=ask.project_id)
        elif ref.startswith("staff:"):
            member = await self.manager.staff.get(ask.staff_id) if ask.staff_id else None
            if ask.kind == "permission":
                # Answered on the agent's own screen, the decision is the CLI's to know, not ours.
                decision = "terminal" if allow is None and via == "terminal" else "allow" if allow else "deny"
                await self.publish("permission.resolved", {"request_id": ask.id, "request_ref": ref, "decision": decision, "via": via, "by": by}, member=member, project_id=ask.project_id)
            else:
                await self.publish("ask.answered", {"request_id": ask.id, "request_ref": ref, "via": via}, member=member, project_id=ask.project_id)

    async def withdraw(self, ask: Ask, *, why: str, by: str = "system") -> bool:
        """Close a request nobody will answer any more: first close wins like any answer, the operator's
        notification closes, and every window that shows it hears ``via: withdrawn`` and lets it go.
        ``by`` is who withdrew it — the system when what it served is over, the orchestrator when it
        decided the answer no longer matters. The row is resolved by the system either way: a
        withdrawal is no answer, and nothing that reads answers may take it for one."""
        if not await self.manager.asks.resolve(ask.id, "system", {"closed": why, "via": "withdrawn", "by": by}):
            return False
        await self._withdrawn(ask)
        ref = str(ask.detail.get("event_ref") or "")
        if ref:
            await self.publish("ask.answered", {"request_id": ask.id, "request_ref": ref, "via": "withdrawn", "by": by, "reason": why}, project_id=ask.project_id)
        return True

    async def _withdrawn(self, ask: Ask) -> None:
        """A request nobody will answer any more, because its session ended: the operator's copy closes."""
        ref = str(ask.detail.get("event_ref") or "")
        resolve = getattr(self.app.notifications, "resolve", None)
        if resolve is not None and ref:
            try:
                await resolve(ref, "withdrawn", via="system")
            except Exception:  # noqa: BLE001
                logger.warning("could not close the notification of %s", ask.short_id, exc_info=True)

    async def resolve_action(self, req: Any) -> ActionOutcome:
        """An answer to a command-line member's request taken from a notification (``staff:<session>:<ask>``)."""
        ask = await self.manager.asks.get(req.target)
        if ask is None:
            raise ActionConflict("withdrawn")
        allow: bool | None = None
        text: str | None = None
        selected: list[str] = []
        if ask.kind == "permission":
            if req.action not in ("allow", "deny"):
                raise ActionRefused(f"a permission is not answered with {req.action!r}")
            allow = req.action == "allow"
        elif req.action.startswith("answer:"):
            options = list(ask.detail.get("options") or [])
            try:
                selected = [str(options[int(req.action.split(":", 1)[1])])]
            except (ValueError, IndexError) as exc:
                raise ActionRefused(f"no option {req.action!r}") from exc
        elif req.action == "answer" and (req.value or "").strip():
            text = req.value.strip()
        else:
            raise ActionRefused(f"a question is not answered with {req.action!r}")
        try:
            await self.answer(ask.id, allow=allow, text=text, selected=selected, by="operator", via=req.via)
        except AlreadyAnswered as exc:
            raise ActionConflict("answered") from exc
        return ActionOutcome("allow" if allow else "deny" if ask.kind == "permission" else "answered")

    async def _release_hold(self, ask: Ask) -> None:
        """The operator hears of a request now: the router's hold ends early, or, without a router, an
        urgent notification says it waits for them."""
        ref = str(ask.detail.get("event_ref") or "")
        notifications = self.app.notifications
        release = getattr(notifications, "release", None)
        if release is not None and ref:
            try:
                await release(ref)
                return
            except Exception:  # noqa: BLE001
                logger.warning("could not release the hold on %s", ask.short_id, exc_info=True)
        if notifications is not None:
            member = await self.manager.staff.get(ask.staff_id) if ask.staff_id else None
            who = member.name if member else "A staff member"
            await notifications.post(Draft(
                "permission" if ask.kind == "permission" else "question",
                f"{who} is waiting for you [{ask.short_id}]",
                ask.text[:1000] + (f"\n\nThe orchestrator suggests: {ask.suggestion}" if ask.suggestion else ""),
                kind=f"staff_{ask.kind}",
                level="urgent",
                project_id=ask.project_id,
                staff_id=ask.staff_id,
                dedupe_key=f"staff-ask:{ask.id}",
                source="staff",
            ))

    async def escalate(self, ask: Ask, *, why: str = "", suggestion: str = "") -> bool:
        """Hand a request the orchestrator has not answered to the operator, with what it would have
        answered when it has a view. True when it moved."""
        if not ask.open or ask.routed_to == "operator":
            return False
        if not await self.manager.asks.route(ask.id, "operator", suggestion):
            return False
        await self.manager.projects.record(ask.project_id, "system", "escalation", f"Request {ask.short_id} went to the operator{': ' + why if why else ''}", {"ask_id": ask.id})
        routed = await self.manager.asks.get(ask.id)
        await self._release_hold(routed or ask)
        await self._announce_to_operator(routed or ask)
        return True

    async def _announce_to_operator(self, ask: Ask) -> None:
        """Say that a request now waits on the operator.

        Handing a request over only changed its row and posted the notification: nothing reached the
        app's event stream, so its Questions list and count learnt of it at the next poll or a reload,
        and the operator read "the request went to you" with nothing on screen to answer. ``ask.routed``
        is only that news; the notification and the orchestrator's wake-ups have their own paths.
        """
        member = await self.manager.staff.get(ask.staff_id) if ask.staff_id else None
        ref = str(ask.detail.get("event_ref") or ask.request_ref)
        await self.publish("ask.routed", {"request_id": ask.id, "request_ref": ref, "routed_to": ask.routed_to}, member=member, project_id=ask.project_id)

    async def claim_answer(self, session_id: str, tool_call_id: str, via: str) -> str | None:
        """An answer typed into a Daedalus member's session is the operator's answer to the request."""
        live = await self.live_for_session(session_id)
        if live is None:
            return None
        row = await self.manager.db.fetchone(
            "SELECT id FROM asks WHERE staff_session_id = ? AND request_ref = ? AND kind = 'question' ORDER BY created_at DESC LIMIT 1", (live.id, tool_call_id)
        )
        if row is None:
            return None
        ask = await self.manager.asks.get(row["id"])
        if ask is None:
            return None
        if ask.open and await self.manager.asks.resolve(ask.id, "operator", {"via": via, "in_session": True}):
            await self._after_answer(live)
            return None
        current = await self.manager.asks.get(ask.id)
        return f"this question was already answered by the {current.resolved_by if current else 'someone else'}"

    # -- following the sessions --------------------------------------------------------------------------

    async def on_turn_event(self, session_id: str, event: TurnEvent | Any) -> None:
        """Every event of a staff session is a sign of life; a few say more."""
        if not isinstance(event, TurnEvent):
            return
        state = self.manager.live_state(session_id)
        if state is None or not state.metadata.get("staff_session_id"):
            return
        live = await self.live(str(state.metadata["staff_session_id"]))
        if live is None:
            return
        if live.session.status == "no_signal":
            await self.ingress.status(live, "working", detail="signal again")
        else:
            await self.manager.staff.touch(live.id)
        if event.type is EventType.TOOL_CALL_PENDING and event.payload.get("kind") == "ask_user" and event.payload.get("tool_name") == "AskOrchestrator":
            questions = (event.payload.get("ask_user_payload") or {}).get("questions") or [{}]
            first = questions[0] if isinstance(questions[0], dict) else {}
            options = [str(o.get("label") or "") for o in first.get("options") or [] if isinstance(o, dict)]
            call_id = str(event.payload.get("tool_call_id") or "")
            await self.ingress.question(live, call_id, str(first.get("question") or ""), options, event_ref=f"ask:{session_id}:{call_id}")
        elif event.type is EventType.TOOL_USE_STOP and isinstance(event.payload.get("final_input"), dict):
            # An input the member was given counts as opened once a tool of its is pointed at it: the
            # host cannot know it was understood, only that it was not skipped.
            await self.contracts.opened(live.id, json.dumps(event.payload["final_input"], ensure_ascii=False))
        elif event.type is EventType.QUEUE_UPDATE and event.payload.get("placed"):
            # A queued message the model has now received: that is the acknowledgement.
            for message_id in event.payload["placed"]:
                if str(message_id).startswith("sm-"):
                    await self.ingress.message_state(str(message_id), "acknowledged")

    async def admit_run(self, staff_session_id: str, session_id: str, run_id: str) -> None:
        await admit_native_run(self.app, staff_session_id, session_id, run_id)

    async def on_run_started(self, session_id: str, run_id: str) -> None:
        live = await self.live_for_session(session_id)
        if live is not None and live.session.status != "working":
            await self.ingress.status(live, "working")

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        state = self.manager.live_state(session_id)
        staff_session_id = str(state.metadata.get("staff_session_id") or "") if state is not None else ""
        if staff_session_id and status in ("completed", "failed", "cancelled"):
            await observe_exit(self.app, staff_session_id=staff_session_id, runtime_ref=run_id,
                               observed_status="error" if status == "failed" else status, bus=self.manager.bus)
        live = await self.live_for_session(session_id)
        if live is None or status == "awaiting":
            return
        if live.session.pause_requested and status in ("completed", "cancelled"):
            await self._settle_pause(live)
        elif await self._open_asks(live.id):
            pass  # still waiting on a question or a permission; the request says so
        elif status == "completed":
            await self.ingress.status(live, "turn_done_unseen")
        elif status == "failed":
            state = self.manager.live_state(session_id)
            await self.ingress.status(live, "error", detail=str(getattr(state, "last_error_message", "") or "the run failed")[:500])
        else:
            await self.ingress.status(live, "idle", detail="interrupted")
        usage = await self.runtime(live.staff).usage(live)
        if usage is not None:
            await self.ingress.usage(live, usage)

    async def on_session_deleted(self, session_id: str) -> None:
        live = await self.live_for_session(session_id)
        if live is not None:
            await self._end(live, "the session was deleted", stop=False)
            self.queue.pump_soon(live.staff.project_id)

    async def on_bus(self, event: AppEvent) -> None:
        """The session-level events of staff sessions, and the board's moves."""
        if event.type in ("task.changed", "task.moved", "staff.status", "terminal.exited"):
            dispatcher = self.app.extensions.get("effects")
            if dispatcher is not None:
                dispatcher.notify()
        if event.type == "permission.pending" and event.session_id:
            live = await self.live_for_session(event.session_id)
            if live is not None:
                p = event.payload
                # A session may say who decides its request: a sensitive browser action is the
                # operator's, whatever the project's autonomy gives the orchestrator.
                route = "operator" if p.get("routed_to") == "operator" else None
                await self.ingress.permission(live, str(p.get("request_id") or ""), str(p.get("tool") or ""), str(p.get("text") or ""), event_ref=str(p.get("request_ref") or ""), route=route)
        elif event.type == "permission.resolved" and event.session_id and event.payload.get("via") != "orchestrator":
            live = await self.live_for_session(event.session_id)
            if live is not None:
                row = await self.manager.db.fetchone(
                    "SELECT id FROM asks WHERE staff_session_id = ? AND request_ref = ? AND kind = 'permission' AND resolved_at IS NULL", (live.id, str(event.payload.get("request_id") or ""))
                )
                if row is not None and await self.manager.asks.resolve(row["id"], "operator", {"allow": event.payload.get("decision") == "allow", "via": event.payload.get("via"), "in_session": True}):
                    await self._after_answer(live)
        elif event.type == "presence":
            for session_id in (event.payload.get("newly_attended") or {}).get("sessions") or ():
                live = await self.live_for_session(str(session_id))
                if live is not None:
                    await self.seen(live)
        elif event.type == "task.moved" and event.payload.get("to") in FINISHED_TASK:
            await self._task_finished(str(event.payload.get("task_id") or ""))
        elif event.type == "task.moved" and event.payload.get("to") in ("todo", "review") and event.project_id:
            # To todo: a dependency finished, and a waiting task may go. To review: the member handed
            # its task in, and its next one may go now rather than at the next tick.
            self.queue.pump_soon(event.project_id)
        elif event.type in ("staff.status", "terminal.exited"):
            if event.type == "terminal.exited" and event.terminal_id:
                rows = await self.app.db.fetchall(
                    "SELECT a.staff_session_id,a.runtime_instance FROM execution_attempts a"
                    " JOIN staff_sessions s ON s.id = a.staff_session_id WHERE s.terminal_id = ?",
                    (event.terminal_id,),
                )
                for row in rows:
                    await observe_exit(self.app, staff_session_id=row["staff_session_id"],
                                       runtime_ref=event.terminal_id, observed_status="exited",
                                       runtime_instance=row["runtime_instance"], bus=self.manager.bus)
            if event.type == "terminal.exited" or event.payload.get("status") in ("exited", "idle", "turn_done_unseen", "error"):
                self.queue.pump_soon(event.project_id if event.type == "staff.status" else None)
            if event.type == "staff.status" and event.staff_id and event.payload.get("status") in ("idle", "turn_done_unseen", "error"):
                await self._one_off_done(event.staff_id)

    async def _task_finished(self, task_id: str) -> None:
        """A task done or dropped: nobody waits to start it, and a one-off helper goes with it."""
        task = await self.task(task_id)
        if task is None or task.project_id is None:
            return
        self.queue.withdraw(task.project_id, task_id=task.id)
        if not task.assignee_staff_id:
            return
        member = await self.manager.staff.get(task.assignee_staff_id)
        if member is None or not member.one_off or not member.active:
            self.queue.pump_soon(task.project_id)
            return
        live = await self.live_of(member)
        if live is not None and live.session.task_id == task.id:
            if live.session.status == "working":
                # A helper's own Report(done) finishes its task in the middle of its turn; stopping
                # it there cuts off the answer to that very call. It goes when the turn ends. Only a
                # turn in progress waits: one starting, asking or silent has nothing to finish.
                return
            await self._end(live, f"its task was {task.status}", stop=True)
        try:
            await self.manager.staff.archive(member.id, by="system")
            await self.publish("project.changed", {"change": "staff.dismissed", "actor": "system"}, member=member)
        except StaffBusy:
            logger.info("one-off %s still has a session; left on the team", member.name)
        self.queue.pump_soon(task.project_id)

    async def _one_off_done(self, staff_id: str) -> None:
        """A one-off helper whose turn ended on a task already finished goes now (see ``_task_finished``)."""
        member = await self.manager.staff.get(staff_id)
        if member is None or not member.one_off or not member.active:
            return
        live = await self.live_of(member)
        if live is None or not live.session.task_id:
            return
        task = await self.task(live.session.task_id)
        if task is not None and task.status in FINISHED_TASK:
            await self._task_finished(task.id)

    # -- the clock -----------------------------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> None:
        """Silence and stale requests: a working session that says nothing is shown silent, and a
        request the orchestrator has left too long goes to the operator."""
        now = now or datetime.now(UTC)
        config = self.manager.config.staff
        await self.settle(now)
        for session in await self.manager.staff.all_live():
            if _age_seconds(session.last_signal_at, now) > config.silence_minutes * 60 and await self.silence_watched(session):
                live = await self.live(session.id)
                if live is not None:
                    await self.ingress.status(live, "no_signal", detail=f"no signal for {config.silence_minutes} minutes")
        cutoff = (now - timedelta(minutes=config.ask_escalate_minutes)).isoformat()
        rows = await self.manager.db.fetchall("SELECT id FROM asks WHERE resolved_at IS NULL AND routed_to = 'orchestrator' AND routed_at < ?", (cutoff,))
        for row in rows:
            ask = await self.manager.asks.get(row["id"])
            if ask is not None:
                await self.escalate(ask, why=f"the orchestrator left it unanswered for {config.ask_escalate_minutes} minutes")
        await self.queue.pump()

    async def settle(self, now: datetime | None = None, *, screens: tuple[str, ...] = ("no_signal",)) -> int:
        """Settle the live command-line sessions whose rows are stale: by their tasks
        (``settle_stale``), then by their screens for the rows still in ``screens``. At start the
        host reads the screens of ``working`` rows too, since the previous host may have gone in the
        middle of a turn that ended while it was away; the ticker reads only the grey ones, which
        nothing else would ever look at again. Returns how many rows moved."""
        moved = 0
        for session in await self.manager.staff.all_live():
            if session.kind != "cli" or session.status not in ("working", "no_signal"):
                continue
            live = await self.live(session.id)
            if live is not None and (await self.settle_stale(live, now)).session.status != session.status:
                moved += 1
        for runtime in {id(r): r for r in self.runtimes.values()}.values():
            settle_idle = getattr(runtime, "settle_idle", None)
            if settle_idle is not None:
                try:
                    moved += await settle_idle(screens)
                except Exception:  # noqa: BLE001 — one runtime's screens must not stop the tick
                    logger.exception("settling the idle sessions of %s failed", getattr(runtime, "kind", "a runtime"))
        return moved

    async def recover_named_files(self) -> int:
        """Once: give members the files their open briefs name by an orchestrator's own inbox path.

        Before files travelled by handle, an orchestrator was told where an attachment lay in its own
        working directory and wrote that path into briefs, where no member could open it (a member on
        the host has no such directory at all). For each open task whose brief names a file of its
        project orchestrator's inbox that is still there, the file becomes the project's, goes with the
        task from now on, and a member already working on it gets it at once with a message naming the
        copy. Only files of that one inbox are taken, so a brief cannot pull in anything else.
        """
        if await self.manager.db.kv_get(RECOVERED_KEY):
            return 0
        workspaces = os.path.realpath(self.manager.settings.workspaces_dir)
        rows = await self.manager.db.fetchall("SELECT id, project_id, brief_json, assignee_staff_id FROM board_tasks WHERE status IN ('todo', 'doing', 'blocked')")
        taken = 0
        for row in rows:
            project = await self.manager.projects.get(row["project_id"]) if row["project_id"] else None
            if project is None or not project.settings.orchestrator.session_id:
                continue
            inboxes = await self._orchestrator_inboxes(project)
            if not inboxes:
                continue
            try:
                brief = json.loads(row["brief_json"] or "{}")
            except ValueError:
                continue
            text = " ".join(str(v) for v in brief.values()) if isinstance(brief, dict) else ""
            found: list[StoredFile] = []
            for path in dict.fromkeys(p.rstrip(".,;:)") for p in INBOX_PATH_RE.findall(text)):
                real = os.path.realpath(path)
                if not real.startswith(workspaces + os.sep) or not any(real.startswith(inbox + os.sep) for inbox in inboxes) or not os.path.isfile(real):
                    continue
                try:
                    found.append(await self.manager.files.add_path(Path(real), name=os.path.basename(real), origin="operator", origin_ref=path, scope=project.id, actor="system"))
                except FileRefused as exc:
                    logger.warning("the file %s named by task %s was not taken: %s", path, row["id"], exc)
            if not found:
                continue
            await self.manager.files.attach_to_task(row["id"], found, actor="system")
            taken += len(found)
            member = await self.manager.staff.get(row["assignee_staff_id"]) if row["assignee_staff_id"] else None
            live = await self.live_of(member) if member is not None else None
            if live is None or live.session.task_id != row["id"]:
                continue  # handed over with the brief at its next start
            try:
                await self.tell(
                    live.staff,
                    "The file your brief names by a path you could not open is now in your own folder; use this copy.",
                    when="now",
                    by="orchestrator",
                    files=found,
                )
            except StaffError as exc:
                logger.warning("could not hand %s the files of task %s: %s", live.staff.name, row["id"], exc)
        await self.manager.db.kv_set(RECOVERED_KEY, True)
        return taken

    async def _orchestrator_inboxes(self, project: Project) -> list[str]:
        """The inbox of each orchestrator session the project has had, real paths."""
        rows = await self.manager.db.fetchall(
            "SELECT id, metadata FROM sessions WHERE project_id = ? AND (json_extract(metadata, '$.orchestrator_of') IS NOT NULL OR json_extract(metadata, '$.orchestrator_retired_of') IS NOT NULL)",
            (project.id,),
        )
        out = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
                workspace = self.manager.workspace_of(row["id"], metadata, project)
            except (RuntimeError, ValueError):
                continue
            out.append(os.path.realpath(workspace / "inbox"))
        return out

    async def loop(self) -> None:
        await asyncio.sleep(FIRST_PUMP_SECONDS)
        try:
            await self.recover_named_files()
        except Exception:  # noqa: BLE001 — a one-off repair must not stop the team's tick
            logger.exception("recovering files named by open briefs failed")
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("staff tick failed")
            await asyncio.sleep(TICK_SECONDS)

    # -- the team server of command-line staff --------------------------------------------------------

    async def authenticate(self, staff_session_id: str, token: str) -> LiveSession:
        """The live session a team token was minted for; ``PermissionError`` for anything else."""
        expected = await self.manager.staff.team_token_hash(staff_session_id)
        if not token or not expected or not secrets.compare_digest(_hash(token), expected):
            raise PermissionError("the team token does not match this session")
        live = await self.live(staff_session_id)
        if live is None:
            raise PermissionError("this session has ended")
        return live

    def attach(self) -> asyncio.Task[None]:
        """Follow the sessions: every hook the team needs from the session manager and the bus."""
        manager = self.manager
        manager.service_hooks["staff"] = self.service
        manager.add_sink(self.on_turn_event)
        manager.run_started_hooks.append(self.on_run_started)
        manager.on_finished(self.on_run_finished)
        manager.delete_hooks.append(self.on_session_deleted)
        manager.answer_claims.append(self.claim_answer)
        notifications = self.app.notifications
        if notifications is not None and hasattr(notifications, "register_resolver"):
            notifications.register_resolver("staff", self.resolve_action)
        return manager.bus.on(
            EventFilter(types=("permission.pending", "permission.resolved", "presence", "task.changed", "task.moved", "staff.status", "terminal.exited")),
            self.on_bus,
            name="staff",
        )

    # -- the tools' hook -------------------------------------------------------------------------------

    async def service(self, op: str, **kwargs: Any) -> Any:
        session_id = str(kwargs.get("session_id") or "")
        live = await self.live_for_session(session_id)
        if live is None:
            raise RuntimeError("this session is not a staff member's live session")
        if op == "can_ask":
            return True
        if op == "report":
            return await self.ingress.report(
                live, str(kwargs.get("kind") or ""), str(kwargs.get("note") or ""), kwargs.get("artifacts"), kwargs.get("remember"),
                call_id=kwargs.get("call_id"), evidence=kwargs.get("evidence"), acknowledged=kwargs.get("acknowledged"),
                operator_steps=kwargs.get("operator_steps"),
            )
        raise ValueError(op)


class Ingress:
    """:class:`daedalus.staff_runtime.TeamIngress`: every change of a staff session's state, written and announced."""

    def __init__(self, team: Team) -> None:
        self.team = team

    @property
    def manager(self) -> SessionManager:
        return self.team.manager

    async def expects_signal(self, live: LiveSession) -> bool:
        return await self.team.silence_watched(live.session)

    async def phase_event(self, live: LiveSession, kind: str, *, meaningful_output: bool = False) -> None:
        """Project an observed CLI event onto its current attempt without treating chatter as work."""
        from daedalus.stores.phase_clocks import PhaseClocks  # Lazy: event ingress also runs without a current attempt.

        async with self.team.app.db.transaction() as conn:
            try:
                identity = await self.team.app.executions.check_staff(conn, live.id)
            except ControlDenied:
                return
            clocks = PhaseClocks(self.team.app.executions)
            if kind == "auth_required":
                await clocks.advance(conn, identity, from_phase="spawn", to_phase="auth")
                await clocks.advance(conn, identity, from_phase="ready", to_phase="auth")
            elif kind == "ready":
                await clocks.advance(conn, identity, from_phase="spawn", to_phase="ready")
                await clocks.advance(conn, identity, from_phase="auth", to_phase="ready")
                await clocks.advance(conn, identity, from_phase="ready", to_phase="first_output",
                                     timeout_seconds=self.manager.config.harness.no_signal_after_s)
            elif meaningful_output:
                await clocks.advance(conn, identity, from_phase="spawn", to_phase="ready")
                await clocks.advance(conn, identity, from_phase="auth", to_phase="ready")
                await clocks.advance(conn, identity, from_phase="ready", to_phase="first_output",
                                     timeout_seconds=self.manager.config.harness.no_signal_after_s)
                try:
                    await clocks.output(conn, identity,
                                        idle_timeout_seconds=self.manager.config.harness.no_signal_after_s)
                except ControlDenied:
                    return
                if kind == "turn_completed":
                    await clocks.close(conn, identity)
            elif kind in ("turn_completed", "turn_failed", "turn_cancelled", "session_ended", "process_exited"):
                await clocks.close(conn, identity)
            else:
                await clocks.heartbeat(conn, identity)

    async def status(self, live: LiveSession, status: str, waiting_for: str = "", *, detail: str = "", actor: str = "") -> None:
        # Compared with the row as it was, not with the caller's copy of it: two paths report the same
        # change — a command-line runtime sets the session working when it delivers an answer, and the
        # team does after it — and a stale copy would announce the second as a change of its own.
        before = await self.manager.staff.session(live.id)
        # One line: it is a status, and the whole question is on the request.
        changed = await self.manager.staff.set_status(live.id, status, " ".join(waiting_for.split())[:200])
        if changed is None:
            return
        previous, session = changed
        if previous == status and session.waiting_for == (before.waiting_for if before is not None else live.session.waiting_for):
            return
        payload: dict[str, Any] = {"status": status, "previous": previous}
        if session.waiting_for:
            payload["waiting_for"] = session.waiting_for
        if detail:
            payload["detail"] = detail[:500]
        if actor:
            payload["actor"] = actor
        await self.team.publish("staff.status", payload, member=live.staff, session_id=live.session_id)
        if status == "turn_done_unseen" and session.pause_requested and session.kind == "cli":
            # A command-line member's turn ends here, not in a run of this host: this is where a
            # pause asked for during the turn takes effect (a Daedalus member's in on_run_finished).
            await self.team._settle_pause(LiveSession(live.staff, session))

    async def _open(self, live: LiveSession, kind: str, request_ref: str, text: str, detail: dict[str, Any], event_ref: str | None, route: str | None = None, risk: str = "routine") -> Ask:
        if request_ref:
            # The same request seen again — a hook the daemon replayed after the host restarted — is
            # the request already open, not a second one for the orchestrator to answer twice.
            row = await self.manager.db.fetchone(
                "SELECT id FROM asks WHERE staff_session_id = ? AND request_ref = ? AND kind = ? AND resolved_at IS NULL LIMIT 1", (live.id, request_ref, kind)
            )
            existing = await self.manager.asks.get(row["id"]) if row is not None else None
            if existing is not None:
                return existing
        project = await self.team.project(live.staff.project_id)
        routed = route or self.team.route(project, kind)
        ask = await self.manager.asks.open(
            project.id,
            origin="staff",
            kind=kind,
            text=text.strip()[:8000] or f"{live.staff.name} asks",
            routed_to=routed,
            staff_id=live.staff.id,
            staff_session_id=live.id,
            task_id=live.session.task_id,
            request_ref=request_ref,
            # The risk is kept with the request: handed to the operator later, an elevated one must still
            # be answered in the app, never from a lock screen.
            detail={**detail, "event_ref": event_ref or "", "risk": risk},
        )
        if event_ref is None:
            # A command-line member's request has no session to announce it, so the ingress does,
            # under a reference of its own that answering it resolves.
            ref = f"staff:{live.id}:{ask.id}"
            await self.manager.db.execute("UPDATE asks SET detail_json = json_set(detail_json, '$.event_ref', ?) WHERE id = ?", (ref, ask.id))
            ask = (await self.manager.asks.get(ask.id)) or ask
            common = {"request_id": ask.id, "request_ref": ref, "title": live.staff.name, "telegram": False, "routed_to": routed, "short_id": ask.short_id}
            if kind == "permission":
                # An elevated request (what a browser buys or sends) is answered in the app, never
                # from a lock screen.
                await self.team.publish("permission.pending", {**common, "kind": "staff", "tool": str(detail.get("tool") or ""), "text": text[:300], "risk": risk, "quick": routed == "operator" and risk != "elevated"}, member=live.staff)
            else:
                options = [{"label": o, "description": ""} for o in detail.get("options") or []]
                await self.team.publish("ask.pending", {**common, "run_id": "", "questions": [{"question": text[:2000], "options": options, "multi": False, "custom": True}], "operator_facing": routed == "operator"}, member=live.staff)
        elif routed == "operator":
            await self.team._release_hold(ask)
        return ask

    async def permission(self, live: LiveSession, request_ref: str, tool: str, summary: str, *, event_ref: str | None = None, route: str | None = None, risk: str = "routine") -> str:
        ask = await self._open(live, "permission", request_ref, f"{tool}: {summary}" if tool else summary, {"tool": tool}, event_ref, route=route, risk=risk)
        await self.status(live, "permission", f"permission [{ask.short_id}]: {tool or summary}")
        return ask.id

    async def question(self, live: LiveSession, request_ref: str, text: str, options: list[str], *, event_ref: str | None = None, call_id: str | None = None) -> str:
        detail: dict[str, Any] = {"options": list(options)}
        if call_id:
            detail["call_id"] = call_id
        ask = await self._open(live, "question", request_ref, text, detail, event_ref)
        await self.status(live, "question", f"question [{ask.short_id}]: {text}")
        return ask.id

    async def asked(self, live: LiveSession, call_id: str) -> Ask | None:
        row = await self.manager.db.fetchone(
            "SELECT id FROM asks WHERE staff_session_id = ? AND json_extract(detail_json, '$.call_id') = ? ORDER BY created_at DESC LIMIT 1", (live.id, call_id)
        )
        return await self.manager.asks.get(row["id"]) if row is not None else None

    async def message_state(self, message_id: str, state: str, error: str = "") -> None:
        before = await self.manager.staff.message(message_id)
        after = await self.manager.staff.set_message_state(message_id, state, error)
        if after is None or before is None or after.state == before.state:
            return
        member = await self.manager.staff.get(after.staff_id)
        payload: dict[str, Any] = {"message_id": message_id, "state": after.state}
        if after.error:
            payload["error"] = after.error[:500]
        await self.team.publish("staff.message", payload, member=member)

    async def report(
        self, live: LiveSession, kind: str, note: str, artifacts: list[str] | None = None, remember: str | None = None, *,
        call_id: str | None = None, evidence: list[dict[str, str]] | None = None, acknowledged: list[str] | None = None,
        operator_steps: dict[str, Any] | None = None,
    ) -> str:
        response, event = await StaffReportService(self.team.app).submit(
            live, kind, note, artifacts=artifacts, remember=remember, evidence=evidence,
            acknowledged=acknowledged, operator_steps=operator_steps, call_id=call_id or "",
        )
        await self._published(event)
        told = f"reported {kind}; report {response['report_id']}"
        if response["confirmed"]:
            told += "; confirmed " + ", ".join(response["confirmed"])
        if response["unknown"]:
            told += "; requirements not confirmed: " + ", ".join(response["unknown"])
        if response["kept_files"]:
            told += "; kept for the team: " + ", ".join(item["name"] for item in response["kept_files"])
        if response["not_kept"]:
            told += "; not kept: " + "; ".join(response["not_kept"])
        if kind == "done":
            told += "; task is in review"
            if response["unproven"]:
                told += "; you gave no evidence for " + ", ".join(response["unproven"])
        return told

    async def _published(self, event: AppEvent | None) -> None:
        if event is None:
            return
        for hook in self.team.report_hooks:
            try:
                await hook(event)
            except Exception:  # noqa: BLE001 — the report stands; a follower that fails is logged
                logger.exception("a report hook failed on %s", event.seq)

    async def implicit_report(self, live: LiveSession, kind: str, text: str) -> None:
        task = await self.team.task(live.session.task_id) if live.session.task_id else None
        payload: dict[str, Any] = {"kind": kind, "text": text[:NOTE_MAX], "actor": "system", "implicit": True}
        if task is not None:
            payload["task_id"] = task.id
        await self._published(await self.team.publish("staff.report", payload, member=live.staff, session_id=live.session_id))

    async def channel(self, live: LiveSession, team_tools: str, detail: str = "") -> None:
        payload: dict[str, Any] = {"team_tools": team_tools}
        if detail:
            payload["detail"] = detail[:500]
        await self.team.publish("staff.channel", payload, member=live.staff, session_id=live.session_id)

    async def messages_of(self, live: LiveSession) -> list[Any]:
        messages = await self.manager.staff.messages(live.staff.id, limit=500)
        return [m for m in reversed(list(messages)) if m.staff_session_id == live.id]

    async def ask(self, live: LiveSession, question: str, options: list[str] | None = None, context: str = "") -> str:
        text = question.strip() + (f"\n\nContext: {context.strip()}" if context.strip() else "")
        return await self.question(live, f"team:{uuid.uuid4().hex[:12]}", text, list(options or []))

    async def usage(self, live: LiveSession, snapshot: UsageSnapshot) -> None:
        await self.manager.staff.record_usage(live.id, snapshot.view())

    async def signal(self, live: LiveSession) -> None:
        await self.manager.staff.touch(live.id)

    async def resolved(self, live: LiveSession, request_ref: str, *, by: str = "operator", via: str = "terminal") -> bool:
        row = await self.manager.db.fetchone(
            "SELECT id FROM asks WHERE staff_session_id = ? AND request_ref = ? AND resolved_at IS NULL ORDER BY created_at DESC LIMIT 1", (live.id, request_ref)
        )
        ask = await self.manager.asks.get(row["id"]) if row is not None else None
        if ask is None or not await self.manager.asks.resolve(ask.id, by, {"via": via, "in_session": True}):
            return False
        if via == "withdrawn":
            await self.team._withdrawn(ask)
        else:
            await self.team._announce_resolved(ask, allow=None, by=by, via=via)
        return True

    async def located(self, live: LiveSession, *, cli_session_id: str | None = None, transcript_ref: str | None = None) -> None:
        await self.manager.staff.started(live.id, cli_session_id=cli_session_id, transcript_ref=transcript_ref)

    async def ended(self, live: LiveSession, reason: str) -> None:
        await self.team._end(live, reason, stop=False)
        # The process is gone, so nobody works the card: left in doing, it read as taken on the
        # board and refused every other member (see ``Team._holder``).
        await self.team.free_card(live.staff, live.session.task_id, f"the session ended ({reason})")
        # A place under the project's concurrency came free.
        self.team.queue.pump_soon(live.staff.project_id)


async def install(app: Application) -> list[asyncio.Task[None]]:
    from daedalus.extensions.review import Review  # Lazy: review.py imports this module for its names

    team = Team(app)
    team.review = Review(app, team)
    app.extensions["staff"] = team
    handler = team.attach()
    return [handler, asyncio.create_task(team.loop(), name="staff")]


__all__ = ["AlreadyAnswered", "Ingress", "Team", "install"]
