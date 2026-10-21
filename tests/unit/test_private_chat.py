"""Telegram without a group: every session in the private chat, one of them current."""

from __future__ import annotations

import time
from typing import Any

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import CommandObject
from aiogram.methods import CreateForumTopic
from aiogram.types import CallbackQuery, Message

from daedalus.transport.telegram.front import SESSION_HEADER, TelegramFront
from tests.support.waiting import grows_to
from tests.unit.test_front import OWNER, RecordingBot, _message, front  # noqa: F401 — the fixture is reused here


def _command(name: str, args: str = "") -> CommandObject:
    return CommandObject(prefix="/", command=name, args=args or None)


def _said(message: Message, replies: list[str]) -> Message:
    """A constructed message cannot answer by itself (no bot behind it); collect what it says."""

    async def answer(text: str, **_: Any) -> None:
        replies.append(str(text))

    object.__setattr__(message, "answer", answer)
    object.__setattr__(message, "reply", answer)
    return message


def _reply_to(text: str, prompt_id: int, prompt_text: str) -> Message:
    """A private message that replies to one of the bot's own messages (a force reply)."""
    return Message.model_validate(
        {
            "message_id": 77,
            "date": int(time.time()),
            "chat": {"id": OWNER, "type": "private"},
            "from": {"id": OWNER, "is_bot": False, "first_name": "A"},
            "text": text,
            "reply_to_message": {
                "message_id": prompt_id,
                "date": int(time.time()),
                "chat": {"id": OWNER, "type": "private"},
                "from": {"id": 42, "is_bot": True, "first_name": "bot"},
                "text": prompt_text,
            },
        }
    )


def _callback(data: str, message: dict[str, Any]) -> CallbackQuery:
    query = CallbackQuery.model_validate(
        {
            "id": "cq",
            "from": {"id": OWNER, "is_bot": False, "first_name": "A"},
            "chat_instance": "x",
            "data": data,
            "message": {"message_id": message["id"], "date": 1, "chat": {"id": OWNER, "type": "private"}, "text": message["text"]},
        }
    )

    async def noop(*_: Any, **__: Any) -> None:
        return None

    object.__setattr__(query, "answer", noop)  # the bound API calls need a bot; stub them
    object.__setattr__(query.message, "edit_text", noop)
    object.__setattr__(query.message, "edit_reply_markup", noop)
    return query


async def test_no_group_means_private_mode(front: TelegramFront) -> None:
    assert front.private_mode() is True
    front.config.telegram.forum_chat_id = -100
    assert front.private_mode() is False  # an installation that bound a group keeps its topics
    front.config.telegram.mode = "private"
    assert front.private_mode() is True  # …until the owner says otherwise


async def test_new_with_a_bound_group_opens_a_topic_and_leaves_the_private_chat(front: TelegramFront) -> None:
    """The private chat is a window. /new still opens a thread, and the answer goes there."""
    replies: list[str] = []
    front.config.telegram.forum_chat_id = -100
    front.config.telegram.mode = "private"
    await front.cmd_new(_said(_message("/new research"), replies), _command("new", "research"))
    assert front.bot.topics == ["research"]  # type: ignore[attr-defined]
    assert await front.current_session_id() == ""
    binding = await front.binding_for_topic(-100, 10)
    assert binding is not None and binding.title == "research"
    outbox = await front.outbox_for_session(binding.session_id)
    assert outbox is not None and (outbox.chat_id, outbox.thread_id) == (-100, 10)
    assert replies[-1].startswith("Created topic 'research'")
    banner = front.bot.sent[-1]  # type: ignore[attr-defined]
    assert banner["chat_id"] == -100 and banner["message_thread_id"] == 10


async def test_a_session_opened_on_the_site_does_not_speak_in_telegram(front: TelegramFront) -> None:
    front.config.telegram.forum_chat_id = -100
    front.config.telegram.mode = "private"
    state = await front.manager.create_session("from the site", metadata={"telegram_detached": True})
    assert await front.outbox_for_session(state.session.id) is None
    assert await front.binding_for_session(state.session.id) is None
    assert await front.adopt_sessions_into_topics() == 0
    assert front.bot.topics == []  # type: ignore[attr-defined]
    assert await front.binding_for_session(state.session.id) is None


async def test_a_site_session_s_spawned_agent_stays_off_telegram(front: TelegramFront) -> None:
    front.config.telegram.forum_chat_id = -100
    front.config.telegram.mode = "private"
    parent = await front.manager.create_session("from the site", metadata={"telegram_detached": True})
    child_id = await front._service_spawn_agent(
        parent.session.id, title="helper", brief="help", files=[], first_message=None, preset=None, mcp=[], peer_name=None
    )
    child = await front.manager.get_state(child_id)
    assert child is not None and child.metadata.get("telegram_detached") is True
    assert await front.outbox_for_session(child_id) is None
    assert front.bot.topics == []  # type: ignore[attr-defined]


