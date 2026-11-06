"""Keep what a model's chosen OpenRouter providers say true: their price, and whether they still exist.

The finding is :mod:`daedalus.providers.openrouter_routes`; this is the part with state. Once a day, and at once
after a refusal, every limited model's listing is read and its price rewritten from the chosen providers, because
those change prices without notice and a stale low one is what a spending cap reserves. A provider that
has left is written beside the choice, for the model's settings to show, and is not removed from it: nothing is
rerouted on the operator's behalf, and one that comes back is used again.

A listing that cannot be read changes nothing. Being unsure a provider is there is not knowing it is gone.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from daedalus.extensions import guest_door
from daedalus.host.config_validation import ConfigConflict, config_revision
from daedalus.providers.openrouter_routes import bill_for, fetch_listing, gone, priced_entry

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

EXTENSION_KEY = "openrouter_routes"
SWEEP_SECONDS = 24 * 3600
FIRST_SWEEP_DELAY_SECONDS = 90.0
"""After the context windows' first sweep, which also reads these listings: two writers at once only cost a retry."""


class OpenRouterRoutes:
    def __init__(self, app: Application) -> None:
        self.app = app
        self._save_lock = asyncio.Lock()
        self._background: set[asyncio.Task[Any]] = set()

    def limited(self) -> set[tuple[str, str]]:
        """Every ``(endpoint id, model)`` that has a choice."""
        return {(p.provider, p.model) for p in self.app.config.presets.values() if p.openrouter_providers}

    async def _read(self, provider_id: str, model: str) -> dict[str, Any] | None:
        manager = self.app.manager
        try:
            endpoint = manager.providers.get(provider_id).endpoint  # type: ignore[union-attr]
        except (KeyError, AttributeError):
            return None
        return await fetch_listing(endpoint.base_url, endpoint.api_key, model)

    async def check(self, pairs: set[tuple[str, str]]) -> int:
        """Bring these models up to date; returns how many changed."""
        listings = {pair: listing for pair in pairs if (listing := await self._read(*pair)) is not None}
        if not listings:
            return 0
        changed = await self._apply(listings)
        if changed:
            await guest_door.routes_changed(self.app)
        return changed

    async def _apply(self, listings: dict[tuple[str, str], dict[str, Any]]) -> int:
        """Write the prices and the departed under the configuration's revision, again on a conflict, so
        an operator's save in between is kept, as the context windows do."""
        async with self._save_lock:
            for _attempt in range(5):
                config = self.app.config
                raw = config.model_dump(mode="json")
                changed = 0
                for (provider_id, model), listing in listings.items():
                    only = config.openrouter_route(provider_id, model)
                    if not only:
                        continue  # the operator cleared it while the listing was being read
                    rows = listing["rows"]
                    departed = gone(rows, only)
                    for preset in raw["presets"].values():
                        if (preset.get("provider"), preset.get("model")) == (provider_id, model) and preset.get("openrouter_providers_gone") != departed:
                            preset["openrouter_providers_gone"] = departed
                            changed += 1
                    bill = bill_for(rows, only)
                    if bill:
                        # With every chosen provider gone there is nothing to price from, and the last known
                        # price says more than none: a call cannot run anyway.
                        pricing = raw["providers"][provider_id].setdefault("pricing", {})
                        entry = priced_entry(pricing.get(model), bill)
                        if entry != pricing.get(model):
                            pricing[model] = entry
                            changed += 1
                if not changed:
                    return 0
                candidate = type(config).model_validate(raw)
                try:
                    await self.app.save_config(candidate, expected_revision=config_revision(config))
                except ConfigConflict:
                    continue
                return changed
            logger.warning("OpenRouter routes not saved: the configuration kept changing underneath")
            return 0

    def record_refusal(self, provider_id: str, model: str) -> None:
        """OpenRouter refused every chosen provider of a model: read the listing now, not at the next sweep, so
        the settings say why as soon as the run's error does. Called from the failing request."""
        task = asyncio.get_running_loop().create_task(self._after_refusal(provider_id, model), name="openrouter-route-refusal")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _after_refusal(self, provider_id: str, model: str) -> None:
        try:
            await self.check({(provider_id, model)})
        except Exception:  # noqa: BLE001
            logger.warning("could not check the route of %s/%s after a refusal", provider_id, model, exc_info=True)

    async def loop(self) -> None:
        await asyncio.sleep(FIRST_SWEEP_DELAY_SECONDS)
        while True:
            try:
                changed = await self.check(self.limited())
                if changed:
                    logger.warning("OpenRouter routes updated: %s changes", changed)
            except Exception:  # noqa: BLE001
                logger.warning("OpenRouter route check failed", exc_info=True)
            await asyncio.sleep(SWEEP_SECONDS)


async def install(app: Application) -> list[asyncio.Task[None]]:
    service = OpenRouterRoutes(app)
    app.extensions[EXTENSION_KEY] = service
    if app.manager is not None:
        app.manager.providers.on_route_refusal = service.record_refusal
    return [asyncio.create_task(service.loop(), name="openrouter-route-check")]


__all__ = ["EXTENSION_KEY", "OpenRouterRoutes", "install"]
