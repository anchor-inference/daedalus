"""What the orchestrator's team tools do: hire, change and dismiss staff, hand them work, talk to them,
read what they did, answer what they ask, and stop or pause them.

The limits live below this module, in the staff store and in :class:`daedalus.extensions.staff.Team`,
so the operator's routes and these tools refuse the same things: a grant without a quoted allowance, a
dismissal of someone still working, a task with half a brief. What this module adds is what only an
orchestrator needs — names resolved from what a model typed, the harness catalog checked before a hire,
and refusals worded so the orchestrator knows its next move.

Every operation receives the project the calling session is the current orchestrator of; the
dispatcher in :mod:`daedalus.extensions.orchestrator_ops` checked that first.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from daedalus.extensions.orchestrator_ops import Refused, _folder
from daedalus.host.events import EventFilter
from daedalus.staff_runtime import LiveSession, ReadRequest
from daedalus.stores.files import FileRefused, StoredFile
from daedalus.stores.projects import Project, ProjectFolder
from daedalus.stores.staff import HARNESS_NAMES, HARNESSES, ISOLATIONS, MESSAGE_MODES, Staff, StaffBusy, StaffError

if TYPE_CHECKING:
    from daedalus.extensions.orchestrator import Orchestrators

logger = logging.getLogger(__name__)

HANDED_IN = ("review", "done", "dropped")
"""A task whose member is finished with it: another Assign of it opens its next round."""
FOLLOW_UP_WINDOW = timedelta(hours=2)
"""How soon after a member handed a card in a new assignment to them counts as the next round of it."""
ROUND_RE = re.compile(r"\] round \d+ begins")
"""A round's line in a card's notes, as ``_round_note`` writes it after the board's timestamp."""
CONTRACT_FIELDS = ("objective", "deliverable", "boundaries", "done_when")
CONTRACT_MIN = 8
"""The least each part of a task's brief may be: enough to be a sentence, so "tbd" is not a contract."""
READ_WHATS = ("last", "turns", "screen", "diff", "reports")
READ_TURNS_MAX = 20
RUNTIME_TIMEOUT_SECONDS = 30.0
"""How long a read or an interrupt may take before the orchestrator is told it did not answer: a runtime
that hangs must not hold the orchestrator's turn with it."""
CURSOR_SEPARATOR = "~"
TELL_MAX = 8000


# -- small helpers -----------------------------------------------------------------------------------------


def _team(orch: Orchestrators) -> Any:
    team = orch.team
    if team is None:
        raise Refused("the staff runtime is not running on this installation")
    return team


async def _member(orch: Orchestrators, project: Project, ref: str, *, active: bool = True) -> Staff:
    member = await orch.manager.staff.find(project.id, (ref or "").strip())
    if member is None:
        raise Refused(f"{project.name} has nobody called {ref!r}; Team() lists the team")
    if active and not member.active:
        raise Refused(f"{member.name} has been dismissed")
    return member


def _label(harness: str) -> str:
    return HARNESS_NAMES.get(harness, harness)


async def _changed(orch: Orchestrators, member: Staff, change: str) -> None:
    """The same ``project.changed`` the operator's routes publish, so another window's team page follows."""
    try:
        await orch.manager.bus.publish("project.changed", {"change": change, "actor": "orchestrator"}, project_id=member.project_id, staff_id=member.id)
    except Exception:  # noqa: BLE001 — the change is written; the event is a courtesy
        logger.warning("could not publish project.changed for %s", member.id, exc_info=True)


async def _journal(orch: Orchestrators, project: Project, kind: str, text: str, refs: dict[str, Any]) -> None:
    await orch.manager.projects.record(project.id, "system", kind, text, refs)
    await orch._changed(project.id, "journal", "orchestrator")


