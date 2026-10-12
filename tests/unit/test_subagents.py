"""Subagents: shared workspace, model choice, async report to the leader."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.subagents import NO_ANSWER, Subagents
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={})
    yield app
    await manager.close()


def _capture(manager: SessionManager) -> list[tuple[str, str, str]]:
    submitted: list[tuple[str, str, str]] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer=False, as_answer=True, origin="operator") -> str:  # type: ignore[no-untyped-def]
        submitted.append((session_id, text, origin))
        return "run-x"

    manager.submit = fake_submit  # type: ignore[method-assign]
    return submitted


async def test_subagent_shares_workspace_and_reports_after_the_leader_turn(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    submitted = _capture(manager)
    result = await subs.spawn(leader_id=leader.session.id, task="count the files", name="counter")
    child = await manager.get_state(result["session_id"])
    assert child is not None and child.workspace == leader.workspace
    assert child.metadata["subagent_of"] == leader.session.id and child.metadata["unattended"] is True
    assert submitted[0][0] == child.session.id and "count the files" in submitted[0][1] and submitted[0][2] == f"subagent-task:{leader.session.id}"
    # The child's workspace survives its deletion: it is the leader's directory.
    (leader.workspace / "made-by-child.txt").write_text("x")
    # The report goes to the leader once the child's run ends, whether or not the leader is running.
    await manager.sessions.append_transcript(child.session.id, [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="there are 3 files\n\n⟦ h | status: completed; next: none | anchors: files ⟧")])])
    await subs.on_run_finished(child.session.id, "run-x", "completed")
    assert submitted[-1][0] == leader.session.id and "there are 3 files" in submitted[-1][1] and submitted[-1][2] == "subagent:counter"
    assert "⟦" not in submitted[-1][1]
    assert await manager.delete_session(child.session.id)
    assert (leader.workspace / "made-by-child.txt").exists()
    # A leader that finished gets a plain restart of its turn: the same submit path handles it.
    await subs.on_run_finished(leader.session.id, "run-y", "completed")  # not a subagent: nothing happens
    assert len(submitted) == 2


async def test_subagent_failed_run_reports_no_answer(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    submitted = _capture(manager)
    result = await subs.spawn(leader_id=leader.session.id, task="explode")
    await subs.on_run_finished(result["session_id"], "run-x", "failed")
    assert submitted[-1][0] == leader.session.id and NO_ANSWER in submitted[-1][1] and "failed" in submitted[-1][1]


async def test_subagent_model_choice_depth_and_limits(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    _capture(manager)
    with pytest.raises(ValueError, match="unknown model"):
        await subs.spawn(leader_id=leader.session.id, task="t", model="nope.model")
    models = await subs.models()
    if models:
        chosen = models[-1]
        result = await subs.spawn(leader_id=leader.session.id, task="t", model=chosen)
        assert (await manager.live.load(result["session_id"])).get("preset") == chosen
    # Without a model the child inherits the leader's override.
    if models:
        await manager.live.set_model(leader.session.id, preset=models[0])
        result = await subs.spawn(leader_id=leader.session.id, task="t2")
        assert (await manager.live.load(result["session_id"])).get("preset") == models[0]
    app.config.subagents.max_depth = 1
    child = await manager.get_state(result["session_id"]) if models else await manager.create_session("c", metadata={"subagent_depth": 1})
    assert child is not None
    with pytest.raises(ValueError, match="too deep"):
        await subs.spawn(leader_id=child.session.id, task="deeper")
    with pytest.raises(ValueError, match="needs a name"):
        await subs.spawn(leader_id=leader.session.id, task="   ")


async def test_subagent_is_removed_after_reporting_unless_kept(app: Any) -> None:

    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    submitted = _capture(manager)
    gone = await subs.spawn(leader_id=leader.session.id, task="one-off", name="tmp")
    kept = await subs.spawn(leader_id=leader.session.id, task="stay", name="helper", keep=True)
    (leader.workspace / "shared.txt").write_text("keep me")
    for sid in (gone["session_id"], kept["session_id"]):
        await manager.sessions.append_transcript(sid, [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="done")])])
        await subs.on_run_finished(sid, "r", "completed")
    await asyncio.gather(*subs._removals)
    assert await manager.get_state(gone["session_id"]) is None
    assert await manager.get_state(kept["session_id"]) is not None
    assert (leader.workspace / "shared.txt").read_text() == "keep me"
    reports = [t for _, t, o in submitted if o.startswith("subagent:")]
    assert any("has been removed" in t for t in reports) and any("SubAgentSend('helper'" in t for t in reports)


async def test_subagent_without_a_task_stands_by_until_it_is_sent_work(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    submitted = _capture(manager)
    with pytest.raises(ValueError, match="needs a name"):
        await subs.spawn(leader_id=leader.session.id, task="  ")
    result = await subs.spawn(leader_id=leader.session.id, name="analyst")
    assert result["idle"] is True and result["run_id"] is None and result["kept"] is True
    assert not submitted  # nothing was run: an idle subagent costs nothing until it is used
    child = await manager.get_state(result["session_id"])
    assert child is not None and child.workspace == leader.workspace
    assert child.metadata["subagent_keep"] is True and "wait for the leader's messages" in child.metadata["brief"]
    assert [c["name"] for c in await subs.children(leader.session.id)] == ["analyst"]
    # It takes its first job through SubAgentSend, like any kept subagent.
    sent = await subs.send(leader_id=leader.session.id, name="analyst", text="read the log")
    assert sent["delivered"] == "run" and submitted[-1][0] == result["session_id"] and "read the log" in submitted[-1][1]
    # Reporting does not remove it.
    await manager.sessions.append_transcript(result["session_id"], [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="the log is empty")])])
    await subs.on_run_finished(result["session_id"], "run-x", "completed")
    assert submitted[-1][2] == "subagent:analyst" and "the log is empty" in submitted[-1][1]
    assert await manager.get_state(result["session_id"]) is not None


async def test_subagent_send_steers_a_running_one_and_restarts_a_kept_one(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    submitted = _capture(manager)
    kept = await subs.spawn(leader_id=leader.session.id, task="stay", name="helper", keep=True)
    twin = await subs.spawn(leader_id=leader.session.id, task="stay", name="helper", keep=True)
    assert twin["name"] == "helper-2"
    with pytest.raises(ValueError, match="no subagent named"):
        await subs.send(leader_id=leader.session.id, name="nobody", text="hi")
    result = await subs.send(leader_id=leader.session.id, name="helper", text="next task")
    assert result["delivered"] == "run" and submitted[-1][0] == kept["session_id"] and "next task" in submitted[-1][1]
    state = await manager.get_state(kept["session_id"])
    assert state is not None
    state.task = asyncio.get_running_loop().create_future()  # type: ignore[assignment]  # looks like a run in flight
    try:
        result = await subs.send(leader_id=leader.session.id, name="helper", text="also this")
        assert result["delivered"] == "steer"
    finally:
        state.task.cancel()  # type: ignore[union-attr]
        state.task = None


async def test_withheld_tools_are_taken_away_from_the_child_session(app: Any) -> None:
    """tools_off is enforced by the host: the child's session carries the switch and its brief says who set it."""
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    _capture(manager)
    result = await subs.spawn(leader_id=leader.session.id, task="review the diff", tools_off=["SelfPropose", "ServiceStart"])
    child = await manager.get_state(result["session_id"])
    assert child is not None
    assert child.metadata["tools_off"] == ["SelfPropose", "ServiceStart"]
    assert manager.tools_off(child) == {"SelfPropose", "ServiceStart"}
    assert manager.blocked_tools_for(child) >= {"SelfPropose", "ServiceStart"}
    assert "SelfPropose" in child.metadata["brief"] and "withheld" in child.metadata["brief"]


