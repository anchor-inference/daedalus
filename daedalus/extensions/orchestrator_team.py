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
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from daedalus.extensions.board_commands import BoardCommands
from daedalus.extensions.likeness import same_card
from daedalus.extensions.orchestrator_contract import bound, narrowing_refusal, source_of
from daedalus.extensions.orchestrator_ops import Refused, _folder, board_principal
from daedalus.extensions.task_contract import (
    CHECK_MAX_CHARS,
    CHECKS_MAX,
    REQUIREMENT_KINDS,
    REQUIREMENTS_MAX,
    split_checks,
)
from daedalus.harness.capabilities import MODE_MEANINGS, RESTRICTIVE_MODES
from daedalus.host.events import EventFilter
from daedalus.staff_runtime import LiveSession, ReadRequest
from daedalus.stores.control import ControlStore, Principal, Scope
from daedalus.stores.files import FileRefused, StoredFile
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.projects import Project, ProjectFolder
from daedalus.stores.staff import (
    DAEDALUS_EFFORTS,
    HARNESS_NAMES,
    HARNESSES,
    ISOLATIONS,
    MESSAGE_MODES,
    Staff,
    StaffBusy,
    StaffError,
)

if TYPE_CHECKING:
    from daedalus.extensions.orchestrator import Orchestrators

logger = logging.getLogger(__name__)

HANDED_IN = ("review", "done", "dropped")
"""A task whose member is finished with it: another Assign of it opens its next round."""
FOLLOW_UP_WINDOW = timedelta(hours=2)
"""How soon after a member handed a card in a new assignment with nearly its title is taken for a
round of it that forgot its task_id."""
SAME_TITLE_MIN = 8
"""The least a title may be, normalised, for another to read as the same work by starting with it:
"Fix" begins too many unrelated titles to mean anything."""
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
    member = await orch.manager.staff.find(project.id, (ref or "").strip(), dismissed=not active)
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


BUILTIN_AGENTS: dict[str, tuple[str, ...]] = {"claude": ("general-purpose", "Explore", "Plan", "claude-code-guide", "statusline-setup")}
"""Agents a CLI brings with it, which its catalog of agent files does not list."""


def _agent_name(agent: str) -> str:
    """"default" is what a model writes when it means the CLI's own default, which is no agent at all."""
    agent = (agent or "").strip()
    return "" if agent.lower() == "default" else agent


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
    agent = fields.get("agent", "")
    if agent and agent not in offered["agent"] and agent not in BUILTIN_AGENTS.get(harness, ()):
        # An empty list of agents used to let any name through, and the CLI then refused it at every
        # launch: a member hired with agent "default" for Claude Code died four times in a row before
        # its first turn. The name is checked against what the CLI really has, built in or defined.
        shown = ", ".join([*BUILTIN_AGENTS.get(harness, ()), *offered["agent"]][:20]) or "none"
        raise Refused(f"{_label(harness)} offers no agent {agent!r}; leave agent empty for its own default (agents here: {shown})")
    for name, value in fields.items():
        if name == "agent":
            continue
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
    agent = _agent_name(agent)
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
        # A Daedalus member works where its folder is: the host's own through the host terminal daemon.
        where = env or (target.env if target is not None else (project.primary.env if project.primary else local))
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
    if member.harness != "daedalus":
        prior = [row for row in await team.resume_sessions(member) if row["owner_name"].casefold() == member.name.casefold()][:5]
        if prior:
            shown = "; ".join(f"{row['id']} ({row['task_title'] or row['started_at'][:16]})" for row in prior)
            told += f". Earlier conversations of {member.name} in this launch folder: {shown}. StaffSessions(staff={member.name!r}) pages through all of them; pass task_id for worktree eligibility"
    if member.one_off:
        told += await _who_made_it(orch, project, member, target)
    if member.permission_mode and member.permission_mode in MODE_MEANINGS.get(member.harness, {}):
        told += f". Its mode {member.permission_mode}: {MODE_MEANINGS[member.harness][member.permission_mode]}"
        if member.permission_mode in RESTRICTIVE_MODES.get(member.harness, frozenset()):
            told += " — work the operator allowed to change or fix things needs another mode (StaffEdit)"
    warn_of = getattr(orch.app.extensions.get("harness"), "hire_warning", None)
    warning = str(await warn_of(where, harness) or "") if harness != "daedalus" and warn_of is not None else ""
    return f"{told}. Warning: {warning}" if warning else told


async def _who_made_it(orch: Orchestrators, project: Project, hired: Staff, folder: ProjectFolder | None) -> str:
    """A hint when a one-off helper is hired while a member of the team who worked recently in the same
    folder is free: rework of a thing is best done by who made it. Never a refusal — the helper may be
    for something else entirely."""
    where = folder or project.primary
    since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    rows = await orch.manager.db.fetchall(
        "SELECT m.id, m.name, t.id AS task_id, t.title FROM staff_sessions s JOIN staff m ON m.id = s.staff_id JOIN board_tasks t ON t.id = s.task_id"
        " WHERE m.project_id = ? AND m.archived_at IS NULL AND m.one_off = 0 AND m.id != ? AND s.started_at >= ? AND (t.folder_id = ? OR t.folder_id IS NULL)"
        " ORDER BY s.started_at DESC",
        (project.id, hired.id, since, where.id if where is not None else ""),
    )
    live = await orch.manager.staff.live_sessions(project.id)
    seen: set[str] = set()
    hints: list[str] = []
    for row in rows:
        session = live.get(row["id"])
        if row["id"] in seen or (session is not None and session.status not in ("idle", "turn_done_unseen")):
            continue
        seen.add(row["id"])
        hints.append(f"{row['name']} is free and worked on \"{row['title']}\" ({row['task_id']})")
        if len(hints) == 2:
            break
    if not hints:
        return ""
    return f". Before you give {hired.name} rework of something already made: {'; '.join(hints)} — Assign(task_id=…) without staff gives a card back to who made it"


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
    if agent is not None:
        agent = _agent_name(agent)
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


