"""A model's own price, typed in its settings: what it overrides, what overrides it, and how a call is booked by it."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from daedalus.config import ModelPresetConfig, ProviderConfig, RuntimeConfig, Settings, keyproxy_base
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.providers.openrouter_routes import priced_entry
from daedalus.providers.registry import ProviderRegistry
from daedalus.stores.database import Database
from tests.unit.test_billing_policy import errors, reply, run, started


def _with(runtime: RuntimeConfig, provider: str, model: str, entry: dict[str, Any] | None) -> RuntimeConfig:
    raw = runtime.model_dump(mode="json")
    pricing = raw["providers"][provider].setdefault("pricing", {})
    if entry is None:
        pricing.pop(model, None)
    else:
        pricing[model] = entry
    return RuntimeConfig.model_validate(raw)


def _answer_all(manager: SessionManager, sent: list[httpx.Request]) -> None:
    # A saved configuration builds new adapters with real clients; every one is pointed back at the fake.
    for adapter in manager.providers._providers.values():
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: sent.append(request) or reply(request)))


def test_a_typed_price_is_not_moved_by_a_choice_of_openrouter_providers() -> None:
    typed = {"manual": True, "billing": "metered", "input": 1.0, "output": 2.0}
    assert priced_entry(typed, {"input": 9.0, "output": 9.0, "cache_hit": None}) == typed
    assert priced_entry({"input": 1.0, "output": 2.0}, {"input": 9.0, "output": 9.0, "cache_hit": None}) == {"input": 9.0, "output": 9.0}


def test_the_view_says_where_the_price_comes_from(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYPROXY_BASE_URL", "http://keyproxy:3200")
    runtime = RuntimeConfig(providers={
        "deepseek": ProviderConfig(kind="deepseek", base_url="http://127.0.0.1:9/deepseek", api_key="key"),
        "claude": ProviderConfig(base_url=f"{keyproxy_base()}/claude/v1", pricing={"claude": {"input": 0.0, "output": 0.0, "cache_hit": 0.0}}),
        "go": ProviderConfig(kind="opencode", base_url="http://127.0.0.1:9/opencode", api_key="key", billing="subscription"),
    })
    registry = ProviderRegistry(Settings(_env_file=None, telegram_bot_token="1:a", owner_user_id=1), runtime)  # type: ignore[call-arg]
    flash = registry.price_view("deepseek", "deepseek-flash")
    assert (flash["input"], flash["manual"], flash["plan"], flash["default"]["source"]) == (0.30, False, False, "builtin")
    login = registry.price_view("claude", "claude-opus-5")
    assert (login["plan"], login["plan_source"], login["plan_locked"], login["default"]["source"], login["input"]) == (True, "login", False, "config", 0.0)
    assert registry.price_view("go", "kimi-k3")["plan_locked"]

    typed = _with(runtime, "deepseek", "deepseek-flash", {"manual": True, "billing": "metered", "input": 0.25, "output": 1.0})
    typed = _with(typed, "claude", "claude-opus-5", {"manual": True, "billing": "metered", "input": 5.0, "output": 25.0})
    registry.reload(typed)
    flash = registry.price_view("deepseek", "deepseek-flash")
    assert (flash["input"], flash["output"], flash["manual"], flash["default"]["input"], flash["default"]["source"]) == (0.25, 1.0, True, 0.30, "builtin")
    login = registry.price_view("claude", "claude-opus-5")
    assert (login["plan"], login["input"], login["default"]["input"]) == (False, 5.0, 0.0)
    # The endpoint the calls go through has the typed price, and the login is now counted per token.
    endpoint = registry.get("claude").endpoint
    assert not endpoint.plan_for("claude-opus-5") and endpoint.measures_spend("claude-opus-5")
    assert endpoint.plan_for("claude-sonnet-5"), "the other models of the login stay a plan"


async def test_a_typed_price_books_the_next_call_and_reset_brings_the_builtin_back(
        settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, sent = await started(settings, db, monkeypatch)
    try:
        state = await manager.create_session("priced")
        await manager.set_model(state.session.id, preset="deepseek.flash")
        runtime = manager.config

        manager.reload_config(_with(runtime, "deepseek", "deepseek-flash", {"manual": True, "billing": "metered", "input": 1.0, "output": 2.0, "cache_hit": 0.5}))
        _answer_all(manager, sent)
        outcome, events = await run(manager, state.session.id, "typed")
        assert outcome == "completed", errors(events)
        # Twelve tokens in, three out (the fake's usage), at the typed rates and not the built-in ones.
        assert (await db.fetchone("SELECT cost_usd FROM usage_events ORDER BY rowid DESC"))[0] == pytest.approx((12 * 1.0 + 3 * 2.0) / 1_000_000)

        manager.reload_config(_with(runtime, "deepseek", "deepseek-flash", {"manual": True, "billing": "subscription", "input": 1.0, "output": 2.0}))
        _answer_all(manager, sent)
        reservations = (await db.fetchone("SELECT count(*) FROM inference_reservations"))[0]
        outcome, events = await run(manager, state.session.id, "on the plan")
        assert outcome == "completed", errors(events)
        assert (await db.fetchone("SELECT cost_usd FROM usage_events ORDER BY rowid DESC"))[0] == 0.0
        assert (await db.fetchone("SELECT count(*) FROM inference_reservations"))[0] == reservations, "a plan reserves nothing"

        manager.reload_config(runtime)
        _answer_all(manager, sent)
        outcome, events = await run(manager, state.session.id, "reset")
        assert outcome == "completed", errors(events)
        price = manager.providers.get("deepseek").endpoint.pricing_for("deepseek-flash")
        assert price is not None and not price.manual and price.input == 0.30
        charged = (await db.fetchone("SELECT cost_usd FROM usage_events ORDER BY rowid DESC"))[0]
        # DeepSeek's built-in schedule: the peak rate or half of it, by the hour the test runs at.
        assert charged in (pytest.approx((12 * 0.30 + 3 * 1.20) / 1_000_000), pytest.approx((12 * 0.15 + 3 * 0.60) / 1_000_000))
    finally:
        await manager.close()


class _App:
    """The application as the price routes use it: a real registry, a save that reloads it."""

    def __init__(self, settings: Settings, runtime: RuntimeConfig) -> None:
        self.settings = settings
        self.config = runtime
        self.providers = ProviderRegistry(settings, runtime)
        self.manager = type("M", (), {"providers": self.providers, "list_sessions": staticmethod(lambda limit=100: [])})()
        self.front: Any = None
        self.extensions: dict[str, Any] = {}

    async def save_config(self, config: RuntimeConfig, *, expected_revision: str | None = None) -> None:
        self.config = config
        self.providers.reload(config)


def test_the_price_routes_write_reset_and_refuse_a_negative_rate(settings: Settings) -> None:
    runtime = RuntimeConfig(
        providers={"deepseek": ProviderConfig(kind="deepseek", base_url="http://127.0.0.1:9/deepseek", api_key="key")},
        presets={"flash": ModelPresetConfig(provider="deepseek", model="deepseek-flash")}, model={"preset": "flash"},
    )
    app = _App(settings, runtime)
    headers = {"X-Daedalus-Token": "tok"}
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        assert client.put("/api/presets/flash/price", json={"input": -1, "output": 1}, headers=headers).status_code == 422
        view = client.put("/api/presets/flash/price", json={"input": 0.5, "output": 2, "billing": "metered"}, headers=headers)
        assert view.status_code == 200, view.text
        price = view.json()["prices"]["flash"]
        assert (price["manual"], price["input"], price["output"], price["default"]["input"]) == (True, 0.5, 2.0, 0.30)
        assert app.providers.get("deepseek").endpoint.pricing_for("deepseek-flash").input == 0.5, "the next call is priced by it, no restart"
        price = client.delete("/api/presets/flash/price", headers=headers).json()["prices"]["flash"]
        assert (price["manual"], price["input"]) == (False, 0.30)
        assert "deepseek-flash" not in app.config.providers["deepseek"].pricing
        assert client.put("/api/presets/nope/price", json={"input": 1, "output": 1}, headers=headers).status_code == 404
