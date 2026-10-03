"""A project's topic in Telegram, driven with a recording bot: only the orchestrator's reports, its
notifications and the requests waiting on the operator appear there, each once; the operator's words
there reach the orchestrator; the topic follows the office. No network."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import Message

from daedalus.config import Settings
from daedalus.extensions import project_topics
from daedalus.extensions.notifications import ROUTED_EVENTS, NotificationRouter, NotificationService
from daedalus.extensions.project_topics import ProjectTopics
from daedalus.host.events import EventFilter
from daedalus.stores.database import Database
from daedalus.transport.telegram.front import TelegramFront
from tests.support.authorized_launch import operator_assignment
from tests.support.waiting import until_await
from tests.unit.test_front import OWNER, RecordingBot, _message
from tests.unit.test_orchestrator import Rig, rig
from tests.unit.test_staff_runtime import ObservedFakeStaffRuntime, board_task, close_team

FORUM = -100


class Bot(RecordingBot):
    def __init__(self) -> None:
        super().__init__()
        self.renamed: list[tuple[int, str]] = []
        self.closed: list[tuple[int, int]] = []

    async def edit_forum_topic(self, chat_id: int, thread_id: int, name: str) -> bool:
        self.renamed.append((thread_id, name))
        return True

    async def close_forum_topic(self, chat_id: int, thread_id: int) -> bool:
        self.closed.append((chat_id, thread_id))
        return True

    async def edit_message_text(self, text: str, *, chat_id: int, message_id: int, **kwargs: Any) -> None:
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})


class Query:
    """A tap on an inline button: what it said back to the operator."""

    def __init__(self, data: str, message: Any = None) -> None:
        self.data = data
        self.message = message
        self.said: list[str] = []

    async def answer(self, text: str = "", **_: Any) -> None:
        self.said.append(text)


class Setup:
    def __init__(self, r: Rig, front: TelegramFront, topics: ProjectTopics, handlers: list[asyncio.Task[None]], submitted: list[tuple[str, str]]) -> None:
        self.r, self.front, self.topics, self.handlers, self.submitted = r, front, topics, handlers, submitted

    @property
    def bot(self) -> Bot:
        return self.front.bot  # type: ignore[return-value]

    async def close(self) -> None:
        for handler in self.handlers:
            handler.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await handler
        for task in [b.task for b in self.front._buffers.values() if b.task is not None]:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        await close_team(self.r.manager)
        await self.r.manager.close()

    async def orchestrator(self) -> str:
        project = await self.r.refreshed()
        return project.settings.orchestrator.session_id


async def setup(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: str = "topics") -> Setup:
    r = await rig(settings, db, tmp_path)
    manager = r.manager

    async def save(_config: Any) -> None:
        return None

    front = TelegramFront(settings, manager.config, manager, save_config=save)
    front.bot = Bot()  # type: ignore[assignment]
    manager.config.telegram.mode = mode
    manager.config.telegram.forum_chat_id = FORUM if mode == "topics" else 0
    manager.config.telegram.reactions = False
    manager.config.telegram.inbound_merge_window_seconds = 0.01
    submitted: list[tuple[str, str]] = []

    async def fake_submit(session_id: str, text: str, attachments: Any = (), **_: Any) -> str:
        submitted.append((session_id, text))
        return "run"

    monkeypatch.setattr(manager, "submit", fake_submit)
    app = r.team.app
    app.front = front  # type: ignore[attr-defined]
    # The real router, so "each item appears once" is proved against the one path that could post twice.
    service = NotificationService(db, manager.bus, front=lambda: front)
    service.set_project_policy(r.orch.notification_policy)
    app.notifications = service  # type: ignore[attr-defined]
    topics = ProjectTopics(app, front)  # type: ignore[arg-type]
    app.extensions["project_topics"] = topics  # type: ignore[attr-defined]
    router = NotificationRouter(service, manager)
    routed = manager.bus.on(EventFilter(types=ROUTED_EVENTS), router.handle, name="notifications")
    return Setup(r, front, topics, [topics.attach(), routed], submitted)


def reply(text: str, to: int, *, chat_id: int = OWNER, chat_type: str = "private", thread: int | None = None) -> Message:
    data: dict[str, Any] = {
        "message_id": 900 + to,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": chat_type, "is_forum": chat_type == "supergroup"},
        "from": {"id": OWNER, "is_bot": False, "first_name": "A"},
        "text": text,
        "reply_to_message": {"message_id": to, "date": int(time.time()), "chat": {"id": chat_id, "type": chat_type}, "text": "earlier"},
    }
    if thread is not None:
        data["message_thread_id"] = thread
        data["is_topic_message"] = True
    return Message.model_validate(data)


def said(message: Message, replies: list[str]) -> Message:
    async def answer(text: str, **_: Any) -> None:
        replies.append(str(text))

    object.__setattr__(message, "reply", answer)
    object.__setattr__(message, "answer", answer)
    return message


def buttons(sent: dict[str, Any]) -> list[str]:
    markup = sent.get("reply_markup")
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


async def test_switching_on_opens_a_quiet_topic_the_operator_writes_in(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await setup(settings, db, tmp_path, monkeypatch)
    try:
        await s.r.orch.enable(s.r.project.id)
        sid = await s.orchestrator()

        # Opening the topic binds the session first and saves the topic in the project's settings
        # after it, in a second step: waiting for the binding alone raced that save under load.
        async def bound() -> bool:
            binding = await s.front.binding_for_session(sid)
            return binding is not None and (await s.r.refreshed()).settings.orchestrator.telegram_topic_id == binding.thread_id

        await until_await(bound, "the project's topic was opened and saved")
        binding = await s.front.binding_for_session(sid)
        assert binding is not None and (binding.chat_id, binding.title) == (FORUM, "Bakery") and s.bot.topics == ["Bakery"]
        assert (await s.r.refreshed()).settings.orchestrator.telegram_topic_id == binding.thread_id
        state = await s.r.manager.get_state(sid)
        assert state is not None and state.metadata.get("telegram_quiet") and not state.metadata.get("telegram_detached")

        # Its turns never stream into the topic: no renderer, no outbox, no approval buttons.
        assert await s.front.outbox_for_session(sid) is None and await s.front._renderer_for(sid, "run-1") is None
        assert s.bot.sent == []

        # The operator writes in the topic: the orchestrator receives it.
        await s.front.on_message(_message("Is the menu page done?", chat_type="supergroup", chat_id=FORUM, thread=binding.thread_id))

        async def arrived() -> bool:
            return s.submitted == [(sid, "Is the menu page done?")]

        await until_await(arrived, "the operator's message reached the orchestrator")
        # A sweep over the sessions never opens a second topic for the orchestrator.
        assert await s.front.adopt_sessions_into_topics() == 0 and s.bot.topics == ["Bakery"]
    finally:
        await s.close()


async def test_a_report_appears_once_and_a_staff_member_never_posts(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await setup(settings, db, tmp_path, monkeypatch)
    try:
        await s.r.orch.enable(s.r.project.id)
        sid = await s.orchestrator()
        await until_await(lambda: _bound(s, sid), "the topic")
        await s.r.call(sid, "project_report", text="The menu page is merged.", title="Menu page done", kind="done")

        async def posted() -> bool:
            return len(s.bot.sent) >= 1

        await until_await(posted, "the report was posted")
        await asyncio.sleep(0.2)
        [post] = s.bot.sent
        binding = await s.front.binding_for_session(sid)
        assert binding is not None and post["chat_id"] == FORUM and post["message_thread_id"] == binding.thread_id
        assert "Menu page done" in post["text"] and "The menu page is merged." in post["text"] and post["text"].startswith("✅")

        # A staff member works, asks the orchestrator and reports: nothing of it reaches Telegram.
        runtime = ObservedFakeStaffRuntime(kind="daedalus")
        runtime.manager = s.r.manager
        s.r.team.runtimes["daedalus"] = runtime
        ada = await s.r.manager.staff.hire(s.r.project.id, name="Ada", isolation="shared")
        task_id = await board_task(s.r.manager, s.r.project, "Menu")
        await operator_assignment(s.r.team, ada, task_id)
        live = await s.r.team.live_of(ada)
        assert live is not None
        staff_session = runtime.started[0]
        await s.r.team.ingress.question(live, "team:q1", "Euros or dollars?", ["Euros", "Dollars"])
        await s.r.team.ingress.report(live, "checkpoint", "half way", call_id="topic-staff-report")
        await asyncio.sleep(0.2)
        assert len(s.bot.sent) == 1, "a request the orchestrator answers is not the operator's, and a staff report is the orchestrator's"
        state = await s.r.manager.get_state(live.session_id) if live.session_id else None
        if state is not None:
            assert await s.front.outbox_for_session(state.session.id) is None
        assert staff_session.staff.id == ada.id
    finally:
        await s.close()


async def _bound(s: Setup, sid: str) -> bool:
    return await s.front.binding_for_session(sid) is not None


async def test_a_question_is_answered_with_its_buttons_and_a_late_tap_is_told(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await setup(settings, db, tmp_path, monkeypatch)
    try:
        await s.r.orch.enable(s.r.project.id)
        sid = await s.orchestrator()
        await until_await(lambda: _bound(s, sid), "the topic")
        await s.r.call(sid, "ask_operator", title="Postgres or SQLite?", text="Postgres or SQLite?", options=["Postgres", "SQLite"])
        [ask] = await s.r.manager.asks.open_for(s.r.project.id, routed_to="operator")

        async def posted() -> bool:
            return len(s.bot.sent) == 1

        await until_await(posted, "the question was posted")
        await asyncio.sleep(0.2)
        [post] = s.bot.sent
        assert f"[{ask.short_id}]" in post["text"] and "Postgres or SQLite?" in post["text"]
        assert buttons(post) == [f"pq:{ask.id}:0", f"pq:{ask.id}:1", f"pw:{ask.id}"]

        tap = Query(f"pq:{ask.id}:0")
        await s.front._dispatch_callback(tap)  # type: ignore[arg-type]
        assert tap.said == ["answered"]
        answered = await s.r.manager.asks.get(ask.id)
        assert answered is not None and answered.resolved_by == "operator" and answered.resolution["selected"] == ["Postgres"] and answered.resolution["via"] == "telegram"
        assert s.bot.edited and "answered in Telegram by you: Postgres" in s.bot.edited[-1]["text"]

        late = Query(f"pq:{ask.id}:1")
        await s.front._dispatch_callback(late)  # type: ignore[arg-type]
        assert late.said == ["too late: answered in Telegram by you: Postgres"]

        # Answered in the app first: the chat's message says so, and a tap there is late.
        await s.r.call(sid, "ask_operator", title="Deploy on Friday?", text="Deploy on Friday?", options=["Yes", "No"])
        second = next(a for a in await s.r.manager.asks.open_for(s.r.project.id, routed_to="operator"))
        await until_await(lambda: _sent(s, 2), "the second question was posted")
        await s.r.team.answer(second.id, selected=["No"], by="operator", via="app")

        async def closed() -> bool:
            return any("answered in the app by you: No" in e["text"] for e in s.bot.edited)

        await until_await(closed, "the posted question was closed")
        tap = Query(f"pq:{second.id}:0")
        await s.front._dispatch_callback(tap)  # type: ignore[arg-type]
        assert tap.said == ["too late: answered in the app by you: No"]
        assert len(s.bot.sent) == 2, "each request appears once"
    finally:
        await s.close()


async def _sent(s: Setup, n: int) -> bool:
    return len(s.bot.sent) == n


async def _remembered(s: Setup, ask_id: str | None) -> bool:
    """The post is sent and written down: a reply to it can only be matched once both have happened."""
    return any(entry.get("ask") == ask_id for entry in (await s.topics._posts()).values())


async def test_an_escalated_permission_is_posted_and_decided_and_words_answer_a_question(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await setup(settings, db, tmp_path, monkeypatch)
    try:
        await s.r.orch.enable(s.r.project.id)
        sid = await s.orchestrator()
        await until_await(lambda: _bound(s, sid), "the topic")
        runtime = ObservedFakeStaffRuntime(kind="daedalus")
        runtime.manager = s.r.manager
        s.r.team.runtimes["daedalus"] = runtime
        ada = await s.r.manager.staff.hire(s.r.project.id, name="Ada", isolation="shared")
        await operator_assignment(s.r.team, ada, await board_task(s.r.manager, s.r.project, "Menu"))
        live = await s.r.team.live_of(ada)
        assert live is not None
        ask_id = await s.r.team.ingress.permission(live, "perm-1", "Exec", "npm install")
        await asyncio.sleep(0.1)
        assert s.bot.sent == [], "the orchestrator's to answer first"
        ask = await s.r.manager.asks.get(ask_id)
        assert ask is not None
        await s.r.team.escalate(ask, why="not in the brief")
        await until_await(lambda: _sent(s, 1), "the escalated request was posted")
        [post] = s.bot.sent
        assert "Ada asks for permission" in post["text"] and buttons(post) == [f"pp:{ask_id}:allow", f"pp:{ask_id}:deny"]
        tap = Query(f"pp:{ask_id}:deny")
        await s.front._dispatch_callback(tap)  # type: ignore[arg-type]
        assert tap.said == ["answered"]
        assert runtime.answered and runtime.answered[-1][2].allow is False

        # A question answered in words: "✍️ Answer", then the reply to the prompt.
        await s.r.call(sid, "ask_operator", title="Which colour for the menu?", text="Which colour for the menu?")
        question = next(a for a in await s.r.manager.asks.open_for(s.r.project.id, routed_to="operator"))
        await until_await(lambda: _remembered(s, question.id), "the question was posted")
        await s.front._dispatch_callback(Query(f"pw:{question.id}"))  # type: ignore[arg-type]
        prompt = s.bot.sent[-1]
        assert f"[{question.short_id}]" in prompt["text"]
        replies: list[str] = []
        binding = await s.front.binding_for_session(sid)
        assert binding is not None
        await s.front.on_message(said(reply("Dark green", prompt["id"], chat_id=FORUM, chat_type="supergroup", thread=binding.thread_id), replies))
        answered = await s.r.manager.asks.get(question.id)
        assert answered is not None and answered.resolution["text"] == "Dark green" and replies == [f"Answered [{question.short_id}]."]
        assert s.submitted == [], "an answer is not also a message to the orchestrator"
    finally:
        await s.close()


async def test_the_topic_follows_the_office(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await setup(settings, db, tmp_path, monkeypatch)
    try:
        await s.r.orch.enable(s.r.project.id)
        first = await s.orchestrator()
        await until_await(lambda: _bound(s, first), "the topic")
        binding = await s.front.binding_for_session(first)
        assert binding is not None

        await s.r.orch.replace(s.r.project.id, "a fresh start")
        second = await s.orchestrator()

        async def repointed() -> bool:
            found = await s.front.binding_for_topic(FORUM, binding.thread_id)
            return found is not None and found.session_id == second

        await until_await(repointed, "the topic moved to the successor")
        assert s.bot.topics == ["Bakery"], "the same topic, not a new one"
        old = await s.r.manager.get_state(first)
        assert old is not None and old.metadata.get("telegram_detached") and await s.front.outbox_for_session(first) is None
        assert await s.front.adopt_sessions_into_topics() == 0

        await s.r.manager.projects.update(s.r.project.id, name="Bakery 2")
        await s.r.manager.bus.publish("project.changed", {"change": "settings", "actor": "operator"}, project_id=s.r.project.id)

        async def renamed() -> bool:
            return s.bot.renamed == [(binding.thread_id, "Bakery 2")]

        await until_await(renamed, "the topic was renamed")

        await s.r.orch.disable(s.r.project.id)

        async def closed() -> bool:
            return s.bot.closed == [(FORUM, binding.thread_id)] and (await s.r.refreshed()).settings.orchestrator.telegram_topic_id == 0

        await until_await(closed, "the topic was closed and forgotten")
        assert await s.front.binding_for_topic(FORUM, binding.thread_id) is None
    finally:
        await s.close()


async def test_a_start_reconciles_every_enabled_project(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await setup(settings, db, tmp_path, monkeypatch, mode="private")
    try:
        await s.r.orch.enable(s.r.project.id)  # while no forum was bound
        sid = await s.orchestrator()
        await asyncio.sleep(0.1)
        assert s.bot.topics == []
        s.r.manager.config.telegram.mode = "topics"
        s.r.manager.config.telegram.forum_chat_id = FORUM
        assert await s.topics.reconcile() == 1 and s.bot.topics == ["Bakery"]
        assert await s.topics.reconcile() == 1 and s.bot.topics == ["Bakery"], "idempotent"
        assert (await s.front.binding_for_session(sid)) is not None
    finally:
        await s.close()


async def test_in_private_mode_the_posts_go_to_the_private_chat_under_the_project_name(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await setup(settings, db, tmp_path, monkeypatch, mode="private")
    try:
        # The private chat has its own current session, which must keep receiving plain messages.
        await s.front.on_message(_message("hello"))
        current = await s.front.current_session_id()

        async def greeted() -> bool:
            return (current, "hello") in s.submitted

        await until_await(greeted, "the first message reached the chat's session")
        await s.r.orch.enable(s.r.project.id)
        sid = await s.orchestrator()
        await s.r.call(sid, "project_report", text="Two tasks merged today.", title="Progress")
        await until_await(lambda: _remembered(s, None), "the report was posted")
        [post] = s.bot.sent
        assert post["chat_id"] == OWNER and post.get("message_thread_id") is None and post["text"].startswith("[Bakery]\n\n")
        assert s.bot.topics == []

        # A reply to the report is for the orchestrator, not for the chat's current session.
        replies: list[str] = []
        await s.front.on_message(said(reply("Good, merge the rest tomorrow", post["id"]), replies))
        assert (sid, "Good, merge the rest tomorrow") in s.submitted and replies == ["Sent to the orchestrator of Bakery."]
        # A plain message still goes to the current session.
        await s.front.on_message(_message("and you, carry on"))

        async def carried() -> bool:
            return (current, "and you, carry on") in s.submitted

        await until_await(carried, "the plain message reached the current session")

        # A question, answered by replying to it.
        await s.r.call(sid, "ask_operator", title="Which day for the release?", text="Which day for the release?")
        question = next(a for a in await s.r.manager.asks.open_for(s.r.project.id, routed_to="operator"))
        await until_await(lambda: _remembered(s, question.id), "the question was posted")
        asked = s.bot.sent[-1]
        assert asked["text"].startswith("[Bakery]\n\n❓ The orchestrator asks")
        await s.front.on_message(said(reply("Thursday", asked["id"]), replies))
        answered = await s.r.manager.asks.get(question.id)
        assert answered is not None and answered.resolution.get("text") == "Thursday"
    finally:
        await s.close()


async def test_a_regular_session_keeps_its_topic_routing(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await setup(settings, db, tmp_path, monkeypatch)
    try:
        await s.r.orch.enable(s.r.project.id)
        state, binding = await s.front.create_session_topic("scratch work")
        assert binding.thread_id and await s.front.outbox_for_session(state.session.id) is not None
        await s.front.on_message(_message("in my own topic", chat_type="supergroup", chat_id=FORUM, thread=binding.thread_id))

        async def arrived() -> bool:
            return (state.session.id, "in my own topic") in s.submitted

        await until_await(arrived, "the message reached the regular session")
        # A reply to something that is not a project's post is the front's ordinary business.
        await s.front.on_message(reply("replying to anything", 5, chat_id=FORUM, chat_type="supergroup", thread=binding.thread_id))

        async def replied() -> bool:
            return any(sid == state.session.id and text.endswith("replying to anything") for sid, text in s.submitted)

        await until_await(replied, "the reply reached the regular session")
    finally:
        await s.close()


async def test_without_a_bot_nothing_is_installed() -> None:
    app = SimpleNamespace(front=None, extensions={})
    assert await project_topics.install(app) == []  # type: ignore[arg-type]
    assert "project_topics" not in app.extensions
