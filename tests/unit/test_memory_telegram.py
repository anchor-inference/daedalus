"""Memory in Telegram: ``/memory`` shows the You pages and the counts with the app for the rest,
``/remember`` files a note as the Memory screen's quick add does, and an answer whose turn saved
something gets one quiet line under it."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import pytest
from aiogram.filters import CommandObject
from protocore.contracts.memory import MemoryScope

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import TENANT, SessionManager
from daedalus.stores.database import Database
from daedalus.transport.telegram.front import TelegramFront
from daedalus.transport.telegram.memorycmd import memory_view, saved_footer
from tests.unit.test_front import RecordingBot, _message


@pytest.fixture
async def front(settings: Settings, db: Database) -> Any:
    config = RuntimeConfig()
    manager = SessionManager(settings, config, db=db)
    await manager.start()

    async def save(cfg: RuntimeConfig) -> None:
        return None

    f = TelegramFront(settings, config, manager, save_config=save)
    f.bot = RecordingBot()  # type: ignore[assignment]
    yield f
    for task in list(f._topic_status_tasks.values()):
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
    await manager.close()


def answered(text: str) -> tuple[Any, list[str]]:
    replies: list[str] = []
    message = _message(text)

    async def answer(reply: str, **_: Any) -> None:
        replies.append(reply)

    object.__setattr__(message, "answer", answer)
    return message, replies


async def test_remember_files_a_note_like_the_quick_add(front: TelegramFront) -> None:
    message, replies = answered("/remember I prefer short answers")
    await front.cmd_remember(message, CommandObject(prefix="/", command="remember", args="I prefer short answers"))
    records = front.manager.memory.records(TENANT)
    assert [r.text for r in records] == ["I prefer short answers"] and records[0].metadata.get("locked")
    assert replies == ["🧠 Saved to memory · Preferences. Undo it from the app's Memory history."]
    again, said = answered("/remember I prefer short answers")
    await front.cmd_remember(again, CommandObject(prefix="/", command="remember", args="I prefer short answers"))
    assert len(front.manager.memory.records(TENANT)) == 1 and "already" in said[0]
    empty, usage = answered("/remember")
    await front.cmd_remember(empty, CommandObject(prefix="/", command="remember", args=None))
    assert usage[0].startswith("usage: /remember")


async def test_memory_shows_the_you_pages_and_links_to_the_app(front: TelegramFront) -> None:
    entries = front.manager.memory_entries
    await entries.remember(MemoryScope.global_, "", "The operator prefers <b>plain</b> prose", kind="preference", metadata={}, origin="operator")
    await entries.remember(MemoryScope.global_, "", "Someone's dog is called Beans", kind="fact", metadata={}, origin="operator")
    message, replies = answered("/memory")
    await front.cmd_memory(message)
    text = replies[0]
    assert text.startswith("<b>🧠 Memory</b> · on") and "2 entries · 2 facts" in text
    assert "&lt;b&gt;plain&lt;/b&gt;" in text, "memory text is escaped"
    assert "Also: Recent work" in text and "app → Memory" in text
    front.config.memory.paused = True
    message, replies = answered("/memory")
    await front.cmd_memory(message)
    assert "paused" in replies[0]


def test_the_short_view_links_when_the_app_has_an_address() -> None:
    listing = [{"id": "e1", "section": "you", "title": "Profile", "count": 9}]
    text = memory_view(listing, {"e1": [f"fact {n}" for n in range(9)]}, {"paused": False, "generate": True}, "https://example.test/app/memory")
    assert '<a href="https://example.test/app/memory">' in text and "and 3 more" in text


class Outbox:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def send_text(self, text: str, *, markdown: bool = True) -> int:
        self.texts.append(text)
        return len(self.texts)


async def test_a_turn_that_saved_something_gets_the_note(front: TelegramFront) -> None:
    entries = front.manager.memory_entries
    await entries.remember(MemoryScope.global_, "", "Someone's dog is called Beans", kind="fact", metadata={}, origin="remember", session_id="s1", run_id="run-1")
    await entries.remember(MemoryScope.global_, "", "The operator prefers tea", kind="preference", metadata={}, origin="remember", session_id="s1", run_id="run-1")
    outbox = Outbox()
    await front._saved_note("run-1", outbox)  # type: ignore[arg-type]
    assert outbox.texts and outbox.texts[0].startswith("🧠 Saved to memory · ")
    assert set(outbox.texts[0].removeprefix("🧠 Saved to memory · ").split(", ")) == {"Recent work", "Preferences"}
    quiet = Outbox()
    await front._saved_note("run-2", quiet)  # type: ignore[arg-type]
    assert quiet.texts == []
    assert saved_footer([]) == "🧠 Saved to memory"
