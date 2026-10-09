"""A run that ends up on another model says so — on the wire, on the turn, and in Telegram.

The failure this covers is silence, not breakage: the provider chain steps down when an endpoint
refuses or stalls, the run finishes on a different model, and nothing anywhere said which model
wrote the answer. The production database shows it happening — runs whose calls start on one
endpoint and finish on another — with no event, no mark on the message and no line in the chat.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from protocore.contracts.llm import LLMRateLimitError, LLMRequest, LLMStreamIdleError, ProviderDelta, ProviderDeltaKind
from protocore.contracts.runtime_constants import LoopConstants
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import Settings
from daedalus.host.session_runner import MODEL_METADATA_KEY, SessionManager
from daedalus.host.transcript_view import message_view
from daedalus.stores.database import Database
from daedalus.transport.telegram.render import RunRenderer, RunView
from tests.support.models import model_config
from tests.unit.test_session_runner import ScriptedProvider, _wait_finished
from tests.unit.test_telegram_render import FakeOutbox


class RefusingProvider(ScriptedProvider):
    """An endpoint at its quota: every stream it is asked for is refused, as a rate limit."""

    def __init__(self, endpoint_id: str = "primary") -> None:
        super().__init__([])
        self.endpoint = type("_Endpoint", (), {"id": endpoint_id})()
        self.attempts = 0

    async def stream_with_tools(self, request: LLMRequest) -> AsyncIterator[ProviderDelta]:
        self.attempts += 1
        raise LLMRateLimitError("primary: rate limited")
        yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="stop")  # pragma: no cover


def _named(provider: ScriptedProvider, endpoint_id: str) -> ScriptedProvider:
    provider.endpoint = type("_Endpoint", (), {"id": endpoint_id})()
    return provider


async def _manager_with_chain(settings: Settings, db: Database, rungs: list[tuple[Any, str]]) -> SessionManager:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    manager.providers.rungs_for = lambda config, preset_id=None: list(rungs)  # type: ignore[method-assign]
    manager.providers.room_for = lambda config: {}  # type: ignore[method-assign]
    return manager


def _changes(events: list[TurnEvent]) -> list[dict[str, Any]]:
    return [e.payload for e in events if e.type is EventType.MODEL_CHANGED]


async def test_the_run_says_which_model_took_over_and_the_answer_carries_it(settings: Settings, db: Database) -> None:
    primary = RefusingProvider("primary")
    standby = _named(ScriptedProvider([{"text": "the standby answered"}]), "standby")
    manager = await _manager_with_chain(settings, db, [(primary, "opus-5"), (standby, "flash")])
    events: list[TurnEvent] = []

    async def sink(session_id: str, event: TurnEvent) -> None:
        events.append(event)

    manager.add_sink(sink)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "hello")
    assert (await waiter)[0][2] == "completed"

    changes = _changes(events)
    assert len(changes) == 1, f"one demotion, one event: {changes}"
    assert changes[0]["from"] == "opus-5" and changes[0]["to"] == "flash"
    assert changes[0]["configured"] == "opus-5" and changes[0]["fallback"] is True
    assert changes[0]["reason"] == "rate_limit", "the core said it was a quota refusal; the event must not lose that"
    assert primary.attempts >= 1

    # …and the answer itself is stamped, so a reload says the same thing the live stream did.
    history = await manager.sessions.list_transcript(state.session.id)
    answers = [m for m in history if m.role.value == "assistant" and m.metadata.get(MODEL_METADATA_KEY)]
    assert answers, "no assistant turn was stamped with the model that produced it"
    stamp = answers[-1].metadata[MODEL_METADATA_KEY]
    assert stamp["model"] == "flash" and stamp["provider"] == "standby"
    assert stamp["fallback"] == {"from": "opus-5", "to": "flash", "reason": "rate_limit"}
    view = message_view(answers[-1])
    assert view["model"] == "flash" and view["fallback"]["from"] == "opus-5"
    assert manager.model_status(state)["fallback"]["to"] == "flash"
    await manager.close()


async def test_a_run_on_its_own_model_says_nothing(settings: Settings, db: Database) -> None:
    """The mechanism must be invisible when nothing happened: no event, no stamp claiming a fallback."""
    only = _named(ScriptedProvider([{"text": "answered"}]), "primary")
    manager = await _manager_with_chain(settings, db, [(only, "opus-5")])
    events: list[TurnEvent] = []

    async def sink(session_id: str, event: TurnEvent) -> None:
        events.append(event)

    manager.add_sink(sink)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "hello")
    await waiter
    assert _changes(events) == []
    history = await manager.sessions.list_transcript(state.session.id)
    stamps = [m.metadata.get(MODEL_METADATA_KEY) for m in history if m.role.value == "assistant"]
    assert stamps and all(s is not None for s in stamps), "an answer is named even when nothing went wrong"
    assert all("fallback" not in s for s in stamps if s), "nothing was replaced; nothing may say it was"
    assert manager.model_status(state)["fallback"] is None
    await manager.close()


async def test_the_configured_model_answering_again_takes_the_note_away(settings: Settings, db: Database) -> None:
    """A second run on the configured model is not a fallback, whatever the run before it did."""
    primary = RefusingProvider("primary")
    standby = _named(ScriptedProvider([{"text": "one"}]), "standby")
    manager = await _manager_with_chain(settings, db, [(primary, "opus-5"), (standby, "flash")])
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "hello")
    await waiter
    assert manager.model_status(state)["fallback"] is not None

    healthy = _named(ScriptedProvider([{"text": "two"}]), "primary")
    manager.providers.rungs_for = lambda config, preset_id=None: [(healthy, "opus-5"), (standby, "flash")]  # type: ignore[method-assign]
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "again")
    await waiter
    status = manager.model_status(state)
    assert status["fallback"] is None and status["effective_model"] == "opus-5"
    await manager.close()


class ToolThenRefusingProvider(ScriptedProvider):
    """Answers the first request with a tool call, then refuses everything after it, as a rate limit.

    This is the shape of the run the whole feature exists for: the configured model writes a turn,
    and only the *next* request is the one the chain has to step down from.
    """

    def __init__(self, endpoint_id: str = "primary") -> None:
        super().__init__([{"tool": "Exec", "args": {"command": "echo hello"}}])
        self.endpoint = type("_Endpoint", (), {"id": endpoint_id})()
        self.calls = 0

    async def stream_with_tools(self, request: LLMRequest) -> AsyncIterator[ProviderDelta]:
        self.calls += 1
        if self.calls == 1:
            async for delta in super().stream_with_tools(request):
                yield delta
            return
        raise LLMRateLimitError("primary: rate limited")


async def test_a_turn_written_before_the_demotion_keeps_the_model_that_wrote_it(settings: Settings, db: Database) -> None:
    """The turn the configured model produced must not be re-attributed to the model that replaced it.

    Transcript rows are written once and never revisited, so a turn stamped with whoever is answering
    at *write* time is misattributed for ever — and a persist is fire-and-forget, so the write of one
    round routinely lands after the next round has already stepped the chain down.
    """
    primary = ToolThenRefusingProvider("primary")
    standby = _named(ScriptedProvider([{"text": "the standby finished it"}]), "standby")
    manager = await _manager_with_chain(settings, db, [(primary, "m-a"), (standby, "m-b")])
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "hello")
    assert (await waiter)[0][2] == "completed"

    history = await manager.sessions.list_transcript(state.session.id)
    stamps = [m.metadata.get(MODEL_METADATA_KEY) for m in history if m.role.value == "assistant"]
    assert len(stamps) == 2, f"a tool round and an answer: {[m.role.value for m in history]}"
    assert stamps[0] == {"provider": "primary", "model": "m-a", "configured": "m-a"}, "the tool call was m-a's work"
    assert stamps[1]["model"] == "m-b" and stamps[1]["fallback"] == {"from": "m-a", "to": "m-b", "reason": "rate_limit"}
    await manager.close()


async def test_telegram_puts_one_line_under_an_answer_a_fallback_wrote(tmp_path: Path) -> None:
    outbox = FakeOutbox()
    view = RunView(run_id="r1", model="opus-5")
    renderer = RunRenderer(outbox, view, edit_interval=0.0)
    await renderer.handle(
        TurnEvent(
            type=EventType.MODEL_CHANGED,
            run_id="r1",
            payload={"to": "flash", "model_name": "flash", "configured": "opus-5", "reason": "rate_limit", "fallback": True},
        )
    )
    await renderer.handle(
        TurnEvent(type=EventType.CONTENT_BLOCK_DELTA, run_id="r1", payload={"delta": {"type": "text_delta", "text": "the answer"}})
    )
    await renderer.finish("completed", workspace=tmp_path)
    delivered = "\n".join(text for text, _ in outbox.sent)
    assert "the answer" in delivered
    assert "answered by flash" in delivered and "opus-5 was unavailable" in delivered
    renderer.close()


async def test_telegram_says_nothing_when_the_configured_model_answered(tmp_path: Path) -> None:
    outbox = FakeOutbox()
    renderer = RunRenderer(outbox, RunView(run_id="r1", model="opus-5"), edit_interval=0.0)
    await renderer.handle(
        TurnEvent(type=EventType.CONTENT_BLOCK_DELTA, run_id="r1", payload={"delta": {"type": "text_delta", "text": "the answer"}})
    )
    await renderer.finish("completed", workspace=tmp_path)
    assert all("answered by" not in text for text, _ in outbox.sent)
    renderer.close()


class _Verdict:
    """What the core's idle watchdog pins on the error it raises; nothing above the core can label it."""

    reason = "timeout"