async def _release_authority(orch: Orchestrators, project: Project, session_id: str,
                             task_id: str | None, *, physical: bool) -> tuple[Principal, Callable[[], Awaitable[None]]]:
    principal = await board_principal(orch, session_id, project.id, "staff.release", task_id)

    async def check() -> None:
        await orch.current(session_id)
        async with orch.manager.db.transaction() as conn:
            await ControlStore(orch.manager.db).authorize(
                conn, principal, Scope("project", project.id), "staff.release", task_id=task_id,
                effects=("execution.stop",) if physical else (),
            )

    await check()
    return principal, check


async def _free_cards(orch: Orchestrators, project: Project, session_id: str,
                      member: Staff, why: str) -> list[str]:
    """Put back to todo the cards a member without a live session still holds in doing; their ids.

    Such a card is nobody's work, and it cannot be released the usual way, which ends a session."""
    team = _team(orch)
    if await orch.manager.staff.live(member.id) is not None:
        return []
    rows = await orch.manager.db.fetchall("SELECT id FROM board_tasks WHERE assignee_staff_id = ? AND status = 'doing' ORDER BY updated_at", (member.id,))
    freed = []
    for row in rows:
        principal, check = await _release_authority(orch, project, session_id, row["id"], physical=False)
        await check()
        if await team.free_card(member, row["id"], why, by="orchestrator", principal=principal) is not None:
            freed.append(row["id"])
    return freed


def _cards(ids: list[str]) -> str:
    return f"card{'s' if len(ids) > 1 else ''} {', '.join(ids)}"


async def dismiss(orch: Orchestrators, project: Project, session_id: str, *, staff: str, release: bool = False, keep_worktree: bool = True) -> str:
    member = await _member(orch, project, staff, active=False)
    if not member.active:
        # A one-off helper leaves the team with its task, and the operator may dismiss anyone from
        # the app: the orchestrator is told so, and what the member left in doing is freed.
        freed = await _free_cards(orch, project, session_id, member, f"{member.name} was dismissed")
        if freed:
            await _journal(orch, project, "control", f"The orchestrator freed {_cards(freed)} left in doing by {member.name}, who had been dismissed.", {"staff_id": member.id})
        return f"{member.name} was already dismissed" + (f"; the {_cards(freed)} they left in doing went back to todo, unassigned" if freed else "")
    live = await orch.manager.staff.live(member.id)
    released = ""
    if live is not None:
        if not release:
            raise Refused(f"{member.name} has a live session ({live.status.replace('_', ' ')}); Dismiss(release=true) ends it first, or Release them and dismiss later")
        principal, check = await _release_authority(orch, project, session_id, live.task_id, physical=True)
        await _team(orch).release(member, keep_worktree=keep_worktree,
                                  reason="dismissed by the orchestrator", by="orchestrator",
                                  principal=principal, check_authority=check)
        released = "; their session was ended" + ("" if keep_worktree else " and the worktree removed if it was clean")
    try:
        archived = await orch.manager.staff.archive(member.id, by="orchestrator")
    except StaffBusy as exc:
        raise Refused(str(exc)) from exc
    await _changed(orch, archived, "staff.dismissed")
    await orch._changed(project.id, "journal", "orchestrator")
    return f"dismissed {archived.name}{released}; their branch and history stay"


# -- handing out work ----------------------------------------------------------------------------------------


MODEL_NAME_MIN = 5
"""The least a model id may be to be looked for in a brief: shorter ones ("o3") match ordinary words."""


async def known_models(orch: Orchestrators, project: Project) -> dict[str, set[str]]:
    """Every model something here can run, by id (lower case), with the executors that run it: the
    Daedalus presets' models and each installed command-line agent's listed models, in the project's
    environments."""
    known: dict[str, set[str]] = {}
    for preset in orch.manager.config.presets.values():
        if preset.model:
            known.setdefault(preset.model.lower(), set()).add("daedalus")
    manager: Any = orch.app.extensions.get("harness")
    if manager is None:
        return known
    for env in sorted({f.env for f in project.folders} or {orch.manager.projects.local_env}):
        try:
            entries = await manager.harnesses(env)
        except Exception:  # noqa: BLE001 — a catalog that cannot be read names no models
            logger.warning("could not read the harness catalog of the %s", env, exc_info=True)
            continue
        for entry in entries:
            if entry.get("installed"):
                for model in entry.get("all_models") or entry.get("models") or []:
                    known.setdefault(str(model).lower(), set()).add(str(entry.get("harness")))
    return known


