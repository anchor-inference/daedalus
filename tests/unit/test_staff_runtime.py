"""Staff at work: assignment, the launch queue, statuses, requests, and the Daedalus runtime end to end."""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.notifications import ActionConflict, ActionRequest
from daedalus.extensions.runtime_observations import observe_exit
from daedalus.extensions.staff import AlreadyAnswered, Team
from daedalus.extensions.task_launch import TaskLaunchEffect
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.launch_queue import Entry, LaunchQueue
from daedalus.host.session_runner import SessionManager
from daedalus.host.staff_daedalus import DaedalusStaffRuntime
from daedalus.staff_runtime import FakeStaffRuntime, LiveSession, OutgoingMessage, Started
from daedalus.stores.control import ControlConflict
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.projects import FolderSpec, Project
from daedalus.stores.staff import Staff, StaffError
from tests.support.authorized_launch import operator_assignment, operator_task
from tests.support.waiting import until_await
from tests.unit.test_session_runner import ScriptedProvider, _manager

BRIEF = {"objective": "Add a menu page", "deliverable": "menu.md in the repository", "boundaries": "Touch nothing else", "done_when": "menu.md is committed"}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def repository(root: Path) -> Path:
    repo = root / "bakery"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "someone")
    git(repo, "config", "user.email", "someone@example.invalid")
    (repo / "README.md").write_text("bakery\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "start")
    return repo


class Notes:
    """The notifications service as far as the team uses it: what it was asked to post."""

    def __init__(self) -> None:
        self.posted: list[Any] = []

    async def post(self, draft: Any) -> None:
        self.posted.append(draft)


class Capacity:
    def __init__(self, running: int = 0, cap: int = 20, waiting: int = 0, down: str | None = None) -> None:
        self.running_now, self.cap_now, self.waiting_now, self.down = running, cap, waiting, down

    async def running(self) -> int:
        return self.running_now

    def cap(self) -> int:
        return self.cap_now

    def waiting(self) -> int:
        return self.waiting_now

    def unavailable(self, env: str) -> str | None:
        return self.down


class ObservedFakeStaffRuntime(FakeStaffRuntime):
    """A fake runtime records an actual host-owned session or terminal binding."""

    manager: SessionManager | None = None

    async def _session(self, req: Any) -> str:
        assert self.manager is not None
        metadata = {"staff_id": req.staff.id, "staff_session_id": req.staff_session_id,
                    "telegram_detached": True}
        if req.worktree is not None:
            metadata.update({"worktree": str(req.worktree.path), "worktree_cwd": str(req.worktree.cwd)})
        state = await self.manager.create_session(
            "Fixture worker", project_id=req.project.id, folder_id=req.folder.id, metadata=metadata,
        )
        return state.session.id

    async def _terminal(self, req: Any) -> str:
        assert self.manager is not None
        terminal_id = f"term-{uuid.uuid4().hex}"
        await self.manager.db.execute(
            "INSERT INTO terminals(id,env,project_id,owner_kind,owner_id,cwd,status,created_at,ptyd_instance)"
            " VALUES (?,?,?,?,?,?,'running',?,?)",
            (terminal_id, req.env, req.project.id, "staff", req.staff.id, str(req.cwd),
             datetime.now(UTC).isoformat(), f"fake-{uuid.uuid4().hex}"),
        )
        return terminal_id

    async def start(self, req: Any) -> Started:
        result = await super().start(req)
        if self.kind == "daedalus":
            return Started(result.terminal_id, result.cli_session_id, result.transcript_ref,
                           session_id=await self._session(req))
        return Started(await self._terminal(req), result.cli_session_id, result.transcript_ref)

    async def resume(self, req: Any, prior: LiveSession) -> Started:
        result = await super().resume(req, prior)
        if self.kind == "daedalus":
            return Started(result.terminal_id, result.cli_session_id, result.transcript_ref,
                           session_id=await self._session(req))
        return Started(await self._terminal(req), result.cli_session_id, result.transcript_ref)


async def team_for(settings: Settings, manager: SessionManager, *, capacity: Any = None, stagger: int = 0) -> Team:
    executions = ExecutionStore(manager.db)
    executions.acquire()
    await executions.boot()
    app = SimpleNamespace(manager=manager, extensions={}, settings=settings, notifications=Notes(),
                          db=manager.db, executions=executions)
    team = Team(app, capacity=capacity)  # type: ignore[arg-type]
    app.extensions["staff"] = team
    team.attach()
    dispatcher = EffectDispatcher(OutboxStore(manager.db))
    dispatcher.register("task.launch", TaskLaunchEffect(app))
    app.extensions["effects"] = dispatcher
    running = asyncio.create_task(dispatcher.run())
    dispatcher.enable()
    _OWNED_TEAMS[manager] = (team, running, executions)
    manager.config.staff.launch_stagger_seconds = stagger
    return team


_OWNED_TEAMS: dict[SessionManager, tuple[Team, asyncio.Task[None], ExecutionStore]] = {}


async def close_team(manager: SessionManager) -> None:
    owned = _OWNED_TEAMS.pop(manager, None)
    if owned is None:
        return
    team, running, executions = owned
    running.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await running
    team.queue.close()
    executions.release()


async def project_with(manager: SessionManager, folder: Path, *, orchestrator: bool = True, autonomy: str = "normal", concurrency: int = 6) -> Project:
    project = await manager.projects.create("Bakery", [FolderSpec(str(folder))])
    await manager.projects.update_orchestrator(project.id, enabled=orchestrator, autonomy=autonomy, concurrency=concurrency)
    await manager.projects.ensure_roots()
    found = await manager.projects.get(project.id)
    assert found is not None
    return found


async def board_task(manager: SessionManager, project: Project, title: str, *, priority: int = 3, brief: dict[str, str] | None = None, status: str = "todo") -> str:
    if status != "todo":
        raise ValueError("a fixture cannot create a completed task without an exact result")
    return await operator_task(manager.db, project.id, title, priority=priority,
                               brief=BRIEF if brief is None else brief)


async def events(manager: SessionManager, *types: str, **ids: str) -> list[AppEvent]:
    return await manager.bus.replay(0, EventFilter(types=types, **ids), limit=5000)


async def effect_waits(manager: SessionManager, effect_id: str, reason: str) -> dict[str, Any]:
    observed: dict[str, Any] = {}

    async def waiting() -> bool:
        view = await OutboxStore(manager.db).view(effect_id)
        if view["state"] == "pending" and view["wait_reason"] == reason:
            observed.update(view)
            return True
        return False

    await until_await(waiting, f"the effect waited for {reason}")
    return observed


async def status_of(manager: SessionManager, member: Staff) -> str:
    live = await manager.staff.live(member.id)
    return live.status if live is not None else "off"


async def task_row(manager: SessionManager, task_id: str) -> dict[str, Any]:
    row = await manager.db.fetchone("SELECT * FROM board_tasks WHERE id = ?", (task_id,))
    assert row is not None
    return dict(row)


# -- end to end on a scripted model ---------------------------------------------------------------


async def test_an_assigned_task_runs_end_to_end_in_its_worktree(settings: Settings, db: Database, tmp_path: Path) -> None:
    repo = repository(tmp_path)
    script = [
        {"tool": "AskOrchestrator", "args": {"question": "Prices in euros or dollars?", "options": ["euros", "dollars"], "context": "the menu shows prices"}},
        {"tool": "Write", "args": {"path": "menu.md", "content": "# Menu\n\nbread 3 EUR\n"}},
        {"tool": "Exec", "args": {"command": "git add -A && git commit -qm 'Add the menu'"}},
        {"tool": "Report", "args": {"kind": "done", "note": "menu.md committed", "artifacts": ["menu.md"], "remember": "the bakery prices in euros"}},
        {"text": "The menu page is committed."},
    ]
    provider = ScriptedProvider(script)
    manager = await _manager(settings, db, provider)
    try:
        team = await team_for(settings, manager)
        project = await project_with(manager, repo)
        ada = await manager.staff.hire(project.id, name="Ada", role="Menu page", isolation="worktree")
        task_id = await board_task(manager, project, "Menu page")

        assigned = await operator_assignment(team, ada, task_id)
        assert assigned["state"] == "queued"
        live = await manager.staff.live(ada.id)
        assert live is not None and live.session_id and live.branch and live.branch.startswith("agent/ada/")
        state = await manager.get_state(live.session_id)
        assert state is not None
        # The session works in the member's worktree, and its walls write there, not in the checkout.
        assert state.workspace == Path(live.worktree_path or "") and state.workspace.parent == repo / ".agents" / "worktrees"
        assert state.services is not None and state.services.walls is not None
        assert state.workspace in state.services.walls.writable and repo not in state.services.walls.writable
        assert "Objective: Add a menu page" in (await manager.sessions.list_transcript(live.session_id))[0].content_blocks[0].text  # type: ignore[union-attr]

        async def asked() -> bool:
            return bool(await manager.asks.open_for(project.id))

        await until_await(asked, "the question reached the request list")
        [ask] = await manager.asks.open_for(project.id)
        assert (ask.kind, ask.routed_to, ask.origin, ask.staff_id) == ("question", "orchestrator", "staff", ada.id)
        assert ask.short_id.startswith("q") and len(ask.short_id) == 6 and ask.request_ref == state.pending.tool_call_id  # type: ignore[union-attr]

        async def waiting() -> bool:
            return await status_of(manager, ada) == "question" and not state.running

        await until_await(waiting, "the member waited on its question")
        answered = await team.answer(ask.short_id, text="euros", selected=["euros"], by="orchestrator")
        assert answered["delivered"] is True and answered["ask"]["resolved_by"] == "orchestrator"
        with pytest.raises(AlreadyAnswered, match="already answered by the orchestrator"):
            await team.answer(ask.id, text="dollars", by="operator")

        async def reviewed() -> bool:
            return (await task_row(manager, task_id))["status"] == "review" and await status_of(manager, ada) == "turn_done_unseen"

        await until_await(reviewed, "the task reached review")
        result = next(m for m in provider.requests[1].messages if m.role.value == "tool")
        assert json.loads(result.content_blocks[0].content)["source"] == "orchestrator"  # type: ignore[union-attr]
        row = await task_row(manager, task_id)
        assert (row["assignee_staff_id"], row["merge_state"], row["branch"], row["folder_id"]) == (ada.id, "proposed", live.branch, project.primary.id)  # type: ignore[union-attr]
        assert "menu.md" in git(repo, "log", "--name-only", "--format=", live.branch or "")
        assert git(repo, "status", "--porcelain") == "", "the operator's checkout is untouched"
        assert "prices in euros" in ((await manager.staff.get(ada.id)) or ada).notes

        statuses = [e.payload["status"] for e in await events(manager, "staff.status", staff_id=ada.id)]
        assert statuses == ["starting", "working", "question", "working", "turn_done_unseen"]
        [report] = await events(manager, "staff.report")
        assert (report.payload["kind"], report.payload["refs"], report.staff_id, report.project_id) == ("done", ["menu.md"], ada.id, project.id)
        receipt = await manager.db.fetchone(
            "SELECT id, attempt_id, outcome FROM result_receipts WHERE task_id = ?", (task_id,),
        )
        assert receipt is not None and receipt["outcome"] == "complete"
        assert receipt["attempt_id"] == row["current_attempt_id"]
        moves = [(e.payload["from"], e.payload["to"]) for e in await events(manager, "task.moved")]
        assert moves == [("todo", "doing")]
        pending = await events(manager, "ask.pending")
        assert pending and all(e.staff_id == ada.id and e.project_id == project.id for e in pending)
        assert (await events(manager, "ask.answered"))[0].payload["via"] == "orchestrator"
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_refused_call_becomes_a_permission_the_orchestrator_grants_from_the_brief(settings: Settings, db: Database, tmp_path: Path) -> None:
    repo = repository(tmp_path)
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "curl -s https://other.example/menu"}}, {"text": "waiting for the permission"}, {"text": "fetched"}])
    manager = await _manager(settings, db, provider)
    try:
        manager.config.policy.egress_allow = ["github.com"]
        team = await team_for(settings, manager)
        project = await project_with(manager, repo)
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        await operator_assignment(team, ada, await board_task(manager, project, "Menu"))
        live = await manager.staff.live(ada.id)
        assert live is not None and live.session_id

        async def permission() -> bool:
            return await status_of(manager, ada) == "permission" and any(a.kind == "permission" for a in await manager.asks.open_for(project.id))

        await until_await(permission, "the refusal became a permission request")
        [ask] = await manager.asks.open_for(project.id)
        assert ask.routed_to == "orchestrator" and "other.example" in ask.text
        await until_await(lambda: _finished(provider), "the refusal reached the model")
        refusal = [m for m in provider.requests[1].messages if m.role.value == "tool"][-1]
        assert "gone to your orchestrator" in refusal.content_blocks[0].content  # type: ignore[union-attr]

        with pytest.raises(StaffError, match="quoted verbatim"):
            await team.answer(ask.id, allow=True, by="orchestrator", basis="it seems fine")
        await manager.projects.set_brief(project.id, "allowed_without_operator", "- Fetch pages from other.example for the menu\n- Run the tests", "operator")
        granted = await team.answer(ask.id, allow=True, by="orchestrator", basis="Fetch pages from other.example for the menu")
        assert granted["delivered"] is True
        gate = manager.policy_gate(live.session_id, "check")
        assert gate.decide("Exec", {"command": "curl -s https://other.example/menu"}).action == "allow"
        journal = await manager.projects.journal(project.id)
        assert any(e.kind == "grant" and "basis: Fetch pages" in e.text for e in journal)
        resolved = await events(manager, "permission.resolved")
        assert [(e.payload["decision"], e.payload["via"], e.staff_id) for e in resolved] == [("allow", "orchestrator", ada.id)]

        await until_await(lambda: _settled(manager, provider, 3), "the member was told to retry")
        told = [b.text for m in provider.requests[2].messages for b in m.content_blocks if hasattr(b, "text")]
        assert any("granted request" in t and "Retry the call" in t for t in told)
    finally:
        await close_team(manager)
        await manager.close()


