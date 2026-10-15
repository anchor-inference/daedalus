"""Speculative attempts: two or three members race at one board task, and the orchestrator keeps one.

Each attempt is an ordinary card of its own, a copy of the task's brief, worked by a one-off member in
a worktree and on a branch of its own, so its launch, its report and its review take the paths every
card takes and stay fenced to that card's own execution. A Daedalus attempt starts from its source
member's conversation (:meth:`SessionManager.fork_into`), with the source's prompt identity pinned, so
every attempt is served the shared history from the provider's cache and pays only for its own tail.
A command-line attempt is a fresh member of the runtime and model the orchestrator names.

What is recorded comes first. The exploration and its attempts are written, with the budget held
against the project's goal budget, before anything is hired; each attempt is marked ``launching``
before its member is hired and ``running`` once its card is handed over. A restart that finds an
attempt still ``launching`` says so and does not launch it again: whether its member started is not
known, and a second launch could leave two members on one card.

Once every attempt has reported, or the deadline or the budget has passed, the orchestrator is woken
once with the attempts side by side and picks a winner, or none. The winner's card goes on to review
and to the merge like any other card, the cards that waited on the task wait on the winner's, and
the other attempts' members are dismissed, their worktrees removed and their branches parked under
``refs/daedalus/explored/`` so the work is out of the way but not destroyed. Picking twice the same
way is a no-op that finishes whatever cleanup the first pick could not.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from daedalus.extensions.board_commands import BoardCommands
from daedalus.extensions.orchestrator_domain import record_verdict, return_result
from daedalus.extensions.orchestrator_ops import Refused, board_principal
from daedalus.extensions.orchestrator_team import (
    CONTRACT_FIELDS,
    CONTRACT_MIN,
    _journal,
    _label,
    _member,
    previous_owner,
)
from daedalus.extensions.task_contract import split_checks
from daedalus.host.gitrun import GitError
from daedalus.host.worktrees import WorktreeError
from daedalus.stores.control import ControlStore, Entity, Scope, now, one
from daedalus.stores.explorations import spent_by_attempt_in
from daedalus.stores.goal_budget import goal_constraints_in, usd
from daedalus.stores.inference_budget import _historical, held_in, microusd

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.extensions.orchestrator import Orchestrators
    from daedalus.stores.projects import Project, ProjectFolder
    from daedalus.stores.staff import Staff

logger = logging.getLogger(__name__)

OPEN = ("launching", "running", "ready")
"""An exploration not yet decided: its task belongs to it."""
TERMINAL = ("reported", "failed", "unknown", "cancelled")
"""Attempts with nothing more to come from them before the decision."""
ATTEMPTS = (2, 3)
SWEEP_SECONDS = 30.0
"""How often deadlines, budgets and attempts that ended without a report are looked at."""
EXCERPT_CHARS = 240
WHY_MIN = 8
PARKED = "refs/daedalus/explored"
"""Where a losing attempt's branch is kept once the branch itself is deleted."""


def _exploration_id(project_id: str, client_operation_id: str) -> str:
    """Derived from the tool call, so the same call repeated finds the exploration it already made."""
    return "x" + hashlib.sha256(f"{project_id}:{client_operation_id}".encode()).hexdigest()[:10]