def runs_model(orch: Orchestrators, member: Staff) -> str:
    """The model a member runs, as far as the host knows: a Daedalus member's preset's model, a
    command-line member's chosen model, or "" for a CLI left on its own default."""
    if member.harness == "daedalus":
        config = orch.manager.config
        preset = config.presets.get(member.model or config.model.preset) or next(iter(config.presets.values()), None)
        return preset.model.lower() if preset is not None and preset.model else ""
    return (member.model or "").lower()


async def other_models(orch: Orchestrators, project: Project, member: Staff, text: str) -> list[str]:
    """The models a text names that ``member`` does not run. A command-line member left on its CLI's
    default may run any model that CLI lists, so only a model outside that list counts against it."""
    lowered = (text or "").lower()
    if not lowered.strip():
        return []
    known = await known_models(orch, project)
    named = [m for m in known if len(m) >= MODEL_NAME_MIN and re.search(rf"(?<![\w.-]){re.escape(m)}(?![\w.-])", lowered)]
    if not named:
        return []
    own = runs_model(orch, member)
    if own:
        return [] if own in named else named
    return [m for m in named if member.harness not in known.get(m, set())]


def _executors(known: dict[str, set[str]], model: str) -> str:
    return ", ".join(sorted(_label(h) for h in known.get(model, set()))) or "nothing here"


