"""Current list prices from models.dev for gateways that publish none of their own.

OpenCode Go and Zen answer ``/models`` with ids only; models.dev (the catalogue OpenCode itself reads) carries
each model's USD per 1M tokens for input, output and cached input. The table is fetched once a day, kept in the
database between starts, and overlaid under the operator's own ``pricing`` entries, which always win.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from daedalus.providers.pricing import ModelPricing

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
SOURCES: dict[str, tuple[str, ...]] = {"opencode": ("opencode-go", "opencode")}
"""Provider kind → the models.dev provider ids whose prices apply, first one winning on a shared model id (a Go
key is billed at Go's prices; a model only Zen offers is priced pay-as-you-go)."""
REFRESH_SECONDS = 24 * 3600
KV_KEY = "modelsdev_prices"


def prices_from_catalog(catalog: dict[str, Any], provider_ids: tuple[str, ...]) -> dict[str, ModelPricing]:
    """The pricing table one kind gets from a models.dev catalogue."""
    table: dict[str, ModelPricing] = {}
    for provider_id in reversed(provider_ids):
        models = (catalog.get(provider_id) or {}).get("models") or {}
        for model_id, model in models.items():
            cost = model.get("cost") if isinstance(model, dict) else None
            if not isinstance(cost, dict) or cost.get("input") is None or cost.get("output") is None:
                continue
            table[str(model_id)] = ModelPricing(
                input=float(cost["input"]),
                output=float(cost["output"]),
                cache_hit=float(cost["cache_read"]) if cost.get("cache_read") is not None else None,
            )
    return table


def entries_from_table(table: dict[str, ModelPricing]) -> dict[str, dict[str, float | None]]:
    return {model: {"input": p.input, "output": p.output, "cache_hit": p.cache_hit} for model, p in table.items()}


def table_from_entries(entries: dict[str, dict[str, Any]]) -> dict[str, ModelPricing]:
    return {model: ModelPricing.from_entry(entry) for model, entry in entries.items() if isinstance(entry, dict)}


async def fetch_catalog(client: httpx.AsyncClient | None = None, *, timeout: float = 15.0) -> dict[str, Any]:
    """The catalogue, or an error. Bounded end to end, closing the client included.

    httpx's own timeout covers the request; it does not cover shutting the connection afterwards, and
    a half-closed connection — the peer gone, the socket left in CLOSE-WAIT — is closed by waiting
    for a FIN that never comes. This is a once-a-day price refresh running in the background of a
    process that has an operator waiting on it, so nothing here may wait indefinitely for anything:
    the whole of it is under one deadline, and a deadline reached is reported as the network failure
    it is, through the same path a refused connection takes.
    """
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=timeout, pool=timeout))
    try:
        async with asyncio.timeout(timeout * 2):
            try:
                response = await client.get(MODELS_DEV_URL, headers={"accept": "application/json"})
                response.raise_for_status()
                data = response.json()
            finally:
                if own:
                    await client.aclose()
    except TimeoutError as exc:
        raise httpx.ReadTimeout(f"models.dev did not answer within {timeout * 2:.0f}s") from exc
    if not isinstance(data, dict):
        raise ValueError("models.dev catalogue is not an object")
    return data


__all__ = ["KV_KEY", "MODELS_DEV_URL", "REFRESH_SECONDS", "SOURCES", "entries_from_table", "fetch_catalog", "prices_from_catalog", "table_from_entries"]