async def _catalog_problem(orch: Orchestrators, harness: str, env: str, folder: ProjectFolder | None, fields: dict[str, str]) -> None:
    """Refuse a command-line member whose agent, model, mode or effort its CLI does not offer.

    Only lists the catalog actually holds are checked: an empty list means the last check found none
    or never ran, and refusing everything then would make hiring impossible until the operator opens
    the Harnesses screen. The manager may not be installed at all; then nothing is checked here and
    the launch's own readiness check is what refuses.
    """
    manager: Any = orch.app.extensions.get("harness")
    if manager is None:
        return
    problem = getattr(manager, "hire_problem", None)
    if problem is not None:
        reason = str(await problem(env, harness) or "")
        if reason:
            raise Refused(reason)
    catalog_of = getattr(manager, "catalog", None)
    if catalog_of is None:
        return
    try:
        catalog = await catalog_of(env, harness, folder.id if folder is not None and folder.env == env else None)
    except (KeyError, ValueError) as exc:
        raise Refused(f"the catalog of {_label(harness)} cannot be read: {exc}") from exc
    offered = {
        "agent": [a.name for a in catalog.agents],
        "model": list(catalog.models),
        "permission_mode": list(catalog.modes),
        "effort": list(catalog.efforts),
    }
    for name, value in fields.items():
        choices = offered.get(name) or []
        if value and choices and value not in choices:
            shown = ", ".join(choices[:20]) + (" …" if len(choices) > 20 else "")
            raise Refused(f"{_label(harness)} in the {env} offers no {name.replace('_', ' ')} {value!r}; it offers {shown} (Harnesses(harness={harness!r}) lists them)")


# -- hiring, editing, dismissing ------------------------------------------------------------------------


async def hire(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    name: str,
    role: str,
    harness: str = "daedalus",
    agent: str = "",
    model: str = "",
    effort: str = "",
    permission_mode: str = "",
    env: str = "",
    folder: str | None = None,
    isolation: str | None = None,
    instructions: str = "",
    one_off: bool = False,
) -> str:
    team = _team(orch)
    harness = (harness or "daedalus").strip().lower()
    if harness not in HARNESSES:
        raise Refused(f"harness is one of {', '.join(HARNESSES)}")
    runtime = team.runtimes.get(harness)
    if runtime is None:
        # The operator may still hire one by hand for later; the orchestrator hires only who can start.
        raise Refused(f"{_label(harness)} is not installed on this installation: nothing here can run its staff yet")
    target = _folder(project, folder) if folder else None
    local = orch.manager.projects.local_env
    if harness == "daedalus":
        where = env or local
    else:
        where = env or (target.env if target is not None else "") or project.settings.default_env or (project.primary.env if project.primary else local)
    available = await runtime.available(where)
    if not available.ok:
        raise Refused(f"{_label(harness)} staff cannot run in the {where} now: {available.reason}")
    if harness != "daedalus":
        await _catalog_problem(orch, harness, where, target, {"agent": agent, "model": model, "permission_mode": permission_mode, "effort": effort})
    if isolation is None or isolation == "":
        home = target or project.primary
        isolation = "worktree" if home is not None and home.is_git and not home.readonly else "shared"
    if isolation not in ISOLATIONS:
        raise Refused(f"isolation is one of {', '.join(ISOLATIONS)}")
    try:
        member = await orch.manager.staff.hire(
            project.id,
            name=name, role=role, harness=harness, agent=agent, model=model, effort=effort, permission_mode=permission_mode,
            env=where if harness != "daedalus" else "", folder_id=target.id if target is not None else None, isolation=isolation,
            instructions=instructions, one_off=bool(one_off), created_by="orchestrator",
        )
    except StaffError as exc:
        raise Refused(str(exc)) from exc
    except KeyError as exc:
        raise Refused(f"{project.name} is gone") from exc
    # The store wrote the hire into the journal in the same transaction.
    await _changed(orch, member, "staff.hired")
    await orch._changed(project.id, "journal", "orchestrator")
    details = [_label(member.harness), member.isolation]
    details += [x for x in (member.agent, member.model, member.effort, member.permission_mode) if x]
    told = f"hired {member.name} [{member.id}] ({', '.join(details)}{', one-off' if member.one_off else ''}); Assign gives them a task"
    warn_of = getattr(orch.app.extensions.get("harness"), "hire_warning", None)
    warning = str(await warn_of(where, harness) or "") if harness != "daedalus" and warn_of is not None else ""
    return f"{told}. Warning: {warning}" if warning else told