async def test_a_withheld_tool_stays_withheld_from_the_grandchild(app: Any) -> None:
    """A child that keeps SubAgent cannot hand on what its leader took away from it."""
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    _capture(manager)
    first = await subs.spawn(leader_id=leader.session.id, task="review the diff", tools_off=["ServiceStart"])
    second = await subs.spawn(leader_id=first["session_id"], task="run the server")
    grandchild = await manager.get_state(second["session_id"])
    assert grandchild is not None
    assert grandchild.metadata["tools_off"] == ["ServiceStart"]
    # The check that matters is the one the engine build and every live update make.
    assert "ServiceStart" in manager.blocked_tools_for(grandchild)
    assert "ServiceStart" in grandchild.metadata["brief"]

    # A grandchild that withholds something of its own keeps both.
    third = await subs.spawn(leader_id=first["session_id"], task="and this", tools_off=["SelfPropose"])
    other = await manager.get_state(third["session_id"])
    assert other is not None
    assert other.metadata["tools_off"] == ["SelfPropose", "ServiceStart"]


async def test_withholding_an_unknown_tool_is_refused_rather_than_ignored(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    _capture(manager)
    with pytest.raises(ValueError, match="no such tool"):
        await subs.spawn(leader_id=leader.session.id, task="t", tools_off=["SelfPropse"])
    result = await subs.spawn(leader_id=leader.session.id, task="t")
    child = await manager.get_state(result["session_id"])
    assert child is not None and "tools_off" not in child.metadata


async def test_a_subagent_with_a_running_job_reports_when_the_job_has_woken_it(app: Any) -> None:
    # A subagent that ended its turn on "the suite is running" was reported half-done and removed,
    # and the delete ended the suite it was waiting for.
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    submitted = _capture(manager)
    result = await subs.spawn(leader_id=leader.session.id, task="run the suite")
    sid = result["session_id"]
    job = SimpleNamespace(kind="job", running=True, reported=False)
    service = SimpleNamespace(kind="service", running=True, reported=False)
    manager.jobs[sid] = {"job-1": job, "svc-1": service}
    await manager.sessions.append_transcript(sid, [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="the suite is running")])])
    await subs.on_run_finished(sid, "run-1", "completed")
    assert all(target != leader.session.id for target, _, _ in submitted)
    assert await manager.get_state(sid) is not None
    # Ended but not yet told: still the subagent's to read first.
    job.running = False
    await subs.on_run_finished(sid, "run-1", "completed")
    assert all(target != leader.session.id for target, _, _ in submitted)
    # Told, and the run it woke has ended: now the report goes, even with the service still up.
    job.reported = True
    await manager.sessions.append_transcript(sid, [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="all 40 tests pass")])])
    await subs.on_run_finished(sid, "run-2", "completed")
    assert submitted[-1][0] == leader.session.id and "all 40 tests pass" in submitted[-1][1]