async def test_use_switches_the_chat_by_number_and_by_title(front: TelegramFront) -> None:
    replies: list[str] = []
    await front.cmd_new(_said(_message("/new alpha"), replies), _command("new", "alpha"))
    alpha = await front.current_session_id()
    await front.cmd_new(_said(_message("/new beta"), replies), _command("new", "beta"))
    beta = await front.current_session_id()
    assert alpha != beta

    await front.cmd_use(_said(_message("/use alph"), replies), _command("use", "alph"))
    assert await front.current_session_id() == alpha and replies[-1].startswith("Writing to 'alpha'")
    await front.on_message(_message("for alpha"))
    await grows_to(front.submitted, 1, "the message reached the manager")  # type: ignore[attr-defined]
    assert front.submitted[-1] == (alpha, "for alpha", [])  # type: ignore[attr-defined]

    listed = await front.manager.list_sessions(limit=50)
    number = 1 + next(i for i, s in enumerate(listed) if s["id"] == beta)
    await front.cmd_use(_said(_message(f"/use {number}"), replies), _command("use", str(number)))
    assert await front.current_session_id() == beta
    await front.on_message(_message("for beta"))
    await grows_to(front.submitted, 2, "the second message reached the manager")  # type: ignore[attr-defined]
    assert front.submitted[-1] == (beta, "for beta", [])  # type: ignore[attr-defined]


async def test_another_session_speaks_under_its_own_name(front: TelegramFront) -> None:
    replies: list[str] = []
    await front.cmd_new(_said(_message("/new digest"), replies), _command("new", "digest"))
    digest = await front.current_session_id()
    await front.cmd_new(_said(_message("/new here"), replies), _command("new", "here"))
    here = await front.current_session_id()

    await front.close_question(digest, "⏳ no answer in time; the run goes on.")
    assert front.bot.sent[-1]["text"].startswith("▸ digest\n")  # type: ignore[attr-defined]
    await front.close_question(here, "⏳ no answer in time; the run goes on.")
    assert not front.bot.sent[-1]["text"].startswith("▸")  # type: ignore[attr-defined]

    outbox = await front.outbox_for_session(digest)
    assert outbox is not None and outbox.chat_id == OWNER and outbox.thread_id is None
    assert await outbox.send_draft(1, "half an answer") is False  # a draft cannot carry the name


async def test_a_question_of_another_session_is_answered_into_that_session(front: TelegramFront) -> None:
    replies: list[str] = []
    other = await front.manager.create_session("background")
    await front.cmd_new(_said(_message("/new here"), replies), _command("new", "here"))
    here = await front.current_session_id()
    await front._ask(other.session.id, {"questions": [{"question": "Colour?", "options": [{"label": "Red"}, {"label": "Blue"}], "multiSelect": False, "allow_custom": False}]})
    sent = front.bot.sent[-1]  # type: ignore[attr-defined]
    assert sent["text"].startswith("▸ background\n")
    buttons = [b for row in sent["reply_markup"].inline_keyboard for b in row]
    await front.on_callback(_callback(buttons[1].callback_data or "", sent))
    assert front.answered == [(other.session.id, [{"question": "Colour?", "selected": ["Blue"], "custom": None}])]  # type: ignore[attr-defined]
    assert await front.current_session_id() == here  # answering does not move the chat


async def test_a_typed_answer_reaches_the_session_that_asked(front: TelegramFront) -> None:
    replies: list[str] = []
    other = await front.manager.create_session("background")
    await front.cmd_new(_said(_message("/new here"), replies), _command("new", "here"))
    here = await front.current_session_id()
    await front._ask(other.session.id, {"questions": [{"question": "Which branch?", "options": [], "allow_custom": True}]})
    sent = front.bot.sent[-1]  # type: ignore[attr-defined]
    buttons = [b for row in sent["reply_markup"].inline_keyboard for b in row]
    await front.on_callback(_callback(buttons[-1].callback_data or "", sent))
    prompt = front.bot.sent[-1]  # type: ignore[attr-defined]
    assert prompt["text"] == "▸ background\n\nType your answer:"  # the prompt says whose question it is, like the card above it

    await front.on_message(_reply_to("the release one", prompt["id"], prompt["text"]))
    await grows_to(front.answered, 1, "the answer reached the manager")  # type: ignore[attr-defined]
    session_id, answers = front.answered[-1]  # type: ignore[attr-defined]
    assert session_id == other.session.id and answers[0]["custom"].endswith("the release one")
    assert front.submitted == []  # type: ignore[attr-defined] — the answer is not a message to the current session
    assert await front.current_session_id() == here


