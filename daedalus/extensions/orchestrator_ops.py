"""What the orchestrator's project tools do: the brief, the folders, the journal, the team, the board,
a read-only look into the files, and its two ways of speaking to the operator.

Every operation starts by asking whether the calling session holds its project's orchestrator office
(:meth:`Orchestrators.current`), so a replaced orchestrator whose engine still runs is refused at the
first call. The results are text for the model: short, with the ids it needs for the next call.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.extensions import wakeups
from daedalus.extensions.board_commands import BoardCommands
from daedalus.extensions.likeness import same_question
from daedalus.extensions.notifications import Draft
from daedalus.extensions.plugins import PluginRefused, PluginRegistry
from daedalus.extensions.task_contract import split_checks
from daedalus.extensions.watch_commands import WatchCommands
from daedalus.extensions.watches import WatchRefused
from daedalus.host import prompts
from daedalus.host.peek import LocalFolderAccess, PeekRefused
from daedalus.staff_runtime import LiveSession
from daedalus.stores.control import ControlDenied, ControlStore, Entity, Principal, Scope
from daedalus.stores.files import HANDOVER_MAX_FILES, MAIN, FileRefused, human_size, parse_handle
from daedalus.stores.projects import (
    BRIEF_SECTIONS,
    COMMITMENT_KIND,
    OPERATOR_ONLY_SECTIONS,
    RULE_KIND,
    Project,
    ProjectError,
    ProjectFolder,
)
from daedalus.stores.staff import HARNESS_NAMES, Ask, StaffError
from daedalus.tools.vision import VisionUnavailable, kept_body

if TYPE_CHECKING:
    from daedalus.extensions.orchestrator import Orchestrators

BRIEF_BODY_MAX = 20_000
JOURNAL_TEXT_MAX = 4000
REPORT_TEXT_MAX = 4000
REPORT_KINDS = ("progress", "done", "blocked", "decision")
PEEK_OPS = ("read", "ls", "find", "search", "git_log", "git_diff", "git_status", "files", "dispatch")
TASK_OPS = ("list", "get", "create", "update", "move")
FOLDER_OPS = ("list", "add", "update", "remove")
TEAM_MESSAGES = 5


class Refused(ValueError):
    """What an orchestrator tool will not do, said so the orchestrator can do something else."""


async def board_principal(orch: Orchestrators, session_id: str, project_id: str,
                          operation: str, task_id: str | None = None) -> Principal:
    """The current office alone is insufficient; the host checks a live delegated grant."""
    authority = orch.app.extensions.get("orchestrator_board_authority")
    if authority is None:
        raise Refused("board writes need an operator-issued orchestrator grant")
    try:
        principal = await authority(session_id=session_id, operation=operation,
                                    project_id=project_id, task_id=task_id)
    except ControlDenied as exc:
        raise Refused(str(exc)) from exc
    if not isinstance(principal, Principal):
        raise Refused("board authority did not return a host identity")
    return principal


async def dispatch(orch: Orchestrators, operation: str, ops: Mapping[str, Callable[..., Awaitable[str]]] | None = None, /, **kwargs: Any) -> Any:
    """Run one operation for the session named by ``session_id`` once it is known to hold the office.
    ``ops`` is every operation there is; the team's live in their own module, which imports this one."""
    handler = (ops if ops is not None else OPS).get(operation)
    if handler is None:
        raise ValueError(f"unknown orchestrator operation {operation!r}")
    session_id = str(kwargs.pop("session_id", "") or "")
    project, _ = await orch.current(session_id)
    return await handler(orch, project, session_id, **kwargs)


# -- the brief -------------------------------------------------------------------------------------


async def brief(orch: Orchestrators, project: Project, session_id: str, *, section: str | None = None, body: str | None = None, append: bool = False) -> str:
    sections = await orch.manager.projects.brief(project.id)
    if section is None and body is None:
        return "\n\n".join(f"## {name}" + (f" (by the {s.updated_by}, {s.updated_at[:16].replace('T', ' ')})" if s.updated_at else "") + f"\n{s.body.strip() or '(empty)'}" for name, s in sections.items())
    if not section or section not in BRIEF_SECTIONS:
        raise Refused(f"the brief has the sections {', '.join(BRIEF_SECTIONS)}")
    if body is None:
        s = sections[section]
        return f"## {section}\n{s.body.strip() or '(empty)'}"
    if section in OPERATOR_ONLY_SECTIONS:
        raise Refused(f"{section} is the operator's alone: it bounds what you may grant. Propose a change with AskOperator.")
    text = body.strip()
    if append and sections[section].body.strip():
        text = sections[section].body.rstrip() + "\n" + text
    if len(text) > BRIEF_BODY_MAX:
        raise Refused(f"a brief section is at most {BRIEF_BODY_MAX} characters")
    try:
        await orch.manager.projects.set_brief(project.id, section, text, "orchestrator")
    except ProjectError as exc:
        raise Refused(str(exc)) from exc
    await orch._changed(project.id, "brief", "orchestrator")
    return f"the brief's {section} is written ({len(text)} characters)"


# -- folders ---------------------------------------------------------------------------------------


def _folder(project: Project, ref: str | None) -> ProjectFolder:
    if not ref:
        return project.primary
    for folder in project.folders:
        # The path as text: a folder's path is a Path, which never equals the string the orchestrator
        # gives, so a folder named by its path, the way Folders() lists it, was "no folder".
        if ref in (folder.id, folder.label, str(folder.path)) or (folder.label and folder.label.lower() == ref.lower()):
            return folder
    # The name a folder is listed by when it has no label is its directory's: Hire(folder="tern")
    # was refused for a folder the list showed as "tern".
    named = [f for f in project.folders if not f.label and Path(f.path).name == ref]
    if len(named) == 1:
        return named[0]
    raise Refused(f"{project.name} has no folder {ref!r}; Folders() lists them")


