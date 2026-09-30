"""What a staff member's work leaves on the board: one card per piece of work, a card that says what was
asked and what came of it, and a review column that holds only what the operator has to look at."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from daedalus.config import Settings
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.stores.database import MIGRATIONS, Database
from daedalus.stores.staff import Staff
from tests.support.waiting import until_await
from tests.unit.test_orchestrator import Rig, rig
from tests.unit.test_orchestrator_team import fake, office
from tests.unit.test_staff_runtime import BRIEF, board_task, task_row

PLAN = {"objective": "Plan the tariff limits for the gateway", "deliverable": "plan.md in the notes folder", "boundaries": "Change no code yet", "done_when": "plan.md names every limit"}
REVISED = {"objective": "Fold the operator's answers into the plan", "deliverable": "plan.md, revised in place", "boundaries": "Change no code yet", "done_when": "plan.md answers all three questions"}


async def hand_in(r: Rig, member: Staff, note: str = "plan.md written: four limits, two open questions") -> str:
    """The member reports its task done and its turn ends, as a member's does."""
    live = await r.team.live_of(member)
    assert live is not None
    told = await r.team.ingress.report(live, "done", note)
    await r.team.ingress.status((await r.team.live_of(member)) or live, "idle")
    return told


def task_of(said: str) -> str:
    """The task id an Assign answer names: '<name> started on <id> …' or '<name> will start <id> …'."""
    found = re.search(r"(?:started on|will start) (\w+) \"", said)
    assert found is not None, said
    return found.group(1)


