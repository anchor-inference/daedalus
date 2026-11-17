"""Translating one of the agent's texts because the operator asked, through the real session runner.

The automatic translation covers a run's final answer and nothing else; a note the agent writes between
its steps ("No MR pipelines… Merging both.") stays in the agent's language. The operator's "Translate"
under such a note goes through the same translator, with the same protection of code, onto the same
display copy, and the history the next run replays never hears of it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.llm import LLMRequest, LLMResponse, ProviderDelta, ProviderDeltaKind
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock

from daedalus.config import Settings
from daedalus.extensions import questions, side_questions
from daedalus.extensions.api_session_share import ROUTES
from daedalus.host import translation
from daedalus.host.events import EventFilter
from daedalus.stores.database import Database
from tests.support.waiting import until_await
from tests.unit import test_agent_language_end_to_end as language
from tests.unit.test_agent_language_end_to_end import CYRILLIC, Agent, Translator, model_text, run, started, views

NOTE = "No MR pipelines on `main` for src/app/config.py. Merging both."
NOTE_RU = "Пайплайнов MR на `main` для src/app/config.py нет. Сливаю обе."
ANSWER = "Merged both."
PHRASES = {
    # Keyed on what the translator is shown: the inline code and the path as tokens. Had either reached
    # it, the lookup would fail the test.
    "No MR pipelines on ⟪0⟫ for ⟪1⟫. Merging both.": "Пайплайнов MR на ⟪0⟫ для ⟪1⟫ нет. Сливаю обе.",
}


class Worker(Agent):
    """The agent: a step may say something and call a tool in the same message, which is what a note is."""

    async def stream_with_tools(self, request: LLMRequest) -> AsyncIterator[ProviderDelta]:
        step = self.script[0] if self.script else None
        if isinstance(step, dict) and step.get("text"):
            yield ProviderDelta(kind=ProviderDeltaKind.text, content=step["text"])
            self.script[0] = {"tool": step["tool"], "args": step["args"]}
        async for delta in super().stream_with_tools(request):
            yield delta


class Book(Translator):
    """The translator, answering from this test's phrase book; held at a gate when asked to be slow."""

    def __init__(self) -> None:
        super().__init__()
        self.gate: asyncio.Event | None = None

    async def complete_text(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self.gate is not None:
            await self.gate.wait()
        if self.failing:
            raise RuntimeError("translator is down")
        shown = "".join(block.text for block in request.messages[-1].content_blocks if isinstance(block, TextBlock))
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=PHRASES[shown])]), stop_reason=StopReason.end_turn)


async def noted(manager: Any, session: str) -> dict[str, Any]:
    """The note between the steps, as the app reads it."""
    return next(view for view in await views(manager, session) if view["role"] == "assistant" and view["tool_calls"])


async def test_a_note_between_steps_is_translated_on_request(settings: Settings, db: Database, tmp_path: Any) -> None:
    probe = tmp_path / "probe.txt"
    probe.write_text("x")
    agent = Worker([{"text": NOTE, "tool": "Read", "args": {"path": str(probe)}}, ANSWER, "Fine."])
    translator = Book()
    manager = await started(settings, db, agent, translator)
    try:
        session = (await manager.create_session("t")).session.id
        # The session translates nothing: the button works whatever the mode says.
        await manager.set_agent_language(session, "")
        await run(manager, session, "Merge them")
        await asyncio.sleep(0.3)  # anything spawned at the end of the run has had its chance
        assert translator.calls == []
        note = await noted(manager, session)
        assert note["text"] == NOTE and note.get("translation") is None
        state = await manager.get_state(session)
        assert state is not None

        # Two clicks at once, from two windows: one call to the translator, both answered.
        translator.gate = asyncio.Event()
        first = asyncio.create_task(manager.translations.asked(state, note["seq"]))
        await asyncio.sleep(0.05)
        second = await manager.translations.asked(state, note["seq"])
        assert (second.metadata.get(translation.METADATA_KEY) or {}).get("state") == "pending"
        assert (await noted(manager, session))["translation"]["state"] == "pending"
        translator.gate.set()
        await first
        assert len(translator.calls) == 1

        # The display copy holds the translation, the code and the path byte for byte; the original is the alternate.
        shown = await noted(manager, session)
        assert shown["text"] == NOTE_RU
        assert shown["translation"]["state"] == "done" and shown["translation"]["alternate"] == NOTE
        assert (shown["translation"]["from"], shown["translation"]["to"]) == ("", "ru")
        assert all("`main`" not in "".join(b.text for b in call.messages[-1].content_blocks if isinstance(b, TextBlock)) for call in translator.calls)
        # The translator was told to recognise the language, the session naming none.
        assert "the language it is written in" in "".join(b.text for b in translator.calls[0].messages[0].content_blocks if isinstance(b, TextBlock))

        # Done is done: a third click pays for nothing.
        await manager.translations.asked(state, note["seq"])
        assert len(translator.calls) == 1

        # Booked under the session, labelled as translation.
        assert all(call.observability.session_id == session and call.observability.call_purpose == translation.PURPOSE and call.observability.call_category == "assist" for call in translator.calls)

        # The working history keeps the agent's words, and the next run replays them and nothing else.
        stored = await manager.sessions.list_messages(session, "daedalus", limit=100)
        assert any(NOTE in m.text for m in stored)
        assert not any(translation.METADATA_KEY in m.metadata for m in stored if m.role is MessageRole.assistant)
        assert not CYRILLIC.search("\n".join(m.text for m in stored))
        await run(manager, session, "Thanks")
        replay = model_text(agent.requests[-1])
        assert NOTE in replay and not CYRILLIC.search(replay), replay
    finally:
        await manager.close()


