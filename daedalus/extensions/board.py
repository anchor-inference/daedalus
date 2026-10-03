"""Task board: the plan lives outside the model's context.

A summary degrades; a board does not. Tasks carry acceptance criteria, a checklist,
dependencies (a task becomes ready when every dependency is done), a priority and the
session working on it. Work-in-progress is limited, a task whose session went quiet is
handed back, and the board is the same object in the Mini App, in ``/board`` and in the
agent's tools.

Every agent sees its own board: the tasks created by its session and its subagents, plus
the tasks the operator posted to nobody in particular. Another agent's tasks are not on it.
Two independent agents each keep a plan there, and a loop agent that "reads the board at the
start of a long task" reads its plan, not a colleague's — the day it did, it picked up a bug
fix a second agent was already working on. Work crosses the line by a hand-over (SubAgent,
SpawnAgent, AskPeer) or by the operator, never by one agent finding it on the other's board.

A project has a board of its own as well: every task whose ``project_id`` is the project. Its staff
and its orchestrator see that board rather than a family's, a task there can be assigned to a staff
member and carries a four-part brief, and "Needs you" is read from the project's open requests to
the operator rather than stored. What the operator posts to a project's board stays off the
ordinary agents' boards: "unaddressed" means unaddressed and outside every project, or a single
agent working in the project would find the team's work on its own board.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from daedalus.extensions.board_commands import BoardCommands
from daedalus.extensions.notifications import Draft
from daedalus.extensions.orchestrator_domain import capture_contract_change, check_planning_capacity
from daedalus.extensions.task_contract import Contracts
from daedalus.stores.control import ControlStore, Entity, Principal, Scope

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

NOTES_MAX_CHARS = 8000
STATUSES = ("todo", "doing", "review", "done", "blocked", "dropped")
TICK_SECONDS = 300
BRIEF_FIELDS = ("objective", "deliverable", "boundaries", "done_when")
BRIEF_MAX_CHARS = 4000
STAFF_MOVES = ("doing", "review", "blocked")
"""Where a staff member may take a task: into work, to review, or aside as blocked. Finishing it is the
operator's acceptance (or the merge), and dropping it is the orchestrator's or the operator's call."""
_ORDER = "CASE status WHEN 'doing' THEN 0 WHEN 'review' THEN 1 WHEN 'todo' THEN 2 WHEN 'blocked' THEN 3 WHEN 'done' THEN 4 ELSE 5 END, priority, created_at"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is acting on the board, as the events and the rules need to know it.

    ``kind`` is ``operator`` (the app, Telegram, no session), ``orchestrator`` (the project's current
    orchestrator session), ``staff`` (a staff member's session) or ``agent`` (any other session).
    """

    kind: str
    session_id: str | None = None
    project_id: str | None = None
    staff_id: str | None = None


OPERATOR = Actor("operator")


def _brief(raw: Any) -> dict[str, str]:
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {k: str(data.get(k) or "") for k in BRIEF_FIELDS}


class Board:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def add(
        self,
        *,
        title: str,
        acceptance: str = "",
        depends_on: list[str] | None = None,
        priority: int = 3,
        checklist: list[str] | None = None,
        session_id: str | None = None,
        notes: str = "",
        project_id: str | None = None,
        assignee_staff_id: str | None = None,
        brief: dict[str, str] | None = None,
        operator: bool = False,
    ) -> dict[str, Any]:
        """Add a task. ``session_id`` is the board it goes on (and, unless ``operator``, who adds it);
        ``project_id`` puts it on a project's board, and a session's own project is used when it is not
        given, so a task is drawn on the board of the project its agent works in."""
        actor = OPERATOR if operator or session_id is None else await self.actor_of(session_id)
        if project_id is None and session_id is not None:
            project_id = actor.project_id or await self._project_of_session(session_id)
        elif project_id is not None and await self.app.db.fetchone("SELECT 1 FROM projects WHERE id = ?", (project_id,)) is None:
            raise KeyError(project_id)
        task_id = uuid.uuid4().hex[:6]
        deps = [d for d in (depends_on or []) if d and d != task_id]
        await self._check_deps(deps, project_id=project_id, actor=None if actor.kind == "operator" else session_id)
        if assignee_staff_id:
            if actor.kind in ("staff", "agent"):
                raise ValueError("only the operator or the project's orchestrator assigns tasks")
            await self._assignee(project_id, assignee_staff_id)
        status = "blocked" if await self._has_open_deps(deps) else "todo"
        async with self.app.db.transaction() as conn:
            if project_id is not None:
                await check_planning_capacity(conn, project_id, deps)
            await conn.execute(
            "INSERT INTO board_tasks(id, title, status, priority, acceptance, checklist, depends_on, session_id, origin_session_id, notes, created_at, updated_at, project_id, assignee_staff_id, brief_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, title[:200], status, max(1, min(int(priority), 5)), acceptance[:2000], json.dumps([{"text": c[:200], "done": False} for c in (checklist or [])]),
                json.dumps(deps), session_id, session_id, notes[:4000], _now(), _now(), project_id, assignee_staff_id or None, json.dumps(self._merge_brief({}, brief)),
            ),
            )
            await conn.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                               " snapshot_json,created_at) VALUES (?,?,?,?,?,?)",
                               (task_id, 1, actor.kind, session_id or "",
                                json.dumps({"requirements": [], "checklist": [
                                    {"id": f"C{index}", "text": text[:200]} for index, text in enumerate(checklist or [], 1)],
                                    "acceptance": acceptance[:2000], "depends_on": deps,
                                    "brief": self._merge_brief({}, brief)},
                                    ensure_ascii=False), _now()))
            await conn.execute("INSERT INTO workflow_steps(id,task_id,step_kind,state,contract_revision)"
                               " VALUES (?,?, 'work','pending',1)", (f"task:{task_id}:work", task_id))
            for dependency in deps:
                await conn.execute("INSERT INTO task_dependency_edges(id,successor_task_id,predecessor_task_id,kind,"
                                   " resolution_state,created_at) VALUES (?,?,?,'required','awaiting_result',?)",
                                   (uuid.uuid4().hex, task_id, dependency, _now()))
        await self.export_plan(session_id)
        task = await self.get(task_id)
        await self._publish("task.created", task, actor, to=status)
        if assignee_staff_id:
            await self._publish("task.assigned", task, actor)
        return task

    # -- who acts, and on which board -------------------------------------------------------

    async def actor_of(self, session_id: str | None) -> Actor:
        """What a session is to the board: the project's orchestrator, a staff member, or an ordinary agent.

        A staff session is known by its ``staff_sessions`` row, which outlives the live period, so a
        staff member's session keeps its project's board after the session ended. The orchestrator is
        known by the one session id its project's settings name, so a replaced orchestrator loses the
        project's board with its office.
        """
        if session_id is None:
            return OPERATOR
        row = await self.app.db.fetchone(
            "SELECT m.id AS staff_id, m.project_id FROM staff_sessions s JOIN staff m ON m.id = s.staff_id WHERE s.session_id = ? ORDER BY s.started_at DESC LIMIT 1",
            (session_id,),
        )
        if row is not None:
            return Actor("staff", session_id, row["project_id"], row["staff_id"])
        row = await self.app.db.fetchone("SELECT id FROM projects WHERE json_extract(settings, '$.orchestrator.session_id') = ?", (session_id,))
        if row is not None:
            return Actor("orchestrator", session_id, row["id"])
        return Actor("agent", session_id)

    async def _project_of_session(self, session_id: str) -> str | None:
        row = await self.app.db.fetchone("SELECT project_id FROM sessions WHERE id = ?", (session_id,))
        return str(row["project_id"]) if row is not None and row["project_id"] else None

    async def _check_deps(self, deps: list[str], *, project_id: str | None, actor: str | None) -> None:
        """A dependency is a task on the same board: the actor's, and the same project's when there is one."""
        for dep in deps:
            try:
                found = await self.get(dep, actor=actor)
            except KeyError:
                raise ValueError(f"unknown dependency {dep}") from None
            if project_id is not None and found.get("project_id") != project_id:
                raise ValueError(f"task {dep} is on another project's board")

    async def _assignee(self, project_id: str | None, staff_id: str) -> None:
        if project_id is None:
            raise ValueError("only a task on a project's board can be assigned to a staff member")
        row = await self.app.db.fetchone("SELECT name, project_id, archived_at FROM staff WHERE id = ?", (staff_id,))
        if row is None or row["project_id"] != project_id:
            raise ValueError("that staff member is not on this project's team")
        if row["archived_at"] is not None:
            raise ValueError(f"{row['name']} has been dismissed")

    @staticmethod
    def _merge_brief(current: dict[str, str], change: dict[str, str] | None) -> dict[str, str]:
        merged = _brief(current)
        for key, value in (change or {}).items():
            if key not in BRIEF_FIELDS:
                raise ValueError(f"a task brief has {', '.join(BRIEF_FIELDS)}, not {key!r}")
            merged[key] = str(value or "").strip()[:BRIEF_MAX_CHARS]
        return merged

    async def _publish(self, event_type: str, task: dict[str, Any], actor: Actor, **extra: Any) -> None:
        """Tell the bus. A board change is the operator's or an agent's work and stands whether or not the
        event could be written, so a bus that fails is logged and nothing else."""
        manager = self.app.manager
        bus = getattr(manager, "bus", None) if manager is not None else None
        if bus is None:
            return
        payload: dict[str, Any] = {"task_id": task["id"], "title": task["title"], "actor": actor.kind, **extra}
        if task.get("assignee_staff_id"):
            payload["assignee_staff_id"] = task["assignee_staff_id"]
        try:
            await bus.publish(event_type, payload, project_id=task.get("project_id"), staff_id=task.get("assignee_staff_id") or actor.staff_id, session_id=actor.session_id)
        except Exception:  # noqa: BLE001
            logger.warning("could not publish %s for task %s", event_type, task["id"], exc_info=True)

    async def family(self, session_id: str) -> list[str]:
        """The session whose board it is and the subagents that share it: a subagent works on its leader's board."""
        leader = session_id
        row = await self.app.db.fetchone("SELECT metadata FROM sessions WHERE id = ?", (session_id,))
        if row is not None:
            try:
                leader = str(json.loads(row["metadata"] or "{}").get("subagent_of") or session_id)
            except (TypeError, ValueError):
                leader = session_id
        subs = await self.app.db.fetchall("SELECT id FROM sessions WHERE metadata LIKE ?", (f'%"subagent_of": "{leader}"%',))
        return [leader, *(str(r["id"]) for r in subs if str(r["id"]) != leader)]

    async def _scope(self, actor: str | None) -> tuple[str, tuple[Any, ...]]:
        """The SQL that narrows a query to what ``actor`` may see.

        Staff and the orchestrator see their project's board. Any other agent sees its family's tasks
        and the operator's unaddressed ones outside every project: a task the operator put on a
        project's board is the team's, not every agent's in that project.
        """
        if actor is None:
            return "1", ()
        who = await self.actor_of(actor)
        if who.kind in ("staff", "orchestrator") and who.project_id is not None:
            return "project_id = ?", (who.project_id,)
        family = await self.family(actor)
        return f"((origin_session_id IS NULL AND project_id IS NULL) OR origin_session_id IN ({','.join('?' for _ in family)}))", tuple(family)

    async def get(self, task_id: str, *, actor: str | None = None) -> dict[str, Any]:
        """One task; for an agent (``actor``) a task outside its board does not exist."""
        where, args = await self._scope(actor)
        row = await self.app.db.fetchone(f"SELECT * FROM board_tasks WHERE id = ? AND {where}", (task_id, *args))
        if row is None:
            raise KeyError(task_id)
        return self._view(dict(row))

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        row["checklist"] = json.loads(row.get("checklist") or "[]")
        row["depends_on"] = json.loads(row.get("depends_on") or "[]")
        row["brief"] = _brief(row.pop("brief_json", None))
        return row

    async def list(self, status: str | None = None, *, include_done: bool = True, actor: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        """The board as ``actor`` sees it; ``None`` is the operator, who sees every session's tasks.
        ``project_id`` narrows it to one project's board."""
        where, args = await self._scope(actor)
        if project_id is not None:
            where, args = f"{where} AND project_id = ?", (*args, project_id)
        if status:
            rows = await self.app.db.fetchall(f"SELECT * FROM board_tasks WHERE status = ? AND {where} ORDER BY priority, created_at", (status, *args))
        elif include_done:
            rows = await self.app.db.fetchall(f"SELECT * FROM board_tasks WHERE {where} ORDER BY {_ORDER}", args)
        else:
            rows = await self.app.db.fetchall(f"SELECT * FROM board_tasks WHERE status NOT IN ('done', 'dropped') AND {where} ORDER BY priority, created_at", args)
        return [self._view(dict(r)) for r in rows]

    async def _has_open_deps(self, deps: list[str]) -> bool:
        for dep in deps:
            row = await self.app.db.fetchone("SELECT status FROM board_tasks WHERE id = ?", (dep,))
            if row is not None and row["status"] not in ("done", "dropped"):
                return True
        return False

    async def update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        note: str = "",
        check: list[int] | None = None,
        uncheck: list[int] | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        title: str | None = None,
        acceptance: str | None = None,
        priority: int | None = None,
        actor: str | None = None,
        assignee_staff_id: str | None = None,
        brief: dict[str, str] | None = None,
        depends_on: list[str] | None = None,
    ) -> dict[str, Any]:
        """Change a task. ``actor`` is the session acting (its board bounds what it may touch; ``None`` is
        the operator); ``session_id`` names who claims the task on the way to 'doing' and defaults to the
        actor. ``assignee_staff_id`` assigns the task (``""`` takes it off whoever had it), ``brief``
        replaces the brief fields it names, and ``depends_on`` replaces the dependencies."""
        task = await self.get(task_id, actor=actor)
        who = await self.actor_of(actor)
        if task.get("accepted_result_id") and (
                status is not None or check or uncheck or title is not None or acceptance is not None
                or brief is not None or depends_on is not None or assignee_staff_id is not None):
            raise ValueError("an accepted task needs its exact-result reopen decision")
        if status and status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        moving = status is not None and status != task["status"]
        if moving and status == "done":
            raise ValueError("a task finishes only through exact-result acceptance")
        if who.kind == "staff":
            # A staff member works its own tasks and reports; the plan of the team is the orchestrator's.
            if moving and task.get("assignee_staff_id") != who.staff_id:
                raise ValueError("a staff member moves only the tasks assigned to them")
            if moving and status not in STAFF_MOVES:
                raise ValueError(f"a staff member takes a task to {', '.join(STAFF_MOVES)}; finishing it is the operator's acceptance")
            if assignee_staff_id is not None or brief is not None or depends_on is not None:
                raise ValueError("a staff member does not reassign or rewrite tasks; ask the orchestrator")
        if who.kind == "agent" and assignee_staff_id:
            raise ValueError("only the operator or the project's orchestrator assigns tasks")
        if assignee_staff_id:
            await self._assignee(task.get("project_id"), assignee_staff_id)
        new_brief = self._merge_brief(task["brief"], brief) if brief is not None else None
        deps = task["depends_on"]
        if depends_on is not None:
            deps = [d for d in dict.fromkeys(depends_on) if d]
            if task_id in deps or await self._reaches(deps, task_id):
                raise ValueError("a task cannot depend on itself, even through another task")
            await self._check_deps(deps, project_id=task.get("project_id"), actor=actor)
        claimant = session_id or actor
        if status == "doing" and task["status"] != "doing":
            if await self._has_open_deps(task["depends_on"]):
                raise ValueError("this task still has unfinished dependencies")
            if claimant is not None:
                # The limit rations one agent's attention, so it counts the tasks its family holds, not the
                # whole installation's: a second agent's three open tasks are not a reason to refuse this one.
                limit = self.app.config.board.wip_limit
                family = await self.family(claimant)
                row = await self.app.db.fetchone(f"SELECT count(*) c FROM board_tasks WHERE status = 'doing' AND session_id IN ({','.join('?' for _ in family)})", tuple(family))
                if row and int(row["c"]) >= limit:
                    raise ValueError(f"work-in-progress limit reached ({limit} tasks in 'doing'); finish or hand back one first")
        # Apply the caller's checklist edits before judging completeness: "I checked the last items,
        # close the task" is one call, and testing the checklist the call *arrived* to refused it.
        # Index precedence is unchanged — an index in both lists ends up unchecked.
        unchecked, checked = set(uncheck or []), set(check or [])
        checklist = [
            {**item, "done": False} if i in unchecked else {**item, "done": True} if i in checked else item
            for i, item in enumerate(task["checklist"])
        ]
        # A task may not be left finished with an item still open, whichever way this call moved:
        # closing it, or editing the checklist of a task that is already done. The gate judges the
        # checklist this call produces, and it runs before anything is written, so a refusal stores
        # nothing — the message has to say so, or the caller retries with the wrong indexes. A call
        # that does neither is let through: a card a member handed in sits in done with its checks
        # still to be marked, and a note added to it is not a claim that they are.
        closing = moving and status == "done"
        editing = not moving and task["status"] == "done" and bool(check or uncheck)
        if closing or editing:
            still_open = [i for i, c in enumerate(checklist) if not c["done"]]
            if still_open:
                named = ", ".join(f"{i} ({str(checklist[i].get('text', '')).strip()[:40]})" for i in still_open[:5])
                more = "" if len(still_open) <= 5 else f" and {len(still_open) - 5} more"
                remedy = (
                    "Repeat it with every index you want checked (check=[...], status='done'), or leave the "
                    "task in another status."
                    if status == "done"
                    else "Reopen the task first (status='todo' or 'doing') and edit its checklist there."
                )
                raise ValueError(f"the checklist is not complete: {named}{more}. Nothing was stored. {remedy}")
        notes = task["notes"] or ""
        if note:
            notes = (notes + "\n" if notes else "") + f"[{_now()[:16].replace('T', ' ')}] {note[:1000]}"
        # The claim belongs to whoever holds the task in 'doing'; leaving 'doing' releases it.
        if status == "doing":
            owner, owner_run = claimant or task["session_id"], run_id or task["run_id"]
        elif status:
            owner, owner_run = None, None
        else:
            owner, owner_run = task["session_id"], task["run_id"]
        new_status = status or task["status"]
        if depends_on is not None and new_status in ("todo", "blocked"):
            # New dependencies decide afresh whether a waiting task is ready.
            new_status = "blocked" if await self._has_open_deps(deps) else "todo"
        assignee = task.get("assignee_staff_id") if assignee_staff_id is None else (assignee_staff_id or None)
        async with self.app.db.transaction() as conn:
            await conn.execute(
                "UPDATE board_tasks SET status = ?, notes = ?, checklist = ?, session_id = ?, run_id = ?,"
                " title = COALESCE(?, title), acceptance = COALESCE(?, acceptance), priority = COALESCE(?, priority), updated_at = ?, heartbeat_at = ?,"
                " assignee_staff_id = ?, brief_json = COALESCE(?, brief_json), depends_on = ?,"
                " entity_revision = entity_revision + 1 WHERE id = ?",
                (
                    new_status, notes[-NOTES_MAX_CHARS:], json.dumps(checklist), owner, owner_run, title, acceptance, priority, _now(), _now(),
                    assignee, json.dumps(new_brief) if new_brief is not None else None, json.dumps(deps), task_id,
                ),
            )
            await capture_contract_change(conn, task_id, origin_kind=who.kind, origin_ref=actor or "")
            if depends_on is not None and deps != task["depends_on"]:
                await conn.execute("UPDATE task_dependency_edges SET kind = 'cancelled', resolution_state = 'cancelled'"
                                   " WHERE successor_task_id = ? AND resolution_state != 'cancelled'", (task_id,))
                for dependency in deps:
                    await conn.execute("INSERT INTO task_dependency_edges(id,successor_task_id,predecessor_task_id,kind,"
                                       " resolution_state,created_at) VALUES (?,?,?,'required','awaiting_result',?)",
                                       (uuid.uuid4().hex, task_id, dependency, _now()))
        if moving and who.kind == "operator" and task["status"] in ("review", "done") and new_status in ("todo", "doing") and task.get("project_id"):
            # The operator taking a card back out of review is their verdict on the result.
            await self.app.db.execute("UPDATE board_tasks SET acceptance_state = 'returned' WHERE id = ?", (task_id,))
        updated = await self.get(task_id)
        # The move is announced before what it causes, so a reader sees "done" and then the dependents
        # it made ready, in that order.
        if new_status != task["status"]:
            await self._publish("task.moved", updated, who, **{"from": task["status"], "to": new_status})
        if assignee != task.get("assignee_staff_id"):
            await self._publish("task.assigned", updated, who)
        if status in ("done", "dropped"):
            await self._promote_dependents(task_id)
        elif status and task["status"] in ("done", "dropped"):
            await self._demote_dependents()
        await self.export_plan(task.get("origin_session_id") or session_id or task.get("session_id"))
        return updated

    async def _reaches(self, starts: list[str], target: str) -> bool:
        """Whether ``target`` is among the dependencies of ``starts``, followed all the way down."""
        seen: set[str] = set()
        frontier = list(starts)
        while frontier:
            current = frontier.pop()
            if current == target:
                return True
            if current in seen or len(seen) > 500:
                continue
            seen.add(current)
            row = await self.app.db.fetchone("SELECT depends_on FROM board_tasks WHERE id = ?", (current,))
            if row is not None:
                frontier.extend(json.loads(row["depends_on"] or "[]"))
        return False

    async def needs_you(self, project_id: str) -> list[dict[str, Any]]:
        """What in this project waits on the operator: the open requests routed to them, oldest first.

        Derived at read time from the requests themselves and never stored on a task, so it cannot
        disagree with them: an answer from anywhere (the app, Telegram, a push action) ends it.
        Each carries the session where it can be answered today, when there is one.
        """
        manager = self.app.manager
        asks_store = getattr(manager, "asks", None) if manager is not None else None
        if asks_store is None:
            return []
        asks = await asks_store.open_for(project_id, routed_to="operator")
        if not asks:
            return []
        staff = {r["id"]: dict(r) for r in await self.app.db.fetchall("SELECT id, name, color, harness FROM staff WHERE project_id = ?", (project_id,))}
        titles = {r["id"]: r["title"] for r in await self.app.db.fetchall("SELECT id, title FROM board_tasks WHERE project_id = ?", (project_id,))}
        row = await self.app.db.fetchone("SELECT json_extract(settings, '$.orchestrator.session_id') AS sid FROM projects WHERE id = ?", (project_id,))
        orchestrator = str(row["sid"]) if row is not None and row["sid"] else None
        out: list[dict[str, Any]] = []
        for ask in asks:
            where: str | None = orchestrator if ask.origin == "orchestrator" else None
            if ask.staff_session_id:
                session = await self.app.db.fetchone("SELECT session_id FROM staff_sessions WHERE id = ?", (ask.staff_session_id,))
                where = session["session_id"] if session is not None else None
            view = ask.view()
            view.update(staff=staff.get(ask.staff_id or ""), task_title=titles.get(ask.task_id or ""), session_id=where)
            out.append(view)
        return out

    async def project_board(self, project_id: str, *, include_done: bool = False) -> dict[str, Any]:
        """A project's board as the app draws it: the tasks with their assignees, "Needs you", and the counts."""
        tasks = await self.list(None, include_done=include_done, project_id=project_id)
        members = {r["id"]: dict(r) for r in await self.app.db.fetchall("SELECT id, name, color, harness, isolation, archived_at FROM staff WHERE project_id = ?", (project_id,))}
        live = {
            r["staff_id"]: dict(r)
            for r in await self.app.db.fetchall(
                "SELECT s.staff_id, s.status, s.waiting_for, s.status_at, s.session_id, s.task_id, s.branch FROM staff_sessions s JOIN staff m ON m.id = s.staff_id WHERE m.project_id = ? AND s.ended_at IS NULL",
                (project_id,),
            )
        }
        for task in tasks:
            member = members.get(task.get("assignee_staff_id") or "")
            if member is None:
                task["assignee"] = None
                continue
            session = live.get(member["id"])
            # The member's live status speaks for this task only while that session works on it.
            on_it = session is not None and session["task_id"] == task["id"]
            task["assignee"] = {
                **member,
                "status": session["status"] if session is not None else "off",
                "on_task": on_it,
                "waiting_for": session["waiting_for"] if on_it and session is not None else "",
                "status_at": session["status_at"] if on_it and session is not None else None,
                "session_id": session["session_id"] if session is not None else None,
            }
        await self._contracts(tasks)
        needs = await self.needs_you(project_id)
        counts_rows = await self.app.db.fetchall("SELECT status, count(*) AS n FROM board_tasks WHERE project_id = ? GROUP BY status", (project_id,))
        counts = {status: 0 for status in STATUSES}
        counts.update({r["status"]: int(r["n"]) for r in counts_rows if r["status"] in counts})
        team = [{"id": m["id"], "name": m["name"], "color": m["color"], "harness": m["harness"], "isolation": m["isolation"]} for m in members.values() if m["archived_at"] is None]
        return {"tasks": tasks, "needs_you": needs, "counts": {**counts, "needs_you": len(needs)}, "staff": team}

    async def _contracts(self, tasks: list[dict[str, Any]]) -> None:
        """Each card's requirements, with who was given each and whether they confirmed or opened it."""
        contracts = Contracts(self.app.db)
        found = await contracts.of_tasks([t["id"] for t in tasks])
        for task in tasks:
            requirements = found.get(task["id"]) or []
            deliveries = await contracts.deliveries(task["id"]) if requirements else {}
            names = {f.id: f.name for f in await self._files(task["id"])} if any(r.file_id for r in requirements) else {}
            task["requirements"] = [
                {**r.view(), "file_name": names.get(r.file_id or "", ""), "deliveries": deliveries.get(r.id) or []} for r in requirements
            ]

    async def _files(self, task_id: str) -> list[Any]:
        manager = self.app.manager
        store = getattr(manager, "files", None) if manager is not None else None
        return list(await store.of_task(task_id)) if store is not None else []

    async def delete(self, task_id: str) -> bool:
        row = await self.app.db.fetchone("SELECT id FROM board_tasks WHERE id = ?", (task_id,))
        if row is None:
            return False
        if await self.app.db.fetchone("SELECT 1 FROM result_receipts WHERE task_id = ? LIMIT 1", (task_id,)):
            raise ValueError("a task with audited results cannot be deleted; archive it instead")
        if await self.app.db.fetchone("SELECT 1 FROM execution_attempts WHERE task_id = ? LIMIT 1", (task_id,)):
            raise ValueError("a task with execution history cannot be deleted; archive it instead")
        if await self.app.db.fetchone("SELECT 1 FROM comparison_groups WHERE task_id = ? LIMIT 1", (task_id,)):
            raise ValueError("a task with comparison history cannot be deleted; archive it instead")
        owner = await self.app.db.fetchone("SELECT origin_session_id FROM board_tasks WHERE id = ?", (task_id,))
        await self.app.db.execute("DELETE FROM board_tasks WHERE id = ?", (task_id,))
        await self.export_plan(owner["origin_session_id"] if owner else None)
        waited: list[str] = []
        for row in await self.app.db.fetchall("SELECT id, depends_on FROM board_tasks WHERE depends_on LIKE ?", (f"%{task_id}%",)):
            deps = [d for d in json.loads(row["depends_on"] or "[]") if d != task_id]
            await self.app.db.execute("UPDATE board_tasks SET depends_on = ? WHERE id = ?", (json.dumps(deps), row["id"]))
            waited.append(row["id"])
        await self._promote(waited)
        return True

    async def _promote_dependents(self, finished: str) -> list[str]:
        """The tasks that waited on ``finished`` and wait on nothing else now become ready.

        Only the tasks that named it: this once went through every blocked task of the installation
        whenever anything finished, and a card the orchestrator had set aside as blocked by hand —
        for a reason no dependency expresses — was put back to todo by the board four times in an
        hour, each time a task somewhere else was done.
        """
        rows = await self.app.db.fetchall("SELECT id FROM board_tasks WHERE status = 'blocked' AND depends_on LIKE ?", (f'%"{finished}"%',))
        return await self._promote([r["id"] for r in rows])

    async def _promote(self, candidates: list[str]) -> list[str]:
        promoted: list[str] = []
        for task_id in candidates:
            row = await self.app.db.fetchone("SELECT id, depends_on FROM board_tasks WHERE id = ? AND status = 'blocked'", (task_id,))
            if row is None:
                continue
            deps = json.loads(row["depends_on"] or "[]")
            if not await self._has_open_deps(deps):
                await self.app.db.execute("UPDATE board_tasks SET status = 'todo', updated_at = ? WHERE id = ?", (_now(), row["id"]))
                promoted.append(row["id"])
                await self._publish("task.moved", await self.get(row["id"]), Actor("system"), **{"from": "blocked", "to": "todo"})
        return promoted

    async def _demote_dependents(self) -> list[str]:
        """A ready task whose dependency was reopened is blocked again."""
        demoted: list[str] = []
        for row in await self.app.db.fetchall("SELECT id, depends_on FROM board_tasks WHERE status = 'todo'"):
            deps = json.loads(row["depends_on"] or "[]")
            if deps and await self._has_open_deps(deps):
                await self.app.db.execute("UPDATE board_tasks SET status = 'blocked', updated_at = ? WHERE id = ?", (_now(), row["id"]))
                demoted.append(row["id"])
                await self._publish("task.moved", await self.get(row["id"]), Actor("system"), **{"from": "todo", "to": "blocked"})
        return demoted

    async def recover_stale(self) -> list[str]:
        """Hand back 'doing' tasks whose session has been quiet for longer than the stale window."""
        manager = self.app.manager
        hours = self.app.config.board.stale_hours
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        handed: list[str] = []
        # An assigned task is the staff runtime's to track: its member may be thinking for hours
        # without touching the board, and handing it back would start a second worker on it.
        for row in await self.app.db.fetchall("SELECT id, title, session_id, heartbeat_at, updated_at FROM board_tasks WHERE status = 'doing' AND assignee_staff_id IS NULL"):
            last = row["heartbeat_at"] or row["updated_at"]
            try:
                seen = datetime.fromisoformat(last)
                seen = seen if seen.tzinfo else seen.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                seen = cutoff
            if seen >= cutoff:
                continue
            state = await manager.get_state(row["session_id"]) if manager is not None and row["session_id"] else None
            if state is not None and (state.running or state.pending is not None):
                continue
            await self.app.db.execute("UPDATE board_tasks SET status = 'todo', session_id = NULL, run_id = NULL, updated_at = ?, notes = substr(notes || ?, ?) WHERE id = ?", (_now(), f"\n[{_now()[:16].replace('T', ' ')}] handed back: no activity for {hours} h", -NOTES_MAX_CHARS, row["id"]))
            handed.append(row["id"])
            await self._publish("task.moved", await self.get(row["id"]), Actor("system"), **{"from": "doing", "to": "todo"})
            if self.app.notifications is not None:
                await self.app.notifications.post(Draft("system", f"Task '{row['title']}' handed back", f"No activity for {hours} h; it is 'todo' again.", kind="board_stale", level="quiet", session_id=row["session_id"], source="board"))
        return handed

    async def touch(self, session_id: str) -> None:
        await self.app.db.execute("UPDATE board_tasks SET heartbeat_at = ? WHERE status = 'doing' AND session_id = ?", (_now(), session_id))

    async def loop(self) -> None:
        while True:
            try:
                await self.recover_stale()
            except Exception:  # noqa: BLE001
                logger.exception("board recovery failed")
            await asyncio.sleep(TICK_SECONDS)

    async def export_plan(self, session_id: str | None) -> None:
        """PLAN.md in the session's workspace: the board's rows for that session as a readable, diffable file.

        The board stays the plan of record (it survives compaction and restarts); the file is its
        rendering where the operator and the agent read files, regenerated on every change.
        """
        if not session_id or self.app.manager is None:
            return
        state = self.app.manager.live_state(session_id)  # a session that is not live gets no file; nothing is resurrected for it
        if state is None:
            return
        # A subagent shares its leader's workspace: the file there lists the whole family's tasks.
        family = await self.family(session_id)
        leader = family[0]
        placeholders = ",".join("?" for _ in family)
        rows = await self.app.db.fetchall(f"SELECT * FROM board_tasks WHERE origin_session_id IN ({placeholders}) ORDER BY CASE status WHEN 'doing' THEN 0 WHEN 'review' THEN 1 WHEN 'todo' THEN 2 WHEN 'blocked' THEN 3 WHEN 'done' THEN 4 ELSE 5 END, priority, created_at", tuple(family))
        tasks = [self._view(dict(r)) for r in rows]
        lines = ["# Plan", "", f"Board tasks created by session {leader}" + (" and its subagents" if len(family) > 1 else "") + "; edit them with the Board tools, this file is regenerated.", ""]
        for t in tasks:
            box = {"done": "x", "dropped": "-"}.get(t["status"], " ")
            deps = f" (after {', '.join(t['depends_on'])})" if t["depends_on"] else ""
            lines.append(f"- [{box}] **{t['id']}** {t['title']} — {t['status']}, priority {t['priority']}{deps}")
            if t.get("acceptance"):
                lines.append(f"  - done when: {t['acceptance']}")
            for item in t["checklist"]:
                lines.append(f"  - [{'x' if item['done'] else ' '}] {item['text']}")
        try:
            await asyncio.to_thread((state.workspace / "PLAN.md").write_text, "\n".join(lines) + "\n", "utf-8")
        except OSError:
            logger.warning("could not write PLAN.md for session %s", session_id, exc_info=True)

    def render(self, tasks: list[dict[str, Any]], *, limit: int = 30) -> str:
        icons = {"todo": "▫️", "doing": "🔵", "review": "🟡", "done": "✅", "blocked": "⛔", "dropped": "✖️"}
        lines = []
        for t in tasks[:limit]:
            done = sum(1 for c in t["checklist"] if c["done"])
            extra = f" [{done}/{len(t['checklist'])}]" if t["checklist"] else ""
            deps = f" ← {', '.join(t['depends_on'])}" if t["depends_on"] else ""
            lines.append(f"{icons.get(t['status'], '·')} {t['id']} p{t['priority']} {t['title']}{extra}{deps}")
        if len(tasks) > limit:
            lines.append(f"… {len(tasks) - limit} more")
        return "\n".join(lines) or "(the board is empty)"

    async def service(self, op: str, **kwargs: Any) -> Any:
        if op == "list":
            return await self.list(kwargs.get("status"), include_done=bool(kwargs.get("include_done", False)), actor=kwargs.get("actor"), project_id=kwargs.get("project_id"))
        if op == "needs_you":
            return await self.needs_you(kwargs["project_id"])
        if op == "get":
            return await self.get(kwargs["task_id"], actor=kwargs.get("actor"))
        if op == "render":
            return self.render(await self.list(kwargs.get("status"), include_done=bool(kwargs.get("include_done", False)), actor=kwargs.get("actor")))
        raise ValueError(op)


async def install(app: Application) -> list[asyncio.Task[None]]:
    board = Board(app)
    app.extensions["board"] = board
    assert app.manager is not None
    app.manager.service_hooks["board"] = board.service

    async def command_service(op: str, *, session_id: str, task_id: str | None = None,
                              client_operation_id: str = "", expected_collection_revision: int | None = None,
                              expected_entity_revision: int | None = None, **kwargs: Any) -> Any:
        authority = app.extensions.get("board_tool_authority")
        if authority is None or (op != "revision" and not client_operation_id):
            raise PermissionError("board writes require a host grant and trusted call id")
        principal, scope = await authority(session_id, "board.task.create" if op in ("add", "revision") else "board.task.update",
                                           task_id=task_id)
        if not isinstance(principal, Principal) or not isinstance(scope, Scope):
            raise PermissionError("board authority did not return a host identity and scope")
        if op == "revision":
            return await ControlStore(app.db).revision(scope, Entity("collection", scope.id))
        commands = BoardCommands(app.db, bus=app.manager.bus)
        if op == "add":
            if expected_collection_revision is None:
                raise ValueError("expected_collection_revision is required")
            created = await commands.create(principal, scope, client_operation_id=client_operation_id,
                                            expected_collection_revision=expected_collection_revision,
                                            source_session_id=session_id, **kwargs)
            return await board.get(created["task_id"], actor=session_id)
        if op == "update":
            if task_id is None or expected_entity_revision is None:
                raise ValueError("task_id and expected_entity_revision are required")
            updated = await commands.update(principal, scope, task_id,
                                            client_operation_id=client_operation_id,
                                            expected_entity_revision=expected_entity_revision, **kwargs)
            return await board.get(updated["task_id"], actor=session_id)
        raise ValueError(op)

    app.manager.service_hooks["board_commands"] = command_service

    async def touch_on_finish(session_id: str, run_id: str, status: str) -> None:
        await board.touch(session_id)

    app.manager.on_finished(touch_on_finish)
    front = app.front
    if front is not None:

        async def cmd_board(message, command) -> None:  # type: ignore[no-untyped-def]
            arg = (command.args or "").strip().lower()
            include_done = arg == "all"
            tasks = await board.list(None, include_done=include_done)
            await message.answer(("📋 Board\n" + board.render(tasks) + "\n\n/board all shows finished tasks too")[:4000])

        front.command_hooks["board"] = cmd_board
    return [asyncio.create_task(board.loop(), name="board")]


__all__ = ["BRIEF_FIELDS", "STAFF_MOVES", "STATUSES", "Actor", "Board", "install"]
