"""A subagent never writes to Telegram: its work reaches the operator through its leader.

Before, a worker spoke in the private chat whenever its leader was not detached at the moment of the
spawn, and in a bound group it opened a topic of its own. These drive the front with a recording bot
and prove nothing reaches it on the subagent's behalf, whatever the leader's standing in the chat.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiogram.filters import CommandObject
from aiogram.types import Message

from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.notifications import NotificationService
from daedalus.extensions.recurring import Recurring, RecurringEffect
from daedalus.extensions.scheduler import Scheduler
from daedalus.stores.control import Principal
from daedalus.stores.outbox import OutboxStore
from daedalus.transport.telegram.front import TelegramFront
from tests.unit.test_front import RecordingBot, _message, front  # noqa: F401 — the fixture is reused here
from tests.unit.test_heartbeat_scheduler import FakeFront, app  # noqa: F401 — the fixture is reused here


def _said(message: Message, replies: list[str]) -> Message:
    async def answer(text: str, **_: Any) -> None:
        replies.append(str(text))

    object.__setattr__(message, "answer", answer)
    object.__setattr__(message, "reply", answer)
    return message


async def _subagent(front: TelegramFront, leader_id: str, name: str = "worker") -> str:
    state = await front.manager.create_session(f"[sub] {name}", metadata={"subagent_of": leader_id, "subagent_name": name, "unattended": True})
    return state.session.id


async def _nothing_reaches_telegram(front: TelegramFront, session_id: str) -> None:
    """Every road the front has from a session to the chat, walked for one session."""
    bot: RecordingBot = front.bot  # type: ignore[assignment]
    sent, topics = len(bot.sent), list(bot.topics)
    assert await front.outbox_for_session(session_id) is None
    assert await front._renderer_for(session_id, "run-1") is None
    await front._ask(session_id, {"questions": [{"question": "which one?", "options": [{"label": "a"}]}]})
    async def fetch() -> Path:
        raise AssertionError("nothing is fetched for a session with no chat")

    assert await front._service_send_file(session_id, fetch, None) == "no chat bound"
    assert len(bot.sent) == sent and bot.topics == topics
    assert await front.binding_for_session(session_id) is None


async def test_a_subagent_of_the_private_chat_s_session_stays_out_of_the_private_chat(front: TelegramFront) -> None:
    await front.on_message(_message("hello"))
    leader_id = await front.current_session_id()
    assert (await front.outbox_for_session(leader_id)) is not None  # the leader does speak here
    worker = await _subagent(front, leader_id)
    await _nothing_reaches_telegram(front, worker)


async def test_a_subagent_stays_quiet_when_its_leader_is_detached_after_the_spawn(front: TelegramFront) -> None:
    """The flag was copied once, at spawn: detaching the leader later left its worker in the chat."""
    leader = await front.manager.create_session("leader")
    worker = await _subagent(front, leader.session.id)
    leader.metadata["telegram_detached"] = True
    leader.session.metadata["telegram_detached"] = True
    await front.manager.sessions.update_metadata(leader.session.id, leader.session.metadata)
    assert await front.outbox_for_session(leader.session.id) is None
    await _nothing_reaches_telegram(front, worker)


async def test_a_subagent_of_a_site_session_stays_quiet(front: TelegramFront) -> None:
    leader = await front.manager.create_session("from the site", metadata={"telegram_detached": True})
    worker = await _subagent(front, leader.session.id)
    await _nothing_reaches_telegram(front, worker)


async def test_a_subagent_of_a_topic_gets_no_topic_of_its_own(front: TelegramFront) -> None:
    front.config.telegram.forum_chat_id = -100
    front.config.telegram.mode = "topics"
    leader, binding = await front.create_session_topic("leader")
    assert binding.thread_id == 10 and front.bot.topics == ["leader"]  # type: ignore[attr-defined]
    worker = await _subagent(front, leader.session.id)
    assert await front.ensure_topic(worker, "[sub] worker") is None
    await _nothing_reaches_telegram(front, worker)
    assert front.bot.topics == ["leader"]  # type: ignore[attr-defined]


async def test_a_session_created_as_a_subagent_is_bound_nowhere(front: TelegramFront) -> None:
    """Neither a topic in the group nor the private chat's own binding, which would make it the chat's session."""
    leader = await front.manager.create_session("leader")
    front.config.telegram.forum_chat_id = -100
    front.config.telegram.mode = "topics"
    state, _ = await front.create_session_topic("[sub] worker", metadata={"subagent_of": leader.session.id})
    assert front.bot.topics == [] and front.bot.sent == []  # type: ignore[attr-defined]
    assert await front.binding_for_session(state.session.id) is None
    front.config.telegram.mode = "private"
    state, _ = await front.create_session_topic("[sub] other", metadata={"subagent_of": leader.session.id})
    assert await front.binding_for_topic(front.settings.owner_user_id, 0) is None
    await _nothing_reaches_telegram(front, state.session.id)


async def test_binding_a_group_does_not_adopt_subagents(front: TelegramFront) -> None:
    replies: list[str] = []
    await front.cmd_new(_said(_message("/new alpha"), replies), CommandObject(prefix="/", command="new", args="alpha"))
    leader_id = await front.current_session_id()
    worker = await _subagent(front, leader_id)
    await front.cmd_bind(_said(_message("/bind", chat_type="supergroup", chat_id=-100), replies))
    assert front.bot.topics == ["alpha"]  # type: ignore[attr-defined]
    assert await front.adopt_sessions_into_topics() == 0
    await _nothing_reaches_telegram(front, worker)


async def test_use_will_not_point_the_private_chat_at_a_subagent(front: TelegramFront) -> None:
    replies: list[str] = []
    await front.on_message(_message("hello"))
    leader_id = await front.current_session_id()
    worker = await _subagent(front, leader_id, "digger")
    await front.cmd_use(_said(_message(f"/use {worker}"), replies), CommandObject(prefix="/", command="use", args=worker))
    assert await front.current_session_id() == leader_id
    assert "subagent" in replies[-1]


async def test_a_subagent_s_reminder_goes_to_the_inbox_and_not_to_the_chat(app: Any) -> None:  # noqa: F811
    leader = await app.manager.create_session("leader")
    worker = await app.manager.create_session("[sub] worker", metadata={"subagent_of": leader.session.id})
    # The shared fixture's service has no metadata lookup; the host's one does, and the router
    # needs it to see that the session is a subagent.
    async def metadata(session_id: str) -> dict[str, Any]:
        return app.manager.live_state(session_id).metadata

    app.notifications = NotificationService(app.db, app.manager.bus, front=lambda: app.front, session_metadata=metadata)
    scheduler = Scheduler(app)
    app.extensions["recurring"] = Recurring(app)
    app.extensions["scheduler"] = scheduler
    dispatcher = EffectDispatcher(OutboxStore(app.db))
    dispatcher.register("schedule.message", RecurringEffect(app))
    app.extensions["effects"] = dispatcher
    revision = await app.db.fetchone(
        "SELECT revision FROM domain_collection_revisions WHERE scope_kind='project' AND scope_id=?",
        (worker.project.id,),
    )
    created = await app.extensions["recurring"].create(
        Principal("operator:1", "operator"), name="check", prompt="look again", cron=None,
        run_at="2026-01-01T00:00:00Z", kind="message", target_session=worker.session.id,
        project_id=worker.project.id,
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        expected_collection_revision=revision["revision"], client_operation_id="reminder-approval",
    )
    row = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],))
    await scheduler.fire(dict(row))
    assert await dispatcher.step()
    fake: FakeFront = app.front
    assert fake.outbox.sent == [] and fake.notified == []
    entries = (await app.notifications.list())["entries"]
    assert [(e["kind"], e["tone"]) for e in entries] == [("reminder", "info")]
