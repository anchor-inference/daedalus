"""The ``auto`` mode: each operator message sorted into quick, normal or long, for its own run only.

The sort sits in front of the operator's message, so it is measured for time as well as for being
right: a model that is slow or fails hands the decision to the heuristic within the timeout, and
the heuristic is held to a floor on a labelled set in both languages.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import AssistConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host import depth, prompts
from daedalus.host.session_runner import TENANT, SessionManager
from daedalus.host.transcript_view import message_view
from daedalus.stores.database import Database
from tests.support.models import DEFAULT_PRESET, model_config
from tests.unit.depth_labelled import HELD_OUT, LABELLED


def _accuracy(rows: list[tuple[str, str]]) -> float:
    return sum(depth.heuristic(text) == label for text, label in rows) / len(rows)


def test_the_heuristic_sorts_the_labelled_messages() -> None:
    assert len(LABELLED) >= 30 and len(HELD_OUT) >= 30
    for rows in (LABELLED, HELD_OUT):
        assert {label for _, label in rows} == set(depth.LEVELS)
        assert any(any("а" <= ch <= "я" for ch in text.lower()) for text, _ in rows)
    # Floors, not the measured figures (which are in the change's note): a later rule that trades
    # many of these for one new case should fail here rather than quietly move every message.
    assert _accuracy(LABELLED) >= 0.9
    assert _accuracy(HELD_OUT) >= 0.85


def test_each_level_names_a_mode_and_an_effort() -> None:
    assert depth.LEVEL_SETTINGS["normal"] == ("", "medium")
    assert depth.Depth("quick", "heuristic", 0).metadata() == {"level": "quick", "mode": "quick", "effort": "low", "source": "heuristic", "ms": 0}
    assert depth.Depth("long", "model", 3).mode == "deep" and depth.Depth("long", "model", 3).effort == "high"
    assert depth.parse_level("Long.") == "long" and depth.parse_level("**quick**") == "quick" and depth.parse_level("medium") is None


async def test_the_model_answers_when_it_answers_in_time() -> None:
    labels = dict(LABELLED + HELD_OUT)
    asked: list[str] = []

    async def fake(prompt: str) -> str:
        asked.append(prompt)
        await asyncio.sleep(0.005)
        return labels[prompt.removeprefix(depth.CLASSIFY_PROMPT)].upper() + "."

    started = time.monotonic()
    results = [await depth.classify(text, fake, timeout=0.9) for text, _ in LABELLED + HELD_OUT]
    elapsed = time.monotonic() - started
    assert all(result.source == "model" for result in results)
    assert [result.level for result in results] == [label for _, label in LABELLED + HELD_OUT]
    assert asked[0].startswith(depth.CLASSIFY_PROMPT)
    assert elapsed / len(results) < 0.2


async def test_a_slow_failing_or_confused_model_leaves_it_to_the_heuristic() -> None:
    async def slow(prompt: str) -> str:
        await asyncio.sleep(5)
        return "long"

    async def broken(prompt: str) -> str:
        raise RuntimeError("provider down")

    async def confused(prompt: str) -> str:
        return "I think this is a medium task"

    started = time.monotonic()
    result = await depth.classify("привет", slow, timeout=0.05)
    assert result.source == "heuristic" and result.level == "quick"
    assert time.monotonic() - started < 0.5
    assert (await depth.classify("implement the parser from scratch", broken, timeout=0.5)).level == "long"
    assert (await depth.classify("hi", confused, timeout=0.5)).source == "heuristic"
    assert (await depth.classify("", None, timeout=0.5)).level == "normal"


async def test_an_auto_message_runs_at_its_level_and_only_that_run(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("s")
        sid = state.session.id
        assert await manager.set_mode(sid, depth.AUTO_MODE) == depth.AUTO_MODE
        assert manager.mode_for(state) is None

        quick = await manager.sort_depth(state, "что такое CRDT?")
        assert quick.level == "quick" and quick.source == "heuristic"
        message = Message(role=MessageRole.user, content_blocks=[TextBlock(text="что такое CRDT?")], metadata={"daedalus.depth": quick.metadata()})
        engine = await manager._build_engine(state, "run-1", message=message)
        assert engine.config.reasoning_effort == "low"
        assert manager.mode_for(state) is not None and manager.mode_for(state).max_iterations == 25

        long = depth.Depth("long", "heuristic", 0)
        deep = await manager._build_engine(state, "run-2", message=Message(role=MessageRole.user, content_blocks=[TextBlock(text="go")], metadata={"daedalus.depth": long.metadata()}))
        assert deep.config.reasoning_effort == "high" and manager.mode_for(state).max_iterations == 400
        # The level's rules ride in the turn context: the system prompt is the same bytes at every level,
        # so a session that moves between levels keeps its provider cache.
        assert deep.config.system_prompt_sections == engine.config.system_prompt_sections
        assert not any("Mode: deep" in section for section in deep.config.system_prompt_sections)
        opened = await manager._with_turn_context(state, Message(role=MessageRole.user, content_blocks=[TextBlock(text="go")], metadata={"daedalus.depth": long.metadata()}))
        assert "Mode: deep" in opened.content_blocks[0].text and prompts.without_turn_context(opened.content_blocks[0].text) == "go"

        # A run opened by anything but a sorted message is the plain agent again.
        await manager._build_engine(state, "run-3", message=Message(role=MessageRole.user, content_blocks=[TextBlock(text="reminder")]))
        assert manager.mode_for(state) is None
        # The operator's own effort in the live controls is not touched by any of it.
        assert (await manager.live.load(sid)).get("reasoning_effort") is None
        assert message_view(message)["depth"]["level"] == "quick"
    finally:
        await manager.close()


async def test_the_operators_correction_is_spent_by_the_next_message(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("s")
        sid = state.session.id
        await manager.set_mode(sid, depth.AUTO_MODE)
        assert await manager.set_next_depth(sid, "long") == "long"
        stored = await manager.sessions.get(sid, TENANT)
        assert stored is not None and stored.metadata["depth_next"] == "long"
        chosen = await manager.sort_depth(state, "hi")
        assert chosen.level == "long" and chosen.source == "override"
        assert "depth_next" not in state.metadata
        assert (await manager.sort_depth(state, "hi")).level == "quick"
    finally:
        await manager.close()


async def test_a_configured_preset_is_asked_and_its_cost_is_labelled(settings: Settings, db: Database) -> None:
    config = model_config(assist=AssistConfig(depth_preset=DEFAULT_PRESET, depth_timeout_seconds=0.5))
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        state = await manager.create_session("s")
        calls: list[tuple[str, str]] = []

        async def small_model(prompt: str, preset: str, *, purpose: str, session_id: str | None = None, max_tokens: int = 200, temperature: float = 0.0) -> str:
            calls.append((preset, purpose))
            return "long"

        manager.small_model = small_model  # type: ignore[method-assign]
        result = await manager.sort_depth(state, "hi")
        assert result.level == "long" and result.source == "model" and calls == [(DEFAULT_PRESET, "depth")]
    finally:
        await manager.close()


async def test_new_chats_start_in_auto_and_the_route_sets_the_next_level(settings: Settings, db: Database) -> None:
    config = model_config()
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None)
        api = build_app(app, "tok")  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test", headers={"X-Daedalus-Token": "tok"}) as client:
            created = (await client.post("/api/sessions", json={"title": "chat"})).json()
            sid = created["id"]
            state = await manager.get_state(sid)
            assert state is not None and state.metadata["mode"] == depth.AUTO_MODE
            assert (await client.post(f"/api/sessions/{sid}/depth", json={"level": "quick"})).json() == {"depth_next": "quick"}
            assert state.metadata["depth_next"] == "quick"
            assert (await client.post(f"/api/sessions/{sid}/depth", json={"level": "huge"})).status_code == 400
            assert (await client.post("/api/sessions/nope/depth", json={"level": "quick"})).status_code == 404
            assert (await client.post(f"/api/sessions/{sid}/mode", json={"mode": "auto"})).json() == {"mode": "auto"}
    finally:
        await manager.close()


async def test_a_submitted_message_carries_its_level_into_the_run_and_the_transcript(settings: Settings, db: Database) -> None:
    from tests.unit.test_session_runner import ScriptedProvider, _manager, _wait_finished

    provider = ScriptedProvider([{"text": "four hundred and one"}])
    manager = await _manager(settings, db, provider)
    try:
        state = await manager.create_session("s")
        await manager.set_mode(state.session.id, depth.AUTO_MODE)
        waiter = asyncio.create_task(_wait_finished(manager))
        await manager.submit(state.session.id, "сколько будет 17 * 23?")
        await waiter
        sent = [m for m in await manager.sessions.list_transcript(state.session.id) if m.role is MessageRole.user]
        assert sent[-1].metadata["daedalus.depth"]["level"] == "quick"
        assert state.engine is not None and state.engine.config.reasoning_effort == "low"
    finally:
        await manager.close()
