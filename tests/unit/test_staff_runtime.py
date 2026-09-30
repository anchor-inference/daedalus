"""Staff at work: assignment, the launch queue, statuses, requests, and the Daedalus runtime end to end."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.notifications import ActionConflict, ActionRequest
from daedalus.extensions.staff import AlreadyAnswered, Team
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.launch_queue import Entry, LaunchQueue
from daedalus.host.session_runner import SessionManager
from daedalus.host.staff_daedalus import DaedalusStaffRuntime
from daedalus.staff_runtime import FakeStaffRuntime, LiveSession, OutgoingMessage
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec, Project
from daedalus.stores.staff import Staff, StaffError
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


async def team_for(settings: Settings, manager: SessionManager, *, capacity: Any = None, stagger: int = 0) -> Team:
    app = SimpleNamespace(manager=manager, extensions={}, settings=settings, notifications=Notes(), db=manager.db)
    team = Team(app, capacity=capacity)  # type: ignore[arg-type]
    app.extensions["staff"] = team
    team.attach()
    manager.config.staff.launch_stagger_seconds = stagger
    return team


async def project_with(manager: SessionManager, folder: Path, *, orchestrator: bool = True, autonomy: str = "normal", concurrency: int = 6) -> Project:
    project = await manager.projects.create("Bakery", [FolderSpec(str(folder))])
    await manager.projects.update_orchestrator(project.id, enabled=orchestrator, autonomy=autonomy, concurrency=concurrency)
    await manager.projects.ensure_roots()
    found = await manager.projects.get(project.id)
    assert found is not None
    return found


async def board_task(manager: SessionManager, project: Project, title: str, *, priority: int = 3, brief: dict[str, str] | None = None, status: str = "todo") -> str:
    task_id = f"t{abs(hash((project.id, title))) % 10**6:06d}"
    now = datetime.now(UTC).isoformat()
    await manager.db.execute(
        "INSERT INTO board_tasks(id, title, status, priority, created_at, updated_at, project_id, brief_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, title, status, priority, now, now, project.id, json.dumps(BRIEF if brief is None else brief)),
    )
    return task_id


async def events(manager: SessionManager, *types: str, **ids: str) -> list[AppEvent]:
    return await manager.bus.replay(0, EventFilter(types=types, **ids), limit=5000)


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

        assigned = await team.assign(ada, task_id, by="operator")
        assert assigned["state"] == "started"
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
        moves = [(e.payload["from"], e.payload["to"]) for e in await events(manager, "task.moved")]
        assert moves == [("todo", "doing"), ("doing", "review")]
        pending = await events(manager, "ask.pending")
        assert pending and all(e.staff_id == ada.id and e.project_id == project.id for e in pending)
        assert (await events(manager, "ask.answered"))[0].payload["via"] == "orchestrator"
    finally:
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
        await team.assign(ada, await board_task(manager, project, "Menu"))
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
        await manager.close()


async def test_the_first_answer_wins_between_the_session_and_the_orchestrator(settings: Settings, db: Database, tmp_path: Path) -> None:
    provider = ScriptedProvider([{"tool": "AskOrchestrator", "args": {"question": "Which oven?"}}, {"text": "ok"}])
    manager = await _manager(settings, db, provider)
    try:
        team = await team_for(settings, manager)
        project = await project_with(manager, repository(tmp_path))
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        await team.assign(ada, await board_task(manager, project, "Oven"))
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
        await manager.close()


# -- the team with a fake runtime ----------------------------------------------------------------------


async def fake_team(settings: Settings, db: Database, tmp_path: Path, *, capacity: Any = None, concurrency: int = 6, **project: Any) -> tuple[SessionManager, Team, FakeStaffRuntime, Project]:
    manager = await _manager(settings, db, ScriptedProvider([]))
    team = await team_for(settings, manager, capacity=capacity)
    runtime = FakeStaffRuntime(kind="daedalus")
    team.runtimes["daedalus"] = runtime
    found = await project_with(manager, repository(tmp_path), concurrency=concurrency, **project)
    return manager, team, runtime, found


async def test_a_dirty_worktree_refuses_done_and_a_pause_commits_it(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="worktree")
        task_id = await board_task(manager, project, "Menu")
        await team.assign(ada, task_id)
        [req] = runtime.started
        assert req.worktree is not None and req.cwd == req.worktree.cwd and req.team_token and req.first_message_id.startswith("sm-")
        assert "Branch: agent/ada/" in req.first_message and "own git worktree" in req.brief_text
        live = await team.live_of(ada)
        assert live is not None
        (req.worktree.path / "menu.md").write_text("bread\n")
        with pytest.raises(ValueError, match="uncommitted changes"):
            await team.ingress.report(live, "done", "finished")
        assert (await task_row(manager, task_id))["status"] == "doing"

        working = await team.pause(ada)
        assert working["paused"] is False and "when the current turn ends" in working["note"]
        await team.ingress.status(live, "turn_done_unseen")
        paused = await team.pause(ada)
        assert paused["paused"] is True and paused["commit"]
        assert "wip:" in git(req.worktree.path, "log", "-1", "--format=%s")
        assert await status_of(manager, ada) == "idle"
        told = await team.ingress.report((await team.live_of(ada)) or live, "done", "finished")
        assert "in review" in told and (await task_row(manager, task_id))["status"] == "review"
    finally:
        await manager.close()


async def test_silence_is_watched_only_while_a_turn_runs_on_a_task_still_being_worked(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A member that handed its task in, finished its turn, or has no task is quiet by right: nothing
    marks it silent, and its health line does not call it so."""
    manager, team, _runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        task_id = await board_task(manager, project, "Menu")
        await team.assign(ada, task_id)
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
        await manager.close()