async def test_the_first_answer_wins_between_the_session_and_the_orchestrator(settings: Settings, db: Database, tmp_path: Path) -> None:
    provider = ScriptedProvider([{"tool": "AskOrchestrator", "args": {"question": "Which oven?"}}, {"text": "ok"}])
    manager = await _manager(settings, db, provider)
    try:
        team = await team_for(settings, manager)
        project = await project_with(manager, repository(tmp_path))
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        await operator_assignment(team, ada, await board_task(manager, project, "Oven"))
        live = await manager.staff.live(ada.id)
        assert live is not None and live.session_id
        state = await manager.get_state(live.session_id)
        assert state is not None

        async def waiting() -> bool:
            return bool(await manager.asks.open_for(project.id)) and state.pending is not None and not state.running

        await until_await(waiting, "the question was asked")
        [ask] = await manager.asks.open_for(project.id)
        outcomes = await asyncio.gather(
            manager.answer(live.session_id, [{"custom": "the left one"}], via="app"),
            team.answer(ask.id, text="the right one", by="orchestrator"),
            return_exceptions=True,
        )
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        assert len(failures) == 1 and "already answered" in str(failures[0])
        resolved = await manager.asks.get(ask.id)
        assert resolved is not None and not resolved.open
        winner = "operator" if isinstance(outcomes[1], BaseException) else "orchestrator"
        assert resolved.resolved_by == winner
        await until_await(lambda: _settled(manager, provider, 2), "the run went on and ended")
        result = next(m for m in provider.requests[1].messages if m.role.value == "tool")
        payload = json.loads(result.content_blocks[0].content)  # type: ignore[union-attr]
        assert payload["source"] == ("user" if winner == "operator" else "orchestrator")
        assert payload["answers"][0]["custom"] == ("the left one" if winner == "operator" else "the right one")
    finally:
        await close_team(manager)
        await manager.close()


