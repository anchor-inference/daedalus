"""The orchestrator's team tools: hiring, handing out work, talking to staff, reading them, answering
their requests within the project's autonomy, and stopping them."""

from __future__ import annotations

import itertools
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import ORCHESTRATOR_ONLY_TOOLS, Settings
from daedalus.extensions.coordinator_authority import approve_authority
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.harness.catalog import HarnessCatalog
from daedalus.harness.contract import AgentEntry, Catalog, InstallInfo, LoginState
from daedalus.host.events import AppEvent
from daedalus.host.services import SessionServices, locator
from daedalus.host.wake_queue import Batch, Pending, Wake
from daedalus.staff_runtime import Availability, FakeStaffRuntime, LiveSession, ReadPage, Receipt
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.harness import HarnessStore
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.staff import Staff
from daedalus.tools.orchestrator import tell
from tests.support.authorized_launch import operator_assignment
from tests.support.models import DEFAULT_PRESET
from tests.support.waiting import until, until_await
from tests.unit.test_orchestrator import Rig, _idle, approve_coordinator, events, rig
from tests.unit.test_staff_runtime import (
    BRIEF,
    Capacity,
    ObservedFakeStaffRuntime,
    board_task,
    close_team,
    observed_cli_exit,
    task_row,
)

ALLOWANCES = "Install npm packages listed in package.json\nRun the test suite as often as needed"


async def office(r: Rig, *, autonomy: str = "normal") -> str:
    """An orchestrator for the rig's project, with a fake Daedalus runtime and the brief's allowances."""
    sid = (await r.orch.enable(r.project.id, autonomy=autonomy)).settings.orchestrator.session_id
    await r.manager.projects.set_brief(r.project.id, "allowed_without_operator", ALLOWANCES, "operator")
    await approve_coordinator(r, sid)
    return sid


def fake(r: Rig, **kwargs: Any) -> ObservedFakeStaffRuntime:
    runtime = ObservedFakeStaffRuntime(kind="daedalus", **kwargs)
    runtime.manager = r.manager
    r.team.runtimes["daedalus"] = runtime
    return runtime


async def working(r: Rig, name: str = "Ada", title: str = "Menu page") -> tuple[Staff, LiveSession]:
    """A member hired by the operator and started on a task, so there is a live session to talk to."""
    member = await r.manager.staff.hire(r.project.id, name=name, role="Menu", isolation="shared")
    await operator_assignment(r.team, member, await board_task(r.manager, r.project, title))
    live = await r.team.live_of(member)
    assert live is not None
    return member, live


async def admitted(r: Rig, task_id: str) -> bool:
    row = await r.manager.db.fetchone(
        "SELECT state FROM effect_outbox WHERE json_extract(payload_json,'$.control.task_id') = ?"
        " AND kind = 'task.launch'"
        " ORDER BY created_at DESC LIMIT 1", (task_id,),
    )
    return row is not None and row["state"] == "completed"


