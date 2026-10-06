"""A spending cap limits the spend it can measure and never stops work it cannot price.

The operator's caps (a per-run limit, a daily limit, provider totals) sit beside subscription
logins, free presets and self-hosted models that publish no per-token price. These drive real
runs through the session manager and the host's inference admission with each of them.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType
from protocore.runtime.soft_stop import CAUSE_PROVIDER_ERROR

from daedalus.config import ModelPresetConfig, ProviderConfig, RuntimeConfig, Settings
from daedalus.host import session_runner
from daedalus.host.session_runner import SessionManager
from daedalus.providers.openai_compat import ProviderEndpoint, UsageRecord
from daedalus.stores.database import Database

BASE = "http://127.0.0.1:9"
FREE_CATALOG = {"providers": [{"id": "opencode", "fresh": True, "base_url": f"{BASE}/opencode",
                               "models": [{"id": "space-bunny-free", "mechanism": "zero_price"}]}]}


def reply(request: httpx.Request) -> httpx.Response:
    """A streamed answer whose usage carries token counts but no price, as a subscription reports."""
    chunks = [{"choices": [{"delta": {"content": "answered"}, "finish_reason": "stop"}]},
              {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}]
    return httpx.Response(200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n")


def config() -> RuntimeConfig:
    runtime = RuntimeConfig(
        providers={"grok": ProviderConfig(kind="openai_compat", base_url=f"{BASE}/grok/v1"),
                   "opencode": ProviderConfig(kind="opencode", base_url=f"{BASE}/opencode", api_key="key"),
                   "deepseek": ProviderConfig(kind="deepseek", base_url=f"{BASE}/deepseek", api_key="key")},
        presets={"grok.grok-4": ModelPresetConfig(provider="grok", model="grok-4"),
                 "opencode.space-bunny-free": ModelPresetConfig(provider="opencode", model="space-bunny-free",
                                                                 free_only=True),
                 "deepseek.flash": ModelPresetConfig(provider="deepseek", model="deepseek-flash")},
        model={"preset": "grok.grok-4"},
    )
    runtime.limits.usd_per_run = 5.0
    return runtime


async def started(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> tuple[SessionManager, list[httpx.Request]]:
    settings.usd_per_day = 3.0
    manager = SessionManager(settings, config(), db=db)
    await manager.start(recovering=False)
    sent: list[httpx.Request] = []
    for adapter in manager.providers._providers.values():
        await adapter._client.aclose()
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: sent.append(request) or reply(request)))

    async def catalog() -> dict[str, Any]:
        return FREE_CATALOG

    monkeypatch.setattr(session_runner.free_catalog, "get", catalog)
    return manager, sent


async def run(manager: SessionManager, session_id: str, text: str) -> tuple[str, list[TurnEvent]]:
    events: list[TurnEvent] = []
    done = asyncio.Event()
    status: list[str] = []

    async def sink(sid: str, event: TurnEvent) -> None:
        if sid == session_id:
            events.append(event)

    async def finished(sid: str, _run_id: str, outcome: str) -> None:
        if sid == session_id:
            status.append(outcome)
            done.set()

    manager.add_sink(sink)
    manager.on_finished(finished)
    await manager.submit(session_id, text)
    await asyncio.wait_for(done.wait(), timeout=30)
    return status[0], events


def errors(events: list[TurnEvent]) -> list[str]:
    return [str(event.payload.get("message")) for event in events if event.type is EventType.ERROR]


async def test_a_priced_model_over_the_measured_daily_cap_is_refused_and_says_why(
        settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, sent = await started(settings, db, monkeypatch)
    try:
        await manager.usage.record(UsageRecord(provider_id="deepseek", model="deepseek-flash", purpose="stream",
                                               raw={}, normalized={}, cost_usd=2.99, duration_ms=1,
                                               run_id="earlier", session_id=None))
        priced = await manager.create_session("priced")
        await manager.set_model(priced.session.id, preset="deepseek.flash")
        outcome, events = await run(manager, priced.session.id, "spend")
        assert outcome == "failed"
        assert sent == []
        message = " ".join(errors(events))
        assert "daily spending cap" in message and "usd_per_day" in message

        # The subscription model beside it still answers under the same exhausted day.
        state = await manager.create_session("subscription")
        outcome, events = await run(manager, state.session.id, "still works")
        assert outcome == "completed", errors(events)
    finally:
        await manager.close()


async def test_a_cap_that_ends_a_run_midway_is_not_reported_as_the_provider(
        settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _ = await started(settings, db, monkeypatch)
    try:
        state = await manager.create_session("winding down")
        events: list[TurnEvent] = []

        async def sink(_sid: str, event: TurnEvent) -> None:
            events.append(event)

        manager.add_sink(sink)
        state.soft_stop_cause = CAUSE_PROVIDER_ERROR
        state.soft_stop_detail = "inference budget: run:r: the available balance under this run's spending cap"
        assert await manager._report_provider_refusal(state, "r")
        payload = events[-1].payload
        assert payload["kind"] == "spending_cap"
        assert "a spending cap stopped this run" in payload["message"] and "provider refused" not in payload["message"]
    finally:
        await manager.close()


async def test_a_closed_daily_flag_stops_priced_calls_only(settings: Settings, db: Database,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _ = await started(settings, db, monkeypatch)

    async def fake_start(_state: Any, _message: Any, *, continue_turn: bool = False) -> str:
        return "started"

    monkeypatch.setattr(manager, "_start_run", fake_start)
    try:
        manager.config.limits.usd_total = 1.0
        await manager.usage.record(UsageRecord(provider_id="deepseek", model="deepseek-flash", purpose="stream",
                                               raw={}, normalized={}, cost_usd=5.0, duration_ms=1,
                                               run_id="earlier", session_id=None))
        manager.budget_flag.parent.mkdir(parents=True, exist_ok=True)
        manager.budget_flag.write_text("priced daily cap")
        state = await manager.create_session("subscription")
        assert await manager.cap_breach(state, "grok", "grok-4") is None
        assert await manager.submit(state.session.id, "hello") == "started"
        priced = await manager.create_session("priced")
        await manager.set_model(priced.session.id, preset="deepseek.flash")
        assert await manager.cap_breach(priced, "deepseek", "deepseek-flash") is not None
        with pytest.raises(RuntimeError, match="daily budget exceeded"):
            await manager.submit(priced.session.id, "hello")
    finally:
        await manager.close()


@pytest.mark.parametrize("kind,model,price,measured", [
    ("llamacpp", "local", None, False),
    ("openai_compat", "grok-4", None, False),
    ("vllm", "qwen", None, False),
    ("openrouter", "vendor/model:free", None, False),
    ("opencode", "space-bunny-free", None, False),
    ("openai_compat", "local", {"input": 0.0, "output": 0.0}, False),
    ("openrouter", "vendor/model", None, True),
    ("deepseek", "deepseek-flash", {"input": 0.3, "output": 1.2}, True),
])
def test_which_calls_a_cap_can_measure(kind: str, model: str, price: dict[str, float] | None, measured: bool) -> None:
    from daedalus.providers.pricing import ModelPricing

    endpoint = ProviderEndpoint(id="p", kind=kind, base_url=BASE,
                                pricing={} if price is None else {model: ModelPricing.from_entry(price)})
    assert endpoint.measures_spend(model) is measured
