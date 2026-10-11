"""Subagents: shared workspace, model choice, async report to the leader."""

from __future__ import annotations

import asyncio
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