class SilentProvider(ScriptedProvider):
    """A model that thinks past the idle budget on its first ``silences`` calls, then answers.

    It raises what the core's watchdog raises at the deadline rather than sleeping until it, so no
    test here waits on a clock.
    """

    def __init__(self, endpoint_id: str, silences: int, text: str) -> None:
        super().__init__([{"text": text}])
        self.endpoint = type("_Endpoint", (), {"id": endpoint_id})()
        self.silences = silences
        self.attempts = 0

    async def stream_with_tools(self, request: LLMRequest) -> AsyncIterator[ProviderDelta]:
        self.attempts += 1
        if self.attempts <= self.silences:
            error = LLMStreamIdleError("LLM stream idle for >90.0s (window=baseline, last_delta_seen=thinking=False)")
            object.__setattr__(error, "classified", _Verdict())
            raise error
        async for delta in super().stream_with_tools(request):
            yield delta


@pytest.fixture
def no_backoff() -> Iterator[None]:
    """The in-place retry waits a backoff on the real clock; these tests are about where it goes, not how long."""
    field = LoopConstants.model_fields["llm_transient_error_retry_backoff_base_seconds"]
    original = field.default
    field.default = 0.0
    LoopConstants.model_rebuild(force=True)
    try:
        yield
    finally:
        field.default = original
        LoopConstants.model_rebuild(force=True)


