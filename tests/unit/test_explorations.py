"""Explorations: competing attempts at one card, launched once, reported together, decided once."""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.extensions import explorations as explorations_module
from daedalus.extensions.explorations import Explorations
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.extensions.runtime_observations import admit_native_run, observe_exit
from daedalus.staff_runtime import Started
from daedalus.stores.database import Database
from tests.support.authorized_launch import operator_assignment
from tests.support.waiting import until_await
from tests.unit.test_orchestrator import Rig, events, rig
from tests.unit.test_orchestrator_team import office
from tests.unit.test_staff_runtime import ObservedFakeStaffRuntime, board_task, close_team, task_row


class Running(ObservedFakeStaffRuntime):
    """A Daedalus runtime whose member is at work in a native run from its start, and whose interrupt
    or stop cancels that run and has its exit observed, as the host's own runtime does: that exit is
    what closes the attempt's execution. ``gate``, when set, holds a start half admitted until it
    opens, the way a loaded machine does."""

    app: Any = None
    gate: asyncio.Event | None = None
    gated: str = ""
    """Whose starts the gate holds: members whose name begins so."""
    entered: asyncio.Event | None = None

    async def start(self, req: Any) -> Started:
        if self.gate is not None and req.staff.name.startswith(self.gated):
            if self.entered is not None:
                self.entered.set()
            await self.gate.wait()
        started = await super().start(req)
        source = await self.app.db.fetchone("SELECT tenant_id FROM sessions WHERE id = ?", (started.session_id,))
        run_id, at = uuid.uuid4().hex, datetime.now(UTC).isoformat()
        await self.app.db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                                  " VALUES (?,?,?,'running',?,?)", (run_id, source["tenant_id"], started.session_id, at, at))
        await admit_native_run(self.app, req.staff_session_id, started.session_id, run_id)
        return started

    async def _cancel_run(self, live: Any) -> None:
        row = await self.app.db.fetchone("SELECT a.native_run_id FROM execution_attempts a JOIN runs r ON r.id = a.native_run_id"
                                         " WHERE a.staff_session_id = ? AND r.status = 'running'", (live.id,))
        if row is None:
            return
        await self.app.db.execute("UPDATE runs SET status = 'cancelled' WHERE id = ?", (row["native_run_id"],))
        await observe_exit(self.app, staff_session_id=live.id, runtime_ref=row["native_run_id"],
                           observed_status="cancelled", bus=self.manager.bus)  # type: ignore[union-attr]

    async def interrupt(self, live: Any) -> None:
        await super().interrupt(live)
        await self._cancel_run(live)

    async def stop(self, live: Any) -> None:
        await super().stop(live)
        await self._cancel_run(live)


def fake(r: Rig) -> Running:
    runtime = Running(kind="daedalus")
    runtime.manager, runtime.app = r.manager, r.team.app
    r.team.runtimes["daedalus"] = runtime
    return runtime


def explorations(r: Rig) -> Explorations:
    service = Explorations(r.team.app)
    r.team.app.extensions["explorations"] = service
    r.team.report_hooks.append(service.reported)
    return service


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


async def attempts_of(r: Rig, exploration_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in await r.manager.db.fetchall(
        "SELECT * FROM exploration_attempts WHERE exploration_id = ? ORDER BY slot", (exploration_id,))]


async def started(r: Rig, call: str = "call-explore", *, held: bool = False, **kwargs: Any) -> tuple[str, str, Any]:
    """An office, a source member who worked an earlier card in its own session, and an exploration
    of a new card with two attempts; ``held`` holds the attempts' starts at the runtime's door."""
    runtime = fake(r)
    if held:
        runtime.gate, runtime.entered, runtime.gated = asyncio.Event(), asyncio.Event(), "Ada-"
    sid = await office(r)
    await r.call(sid, "team", concurrency=4)
    source = await r.manager.staff.hire(r.project.id, name="Ada", role="Menu", isolation="worktree")
    first = await board_task(r.manager, r.project, "Menu page")
    await operator_assignment(r.team, source, first)
    task_id = await board_task(r.manager, r.project, "Price list")
    said = await r.call(sid, "explore", task_id=task_id, attempts=2, staff="Ada", client_operation_id=call, **kwargs)
    return sid, task_id, (runtime, said)


