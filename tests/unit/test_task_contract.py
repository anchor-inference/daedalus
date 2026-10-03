"""A card's contract and its acceptance: the operator's conditions on the card and in the member's
brief, the files the work starts from opened before it is handed in, a requirement added while the
member works reaching it with a receipt, and a result that is handed in, checked, or the operator's to
approve — each told apart, and a card accepted only with a mark for everything it promised."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.extensions.task_contract import split_checks
from daedalus.stores.database import MIGRATIONS, Database
from daedalus.stores.files import StoredFile
from daedalus.stores.staff import Staff
from tests.unit.test_board_rounds import task_of
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
        said = await r.call(
            sid, "assign", staff="Mira", title="Promo script", **SCRIPT,
            inputs=[{"file": one.handle, "text": "the quality bar: pacing and sound"}, two.handle],
            requirements=["Real screen recordings only, nothing drawn"],
        )
        task_id = task_of(said)
        assert "Requirements R1, R2, R3 are on the card" in said
        brief = runtime.started[-1].first_message
        assert "R1 (quality, from the operator): Real screen recordings only, nothing drawn" in brief
        assert "R2 (input, from the operator): the quality bar: pacing and sound — open " in brief and "/inbox/" in brief
        assert "R3 (input, from the operator): ref-b.mp4: the work starts from it — open " in brief
        assert "C1 script.md runs 45 to 60 seconds read aloud" in brief and "C2 every scene names the screen it shows" in brief
        assert "outrank the boundaries" in brief

        session = await live(r, mira)
        with pytest.raises(ValueError, match="inputs you have not opened: R2 .*R3"):
            await r.team.ingress.report(session, "done", "script.md written from the brief", call_id=f"fixture-report:{uuid.uuid4().hex}")
        assert (await task_row(r.manager, task_id))["status"] == "doing", "a refused hand-in moves nothing"

        # A tool of the member pointed at the copy is what counts as opening it.
        rows = await r.manager.db.fetchall("SELECT path FROM requirement_deliveries WHERE staff_session_id = ? AND path != ''", (session.id,))
        for row in rows:
            await r.team.contracts.opened(session.id, '{"path": "' + row["path"].split("/", 3)[-1] + '"}')
        told = await r.team.ingress.report(session, "done", "script.md: 52 seconds, six scenes", evidence=[{"item": "C1", "how": "read aloud with a timer", "result": "52 s"}], call_id=f"fixture-report:{uuid.uuid4().hex}")
        assert f"task {task_id} is handed in" in told and "you gave no evidence for C2, R1" in told
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
        task_id = task_of(await r.call(sid, "assign", staff="Cli", title="Promo script", **SCRIPT, inputs=[ref.handle]))
        cli = await r.manager.staff.find(r.project.id, "Cli")
        assert cli is not None
        session = await live(r, cli)
        brief = r.team.runtimes["claude"].started[-1].first_message  # type: ignore[attr-defined]
        assert 'confirm with Report(acknowledged=["R1"]) once you have read it' in brief
        with pytest.raises(ValueError, match="inputs you have not confirmed: R1"):
            await r.team.ingress.report(session, "done", "done", call_id=f"fixture-report:{uuid.uuid4().hex}")
        told = await r.team.ingress.report(session, "done", "script.md written after watching ref.mp4", acknowledged=["R1"], call_id=f"fixture-report:{uuid.uuid4().hex}")
        assert "confirmed R1" in told and f"task {task_id} is handed in" in told
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_requirement_added_while_the_member_works_reaches_it_with_a_receipt_until_it_confirms(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        mira = await member(r)
        task_id = task_of(await r.call(sid, "assign", staff="Mira", title="Promo script", **SCRIPT))
        said = await r.call(sid, "require", task_id=task_id, text="English only for now", kind="scope", source="operator")
        assert said.startswith(f"R1 is on {task_id} (scope, from the operator); sent to Mira into the turn they are in (receipt: submitted)")
        message = runtime.sent[-1][1]
        assert message.mode == "now" and message.text.startswith(f"[requirement R1 of task {task_id}, from the operator]\nEnglish only for now")
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert f"R1 of {task_id} sent to Mira" in state and "not confirmed: English only for now" in state

        told = await r.team.ingress.report(await live(r, mira), "checkpoint", "noted: English cut only", acknowledged=["R1", "R9"], call_id=f"fixture-report:{uuid.uuid4().hex}")
        assert "confirmed R1" in told and f"task {task_id} has no requirement R9 in force" in told
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
        await member(r)
        task_id = task_of(await r.call(sid, "assign", staff="Mira", title="Record the screens", **SCRIPT, requirements=["Real screen recordings at 2K/60, nothing drawn"]))
        with pytest.raises(Refused, match="R1 is the operator's .*their decision. AskOperator"):
            await r.call(sid, "require", task_id=task_id, text="Draw the settings screen if it cannot be recorded", replaces="R1")
        with pytest.raises(Refused, match="R1 is the operator's"):
            await r.call(sid, "require", task_id=task_id, withdraw="R1")
        with pytest.raises(Refused, match="was not answered by the operator yet"):
            ask = await r.orch.open_request(await r.refreshed(), sid, kind="question", text="May the settings screen be drawn?", options=["Yes", "No"], detail={}, title="Drawn screen")
            await r.call(sid, "require", task_id=task_id, text="Draw only the settings screen", replaces="R1", source=f"answer:{ask.short_id}")
        await r.manager.asks.resolve(ask.id, "operator", {"selected": ["Yes"]})
        said = await r.call(sid, "require", task_id=task_id, text="Real recordings, except the settings screen, which may be drawn", replaces="R1", source=f"answer:{ask.short_id}")
        assert said.startswith(f"R2 is on {task_id} (quality, from the operator's answer [{ask.short_id}]) in place of R1")
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
        again = await r.call(sid, "require", task_id=task_id, text="point number 3")
        assert again.startswith("R4 is on"), "the same words again are the requirement already there"
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
    task_id = task_of(await r.call(sid, "assign", staff="Mira", title="Promo cut", **{**SCRIPT, **extra}))
    await r.team.ingress.report(await live(r, mira), "done", "promo.mp4: 58 s, no sound", evidence=[{"item": "C1", "how": "ffprobe", "result": "58 s"}], call_id=f"fixture-report:{uuid.uuid4().hex}")
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
        with pytest.raises(Refused, match=r"not marked: C2 \(every scene names the screen it shows\), R1 \(Music and sound design"):
            await r.call(sid, "accept", task_id=task_id, checks=[{"item": "C1", "ok": True}])
        with pytest.raises(Refused, match="R1 is marked not met: accepted needs every item met"):
            await r.call(sid, "accept", task_id=task_id, checks=[{"item": "C1", "ok": True}, {"item": "C2", "ok": True}, {"item": "R1", "ok": False, "note": "silent"}])
        with pytest.raises(Refused, match="matches no check"):
            await r.call(sid, "accept", task_id=task_id, checks=[{"item": "C7", "ok": True}])
        said = await r.call(sid, "accept", task_id=task_id, checks=[{"item": "C1", "ok": True}, {"item": "every scene names the screen it shows", "ok": True}, {"item": "R1", "ok": True, "note": "heard it"}])
        assert said.startswith(f'{task_id} "Promo cut" is accepted: C1 met; C2 met; R1 met (heard it)')
        row = await task_row(r.manager, task_id)
        assert row["acceptance_state"] == "accepted"
        card = await r.board.get(task_id)
        assert [c["done"] for c in card["checklist"]] == [True, True] and card["checklist"][0]["evidence"]["result"] == "58 s"
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_returned_result_goes_back_to_its_member_with_what_failed(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        task_id, _ = await handed_in(r, sid, requirements=["Music and sound design, as the references have"])
        with pytest.raises(Refused, match="say what is wrong"):
            await r.call(sid, "accept", task_id=task_id, verdict="returned")
        said = await r.call(sid, "accept", task_id=task_id, verdict="returned", checks=[{"item": "R1", "ok": False, "note": "the cut is silent"}])
        assert said.startswith(f"returned {task_id}: R1 not met: the cut is silent. Mira started on {task_id}")
        assert "The orchestrator checked your last result and returned it: R1 not met: the cut is silent" in runtime.started[-1].first_message
        row = await task_row(r.manager, task_id)
        assert (row["status"], row["acceptance_state"]) == ("doing", "returned")
        card = await r.board.get(task_id)
        assert not any(c.get("evidence") or c.get("mark") for c in card["checklist"]), "a new round starts unmarked"
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_what_the_operator_uses_themselves_is_theirs_to_approve(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        task_id, _ = await handed_in(r, sid)
        said = await r.call(sid, "accept", task_id=task_id, ask_operator=True, checks=[{"item": "C1", "ok": True}, {"item": "C2", "ok": False, "note": "I could not see the screens"}])
        assert "is in the operator's review column with your marks (C1 met; C2 not met (I could not see the screens))" in said
        row = await task_row(r.manager, task_id)
        assert (row["status"], row["acceptance_state"]) == ("review", "accepted")
        posted = r.team.app.notifications.posted[-1]
        assert "waits for your acceptance" in posted.title and "C2 not met" in posted.body
        await r.board.accept(task_id)
        assert (await task_row(r.manager, task_id))["acceptance_state"] == "operator_approved"
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
        await r.call(sid, "accept", task_id=task_id, checks=[{"item": "C1", "ok": True}, {"item": "C2", "ok": True}])
        said = await r.call(sid, "project_report", text="The promo is ready to watch.", kind="done", task_id=task_id)
        assert "not checked" not in said
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_note_on_a_handed_in_card_is_not_refused_for_its_unmarked_checks(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A card a member handed in sits in done with its checks still to mark; the board refused every
    later edit of it as "the checklist is not complete"."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        task_id, _ = await handed_in(r, sid)
        await r.call(sid, "tasks", op="update", task_id=task_id, note="the operator watches it tonight")
        with pytest.raises(ValueError, match="the checklist is not complete"):
            await r.board.update(task_id, check=[0])
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


async def test_a_card_made_without_checks_takes_the_ones_its_acceptance_marks(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A finished card from before cards had checks: marking its checks by their words was refused as
    matching nothing, and the orchestrator went round in circles on a routine result."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        mira = await member(r)
        task_id = task_of(await r.call(sid, "assign", staff="Mira", title="Clean the render folder", **SCRIPT))
        await r.manager.db.execute("UPDATE board_tasks SET checklist = '[]' WHERE id = ?", (task_id,))
        await r.team.ingress.report(await live(r, mira), "done", "40,551 listed files deleted, finals intact", call_id=f"fixture-report:{uuid.uuid4().hex}")
        said = await r.call(sid, "accept", task_id=task_id, checks=[{"item": "The listed files are gone", "ok": True}, {"item": "The finals are intact", "ok": True, "note": "checksums"}])
        assert said.startswith(f'{task_id} "Clean the render folder" is accepted: C1 met; C2 met (checksums)')
        assert [c["text"] for c in (await r.board.get(task_id))["checklist"]] == ["The listed files are gone", "The finals are intact"]
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