async def folders(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    op: str = "list",
    path: str | None = None,
    folder: str | None = None,
    label: str | None = None,
    env: str | None = None,
    readonly: bool | None = None,
) -> str:
    if op not in FOLDER_OPS:
        raise Refused(f"op is one of {', '.join(FOLDER_OPS)}")
    manager = orch.manager
    local = manager.projects.local_env
    if op == "list":
        return "\n".join(orch._folder_line(f) + ("" if f.local(local) else " — staff in a host terminal only") for f in project.folders)
    if op == "add":
        if not path:
            raise Refused("add needs a path")
        wanted = env or local
        if wanted not in ("container", "host"):
            raise Refused("env is container or host")
        if wanted != local:
            # A folder on the other side of the container is reachable only through the operator's own
            # machine; that is theirs to open. What the store would refuse is refused here, before the
            # operator is asked: an approval spent on a folder that could never be added told nobody why.
            try:
                target = await manager.projects.check_folder(project.id, path)
            except ProjectError as exc:
                raise Refused(str(exc)) from exc
            also = await manager.projects.holders(target, besides=project.id)
            detail = {"path": str(target), "label": label or "", "env": wanted, "readonly": bool(readonly), "also_in": also}
            text = f"Add the {wanted} folder {target} to {project.name}?" + (" (read-only)" if readonly else "")
            if also:
                text += f" It is also in the project{'s' if len(also) > 1 else ''} {', '.join(also)}."
            ask = await orch.open_request(project, session_id, kind="folder", text=text, options=["Add", "Don't add"], detail=detail)
            return f"a {wanted} folder needs the operator's confirmation; asked as [{ask.short_id}]. The answer arrives as an event."
        try:
            added = await manager.projects.add_folder(project.id, path, label=label or "", env=wanted, readonly=bool(readonly))
        except ProjectError as exc:
            raise Refused(str(exc)) from exc
        await manager.projects.record(project.id, "system", "folder", f"The orchestrator added the folder {added.path}" + (" (read-only)" if added.readonly else "") + ".", {"folder_id": added.id})
        await _reload(orch, project.id, "folders")
        return f"added [{added.id}] {added.path}"
    target = _folder(project, folder or path)
    if op == "update":
        if readonly is False and target.readonly:
            # Unlocking a folder would let staff write where the operator said they may not.
            raise Refused("only the operator makes a read-only folder writable; ask them")
        await manager.projects.update_folder(project.id, target.id, label=label, readonly=True if readonly else None)
        await manager.projects.record(project.id, "system", "folder", f"The orchestrator changed the folder {target.path}" + (f": label {label}" if label else "") + ("; now read-only" if readonly else "") + ".", {"folder_id": target.id})
        await _reload(orch, project.id, "folders")
        return f"folder [{target.id}] updated"
    if len(project.folders) == 1:
        raise Refused(f"{target.path} is the only folder of {project.name}")
    working = [s["title"] or s["id"] for s in await manager.projects.sessions_in_folder(project.id, target.id)]
    working += [m.name for m in await manager.staff.list(project.id) if (live := await manager.staff.live(m.id)) is not None and live.folder_id == target.id and m.name not in working]
    if working:
        raise Refused(f"{', '.join(working)} work in {target.path}; it can be removed once nobody does")
    try:
        await manager.projects.remove_folder(project.id, target.id)
    except (ProjectError, KeyError) as exc:
        raise Refused(str(exc)) from exc
    await manager.projects.record(project.id, "system", "folder", f"The orchestrator removed the folder {target.path} from the project; nothing on disk was touched.", {"folder_id": target.id})
    await _reload(orch, project.id, "folders")
    return f"removed [{target.id}] {target.path} from the project (the files are untouched)"


async def _reload(orch: Orchestrators, project_id: str, change: str) -> None:
    project = await orch.manager.projects.get(project_id)
    await orch.manager.reload_project(project, project_id)
    await orch._changed(project_id, change, "orchestrator")


# -- the journal -----------------------------------------------------------------------------------


async def journal(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    op: str = "write",
    text: str = "",
    why: str = "",
    kind: str = "",
    rule: int | None = None,
    before: int | None = None,
    limit: int = 20,
    commitment: int | None = None,
    task_id: str | None = None,
) -> str:
    kind = "".join(c for c in (kind or "").lower() if c.isalnum() or c in "_-")[:30]
    if op == "read":
        entries = await orch.manager.projects.journal(project.id, before=before, limit=max(1, min(int(limit), 50)), kind=kind or None)
        if not entries:
            return "(the journal is empty)" if before is None and not kind else "(no older entries)" if before is not None else f"(no {kind} entries)"
        lifted = await orch.manager.projects.lifted_rules(project.id) if any(e.kind == RULE_KIND for e in entries) else set()
        lines = [f"#{e.id} {e.at[:16].replace('T', ' ')} {e.author}/{e.kind}{' (lifted)' if e.kind == RULE_KIND and e.id in lifted else ''}: {e.text}" for e in entries]
        return "\n".join(lines) + f"\n(older: Journal(op='read', before={entries[-1].id}{f', kind={kind!r}' if kind else ''}))"
    if op == "lift":
        if rule is None:
            raise Refused("op='lift' needs rule, the id of the rule to lift (the state block lists the rules in force with their ids)")
        try:
            lifted_rule, entry = await orch.manager.projects.lift_rule(project.id, int(rule), "orchestrator", why)
        except ProjectError as exc:
            raise Refused(str(exc)) from exc
        await orch._changed(project.id, "journal", "orchestrator")
        told = await _announce_rule(orch, project, prompts.STAFF_RULE_LIFTED.format(text=lifted_rule.text))
        return f"rule #{lifted_rule.id} lifted (journal #{entry.id}); it leaves the state block and the briefs{told}"
    if op == "keep":
        if commitment is None:
            raise Refused("op='keep' needs commitment, the id of the commitment kept (the state block lists the open ones)")
        try:
            kept, entry = await orch.manager.projects.keep_commitment(project.id, int(commitment), "orchestrator", why)
        except ProjectError as exc:
            raise Refused(str(exc)) from exc
        await orch._changed(project.id, "journal", "orchestrator")
        return f"commitment #{kept.id} is closed (journal #{entry.id})"
    if op != "write":
        raise Refused("op is write, read, lift or keep")
    if kind == COMMITMENT_KIND:
        # One per ask of a message that asked several things: a question about the plan and a
        # clean-up asked together once came back as the clean-up alone, and nothing showed the
        # other was still owed.
        if task_id and await orch.manager.db.fetchone("SELECT 1 FROM board_tasks WHERE id = ? AND project_id = ?", (task_id, project.id)) is None:
            raise Refused(f"no task {task_id} on {project.name}'s board")
        try:
            entry = await orch.manager.projects.add_commitment(project.id, "orchestrator", text, task_id=task_id or "", message_seq=await orch.operator_message_seq(session_id))
        except ProjectError as exc:
            raise Refused(str(exc)) from exc
        await orch._changed(project.id, "journal", "orchestrator")
        return f"commitment #{entry.id} is open: the state block and the operator's list show it until Journal(op='keep', commitment={entry.id})" + (f" or task {task_id} is accepted" if task_id else "")
    if kind == RULE_KIND:
        # The operator's standing instruction is kept as a rule, not appended to the brief: a line
        # added at the end of the brief's notes fell past the part of each section the state block
        # shows, and the orchestrator never saw the instruction again after the turn that wrote it.
        try:
            entry = await orch.manager.projects.add_rule(project.id, "orchestrator", text)
        except ProjectError as exc:
            raise Refused(str(exc)) from exc
        await orch._changed(project.id, "journal", "orchestrator")
        told = await _announce_rule(orch, project, prompts.STAFF_RULE_ADDED.format(text=entry.text))
        return f"rule #{entry.id} is in force: the state block shows it whole every turn, and every member gets it with each task{told}"
    body = " ".join(text.split()) if "\n" not in text else text.strip()
    if not body:
        raise Refused("a journal entry needs text")
    if why.strip():
        body += f"\nWhy: {why.strip()}"
    if len(body) > JOURNAL_TEXT_MAX:
        raise Refused(f"a journal entry is at most {JOURNAL_TEXT_MAX} characters")
    entry = await orch.manager.projects.record(project.id, "orchestrator", kind or "decision", body)
    await orch._changed(project.id, "journal", "orchestrator")
    return f"journal entry #{entry.id} written"


