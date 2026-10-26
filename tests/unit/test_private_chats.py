"""Private chats: kept like any chat, never learned from, indexed, handed memory or found from another chat.

Every check here asks the one predicate, :func:`daedalus.memory.privacy.is_private`, through the feature
that must honour it, with fakes in place of models and of Telegram."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from aiogram.filters import CommandObject
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import Message, MessageRole, Session, TextBlock

from daedalus.config import AssistConfig, RuntimeConfig, Settings
from daedalus.extensions import commands as slash
from daedalus.extensions.api import build_app
from daedalus.extensions.learning import Learning
from daedalus.host import predict, user_style
from daedalus.host.session_runner import SessionManager
from daedalus.memory import privacy
from daedalus.search.service import ConversationSearch
from daedalus.search.store import Index
from daedalus.speech.embedding_catalog import MODEL
from daedalus.stores.database import Database
from daedalus.stores.sqlite import SqliteSessionStore
from daedalus.transport.telegram.front import PRIVATE_MARK, TelegramFront
from tests.support.models import model_config
from tests.unit.test_front import RecordingBot, _message

TOKEN = "test-token"
HEADERS = {"X-Daedalus-Token": TOKEN}


def _user(text: str) -> Message:
    return Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": "operator"})


def _text(message: Message) -> str:
    return "".join(b.text for b in message.content_blocks if isinstance(b, TextBlock))


async def _manager(settings: Settings, db: Database, config: RuntimeConfig | None = None) -> SessionManager:
    manager = SessionManager(settings, config or model_config(), db=db)
    await manager.start()
    return manager


def test_the_predicate_reads_every_shape_a_caller_holds() -> None:
    assert privacy.is_private({"private": True})
    assert privacy.is_private('{"private": true}')
    assert privacy.is_private(SimpleNamespace(metadata={"private": True}))
    assert privacy.is_private(SimpleNamespace(session=SimpleNamespace(metadata={"private": True})))
    # Only the literal True: a stray value must not quietly switch the memory off.
    for value in (None, {}, {"private": "yes"}, {"private": 1}, "{}", "not json", '["private"]', SimpleNamespace()):
        assert not privacy.is_private(value)
    assert privacy.parent_ids({"subagent_of": "a", "btw_of": "b", "forked_from": {"session_id": "c", "seq": 3}}) == ["a", "b", "c"]


async def test_the_sql_condition_agrees_with_the_predicate(db: Database) -> None:
    store = SqliteSessionStore(db)
    await store.create(Session(id="open", tenant_id="daedalus", title="open"))
    await store.create(Session(id="closed", tenant_id="daedalus", title="closed", metadata={"private": True}))
    await store.create(Session(id="odd", tenant_id="daedalus", title="odd", metadata={"private": "yes"}))
    rows = await db.fetchall(f"SELECT s.id, s.metadata FROM sessions s WHERE {privacy.public_sessions_sql()} ORDER BY s.id")
    assert [r["id"] for r in rows] == ["odd", "open"]
    assert all(not privacy.is_private(r["metadata"]) for r in rows)


async def test_a_chat_is_made_private_through_the_api_and_says_so(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    app = SimpleNamespace(settings=settings, config=model_config(), db=db, manager=manager, front=None, extensions={})
    api = build_app(app, TOKEN)  # type: ignore[arg-type]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            made = await client.post("/api/sessions", json={"title": "between us", "private": True}, headers=HEADERS)
            assert made.status_code == 200, made.text
            plain = (await client.post("/api/sessions", json={"title": "ordinary"}, headers=HEADERS)).json()
            secret = made.json()["id"]
            assert (await client.get(f"/api/sessions/{secret}", headers=HEADERS)).json()["private"] is True
            assert (await client.get(f"/api/sessions/{plain['id']}", headers=HEADERS)).json()["private"] is False
            listing = (await client.get("/api/sessions", headers=HEADERS)).json()["sessions"]
            assert {row["id"]: bool(row["metadata"].get("private")) for row in listing} == {secret: True, plain["id"]: False}
        row = await db.fetchone("SELECT metadata FROM sessions WHERE id = ?", (secret,))
        assert json.loads(row["metadata"])["private"] is True
    finally:
        await manager.close()


async def test_what_is_made_from_a_private_chat_is_private_too(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        secret = await manager.create_session("secret", metadata={"private": True})
        plain = await manager.create_session("plain")
        await manager.sessions.append_transcript(secret.session.id, [_user("one"), _user("two")])
        sub = await manager.create_session("[sub] helper", metadata={"subagent_of": secret.session.id}, project_id=secret.project.id)
        side = await manager.create_session("side", metadata={"btw_of": secret.session.id}, project_id=secret.project.id)
        coach = await manager.create_session("coach", metadata={"prompt_coach_of": secret.session.id}, project_id=secret.project.id)
        other = await manager.create_session("[sub] helper", metadata={"subagent_of": plain.session.id}, project_id=plain.project.id)
        assert all(privacy.is_private(state) for state in (sub, side, coach))
        assert not privacy.is_private(other)
        # Past the end of the transcript: the whole conversation is copied.
        fork = await asyncio.wait_for(manager.fork(secret.session.id, 10_000), timeout=10)
        assert manager.is_private(fork["id"])
    finally:
        await manager.close()


async def test_the_memory_tools_refuse_in_a_private_chat_and_work_elsewhere(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        secret = (await manager.create_session("secret", metadata={"private": True})).session.id
        plain = (await manager.create_session("plain")).session.id
        remember, recall, forget = (manager.tools.get(name) for name in ("Remember", "Recall", "Forget"))
        assert all(isinstance(tool, privacy.PrivateChatGuard) for tool in (remember, recall, forget))
        assert remember.definition.name == "Remember"  # type: ignore[union-attr]
        note = {"text": "The bakery opens at seven.", "category": "fact", "topics": ["bakery"]}

        def ctx(session_id: str) -> ToolContext:
            return ToolContext(tenant_id="daedalus", run_id="r", session_id=session_id, metadata={"tool_call_id": "c1"})

        refused = await remember.invoke(ctx(secret), note)  # type: ignore[union-attr]
        assert refused.is_error and "private" in refused.content and refused.tool_call_id == "c1"
        assert refused.metadata == {"refused": "private_chat"}
        assert manager.memory.records("daedalus") == []
        saved = await remember.invoke(ctx(plain), note)  # type: ignore[union-attr]
        assert not saved.is_error, saved.content
        assert "bakery" in (await recall.invoke(ctx(plain), {"query": "bakery"})).content  # type: ignore[union-attr]
        hidden = await recall.invoke(ctx(secret), {"query": "bakery"})  # type: ignore[union-attr]
        assert hidden.is_error and "bakery" not in hidden.content
        forgotten = await forget.invoke(ctx(secret), {"memory_id": saved.metadata["memory_id"]})  # type: ignore[union-attr]
        assert forgotten.is_error and len(manager.memory.records("daedalus")) == 1
    finally:
        await manager.close()


async def test_a_private_chat_is_told_so_and_handed_no_project_facts(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = await _manager(settings, db)

    async def facts(_db: Any, _project: str) -> str:
        return "- Project facts: the oven runs hot"

    monkeypatch.setattr("daedalus.host.session_runner.project_facts_note", facts)
    try:
        secret = await manager.create_session("secret", metadata={"private": True})
        plain = await manager.create_session("plain")
        hello = Message(role=MessageRole.user, content_blocks=[TextBlock(text="hello")])
        said = _text(await manager._with_turn_context(secret, hello))
        assert privacy.TURN_NOTE in said and "oven" not in said
        said = _text(await manager._with_turn_context(plain, hello))
        assert privacy.TURN_NOTE not in said and "oven" in said
    finally:
        await manager.close()


async def test_a_private_chat_leaves_no_learning_record(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    config = RuntimeConfig()
    config.memory.extract_after_run = True
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={})
    learning = Learning(app)  # type: ignore[arg-type]
    extracted: list[str] = []

    async def extract(session_id: str, run_id: str) -> int:
        extracted.append(session_id)
        return 0

    learning.extract_memories = extract  # type: ignore[method-assign]
    try:
        for title, metadata in (("secret", {"private": True}), ("plain", {})):
            state = await manager.create_session(title, metadata=metadata)
            state.engine = SimpleNamespace(history=[_user(f"ask from {title}")])  # type: ignore[assignment]
            await learning.on_run_finished(state.session.id, f"run-{title}", "completed")
        rows = await db.fetchall("SELECT ask FROM learning_records")
        assert [r["ask"] for r in rows] == ["ask from plain"]
        assert len(extracted) == 1 and not manager.is_private(extracted[0])
    finally:
        await manager.close()


async def test_the_style_profile_never_reads_a_private_chat(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(assist=AssistConfig()), db=db)  # type: ignore[arg-type]
    await manager.start()
    asked: list[str] = []

    async def small_model(prompt: str, preset: str, **_: Any) -> str:
        asked.append(prompt)
        return "a guess\nSTYLE:\nterse"

    manager.small_model = small_model  # type: ignore[method-assign]
    try:
        secret = (await manager.create_session("secret", metadata={"private": True})).session.id
        plain = (await manager.create_session("plain")).session.id
        await manager.sessions.append_transcript(secret, [_user("my secret words")])
        await manager.sessions.append_transcript(plain, [_user("keep it short")])
        texts, through = await user_style.pending(db, 0)
        assert texts == ["keep it short"] and through > 0
        await user_style.save(db, "writes tersely", 0, 1)
        guesser = predict.Predictor(manager)
        await guesser._ask(secret, "", ["Operator: hi", "Agent: hello"])
        assert "writes tersely" not in asked[-1] and "my secret words" not in asked[-1]
        assert (await user_style.load(db)).summary == "writes tersely"  # the guess in a private chat taught nothing
    finally:
        await manager.close()


async def test_the_index_never_embeds_a_private_chat(db: Database, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SqliteSessionStore(db)
    await store.create(Session(id="open", tenant_id="daedalus", title="open chat"))
    await store.create(Session(id="closed", tenant_id="daedalus", title="closed chat", metadata={"private": True}))
    await store.append_transcript("open", [_user("a bicycle route along the river")])
    await store.append_transcript("closed", [_user("a secret route along the river")])
    index = Index(db)
    seqs = {r["session_id"]: r["seq"] for r in await db.fetchall("SELECT session_id, seq FROM transcript")}
    assert await index.private(seqs["closed"]) and not await index.private(seqs["open"])
    encoded: list[str] = []
    service = ConversationSearch(db, tmp_path)
    service.settings = {"mode": "local", "paused": False}
    service.encoder = SimpleNamespace(encode=lambda text, **_: encoded.append(text) or [1.0] * MODEL.dimension)
    monkeypatch.setattr(service, "reason", lambda: "ready")
    for _ in range(6):
        await service.step(passages=8, seconds=10)
    assert all("secret" not in text and text != "closed chat" for text in encoded)
    assert any("bicycle" in text for text in encoded) and "open chat" in encoded
    assert (await db.fetchone("SELECT count(*) n FROM search_pending"))["n"] == 0
    vectors = await db.fetchall("SELECT t.session_id FROM search_vectors v JOIN transcript t ON t.seq = v.seq")
    assert {r["session_id"] for r in vectors} == {"open"}
    titles = await db.fetchall("SELECT session_id FROM search_titles")
    assert [r["session_id"] for r in titles] == ["open"]


async def test_the_palette_starts_a_private_chat(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    app = SimpleNamespace(settings=settings, config=model_config(), db=db, manager=manager, front=None, extensions={})
    try:
        current = (await manager.create_session("s")).session.id
        made = await slash.run_command(app, current, "/private between us")  # type: ignore[arg-type]
        assert "private chat 'between us'" in made
        created = made.split("(", 1)[1].split(")", 1)[0]
        assert manager.is_private(created) and not manager.is_private(current)
    finally:
        await manager.close()


@pytest.fixture
async def front(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> TelegramFront:
    config = RuntimeConfig()
    manager = SessionManager(settings, config, db=db)
    await manager.start()

    async def save(cfg: RuntimeConfig) -> None:
        return None

    f = TelegramFront(settings, config, manager, save_config=save)
    f.bot = RecordingBot()  # type: ignore[assignment]
    yield f  # type: ignore[misc]
    for task in list(f._topic_status_tasks.values()):
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
    await manager.close()


def _answered(text: str, **kwargs: Any) -> tuple[Any, list[str]]:
    """A command message whose replies are recorded: a bound API call would need a real bot."""
    replies: list[str] = []
    message = _message(text, **kwargs)

    async def answer(reply: str, **_: Any) -> None:
        replies.append(reply)

    object.__setattr__(message, "answer", answer)
    return message, replies


async def test_telegram_private_opens_a_marked_topic(front: TelegramFront) -> None:
    front.config.telegram.forum_chat_id = -100
    message, replies = _answered("/private plans", chat_type="supergroup", chat_id=-100)
    await front.cmd_private(message, CommandObject(prefix="/", command="private", args="plans"))
    assert front.bot.topics == [f"{PRIVATE_MARK} plans"]  # type: ignore[attr-defined]
    binding = await front.binding_for_topic(-100, 10)
    assert binding is not None
    state = await front.manager.get_state(binding.session_id)
    assert state is not None and privacy.is_private(state) and state.session.title == "plans"
    assert "private chat" in replies[0]
    assert "private chat" in front.bot.sent[0]["text"]  # type: ignore[attr-defined]  # the topic's banner says so too
    await front.cmd_new(_message("/new open", chat_type="supergroup", chat_id=-100), CommandObject(prefix="/", command="new", args="open"))
    assert front.bot.topics[-1] == "open"  # type: ignore[attr-defined]


async def test_telegram_private_in_the_private_chat_marks_the_session(front: TelegramFront) -> None:
    message, replies = _answered("/private")
    await front.cmd_private(message, CommandObject(prefix="/", command="private", args=None))
    current = await front.current_session_id()
    assert current and front.manager.is_private(current)
    assert replies and "New private chat" in replies[0]
    listing, listed = _answered("/sessions")
    await front.cmd_sessions(listing)
    assert f"{PRIVATE_MARK} private" in listed[0]