async def assign(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    staff: str | None = None,
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
    new: bool = False,
    requirements: list[Any] | None = None,
    inputs: list[Any] | None = None,
    checks: list[str] | None = None,
    reason: str = "",
    resume_from: str | None = None,
    client_operation_id: str = "",
    expected_entity_revision: int | None = None,
    expected_collection_revision: int | None = None,
    effort: str | None = None,
) -> str:
    team = _team(orch)
    board = orch.board
    if board is None:
        raise Refused("the board is not available on this installation")
    if not client_operation_id:
        raise Refused("Assign requires a trusted tool call id")
    handed = await _files(orch, project, session_id, files)
    wanted_inputs = await _inputs(orch, project, session_id, inputs)
    wanted_requirements = await _requirements(orch, project, session_id, requirements)
    wanted_checks = _checks(checks)
    given = {k: v.strip() for k, v in (("objective", objective), ("deliverable", deliverable), ("boundaries", boundaries), ("done_when", done_when)) if v is not None and v.strip()}
    target = _folder(project, folder) if folder else None
    wanted = " ".join((title or "").split())
    existing: dict[str, Any] | None = None
    previous: dict[str, Any] | None = None
    owner: Staff | None = None
    if task_id:
        if task_id in (depends_on or []):
            # The board's own refusal, "a task cannot depend on itself, even through another task",
            # left the orchestrator guessing which task it meant: it had given the card it was
            # assigning as the one the reviewer should wait for.
            raise Refused(
                f"task {task_id} cannot wait for itself: depends_on names the task being assigned. For work that follows it, "
                f"create a task of its own (a title and a brief, no task_id) with depends_on=['{task_id}']"
            )
        try:
            existing = await board.get(task_id, actor=session_id)
        except KeyError as exc:
            raise Refused(f"no task {task_id} on {project.name}'s board; Tasks() lists them") from exc
        # Repeating the saved brief while naming a card must not turn a first assignment
        # into an edit that requires project-wide planning authority.
        given = {key: value for key, value in given.items()
                 if value != str(existing.get("brief", {}).get(key) or "").strip()}
        if existing["status"] in HANDED_IN and existing.get("branch"):
            raise Refused(
                f"task {task_id} has work on branch {existing['branch']}, which the operator reviews and merges; "
                "for more work on it, create a new task"
            )
        owner = await previous_owner(orch, existing["id"])
    elif not wanted:
        raise Refused("Assign needs a task_id, or a title and the four parts of a brief for a new task")
    queued = await orch.manager.staff.get(existing["assignee_staff_id"]) if existing is not None and existing.get("assignee_staff_id") else None
    if staff:
        member = await _member(orch, project, staff)
    elif owner is not None and owner.active:
        member = owner
    elif owner is None and queued is not None and queued.active:
        member = queued  # assigned and waiting to start: it is still theirs
    elif existing is not None:
        gone = f"{owner.name}, who worked it, has left the team" if owner is not None else "nobody has worked it yet"
        raise Refused(f"task {existing['id']}: {gone}; name who takes it (staff=…)")
    else:
        raise Refused("Assign needs staff: who takes the new task")
    if effort is not None and (member.harness != "daedalus" or effort not in DAEDALUS_EFFORTS[1:]):
        raise Refused("an assignment's effort is a Daedalus effort: off, low, medium, high or xhigh")
    handover = ""
    at_work = existing is not None and existing["status"] == "doing"
    if owner is not None and owner.active and not owner.one_off and owner.id != member.id and not at_work:
        # A one-off helper was hired for an errand, not for the thing it made: its work may go to anyone.
        # A card in doing is its worker's until it is released, which the team says below.
        why = " ".join((reason or "").split())
        if len(why) < 8:
            # A helper was hired for two small fixes to a video while the member who had made it sat
            # free; it died at its start, and it took nine failed calls to give the work back to her.
            raise Refused(
                f"task {existing['id']} is {owner.name}'s work, and rework goes to whoever made it: Assign(task_id='{existing['id']}') "  # type: ignore[index]
                f"without staff gives it to {owner.name}. To hand it to {member.name} instead, say why in reason (a sentence; the card and the journal keep it)"
            )
        handover = why
    merged = {k: str((existing or {}).get("brief", {}).get(k) or "").strip() for k in CONTRACT_FIELDS}
    merged.update(given)
    # The model preflight must read the whole effective brief even when repeated fields
    # are not edits. A saved model request cannot disappear with a task-scoped assignment.
    brief_text = " ".join([*merged.values(), wanted, *(text for text, _, _, _ in wanted_requirements)])
    await _grants_kept(orch, existing, wanted_requirements)
    await _able(orch, member, existing, wanted_requirements, reason)
    mismatch = await other_models(orch, project, member, brief_text)
    if mismatch and len(" ".join((reason or "").split())) < 8:
        # Asked for a named model at a named effort to check a mail service, the orchestrator told a
        # member on another CLI to "raise a one-off reviewer on that model if available": a member
        # cannot hire, and the work went to the model nobody had asked for.
        known = await known_models(orch, project)
        model = mismatch[0]
        runs = runs_model(orch, member) or f"{_label(member.harness)}'s default model"
        raise Refused(
            f"the brief names {model}, and {member.name} runs {runs}: work the operator wants done by {model} goes to a member that runs it "
            f"({_executors(known, model)} can) — Hire(harness=…, model='{model}', effort=…, one_off=true), or StaffEdit a member to it; a member "
            f"cannot hire one. If {member.name} should do it anyway, say why in reason"
        )
    if existing is None:
        previous = await _last_handed_in(orch, project, member, session_id)
        if previous is not None and not new and _same_work(wanted, previous["title"]):
            raise Refused(
                f"{member.name} handed in {previous['id']} \"{previous['title']}\" {_minutes_ago(previous)}, and \"{wanted}\" reads as the same work. "
                f"For its next round, Assign(task_id='{previous['id']}', …) reopens that card and keeps its history; for separate work, repeat this with new=true"
            )
    # Checked before anything is written, so a refused hand-over leaves no half-briefed task behind.
    if existing is None:
        await refuse_twin(orch, project, wanted, merged["objective"], new=new, reason=reason)
    short = [k.replace("_", "-") for k in CONTRACT_FIELDS if len(merged[k]) < CONTRACT_MIN]
    if short:
        raise Refused(
            f"the brief has no usable {', '.join(short)} (each part at least {CONTRACT_MIN} characters); "
            "a task is handed over with its objective, deliverable, boundaries and done_when"
        )
    renamed = wanted if existing is not None and wanted and wanted != existing["title"] else None
    reopened = existing is not None and existing["status"] in HANDED_IN
    if reopened:
        raise Refused("return the exact reviewed result before assigning another round")
    if existing is not None and renamed is not None and not reopened and not _same_work(renamed, existing["title"]) and len(" ".join((reason or "").split())) < 8:
        # A card a member was at work on was renamed into an unrelated check of a mail service by an
        # Assign that named it: the work it held left the board half done, under another title, and
        # the member was switched to the new work in the same session.
        holder = await orch.manager.staff.get(existing["assignee_staff_id"]) if existing.get("assignee_staff_id") else None
        at = f", and {holder.name} is at work on it" if holder is not None and existing["status"] == "doing" else ", and it is not handed in"
        raise Refused(
            f"task {existing['id']} is \"{existing['title']}\"{at}: \"{renamed}\" is other work, which is a card of its own — Assign without "
            "task_id. If it is the same work under a better title, say so in reason"
        )
    if existing is not None:
        ownership = await orch.manager.db.fetchone(
            "SELECT t.current_attempt_id,"
            " EXISTS(SELECT 1 FROM runtime_exit_observations e"
            " WHERE e.attempt_id = t.current_attempt_id) AS exited,"
            " EXISTS(SELECT 1 FROM effect_outbox o WHERE o.kind = 'task.launch'"
            " AND o.state IN ('pending','claimed','unknown')"
            " AND json_extract(o.payload_json,'$.control.task_id') = t.id) AS pending_launch"
            " FROM board_tasks t WHERE t.id = ?", (existing["id"],))
        if ownership is not None and ((ownership["current_attempt_id"] and not ownership["exited"])
                                      or ownership["pending_launch"]):
            # An edit followed by a refused launch used to leave the old worker running under
            # a newly written title and brief, with no command tying them to the same attempt.
            raise Refused("stop or reconcile the current execution before reassigning or changing and relaunching this task")
        reopen_after_exit = bool(ownership is not None and ownership["current_attempt_id"]
                                 and ownership["exited"] and existing["status"] == "doing")
    try:
        commands = BoardCommands(orch.manager.db, bus=orch.manager.bus)
        scope = Scope("project", project.id)
        handoff_requirements = [{"text": text, "kind": kind, "source": source}
                                for text, kind, source, _ in wanted_requirements]
        handoff_requirements.extend({"text": why or f"{stored.name}: the work starts from it",
                                     "kind": "input", "source": "operator" if stored.origin == "operator" else "orchestrator",
                                     "file_id": stored.id} for stored, why in wanted_inputs)
        handed = handed + [stored for stored, _ in wanted_inputs if stored.id not in {file.id for file in handed}]
        file_ids = [stored.id for stored in handed]
        if existing is None:
            if expected_collection_revision is None:
                raise Refused("new assignments need expected_collection_revision from Tasks(list)")
            principal = await board_principal(orch, session_id, project.id, "board.task.create")
            created = await commands.create(principal, scope, client_operation_id=client_operation_id,
                                            expected_collection_revision=expected_collection_revision,
                                            title=wanted, brief=merged, checklist=wanted_checks or split_checks(merged["done_when"]),
                                            depends_on=depends_on, priority=int(priority or 3),
                                            source_session_id=session_id, assignee_staff_id=member.id,
                                            folder_id=target.id if target else None,
                                            file_ids=file_ids, requirements=handoff_requirements)
            task = await board.get(created["task_id"], actor=session_id)
        else:
            task = existing
            if expected_entity_revision is None:
                raise Refused("existing assignments need expected_entity_revision from Tasks(get)")
            unchanged = (task["status"] == "todo" and task["assignee_staff_id"] == member.id
                         and not renamed and not given and priority is None and depends_on is None
                         and folder is None and files is None and requirements is None and inputs is None
                         and checks is None and not handover and not reopen_after_exit)
            # A card a worker left behind still names its attempt; only the update path may hand it on.
            first_assignment = (task["status"] == "todo" and not task["assignee_staff_id"]
                                and not task.get("current_attempt_id") and not renamed and not given and priority is None and depends_on is None
                                and folder is None and files is None and requirements is None and inputs is None
                                and checks is None and not handover and not reopen_after_exit)
            if first_assignment:
                try:
                    principal = await board_principal(orch, session_id, project.id, "board.task.assign", task["id"])
                except Refused:
                    first_assignment = False
            if first_assignment:
                changed = await commands.assign(principal, scope, task["id"], client_operation_id=client_operation_id,
                                                expected_entity_revision=expected_entity_revision, staff_id=member.id)
                task = await board.get(changed["task_id"], actor=session_id)
            elif not unchanged:
                # Requiring a planning grant to launch an already assigned card made task-scoped
                # execution grants unusable, even though the card and its files were untouched.
                principal = await board_principal(orch, session_id, project.id, "board.task.update", task["id"])
                changed = await commands.update(principal, scope, task["id"], client_operation_id=client_operation_id,
                                                expected_entity_revision=expected_entity_revision,
                                                title=renamed, brief=given or None, depends_on=depends_on, priority=priority,
                                                status="todo" if reopen_after_exit else None,
                                                checklist=wanted_checks or (split_checks(merged["done_when"])
                                                                              if "done_when" in given else None),
                                                note=(f"reassigned from {owner.name}: {handover}" if handover and owner else ""),
                                                reassignment_reason=handover or None,
                                                assignee_staff_id=member.id, folder_id=target.id if target else None,
                                                file_ids=file_ids, requirements=handoff_requirements)
                task = await board.get(changed["task_id"], actor=session_id)
    except KeyError as exc:
        raise Refused(f"no task {exc.args[0] if exc.args else ''} on {project.name}'s board") from exc
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    added = f"{len(handoff_requirements)} requirements are on the card." if handoff_requirements else ""
    try:
        principal = await board_principal(orch, session_id, project.id, "task.launch", task["id"])
        launched = await team.assign(member, task["id"], principal=principal,
                                     client_operation_id=client_operation_id + ":launch",
                                     expected_entity_revision=task["entity_revision"],
                                     resume_from=resume_from, effort=effort)
    except KeyError as exc:
        raise Refused(f"no task {task['id']} on {project.name}'s board") from exc
    except StaffError as exc:
        raise Refused(f"task {task['id']} stays on the board, unstarted: {exc}") from exc
    await orch.loops.close(project.id, task_id=task["id"], by="orchestrator", decision=f"assigned to {member.name}" + (" for another round" if reopened else ""))
    for waited in depends_on or []:
        await orch.loops.close(project.id, task_id=waited, by="orchestrator", decision=f"task {task['id']} follows it")
    notes: list[str] = []
    if renamed is not None and existing is not None:
        # Said out loud: a card renamed in passing is a card whose earlier work leaves the board
        # under another name, and nobody who reads the board afterwards can tell.
        notes.append(f"The card was renamed: \"{existing['title']}\" → \"{renamed}\"; its earlier rounds stay in its notes.")
    if previous is not None:
        notes.append(
            f"This is a card of its own. {member.name}'s previous card {previous['id']} \"{previous['title']}\" was handed in {_minutes_ago(previous)}; "
            f"if this work is its next round, drop {task['id']} and Assign(task_id='{previous['id']}') instead."
        )
    if added:
        notes.append(added)
    tail = "".join(" " + note for note in notes)
    admission = await OutboxStore(orch.manager.db).view(launched["effect_id"])
    waiting = (f" queue position {admission['wait_position']} ({admission['wait_reason']})"
               if admission.get("wait_position") is not None else " admission in progress")
    files_wait = " The files are copied to them when they start." if handed else ""
    return (f"{member.name} will start {task['id']} \"{task['title']}\" after admitted;"
            f" effect {launched['effect_id']}: {admission['state']},{waiting}.{files_wait}" + tail)