async def _announce_rule(orch: Orchestrators, project: Project, text: str) -> str:
    """Tell the members at work now of a rule made or lifted: the host does it, so no member works on
    under a rule that changed while its brief was already written."""
    team = orch.team
    if team is None:
        return ""
    told = await team.announce_rule(project.id, text, by="orchestrator")
    return f"; {', '.join(told)} {'is' if len(told) == 1 else 'are'} told when their current turn ends" if told else ""


# -- the team --------------------------------------------------------------------------------------


async def team(orch: Orchestrators, project: Project, session_id: str, *, staff: str | None = None, concurrency: int | None = None) -> str:
    manager = orch.manager
    if concurrency is not None:
        cap = project.settings.orchestrator.concurrency_cap
        if not 1 <= int(concurrency) <= cap:
            raise Refused(f"concurrency is between 1 and the project's cap of {cap}; the operator moves the cap")
        project = await orch.update(project.id, concurrency=int(concurrency), by="orchestrator")
        if staff is None:
            return f"concurrency is now {project.settings.orchestrator.concurrency} of {project.settings.orchestrator.concurrency_cap}"
    if staff is None:
        now = datetime.now(UTC)
        live = await manager.staff.live_sessions(project.id)
        tasks = {r["id"]: dict(r) for r in await manager.db.fetchall("SELECT id, title FROM board_tasks WHERE project_id = ?", (project.id,))}
        members = await manager.staff.list(project.id)
        if not members:
            return "the team is empty"
        return "\n".join(("[one-off] " if m.one_off else "") + f"[{m.id}] " + orch._member_line(m, live.get(m.id), tasks, now) for m in members)
    member = await manager.staff.find(project.id, staff)
    if member is None:
        raise Refused(f"{project.name} has nobody called {staff!r}; Team() lists the team")
    sessions = await manager.staff.sessions(member.id, limit=5)
    lines = [
        f"{member.name} [{member.id}] — {member.role or 'no role'}",
        f"harness {HARNESS_NAMES.get(member.harness, member.harness)}" + (f", agent {member.agent}" if member.agent else "") + (f", model {member.model}" if member.model else "") + f", isolation {member.isolation}" + (", one-off" if member.one_off else "") + (", dismissed" if member.archived_at else ""),
    ]
    if member.instructions:
        lines.append(f"instructions: {member.instructions[:600]}")
    if member.notes:
        lines.append(f"notes: {member.notes[-600:]}")
    for s in sessions:
        state = "live" if s.live else f"ended ({s.end_reason or s.status})"
        lines.append(f"session {s.id}: {state}, {s.status}" + (f", task {s.task_id}" if s.task_id else "") + (f", branch {s.branch}" if s.branch else "") + (f", waiting for {s.waiting_for}" if s.waiting_for else ""))
        if s.live and hasattr(orch.team, "health"):
            # The same verdict the operator sees on the member's card, so the two never disagree
            # about whether the member can hear them.
            lines.append((await orch.team.health(LiveSession(member, s))).line(datetime.now(UTC)))
    if orch.team is not None:
        for entry in orch.team.queue.waiting_for(member.id):
            lines.append(f"queued: task {entry['task_id']} at {entry['position']} ({entry['reason']}: {entry['detail']})")
    messages = await manager.staff.messages(member.id, limit=TEAM_MESSAGES)
    for message in messages:
        lines.append(f"message {message.id} from the {message.origin} ({message.mode}): {message.state}" + (f" — {message.error}" if message.error else ""))
    return "\n".join(lines)


# -- the board -------------------------------------------------------------------------------------


def _task_line(task: dict[str, Any], names: dict[str, str]) -> str:
    assignee = names.get(task.get("assignee_staff_id") or "", "unassigned")
    return f"{task['id']} [{task['status']}] P{task['priority']} {task['title']} ({assignee})" + (f" after {', '.join(task['depends_on'])}" if task.get("depends_on") else "")


