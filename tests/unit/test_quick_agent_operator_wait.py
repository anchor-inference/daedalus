"""A quick agent waiting, inside its tool call, for what only the operator can do in the browser.

An ordinary session asks, ends its turn and is woken with the answer. A quick agent's turn ending is
its run ending, and its leader sits in the QuickAgent call meanwhile, so the answer resolves the
helper's call instead, and the leader is never woken with it."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import MessageRole

from daedalus.browser.agent import QUICK_WRAP, BrowserAgent, Caller, WaitsHere
from daedalus.browser.login import LoginDesk
from daedalus.browser.model import Owner
from daedalus.browser.service import Browsers
from daedalus.config import (
    QUICK_AGENT_BLOCKED_TOOLS,
    BrowserConfig,
    ModelPresetConfig,
    ProviderConfig,
    RuntimeConfig,
    Settings,
)
from daedalus.extensions import browser as browser_extension
from daedalus.extensions import subagents as subagents_module
from daedalus.extensions.subagents import Subagents
from daedalus.host import tool_groups
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.browser import _caller
from daedalus.tools.subagent import quick_agent as quick_agent_tool
from tests.support.fake_browserd import Element, FakeBrowserd
from tests.unit.test_browser_service import FakeBus, FakeOwners, wait_until

LEADER = Owner("session", "lead1", project_id="proj1", session_id="lead1")
GROUP = "s-lead1"


@pytest.fixture
def run_dir() -> Iterable[Path]:
    path = Path(tempfile.mkdtemp(prefix="bd-"))
    yield path / "run"
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def daemon(run_dir: Path) -> AsyncIterator[FakeBrowserd]:
    fake = await FakeBrowserd(run_dir).start()
    yield fake
    await fake.stop()


class Rig:
    def __init__(self, service: Browsers, bus: FakeBus) -> None:
        self.service = service
        self.bus = bus
        self.agent = BrowserAgent(service)
        self.woken: list[tuple[str, str, bool]] = []
        """Every wake, the ones only for a running turn included: the leader of a quick agent is running."""

    def caller(self, *, waits: float | None = None) -> Caller:
        async def gate(_: Any) -> tuple[bool, str]:
            return True, ""

        return Caller(
            owner=LEADER, actor="agent:helper1", gate=gate, files=None,  # type: ignore[arg-type]
            waits=WaitsHere(session_id="helper1", seconds=waits) if waits is not None else None,
        )

    async def need(self) -> Any:
        return (await self.service.get(GROUP))["needs_you"]

    async def control(self) -> str:
        return str((await self.service.get(GROUP))["control"]["owner"])

    async def waiting(self) -> bool:
        return bool(self.service.operator_waits_of("helper1"))


@pytest.fixture
async def rig(db: Database, run_dir: Path, daemon: FakeBrowserd) -> AsyncIterator[Rig]:
    owners = FakeOwners()
    owners.add(LEADER, "Lead chat")
    bus = FakeBus()
    made: Rig | None = None

    async def wake(owner: Owner, text: str, *, only_if_running: bool = False) -> None:
        assert made is not None
        made.woken.append((str(owner.session_id), text, only_if_running))

    service = Browsers(db, run_dirs={"container": run_dir, "host": None}, config=lambda: BrowserConfig(agent_wait_seconds=5, control_wait_seconds=0.2), owners=owners, bus=bus, wake=wake)  # type: ignore[arg-type]
    await service.start()
    assert await service.wait_available("container")
    made = Rig(service, bus)
    await service.open(LEADER, url="https://shop.test/", actor="agent:lead1")
    yield made
    await service.close()


async def _settled() -> None:
    # The control event travels from the daemon and back; a wake it caused would have landed by then.
    await asyncio.sleep(0.2)


async def test_a_quick_agents_handoff_waits_and_the_give_back_answers_it_not_the_leader(rig: Rig) -> None:
    call = asyncio.create_task(rig.agent.handoff(rig.caller(waits=30), reason="captcha", what="solve the CAPTCHA on shop.test"))
    await wait_until(rig.waiting, True)
    # Asked exactly as an ordinary session asks: paused, on the group's "needs you", announced.
    assert not call.done() and await rig.control() == "paused"
    assert (await rig.need())["reason"] == "captcha"
    assert rig.bus.of("browser.needs_you")[-1][1]["session_id"] == "lead1"
    view = rig.service.operator_waits_of("helper1")[0]
    assert view["kind"] == "captcha" and view["group_id"] == GROUP and view["until"]
    await rig.service.control(GROUP, "agent", note="solved it")
    text = await asyncio.wait_for(call, 5)
    assert "gave the browser back" in text and "solved it" in text and "Take a BrowserSnapshot" in text
    assert "End your turn" not in text
    await _settled()
    assert rig.woken == []
    assert await rig.need() is None and not rig.service.operator_waits_of("helper1")


async def test_a_declined_handoff_and_a_declined_sign_in_say_so(rig: Rig) -> None:
    call = asyncio.create_task(rig.agent.handoff(rig.caller(waits=30), reason="payment", what="pay the invoice"))
    await wait_until(rig.waiting, True)
    await rig.service.decline(GROUP)
    text = await asyncio.wait_for(call, 5)
    assert "declined to pay the invoice" in text and QUICK_WRAP in text
    await _settled()
    assert rig.woken == [] and await rig.control() == "agent"
    # The private sign-in's own No goes the same way, through the form's decline.
    await rig.service.handoff(GROUP, "login", "sign in to shop.test: the order", actor="agent:helper1", login={"id": "L1", "site": "shop.test"})

    async def nobody_withdraws() -> None:
        return None

    waited = asyncio.create_task(rig.service.wait_for_operator(GROUP, session_id="helper1", kind="login", what="sign in", seconds=30, withdraw=nobody_withdraws, group_id=GROUP))
    await wait_until(rig.waiting, True)
    await LoginDesk(rig.service).decline(GROUP, "L1")
    outcome, said = await asyncio.wait_for(waited, 5)
    assert outcome == "declined" and "declined to sign in to shop.test" in said
    await _settled()
    assert rig.woken == []


async def test_nobody_answering_withdraws_the_request_and_wakes_nobody(rig: Rig) -> None:
    text = await asyncio.wait_for(rig.agent.handoff(rig.caller(waits=0.3), reason="two_factor", what="enter the code"), 10)
    assert "did not answer within 1 min" in text and "withdrawn" in text and QUICK_WRAP in text
    await wait_until(rig.control, "agent")
    assert await rig.need() is None
    # The notification is settled by the give-back event, and the leader hears nothing of it.
    await wait_until(lambda: _count(rig.bus, "browser.returned"), 1)
    await _settled()
    assert rig.woken == []
    actions = [e["action"] for e in await rig.service.audit_log(GROUP)]
    assert "withdraw" in actions
    # A later, unrelated give-back is the owner's again: the finished wait cannot swallow it.
    await rig.service.handoff(GROUP, "other", "look at the page", actor="agent:lead1")
    await rig.service.control(GROUP, "agent")
    await wait_until(lambda: _async(len(rig.woken)), 1)
    assert rig.woken[0][0] == "lead1" and not rig.woken[0][2]


async def test_an_operator_at_the_controls_keeps_the_wait_open(rig: Rig) -> None:
    call = asyncio.create_task(rig.agent.handoff(rig.caller(waits=0.3), reason="captcha", what="solve it"))
    await wait_until(rig.waiting, True)
    await rig.service.control(GROUP, "human", client_id="v1")
    await wait_until(rig.control, "human")
    await asyncio.sleep(0.6)
    assert not call.done()
    await rig.service.control(GROUP, "agent")
    assert "gave the browser back" in await asyncio.wait_for(call, 5)


async def test_a_cancelled_wait_takes_the_request_back(rig: Rig) -> None:
    call = asyncio.create_task(rig.agent.handoff(rig.caller(waits=30), reason="confirm", what="confirm the order"))
    await wait_until(rig.waiting, True)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    await wait_until(rig.control, "agent")
    assert await rig.need() is None and not rig.service.operator_waits_of("helper1")
    await _settled()
    assert rig.woken == []
    # The leader's stop reaches it the same way when the helper's call is not the one cancelled.
    again = asyncio.create_task(rig.agent.handoff(rig.caller(waits=30), reason="confirm", what="confirm the order"))
    await wait_until(rig.waiting, True)
    assert await rig.service.cancel_waits_of("helper1") == 1
    assert "leader was stopped" in await asyncio.wait_for(again, 5)
    await wait_until(rig.control, "agent")
    assert await rig.need() is None


async def test_a_closed_browser_ends_the_wait(rig: Rig) -> None:
    call = asyncio.create_task(rig.agent.handoff(rig.caller(waits=30), reason="captcha", what="solve it"))
    await wait_until(rig.waiting, True)
    await rig.service.close_group(GROUP, actor="operator")
    text = await asyncio.wait_for(call, 5)
    assert "browser was closed" in text and QUICK_WRAP in text


async def test_an_ordinary_session_still_ends_its_turn_and_is_woken(rig: Rig) -> None:
    text = await rig.agent.handoff(rig.caller(), reason="captcha", what="solve it")
    assert "End your turn now" in text and not rig.service.operator_waits_of("helper1")
    await rig.service.control(GROUP, "agent")
    await wait_until(lambda: _async(len(rig.woken)), 1)
    assert rig.woken[0][0] == "lead1" and "gave the browser back" in rig.woken[0][1] and not rig.woken[0][2]


async def _async(value: Any) -> Any:
    return value


async def _count(bus: FakeBus, event_type: str) -> int:
    return len(bus.of(event_type))


# -- through the session manager: the clock, the Chrome ask and the whole helper run ------------------


PRESETS = ("local.big", "local.fast")


def _runtime(**browser: Any) -> RuntimeConfig:
    runtime = RuntimeConfig(
        providers={"local": ProviderConfig(kind="openai_compat", base_url="http://127.0.0.1:9/v1")},
        presets={pid: ModelPresetConfig(provider="local", model=pid.split(".")[1]) for pid in PRESETS},
        model={"preset": "local.big"},
        browser=BrowserConfig(control_wait_seconds=0.2, **browser),
    )
    runtime.subagents.quick_models = ["local.fast"]
    return runtime


@pytest.fixture
def base() -> Iterable[Path]:
    path = Path(tempfile.mkdtemp(prefix="bd-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def shop(base: Path) -> AsyncIterator[FakeBrowserd]:
    fake = await FakeBrowserd(base / "b").start()
    fake.page("https://shop.test/", title="Shop", heading="Shop", elements={"e1": Element("e1", "button", "Pay", goes_to="https://shop.test/paid")})
    fake.page("https://shop.test/paid", title="Paid", heading="Thank you")
    yield fake
    await fake.stop()


async def _app(settings: Settings, db: Database, base: Path, runtime: RuntimeConfig) -> tuple[Any, list[Any]]:
    configured = settings.model_copy(update={"browser_container_dir": base / "b"})
    manager = SessionManager(configured, runtime, db=db)
    await manager.start()
    app = SimpleNamespace(settings=configured, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None)
    tasks = await browser_extension.install(app)  # type: ignore[arg-type]
    assert await app.extensions["browser"].wait_available("container")
    subs = Subagents(app)  # type: ignore[arg-type]
    app.extensions["subagents"] = subs
    manager.service_hooks["subagents"] = subs.service
    manager.delete_hooks.append(subs.on_session_deleted)
    return app, tasks


async def _close(app: Any, tasks: list[Any]) -> None:
    for task in tasks:
        task.cancel()
    await app.extensions["browser"].close()
    await app.manager.close()


def test_the_move_into_the_operators_chrome_is_no_longer_refused() -> None:
    assert "BrowserAskOperatorChrome" not in QUICK_AGENT_BLOCKED_TOOLS and "ReportToLeader" in QUICK_AGENT_BLOCKED_TOOLS


def test_the_waiting_calls_keep_their_own_clock_past_the_flat_tool_timeout() -> None:
    from daedalus.tools.browser import browser_ask_operator_chrome, browser_handoff, browser_login

    # The core gives a tool that declares its own clock that budget instead of limits.tool_timeout_seconds
    # (900 s), which cancelled the leader's QuickAgent call and deleted the helper mid-wait.
    for made in (quick_agent_tool, browser_handoff, browser_login, browser_ask_operator_chrome):
        tool = made()
        assert tool.should_defer is True and tool.default_timeout_ms > 900_000  # type: ignore[attr-defined]


async def test_the_quick_wait_limit_stands_still_while_the_helper_waits_for_the_operator(settings: Settings, db: Database, base: Path, shop: FakeBrowserd, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subagents_module, "QUICK_POLL_SECONDS", 0.01)
    app, tasks = await _app(settings, db, base, _runtime())
    try:
        manager: SessionManager = app.manager
        subs: Subagents = app.extensions["subagents"]
        helper = await manager.create_session("helper")
        state = await manager.get_state(helper.session.id)
        assert state is not None
        state.task = asyncio.get_running_loop().create_future()  # type: ignore[assignment]  # a run in flight
        waiting = [True]
        monkeypatch.setattr(subs, "operator_waits", lambda cid: [{"kind": "captcha"}] if waiting[0] else [])
        stopped: list[str] = []

        async def stop(session_id: str) -> bool:
            stopped.append(session_id)
            state.task.set_result(None)  # type: ignore[union-attr]
            return True

        manager.stop = stop  # type: ignore[method-assign]
        awaited = asyncio.create_task(subs._await_quick(helper.session.id, 0.2))
        # Four times its limit spent waiting for the operator, and the helper is still given its time.
        await asyncio.sleep(0.8)
        assert not awaited.done() and stopped == []
        waiting[0] = False
        assert await asyncio.wait_for(awaited, 5) == "timeout"
        assert stopped == [helper.session.id]
    finally:
        await _close(app, tasks)


async def test_the_move_into_the_operators_chrome_waits_on_the_leaders_card(settings: Settings, db: Database, base: Path, shop: FakeBrowserd, monkeypatch: pytest.MonkeyPatch) -> None:
    app, tasks = await _app(settings, db, base, _runtime())
    try:
        manager: SessionManager = app.manager
        service: Browsers = app.extensions["browser"]
        leader = await manager.create_session("lead")
        lid = leader.session.id
        helper = await manager.create_session("[quick] t", metadata={"subagent_of": lid, "quick_agent": {"call": "c1"}})
        cid = helper.session.id
        assert "BrowserAskOperatorChrome" not in manager.blocked_tools_for(helper)
        # The operator's Chrome is offered and connected; the switch itself is the panel's, stood in for here.
        monkeypatch.setattr(service, "user_offered", lambda: True)
        monkeypatch.setattr(service, "user_connection", lambda: (True, "Work", ""))
        switched: list[str] = []

        async def switch(session_id: str, env: str, *, consent: bool) -> dict[str, Any]:
            switched.append(session_id)
            return {}

        monkeypatch.setattr(service, "switch_conversation", switch)
        woken: list[str] = []

        async def wake(owner: Owner, text: str, *, only_if_running: bool = False) -> None:
            woken.append(text)

        service.wake = wake
        tool = manager.tools.get("BrowserAskOperatorChrome")
        assert tool is not None
        call = asyncio.create_task(tool.invoke(ToolContext(tenant_id="t", run_id="r", session_id=cid), {"reason": "the bank is signed in there"}))
        await wait_until(lambda: _async(bool(service.operator_waits_of(cid))), True)
        # On the leader's card, where the operator is; the helper has none anybody sees.
        pending = (await manager.get_state(lid)).metadata.get("policy_pending") or {}  # type: ignore[union-attr]
        assert len(pending) == 1 and not ((await manager.get_state(cid)).metadata.get("policy_pending"))  # type: ignore[union-attr]
        await manager.grant(lid, next(iter(pending)), via="app")
        result = await asyncio.wait_for(call, 5)
        assert not result.is_error and "switched this conversation to their own Chrome" in str(result.content)
        assert switched == [lid] and woken == []
        # Unanswered, it is taken off the leader's card when the wait gives up.
        manager.config.subagents.operator_wait_minutes = 1
        monkeypatch.setattr(service, "env_for", lambda owner: _async("container"))
        caller = _caller(ToolContext(tenant_id="t", run_id="r", session_id=cid))
        assert caller is not None and caller.waits is not None and caller.waits.seconds == 60
        caller.waits.seconds = 0.2
        text, failed = await service_agent(app).ask_operator_chrome(caller, reason="once more")
        assert failed and "did not answer" in text
        assert not (await manager.get_state(lid)).metadata.get("policy_pending")  # type: ignore[union-attr]
        assert woken == []
    finally:
        await _close(app, tasks)


def service_agent(app: Any) -> BrowserAgent:
    return app.manager.service_hooks["browser"]


async def test_a_stopped_leader_withdraws_what_its_helper_asked(settings: Settings, db: Database, base: Path, shop: FakeBrowserd, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subagents_module, "QUICK_POLL_SECONDS", 0.01)
    app, tasks = await _app(settings, db, base, _runtime())
    try:
        manager: SessionManager = app.manager
        subs: Subagents = app.extensions["subagents"]
        service: Browsers = app.extensions["browser"]
        leader = await manager.create_session("lead")
        lid = leader.session.id
        started: list[str] = []
        held: list[asyncio.Task[Any]] = []

        async def helper_run(session_id: str, text: str, attachments: Any = (), **_: Any) -> str:
            # The helper's run: it opens the leader's browser and hands it over, and waits there.
            started.append(session_id)
            state = await manager.get_state(session_id)
            state.task = asyncio.get_running_loop().create_future()  # type: ignore[union-attr,assignment]
            context = ToolContext(tenant_id="t", run_id="r", session_id=session_id)
            await manager.tools.get("BrowserOpen").invoke(context, {"url": "https://shop.test/"})  # type: ignore[union-attr]
            held.append(asyncio.create_task(manager.tools.get("BrowserHandoff").invoke(context, {"reason": "captcha", "what": "solve it"})))  # type: ignore[union-attr]
            return "run-h"

        manager.submit = helper_run  # type: ignore[method-assign]
        call = asyncio.create_task(subs.quick(leader_id=lid, task="pay the invoice", call_id="c1"))
        await wait_until(lambda: _async(bool(started) and bool(service.operator_waits_of(started[0]))), True)
        group = f"s-{lid}"
        assert (await service.get(group))["needs_you"] is not None
        # The leader's transcript reads what the helper waits for under its call.
        row = (await subs.quick_children(lid))[0]
        assert row["waiting"]["kind"] == "captcha" and row["waiting"]["group_id"] == group
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await wait_until(lambda: _control(service, group), "agent")
        assert (await service.get(group))["needs_you"] is None
        assert await manager.get_state(started[0]) is None
        for task in held:
            task.cancel()
    finally:
        await _close(app, tasks)


async def _control(service: Browsers, group: str) -> str:
    return str((await service.get(group))["control"]["owner"])


async def test_a_real_helper_run_waits_for_the_handoff_and_carries_on_in_the_same_run(settings: Settings, db: Database, base: Path, shop: FakeBrowserd, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through ``Subagents.quick`` and the loop: the helper's model hands the browser over, the
    operator gives it back, and the same run clicks on and reports, which the leader's call returns."""
    from tests.support.models import FALLBACK_PRESET, model_config
    from tests.unit.test_session_runner import ScriptedProvider

    monkeypatch.setattr(subagents_module, "QUICK_POLL_SECONDS", 0.01)
    provider = ScriptedProvider([
        {"tool": "BrowserOpen", "args": {"url": "https://shop.test/"}},
        {"tool": "BrowserHandoff", "args": {"reason": "captcha", "what": "solve the CAPTCHA on shop.test"}},
        {"tool": "BrowserAct", "args": {"ref": "e1", "action": "click"}},
        {"text": "Paid after the operator solved the CAPTCHA; the page says Thank you."},
    ])
    runtime = model_config()
    runtime.browser = BrowserConfig(control_wait_seconds=0.2)
    app, tasks = await _app(settings, db, base, runtime)
    try:
        manager: SessionManager = app.manager
        subs: Subagents = app.extensions["subagents"]
        service: Browsers = app.extensions["browser"]
        manager.providers.rungs_for = lambda config, preset_id=None: [(provider, "scripted-model")]  # type: ignore[method-assign]
        manager.quick_models = lambda metadata, session_id=None: [FALLBACK_PRESET]  # type: ignore[method-assign]
        leader = await manager.create_session("lead")
        lid = leader.session.id
        for meta in (leader.metadata, leader.session.metadata):
            meta[tool_groups.CARRIED_KEY] = ["browser"]
        submitted: list[str] = []
        real_submit = manager.submit

        async def watched(session_id: str, text: str, *args: Any, **kwargs: Any) -> str:
            submitted.append(session_id)
            return await real_submit(session_id, text, *args, **kwargs)

        manager.submit = watched  # type: ignore[method-assign]
        call = asyncio.create_task(subs.quick(leader_id=lid, task="pay the invoice on shop.test", call_id="toolu_q"))
        group = f"s-{lid}"

        async def asked() -> bool:
            children = await subs.quick_children(lid)
            return bool(children) and bool(service.operator_waits_of(children[0]["session_id"]))

        await wait_until(asked, True)
        cid = (await subs.quick_children(lid))[0]["session_id"]
        assert (await manager.get_state(cid)).running  # type: ignore[union-attr]  # the same run, waiting in its call
        await service.control(group, "agent", note="done, go on")
        result = await asyncio.wait_for(call, 30)
        assert result["completed"], result
        assert "Paid after the operator solved the CAPTCHA" in result["answer"]
        # The helper's next request carried the give-back as the handoff's result, in the same run.
        handoff_result = [b for m in provider.requests[2].messages if m.role is MessageRole.tool for b in m.content_blocks]
        assert "gave the browser back" in str(handoff_result[-1].content) and "done, go on" in str(handoff_result[-1].content)
        assert len(provider.requests) == 4
        # The leader was never submitted to: no wake, no steer.
        assert lid not in submitted
    finally:
        await _close(app, tasks)


def test_the_helper_is_told_to_report_a_choice_and_wait_only_for_the_browser() -> None:
    brief = subagents_module.QUICK_BRIEF
    assert "Ask nobody questions" in brief and "BrowserHandoff" in brief and "do not end your turn after it" in brief
    assert "ask no questions" in subagents_module.QUICK_TASK_HEADER