TWIN_WINDOW = timedelta(hours=6)
"""How long a finished card still counts as the place for its work: a new card for it within this
window is the same work opened twice."""


async def refuse_twin(orch: Orchestrators, project: Project, title: str, objective: str, *, new: bool, reason: str) -> None:
    """Refuse a new card for work already on the board: open, or finished within ``TWIN_WINDOW``.

    A read-only audit of a mail service was opened a second time, under a new title, when the
    orchestrator gave the work to a newly hired member instead of handing the card over; a
    translation of the skills had two cards, one of them dropped later. ``new=true`` with a reason
    says it is separate work after all; a card the member just handed in needs no reason (a round of
    it is refused on its own, see ``_same_work``)."""
    since = (datetime.now(UTC) - TWIN_WINDOW).isoformat()
    rows = await orch.manager.db.fetchall(
        "SELECT t.id, t.title, t.status, t.brief_json, m.name FROM board_tasks t LEFT JOIN staff m ON m.id = t.assignee_staff_id"
        " WHERE t.project_id = ? AND (t.status IN ('todo', 'doing', 'blocked', 'review') OR (t.status IN ('done', 'dropped') AND t.updated_at >= ?))"
        " ORDER BY t.updated_at DESC",
        (project.id, since),
    )
    for row in rows:
        try:
            other = str(json.loads(row["brief_json"] or "{}").get("objective") or "")
        except ValueError:
            other = ""
        if not same_card(title, row["title"], objective, other):
            continue
        if new and (len(" ".join((reason or "").split())) >= 8 or row["status"] in HANDED_IN):
            continue
        holder = f", {row['name']}" if row["name"] else ""
        raise Refused(
            f"\"{title}\" is work already on the board: {row['id']} \"{row['title']}\" ({row['status']}{holder}). For the same work, "
            f"Assign(task_id='{row['id']}', …) hands that card on — to someone other than who worked it, with a reason; for separate work, "
            "repeat this with new=true and a reason saying how it differs"
        )