async def staff_edit(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    staff: str,
    role: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    env: str | None = None,
    folder: str | None = None,
    isolation: str | None = None,
    instructions: str | None = None,
    notes: str | None = None,
) -> str:
    member = await _member(orch, project, staff)
    changes: dict[str, Any] = {
        k: v for k, v in (
            ("role", role), ("agent", agent), ("model", model), ("effort", effort), ("permission_mode", permission_mode),
            ("env", env), ("isolation", isolation), ("instructions", instructions), ("notes", notes),
        ) if v is not None
    }
    target: ProjectFolder | None = None
    if folder is not None:
        if folder == "":
            changes["default_folder_id"] = ""
        else:
            target = _folder(project, folder)
            changes["default_folder_id"] = target.id
    if not changes:
        raise Refused("say what changes: role, agent, model, effort, permission_mode, env, folder, isolation, instructions or notes")
    if member.harness != "daedalus":
        where = env or member.env or (target.env if target is not None else orch.manager.projects.local_env)
        await _catalog_problem(orch, member.harness, where, target, {k: str(changes[k]) for k in ("agent", "model", "permission_mode", "effort") if k in changes})
    try:
        updated = await orch.manager.staff.update(member.id, **changes)
    except StaffError as exc:
        raise Refused(str(exc)) from exc
    names = sorted("folder" if k == "default_folder_id" else k.replace("_", " ") for k in changes)
    await _journal(orch, project, "staff", f"The orchestrator changed {updated.name}'s {', '.join(names)}.", {"staff_id": updated.id, "fields": names})
    await _changed(orch, updated, "staff.updated")
    live = await orch.manager.staff.live(updated.id)
    when = "from their next session; the current one goes on as it started" if live is not None else "from their next session"
    return f"{updated.name}: {', '.join(names)} changed, in effect {when}"


async def dismiss(orch: Orchestrators, project: Project, session_id: str, *, staff: str, release: bool = False, keep_worktree: bool = True) -> str:
    member = await _member(orch, project, staff)
    live = await orch.manager.staff.live(member.id)
    released = ""
    if live is not None:
        if not release:
            raise Refused(f"{member.name} has a live session ({live.status.replace('_', ' ')}); Dismiss(release=true) ends it first, or Release them and dismiss later")
        await _team(orch).release(member, keep_worktree=keep_worktree, reason="dismissed by the orchestrator", by="orchestrator")
        released = "; their session was ended" + ("" if keep_worktree else " and the worktree removed if it was clean")
    try:
        archived = await orch.manager.staff.archive(member.id, by="orchestrator")
    except StaffBusy as exc:
        raise Refused(str(exc)) from exc
    await _changed(orch, archived, "staff.dismissed")
    await orch._changed(project.id, "journal", "orchestrator")
    return f"dismissed {archived.name}{released}; their branch and history stay"


# -- handing out work ----------------------------------------------------------------------------------------