async def tasks(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    op: str = "list",
    task_id: str | None = None,
    title: str | None = None,
    objective: str | None = None,
    deliverable: str | None = None,
    boundaries: str | None = None,
    done_when: str | None = None,
    status: str | None = None,
    priority: int | None = None,
    depends_on: list[str] | None = None,
    assignee: str | None = None,
    note: str = "",
    waiting_on: str = "",
    new: bool = False,
    reason: str = "",
    client_operation_id: str = "",
    expected_entity_revision: int | None = None,
    expected_collection_revision: int | None = None,
) -> str:
    board = orch.board
    if board is None:
        raise Refused("the board is not available on this installation")
    if op not in TASK_OPS:
        raise Refused(f"op is one of {', '.join(TASK_OPS)}")
    names = {m.id: m.name for m in await orch.manager.staff.list(project.id, archived=True)}
    scope = Scope("project", project.id)
    commands = BoardCommands(orch.manager.db, bus=orch.manager.bus)
    if op == "list":
        rows = await board.list(status, include_done=status is not None, actor=session_id)
        revision = await ControlStore(orch.manager.db).revision(scope, Entity("collection", project.id))
        return f"collection revision {revision}\n" + ("\n".join(_task_line(t, names) for t in rows) or "(the board is empty)")
    brief = {k: v for k, v in (("objective", objective), ("deliverable", deliverable), ("boundaries", boundaries), ("done_when", done_when)) if v is not None}
    member_id: str | None = None
    if assignee is not None and assignee != "":
        member = await orch.manager.staff.find(project.id, assignee)
        if member is None or not member.active:
            raise Refused(f"{project.name} has nobody active called {assignee!r}")
        member_id = member.id
    try:
        if op == "create":
            if not title:
                raise Refused("a task needs a title")
            if not client_operation_id or expected_collection_revision is None:
                raise Refused("create needs its trusted call id and expected_collection_revision from Tasks(list)")
            from daedalus.extensions.orchestrator_team import refuse_twin  # Lazy: the team's module imports this one

            await refuse_twin(orch, project, " ".join(title.split()), objective or "", new=new, reason=reason)
            # Its done-when becomes its checks, as a card made by Assign gets them.
            checklist = split_checks(done_when or "")
            principal = await board_principal(orch, session_id, project.id, "board.task.create")
            created = await commands.create(principal, scope, client_operation_id=client_operation_id,
                                            expected_collection_revision=expected_collection_revision,
                                            title=title, acceptance=done_when or "", checklist=checklist,
                                            depends_on=depends_on, priority=priority or 3, notes=note,
                                            brief=brief, source_session_id=session_id,
                                            assignee_staff_id=member_id)
            task = await board.get(created["task_id"], actor=session_id)
        elif not task_id:
            raise Refused(f"{op} needs a task_id")
        elif op == "get":
            task = await board.get(task_id, actor=session_id)
            parts = [_task_line(task, names), f"entity revision {task['entity_revision']}"]
            parts += [f"{k.replace('_', ' ')}: {v}" for k, v in task["brief"].items() if v]
            parts += await _contract_lines(orch, task)
            if task.get("branch"):
                parts.append(f"branch {task['branch']} ({task.get('merge_state') or 'unmerged'})")
            if task.get("notes"):
                parts.append("notes:\n" + task["notes"][-1500:])
            return "\n".join(parts)
        else:
            if not client_operation_id or expected_entity_revision is None:
                raise Refused("update needs its trusted call id and expected_entity_revision from Tasks(get)")
            if op == "move" and not status:
                raise Refused("move needs a status")
            if status in ("doing", "review", "done"):
                raise Refused("launch, review and completion require their exact receipt commands")
            if assignee is not None and assignee == "":
                raise Refused("unassignment requires stopping or reconciling the current execution")
            waits = " ".join((waiting_on or "").split())
            if waits:
                if status != "blocked":
                    raise Refused("waiting_on goes with status='blocked': what the card waits for")
                note = (note + " " if note else "") + f"waits on: {waits}"
            principal = await board_principal(orch, session_id, project.id, "board.task.update", task_id)
            updated = await commands.update(principal, scope, task_id, client_operation_id=client_operation_id,
                                            expected_entity_revision=expected_entity_revision,
                                            status=status, note=note, title=title if op == "update" else None,
                                            priority=priority, brief=brief or None, depends_on=depends_on,
                                            assignee_staff_id=member_id)
            task = await board.get(updated["task_id"], actor=session_id)
    except KeyError as exc:
        raise Refused(f"no task {exc.args[0] if exc.args else task_id} on {project.name}'s board") from exc
    except ValueError as exc:
        if isinstance(exc, Refused):
            raise
        raise Refused(str(exc)) from exc
    line = _task_line(task, names)
    if op == "move" and status == "blocked":
        if waits:
            await orch.loops.close(project.id, task_id=task["id"], by="orchestrator", decision=f"set aside, waiting on {waits}")
        elif await orch.manager.db.fetchone("SELECT 1 FROM open_loops WHERE task_id = ? AND closed_at IS NULL", (task["id"],)):
            line += "\nIts result still waits for your decision: say what the card waits on (waiting_on=…), give the next step, or Decide"
    elif op == "move" and status == "dropped":
        await orch.loops.close(project.id, task_id=task["id"], by="orchestrator", decision=f"dropped{': ' + note if note else ''}")
        await orch.keep_commitments_of(project.id, task["id"], f"its task {task['id']} was dropped")
    if member_id and orch.team is not None:
        member = await orch.manager.staff.get(member_id)
        try:
            principal = await board_principal(orch, session_id, project.id, "task.launch", task["id"])
            launched = await orch.team.assign(member, task["id"], principal=principal,
                                              client_operation_id=client_operation_id + ":launch",
                                              expected_entity_revision=task["entity_revision"])
        except (StaffError, KeyError) as exc:
            return f"{line}\nassigned, but it cannot start: {exc}"
        state = launched.get("state")
        where = f" (queue position {launched.get('position')}: {launched.get('detail')})" if state == "queued" else ""
        return f"{line}\n{member.name if member else 'the assignee'}: {state}{where}"
    return line


async def _contract_lines(orch: Orchestrators, task: dict[str, Any]) -> list[str]:
    """A card's acceptance, checks and requirements as Tasks(op='get') shows them."""
    team = orch.team
    if team is None:
        return []
    lines: list[str] = []
    state = str(task.get("acceptance_state") or "")
    if state:
        lines.append("acceptance: " + {"handed_in": "handed in, not checked", "accepted": "checked and accepted by you", "operator_approved": "approved by the operator", "returned": "returned for another round"}.get(state, state))
    for index, item in enumerate(task.get("checklist") or [], start=1):
        evidence = item.get("evidence") or {}
        mark = item.get("mark") or {}
        said = f" — evidence: {evidence.get('how')}: {evidence.get('result')}" if evidence else ""
        judged = f" — {'met' if mark.get('ok') else 'NOT met'}{': ' + mark['note'] if mark.get('note') else ''}" if mark else ""
        lines.append(f"C{index} {item.get('text')}{said}{judged}")
    deliveries = await team.contracts.deliveries(task["id"])
    for requirement in await team.contracts.requirements(task["id"]):
        given = deliveries.get(requirement.id) or []
        heard = ", ".join(
            f"{d['staff_name']}: " + ("confirmed" if d["acknowledged_at"] else "opened" if d["opened_at"] else f"sent ({d['via']})")
            for d in given
        )
        mark = requirement.mark
        judged = f" — {'met' if mark.get('ok') else 'NOT met'}" if mark else ""
        lines.append(f"{requirement.label} [{requirement.kind}, from {requirement.origin()}] {requirement.text}" + (f" ({heard})" if heard else "") + judged)
    return lines