async def previous_owner(orch: Orchestrators, task_id: str) -> Staff | None:
    """Who last worked a card: the member of its latest session. An assignee that never started on it
    is not; the work is the previous owner's only once they did some."""
    row = await orch.manager.db.fetchone("SELECT staff_id FROM staff_sessions WHERE task_id = ? ORDER BY started_at DESC LIMIT 1", (task_id,))
    return await orch.manager.staff.get(row["staff_id"]) if row is not None else None


async def _inputs(orch: Orchestrators, project: Project, session_id: str, raw: list[Any] | None) -> list[tuple[StoredFile, str]]:
    """The files a hand-over names as what the work starts from, with what each is for."""
    wanted: list[tuple[str, str]] = []
    for entry in raw or []:
        if isinstance(entry, dict):
            ref, why = str(entry.get("file") or entry.get("handle") or "").strip(), str(entry.get("text") or entry.get("why") or "").strip()
        else:
            ref, why = str(entry or "").strip(), ""
        if ref:
            wanted.append((ref, why))
    if not wanted:
        return []
    found = await _files(orch, project, session_id, [ref for ref, _ in wanted])
    return [(stored, why) for stored, (_, why) in zip(found, wanted, strict=True)]


async def _requirements(orch: Orchestrators, project: Project, session_id: str, raw: list[Any] | None) -> list[tuple[str, str, str, str]]:
    """``(text, kind, source, why)`` of each requirement a hand-over carries, checked before anything
    is written. A bare string is the operator's condition, which is what a requirement usually is."""
    out: list[tuple[str, str, str, str]] = []
    for entry in raw or []:
        if isinstance(entry, dict):
            text, kind, source = str(entry.get("text") or "").strip(), str(entry.get("kind") or "quality").strip().lower(), entry.get("source")
            why = str(entry.get("why") or "").strip()
        else:
            text, kind, source, why = str(entry or "").strip(), "quality", "operator", ""
        if not text:
            continue
        if kind == "input":
            raise Refused("an input requirement names its file: put it in inputs=[…], not requirements")
        if kind not in REQUIREMENT_KINDS:
            raise Refused(f"a requirement's kind is one of {', '.join(k for k in REQUIREMENT_KINDS if k != 'input')}")
        out.append((text, kind, await bound(orch, session_id, await source_of(orch, project, str(source) if source else "operator")), why))
    if len(out) > REQUIREMENTS_MAX:
        raise Refused(f"at most {REQUIREMENTS_MAX} requirements on a card; merge some")
    return out


async def _grants_kept(orch: Orchestrators, existing: dict[str, Any] | None, wanted: list[tuple[str, str, str, str]]) -> None:
    """Refuse, before anything is written, a condition of the orchestrator's that narrows what the
    operator allowed for the work without saying why (see ``orchestrator_contract.narrowed``)."""
    grants = [r.text for r in await _team(orch).contracts.requirements(existing["id"]) if r.kind == "scope" and r.from_operator] if existing is not None else []
    grants += [text for text, kind, source, _ in wanted if kind == "scope" and source != "orchestrator"]
    for _, kind, source, why in wanted:
        if grants and kind == "constraint" and source == "orchestrator" and len(" ".join(why.split())) < 8:
            raise Refused(narrowing_refusal(grants[0]))


def restricted(member: Staff) -> str:
    """What a member's permission mode keeps it from doing, when it keeps it from changing anything."""
    if member.permission_mode in RESTRICTIVE_MODES.get(member.harness, frozenset()):
        return f"{member.permission_mode}: {MODE_MEANINGS.get(member.harness, {}).get(member.permission_mode, 'it changes nothing')}"
    return ""


