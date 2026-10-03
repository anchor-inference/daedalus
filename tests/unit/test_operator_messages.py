"""The operator's side of an orchestrated project: a member's steps for them delivered by the host word
for word, a message that arrives in the middle of a turn said to be so, a message that names what it
replies to, a message that asked several things kept as several commitments, what became of each
message, and searching one conversation."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from protocore.contracts.types import TextBlock

from daedalus.config import Settings
from daedalus.extensions.operator_steps import StepsRefused, normalise
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.stores.database import Database
from tests.support.authorized_launch import operator_task
from tests.support.authorized_results import accept_branchless_result
from tests.support.authorized_stop import bind_native_run, finish_native_stop, stop_native_task
from tests.support.waiting import until_await
from tests.unit.test_board_rounds import task_of
from tests.unit.test_orchestrator import Rig, rig
from tests.unit.test_orchestrator_team import fake, office
from tests.unit.test_session_runner import ScriptedProvider
from tests.unit.test_staff_runtime import close_team
from tests.unit.test_steer_queue import H, _await_run, _client, _manager
from tests.unit.test_task_contract import SCRIPT, apply_requirement

STEPS = {
    "goal": "Manage the team's mailboxes yourself",
    "steps": ["Open the tunnel with the command in the admin notes.", "Sign in as admin@ with the password from the secrets file.", "Under Accounts, New account adds a mailbox."],
    "roles": [{"account": "admin@", "purpose": "manages accounts"}, {"account": "team@", "purpose": "the team's mailbox"}],
    "expected": "The new mailbox is in the list.",
    "verified": "unverified",
    "verified_how": "step 3 only on an older copy",
}


async def working_on(r: Rig, sid: str, name: str = "Webops") -> tuple[str, Any]:
    member = await r.manager.staff.hire(r.project.id, name=name, role="Mail", isolation="shared")
    task_id = task_of(await r.call(sid, "assign", staff=name, title="Mail admin page", **SCRIPT))
    live = await r.team.live_of(member)
    assert live is not None
    return task_id, live


# -- a member's steps for the operator ------------------------------------------------------------------------


def test_steps_are_checked_before_they_are_carried() -> None:
    with pytest.raises(StepsRefused, match="needs its goal"):
        normalise({"steps": ["a"], "verified": "unverified"})
    with pytest.raises(StepsRefused, match="verified is 'on-running-version'"):
        normalise({"goal": "g", "steps": ["a"]})
    with pytest.raises(StepsRefused, match="list of the steps"):
        normalise({"goal": "g", "steps": [], "verified": "unverified"})
    steps = normalise(STEPS)
    text = steps.markdown(member="Webops", task="t1")
    assert "**Not checked on the running version.** step 3 only on an older copy" in text
    assert "| admin@ | manages accounts |" in text and "3. Under Accounts, New account adds a mailbox." in text


async def test_a_members_steps_reach_the_operator_word_for_word_and_the_orchestrator_is_told_not_to_retell_them(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The retelling of a member's instruction lost the table of accounts and the steps, then a step
    with the current password; an address the member never gave was offered as the way in."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        await r.orch.stop_queue(r.project.id)
        task_id, live = await working_on(r, sid)
        with pytest.raises(ValueError, match="verified is"):
            await r.team.ingress.report(live, "done", "the page works", operator_steps={"goal": "g", "steps": ["a"]}, call_id=f"fixture-report:{uuid.uuid4().hex}")
        told = await r.team.ingress.report(live, "done", "the admin page works through the tunnel", operator_steps=STEPS, call_id=f"fixture-report:{uuid.uuid4().hex}")
        assert "reported done; report " in told and "operator-steps-" in told
        [note] = [m for m in await r.manager.sessions.list_transcript(sid) if m.metadata.get("daedalus.operator_steps")]
        text = "".join(b.text for b in note.content_blocks if isinstance(b, TextBlock))
        assert text.startswith("# Manage the team's mailboxes yourself") and "2. Sign in as admin@" in text and "Not checked on the running version" in text
        carried = note.metadata["daedalus.operator_steps"]
        assert carried["member"] == "Webops" and carried["task_id"] == task_id and carried["file"]["name"].startswith(f"operator-steps-{task_id}-")
        posted = r.team.app.notifications.posted[-1]
        assert posted.title.endswith("steps for you — Manage the team's mailboxes yourself (not checked on the running version)") and "| team@ | the team's mailbox |" in posted.body
        stored = await r.manager.files.in_scope(f"att:{carried['file']['id']}", r.project.id)
        assert (await r.manager.files.read(stored)).decode().startswith("# Manage the team's mailboxes yourself")
        [event] = [e for e in await r.manager.bus.replay(0) if e.type == "staff.report" and e.payload.get("operator_steps")]
        line = await r.orch.line(await r.refreshed(), event)
        assert "were put in front of the operator word for word" in line and "NOT checked on the running version (step 3 only on an older copy)" in line and "do not retell them" in line
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- a message in the middle of a turn ------------------------------------------------------------------------------


async def test_a_message_written_during_a_turn_reaches_the_model_marked_and_the_chat_as_written(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 1"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)

    async def mark(session_id: str, text: str) -> str:
        from daedalus.host import prompts

        return prompts.MID_TURN_NOTE.format(began="", recent="") + text

    manager.steer_hooks.append(mark)
    state = await manager.create_session("steered")
    sid = state.session.id
    await manager.submit(sid, "start")
    await asyncio.sleep(0.3)
    await manager.submit(sid, "the mail instruction, not the updater", steer=True)
    await _await_run(manager)
    placed = [b.text for m in provider.requests[-1].messages for b in m.content_blocks if isinstance(b, TextBlock)]
    assert any(t.startswith("(The operator wrote this while you were in the middle of a turn.") and t.endswith("the mail instruction, not the updater") for t in placed)
    shown = [m["text"] for m in await manager.transcript_page(sid) if m["role"] == "user" and not m["internal"]]
    assert shown.count("the mail instruction, not the updater") == 1
    await manager.close()


async def test_the_orchestrator_is_told_a_message_came_during_a_turn_and_what_the_turn_was_about(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A complaint about a member's missing instruction for the mail arrived while the orchestrator read
    the updater's report, and was answered about the updater."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        await r.orch.stop_queue(r.project.id)
        from protocore.contracts.types import Message, MessageRole

        await r.manager.sessions.append_transcript(sid, [Message(role=MessageRole.user, content_blocks=[TextBlock(text="[events · Bakery · 1 since 09:00]\n- 09:00 sol reported stuck on the updater")], metadata={"daedalus.origin": "events"})])
        _, live = await working_on(r, sid, "Webops")
        await r.team.ingress.report(live, "done", "mail split; your steps are in the report", call_id=f"fixture-report:{uuid.uuid4().hex}")
        said = await r.orch.steer_note(sid, "why don't you pass on the instruction?")
        assert "— the latest reports: Webops — done on \"Mail admin page\"" in said
        assert said.startswith("(The operator wrote this while you were in the middle of a turn that began with: «[events · Bakery · 1 since 09:00] / - 09:00 sol reported stuck on the updater»")
        assert said.endswith("why don't you pass on the instruction?")
        ordinary = await r.manager.create_session("not an orchestrator")
        assert await r.orch.steer_note(ordinary.session.id, "hello") == "hello"
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- replying to something in the chat ----------------------------------------------------------------------------------


async def test_a_message_names_what_it_replies_to(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "first"}, {"text": "second"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("replies")
    sid = state.session.id
    await manager.submit(sid, "webops reported the mail split")
    await _await_run(manager)
    seq = next(m["seq"] for m in await manager.transcript_page(sid) if m["role"] == "user")
    async with _client(settings, db, manager) as client:
        missing = await client.post(f"/api/sessions/{sid}/messages", headers=H, json={"text": "why not?", "reply_to": {"seq": 999999}})
        assert missing.status_code == 404
        sent = await client.post(f"/api/sessions/{sid}/messages", headers=H, json={"text": "pass on its steps", "reply_to": {"seq": seq, "excerpt": "webops reported the mail split"}})
        assert sent.status_code == 200
        await _await_run(manager)
    placed = [b.text for m in provider.requests[-1].messages for b in m.content_blocks if isinstance(b, TextBlock)]
    assert any(t.startswith("[In reply to: «webops reported the mail split»]\npass on its steps") for t in placed)
    [message] = [m for m in await manager.sessions.list_transcript(sid) if m.metadata.get("daedalus.reply_to")]
    assert message.metadata["daedalus.reply_to"] == {"seq": seq, "excerpt": "webops reported the mail split"}
    await manager.close()


# -- several asks, several commitments ------------------------------------------------------------------------------------


async def test_a_message_that_asks_several_things_is_kept_as_several_commitments(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A question about the plan and a clean-up asked together came back as the clean-up alone."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        await r.orch.stop_queue(r.project.id)
        from protocore.contracts.types import Message, MessageRole

        await r.manager.sessions.append_transcript(sid, [Message(role=MessageRole.user, content_blocks=[TextBlock(text="Clean up the drafts, and what do we publish next?")], metadata={"daedalus.origin": "operator"})])
        seq = await r.orch.operator_message_seq(sid)
        task_id, live = await working_on(r, sid, "Ira")
        said = await r.call(sid, "journal", kind="commitment", text="Clean up the drafts", task_id=task_id)
        assert "is open" in said
        await r.call(sid, "journal", kind="commitment", text="Answer what we publish next, from the plan")
        with pytest.raises(Refused, match="no task nope"):
            await r.call(sid, "journal", kind="commitment", text="x", task_id="nope")
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert "What you took on for the operator" in state and "Clean up the drafts — card" in state and "Answer what we publish next" in state
        focus = await r.orch.focus_state(await r.refreshed())
        assert [c["text"] for c in focus["commitments"]] == ["Clean up the drafts", "Answer what we publish next, from the plan"]
        assert [c["kind"] for c in focus["receipts"][str(seq)]] == ["commitment", "commitment"]

        _folder, cwd = await r.team.cwd_of(live)
        (Path(cwd) / "drafts.txt").write_text("The obsolete drafts were removed\n")
        await r.team.ingress.report(live, "done", "drafts cleaned", artifacts=["drafts.txt"],
                                    evidence=[{"item": "C1", "how": "listed", "result": "ok"}],
                                    call_id=f"fixture-report:{uuid.uuid4().hex}")
        result_id = await accept_branchless_result(r.team, task_id)
        accepted = (await r.orch.focus_state(await r.refreshed()))["accepted_results"]
        [shown] = accepted
        receipt = await r.manager.db.fetchone("SELECT contract_revision,attempt_id,original_digest FROM result_receipts WHERE id = ?", (result_id,))
        assert receipt is not None
        assert (shown["task_id"], shown["result_id"], shown["author"]) == (task_id, result_id, "Ira")
        assert (shown["contract_revision"], shown["attempt_id"], shown["original_digest"]) == (
            receipt["contract_revision"], receipt["attempt_id"], receipt["original_digest"])
        kept = await r.manager.db.fetchall("SELECT refs_json FROM project_journal"
                                           " WHERE project_id = ? AND kind = 'commitment_kept'", (r.project.id,))
        assert len(kept) == 1
        assert json.loads(kept[0]["refs_json"])["result_id"] == result_id
        [left] = (await r.orch.focus_state(await r.refreshed()))["commitments"]
        assert left["text"].startswith("Answer what we publish next")
        await r.call(sid, "journal", op="keep", commitment=left["id"], why="answered from the plan file")
        assert (await r.orch.focus_state(await r.refreshed()))["commitments"] == []
        with pytest.raises(Refused, match="closed already"):
            await r.call(sid, "journal", op="keep", commitment=left["id"])
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- what became of the operator's words ----------------------------------------------------------------------------------


async def test_a_requirement_from_the_operators_message_carries_the_message_and_its_fate(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        await r.orch.stop_queue(r.project.id)
        task_id, live = await working_on(r, sid, "Leo")
        from protocore.contracts.types import Message, MessageRole

        await r.manager.sessions.append_transcript(sid, [Message(role=MessageRole.user, content_blocks=[TextBlock(text="English only for now")], metadata={"daedalus.origin": "operator"})])
        seq = await r.orch.operator_message_seq(sid)
        run_id, stops = await bind_native_run(r.team, live, monkeypatch)
        staged = json.loads(await r.call(sid, "require", task_id=task_id, text="English only for now",
                                         kind="scope", source="operator"))
        assert staged["state"] == "pending_physical_exit"
        assert runtime.sent == []
        await finish_native_stop(r.team, live, run_id, staged["stop_effect_id"], stops)
        await r.call(sid, "require", op="apply", task_id=task_id, intent_id=staged["intent_id"])
        await r.call(sid, "assign", task_id=task_id, staff="Leo")
        from tests.unit.test_orchestrator_team import admitted

        await until_await(lambda: admitted(r, task_id), "the operator's revised scope launched")
        assert "English only for now" in runtime.started[-1].first_message
        [receipt] = (await r.orch.focus_state(await r.refreshed()))["receipts"][str(seq)]
        assert (receipt["kind"], receipt["label"], receipt["task_id"]) == ("requirement", "R1", task_id)
        assert receipt["deliveries"] == [{"staff_name": "Leo", "acknowledged": False, "opened": False,
                                          "via": "brief", "cli": False}]
        member = await r.manager.staff.find(r.project.id, "Leo")
        assert member is not None
        fresh = await r.team.live_of(member)
        assert fresh is not None and fresh.id != live.id
        await r.team.ingress.report(fresh, "checkpoint", "noted", acknowledged=["R1"],
                                    call_id=f"fixture-report:{uuid.uuid4().hex}")
        [receipt] = (await r.orch.focus_state(await r.refreshed()))["receipts"][str(seq)]
        assert receipt["deliveries"][0]["acknowledged"] is True
        focus = await r.orch.focus_state(await r.refreshed())
        assert focus["counts"]["in_work"] == 1 and focus["goals"][0]["owner"]["name"] == "Leo"
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_the_focus_state_lists_the_results_waiting_for_a_decision(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        await r.orch.stop_queue(r.project.id)
        task_id, live = await working_on(r, sid, "Sol")
        await r.team.ingress.report(live, "stuck", "the supervisor races the swap", call_id=f"fixture-report:{uuid.uuid4().hex}")
        with pytest.raises(Refused, match="stop or reconcile"):
            await r.call(sid, "tasks", op="move", task_id=task_id, status="blocked", note="x")
        await stop_native_task(r.team, task_id, live, monkeypatch)
        await r.call(sid, "tasks", op="move", task_id=task_id, status="blocked", note="x")
        focus = await r.orch.focus_state(await r.refreshed())
        [result] = focus["open_results"]
        assert (result["cause"], result["task_id"], result["staff_name"]) == ("report_stuck", task_id, "Sol")
        assert focus["counts"]["decisions"] == 1 and focus["goals"][0]["next"] == "waiting for the orchestrator's decision"
        await r.call(sid, "tasks", op="move", task_id=task_id, status="blocked", waiting_on="the operator's choice of supervisor")
        focus = await r.orch.focus_state(await r.refreshed())
        assert focus["open_results"] == [] and focus["goals"][0]["next"] == "the operator's choice of supervisor"
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- the copy that opens a turn -------------------------------------------------------------------------------------------


async def test_a_queued_message_is_read_once_where_the_model_read_it(settings: Settings, db: Database) -> None:
    """Sixteen of the operator's messages looked delivered twice: the words written into the chat when
    they were queued, and again where the model read them. The model got them once; the history tools
    showed them twice, the first time out of order."""
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 1"}}, {"text": "done"}, {"text": "then this"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("drained")
    sid = state.session.id
    await manager.submit(sid, "start")
    await asyncio.sleep(0.2)
    await manager.submit(sid, "a zebra crossing note", follow_up=True)
    await _await_run(manager)
    for _ in range(50):
        if len(provider.requests) >= 3:
            break
        await asyncio.sleep(0.1)
    rows = await manager.sessions.list_transcript(sid)
    assert sum(1 for m in rows if "a zebra crossing note" in "".join(b.text for b in m.content_blocks if isinstance(b, TextBlock))) == 2
    from protocore.contracts.tools import ToolContext

    from daedalus.host.services import SessionServices, locator
    from daedalus.tools.history import history_expand, history_search

    locator.register(SessionServices(session_id=sid, workspace_dir=state.workspace, extra={"manager": manager}))
    try:
        context = ToolContext(tenant_id="t", run_id="r", session_id=sid, metadata={"tool_call_id": "c"})
        found = await history_search().invoke(context, {"query": "zebra"})
        assert found.content.count("crossing note") == 1
        first = min(await manager.sessions.transcript_seqs(sid, [manager.sessions.transcript_key(m) for m in rows]))
        expanded = await history_expand().invoke(context, {"from_seq": first, "to_seq": first + 30})
        assert expanded.content.count("a zebra crossing note") == 1
    finally:
        locator.unregister(sid)
    await manager.close()


async def test_a_message_sent_in_the_last_seconds_of_a_turn_stays_in_the_chat(settings: Settings, db: Database) -> None:
    """A voice message the operator sent while the orchestrator was writing its last reply was never
    placed in that turn; it opened the next one. The queued row and the row that opened the turn were
    both hidden, and the message vanished from the chat."""
    from protocore.contracts.types import Message, MessageRole

    manager = await _manager(settings, db, ScriptedProvider([]))
    state = await manager.create_session("vanished")
    sid = state.session.id
    words = "the mail instruction, not the updater"
    await manager.sessions.append_transcript(sid, [
        Message(role=MessageRole.user, content_blocks=[TextBlock(text=words)], metadata={"daedalus.origin": "operator", "daedalus.delivery": "steer", "daedalus.queued": True}),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="the reply written meanwhile")], metadata={}),
        Message(role=MessageRole.user, content_blocks=[TextBlock(text=words)], metadata={"daedalus.origin": "operator", "daedalus.delivery": "drained"}),
    ])
    shown = [m for m in await manager.transcript_page(sid) if not m["internal"]]
    assert [(m["role"], m["text"], m.get("delivery")) for m in shown] == [("assistant", "the reply written meanwhile", None), ("user", words, "drained")]
    await manager.close()


async def test_what_the_operator_allowed_for_a_card_is_the_basis_of_a_grant_within_it(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The operator allowed one test message for a mail check; the orchestrator could only ask them to
    write it into the brief's allowances, which are for what holds everywhere."""
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        await r.orch.stop_queue(r.project.id)
        await r.manager.staff.hire(r.project.id, name="Luna", role="Mail", isolation="shared")
        task_id = await operator_task(db, r.project.id, "Mail relay", brief=SCRIPT)
        await apply_requirement(r, sid, task_id, text="Check the relay format with a local dry run",
                                kind="scope", source="orchestrator")
        await apply_requirement(r, sid, task_id,
                                text="One test message to the operator's own address is allowed",
                                kind="scope", source="operator")
        await r.call(sid, "assign", staff="Luna", task_id=task_id)
        luna = await r.manager.staff.find(r.project.id, "Luna")
        assert luna is not None
        live = await r.team.live_of(luna)
        assert live is not None
        ask = await r.manager.asks.get(await r.team.ingress.permission(live, "p-1", "Exec", "send one test message"))
        assert ask is not None
        with pytest.raises(Refused, match="or the R… of the operator's own scope"):
            await r.call(sid, "answer", request_id=ask.short_id, allow=True, basis="R1")
        assert await r.call(sid, "answer", request_id=ask.short_id, allow=True, basis="R2") == f"request {ask.short_id} granted"
        assert runtime.answered[-1][2].allow is True
    finally:
        await close_team(r.manager)
        await r.manager.close()
