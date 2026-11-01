"""Telegram front handlers driven with constructed updates and a recording bot."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from aiogram.filters import CommandObject
from aiogram.types import CallbackQuery, Message

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.transport.telegram.front import TelegramFront
from tests.support.waiting import grows_to

OWNER = 1


class RecordingBot:
    """The subset of the aiogram Bot API the front uses."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.topics: list[str] = []
        self._id = 100

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> SimpleNamespace:
        self._id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "id": self._id, **kwargs})
        return SimpleNamespace(message_id=self._id)

    async def edit_message_text(self, text: str, *, chat_id: int, message_id: int, **kwargs: Any) -> None:
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    async def create_forum_topic(self, chat_id: int, name: str) -> SimpleNamespace:
        self.topics.append(name)
        return SimpleNamespace(message_thread_id=len(self.topics) * 10)

    async def close_forum_topic(self, chat_id: int, thread_id: int) -> bool:
        self.closed_topic = (chat_id, thread_id)
        return True

    async def get_file(self, file_id: str) -> SimpleNamespace:
        return SimpleNamespace(file_path=f"documents/{file_id}")

    async def download(self, file: Any, destination: Path) -> None:
        Path(destination).write_bytes(b"payload")


def _message(text: str | None = None, *, chat_type: str = "private", chat_id: int = OWNER, thread: int | None = None, document: bool = False, user: int = OWNER) -> Message:
    data: dict[str, Any] = {
        "message_id": 1,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": chat_type, "is_forum": chat_type == "supergroup"},
        "from": {"id": user, "is_bot": False, "first_name": "A"},
    }
    if text is not None:
        data["text"] = text
    if thread is not None:
        data["message_thread_id"] = thread
        data["is_topic_message"] = True
    if document:
        data["document"] = {"file_id": "f1", "file_unique_id": "u1", "file_name": "data.csv", "mime_type": "text/csv"}
        data["caption"] = text
        data.pop("text", None)
    return Message.model_validate(data)


@pytest.fixture
async def front(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> TelegramFront:
    config = RuntimeConfig()
    config.telegram.inbound_merge_window_seconds = 0.01
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    submitted: list[tuple[str, str, list[str]]] = []
    addresses: list[dict[str, Any] | None] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer: bool = False, via: str = "app", address: dict[str, Any] | None = None) -> str:  # type: ignore[no-untyped-def]
        submitted.append((session_id, text, [a.path.name for a in attachments]))
        addresses.append(address)
        return "run"

    answered: list[tuple[str, list[dict[str, Any]]]] = []

    async def fake_answer(session_id: str, answers: list[dict[str, Any]], *, via: str) -> str:
        answered.append((session_id, answers))
        return "run"

    monkeypatch.setattr(manager, "submit", fake_submit)
    monkeypatch.setattr(manager, "answer", fake_answer)

    async def save(cfg: RuntimeConfig) -> None:
        return None

    f = TelegramFront(settings, config, manager, save_config=save)
    f.bot = RecordingBot()  # type: ignore[assignment]
    f.submitted = submitted  # type: ignore[attr-defined]
    f.addresses = addresses  # type: ignore[attr-defined]
    f.answered = answered  # type: ignore[attr-defined]
    yield f  # type: ignore[misc]
    # Everything the front still has in flight is stopped before the manager is closed. A test that
    # waits for the effect it asserts on — rather than sleeping and hoping — can legitimately finish
    # while a handler task is still unwinding, and a task that reaches the database while it is
    # being shut under it is a hang, not a failure.
    for task in [b.task for b in f._buffers.values() if b.task is not None] + list(f._topic_status_tasks.values()) + list(f._stale_notices.values()):
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
    await manager.close()


async def test_private_message_creates_the_first_session_and_submits(front: TelegramFront) -> None:
    await front.on_message(_message("hello agent"))
    await grows_to(front.submitted, 1, "the message reached the manager")  # type: ignore[attr-defined]
    assert front.submitted == [(front.submitted[0][0], "hello agent", [])]  # type: ignore[attr-defined]
    session_id = front.submitted[0][0]  # type: ignore[attr-defined]
    assert await front.current_session_id() == session_id  # with no group bound the chat is the window onto it
    state = await front.manager.get_state(session_id)
    assert state is not None and state.session.title == "direct"


async def test_private_message_uses_the_bound_direct_topic_in_topics_mode(front: TelegramFront) -> None:
    front.config.telegram.forum_chat_id = -100
    await front.on_message(_message("hello agent"))
    await grows_to(front.submitted, 1, "the message reached the manager")  # type: ignore[attr-defined]
    binding = await front.binding_for_topic(OWNER, 0)
    assert binding is not None and binding.title == "direct"
    assert front.submitted[0][0] == binding.session_id  # type: ignore[attr-defined]


