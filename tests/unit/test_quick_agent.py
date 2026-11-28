"""QuickAgent: a fast model run inside the leader's call, its reply returned as the call's result."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import ModelPresetConfig, ProviderConfig, RuntimeConfig, Settings
from daedalus.extensions import subagents as subagents_module
from daedalus.extensions.subagents import Subagents
from daedalus.host import call_scope, prompts, quick_agent, tool_groups
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.browser import _caller
from daedalus.tools.history import history_expand, history_search
from daedalus.tools.subagent import quick_agent as quick_agent_tool

PRESETS = ("local.big", "local.fast", "local.tiny")


def _config(quick: list[str] | None = None) -> RuntimeConfig:
    runtime = RuntimeConfig(
        providers={"local": ProviderConfig(kind="openai_compat", base_url="http://127.0.0.1:9/v1")},
        presets={pid: ModelPresetConfig(provider="local", model=pid.split(".")[1]) for pid in PRESETS},
        model={"preset": "local.big"},
    )
    runtime.subagents.quick_models = list(quick if quick is not None else ["local.fast", "local.tiny"])
    return runtime


@pytest.fixture
async def app(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> Any:
    # A poll every half second is right for a person waiting; a test waits on nothing but itself.
    monkeypatch.setattr(subagents_module, "QUICK_POLL_SECONDS", 0.01)
    manager = SessionManager(settings, _config(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={})
    subs = Subagents(app)  # type: ignore[arg-type]
    app.extensions["subagents"] = subs
    manager.service_hooks["subagents"] = subs.service
    manager.delete_hooks.append(subs.on_session_deleted)
    yield app
    await manager.close()


def _answering(manager: SessionManager, reply: str = "the total is 12 939,28") -> list[tuple[str, str, str]]:
    """Stand in for the model: every run the host starts ends at once with ``reply`` as its final message."""
    submitted: list[tuple[str, str, str]] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer=False, as_answer=True, origin="operator", **_: Any) -> str:  # type: ignore[no-untyped-def]
        submitted.append((session_id, text, origin))
        await manager.sessions.append_transcript(session_id, [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=reply)])])
        return "run-q"

    manager.submit = fake_submit  # type: ignore[method-assign]
    return submitted


async def test_the_reply_comes_back_as_the_result_and_the_helper_is_on_no_list(app: Any) -> None:
    manager: SessionManager = app.manager
    subs: Subagents = app.extensions["subagents"]
    leader = await manager.create_session("lead")
    lid = leader.session.id
    submitted = _answering(manager)
    result = await subs.quick(leader_id=lid, task="fill the three drafts and report their numbers", call_id="toolu_1")
    assert result["completed"] and result["answer"] == "the total is 12 939,28"
    cid = result["session_id"]
    assert f"(session {cid})" in result["header"] and "local.fast" in result["header"]
    # It ran on the first quick model, with the task framed as its leader's.
    assert (await manager.live.load(cid)).get("preset") == "local.fast"
    assert submitted[0][0] == cid and "fill the three drafts" in submitted[0][1] and submitted[0][2] == f"subagent-task:{lid}"
    # Nothing reached the leader as a message: the call is the only way back.
    assert all(sid != lid for sid, _, _ in submitted)
    await subs.on_run_finished(cid, "run-q", "completed")
    assert all(sid != lid for sid, _, _ in submitted)
    # Not on the Subagents panel, not among the leader's helpers, not in the session lists; the transcript finds it by call.
    assert await subs.views(lid) == [] and await subs.children(lid) == []
    assert cid not in {row["id"] for row in await manager.list_sessions(limit=500)}
    assert [(c["session_id"], c["call"]) for c in await subs.quick_children(lid)] == [(cid, "toolu_1")]


async def test_the_tool_returns_the_reply_inline_and_names_the_session(app: Any) -> None:
    manager: SessionManager = app.manager
    leader = await manager.create_session("lead")
    _answering(manager, "page title: Платежи в тенге")
    context = ToolContext(tenant_id="daedalus", run_id="r", session_id=leader.session.id)
    token = call_scope.CURRENT.set(call_scope.CallScope(session_id=leader.session.id, tool_call_id="toolu_9", tool="QuickAgent"))
    try:
        result = await quick_agent_tool().invoke(context, {"task": "read the page title", "model": "local.tiny", "effort": "low"})
    finally:
        call_scope.CURRENT.reset(token)
    assert not result.is_error and result.content.endswith("page title: Платежи в тенге")
    cid = str(result.metadata["session_id"])
    assert result.content.startswith(f"[quick agent on local.tiny (session {cid}) finished")
    assert quick_agent.call_of((await manager.get_state(cid)).metadata) == "toolu_9"  # type: ignore[union-attr]
    controls = await manager.live.load(cid)
    assert controls.get("preset") == "local.tiny" and controls.get("reasoning_effort") == "low"


async def test_its_checks_are_lifted_whatever_the_leader_says(app: Any) -> None:
    manager: SessionManager = app.manager
    subs: Subagents = app.extensions["subagents"]
    leader = await manager.create_session("lead")
    lid = leader.session.id
    assert not leader.bypass
    _answering(manager)
    result = await subs.quick(leader_id=lid, task="click through")
    child = await manager.get_state(result["session_id"])
    assert child is not None and child.bypass and await manager.sessions.bypass(child.session.id)
    # The leader switching its own checks on and off leaves the helper's lifted.
    await manager.set_bypass(lid, True, via="test")
    await manager.set_bypass(lid, False, via="test")
    assert child.bypass and await manager.sessions.bypass(child.session.id)


async def test_the_models_come_from_settings_and_the_session_may_choose_its_own(app: Any) -> None:
    manager: SessionManager = app.manager
    subs: Subagents = app.extensions["subagents"]
    leader = await manager.create_session("lead")
    lid = leader.session.id
    _answering(manager)
    assert manager.quick_models(leader.metadata) == ["local.fast", "local.tiny"]
    with pytest.raises(ValueError, match="'local.big' is not a quick model here; choose one of: local.fast, local.tiny"):
        await subs.quick(leader_id=lid, task="t", model="local.big")
    notes = manager.notes_for(leader, overrides=await manager.live.load(lid))
    assert "Models QuickAgent accepts (preset ids, the first is its default): local.fast, local.tiny" in notes
    assert "QuickAgent" not in manager.blocked_tools_for(leader)
    # The session's own list wins, a gone preset drops out, and its first is the default.
    await manager.set_quick_models(lid, ["local.tiny", "gone.x"])
    assert manager.quick_models(leader.metadata) == ["local.tiny"]
    result = await subs.quick(leader_id=lid, task="t")
    assert (await manager.live.load(result["session_id"])).get("preset") == "local.tiny"
    # An empty list turns the tool off here: off the surface, out of the environment, refused if called.
    await manager.set_quick_models(lid, [])
    assert "QuickAgent" in manager.blocked_tools_for(leader)
    assert "QuickAgent" not in manager.notes_for(leader, overrides={})
    with pytest.raises(ValueError, match="no quick model is chosen"):
        await subs.quick(leader_id=lid, task="t")
    # Back to the default from Settings.
    await manager.set_quick_models(lid, None)
    assert manager.quick_models(leader.metadata) == ["local.fast", "local.tiny"]
    # With no default either, no session has it.
    manager.config.subagents.quick_models = []
    assert "QuickAgent" in manager.blocked_tools_for(leader)


async def test_the_helper_reaches_what_the_leader_reaches_and_nothing_that_asks(app: Any) -> None:
    manager: SessionManager = app.manager
    subs: Subagents = app.extensions["subagents"]
    leader = await manager.create_session("lead")
    lid = leader.session.id
    for meta in (leader.metadata, leader.session.metadata):
        meta[tool_groups.CARRIED_KEY] = ["browser"]
        meta["discovered_tools"] = ["BrowserOpen"]
        meta["tools_off"] = ["ServiceStart"]
    _answering(manager)
    result = await subs.quick(leader_id=lid, task="t")
    child = await manager.get_state(result["session_id"])
    assert child is not None and child.workspace == leader.workspace and child.project.id == leader.project.id  # type: ignore[union-attr]
    # The tools the leader had loaded come loaded: no search for the browser before the first click.
    assert child.metadata[tool_groups.CARRIED_KEY] == ["browser"] and child.metadata["discovered_tools"] == ["BrowserOpen"]
    blocked = manager.blocked_tools_for(child)
    known = {t.name for t in manager.tools.list_all()}
    # No agents of its own, no questions to anyone, no message on the way, and the leader's refusals hold.
    # The browser's requests to the operator are not among them: they wait in the call
    # (test_quick_agent_operator_wait.py).
    for name in ("QuickAgent", "SubAgent", "AskUser", "ReportToLeader", "ServiceStart"):
        assert name in blocked or name not in known
    assert "ReportToLeader" in known and "ReportToLeader" in blocked
    with pytest.raises(ValueError, match="cannot start a quick agent"):
        await subs.quick(leader_id=child.session.id, task="deeper")


async def test_the_helper_drives_the_leaders_browser(app: Any) -> None:
    manager: SessionManager = app.manager
    subs: Subagents = app.extensions["subagents"]
    leader = await manager.create_session("lead")
    _answering(manager)
    result = await subs.quick(leader_id=leader.session.id, task="t")
    cid = result["session_id"]
    caller = _caller(ToolContext(tenant_id="daedalus", run_id="r", session_id=cid))
    assert caller is not None
    # The leader's group, tabs and Chrome; the actor in the log is still the helper.
    assert caller.owner.id == leader.session.id and caller.owner.session_id == leader.session.id
    assert caller.actor == f"agent:{cid}"
    own = _caller(ToolContext(tenant_id="daedalus", run_id="r", session_id=leader.session.id))
    assert own is not None and own.owner == caller.owner


async def test_the_helper_reads_its_leaders_history_on_demand(app: Any) -> None:
    manager: SessionManager = app.manager
    subs: Subagents = app.extensions["subagents"]
    leader = await manager.create_session("lead", metadata={"private": True})
    lid = leader.session.id
    await manager.sessions.append_transcript(lid, [Message(role=MessageRole.user, content_blocks=[TextBlock(text="the ОПВР amount is 2975 for Tikhonov")], metadata={"daedalus.origin": "operator"})])
    _answering(manager)
    result = await subs.quick(leader_id=lid, task="t")
    cid = result["session_id"]
    # Its task is all it starts with; the brief says where the rest is.
    brief = (await manager.get_state(cid)).metadata["brief"]  # type: ignore[union-attr]
    assert f'session_id="{lid}"' in brief
    context = ToolContext(tenant_id="daedalus", run_id="r", session_id=cid)
    found = await history_search().invoke(context, {"query": "Tikhonov", "session_id": lid})
    assert not found.is_error and "seq" in found.content
    seq = int(found.content.split()[1])
    read = await history_expand().invoke(context, {"from_seq": seq, "session_id": lid})
    # The leader's chat is private, and its own helper reads it anyway: it is the chat the helper works for.
    assert not read.is_error and "2975" in read.content
    # Any other chat is out of HistorySearch's reach.
    other = await manager.create_session("other")
    refused = await history_search().invoke(ToolContext(tenant_id="daedalus", run_id="r", session_id=other.session.id), {"query": "Tikhonov", "session_id": lid})
    assert refused.is_error and "only your leader" in refused.content


async def test_a_stopped_leader_takes_its_helper_down(app: Any) -> None:
    manager: SessionManager = app.manager
    subs: Subagents = app.extensions["subagents"]
    leader = await manager.create_session("lead")
    started: list[str] = []

    async def never_finishes(session_id: str, text: str, attachments=(), **_: Any) -> str:  # type: ignore[no-untyped-def]
        started.append(session_id)
        state = await manager.get_state(session_id)
        state.task = asyncio.get_running_loop().create_future()  # type: ignore[union-attr,assignment]  # looks like a run in flight
        return "run-long"

    manager.submit = never_finishes  # type: ignore[method-assign]
    call = asyncio.create_task(subs.quick(leader_id=leader.session.id, task="t"))
    while not started:
        await asyncio.sleep(0.01)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert await manager.get_state(started[0]) is None


async def test_a_helper_past_its_time_is_stopped_and_its_work_so_far_returned(app: Any) -> None:
    manager: SessionManager = app.manager
    subs: Subagents = app.extensions["subagents"]
    leader = await manager.create_session("lead")

    async def slow(session_id: str, text: str, attachments=(), **_: Any) -> str:  # type: ignore[no-untyped-def]
        state = await manager.get_state(session_id)
        state.task = asyncio.get_running_loop().create_future()  # type: ignore[union-attr,assignment]
        await manager.sessions.append_transcript(session_id, [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="two of three drafts saved")])])
        return "run-slow"

    async def stop(session_id: str) -> bool:
        (await manager.get_state(session_id)).task.set_result(None)  # type: ignore[union-attr]
        return True

    manager.submit = slow  # type: ignore[method-assign]
    manager.stop = stop  # type: ignore[method-assign]
    original = subs._await_quick

    async def short(cid: str, seconds: float) -> str:
        return await original(cid, 0.05)

    subs._await_quick = short  # type: ignore[method-assign]
    result = await subs.quick(leader_id=leader.session.id, task="t")
    assert not result["completed"] and "stopped at its time limit" in result["header"]
    assert "two of three drafts saved" in result["answer"]


async def test_old_helpers_are_pruned_and_all_go_with_their_leader(app: Any) -> None:
    manager: SessionManager = app.manager
    subs: Subagents = app.extensions["subagents"]
    leader = await manager.create_session("lead")
    lid = leader.session.id
    _answering(manager)
    made = [(await subs.quick(leader_id=lid, task=f"t{n}", call_id=f"c{n}"))["session_id"] for n in range(quick_agent.KEEP_PER_LEADER + 2)]
    kept = [c["session_id"] for c in await subs.quick_children(lid)]
    assert len(kept) == quick_agent.KEEP_PER_LEADER and made[-1] in kept and made[0] not in kept
    assert await manager.delete_session(lid)
    for cid in kept:
        assert await manager.get_state(cid) is None


def test_the_prompt_teaches_it_only_where_it_is_advertised() -> None:
    shown = "".join(prompts.tool_sections({"QuickAgent"}))
    assert prompts.BOARD_QUICK in shown and "browser" in prompts.BOARD_QUICK and "never asks" in prompts.BOARD_QUICK
    assert prompts.BOARD_QUICK not in "".join(prompts.tool_sections({"SubAgent", "SubAgentSend", "SubAgentList"}))


async def test_a_real_run_hands_the_task_over_and_continues_with_the_reply(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the loop: the leader's model calls QuickAgent, the helper's run answers, and the
    leader's next request carries that answer as the call's result."""
    from tests.support.models import FALLBACK_PRESET, model_config
    from tests.unit.test_session_runner import ScriptedProvider

    monkeypatch.setattr(subagents_module, "QUICK_POLL_SECONDS", 0.01)
    provider = ScriptedProvider([
        {"tool": "QuickAgent", "args": {"task": "count the rows in the report and say the number"}},
        {"text": "there are 42 rows"},
        {"text": "The report has 42 rows."},
    ])
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        manager.providers.rungs_for = lambda config, preset_id=None: [(provider, "scripted-model")]  # type: ignore[method-assign]
        manager.quick_models = lambda metadata, session_id=None: [FALLBACK_PRESET]  # type: ignore[method-assign]
        app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={})
        subs = Subagents(app)  # type: ignore[arg-type]
        manager.service_hooks["subagents"] = subs.service
        leader = await manager.create_session("lead")
        finished = asyncio.Event()

        async def on_finished(session_id: str, run_id: str, status: str) -> None:
            if session_id == leader.session.id:
                finished.set()

        manager.on_finished(on_finished)
        await manager.submit(leader.session.id, "how many rows?")
        await asyncio.wait_for(finished.wait(), timeout=30)
        first, helper, last = provider.requests
        leader_tools = {t.name for t in first.tools or ()}
        helper_tools = {t.name for t in helper.tools or ()}
        assert "QuickAgent" in leader_tools and "QuickAgent" not in helper_tools and "ReportToLeader" not in helper_tools
        # The leader is taught when to hand work over; the helper, which cannot, is not.
        assert "QuickAgent(task) runs a fast model" in str(first.messages[0].content_blocks)
        assert "QuickAgent(task) runs a fast model" not in str(helper.messages[0].content_blocks)
        assert "You are a quick agent of session" in str(helper.messages[0].content_blocks)
        # The helper starts from its task, not from the leader's conversation.
        assert "how many rows?" not in " ".join(str(m.content_blocks) for m in helper.messages)
        results = [b for m in last.messages if m.role is MessageRole.tool for b in m.content_blocks]
        assert results and "there are 42 rows" in str(results[-1].content) and "quick agent on" in str(results[-1].content)
        children = await subs.quick_children(leader.session.id)
        assert len(children) == 1 and children[0]["call"] and (await manager.get_state(children[0]["session_id"])).bypass  # type: ignore[union-attr]
    finally:
        await manager.close()
