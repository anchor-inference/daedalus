"""Run rendering and ingress polish: throttle tiers, tool timers, delivery fallback, reactions, guards."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from aiogram.filters import CommandObject
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.transport.telegram.front import RUN_REACTIONS, TelegramFront, _cost_words, flooded, note_flood
from daedalus.transport.telegram.render import RunRenderer, RunView
from tests.support.waiting import grows_to
from tests.unit.test_front import OWNER, _message, front  # noqa: F401 — the fixture is reused here
from tests.unit.test_telegram_render import FakeOutbox


def _evt(t: EventType, **payload: Any) -> TurnEvent:
    return TurnEvent(type=t, run_id="r1", payload=payload)


def test_edit_interval_grows_with_run_age() -> None:
    renderer = RunRenderer(FakeOutbox(), RunView(run_id="r1", model="m"), edit_interval=1.0, edit_tiers=[[60, 1], [300, 2], [0, 10]])
    assert renderer.current_edit_interval() == 1.0
    renderer.view.started = time.monotonic() - 120
    assert renderer.current_edit_interval() == 2.0
    renderer.view.started = time.monotonic() - 3600
    assert renderer.current_edit_interval() == 10.0


async def test_tool_log_carries_durations_and_slow_marker() -> None:
    renderer = RunRenderer(FakeOutbox(), RunView(run_id="r1", model="m"), edit_interval=0.0, slow_tool_seconds=30)
    await renderer.handle(_evt(EventType.TOOL_USE_START, tool_call_id="c1", tool_name="Exec"))
    renderer.view.tool_started["c1"] -= 95  # the call has been running for 95 s
    renderer.view.current_tool_since -= 95
    assert "⏱ Exec · 1m 00s (still running)" in renderer.render_status()
    await renderer.handle(_evt(EventType.TOOL_USE_STOP, tool_call_id="c1", final_input={"command": "sleep 95"}))
    await renderer.handle(_evt(EventType.TOOL_RESULT, tool_call_id="c1", content="done"))
    assert renderer.view.tools[0].endswith("sleep 95 · 1m 35s")
    assert renderer.view.current_tool is None
    if renderer._tick_task is not None:
        renderer._tick_task.cancel()


async def test_secret_in_tool_arguments_is_masked_in_the_log() -> None:
    renderer = RunRenderer(FakeOutbox(), RunView(run_id="r1", model="m"), edit_interval=0.0)
    await renderer.handle(_evt(EventType.TOOL_USE_START, tool_call_id="c1", tool_name="Exec"))
    await renderer.handle(_evt(EventType.TOOL_USE_STOP, tool_call_id="c1", final_input={"command": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123' x"}))
    assert "abcdefghijklmnopqrstuvwxyz0123" not in renderer.view.tools[0]
    assert "Bearer •••" in renderer.view.tools[0]


class RefusingOutbox(FakeOutbox):
    """Telegram rejects every text send; documents work."""

    async def send_text(self, text: str, *, markdown: bool = True) -> int:
        raise RuntimeError("Bad Request: can't parse entities")


async def test_final_answer_falls_back_to_a_file_when_text_is_refused(tmp_path: Path) -> None:
    outbox = RefusingOutbox()
    renderer = RunRenderer(outbox, RunView(run_id="r1", model="m"), edit_interval=0.0)
    await renderer.handle(_evt(EventType.MESSAGE_START))
    await renderer.handle(_evt(EventType.CONTENT_BLOCK_DELTA, delta={"type": "text_delta", "text": "answer *with* bad markup"}))
    await renderer.finish("completed", workspace=tmp_path)
    assert outbox.documents == [tmp_path / "answer.md"]
    assert (tmp_path / "answer.md").read_text() == "answer *with* bad markup"


class SilentOutbox(RefusingOutbox):
    async def send_document(self, path: Path, caption: str | None = None) -> int:
        raise RuntimeError("Request Entity Too Large")


async def test_delivery_failure_is_recorded_not_swallowed(tmp_path: Path) -> None:
    renderer = RunRenderer(SilentOutbox(), RunView(run_id="r1", model="m"), edit_interval=0.0)
    await renderer.handle(_evt(EventType.MESSAGE_START))
    await renderer.handle(_evt(EventType.CONTENT_BLOCK_DELTA, delta={"type": "text_delta", "text": "answer"}))
    await renderer.finish("completed", workspace=tmp_path)
    assert renderer.view.delivery_failed is True
    assert (tmp_path / "answer.md").exists()


async def test_ready_inline_media_is_delivered_by_native_telegram_type(front: TelegramFront, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = [tmp_path / name for name in ("one.png", "two.png", "clip.mp4", "voice.mp3", "loop.gif", "act.docx")]
    for path in files:
        path.write_bytes(b"content")

    async def ready_for_run(session_id: str, run_id: str) -> list[dict[str, Any]]:
        assert (session_id, run_id) == ("session", "run")
        return [
            {"id": "album", "layout": "album", "items": [{"path": str(files[0]), "kind": "image", "caption": "A"}, {"path": str(files[1]), "kind": "image", "caption": "B"}]},
            {"id": "mixed", "layout": "album", "items": [{"path": str(files[0]), "kind": "image", "caption": "A"}, {"path": str(files[2]), "kind": "video", "caption": "V"}, {"path": str(files[4]), "kind": "animation", "caption": "G"}]},
            {"id": "video", "layout": "single", "items": [{"path": str(files[2]), "kind": "video", "caption": "V"}]},
            {"id": "audio", "layout": "single", "items": [{"path": str(files[3]), "kind": "audio", "caption": "S"}]},
            {"id": "animation", "layout": "single", "items": [{"path": str(files[4]), "kind": "animation", "caption": "G"}]},
            {"id": "document", "layout": "single", "items": [{"path": str(files[5]), "kind": "file", "caption": "D"}]},
        ]

    monkeypatch.setattr(front.manager.media, "ready_for_run", ready_for_run)
    outbox = FakeOutbox()
    assert await front._deliver_inline_media("session", "run", outbox)
    assert outbox.media == [
        ("album", files[:2], "A"),
        ("photo", files[0], "A"),
        ("video", files[2], "V"),
        ("animation", files[4], "G"),
        ("video", files[2], "V"),
        ("audio", files[3], "S"),
        ("animation", files[4], "G"),
    ]
    # An attached document reaches the chat as one; it used to fall through to send_photo.
    assert outbox.documents == [files[5]]


def test_flood_budget_is_per_chat() -> None:
    note_flood(-42, 0.2)
    assert flooded(-42) and not flooded(-43)


def test_cost_words_distinguish_unknown_from_zero() -> None:
    assert _cost_words(None, 0) == ""
    assert _cost_words(None, 3) == " · cost unknown (no price for these calls)"
    assert _cost_words(1.5, 0) == " · $1.5000"
    assert _cost_words(1.5, 2) == " · $1.5000 (+2 unmetered calls)"


async def test_stale_message_is_acknowledged_not_executed(front: TelegramFront, monkeypatch: pytest.MonkeyPatch) -> None:
    # The notice is deliberately delayed so a burst of stale messages is acknowledged once. The
    # delay is patched down and then waited *on*, rather than slept through: three seconds of real
    # sleep is three seconds a loaded host can overrun, and it tests the clock, not the coalescing.
    monkeypatch.setattr("daedalus.transport.telegram.front.STALE_NOTICE_DELAY_SECONDS", 0.05)
    replies: list[str] = []

    async def reply(text: str, **_: Any) -> None:
        replies.append(text)

    old = _message("deploy it")
    object.__setattr__(old, "date", old.date.replace(year=2020))
    object.__setattr__(old, "reply", reply)
    await front.on_message(old)
    second = _message("and this")
    object.__setattr__(second, "date", second.date.replace(year=2020))
    object.__setattr__(second, "reply", reply)
    await front.on_message(second)
    await grows_to(replies, 1, "the one notice for the stale burst")
    assert front.submitted == []  # type: ignore[attr-defined]
    assert replies == [replies[0]] and replies[0].startswith("⏳ Ignored: 2 messages")


async def test_unknown_command_goes_to_the_agent(front: TelegramFront) -> None:
    await front.on_message(_message("/standup what did we do yesterday"))
    await grows_to(front.submitted, 1, "the command reached the manager")  # type: ignore[attr-defined]
    assert front.submitted[0][1] == "/standup what did we do yesterday"  # type: ignore[attr-defined]


async def test_sticker_and_reply_context_become_text(front: TelegramFront) -> None:
    msg = _message("do this instead")
    data = msg.model_dump()
    data["reply_to_message"] = {
        "message_id": 7,
        "date": int(time.time()),
        "chat": {"id": OWNER, "type": "private"},
        "from": {"id": 555, "is_bot": True, "first_name": "Daedalus"},
        "text": "I would refactor the parser first.",
    }
    msg = type(msg).model_validate(data)
    await front.on_message(msg)
    await grows_to(front.submitted, 1, "the reply reached the manager")  # type: ignore[attr-defined]
    assert front.submitted[0][1] == '[replying to the agent: "I would refactor the parser first."]\n\ndo this instead'  # type: ignore[attr-defined]
    sticker = _message(None)
    data = sticker.model_dump()
    data["sticker"] = {"file_id": "s", "file_unique_id": "su", "type": "regular", "width": 1, "height": 1, "is_animated": False, "is_video": False, "emoji": "😂", "set_name": "Pack"}
    await front.on_message(type(sticker).model_validate(data))
    await grows_to(front.submitted, 2, "the sticker reached the manager")  # type: ignore[attr-defined]
    assert front.submitted[1][1] == "[sticker 😂 from set Pack]"  # type: ignore[attr-defined]


async def test_oversize_file_is_refused_before_download(front: TelegramFront) -> None:
    replies: list[str] = []

    async def reply(text: str, **_: Any) -> None:
        replies.append(text)

    front.config.telegram.max_inbound_file_mb = 1
    msg = _message("big", document=True)
    data = msg.model_dump()
    data["document"]["file_size"] = 5 * 1_048_576
    msg = type(msg).model_validate(data)
    object.__setattr__(msg, "reply", reply)
    await front.on_message(msg)
    await grows_to(replies, 1, "the refusal reached the operator")
    assert front.submitted == []  # type: ignore[attr-defined]
    assert "file refused" in replies[0]


async def test_reactions_and_topic_status_follow_the_run(front: TelegramFront, monkeypatch: pytest.MonkeyPatch) -> None:
    # The rename is debounced so a burst of status changes is one rename. Patched down and waited
    # on, for the same reason as the stale notice above.
    monkeypatch.setattr("daedalus.transport.telegram.front.TOPIC_RENAME_DEBOUNCE_SECONDS", 0.05)
    reactions: list[tuple[int, int, list[Any]]] = []
    renames: list[tuple[int, int, str]] = []

    async def set_message_reaction(chat_id: int, message_id: int, reaction: list[Any]) -> bool:
        reactions.append((chat_id, message_id, reaction))
        return True

    async def edit_forum_topic(chat_id: int, thread_id: int, name: str, **_: Any) -> bool:
        renames.append((chat_id, thread_id, name))
        return True

    front.bot.set_message_reaction = set_message_reaction  # type: ignore[attr-defined]
    front.bot.edit_forum_topic = edit_forum_topic  # type: ignore[attr-defined]
    front.config.telegram.forum_chat_id = -100
    await front.cmd_new(_message("/new job", chat_type="supergroup", chat_id=-100), CommandObject(prefix="/", command="new", args="job"))
    binding = await front.binding_for_topic(-100, 10)
    assert binding is not None
    await front.on_message(_message("go", chat_type="supergroup", chat_id=-100, thread=10))
    await grows_to(front.submitted, 1, "the message reached the manager")  # type: ignore[attr-defined]
    await grows_to(reactions, 1, "the message was reacted to")
    assert reactions[0][2][0].emoji == RUN_REACTIONS["received"]
    assert front._topic_name(binding.session_id, "job") == "🟢 job"
    await front._on_finished(binding.session_id, "nope", "completed")
    assert reactions[-1][2][0].emoji == RUN_REACTIONS["completed"]
    assert front._topic_name(binding.session_id, "job") == "🏁 job"
    await grows_to(renames, 1, "the topic was renamed after the debounce")
    assert renames[-1] == (-100, 10, "🏁 job")


async def test_final_answer_is_masked_and_headline_stripped_before_telegram(tmp_path: Path) -> None:
    outbox = FakeOutbox()
    renderer = RunRenderer(outbox, RunView(run_id="r1", model="m"), edit_interval=0.0)
    await renderer.handle(_evt(EventType.MESSAGE_START))
    await renderer.handle(_evt(EventType.CONTENT_BLOCK_DELTA, delta={"type": "text_delta", "text": "Token is ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.\n\n⟦ echo | status: completed; next: none | anchors: token ⟧"}))
    await renderer.finish("completed", workspace=tmp_path)
    sent = [t for t, _ in outbox.sent if "Token is" in t]
    assert sent and "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in sent[0] and "⟦" not in sent[0]
