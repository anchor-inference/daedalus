"""Talking to the agent in another language, end to end through the real session runner.

Two scripted models stand in for the two real ones: the agent, which must only ever read English, and
the translator, which answers from a phrase book and records what it was shown. The operator writes in
Russian with a code block; the agent gets English with the code intact; its answer is shown in Russian
with the original kept; the next run replays English only; a translator that fails leaves the original
marked; and a session switched off sends the words as written.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from protocore.contracts.llm import ILLMProvider, LLMRequest, LLMResponse, ProviderDelta, ProviderDeltaKind
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock

from daedalus.config import Settings, TranslationConfig
from daedalus.host import translation
from daedalus.host.events import EventFilter
from daedalus.host.session_runner import SessionManager
from daedalus.providers.pricing import ModelPricing
from daedalus.stores.database import Database
from tests.support.models import VISION_PRESET, model_config
from tests.support.waiting import until, until_await

TRANSLATOR = VISION_PRESET

CODE = "```python\ndef parse(path):\n    return open(path).read()  # не трогай\n```"
OPERATOR_SAYS = f"Почини функцию `parse` в файле src/app/config.py, вот код:\n{CODE}\nИ проверь ещё раз."
AGENT_ANSWERS = "Done: `parse` now closes the file. See src/app/config.py."
CYRILLIC = re.compile(r"[А-Яа-яЁё]")

PHRASES = {
    # The translator is shown the text with the code, the path and the inline name as tokens; the phrase
    # book is keyed on exactly that, so a code block that reached it would find no entry and fail the test.
    "Почини функцию ⟪0⟫ в файле ⟪1⟫, вот код:\n⟪2⟫\nИ проверь ещё раз.": "Fix the function ⟪0⟫ in ⟪1⟫, here is the code:\n⟪2⟫\nAnd check it again.",
    "Done: ⟪0⟫ now closes the file. See ⟪1⟫.": "Готово: ⟪0⟫ теперь закрывает файл. См. ⟪1⟫.",
    "Почему?": "Why?",
    "Because the file was never closed.": "Потому что файл не закрывался.",
    "Q0: Which file should I fix first?\nQ0.0: The parser\nQ0.0.d: It reads the configuration\nQ0.1: The writer": "Q0: Какой файл чинить первым?\nQ0.0: Парсер\nQ0.0.d: Он читает конфигурацию\nQ0.1: Писатель",
    "Оба, по очереди": "Both, one after the other",
}


class Agent(ILLMProvider):
    """The working model: answers each request with the next scripted text and keeps every request."""

    def __init__(self, script: list[str | dict[str, Any]]) -> None:
        self.script = list(script)
        self.requests: list[LLMRequest] = []

    class _Endpoint:
        id = "scripted"
        kind = "scripted"
        pricing = {"scripted-model": ModelPricing(input_limit=128_000, limit_source="scripted test provider")}

        def plan_for(self, model: str) -> bool:
            return False

    endpoint = _Endpoint()

    async def stream_with_tools(self, request: LLMRequest) -> AsyncIterator[ProviderDelta]:
        self.requests.append(request)
        step = self.script.pop(0) if self.script else "nothing left"
        if isinstance(step, dict):
            call_id = f"call_{len(self.requests)}"
            yield ProviderDelta(kind=ProviderDeltaKind.tool_use_start, tool_call_id=call_id, tool_name=step["tool"])
            yield ProviderDelta(kind=ProviderDeltaKind.tool_use_input, tool_call_id=call_id, tool_input_delta=json.dumps(step["args"]))
            yield ProviderDelta(kind=ProviderDeltaKind.tool_use_stop, tool_call_id=call_id, tool_name=step["tool"], tool_input_final=step["args"], is_block_end=True)
            yield ProviderDelta(kind=ProviderDeltaKind.usage, usage={"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0})
            yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="tool_use")
            return
        yield ProviderDelta(kind=ProviderDeltaKind.text, content=step)
        yield ProviderDelta(kind=ProviderDeltaKind.usage, usage={"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0})
        yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="stop")

    async def complete_structured(self, request: LLMRequest, response_schema: dict[str, Any]) -> LLMResponse:
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text='{"summary": "summary"}')]), stop_reason=StopReason.end_turn)

    async def complete_text(self, request: LLMRequest) -> LLMResponse:
        # The host's own helpers (a chat title) also come here; they are not what this test is about.
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Title")]), stop_reason=StopReason.end_turn)

    def count_tokens(self, text: str, model: str | None = None) -> int:
        return len(text) // 4

    async def aclose(self) -> None:
        return None


class Translator(Agent):
    """The translator: looks the masked text up in the phrase book, or fails when told to."""

    def __init__(self) -> None:
        super().__init__([])
        self.calls: list[LLMRequest] = []
        self.failing = False

    async def complete_text(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self.failing:
            raise RuntimeError("translator is down")
        shown = "".join(block.text for block in request.messages[-1].content_blocks if isinstance(block, TextBlock))
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=PHRASES[shown])]), stop_reason=StopReason.end_turn)


def model_text(request: LLMRequest) -> str:
    """Everything the agent was shown of the conversation, the system prompt aside."""
    return "\n".join(block.text for m in request.messages if m.role is not MessageRole.system for block in m.content_blocks if isinstance(block, TextBlock))


async def started(settings: Settings, db: Database, agent: Agent, translator: Translator) -> SessionManager:
    config = model_config(translation=TranslationConfig(operator_language="ru", preset=TRANSLATOR, timeout_seconds=10))
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    manager.providers.rungs_for = lambda config, preset_id=None: [(translator, "translator-model")] if preset_id == TRANSLATOR else [(agent, "scripted-model")]  # type: ignore[method-assign]
    return manager


async def run(manager: SessionManager, session_id: str, text: str, *, ends: str = "completed") -> str:
    done = asyncio.Event()
    finished: list[str] = []

    async def on_finished(sid: str, run_id: str, status: str) -> None:
        if sid == session_id:
            finished.append(status)
            done.set()

    manager.on_finished(on_finished)
    try:
        run_id = await manager.submit(session_id, text)
        await asyncio.wait_for(done.wait(), timeout=30)
    finally:
        manager._finished.remove(on_finished)
    assert finished == [ends]
    return run_id


async def views(manager: SessionManager, session_id: str) -> list[dict[str, Any]]:
    return [view for view in await manager.transcript_page(session_id, tail=50) if not view.get("internal")]


async def settled_answer(manager: SessionManager, session_id: str, state: str) -> dict[str, Any]:
    found: dict[str, Any] = {}

    async def ready() -> bool:
        answers = [view for view in await views(manager, session_id) if view["role"] == "assistant"]
        if answers and (answers[-1].get("translation") or {}).get("state") == state:
            found.update(answers[-1])
            return True
        return False

    await until_await(ready, f"the answer to be {state}", timeout=20)
    return found


async def test_operator_and_agent_speak_through_the_translator(settings: Settings, db: Database) -> None:
    agent = Agent([AGENT_ANSWERS, "Because the file was never closed."])
    translator = Translator()
    manager = await started(settings, db, agent, translator)
    announced: list[dict[str, Any]] = []

    async def heard(event: Any) -> None:
        announced.append(dict(event.payload))

    listener = manager.bus.on(EventFilter(types=("run.finished", "answer.translated")), heard, name="test-translated")
    try:
        session = (await manager.create_session("t")).session.id
        assert (await manager.set_agent_language(session, "en"))["active"] is True

        await run(manager, session, OPERATOR_SAYS)
        answer = await settled_answer(manager, session, "done")

        # 1 and 2: the agent got English, the code block byte for byte, and no Russian at all.
        first = model_text(agent.requests[0])
        assert "Fix the function `parse` in src/app/config.py, here is the code:" in first
        assert CODE in first
        assert not CYRILLIC.search(first.replace(CODE, "")), first  # nothing Russian outside the code
        assert "не трогай" in CODE  # the comment inside the code is the operator's and travels as it is
        # The translator never saw the code either: it was a token.
        assert all(CODE not in "".join(b.text for b in call.messages[-1].content_blocks if isinstance(b, TextBlock)) for call in translator.calls)

        # 3: the operator reads their own words and the answer in Russian, with the other side kept.
        shown = await views(manager, session)
        mine = next(view for view in shown if view["role"] == "user")
        assert mine["text"] == OPERATOR_SAYS
        assert mine["translation"]["direction"] == "out" and mine["translation"]["state"] == "done"
        assert CODE in mine["translation"]["alternate"] and "Fix the function" in mine["translation"]["alternate"]
        assert answer["text"] == "Готово: `parse` теперь закрывает файл. См. src/app/config.py."
        assert answer["translation"]["alternate"] == AGENT_ANSWERS
        assert (answer["translation"]["from"], answer["translation"]["to"]) == ("en", "ru")

        # The finished-run event held the summary back, and the translated one carries it in Russian.
        await until(lambda: any(event.get("translation") == "done" for event in announced), "the translated answer to be announced", timeout=10)
        finished = next(event for event in announced if "translation" not in event)
        assert finished["translating"] is True and "summary" not in finished
        assert next(event for event in announced if event.get("translation") == "done")["summary"].startswith("Готово")

        # The translator's calls are booked under the session, labelled as translation.
        assert translator.calls and all(call.observability.session_id == session and call.observability.call_purpose == translation.PURPOSE and call.observability.call_category == "assist" for call in translator.calls)

        # 4: the next run replays the history: English only, the translation of the answer nowhere in it.
        await run(manager, session, "Почему?")
        await settled_answer(manager, session, "done")
        replay = model_text(agent.requests[1])
        assert AGENT_ANSWERS in replay and "Why?" in replay
        assert not CYRILLIC.search(replay.replace(CODE, "")), replay
        stored = "\n".join(m.text for m in await manager.sessions.list_messages(session, "daedalus", limit=100))
        assert "Готово" not in stored and "Потому что" not in stored
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        await manager.close()


async def test_a_failing_translator_falls_back_to_the_original(settings: Settings, db: Database) -> None:
    agent = Agent([AGENT_ANSWERS])
    translator = Translator()
    translator.failing = True
    manager = await started(settings, db, agent, translator)
    try:
        session = (await manager.create_session("t")).session.id
        await manager.set_agent_language(session, "en")
        await run(manager, session, OPERATOR_SAYS)
        answer = await settled_answer(manager, session, "failed")

        # 5: the turn was not blocked: the agent got the words as written, and both rows say so.
        assert OPERATOR_SAYS in model_text(agent.requests[0])
        mine = next(view for view in await views(manager, session) if view["role"] == "user")
        assert mine["text"] == OPERATOR_SAYS and mine["translation"]["state"] == "failed" and "alternate" not in mine["translation"]
        assert answer["text"] == AGENT_ANSWERS
        assert "translator is down" in answer["translation"]["error"]
    finally:
        await manager.close()


async def test_turning_the_mode_off_sends_the_words_as_written(settings: Settings, db: Database) -> None:
    agent = Agent(["Сделано.", AGENT_ANSWERS])
    translator = Translator()
    manager = await started(settings, db, agent, translator)
    try:
        session = (await manager.create_session("t")).session.id
        await manager.set_agent_language(session, "en")
        # 6: off for this session, whatever the default says.
        view = await manager.set_agent_language(session, "")
        assert view == {"agent": "", "operator": "ru", "active": False, "explicit": True, "default": ""}
        await run(manager, session, OPERATOR_SAYS)
        await asyncio.sleep(0.3)  # anything spawned at the end of the run has had its chance
        assert OPERATOR_SAYS in model_text(agent.requests[0])
        assert translator.calls == []
        assert all(view.get("translation") is None for view in await views(manager, session))

        # The settings' default applies to a session that never chose, and following it again works.
        manager.config.translation.agent_language = "en"
        assert (await manager.set_agent_language(session, None)) == {"agent": "en", "operator": "ru", "active": True, "explicit": False, "default": "en"}
    finally:
        await manager.close()


async def test_a_question_is_shown_translated_and_answered_in_the_agents_words(settings: Settings, db: Database) -> None:
    question = {"question": "Which file should I fix first?", "allow_custom": True, "options": [{"label": "The parser", "description": "It reads the configuration"}, {"label": "The writer"}]}
    agent = Agent([{"tool": "AskUser", "args": {"questions": [question]}}, "Fixing both.", AGENT_ANSWERS])
    translator = Translator()
    manager = await started(settings, db, agent, translator)
    try:
        session = (await manager.create_session("t")).session.id
        await manager.set_agent_language(session, "en")
        await run(manager, session, OPERATOR_SAYS, ends="awaiting")
        state = await manager.get_state(session)
        assert state is not None and state.pending is not None
        shown = state.pending.payload["questions"][0]
        # The card reads in Russian; the options keep the agent's labels, which is what an answer names.
        assert shown["translation"] == {"question": "Какой файл чинить первым?", "options": [{"label": "Парсер", "description": "Он читает конфигурацию"}, {"label": "Писатель", "description": ""}]}
        assert [option["label"] for option in shown["options"]] == ["The parser", "The writer"]
        assert manager._ask_payload(state, state.pending)["questions"][0]["question"] == "Какой файл чинить первым?"

        done = asyncio.Event()

        async def on_finished(sid: str, run_id: str, status: str) -> None:
            if status == "completed":
                done.set()

        manager.on_finished(on_finished)
        await manager.answer(session, [{"selected": ["The parser"], "custom": "Оба, по очереди"}], via="app")
        await asyncio.wait_for(done.wait(), timeout=30)
        result = [m for m in agent.requests[-1].messages if m.role is MessageRole.tool][-1].content_blocks[0].content  # type: ignore[union-attr]
        assert "The parser" in result and "Both, one after the other" in result and not CYRILLIC.search(result)
    finally:
        await manager.close()