async def test_a_task_handed_in_or_done_frees_its_member_at_once(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A command-line member whose session still carries a task that is over, with a status its screen
    last gave, is not busy: the task that waits for it starts as soon as the old one is done or in
    review. Three members once sat "busy with another task" for an hour, their tasks done and their
    CLIs idle, while four new tasks waited for them."""
    manager, team, _runtime, project = await fake_team(settings, db, tmp_path, capacity=Capacity())
    try:
        cli = FakeStaffRuntime(kind="claude")
        team.runtimes["claude"] = cli
        cleo = await manager.staff.hire(project.id, name="Cleo", harness="claude", isolation="shared")
        scouting, next_up, third = [await board_task(manager, project, title) for title in ("Scouting", "Next up", "Third")]
        assert (await team.assign(cleo, scouting))["state"] == "started"
        live = await team.live_of(cleo)
        assert live is not None and live.session.kind == "cli"
        await team.ingress.status(live, "no_signal", detail="read from the screen")
        waits = await team.assign(cleo, next_up)
        assert (waits["state"], waits["reason"]) == ("queued", "busy"), "the scouting task is still being worked"

        task = await team.task(scouting)
        assert task is not None
        await team._move_task(task, "done", actor="operator")

        async def started(task_id: str) -> bool:
            return (await task_row(manager, task_id))["status"] == "doing"

        async with asyncio.timeout(10):
            while not await started(next_up):
                await asyncio.sleep(0.02)
        assert [r.task.id for r in cli.started] == [scouting, next_up]
        after = await manager.staff.live(cleo.id)
        assert after is not None and after.task_id == next_up and after.status == "starting"
        assert team.queue.queue(project.id) == []

        # The same when the member hands its task in for review, from a row still grey.
        await team.ingress.status((await team.live_of(cleo)) or live, "no_signal")
        assert (await team.assign(cleo, third))["reason"] == "busy"
        handed = await team.task(next_up)
        assert handed is not None
        await team._move_task(handed, "review", actor="staff")
        async with asyncio.timeout(10):
            while not await started(third):
                await asyncio.sleep(0.02)
        assert [r.task.id for r in cli.started] == [scouting, next_up, third]
    finally:
        await manager.close()


async def test_silence_goes_grey_and_a_request_left_too_long_goes_to_the_operator(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        await team.assign(ada, await board_task(manager, project, "Menu"))
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
        await manager.close()


async def test_autonomy_decides_who_answers(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path, autonomy="ask")
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        await team.assign(ada, await board_task(manager, project, "Menu"))
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
        await manager.close()


async def test_a_one_off_goes_with_its_task(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        helper = await manager.staff.hire(project.id, name="Helper", isolation="shared", one_off=True)
        task_id = await board_task(manager, project, "Look it up")
        await team.assign(helper, task_id)
        live = await team.live_of(helper)
        assert live is not None
        await manager.db.execute("UPDATE board_tasks SET status = 'done' WHERE id = ?", (task_id,))
        await team._task_finished(task_id)
        member = await manager.staff.get(helper.id)
        assert member is not None and not member.active
        assert runtime.stopped == [live.id] and await manager.staff.live(helper.id) is None
    finally:
        await manager.close()


async def test_a_task_started_again_links_the_session_that_ended(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        task_id = await board_task(manager, project, "Menu")
        await team.assign(ada, task_id)
        first = await team.live_of(ada)
        assert first is not None
        await team.ingress.status(first, "error", detail="the model failed")
        await manager.staff.end_session(first.id, "crashed: the model failed")
        await manager.db.execute("UPDATE board_tasks SET status = 'todo' WHERE id = ?", (task_id,))
        await team.assign(ada, task_id)
        second = await team.live_of(ada)
        assert second is not None and second.session.predecessor_id == first.id
        assert "ended: crashed: the model failed" in runtime.started[-1].first_message
        assert runtime.started[-1].predecessor is not None and runtime.started[-1].predecessor.id == first.id
    finally:
        await manager.close()


async def test_a_task_without_its_brief_or_runtime_is_refused(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        thin = await board_task(manager, project, "Thin", brief={"objective": "x"})
        with pytest.raises(StaffError, match="no deliverable, boundaries, done-when"):
            await team.assign(ada, thin)
        cleo = await manager.staff.hire(project.id, name="Cleo", harness="claude", isolation="shared")
        with pytest.raises(StaffError, match="Claude Code staff cannot be started here yet"):
            await team.assign(cleo, await board_task(manager, project, "Full"))
    finally:
        await manager.close()


async def test_the_seventh_waits_for_a_slot_and_the_most_urgent_goes_first(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        members = [await manager.staff.hire(project.id, name=f"Worker {n}", isolation="shared") for n in range(8)]
        for n, member in enumerate(members[:6]):
            assigned = await team.assign(member, await board_task(manager, project, f"Task {n}"))
            assert assigned["state"] == "started"
            live = await team.live_of(member)
            assert live is not None
            await team.ingress.status(live, "working")
        late = await team.assign(members[6], await board_task(manager, project, "Later", priority=4))
        # The board hands its own view of the task; an id does as well.
        urgent = await team.assign(members[7], {"id": await board_task(manager, project, "Urgent", priority=1)})
        assert (late["state"], late["reason"]) == ("queued", "project")
        assert "6 of the project's 6" in late["detail"]
        assert [q["staff_id"] for q in team.queue.queue(project.id)] == [members[7].id, members[6].id]
        assert team.queue.waiting_for(members[6].id)[0]["position"] == 2

        freed = await team.live_of(members[0])
        assert freed is not None
        await team.ingress.status(freed, "turn_done_unseen")
        await team.queue.pump(project.id)
        assert await status_of(manager, members[7]) == "starting"
        assert await status_of(manager, members[6]) == "off"
        assert [q["staff_id"] for q in team.queue.queue(project.id)] == [members[6].id]
        assert urgent["position"] == 1
    finally:
        await manager.close()


async def test_command_line_staff_wait_for_the_machine_and_daedalus_staff_do_not(settings: Settings, db: Database, tmp_path: Path) -> None:
    capacity = Capacity(running=20, cap=20)
    manager, team, runtime, project = await fake_team(settings, db, tmp_path, capacity=capacity)
    try:
        cli = FakeStaffRuntime(kind="claude")
        team.runtimes["claude"] = cli
        cleo = await manager.staff.hire(project.id, name="Cleo", harness="claude", isolation="shared")
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        waits = await team.assign(cleo, await board_task(manager, project, "Terminal work"))
        assert (waits["state"], waits["reason"]) == ("queued", "machine")
        assert "20 of the machine's 20 terminal sessions" in waits["detail"]
        goes = await team.assign(ada, await board_task(manager, project, "Daedalus work"))
        assert goes["state"] == "started" and cli.started == []

        capacity.waiting_now, capacity.running_now = 1, 19
        await team.queue.pump(project.id)
        assert team.queue.queue(project.id)[0]["reason"] == "machine", "the service's own line was promised places first"
        capacity.waiting_now, capacity.down = 0, "not_running"
        await team.queue.pump(project.id)
        assert team.queue.queue(project.id)[0]["reason"] == "terminals"
        capacity.down = None
        await team.queue.pump(project.id)
        assert len(cli.started) == 1 and cli.started[0].team_url.endswith(f"/api/team/{cli.started[0].staff_session_id}")
        assert team.queue.queue(project.id) == []
    finally:
        await manager.close()


async def test_without_the_terminals_service_command_line_staff_wait_with_the_reason(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        team.runtimes["codex"] = FakeStaffRuntime(kind="codex")
        assert team.capacity() is None
        max_ = await manager.staff.hire(project.id, name="Max", harness="codex", isolation="shared")
        waits = await team.assign(max_, await board_task(manager, project, "Terminal work"))
        assert (waits["state"], waits["reason"]) == ("queued", "terminals")
        assert "terminals service is not running" in waits["detail"]
        listed = team.queue.waiting_for(max_.id)
        assert listed and listed[0]["reason"] == "terminals"
    finally:
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
        await team.assign(cleo, task_id)
        [req] = cli.started
        app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={"staff": team}, guard=None)
        api = build_app(app, "tok")  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            url = f"/api/team/{req.staff_session_id}"
            wrong = await client.post(f"{url}/report", headers={"X-Daedalus-Team-Token": "not-it"}, json={"kind": "checkpoint", "note": "half"})
            assert wrong.status_code == 401
            operator = await client.post(f"{url}/report", headers={"X-Daedalus-Token": "tok"}, json={"kind": "checkpoint", "note": "half"})
            assert operator.status_code == 401, "the operator's token is not a team token"
            ok = await client.post(f"{url}/report", headers={"X-Daedalus-Team-Token": req.team_token}, json={"kind": "done", "note": "all"})
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
            gone = await client.post(f"{url}/report", headers={"X-Daedalus-Team-Token": req.team_token}, json={"kind": "checkpoint", "note": "late"})
            assert gone.status_code == 401
    finally:
        await manager.close()


async def test_a_restart_offers_assigned_tasks_to_the_queue_again(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path, concurrency=1)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        bo = await manager.staff.hire(project.id, name="Bo", isolation="shared")
        await team.assign(ada, await board_task(manager, project, "First"))
        live = await team.live_of(ada)
        assert live is not None
        await team.ingress.status(live, "working")
        waiting = await board_task(manager, project, "Second")
        assert (await team.assign(bo, waiting))["reason"] == "project"

        fresh = await team_for(settings, manager)
        fresh.runtimes["daedalus"] = runtime
        assert await fresh.rebuild() == 1
        assert [q["task_id"] for q in fresh.queue.queue(project.id)] == [waiting]
        await fresh.queue.pump(project.id)
        assert fresh.queue.queue(project.id)[0]["reason"] == "project"
    finally:
        await manager.close()


async def test_live_sessions_answer_for_a_member(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        with pytest.raises(StaffError, match="no live session"):
            await team.tell(ada, "hello")
        await team.assign(ada, await board_task(manager, project, "Menu"))
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
        await manager.close()


async def test_a_notification_answers_a_command_line_request_and_the_router_holds_for_the_orchestrator(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path, capacity=Capacity())
    try:
        cli = FakeStaffRuntime(kind="claude")
        team.runtimes["claude"] = cli
        cleo = await manager.staff.hire(project.id, name="Cleo", harness="claude", isolation="shared")
        await team.assign(cleo, await board_task(manager, project, "Menu"))
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
        with pytest.raises(StaffError) as refused:
            await team.assign(ada, task_id)
        said = str(refused.value)
        assert "notes is not a git repository" in said and "Ada works in a git worktree of their own" in said
        assert "a folder of the project that is a git repository" in said and "isolation to shared" in said and "read-only" in said
        assert runtime.started == [] and await status_of(manager, ada) == "off"
        row = await task_row(manager, task_id)
        assert (row["status"], row["assignee_staff_id"]) == ("todo", None)
        assert team.queue.queue(project.id) == []

        # A member who works in the folder itself takes it, in the folder.
        bo = await manager.staff.hire(project.id, name="Bo", isolation="shared")
        assert (await team.assign(bo, task_id))["state"] == "started"
        [req] = runtime.started
        assert req.worktree is None and req.cwd == tmp_path / "notes"
    finally:
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
        assert (await team.assign(bo, first))["state"] == "started"
        busy = await team.live_of(bo)
        assert busy is not None
        await team.ingress.status(busy, "working")
        waiting = await board_task(manager, project, "Tidy the notes")
        assert (await team.assign(ada, waiting))["state"] == "queued"
        await manager.db.execute("UPDATE board_tasks SET folder_id = ? WHERE id = ?", (folder_id, waiting))

        await team.ingress.status(busy, "turn_done_unseen")
        await team.queue.pump(project.id)

        async def taken_back() -> bool:
            return (await task_row(manager, waiting))["assignee_staff_id"] is None

        await until_await(taken_back)
        assert [r.task.id for r in runtime.started] == [first], "nothing started in the plain folder"
        assert await status_of(manager, ada) == "off"
        row = await task_row(manager, waiting)
        assert row["status"] == "todo" and "Ada could not start: Ada works in a git worktree of their own" in row["notes"]
        [event] = [e for e in await events(manager, "task.assigned") if e.payload.get("task_id") == waiting and e.payload.get("error")]
        assert "not a git repository" in event.payload["error"] and "isolation to shared" in event.payload["error"]
    finally:
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
        assert (await team.assign(ada, await board_task(manager, project, "Menu")))["state"] == "started"
        [req] = runtime.started
        assert req.worktree is not None and req.cwd == req.worktree.cwd != refreshed.primary.path
    finally:
        await manager.close()
