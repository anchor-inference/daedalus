"""WebGL in the agent's own browser: off unless the operator says yes, or allowed it beforehand.

The agent asks through ``BrowserOpen(webgl=…)``; the request goes on the operator's card like any
approval, their yes switches the profile's browser (the daemon restarts it with its tabs) and wakes the
agent, and nothing changes while it waits. Driven against the fake daemon, which answers
``profile.webgl`` as browserd does.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext
from pydantic import ValidationError

from daedalus.browser.agent import Caller, SensitiveAsk, webgl_line
from daedalus.browser.model import Owner, group_id
from daedalus.config import BrowserConfig, RuntimeConfig, Settings
from daedalus.extensions import browser as browser_extension
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.fake_browserd import FakeBrowserd
from tests.unit.test_browser_service import wait_until

WHY = "check that the three.js extension draws its scene"


@pytest.fixture
def base() -> Iterable[Path]:
    path = Path(tempfile.mkdtemp(prefix="bw-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def daemon(base: Path) -> AsyncIterator[FakeBrowserd]:
    fake = await FakeBrowserd(base / "b").start()
    yield fake
    await fake.stop()


@pytest.fixture
def runtime() -> RuntimeConfig:
    return RuntimeConfig(browser=BrowserConfig(control_wait_seconds=0.2))


class Rig:
    def __init__(self, manager: SessionManager, app: Any) -> None:
        self.manager = manager
        self.app = app
        self.told: list[tuple[Owner, str]] = []

        async def wake(owner: Owner, text: str) -> None:
            self.told.append((owner, text))

        self.service.wake = wake

    @property
    def service(self) -> Any:
        return self.app.extensions["browser"]

    async def session(self) -> str:
        state = await self.manager.create_session("chat")
        await self.manager.get_state(state.session.id)
        return state.session.id

    async def call(self, sid: str, tool_name: str, **arguments: Any) -> tuple[str, bool]:
        tool = self.manager.tools.get(tool_name)
        assert tool is not None, tool_name
        result = await tool.invoke(ToolContext(tenant_id="t", run_id="r", session_id=sid, metadata={"tool_call_id": "c"}), arguments)
        return str(result.content), bool(result.is_error)

    def pending(self, sid: str) -> dict[str, Any]:
        state = self.manager.live_state(sid)
        return dict(state.metadata.get("policy_pending") or {}) if state is not None else {}

    def allow(self, level: str) -> None:
        self.manager.config.browser.webgl_allow = level


@pytest.fixture
async def rig(settings: Settings, db: Database, base: Path, daemon: FakeBrowserd, runtime: RuntimeConfig) -> AsyncIterator[Rig]:
    configured = settings.model_copy(update={"browser_container_dir": base / "b", "browser_user_dir": None, "native": False})
    manager = SessionManager(configured, runtime, db=db)
    await manager.start()
    app = SimpleNamespace(settings=configured, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    tasks = await browser_extension.install(app)  # type: ignore[arg-type]
    assert await app.extensions["browser"].wait_available("container")
    yield Rig(manager, app)
    for task in tasks:
        task.cancel()
    await app.extensions["browser"].close()
    await manager.close()


async def _exit_reason(rig: Rig, browser: str) -> str:
    found = await rig.service.db.fetchone("SELECT reason FROM browsers WHERE id = ?", (browser,))
    return str(found["reason"]) if found is not None else ""


def profile_asked(daemon: FakeBrowserd) -> str:
    """The profile the host last asked the daemon about: a chat's is its own project's."""
    return next(str(params["profile"]) for method, params in reversed(daemon.calls) if method == "profile.webgl")


def mode_changes(daemon: FakeBrowserd) -> list[str]:
    return [str(params["mode"]) for method, params in daemon.calls if method == "profile.webgl" and params.get("mode")]


async def asked(rig: Rig, sid: str, mode: str = "software") -> str:
    text, failed = await rig.call(sid, "BrowserOpen", webgl=mode, why=WHY)
    assert failed and "Waiting for the operator" in text and "End your turn" in text, text
    (key, request), = rig.pending(sid).items()
    assert key in text
    assert request["on_answer"] == "browser.webgl" and request["webgl"] == mode
    assert WHY in request["text"] and "restarts in that mode, keeping its tabs" in request["text"]
    return str(key)


# -- asking ------------------------------------------------------------------------------------------


async def test_the_agents_ask_changes_nothing_until_the_operator_says_yes(rig: Rig, daemon: FakeBrowserd) -> None:
    sid = await rig.session()
    opened, failed = await rig.call(sid, "BrowserOpen")
    assert not failed, opened
    key = await asked(rig, sid)
    assert mode_changes(daemon) == [] and daemon.webgl.get(profile_asked(daemon), "off") == "off"
    # The yes is the operator's alone: an orchestrator answering it is refused and nothing changes.
    with pytest.raises(ValueError, match="only the operator"):
        await rig.manager.grant(sid, key, via="orchestrator")
    assert mode_changes(daemon) == [] and key in rig.pending(sid)


async def test_the_operators_yes_restarts_the_browser_in_the_mode_and_keeps_its_group(rig: Rig, daemon: FakeBrowserd) -> None:
    sid = await rig.session()
    await rig.call(sid, "BrowserOpen", url="https://example.com/scene")
    gid = group_id(Owner("session", sid))
    before = daemon.groups[gid].browser_id
    key = await asked(rig, sid)
    await rig.manager.grant(sid, key, via="app")
    assert mode_changes(daemon) == ["software"]
    after = daemon.groups[gid].browser_id
    assert after != before and [t.page.url for t in daemon.groups[gid].tabs] == ["https://example.com/scene"]
    # The relaunch's exit closed no row: the group is the same open one, on the new browser.
    async def row() -> tuple[str, str]:
        current = await rig.service.get_row(gid)
        return current["status"], current["browser_id"]

    await wait_until(row, ("open", after), timeout=5)
    # Once the old browser's exit has been heard, the row is still the open one.
    await wait_until(lambda: _exit_reason(rig, before), "relaunch", timeout=5)
    assert await row() == ("open", after)
    # No grant is left for the agent to spend, and the agent hears the answer with the browser's word.
    state = rig.manager.live_state(sid)
    assert state is not None and not state.metadata.get("policy_grants") and not rig.pending(sid)
    owner, text = rig.told[-1]
    assert owner.session_id == sid and "allowed software WebGL" in text and "restarted in that mode with your tabs" in text and "SwiftShader" in text
    # Asking for the mode it now has only opens the browser, and says how it draws.
    text, failed = await rig.call(sid, "BrowserOpen", webgl="software", why=WHY)
    assert not failed and "WebGL: software — pages get WebGL" in text, text
    assert mode_changes(daemon) == ["software"] and not rig.pending(sid)


async def test_a_yes_before_the_browser_runs_keeps_the_mode_for_its_start(rig: Rig, daemon: FakeBrowserd) -> None:
    sid = await rig.session()
    key = await asked(rig, sid)
    await rig.manager.grant(sid, key, via="app")
    assert daemon.webgl[profile_asked(daemon)] == "software" and not daemon.browsers
    assert "starts in that mode when you open it" in rig.told[-1][1]
    text, failed = await rig.call(sid, "BrowserOpen")
    assert not failed and daemon.browser_webgl[next(iter(daemon.browsers))] == "software", text


async def test_the_operators_no_leaves_the_browser_as_it_was(rig: Rig, daemon: FakeBrowserd) -> None:
    sid = await rig.session()
    key = await asked(rig, sid, "software")
    await rig.manager.refuse(sid, key, via="app")
    assert mode_changes(daemon) == [] and not rig.pending(sid)
    assert "declined software WebGL" in rig.told[-1][1]


async def test_an_ask_says_why_or_is_refused(rig: Rig, daemon: FakeBrowserd) -> None:
    sid = await rig.session()
    text, failed = await rig.call(sid, "BrowserOpen", webgl="software")
    assert failed and "why says in a sentence" in text and not rig.pending(sid)
    text, failed = await rig.call(sid, "BrowserOpen", webgl="on", why=WHY)
    assert failed and "webgl is one of off, software, gpu" in text
    text, failed = await rig.call(sid, "BrowserOpen", webgl="software", fresh=True, why=WHY)
    assert failed and "throwaway browser" in text and not rig.pending(sid)


# -- the operator's standing permission -------------------------------------------------------------


async def test_a_standing_permission_lets_its_level_through_and_asks_above_it(rig: Rig, daemon: FakeBrowserd) -> None:
    daemon.gpu = (True, "")
    await rig.service._refresh_info(rig.service.links["container"])  # noqa: SLF001 — the daemon's report read again, as after a browser start
    rig.allow("software")
    sid = await rig.session()
    text, failed = await rig.call(sid, "BrowserOpen", webgl="software")
    assert not failed and "WebGL: software — pages get WebGL" in text, text
    assert mode_changes(daemon) == ["software"] and not rig.pending(sid)
    # GPU is above the permission: it asks.
    await asked(rig, sid, "gpu")
    assert mode_changes(daemon) == ["software"]
    rig.allow("gpu")
    text, failed = await rig.call(sid, "BrowserOpen", webgl="gpu")
    assert not failed and "WebGL: gpu — pages get WebGL, drawn by ANGLE (NVIDIA" in text, text


async def test_turning_webgl_off_never_asks(rig: Rig, daemon: FakeBrowserd) -> None:
    sid = await rig.session()
    await rig.call(sid, "BrowserOpen")
    daemon.webgl[daemon.groups[group_id(Owner("session", sid))].profile] = "software"
    text, failed = await rig.call(sid, "BrowserOpen", webgl="off")
    assert not failed and "WebGL: off — pages get no WebGL" in text, text
    assert mode_changes(daemon) == ["off"] and not rig.pending(sid)


async def test_gpu_where_no_gpu_is_passed_in_is_said_plainly_and_nothing_is_asked(rig: Rig, daemon: FakeBrowserd) -> None:
    rig.allow("gpu")
    sid = await rig.session()
    text, failed = await rig.call(sid, "BrowserOpen", webgl="gpu", why=WHY)
    assert failed and "GPU WebGL is not available here: no GPU render node" in text and "webgl='software'" in text, text
    assert mode_changes(daemon) == [] and not rig.pending(sid)


async def test_a_command_line_member_cannot_ask_but_a_standing_permission_still_holds(rig: Rig, daemon: FakeBrowserd) -> None:
    async def never(ask: SensitiveAsk) -> tuple[bool, str]:
        raise AssertionError("nothing here should ask")

    agent = rig.app.extensions["browser_agent"]
    caller = Caller(owner=Owner("staff", "m1", staff_id="m1"), actor="staff:m1", gate=never, files=None)  # type: ignore[arg-type]
    text, failed = await agent.run("BrowserOpen", {"webgl": "software", "why": WHY}, caller)
    assert failed and "a command-line member cannot ask them" in text and mode_changes(daemon) == []
    rig.allow("software")
    # This rig has no team, so the open after the switch finds no member m1; the switch is what counts.
    await agent.run("BrowserOpen", {"webgl": "software"}, caller)
    assert mode_changes(daemon) == ["software"] and daemon.webgl["staff-m1"] == "software"


# -- what the operator sees --------------------------------------------------------------------------


async def test_the_environment_listing_carries_what_the_daemon_offers(rig: Rig, daemon: FakeBrowserd) -> None:
    (container,) = [e for e in rig.service.environments() if e["env"] == "container"]
    assert container["webgl"] == {"modes": ["off", "software"], "gpu": False, "gpu_reason": daemon.gpu[1]}


def test_the_standing_permission_is_one_of_three_levels_and_asks_by_default() -> None:
    assert BrowserConfig().webgl_allow == "ask"
    for level in ("ask", "software", "gpu"):
        assert BrowserConfig(webgl_allow=level).webgl_allow == level  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        BrowserConfig(webgl_allow="always")  # type: ignore[arg-type]


def test_the_line_on_how_the_browser_draws_tells_a_chosen_mode_from_a_working_one() -> None:
    assert webgl_line({"mode": "software", "graphics": None}) == "WebGL: software (the browser starts in this mode when it next opens)."
    working = webgl_line({"mode": "gpu", "graphics": {"available": True, "renderer": "ANGLE (NVIDIA, Vulkan)", "webgl": "enabled"}})
    assert working == "WebGL: gpu — pages get WebGL, drawn by ANGLE (NVIDIA, Vulkan)."
    broken = webgl_line({"mode": "gpu", "graphics": {"available": False, "renderer": "", "webgl": "disabled_off"}})
    assert "was chosen, but the browser reports no WebGL (disabled_off" in broken
    assert "BrowserOpen(webgl='software')" in webgl_line({"mode": "off", "graphics": {"available": False, "webgl": "disabled_off"}})