async def assign(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    staff: str,
    task_id: str | None = None,
    title: str | None = None,
    objective: str | None = None,
    deliverable: str | None = None,
    boundaries: str | None = None,
    done_when: str | None = None,
    folder: str | None = None,
    priority: int | None = None,
    depends_on: list[str] | None = None,
    files: list[str] | None = None,
) -> str:
    team = _team(orch)
    board = orch.board
    if board is None:
        raise Refused("the board is not available on this installation")
    member = await _member(orch, project, staff)
    handed = await _files(orch, project, session_id, files)
    given = {k: v.strip() for k, v in (("objective", objective), ("deliverable", deliverable), ("boundaries", boundaries), ("done_when", done_when)) if v is not None and v.strip()}
    target = _folder(project, folder) if folder else None
    existing: dict[str, Any] | None = None
    continued = False
    if task_id:
        try:
            existing = await board.get(task_id, actor=session_id)
        except KeyError as exc:
            raise Refused(f"no task {task_id} on {project.name}'s board; Tasks() lists them") from exc
        if existing["status"] in HANDED_IN and existing.get("branch"):
            raise Refused(
                f"task {task_id} has work on branch {existing['branch']}, which the operator reviews and merges; "
                "for more work on it, create a new task"
            )
    elif not (title or "").strip():
        raise Refused("Assign needs a task_id, or a title and the four parts of a brief for a new task")
    else:
        existing = await _follow_up_of(orch, project, member, session_id, depends_on=depends_on, folder=target)
        continued = existing is not None
    # Checked before anything is written, so a refused hand-over leaves no half-briefed task behind.
    merged = {k: str((existing or {}).get("brief", {}).get(k) or "").strip() for k in CONTRACT_FIELDS}
    merged.update(given)
    short = [k.replace("_", "-") for k in CONTRACT_FIELDS if len(merged[k]) < CONTRACT_MIN]
    if short:
        raise Refused(
            f"the brief has no usable {', '.join(short)} (each part at least {CONTRACT_MIN} characters); "
            "a task is handed over with its objective, deliverable, boundaries and done_when"
        )
    try:
        if existing is None:
            task = await board.add(title=str(title).strip(), session_id=session_id, brief=merged, depends_on=depends_on, priority=int(priority or 3))
        elif existing["status"] in HANDED_IN:
            # The next round of work already on the board: the same card is reopened with the new brief,
            # and the round it replaces is kept in its notes, rather than a new card per round.
            renamed = (title or "").strip()
            renamed = renamed if renamed and renamed != existing["title"] else None
            task = await board.update(
                existing["id"], actor=session_id, status="todo", note=_round_note(existing), title=renamed, brief=given or None, depends_on=depends_on, priority=priority,
            )
        else:
            task = existing
            if given or depends_on is not None or priority is not None:
                task = await board.update(task["id"], actor=session_id, brief=given or None, depends_on=depends_on, priority=priority)
    except KeyError as exc:
        raise Refused(f"no task {exc.args[0] if exc.args else ''} on {project.name}'s board") from exc
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    if target is not None and task.get("folder_id") != target.id:
        if task.get("status") == "doing":
            raise Refused(f"task {task['id']} is being worked on in its folder; release its worker before moving it")
        # The board has no folder of its own to set; the team reads the task's folder at the start.
        await orch.manager.db.execute("UPDATE board_tasks SET folder_id = ? WHERE id = ?", (target.id, task["id"]))
    if handed:
        # On the task, not on this call: a task that waits in the queue, or is started again after a
        # crash, is handed its files at every start.
        await orch.manager.files.attach_to_task(task["id"], handed, actor="orchestrator")
    try:
        launched = await team.assign(member, task["id"], by="orchestrator")
    except KeyError as exc:
        raise Refused(f"no task {task['id']} on {project.name}'s board") from exc
    except StaffError as exc:
        raise Refused(f"task {task['id']} stays on the board, unstarted: {exc}") from exc
    lead = ""
    if continued:
        lead = (
            f"This continues {member.name}'s task {task['id']}, which they handed in a moment ago, as its next round on the same card. "
            "For unrelated work, create the task with Tasks(op='create') and Assign its task_id. "
        )
    if launched.get("state") == "started":
        return lead + f"{member.name} started on {task['id']} \"{task['title']}\"" + await _delivered_note(orch, task["id"], handed)
    waits = " The files are copied to them when they start." if handed else ""
    return lead + f"{member.name} will start {task['id']} \"{task['title']}\" when it is their turn: queue position {launched.get('position')} — {launched.get('detail')}{waits}"


