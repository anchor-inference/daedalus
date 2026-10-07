"""Prices for a gateway that publishes none: fetched from models.dev, cached, under the operator's own."""

from __future__ import annotations

from typing import Any

import pytest

from daedalus.config import ProviderConfig, RuntimeConfig, Settings
from daedalus.providers import modelsdev
from daedalus.providers.registry import ProviderRegistry

CATALOG = {
    "opencode-go": {"models": {"kimi-k3": {"cost": {"input": 3.0, "output": 15.0, "cache_read": 0.3}}, "glm-5.3-flash": {"cost": {"input": 0.15, "output": 0.5, "cache_read": 0.03}}}},
    "opencode": {"models": {"kimi-k3": {"cost": {"input": 9.0, "output": 9.0, "cache_read": 9.0}}, "claude-sonnet-5": {"cost": {"input": 2.0, "output": 10.0, "cache_read": 0.2}}}},
}


def test_the_first_source_wins_on_a_shared_model_and_the_rest_fill_in() -> None:
    table = modelsdev.prices_from_catalog(CATALOG, ("opencode-go", "opencode"))
    assert table["kimi-k3"].input == 3.0 and table["kimi-k3"].cache_hit == 0.3
    assert table["claude-sonnet-5"].output == 10.0
    assert set(table) == {"kimi-k3", "glm-5.3-flash", "claude-sonnet-5"}


def test_an_opencode_endpoint_is_priced_at_zen_rates_only() -> None:
    """Metered OpenCode is Zen; Go, the prepaid plan, is never priced per token, so its list never applies."""
    table = modelsdev.prices_from_catalog(CATALOG, modelsdev.SOURCES["opencode"])
    assert table["kimi-k3"].input == 9.0 and "glm-5.3-flash" not in table


class _Db:
    def __init__(self) -> None:
        self.kv: dict[str, Any] = {}

    async def kv_get(self, key: str, default: Any = None) -> Any:
        return self.kv.get(key, default)

    async def kv_set(self, key: str, value: Any) -> None:
        self.kv[key] = value


@pytest.mark.asyncio
async def test_refresh_puts_fetched_prices_under_the_operators_and_caches_them(monkeypatch: pytest.MonkeyPatch) -> None:
    config = RuntimeConfig()
    config.providers["opencode_zen"] = ProviderConfig(kind="opencode", base_url="http://keyproxy:3200/opencode_zen", pricing={"kimi-k3": {"input": 1.0, "output": 1.0, "cache_hit": 0.1}})
    registry = ProviderRegistry(Settings(), config)
    calls: list[int] = []

    async def fake_fetch(client=None, *, timeout=15.0):  # type: ignore[no-untyped-def]
        calls.append(1)
        return CATALOG

    monkeypatch.setattr(modelsdev, "fetch_catalog", fake_fetch)
    db = _Db()
    assert await registry.refresh_prices(db) == 2
    endpoint = registry.get("opencode_zen").endpoint
    assert endpoint.pricing_for("kimi-k3").input == 1.0  # the operator's entry stays
    assert endpoint.pricing_for("claude-sonnet-5").input == 2.0  # fetched
    assert endpoint.pricing_for("glm-5.3-flash") is None  # Go's alone: unknown on Zen, never zero
    # The Go subscription beside it takes no price at all, fetched or not.
    assert registry.get("opencode").endpoint.pricing == {}
    # A second refresh within the day reads the cache, not the network, and a reload keeps the fetched prices.
    assert await registry.refresh_prices(db) == 2
    assert len(calls) == 1
    registry.reload(config)
    assert registry.get("opencode_zen").endpoint.pricing_for("claude-sonnet-5").output == 10.0
    assert registry.get("opencode").endpoint.pricing == {}