async def test_a_failed_subagent_reports_even_with_a_job_running(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    submitted = _capture(manager)
    result = await subs.spawn(leader_id=leader.session.id, task="explode")
    manager.jobs[result["session_id"]] = {"job-1": SimpleNamespace(kind="job", running=True, reported=False)}
    await subs.on_run_finished(result["session_id"], "run-x", "failed")
    assert submitted[-1][0] == leader.session.id and "failed" in submitted[-1][1]


def _capture_all(manager: SessionManager) -> list[dict[str, Any]]:
    """Every submit with its keyword arguments: the steer flag is the point of these tests."""
    submitted: list[dict[str, Any]] = []

    async def fake_submit(session_id: str, text: str, attachments: Any = (), **kwargs: Any) -> str:
        submitted.append({"session_id": session_id, "text": text, **kwargs})
        return "run-x"

    manager.submit = fake_submit  # type: ignore[method-assign]
    return submitted


async def _idle_since(db: Database, session_id: str, minutes: float) -> None:
    """Make the session's last activity lie this far in the past, as the list reads it."""
    at = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    await db.execute("UPDATE sessions SET last_message_at = ? WHERE id = ?", (at, session_id))


def _running(state: Any) -> None:
    state.task = asyncio.get_running_loop().create_future()


def _stopped(state: Any) -> None:
    state.task.cancel()
    state.task = None


async def test_subagents_are_not_listed_among_the_jobs(app: Any) -> None:
    """They have a panel of their own; listed as jobs too, a standing helper looked like a job that never ends."""
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    _capture(manager)
    await subs.spawn(leader_id=leader.session.id, task="count the files")
    assert await manager.task_views(leader.session.id) == []
    assert not await manager.stop_task(leader.session.id, "agent:anything")


async def test_the_views_carry_what_the_panel_draws_and_put_the_active_ones_first(app: Any, db: Database) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    _capture(manager)
    long_task = "audit the logs for errors\n" + "x" * 1000
    worker = await subs.spawn(leader_id=leader.session.id, task=long_task, name="auditor")
    standby = await subs.spawn(leader_id=leader.session.id, name="analyst")
    await _idle_since(db, worker["session_id"], 120)
    await _idle_since(db, standby["session_id"], 5)
    state = await manager.get_state(worker["session_id"])
    assert state is not None
    assert state.metadata["subagent_task"] == long_task[:400]

    async def label(s: Any) -> str:
        return f"model-of-{s.session.metadata['subagent_name']}"

    views = await subs.views(leader.session.id, model_label=label)
    assert [v["name"] for v in views] == ["analyst", "auditor"]  # newest activity first while none runs
    analyst = views[0]
    assert set(analyst) >= {"session_id", "name", "title", "status", "running", "model", "kept", "idle_start", "created_at", "last_activity_at", "run_started_at"}
    assert analyst["title"] == "" and analyst["idle_start"] is True and analyst["kept"] is True and analyst["status"] == "idle"
    assert analyst["model"] == "model-of-analyst" and analyst["run_started_at"] is None
    # A running helper goes first whatever its age, and says when its run began.
    state.run_active_since = time.monotonic() - 90
    _running(state)
    try:
        views = await subs.views(leader.session.id)
        auditor = views[0]
        assert auditor["name"] == "auditor" and auditor["status"] == "running" and auditor["running"] is True
        assert auditor["title"] == "audit the logs for errors" and auditor["idle_start"] is False and auditor["kept"] is False
        assert auditor["model"] == ""  # no label given: left empty rather than guessed
        began = datetime.fromisoformat(auditor["run_started_at"])
        assert abs((datetime.now(UTC) - began).total_seconds() - 90) < 5
    finally:
        _stopped(state)
    state.last_error_kind = "llm_error"
    assert {v["name"]: v["status"] for v in await subs.views(leader.session.id)}["auditor"] == "failed"


async def test_the_subagents_route_answers_with_the_views(app: Any) -> None:
    import httpx

    from daedalus.extensions.api import build_app

    manager: SessionManager = app.manager
    subs = Subagents(app)
    app.extensions["subagents"] = subs
    app.guard = None
    app.create_session = manager.create_session
    leader = await manager.create_session("lead")
    _capture(manager)
    await subs.spawn(leader_id=leader.session.id, name="analyst")
    headers = {"X-Daedalus-Token": "tok"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        body = (await client.get(f"/api/sessions/{leader.session.id}/subagents", headers=headers)).json()
        assert [v["name"] for v in body["subagents"]] == ["analyst"] and body["subagents"][0]["model"]
        assert (await client.get("/api/sessions/nope/subagents", headers=headers)).status_code == 404
        assert (await client.get(f"/api/sessions/{leader.session.id}/subagents")).status_code in (401, 403)
        detail = (await client.get(f"/api/sessions/{leader.session.id}", headers=headers)).json()
        assert detail["subagents"] == body["subagents"]


async def test_report_to_leader_steers_a_running_leader_and_wakes_an_idle_one(app: Any) -> None:
    from protocore.contracts.tools import ToolContext

    from daedalus.tools.subagent import report_to_leader

    manager: SessionManager = app.manager
    subs = Subagents(app)
    manager.service_hooks["subagents"] = subs.service
    leader = await manager.create_session("lead")
    submitted = _capture_all(manager)
    child = await subs.spawn(leader_id=leader.session.id, task="dig", name="digger")
    submitted.clear()
    _running(leader)
    try:
        result = await subs.report(session_id=child["session_id"], text="  found the bug in parse()  ")
    finally:
        _stopped(leader)
    assert result["delivered"] == "steer"
    [sent] = submitted
    assert sent["session_id"] == leader.session.id and sent["steer"] is True and sent["as_answer"] is False
    assert sent["origin"] == "subagent:digger"
    assert sent["text"].startswith("[message from subagent 'digger' via ReportToLeader — it is still working") and sent["text"].endswith("found the bug in parse()")
    # Idle leader: the same steer submit, which starts a run on a session that has none.
    context = ToolContext(tenant_id="t", run_id="r", session_id=child["session_id"], metadata={"tool_call_id": "c1"})
    answer = await report_to_leader().invoke(context, {"text": "blocked: no access to the db"})
    assert not answer.is_error and "starts a turn" in str(answer.content)
    assert submitted[-1]["steer"] is True and submitted[-1]["session_id"] == leader.session.id and "blocked: no access" in submitted[-1]["text"]


async def test_report_to_leader_is_refused_without_text_leader_or_a_live_leader(app: Any) -> None:
    from protocore.contracts.tools import ToolContext

    from daedalus.tools.subagent import report_to_leader

    manager: SessionManager = app.manager
    subs = Subagents(app)
    manager.service_hooks["subagents"] = subs.service
    leader = await manager.create_session("lead")
    submitted = _capture_all(manager)
    child = await subs.spawn(leader_id=leader.session.id, task="dig", name="digger")
    submitted.clear()
    with pytest.raises(ValueError, match="empty"):
        await subs.report(session_id=child["session_id"], text="   ")
    with pytest.raises(ValueError, match="not a subagent"):
        await subs.report(session_id=leader.session.id, text="hello")
    refused = await report_to_leader().invoke(ToolContext(tenant_id="t", run_id="r", session_id=leader.session.id, metadata={"tool_call_id": "c1"}), {"text": "hello"})
    assert refused.is_error and "not a subagent" in str(refused.content)
    await manager.delete_session(leader.session.id, delete_workspace=False)
    with pytest.raises(RuntimeError, match="no longer exists"):
        await subs.report(session_id=child["session_id"], text="anyone?")
    assert submitted == []


async def test_only_a_subagent_and_its_own_subagents_may_report_to_a_leader(app: Any) -> None:
    """Judged per session after the chain: a block on the leader inherited down the chain would take it from every helper."""
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    _capture(manager)
    first = await subs.spawn(leader_id=leader.session.id, task="t", name="one")
    second = await subs.spawn(leader_id=first["session_id"], task="t", name="two")
    child = await manager.get_state(first["session_id"])
    grandchild = await manager.get_state(second["session_id"])
    assert child is not None and grandchild is not None
    assert "ReportToLeader" in {t.name for t in manager.tools.list_all()}
    assert "ReportToLeader" in manager.blocked_tools_for(leader)
    assert "ReportToLeader" not in manager.blocked_tools_for(child)
    assert "ReportToLeader" not in manager.blocked_tools_for(grandchild)


async def test_subagent_delete_removes_a_helper_by_name_and_keeps_its_files(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    _capture(manager)
    kept = await subs.spawn(leader_id=leader.session.id, name="analyst")
    (leader.workspace / "notes.txt").write_text("stay")
    with pytest.raises(ValueError, match="yours: analyst"):
        await subs.service("delete", leader_id=leader.session.id, name="nobody")
    result = await subs.service("delete", leader_id=leader.session.id, name="analyst")
    assert result["session_id"] == kept["session_id"] and result["was_running"] is False
    assert await manager.get_state(kept["session_id"]) is None
    assert (leader.workspace / "notes.txt").read_text() == "stay"
    assert await subs.children(leader.session.id) == []


async def _reminders(subs: Subagents) -> None:
    await asyncio.gather(*subs._reminders)


async def test_a_leader_is_reminded_once_about_helpers_left_idle(app: Any, db: Database) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    lid = leader.session.id
    submitted = _capture_all(manager)
    old = await subs.spawn(leader_id=lid, task="review", name="reviewer", keep=True)
    standby = await subs.spawn(leader_id=lid, name="analyst")
    fresh = await subs.spawn(leader_id=lid, task="still useful", name="fresh", keep=True)
    busy = await subs.spawn(leader_id=lid, task="long one", name="busy", keep=True)
    await manager.sessions.append_transcript(old["session_id"], [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="the diff looks fine " + "y" * 400)])])
    for sid in (old["session_id"], standby["session_id"], busy["session_id"]):
        await _idle_since(db, sid, 180)
    await _idle_since(db, fresh["session_id"], 5)
    busy_state = await manager.get_state(busy["session_id"])
    assert busy_state is not None
    _running(busy_state)
    submitted.clear()
    try:
        await subs.on_run_finished(lid, "run-1", "completed")
        await _reminders(subs)
    finally:
        _stopped(busy_state)
    [note] = submitted
    assert note["session_id"] == lid and note["origin"] == "subagents" and note["steer"] is True and note["as_answer"] is False
    text = note["text"]
    assert "- reviewer (idle, kept, idle for 3 h): the diff looks fine" in text and "…" in text
    assert "- analyst (idle, kept, idle for 3 h): (no reply yet)" in text
    assert "fresh" not in text and "busy" not in text  # active a moment ago, and still running
    assert "SubAgentDelete(name)" in text and "will not be asked about them again" in text
    assert sorted(leader.metadata["subagents_reminded"]) == sorted([old["session_id"], standby["session_id"]])
    row = (await manager.list_sessions(ids=[lid]))[0]
    assert sorted(row["metadata"]["subagents_reminded"]) == sorted([old["session_id"], standby["session_id"]])

    # The reminder's own run ending, and any later one, asks nothing again about the same helpers.
    await _idle_since(db, busy["session_id"], 1)  # its run has just ended
    await subs.on_run_finished(lid, "run-2", "completed")
    await _reminders(subs)
    assert len(submitted) == 1

    # A helper that goes idle later is named alone.
    await _idle_since(db, busy["session_id"], 60)
    await subs.on_run_finished(lid, "run-3", "completed")
    await _reminders(subs)
    assert len(submitted) == 2
    assert "- busy (idle, kept, idle for 1 h)" in submitted[-1]["text"]
    assert "reviewer" not in submitted[-1]["text"] and "analyst" not in submitted[-1]["text"]


async def test_no_reminder_after_a_run_that_did_not_complete_or_for_a_helper_on_its_way_out(app: Any, db: Database) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    lid = leader.session.id
    submitted = _capture_all(manager)
    kept = await subs.spawn(leader_id=lid, task="t", name="kept", keep=True)
    await _idle_since(db, kept["session_id"], 300)
    submitted.clear()
    for status in ("failed", "cancelled", "awaiting"):
        await subs.on_run_finished(lid, "run-x", status)
    await _reminders(subs)
    assert submitted == [] and "subagents_reminded" not in leader.metadata
    # A removal already scheduled is not a forgotten helper.
    subs._removing.add(kept["session_id"])
    await subs.on_run_finished(lid, "run-y", "completed")
    await _reminders(subs)
    assert submitted == []
    subs._removing.clear()
    manager.shutting_down = True
    try:
        await subs.on_run_finished(lid, "run-z", "completed")
        await _reminders(subs)
    finally:
        manager.shutting_down = False
    assert submitted == []