async def test_work_the_orchestrator_handed_out_is_done_when_reported_and_keeps_its_result(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        ada = await r.manager.staff.hire(r.project.id, name="Ada", role="Planner", isolation="shared")
        task_id = task_of(await r.call(sid, "assign", staff="Ada", title="Tariff plan", **PLAN))
        waiting = await r.board.add(title="Build the limits", session_id=sid, brief=BRIEF, depends_on=[task_id])
        assert waiting["status"] == "blocked"

        told = await hand_in(r, ada)
        row = await task_row(r.manager, task_id)
        assert row["status"] == "done", "no branch and no operator request: nothing for the operator to review"
        assert "is done" in told and "in review" not in told
        assert "result from Ada: plan.md written: four limits, two open questions" in row["notes"]
        assert (await task_row(r.manager, waiting["id"]))["status"] == "todo", "what waited on it is ready"
    finally:
        await r.manager.close()


async def test_the_operators_own_task_and_a_branch_still_go_to_review(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        await office(r)
        ada = await r.manager.staff.hire(r.project.id, name="Ada", role="Menu", isolation="shared")
        posted = await board_task(r.manager, r.project, "Menu page")  # the operator's: no origin session
        await r.team.assign(ada, posted, by="operator")
        told = await hand_in(r, ada, "menu.md committed")
        row = await task_row(r.manager, posted)
        assert row["status"] == "review" and "in review" in told
        assert "result from Ada: menu.md committed" in row["notes"]

        ben = await r.manager.staff.hire(r.project.id, name="Ben", role="Menu", isolation="worktree")
        branched = await r.board.add(title="Prices", session_id=(await r.refreshed()).settings.orchestrator.session_id, brief=BRIEF)
        await r.team.assign(ben, branched["id"], by="orchestrator")
        live = await r.team.live_of(ben)
        assert live is not None and live.session.worktree_path
        await r.team.ingress.report(live, "done", "prices on their branch")
        assert (await task_row(r.manager, branched["id"]))["status"] == "review", "a branch is the operator's to merge"
    finally:
        await r.manager.close()


async def test_new_work_for_the_same_member_never_takes_over_the_card_they_just_handed_in(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A new assignment used to continue the member's last card whenever it came within two hours of
    the hand-in: the presentation the operator had asked for was renamed into a lessons card and left
    the board. Now it gets a card of its own, and the answer names the card it could have been."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        leo = await r.manager.staff.hire(r.project.id, name="Leo", role="Designer", isolation="shared")
        presentation = task_of(await r.call(sid, "assign", staff="Leo", title="Presentation of the product", **PLAN))
        await hand_in(r, leo, "slides.pdf, twelve slides")

        said = await r.call(sid, "assign", staff="Leo", title="Lessons from the videos, to the archive", **REVISED)
        lessons = task_of(said)
        assert lessons != presentation
        assert f"previous card {presentation} \"Presentation of the product\" was handed in" in said and f"Assign(task_id='{presentation}')" in said
        card = await r.board.get(presentation)
        assert card["title"] == "Presentation of the product" and card["status"] == "done"
        assert card["brief"]["objective"] == PLAN["objective"] and "round 2" not in card["notes"]
        assert (await r.board.get(lessons))["brief"]["objective"] == REVISED["objective"]
    finally:
        await r.manager.close()


async def test_a_round_that_forgot_its_task_id_is_refused_and_a_task_id_continues_the_card(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The failure the continuing guarded against — thirteen cards in two days, each a step of the
    same work — is caught by the title instead: nearly the title just handed in is refused until the
    task_id or new=true says which it is."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        ada = await r.manager.staff.hire(r.project.id, name="Ada", role="Planner", isolation="shared")
        first = task_of(await r.call(sid, "assign", staff="Ada", title="Tariff plan", **PLAN))
        await hand_in(r, ada)

        with pytest.raises(Refused, match=f"handed in {first} \"Tariff plan\" .* reads as the same work.*task_id='{first}'.*new=true"):
            await r.call(sid, "assign", staff="Ada", title="Tariff plan, revision 2", **REVISED)
        assert len(await r.board.list(project_id=r.project.id)) == 1, "a refused hand-over writes nothing"

        said = await r.call(sid, "assign", staff="Ada", task_id=first, title="Tariff plan, revision 2", **REVISED)
        assert task_of(said) == first
        assert 'The card was renamed: "Tariff plan" → "Tariff plan, revision 2"' in said
        card = await r.board.get(first)
        assert card["title"] == "Tariff plan, revision 2" and card["status"] == "doing"
        assert card["brief"]["objective"] == REVISED["objective"]
        assert 'round 2 begins, reopened from done by the orchestrator; round 1 was "Tariff plan": Plan the tariff limits' in card["notes"]

        await hand_in(r, ada, "revised")
        again = await r.call(sid, "assign", staff="Ada", task_id=first, done_when="plan.md answers all four questions")
        assert task_of(again) == first and "renamed" not in again
        card = await r.board.get(first)
        assert "round 3 begins" in card["notes"] and card["brief"]["done_when"] == "plan.md answers all four questions"
        assert len([t for t in await r.board.list(project_id=r.project.id) if t["assignee_staff_id"] == ada.id]) == 1

        await hand_in(r, ada, "final")
        separate = task_of(await r.call(sid, "assign", staff="Ada", title="Tariff plan, revision 2", new=True, **BRIEF))
        assert separate != first, "new=true is the orchestrator saying it is separate work"
    finally:
        await r.manager.close()


def test_titles_read_as_the_same_work_only_when_they_are_or_begin_with_each_other() -> None:
    from daedalus.extensions.orchestrator_team import _same_work

    assert _same_work("Tariff plan", "tariff  plan!")
    assert _same_work("Tariff plan, revision 2", "Tariff plan")
    assert _same_work("План тарифов", "План тарифов — раунд 2")
    assert not _same_work("Fix login", "Fix"), "too short to mean the same work"
    assert not _same_work("Tariff planning", "Tariff plan"), "a prefix of a word is not the same title"
    assert not _same_work("Presentation of the product", "Lessons from the videos, to the archive")


async def test_other_work_gets_a_card_of_its_own(settings: Settings, db: Database, tmp_path: Path) -> None:
    """Without a task_id nothing is continued: not after a pause, not for work that waits on
    something, and a card with a branch is not reopened even by its task_id."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        ada = await r.manager.staff.hire(r.project.id, name="Ada", role="Planner", isolation="shared")
        first = task_of(await r.call(sid, "assign", staff="Ada", title="Tariff plan", **PLAN))
        await hand_in(r, ada)
        hours_ago = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        await r.manager.db.execute("UPDATE board_tasks SET updated_at = ? WHERE id = ?", (hours_ago, first))
        later = task_of(await r.call(sid, "assign", staff="Ada", title="Audit the repositories", **BRIEF))
        assert later != first and (await task_row(r.manager, first))["status"] == "done"

        await hand_in(r, ada)
        after = task_of(await r.call(sid, "assign", staff="Ada", title="Build the limits", depends_on=[later], **BRIEF))
        assert after not in (first, later), "work that waits on another task is its own"
        with pytest.raises(Refused, match="branch"):
            await r.manager.db.execute("UPDATE board_tasks SET status = 'review', branch = 'agent/ada/x' WHERE id = ?", (first,))
            await r.call(sid, "assign", staff="Ada", task_id=first)
    finally:
        await r.manager.close()


async def test_a_one_off_helper_goes_when_its_turn_ends_not_in_the_middle_of_its_report(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        helper = await r.manager.staff.hire(r.project.id, name="Tmp", role="Helper", isolation="shared", one_off=True)
        task_id = task_of(await r.call(sid, "assign", staff="Tmp", title="Tariff plan", **PLAN))
        live = await r.team.live_of(helper)
        assert live is not None
        await r.team.ingress.status(live, "working")
        await r.team.ingress.report(live, "done", "plan written")
        # What the bus does with the move, done here in the open so the order is the test's.
        await r.team._task_finished(task_id)
        member = await r.manager.staff.get(helper.id)
        assert member is not None and member.active, "still in the turn that made the report"
        await r.team.ingress.status((await r.team.live_of(helper)) or live, "turn_done_unseen")

        async def dismissed() -> bool:
            gone = await r.manager.staff.get(helper.id)
            return gone is not None and not gone.active

        await until_await(dismissed, "the helper was dismissed at the end of its turn")
    finally:
        await r.manager.close()


# -- the cards review collected before this --------------------------------------------------------------


def _cleanup() -> str:
    [script] = [m for m in MIGRATIONS if isinstance(m, str) and "review is for what the operator must look at" in m]
    return script


def test_the_cleanup_closes_only_the_orchestrators_branchless_review_cards(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "board.sqlite")
    conn.executescript(
        """
        CREATE TABLE board_tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, notes TEXT NOT NULL DEFAULT '', session_id TEXT, run_id TEXT, updated_at TEXT,
            origin_session_id TEXT, project_id TEXT, assignee_staff_id TEXT, branch TEXT);
        CREATE TABLE app_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, type TEXT, payload_json TEXT);
        """
    )
    rows = [
        ("round", "review", "", "orch", "p", "st-ada", None),  # the junk: closed, with its result
        ("noted", "review", "operator: keep the tone", "orch", "p", "st-ada", ""),  # closed, its notes kept
        ("mine", "review", "", None, "p", "st-ada", None),  # the operator's own request
        ("merge", "review", "", "orch", "p", "st-ada", "agent/ada/x"),  # a branch to merge
        ("plan", "review", "", "agent", None, None, None),  # an ordinary agent's plan
        ("open", "doing", "", "orch", "p", "st-ada", None),
    ]
    conn.executemany("INSERT INTO board_tasks(id, title, status, notes, origin_session_id, project_id, assignee_staff_id, branch, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'x')", [(i, i, s, n, o, p, a, b) for i, s, n, o, p, a, b in rows])
    reports = [("checkpoint", "half way"), ("done", "first answer"), ("done", "the plan\nin two lines")]
    conn.executemany("INSERT INTO app_events(at, type, payload_json) VALUES ('2026-09-26T12:01:00Z', 'staff.report', ?)", [(json.dumps({"kind": k, "text": t, "task_id": "round"}),) for k, t in reports])

    conn.executescript(_cleanup())
    status = dict(conn.execute("SELECT id, status FROM board_tasks").fetchall())
    assert status == {"round": "done", "noted": "done", "mine": "review", "merge": "review", "plan": "review", "open": "doing"}
    notes = dict(conn.execute("SELECT id, notes FROM board_tasks").fetchall())
    assert notes["round"].startswith("[2026-09-26 12:01] result: the plan in two lines\n[") and notes["round"].endswith("review is for what the operator must look at")
    assert notes["noted"].startswith("operator: keep the tone\n[") and "result:" not in notes["noted"]

    before = conn.total_changes
    conn.executescript(_cleanup())
    assert conn.total_changes == before, "a second run finds nothing to close"
