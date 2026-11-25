"""A card's history is a table of its own, written with each change, whichever path made it.

The paths that change a card without announcing it are the ones the history most needs: a member's
report into review, the verdict that finishes it, the dependents a close or an acceptance moves, a
CI or requirement change that sends a card back. Each is pinned here from the line it leaves, with
who acted and, for a dependent, the card that moved it. The migration gives the cards that already
exist what can still be told truthfully, and says it was reconstructed.
"""

# ruff: noqa: F811 — the board fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from daedalus.config import Settings
from daedalus.extensions.board_history import task_history
from daedalus.extensions.ci_observations import set_required_checks
from daedalus.stores.database import MIGRATIONS, Database
from daedalus.stores.task_history import TRIGGERS
from tests.support.authorized_results import accept_branchless_result
from tests.unit.test_board_extras import HEADERS, app, client, create, day, edit  # noqa: F401 — fixtures by name
from tests.unit.test_board_rounds import PLAN, assigned, hand_in
from tests.unit.test_orchestrator import rig
from tests.unit.test_orchestrator_team import fake, office
from tests.unit.test_staff_runtime import BRIEF, close_team


async def lines(client: httpx.AsyncClient, task_id: str) -> list[dict[str, Any]]:
    got = await client.get(f"/api/board/{task_id}/events", headers=HEADERS)
    assert got.status_code == 200, got.text
    return got.json()


async def revision_of(client: httpx.AsyncClient, task_id: str) -> int:
    listing = (await client.get("/api/board?include_done=1", headers=HEADERS)).json()
    return int(next(t["entity_revision"] for t in listing if t["id"] == task_id))


async def test_every_change_a_reader_cares_about_is_a_line_with_who_made_it(client: httpx.AsyncClient) -> None:
    task = await create(client, "Plan the launch", due_at=day(3))
    await edit(client, task, title="Plan the spring launch")
    await edit(client, task, priority=1)
    await edit(client, task, acceptance="the venue is booked")  # a field nobody reads in a history: no line
    await edit(client, task, assignee_operator=True)
    await edit(client, task, status="doing")
    closed = await client.post(f"/api/board/{task['id']}/close", headers=HEADERS, json={
        "client_operation_id": "close-launch", "expected_entity_revision": await revision_of(client, task["id"]),
        "status": "done", "reason": "booked and announced"})
    assert closed.status_code == 200, closed.text

    got = await lines(client, task["id"])
    assert [(e["type"], e.get("field")) for e in got] == [
        ("task.created", None), ("task.changed", "title"), ("task.changed", "priority"), ("task.assigned", None),
        ("task.moved", None), ("task.accepted", None), ("task.moved", None)]
    created, titled, prioritised, kept, started, accepted, done = got
    assert created["to"] == "todo" and created["due_at"] == f"{day(3)}T23:59:59Z" and created["text"] == "Created in todo"
    assert titled["change"] == {"from": "Plan the launch", "to": "Plan the spring launch"}
    assert prioritised["change"] == {"from": 3, "to": 1} and prioritised["text"] == "Priority P3 → P1"
    assert kept["assignee"] == {"kind": "operator"}
    assert (started["from"], started["to"]) == ("todo", "doing")
    # The operator's Close accepts a result of their own and finishes the card: both are lines of that one command.
    assert accepted["operation"] == done["operation"] == "board.task.close"
    assert (done["from"], done["to"]) == ("doing", "done")
    assert {e["actor"]["kind"] for e in got} == {"operator"} and {e["actor"]["id"] for e in got} == {"operator:1"}
    assert not any(e["backfilled"] for e in got)
    assert [e["seq"] for e in got] == sorted(e["seq"] for e in got) and all(e["at"].endswith("Z") for e in got)


async def test_a_dependent_moved_by_a_close_names_the_card_that_moved_it(client: httpx.AsyncClient) -> None:
    first = await create(client, "Book the venue")
    then = await create(client, "Print the tickets", depends_on=[first["id"]])
    assert then["status"] == "blocked"
    closed = await client.post(f"/api/board/{first['id']}/close", headers=HEADERS, json={
        "client_operation_id": "close-venue", "expected_entity_revision": await revision_of(client, first["id"]),
        "status": "done", "reason": "booked"})
    assert closed.status_code == 200, closed.text

    created, unblocked = await lines(client, then["id"])
    assert created["to"] == "blocked"
    assert (unblocked["type"], unblocked["from"], unblocked["to"]) == ("task.moved", "blocked", "todo")
    assert unblocked["cause"] == {"task_id": first["id"], "title": "Book the venue"}
    assert unblocked["operation"] == "board.task.close" and unblocked["actor"]["kind"] == "operator"
    assert "cause" not in (await lines(client, first["id"]))[-1]