async def test_one_long_silent_think_keeps_the_run_on_its_model(settings: Settings, db: Database, no_backoff: None) -> None:
    """The first silence is retried on the same model: no demotion, no notice, no hold on the model."""
    primary = SilentProvider("primary", silences=1, text="opus answered after a long think")
    standby = _named(ScriptedProvider([{"text": "the standby answered"}]), "standby")
    manager = await _manager_with_chain(settings, db, [(primary, "opus-5"), (standby, "flash")])
    events: list[TurnEvent] = []

    async def sink(session_id: str, event: TurnEvent) -> None:
        events.append(event)

    manager.add_sink(sink)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "hello")
    assert (await waiter)[0][2] == "completed"

    assert primary.attempts == 2
    assert standby.requests == []
    assert _changes(events) == []
    assert manager.model_status(state)["fallback"] is None
    await manager.close()


async def test_a_silence_that_repeats_moves_the_run_and_says_it_stalled(settings: Settings, db: Database, no_backoff: None) -> None:
    primary = SilentProvider("primary", silences=2, text="never")
    standby = _named(ScriptedProvider([{"text": "the standby answered"}]), "standby")
    manager = await _manager_with_chain(settings, db, [(primary, "opus-5"), (standby, "flash")])
    events: list[TurnEvent] = []

    async def sink(session_id: str, event: TurnEvent) -> None:
        events.append(event)

    manager.add_sink(sink)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "hello")
    assert (await waiter)[0][2] == "completed"

    changes = _changes(events)
    assert primary.attempts == 2
    assert [(c["to"], c["reason"]) for c in changes] == [("flash", "stalled")], "a silent stream is not an unreachable provider"
    await manager.close()
