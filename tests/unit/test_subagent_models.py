"""Which presets a session's helpers may run on: the default, the session's own choice, and what a child inherits."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import ModelPresetConfig, ProviderConfig, RuntimeConfig, Settings
from daedalus.extensions import orchestrator_ops, orchestrator_team
from daedalus.extensions.subagents import Subagents
from daedalus.host import subagent_models
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.unit.test_subagents import _capture

PRESETS = ("local.a", "local.b", "local.c")


def _config(mode: str = "all", presets: list[str] | None = None) -> RuntimeConfig:
    runtime = RuntimeConfig(
        providers={"local": ProviderConfig(kind="openai_compat", base_url="http://127.0.0.1:9/v1")},
        presets={pid: ModelPresetConfig(provider="local", model=pid.split(".")[1]) for pid in PRESETS},
        model={"preset": "local.a"},
    )
    runtime.subagents.models.mode = mode  # type: ignore[assignment]
    runtime.subagents.models.presets = list(presets or [])
    return runtime


def test_the_three_modes_and_what_they_refuse() -> None:
    every = subagent_models.allowance("all", [], available=PRESETS, session_preset="local.a")
    assert every.presets == PRESETS and not every.limited and every.inherited() is None
    own = subagent_models.allowance("session", [], available=PRESETS, session_preset="local.b")
    assert own.presets == ("local.b",) and own.leader_model
    own.check(None)
    with pytest.raises(ValueError, match="'local.c' is not allowed .* allowed: local.b"):
        own.check("local.c")
    # A deleted or unreachable preset drops out of a chosen list rather than breaking it.
    chosen = subagent_models.allowance("chosen", ["local.c", "gone.x"], available=PRESETS, session_preset="local.a")
    assert chosen.presets == ("local.c",) and not chosen.leader_model
    with pytest.raises(ValueError, match="do not include its own model"):
        chosen.check(None)
    assert chosen.inherited() == {"mode": "chosen", "presets": ["local.c"]}
    # The session's own choice wins over the default; without one the default holds.
    default = SimpleNamespace(mode="session", presets=[])
    assert subagent_models.choice({}, default) == ("session", [], True)
    assert subagent_models.choice({subagent_models.KEY: {"mode": "chosen", "presets": ["local.b"]}}, default) == ("chosen", ["local.b"], False)


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, _config(), db=db)
    await manager.start()
    yield SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={})
    await manager.close()


async def test_two_allowed_models_refuse_a_third_and_the_list_shows_only_them(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, _config(), db=db)
    await manager.start()
    try:
        subs = Subagents(SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}))  # type: ignore[arg-type]
        leader = await manager.create_session("lead")
        lid = leader.session.id
        _capture(manager)
        await manager.set_subagent_models(lid, "chosen", ["local.a", "local.b"])

        with pytest.raises(ValueError, match="'local.c' is not allowed for helpers of this session; allowed: local.a, local.b"):
            await subs.spawn(leader_id=lid, task="t", model="local.c")
        # What SubAgentList prints, and what the environment tells the model.
        assert await subs.service("models", leader_id=lid) == ["local.a", "local.b"]
        notes = manager.notes_for(await manager.get_state(lid), overrides=await manager.live.load(lid))  # type: ignore[arg-type]
        assert "Models SubAgent accepts (preset ids): local.a, local.b" in notes and "local.c" not in notes

        # The child carries the list as its own: nothing it starts can reach further.
        result = await subs.spawn(leader_id=lid, task="t", model="local.b")
        child = await manager.get_state(result["session_id"])
        assert child is not None and child.metadata[subagent_models.KEY] == {"mode": "chosen", "presets": ["local.a", "local.b"]}
    finally:
        await manager.close()

    # Survives a restart: it is in the session's stored metadata.
    again = SessionManager(settings, _config(), db=db)
    await again.start()
    try:
        state = await again.get_state(lid)
        assert state is not None
        assert again.subagent_allowance(state.metadata, await again.live.load(lid)).presets == ("local.a", "local.b")
        # Back to the default from Settings, which here is every model.
        await again.set_subagent_models(lid, None, [])
        state = await again.get_state(lid)
        assert again.subagent_allowance(state.metadata, {}).presets == PRESETS  # type: ignore[union-attr]
    finally:
        await again.close()


async def test_the_global_default_holds_until_a_session_chooses(app: Any) -> None:
    manager: SessionManager = app.manager
    manager.reload_config(_config("session"))
    app.config = manager.config
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    _capture(manager)
    await manager.live.set_model(leader.session.id, preset="local.b")
    with pytest.raises(ValueError, match="allowed: local.b"):
        await subs.spawn(leader_id=leader.session.id, task="t", model="local.a")
    assert (await subs.spawn(leader_id=leader.session.id, task="t"))["session_id"]
    await manager.set_subagent_models(leader.session.id, "all", [])
    assert (await subs.spawn(leader_id=leader.session.id, task="t", model="local.a"))["session_id"]


async def test_a_hire_is_held_to_the_hiring_sessions_list(app: Any) -> None:
    manager: SessionManager = app.manager
    leader = await manager.create_session("orchestrator")
    await manager.set_subagent_models(leader.session.id, "chosen", ["local.a"])
    orch = SimpleNamespace(manager=manager)
    with pytest.raises(orchestrator_ops.Refused, match="'local.c' is not allowed"):
        await orchestrator_team._helper_model(orch, leader.session.id, "local.c")  # type: ignore[arg-type]
    await orchestrator_team._helper_model(orch, leader.session.id, "local.a")  # type: ignore[arg-type]
    await orchestrator_team._helper_model(orch, leader.session.id, "")  # type: ignore[arg-type]


async def test_the_session_route_reads_saves_and_returns_to_the_default(settings: Settings, db: Database) -> None:
    import httpx

    from daedalus.extensions.api import build_app

    manager = SessionManager(settings, _config(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    headers = {"X-Daedalus-Token": "tok"}
    try:
        sid = (await manager.create_session("chat")).session.id
        url = f"/api/sessions/{sid}/subagent-models"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
            view = (await client.get(url, headers=headers)).json()
            assert (view["mode"], view["inherited"], view["allowed"], view["session_preset"]) == ("all", True, list(PRESETS), "local.a")
            view = (await client.put(url, json={"mode": "chosen", "presets": ["local.b", "local.c"]}, headers=headers)).json()
            assert (view["mode"], view["inherited"], view["allowed"]) == ("chosen", False, ["local.b", "local.c"])
            assert (await client.put(url, json={"mode": "chosen", "presets": ["nope"]}, headers=headers)).status_code == 422
            view = (await client.put(url, json={"mode": None}, headers=headers)).json()
            assert (view["mode"], view["inherited"]) == ("all", True)
            assert (await client.get("/api/sessions/missing/subagent-models", headers=headers)).status_code == 404
    finally:
        await manager.close()