async def _able(orch: Orchestrators, member: Staff, existing: dict[str, Any] | None, wanted: list[tuple[str, str, str, str]], reason: str) -> None:
    """Refuse work the operator allowed to change things to a member whose mode changes nothing.

    A reviewer was hired read-only for a check the operator had said to fix as well; when the operator
    then allowed a test message, the reviewer could not send it — its sandbox had neither writes nor a
    network — and the orchestrator asked the operator to lift a restriction it had set itself."""
    limit = restricted(member)
    if not limit or len(" ".join((reason or "").split())) >= 8:
        return
    grants = [r.text for r in await _team(orch).contracts.requirements(existing["id"]) if r.kind == "scope" and r.from_operator] if existing is not None else []
    grants += [text for text, kind, source, _ in wanted if kind == "scope" and source != "orchestrator"]
    if grants:
        raise Refused(
            f"the operator allowed this work \"{grants[0][:120]}\", and {member.name} runs {limit}. StaffEdit(staff='{member.name}', "
            "permission_mode=…) to a mode that can do it — or say why in reason; never ask the operator to allow it again"
        )


def _checks(raw: list[str] | None) -> list[str]:
    items = [" ".join(str(c or "").split()) for c in raw or []]
    items = [c for c in items if c]
    if len(items) > CHECKS_MAX:
        raise Refused(f"at most {CHECKS_MAX} checks on a card; each is something anyone can see pass")
    if any(len(c) > CHECK_MAX_CHARS for c in items):
        raise Refused(f"a check is at most {CHECK_MAX_CHARS} characters")
    return items


async def _last_handed_in(orch: Orchestrators, project: Project, member: Staff, session_id: str) -> dict[str, Any] | None:
    """The card a member handed in last, when it is recent enough that a new assignment could be its
    next round: one the orchestrator wrote, without a branch, handed in within ``FOLLOW_UP_WINDOW``.

    It is never taken as the card of a new assignment. It once was, for any new work given to the
    same member within two hours: the presentation the operator had asked for was renamed "lessons
    from the videos, to the archive" and left the board, and another card had been five different
    pieces of work in turn. It is named instead, so a round that forgot its task_id is caught (see
    ``_same_work``) and any other assignment says which card it could have been.
    """
    row = await orch.manager.db.fetchone(
        "SELECT id, status, branch, origin_session_id, updated_at FROM board_tasks WHERE project_id = ? AND assignee_staff_id = ? AND status IN ('review', 'done', 'dropped') "
        "ORDER BY updated_at DESC LIMIT 1",
        (project.id, member.id),
    )
    if row is None or row["branch"] or not row["origin_session_id"]:
        return None
    try:
        handed_in = datetime.fromisoformat(row["updated_at"])
    except (TypeError, ValueError):
        return None
    handed_in = handed_in if handed_in.tzinfo else handed_in.replace(tzinfo=UTC)
    if datetime.now(UTC) - handed_in > FOLLOW_UP_WINDOW:
        return None
    try:
        return await orch.board.get(row["id"], actor=session_id)  # type: ignore[no-any-return]
    except KeyError:
        return None