async def test_a_failed_request_keeps_the_original_and_may_be_asked_again(settings: Settings, db: Database) -> None:
    agent = Worker([ANSWER])
    translator = Book()
    translator.failing = True
    manager = await started(settings, db, agent, translator)
    try:
        session = (await manager.create_session("t")).session.id
        await manager.set_agent_language(session, "")
        await run(manager, session, "Merge them")
        answer = next(view for view in await views(manager, session) if view["role"] == "assistant")
        state = await manager.get_state(session)
        assert state is not None
        failed = await manager.translations.asked(state, answer["seq"])
        mark = failed.metadata[translation.METADATA_KEY]
        assert mark["state"] == "failed" and "translator is down" in mark["error"]
        shown = next(view for view in await views(manager, session) if view["role"] == "assistant")
        assert shown["text"] == ANSWER and "alternate" not in shown["translation"]
        # Asked again once the translator is back, it is tried again rather than refused as done.
        translator.failing = False
        again = await manager.translations.asked(state, answer["seq"])
        assert len(translator.calls) == 2
        # The phrase book has no entry for it, so it fails again, and says so in its own words.
        assert "KeyError" in again.metadata[translation.METADATA_KEY]["error"]

        # Only the agent's own texts: the operator's message and a row of another session are refused.
        mine = next(view for view in await views(manager, session) if view["role"] == "user")
        with pytest.raises(ValueError):
            await manager.translations.asked(state, mine["seq"])
        with pytest.raises(LookupError):
            await manager.translations.asked(state, 10_000_000)
    finally:
        await manager.close()


def test_the_route_is_open_to_a_live_link_visitor() -> None:
    path = "/api/sessions/" + "a" * 32 + "/messages/42/translate"
    assert any(method == "POST" and pattern.fullmatch(path) for method, pattern, _ in ROUTES)


@pytest.mark.parametrize("origin", ["job", "events", "side_questions", "reminder"])
async def test_a_run_woken_by_a_job_is_translated_and_announces_nothing(settings: Settings, db: Database, origin: str) -> None:
    """The answer of a run a finished job, a project's events, answers to side questions or a reminder
    woke is in the chat like any other and is translated; it was never a "finished" notice or a held
    Telegram answer, and translating it must not make it one."""
    PHRASES.update(language.PHRASES)
    agent = Worker([language.AGENT_ANSWERS])
    translator = Book()
    manager = await started(settings, db, agent, translator)
    finished: list[dict[str, Any]] = []
    announced: list[dict[str, Any]] = []

    async def heard(event: Any) -> None:
        (announced if event.type == "answer.translated" else finished).append(dict(event.payload))

    listener = manager.bus.on(EventFilter(types=("run.finished", "answer.translated")), heard, name="test-job-translated")
    try:
        session = (await manager.create_session("t")).session.id
        await manager.set_agent_language(session, "en")
        done = asyncio.Event()

        async def on_finished(sid: str, run_id: str, status: str) -> None:
            if sid == session:
                done.set()

        manager.on_finished(on_finished)
        run_id = await manager.submit(session, "Job 7 finished: exit 0.", as_answer=False, origin=origin)
        await asyncio.wait_for(done.wait(), timeout=30)

        answer = await language.settled_answer(manager, session, "done")
        assert answer["text"] == "Готово: `parse` теперь закрывает файл. См. src/app/config.py."
        assert answer["translation"]["alternate"] == language.AGENT_ANSWERS
        # The job's words went to the agent as the host wrote them: only the operator's are translated out.
        assert "Job 7 finished: exit 0." in model_text(agent.requests[0])
        assert len(translator.calls) == 1

        # Nothing new is said: the run finished as a quiet one, Telegram holds nothing, no translated notice.
        await asyncio.sleep(0.3)
        assert finished and finished[0]["operator_facing"] is False and "translating" not in finished[0]
        assert announced == []
        assert not manager.translations.waits_for(run_id)
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        await manager.close()


