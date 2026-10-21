"""A side question (``/btw``): it reads its chat's conversation, changes nothing, leaves the chat's history
alone, answers while the chat's run goes on, and no list shows it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResultBlock, ToolUseBlock

from daedalus.config import BTW_TOOLS, Settings
from daedalus.extensions import btw as btw_extension
from daedalus.extensions.api import build_app
from daedalus.host import btw, prompt_coach, prompts
from daedalus.host.btw import BTW_KEY, BtwRefused, lent_history, open_btw
from daedalus.host.engine_factory import TENANT
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.sqlite import LENT_KEY
from tests.support.models import model_config
from tests.unit.test_session_runner import ScriptedProvider
from tests.unit.test_steer_queue import _held_tool

H = {"X-Daedalus-Token": "tok"}


@pytest.fixture
async def manager(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    manager.submitted = []  # type: ignore[attr-defined]

    async def fake_submit(session_id: str, text: str, attachments: Any = (), **_: Any) -> str:
        manager.submitted.append((session_id, text))  # type: ignore[attr-defined]
        return "run-x"

    manager.submit = fake_submit  # type: ignore[method-assign]
    yield manager
    await manager.close()


def _said(role: MessageRole, text: str) -> Message:
    return Message(role=role, content_blocks=[TextBlock(text=text)])


async def _chat_with_history(manager: SessionManager) -> Any:
    chat = await manager.create_session("Bakery site", own_directory=True)
    history = [_said(MessageRole.user, "rebuild the menu page"), _said(MessageRole.assistant, "I moved the prices into menu.json")]
    await manager.sessions.replace_messages(chat.session.id, TENANT, history)
    return chat


def _texts(messages: Any) -> list[str]:
    return [b.text for m in messages for b in m.content_blocks if isinstance(b, TextBlock)]


# -- what it reads and what it may call ------------------------------------------------------------


async def test_a_side_question_reads_the_chats_conversation_and_may_only_look(manager: SessionManager) -> None:
    chat = await _chat_with_history(manager)
    opened = await open_btw(manager, chat.session.id, "where did the prices go?")
    side = await manager.get_state(opened["session_id"])
    assert side is not None and manager.is_btw(side) and manager.lives_in_window(side)
    assert side.metadata[BTW_KEY] == chat.session.id
    lent = await manager.sessions.list_messages(side.session.id, TENANT)
    assert _texts(lent) == ["rebuild the menu page", "I moved the prices into menu.json"]
    assert all(m.metadata[LENT_KEY] == chat.session.id for m in lent)
    assert manager.submitted == [(side.session.id, "where did the prices go?")]  # type: ignore[attr-defined]
    known = {t.name for t in manager.tools.list_all()}
    assert manager.blocked_tools_for(side) == known - set(BTW_TOOLS)
    for name in ("Write", "Edit", "Exec", "AskUser", "SavePrompt", "SubAgent", "WebFetch", "Notify", "Remember"):
        assert name in manager.blocked_tools_for(side), name
    # Its folder is the chat's, read-only.
    assert side.workspace == chat.workspace
    assert side.services.walls is not None and side.services.walls.writable == ()


async def test_the_side_question_is_told_what_it_is_and_answers_nobody_in_the_lists(manager: SessionManager) -> None:
    chat = await _chat_with_history(manager)
    side = await manager.get_state((await open_btw(manager, chat.session.id, "why?"))["session_id"])
    sections = prompts.btw_sections(answer_language="", workspace=str(side.workspace))
    assert "side question" in sections[0] and str(side.workspace) in sections[0]
    assert "Daedalus, a personal autonomous agent" not in "".join(sections), "not the working agent's persona"
    side.run_origin = "operator"
    assert not manager._operator_facing(side)
    assert set(manager.tool_group_loads_for(side).values()) == {"eager"}
    listed = {row["id"] for row in await manager.list_sessions()}
    assert chat.session.id in listed and side.session.id not in listed
    assert prompt_coach.hidden(side.session.metadata)
    assert side.session.id not in {user["id"] for user in await manager.workspace_users(chat.workspace)}


def test_a_copy_taken_mid_step_ends_where_it_can_be_answered() -> None:
    asked = Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="a", name="Read", arguments_json="{}"), ToolUseBlock(tool_call_id="b", name="Read", arguments_json="{}")])
    half = Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="a", content="one")])
    whole = Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="b", content="two")])
    start = _said(MessageRole.user, "start")
    assert _texts(lent_history([start, asked], "chat")) == ["start"]
    assert len(lent_history([start, asked, half], "chat")) == 1, "a batch with a result missing goes whole"
    assert len(lent_history([start, asked, half, whole], "chat")) == 4
    original = _said(MessageRole.user, "mine")
    lent_history([original], "chat")
    assert LENT_KEY not in original.metadata, "the chat's own message is never marked"


# -- the window and the chat -----------------------------------------------------------------------


async def test_a_new_question_replaces_the_last_and_closing_or_deleting_the_chat_takes_it(manager: SessionManager) -> None:
    chat = await _chat_with_history(manager)
    first = (await open_btw(manager, chat.session.id, "one"))["session_id"]
    second = (await open_btw(manager, chat.session.id, "two"))["session_id"]
    assert first != second and await manager.get_state(first) is None
    assert await btw.btws_of(manager, chat.session.id) == [second]
    assert await btw.close_btw(manager, chat.session.id) == 1
    assert await manager.get_state(second) is None and (chat.workspace).exists(), "the chat's directory stays"
    third = (await open_btw(manager, chat.session.id, "three"))["session_id"]
    app = SimpleNamespace(manager=manager)
    await btw_extension.install(app)  # type: ignore[arg-type]
    await manager.delete_session(chat.session.id)
    assert await manager.get_state(third) is None


async def test_a_side_question_is_asked_about_a_chat_only(manager: SessionManager) -> None:
    chat = await _chat_with_history(manager)
    side = (await open_btw(manager, chat.session.id, "one"))["session_id"]
    with pytest.raises(BtwRefused):
        await open_btw(manager, side, "about the side question itself")
    with pytest.raises(BtwRefused):
        await open_btw(manager, chat.session.id, "   ")
    with pytest.raises(KeyError):
        await open_btw(manager, "nope", "anything")


async def test_the_routes_open_and_close_it(settings: Settings, db: Database, manager: SessionManager) -> None:
    chat = await _chat_with_history(manager)
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        opened = await client.post(f"/api/sessions/{chat.session.id}/btw", json={"question": "what changed?"}, headers=H)
        assert opened.status_code == 200, opened.text
        side = opened.json()["btw"]["session_id"]
        assert opened.json()["btw"]["parent_id"] == chat.session.id
        assert (await client.post(f"/api/sessions/{chat.session.id}/btw", json={"question": ""}, headers=H)).status_code == 422
        assert (await client.post("/api/sessions/nope/btw", json={"question": "x"}, headers=H)).status_code == 404
        assert (await client.post(f"/api/sessions/{side}/btw", json={"question": "x"}, headers=H)).status_code == 400
        detail = (await client.get(f"/api/sessions/{side}", headers=H)).json()
        assert detail["btw_of"] == chat.session.id
        closed = await client.delete(f"/api/sessions/{chat.session.id}/btw", headers=H)
        assert closed.json() == {"deleted": 1}
        assert await manager.get_state(side) is None


# -- a real run beside a busy chat -----------------------------------------------------------------


async def test_it_answers_while_the_chat_works_and_leaves_the_chat_alone(settings: Settings, db: Database) -> None:
    # The chat's first request holds its run inside a tool; the side question's request takes the second
    # answer while it is held; the chat's run then finishes on the third.
    provider = ScriptedProvider([_held_tool(settings), {"text": "the prices are in menu.json"}, {"text": "done"}])
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    manager.providers.rungs_for = lambda config, preset_id=None: [(provider, "scripted-model")]  # type: ignore[method-assign]
    try:
        chat = await manager.create_session("Bakery site", own_directory=True)
        await manager.submit(chat.session.id, "rebuild the menu page")
        for _ in range(400):
            if provider.requests:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        assert chat.running, "the chat is busy in its tool"
        opened = await open_btw(manager, chat.session.id, "where did the prices go?")
        side = await manager.get_state(opened["session_id"])
        assert side is not None and side.task is not None
        await side.task
        assert chat.running, "the side question neither waited for nor stopped the chat's run"
        request = provider.requests[1]
        seen = "\n".join(_texts(request.messages))
        assert "rebuild the menu page" in seen and "where did the prices go?" in seen
        assert "You are answering a side question" in seen and prompts.BTW_TURN_NOTE in seen
        offered = {getattr(tool, "name", None) or (tool.get("function") or tool).get("name") for tool in request.tools or []}
        assert offered <= set(BTW_TOOLS)
        # Its window shows the question and the answer, never the lent conversation.
        shown = [(m["role"], m["text"]) for m in await manager.transcript_page(side.session.id) if not m.get("internal")]
        assert shown == [("user", "where did the prices go?"), ("assistant", "the prices are in menu.json")]
        (settings.state_dir / "release-the-tool").touch()
        await chat.task
        chat_seen = _texts(await manager.sessions.list_messages(chat.session.id, TENANT))
        assert not any("where did the prices go?" in text or "menu.json" in text for text in chat_seen)
        assert all("where did the prices go?" not in m["text"] for m in await manager.transcript_page(chat.session.id))
        assert "where did the prices go?" not in "\n".join(_texts(provider.requests[2].messages)), "nothing reached the chat's run"
    finally:
        (settings.state_dir / "release-the-tool").touch()
        await manager.close()