async def test_the_current_session_survives_a_restart(front: TelegramFront) -> None:
    replies: list[str] = []
    await front.cmd_new(_said(_message("/new long running"), replies), _command("new", "long running"))
    chosen = await front.current_session_id()
    restarted = TelegramFront(front.settings, front.config, front.manager, save_config=front.save_config)
    restarted.bot = RecordingBot()  # type: ignore[assignment]
    assert await restarted.current_session_id() == chosen
    assert (await restarted.current_state()).session.id == chosen  # type: ignore[union-attr]


async def test_closing_the_current_session_frees_the_chat(front: TelegramFront) -> None:
    replies: list[str] = []
    await front.cmd_new(_said(_message("/new done with it"), replies), _command("new", "done with it"))
    chosen = await front.current_session_id()
    await front.cmd_close(_said(_message("/close"), replies))
    card = front.bot.sent[-1]  # type: ignore[attr-defined]
    assert f"Close session 'done with it' ({chosen})" in card["text"]
    await front.on_callback(_callback(f"cl:{chosen}:keep", card))
    assert await front.current_session_id() == ""
    await front.on_message(_message("hello again"))
    await grows_to(front.submitted, 1, "the message reached the manager")  # type: ignore[attr-defined]
    assert front.submitted[-1][0] != chosen  # type: ignore[attr-defined] — a fresh session takes the chat


async def test_binding_a_group_gives_the_sessions_already_open_a_topic_each(front: TelegramFront) -> None:
    """A session born in the private chat keeps its name after the switch instead of joining a crowd in General."""
    replies: list[str] = []
    await front.cmd_new(_said(_message("/new alpha"), replies), _command("new", "alpha"))
    await front.cmd_new(_said(_message("/new beta"), replies), _command("new", "beta"))
    assert front.bot.topics == []  # type: ignore[attr-defined] — private mode opens none

    await front.cmd_bind(_said(_message("/bind", chat_type="supergroup", chat_id=-100), replies))
    assert front.private_mode() is False
    assert sorted(front.bot.topics) == ["alpha", "beta"]  # type: ignore[attr-defined]
    for session in await front.manager.list_sessions():
        outbox = await front.outbox_for_session(session["id"])
        assert outbox is not None and outbox.chat_id == -100 and outbox.thread_id
        assert outbox.header is None  # in its own topic a session needs no name


async def test_a_session_telegram_will_not_open_a_topic_for_is_named_in_general(front: TelegramFront, monkeypatch: pytest.MonkeyPatch) -> None:
    replies: list[str] = []
    await front.cmd_new(_said(_message("/new orphan"), replies), _command("new", "orphan"))
    orphan = await front.current_session_id()
    front.config.telegram.forum_chat_id = -100
    front.config.telegram.mode = "topics"

    async def busy(chat_id: int, name: str) -> None:
        raise TelegramRetryAfter(CreateForumTopic(chat_id=chat_id, name=name), "Too Many Requests", 0)

    monkeypatch.setattr(front.bot, "create_forum_topic", busy)
    outbox = await front.outbox_for_session(orphan)
    assert outbox is not None and outbox.chat_id == -100 and outbox.thread_id is None
    await outbox.send_text("the answer", markdown=False)
    assert front.bot.sent[-1]["text"] == f"{SESSION_HEADER} orphan\n\nthe answer"  # type: ignore[attr-defined]

    monkeypatch.undo()  # the pause passes; the next output opens the topic after all
    outbox = await front.outbox_for_session(orphan)
    assert outbox is not None and outbox.thread_id == 10 and outbox.header is None
    assert (await front.binding_for_session(orphan)) is not None


async def test_use_refuses_a_number_sessions_never_printed(front: TelegramFront) -> None:
    replies: list[str] = []
    await front.cmd_new(_said(_message("/new alpha"), replies), _command("new", "alpha"))
    await front.cmd_use(_said(_message("/use 42"), replies), _command("use", "42"))
    assert "No session matches" in replies[-1]
    assert await front.current_session_id() == (await front.manager.list_sessions())[0]["id"]


async def test_deleting_the_current_session_frees_the_private_chat(front: TelegramFront) -> None:
    """What /delete and the close button do, the API's delete does too: the chat stops pointing at a corpse."""
    replies: list[str] = []
    await front.cmd_new(_said(_message("/new doomed"), replies), _command("new", "doomed"))
    doomed = await front.current_session_id()
    await front.forget_session(doomed)
    assert await front.manager.delete_session(doomed) is True
    assert await front.current_session_id() == ""
    await front.on_message(_message("hello again"))
    await grows_to(front.submitted, 1, "the message reached the manager")  # type: ignore[attr-defined]
    assert front.submitted[-1][0] != doomed  # type: ignore[attr-defined]
