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


async def started(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch,
                  runtime: RuntimeConfig | None = None) -> tuple[SessionManager, list[httpx.Request]]:
    settings.usd_per_day = 3.0
    manager = SessionManager(settings, runtime or config(), db=db)
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


async def test_capped_runs_answer_on_subscription_free_and_after_unpriced_usage(
        settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, sent = await started(settings, db, monkeypatch)
    try:
        state = await manager.create_session("subscription")
        outcome, events = await run(manager, state.session.id, "first")
        assert outcome == "completed", errors(events)
        assert (await db.fetchone("SELECT count(*) FROM usage_events WHERE cost_usd IS NULL"))[0] == 1

        # A day that already holds spend of unknown price used to refuse every later call.
        outcome, events = await run(manager, state.session.id, "second")
        assert outcome == "completed", errors(events)

        free = await manager.create_session("free")
        await manager.set_model(free.session.id, preset="opencode.space-bunny-free")
        outcome, events = await run(manager, free.session.id, "third")
        assert outcome == "completed", errors(events)

        assert (await db.fetchone("SELECT count(*) FROM inference_reservations"))[0] == 0

        # A priced model still reserves under the day's cap, beside the unpriced rows already in it.
        priced = await manager.create_session("priced")
        await manager.set_model(priced.session.id, preset="deepseek.flash")
        outcome, events = await run(manager, priced.session.id, "fourth")
        assert outcome == "completed", errors(events)
        assert len(sent) == 4
        assert (await db.fetchone("SELECT state FROM inference_reservations"))[0] == "settled"
    finally:
        await manager.close()


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


GO_LIST_PRICE = {"input": 3.0, "output": 15.0, "cache_hit": 0.3}
MODELS_DEV = {
    "opencode-go": {"models": {"kimi-k3": {"cost": {"input": 3.0, "output": 15.0, "cache_read": 0.3}}}},
    "opencode": {"models": {"kimi-k3": {"cost": {"input": 2000.0, "output": 10000.0, "cache_read": 200.0}},
                            "big-pickle": {"cost": {"input": 0, "output": 0, "cache_read": 0}}}},
}


def opencode_config() -> RuntimeConfig:
    """Go and Zen side by side, as the seed leaves them: one prepaid plan, one gateway billed per token."""
    runtime = RuntimeConfig(
        providers={"opencode": ProviderConfig(kind="opencode", base_url=f"{BASE}/opencode", billing="subscription",
                                              # A list price left behind on the plan must not become a charge.
                                              pricing={"kimi-k3": GO_LIST_PRICE}),
                   "opencode_zen": ProviderConfig(kind="opencode", base_url=f"{BASE}/opencode_zen")},
        presets={"opencode.kimi-k3": ModelPresetConfig(provider="opencode", model="kimi-k3"),
                 "opencode_zen.kimi-k3": ModelPresetConfig(provider="opencode_zen", model="kimi-k3"),
                 "opencode_zen.big-pickle": ModelPresetConfig(provider="opencode_zen", model="big-pickle")},
        model={"preset": "opencode.kimi-k3"},
    )
    runtime.limits.usd_per_run = 5.0
    return runtime


async def opencode_started(settings: Settings, db: Database,
                           monkeypatch: pytest.MonkeyPatch) -> tuple[SessionManager, list[httpx.Request]]:
    from daedalus.providers import modelsdev

    async def catalog(client: Any = None, *, timeout: float = 15.0) -> dict[str, Any]:
        return MODELS_DEV

    monkeypatch.setattr(modelsdev, "fetch_catalog", catalog)
    manager, sent = await started(settings, db, monkeypatch, opencode_config())
    await manager.providers.refresh_prices(None, force=True)
    return manager, sent


def go_reply(request: httpx.Request) -> httpx.Response:
    """The gateway's answer, with the list-price cost OpenCode reports for a call beside its token counts."""
    chunks = [{"choices": [{"delta": {"content": "answered"}, "finish_reason": "stop"}]},
              {"choices": [], "usage": {"prompt_tokens": 12_000, "completion_tokens": 3_000, "cost": 0.081}}]
    return httpx.Response(200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n")


async def test_an_opencode_go_run_under_dollar_caps_is_not_charged_against_them(
        settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, sent = await opencode_started(settings, db, monkeypatch)
    for adapter in manager.providers._providers.values():
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: sent.append(request) or go_reply(request)))
    try:
        endpoint = manager.providers.get("opencode").endpoint
        assert endpoint.subscription and endpoint.pricing == {} and not endpoint.measures_spend("kimi-k3")
        # A day already past its dollar cap on priced work does not stop the plan.
        await manager.usage.record(UsageRecord(provider_id="deepseek", model="deepseek-flash", purpose="stream",
                                               raw={}, normalized={}, cost_usd=2.99, duration_ms=1,
                                               run_id="earlier", session_id=None))
        state = await manager.create_session("go")
        for text in ("first", "second"):
            outcome, events = await run(manager, state.session.id, text)
            assert outcome == "completed", errors(events)
        rows = await db.fetchall("SELECT cost_usd FROM usage_events WHERE provider_id = 'opencode'")
        assert [row["cost_usd"] for row in rows] == [0.0, 0.0]
        assert (await db.fetchone("SELECT count(*) FROM inference_reservations"))[0] == 0
        assert await manager.cap_breach(state, "opencode", "kimi-k3") is None
        spent, unmetered = await manager.spend()
        assert (spent, unmetered) == (pytest.approx(2.99), 0)
        assert len(sent) == 2 and all(request.url.path.startswith("/opencode/") for request in sent)
    finally:
        await manager.close()


async def test_a_priced_zen_model_is_charged_and_capped_and_a_free_one_is_free(
        settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, sent = await opencode_started(settings, db, monkeypatch)
    try:
        zen = manager.providers.get("opencode_zen").endpoint
        assert not zen.subscription and zen.pricing_for("kimi-k3").input == 2000.0  # Zen's price, not Go's
        assert zen.measures_spend("kimi-k3") and not zen.measures_spend("big-pickle")

        priced = await manager.create_session("zen priced")
        await manager.set_model(priced.session.id, preset="opencode_zen.kimi-k3")
        outcome, events = await run(manager, priced.session.id, "spend")
        assert outcome == "completed", errors(events)
        charged = (await db.fetchone("SELECT cost_usd FROM usage_events WHERE provider_id = 'opencode_zen'"))[0]
        assert charged == pytest.approx((12 * 2000.0 + 3 * 10000.0) / 1_000_000)

        free = await manager.create_session("zen free")
        await manager.set_model(free.session.id, preset="opencode_zen.big-pickle")
        outcome, events = await run(manager, free.session.id, "free")
        assert outcome == "completed", errors(events)
        assert (await db.fetchone("SELECT cost_usd FROM usage_events WHERE model = 'big-pickle'"))[0] == 0.0

        # Past the total cap the priced Zen model is refused; the free one and the plan are not.
        manager.config.limits.usd_total = 3.0
        await manager.usage.record(UsageRecord(provider_id="opencode_zen", model="kimi-k3", purpose="stream",
                                               raw={}, normalized={}, cost_usd=3.5, duration_ms=1,
                                               run_id="earlier", session_id=None))
        breach = await manager.cap_breach(priced, "opencode_zen", "kimi-k3")
        assert breach is not None and breach[0] == "total_cap"
        assert await manager.cap_breach(free, "opencode_zen", "big-pickle") is None
        go = await manager.create_session("go")
        assert await manager.cap_breach(go, "opencode", "kimi-k3") is None
        outcome, events = await run(manager, go.session.id, "still on the plan")
        assert outcome == "completed", errors(events)
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
    ("opencode", "kimi-k3", {"input": 3.0, "output": 15.0}, True),
    ("opencode go", "kimi-k3", {"input": 3.0, "output": 15.0}, False),
])
def test_which_calls_a_cap_can_measure(kind: str, model: str, price: dict[str, float] | None, measured: bool) -> None:
    from daedalus.providers.pricing import ModelPricing

    endpoint = ProviderEndpoint(id="p", kind=kind.split()[0], base_url=BASE, subscription=kind.endswith(" go"),
                                pricing={} if price is None else {model: ModelPricing.from_entry(price)})
    assert endpoint.measures_spend(model) is measured


async def test_a_free_preset_runs_on_its_last_listing_while_the_catalog_is_unreachable(
        settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, sent = await started(settings, db, monkeypatch)
    listing = {"providers": [{**FREE_CATALOG["providers"][0], "fresh": False}]}

    async def stale() -> dict[str, Any]:
        return listing

    monkeypatch.setattr(session_runner.free_catalog, "get", stale)
    try:
        free = await manager.create_session("free")
        await manager.set_model(free.session.id, preset="opencode.space-bunny-free")
        outcome, events = await run(manager, free.session.id, "while the list is down")
        assert outcome == "completed", errors(events)

        # A model the provider no longer lists as free is the promise the preset makes; it still stops.
        listing["providers"][0]["models"] = [{"id": "space-bunny-free", "mechanism": "paid"}]
        with pytest.raises(RuntimeError, match="no longer listed as free"):
            await run(manager, free.session.id, "after it became paid")
        assert len(sent) == 1
    finally:
        await manager.close()