def _one_line(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _later(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


class Explorations:
    def __init__(self, app: Application) -> None:
        self.app = app
        self._lock = asyncio.Lock()

    @property
    def db(self) -> Any:
        return self.app.manager.db

    def _orch(self) -> Orchestrators:
        orch = self.app.extensions.get("orchestrator")
        if orch is None:
            raise RuntimeError("orchestrators are not installed")
        return orch  # type: ignore[no-any-return]

    # -- starting ----------------------------------------------------------------------------------

    async def start(self, orch: Orchestrators, project: Project, session_id: str, *, task_id: str,
                    attempts: int, staff: str | None, variants: list[dict[str, Any]] | None,
                    budget_usd: float | str | None, deadline_minutes: int | None,
                    client_operation_id: str) -> str:
        if not client_operation_id:
            raise Refused("Explore requires a trusted tool call id")
        exploration_id = _exploration_id(project.id, client_operation_id)
        if await self.db.fetchone("SELECT 1 FROM explorations WHERE id = ?", (exploration_id,)) is not None:
            return await self.status_text(project, exploration_id)
        if type(attempts) is not int or attempts not in ATTEMPTS:
            raise Refused("attempts is 2 or 3")
        wanted = list(variants or [])
        if len(wanted) > attempts or any(not isinstance(item, dict) for item in wanted):
            raise Refused("variants is a list of at most one {harness, model, approach} per attempt")
        board = orch.board
        team = orch.team
        if board is None or team is None:
            raise Refused("the board and the staff runtime are needed for an exploration")
        try:
            task = await board.get(task_id, actor=session_id)
        except KeyError as exc:
            raise Refused(f"no task {task_id} on {project.name}'s board; Tasks() lists them") from exc
        if task.get("project_id") != project.id:
            raise Refused("the task is outside this orchestrator's project")
        if await self.db.fetchone("SELECT 1 FROM exploration_attempts WHERE task_id = ?", (task_id,)) is not None:
            raise Refused(f"{task_id} is itself an attempt of an exploration; explore the task it copies")
        if task["status"] not in ("todo", "blocked") or task.get("current_attempt_id"):
            raise Refused(f"{task_id} is {task['status']}: an exploration starts from a card nobody is working, in todo or blocked")
        open_one = await self.db.fetchone("SELECT id FROM explorations WHERE task_id = ? AND state IN (?,?,?)",
                                          (task_id, *OPEN))
        if open_one is not None:
            raise Refused(f"{task_id} is already being explored in {open_one['id']}; Explore(op='status', exploration='{open_one['id']}')")
        brief = {key: str(task.get("brief", {}).get(key) or "").strip() for key in CONTRACT_FIELDS}
        short = [key.replace("_", "-") for key, value in brief.items() if len(value) < CONTRACT_MIN]
        if short:
            raise Refused(f"the card has no usable {', '.join(short)}; every attempt is handed the whole brief, so write it first")
        folder = self._folder(project, task.get("folder_id"))
        if folder is None or folder.readonly:
            raise Refused("an exploration needs a writable project folder: each attempt works on a branch of its own")
        try:
            await team.worktrees.check(folder)
        except WorktreeError as exc:
            raise Refused(f"{folder.path} cannot hold worktrees ({exc}); each attempt needs a branch of its own") from exc
        source = await _member(orch, project, staff) if staff else await self._source(orch, task)
        if source is None and not any(item.get("harness") for item in wanted):
            raise Refused("name staff= whose setup the attempts copy, or a harness for each attempt in variants")
        await board_principal(orch, session_id, project.id, "board.task.create")
        config = self.app.manager.config.orchestrator
        if budget_usd is None:
            budget = microusd(config.explore_usd_per_attempt) * attempts
        else:
            try:
                budget = microusd(budget_usd)
            except ValueError as exc:
                raise Refused(str(exc)) from exc
            if budget <= 0:
                raise Refused("budget_usd must be more than nothing")
        minutes = deadline_minutes if deadline_minutes is not None else config.explore_deadline_minutes
        if type(minutes) is not int or not 5 <= minutes <= 1440:
            raise Refused("deadline_minutes is between 5 and 1440")
        fork = await self._fork_source(source)
        plans = []
        for slot in range(1, attempts + 1):
            variant = wanted[slot - 1] if slot <= len(wanted) else {}
            harness = str(variant.get("harness") or (source.harness if source is not None else "")).strip().lower()
            if harness == "native":
                harness = "daedalus"
            model = str(variant.get("model") or (source.model if source is not None and harness == source.harness else "")).strip()
            approach = " ".join(str(variant.get("approach") or "").split())[:1000]
            plans.append((slot, harness, model, approach,
                          fork if harness == "daedalus" and source is not None and source.harness == "daedalus" else None))
        at = now()
        async with self.db.transaction() as conn:
            goal = await conn.execute("SELECT budget_id FROM project_goal_budgets WHERE project_id = ?", (project.id,))
            goal_row = await goal.fetchone()
            await goal.close()
            for limit in await goal_constraints_in(conn, project.id, coordinator=False):
                spent = await _historical(conn, limit)
                held, _ = await held_in(conn, limit)
                free = max(0, limit.cap_microusd - spent - held)
                if budget > free:
                    raise Refused(
                        f"the project's goal budget has ${usd(free)} free and {attempts} attempts reserve ${usd(budget)}: "
                        "explore with fewer attempts or a smaller budget_usd, or ask the operator to raise the budget"
                    )
            try:
                await conn.execute(
                    "INSERT INTO explorations(id,project_id,task_id,orchestrator_session_id,source_staff_id,state,"
                    " budget_microusd,goal_budget_id,deadline_at,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,'launching',?,?,?,?,?)",
                    (exploration_id, project.id, task_id, session_id, source.id if source is not None else None,
                     budget, goal_row["budget_id"] if goal_row is not None else None, _later(minutes), at, at),
                )
            except Exception as exc:  # noqa: BLE001 — the one-open-per-task index is the race's arbiter
                raise Refused(f"{task_id} is already being explored") from exc
            for slot, harness, model, approach, fork_from in plans:
                await conn.execute(
                    "INSERT INTO exploration_attempts(exploration_id,slot,harness,model,approach,fork_session_id,"
                    " state,created_at,updated_at) VALUES (?,?,?,?,?,?,'planned',?,?)",
                    (exploration_id, slot, harness, model, approach, fork_from, at, at),
                )
        lines = []
        for slot, *_ in plans:
            lines.append(await self._launch(orch, project, session_id, exploration_id, slot, task, folder, source))
        running = await self.db.fetchone("SELECT count(*) AS n FROM exploration_attempts WHERE exploration_id = ?"
                                         " AND state = 'running'", (exploration_id,))
        if running is None or not running["n"]:
            await self._set_state(exploration_id, "abandoned", from_states=("launching",),
                                  extra={"decision_reason": "no attempt could start"})
            cleanup = await self._clean_up(orch, project, session_id, exploration_id, winner=None,
                                           why="no attempt could start")
            return "No attempt could start, so nothing is explored:\n" + "\n".join(lines + cleanup)
        await self._set_state(exploration_id, "running", from_states=("launching",))
        await self._park_task(orch, project, session_id, task_id, exploration_id)
        await _journal(orch, project, "decision",
                       f"The orchestrator explores {task_id} \"{task['title']}\" with {attempts} competing attempts ({exploration_id}).",
                       {"task_id": task_id, "exploration_id": exploration_id})
        return (f"Exploring {task_id} in {exploration_id}, ${usd(budget)} held, deadline in {minutes} min:\n"
                + "\n".join(lines)
                + "\nYou are woken once, when every attempt has reported or the deadline or budget passes; "
                  "their reports do not wake you one by one. Explore(op='status', exploration=…) shows them meanwhile.")

    async def _source(self, orch: Orchestrators, task: dict[str, Any]) -> Staff | None:
        owner = await previous_owner(orch, task["id"])
        if owner is not None and owner.active:
            return owner
        if task.get("assignee_staff_id"):
            assignee = await orch.manager.staff.get(task["assignee_staff_id"])
            if assignee is not None and assignee.active:
                return assignee
        return None

    async def _fork_source(self, source: Staff | None) -> str | None:
        """The source member's latest Daedalus session, which every native attempt continues."""
        if source is None or source.harness != "daedalus":
            return None
        row = await self.db.fetchone("SELECT session_id FROM staff_sessions WHERE staff_id = ? AND kind = 'daedalus'"
                                     " AND session_id IS NOT NULL ORDER BY started_at DESC LIMIT 1", (source.id,))
        return str(row["session_id"]) if row is not None else None

    @staticmethod
    def _folder(project: Project, folder_id: str | None) -> ProjectFolder | None:
        for folder in project.folders:
            if folder.id == folder_id:
                return folder
        return project.primary

    async def _attempt(self, exploration_id: str, slot: int) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM exploration_attempts WHERE exploration_id = ? AND slot = ?",
                                     (exploration_id, slot))
        if row is None:
            raise KeyError(f"{exploration_id}:{slot}")
        return dict(row)

    async def _mark(self, exploration_id: str, slot: int, state: str, *, from_states: tuple[str, ...],
                    **fields: Any) -> bool:
        assignments = ",".join(f"{name} = ?" for name in ("state", "updated_at", *fields))
        marks = ",".join("?" for _ in from_states)
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE exploration_attempts SET {assignments} WHERE exploration_id = ? AND slot = ?"  # noqa: S608 — fixed names
                f" AND state IN ({marks})", (state, now(), *fields.values(), exploration_id, slot, *from_states))
            changed = cursor.rowcount
            await cursor.close()
        return changed == 1

    async def _set_state(self, exploration_id: str, state: str, *, from_states: tuple[str, ...],
                         extra: dict[str, Any] | None = None) -> bool:
        fields = {"state": state, "updated_at": now(), **(extra or {})}
        assignments = ",".join(f"{name} = ?" for name in fields)
        marks = ",".join("?" for _ in from_states)
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE explorations SET {assignments} WHERE id = ? AND state IN ({marks})",  # noqa: S608 — fixed names
                (*fields.values(), exploration_id, *from_states))
            changed = cursor.rowcount
            await cursor.close()
        return changed == 1

    async def _launch(self, orch: Orchestrators, project: Project, session_id: str, exploration_id: str,
                      slot: int, task: dict[str, Any], folder: ProjectFolder, source: Staff | None) -> str:
        """Hire, copy the card and hand it over, recording each step before the next one."""
        from daedalus.extensions.orchestrator_team import assign, hire

        if not await self._mark(exploration_id, slot, "launching", from_states=("planned",)):
            attempt = await self._attempt(exploration_id, slot)
            return f"  {slot}. not launched again: it is {attempt['state']}"
        attempt = await self._attempt(exploration_id, slot)
        name = f"{source.name if source is not None else 'Attempt'}-{exploration_id[1:5]}-{slot}"
        stage = "hire"
        try:
            await hire(orch, project, session_id, name=name, role=(source.role if source is not None else "") or "Exploration attempt",
                       harness=attempt["harness"], agent=source.agent if source is not None and source.harness == attempt["harness"] else "",
                       model=attempt["model"], effort=source.effort if source is not None and source.harness == attempt["harness"] else "",
                       permission_mode=source.permission_mode if source is not None and source.harness == attempt["harness"] else "",
                       env=source.env if source is not None and source.harness == attempt["harness"] else "",
                       folder=folder.id, isolation="worktree",
                       instructions=source.instructions if source is not None else "", one_off=True)
            member = await _member(orch, project, name)
            await self._mark(exploration_id, slot, "launching", from_states=("launching",), staff_id=member.id)
            stage = "card"
            card_id = await self._card(orch, project, session_id, exploration_id, slot, task, attempt["approach"])
            await self._mark(exploration_id, slot, "launching", from_states=("launching",), task_id=card_id)
            stage = "assign"
            revision = await ControlStore(self.db).revision(Scope("project", project.id), Entity("task", card_id))
            await assign(orch, project, session_id, staff=member.id, task_id=card_id,
                         reason=f"attempt {slot} of exploration {exploration_id}",
                         client_operation_id=f"explore:{exploration_id}:assign:{slot}",
                         expected_entity_revision=revision)
        except Refused as exc:
            # Refused before its launch command was written: nothing of this attempt runs.
            await self._mark(exploration_id, slot, "failed", from_states=("launching",), detail=f"{stage}: {exc}"[:1000])
            return f"  {slot}. could not start ({stage}): {exc}"
        except Exception as exc:  # noqa: BLE001 — an outcome nobody can vouch for is unknown, not failed
            logger.exception("exploration %s attempt %s failed at %s", exploration_id, slot, stage)
            await self._mark(exploration_id, slot, "unknown", from_states=("launching",), detail=f"{stage}: {exc}"[:1000])
            return f"  {slot}. outcome unknown ({stage}): {exc}"
        await self._mark(exploration_id, slot, "running", from_states=("launching",))
        where = _label(member.harness) + (f", {member.model}" if member.model else "")
        forked = " from the source's conversation" if attempt["fork_session_id"] and member.harness == "daedalus" else ""
        approach = f"; approach: {_one_line(attempt['approach'], 120)}" if attempt["approach"] else ""
        return f"  {slot}. {member.name} ({where}){forked} on {card_id}{approach}"

    async def _card(self, orch: Orchestrators, project: Project, session_id: str, exploration_id: str,
                    slot: int, task: dict[str, Any], approach: str) -> str:
        brief = {key: str(task.get("brief", {}).get(key) or "").strip() for key in CONTRACT_FIELDS}
        if approach:
            brief["boundaries"] = (brief["boundaries"] + f"\nThe approach this attempt takes: {approach}")[:4000]
        brief["boundaries"] = (brief["boundaries"] + f"\nOne of several competing attempts at {task['id']}: "
                               "work only on this card's branch; the orchestrator keeps one attempt.")[:4000]
        scope = Scope("project", project.id)
        principal = await board_principal(orch, session_id, project.id, "board.task.create")
        revision = await ControlStore(self.db).revision(scope, Entity("collection", project.id))
        created = await BoardCommands(self.db, bus=self.app.manager.bus).create(
            principal, scope, client_operation_id=f"explore:{exploration_id}:card:{slot}",
            expected_collection_revision=revision, title=f"{task['title']} — attempt {slot}"[:200],
            acceptance=task.get("acceptance") or brief["done_when"], checklist=split_checks(brief["done_when"]),
            priority=int(task.get("priority") or 3), brief=brief,
            notes=f"Attempt {slot} of exploration {exploration_id} on {task['id']}.",
            source_session_id=session_id, folder_id=task.get("folder_id"))
        return str(created["task_id"])

    async def _park_task(self, orch: Orchestrators, project: Project, session_id: str, task_id: str,
                         exploration_id: str) -> None:
        """Show the task as waiting on its exploration, so nothing hands it out meanwhile."""
        try:
            await self._update_card(orch, project, session_id, task_id, f"explore:{exploration_id}:park",
                                    status="blocked", note=f"waits on: exploration {exploration_id}")
        except Refused as exc:
            logger.info("could not mark %s as explored: %s", task_id, exc)

    async def _update_card(self, orch: Orchestrators, project: Project, session_id: str, task_id: str,
                           operation_id: str, **changes: Any) -> None:
        scope = Scope("project", project.id)
        principal = await board_principal(orch, session_id, project.id, "board.task.update", task_id)
        revision = await ControlStore(self.db).revision(scope, Entity("task", task_id))
        try:
            await BoardCommands(self.db, bus=self.app.manager.bus).update(
                principal, scope, task_id, client_operation_id=operation_id,
                expected_entity_revision=revision, **changes)
        except (KeyError, ValueError) as exc:
            raise Refused(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — a conflict on one card must not stop the others
            raise Refused(str(exc)) from exc

    # -- following the attempts --------------------------------------------------------------------

    async def reported(self, event: Any) -> None:
        """A member's report: a ``done`` on an attempt's card hands that attempt in."""
        if event.type != "staff.report" or event.payload.get("kind") != "done":
            return
        task_id = str(event.payload.get("task_id") or "")
        if not task_id:
            return
        row = await self.db.fetchone(
            "SELECT a.exploration_id,a.slot FROM exploration_attempts a JOIN explorations e ON e.id = a.exploration_id"
            " WHERE a.task_id = ? AND e.state IN (?,?,?)", (task_id, *OPEN))
        if row is None:
            return
        result_id = str(event.payload.get("report_id") or "") or None
        await self._mark(row["exploration_id"], row["slot"], "reported",
                         from_states=("running", "unknown", "launching"), result_id=result_id)
        await self.settle(row["exploration_id"])

    async def quiet(self, event: Any) -> bool:
        """Whether an event is an attempt's own progress, which wakes its orchestrator only once, at
        the end. A question, a permission or a crash still wakes it: those need it now."""
        p = event.payload
        if event.type == "staff.report":
            if p.get("kind") not in ("done", None) and not p.get("implicit"):
                return False
        elif event.type == "staff.status":
            if p.get("status") == "error":
                return False
        task_id = str(p.get("task_id") or "")
        if task_id:
            row = await self.db.fetchone(
                "SELECT 1 FROM exploration_attempts a JOIN explorations e ON e.id = a.exploration_id"
                " WHERE a.task_id = ? AND e.state IN ('launching','running')", (task_id,))
            return row is not None
        if event.staff_id:
            row = await self.db.fetchone(
                "SELECT 1 FROM exploration_attempts a JOIN explorations e ON e.id = a.exploration_id"
                " WHERE a.staff_id = ? AND e.state IN ('launching','running')", (event.staff_id,))
            return row is not None
        return False

    async def sweep(self) -> None:
        """Deadlines, budgets, and attempts whose member stopped without a report."""
        rows = await self.db.fetchall("SELECT id FROM explorations WHERE state = 'running'")
        for row in rows:
            try:
                await self._lost_attempts(row["id"])
                await self.settle(row["id"])
            except Exception:  # noqa: BLE001 — one exploration must not stop the others' clocks
                logger.exception("could not sweep exploration %s", row["id"])

    async def _lost_attempts(self, exploration_id: str) -> None:
        rows = await self.db.fetchall(
            "SELECT a.slot,t.status,t.assignee_staff_id FROM exploration_attempts a JOIN board_tasks t ON t.id = a.task_id"
            " WHERE a.exploration_id = ? AND a.state = 'running'", (exploration_id,))
        for row in rows:
            if row["status"] in ("todo", "dropped") and not row["assignee_staff_id"]:
                await self._mark(exploration_id, row["slot"], "failed", from_states=("running",),
                                 detail="its member stopped without a report and the card went back to todo")

    async def settle(self, exploration_id: str) -> bool:
        """Make the exploration ready once nothing more is to come; whether this call did."""
        async with self._lock:
            row = await self.db.fetchone("SELECT * FROM explorations WHERE id = ?", (exploration_id,))
            if row is None or row["state"] != "running":
                return False
            attempts = [dict(a) for a in await self.db.fetchall(
                "SELECT * FROM exploration_attempts WHERE exploration_id = ? ORDER BY slot", (exploration_id,))]
            async with self.db.transaction() as conn:
                spend = await spent_by_attempt_in(conn, exploration_id)
            spent = sum(int(round(item["usd"] * 1_000_000)) for item in spend.values())
            if all(a["state"] in TERMINAL for a in attempts):
                reason = "every attempt has reported" if all(a["state"] == "reported" for a in attempts) else "no attempt is still working"
            elif row["deadline_at"] <= now():
                reason = "the deadline passed"
            elif spent >= int(row["budget_microusd"]):
                reason = f"the budget of ${usd(int(row['budget_microusd']))} is spent"
            else:
                return False
            if not await self._set_state(exploration_id, "ready", from_states=("running",),
                                         extra={"ready_reason": reason}):
                return False
        await self._stop_unfinished(row, attempts, reason)
        lines = await self.summary(exploration_id)
        await self._set_state(exploration_id, "ready", from_states=("ready",), extra={"summary": "\n".join(lines)})
        await self.app.manager.bus.publish(
            "orchestrator.exploration",
            {"exploration_id": exploration_id, "task_id": row["task_id"], "state": "ready", "reason": reason,
             "lines": lines}, project_id=row["project_id"])
        return True

    async def _stop_unfinished(self, exploration: Any, attempts: list[dict[str, Any]], reason: str) -> None:
        """Stop the turns of attempts still working at a deadline or a spent budget; they keep their
        session, and a pick or a none releases them."""
        unfinished = [a for a in attempts if a["state"] in ("running", "launching", "planned")]
        if not unfinished:
            return
        orch = self._orch()
        for attempt in unfinished:
            if attempt["staff_id"] and orch.team is not None:
                member = await orch.manager.staff.get(attempt["staff_id"])
                if member is not None and member.active:
                    try:
                        async with asyncio.timeout(30):
                            await orch.team.interrupt(member)
                    except Exception:  # noqa: BLE001 — a member already idle has no turn to stop
                        logger.info("could not stop %s for exploration %s", member.name, exploration["id"])
            await self._mark(exploration["id"], attempt["slot"], "failed", from_states=("running", "launching", "planned"),
                             detail=f"stopped unfinished: {reason}")

    async def summary(self, exploration_id: str) -> list[str]:
        """The attempts side by side: what each changed, the checks it reported, what it cost."""
        orch = self._orch()
        row = await self.db.fetchone("SELECT * FROM explorations WHERE id = ?", (exploration_id,))
        if row is None:
            raise KeyError(exploration_id)
        project = await orch.manager.projects.get(row["project_id"])
        task = await self.db.fetchone("SELECT title FROM board_tasks WHERE id = ?", (row["task_id"],))
        attempts = await self.db.fetchall("SELECT * FROM exploration_attempts WHERE exploration_id = ? ORDER BY slot",
                                          (exploration_id,))
        async with self.db.transaction() as conn:
            spend = await spent_by_attempt_in(conn, exploration_id)
        lines = [f"Exploration {exploration_id} of {row['task_id']} \"{task['title'] if task else ''}\": "
                 f"{row['ready_reason'] or row['state']}."]
        for attempt in attempts:
            member = await orch.manager.staff.get(attempt["staff_id"]) if attempt["staff_id"] else None
            who = member.name if member is not None else f"attempt {attempt['slot']}"
            runtime = _label(attempt["harness"] or "daedalus") + (f", {attempt['model']}" if attempt["model"] else "")
            cost = spend.get(attempt["slot"], {"usd": 0.0, "unpriced": False})
            price = f"${cost['usd']:.2f}" + (" + unpriced" if cost["unpriced"] else "")
            if attempt["state"] not in ("reported", "won", "lost") or not attempt["task_id"]:
                lines.append(f"  {attempt['slot']}. {who} ({runtime}) — {attempt['state']}: "
                             f"{_one_line(attempt['detail'], 200) or 'no report'}; {price}")
                continue
            card = await self.db.fetchone("SELECT branch,folder_id FROM board_tasks WHERE id = ?", (attempt["task_id"],))
            diff = await self._diffstat(project, card)
            result = await self.db.fetchone("SELECT original_text,checks_json FROM result_receipts WHERE id = ?",
                                            (attempt["result_id"],)) if attempt["result_id"] else None
            checks = json.loads(result["checks_json"] or "[]") if result is not None else []
            shown = ", ".join(f"{c.get('item')} {_one_line(str(c.get('result') or 'shown'), 40)}" for c in checks[:4])
            excerpt = _one_line(str(result["original_text"] or "(the report is kept as a file)") if result is not None else "", EXCERPT_CHARS)
            lines.append(f"  {attempt['slot']}. {who} ({runtime}) — reported on {attempt['task_id']}"
                         f"{' branch ' + card['branch'] if card is not None and card['branch'] else ''}: {diff}; "
                         f"checks: {shown or 'none shown'}; {price}. \"{excerpt}\"")
        lines.append(f"Explore(op='pick', exploration='{exploration_id}', winner=<number>, why=…) keeps one: its card goes "
                     "on to your review and the merge, the cards that waited on the task wait on it, and the other "
                     "attempts are dismissed with their worktrees and branches; winner='none' keeps none and puts the task back in todo.")
        return lines

    async def _diffstat(self, project: Project | None, card: Any) -> str:
        orch = self._orch()
        if project is None or card is None or not card["branch"] or orch.team is None:
            return "no branch"
        folder = self._folder(project, card["folder_id"])
        if folder is None:
            return "no folder"
        try:
            compared = await orch.team.worktrees.compare(folder, card["branch"], max_files=300, max_lines=50_000, max_chars=1)
        except Exception as exc:  # noqa: BLE001 — a missing diff is said, not raised into the summary
            return f"diff unavailable ({_one_line(str(exc), 80)})"
        if not compared.exists:
            return "the branch is gone"
        added = sum(f.added or 0 for f in compared.files)
        removed = sum(f.removed or 0 for f in compared.files)
        conflicts = "" if not compared.conflicts else f", conflicts in {len(compared.conflicts)}"
        return f"{len(compared.files)} files +{added} −{removed}{conflicts}"

    # -- deciding ----------------------------------------------------------------------------------

    async def pick(self, orch: Orchestrators, project: Project, session_id: str, *, exploration_id: str,
                   winner: int | str | None, why: str) -> str:
        row = await self.db.fetchone("SELECT * FROM explorations WHERE id = ? AND project_id = ?",
                                     (exploration_id, project.id))
        if row is None:
            raise Refused(f"no exploration {exploration_id} in {project.name}")
        reason = " ".join((why or "").split())
        if len(reason) < WHY_MIN:
            raise Refused("say why in why= (a sentence): the journal and the cards keep it")
        none = winner is None or (isinstance(winner, str) and winner.strip().lower() == "none")
        slot = None
        if not none:
            try:
                slot = int(winner)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise Refused("winner is an attempt's number, or 'none'") from exc
        if row["state"] in ("chosen", "abandoned"):
            same = (row["state"] == "abandoned" and none) or (row["state"] == "chosen" and row["winner_slot"] == slot)
            if not same:
                kept = f"attempt {row['winner_slot']}" if row["state"] == "chosen" else "none"
                raise Refused(f"{exploration_id} is decided already: it kept {kept}")
            cleanup = await self._clean_up(orch, project, session_id, exploration_id, winner=slot, why=row["decision_reason"])
            return f"{exploration_id} was decided already ({'kept ' + str(slot) if slot else 'kept none'}); nothing changed." + (
                "\n" + "\n".join(cleanup) if cleanup else "")
        if row["state"] == "launching":
            raise Refused(f"{exploration_id} is still launching its attempts")
        if slot is not None:
            attempt = await self.db.fetchone("SELECT state,task_id FROM exploration_attempts WHERE exploration_id = ? AND slot = ?",
                                             (exploration_id, slot))
            if attempt is None:
                raise Refused(f"{exploration_id} has no attempt {slot}")
            if attempt["state"] != "reported":
                raise Refused(f"attempt {slot} is {attempt['state']}: only an attempt that reported done can be kept")
        state = "abandoned" if none else "chosen"
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE explorations SET state = ?,winner_slot = ?,decision_reason = ?,decided_at = ?,updated_at = ?"
                " WHERE id = ? AND state IN ('running','ready')", (state, slot, reason, now(), now(), exploration_id))
            changed = cursor.rowcount
            await cursor.close()
            if changed != 1:
                raise Refused(f"{exploration_id} changed meanwhile; Explore(op='status') shows it")
            await conn.execute("UPDATE exploration_attempts SET state = CASE WHEN slot = ? THEN 'won' ELSE 'lost' END,"
                               " updated_at = ? WHERE exploration_id = ?", (slot if slot is not None else -1, now(), exploration_id))
        attempts = await self.db.fetchall("SELECT * FROM exploration_attempts WHERE exploration_id = ?", (exploration_id,))
        unfinished = [dict(a) for a in attempts if a["slot"] != slot]
        for attempt in unfinished:
            if attempt["staff_id"] and orch.team is not None:
                member = await orch.manager.staff.get(attempt["staff_id"])
                if member is not None and member.active:
                    try:
                        async with asyncio.timeout(30):
                            await orch.team.interrupt(member)
                    except Exception:  # noqa: BLE001 — an idle member has no turn to stop
                        pass
        cleanup = await self._clean_up(orch, project, session_id, exploration_id, winner=slot, why=reason)
        task_id = row["task_id"]
        if none:
            text = f"Kept none of {exploration_id}; {task_id} is back in todo."
            await _journal(orch, project, "decision", f"The orchestrator kept none of the attempts at {task_id} ({exploration_id}): {reason}",
                           {"task_id": task_id, "exploration_id": exploration_id})
        else:
            card = await self.db.fetchone("SELECT task_id FROM exploration_attempts WHERE exploration_id = ? AND slot = ?",
                                          (exploration_id, slot))
            text = (f"Kept attempt {slot} of {exploration_id}: {card['task_id']} carries {task_id} on, in review — "
                    "ReviewResult it as any card; the operator merges it.")
            await _journal(orch, project, "decision",
                           f"The orchestrator kept attempt {slot} ({card['task_id']}) of {exploration_id} for {task_id}: {reason}",
                           {"task_id": task_id, "exploration_id": exploration_id, "winner_task_id": card["task_id"]})
        return text + ("\n" + "\n".join(cleanup) if cleanup else "")

    async def _clean_up(self, orch: Orchestrators, project: Project, session_id: str, exploration_id: str, *,
                        winner: int | None, why: str) -> list[str]:
        """Release the attempts not kept and move the cards on; each step is done once and recorded,
        so a second pick finishes what the first could not. Returns what could not be done."""
        problems: list[str] = []
        exploration = await self.db.fetchone("SELECT * FROM explorations WHERE id = ?", (exploration_id,))
        attempts = [dict(a) for a in await self.db.fetchall(
            "SELECT * FROM exploration_attempts WHERE exploration_id = ? ORDER BY slot", (exploration_id,))]
        winner_card = next((a["task_id"] for a in attempts if a["slot"] == winner), None)
        for attempt in attempts:
            if attempt["cleaned_at"] or attempt["slot"] == winner:
                continue
            problem = await self._release(orch, project, session_id, exploration_id, attempt, why)
            if problem:
                problems.append(f"  {attempt['slot']}. {problem}")
                continue
            async with self.db.transaction() as conn:
                await conn.execute("UPDATE exploration_attempts SET cleaned_at = ?,updated_at = ? WHERE exploration_id = ?"
                                   " AND slot = ?", (now(), now(), exploration_id, attempt["slot"]))
        if exploration is None:
            return problems
        task_id = exploration["task_id"]
        task = await self.db.fetchone("SELECT status FROM board_tasks WHERE id = ?", (task_id,))
        if task is None:
            return problems
        try:
            if winner_card is not None and task["status"] != "dropped":
                problems += await self._hand_dependents(orch, project, session_id, exploration_id, task_id, winner_card)
                await self._update_card(orch, project, session_id, winner_card, f"explore:{exploration_id}:won",
                                        note=f"kept from exploration {exploration_id} of {task_id}: {_one_line(why, 300)}")
                await self._update_card(orch, project, session_id, task_id, f"explore:{exploration_id}:carried",
                                        status="dropped", note=f"carried on as {winner_card} (exploration {exploration_id})")
            elif winner_card is None and task["status"] == "blocked":
                await self._update_card(orch, project, session_id, task_id, f"explore:{exploration_id}:none",
                                        status="todo", note=f"exploration {exploration_id} kept none: {_one_line(why, 300)}")
        except Refused as exc:
            problems.append(f"  {task_id}: {exc}")
        return problems

    async def _release(self, orch: Orchestrators, project: Project, session_id: str, exploration_id: str,
                       attempt: dict[str, Any], why: str) -> str:
        from daedalus.extensions.orchestrator_team import dismiss

        if attempt["staff_id"]:
            member = await orch.manager.staff.get(attempt["staff_id"])
            if member is not None and member.active:
                try:
                    await dismiss(orch, project, session_id, staff=member.id, release=True, keep_worktree=False)
                except Refused as exc:
                    return f"{member.name} was not dismissed: {exc}"
        if not attempt["task_id"]:
            return ""
        card = await self.db.fetchone("SELECT status,branch,folder_id FROM board_tasks WHERE id = ?", (attempt["task_id"],))
        if card is None:
            return ""
        if card["branch"] and orch.team is not None:
            folder = self._folder(project, card["folder_id"])
            if folder is not None:
                try:
                    for entry in await orch.team.worktrees.inventory(folder):
                        if entry.branch == card["branch"]:
                            await orch.team.worktrees.remove_listed(folder, entry.path, delete_branch=False)
                    await orch.team.worktrees.park_branch(folder, card["branch"],
                                                          f"{PARKED}/{exploration_id}/{attempt['slot']}")
                except (WorktreeError, GitError, OSError) as exc:
                    return f"the branch {card['branch']} stays: {exc}"
        if card["status"] == "review" and attempt["result_id"]:
            try:
                await self._return(orch, project, session_id, exploration_id, attempt, why)
            except Refused as exc:
                return f"{attempt['task_id']} stays in review: {exc}"
            card = await self.db.fetchone("SELECT status,branch,folder_id FROM board_tasks WHERE id = ?", (attempt["task_id"],))
        if card is not None and card["status"] != "dropped":
            try:
                await self._update_card(orch, project, session_id, attempt["task_id"],
                                        f"explore:{exploration_id}:drop:{attempt['slot']}", status="dropped",
                                        note=f"not kept from exploration {exploration_id}: {_one_line(why, 300)}")
            except Refused as exc:
                return f"{attempt['task_id']} stays {card['status']}: {exc}"  # type: ignore[index]
        await orch.loops.close(project.id, task_id=attempt["task_id"], by="system",
                               decision=f"exploration {exploration_id} kept another attempt")
        return ""

    async def _return(self, orch: Orchestrators, project: Project, session_id: str, exploration_id: str,
                      attempt: dict[str, Any], why: str) -> None:
        """Send a losing attempt's result back the way any reviewed result goes back: a verdict that
        it is not the one kept, then its return. A handed-in card is never simply moved off review."""
        authority = orch.app.extensions.get("orchestrator_review_authority")
        if authority is None:
            raise Refused("review authority is unavailable")
        card_id = str(attempt["task_id"])
        try:
            principal = await authority(session_id=session_id, project_id=project.id, task_id=card_id,
                                        operation="review.return")
        except Exception as exc:  # noqa: BLE001 — no grant is said, not raised past the others
            raise Refused(str(exc)) from exc
        scope = Scope("project", project.id)
        control = ControlStore(self.db)
        reason = f"not kept: exploration {exploration_id} kept another attempt — {_one_line(why, 300)}"

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            row = await one(conn, "SELECT contract_revision FROM result_receipts WHERE id = ?", (attempt["result_id"],))
            if row is None:
                raise KeyError(attempt["result_id"])
            verdict_id = f"{mutation.object_id}-verdict"
            await record_verdict(conn, verdict_id=verdict_id, result_id=attempt["result_id"],
                                 reviewer_actor_id=principal.actor_id, verification="stale", accepted=False,
                                 head=None, base=None, environment_digest=None, evidence_ids=[], reason=reason)
            return await return_result(conn, return_id=mutation.object_id, task_id=card_id,
                                       result_id=attempt["result_id"], verdict_id=verdict_id,
                                       contract_revision=int(row["contract_revision"]),
                                       actor_id=principal.actor_id, reason=reason)

        try:
            await control.mutate(principal, scope, "review.return", f"explore:{exploration_id}:return:{attempt['slot']}",
                                 await control.revision(scope, Entity("task", card_id)), Entity("task", card_id),
                                 {"task_id": card_id, "result_id": attempt["result_id"], "reason": reason}, effect)
        except Exception as exc:  # noqa: BLE001 — a conflict is reported with the attempt, not raised
            raise Refused(str(exc)) from exc

    async def _hand_dependents(self, orch: Orchestrators, project: Project, session_id: str, exploration_id: str,
                               task_id: str, winner_card: str) -> list[str]:
        """The cards that waited on the task wait on the attempt that carries it on."""
        problems: list[str] = []
        rows = await self.db.fetchall("SELECT id,depends_on FROM board_tasks WHERE project_id = ? AND status NOT IN ('done','dropped')",
                                      (project.id,))
        for row in rows:
            deps = json.loads(row["depends_on"] or "[]")
            if task_id not in deps:
                continue
            changed = [winner_card if dep == task_id else dep for dep in deps]
            try:
                await self._update_card(orch, project, session_id, row["id"], f"explore:{exploration_id}:deps:{row['id']}",
                                        depends_on=changed)
            except Refused as exc:
                problems.append(f"  {row['id']} still waits on {task_id}: {exc}")
        return problems

    # -- reading -----------------------------------------------------------------------------------

    async def status_text(self, project: Project, exploration_id: str) -> str:
        row = await self.db.fetchone("SELECT * FROM explorations WHERE id = ? AND project_id = ?", (exploration_id, project.id))
        if row is None:
            raise Refused(f"no exploration {exploration_id} in {project.name}")
        if row["state"] == "ready" and row["summary"]:
            return row["summary"]
        lines = await self.summary(exploration_id)
        head = f"{exploration_id} is {row['state']}"
        if row["state"] in ("launching", "running"):
            head += f", deadline {row['deadline_at'][:16].replace('T', ' ')} UTC"
        elif row["state"] == "chosen":
            head += f": kept attempt {row['winner_slot']} — {row['decision_reason']}"
        elif row["state"] == "abandoned":
            head += f": kept none — {row['decision_reason']}"
        return head + "\n" + "\n".join(lines[1:-1] if row["state"] in ("chosen", "abandoned") else lines[1:])

    async def for_task(self, task_id: str) -> list[dict[str, Any]]:
        """The explorations a card is the task of, or an attempt of, for the app's card."""
        rows = await self.db.fetchall(
            "SELECT DISTINCT e.* FROM explorations e LEFT JOIN exploration_attempts a ON a.exploration_id = e.id"
            " WHERE e.task_id = ? OR a.task_id = ? ORDER BY e.created_at DESC LIMIT 10", (task_id, task_id))
        out = []
        for row in rows:
            async with self.db.transaction() as conn:
                spend = await spent_by_attempt_in(conn, row["id"])
            attempts = []
            for attempt in await self.db.fetchall("SELECT a.*,s.name FROM exploration_attempts a LEFT JOIN staff s ON s.id = a.staff_id"
                                                  " WHERE a.exploration_id = ? ORDER BY a.slot", (row["id"],)):
                cost = spend.get(attempt["slot"], {"usd": 0.0, "unpriced": False})
                attempts.append({"slot": attempt["slot"], "state": attempt["state"], "staff_name": attempt["name"] or "",
                                 "harness": attempt["harness"], "model": attempt["model"], "task_id": attempt["task_id"],
                                 "forked": bool(attempt["fork_session_id"]), "usd": round(cost["usd"], 4),
                                 "unpriced": cost["unpriced"]})
            out.append({"id": row["id"], "task_id": row["task_id"], "state": row["state"],
                        "reason": row["ready_reason"], "winner_slot": row["winner_slot"],
                        "decision_reason": row["decision_reason"], "budget_usd": usd(int(row["budget_microusd"])),
                        "deadline_at": row["deadline_at"], "created_at": row["created_at"], "attempts": attempts})
        return out

    # -- restarts ----------------------------------------------------------------------------------

    async def recover(self) -> None:
        """Say what a restart interrupted; never launch it again."""
        rows = await self.db.fetchall("SELECT id FROM explorations WHERE state = 'launching'")
        for row in rows:
            exploration_id = row["id"]
            async with self.db.transaction() as conn:
                await conn.execute("UPDATE exploration_attempts SET state = 'cancelled',updated_at = ?,"
                                   " detail = 'never launched: the host restarted before its turn came' WHERE exploration_id = ?"
                                   " AND state = 'planned'", (now(), exploration_id))
                await conn.execute("UPDATE exploration_attempts SET state = 'unknown',updated_at = ?,"
                                   " detail = 'the host restarted while it was being launched; it was not launched again'"
                                   " WHERE exploration_id = ? AND state = 'launching'", (now(), exploration_id))
            await self._set_state(exploration_id, "running", from_states=("launching",))
            await self.settle(exploration_id)

    async def run(self) -> None:
        await self.recover()
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 — the next sweep tries again
                logger.exception("exploration sweep failed")