async def _follow_up_of(orch: Orchestrators, project: Project, member: Staff, session_id: str, *, depends_on: list[str] | None, folder: ProjectFolder | None) -> dict[str, Any] | None:
    """The card a new assignment continues, when it is plainly the next round of the same work.

    An orchestrator talks to a member in rounds — assess, plan, revise the plan, fold in the
    operator's answers, implement — and gave every round a title and so a card of its own: one member
    left thirteen cards in two days, each a step of the same two pieces of work. A new assignment is
    taken as the next round when the member's latest card is one the orchestrator wrote, has no branch,
    was handed in within ``FOLLOW_UP_WINDOW``, and nothing waits on it; the call itself waits on
    nothing and names no other folder. Anything else gets a card of its own.
    """
    row = await orch.manager.db.fetchone(
        "SELECT id, status, branch, origin_session_id, folder_id, updated_at FROM board_tasks WHERE project_id = ? AND assignee_staff_id = ? ORDER BY created_at DESC LIMIT 1",
        (project.id, member.id),
    )
    if row is None or depends_on or row["status"] not in HANDED_IN or row["branch"] or not row["origin_session_id"]:
        return None
    if folder is not None and row["folder_id"] and row["folder_id"] != folder.id:
        return None
    try:
        handed_in = datetime.fromisoformat(row["updated_at"])
    except (TypeError, ValueError):
        return None
    handed_in = handed_in if handed_in.tzinfo else handed_in.replace(tzinfo=UTC)
    if datetime.now(UTC) - handed_in > FOLLOW_UP_WINDOW:
        return None
    if await orch.manager.db.fetchone("SELECT 1 FROM board_tasks WHERE depends_on LIKE ?", (f'%"{row["id"]}"%',)) is not None:
        return None
    try:
        return await orch.board.get(row["id"], actor=session_id)  # type: ignore[no-any-return]
    except KeyError:
        return None


def _round_note(task: dict[str, Any]) -> str:
    """The history line a reopened card keeps: which round begins and what the one before it asked,
    since the new brief replaces the old one on the card."""
    rounds = len(ROUND_RE.findall(str(task.get("notes") or ""))) + 2
    before = " ".join(str(task.get("brief", {}).get("objective") or "").split())[:300]
    return f"round {rounds} begins, reopened from {task['status']} by the orchestrator; round {rounds - 1} was \"{task['title']}\"" + (f": {before}" if before else "")


async def _files(orch: Orchestrators, project: Project, session_id: str, refs: list[str] | None) -> list[StoredFile]:
    """The files a hand-over names, all checked before anything is written or sent."""
    if not refs:
        return []
    try:
        return await _team(orch).handoff.resolve(refs, project=project, actor="orchestrator")
    except FileRefused as exc:
        raise Refused(str(exc)) from exc


async def _delivered_note(orch: Orchestrators, task_id: str, handed: list[StoredFile]) -> str:
    """Where the member got the files, from the delivery records: the orchestrator learns the member
    has them, not a path it should repeat — the brief already names them."""
    if not handed:
        return ""
    return f"; {len(handed)} file{'s' if len(handed) > 1 else ''} copied where they can open it{'' if len(handed) == 1 else ' them'}, and the brief names {'it' if len(handed) == 1 else 'them'}"


# -- talking and reading -------------------------------------------------------------------------------------


async def tell(orch: Orchestrators, project: Project, session_id: str, *, staff: str, text: str, when: str = "now", files: list[str] | None = None) -> str:
    if when not in MESSAGE_MODES:
        raise Refused(f"when is one of {', '.join(MESSAGE_MODES)}")
    body = (text or "").strip()
    if not body:
        raise Refused("the message is empty")
    if len(body) > TELL_MAX:
        raise Refused(f"a message is at most {TELL_MAX} characters; put the detail in the task or the journal")
    member = await _member(orch, project, staff)
    handed = await _files(orch, project, session_id, files)
    try:
        receipt = await _team(orch).tell(member, body, when=when, by="orchestrator", files=handed)
    except StaffError as exc:
        raise Refused(str(exc)) from exc
    line = f"message {receipt['message_id']} to {member.name} ({when}): {receipt['state']}"
    if receipt.get("files"):
        line += f", with {len(receipt['files'])} file{'s' if len(receipt['files']) > 1 else ''} copied where they can open {'it' if len(receipt['files']) == 1 else 'them'}"
    degraded = receipt.get("degraded_to")
    if degraded:
        line += " — " + _degraded(member, degraded)
    if receipt.get("error"):
        line += f" — {receipt['error']}"
    return line