async def test_non_owner_is_ignored(front: TelegramFront) -> None:
    # The only one of these with nothing to wait on: what is asserted is that nothing happens. A
    # sleep here can mask a failure on a loaded host but cannot invent one, which is the right way
    # round — and the owner's message after it proves the pipeline was running the whole time.
    await front.on_message(_message("hi", user=999))
    await front.on_message(_message("but this one is mine"))
    await grows_to(front.submitted, 1, "the owner's message reached the manager")  # type: ignore[attr-defined]
    assert [text for _, text, _ in front.submitted] == ["but this one is mine"]  # type: ignore[attr-defined]


async def test_fragments_and_files_merge_into_one_submission(front: TelegramFront) -> None:
    # A window wide enough that both messages are certainly inside it: the merge is what is being
    # tested, and a ten-millisecond window on a loaded host can close between the two calls.
    front.config.telegram.inbound_merge_window_seconds = 1.0
    await front.on_message(_message("part one"))
    await front.on_message(_message("part two", document=True))
    await grows_to(front.submitted, 1, "the merged message reached the manager")  # type: ignore[attr-defined]
    assert len(front.submitted) == 1  # type: ignore[attr-defined]
    _, text, files = front.submitted[0]  # type: ignore[attr-defined]
    assert text == "part one\npart two" and files == ["data.csv"]


async def test_new_in_forum_creates_topic_and_binding(front: TelegramFront) -> None:
    front.config.telegram.forum_chat_id = -100
    await front.cmd_new(_message("/new research", chat_type="supergroup", chat_id=-100), CommandObject(prefix="/", command="new", args="research"))
    assert front.bot.topics == ["research"]  # type: ignore[attr-defined]
    binding = await front.binding_for_topic(-100, 10)
    assert binding is not None and binding.title == "research"
    await front.on_message(_message("go", chat_type="supergroup", chat_id=-100, thread=10))
    await grows_to(front.submitted, 1, "the message reached the manager")  # type: ignore[attr-defined]
    assert front.submitted[0][0] == binding.session_id  # type: ignore[attr-defined]


async def test_detaching_a_topic_keeps_the_session_web_only(front: TelegramFront) -> None:
    front.config.telegram.forum_chat_id = -100
    state = await front.manager.create_session("local agent")
    neighbour = await front.manager.create_session("still linked")
    await front.bind_topic(-100, 42, state.session.id, state.session.title)
    await front.bind_topic(-100, 43, neighbour.session.id, neighbour.session.title)

    assert await front.detach_session(state.session.id)
    assert front.bot.closed_topic == (-100, 42)  # type: ignore[attr-defined]
    assert await front.binding_for_session(state.session.id) is None
    assert await front.outbox_for_session(state.session.id) is None
    kept = await front.manager.get_state(state.session.id)
    assert kept is not None and kept.metadata["telegram_detached"] is True

    # Switching the installation to private-chat mode must not silently reconnect a session
    # that the operator explicitly detached from Telegram.
    front.config.telegram.mode = "private"
    assert await front.outbox_for_session(state.session.id) is None
    neighbour_outbox = await front.outbox_for_session(neighbour.session.id)
    assert neighbour_outbox is not None
    assert (neighbour_outbox.chat_id, neighbour_outbox.thread_id) == (-100, 43)


async def test_detach_command_disconnects_the_topic_it_was_sent_from(front: TelegramFront) -> None:
    front.config.telegram.forum_chat_id = -100
    state = await front.manager.create_session("local agent")
    await front.bind_topic(-100, 42, state.session.id, state.session.title)
    replies: list[str] = []
    message = _message("/detach", chat_type="supergroup", chat_id=-100, thread=42)

    async def answer(text: str, **kwargs: Any) -> None:
        replies.append(text)

    object.__setattr__(message, "answer", answer)
    await front.cmd_detach(message)

    assert replies and replies[0].startswith("Disconnecting") and "Mini App" in replies[0]
    assert await front.binding_for_session(state.session.id) is None
    assert await front.manager.get_state(state.session.id) is not None


async def test_site_reports_and_disconnects_a_telegram_topic(front: TelegramFront) -> None:
    front.config.telegram.forum_chat_id = -100
    state = await front.manager.create_session("local agent")
    await front.bind_topic(-100, 42, state.session.id, state.session.title)
    app = SimpleNamespace(
        settings=front.settings,
        config=front.config,
        db=front.manager.db,
        manager=front.manager,
        front=front,
        extensions={},
        guard=None,
        create_session=front.manager.create_session,
    )
    transport = httpx.ASGITransport(app=build_app(app, "tok"))  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        detail = await client.get(f"/api/sessions/{state.session.id}", headers={"X-Daedalus-Token": "tok"})
        assert detail.status_code == 200 and detail.json()["telegram_linked"] is True
        detached = await client.delete(f"/api/sessions/{state.session.id}/telegram", headers={"X-Daedalus-Token": "tok"})
        assert detached.status_code == 200 and detached.json() == {"detached": True}
        detail = await client.get(f"/api/sessions/{state.session.id}", headers={"X-Daedalus-Token": "tok"})
        assert detail.status_code == 200 and detail.json()["telegram_linked"] is False