async def op_explore(orch: Orchestrators, project: Project, session_id: str, *, op: str = "start",
                     task_id: str | None = None, attempts: int = 2, staff: str | None = None,
                     variants: list[dict[str, Any]] | None = None, budget_usd: float | str | None = None,
                     deadline_minutes: int | None = None, exploration: str | None = None,
                     winner: int | str | None = None, why: str = "", client_operation_id: str = "") -> str:
    service = orch.app.extensions.get("explorations")
    if service is None:
        raise Refused("explorations are not available on this installation")
    if op == "start":
        if not task_id:
            raise Refused("Explore needs the task_id of the card to explore")
        return await service.start(orch, project, session_id, task_id=task_id, attempts=attempts, staff=staff,
                                   variants=variants, budget_usd=budget_usd, deadline_minutes=deadline_minutes,
                                   client_operation_id=client_operation_id)
    if op == "status":
        if exploration:
            return await service.status_text(project, exploration)
        rows = await service.db.fetchall("SELECT id,task_id,state FROM explorations WHERE project_id = ?"
                                         " ORDER BY created_at DESC LIMIT 10", (project.id,))
        return "\n".join(f"{r['id']}: {r['task_id']} {r['state']}" for r in rows) or "no explorations yet"
    if op == "pick":
        if not exploration:
            raise Refused("pick needs exploration= and winner= (a number, or 'none')")
        return await service.pick(orch, project, session_id, exploration_id=exploration, winner=winner, why=why)
    raise Refused("op is start, status or pick")


OPS = {"explore": op_explore}


async def install(app: Application) -> list[asyncio.Task[None]]:
    service = Explorations(app)
    app.extensions["explorations"] = service
    team = app.extensions.get("staff")
    if team is not None:
        team.report_hooks.append(service.reported)
    return [asyncio.create_task(service.run(), name="explorations")]


__all__ = ["Explorations", "OPS", "install", "op_explore"]
