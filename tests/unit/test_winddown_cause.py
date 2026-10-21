"""A run the model endpoint refused ends as a failure, and says so in the provider's own words.

The incident: a fresh session, one message, and an answer that apologised for running out of
budget. Nothing had run out. The endpoint had rejected every request — ``HTTP 400 … failed to
parse grammar`` — the core's retries were spent, and the run was wound down. The wind-down notice
was the one text used for every bound, so the model was told it had reached its budget; it
believed it, decided it must have done work, and wrote a closing summary of a run that never
started. The operator saw a polite non-answer and no error anywhere.

Three things have to hold, and each is a test here: a run that produced nothing is not wound down
at all, a wind-down the provider caused names the provider rather than a budget, and a real budget
keeps the wording it had.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
from protocore.contracts.llm import LLMProviderError, LLMRequest, ProviderDelta, ProviderDeltaKind
from protocore.contracts.outcome import RunOutcome
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.host import engine_factory
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.transport.telegram.render import RunRenderer, RunView
from tests.support.models import model_config
from tests.unit.test_session_runner import ScriptedProvider, _wait_finished
from tests.unit.test_telegram_render import FakeOutbox

REFUSAL = 'HTTP 400: {"error":{"code":400,"message":"Failed to initialize samplers: failed to parse grammar"}}'


class GrammarRefusingProvider(ScriptedProvider):
    """An endpoint that rejects the request itself: every stream, the same 400.

    Not a blip and not a quota — nothing the run can wait out, and nothing another attempt
    changes. ``script`` is answered normally until ``fail_from``, so a run can be given real work
    to do before the endpoint stops taking it.
    """

    def __init__(self, script: list[dict[str, Any]] | None = None, *, refuse: set[int] | None = None) -> None:
        super().__init__(list(script or []))
        self.attempts = 0
        self._refuse = refuse
        """Which attempts are refused, counted from one; ``None`` refuses every one of them."""

    async def stream_with_tools(self, request: LLMRequest) -> AsyncIterator[ProviderDelta]:
        self.attempts += 1
        if self._refuse is None or self.attempts in self._refuse:
            raise LLMProviderError(REFUSAL)
        async for delta in super().stream_with_tools(request):
            yield delta


def _no_retries(monkeypatch: Any) -> None:
    """The bounded in-place retry, spent. The wait it would cost buys nothing a test needs."""
    original = engine_factory.runtime_constants

    def patched(*args: Any, **kwargs: Any) -> Any:
        return original(*args, **kwargs).model_copy(
            update={"llm_transient_error_retry_max_attempts": 0}
        )

    monkeypatch.setattr(engine_factory, "runtime_constants", patched)


async def _manager(settings: Settings, db: Database, provider: ScriptedProvider) -> SessionManager:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    manager.providers.rungs_for = lambda config, preset_id=None: [(provider, "scripted-model")]  # type: ignore[method-assign]
    manager.providers.room_for = lambda config: {}  # type: ignore[method-assign]
    return manager


def _notified(events: list[TurnEvent]) -> list[dict[str, Any]]:
    return [
        e.payload
        for e in events
        if e.type is EventType.STATE_CHANGED and e.payload.get("reason") == "soft_stop_notified"
    ]


async def test_a_refused_run_fails_instead_of_apologising(
    settings: Settings, db: Database, monkeypatch: Any
) -> None:
    """The first request never reached the model, so there is nothing to write an answer about."""
    _no_retries(monkeypatch)
    provider = GrammarRefusingProvider()
    manager = await _manager(settings, db, provider)
    events: list[TurnEvent] = []

    async def sink(session_id: str, event: TurnEvent) -> None:
        events.append(event)

    manager.add_sink(sink)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "напиши простенький сайт")
    assert (await waiter)[0][2] == "failed"

    assert not _notified(events), "a run that produced nothing is not asked to sum it up"
    errors = [e for e in events if e.type is EventType.ERROR]
    assert errors, "the operator is told, on the wire, not only in the reply"
    assert "failed to parse grammar" in str(errors[-1].payload.get("message") or "")
    assert errors[-1].payload.get("kind") == "llm_provider_error"

    history = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    assert [m.role.value for m in history] == ["user"], "no invented closing statement"
    assert state.last_error_kind == "llm_provider_error"
    await manager.close()


async def test_reasoning_only_exhaustion_is_failed_even_after_a_closing_answer(settings: Settings, db: Database) -> None:
    class StalledProvider(ScriptedProvider):
        async def stream_with_tools(self, request: LLMRequest) -> AsyncIterator[ProviderDelta]:
            if len(self.requests) < 4:
                self.requests.append(request)
                yield ProviderDelta(kind=ProviderDeltaKind.thinking, content="Considering the task.")
                yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="stop")
            else:
                async for delta in super().stream_with_tools(request):
                    yield delta

    provider = StalledProvider([{"text": "Could not finish."}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("stalled")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "Explain the result")
    assert (await waiter)[0][2] == "failed"
    assert state.run_outcome is RunOutcome.failed and state.run_cause is not None and state.run_cause.kind == "model_no_progress"
    assert state.last_error_kind == "model_no_progress"
    assert state.outage_task is None
    assert (await manager.list_sessions(ids=[state.session.id]))[0]["status"] == "failed"
    await manager.close()


async def test_the_operator_sees_the_refusal_in_the_app_and_in_telegram(
    settings: Settings, db: Database, monkeypatch: Any
) -> None:
    """Both fronts. The app reads the session as failed and carries the provider's words; the
    Telegram view puts the same line in the run it is drawing."""
    _no_retries(monkeypatch)
    provider = GrammarRefusingProvider()
    manager = await _manager(settings, db, provider)
    view = RunView(run_id="r1", model="m")
    renderer = RunRenderer(FakeOutbox(), view, edit_interval=0.0)

    async def sink(session_id: str, event: TurnEvent) -> None:
        await renderer.handle(event)

    manager.add_sink(sink)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "сделай сайт")
    await waiter

    assert any("failed to parse grammar" in line for line in view.narration), view.narration
    assert state.last_error_message and "failed to parse grammar" in state.last_error_message

    # And the app: the session reads as failed and hands the reason over, so the screen has
    # something to draw besides an idle chip.
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        detail = (await client.get(f"/api/sessions/{state.session.id}", headers={"X-Daedalus-Token": "tok"})).json()
    assert detail["status"] == "failed"
    assert "failed to parse grammar" in detail["error"]
    await manager.close()


async def test_a_run_that_did_work_is_wound_down_without_claiming_a_budget(
    settings: Settings, db: Database, monkeypatch: Any
) -> None:
    """Work happened, then the endpoint went. The run closes — and is told why, truthfully.

    The notice is the whole point: told it reached its budget, the model reports on turns it was
    never given. Told the endpoint failed, it has one true thing to say.
    """
    _no_retries(monkeypatch)
    # A tool call lands, the stream after it is refused, and the endpoint takes the wind-down's
    # own turn — so the notice really reaches the model and its closing answer is written.
    provider = GrammarRefusingProvider(
        [
            {"tool": "Exec", "args": {"command": "echo working"}},
            {"text": "the endpoint stopped answering; nothing further was done"},
        ],
        refuse={2},
    )
    manager = await _manager(settings, db, provider)
    events: list[TurnEvent] = []

    async def sink(session_id: str, event: TurnEvent) -> None:
        events.append(event)

    manager.add_sink(sink)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "run it")
    assert (await waiter)[0][2] == "failed", "the reply is not the only trace of the failure"

    notified = _notified(events)
    assert notified and notified[0]["soft_stop_cause"] == "provider_error"
    assert "failed to parse grammar" in str(notified[0].get("soft_stop_detail") or "")
    assert state.run_outcome is RunOutcome.failed and state.run_cause is not None and state.run_cause.kind == "provider_error"
    assert "failed to parse grammar" in state.run_cause.detail

    notice = _wind_down_notice(manager, state)
    assert notice, "the model is told what happened"
    assert "reached its budget" not in notice
    assert "model endpoint failed" in notice
    errors = [e for e in events if e.type is EventType.ERROR]
    assert errors and "failed to parse grammar" in str(errors[-1].payload.get("message") or "")
    await manager.close()


async def test_a_run_that_really_runs_out_of_turns_keeps_the_wording_it_had(
    settings: Settings, db: Database, monkeypatch: Any
) -> None:
    """A budget that was really spent is still reported as a budget, and the run still answers."""
    original = engine_factory.runtime_constants

    def patched(*args: Any, **kwargs: Any) -> Any:
        return original(*args, **kwargs).model_copy(update={"max_turns_per_run": 2})

    monkeypatch.setattr(engine_factory, "runtime_constants", patched)
    provider = ScriptedProvider(
        [
            {"tool": "Exec", "args": {"command": "echo one"}},
            {"tool": "Exec", "args": {"command": "echo two"}},
            {"text": "what I managed to do"},
        ]
    )
    manager = await _manager(settings, db, provider)
    events: list[TurnEvent] = []

    async def sink(session_id: str, event: TurnEvent) -> None:
        events.append(event)

    manager.add_sink(sink)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "keep going")
    assert (await waiter)[0][2] == "completed"

    notified = _notified(events)
    assert notified and notified[0]["soft_stop_cause"] == "max_turns"
    notice = _wind_down_notice(manager, state)
    assert notice and "reached its budget (max_turns)" in notice
    assert not [e for e in events if e.type is EventType.ERROR]
    await manager.close()


def _wind_down_notice(manager: SessionManager, state: Any) -> str:
    """The notice the model was actually shown, taken from the request it was shown in.

    Read off the wire rather than out of history on purpose: the notice is removed from history
    when the run ends, and what matters is the text that went to the provider.
    """
    provider = manager.providers.rungs_for(None)[0][0]  # type: ignore[arg-type]
    for request in reversed(provider.requests):
        for message in reversed(request.messages):
            text = "".join(getattr(b, "text", "") for b in message.content_blocks)
            if "internal control" in text:
                return text
    return ""