QUESTION = "Keep access when a subscription expires?"
SIDE_PHRASES = {
    QUESTION: "Оставлять доступ, когда подписка истекает?",
    "Snapshot only": "Только снимок",
    "Snapshot and current access": "Снимок и текущий доступ",
    "оставь снимок навсегда": "keep the snapshot forever",
    "Noted.": "Принято.",
}


def application(manager: Any, settings: Settings, db: Database) -> Any:
    return SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, notifications=None)


async def test_a_side_question_is_shown_translated_and_answered_in_the_agents_words(settings: Settings, db: Database) -> None:
    """A side question of a translated session reads in the operator's language in the Questions tab; the
    option chosen is still the agent's own, and the words typed beside it reach the agent translated."""
    PHRASES.update(SIDE_PHRASES)
    agent = Worker(["Noted."])
    translator = Book()
    manager = await started(settings, db, agent, translator)
    app = application(manager, settings, db)
    try:
        session = (await manager.create_session("t")).session.id
        await manager.set_agent_language(session, "en")
        await side_questions.ask(app, session_id=session, question=QUESTION, options=["Snapshot only", "Snapshot and current access"])

        async def translated() -> dict[str, Any] | None:
            [card] = await questions.waiting(app, session_id=session)
            mark = card["detail"].get(translation.ASK_KEY) or {}
            return card if mark.get("state") == "done" else None

        await until_await(translated, "the side question to be translated", timeout=10)
        card = await translated()
        assert card is not None and card["translate_into"] == "ru"
        mark = card["detail"][translation.ASK_KEY]
        assert mark["text"] == SIDE_PHRASES[QUESTION] and mark["options"] == ["Только снимок", "Снимок и текущий доступ"]
        assert (mark["from"], mark["to"]) == ("en", "ru") and "asked" not in mark
        # The question and its options are still the agent's: they are what an answer names.
        assert card["text"] == QUESTION and card["options"] == ["Snapshot only", "Snapshot and current access"]
        assert all(call.observability.call_purpose == translation.PURPOSE and call.observability.session_id == session for call in translator.calls)

        done = asyncio.Event()

        async def on_finished(sid: str, run_id: str, status: str) -> None:
            if sid == session:
                done.set()

        manager.on_finished(on_finished)
        result = await questions.answer(app, [{"ask_id": card["id"], "selected": ["Snapshot only"], "note": "оставь снимок навсегда"}], project_id=None, via="app", session_id=session)
        assert [r["state"] for r in result["results"]] == ["answered"]
        await asyncio.wait_for(done.wait(), timeout=30)
        shown = model_text(agent.requests[-1])
        assert "Answer: Snapshot only — keep the snapshot forever" in shown, shown
        assert not CYRILLIC.search(shown), shown
        # The row keeps what the operator typed.
        assert (await manager.asks.get(card["id"])).resolution["note"] == "оставь снимок навсегда"  # type: ignore[union-attr]
        # The message the run was woken with reads in the operator's words, the agent's side one click away.
        [woken] = [view for view in await views(manager, session) if view["role"] == "user" and view["origin"] == side_questions.ORIGIN]
        assert SIDE_PHRASES[QUESTION] in woken["text"] and "Answer: Snapshot only — оставь снимок навсегда" in woken["text"], woken["text"]
        assert woken["translation"]["direction"] == "out" and woken["translation"]["state"] == "done"
        assert "keep the snapshot forever" in woken["translation"]["alternate"] and QUESTION in woken["translation"]["alternate"]
        # Woken by the answers, not by the operator's message: its answer is translated too.
        answer = await language.settled_answer(manager, session, "done")
        assert answer["translation"]["alternate"] == "Noted."
    finally:
        await manager.close()


async def test_any_question_is_translated_on_request_once(settings: Settings, db: Database) -> None:
    PHRASES.update(SIDE_PHRASES)
    translator = Book()
    manager = await started(settings, db, Worker([]), translator)
    app = application(manager, settings, db)
    try:
        session = (await manager.create_session("t")).session.id
        await manager.set_agent_language(session, "")
        await side_questions.ask(app, session_id=session, question=QUESTION, options=["Snapshot only"])
        await asyncio.sleep(0.3)
        [card] = await questions.waiting(app, session_id=session)
        assert translation.ASK_KEY not in card["detail"] and translator.calls == []

        mark = await manager.translations.ask(card["id"], asked=True)
        assert mark == {"from": "", "to": "ru", "asked": True, "state": "done", "title": "", "text": SIDE_PHRASES[QUESTION], "options": ["Только снимок"]}
        assert len(translator.calls) == 2  # the text and the option; an empty title costs nothing
        again = await manager.translations.ask(card["id"], asked=True)
        assert again == mark and len(translator.calls) == 2
        [card] = await questions.waiting(app, session_id=session)
        assert card["detail"][translation.ASK_KEY] == mark
    finally:
        await manager.close()