async def test_first_assignment_has_task_scope_and_cannot_edit_the_card(settings: Settings, db: Database, tmp_path: Path) -> None:
    from daedalus.extensions.board_commands import BoardCommands
    from daedalus.extensions.coordinator_authority import resolve_authority
    from daedalus.stores.control import ControlDenied

    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = (await r.orch.enable(r.project.id, autonomy="ask")).settings.orchestrator.session_id
        member = await r.manager.staff.hire(r.project.id, name="Ada", role="Menu", isolation="shared")
        task_id = await board_task(r.manager, r.project, "Menu page")
        other_id = await board_task(r.manager, r.project, "Other page")
        await r.manager.db.execute("UPDATE board_tasks SET folder_id = ? WHERE id = ?", (r.project.folders[0].id, task_id))
        scope = Scope("project", r.project.id)
        operator = Principal.operator({"via": "token", "user_id": 1})
        revision = await ControlStore(r.manager.db).revision(scope, Entity("project", r.project.id))
        await approve_authority(r.team.app, r.project.id, operator,
                                client_operation_id="task-assignment-and-run", expected_entity_revision=revision,
                                expected_coordinator_session_id=sid, bundle_id="assignment_execution",
                                task_id=task_id, expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
        for change in ({"title": "Renamed menu page", "reason": "This remains exactly the same task"},
                       {"objective": "Replace the menu page with an updated menu"},
                       {"priority": 1}, {"checks": ["Menu page renders"]}):
            with pytest.raises(Refused, match="grant|scope|denied"):
                await r.call(sid, "assign", staff=member.id, task_id=task_id, **change)
        with pytest.raises(ControlDenied):
            await resolve_authority(r.team.app, session_id=sid, project_id=r.project.id,
                                    operation="board.task.assign", task_id=other_id)
        before = dict(await r.manager.db.fetchone("SELECT * FROM board_tasks WHERE id = ?", (task_id,)))
        card = await r.orch.board.get(task_id, actor=sid)
        said = await r.call(sid, "assign", staff=member.id, task_id=task_id,
                            **card["brief"])
        assert said.startswith(f"Ada will start {task_id}")
        after = dict(await r.manager.db.fetchone("SELECT * FROM board_tasks WHERE id = ?", (task_id,)))
        for field in ("title", "brief_json", "checklist", "acceptance", "folder_id", "depends_on", "priority"):
            assert after[field] == before[field]
        assert after["assignee_staff_id"] == member.id
        assert await admitted(r, task_id)
        assert (await r.manager.db.fetchone("SELECT count(*) FROM operation_receipts"
                                            " WHERE operation_kind = 'board.task.update'"))[0] == 0
        principal = await resolve_authority(r.team.app, session_id=sid, project_id=r.project.id,
                                             operation="board.task.assign", task_id=task_id)
        with pytest.raises(ControlDenied):
            await BoardCommands(r.manager.db).update(principal, scope, task_id, client_operation_id="forbidden-edit",
                                                     expected_entity_revision=after["entity_revision"], title="Changed")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_unchanged_assigned_task_starts_with_task_execution_grant_only(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = (await r.orch.enable(r.project.id, autonomy="ask")).settings.orchestrator.session_id
        member = await r.manager.staff.hire(r.project.id, name="Ada", role="Menu", isolation="shared")
        task_id = await board_task(r.manager, r.project, "Menu page")
        await r.manager.db.execute("UPDATE board_tasks SET assignee_staff_id = ? WHERE id = ?", (member.id, task_id))
        scope = Scope("project", r.project.id)
        operator = Principal.operator({"via": "token", "user_id": 1})
        revision = await ControlStore(r.manager.db).revision(scope, Entity("project", r.project.id))
        await approve_authority(r.team.app, r.project.id, operator,
                                client_operation_id="task-execution-only", expected_entity_revision=revision,
                                expected_coordinator_session_id=sid, bundle_id="execution",
                                task_id=task_id, expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())

        for change in ({"title": "Renamed menu page", "reason": "This remains exactly the same task"},
                       {"objective": "Replace the menu page with an updated menu"},
                       {"priority": 1}, {"checks": ["Menu page renders"]}):
            with pytest.raises(Refused, match="grant|scope|denied"):
                await r.call(sid, "assign", task_id=task_id, **change)
        said = await r.call(sid, "assign", task_id=task_id)
        assert said.startswith(f"Ada will start {task_id}")
        assert await admitted(r, task_id)
        assert (await r.manager.db.fetchone("SELECT count(*) FROM operation_receipts"
                                            " WHERE operation_kind = 'board.task.update'"))[0] == 0
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_repeated_saved_brief_keeps_model_preflight_with_task_grant(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        member = await r.manager.staff.hire(r.project.id, name="Ada", role="Menu", isolation="shared")
        brief = {**BRIEF, "objective": "Build the menu page with qwen/qwen3.7-flash"}
        task_id = await board_task(r.manager, r.project, "Menu page", brief=brief)
        scope = Scope("project", r.project.id)
        operator = Principal.operator({"via": "token", "user_id": 1})
        revision = await ControlStore(r.manager.db).revision(scope, Entity("project", r.project.id))
        await approve_authority(r.team.app, r.project.id, operator,
                                client_operation_id="model-brief-assignment", expected_entity_revision=revision,
                                expected_coordinator_session_id=sid, bundle_id="assignment_execution",
                                task_id=task_id, expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())

        with pytest.raises(Refused, match=r"the brief names qwen/qwen3\.7-flash"):
            await r.call(sid, "assign", staff=member.id, task_id=task_id, **brief)
        row = await r.manager.db.fetchone("SELECT assignee_staff_id FROM board_tasks WHERE id = ?", (task_id,))
        assert row["assignee_staff_id"] is None
        assert not await admitted(r, task_id)

        said = await r.call(sid, "assign", staff=member.id, task_id=task_id, **brief,
                            reason="The operator explicitly accepts the available model")
        assert said.startswith(f"Ada will start {task_id}")
        assert await admitted(r, task_id)
        assert (await r.manager.db.fetchone("SELECT count(*) FROM operation_receipts"
                                            " WHERE operation_kind = 'board.task.update'"))[0] == 0
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def journal_texts(r: Rig) -> list[str]:
    return [e.text for e in await r.manager.projects.journal(r.project.id, limit=50)]


# -- answering: the autonomy matrix ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("autonomy", "basis", "granted"),
    [
        ("normal", "", False),
        ("normal", "packages are fine here", False),
        ("normal", "npm pack", False),
        ("normal", "npm packages listed", True),
        ("normal", "Install npm packages listed in package.json", True),
        ("normal", "Install  npm packages\nlisted in package.json", True),
        ("full", "", False),
        ("full", "the task needs the dependency", True),
    ],
)
async def test_a_grant_follows_the_autonomy_and_the_quoted_allowance(settings: Settings, db: Database, tmp_path: Path, autonomy: str, basis: str, granted: bool) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r, autonomy=autonomy)
        member, live = await working(r)
        ask_id = await r.team.ingress.permission(live, "perm-1", "Exec", "npm install grammy")
        ask = await r.manager.asks.get(ask_id)
        assert ask is not None and ask.routed_to == "orchestrator"
        if not granted:
            with pytest.raises(Refused, match="basis|reason"):
                await r.call(sid, "answer", request_id=ask.short_id, allow=True, basis=basis)
            assert (await r.manager.asks.get(ask_id)).open  # type: ignore[union-attr]
            assert runtime.answered == []
            # Denying is always allowed, and needs no basis.
            said = await r.call(sid, "answer", request_id=ask.short_id, allow=False)
            assert said == f"request {ask.short_id} denied"
            assert runtime.answered[-1][2].allow is False
            assert not any("granted" in t for t in await journal_texts(r))
            return
        said = await r.call(sid, "answer", request_id=ask.short_id, allow=True, basis=basis)
        assert said == f"request {ask.short_id} granted"
        [(_, ref, decision)] = runtime.answered
        assert (ref.request_ref, decision.allow, decision.by) == ("perm-1", True, "orchestrator")
        resolved = await r.manager.asks.get(ask_id)
        assert resolved is not None and resolved.resolved_by == "orchestrator" and resolved.resolution["basis"] == basis
        assert any(t.startswith(f"The orchestrator granted {member.name}") for t in await journal_texts(r))
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_under_ask_autonomy_a_question_becomes_a_suggestion_and_a_permission_is_the_operators(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r, autonomy="ask")
        _, live = await working(r)
        question = await r.manager.asks.get(await r.team.ingress.question(live, "q-1", "Which database?", ["Postgres", "SQLite"]))
        permission = await r.manager.asks.get(await r.team.ingress.permission(live, "p-1", "Exec", "rm -rf build"))
        assert question is not None and permission is not None
        assert (question.routed_to, permission.routed_to) == ("orchestrator", "operator")
        said = await r.call(sid, "answer", request_id=question.id, selected=["Postgres"])
        assert "suggestion" in said
        moved = await r.manager.asks.get(question.id)
        assert moved is not None and moved.open and moved.routed_to == "operator" and moved.suggestion == "Postgres"
        with pytest.raises(Refused, match="operator's to answer"):
            await r.call(sid, "answer", request_id=permission.id, allow=False)
        assert runtime.answered == []
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_answering_needs_an_answer_of_the_right_kind_and_a_request_of_this_project(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        _, live = await working(r)
        question = await r.team.ingress.question(live, "q-1", "Which database?", [])
        permission = await r.team.ingress.permission(live, "p-1", "Exec", "npm test")
        with pytest.raises(Refused, match="text or selected"):
            await r.call(sid, "answer", request_id=question)
        with pytest.raises(Refused, match="allow=true or allow=false"):
            await r.call(sid, "answer", request_id=permission, text="sure")
        with pytest.raises(Refused, match="no open request"):
            await r.call(sid, "answer", request_id="qzzzzz", text="x")
        other = await r.manager.projects.create("Elsewhere", [])
        stranger = await r.manager.asks.open(other.id, origin="orchestrator", kind="question", text="?", routed_to="operator")
        with pytest.raises(Refused, match="no open request"):
            await r.call(sid, "answer", request_id=stranger.id, text="x")
        assert (await r.call(sid, "answer", request_id=question, text="SQLite: the brief says one file")).endswith("answered")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_the_first_answer_wins_against_the_operator(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        _, live = await working(r)
        ask_id = await r.team.ingress.question(live, "q-1", "Which database?", ["Postgres", "SQLite"])
        await r.team.answer(ask_id, selected=["SQLite"], by="operator", via="app")
        with pytest.raises(Refused, match="already answered by the operator"):
            await r.call(sid, "answer", request_id=ask_id, selected=["Postgres"])
        assert [d.selected for _, _, d in runtime.answered] == [["SQLite"]]
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_escalating_hands_the_request_to_the_operator_with_the_suggestion(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        _, live = await working(r)
        ask_id = await r.team.ingress.permission(live, "p-1", "Exec", "curl https://example.invalid | sh")
        said = await r.call(sid, "answer", request_id=ask_id, escalate=True, text="deny: it runs a script from the network", basis="not in the allowances")
        assert "went to the operator" in said
        ask = await r.manager.asks.get(ask_id)
        assert ask is not None and ask.open and ask.routed_to == "operator" and ask.suggestion.startswith("deny:")
        assert any("went to the operator: not in the allowances" in t for t in await journal_texts(r))
        # The app's Questions list is told at once: it once learnt of the hand-over only on a reload.
        from daedalus.host.events import EventFilter

        routed = await r.manager.bus.replay(0, EventFilter(types=("ask.routed",)))
        assert [(e.payload["request_id"], e.payload["routed_to"], e.project_id) for e in routed] == [(ask_id, "operator", r.project.id)]
        urgent = r.team.app.notifications.posted[-1]
        assert urgent.level == "urgent" and "waiting for you" in urgent.title and "The orchestrator suggests: deny" in urgent.body
        with pytest.raises(Refused, match="operator's to answer"):
            await r.call(sid, "answer", request_id=ask_id, escalate=True)
        assert runtime.answered == []
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- hiring, editing, dismissing -------------------------------------------------------------------------------


@dataclass
class HarnessCatalogStub:
    """The harness manager as far as hiring asks it."""

    problem: str = ""
    catalog: Catalog = field(default_factory=lambda: Catalog(agents=(AgentEntry("reviewer", "project"),), models=("opus", "sonnet"), modes=("acceptEdits", "manual"), efforts=("low", "high")))
    asked: list[tuple[str, str, str | None]] = field(default_factory=list)

    async def hire_problem(self, env: str, harness: str) -> str:
        return self.problem

    async def catalog_of(self, env: str, harness: str, folder_id: str | None = None) -> Catalog:
        self.asked.append((env, harness, folder_id))
        return self.catalog


async def test_hiring_checks_the_executor_the_model_and_the_name(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        with pytest.raises(Refused, match="not a model preset"):
            await r.call(sid, "hire", name="Rex", role="Review", model="no-such-preset")
        with pytest.raises(Refused, match="Claude Code is not installed on this installation"):
            await r.call(sid, "hire", name="Cleo", role="Code", harness="claude")
        with pytest.raises(Refused, match="harness is one of"):
            await r.call(sid, "hire", name="Cleo", role="Code", harness="emacs")
        said = await r.call(sid, "hire", name="Rex", role="Review", model=DEFAULT_PRESET)
        assert said.startswith("hired Rex [st-") and "worktree" in said, "a git folder gives its own worktree by default"
        rex = await r.manager.staff.by_name(r.project.id, "Rex")
        assert rex is not None and rex.created_by == "orchestrator" and rex.model == DEFAULT_PRESET
        with pytest.raises(Refused, match="already has someone called rex"):
            await r.call(sid, "hire", name="rex", role="Review again")
        assert "The orchestrator hired Rex (Daedalus): Review" in await journal_texts(r)
        [changed] = [e for e in await events(r.manager, "project.changed") if e.payload.get("change") == "staff.hired"]
        assert changed.payload["actor"] == "orchestrator" and changed.staff_id == rex.id

        # A command-line member: the runtime must be there, and the catalog must offer what is asked for.
        r.team.runtimes["claude"] = FakeStaffRuntime(kind="claude")
        stub = HarnessCatalogStub()
        r.team.app.extensions["harness"] = type("Manager", (), {"hire_problem": stub.hire_problem, "catalog": stub.catalog_of})()
        with pytest.raises(Refused, match="offers no model 'gpt'"):
            await r.call(sid, "hire", name="Cleo", role="Code", harness="claude", model="gpt")
        with pytest.raises(Refused, match="offers no agent 'poet'"):
            await r.call(sid, "hire", name="Cleo", role="Code", harness="claude", agent="poet")
        said = await r.call(sid, "hire", name="Cleo", role="Code", harness="claude", agent="reviewer", model="opus", permission_mode="acceptEdits")
        assert said.startswith("hired Cleo") and "Claude Code" in said
        # "default" means the CLI's own default, which is no agent: kept as none, never passed on.
        said = await r.call(sid, "hire", name="Dora", role="Mail", harness="claude", agent="default", model="sonnet")
        dora = await r.manager.staff.by_name(r.project.id, "Dora")
        assert said.startswith("hired Dora") and dora is not None and dora.agent == ""
        # An agent Claude Code brings with it is there though no agent file names it.
        said = await r.call(sid, "hire", name="Ed", role="Look around", harness="claude", agent="Explore")
        assert said.startswith("hired Ed")
        # With no agent files at all, an unknown name is still refused rather than let through.
        stub.catalog = Catalog(models=("opus", "sonnet"), modes=("acceptEdits", "manual"), efforts=("low", "high"))
        with pytest.raises(Refused, match="offers no agent 'poet'"):
            await r.call(sid, "hire", name="Fay", role="Code", harness="claude", agent="poet")
        stub.problem = "Claude Code is not installed in the container environment"
        with pytest.raises(Refused, match="not installed in the container"):
            await r.call(sid, "hire", name="Cody", role="Code", harness="claude")
        r.team.runtimes["claude"] = FakeStaffRuntime(kind="claude", availability=Availability(False, "the terminal daemon is down"))
        stub.problem = ""
        with pytest.raises(Refused, match="terminal daemon is down"):
            await r.call(sid, "hire", name="Cody", role="Code", harness="claude")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_editing_is_journaled_and_dismissing_a_working_member_needs_release(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        member, live = await working(r)
        said = await r.call(sid, "staff_edit", staff="Ada", role="Menu and prices", instructions="Prices in euros")
        assert "instructions, role changed" in said and "next session" in said
        assert "The orchestrator changed Ada's instructions, role." in await journal_texts(r)
        with pytest.raises(Refused, match="say what changes"):
            await r.call(sid, "staff_edit", staff="Ada")
        with pytest.raises(Refused, match="live session .*release=true"):
            await r.call(sid, "dismiss", staff="Ada")
        said = await r.call(sid, "dismiss", staff="Ada", release=True)
        assert said.startswith("dismissed Ada; their session was ended")
        assert runtime.stopped == [live.id]
        gone = await r.manager.staff.get(member.id)
        assert gone is not None and not gone.active
        [moved] = [e for e in await events(r.manager, "task.moved") if e.payload["to"] == "todo"]
        assert moved.payload["actor"] == "orchestrator"
        assert "The orchestrator dismissed Ada" in await journal_texts(r)
        with pytest.raises(Refused, match="nobody called 'Ada'"):
            await r.call(sid, "tell", staff="Ada", text="hello")
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- assigning ----------------------------------------------------------------------------------------------------


async def test_assign_can_override_daedalus_effort_without_changing_member_default(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        member = await r.manager.staff.hire(r.project.id, name="Ada", role="Menu", isolation="shared", effort="medium")
        before = await r.board.list(actor=sid)
        with pytest.raises(Refused, match="Daedalus effort"):
            await r.call(sid, "assign", staff="Ada", title="Invalid", effort="extreme", **BRIEF)
        assert await r.board.list(actor=sid) == before
        said = await r.call(sid, "assign", staff="Ada", title="Menu page", effort="high", **BRIEF)
        assert said.startswith("Ada will start ") and "effect " in said
        await until(lambda: len(runtime.started) == 1, "the effort override reached runtime admission")
        assert runtime.started[0].effort == "high"
        assert (await r.manager.staff.get(member.id)).effort == "medium"
    finally:
        await r.manager.close()


async def test_assign_needs_the_whole_contract_and_creates_the_task_as_the_orchestrator(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        await r.manager.staff.hire(r.project.id, name="Ada", role="Menu", isolation="shared")
        before = await r.board.list(actor=sid)
        with pytest.raises(Refused, match="no usable deliverable, done-when"):
            await r.call(sid, "assign", staff="Ada", title="Menu page", objective="Add a menu page", deliverable="tbd", boundaries="Touch nothing else")
        with pytest.raises(Refused, match="a title and the four parts"):
            await r.call(sid, "assign", staff="Ada", **BRIEF)
        with pytest.raises(Refused, match="no task nope"):
            await r.call(sid, "assign", staff="Ada", task_id="nope")
        assert await r.board.list(actor=sid) == before, "a refused hand-over leaves nothing on the board"

        said = await r.call(sid, "assign", staff="Ada", title="Menu page", priority=2, **BRIEF)
        assert said.startswith("Ada will start ") and "effect " in said
        task_id = said.split()[3]
        await until(lambda: len(runtime.started) == 1, "the approved launch started Ada")
        [created] = [e for e in await events(r.manager, "task.created") if e.payload["task_id"] == task_id]
        [assigned] = [e for e in await events(r.manager, "task.assigned") if e.payload["task_id"] == task_id]
        assert created.payload["actor"] == assigned.payload["actor"] == "agent"
        assert created.payload["actor_id"] == assigned.payload["actor_id"] == f"orchestrator:{sid}"
        [started] = runtime.started
        assert started.task is not None and started.task.id == task_id and started.origin == "orchestrator"

        # An existing task with half a brief is completed by the call.
        half = await board_task(r.manager, r.project, "Prices", brief={"objective": "Put prices on the menu", "deliverable": "", "boundaries": "", "done_when": ""})
        with pytest.raises(Refused, match="deliverable, boundaries, done-when"):
            await r.call(sid, "assign", staff="Ada", task_id=half)
        said = await r.call(sid, "assign", staff="Ada", task_id=half, deliverable="prices.md committed", boundaries="Only prices.md", done_when="prices.md lists every dish")
        assert "Ada will start" in said and "effect " in said
        async def busy() -> bool:
            action = await r.manager.db.fetchone("SELECT id FROM effect_outbox WHERE kind='task.launch'"
                                                 " AND json_extract(payload_json,'$.control.task_id') = ?",
                                                 (half,))
            return action is not None and (await OutboxStore(r.manager.db).view(action["id"]))["wait_reason"] == "busy"
        await until_await(busy, "the assigned member was still working on the first task")
        task = await r.board.get(half, actor=sid)
        assert task["brief"]["deliverable"] == "prices.md committed" and task["assignee_staff_id"]
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_staff_sessions_tool_finds_a_finished_cli_chat(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        assert "StaffSessions" in {tool.name for tool in r.manager.tools.list_all()}
        sid = await office(r)
        cli = ObservedFakeStaffRuntime(kind="cursor")
        cli.manager = r.manager
        r.team.runtimes["cursor"] = cli
        r.team._capacity = Capacity(running=0, cap=20)
        member = await r.manager.staff.hire(r.project.id, name="Ada", harness="cursor", isolation="worktree")
        task_id = await board_task(r.manager, r.project, "Menu")
        queued = await operator_assignment(r.team, member, task_id, wait_for_admission=False)
        async def observed() -> bool:
            view = await OutboxStore(r.manager.db).view(queued["effect_id"])
            return view["state"] == "completed" or view["wait_reason"] is not None
        await until_await(observed, "the CLI launch reached an admission decision")
        view = await OutboxStore(r.manager.db).view(queued["effect_id"])
        assert view["state"] == "completed", (view["wait_reason"], view["wait_detail"], view["error"])
        first = await r.team.live_of(member)
        assert first is not None
        await observed_cli_exit(r.team, first)
        await r.manager.staff.end_session(first.id, "terminal closed")
        listed = await r.call(sid, "staff_sessions", staff="Ada", task_id=task_id)
        assert first.id in listed and "ready to resume" in listed
        assert "Assign(task_id=..." in listed
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_assignments_past_the_concurrency_wait_in_the_queue(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        await r.call(sid, "team", concurrency=1)
        for name in ("Ada", "Ben"):
            await r.manager.staff.hire(r.project.id, name=name, role="Menu", isolation="shared")
        first = await r.call(sid, "assign", staff="Ada", title="Menu page", **BRIEF)
        second = await r.call(sid, "assign", staff="Ben", title="Photos", **BRIEF)
        assert first.startswith("Ada will start") and second.startswith("Ben will start")
        async def deferred() -> bool:
            rows = await r.manager.db.fetchall("SELECT id FROM effect_outbox WHERE kind = 'task.launch'"
                                               " ORDER BY created_at,id")
            if len(rows) != 2:
                return False
            views = [await OutboxStore(r.manager.db).view(row["id"]) for row in rows]
            return any(view["wait_reason"] == "project" for view in views)
        await until_await(deferred, "the second launch waited for the project limit")
        assert len(runtime.started) == 1
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- talking, reading and control ------------------------------------------------------------------------------------


async def test_the_tell_tool_hands_its_timing_through_and_offers_only_the_three(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        await working(r)
        schema = tell().definition.parameters.properties["when"]
        assert schema["enum"] == ["now", "after_turn", "interrupt"]
        assert "now (the default)" in tell().definition.description
        locator.register(SessionServices(session_id=sid, workspace_dir=tmp_path, max_tool_output_chars=4000, extra={"manager": r.manager}))
        try:
            context = ToolContext(tenant_id="t", run_id="r", session_id=sid, metadata={"tool_call_id": "c"})
            later = await tell().invoke(context, {"staff": "Ada", "text": "then the prices", "when": "after_turn"})
            default = await tell().invoke(context, {"staff": "Ada", "text": "use the owner's sheet"})
        finally:
            locator.unregister(sid)
        assert not later.is_error and not default.is_error
        assert [(m.text, m.mode) for _, m in runtime.sent] == [("then the prices", "after_turn"), ("use the owner's sheet", "now")]
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_tell_passes_its_timing_to_the_runtime_and_returns_the_receipt(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        await working(r)
        for when in ("after_turn", "now", "interrupt"):
            said = await r.call(sid, "tell", staff="Ada", text=f"a message for {when}", when=when)
            assert said.endswith(f"({when}): submitted")
        # Without a timing a message goes into the running turn: a message to someone at work is
        # about that work, and one held until the turn's end arrived after the work it was for.
        await r.call(sid, "tell", staff="Ada", text="the default")
        assert [(m.mode, m.origin) for _, m in runtime.sent] == [("after_turn", "orchestrator"), ("now", "orchestrator"), ("interrupt", "orchestrator"), ("now", "orchestrator")]
        runtime.receipt = Receipt("submitted", degraded_to="after_turn")
        waits = await r.call(sid, "tell", staff="Ada", text="now")
        assert "cannot take a message into a running turn" in waits and "Interrupt first if it cannot wait" in waits
        runtime.receipt = Receipt("submitted", degraded_to="interrupt")
        assert "turn was interrupted" in await r.call(sid, "tell", staff="Ada", text="now")
        runtime.receipt = Receipt("failed", "the terminal is gone")
        assert await r.call(sid, "tell", staff="Ada", text="now") == f"message {runtime.sent[-1][1].id} to Ada (now): failed — the terminal is gone"
        for old in ("queue", "steer", "shout"):
            with pytest.raises(Refused, match="when is one of now, after_turn, interrupt"):
                await r.call(sid, "tell", staff="Ada", text="x", when=old)
        with pytest.raises(Refused, match="empty"):
            await r.call(sid, "tell", staff="Ada", text="  ")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_read_staff_pages_are_bounded_carry_their_session_and_mark_the_turn_seen(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r, page=ReadPage("The menu page is committed.", "42", False))
        sid = await office(r)
        member, live = await working(r)
        await r.team.ingress.status(live, "turn_done_unseen")
        said = await r.call(sid, "read_staff", staff="Ada")
        assert "The menu page is committed." in said and f"cursor='{live.id}~42'" in said
        assert runtime.reads[-1][1].max_chars == r.manager.config.staff.read_default_chars
        assert (await r.manager.staff.live(member.id)).status == "idle"  # type: ignore[union-attr]

        runtime.page = ReadPage("x" * 100, "43", True)
        said = await r.call(sid, "read_staff", staff="Ada", what="turns", turns=500, max_chars=10**9, cursor=f"{live.id}~42")
        request = runtime.reads[-1][1]
        assert (request.what, request.turns, request.cursor, request.max_chars) == ("turns", 20, "42", r.manager.config.staff.read_max_chars)
        assert f"[cut to {r.manager.config.staff.read_max_chars} characters]" in said
        await r.call(sid, "read_staff", staff="Ada", max_chars=1)
        assert runtime.reads[-1][1].max_chars == 200
        with pytest.raises(Refused, match="another of Ada's sessions"):
            await r.call(sid, "read_staff", staff="Ada", cursor="ss-old~42")
        with pytest.raises(Refused, match="what is one of"):
            await r.call(sid, "read_staff", staff="Ada", what="mind")

        # An ended session can still be read: what a released member did is still worth knowing.
        await r.team.release(member)
        runtime.page = ReadPage("last words", None, False)
        said = await r.call(sid, "read_staff", staff="Ada")
        assert "ended (released)" in said and "last words" in said
        await r.manager.staff.hire(r.project.id, name="Ben", role="Photos", isolation="shared")
        with pytest.raises(Refused, match="Ben has not worked yet"):
            await r.call(sid, "read_staff", staff="Ben")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_report_reaches_the_orchestrator_whole_and_can_be_read_again(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A member put its whole answer in a checkpoint and ended its turn on "answered above": the wake
    line cut the report at 300 characters, ReadStaff's last reply said nothing, and a second, longer
    report was cut at 2000 when it was taken in. The report now arrives whole, can be read again with
    what="reports", and is cut only past a far larger bound, saying so."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r, page=ReadPage("I've answered the GPU question.", None, False))
        sid = await office(r)
        _, live = await working(r)
        answer = "\n".join(f"{n}. " + "The frames are drawn in Chrome on the GPU. " * 8 for n in range(1, 9))
        assert len(answer) > 2000
        await r.team.ingress.report(live, "checkpoint", answer, call_id="fixture-checkpoint-one")
        reported = (await r.manager.bus.latest(events_filter("staff.report"), limit=1))[0]
        line = await r.orch.line(await r.refreshed(), reported)
        assert "8. The frames" in line and "…" not in line, line[-200:]

        said = await r.call(sid, "read_staff", staff="Ada", what="reports", max_chars=20000)
        assert "1. The frames" in said and "8. The frames" in said and "checkpoint" in said

        told = await r.team.ingress.report(live, "done", "x" * 20000, call_id="fixture-done-one")
        assert "reported done" in told
        last = (await r.manager.bus.latest(events_filter("staff.report"), limit=1))[0]
        assert last.payload["text"].endswith("[full report in report receipt]")
        from daedalus.extensions.staff_results import StaffReportService

        assert await StaffReportService(r.team.app).original(
            r.project.id, live.session.task_id, last.payload["report_id"],
        ) == b"x" * 20000
    finally:
        await close_team(r.manager)
        await r.manager.close()


def events_filter(kind: str) -> Any:
    from daedalus.host.events import EventFilter

    return EventFilter(types=(kind,))


async def test_interrupt_pause_and_release_act_through_the_runtime_and_are_journaled(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        member, live = await working(r)
        assert "turn is stopped" in await r.call(sid, "interrupt", staff="Ada")
        assert runtime.interrupted == [live.id]
        assert "pauses when the current turn ends" in await r.call(sid, "pause", staff="Ada")
        assert (await r.manager.staff.live(member.id)).pause_requested  # type: ignore[union-attr]
        assert "session ended" in await r.call(sid, "release", staff="Ada")
        assert runtime.stopped == [live.id]
        [exited] = [e for e in await events(r.manager, "staff.status") if e.payload["status"] == "exited"]
        assert exited.payload["actor"] == "orchestrator"
        assert exited.payload["actor_id"] == f"orchestrator:{sid}"
        assert await r.orch.classify(r.project.id, exited) is None, "its own release does not wake it"
        texts = await journal_texts(r)
        assert {"The orchestrator interrupted Ada's turn.", "The orchestrator paused Ada.", "The orchestrator released Ada."} <= set(texts)
        with pytest.raises(Refused, match="no live session"):
            await r.call(sid, "release", staff="Ada")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_release_needs_a_current_operator_approval_before_stopping_a_worker(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = (await r.orch.enable(r.project.id, autonomy="ask")).settings.orchestrator.session_id
        member, live = await working(r)
        with pytest.raises(Refused, match="operator-issued orchestrator grant|no current grant"):
            await r.call(sid, "release", staff=member.name)
        assert runtime.stopped == [] and await r.team.live_of(member) is not None
        await approve_coordinator(r, sid)
        assert "session ended" in await r.call(sid, "release", staff=member.name)
        assert runtime.stopped == [live.id]
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- the rest of the office --------------------------------------------------------------------------------------------


async def test_harnesses_lists_the_executors_and_is_the_orchestrators_alone(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        assert "Harnesses" in ORCHESTRATOR_ONLY_TOOLS
        said = await r.call(sid, "harnesses")
        assert said.startswith("Daedalus: ready") and "not set up" in said
        assert "models (presets)" in await r.call(sid, "harnesses", harness="daedalus")
        r.team.app.extensions["harness"] = HarnessCatalog(HarnessStore(r.manager.db))
        said = await r.call(sid, "harnesses")
        assert "Claude Code in the container: not installed" in said and "no staff runtime here yet" in said
        assert "Claude Code in the container: steer" in await r.call(sid, "harnesses", harness="claude")
        # The models the operator chose to offer are the ones named; the rest are counted and still usable.
        store = r.team.app.extensions["harness"].store
        install = InstallInfo(True, "/home/operator/.local/bin/claude", "2.1.281", "native")
        await store.record_check("container", "claude", install=install, login=LoginState("yes"), catalog=Catalog(models=("opus", "sonnet", "claude-opus-5-5", "claude-sonnet-5-5")))
        assert "  models: opus, sonnet, claude-opus-5-5, claude-sonnet-5-5" in await r.call(sid, "harnesses", harness="claude")
        await store.set_offered_models("container", "claude", ["claude-opus-5-5"])
        said = await r.call(sid, "harnesses", harness="claude")
        assert "  models the operator offers: claude-opus-5-5; 3 other models of this CLI can still be named when asked for" in said, said
        ordinary = await r.manager.create_session("work", project_id=r.project.id)
        assert "Harnesses" in r.manager.blocked_tools_for(ordinary)
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_ask_operator_and_report_refuse_a_dispatch_the_project_does_not_have(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        sid = await office(r)
        with pytest.raises(Refused, match="no dispatch"):
            await r.call(sid, "ask_operator", title="Ship on Friday?", text="Ship on Friday?", dispatch_id="d12345")
        assert await r.manager.asks.open_for(r.project.id) == [], "nothing is asked under a dispatch that is not there"
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_full_autonomy_leaves_the_command_line_agents_permission_mode_alone(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id, autonomy="full")
        assert r.team.permission_level(project) == r.team.permission_level(await r.orch.update(r.project.id, autonomy="normal")) == "edits"
        assert r.team.permission_level(await r.orch.update(r.project.id, autonomy="ask")) == "ask"
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_replaced_orchestrator_cannot_use_the_team_tools(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        old = await office(r)
        await working(r)
        await r.orch.replace(r.project.id, "testing")
        for operation, kwargs in (("hire", {"name": "Rex", "role": "Review"}), ("tell", {"staff": "Ada", "text": "hi"}), ("release", {"staff": "Ada"})):
            with pytest.raises(Exception, match="replaced by"):
                await r.call(old, operation, **kwargs)
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- acceptance --------------------------------------------------------------------------------------------------------


async def test_a_scripted_orchestrator_hires_assigns_answers_from_the_brief_and_escalates(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The acceptance: over two real turns the orchestrator hires a Daedalus reviewer, assigns a task,
    answers the reviewer's question from the brief and escalates its permission; every action leaves
    a journal entry or an event."""
    contract = {
        "objective": "Review the menu page for wrong prices",
        "deliverable": "A list of wrong prices in the report",
        "boundaries": "Read only; change no file",
        "done_when": "Every dish on the menu was checked",
    }
    r = await rig(settings, db, tmp_path, [
        {"tool": "Hire", "args": {"name": "Rex", "role": "Reviewer", "isolation": "shared"}},
        {"tool": "Assign", "args": {"staff": "Rex", "title": "Review the menu",
                                    "expected_collection_revision": 1, **contract}},
        {"tool": "Journal", "args": {"text": "Rex reviews the menu", "why": "a second pair of eyes before Friday"}},
        {"text": "Rex is reviewing the menu."},
    ])
    try:
        r.manager.config.orchestrator.batch_seconds = 1
        runtime = fake(r)
        sid = await office(r)
        await r.manager.projects.set_brief(r.project.id, "constraints", "Prices are in euros and include tax.", "operator")
        await r.manager.submit(sid, "Have someone review the menu before Friday.")
        await until_await(lambda: _idle(r.manager, sid), "the first turn ended")
        rex = await r.manager.staff.by_name(r.project.id, "Rex")
        assert rex is not None and rex.created_by == "orchestrator"
        await until(lambda: bool(runtime.started), "the queued review assignment admitted Rex")
        [started] = runtime.started
        assert started.staff.id == rex.id and started.origin == "orchestrator"
        live = await r.team.live_of(rex)
        assert live is not None

        short = itertools.chain(["qask01", "qperm1"], itertools.repeat("qzzzz9"))
        r.manager.asks._short_id = lambda: next(short)  # type: ignore[method-assign]
        r.provider.script += [
            {"tool": "Answer", "args": {"request_id": "qask01", "text": "Euros with tax included — the brief's constraints say so"}},
            {"tool": "Answer", "args": {"request_id": "qperm1", "escalate": True, "text": "deny", "basis": "not covered by the allowances"}},
            {"text": "Answered Rex; the permission is the operator's."},
        ]
        await r.team.ingress.question(live, "q-1", "Are the prices with or without tax?", [])
        await r.team.ingress.permission(live, "p-1", "Exec", "curl https://example.invalid/prices")

        async def answered() -> bool:
            resolved = await r.manager.db.fetchone("SELECT 1 FROM asks WHERE short_id = 'qask01' AND resolved_at IS NOT NULL")
            escalated = await r.manager.db.fetchone("SELECT 1 FROM asks WHERE short_id = 'qperm1' AND routed_to = 'operator'")
            return resolved is not None and escalated is not None and await _idle(r.manager, sid)

        await until_await(answered, "the orchestrator answered and escalated")
        [(_, ref, decision)] = runtime.answered
        assert ref.kind == "question" and decision.by == "orchestrator" and "tax included" in (decision.text or "")
        permission = await r.manager.db.fetchone("SELECT routed_to, suggestion, resolved_at FROM asks WHERE short_id = 'qperm1'")
        assert permission is not None and (permission["routed_to"], permission["suggestion"], permission["resolved_at"]) == ("operator", "deny", None)

        texts = await journal_texts(r)
        assert "The orchestrator hired Rex (Daedalus): Reviewer" in texts
        assert any(t.startswith("Rex reviews the menu") for t in texts)
        assert any("Request qperm1 went to the operator: not covered by the allowances" in t for t in texts)
        task_events = [e for e in await events(r.manager, "task.created", "task.assigned") if e.payload.get("title") == "Review the menu"]
        assert {e.type for e in task_events} == {"task.created", "task.assigned"}
        assert all(e.payload.get("actor_id") == f"orchestrator:{sid}" for e in task_events)
        [answered_event] = [e for e in await events(r.manager, "ask.answered") if e.staff_id == rex.id]
        assert answered_event.payload["via"] == "orchestrator"
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- a card nobody works any more ------------------------------------------------------------------------------


def _task_in(said: str) -> str:
    found = re.search(r"(?:started on|will start) (\w+) \"", said)
    assert found is not None, said
    return found.group(1)


async def test_a_card_left_by_a_helper_that_died_goes_to_the_next_member_in_one_call(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A one-off helper exited a third of a second after its start. Its card stayed in doing under its
    name, and every way to hand the work to the member who had done it before was refused: Assign as
    "being worked on by someone else", Release as "no live session", Dismiss as "nobody called" once
    the operator had dismissed the helper. The orchestrator hired a second helper to free the card."""
    r = await rig(settings, db, tmp_path)
    try:
        runtime = ObservedFakeStaffRuntime(kind="cursor")
        runtime.manager = r.manager
        r.team.runtimes["cursor"] = runtime
        r.team._capacity = Capacity()
        sid = await office(r)
        await r.manager.staff.hire(r.project.id, name="Ira", role="Video", harness="cursor", isolation="worktree")
        helper = await r.manager.staff.hire(r.project.id, name="mediafix", role="Video", harness="cursor", isolation="worktree", one_off=True)
        task_id = _task_in(await r.call(sid, "assign", staff="mediafix", title="Fix the two captions", **BRIEF))
        async def helper_started() -> bool:
            return await r.team.live_of(helper) is not None

        await until_await(helper_started, "the helper's queued launch was admitted")
        await until_await(lambda: admitted(r, task_id), "the helper's launch effect completed")
        live = await r.team.live_of(helper)
        assert live is not None

        # The process went on its own: the card is nobody's, and says why.
        await observed_cli_exit(r.team, live)
        await r.team.ingress.ended(live, "Claude Code exited with code 1; its screen last showed: Error: unknown model")
        row = await task_row(r.manager, task_id)
        assert (row["status"], row["assignee_staff_id"]) == ("todo", None)
        assert "mediafix no longer works this card: the session ended (Claude Code exited with code 1; its screen last showed: Error: unknown model)" in row["notes"]

        said = await r.call(sid, "assign", staff="Ira", task_id=task_id)
        assert said.startswith(f"Ira will start {task_id}")
        await until_await(lambda: admitted(r, task_id), "Ira's reassigned launch completed")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_card_held_by_a_member_who_is_gone_is_passed_on_released_or_freed_by_dismiss(settings: Settings, db: Database, tmp_path: Path) -> None:
    """Cards an earlier host left in doing under a member whose session had ended, and who the operator
    had dismissed since: each tool that frees a card frees this one too, and a live member keeps theirs."""
    r = await rig(settings, db, tmp_path)
    try:
        runtime = ObservedFakeStaffRuntime(kind="cursor")
        runtime.manager = r.manager
        r.team.runtimes["cursor"] = runtime
        r.team._capacity = Capacity()
        sid = await office(r)
        await r.manager.staff.hire(r.project.id, name="Ira", role="Video", harness="cursor", isolation="worktree")
        helper = await r.manager.staff.hire(r.project.id, name="mediafix", role="Video", harness="cursor", isolation="worktree", one_off=True)
        first = _task_in(await r.call(sid, "assign", staff="mediafix", title="Fix the two captions", **BRIEF))
        async def helper_started() -> bool:
            return await r.team.live_of(helper) is not None

        await until_await(helper_started, "the helper's queued launch was admitted")
        await until_await(lambda: admitted(r, first), "the helper's launch effect completed")
        live = await r.team.live_of(helper)
        assert live is not None
        await observed_cli_exit(r.team, live)
        await r.manager.staff.end_session(live.id, "Claude Code exited with code 1")
        await r.manager.staff.archive(helper.id, by="operator")
        assert (await task_row(r.manager, first))["status"] == "doing"

        released = await r.call(sid, "release", staff="mediafix")
        assert first in released and (await task_row(r.manager, first))["status"] == "todo"
        said = await r.call(sid, "assign", staff="Ira", task_id=first)
        assert said.startswith(f"Ira will start {first}")
        await until_await(lambda: admitted(r, first), "Ira's handed-off launch completed")

        second = await board_task(r.manager, r.project, "Second cut")
        third = await board_task(r.manager, r.project, "Third cut")
        await r.manager.db.execute("UPDATE board_tasks SET status = 'doing', assignee_staff_id = ? WHERE id IN (?, ?)", (helper.id, second, third))
        said = await r.call(sid, "release", staff="mediafix")
        assert said == f"mediafix had no live session; the cards {second}, {third} they held in doing went back to todo, unassigned"
        assert [(await task_row(r.manager, t))["status"] for t in (second, third)] == ["todo", "todo"]
        with pytest.raises(Refused, match="has no live session and holds no card in doing"):
            await r.call(sid, "release", staff="mediafix")

        await r.manager.db.execute("UPDATE board_tasks SET status = 'doing', assignee_staff_id = ? WHERE id = ?", (helper.id, second))
        said = await r.call(sid, "dismiss", staff="mediafix")
        assert said == f"mediafix was already dismissed; the card {second} they left in doing went back to todo, unassigned"
        assert await r.call(sid, "dismiss", staff="mediafix") == "mediafix was already dismissed"

        # This ownership check needs both workers active at the same time.
        await r.orch.update(r.project.id, concurrency=2)
        # Silence is not gone: a member whose live session is on the card keeps it.
        ada, ada_live = await working(r, "Ada", "Menu page")
        await r.team.ingress.status(ada_live, "no_signal")
        with pytest.raises(Refused, match="stop or reconcile the current execution before reassigning"):
            await r.call(sid, "assign", staff="Ira", task_id=ada_live.session.task_id)
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_reading_a_member_whose_session_ended_says_what_there_is(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        member, live = await working(r)
        await r.manager.staff.end_session(live.id, "Claude Code exited with code 1")
        said = await r.call(sid, "read_staff", staff="Ada", what="screen")
        assert said.endswith("(ended: Claude Code exited with code 1): there is no screen of an ended session; what=\"last\" or \"reports\" shows what it left")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_card_set_aside_by_hand_stays_so_and_starts_when_it_is_assigned(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A manually blocked card stays blocked across other board writes until explicitly released."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        r.team._capacity = Capacity()
        sid = await office(r)
        updates = (await r.call(sid, "tasks", op="create", title="Safe updates", **BRIEF)).split()[0]
        await r.call(sid, "tasks", op="move", task_id=updates, status="blocked",
                     note="the prototype stopped; an architecture has to be chosen")
        await r.call(sid, "tasks", op="create", title="Unrelated", **BRIEF)
        assert (await task_row(r.manager, updates))["status"] == "blocked"

        await r.manager.staff.hire(r.project.id, name="release", role="Updates", isolation="shared")
        await r.call(sid, "tasks", op="move", task_id=updates, status="todo")
        said = await r.call(sid, "assign", staff="release", task_id=updates)
        assert said.startswith(f"release will start {updates}")
        await until_await(lambda: admitted(r, updates), "the explicitly unblocked task launched")

        with pytest.raises(Refused, match=rf"task {updates} cannot wait for itself.*depends_on=\['{updates}'\]"):
            await r.call(sid, "assign", staff="release", task_id=updates, depends_on=[updates])
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_report_is_never_folded_into_the_more_line(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A wake-up of forty events showed thirty and counted the rest: a member's report among the rest
    reached the orchestrator as a number."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        await office(r)
        _, live = await working(r)
        answer = "\n".join(f"{n}. " + "The frames are drawn in Chrome on the GPU. " * 4 for n in range(1, 6))
        await r.team.ingress.report(live, "checkpoint", answer, call_id="fixture-checkpoint-two")
        reported = (await r.manager.bus.latest(events_filter("staff.report"), limit=1))[0]
        news = [AppEvent(i + 1, "2026-01-01T10:00:00+00:00", "run.started", {}, project_id=r.project.id) for i in range(39)]
        batch = [*news[:34], reported, *news[34:]]
        assert r.manager.config.orchestrator.batch_max_lines == 30 and len(batch) == 40
        text = await r.orch.render(r.project.id, Batch(tuple(Pending(e, Wake(str(e.seq)), time.monotonic()) for e in batch), urgent=True))
        assert "Ada reported checkpoint" in text and "5. The frames are drawn" in text
        assert "- … and 9 more" in text

        await r.team.ingress.implicit_report(live, "turn_done", "word " * 400)
        implicit = (await r.manager.bus.latest(events_filter("staff.report"), limit=1))[0]
        line = await r.orch.line(await r.refreshed(), implicit)
        assert "word " * 190 in line and "ReadStaff(\"Ada\")" in line
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_assign_to_a_worktree_member_in_a_plain_folder_is_refused_with_the_way_out(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The orchestrator assigns a task in a folder that is no git repository to a member hired for a
    worktree of their own. The start used to go ahead in the folder itself; Assign now refuses, says
    the card stays unstarted, and names both ways out before a launch enters the queue."""
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        r.team._capacity = Capacity()
        sid = await office(r)
        notes = tmp_path / "notes"
        notes.mkdir()
        await r.manager.projects.add_folder(r.project.id, str(notes))
        await r.manager.staff.hire(r.project.id, name="Ada", role="Menu", isolation="worktree")
        with pytest.raises(Refused) as refused:
            await r.call(sid, "assign", staff="Ada", title="Tidy the notes",
                         folder=str(notes), wait_for_admission=False, **BRIEF)
        said = str(refused.value)
        assert "is not a git repository" in said
        assert "a folder of the project that is a git repository" in said and "isolation to shared" in said
        task = await r.manager.db.fetchone("SELECT id,status FROM board_tasks WHERE title = 'Tidy the notes'")
        assert task is not None and task["status"] == "todo"
        assert await r.manager.db.fetchone("SELECT id FROM effect_outbox WHERE kind = 'task.launch'"
                                            " AND json_extract(payload_json,'$.control.task_id') = ?", (task["id"],)) is None
        assert runtime.started == []
    finally:
        await close_team(r.manager)
        await r.manager.close()