# -- looking into the files ------------------------------------------------------------------------


FILES_LISTED = 30


async def project_files(orch: Orchestrators, project: Project) -> str:
    """The project's files, newest first: what Peek(op='files') shows."""
    store = orch.manager.files
    listed = await store.listing(project.id, limit=FILES_LISTED)
    if not listed:
        return f"{project.name} has no files yet. The operator's attachments in this chat and the files staff report back are kept here, each with a handle (att:…)."
    lines = [f"- {f.handle} {f.name} ({f.mime}, {human_size(f.size)}; from the {f.origin}, {f.created_at[:16].replace('T', ' ')})" for f in listed]
    return "\n".join([f"{project.name}'s files, newest first (read one with Peek(op='read', path='att:…'); hand one over with Assign or Tell files=[…]):", *lines])


async def read_kept(orch: Orchestrators, scope: str, handle_text: str, *, offset: int, limit: int) -> str:
    """A kept file's lines, for a scope that may use its handle."""
    try:
        stored = await orch.manager.files.in_scope(handle_text, scope)
    except FileRefused as exc:
        raise Refused(str(exc)) from exc
    data = await orch.manager.files.read(stored)
    try:
        body = await kept_body(orch.manager, data, stored, offset=offset, limit=limit)
    except (PeekRefused, VisionUnavailable) as exc:
        raise Refused(str(exc)) from exc
    return f"{stored.handle} {stored.name} ({stored.mime}, {stored.size} bytes):\n{body}"


def _own_workspace(orch: Orchestrators, session_id: str, path: str) -> LocalFolderAccess | None:
    """The orchestrator's own working directory, when ``path`` is inside it: where a host project's
    orchestrator keeps what the operator attached before handles existed, which no project folder holds."""
    state = orch.manager.live_state(session_id)
    services = orch.manager.locator_services(session_id)
    if state is None or services is None or not os.path.isabs(path):
        return None
    own = Path(os.path.realpath(state.workspace))
    wanted = Path(os.path.realpath(path))
    if wanted != own and own not in wanted.parents:
        return None
    return LocalFolderAccess(own, services)


async def peek(orch: Orchestrators, project: Project, session_id: str, *, op: str, path: str = "", folder: str | None = None, pattern: str = "", ref: str = "", offset: int = 1, limit: int = 200) -> str:
    if op not in PEEK_OPS:
        raise Refused(f"op is one of {', '.join(PEEK_OPS)}")
    if op == "files":
        return await project_files(orch, project)
    if op == "dispatch":
        return await read_dispatch(orch, project, ref or path)
    if parse_handle(path) is not None and (path.strip().startswith("att:") or not folder):
        if op not in ("read", "ls"):
            raise Refused(f"{path} is a kept file: read it with op='read'")
        return await read_kept(orch, project.id, path, offset=offset, limit=limit)
    own = _own_workspace(orch, session_id, path) if not folder and op in ("read", "ls", "find", "search") else None
    access = own if own is not None else orch.folder_access(project, _folder(project, folder), session_id)
    target = _folder(project, folder)
    try:
        if op == "read":
            if not path:
                raise Refused("read needs a path")
            return await access.read(path, offset=offset, limit=limit)
        if op == "ls":
            return await access.ls(path)
        if op == "find":
            return await access.find(pattern, path)
        if op == "search":
            return await access.search(pattern, path)
        if not target.is_git and target.local(orch.manager.projects.local_env):
            # A folder of the other environment is recorded as no repository because this process
            # could not look; git on that side says whether it is one.
            raise Refused(f"{target.path} is not a git repository")
        if op == "git_log":
            return await access.git_log(ref, path, limit=limit if limit != 200 else 20)
        if op == "git_diff":
            return await access.git_diff(ref, path)
        return await access.git_status()
    except PeekRefused as exc:
        raise Refused(str(exc)) from exc


async def read_dispatch(orch: Orchestrators, project: Project, ref: str) -> str:
    """A dispatch of this project whole — what the operator asked for, relayed — with what was said on it.

    The journal and the wake-up once carried a dispatch cut at 600 characters, and no tool gave the
    rest: a long request reached the orchestrator as its first paragraph."""
    dispatch = await orch.manager.dispatches.get(ref) if (ref or "").strip() else None
    if dispatch is None or dispatch.project_id != project.id:
        raise Refused(f"{project.name} has no dispatch {ref!r}; the state block lists the open ones")
    lines = [
        f"dispatch {dispatch.id} (#{dispatch.seq}), {dispatch.status}, from {dispatch.created_at[:16].replace('T', ' ')}" + (f": {dispatch.title}" if dispatch.title else ""),
        dispatch.text,
    ]
    if dispatch.result:
        lines += ["", f"result: {dispatch.result}"]
    messages = await orch.manager.dispatches.messages(dispatch.id)
    if messages:
        lines += ["", "said on it since, oldest first:"]
        lines += [f"— {m.at[:16].replace('T', ' ')} {m.author} ({m.kind}): {m.text}" for m in messages]
    return "\n".join(lines)


# -- speaking to the operator ----------------------------------------------------------------------


ASK_BATCH_MAX = 12
"""Questions in one AskOperator call. More than a screenful at once is a survey, not a set of decisions."""
TITLE_MAX = 120


@dataclass(frozen=True, slots=True)
class _Question:
    title: str
    text: str
    options: list[str]
    multi: bool
    urgent: bool
    task_id: str | None
    dispatch_id: str | None