def _degraded(member: Staff, degraded: str) -> str:
    """What became of a message for now whose executor cannot take one into a running turn, in words
    the orchestrator can act on: it may want Interrupt instead, or nothing at all."""
    label = HARNESS_NAMES.get(member.harness, member.harness)
    if degraded == "after_turn":
        return f"{label} cannot take a message into a running turn, so it goes in when {member.name}'s turn ends; Interrupt first if it cannot wait"
    return f"in {label} a message during a turn stops that turn, so {member.name}'s turn was interrupted and the message starts the next one"


def _cursor(staff_session_id: str, inner: str | None) -> str | None:
    return f"{staff_session_id}{CURSOR_SEPARATOR}{inner}" if inner else None


async def _reports(orch: Orchestrators, member: Staff, count: int, limit: int) -> str:
    """A member's last reports, whole, oldest first.

    A command-line member often puts its real answer in a report and ends the turn on a line such as
    "answered above", so its last reply says nothing; and a long wake-up is compacted away. Without
    this the orchestrator had no way back to what the member reported.
    """
    events = await orch.manager.bus.latest(EventFilter(types=("staff.report",), staff_id=member.id), limit=count)
    if not events:
        return f"{member.name} has sent no reports yet"
    parts = []
    for event in events:
        p = event.payload
        kind = str(p.get("kind") or "report") + (" (made from the turn's last message)" if p.get("implicit") else "")
        task = f" on task {p['task_id']}" if p.get("task_id") else ""
        parts.append(f"— {event.at[:16].replace('T', ' ')} {kind}{task}:\n{str(p.get('text') or '').strip()}")
    text = f"{member.name}'s last {len(parts)} report{'s' if len(parts) != 1 else ''}, oldest first:\n" + "\n\n".join(parts)
    if len(text) > limit:
        text = text[:limit].rstrip() + f"\n[cut to {limit} characters; ask for fewer reports (turns) or a larger max_chars]"
    return text


async def read_staff(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    staff: str,
    what: str = "last",
    turns: int = 1,
    cursor: str | None = None,
    max_chars: int | None = None,
) -> str:
    """A bounded page of what a member did. The cursor names the session it came from, so a cursor
    kept across a new task is refused instead of being applied to a different transcript."""
    if what not in READ_WHATS:
        raise Refused(f"what is one of {', '.join(READ_WHATS)}")
    team = _team(orch)
    member = await _member(orch, project, staff, active=False)
    config = orch.manager.config.staff
    limit = config.read_default_chars if max_chars is None else max(200, min(int(max_chars), config.read_max_chars))
    if what == "reports":
        return await _reports(orch, member, max(1, min(int(turns), READ_TURNS_MAX)), limit)
    session = await orch.manager.staff.live(member.id)
    if session is None:
        latest = await orch.manager.staff.sessions(member.id, limit=1)
        if not latest:
            raise Refused(f"{member.name} has not worked yet")
        session = latest[0]
    inner: str | None = None
    if cursor:
        owner, _, inner = cursor.partition(CURSOR_SEPARATOR)
        if owner != session.id or not inner:
            raise Refused(f"that cursor belongs to another of {member.name}'s sessions; read again without it")
    live = LiveSession(member, session)
    try:
        async with asyncio.timeout(RUNTIME_TIMEOUT_SECONDS):
            page = await team.runtime(member).read(live, ReadRequest(what=what, turns=max(1, min(int(turns), READ_TURNS_MAX)), cursor=inner, max_chars=limit))  # type: ignore[arg-type]
    except StaffError as exc:
        raise Refused(str(exc)) from exc
    except TimeoutError as exc:
        raise Refused(f"{member.name}'s runtime did not answer within {int(RUNTIME_TIMEOUT_SECONDS)} s") from exc
    if session.live:
        # Read is seen: a finished turn stops being news for the team page and the next wake-up.
        await team.seen(live)
    state = "live" if session.live else f"ended ({session.end_reason or session.status})"
    head = f"{member.name}, session {session.id} ({state}, {session.status.replace('_', ' ')}), {what}:"
    tail: list[str] = []
    if page.truncated:
        tail.append(f"[cut to {limit} characters" + (f"; max_chars up to {config.read_max_chars} shows more]" if limit < config.read_max_chars else "]"))
    next_cursor = _cursor(session.id, page.next_cursor)
    if next_cursor and what in ("last", "turns"):
        tail.append(f"[for what comes after this later: cursor={next_cursor!r}]")
    return "\n".join([head, page.text or "(nothing)", *tail])


