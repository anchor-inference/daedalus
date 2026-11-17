"""What the operator sends from the app in a translated session is acknowledged before the translator answers.

The send used to wait for the translation, and the operator watched a frozen composer for as long as the
translator took. Here the translator is held until the test lets it go: the send returns at once, the
session's listeners hear the message as translating and then as settled, in that order, and the agent
still gets the English. Two messages sent back to back reach the agent in the order they were written
even when the second translation comes back first, and a refusal after the acknowledgement comes back as
an event carrying the words.

The same holds for an answer to the agent's question and for answers to its side questions, and for all
three across a restart: what was acknowledged is kept until it is delivered, listed as translating by the
next process at once, and delivered by it in the order it was sent.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.llm import LLMRequest, LLMResponse
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock

from daedalus.config import Settings
from daedalus.extensions import questions, side_questions
from daedalus.host.session_runner import Attachment, SessionManager
from daedalus.stores.database import Database
from tests.support.waiting import until
from tests.unit.test_agent_language_end_to_end import Agent, Translator, model_text, run, started

PHRASES = {"Почему?": "Why?", "А тесты?": "And the tests?", "Оба, по очереди": "Both, one after the other", "оставь снимок навсегда": "keep the snapshot forever"}
QUESTION = {"question": "Which file should I fix first?", "allow_custom": True, "options": [{"label": "The parser"}, {"label": "The writer"}]}


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
            # The answer on its way back to the operator, a question shown to them: not what these tests hold.
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


def settled_of(heard: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event["settled"] for event in heard if "settled" in event]


async def rows(db: Database) -> list[str]:
    return [row["id"] for row in await db.fetchall("SELECT id FROM translating_inputs ORDER BY seq")]


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
    entry = manager.translating(session)[0]
    assert (entry["text"], entry["kind"], entry["from"], entry["to"]) == ("Почему?", "message", "ru", "en")
    # Kept on disk from the acknowledgement on, not only in memory.
    assert await rows(db) == ["m1"]
    await until(lambda: len(heard) == 1, "the message to be announced as translating")
    assert heard[0]["translating"][0]["id"] == "m1" and "settled" not in heard[0]

    translator.gates["Почему?"].set()
    await until(lambda: done == ["completed"], "the run to finish", timeout=20)
    assert "Why?" in model_text(agent.requests[0]) and "Почему" not in model_text(agent.requests[0])
    # Translating, then settled as sent with the translation done, and nothing left on the list or on disk.
    await until(lambda: len(heard) == 2, "the message to settle")
    assert heard[1]["translating"] == [] and heard[1]["settled"] == {"id": "m1", "kind": "message", "text": "Почему?", "outcome": "sent", "state": "done"}
    assert manager.translating(session) == [] and await rows(db) == []
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
    assert [settled["id"] for settled in settled_of(heard)] == ["m1", "m2"]
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
    await until(lambda: len(settled_of(heard)) == 1, "the message to settle")
    settled = settled_of(heard)[0]
    assert settled["outcome"] == "sent" and settled["state"] == "failed" and "translator" in settled["error"]
    assert "Почему?" in model_text(agent.requests[0])

    # Refused after the acknowledgement (the host is installing its dependencies meanwhile): the words come
    # back, and the row goes, for a refusal is an outcome where a stop is not.
    translator.failing = False
    await manager.submit(session, "А тесты?", client_message_id="m2", background_translation=True)
    maintenance = settings.state_dir / "dependencies" / "maintenance"
    maintenance.parent.mkdir(parents=True, exist_ok=True)
    maintenance.touch()
    translator.gates["А тесты?"].set()
    await until(lambda: len(settled_of(heard)) == 2, "the second message to settle", timeout=20)
    refused = settled_of(heard)[-1]
    assert refused["outcome"] == "refused" and refused["text"] == "А тесты?" and "dependencies" in refused["error"]
    assert await rows(db) == []
    maintenance.unlink()
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


async def test_a_restart_mid_translation_delivers_what_was_acknowledged_in_order(settings: Settings, db: Database) -> None:
    """The process stops while the translator has two messages, one with a file. The next one lists them
    as translating before it delivers anything, then delivers both, the file with the first, in the
    order they were sent; a stop is not a refusal, so nobody was told they were refused."""
    first = HeldTranslator()
    manager = await started(settings, db, Agent([]), first)
    heard = listen(manager)
    session = (await manager.create_session("t")).session.id
    await manager.set_agent_language(session, "en")
    staged = settings.state_dir / "upload-staging" / "notes.part"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("the notes")

    await manager.submit(session, "Почему?", [Attachment(path=staged, name="notes.txt", mime_type="text/plain")], client_message_id="m1", background_translation=True)
    # What the front does as soon as the send returns: its staging is no place to keep the file.
    staged.unlink(missing_ok=True)
    await manager.submit(session, "А тесты?", client_message_id="m2", background_translation=True)
    await until(lambda: len(first.asked) == 2, "both messages to be with the translator")
    await manager.close()
    assert settled_of(heard) == []
    assert await rows(db) == ["m1", "m2"]

    agent = Agent(["One.", "Two."])
    second = HeldTranslator()
    restarted = await started(settings, db, agent, second)
    try:
        # Listed before anything is delivered, so the app draws them as translating after the restart too.
        listed = restarted.translating(session)
        assert [(entry["id"], entry["text"], entry["kind"]) for entry in listed] == [("m1", "Почему?", "message"), ("m2", "А тесты?", "message")]
        assert listed[0]["files"] == ["notes.txt"]
        heard = listen(restarted)
        done = await finished_runs(restarted, session)
        assert await restarted.resume_translating() == 2
        await until(lambda: len(second.asked) == 2, "the next process to translate both again")
        second.gates["А тесты?"].set()
        await asyncio.sleep(0.2)
        assert agent.requests == []
        second.gates["Почему?"].set()
        await until(lambda: [settled["id"] for settled in settled_of(heard)] == ["m1", "m2"], "both to be delivered", timeout=20)
        await until(lambda: any("And the tests?" in model_text(request) for request in agent.requests), "the second to reach the agent", timeout=20)
        await until(lambda: len(done) >= 1 and not restarted._states[session].running, "the runs to finish", timeout=20)
        everything = "\n".join(model_text(request) for request in agent.requests)
        assert everything.index("Why?") < everything.index("And the tests?") and "notes.txt" in everything
        assert all(settled["outcome"] == "sent" for settled in settled_of(heard))
        assert restarted.translating(session) == [] and await rows(db) == []
        assert not any((settings.state_dir / "translating").iterdir())
    finally:
        await restarted.close()


async def test_an_answer_to_the_agents_question_is_acknowledged_kept_and_delivered_after_a_restart(settings: Settings, db: Database) -> None:
    """The card's answer with typed words goes like a message: acknowledged at once, listed as translating
    with the question it answers, and a second answer meanwhile refused. A restart while the translator
    has it restores the question and then answers it with the translated words."""
    agent = Agent([{"tool": "AskUser", "args": {"questions": [QUESTION]}}])
    first = HeldTranslator()
    manager = await started(settings, db, agent, first)
    session = (await manager.create_session("t")).session.id
    await manager.set_agent_language(session, "en")
    await run(manager, session, "Почини", ends="awaiting")
    state = await manager.get_state(session)
    assert state is not None and state.pending is not None
    call = state.pending.tool_call_id

    answer = [{"selected": ["The parser"], "custom": "Оба, по очереди"}]
    assert await asyncio.wait_for(manager.answer(session, answer, via="app", background_translation=True), timeout=2) == ""
    [entry] = manager.translating(session)
    assert (entry["kind"], entry["question"], entry["text"]) == ("answer", call, "The parser; Оба, по очереди")
    with pytest.raises(RuntimeError, match="already being translated"):
        await manager.answer(session, [{"selected": ["The writer"]}], via="telegram")
    await until(lambda: first.asked == ["Оба, по очереди"], "the answer to be with the translator")
    await manager.close()
    assert await rows(db) == [f"answer:{call}"]

    agent.script = ["Fixing both."]
    second = HeldTranslator()
    restarted = await started(settings, db, agent, second)
    try:
        assert [entry["kind"] for entry in restarted.translating(session)] == ["answer"]
        heard = listen(restarted)
        done = await finished_runs(restarted, session)
        await restarted.resume_unfinished()
        await restarted.resume_translating()
        second.gates["Оба, по очереди"].set()
        await until(lambda: done == ["completed"], "the answered run to finish", timeout=20)
        result = [m for m in agent.requests[-1].messages if m.role is MessageRole.tool][-1].content_blocks[0].content  # type: ignore[union-attr]
        assert "The parser" in result and "Both, one after the other" in result and "Оба" not in result
        await until(lambda: len(settled_of(heard)) == 1, "the answer to settle")
        assert settled_of(heard)[0] == {"id": f"answer:{call}", "kind": "answer", "question": call, "text": "The parser; Оба, по очереди", "outcome": "sent", "state": "done"}
        assert await rows(db) == []
    finally:
        await restarted.close()


def application(manager: SessionManager, settings: Settings, db: Database) -> Any:
    return SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, notifications=None)


async def test_side_answers_are_acknowledged_listed_as_translating_and_come_back_open_when_refused(settings: Settings, db: Database) -> None:
    agent = Agent(["Noted."])
    translator = HeldTranslator()
    manager = await started(settings, db, agent, translator)
    app = application(manager, settings, db)
    heard = listen(manager)
    session = (await manager.create_session("t")).session.id
    await manager.set_agent_language(session, "en")
    done = await finished_runs(manager, session)
    await side_questions.ask(app, session_id=session, question="Keep access when a subscription expires?", options=["Snapshot only", "Snapshot and current access"])
    [card] = await questions.waiting(app, session_id=session)

    item = {"ask_id": card["id"], "selected": ["Snapshot only"], "note": "оставь снимок навсегда"}
    result = await asyncio.wait_for(questions.answer(app, [item], project_id=None, via="app", session_id=session, background_translation=True), timeout=2)
    [outcome] = result["results"]
    assert outcome["state"] == "answered" and outcome["translating"] == {"to": "en"} and outcome["delivered"] is False
    # Answered, and still the operator's to see in the tab, as translating, until it reaches the session.
    [listed] = await questions.waiting(app, session_id=session)
    assert listed["id"] == card["id"] and listed["translating"]["to"] == "en"
    [entry] = manager.translating(session)
    assert entry["kind"] == "side" and entry["ask_ids"] == [card["id"]] and entry["text"] == "Snapshot only — оставь снимок навсегда"

    translator.gates["оставь снимок навсегда"].set()
    await until(lambda: done == ["completed"], "the run woken by the answers to finish", timeout=20)
    assert "Answer: Snapshot only — keep the snapshot forever" in model_text(agent.requests[-1])
    await until(lambda: len(settled_of(heard)) == 1, "the answers to settle")
    assert settled_of(heard)[0]["outcome"] == "sent" and settled_of(heard)[0]["ask_ids"] == [card["id"]]
    assert await questions.waiting(app, session_id=session) == [] and await rows(db) == []

    # Refused after the acknowledgement: the question waits again, and the stream says why.
    await side_questions.ask(app, session_id=session, question="And the old exports?", options=["Delete"])
    [again] = await questions.waiting(app, session_id=session)
    translator.gates["оставь снимок навсегда"].clear()
    await questions.answer(app, [{"ask_id": again["id"], "text": "оставь снимок навсегда"}], project_id=None, via="app", session_id=session, background_translation=True)
    maintenance = settings.state_dir / "dependencies" / "maintenance"
    maintenance.parent.mkdir(parents=True, exist_ok=True)
    maintenance.touch()
    translator.gates["оставь снимок навсегда"].set()
    await until(lambda: len(settled_of(heard)) == 2, "the second batch to settle", timeout=20)
    refused = settled_of(heard)[-1]
    assert refused["outcome"] == "refused" and "waits again" in refused["error"]
    [reopened] = await questions.waiting(app, session_id=session)
    assert reopened["id"] == again["id"] and "translating" not in reopened and reopened["resolved_at"] is None
    maintenance.unlink()
    await manager.close()