async def _question(orch: Orchestrators, project: Project, raw: Any, where: str) -> _Question:
    """One question checked before anything is asked, so a batch with a bad question asks nothing and
    the refusal names which one."""
    if not isinstance(raw, dict):
        raise Refused(f"{where} is not an object with a title and a text")
    title = " ".join(str(raw.get("title") or "").split())
    text = str(raw.get("text") or "").strip()
    if not title:
        raise Refused(f"{where} has no title: give each question a few words the operator can scan in a list")
    if len(title) > TITLE_MAX:
        raise Refused(f"{where}'s title is longer than {TITLE_MAX} characters; say it in a few words and put the rest in text")
    if not text:
        raise Refused(f"{where} has no text")
    context = str(raw.get("context") or "").strip()
    if context:
        text += f"\n\nContext: {context}"
    options = raw.get("options") or []
    if not isinstance(options, list):
        raise Refused(f"{where}'s options are a list of strings")
    labels = list(dict.fromkeys(str(o).strip()[:200] for o in options if str(o or "").strip()))[:8]
    multi = bool(raw.get("multi")) and len(labels) > 1
    task_id = str(raw["task_id"]) if raw.get("task_id") else None
    if task_id:
        row = await orch.manager.db.fetchone("SELECT 1 FROM board_tasks WHERE id = ? AND project_id = ?", (task_id, project.id))
        if row is None:
            raise Refused(f"{where}: no task {task_id} on {project.name}'s board")
    linked: str | None = None
    dispatch_id = str(raw["dispatch_id"]).strip() if raw.get("dispatch_id") else ""
    if dispatch_id:
        # The link that shows this question in the main orchestrator's chat as well as this one. It is
        # checked here, so a question is never shown under another project's dispatch or a closed one.
        dispatch = await orch.manager.dispatches.get(dispatch_id)
        if dispatch is None or dispatch.project_id != project.id:
            raise Refused(f"{where}: {project.name} has no dispatch {dispatch_id!r}; the state block lists its open dispatches")
        if dispatch.status not in ("open", "blocked"):
            raise Refused(f"{where}: dispatch {dispatch.id} is {dispatch.status}; ask without it")
        linked = dispatch.id
    return _Question(title, text[:8000], labels, multi, bool(raw.get("urgent")), task_id, linked)


async def ask_operator(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    op: str = "ask",
    id: str | None = None,
    ids: list[str] | None = None,
    reason: str = "",
    questions: list[Any] | None = None,
    title: str = "",
    text: str = "",
    options: list[str] | None = None,
    multi: bool = False,
    context: str = "",
    task_id: str | None = None,
    urgent: bool = False,
    dispatch_id: str | None = None,
) -> str:
    """One question, or several at once. Every question is checked before any is asked, and all of
    them are asked before the call returns, so the ids come back at once and the operator's list
    fills in one go. The answers come later, together if the operator answers them together.

    ``op='update'`` rewords an open question in place and ``op='withdraw'`` takes questions back; a
    new question on the subject of one still open is refused with that question's id."""
    if op == "update":
        return await update_question(orch, project, session_id, ref=id or "", title=title, text=text, options=options, multi=multi, context=context, urgent=urgent)
    if op == "withdraw":
        return await withdraw_questions(orch, project, session_id, ids=ids or ([id] if id else []), reason=reason)
    if op != "ask":
        raise Refused("op is ask (the default), update or withdraw")
    if questions:
        if not isinstance(questions, list):
            raise Refused("questions is a list of {title, text, options?, multi?, urgent?, dispatch_id?, context?}")
        if len(questions) > ASK_BATCH_MAX:
            raise Refused(f"at most {ASK_BATCH_MAX} questions at once; ask what blocks the work first")
        if text or title:
            raise Refused("give either questions=[…] or one question with its title and text, not both")
        raws: list[Any] = list(questions)
    else:
        raws = [{"title": title, "text": text, "options": options, "multi": multi, "context": context, "task_id": task_id, "urgent": urgent, "dispatch_id": dispatch_id}]
    single = len(raws) == 1
    checked = [await _question(orch, project, raw, "the question" if single else f"question {i + 1}") for i, raw in enumerate(raws)]
    await _not_asked_already(orch, project, checked)
    asked: list[Ask] = []
    for q in checked:
        # No "only the options" switch: the operator may always answer in their own words, or add a
        # note to an option. An orchestrator once shut that off for a question whose options did not
        # fit, and the operator had no way to say so.
        detail: dict[str, Any] = {"urgent": q.urgent, "multi": q.multi}
        asked.append(await orch.open_request(project, session_id, kind="question", title=q.title, text=q.text, options=q.options, detail=detail, task_id=q.task_id, dispatch_id=q.dispatch_id))
    for ask in asked:
        if ask.task_id:
            await orch.loops.close(project.id, task_id=ask.task_id, by="orchestrator", decision=f"asked the operator [{ask.short_id}] {ask.title}")
    shown = " Those with a dispatch_id are shown in the main orchestrator's chat too." if any(a.dispatch_id for a in asked) else ""
    if single:
        shown = " It is shown in the main orchestrator's chat too." if asked[0].dispatch_id else ""
        return f"asked the operator as [{asked[0].short_id}]; do not wait — the answer arrives as an event in a later wake-up.{shown}"
    listed = "; ".join(f"[{a.short_id}] {a.title}" for a in asked)
    return f"asked the operator {len(asked)} questions: {listed}. Do not wait — the answers arrive as events, together when the operator answers them together.{shown}"


def _question_body(title: str, text: str, options: list[str]) -> str:
    return " ".join([title, text.split("\n\nContext:")[0], *options])


async def _not_asked_already(orch: Orchestrators, project: Project, checked: list[_Question]) -> None:
    """Refuse a question that asks again what an open question of the orchestrator's already asks.

    The operator found six questions in their list, three of them the first three reworded: a turn
    that meant to remind them asked afresh, while the first ones had been waiting for eight minutes
    and the state block said so. The refusal names the open question and how to change it in place."""
    open_ones = [a for a in await orch.manager.asks.open_for(project.id) if a.origin == "orchestrator" and a.kind == "question"]
    clashes: list[str] = []
    for q in checked:
        mine = _question_body(q.title, q.text, q.options)
        for ask in open_ones:
            if same_question(mine, _question_body(ask.title, ask.text, [str(o) for o in ask.detail.get("options") or []])):
                clashes.append(f"\"{q.title}\" asks what [{ask.short_id}] \"{ask.heading}\" already asks")
                break
    if clashes:
        raise Refused(
            "nothing was asked: " + "; ".join(clashes) + ". The operator still has it in their list: change it in place with "
            "AskOperator(op='update', id=…, …) — an answer they began to write stays with it — or take it back with op='withdraw'. "
            "Ask the other questions of this call again without it"
        )


