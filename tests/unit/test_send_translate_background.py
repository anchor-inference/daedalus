"""A message sent from the app in a translated session is acknowledged before the translator answers.

The send used to wait for the translation, and the operator watched a frozen composer for as long as the
translator took. Here the translator is held until the test lets it go: the send returns at once, the
session's listeners hear the message as translating and then as settled, in that order, and the agent
still gets the English. Two messages sent back to back reach the agent in the order they were written
even when the second translation comes back first, and a refusal after the acknowledgement comes back as
an event carrying the words.
"""

from __future__ import annotations

import asyncio
from typing import Any

from protocore.contracts.llm import LLMRequest, LLMResponse
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock

from daedalus.config import Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.waiting import until
from tests.unit.test_agent_language_end_to_end import Agent, Translator, model_text, started

PHRASES = {"Почему?": "Why?", "А тесты?": "And the tests?"}


class HeldTranslator(Translator):
    """A translator that answers each phrase only once the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.gates: dict[str, asyncio.Event] = {phrase: asyncio.Event() for phrase in PHRASES}
        self.asked: list[str] = []

    async def complete_text(self, request: LLMRequest) -> LLMResponse:
        shown = "".join(block.text for block in request.messages[-1].content_blocks if isinstance(block, TextBlock))
        self.calls.append(request)
        if shown not in self.gates:
            # The answer on its way back to the operator: not what these tests hold.
            raise RuntimeError("not in the phrase book")
        self.asked.append(shown)
        await self.gates[shown].wait()
        if self.failing:
            raise RuntimeError("translator is down")
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=PHRASES[shown])]), stop_reason=StopReason.end_turn)


def listen(manager: SessionManager) -> list[dict[str, Any]]:
    heard: list[dict[str, Any]] = []

    async def sink(session_id: str, event: Any) -> None:
        if str(getattr(event, "type", "")) == "message_translating":
            heard.append({key: value for key, value in event.payload.items() if not key.startswith("_")})

    manager.add_sink(sink)
    return heard


async def finished_runs(manager: SessionManager, session_id: str) -> list[str]:
    done: list[str] = []

    async def on_finished(sid: str, run_id: str, status: str) -> None:
        if sid == session_id:
            done.append(status)

    manager.on_finished(on_finished)
    return done


async def test_the_send_returns_before_the_translation_and_the_agent_gets_it(settings: Settings, db: Database) -> None:
    agent = Agent(["Because."])
    translator = HeldTranslator()
    manager = await started(settings, db, agent, translator)
    heard = listen(manager)
    session = (await manager.create_session("t")).session.id
    await manager.set_agent_language(session, "en")
    done = await finished_runs(manager, session)

    acknowledged = await asyncio.wait_for(manager.submit(session, "Почему?", client_message_id="m1", background_translation=True), timeout=2)

    assert acknowledged == ""
    # Held: the translator has the words, nothing has reached the agent, and the session says so.
    await until(lambda: len(translator.asked) == 1, "the translator to be asked")
    assert agent.requests == []
    assert [entry["id"] for entry in manager.translating(session)] == ["m1"]
    assert manager.translating(session)[0]["text"] == "Почему?"
    assert (manager.translating(session)[0]["from"], manager.translating(session)[0]["to"]) == ("ru", "en")
    await until(lambda: len(heard) == 1, "the message to be announced as translating")
    assert heard[0]["translating"][0]["id"] == "m1" and "settled" not in heard[0]

    translator.gates["Почему?"].set()
    await until(lambda: done == ["completed"], "the run to finish", timeout=20)
    assert "Why?" in model_text(agent.requests[0]) and "Почему" not in model_text(agent.requests[0])
    # Translating, then settled as sent with the translation done, and nothing left on the list.
    assert len(heard) == 2
    assert heard[1]["translating"] == [] and heard[1]["settled"] == {"id": "m1", "text": "Почему?", "outcome": "sent", "state": "done"}
    assert manager.translating(session) == []
    # The receipt is the one the ordinary path writes, so a retry of the same send is answered from it.
    assert (await manager.live.receipt(session, "m1"))["status"] == "consumed"
    assert await manager.submit(session, "Почему?", client_message_id="m1", background_translation=True) != ""
    assert translator.asked == ["Почему?"]
    await manager.close()


async def test_messages_reach_the_agent_in_the_order_they_were_sent(settings: Settings, db: Database) -> None:
    agent = Agent(["One.", "Two."])
    translator = HeldTranslator()
    manager = await started(settings, db, agent, translator)
    heard = listen(manager)
    session = (await manager.create_session("t")).session.id
    await manager.set_agent_language(session, "en")
    done = await finished_runs(manager, session)

    await manager.submit(session, "Почему?", client_message_id="m1", background_translation=True)
    await manager.submit(session, "А тесты?", client_message_id="m2", background_translation=True)
    await until(lambda: len(translator.asked) == 2, "both translations to run side by side")
    assert [entry["id"] for entry in manager.translating(session)] == ["m1", "m2"]

    # The second translation is back first; it still waits for the first message to be delivered.
    translator.gates["А тесты?"].set()
    await asyncio.sleep(0.2)
    assert agent.requests == []
    translator.gates["Почему?"].set()
    await until(lambda: not manager.translating(session), "both messages to be delivered", timeout=20)
    # The second is a steer into the run the first started, or the next run when that one has ended.
    await until(lambda: any("And the tests?" in model_text(request) for request in agent.requests), "the second message to reach the agent", timeout=20)
    await until(lambda: len(done) >= 1 and not manager._states[session].running, "the runs to finish", timeout=20)

    everything = "\n".join(model_text(request) for request in agent.requests)
    assert everything.index("Why?") < everything.index("And the tests?")
    assert [event["settled"]["id"] for event in heard if "settled" in event] == ["m1", "m2"]
    await manager.close()


async def test_a_failed_translation_goes_as_written_and_a_refusal_comes_back(settings: Settings, db: Database) -> None:
    agent = Agent(["Because."])
    translator = HeldTranslator()
    translator.failing = True
    manager = await started(settings, db, agent, translator)
    heard = listen(manager)
    session = (await manager.create_session("t")).session.id
    await manager.set_agent_language(session, "en")
    done = await finished_runs(manager, session)

    await manager.submit(session, "Почему?", client_message_id="m1", background_translation=True)
    translator.gates["Почему?"].set()
    await until(lambda: done == ["completed"], "the run to finish", timeout=20)
    settled = next(event["settled"] for event in heard if "settled" in event)
    assert settled["outcome"] == "sent" and settled["state"] == "failed" and "translator" in settled["error"]
    assert "Почему?" in model_text(agent.requests[0])

    # Refused after the acknowledgement (the session went away meanwhile): the words come back.
    translator.failing = False
    translator.gates["А тесты?"].clear()
    await manager.submit(session, "А тесты?", client_message_id="m2", background_translation=True)
    manager.shutting_down = True
    translator.gates["А тесты?"].set()
    await until(lambda: sum("settled" in event for event in heard) == 2, "the second message to settle", timeout=20)
    refused = [event["settled"] for event in heard if "settled" in event][-1]
    assert refused["outcome"] == "refused" and refused["text"] == "А тесты?" and "stopping" in refused["error"]
    manager.shutting_down = False
    await manager.close()


async def test_without_a_client_id_or_a_translated_session_the_send_waits_as_before(settings: Settings, db: Database) -> None:
    agent = Agent(["Fine."])
    translator = HeldTranslator()
    manager = await started(settings, db, agent, translator)
    session = (await manager.create_session("t")).session.id
    done = await finished_runs(manager, session)
    # No translation in this session: nothing is held, and the send starts the run itself.
    run_id = await manager.submit(session, "Hello", client_message_id="m1", background_translation=True)
    assert run_id and manager.translating(session) == []
    await until(lambda: done == ["completed"], "the run to finish", timeout=20)
    await manager.close()
