"""A helper the daily cap stops, or whose leader the cap stops, is reported, kept and carried on.

A leader on a paid model ran three helpers on a subscription. The day's cap closed while they worked;
the first one finished an hour later, its report was refused because the leader could not start a
run, the refusal went to the log, and the helper was deleted: the leader never heard of it and the
operator saw it vanish. These tests hold the host to the other ending.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolUseBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import budget_watch
from daedalus.extensions.subagents import CONTINUE_NOTE, HELD_KEY, NOTICE_KEY, PARKED_KEY, Subagents
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

DAILY = "💸 a spending cap stopped this run before its next model call — inference budget: daily:2026-11-09T00:00:00+00:00: this call needs a $0.300000 worst-case reservation; $0.000000 is the available balance under the daily spending cap"


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={})
    yield app
    await manager.close()


class Gate:
    """``submit`` as the host answers it while the daily cap is closed: a paid leader is refused."""

    def __init__(self, manager: SessionManager, *, paid: set[str]) -> None:
        self.manager = manager
        self.paid = paid
        self.sent: list[dict[str, Any]] = []
        self.seen_keys: set[str] = set()
        manager.submit = self.submit  # type: ignore[method-assign]

    def close(self) -> None:
        self.manager.budget_flag.parent.mkdir(parents=True, exist_ok=True)
        self.manager.budget_flag.write_text("20.8069 > 20.00 at 2026-11-09T20:01:02+00:00\n")

    def open(self) -> None:
        self.manager.budget_flag.unlink(missing_ok=True)

    async def submit(self, session_id: str, text: str, attachments: Any = (), **kwargs: Any) -> str:
        if session_id in self.paid and self.manager.budget_exceeded():
            raise RuntimeError(f"daily budget exceeded ({self.manager.budget_exceeded()}); the daily cap reopens at 00:00 UTC")
        key = kwargs.get("client_message_id")
        if key and key in self.seen_keys:
            return "run-again"  # the input receipt: the same message twice is the first one
        if key:
            self.seen_keys.add(key)
        self.sent.append({"session_id": session_id, "text": text, **kwargs})
        return "run-x"

    def to(self, session_id: str) -> list[str]:
        return [item["text"] for item in self.sent if item["session_id"] == session_id]


async def _say(manager: SessionManager, session_id: str, text: str, *, wrote: str = "") -> None:
    blocks: list[Any] = [TextBlock(text=text)]
    if wrote:
        blocks.append(ToolUseBlock(tool_call_id=f"call-{wrote}", name="Write", arguments_json=json.dumps({"path": wrote, "content": "x"})))
    await manager.sessions.append_transcript(session_id, [Message(role=MessageRole.assistant, content_blocks=blocks)])


async def _notices(manager: SessionManager, session_id: str) -> list[dict[str, Any]]:
    rows = await manager.sessions.list_transcript(session_id)
    return [m.metadata[NOTICE_KEY] for m in rows if NOTICE_KEY in m.metadata]


async def test_a_report_the_leader_cannot_take_is_held_and_delivered_once_when_the_cap_reopens(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("Site Design")
    gate = Gate(manager, paid={leader.session.id})
    child = await subs.spawn(leader_id=leader.session.id, task="build candidate a", name="cand-a")
    cid = child["session_id"]
    gate.close()
    # The helper runs on a subscription and finishes while the leader's paid model is shut out.
    await _say(manager, cid, "candidate a is ready: redesign/a/README.md")
    await subs.on_run_finished(cid, "run-1", "completed")
    assert not gate.to(leader.session.id)
    state = await manager.get_state(cid)
    assert state is not None, "the helper was deleted with its report undelivered"
    assert [entry["key"] for entry in state.metadata[HELD_KEY]] == [f"subagent-report:{cid}:run-1"]
    # The operator sees it in the leader's chat, with the way out, not only in a toast.
    notices = await _notices(manager, leader.session.id)
    assert notices and notices[-1]["state"] == "held" and notices[-1]["name"] == "cand-a" and notices[-1]["budget"] is True
    note = [m for m in await manager.sessions.list_transcript(leader.session.id) if NOTICE_KEY in m.metadata][-1]
    assert "00:00 UTC" in note.content_blocks[0].text  # type: ignore[union-attr]
    # The panel keeps drawing it, with a report waiting.
    view = next(v for v in await subs.views(leader.session.id) if v["session_id"] == cid)
    assert view["reports_held"] == 1
    # Still closed: looking again delivers nothing and loses nothing.
    await subs.budget_tick()
    assert not gate.to(leader.session.id) and (await manager.get_state(cid)) is not None
    # The cap reopens: the report goes, once, and the one-off helper is removed after it.
    gate.open()
    await subs.budget_tick()
    await subs.budget_tick()
    reports = gate.to(leader.session.id)
    assert len(reports) == 1 and "candidate a is ready" in reports[0] and "finished: completed" in reports[0]
    assert gate.sent[-1]["origin"] == "subagent:cand-a" and gate.sent[-1]["client_message_id"] == f"subagent-report:{cid}:run-1"
    await asyncio.gather(*subs._removals)
    assert await manager.get_state(cid) is None


async def test_a_helper_the_daily_cap_stops_reports_to_its_leader_is_parked_and_goes_on_once(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    gate = Gate(manager, paid=set())
    child = await subs.spawn(leader_id=leader.session.id, task="build candidate b", name="cand-b")
    cid = child["session_id"]
    await _say(manager, cid, "main page drafted, moving to protocore", wrote="redesign/b/main/index.html")
    gate.close()
    state = await manager.get_state(cid)
    assert state is not None
    state.last_error_kind = "spending_cap"
    state.last_error_message = DAILY
    await subs.on_run_finished(cid, "run-1", "failed")
    # The leader hears what happened, what the helper had done, and that it goes on by itself.
    report = gate.to(leader.session.id)[-1]
    assert "paused by the daily spending cap" in report and "parked, not removed" in report
    assert "files it wrote: redesign/b/main/index.html" in report and "main page drafted" in report
    assert "00:00 UTC" in report
    state = await manager.get_state(cid)
    assert state is not None and state.metadata[PARKED_KEY]["reason"] == "budget"
    assert not subs._removing
    notices = await _notices(manager, leader.session.id)
    assert notices[-1]["state"] == "parked" and notices[-1]["session_id"] == cid
    view = next(v for v in await subs.views(leader.session.id) if v["session_id"] == cid)
    assert view["status"] == "parked" and view["parked"]["reason"] == "budget"
    # Closed: nothing starts it.
    await subs.budget_tick()
    assert not gate.to(cid)[1:]
    # Open: exactly one run continues it, however often the watch looks.
    gate.open()
    await subs.budget_tick()
    await subs.budget_tick()
    continued = [t for t in gate.to(cid) if t == CONTINUE_NOTE]
    assert len(continued) == 1
    assert PARKED_KEY not in (await manager.get_state(cid)).metadata  # type: ignore[union-attr]
    # Its real result reaches the leader once.
    state = await manager.get_state(cid)
    state.last_error_kind = ""  # type: ignore[union-attr]
    state.last_error_message = ""  # type: ignore[union-attr]
    await _say(manager, cid, "candidate b is ready: redesign/b/README.md")
    await subs.on_run_finished(cid, "run-2", "completed")
    await subs.on_run_finished(cid, "run-2", "completed")
    finals = [t for t in gate.to(leader.session.id) if "candidate b is ready" in t]
    assert len(finals) == 1 and "finished: completed" in finals[0]


async def test_a_helper_found_parked_at_start_is_reported_and_left_to_the_resumed_run(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    gate = Gate(manager, paid={leader.session.id})
    child = await subs.spawn(leader_id=leader.session.id, task="build candidate c", name="cand-c")
    cid = child["session_id"]
    gate.close()
    # Boot recovery kept the helper's unfinished run for later: the cap was closed.
    manager.budget_parked["run-s"] = cid
    await subs.budget_tick()
    state = await manager.get_state(cid)
    assert state is not None and state.metadata[PARKED_KEY]["mode"] == "snapshot"
    assert state.metadata[HELD_KEY][0]["key"] == f"subagent-parked:{cid}:run-s"
    assert (await _notices(manager, leader.session.id))[-1]["state"] == "parked"
    # The cap reopens. The watch resumes the saved run itself, so no second run is started on top,
    # and the held word to the leader goes once.
    gate.open()
    resumed: list[str] = []

    async def fake_resume() -> list[str]:
        resumed.extend(manager.budget_parked)
        manager.budget_parked.clear()
        return list(resumed)

    manager.resume_budget_parked = fake_resume  # type: ignore[method-assign]
    app.extensions["subagents"] = subs
    state.task = asyncio.get_running_loop().create_future()  # the resumed run is under way
    try:
        await budget_watch.tick(app)
        await budget_watch.tick(app)
    finally:
        state.task.cancel()
        state.task = None
    assert resumed == ["run-s"]
    assert not [t for t in gate.to(cid) if t == CONTINUE_NOTE]
    assert len([t for t in gate.to(leader.session.id) if "paused by the daily spending cap" in t]) == 1
    assert PARKED_KEY not in (await manager.get_state(cid)).metadata  # type: ignore[union-attr]


@pytest.mark.parametrize(("status", "kind", "words"), [
    ("cancelled", "", "was stopped before it finished"),
    ("failed", "", "failed before its report"),
    ("cancelled", "run_cap", "stopped by a cap"),
])
async def test_every_other_ending_without_a_report_is_reported_and_keeps_the_helper(app: Any, status: str, kind: str, words: str) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    gate = Gate(manager, paid=set())
    child = await subs.spawn(leader_id=leader.session.id, task="t", name="h")
    cid = child["session_id"]
    await _say(manager, cid, "half way")
    state = await manager.get_state(cid)
    state.last_error_kind = kind  # type: ignore[union-attr]
    state.last_error_message = "💸 per-run cap reached: $5.03 spent of $5.00" if kind else ""  # type: ignore[union-attr]
    await subs.on_run_finished(cid, "run-1", status)
    report = gate.to(leader.session.id)[-1]
    assert words in report and "half way" in report and "SubAgentDelete('h')" in report
    assert await manager.get_state(cid) is not None and not subs._removing
    assert PARKED_KEY not in (await manager.get_state(cid)).metadata  # type: ignore[union-attr]


async def test_boot_recovery_remembers_the_runs_the_cap_parked_and_the_watch_resumes_them(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    manager: SessionManager = app.manager
    leader = await manager.create_session("lead")
    gate = Gate(manager, paid=set())
    gate.close()

    async def snapshots() -> list[dict[str, Any]]:
        return [{"run_id": "run-p", "session_id": leader.session.id, "snapshot": {}}]

    monkeypatch.setattr(manager.events, "unfinished_snapshots", snapshots)
    monkeypatch.setattr(manager, "provider_costs_nothing", lambda *_: False)
    assert await manager.resume_unfinished() == []
    assert manager.budget_parked == {"run-p": leader.session.id}
    asked: list[set[str] | None] = []

    async def resume(allowed: set[str] | None = None) -> list[str]:
        asked.append(allowed)
        return sorted(allowed or ())

    monkeypatch.setattr(manager, "resume_unfinished", resume)
    # Still closed: the watch leaves them parked.
    await budget_watch.tick(app)
    assert asked == [] and manager.budget_parked
    gate.open()
    await budget_watch.tick(app)
    await budget_watch.tick(app)
    assert asked == [{"run-p"}] and manager.budget_parked == {}


async def test_a_note_written_between_runs_tells_the_app_to_read_the_chat_again(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    manager: SessionManager = app.manager
    leader = await manager.create_session("lead")
    heard: list[str] = []

    async def sinks(session_id: str, event: Any) -> None:
        heard.append(str(event.type))

    monkeypatch.setattr(manager, "_notify_sinks", sinks)
    note = Message(role=MessageRole.user, content_blocks=[TextBlock(text="parked")], metadata={"daedalus.origin": "subagent:h", NOTICE_KEY: {"state": "parked"}})
    await manager.note_in_chat(leader.session.id, note)
    assert heard == ["transcript_note"]
    rows = await manager.sessions.list_transcript(leader.session.id)
    assert rows[-1].metadata[NOTICE_KEY] == {"state": "parked"}
    # The model never reads it: the line is the transcript's, not the working history's.
    history = await manager.sessions.list_messages(leader.session.id, "daedalus", limit=100)
    assert all(NOTICE_KEY not in m.metadata for m in history)


async def test_the_panel_says_whether_a_report_reached_the_leader_and_marks_it_once_a_held_one_goes(app: Any) -> None:
    """A kept helper stays after its report, so its card is the one place the operator reads whether the leader has it."""
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    gate = Gate(manager, paid={leader.session.id})
    cid = (await subs.spawn(leader_id=leader.session.id, task="build candidate c", name="cand-c", keep=True))["session_id"]
    gate.close()
    await _say(manager, cid, "candidate c is ready: redesign/c/README.md")
    await subs.on_run_finished(cid, "run-1", "completed")
    view = next(v for v in await subs.views(leader.session.id) if v["session_id"] == cid)
    assert view["report"]["cause"] == "completed" and view["report"]["delivered"] is False and view["report"]["delivered_at"] is None
    assert view["report"]["summary"] == "candidate c is ready: redesign/c/README.md"
    gate.open()
    await subs.budget_tick()
    view = next(v for v in await subs.views(leader.session.id) if v["session_id"] == cid)
    assert view["reports_held"] == 0 and view["report"]["delivered"] is True and view["report"]["delivered_at"]
    # A failed run's card carries what stopped it, delivered at once to a leader that can take it.
    await subs.on_run_finished(cid, "run-2", "failed")
    view = next(v for v in await subs.views(leader.session.id) if v["session_id"] == cid)
    assert view["report"]["cause"] == "failed" and view["report"]["delivered"] is True