async def update_question(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    ref: str,
    title: str = "",
    text: str = "",
    options: list[str] | None = None,
    multi: bool = False,
    context: str = "",
    urgent: bool = False,
) -> str:
    """Reword an open question of the orchestrator's in place: the operator's card changes, it keeps
    its place and its id, and what they already typed as an answer stays."""
    ask = await _own_open_question(orch, project, ref)
    before, _, said_context = ask.text.partition("\n\nContext:")
    raw = {
        "title": title or ask.title, "text": text or before, "context": context or said_context.strip(),
        "options": options if options is not None else list(ask.detail.get("options") or []), "multi": multi or bool(ask.detail.get("multi")),
        "urgent": urgent or bool(ask.detail.get("urgent")), "task_id": ask.task_id, "dispatch_id": None,
    }
    q = await _question(orch, project, raw, "the question")
    revision = int(ask.detail.get("revision") or 0) + 1
    detail = {**ask.detail, "options": q.options, "multi": q.multi, "urgent": q.urgent, "revision": revision, "updated_at": datetime.now(UTC).isoformat()}
    try:
        changed = await orch.manager.asks.revise(ask.id, title=q.title, text=q.text, detail=detail)
    except StaffError as exc:
        raise Refused(str(exc)) from exc
    if not changed:
        raise Refused(f"[{ask.short_id}] was answered or withdrawn a moment ago; its answer arrives as an event")
    await orch.manager.bus.publish("ask.updated", {"request_id": ask.id, "short_id": ask.short_id, "routed_to": ask.routed_to}, project_id=project.id, session_id=session_id)
    return f"[{ask.short_id}] is updated in place (revision {revision}); the operator sees it changed, and an answer they began to write stays"


async def _own_open_question(orch: Orchestrators, project: Project, ref: str) -> Ask:
    text = (ref or "").strip().strip("[]")
    if not text:
        raise Refused("give the id of the question to update; the state block lists yours still waiting")
    ask = await orch.manager.asks.get(text)
    if ask is None or ask.project_id != project.id:
        raise Refused(f"{project.name} has no open question {ref!r}; the state block lists yours still waiting")
    if ask.origin != "orchestrator":
        raise Refused(f"[{ask.short_id}] is a staff member's request, not your question")
    if not ask.open:
        raise Refused(f"[{ask.short_id}] was already answered by the {ask.resolved_by}: {orch._answer_text(ask)}")
    return ask


async def withdraw_questions(orch: Orchestrators, project: Project, session_id: str, *, ids: list[str] | None = None, reason: str = "") -> str:
    """Take back questions of its own that still wait, one or many. Each id is judged on its own: one
    answered a moment ago is reported with its answer, which is news the orchestrator needs, rather
    than failing the rest."""
    why = " ".join((reason or "").split())
    if not why:
        raise Refused("say why, in a few words: the operator sees it where the question was")
    refs = [str(i).strip() for i in (ids or []) if str(i or "").strip()]
    if not refs:
        raise Refused("give the ids of the questions to withdraw; the state block lists those still waiting")
    team = orch.team
    if team is None:
        raise Refused("the team is not running on this installation")
    withdrawn: list[str] = []
    refused: list[str] = []
    for ref in dict.fromkeys(refs):
        ask = await orch.manager.asks.get(ref)
        if ask is None:
            row = await orch.manager.db.fetchone("SELECT id FROM asks WHERE short_id = ? AND project_id = ? ORDER BY created_at DESC LIMIT 1", (ref.lower().lstrip("[").rstrip("]"), project.id))
            ask = await orch.manager.asks.get(row["id"]) if row is not None else None
        if ask is None or ask.project_id != project.id:
            refused.append(f"{ref}: {project.name} has no such request")
        elif ask.origin != "orchestrator":
            refused.append(f"[{ask.short_id}]: not yours to withdraw — it is a staff member's request")
        elif not ask.open:
            if ask.resolved_by == "system":
                refused.append(f"[{ask.short_id}]: already withdrawn")
            else:
                refused.append(f"[{ask.short_id}]: already answered by the {ask.resolved_by}: {orch._answer_text(ask)}")
        elif await team.withdraw(ask, why=why, by="orchestrator"):
            withdrawn.append(f"[{ask.short_id}]")
        else:
            refused.append(f"[{ask.short_id}]: answered a moment ago; its answer arrives as an event")
    if not withdrawn:
        raise Refused("nothing withdrawn — " + "; ".join(refused))
    await orch.manager.projects.record(project.id, "orchestrator", "withdrawal", f"Withdrew {', '.join(withdrawn)}: {why}", {"asks": ",".join(withdrawn)})
    tail = f" Not withdrawn: {'; '.join(refused)}." if refused else ""
    return f"withdrew {', '.join(withdrawn)}; the operator sees each one go with your reason.{tail}"