async def test_ask_user_keyboard_single_choice_resumes_run(front: TelegramFront) -> None:
    state = await front.manager.create_session("q")
    await front.bind_topic(OWNER, 0, state.session.id, "q")
    await front._ask(state.session.id, {"questions": [{"question": "Color?", "options": [{"label": "Red"}, {"label": "Blue"}], "multiSelect": False, "allow_custom": False}]})
    sent = front.bot.sent[-1]  # type: ignore[attr-defined]
    buttons = [b for row in sent["reply_markup"].inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["Red", "Blue"]
    query = CallbackQuery.model_validate(
        {
            "id": "cq",
            "from": {"id": OWNER, "is_bot": False, "first_name": "A"},
            "chat_instance": "x",
            "data": buttons[1].callback_data,
            "message": {"message_id": sent["id"], "date": 1, "chat": {"id": OWNER, "type": "private"}, "text": sent["text"]},
        }
    )

    async def answer(*args: Any, **kwargs: Any) -> None:
        return None

    query = query.model_copy(update={})
    object.__setattr__(query, "answer", answer)  # bound API call needs a bot; stub it
    object.__setattr__(query.message, "edit_text", answer)
    await front.on_callback(query)
    assert front.answered == [(state.session.id, [{"question": "Color?", "selected": ["Blue"], "custom": None}])]  # type: ignore[attr-defined]


def test_builtin_callback_prefixes_do_not_shadow_extension_hooks() -> None:
    """Extensions register callback prefixes (selfdev uses ``cp``); the front's own must stay distinct."""
    import re
    from pathlib import Path

    front_src = Path("daedalus/transport/telegram/front.py").read_text()
    builtin = set(re.findall(r'if data\[0\] == "(\w+)"', front_src))
    selfdev_src = Path("daedalus/extensions/selfdev.py").read_text()
    extension = set(re.findall(r'callback_hooks\["(\w+)"\]', selfdev_src))
    assert builtin.isdisjoint(extension), builtin & extension


async def test_a_question_answered_in_the_app_retires_its_keyboard_here(front: TelegramFront) -> None:
    state = await front.manager.create_session("q")
    sid = state.session.id
    await front.bind_topic(OWNER, 0, sid, "q")
    retired: list[tuple[int, int]] = []

    async def edit_message_reply_markup(*, chat_id: int, message_id: int, reply_markup: Any) -> None:
        retired.append((chat_id, message_id))

    front.bot.edit_message_reply_markup = edit_message_reply_markup  # type: ignore[attr-defined]
    front.listen()
    try:
        await front._ask(sid, {"questions": [{"question": "Color?", "options": [{"label": "Red"}], "multiSelect": False, "allow_custom": False}]})
        asked = front.bot.sent[-1]["id"]  # type: ignore[attr-defined]
        # Answered here: the front already knows, and says nothing more.
        await front.manager.bus.publish("ask.answered", {"request_id": "c1", "request_ref": f"ask:{sid}:c1", "via": "telegram"}, session_id=sid)
        await front.manager.bus.publish("ask.answered", {"request_id": "c1", "request_ref": f"ask:{sid}:c1", "via": "timeout"}, session_id=sid)
        count = len(front.bot.sent)  # type: ignore[attr-defined]
        await front.manager.bus.publish("ask.answered", {"request_id": "c1", "request_ref": f"ask:{sid}:c1", "via": "app"}, session_id=sid)
        await grows_to(front.bot.sent, count + 1, "the note")  # type: ignore[attr-defined]
        assert front.bot.sent[-1]["text"] == "Answered in the app."  # type: ignore[attr-defined]
        assert retired == [(OWNER, asked)] and sid not in front._question_state
        assert len(front.bot.sent) == count + 1, "the answers from Telegram and the timeout posted nothing"  # type: ignore[attr-defined]
    finally:
        await front.stop_listening()


async def test_leave_refused_carries_the_key_and_closes_the_request(front: TelegramFront) -> None:
    state = await front.manager.create_session("p")
    sid = state.session.id
    calls: list[tuple[str, str, str]] = []

    async def refuse(session_id: str, key: str, *, via: str) -> dict[str, Any]:
        calls.append((session_id, key, via))
        return {"key": key, "refuses": None}

    front.manager.refuse = refuse  # type: ignore[method-assign]
    edited: list[str] = []

    async def reply(*args: Any, **kwargs: Any) -> None:
        edited.extend(str(a) for a in args)

    query = CallbackQuery.model_validate(
        {
            "id": "cq",
            "from": {"id": OWNER, "is_bot": False, "first_name": "A"},
            "chat_instance": "x",
            "data": f"pa:{sid}:0123456789ab:no",
            "message": {"message_id": 5, "date": 1, "chat": {"id": OWNER, "type": "private"}, "text": "refused"},
        }
    )
    object.__setattr__(query, "answer", reply)
    object.__setattr__(query.message, "edit_text", reply)
    await front.on_callback(query)
    assert calls == [(sid, "0123456789ab", "telegram")]
    assert "Left refused." in edited


async def test_allow_under_a_closed_request_grants_nothing(front: TelegramFront) -> None:
    """The buttons stay in the chat after the request is answered in the app or closed with its run;
    pressing Allow there days later used to write a grant the agent was never told about."""
    state = await front.manager.create_session("p")
    sid = state.session.id
    edited: list[str] = []

    async def reply(*args: Any, **kwargs: Any) -> None:
        edited.extend(str(a) for a in args)

    def press(key: str) -> CallbackQuery:
        query = CallbackQuery.model_validate(
            {
                "id": "cq",
                "from": {"id": OWNER, "is_bot": False, "first_name": "A"},
                "chat_instance": "x",
                "data": f"pa:{sid}:{key}",
                "message": {"message_id": 5, "date": 1, "chat": {"id": OWNER, "type": "private"}, "text": "refused"},
            }
        )
        object.__setattr__(query, "answer", reply)
        object.__setattr__(query.message, "edit_text", reply)
        return query

    await front.on_callback(press("0123456789ab"))
    assert not state.metadata.get("policy_grants") and any("This request is closed" in text for text in edited)
    # An open one is still granted from the same buttons.
    state.metadata["policy_pending"] = {"abcdef012345": {"tool": "Exec", "text": "echo hi", "at": "2026-10-31T00:00:00+00:00"}}
    await front.on_callback(press("abcdef012345"))
    assert "abcdef012345" in state.metadata["policy_grants"] and "granted once" in edited


async def test_a_message_from_a_topic_is_submitted_with_where_it_was_written(front: TelegramFront) -> None:
    """The address rides with the message, so a stop that hands it back unsent finds the topic."""
    front.config.telegram.forum_chat_id = -100
    state = await front.manager.create_session("research")
    await front.bind_topic(-100, 10, state.session.id, state.session.title)
    await front.on_message(_message("go", chat_type="supergroup", chat_id=-100, thread=10))
    await grows_to(front.submitted, 1, "the message reached the manager")  # type: ignore[attr-defined]
    assert front.addresses == [{"chat_id": -100, "thread_id": 10}]  # type: ignore[attr-defined]


async def test_a_message_handed_back_unsent_goes_back_to_the_topic_it_was_typed_in(front: TelegramFront) -> None:
    """Only the app's composer heard about a message a stop handed back; whoever typed it in Telegram
    saw nothing. It comes back there, words in a block one tap copies, and is not queued again."""

    class RichBot(RecordingBot):
        async def send_rich_message(self, chat_id: int, message: Any, **kwargs: Any) -> SimpleNamespace:
            self._id += 1
            self.sent.append({"chat_id": chat_id, "html": message.html, "id": self._id, **kwargs})
            return SimpleNamespace(message_id=self._id)

    bot = RichBot()
    front.bot = bot  # type: ignore[assignment]
    state = await front.manager.create_session("stopped")
    messages = [
        {"id": "q1", "text": "check <both> servers", "files": ["shot.png"], "via": "telegram", "address": {"chat_id": -100, "thread_id": 42}},
        {"id": "c-app", "text": "typed in the app", "files": [], "via": "app", "address": {}},
        {"id": "q2", "text": "and the logs", "files": [], "via": "telegram", "address": {"chat_id": OWNER, "thread_id": 0}},
    ]
    await front.manager._give_back(state.session.id, messages, "clear")

    assert [(m["chat_id"], m.get("message_thread_id")) for m in bot.sent] == [(-100, 42), (OWNER, None)]
    first = bot.sent[0]["html"]
    assert "Not sent" in first and "history was cleared" in first
    assert "<pre>check &lt;both&gt; servers</pre>" in first and "<code>shot.png</code>" in first
    assert "typed in the app" not in " ".join(m["html"] for m in bot.sent)
    assert await front.manager.queued_input(state.session.id) == []