def _normal_title(title: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", title.casefold()).split())


def _same_work(title: str, other: str) -> bool:
    """Whether two titles read as one piece of work: the same words, or one that begins with the
    other ("Tariff plan" and "Tariff plan, revision 2"). Nothing cleverer: a refusal made on a guess
    about meaning would stop work that is merely similar."""
    a, b = _normal_title(title), _normal_title(other)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= SAME_TITLE_MIN and longer.startswith(shorter + " ")


def _minutes_ago(task: dict[str, Any]) -> str:
    try:
        then = datetime.fromisoformat(str(task.get("updated_at") or ""))
    except ValueError:
        return "a moment ago"
    then = then if then.tzinfo else then.replace(tzinfo=UTC)
    minutes = int((datetime.now(UTC) - then).total_seconds() // 60)
    return "a moment ago" if minutes < 1 else f"{minutes} minute{'s' if minutes != 1 else ''} ago"


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
    live = await _team(orch).live_of(member)
    about = live.session.task_id if live is not None else None
    await orch.loops.close(
        project.id, task_id=about, staff_id=None if about else member.id, by="orchestrator", decision=f"told {member.name}: {' '.join(body.split())[:200]}",
        causes={"report_stuck", "report_needs_input"},
    )
    line = f"message {receipt['message_id']} to {member.name} ({when}): {receipt['state']}"
    named = await other_models(orch, project, member, body)
    if named:
        line += (
            f" — note: the message names {', '.join(named)}, which {member.name} does not run and cannot hire; if the operator wants "
            f"{named[0]} to do this, Hire it yourself (one_off=true) and Assign it the work"
        )
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


async def staff_sessions(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    staff: str,
    task_id: str | None = None,
    before: str | None = None,
    limit: int = 20,
) -> str:
    member = await _member(orch, project, staff)
    if member.harness == "daedalus":
        raise Refused("StaffSessions resumes command-line harness conversations; Daedalus has its own chat history")
    try:
        rows = await _team(orch).resume_sessions(member, task_id=task_id, before=before, limit=max(1, min(int(limit), 50)))
    except StaffError as exc:
        raise Refused(str(exc)) from exc
    if not rows:
        return f"No recorded {member.harness} conversations in {member.name}'s launch folder."
    lines = [f"{member.name}'s {member.harness} conversations in this launch folder (newest first):"]
    for row in rows:
        state = "ready to resume" if row["can_resume"] else "session is still live" if row["resume_reason"] == "live" else "different launch folder, worktree or branch"
        lines.append(f"- {row['id']} · {row['started_at'][:16]} · {row['owner_name']} · {row['task_title'] or row['task_id'] or 'untitled'} · {state}")
    lines.append(f"Next page: StaffSessions(staff={member.name!r}, before={rows[-1]['id']!r}, task_id={task_id!r})")
    if member.isolation == "worktree" and not task_id:
        lines.append("Give task_id to check which sessions match that task's worktree and branch.")
    lines.append("To continue one, use Assign(task_id=..., staff=..., resume_from=<session id>); no session is resumed implicitly.")
    return "\n".join(lines)


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
    if what == "screen" and not session.live:
        return f"{member.name}, session {session.id} (ended: {session.end_reason or session.status}): there is no screen of an ended session; what=\"last\" or \"reports\" shows what it left"
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
        if ask.staff_id:
            await orch.loops.close(project.id, task_id=ask.task_id, staff_id=None if ask.task_id else ask.staff_id, by="orchestrator", decision=f"request {ask.short_id} went to the operator")
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
    if ask.staff_id:
        await orch.loops.close(
            project.id, task_id=ask.task_id, staff_id=None if ask.task_id else ask.staff_id, by="orchestrator", decision=f"{what} request {ask.short_id}",
            causes={"report_stuck", "report_needs_input"},
        )
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
    member = await _member(orch, project, staff, active=False)
    live = await orch.manager.staff.live(member.id) if member.active else None
    ended = False
    if live is not None:
        principal, check = await _release_authority(orch, project, session_id, live.task_id, physical=True)
        ended = await _team(orch).release(
            member, keep_worktree=keep_worktree, reason="released by the orchestrator",
            by="orchestrator", principal=principal, check_authority=check,
        )
    if not ended:
        # Releasing is how a card is freed, so a member whose session is already over still frees
        # its cards: "no live session" once left a card in doing that nothing else could move.
        freed = await _free_cards(orch, project, session_id, member,
                                  "released by the orchestrator after the session had ended")
        if not freed:
            raise Refused(f"{member.name} has no live session and holds no card in doing")
        await _journal(orch, project, "control", f"The orchestrator freed {_cards(freed)} left in doing by {member.name}, whose session had ended.", {"staff_id": member.id})
        return f"{member.name} had no live session; the {_cards(freed)} they held in doing went back to todo, unassigned"
    await _journal(orch, project, "control", f"The orchestrator released {member.name}" + ("" if keep_worktree else " and asked for the worktree to go") + ".", {"staff_id": member.id})
    return f"{member.name}'s session ended; an unfinished task went back to todo, and the branch stays"


# -- the command-line agents ---------------------------------------------------------------------------------------


def _models_line(entry: dict[str, Any]) -> str:
    """The models a CLI offers. When the operator chose some, those are the ones to hire with and to
    call by name; the rest are counted, and said to be usable, so a model the operator asks for by
    name is never refused as unknown."""
    offered = [str(m) for m in entry.get("models") or []]
    if not entry.get("models_chosen"):
        return f"  models: {', '.join(offered) or 'unknown'}"
    others = len([m for m in entry.get("all_models") or [] if m not in offered])
    rest = f"; {others} other model{'' if others == 1 else 's'} of this CLI can still be named when asked for" if others else ""
    return f"  models the operator offers: {', '.join(offered)}{rest}"


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
        local = orch.manager.projects.local_env
        reach = f"in the {local}" + (" and, through the host terminal, in host folders" if local == "container" else "")
        lines.append("Daedalus: " + ("ready" if "daedalus" in runtimes else "no runtime") + f", {reach}; efforts off, low, medium, high, xhigh")
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
            "efforts: off, low, medium, high, xhigh; the preset is the default, Hire/StaffEdit sets a member default, Assign overrides one assignment",
        ])
    if manager is None:
        raise Refused("the command-line agents are not set up on this installation")
    catalog_of = getattr(manager, "catalog", None)
    for where in environments:
        caps = manager.capabilities(name)
        lines.append(f"{caps.label} in the {where}: steer {caps.steer}, status from {caps.status_channel_label}" + ("" if name in runtimes else "; no staff runtime here yet"))
        found: dict[str, Any] = next((e for e in await manager.harnesses(where) if e.get("harness") == name), {})
        lines.append(_models_line(found))
        if catalog_of is None:
            lines.append(f"  agents: {', '.join(str(a.get('name')) for a in found.get('agents') or [] if isinstance(a, dict)) or 'none known'}")
            continue
        catalog = await catalog_of(where, name, target.id if target is not None and target.env == where else None)
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
    "staff_sessions": staff_sessions,
    "answer": answer,
    "interrupt": interrupt,
    "pause": pause,
    "release": release,
    "harnesses": harnesses,
}

__all__ = ["CONTRACT_MIN", "OPS"]