async def project_report(orch: Orchestrators, project: Project, session_id: str, *, text: str, title: str = "", kind: str = "progress", task_id: str | None = None, dispatch_id: str | None = None, files: list[str] | None = None) -> str:
    """``dispatch_id`` names the main orchestrator's hand-over this report answers: a done or blocked
    report closes it, a progress or decision report is a message on it. Either reaches the main
    orchestrator, which tells the operator in its chat; the project's own notification is then quiet,
    so one report does not sound twice on the operator's phone."""
    body = text.strip()
    if not body:
        raise Refused("a report needs text")
    if kind not in REPORT_KINDS:
        raise Refused(f"kind is one of {', '.join(REPORT_KINDS)}")
    if len(body) > REPORT_TEXT_MAX:
        raise Refused(f"a report is at most {REPORT_TEXT_MAX} characters; the journal holds the detail")
    headline = " ".join((title or "").split())[:120] or f"{project.name}: {kind}"
    unchecked = ""
    if kind == "done" and task_id:
        row = await orch.manager.db.fetchone("SELECT acceptance_state FROM board_tasks WHERE id = ? AND project_id = ?", (task_id, project.id))
        if row is not None and row["acceptance_state"] == "handed_in":
            # "Ready" once went out for work nobody had looked at; the operator learns which it is.
            body = f"(Handed in by the member, not checked yet.)\n{body}"
            unchecked = f"; task {task_id} is handed in and not checked, and the operator was told so — ReviewResult shows its exact report"
    attached = []
    for ref in [str(f) for f in files or [] if str(f or "").strip()][:HANDOVER_MAX_FILES]:
        try:
            attached.append(await orch.manager.files.in_scope(ref, project.id))
        except FileRefused as exc:
            raise Refused(str(exc)) from exc
    dispatches = orch.app.extensions.get("dispatches")
    closed = ""
    if dispatch_id:
        if dispatches is None:
            raise Refused("dispatches are not running on this installation; report without dispatch_id")
        try:
            dispatch = await dispatches.report(project, dispatch_id.strip(), kind=kind, text=body, title=(title or "").strip(), files=attached)
        except ValueError as exc:
            raise Refused(str(exc)) from exc
        closed = f"; dispatch {dispatch.id} is {dispatch.status}" if dispatch.status != "open" else f"; added to dispatch {dispatch.id}"
    elif attached:
        # No dispatch to carry them: still the operator's to download from the report, and the main
        # orchestrator's to read if the operator asks it about them.
        for stored in attached:
            await orch.manager.files.grant(stored, MAIN, actor="orchestrator", target="report")
    if attached:
        body += "\n\nFiles: " + ", ".join(f.short() for f in attached)
    refs: dict[str, Any] = {"kind": kind}
    if task_id:
        refs["task_id"] = task_id
    if dispatch_id:
        refs["dispatch_id"] = dispatch_id.strip()
    entry = await orch.manager.projects.record(project.id, "orchestrator", "report", f"{headline}\n{body}", refs)
    notifications = orch.app.notifications
    if notifications is not None:
        await notifications.post(Draft(
            "orchestrator_report",
            headline if headline.startswith(project.name) else f"{project.name}: {headline}",
            body,
            kind=f"project_report_{kind}",
            tone="warning" if kind == "blocked" else "ok" if kind == "done" else "info",
            level="quiet" if dispatch_id else "normal",
            project_id=project.id,
            session_id=session_id,
            link=f"/app/project/{project.id}",
            source="project_report",
        ))
    await orch._changed(project.id, "journal", "orchestrator")
    return f"reported (journal #{entry.id}){closed}{unchecked}"


# -- its own alarms -------------------------------------------------------------------------------


async def wake_me(orch: Orchestrators, project: Project, session_id: str, *, note: str, at: str | None = None, in_minutes: int | None = None, cron: str | None = None) -> str:
    try:
        wakeup = await wakeups.set_wakeup(orch.app, project, note=note, at=at, in_minutes=in_minutes, cron=cron, by_session=session_id)
    except wakeups.WakeupRefused as exc:
        raise Refused(str(exc)) from exc
    await orch._changed(project.id, "wakeups", "orchestrator")
    return f"wake-up saved: {wakeups.describe(wakeup)}. It can fire only after the operator approves the standing schedule in the app."


def _watches(orch: Orchestrators) -> Any:
    found = orch.app.extensions.get("watches")
    if found is None:
        raise Refused("watches are not running on this installation")
    return found


async def watch(orch: Orchestrators, project: Project, session_id: str, *, when: Any, then: Any,
                cooldown_minutes: Any = 10, once: bool = False, note: str = "",
                deadline_at: str | None = None, client_operation_id: str) -> str:
    authority = orch.app.extensions.get("board_tool_authority")
    if authority is None:
        raise Refused("watch authority is unavailable")
    try:
        principal, scope = await authority(session_id, "watch.create")
        if scope != Scope("project", project.id):
            raise ControlDenied("watch authority belongs to another project")
        revision = await ControlStore(orch.manager.db).revision(scope, Entity("collection", project.id))
        made = await WatchCommands(_watches(orch)).create(
            principal, project, when=when, then=then, cooldown_minutes=cooldown_minutes,
            once=once, note=note, deadline_at=deadline_at,
            expected_collection_revision=revision, client_operation_id=client_operation_id,
        )
    except (WatchRefused, ControlDenied) as exc:
        raise Refused(str(exc)) from exc
    return f"watch {made['id']} set: {made['describe']} (cooldown {made['cooldown_minutes']} min{', once' if made['once'] else ''}). Unwatch(\"{made['id']}\") removes it."


async def unwatch(orch: Orchestrators, project: Project, session_id: str, *, id: str,
                  client_operation_id: str) -> str:
    ref = (id or "").strip()
    if not ref:
        raise Refused("give the id of a wake-up or a watch; the state block lists them")
    watches: Any = orch.app.extensions.get("watches")
    current = watches.get(project.id, ref) if watches is not None else None
    if current is not None:
        authority = orch.app.extensions.get("board_tool_authority")
        if authority is None:
            raise Refused("watch authority is unavailable")
        principal, scope = await authority(session_id, "watch.remove")
        if scope != Scope("project", project.id):
            raise Refused("watch authority belongs to another project")
        revision = await ControlStore(orch.manager.db).revision(scope, Entity("project", project.id))
        await WatchCommands(watches).remove(
            principal, project.id, ref, expected_entity_revision=revision,
            expected_condition_revision=current.condition_revision,
            client_operation_id=client_operation_id,
        )
        return f"watch {ref} removed"
    try:
        removed = await wakeups.cancel(orch.app, project.id, ref, by_session=session_id)
    except wakeups.WakeupRefused as exc:
        raise Refused(str(exc)) from exc
    if removed:
        await orch._changed(project.id, "wakeups", "orchestrator")
        return f"wake-up {ref} cancelled"
    raise Refused(f"{project.name} has no wake-up or watch {ref!r}; the state block lists them with their ids")


async def plugin_read(orch: Orchestrators, project: Project, session_id: str, *,
                      plugin_id: str, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    registry = orch.app.extensions.get("plugin_registry")
    if not isinstance(registry, PluginRegistry):
        raise Refused("project extensions are unavailable")
    try:
        result = await registry.invoke_read(Principal(session_id, "agent"), project.id, plugin_id,
                                            tool_name, arguments or {})
    except PluginRefused as exc:
        raise Refused(str(exc)) from exc
    answer = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if len(answer) > 8000:
        raise Refused("the extension result exceeds the project tool limit")
    return answer


OPS: dict[str, Callable[..., Awaitable[str]]] = {
    "brief": brief,
    "folders": folders,
    "journal": journal,
    "team": team,
    "tasks": tasks,
    "peek": peek,
    "ask_operator": ask_operator,
    "withdraw_questions": withdraw_questions,
    "project_report": project_report,
    "wake_me": wake_me,
    "watch": watch,
    "unwatch": unwatch,
    "plugin_read": plugin_read,
}

__all__ = ["OPS", "Refused", "dispatch"]