# -- answering -------------------------------------------------------------------------------------------------


async def answer(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    request_id: str,
    allow: bool | None = None,
    text: str | None = None,
    selected: list[str] | None = None,
    basis: str = "",
    escalate: bool = False,
) -> str:
    team = _team(orch)
    ask = await orch.manager.asks.get((request_id or "").strip())
    if ask is None or ask.project_id != project.id:
        raise Refused(f"{project.name} has no open request {request_id!r}; the state block lists them")
    if not ask.open:
        raise Refused(f"request {ask.short_id} was already answered by the {ask.resolved_by}")
    if ask.routed_to != "orchestrator":
        raise Refused(f"request {ask.short_id} is the operator's to answer")
    if escalate:
        why = (basis or "").strip() or "the orchestrator could not decide it"
        moved = await team.escalate(ask, why=why, suggestion=(text or "").strip() or ", ".join(selected or []))
        if not moved:
            current = await orch.manager.asks.get(ask.id)
            raise Refused(f"request {ask.short_id} could not be escalated: it is " + ("answered" if current is not None and not current.open else "already the operator's"))
        return f"request {ask.short_id} went to the operator (urgent); their answer reaches the requester directly"
    if ask.kind == "permission" and allow is None:
        raise Refused("a permission is answered with allow=true or allow=false, or escalate=true")
    if ask.kind != "permission" and not ((text or "").strip() or selected):
        raise Refused("a question is answered with text or selected options, or escalate=true")
    try:
        outcome = await team.answer(ask.id, allow=allow, text=text, selected=selected, by="orchestrator", basis=basis, via="orchestrator")
    except StaffError as exc:
        raise Refused(str(exc)) from exc
    if outcome["state"] == "suggested":
        return f"in {project.name} the operator answers questions: your answer went to them as a suggestion on {ask.short_id}"
    what = ("granted" if allow else "denied") if ask.kind == "permission" else "answered"
    line = f"request {ask.short_id} {what}"
    if not outcome.get("delivered"):
        line += f", but it could not be delivered: {outcome.get('error') or 'unknown reason'}"
    return line


# -- control -----------------------------------------------------------------------------------------------------


async def interrupt(orch: Orchestrators, project: Project, session_id: str, *, staff: str) -> str:
    member = await _member(orch, project, staff)
    try:
        async with asyncio.timeout(RUNTIME_TIMEOUT_SECONDS):
            await _team(orch).interrupt(member)
    except StaffError as exc:
        raise Refused(str(exc)) from exc
    except TimeoutError as exc:
        raise Refused(f"{member.name}'s runtime did not stop within {int(RUNTIME_TIMEOUT_SECONDS)} s") from exc
    await _journal(orch, project, "control", f"The orchestrator interrupted {member.name}'s turn.", {"staff_id": member.id})
    return f"{member.name}'s turn is stopped; the session stays, and Tell gives them what to do next"


async def pause(orch: Orchestrators, project: Project, session_id: str, *, staff: str) -> str:
    member = await _member(orch, project, staff)
    try:
        outcome = await _team(orch).pause(member)
    except StaffError as exc:
        raise Refused(str(exc)) from exc
    await _journal(orch, project, "control", f"The orchestrator paused {member.name}.", {"staff_id": member.id})
    if outcome.get("paused"):
        commit = outcome.get("commit")
        return f"{member.name} is paused" + (f"; work in progress committed as {str(commit)[:10]}" if commit else "") + "; Tell or Assign resumes them"
    return f"{member.name} pauses when the current turn ends; work in progress is then committed"


async def release(orch: Orchestrators, project: Project, session_id: str, *, staff: str, keep_worktree: bool = True) -> str:
    member = await _member(orch, project, staff)
    ended = await _team(orch).release(member, keep_worktree=keep_worktree, reason="released by the orchestrator", by="orchestrator")
    if not ended:
        raise Refused(f"{member.name} has no live session")
    await _journal(orch, project, "control", f"The orchestrator released {member.name}" + ("" if keep_worktree else " and asked for the worktree to go") + ".", {"staff_id": member.id})
    return f"{member.name}'s session ended; an unfinished task went back to todo, and the branch stays"


