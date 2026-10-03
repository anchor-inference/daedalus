"""The register of open results: a member's done, stuck or needs-input and a card nobody took stay on the
orchestrator's list until a decision follows; the host says so once at the end of a turn that left one,
and never again; the decisions that close one are the ones that decide something."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.host.events import EventFilter
from daedalus.stores.database import Database
from daedalus.stores.staff import Staff
from tests.support.authorized_results import accept_branchless_result
from tests.unit.test_board_rounds import task_of
from tests.unit.test_orchestrator import Rig, rig
from tests.unit.test_orchestrator_team import fake, office
from tests.unit.test_staff_runtime import close_team
from tests.unit.test_task_contract import SCRIPT

UPDATER = {
    "objective": "Prove the updater never loses a write while it swaps the files",
    "deliverable": "A prototype branch and a report of which gates pass",
    "boundaries": "Only the updater's code; the running app is not touched",
    "done_when": "The three gate tests pass",
}


async def quiet(r: Rig) -> str:
    """The office without its wake queue: these tests play the end of a turn themselves, and a queue
    delivering the events meanwhile would move the cursor they are about."""
    sid = await office(r)
    await r.orch.stop_queue(r.project.id)
    return sid


async def started(r: Rig, sid: str, name: str = "Sol", title: str = "Updater prototype") -> tuple[str, Staff]:
    member = await r.manager.staff.hire(r.project.id, name=name, role="Updater", isolation="shared")
    task_id = task_of(await r.call(sid, "assign", staff=name, title=title, **UPDATER))
    return task_id, member


async def report(r: Rig, member: Staff, kind: str, text: str,
                 *, artifacts: list[str] | None = None) -> str:
    live = await r.team.live_of(member)
    assert live is not None
    told = await r.team.ingress.report(live, kind, text, artifacts=artifacts,
                                       call_id=f"fixture-report:{uuid.uuid4().hex}")
    await r.team.ingress.status((await r.team.live_of(member)) or live, "idle")
    return told


async def reminders(r: Rig) -> list[Any]:
    return await r.manager.bus.replay(0, EventFilter(types=("orchestrator.open_results",), project_id=r.project.id), limit=100)


async def test_a_blocker_left_without_a_decision_is_said_once_and_stays_listed(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A member proved a blocker and stopped; the orchestrator marked the card, wrote the journal,
    told the operator — and handed the next step to nobody, with two members free."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await quiet(r)
        task_id, sol = await started(r, sid)
        await r.manager.staff.hire(r.project.id, name="Gleb", role="Reviews", isolation="shared")
        turn = datetime.now(UTC).isoformat()
        await report(r, sol, "stuck", "a race between the swap and the supervisor is proven; this design cannot go on")
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert "Waiting for your decision" in state and f"Sol reported stuck on \"Updater prototype\" ({task_id})" in state and "free: Gleb" in state

        # The turn ends with a report to the operator and nothing else: the host says it once.
        await r.call(sid, "project_report", text="The updater is blocked", kind="blocked", task_id=task_id)
        assert len(await r.orch.turn_ended(r.project.id, turn)) == 1
        [said] = await reminders(r)
        line = await r.orch.line(await r.refreshed(), said)
        assert line.startswith("your last turn ended with no decision on these") and f"({task_id})" in line
        assert await r.orch.turn_ended(r.project.id, datetime.now(UTC).isoformat()) == [], "a second turn is not reminded again"
        assert len(await reminders(r)) == 1
        assert f"({task_id})" in await r.orch.project_state(await r.refreshed(), session_id=sid), "it stays listed until decided"

        # The next step on the card is a decision.
        await r.manager.staff.hire(r.project.id, name="Rel", role="Delivery", isolation="shared")
        follow = task_of(await r.call(sid, "assign", staff="Rel", title="Swap handshake design", **{**UPDATER, "objective": "Design a handshake with the supervisor before the swap"}, depends_on=[task_id]))
        assert follow != task_id
        assert "Waiting for your decision" not in await r.orch.project_state(await r.refreshed(), session_id=sid)
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_result_the_orchestrator_had_not_been_given_yet_is_not_one_it_passed_over(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        _, sol = await started(r, sid)
        before = r.manager.bus.head
        await report(r, sol, "needs_input", "which supervisor version do we target?")
        assert await r.orch.loops.turn_ended(r.project.id, datetime.now(UTC).isoformat(), delivered=before) == []
        assert len(await r.orch.loops.turn_ended(r.project.id, datetime.now(UTC).isoformat(), delivered=r.manager.bus.head)) == 1
    finally:
        await close_team(r.manager)
        await r.manager.close()


@pytest.mark.parametrize("decision", ["tell", "blocked_waiting", "decide", "ask", "dropped"])
async def test_each_decision_closes_the_result(settings: Settings, db: Database, tmp_path: Path, decision: str) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        task_id, sol = await started(r, sid)
        await report(r, sol, "needs_input", "which supervisor version do we target?")
        if decision == "tell":
            live = await r.team.live_of(sol)
            assert live is not None and live.session.task_id == task_id
            await r.call(sid, "tell", staff="Sol", text="Target the supervisor on main")
        elif decision == "blocked_waiting":
            said = await r.call(sid, "tasks", op="move", task_id=task_id, status="blocked")
            assert "still waits for your decision" in said, "blocked alone says nothing of what it waits for"
            await r.call(sid, "tasks", op="move", task_id=task_id, status="blocked", waiting_on="the operator's choice of supervisor")
        elif decision == "decide":
            with pytest.raises(Refused, match="say why"):
                await r.call(sid, "decide", task_id=task_id, why="ok")
            said = await r.call(sid, "decide", task_id=task_id, why="the operator paused the updater work until next week")
            assert said.startswith(f"decided: nothing further on task {task_id}")
            journal = [e.text for e in await r.manager.projects.journal(r.project.id, limit=5)]
            assert f"Nothing further on task {task_id}: the operator paused the updater work until next week" in journal
        elif decision == "ask":
            await r.call(sid, "ask_operator", title="Supervisor version", text="Which supervisor version should the updater target?", task_id=task_id)
        else:
            await r.call(sid, "tasks", op="move", task_id=task_id, status="dropped", note="replaced by a new design")
        assert await r.orch.loops.open(r.project.id) == []
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_silence_decides_nothing(settings: Settings, db: Database, tmp_path: Path) -> None:
    """StaySilent after a batch with a result in it is no decision: the result stays open."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        task_id, sol = await started(r, sid)
        live = await r.team.live_of(sol)
        assert live is not None
        _folder, cwd = await r.team.cwd_of(live)
        (Path(cwd) / "gates.txt").write_text("All three gates passed\n")
        await report(r, sol, "done", "the three gates pass", artifacts=["gates.txt"])
        await r.call(sid, "journal", text="read sol's report")
        [loop] = await r.orch.loops.open(r.project.id)
        assert (loop["task_id"], loop["cause"]) == (task_id, "report_done")
        await accept_branchless_result(r.team, task_id)
        assert await r.orch.loops.open(r.project.id) == []
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_card_nobody_took_is_raised_after_a_turn_and_a_decided_one_is_not_raised_again(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A presentation the operator had ordered sat unassigned behind a condition the orchestrator made up."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await quiet(r)
        card = await r.board.add(title="Capabilities presentation", session_id=sid, brief={**SCRIPT, "objective": "A capabilities presentation of the product"})
        assert await r.orch.turn_ended(r.project.id, card["created_at"]) == [], "made in this turn: not passed over yet"
        [loop_id] = await r.orch.turn_ended(r.project.id, datetime.now(UTC).isoformat())
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert f"[L{loop_id}] \"Capabilities presentation\" ({card['id']}) is in todo with nobody on it" in state
        await r.call(sid, "decide", loop=f"L{loop_id}", why="it starts after the operator approves the voice price")
        assert await r.orch.turn_ended(r.project.id, datetime.now(UTC).isoformat()) == []
        assert await r.orch.loops.open(r.project.id) == []
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_the_operator_acting_on_the_card_closes_its_result(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        task_id, sol = await started(r, sid)
        await report(r, sol, "stuck", "blocked on the supervisor")
        await r.board.update(task_id, status="dropped", note="not needed any more")
        from tests.support.waiting import until_await

        async def closed() -> bool:
            return await r.orch.loops.open(r.project.id) == []

        await until_await(closed, "the operator's move closed the result")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_the_end_of_a_real_turn_says_it_once_and_the_reminder_is_a_wake_up_of_its_own(settings: Settings, db: Database, tmp_path: Path) -> None:
    """Through the wake queue and a run of the orchestrator's: the report wakes it, its turn decides
    nothing, the reminder wakes it once more, and that turn is not followed by another reminder."""
    from tests.support.waiting import until_await
    from tests.unit.test_orchestrator import last_user_text

    r = await rig(settings, db, tmp_path, [{"text": "Read it."}, {"text": "Still thinking."}, {"text": "Nothing."}])
    try:
        fake(r)
        r.manager.config.orchestrator.batch_seconds = 0
        sid = await office(r)
        task_id, sol = await started(r, sid)
        await report(r, sol, "stuck", "a race between the swap and the supervisor is proven")

        async def reminded_and_heard() -> bool:
            return len(await reminders(r)) == 1 and any(
                "your last turn ended with no decision on these" in last_user_text(r.provider, index)
                for index in range(len(r.provider.requests))
            )

        await until_await(reminded_and_heard, "the reminder was published and delivered")
        assert "your last turn ended with no decision on these" in last_user_text(r.provider)
        assert f"({task_id})" in last_user_text(r.provider)

        async def idle() -> bool:
            state = r.manager.live_state(sid)
            return state is not None and not state.running

        await until_await(idle, "the reminded turn ended")
        assert len(await reminders(r)) == 1, "one reminder, whatever the next turn does"
    finally:
        await close_team(r.manager)
        await r.manager.close()
