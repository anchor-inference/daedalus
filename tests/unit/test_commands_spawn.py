"""Slash commands from the Mini App, and agents that create standing agents."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import commands as slash
from daedalus.extensions.board import Board
from daedalus.extensions.notifications import NotificationService
from daedalus.extensions.peers import Peers
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.chat import spawn_agent
from tests.support.models import model_config


def _app(settings: Settings, db: Database, manager: SessionManager, config: RuntimeConfig) -> Any:
    """What ``run_command`` reads of the Application, with nothing behind it but the manager."""

    async def save_config(cfg: RuntimeConfig, *, expected_revision: str | None = None) -> None:
        app.config = cfg

    async def create_session(title: str, **kw: Any) -> Any:
        return await manager.create_session(title, **kw)

    app = SimpleNamespace(
        settings=settings,
        config=config,
        db=db,
        manager=manager,
        front=None,
        extensions={},
        guard=None,
        save_config=save_config,
        create_session=create_session,
    )
    return app


def test_parse_recognises_commands() -> None:
    assert slash.parse("/compact focus on files") == ("compact", "focus on files")
    assert slash.parse("/Model@daedalus_bot") == ("model", "")
    assert slash.parse("plain text") is None and slash.parse("/") is None and slash.parse("/not a command!") == ("not", "a command!")
    assert {c.name for c in slash.COMMANDS} >= {"compact", "cap", "brief", "usage", "doctor", "inbox"}


async def test_commands_run_without_the_chat_front(settings: Settings, db: Database) -> None:
    config = model_config()
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    app = _app(settings, db, manager, config)
    app.notifications = NotificationService(db, manager.bus)
    state = await manager.create_session("s")
    sid = state.session.id

    async def run(line: str) -> str:
        return await slash.run_command(app, sid, line)

    assert "Session cap: $2.00" in await run("/cap 2")
    assert state.metadata["usd_cap"] == 2.0
    assert "removed" in await run("/cap none")
    assert "No brief" in await run("/brief")
    assert "updated" in await run("/brief You keep the changelog.")
    assert "You keep the changelog." in manager.notes_for(state) and "Brief:" in await run("/brief")
    assert "Working rules" in await run("/prompt")
    assert "nothing unread" in await run("/inbox")
    assert "No sessions with closed topics" in await run("/cleanup")
    with pytest.raises(KeyError):
        await run("/nonsense")
    with pytest.raises(RuntimeError, match="Telegram chat itself"):
        await run("/bind")  # the chat's own commands are named, not answered with "unknown command"
    await manager.close()


async def test_the_chat_commands_are_not_the_chat_s_to_run(settings: Settings, db: Database) -> None:
    """Everything the Mini App offers runs on an installation with no bot token at all."""
    config = model_config()
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    app = _app(settings, db, manager, config)
    state = await manager.create_session("s")
    sid = state.session.id

    async def run(line: str) -> str:
        return await slash.run_command(app, sid, line)

    assert "Nothing is running" in await run("/stop")

    preset_id = next(iter(config.presets))
    assert "models:" in await run("/model") and preset_id in await run("/model")
    assert "Session model:" in await run(f"/model {preset_id}")
    assert "global default" in await run("/model default")
    assert "usage: /model" in await run("/model nosuchprovider/x")
    assert "thinking=True effort=high" in await run("/thinking high")
    assert "thinking=True effort=xhigh" in await run("/thinking xhigh")
    assert "usage: /thinking" in await run("/thinking perhaps")
    assert "xhigh" in await run("/thinking perhaps")
    assert "mode: default" in await run("/mode")
    assert "no such mode" in await run("/mode nonesuch")

    assert "Renamed to: kept" in await run("/rename kept")
    renamed = await manager.get_state(sid)
    assert renamed is not None and renamed.session.title == "kept"

    made = await run("/new second")
    second = made.rsplit("(", 1)[1].rstrip(").")
    assert "New session 'second'" in made and await manager.get_state(second) is not None

    listed = await run("/sessions")
    assert "kept" in listed and "second" in listed
    assert "Idle" in await run("/status")
    usage = await run("/usage")
    assert usage.startswith("today: 0 calls") and "this session: 0 calls" in usage
    settings_text = await run("/settings")
    assert "model:" in settings_text and "no Telegram front" in settings_text

    assert "verbosity=2" in await run("/verbosity 2") and app.config.telegram.verbosity == 2
    assert "usage: /verbosity" in await run("/verbosity loud")
    assert "approval=auto" in await run("/approval auto") and app.config.self_change.approval == "auto"

    assert f"Session {second} deleted" in await run(f"/delete {second}")
    assert await manager.get_state(second) is None
    assert "no such session" in await run("/delete nope")
    await manager.close()


async def test_the_palette_offers_only_what_is_installed(settings: Settings, db: Database) -> None:
    config = model_config()
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    app = _app(settings, db, manager, config)
    state = await manager.create_session("s")
    sid = state.session.id

    async def run(line: str) -> str:
        return await slash.run_command(app, sid, line)

    bare = {c.name for c in slash.available(app)}
    assert {"compact", "stop", "model", "usage", "settings", "delete"} <= bare
    assert bare.isdisjoint({"board", "peer", "loop", "inbox", "rebuild", "rollback", "schedules"})
    assert "The board is not installed" in await run("/board")
    assert "The scheduler is not installed" in await run("/schedules")
    assert "Self-development is not installed" in await run("/rebuild")

    app.extensions["board"] = Board(app)
    app.extensions["peers"] = Peers(app)
    assert {"board", "peer"} <= {c.name for c in slash.available(app)}
    assert "the board is empty" in await run("/board")
    assert "peer 'alpha'" in await run("/peer here alpha")
    assert "alpha" in await run("/peer")
    assert "forgotten" in await run("/peer forget alpha")

    done: list[tuple[str, Any]] = []

    async def rebuild(reason: str) -> str:
        done.append(("rebuild", reason))
        return "rebuilding"

    async def rollback(steps: int) -> str:
        done.append(("rollback", steps))
        return "rolled back"

    app.extensions["selfdev"] = SimpleNamespace(rebuild=rebuild, rollback=rollback)
    assert await run("/rebuild") == "rebuilding" and done[-1] == ("rebuild", "operator request")
    assert await run("/rollback 2") == "rolled back" and done[-1] == ("rollback", 2)
    assert "usage: /rollback" in await run("/rollback soon")
    await manager.close()


async def test_spawn_agent_tool_hands_over_brief_files_and_settings(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    parent = await manager.create_session("parent")
    (parent.workspace / "howto.md").write_text("step 1")
    calls: list[dict[str, Any]] = []

    async def fake_spawn(**kw: Any) -> str:
        calls.append(kw)
        return "child123"

    parent.services.spawn_agent = fake_spawn  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r", session_id=parent.session.id)
    missing = await spawn_agent().invoke(ctx, {"title": "Changelog keeper", "brief": "Keep it.", "files": ["nope.md"]})
    assert missing.is_error and "do not exist" in missing.content
    empty = await spawn_agent().invoke(ctx, {"title": "x", "brief": "  "})
    assert empty.is_error
    result = await spawn_agent().invoke(ctx, {"title": "Changelog keeper", "brief": "Keep the changelog current.", "files": ["howto.md"], "peer_name": "changelog", "mode": "careful", "mcp": ["postingboard"]})
    assert not result.is_error and result.metadata["session_id"] == "child123" and "peer 'changelog'" in result.content
    assert calls[0]["files"] == [str(parent.workspace / "howto.md")] and calls[0]["mode"] == "careful" and calls[0]["mcp"] == ["postingboard"]
    await manager.close()


async def test_self_propose_failure_is_an_error_result(settings: Settings, db: Database) -> None:
    from daedalus.tools.selfdev import self_propose

    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    state = await manager.create_session("p")

    async def refused(**kw: Any) -> str:
        raise RuntimeError("proposal failed: remote: Permission to x/y.git denied to me. 403")

    state.services.self_propose = refused  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r", session_id=state.session.id)
    result = await self_propose().invoke(ctx, {"repo": "bot", "title": "t", "summary": "s"})
    assert result.is_error and "proposal failed" in result.content and "write access" in result.content
    await manager.close()


def test_core_compaction_trigger_stays_below_the_output_reservation() -> None:
    from daedalus.host.engine_factory import runtime_constants

    config = model_config()
    rc = runtime_constants(config, context_window=128_000, max_output_tokens=32_000, thinking=False, mode=None)
    assert rc.compaction_trigger_ratio == 0.6  # 1 − 32k/128k − 0.15: a turn of tool results below the vLLM cliff, under the 0.85 default
    rc = runtime_constants(config, context_window=128_000, max_output_tokens=4_000, thinking=False, mode=None)
    assert rc.compaction_trigger_ratio == 0.82  # 1 − 4k/128k − 0.15: still below the 0.85 default