async def _finished(provider: ScriptedProvider) -> bool:
    return len(provider.requests) >= 2


async def _settled(manager: SessionManager, provider: ScriptedProvider, requests: int) -> bool:
    """The model was asked ``requests`` times and every run has ended: a test closes only an idle manager."""
    return len(provider.requests) >= requests and not any(s.running for s in manager._states.values())


async def test_staff_have_their_tools_and_nobody_else_does(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        team = await team_for(settings, manager)
        team.runtimes["daedalus"] = FakeStaffRuntime(kind="daedalus")
        project = await project_with(manager, repository(tmp_path))
        ordinary = await manager.create_session("ordinary", project_id=project.id)
        staff = await manager.create_session("staff", project_id=project.id, metadata={"staff_id": "st-1", "staff_session_id": "ss-1"})
        blocked_staff = manager.blocked_tools_for(staff)
        blocked_ordinary = manager.blocked_tools_for(ordinary)
        assert {"AskUser", "SpawnAgent", "ScheduleCreate", "SelfPropose"} <= blocked_staff
        assert not {"Report", "AskOrchestrator", "Exec", "SubAgent"} & blocked_staff
        assert {"Report", "AskOrchestrator"} <= blocked_ordinary and "AskUser" not in blocked_ordinary
        # A subagent of a staff member inherits its restrictions, and reports through its leader.
        sub = await manager.create_session("sub", project_id=project.id, metadata={"subagent_of": staff.session.id})
        assert {"AskUser", "SpawnAgent", "Report", "AskOrchestrator"} <= manager.blocked_tools_for(sub)
    finally:
        await close_team(manager)
        await manager.close()


# -- the team with a fake runtime ----------------------------------------------------------------------


async def fake_team(settings: Settings, db: Database, tmp_path: Path, *, capacity: Any = None, concurrency: int = 6, **project: Any) -> tuple[SessionManager, Team, FakeStaffRuntime, Project]:
    manager = await _manager(settings, db, ScriptedProvider([]))
    team = await team_for(settings, manager, capacity=capacity)
    runtime = ObservedFakeStaffRuntime(kind="daedalus")
    runtime.manager = manager
    team.runtimes["daedalus"] = runtime
    found = await project_with(manager, repository(tmp_path), concurrency=concurrency, **project)
    return manager, team, runtime, found


async def observed_cli_exit(team: Team, live: LiveSession) -> None:
    """A fake daemon proves the exact terminal instance ended before a new launch."""
    terminal_id = live.terminal_id
    assert terminal_id
    row = await team.app.db.fetchone("SELECT ptyd_instance FROM terminals WHERE id = ?", (terminal_id,))
    assert row is not None and row["ptyd_instance"]
    await team.app.db.execute("UPDATE terminals SET status = 'exited' WHERE id = ?", (terminal_id,))
    await team.app.db.execute("INSERT INTO terminal_exit_observations(terminal_id,runtime_instance,observed_at)"
                              " VALUES (?,?,?)", (terminal_id, row["ptyd_instance"], datetime.now(UTC).isoformat()))
    assert await observe_exit(team.app, staff_session_id=live.id, runtime_ref=terminal_id,
                              observed_status="exited", runtime_instance=row["ptyd_instance"])


async def test_a_dirty_worktree_refuses_done_and_a_pause_commits_it(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="worktree")
        task_id = await board_task(manager, project, "Menu")
        await operator_assignment(team, ada, task_id)
        [req] = runtime.started
        assert req.worktree is not None and req.cwd == req.worktree.cwd and req.team_token and req.first_message_id.startswith("sm-")
        assert "Branch: agent/ada/" in req.first_message and "own git worktree" in req.brief_text
        live = await team.live_of(ada)
        assert live is not None
        (req.worktree.path / "menu.md").write_text("bread\n")
        with pytest.raises(ValueError, match="uncommitted changes"):
            await team.ingress.report(live, "done", "finished", call_id="fixture-done")
        assert (await task_row(manager, task_id))["status"] == "doing"

        working = await team.pause(ada)
        assert working["paused"] is False and "when the current turn ends" in working["note"]
        await team.ingress.status(live, "turn_done_unseen")
        paused = await team.pause(ada)
        assert paused["paused"] is True and paused["commit"]
        assert "wip:" in git(req.worktree.path, "log", "-1", "--format=%s")
        assert await status_of(manager, ada) == "idle"
        told = await team.ingress.report((await team.live_of(ada)) or live, "done", "finished",
                                         call_id="fixture-done")
        assert "in review" in told and (await task_row(manager, task_id))["status"] == "review"
    finally:
        await close_team(manager)
        await manager.close()


async def test_silence_is_watched_only_while_a_turn_runs_on_a_task_still_being_worked(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A member that handed its task in, finished its turn, or has no task is quiet by right: nothing
    marks it silent, and its health line does not call it so."""
    manager, team, _runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        task_id = await board_task(manager, project, "Menu")
        await operator_assignment(team, ada, task_id)
        live = await team.live_of(ada)
        assert live is not None
        await team.ingress.status(live, "working")
        later = datetime.now(UTC) + timedelta(minutes=manager.config.staff.silence_minutes + 1)
        for settled in ("review", "done", "dropped"):
            await manager.db.execute("UPDATE board_tasks SET status = ? WHERE id = ?", (settled, task_id))
            await team.tick(later)
            assert await status_of(manager, ada) == "working", settled
            health = await team.health((await team.live_of(ada)) or live, later)
            assert (health.silent, health.silent_s) == (False, None), settled
        for quiet in ("idle", "turn_done_unseen"):
            await manager.db.execute("UPDATE board_tasks SET status = 'doing' WHERE id = ?", (task_id,))
            await team.ingress.status(live, quiet)
            await team.tick(later)
            assert await status_of(manager, ada) == quiet
        await team.ingress.status(live, "working")
        await manager.db.execute("UPDATE staff_sessions SET task_id = NULL WHERE id = ?", (live.id,))
        await team.tick(later)
        assert await status_of(manager, ada) == "working", "no task, no silence"
        await manager.db.execute("UPDATE staff_sessions SET task_id = ? WHERE id = ?", (task_id, live.id))
        assert (await team.health((await team.live_of(ada)) or live, later)).silent is True
        await team.tick(later)
        assert await status_of(manager, ada) == "no_signal", "a turn on a task being worked still goes grey"
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_status_change_cannot_replace_a_running_attempt(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A board status is not physical exit proof, so it cannot release an owned worker slot."""
    manager, team, _runtime, project = await fake_team(settings, db, tmp_path, capacity=Capacity())
    try:
        cli = FakeStaffRuntime(kind="claude")
        team.runtimes["claude"] = cli
        cleo = await manager.staff.hire(project.id, name="Cleo", harness="claude", isolation="shared")
        scouting = await board_task(manager, project, "Scouting")
        assert (await operator_assignment(team, cleo, scouting))["state"] == "queued"
        live = await team.live_of(cleo)
        assert live is not None and live.session.kind == "cli"
        await team.ingress.status(live, "no_signal", detail="read from the screen")
        task = await team.task(scouting)
        assert task is not None
        await team._move_task(task, "done", actor="operator")
        attempt = await manager.db.fetchone(
            "SELECT state, provider_session_ref FROM execution_attempts WHERE task_id = ?", (scouting,),
        )
        assert attempt is not None and attempt["state"] == "running" and attempt["provider_session_ref"]
        assert [r.task.id for r in cli.started] == [scouting]
        assert (await manager.staff.live(cleo.id)).task_id == scouting
    finally:
        await close_team(manager)
        await manager.close()


async def test_silence_goes_grey_and_a_request_left_too_long_goes_to_the_operator(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        await operator_assignment(team, ada, await board_task(manager, project, "Menu"))
        live = await team.live_of(ada)
        assert live is not None
        await team.ingress.status(live, "working")
        later = datetime.now(UTC) + timedelta(minutes=manager.config.staff.silence_minutes + 1)
        await team.tick(later)
        assert await status_of(manager, ada) == "no_signal"

        live = await team.live_of(ada)
        assert live is not None
        ask_id = await team.ingress.question(live, "team:1", "Which flour?", ["wheat", "rye"])
        ask = await manager.asks.get(ask_id)
        assert ask is not None and ask.routed_to == "orchestrator" and await status_of(manager, ada) == "question"
        pending = await events(manager, "ask.pending")
        assert pending[-1].payload["request_ref"] == f"staff:{live.id}:{ask_id}" and pending[-1].staff_id == ada.id
        await team.tick(datetime.now(UTC) + timedelta(minutes=manager.config.staff.ask_escalate_minutes + 1))
        escalated = await manager.asks.get(ask_id)
        assert escalated is not None and escalated.routed_to == "operator"
        assert any("waiting for you" in d.title and d.level == "urgent" for d in team.app.notifications.posted)  # type: ignore[attr-defined]
        assert any(e.kind == "escalation" for e in await manager.projects.journal(project.id))

        answered = await team.answer(ask_id, selected=["rye"], by="operator", via="app")
        assert answered["delivered"] is True
        assert runtime.answered[-1][2].selected == ["rye"] and runtime.answered[-1][2].by == "operator"
        assert [e.payload["via"] for e in await events(manager, "ask.answered")] == ["app"]
        assert await status_of(manager, ada) == "working"
    finally:
        await close_team(manager)
        await manager.close()


async def test_autonomy_decides_who_answers(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path, autonomy="ask")
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        await operator_assignment(team, ada, await board_task(manager, project, "Menu"))
        live = await team.live_of(ada)
        assert live is not None
        question = await team.ingress.question(live, "team:q", "Which flour?", [])
        permission = await team.ingress.permission(live, "req-7", "Bash", "npm install")
        assert (await manager.asks.get(permission)).routed_to == "operator"  # type: ignore[union-attr]
        with pytest.raises(StaffError, match="operator's to answer"):
            await team.answer(permission, allow=True, by="orchestrator", basis="anything at all here")
        suggested = await team.answer(question, text="rye", by="orchestrator")
        assert suggested["state"] == "suggested" and suggested["ask"]["routed_to"] == "operator" and suggested["ask"]["suggestion"] == "rye"
        assert runtime.answered == []
        denied = await team.answer(permission, allow=False, text="not now", by="operator")
        assert denied["delivered"] is True and runtime.answered[-1][1].request_ref == "req-7"
        resolved = await events(manager, "permission.resolved")
        assert resolved[-1].payload["decision"] == "deny"

        await manager.projects.update_orchestrator(project.id, enabled=False)
        assert team.route((await manager.projects.get(project.id)), "question") == "operator"  # type: ignore[arg-type]
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_one_off_goes_with_its_task(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        helper = await manager.staff.hire(project.id, name="Helper", isolation="shared", one_off=True)
        task_id = await board_task(manager, project, "Look it up")
        await operator_assignment(team, helper, task_id)
        live = await team.live_of(helper)
        assert live is not None
        await manager.db.execute("UPDATE board_tasks SET status = 'done' WHERE id = ?", (task_id,))
        await team._task_finished(task_id)
        member = await manager.staff.get(helper.id)
        assert member is not None and not member.active
        assert runtime.stopped == [live.id] and await manager.staff.live(helper.id) is None
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_crashed_session_requires_physical_exit_proof_before_relaunch(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        task_id = await board_task(manager, project, "Menu")
        await operator_assignment(team, ada, task_id)
        first = await team.live_of(ada)
        assert first is not None
        await team.ingress.status(first, "error", detail="the model failed")
        await manager.staff.end_session(first.id, "crashed: the model failed")
        await manager.db.execute("UPDATE board_tasks SET status = 'todo' WHERE id = ?", (task_id,))
        with pytest.raises(ControlConflict, match="stop or reconcile the previous execution"):
            await operator_assignment(team, ada, task_id)
        assert len(runtime.started) == 1
        row = await manager.db.fetchone("SELECT state FROM execution_attempts WHERE task_id = ?", (task_id,))
        assert row is not None and row["state"] == "running"
    finally:
        await close_team(manager)
        await manager.close()


async def test_cli_resume_lists_archived_staff_and_uses_the_selected_conversation(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, _runtime, project = await fake_team(settings, db, tmp_path, capacity=Capacity())
    runtime = ObservedFakeStaffRuntime(kind="cursor")
    runtime.manager = manager
    team.runtimes["cursor"] = runtime
    try:
        first = await manager.staff.hire(project.id, name="Ada", harness="cursor", isolation="shared")
        task_id = await board_task(manager, project, "Menu")
        assert (await operator_assignment(team, first, task_id))["state"] == "queued"
        original = await team.live_of(first)
        assert original is not None and original.cli_session_id
        await team.ingress.report(original, "done", "first menu finished", call_id="first-menu-result")
        await observed_cli_exit(team, original)
        await manager.staff.end_session(original.id, "terminal closed")
        await manager.staff.archive(first.id)
        again = await manager.staff.hire(project.id, name="Ada", harness="cursor", isolation="shared")
        next_task = await board_task(manager, project, "Prices")
        history = await team.resume_sessions(again, task_id=next_task)
        assert [(row["id"], row["owner_name"], row["can_resume"]) for row in history] == [(original.id, "Ada", True)]
        assert (await operator_assignment(team, again, next_task, resume_from=original.id))["state"] == "queued"
        assert runtime.resumed[-1][1] == original.id
        resumed = await team.live_of(again)
        assert resumed is not None and resumed.session.predecessor_id == original.id
        assert resumed.cli_session_id == original.cli_session_id
        assert resumed.terminal_id != original.terminal_id
        assert not next(row for row in await team.resume_sessions(again, task_id=next_task) if row["id"] == original.id)["can_resume"]
    finally:
        await close_team(manager)
        await manager.close()


async def test_cli_resume_rejects_another_folder_or_harness(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, _runtime, project = await fake_team(settings, db, tmp_path, capacity=Capacity())
    cursor_runtime = ObservedFakeStaffRuntime(kind="cursor")
    cursor_runtime.manager = manager
    team.runtimes["cursor"] = cursor_runtime
    team.runtimes["claude"] = FakeStaffRuntime(kind="claude")
    try:
        cursor = await manager.staff.hire(project.id, name="Ada", harness="cursor", isolation="shared")
        task_id = await board_task(manager, project, "Menu")
        await operator_assignment(team, cursor, task_id)
        original = await team.live_of(cursor)
        assert original is not None
        await team.ingress.report(original, "done", "menu finished", call_id="menu-result")
        await observed_cli_exit(team, original)
        await manager.staff.end_session(original.id, "terminal closed")
        next_task = await board_task(manager, project, "Next menu")
        claude = await manager.staff.hire(project.id, name="Ben", harness="claude", isolation="shared")
        with pytest.raises(AssertionError, match="selected harness"):
            await operator_assignment(team, claude, next_task, resume_from=original.id)
        other = tmp_path / "other"
        other.mkdir()
        folder = await manager.projects.add_folder(project.id, str(other))
        await manager.db.execute("UPDATE board_tasks SET folder_id = ? WHERE id = ?", (folder.id, next_task))
        elsewhere = await manager.staff.hire(project.id, name="Cleo", harness="cursor", isolation="shared", folder_id=folder.id)
        with pytest.raises(AssertionError, match="another folder path"):
            await operator_assignment(team, elsewhere, next_task, resume_from=original.id)
    finally:
        await close_team(manager)
        await manager.close()


async def test_cli_resume_follows_launch_path_after_folder_is_added_again(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, _runtime, project = await fake_team(settings, db, tmp_path, capacity=Capacity())
    runtime = ObservedFakeStaffRuntime(kind="cursor")
    runtime.manager = manager
    team.runtimes["cursor"] = runtime
    try:
        old_folder = project.primary
        member = await manager.staff.hire(project.id, name="Ada", harness="cursor", isolation="shared")
        task_id = await board_task(manager, project, "Menu")
        await operator_assignment(team, member, task_id)
        original = await team.live_of(member)
        assert original is not None
        await team.ingress.report(original, "done", "menu finished", call_id="menu-result")
        await observed_cli_exit(team, original)
        await manager.staff.end_session(original.id, "terminal closed")
        await manager.staff.archive(member.id)
        other = tmp_path / "other"
        other.mkdir()
        await manager.projects.add_folder(project.id, str(other))
        await manager.projects.remove_folder(project.id, old_folder.id)
        added_again = await manager.projects.add_folder(project.id, str(old_folder.path))
        assert added_again.id != old_folder.id
        next_task = await board_task(manager, project, "Prices")
        await manager.db.execute("UPDATE board_tasks SET folder_id = ? WHERE id = ?", (added_again.id, next_task))
        again = await manager.staff.hire(project.id, name="Ada", harness="cursor", isolation="shared", folder_id=added_again.id)
        history = await team.resume_sessions(again, task_id=next_task)
        assert [row["id"] for row in history if row["can_resume"]] == [original.id]
        assert (await operator_assignment(team, again, next_task, resume_from=original.id))["state"] == "queued"
        assert runtime.resumed[-1][1] == original.id
    finally:
        await close_team(manager)
        await manager.close()


async def test_queued_cli_resume_survives_queue_rebuild(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, _runtime, project = await fake_team(settings, db, tmp_path, capacity=Capacity())
    runtime = ObservedFakeStaffRuntime(kind="cursor")
    runtime.manager = manager
    team.runtimes["cursor"] = runtime
    try:
        member = await manager.staff.hire(project.id, name="Ada", harness="cursor", isolation="shared")
        task_id = await board_task(manager, project, "Menu")
        await operator_assignment(team, member, task_id)
        first = await team.live_of(member)
        assert first is not None
        await team.ingress.report(first, "done", "first menu finished", call_id="first-menu-result")
        await observed_cli_exit(team, first)
        await manager.staff.end_session(first.id, "terminal closed")
        next_task = await board_task(manager, project, "Prices")
        team._capacity = Capacity(running=1, cap=1)
        queued = await operator_assignment(team, member, next_task, resume_from=first.id,
                                           wait_for_admission=False)
        assert queued["state"] == "queued"
        async def deferred() -> bool:
            row = await manager.db.fetchone("SELECT state,error FROM effect_outbox WHERE id = ?",
                                            (queued["effect_id"],))
            return row is not None and row["state"] == "pending" and bool(row["error"])

        await until_await(deferred, "the durable launch waited for machine capacity")
        _, prior_dispatch, executions = _OWNED_TEAMS[manager]
        prior_dispatch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await prior_dispatch
        dispatcher = EffectDispatcher(OutboxStore(manager.db))
        dispatcher.register("task.launch", TaskLaunchEffect(team.app))
        team.app.extensions["effects"] = dispatcher
        current_dispatch = asyncio.create_task(dispatcher.run())
        dispatcher.enable()
        _OWNED_TEAMS[manager] = (team, current_dispatch, executions)
        team._capacity = Capacity()
        dispatcher.notify()

        async def admitted() -> bool:
            row = await manager.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?",
                                            (queued["effect_id"],))
            return row is not None and row["state"] == "completed"

        await until_await(admitted, "the retained command admitted the resumed CLI")
        assert runtime.resumed[-1][1] == first.id
        assert (await manager.staff.live(member.id)).task_id == next_task
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_task_without_its_brief_or_runtime_is_refused(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path, capacity=Capacity())
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        thin = await board_task(manager, project, "Thin", brief={"objective": "x"})
        with pytest.raises(AssertionError, match="task brief is incomplete"):
            await operator_assignment(team, ada, thin)
        cleo = await manager.staff.hire(project.id, name="Cleo", harness="claude", isolation="shared")
        with pytest.raises(AssertionError, match="Claude Code staff cannot be started here yet"):
            await operator_assignment(team, cleo, await board_task(manager, project, "Full"))
        assert await manager.staff.live(cleo.id) is None
    finally:
        await close_team(manager)
        await manager.close()


async def test_the_seventh_waits_for_a_slot_and_the_most_urgent_goes_first(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        members = [await manager.staff.hire(project.id, name=f"Worker {n}", isolation="shared") for n in range(8)]
        for n, member in enumerate(members[:6]):
            assigned = await operator_assignment(team, member, await board_task(manager, project, f"Task {n}"))
            assert assigned["state"] == "queued"
            live = await team.live_of(member)
            assert live is not None
            await team.ingress.status(live, "working")
        late = await operator_assignment(team, members[6], await board_task(manager, project, "Later", priority=4),
                                         wait_for_admission=False)
        # The board hands its own view of the task; an id does as well.
        urgent_id = await board_task(manager, project, "Urgent", priority=1)
        urgent = await operator_assignment(team, members[7], {"id": urgent_id}, wait_for_admission=False)

        async def both_deferred() -> bool:
            views = [await OutboxStore(manager.db).view(effect_id) for effect_id in
                     (late["effect_id"], urgent["effect_id"])]
            return all(view["state"] == "pending" and view["wait_reason"] == "project" for view in views)

        await until_await(both_deferred, "the two launches waited for a project slot")
        late_view = await OutboxStore(manager.db).view(late["effect_id"])
        urgent_view = await OutboxStore(manager.db).view(urgent["effect_id"])
        assert urgent_view["priority"] == 1 and late_view["priority"] == 4

        freed = await team.live_of(members[0])
        assert freed is not None
        await team.ingress.report(freed, "done", "finished", call_id="first-worker-result")
        await team.ingress.status(freed, "turn_done_unseen")
        team.app.extensions["effects"].notify()

        async def urgent_started() -> bool:
            return (await OutboxStore(manager.db).view(urgent["effect_id"]))["state"] == "completed"

        try:
            await until_await(urgent_started, "the urgent launch won the released project slot")
        except AssertionError as exc:
            views = [await OutboxStore(manager.db).view(item["effect_id"]) for item in (late, urgent)]
            raise AssertionError(f"{views}; statuses={[await status_of(manager, item) for item in members]}") from exc
        assert await status_of(manager, members[7]) == "starting"
        assert await status_of(manager, members[6]) == "off"
        assert runtime.started[-1].task.id == urgent_id
        assert len(runtime.started) == 7
    finally:
        await close_team(manager)
        await manager.close()


async def test_command_line_staff_wait_for_the_machine_and_daedalus_staff_do_not(settings: Settings, db: Database, tmp_path: Path) -> None:
    capacity = Capacity(running=20, cap=20)
    manager, team, runtime, project = await fake_team(settings, db, tmp_path, capacity=capacity)
    try:
        cli = FakeStaffRuntime(kind="claude")
        team.runtimes["claude"] = cli
        cleo = await manager.staff.hire(project.id, name="Cleo", harness="claude", isolation="shared")
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        waits = await operator_assignment(team, cleo, await board_task(manager, project, "Terminal work"),
                                          wait_for_admission=False)
        assert waits["state"] == "queued"
        assert "20 of the machine's 20 terminal sessions" in (
            await effect_waits(manager, waits["effect_id"], "machine"))["wait_detail"]
        goes = await operator_assignment(team, ada, await board_task(manager, project, "Daedalus work"))
        assert goes["state"] == "queued" and cli.started == []

        capacity.waiting_now, capacity.running_now = 1, 19
        team.app.extensions["effects"].notify()
        assert (await effect_waits(manager, waits["effect_id"], "machine"))["wait_detail"]
        capacity.waiting_now, capacity.down = 0, "not_running"
        team.app.extensions["effects"].notify()
        await effect_waits(manager, waits["effect_id"], "terminals")
        capacity.down = None
        team.app.extensions["effects"].notify()

        async def cli_started() -> bool:
            return (await OutboxStore(manager.db).view(waits["effect_id"]))["state"] == "completed"

        await until_await(cli_started, "the CLI launch passed machine admission")
        assert len(cli.started) == 1 and cli.started[0].team_url.endswith(f"/api/team/{cli.started[0].staff_session_id}")
    finally:
        await close_team(manager)
        await manager.close()


async def test_without_the_terminals_service_command_line_staff_wait_with_the_reason(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        team.runtimes["codex"] = FakeStaffRuntime(kind="codex")
        assert team.capacity() is None
        max_ = await manager.staff.hire(project.id, name="Max", harness="codex", isolation="shared")
        waits = await operator_assignment(team, max_, await board_task(manager, project, "Terminal work"),
                                          wait_for_admission=False)
        assert waits["state"] == "queued"
        view = await effect_waits(manager, waits["effect_id"], "terminals")
        assert "terminals service is not running" in view["wait_detail"]
        assert view["task_id"] and view["wait_position"] == 1
    finally:
        await close_team(manager)
        await manager.close()


async def test_launches_of_a_project_are_spaced_apart() -> None:
    clock = [100.0]
    launched: list[str] = []

    async def launch(entry: Entry) -> None:
        launched.append(entry.task_id)

    async def nothing(entry: Entry) -> str | None:
        return None

    async def count(project_id: str) -> int:
        return len(launched)

    async def six(project_id: str) -> int:
        return 6

    queue = LaunchQueue(concurrency=six, active=count, ready=nothing, free=nothing, launch=launch, capacity=lambda: None, stagger=lambda: 5, clock=lambda: clock[0])
    try:
        first = await queue.request(Entry("p", "s1", "One", "t1", 3, False, "operator"))
        second = await queue.request(Entry("p", "s2", "Two", "t2", 3, False, "operator"))
        assert first.state == "started" and (second.state, second.reason) == ("queued", "stagger")
        clock[0] += 2
        await queue.pump("p")
        assert launched == ["t1"] and queue.queue("p")[0]["detail"].startswith("starts in about 3 s")
        clock[0] += 3.5
        await queue.pump("p")
        assert launched == ["t1", "t2"]
    finally:
        queue.close()


async def test_the_team_server_takes_only_its_own_token(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        cli = FakeStaffRuntime(kind="claude")
        team.runtimes["claude"] = cli
        team._capacity = Capacity()
        cleo = await manager.staff.hire(project.id, name="Cleo", harness="claude", isolation="shared")
        task_id = await board_task(manager, project, "Menu")
        await operator_assignment(team, cleo, task_id)
        [req] = cli.started
        app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={"staff": team}, guard=None)
        api = build_app(app, "tok")  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            url = f"/api/team/{req.staff_session_id}"
            wrong = await client.post(f"{url}/report", headers={"X-Daedalus-Team-Token": "not-it"},
                                      json={"kind": "checkpoint", "note": "half", "client_operation_id": "wrong"})
            assert wrong.status_code == 401
            operator = await client.post(f"{url}/report", headers={"X-Daedalus-Token": "tok"},
                                         json={"kind": "checkpoint", "note": "half", "client_operation_id": "operator"})
            assert operator.status_code == 401, "the operator's token is not a team token"
            ok = await client.post(f"{url}/report", headers={"X-Daedalus-Team-Token": req.team_token},
                                   json={"kind": "done", "note": "all", "client_operation_id": "done"})
            assert ok.status_code == 200 and "in review" in ok.json()["text"]
            asked = await client.post(f"{url}/ask", headers={"X-Daedalus-Team-Token": req.team_token}, json={"question": "Next?", "options": ["a", "b"]})
            assert asked.status_code == 200 and asked.json()["short_id"].startswith("q")

            listing = await client.get(f"/api/asks?project={project.id}", headers={"X-Daedalus-Token": "tok"})
            [ask] = listing.json()["asks"]
            first = await client.post(f"/api/asks/{ask['short_id']}/answer", headers={"X-Daedalus-Token": "tok"}, json={"selected": ["a"]})
            assert first.status_code == 200 and first.json()["delivered"] is True
            again = await client.post(f"/api/asks/{ask['id']}/answer", headers={"X-Daedalus-Token": "tok"}, json={"selected": ["b"]})
            assert again.status_code == 409 and "already answered by the operator" in again.json()["detail"]

            told = await client.post(f"/api/staff/{cleo.id}/messages", headers={"X-Daedalus-Token": "tok"}, json={"text": "also the prices", "when": "now"})
            assert told.status_code == 200 and told.json()["state"] == "submitted"
            assert cli.sent[-1][1].mode == "now"
            # The operator's message takes the same default as the orchestrator's: into the running turn.
            later = await client.post(f"/api/staff/{cleo.id}/messages", headers={"X-Daedalus-Token": "tok"}, json={"text": "and the menu"})
            assert later.status_code == 200 and cli.sent[-1][1].mode == "now"
            old = await client.post(f"/api/staff/{cleo.id}/messages", headers={"X-Daedalus-Token": "tok"}, json={"text": "x", "mode": "steer"})
            assert old.status_code == 422, "the old field is gone, not quietly ignored"
            released = await client.post(f"/api/staff/{cleo.id}/release", headers={"X-Daedalus-Token": "tok"}, json={"keep_worktree": True})
            assert released.json() == {"released": True} and cli.stopped
            gone = await client.post(f"{url}/report", headers={"X-Daedalus-Team-Token": req.team_token},
                                     json={"kind": "checkpoint", "note": "late", "client_operation_id": "late"})
            assert gone.status_code == 401
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_restart_offers_assigned_tasks_to_the_queue_again(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path, concurrency=1)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        bo = await manager.staff.hire(project.id, name="Bo", isolation="shared")
        await operator_assignment(team, ada, await board_task(manager, project, "First"))
        live = await team.live_of(ada)
        assert live is not None
        await team.ingress.status(live, "working")
        waiting = await board_task(manager, project, "Second")
        queued = await operator_assignment(team, bo, waiting, wait_for_admission=False)
        view = await effect_waits(manager, queued["effect_id"], "project")
        assert view["task_id"] == waiting and view["wait_position"] == 1

        _, prior_dispatch, executions = _OWNED_TEAMS[manager]
        prior_dispatch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await prior_dispatch
        dispatcher = EffectDispatcher(OutboxStore(manager.db))
        dispatcher.register("task.launch", TaskLaunchEffect(team.app))
        team.app.extensions["effects"] = dispatcher
        current_dispatch = asyncio.create_task(dispatcher.run())
        dispatcher.enable()
        _OWNED_TEAMS[manager] = (team, current_dispatch, executions)
        await effect_waits(manager, queued["effect_id"], "project")
        assert runtime.started[0].task.title == "First" and len(runtime.started) == 1
    finally:
        await close_team(manager)
        await manager.close()


async def test_live_sessions_answer_for_a_member(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        with pytest.raises(StaffError, match="no live session"):
            await team.tell(ada, "hello")
        await operator_assignment(team, ada, await board_task(manager, project, "Menu"))
        live = await team.live_of(ada)
        assert isinstance(live, LiveSession) and live.staff.id == ada.id
        told = await team.tell(ada, "hello", when="after_turn")
        message = await manager.staff.message(told["message_id"])
        assert message is not None and message.state == "submitted" and message.attempts == 1
        assert [e.payload["state"] for e in await events(manager, "staff.message") if e.payload["message_id"] == told["message_id"]] == ["submitted"]
        runtime.receipt = runtime.receipt.__class__("failed", "the terminal is gone")
        failed = await team.tell(ada, "again")
        assert (failed["state"], failed["error"]) == ("failed", "the terminal is gone")
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_notification_answers_a_command_line_request_and_the_router_holds_for_the_orchestrator(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path, capacity=Capacity())
    try:
        cli = FakeStaffRuntime(kind="claude")
        team.runtimes["claude"] = cli
        cleo = await manager.staff.hire(project.id, name="Cleo", harness="claude", isolation="shared")
        await operator_assignment(team, cleo, await board_task(manager, project, "Menu"))
        live = await team.live_of(cleo)
        assert live is not None
        permission = await team.ingress.permission(live, "hook-42", "Bash", "npm install")
        ref = f"staff:{live.id}:{permission}"
        assert (await events(manager, "permission.pending"))[-1].payload["request_ref"] == ref
        request = ActionRequest(ref, "staff", live.id, permission, "allow", None, "push", None)  # type: ignore[arg-type]
        assert (await team.resolve_action(request)).resolution == "allow"
        assert cli.answered[-1][1].request_ref == "hook-42" and cli.answered[-1][2].allow is True
        with pytest.raises(ActionConflict):
            await team.resolve_action(request)
        resolved = (await events(manager, "permission.resolved"))[-1].payload
        assert (resolved["request_ref"], resolved["via"], resolved["decision"]) == (ref, "push", "allow")
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_daedalus_member_is_steered_for_now_and_followed_up_after_the_turn() -> None:
    """A Daedalus member has the host's own queues: now is a steer into the running run, after the
    turn a follow-up it takes when the run ends, and an interrupt stops the run before sending."""
    submitted: list[dict[str, Any]] = []
    stopped: list[str] = []

    async def submit(session_id: str, text: str, **kwargs: Any) -> None:
        submitted.append({"session": session_id, "text": text, **kwargs})

    async def receipt(session_id: str, message_id: str) -> dict[str, Any]:
        return {"status": "consumed" if message_id == "m-now" else "queued"}

    async def stop(session_id: str) -> None:
        stopped.append(session_id)

    manager = SimpleNamespace(submit=submit, stop=stop, live=SimpleNamespace(receipt=receipt), live_state=lambda session_id: None)
    runtime = DaedalusStaffRuntime(manager)  # type: ignore[arg-type]
    live = SimpleNamespace(session_id="s-ada", staff=SimpleNamespace(name="Ada"))
    now = await runtime.send(live, OutgoingMessage("m-now", "use the owner's sheet", "now", "orchestrator"))  # type: ignore[arg-type]
    later = await runtime.send(live, OutgoingMessage("m-later", "then the prices", "after_turn", "operator"))  # type: ignore[arg-type]
    await runtime.send(live, OutgoingMessage("m-stop", "stop, wrong file", "interrupt", "orchestrator"))  # type: ignore[arg-type]
    assert [(s["client_message_id"], s["steer"], s["follow_up"]) for s in submitted] == [("m-now", True, False), ("m-later", False, True), ("m-stop", False, False)]
    assert (now.state, later.state) == ("acknowledged", "submitted")
    assert stopped == ["s-ada"], "only the interrupt stopped the run"


# -- a worktree of one's own, or no start --------------------------------------------------------------


async def plain_folder(manager: SessionManager, project: Project, tmp_path: Path) -> str:
    """A folder of the project that is not a git repository: notes, a download, a home directory."""
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "todo.txt").write_text("bread\n")
    return (await manager.projects.add_folder(project.id, str(notes))).id


async def test_a_worktree_member_is_refused_a_task_whose_folder_has_no_worktree_to_give(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The member is hired for a worktree of their own and the task names a folder git knows nothing
    of. The start once went ahead in the folder itself, with nothing said: a command-line member ran
    with the operator's home directory as its sandbox's writable root. It is refused when assigned,
    with what to do about it, and nothing starts."""
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        folder_id = await plain_folder(manager, project, tmp_path)
        ada = await manager.staff.hire(project.id, name="Ada", isolation="worktree")
        task_id = await board_task(manager, project, "Tidy the notes")
        await manager.db.execute("UPDATE board_tasks SET folder_id = ? WHERE id = ?", (folder_id, task_id))
        with pytest.raises(AssertionError) as refused:
            await operator_assignment(team, ada, task_id)
        said = str(refused.value)
        assert "notes is not a git repository" in said and "Ada works in a git worktree of their own" in said
        assert "a folder of the project that is a git repository" in said and "isolation to shared" in said and "read-only" in said
        assert runtime.started == [] and await status_of(manager, ada) == "off"
        row = await task_row(manager, task_id)
        assert (row["status"], row["assignee_staff_id"]) == ("todo", ada.id)
        assert not await manager.db.fetchall("SELECT id FROM execution_attempts WHERE task_id = ?", (task_id,))
        assert team.queue.queue(project.id) == []

        # A member who works in the folder itself takes it, in the folder.
        bo = await manager.staff.hire(project.id, name="Bo", isolation="shared")
        shared_task = await board_task(manager, project, "Tidy shared notes")
        await manager.db.execute("UPDATE board_tasks SET folder_id = ? WHERE id = ?", (folder_id, shared_task))
        assert (await operator_assignment(team, bo, shared_task))["state"] == "queued"
        [req] = runtime.started
        assert req.worktree is None and req.cwd == tmp_path / "notes"
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_queued_start_whose_folder_has_no_worktree_to_give_is_taken_back_and_says_why(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The folder can change while the task waits in the queue (the orchestrator moves it to another
    folder of the project). The start refuses as the assignment would have: the task is unassigned,
    the event that wakes the orchestrator carries the whole reason, and the card says it too."""
    manager, team, runtime, project = await fake_team(settings, db, tmp_path, concurrency=1)
    try:
        folder_id = await plain_folder(manager, project, tmp_path)
        bo = await manager.staff.hire(project.id, name="Bo", isolation="shared")
        ada = await manager.staff.hire(project.id, name="Ada", isolation="worktree")
        first = await board_task(manager, project, "Bake")
        assert (await operator_assignment(team, bo, first))["state"] == "queued"
        busy = await team.live_of(bo)
        assert busy is not None
        await team.ingress.status(busy, "working")
        waiting = await board_task(manager, project, "Tidy the notes")
        queued = await operator_assignment(team, ada, waiting, wait_for_admission=False)
        assert queued["state"] == "queued"
        await manager.db.execute("UPDATE board_tasks SET folder_id = ? WHERE id = ?", (folder_id, waiting))

        await team.ingress.report(busy, "done", "first task finished", call_id="first-task-result")
        await team.ingress.status(busy, "turn_done_unseen")
        team.app.extensions["effects"].notify()

        async def refused() -> bool:
            row = await manager.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?",
                                            (queued["effect_id"],))
            return row is not None and row["state"] == "failed"

        await until_await(refused, "the queued command rejected the changed folder")
        assert [r.task.id for r in runtime.started] == [first], "nothing started in the plain folder"
        assert await status_of(manager, ada) == "off"
        row = await task_row(manager, waiting)
        assert row["status"] == "todo" and row["assignee_staff_id"] == ada.id
        effect = await OutboxStore(manager.db).view(queued["effect_id"])
        assert "task folder changed after the launch command" in effect["error"]
        assert not await manager.db.fetchall("SELECT id FROM execution_attempts WHERE task_id = ?", (waiting,))
    finally:
        await close_team(manager)
        await manager.close()


async def test_git_is_asked_whether_a_worktree_can_be_made_not_the_stored_flag(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The stored `is_git` is known only for folders of this process's environment, and is false for
    every host folder: the start that trusted it gave host members no worktree at all. A folder the
    flag calls plain but git calls a repository gets its worktree."""
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="worktree")
        await manager.db.execute("UPDATE project_folders SET is_git = 0 WHERE project_id = ?", (project.id,))
        refreshed = await manager.projects.get(project.id)
        assert refreshed is not None and refreshed.primary is not None and not refreshed.primary.is_git
        assert (await operator_assignment(team, ada, await board_task(manager, project, "Menu")))["state"] == "queued"
        [req] = runtime.started
        assert req.worktree is not None and req.cwd == req.worktree.cwd != refreshed.primary.path
    finally:
        await close_team(manager)
        await manager.close()