async def report_done(r: Rig, staff_id: str, text: str, *, commit: str = "") -> None:
    async def live() -> bool:
        row = await r.manager.db.fetchone(
            "SELECT 1 FROM execution_attempts a JOIN staff_sessions s ON s.id = a.staff_session_id"
            " WHERE s.staff_id = ? AND a.state = 'running' AND a.provider_session_ref IS NOT NULL", (staff_id,))
        return row is not None and await r.team.live_of(await r.manager.staff.get(staff_id)) is not None

    await until_await(live, "the attempt's member is at work")
    session = await r.team.live_of(await r.manager.staff.get(staff_id))
    if commit:
        where = Path(session.session.worktree_path)
        (where / "prices.md").write_text(commit)
        git(where, "add", "-A")
        git(where, "commit", "-qm", "Write the price list")
    await r.team.ingress.report(session, "done", text, call_id=f"done-{staff_id}")


async def test_an_exploration_is_recorded_launched_once_and_forks_the_source_conversation(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        service = explorations(r)
        sid, task_id, (runtime, said) = await started(r)
        [row] = await r.manager.db.fetchall("SELECT * FROM explorations")
        assert row["state"] == "running" and row["task_id"] == task_id, said
        attempts = await attempts_of(r, row["id"])
        assert [a["state"] for a in attempts] == ["running", "running"], said
        assert len({a["task_id"] for a in attempts}) == 2 and all(a["staff_id"] for a in attempts)
        source_session = await r.manager.db.fetchone("SELECT session_id FROM staff_sessions s JOIN staff m ON m.id = s.staff_id"
                                                     " WHERE m.name = 'Ada'")
        assert {a["fork_session_id"] for a in attempts} == {source_session["session_id"]}
        async def launched() -> bool:
            return len(runtime.started) == 3

        await until_await(launched, "both attempts were launched")
        forks = [req for req in runtime.started if req.fork_from]
        assert {req.fork_from for req in forks} == {source_session["session_id"]} and len(forks) == 2
        assert len({str(req.worktree.path) for req in forks}) == 2, "each attempt has a worktree of its own"
        assert (await task_row(r.manager, task_id))["status"] == "blocked"
        # The same call again finds what it made and launches nothing more.
        again = await r.call(sid, "explore", task_id=task_id, attempts=2, staff="Ada", client_operation_id="call-explore")
        assert row["id"] in again and len(runtime.started) == 3
        with pytest.raises(Refused, match="already being explored"):
            await r.call(sid, "explore", task_id=task_id, attempts=2, staff="Ada", client_operation_id="another")
        with pytest.raises(Refused, match="being explored"):
            await r.call(sid, "assign", staff="Ada", task_id=task_id)
        assert service is r.team.app.extensions["explorations"]
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_the_orchestrator_is_woken_once_with_the_attempts_side_by_side_and_keeps_one(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        service = explorations(r)
        sid, task_id, _ = await started(r)
        follower = await board_task(r.manager, r.project, "Print the price list")
        revision = (await task_row(r.manager, follower))["entity_revision"]
        await r.call(sid, "tasks", op="update", task_id=follower, depends_on=[task_id], expected_entity_revision=revision)
        [row] = await r.manager.db.fetchall("SELECT * FROM explorations")
        attempts = await attempts_of(r, row["id"])
        await report_done(r, attempts[0]["staff_id"], "Made the list as a table.")
        reports = await events(r.manager, "staff.report")
        assert reports and all([await r.orch.classify(r.project.id, e) is None for e in reports])
        assert not await events(r.manager, "orchestrator.exploration"), "one attempt is still at work"
        await report_done(r, attempts[1]["staff_id"], "Made the list as plain lines.", commit="bread 3\n")
        [news] = await events(r.manager, "orchestrator.exploration")
        wake = await r.orch.classify(r.project.id, news)
        assert wake is not None and wake.urgent
        text = await r.orch.line(await r.refreshed(), news)
        assert "every attempt has reported" in text and "Made the list as a table." in text and "plain lines" in text
        assert "Explore(op='pick'" in text

        with pytest.raises(Refused, match="why"):
            await r.call(sid, "explore", op="pick", exploration=row["id"], winner="1", why="")
        said = await r.call(sid, "explore", op="pick", exploration=row["id"], winner="1", why="The table reads better on paper.")
        assert "Kept attempt 1" in said, said
        after = await attempts_of(r, row["id"])
        assert [a["state"] for a in after] == ["won", "lost"] and after[1]["cleaned_at"], said
        loser = await task_row(r.manager, after[1]["task_id"])
        assert loser["status"] == "dropped"
        assert (await r.manager.staff.get(after[1]["staff_id"])).archived_at is not None  # type: ignore[union-attr]
        assert loser["branch"] not in git(r.repo, "branch", "--list")
        assert git(r.repo, "rev-parse", "--verify", f"refs/daedalus/explored/{row['id']}/2").strip()
        assert (await task_row(r.manager, task_id))["status"] == "dropped"
        assert (await task_row(r.manager, after[0]["task_id"]))["status"] == "review"
        assert after[0]["task_id"] in (await task_row(r.manager, follower))["depends_on"]

        [card_view] = await service.for_task(after[1]["task_id"])
        assert card_view["state"] == "chosen" and card_view["winner_slot"] == 1
        assert [a["state"] for a in card_view["attempts"]] == ["won", "lost"] and card_view["attempts"][0]["staff_name"]

        # The same pick again changes nothing; another pick is refused.
        assert "decided already" in await r.call(sid, "explore", op="pick", exploration=row["id"], winner="1", why="The table reads better on paper.")
        with pytest.raises(Refused, match="decided already"):
            await r.call(sid, "explore", op="pick", exploration=row["id"], winner="2", why="Plain lines after all, sorry.")
        assert service is not None
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_keeping_none_releases_every_attempt_and_puts_the_task_back(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        service = explorations(r)
        sid, task_id, _ = await started(r)
        [row] = await r.manager.db.fetchall("SELECT * FROM explorations")
        await r.manager.db.execute("UPDATE explorations SET deadline_at = '2000-01-01' WHERE id = ?", (row["id"],))
        await service.sweep()
        [news] = await events(r.manager, "orchestrator.exploration")
        assert news.payload["reason"] == "the deadline passed"
        assert [a["state"] for a in await attempts_of(r, row["id"])] == ["failed", "failed"]
        said = await r.call(sid, "explore", op="pick", exploration=row["id"], winner="none", why="Neither got far enough.")
        assert "Kept none" in said, said
        assert (await task_row(r.manager, task_id))["status"] == "todo"
        assert all(a["cleaned_at"] for a in await attempts_of(r, row["id"])), said
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def launches(r: Rig, card_id: str) -> list[str]:
    return [row["state"] for row in await r.manager.db.fetchall(
        "SELECT state FROM effect_outbox WHERE kind = 'task.launch' AND json_extract(payload_json,'$.control.task_id') = ?"
        " ORDER BY created_at", (card_id,))]


async def test_a_deadline_during_the_starts_withdraws_them_and_a_second_pick_finishes(settings: Settings, db: Database, tmp_path: Path,
                                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """The deadline came while one attempt's start was half admitted and the other's still waited for
    a place. Dismissing the members under those starts made them fail afterwards, taking the cards'
    assignees back between the read and the write of the drop, or ended the session a start was
    entering: the cards could not be dropped and the orchestrator was told they "stay todo"."""
    r = await rig(settings, db, tmp_path)
    try:
        service = explorations(r)
        sid, task_id, (runtime, _) = await started(r, held=True)
        await asyncio.wait_for(runtime.entered.wait(), timeout=30)
        [row] = await r.manager.db.fetchall("SELECT * FROM explorations")
        first, second = [a["task_id"] for a in await attempts_of(r, row["id"])]
        assert (await launches(r, first), await launches(r, second)) == (["claimed"], ["pending"])
        await r.manager.db.execute("UPDATE explorations SET deadline_at = '2000-01-01' WHERE id = ?", (row["id"],))
        await service.sweep()
        [news] = await events(r.manager, "orchestrator.exploration")
        assert news.payload["reason"] == "the deadline passed"
        # The start still waiting is withdrawn: nobody waits for that attempt's work any more.
        assert await launches(r, second) == ["cancelled"]

        # The start being admitted is waited for, briefly; still held, the card is named and kept.
        monkeypatch.setattr(explorations_module, "SETTLE_SECONDS", 0.3)
        said = await r.call(sid, "explore", op="pick", exploration=row["id"], winner="none", why="Neither got far enough.")
        assert "Kept none" in said and f"{first} is held" in said and second not in said, said
        assert [bool(a["cleaned_at"]) for a in await attempts_of(r, row["id"])] == [False, True], said
        assert (await task_row(r.manager, second))["status"] == "dropped"
        assert (await task_row(r.manager, task_id))["status"] == "todo"

        # Once it is through, the same pick again releases the member it started and drops the card.
        monkeypatch.setattr(explorations_module, "SETTLE_SECONDS", 30.0)
        runtime.gate.set()
        again = await r.call(sid, "explore", op="pick", exploration=row["id"], winner="none", why="Neither got far enough.")
        assert "decided already" in again and "held" not in again, again
        attempts = await attempts_of(r, row["id"])
        assert all(a["cleaned_at"] for a in attempts), again
        assert (await task_row(r.manager, first))["status"] == "dropped"
        execution = await r.manager.db.fetchone("SELECT a.state FROM execution_attempts a JOIN board_tasks t"
                                                " ON t.current_attempt_id = a.id WHERE t.id = ?", (first,))
        assert execution["state"] == "cancelled"
        assert (await r.manager.staff.get(attempts[0]["staff_id"])).archived_at is not None  # type: ignore[union-attr]
        assert [req.staff.name.startswith("Ada-") for req in runtime.started].count(True) == 1, "the withdrawn start never ran"
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_restart_reports_attempts_in_flight_and_launches_nothing_again(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        service = explorations(r)
        runtime = fake(r)
        sid = await office(r)
        task_id = await board_task(r.manager, r.project, "Price list")
        at = "2026-01-01T00:00:00+00:00"
        await r.manager.db.execute(
            "INSERT INTO explorations(id,project_id,task_id,orchestrator_session_id,state,budget_microusd,deadline_at,created_at,updated_at)"
            " VALUES ('xrestart',?,?,?,'launching',2000000,'2999-01-01',?,?)", (r.project.id, task_id, sid, at, at))
        for slot, state in ((1, "launching"), (2, "planned")):
            await r.manager.db.execute(
                "INSERT INTO exploration_attempts(exploration_id,slot,harness,state,created_at,updated_at)"
                " VALUES ('xrestart',?,'daedalus',?,?,?)", (slot, state, at, at))
        await service.recover()
        attempts = await attempts_of(r, "xrestart")
        assert [a["state"] for a in attempts] == ["unknown", "cancelled"]
        assert "not launched again" in attempts[0]["detail"]
        assert runtime.started == []
        [news] = await events(r.manager, "orchestrator.exploration")
        assert news.payload["state"] == "ready"
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_goal_budget_that_cannot_hold_the_attempts_refuses_before_anything_starts(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        explorations(r)
        await r.manager.db.execute(
            "INSERT INTO project_goal_budgets(project_id,budget_id,limit_microusd,coordination_limit_microusd,"
            "activated_goal_revision,activated_at,updated_at) VALUES (?,?,?,?,1,'now','now')",
            (r.project.id, "goal-1", 1_500_000, 0))
        with pytest.raises(Refused, match="goal budget has \\$1.500000 free"):
            await started(r)
        assert not await r.manager.db.fetchall("SELECT * FROM explorations")
        assert await r.manager.db.fetchone("SELECT 1 FROM staff WHERE name LIKE 'Ada-%'") is None
        # Within the budget it starts, and the hold counts against the goal balance until it is decided.
        sid = (await r.refreshed()).settings.orchestrator.session_id
        task_id = (await r.manager.db.fetchone("SELECT id FROM board_tasks WHERE title = 'Price list'"))["id"]
        await r.call(sid, "explore", task_id=task_id, attempts=2, staff="Ada", budget_usd=1.2, client_operation_id="smaller")
        from daedalus.stores.goal_budget import view_in

        async with r.manager.db.transaction() as conn:
            view = await view_in(conn, r.project.id)
        assert view is not None and view["total"]["held_usd"] == "1.200000"
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_forked_attempt_repeats_its_source_request_prefix_byte_for_byte(settings: Settings, db: Database) -> None:
    """The cache only serves a prefix whose bytes are the same. A fork with its own title, working
    directory and brief wrote another system prompt, and nothing of the shared history was cached."""
    import asyncio

    from daedalus.providers.openai_compat import CACHE_ROUTING
    from tests.unit.test_session_runner import ScriptedProvider, _manager

    class Routed(ScriptedProvider):
        routes: list[str | None] = []

        async def stream_with_tools(self, request: Any):  # type: ignore[no-untyped-def,override]
            self.routes.append(CACHE_ROUTING.get())
            async for delta in super().stream_with_tools(request):
                yield delta

    provider = Routed([{"text": "first answer"}, {"text": "second answer"}, {"text": "attempt answer"}])
    manager = await _manager(settings, db, provider)
    finished: list[str] = []
    done = asyncio.Event()

    async def on_finished(session_id: str, run_id: str, status: str) -> None:
        finished.append(session_id)
        done.set()

    manager.on_finished(on_finished)

    async def turn(session_id: str, text: str) -> None:
        done.clear()
        await manager.submit(session_id, text)
        await asyncio.wait_for(done.wait(), timeout=30)

    try:
        source = await manager.create_session("Ada · Menu page", metadata={"brief": "Ada works on the menu."})
        await turn(source.session.id, "Write the menu.")
        attempt = await manager.create_session("Ada-1 · Menu page — attempt 1", metadata={"brief": "Another brief."})
        await manager.fork_into(source.session.id, 2**62, attempt, copy_workspace=False)
        await manager.pin_fork_identity(attempt.session.id, source.session.id)
        await turn(source.session.id, "Now the prices.")
        await turn(attempt.session.id, "Try the prices another way.")
        source_next, attempt_first = provider.requests[1], provider.requests[2]
        assert source_next.tools == attempt_first.tools
        shared = len(source_next.messages) - 1
        assert shared >= 3, "system prompt, the first question and its answer"
        # What reaches the wire: each message's role and content, in order.
        def wire(messages: Any) -> list[Any]:
            return [(m.role, [b.model_dump() for b in m.content_blocks]) for m in messages]

        assert wire(source_next.messages[:shared]) == wire(attempt_first.messages[:shared])
        assert "Another brief." not in str(attempt_first.messages[0].model_dump())
        assert provider.routes == [None, None, source.session.id]
    finally:
        await manager.close()