# -- the command-line agents ---------------------------------------------------------------------------------------


async def harnesses(orch: Orchestrators, project: Project, session_id: str, *, harness: str | None = None, env: str | None = None, folder: str | None = None) -> str:
    """What each executor can do here, read from the harness catalog, with whether a runtime is there
    to start it — the two things a hire depends on."""
    manager: Any = orch.app.extensions.get("harness")
    team = orch.team
    runtimes = set(team.runtimes) if team is not None else set()
    target = _folder(project, folder) if folder else None
    environments = [env] if env else sorted({f.env for f in project.folders} or {orch.manager.projects.local_env})
    lines: list[str] = []
    if harness is None:
        lines.append("Daedalus: " + ("ready" if "daedalus" in runtimes else "no runtime") + f", in the {orch.manager.projects.local_env}")
        if manager is None:
            lines.append("the command-line agents are not set up on this installation")
            return "\n".join(lines)
        for where in environments:
            for entry in await manager.harnesses(where):
                name = str(entry.get("harness"))
                state = [f"{entry.get('label') or _label(name)} in the {where}:"]
                state.append(f"installed {entry.get('installed_version')}" if entry.get("installed") else "not installed")
                if entry.get("installed"):
                    state.append(f"signed in: {entry.get('logged_in')}")
                    if not entry.get("tested"):
                        state.append("outside the tested versions")
                if entry.get("unavailable"):
                    state.append(f"cannot run staff: {entry['unavailable']}")
                if name not in runtimes:
                    state.append("no staff runtime here yet")
                lines.append(" ".join(state))
        return "\n".join(lines)
    name = harness.strip().lower()
    if name not in HARNESSES:
        raise Refused(f"harness is one of {', '.join(HARNESSES)}")
    if name == "daedalus":
        presets = sorted(orch.manager.config.presets)
        return "\n".join([
            "Daedalus: " + ("ready" if "daedalus" in runtimes else "no runtime"),
            f"models (presets): {', '.join(presets) or 'the default only'}",
            f"agents (personas): {', '.join(orch.manager.staff.personas()) or 'none'}",
        ])
    if manager is None:
        raise Refused("the command-line agents are not set up on this installation")
    catalog_of = getattr(manager, "catalog", None)
    for where in environments:
        caps = manager.capabilities(name)
        lines.append(f"{caps.label} in the {where}: steer {caps.steer}, status from {caps.status_channel_label}" + ("" if name in runtimes else "; no staff runtime here yet"))
        if catalog_of is None:
            found: dict[str, Any] = next((e for e in await manager.harnesses(where) if e.get("harness") == name), {})
            lines.append(f"  models: {', '.join(found.get('models') or []) or 'unknown'}")
            lines.append(f"  agents: {', '.join(str(a.get('name')) for a in found.get('agents') or [] if isinstance(a, dict)) or 'none known'}")
            continue
        catalog = await catalog_of(where, name, target.id if target is not None and target.env == where else None)
        lines.append(f"  models: {', '.join(catalog.models) or 'unknown'}")
        lines.append(f"  agents: {', '.join(f'{a.name} ({a.source})' for a in catalog.agents) or 'none known'}")
        if catalog.modes:
            lines.append(f"  permission modes: {', '.join(catalog.modes)}")
        if catalog.efforts:
            lines.append(f"  efforts: {', '.join(catalog.efforts)}")
    return "\n".join(lines)


OPS: dict[str, Callable[..., Awaitable[str]]] = {
    "hire": hire,
    "staff_edit": staff_edit,
    "dismiss": dismiss,
    "assign": assign,
    "tell": tell,
    "read_staff": read_staff,
    "answer": answer,
    "interrupt": interrupt,
    "pause": pause,
    "release": release,
    "harnesses": harnesses,
}

__all__ = ["CONTRACT_MIN", "OPS"]
