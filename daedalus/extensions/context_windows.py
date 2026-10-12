"""Keep every automatic preset's context window true: find it, remember it, write it into the configuration.

The finding is :mod:`daedalus.providers.context_window`. This is the part with state: what each
provider's listing said (kept a day, so a sweep or a save does not ask again), every limit a refusal
stated (kept for good, since a refusal is the strongest evidence there is), and the writing of the
result into ``config.toml`` — where every reader of ``preset.context_window`` already looks, so the
run, the fallback chain's fit check, compaction and the screens all see the found window without one
of them having to know it was found.

A preset whose window the operator typed (``context_window_source = "manual"``) is never touched.
Nothing here runs while a run waits: the sweep is a background task, a save waits only for the one
preset it saved, and a refusal is recorded from the side of the request that failed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from daedalus.config import ModelPresetConfig, RuntimeConfig
from daedalus.host.config_validation import ConfigConflict, config_revision
from daedalus.providers.context_window import (
    MANUAL,
    Found,
    Resolved,
    catalog_window,
    decide,
    provider_window,
    table_window,
)

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

KV_KEY = "context_windows"
LISTING_TTL_SECONDS = 24 * 3600
"""How long a provider's answer stands before it is asked again: a server restarted with another
length is noticed within a day, or at once with Refresh."""
SWEEP_SECONDS = 24 * 3600
FIRST_SWEEP_DELAY_SECONDS = 60.0
"""The first sweep waits for the price refresh, whose download carries the models.dev windows."""


class ContextWindows:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.listed: dict[str, dict[str, dict[str, Any]]] = {}
        """provider id → model → ``{"tokens", "detail", "at"}``; ``tokens`` 0 records "asked, said nothing"."""
        self.refused: dict[str, dict[str, int]] = {}
        """provider id → model → the window a refusal stated."""
        self._save_lock = asyncio.Lock()
        self._background: set[asyncio.Task[Any]] = set()

    async def load(self) -> None:
        data = await self.app.db.kv_get(KV_KEY, {}) or {}
        listed = data.get("listed") if isinstance(data, dict) else None
        refused = data.get("refused") if isinstance(data, dict) else None
        self.listed = {str(p): {str(m): dict(v) for m, v in models.items() if isinstance(v, dict)} for p, models in (listed or {}).items() if isinstance(models, dict)}
        self.refused = {str(p): {str(m): int(v) for m, v in models.items() if type(v) is int} for p, models in (refused or {}).items() if isinstance(models, dict)}

    async def _persist(self) -> None:
        await self.app.db.kv_set(KV_KEY, {"listed": self.listed, "refused": self.refused})

    def _catalog(self) -> dict[str, int]:
        manager = self.app.manager
        return dict(getattr(getattr(manager, "providers", None), "catalog_windows", {}) or {})

    async def provider_found(self, provider_id: str, model: str, *, refresh: bool = False) -> Found | None:
        """What the provider's own listing says, from the cache while it is fresh."""
        cached = self.listed.get(provider_id, {}).get(model)
        if cached is not None and not refresh and time.time() - float(cached.get("at") or 0) < LISTING_TTL_SECONDS:
            tokens = int(cached.get("tokens") or 0)
            return Found(tokens, str(cached.get("detail") or "")) if tokens else None
        manager = self.app.manager
        try:
            endpoint = manager.providers.get(provider_id).endpoint  # type: ignore[union-attr]
        except (KeyError, AttributeError):
            return None  # not configured, or no key: nothing to ask
        try:
            found = await provider_window(endpoint.base_url, model, kind=endpoint.kind, api_key=endpoint.api_key)
        except Exception:  # noqa: BLE001 — a listing that cannot be read is a source that knows nothing
            logger.warning("could not ask %s for the context window of %s", provider_id, model, exc_info=True)
            found = None
        self.listed.setdefault(provider_id, {})[model] = {"tokens": found.tokens if found else 0, "detail": found.detail if found else "", "at": time.time()}
        await self._persist()
        return found

    def sources(self, preset: ModelPresetConfig, found: Found | None) -> dict[str, int | None]:
        """What each source knows about one preset's model, for the screen that explains the choice."""
        return {
            "provider": found.tokens if found else None,
            "catalog": catalog_window(self._catalog(), preset.model),
            "table": table_window(preset.model),
            "error": self.refused.get(preset.provider, {}).get(preset.model),
        }

    def _decide(self, preset: ModelPresetConfig, found: Found | None) -> Resolved | None:
        known = self.sources(preset, found)
        return decide(provider=found, catalog=known["catalog"], table=known["table"], refused=known["error"])

    async def resolve(self, preset: ModelPresetConfig, *, refresh: bool = False) -> tuple[Resolved | None, dict[str, int | None]]:
        found = await self.provider_found(preset.provider, preset.model, refresh=refresh) if preset.model else None
        return self._decide(preset, found), self.sources(preset, found)

    def view(self, preset: ModelPresetConfig, known: dict[str, int | None] | None = None) -> dict[str, Any]:
        """The preset's window as the model settings show it: the number, its source, and every source's say."""
        if known is None:
            cached = self.listed.get(preset.provider, {}).get(preset.model) or {}
            tokens = int(cached.get("tokens") or 0)
            known = self.sources(preset, Found(tokens) if tokens else None)
        return {"window": preset.context_window, "source": preset.context_window_source, "sources": known}

    async def apply(self, updates: dict[str, Resolved]) -> RuntimeConfig:
        """Write found windows into the configuration, leaving manual presets and unchanged ones alone.

        Read, change and save under the configuration's revision, again on a conflict: an operator's
        save in between is kept, and the found window is written on top of it.
        """
        async with self._save_lock:
            for _attempt in range(5):
                config = self.app.config
                raw = config.model_dump(mode="json")
                changed = False
                for pid, resolved in updates.items():
                    entry = raw.get("presets", {}).get(pid)
                    if not isinstance(entry, dict) or entry.get("context_window_source") == MANUAL:
                        continue
                    tokens = max(8_000, min(4_000_000, int(resolved.tokens)))
                    if entry.get("context_window") != tokens or entry.get("context_window_source") != resolved.source:
                        entry["context_window"] = tokens
                        entry["context_window_source"] = resolved.source
                        changed = True
                if not changed:
                    return config
                candidate = type(config).model_validate(raw)
                try:
                    await self.app.save_config(candidate, expected_revision=config_revision(config))
                except ConfigConflict:
                    continue
                return candidate
            logger.warning("context windows not saved: the configuration kept changing underneath")
            return self.app.config

    async def resolve_preset(self, preset_id: str, *, refresh: bool = False) -> dict[str, Any]:
        """Find one preset's window now (a save, the Refresh button) and write it when it is automatic."""
        preset = self.app.config.presets.get(preset_id)
        if preset is None:
            raise KeyError(preset_id)
        resolved, known = await self.resolve(preset, refresh=refresh)
        if resolved is not None and preset.context_window_source != MANUAL:
            await self.apply({preset_id: resolved})
        return self.view(self.app.config.presets.get(preset_id, preset), known)

    async def sweep(self, *, refresh: bool = False) -> int:
        """Bring every automatic preset up to date; returns how many windows changed."""
        updates: dict[str, Resolved] = {}
        for pid, preset in list(self.app.config.presets.items()):
            if preset.context_window_source == MANUAL or not preset.model:
                continue
            resolved, _known = await self.resolve(preset, refresh=refresh)
            if resolved is not None and (resolved.tokens, resolved.source) != (preset.context_window, preset.context_window_source):
                updates[pid] = resolved
        if updates:
            await self.apply(updates)
            logger.warning("context windows found: %s", ", ".join(f"{pid}={r.tokens} ({r.source})" for pid, r in updates.items()))
        return len(updates)

    def record_refusal(self, provider_id: str, model: str, window: int) -> None:
        """A provider refused a request at ``window``: remember it, and shrink the automatic presets above it.

        Called synchronously from the failing request, so the saving happens in a task of its own.
        """
        if self.refused.get(provider_id, {}).get(model) == window:
            return
        self.refused.setdefault(provider_id, {})[model] = int(window)
        logger.warning("%s refused a request to %s at a context window of %s tokens; remembered", provider_id, model, window)
        task = asyncio.get_running_loop().create_task(self._after_refusal(provider_id, model), name="context-window-refusal")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _after_refusal(self, provider_id: str, model: str) -> None:
        try:
            await self._persist()
            updates: dict[str, Resolved] = {}
            for pid, preset in self.app.config.presets.items():
                if preset.provider != provider_id or preset.model != model or preset.context_window_source == MANUAL:
                    continue
                cached = self.listed.get(provider_id, {}).get(model) or {}
                tokens = int(cached.get("tokens") or 0)
                resolved = self._decide(preset, Found(tokens) if tokens else None)
                if resolved is not None:
                    updates[pid] = resolved
            if updates:
                await self.apply(updates)
        except Exception:  # noqa: BLE001
            logger.warning("could not apply a refused context window for %s/%s", provider_id, model, exc_info=True)

    async def loop(self) -> None:
        await asyncio.sleep(FIRST_SWEEP_DELAY_SECONDS)
        while True:
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001
                logger.warning("context-window sweep failed", exc_info=True)
            await asyncio.sleep(SWEEP_SECONDS)


async def install(app: Application) -> list[asyncio.Task[None]]:
    service = ContextWindows(app)
    app.extensions["context_windows"] = service
    await service.load()
    if app.manager is not None:
        app.manager.providers.on_context_refusal = service.record_refusal
    return [asyncio.create_task(service.loop(), name="context-window-sweep")]


__all__ = ["ContextWindows", "KV_KEY", "install"]
