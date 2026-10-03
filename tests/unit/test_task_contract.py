"""A card's contract and its acceptance: the operator's conditions on the card and in the member's
brief, the files the work starts from opened before it is handed in, a requirement added while the
member works reaching it with a receipt, and a result that is handed in, checked, or the operator's to
approve — each told apart, and a card accepted only with a mark for everything it promised."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.extensions.runtime_observations import admit_native_run, observe_exit
from daedalus.extensions.task_contract import split_checks
from daedalus.stores.database import MIGRATIONS, Database
from daedalus.stores.files import StoredFile
from daedalus.stores.staff import Staff
from tests.support.authorized_launch import operator_task
from tests.support.authorized_results import accept_branchless_result, return_reviewed_result
from tests.support.authorized_stop import bind_native_run, finish_native_stop
from tests.support.waiting import until_await
from tests.unit.test_board_rounds import assigned, task_of
from tests.unit.test_orchestrator import Rig, rig
from tests.unit.test_orchestrator_team import fake, office
from tests.unit.test_staff_runtime import Capacity, close_team, task_row

SCRIPT = {
    "objective": "Write a 45 to 60 second promo script at the level of the two reference videos",
    "deliverable": "script.md in the promo folder",
    "boundaries": "Only the promo folder; publish nothing",
    "done_when": "- script.md runs 45 to 60 seconds read aloud\n- every scene names the screen it shows",
}


async def attach(r: Rig, name: str) -> StoredFile:
    return await r.manager.files.add(b"\x00\x00\x00\x18ftypmp42" + name.encode(), name=name, mime="video/mp4", origin="operator", origin_ref="chat", scope=r.project.id, actor="operator")


async def member(r: Rig, name: str = "Mira", harness: str = "daedalus") -> Staff:
    return await r.manager.staff.hire(r.project.id, name=name, role="Scripts", harness=harness, isolation="shared")


async def live(r: Rig, who: Staff) -> Any:
    found = await r.team.live_of(who)
    assert found is not None
    return found


async def apply_requirement(r: Rig, sid: str, task_id: str, **request: Any) -> dict[str, Any]:
    """Use both real receipts when the task has no running attempt."""
    staged = json.loads(await r.call(sid, "require", task_id=task_id, **request))
    assert staged["state"] == "ready_to_apply"
    applied = json.loads(await r.call(sid, "require", op="apply", task_id=task_id,
                                      intent_id=staged["intent_id"]))
    assert applied["state"] == "applied"
    return applied


# -- the contract ------------------------------------------------------------------------------------------------


async def test_inputs_and_the_operators_conditions_are_in_the_brief_and_an_unopened_input_blocks_the_hand_in(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The scriptwriter was briefed "at the level of the two references" with no files, and reported a
    script it said no reference had reached. The references are inputs now: named in the brief, and
    the work cannot be handed in until the member has opened them."""
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        mira = await member(r)
        one, two = await attach(r, "ref-a.mp4"), await attach(r, "ref-b.mp4")
        task_id, said = await assigned(
            r, sid, staff="Mira", title="Promo script", **SCRIPT,
            inputs=[{"file": one.handle, "text": "the quality bar: pacing and sound"}, two.handle],
            requirements=["Real screen recordings only, nothing drawn"],
        )
        assert "3 requirements are on the card" in said
        brief = runtime.started[-1].first_message
        assert "R1 (quality, from the operator): Real screen recordings only, nothing drawn" in brief
        assert "R2 (input, from the operator): the quality bar: pacing and sound — open " in brief and "/inbox/" in brief
        assert "R3 (input, from the operator): ref-b.mp4: the work starts from it — open " in brief
        assert "C1 script.md runs 45 to 60 seconds read aloud" in brief and "C2 every scene names the screen it shows" in brief
        assert "outrank the boundaries" in brief

        session = await live(r, mira)
        with pytest.raises(ValueError, match="task input was not opened or confirmed: R2, R3"):
            await r.team.ingress.report(session, "done", "script.md written from the brief", call_id=f"fixture-report:{uuid.uuid4().hex}")
        assert (await task_row(r.manager, task_id))["status"] == "doing", "a refused hand-in moves nothing"

        # A tool of the member pointed at the copy is what counts as opening it.
        rows = await r.manager.db.fetchall("SELECT path FROM requirement_deliveries WHERE staff_session_id = ? AND path != ''", (session.id,))
        for row in rows:
            await r.team.contracts.opened(session.id, '{"path": "' + row["path"].split("/", 3)[-1] + '"}')
        told = await r.team.ingress.report(session, "done", "script.md: 52 seconds, six scenes", evidence=[{"item": "C1", "how": "read aloud with a timer", "result": "52 s"}], call_id=f"fixture-report:{uuid.uuid4().hex}")
        assert "task is in review" in told and "you gave no evidence for C2, R1" in told
        assert (await task_row(r.manager, task_id))["acceptance_state"] == "handed_in"
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_command_line_member_confirms_its_inputs_in_words(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The host cannot see a command-line member open a file, so it takes its word, said out loud."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        r.team.runtimes["claude"] = r.team.runtimes["daedalus"]
        r.team._capacity = Capacity()  # a terminal to start the command-line member in
        sid = await office(r)
        await member(r, "Cli", harness="claude")
        ref = await attach(r, "ref.mp4")
        task_id, _ = await assigned(r, sid, staff="Cli", title="Promo script", **SCRIPT, inputs=[ref.handle])
        cli = await r.manager.staff.find(r.project.id, "Cli")
        assert cli is not None
        session = await live(r, cli)
        brief = r.team.runtimes["claude"].started[-1].first_message  # type: ignore[attr-defined]
        assert 'confirm with Report(acknowledged=["R1"]) once you have read it' in brief
        with pytest.raises(ValueError, match="task input was not opened or confirmed: R1"):
            await r.team.ingress.report(session, "done", "done", call_id=f"fixture-report:{uuid.uuid4().hex}")
        told = await r.team.ingress.report(session, "done", "script.md written after watching ref.mp4", acknowledged=["R1"], call_id=f"fixture-report:{uuid.uuid4().hex}")
        assert "confirmed R1" in told and "task is in review" in told
        assert (await task_row(r.manager, task_id))["acceptance_state"] == "handed_in"
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_requirement_added_while_the_member_works_waits_for_a_new_attempt(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        mira = await member(r)
        task_id = task_of(await r.call(sid, "assign", staff="Mira", title="Promo script", **SCRIPT))
        old = await live(r, mira)
        run_id, stops = await bind_native_run(r.team, old, monkeypatch)
        staged = json.loads(await r.call(sid, "require", task_id=task_id, text="English only for now",
                                         kind="scope", source="operator"))
        assert staged["state"] == "pending_physical_exit"
        assert runtime.sent == []
        assert await r.manager.db.fetchone("SELECT id FROM task_requirements WHERE task_id = ?", (task_id,)) is None
        await finish_native_stop(r.team, old, run_id, staged["stop_effect_id"], stops)
        with pytest.raises(PermissionError):
            await r.team.ingress.report(old, "checkpoint", "stale acknowledgement", acknowledged=["R1"],
                                        call_id=f"fixture-report:{uuid.uuid4().hex}")
        applied = json.loads(await r.call(sid, "require", op="apply", task_id=task_id,
                                          intent_id=staged["intent_id"]))
        assert applied["state"] == "applied" and applied["contract_revision"] == 2
        await r.call(sid, "assign", task_id=task_id, staff="Mira")
        from tests.unit.test_orchestrator_team import admitted

        await until_await(lambda: admitted(r, task_id), "the new contract launched")
        assert "R1 (scope, from the operator): English only for now" in runtime.started[-1].first_message
        fresh = await live(r, mira)
        assert fresh.id != old.id
        told = await r.team.ingress.report(fresh, "checkpoint", "noted: English cut only",
                                           acknowledged=["R1", "R9"], call_id=f"fixture-report:{uuid.uuid4().hex}")
        assert "confirmed R1" in told and "requirements not confirmed: R9" in told
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert "not confirmed" not in state
        assert "R1 [scope, from the operator] English only for now (Mira: confirmed)" in await r.call(sid, "tasks", op="get", task_id=task_id)
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_the_operators_requirement_is_changed_only_on_their_answer(settings: Settings, db: Database, tmp_path: Path) -> None:
    """"Draw the scene if you cannot record it" went to a member as the orchestrator's own allowance,
    against the operator's demand for real recordings."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        task_id = await operator_task(db, r.project.id, "Record the screens", brief=SCRIPT)
        await apply_requirement(r, sid, task_id, text="Real screen recordings at 2K/60, nothing drawn",
                                source="operator")
        with pytest.raises(Refused, match="R1 is the operator's .*their decision. AskOperator"):
            await r.call(sid, "require", task_id=task_id, text="Draw the settings screen if it cannot be recorded", replaces="R1")
        with pytest.raises(Refused, match="R1 is the operator's"):
            await r.call(sid, "require", task_id=task_id, withdraw="R1")
        with pytest.raises(Refused, match="was not answered by the operator yet"):
            ask = await r.orch.open_request(await r.refreshed(), sid, kind="question", text="May the settings screen be drawn?", options=["Yes", "No"], detail={}, title="Drawn screen")
            await r.call(sid, "require", task_id=task_id, text="Draw only the settings screen", replaces="R1", source=f"answer:{ask.short_id}")
        await r.manager.asks.resolve(ask.id, "operator", {"selected": ["Yes"]})
        applied = await apply_requirement(r, sid, task_id,
                                          text="Real recordings, except the settings screen, which may be drawn",
                                          replaces="R1", source=f"answer:{ask.short_id}")
        assert applied["contract_revision"] == 3
        states = {row["number"]: row["state"] for row in await r.manager.db.fetchall("SELECT number, state FROM task_requirements WHERE task_id = ?", (task_id,))}
        assert states == {1: "superseded", 2: "active"}
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_card_holds_a_bounded_number_of_requirements(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        await member(r)
        task_id = task_of(await r.call(sid, "assign", staff="Mira", title="Promo script", **SCRIPT, requirements=[f"point number {i}" for i in range(20)]))
        with pytest.raises(Refused, match="already has 20 requirements in force; merge some"):
            await r.call(sid, "require", task_id=task_id, text="one more point")
        again = json.loads(await r.call(sid, "require", task_id=task_id, text="point number 3"))
        assert (again["state"], again["label"]) == ("unchanged", "R4"), "the same words need no revision"
    finally:
        await close_team(r.manager)
        await r.manager.close()


def test_a_done_when_becomes_one_check_per_line_or_item() -> None:
    assert split_checks("- plays\n- runs 45 to 60 s\n\n3. has sound") == ["plays", "runs 45 to 60 s", "has sound"]
    assert split_checks("the file plays; it runs 45 to 60 seconds") == ["the file plays", "it runs 45 to 60 seconds"]
    assert split_checks("plays; ok") == ["plays; ok"], "halves too short to be checks stay one sentence"
    assert split_checks("") == []


# -- acceptance -------------------------------------------------------------------------------------------------


async def handed_in(r: Rig, sid: str, **extra: Any) -> tuple[str, Staff]:
    mira = await member(r)
    task_id, _ = await assigned(r, sid, staff="Mira", title="Promo cut", **{**SCRIPT, **extra})
    current = await live(r, mira)
    _folder, cwd = await r.team.cwd_of(current)
    (Path(cwd) / "script.md").write_text("A 58-second promo cut with named scenes\n")
    source = await r.manager.db.fetchone("SELECT tenant_id FROM sessions WHERE id = ?",
                                         (current.session.session_id,))
    assert source is not None
    run_id = uuid.uuid4().hex
    at = datetime.now(UTC).isoformat()
    await r.manager.db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                               " VALUES (?,?,?,'running',?,?)",
                               (run_id, source["tenant_id"], current.session.session_id, at, at))
    await admit_native_run(r.team.app, current.id, current.session.session_id, run_id)
    await r.team.ingress.report(current, "done", "promo.mp4: 58 s, no sound", artifacts=["script.md"],
                                evidence=[{"item": "C1", "how": "ffprobe", "result": "58 s"}],
                                call_id=f"fixture-report:{uuid.uuid4().hex}")
    await r.manager.db.execute("UPDATE runs SET status = 'completed' WHERE id = ?", (run_id,))
    assert await observe_exit(r.team.app, staff_session_id=current.id, runtime_ref=run_id,
                              observed_status="completed")
    await r.team.ingress.status(await live(r, mira), "idle")
    return task_id, mira


async def test_a_card_is_accepted_only_with_a_mark_for_every_check_and_requirement(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A silent cut was closed as done against a bar of two reference videos with sound: a member's
    report was all it took."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        task_id, _ = await handed_in(r, sid, requirements=["Music and sound design, as the references have"])
        result_id = await accept_branchless_result(r.team, task_id)
        row = await task_row(r.manager, task_id)
        assert row["acceptance_state"] == "operator_approved" and row["accepted_result_id"] == result_id
        card = await r.board.get(task_id)
        assert [c["done"] for c in card["checklist"]] == [True, True]
        report = await r.manager.db.fetchone("SELECT checks_json FROM result_receipts WHERE id = ?", (result_id,))
        assert report is not None and json.loads(report["checks_json"])[0]["result"] == "58 s"
        evidence = await r.manager.db.fetchall("SELECT criterion_id FROM review_evidence WHERE result_id = ?", (result_id,))
        snapshot = await r.manager.db.fetchone("SELECT snapshot_json FROM task_contract_versions"
                                               " WHERE task_id = ? AND contract_revision = 1", (task_id,))
        assert snapshot is not None
        required = json.loads(snapshot["snapshot_json"])
        assert {row["criterion_id"] for row in evidence} == {
            *(item["id"] for item in required["checklist"]),
            *(item["id"] for item in required["requirements"]),
        }
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_returned_result_goes_back_to_its_member_with_what_failed(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        task_id, _ = await handed_in(r, sid, requirements=["Music and sound design, as the references have"])
        result_id = await return_reviewed_result(r.team, task_id, reason="the cut is silent")
        row = await task_row(r.manager, task_id)
        assert (row["status"], row["acceptance_state"]) == ("todo", "returned")
        returned = await r.manager.db.fetchone("SELECT result_id,reason FROM review_returns WHERE task_id = ?", (task_id,))
        assert returned is not None and (returned["result_id"], returned["reason"]) == (result_id, "the cut is silent")
        await r.call(sid, "assign", task_id=task_id, staff="Mira")
        from tests.support.waiting import until_await
        from tests.unit.test_orchestrator_team import admitted
        await until_await(lambda: admitted(r, task_id), "the returned task launched with a new attempt")
        assert "The orchestrator checked your last result and returned it: the cut is silent" in runtime.started[-1].first_message
        card = await r.board.get(task_id)
        assert not any(c.get("evidence") or c.get("mark") for c in card["checklist"]), "a new round starts unmarked"
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_operator_review_requires_exact_approval(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        task_id, _ = await handed_in(r, sid)
        row = await task_row(r.manager, task_id)
        assert (row["status"], row["acceptance_state"], row["accepted_result_id"]) == (
            "review", "handed_in", None)
        result_id = await accept_branchless_result(r.team, task_id)
        row = await task_row(r.manager, task_id)
        assert (row["status"], row["acceptance_state"], row["accepted_result_id"]) == (
            "done", "operator_approved", result_id)
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_report_of_done_to_the_operator_says_when_nobody_checked_the_work(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        task_id, _ = await handed_in(r, sid)
        said = await r.call(sid, "project_report", text="The promo is ready to watch.", kind="done", task_id=task_id)
        assert "is handed in and not checked, and the operator was told so" in said
        assert r.team.app.notifications.posted[-1].body.startswith("(Handed in by the member, not checked yet.)\nThe promo is ready to watch.")
        await accept_branchless_result(r.team, task_id)
        said = await r.call(sid, "project_report", text="The promo is ready to watch.", kind="done", task_id=task_id)
        assert "not checked" not in said
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_note_on_a_handed_in_card_is_not_refused_for_its_unmarked_checks(settings: Settings, db: Database, tmp_path: Path) -> None:
    """An editorial note on a handed-in result cannot silently accept its unmet checks."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        task_id, _ = await handed_in(r, sid)
        await r.call(sid, "tasks", op="update", task_id=task_id, note="the operator watches it tonight")
        card = await r.board.get(task_id)
        assert "the operator watches it tonight" in card["notes"]
        assert not any(check["done"] for check in card["checklist"])
        row = await task_row(r.manager, task_id)
        assert row["status"] == "review" and row["accepted_result_id"] is None
    finally:
        await close_team(r.manager)
        await r.manager.close()


def _migration() -> str:
    [script] = [m for m in MIGRATIONS if isinstance(m, str) and "CREATE TABLE task_requirements" in m]
    return script


def test_the_migration_says_how_far_each_finished_card_was_accepted(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "board.sqlite")
    conn.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY);
        CREATE TABLE board_tasks (id TEXT PRIMARY KEY, status TEXT, branch TEXT, merge_state TEXT, origin_session_id TEXT, project_id TEXT, updated_at TEXT);
        """
    )
    recent = "2999-01-01T00:00:00+00:00"
    rows = [
        ("merged", "done", "agent/ada/x", "merged", "orch", "p", "2026-01-01T00:00:00+00:00"),
        ("recent", "done", None, "", "orch", "p", recent),
        ("old", "done", None, "", "orch", "p", "2026-01-01T00:00:00+00:00"),
        ("mine", "done", None, "", None, "p", recent),
        ("review", "review", "agent/ada/y", "proposed", "orch", "p", recent),
        ("open", "doing", None, "", "orch", "p", recent),
        ("plan", "done", None, "", "agent", None, recent),
    ]
    conn.executemany("INSERT INTO board_tasks VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.executescript(_migration())
    states = dict(conn.execute("SELECT id, acceptance_state FROM board_tasks").fetchall())
    assert states == {"merged": "operator_approved", "recent": "handed_in", "old": "accepted", "mine": "accepted", "review": "handed_in", "open": "", "plan": ""}
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE board_tasks SET acceptance_state = 'maybe' WHERE id = 'open'")


async def test_assignment_done_when_checks_bind_result_acceptance(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The card's immutable done-when checks carry through its report and exact acceptance."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        mira = await member(r)
        task_id, _ = await assigned(r, sid, staff="Mira", title="Clean the render folder", **SCRIPT)
        current = await live(r, mira)
        _folder, cwd = await r.team.cwd_of(current)
        (Path(cwd) / "inventory.txt").write_text("Files removed; final assets retained\n")
        await r.team.ingress.report(current, "done", "40,551 listed files deleted, finals intact",
                                    artifacts=["inventory.txt"], call_id=f"fixture-report:{uuid.uuid4().hex}")
        result_id = await accept_branchless_result(r.team, task_id)
        assert (await task_row(r.manager, task_id))["accepted_result_id"] == result_id
        assert [c["text"] for c in (await r.board.get(task_id))["checklist"]] == [
            "script.md runs 45 to 60 seconds read aloud", "every scene names the screen it shows"]
        # A card made by Tasks gets its checks from its done-when, as one made by Assign does.
        made = await r.call(sid, "tasks", op="create", title="Captions for the video", **{**SCRIPT, "objective": "Write the captions of the product video in both languages"})
        created = await r.board.get(made.split()[0])
        assert [c["text"] for c in created["checklist"]] == ["script.md runs 45 to 60 seconds read aloud", "every scene names the screen it shows"]
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_folder_is_found_by_the_name_it_is_listed_by(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        name = Path(r.project.primary.path).name
        assert name in await r.call(sid, "folders")
        said = await r.call(sid, "hire", name="Rex", role="Review", folder=name)
        assert said.startswith("hired Rex")
    finally:
        await close_team(r.manager)
        await r.manager.close()
