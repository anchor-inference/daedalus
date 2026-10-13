"""The next-message guess: asked for by the app, made at most once per answer, and skipped whenever
it would be wasted — off, a busy or long session, the day's allowance spent."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
from protocore.contracts.llm import LLMObservabilityContext, LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import AssistConfig, Settings
from daedalus.host import predict
from daedalus.host.session_runner import SessionManager
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.providers.pricing import ModelPricing
from daedalus.stores.database import Database
from daedalus.stores.sqlite import SqliteUsageSink
from tests.support.models import model_config


def test_the_guess_is_one_clean_line_or_nothing() -> None:
    assert predict.clean('"Run the tests now"\nand more') == "Run the tests now"
    assert predict.clean("Operator: давай дальше") == "давай дальше"
    assert predict.clean("NONE.") == "" and predict.clean("   ") == ""
    assert len(predict.clean("x" * 500)) == predict.MAX_CHARS


async def _manager(settings: Settings, db: Database, **assist: object) -> tuple[SessionManager, list[str]]:
    manager = SessionManager(settings, model_config(assist=AssistConfig(**assist)), db=db)  # type: ignore[arg-type]
    await manager.start()
    asked: list[str] = []

    async def small_model(prompt: str, preset: str, *, purpose: str, session_id: str | None = None, max_tokens: int = 200, temperature: float = 0.0) -> str:
        assert purpose == predict.PURPOSE
        asked.append(prompt)
        return '"Now run the full suite"'

    manager.small_model = small_model  # type: ignore[method-assign]
    return manager, asked


async def _exchange(manager: SessionManager, sid: str, question: str, answer: str) -> None:
    await manager.sessions.append_transcript(sid, [
        Message(role=MessageRole.user, content_blocks=[TextBlock(text=question)], metadata={"daedalus.origin": "operator"}),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=answer)]),
    ])


async def test_a_settled_answer_gets_one_guess(settings: Settings, db: Database) -> None:
    manager, asked = await _manager(settings, db)
    try:
        guesses = predict.Predictor(manager)
        state = await manager.create_session("s")
        sid = state.session.id
        assert await guesses.suggest(sid) == {"text": "", "skipped": "empty"}
        await _exchange(manager, sid, "fix the flaky test", "Fixed: the fixture now waits for the port.")
        assert await guesses.suggest(sid) == {"text": "Now run the full suite"}
        assert await guesses.suggest(sid) == {"text": "Now run the full suite"}
        assert len(asked) == 1 and "Agent: Fixed: the fixture" in asked[0] and "Operator: fix the flaky test" in asked[0]
        # A new answer is a new guess.
        await _exchange(manager, sid, "and the other one?", "Done too.")
        await guesses.suggest(sid)
        assert len(asked) == 2
        # The operator spoke last: there is no answer to follow.
        await manager.sessions.append_transcript(sid, [Message(role=MessageRole.user, content_blocks=[TextBlock(text="hm")], metadata={"daedalus.origin": "operator"})])
        assert (await guesses.suggest(sid))["skipped"] == "no_answer"
    finally:
        await manager.close()


async def test_it_is_skipped_when_off_busy_long_or_over_the_allowance(settings: Settings, db: Database) -> None:
    manager, asked = await _manager(settings, db, predict_max_messages=10, predict_usd_per_day=0.01)
    try:
        guesses = predict.Predictor(manager)
        state = await manager.create_session("s")
        sid = state.session.id
        await _exchange(manager, sid, "q", "a")
        hold = asyncio.Event()
        state.task = asyncio.create_task(hold.wait())
        assert (await guesses.suggest(sid))["skipped"] == "busy"
        hold.set()
        await state.task
        state.task = None
        await manager.db.execute(
            "INSERT INTO usage_events(at, provider_id, model, purpose, cost_usd, raw) VALUES (?, 'deepseek', 'm', ?, 0.02, '{}')",
            (datetime.now(UTC).isoformat(), predict.PURPOSE),
        )
        assert await guesses.spent_today() >= 0.02
        assert (await guesses.suggest(sid))["skipped"] == "cap"
        for _ in range(6):
            await _exchange(manager, sid, "q", "a")
        assert (await guesses.suggest(sid))["skipped"] == "long"
        manager.config.assist.predict = False
        assert (await guesses.suggest(sid))["skipped"] == "off"
        assert asked == []
    finally:
        await manager.close()


async def test_a_helpers_call_is_booked_under_its_own_label(db: Database) -> None:
    endpoint = ProviderEndpoint(id="paid", kind="openai_compat", base_url="http://127.0.0.1", pricing={"model": ModelPricing(input=1, output=1)})
    answer = httpx.Response(200, json={"choices": [{"message": {"content": "long"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}})
    adapter = OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: answer)), usage_sink=SqliteUsageSink(db))
    try:
        for category in ("assist", "session"):
            await adapter.complete_text(LLMRequest(
                model="model", max_tokens=8, messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])],
                observability=LLMObservabilityContext(tenant_id="t", call_purpose=predict.PURPOSE, call_category=category),
            ))
    finally:
        await adapter.aclose()
    rows = [row["purpose"] for row in await db.fetchall("SELECT purpose FROM usage_events ORDER BY seq")]
    # A run's own calls keep their call kind; only a helper's is renamed.
    assert rows == [predict.PURPOSE, "text"]