async def test_a_members_report_its_acceptance_and_the_dependent_it_frees_are_lines(settings: Settings, db: Database,
                                                                                    tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        ada = await r.manager.staff.hire(r.project.id, name="Ada", role="Planner", isolation="shared")
        task_id, _ = await assigned(r, sid, staff="Ada", title="Tariff plan", **PLAN)
        waiting = await r.board.add(title="Build the limits", session_id=sid, brief=BRIEF, depends_on=[task_id])
        live = await r.team.live_of(ada)
        assert live is not None
        _folder, cwd = await r.team.cwd_of(live)
        (Path(cwd) / "plan.md").write_text("Four limits and two open questions\n")
        await hand_in(r, ada, artifacts=["plan.md"])
        await accept_branchless_result(r.team, task_id)

        got = await task_history(db, task_id)
        moves = [(e["from"], e["to"]) for e in got if e["type"] == "task.moved"]
        assert ("doing", "review") in moves and moves[-1] == ("review", "done"), moves
        report = next(e for e in got if e["type"] == "task.moved" and e["to"] == "review")
        # The report is the member's own word: its line names the member, not the system.
        assert report["actor"] == {"kind": "agent", "id": f"staff:{ada.id}", "name": "Ada"}
        accepted = next(e for e in got if e["type"] == "task.accepted")
        assert accepted["actor"]["kind"] == "operator" and accepted["seq"] < got[-1]["seq"]
        freed = [e for e in await task_history(db, waiting["id"]) if e["type"] == "task.moved"]
        assert [(e["from"], e["to"], e["cause"]["task_id"]) for e in freed] == [("blocked", "todo", task_id)]
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_ci_requirement_that_returns_a_card_from_review_is_a_line(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO board_tasks(id,title,status,priority,created_at,updated_at) VALUES"
                         " ('task1','Review','review',3,'2026-01-01','2026-01-01')")
        await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                         "snapshot_json,created_at) VALUES ('task1',1,'operator','','{}','2026-01-01')")
        async with db.transaction() as conn:
            revised = await set_required_checks(conn, task_id="task1", provider="github", repository_id="7",
                                                check_names=["unit"], origin_ref="receipt")
        assert revised["returned_from_review"] is True
        moved = (await task_history(db, "task1"))[-1]
        # Outside any command the change is the system's: nothing claimed it, and nothing pretends to know who.
        assert (moved["from"], moved["to"], moved["actor"]) == ("review", "todo", {"kind": "system", "id": None, "name": None})
    finally:
        await db.close()


async def test_a_card_deleted_by_the_legacy_board_takes_its_history_with_it(db: Database) -> None:
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,created_at,updated_at) VALUES ('t1','x','todo',3,'a','a')")
    assert (await db.fetchone("SELECT count(*) FROM board_task_history WHERE task_id = 't1'"))[0] == 1
    await db.execute("DELETE FROM board_tasks WHERE id = 't1'")
    assert (await db.fetchone("SELECT count(*) FROM board_task_history WHERE task_id = 't1'"))[0] == 0


async def test_writes_that_change_nothing_a_reader_cares_about_leave_no_line(db: Database) -> None:
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,created_at,updated_at) VALUES ('t1','x','todo',3,'a','a')")
    await db.execute("UPDATE board_tasks SET heartbeat_at = 'now', notes = 'n', status = 'todo', title = 'x' WHERE id = 't1'")
    assert (await db.fetchone("SELECT count(*) FROM board_task_history WHERE task_id = 't1'"))[0] == 1


# -- the migration -------------------------------------------------------------------------------


