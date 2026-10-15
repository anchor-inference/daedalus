"""Autopilot: a judge at the end of a chat's run, its four verdicts, and the rules that keep it from
acting where it must not — once per run, never after a stop, never past the operator, never twice."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import Settings
from daedalus.extensions import autopilot, questions, side_questions
from daedalus.extensions.notifications import ActionConflict
from daedalus.host import autopilot as judge
from daedalus.host.session_runner import SessionManager
from daedalus.providers.wire import messages_to_wire
from daedalus.stores.database import Database
from tests.support.waiting import until_await
from tests.unit.test_session_runner import ScriptedProvider, _manager

# -- the judge's answer ----------------------------------------------------------------------------


def test_each_verdict_is_read_and_anything_else_is_none() -> None:
    assert judge.parse('{"verdict": "continue", "next_goal": "run the tests"}') == judge.Verdict("continue", next_goal="run the tests")
    assert judge.parse('```json\n{"verdict": "done", "summary": "all  green"}\n```') == judge.Verdict("done", summary="all green")
    assert judge.parse('Sure: {"verdict": "ask", "question": "Which branch?"} ok').question == "Which branch?"  # type: ignore[union-attr]
    choice = judge.parse(json.dumps({"verdict": "choice", "reason": "two ways", "options": [
        {"label": "Fix", "next_goal": "fix it"}, {"label": "fix", "next_goal": "dup"}, {"label": "Revert", "next_goal": "revert it"}]}))
    assert choice is not None and [o.label for o in choice.options] == ["Fix", "Revert"]
    for bad in ("", "no json", '{"verdict": "continue"}', '{"verdict": "ask", "question": ""}', '{"verdict": "explode"}', "[1, 2]",
                json.dumps({"verdict": "choice", "options": [{"label": "only", "next_goal": "one"}]}),
                json.dumps({"verdict": "choice", "options": [{"label": str(i), "next_goal": "g"} for i in range(5)]})):
        assert judge.parse(bad) is None, bad


def test_the_request_names_the_turns_used_and_carries_the_style_apart() -> None:
    text = judge.request(["Operator: go", "Agent: done step one"], style="STYLE BLOCK", used=1, allowed=3)
    assert text.startswith("STYLE BLOCK") and "1 of 3" in text and text.endswith("Agent: done step one")
    assert "STYLE" not in judge.INSTRUCTIONS.split("Rules:")[0]


# -- a session with autopilot on -------------------------------------------------------------------


class Notes:
    """The notifications service as autopilot uses it: what was posted and what was resolved."""

    def __init__(self) -> None:
        self.posted: list[Any] = []
        self.resolved: list[str] = []
        self.resolvers: dict[str, Any] = {}

    def language(self) -> str:
        return "en"

    async def post(self, draft: Any, *, force: bool = False) -> None:
        self.posted.append(draft)

    async def resolve(self, ref: str, resolution: str, *, via: str) -> int:
        self.resolved.append(ref)
        return 1

    def register_resolver(self, prefix: str, resolver: Any) -> None:
        self.resolvers[prefix] = resolver


@pytest.fixture
async def pilot(settings: Settings, db: Database) -> Any:
    provider = ScriptedProvider([{"text": f"answer {i}"} for i in range(12)])
    manager = await _manager(settings, db, provider)
    manager.config.autopilot.enabled = True
    verdicts: list[str] = []
    asked: list[dict[str, Any]] = []

    async def small_model(prompt: str, preset: str, *, purpose: str, session_id: str | None = None, max_tokens: int = 200,
                          temperature: float = 0.0, instructions: str = "") -> str:
        asked.append({"prompt": prompt, "purpose": purpose, "instructions": instructions})
        return verdicts.pop(0) if verdicts else '{"verdict": "done", "summary": "finished"}'

    manager.small_model = small_model  # type: ignore[method-assign]
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, notifications=Notes())
    await side_questions.install(app)  # type: ignore[arg-type]
    await autopilot.install(app)  # type: ignore[arg-type]
    service: autopilot.Autopilot = app.extensions["autopilot"]
    yield SimpleNamespace(app=app, manager=manager, provider=provider, service=service, verdicts=verdicts, asked=asked)
    await service.close()
    await manager.close()


async def quiet(p: Any) -> None:
    """No run is going and no judge is thinking: what autopilot does after this message is done."""
    async def idle() -> bool:
        return not p.service._tasks and not any(s.running or (s.housekeeping is not None and not s.housekeeping.done()) for s in p.manager._states.values())

    await until_await(idle, "the session and its autopilot settled")


async def user_rows(manager: SessionManager, sid: str) -> list[Message]:
    return [m for m in await manager.sessions.list_transcript(sid, limit=1000) if m.role is MessageRole.user]


async def chat(p: Any, *, on: bool = True) -> str:
    state = await p.manager.create_session("chat")
    if on:
        await p.service.set_switch(state.session.id, True)
    return state.session.id


async def test_continue_appends_a_marked_turn_and_stops_at_the_limit(pilot: Any) -> None:
    pilot.manager.config.autopilot.max_continuations = 1
    pilot.verdicts += ['{"verdict": "continue", "next_goal": "now run the tests"}', '{"verdict": "continue", "next_goal": "and lint"}']
    sid = await chat(pilot)
    await pilot.manager.submit(sid, "fix the bug")
    await quiet(pilot)
    rows = await user_rows(pilot.manager, sid)
    marked = [m for m in rows if m.metadata.get("daedalus.autopilot")]
    assert len(marked) == 1 and marked[0].metadata["daedalus.origin"] == "autopilot"
    assert marked[0].metadata["daedalus.autopilot"]["kind"] == "continue"
    assert "now run the tests" in marked[0].content_blocks[0].text  # type: ignore[union-attr]
    # The second "continue" came after the allowance was used: no silent turn, a one-tap question instead.
    waiting = await pilot.manager.asks.of_session(sid)
    assert len(waiting) == 1 and waiting[0].detail["options"] == ["Continue"]
    # The goal is quoted in the question, so the operator knows what Continue would send.
    assert "and lint" in waiting[0].text
    assert (await pilot.service.status(sid))["used"] == 1
    # Every judge call is booked under its own label, the instructions apart from the moving request.
    assert {a["purpose"] for a in pilot.asked} == {judge.PURPOSE} and all(a["instructions"] == judge.INSTRUCTIONS for a in pilot.asked)
    assert "Autopilot: now run the tests" in pilot.asked[-1]["prompt"]


async def test_the_continuation_keeps_the_sessions_prefix(pilot: Any) -> None:
    pilot.verdicts.append('{"verdict": "continue", "next_goal": "go on"}')
    sid = await chat(pilot)
    await pilot.manager.submit(sid, "start")
    await quiet(pilot)
    first, second = [await messages_to_wire(r.messages) for r in pilot.provider.requests[:2]]
    # What goes over the wire for the second run begins with every message the first one sent, byte for
    # byte: the judge touched nothing, and the continuation only added at the end.
    assert json.dumps(second[: len(first)], sort_keys=True) == json.dumps(first, sort_keys=True)
    assert len(second) > len(first) and "go on" in second[-1]["content"]


async def test_a_tap_admits_one_turn_however_often_it_arrives(pilot: Any) -> None:
    pilot.verdicts.append(json.dumps({"verdict": "choice", "reason": "pick", "options": [
        {"label": "Tests", "next_goal": "run the tests"}, {"label": "Docs", "next_goal": "write the docs"}]}))
    sid = await chat(pilot)
    await pilot.manager.submit(sid, "done with the feature")
    await quiet(pilot)
    row = await pilot.manager.db.fetchone("SELECT id FROM autopilot_decisions WHERE verdict = 'choice'")
    decision = str(row["id"])
    [waiting] = await questions.waiting(pilot.app, session_id=sid)
    assert waiting["options"] == ["Tests", "Docs"] and "write the docs" not in json.dumps(waiting)  # the goals stay on the server
    assert pilot.app.notifications.posted[-1].request_ref == f"autopilot:{sid}:{decision}"
    results = await asyncio.gather(pilot.service.choose(decision, 1, via="telegram"), pilot.service.choose(decision, 0, via="notification"), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["ActionConflict", "str"]
    with pytest.raises(ActionConflict):
        await pilot.service.choose(decision, 1, via="telegram")
    await quiet(pilot)
    chosen = [m for m in await user_rows(pilot.manager, sid) if (m.metadata.get("daedalus.autopilot") or {}).get("kind") == "choice"]
    assert len(chosen) == 1
    # A tap is the operator's decision: the turns counted from it start again.
    assert (await pilot.service.status(sid))["used"] == 0
    # A replay of the admission itself finds the key taken and sends nothing.
    assert await pilot.service._admit(decision, sid, "again", kind="choice", via="x") == "taken"
    assert len([m for m in await user_rows(pilot.manager, sid) if m.metadata.get("daedalus.autopilot")]) == 1


async def test_an_ask_answered_in_the_panel_goes_in_as_the_operators_words(pilot: Any) -> None:
    pilot.verdicts.append('{"verdict": "ask", "question": "Which database?"}')
    sid = await chat(pilot)
    await pilot.manager.submit(sid, "set up storage")
    await quiet(pilot)
    [ask] = await questions.waiting(pilot.app, session_id=sid)
    assert ask["asker"] == "autopilot" and not ask["files"]
    done = await questions.answer(pilot.app, [{"ask_id": ask["id"], "text": "SQLite"}], project_id=None, via="app", session_id=sid)
    assert done["results"][0]["delivered"] is True
    await quiet(pilot)
    answer = [m for m in await user_rows(pilot.manager, sid) if (m.metadata.get("daedalus.autopilot") or {}).get("kind") == "answer"]
    assert len(answer) == 1 and answer[0].metadata["daedalus.origin"] == "operator"
    assert answer[0].metadata["daedalus.autopilot"]["question"] == "Which database?"


async def test_done_is_a_notification_under_the_runs_own_key(pilot: Any) -> None:
    sid = await chat(pilot)
    await pilot.manager.submit(sid, "small thing")
    await quiet(pilot)
    [note] = [d for d in pilot.app.notifications.posted if d.kind == "autopilot"]
    assert note.category == "run_finished" and note.body == "finished" and note.dedupe_key == f"run:{sid}"


async def test_it_never_acts_where_it_must_not(pilot: Any) -> None:
    manager, service = pilot.manager, pilot.service
    # Off for the session, off for the installation, a role of its own: nothing is judged.
    plain = await chat(pilot, on=False)
    await manager.submit(plain, "hi")
    await quiet(pilot)
    sub = await manager.create_session("sub", metadata={"subagent_of": plain})
    with pytest.raises(autopilot.Refused):
        await service.set_switch(sub.session.id, True)
    assert pilot.asked == []
    sid = await chat(pilot)
    manager.config.autopilot.enabled = False
    await manager.submit(sid, "hi")
    await quiet(pilot)
    assert pilot.asked == []
    manager.config.autopilot.enabled = True
    # A stopped or failed run is not judged.
    state = await manager.get_state(sid)
    await service.on_run_finished(sid, "r-cancelled", "cancelled")
    await service.on_run_finished(sid, "r-failed", "failed")
    assert not service._tasks
    # A question still waiting holds it back.
    await manager.asks.open(None, origin="session", kind="question", text="q?", routed_to="operator", detail={}, session_id=sid)
    assert await service.judge(sid, "r-asked") == "skipped:question"
    assert state is not None and pilot.asked == []


async def test_a_message_written_while_it_judged_wins(pilot: Any) -> None:
    sid = await chat(pilot)
    await pilot.manager.submit(sid, "first")
    await quiet(pilot)

    async def small_model(prompt: str, preset: str, **_: Any) -> str:
        await pilot.manager.sessions.append_transcript(sid, [Message(role=MessageRole.user, content_blocks=[TextBlock(text="wait")], metadata={"daedalus.origin": "operator"})])
        return '{"verdict": "continue", "next_goal": "go"}'

    pilot.manager.small_model = small_model
    assert await pilot.service.judge(sid, "r-late") == "skipped:newer"
    assert await pilot.service.judge(sid, "r-late") == "skipped:judged"
    assert not [m for m in await user_rows(pilot.manager, sid) if m.metadata.get("daedalus.autopilot")]


async def test_a_restart_closes_what_it_caught_and_replays_nothing(pilot: Any) -> None:
    manager, service = pilot.manager, pilot.service
    sid = await chat(pilot)
    now = "2026-01-01T00:00:00+00:00"
    for decision, state, key in (("d-judging", "judging", None), ("d-unknown", "admitting", "autopilot:d-unknown"), ("d-placed", "admitting", "autopilot:d-placed")):
        await manager.db.execute(
            "INSERT INTO autopilot_decisions(id, session_id, run_id, state, admission_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (decision, sid, f"run-{decision}", state, key, now, now),
        )
    await manager.live.accept(sid, "autopilot:d-placed", "input", {"text": "x"})
    await manager.live.consume(sid, "autopilot:d-placed", run_id="r", step_id="start", message_seq=None)
    await service.recover()
    rows = {r["id"]: r["state"] for r in await manager.db.fetchall("SELECT id, state FROM autopilot_decisions")}
    assert rows == {"d-judging": "abandoned", "d-unknown": "abandoned", "d-placed": "admitted"}
    assert await user_rows(manager, sid) == []
