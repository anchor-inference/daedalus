"""What the coordinator's board tools say when they refuse: the state that refused, and the one call
that gets the work moving again. Each case here once ended a coordinator's attempt to hand work over
in a loop of refusals that each pointed at another refused call."""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.config import Settings
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.stores.control import ControlStore, Entity, Scope
from daedalus.stores.database import Database
from tests.support.authorized_results import accept_branchless_result
from tests.unit.test_board_rounds import PLAN, REVISED, assigned, hand_in, task_of
from tests.unit.test_orchestrator import rig
from tests.unit.test_orchestrator_team import fake, office, working
from tests.unit.test_staff_runtime import BRIEF, board_task, close_team, task_row


async def _collection(r: object) -> int:
    return await ControlStore(r.manager.db).revision(Scope("project", r.project.id), Entity("collection", r.project.id))  # type: ignore[attr-defined]


async def test_a_further_round_on_an_accepted_card_says_to_make_a_card_of_its_own(settings: Settings, db: Database,
                                                                                  tmp_path: Path) -> None:
    """Asked for one more check after it had accepted a member's report, a coordinator was refused
    with "return the exact reviewed result", the return was refused because the card was done, and
    the card it then made was answered with "drop it and Assign(task_id=…) instead" — the very call
    refused first."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        ada = await r.manager.staff.hire(r.project.id, name="Ada", role="Planner", isolation="shared")
        first, _ = await assigned(r, sid, staff="Ada", title="Tariff plan", **PLAN)
        live = await r.team.live_of(ada)
        assert live is not None
        _folder, cwd = await r.team.cwd_of(live)
        (Path(cwd) / "plan.md").write_text("Four limits and two open questions\n")
        await hand_in(r, ada, artifacts=["plan.md"])
        await accept_branchless_result(r.team, first)
        assert (await task_row(r.manager, first))["status"] == "done"

        with pytest.raises(Refused) as refused:
            await r.call(sid, "assign", staff="Ada", task_id=first, title="Why the plan has no fifth limit", **REVISED)
        said = str(refused.value)
        assert f"task {first} is done" in said and "accepted" in said
        assert "a card of its own" in said and "without task_id" in said
        assert "return the exact reviewed result" not in said

        with pytest.raises(Refused) as refused:
            await r.call(sid, "review_result", task_id=first, op="return", client_operation_id="return-done",
                         reason="the operator wants one more check of the limits")
        said = str(refused.value)
        assert f"task {first} is done" in said and "a card of its own" in said
        assert "is not the current reviewed result" not in said

        follow_up = await r.call(sid, "assign", staff="Ada", title="Why the plan has no fifth limit", **REVISED)
        assert task_of(follow_up) != first
        assert f"Assign(task_id='{first}')" not in follow_up, "a finished card is never offered for its next round"
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_further_round_on_a_card_in_review_names_the_return_that_opens_it(settings: Settings, db: Database,
                                                                                  tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        ada = await r.manager.staff.hire(r.project.id, name="Ada", role="Planner", isolation="shared")
        first, _ = await assigned(r, sid, staff="Ada", title="Tariff plan", **PLAN)
        await hand_in(r, ada)
        assert (await task_row(r.manager, first))["status"] == "review"

        with pytest.raises(Refused) as refused:
            await r.call(sid, "assign", staff="Ada", task_id=first, **REVISED)
        said = str(refused.value)
        assert f"task {first} is in review" in said
        assert f"ReviewResult(op='return', task_id='{first}', reason=…)" in said and f"Assign(task_id='{first}')" in said

        with pytest.raises(Refused) as refused:
            await r.call(sid, "tasks", op="move", task_id=first, status="todo")
        said = str(refused.value)
        assert f"ReviewResult(op='return', task_id='{first}'" in said, said

        # A file handle is not evidence; ten verdicts in a row were refused without saying so.
        result = await r.manager.db.fetchone("SELECT id FROM result_receipts WHERE task_id = ?", (first,))
        assert result is not None
        with pytest.raises(Refused) as refused:
            await r.call(sid, "review_result", task_id=first, op="verdict", client_operation_id="verdict-handle", result_id=result["id"],
                         verification="failed", evidence_ids=["att:867ab10d1503"], reason="the plan misses a limit")
        said = str(refused.value)
        assert "evidence att:867ab10d1503 is not recorded on result" in said and "Recorded on it: nothing yet" in said
        assert "evidence_ids=[]" in said
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_stale_revision_is_answered_with_the_current_one_and_a_left_out_one_is_filled_in(
        settings: Settings, db: Database, tmp_path: Path) -> None:
    """"the entity has changed" alone sent coordinators to Tasks(list) and back for one number; a
    hire between the list and the Assign was enough to cause it."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        await r.manager.staff.hire(r.project.id, name="Ada", role="Planner", isolation="shared")
        stale = await _collection(r)
        await board_task(r.manager, r.project, "Something else")
        current = await _collection(r)
        assert current != stale
        with pytest.raises(Refused) as refused:
            await r.call(sid, "assign", staff="Ada", title="Tariff plan", expected_collection_revision=stale, **PLAN)
        said = str(refused.value)
        assert f"collection revision {current} now" in said and f"expected_collection_revision={current}" in said

        task_id = task_of(await r.call(sid, "assign", staff="Ada", title="Tariff plan",
                                       expected_collection_revision=None, **PLAN))
        listed = await r.call(sid, "tasks", op="create", title="Audit the repositories",
                              expected_collection_revision=None, **BRIEF)
        assert "Audit the repositories" in listed

        row = await task_row(r.manager, task_id)
        with pytest.raises(Refused) as refused:
            await r.call(sid, "tasks", op="update", task_id=task_id, priority=2,
                         expected_entity_revision=int(row["entity_revision"]) - 1)
        said = str(refused.value)
        now = int((await task_row(r.manager, task_id))["entity_revision"])
        assert f"entity revision {now} now" in said and f"expected_entity_revision={now}" in said
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_card_held_by_a_running_member_names_who_holds_it_and_how_to_free_it(
        settings: Settings, db: Database, tmp_path: Path) -> None:
    """"stop or reconcile the current execution" named nothing to stop: the coordinator tried
    Release, was told the member had no live session, and Assign refused again."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        await r.orch.update(r.project.id, concurrency=2)
        await r.manager.staff.hire(r.project.id, name="Ira", role="Menu", isolation="shared")
        _ada, live = await working(r, "Ada", "Menu page")
        task_id = live.session.task_id
        assert task_id
        with pytest.raises(Refused) as refused:
            await r.call(sid, "assign", staff="Ira", task_id=task_id, reason="Ira knows the menu better than anyone")
        said = str(refused.value)
        assert "stop or reconcile the current execution" in said
        assert "Ada is at work on it" in said and "Release(staff='Ada')" in said
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_card_whose_start_is_queued_says_it_starts_by_itself(settings: Settings, db: Database,
                                                                     tmp_path: Path) -> None:
    """A start held for a free place was refused as "stop or reconcile the current execution":
    nothing ran to stop, and the coordinator released a member that had no session."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        await r.orch.update(r.project.id, concurrency=1)
        await working(r, "Ada", "Menu page")
        await r.manager.staff.hire(r.project.id, name="Ira", role="Menu", isolation="shared")
        said = await r.call(sid, "assign", staff="Ira", title="Tariff plan", **PLAN)
        task_id = task_of(said)
        assert "pending" in said, said
        with pytest.raises(Refused) as refused:
            await r.call(sid, "assign", staff="Ira", task_id=task_id, done_when="plan.md names every limit and its owner")
        said = str(refused.value)
        assert "its start is already waiting" in said and "starts by itself" in said
        assert "Release" not in said
        with pytest.raises(Refused) as refused:
            await r.call(sid, "tell", staff="Ira", text="Name the owner of each limit as well")
        said = str(refused.value)
        assert f"their card {task_id} is waiting to start" in said and "assign a task to start one" in said
    finally:
        await close_team(r.manager)
        await r.manager.close()