async def test_the_migration_backfills_what_is_known_and_marks_it(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    db = Database(path, local_env="container")
    await db.open()
    # The shape before the table: no history, no triggers, the ring's index by task.
    for name in ("board_task_history_created", "board_task_history_changed"):
        await db.execute(f"DROP TRIGGER {name}")
    await db.execute("DROP TABLE board_task_history")
    await db.execute("CREATE INDEX app_events_by_task ON app_events(json_extract(payload_json, '$.task_id'), seq)"
                     " WHERE json_extract(payload_json, '$.task_id') IS NOT NULL")
    old, recent, finished, gone = "old001", "new001", "fin001", "rem001"
    for task_id, status, created in ((old, "todo", "2026-01-05T10:00:00+00:00"), (recent, "blocked", "2026-11-24T09:00:00.123456+00:00"),
                                     (finished, "done", "2026-02-01T08:00:00+00:00"), (gone, "dropped", "not a date")):
        await db.execute("INSERT INTO board_tasks(id,title,status,priority,created_at,updated_at) VALUES (?,?,?,3,?,?)",
                         (task_id, task_id, status, created, created))
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,snapshot_json,created_at)"
                     " VALUES (?,1,'operator','','{}','2026-02-01T08:00:00+00:00')", (finished,))
    await db.execute("INSERT INTO result_receipts(id,task_id,contract_revision,attempt_id,outcome,original_text,original_blob_ref,"
                     "original_digest,original_size_bytes,checks_json,limitations_json,actor_id,created_at)"
                     " VALUES ('r1',?,1,NULL,'complete','done',NULL,'d',4,'[]','[]','staff:x','2026-02-02T08:00:00+00:00')", (finished,))
    await db.execute("INSERT INTO review_verdicts(id,result_id,contract_revision,reviewer_actor_id,verification,accepted,head,base,"
                     "environment_digest,evidence_json,reason,self_review_waiver_receipt_id,created_at)"
                     " VALUES ('v1','r1',1,'operator:1','verified',1,NULL,NULL,NULL,'[]','fine',NULL,'2026-02-03T08:00:00+00:00')")
    await db.execute("UPDATE board_tasks SET accepted_result_id = 'r1' WHERE id = ?", (finished,))
    await db.execute("UPDATE board_tasks SET removed_at = '2026-03-01T00:00:00+00:00' WHERE id = ?", (gone,))
    for at, kind, payload in (
            ("2026-11-24T09:00:00.200Z", "task.created", {"task_id": recent, "actor": "agent", "actor_id": "orchestrator:s1"}),
            ("2026-11-24T09:05:00.000Z", "task.changed", {"task_id": recent, "actor": "operator"}),  # names no field: left out
            ("2026-11-24T09:06:00.000Z", "task.changed", {"task_id": recent, "actor": "operator", "due_at": "2026-12-01T23:59:59Z"}),
            ("2026-11-24T09:07:00.000Z", "task.moved", {"task_id": recent, "actor": "operator", "actor_id": "operator:1", "from": "todo", "to": "blocked"}),
            ("2026-11-24T09:08:00.000Z", "task.ci_failed", {"task_id": recent}),
            ("2026-11-24T09:09:00.000Z", "task.moved", {"task_id": "someone-elses", "from": "todo", "to": "doing"})):
        await db.execute("INSERT INTO app_events(at, type, payload_json) VALUES (?, ?, ?)", (at, kind, json.dumps(payload)))
    before = next(i for i, step in enumerate(MIGRATIONS) if getattr(getattr(step, "step", None), "__name__", "") == "backfill")
    await db.execute(f"UPDATE schema_version SET version = {before}")
    await db.close()

    for _ in range(2):  # the second start writes nothing twice
        again = Database(path, local_env="container")
        await again.open()
        try:
            assert int((await again.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
            history = {task_id: await task_history(again, task_id) for task_id in (old, recent, finished, gone)}
            assert all(e["backfilled"] for got in history.values() for e in got)
            # A card older than the ring: its creation from the row, the column it was made in unknown.
            assert [(e["type"], e["to"], e["at"]) for e in history[old]] == [("task.created", None, "2026-01-05T10:00:00.000Z")]
            assert history[old][0]["text"] == "Created" and history[old][0]["actor"]["kind"] == "system"
            # A recent card: what the ring still holds, with who acted, and nothing invented.
            assert [(e["type"], e.get("field")) for e in history[recent]] == [("task.created", None), ("task.changed", "due_at"), ("task.moved", None)]
            assert history[recent][0]["actor"]["id"] == "orchestrator:s1" and history[recent][2]["actor"]["id"] == "operator:1"
            assert history[recent][1]["due_at"] == "2026-12-01T23:59:59Z"
            # A finished card: its creation and the verdict that accepted the result it carries, in time order.
            assert [(e["type"], e["at"]) for e in history[finished]] == [
                ("task.created", "2026-02-01T08:00:00.000Z"), ("task.accepted", "2026-02-03T08:00:00.000Z")]
            assert history[finished][1]["actor"]["id"] == "operator:1"
            # A removed card whose creation date is unreadable keeps it as it was, first.
            assert [(e["type"], e["at"]) for e in history[gone]] == [("task.created", "not a date"), ("task.removed", "2026-03-01T00:00:00.000Z")]
            assert await again.fetchall("SELECT 1 FROM sqlite_master WHERE name = 'app_events_by_task'") == []
            assert (await again.fetchone("SELECT count(*) FROM board_task_history"))[0] == 8
            assert [r[0] for r in await again.fetchall("PRAGMA integrity_check")] == ["ok"]
            assert [tuple(r) for r in await again.fetchall("PRAGMA foreign_key_check")] == []
        finally:
            await again.close()
    # From now on the triggers write the live lines.
    again = Database(path, local_env="container")
    await again.open()
    try:
        await again.execute("UPDATE board_tasks SET status = 'doing' WHERE id = ?", (old,))
        assert [(e["type"], e["backfilled"]) for e in await task_history(again, old)] == [("task.created", True), ("task.moved", False)]
    finally:
        await again.close()


def test_the_triggers_can_be_made_again_by_a_migration_that_rebuilds_the_table() -> None:
    # A rebuild of board_tasks drops them with the table; the statement it runs again must stand alone.
    assert TRIGGERS.count("CREATE TRIGGER IF NOT EXISTS") == 2 and "board_task_history(" in TRIGGERS
