"""Build provider adapters from configuration."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx

from daedalus.config import ProviderConfig, RuntimeConfig, Settings
from daedalus.providers.openai_compat import (
    ImageLoader,
    OpenAICompatibleProvider,
    ProviderEndpoint,
    UsageSink,
)
from daedalus.providers.pricing import ModelPricing, pricing_table

logger = logging.getLogger(__name__)

VENDOR_HOSTS = {"deepseek": "api.deepseek.com", "openrouter": "openrouter.ai", "opencode": "opencode.ai"}


def _is_vendor_host(kind: str, base_url: str) -> bool:
    host = (urlsplit(base_url if "://" in base_url else "//" + base_url).hostname or "").lower()
    return host == VENDOR_HOSTS.get(kind, "")


class ProviderRegistry:
    """Configured endpoints → live adapters (one shared HTTP client each)."""

    def __init__(
        self,
        settings: Settings,
        config: RuntimeConfig,
        *,
        usage_sink: UsageSink | None = None,
        image_loader: ImageLoader | None = None,
    ) -> None:
        self._settings = settings
        self._usage_sink = usage_sink
        self._image_loader = image_loader
        self._providers: dict[str, OpenAICompatibleProvider] = {}
        self._retired: list[OpenAICompatibleProvider] = []
        self._users: dict[OpenAICompatibleProvider, int] = {}
        self._fetched: dict[str, dict[str, ModelPricing]] = {}
        """Prices fetched from models.dev, by provider kind; under the operator's own entries."""
        self.reload(config)

    def _images_for(self, provider_id: str, model: str) -> bool:
        config = getattr(self, "_config", None)
        if config is None:
            return False
        return any(p.provider == provider_id and p.model == model and p.images for p in config.presets.values())

    def reload(self, config: RuntimeConfig) -> None:
        fresh: dict[str, OpenAICompatibleProvider] = {}
        for provider_id, pc in config.providers.items():
            endpoint = self._endpoint(provider_id, pc)
            if endpoint is None:
                continue
            existing = self._providers.get(provider_id)
            if existing is not None and existing.endpoint == endpoint:
                fresh[provider_id] = existing
                continue
            if existing is not None:
                self._retired.append(existing)
            fresh[provider_id] = OpenAICompatibleProvider(
                endpoint,
                client=httpx.AsyncClient(
                    timeout=httpx.Timeout(endpoint.timeout_seconds, connect=30.0)
                ),
                usage_sink=self._usage_sink,
                image_loader=self._image_loader,
                images_for=lambda model, pid=provider_id: self._images_for(pid, model),
            )
        self._config = config
        self._retired.extend(p for pid, p in self._providers.items() if pid not in fresh)
        self._providers = fresh

    async def close_retired(self) -> None:
        """Close replaced adapters only after their whole run releases them, including pauses."""
        retired, self._retired = self._retired, []
        for provider in retired:
            if self._users.get(provider, 0):
                self._retired.append(provider)
            else:
                await provider.aclose()

    @contextmanager
    def hold(self, providers: Sequence[OpenAICompatibleProvider]) -> Iterator[None]:
        """Keep a run's adapters alive between requests while settings can replace the registry."""
        held = set(providers)
        for provider in held:
            self._users[provider] = self._users.get(provider, 0) + 1
        try:
            yield
        finally:
            for provider in held:
                remaining = self._users[provider] - 1
                if remaining:
                    self._users[provider] = remaining
                else:
                    del self._users[provider]

    def _endpoint(self, provider_id: str, pc: ProviderConfig) -> ProviderEndpoint | None:
        base_url = pc.base_url
        headers: dict[str, str] = {}
        env_keys = {
            "deepseek": self._settings.deepseek_api_key,
            "openrouter": self._settings.openrouter_api_key,
            "vllm": self._settings.vllm_api_key,
        }
        if pc.kind == "openrouter":
            headers = {"HTTP-Referer": "https://github.com/anchor-inference/daedalus", "X-Title": "Daedalus"}
        elif pc.kind == "vllm":
            # The environment pair is the deployment-time default; the operator may point
            # any of these at a different endpoint (base_url, api_key) in config.toml.
            base_url = base_url or self._settings.vllm_base_url
        # A per-endpoint key stored in config.toml wins; otherwise fall back to the
        # environment key of the built-in kinds. Self-hosted endpoints need no key at all.
        api_key = pc.api_key or env_keys.get(pc.kind, "")
        if not base_url:
            return None
        if pc.kind in ("deepseek", "openrouter", "opencode") and not api_key and _is_vendor_host(pc.kind, base_url):
            return None  # the vendor itself needs a key; a key proxy in front of it does not
        return ProviderEndpoint(
            id=provider_id,
            kind=pc.kind,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=pc.timeout_seconds,
            extra_headers=headers,
            # A llama.cpp process runs the operator's own model. It has no billable token price, so
            # neither a similarly named hosted model nor a stale hand-written price may make its
            # calls consume a session, provider, total or daily spending cap.
            pricing={} if pc.kind == "llamacpp" else {**self._fetched.get(pc.kind, {}), **pricing_table(pc.kind, pc.pricing)},
            temperature=pc.temperature,
        )

    async def refresh_prices(self, db: Any | None = None, *, force: bool = False) -> int:
        """Bring the fetched price tables up to date and put them under every live endpoint of their kind.

        The catalogue is read from the database when it is younger than a day, from models.dev otherwise; a
        fetch that fails leaves whatever table was there. Returns how many models got a price this way.
        """
        from daedalus.providers import modelsdev  # Lazy: keeps the catalogue client out of every registry import

        cached = await db.kv_get(modelsdev.KV_KEY, None) if db is not None else None
        now = time.time()
        tables: dict[str, dict[str, ModelPricing]] = {}
        fresh = isinstance(cached, dict) and now - float(cached.get("at") or 0) < modelsdev.REFRESH_SECONDS
        if fresh and not force:
            tables = {kind: modelsdev.table_from_entries(entries) for kind, entries in (cached.get("tables") or {}).items() if isinstance(entries, dict)}
        else:
            try:
                catalog = await modelsdev.fetch_catalog()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("models.dev prices not refreshed: %s", exc)
                if isinstance(cached, dict):
                    tables = {kind: modelsdev.table_from_entries(entries) for kind, entries in (cached.get("tables") or {}).items() if isinstance(entries, dict)}
            else:
                tables = {kind: modelsdev.prices_from_catalog(catalog, ids) for kind, ids in modelsdev.SOURCES.items()}
                if db is not None:
                    await db.kv_set(modelsdev.KV_KEY, {"at": now, "tables": {kind: modelsdev.entries_from_table(table) for kind, table in tables.items()}})
        if not tables:
            return 0
        self._fetched = tables
        config = getattr(self, "_config", None)
        for provider_id, provider in self._providers.items():
            table = tables.get(provider.endpoint.kind)
            if not table:
                continue
            configured = set(config.providers[provider_id].pricing if config and provider_id in config.providers else {})
            for model, price in table.items():
                if model not in configured:
                    provider.endpoint.pricing[model] = price
        return sum(len(table) for table in tables.values())

    def get(self, provider_id: str) -> OpenAICompatibleProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise KeyError(
                f"provider {provider_id!r} is not configured or has no API key; "
                f"available: {sorted(self._providers)}"
            ) from exc

    def available(self) -> Sequence[str]:
        return tuple(sorted(self._providers))

    def rungs_for(self, config: RuntimeConfig, preset_id: str | None = None) -> list[tuple[OpenAICompatibleProvider, str]]:
        """``(adapter, model)`` pairs: the chosen (or default) preset first, then the fallback chain."""
        first_id, first = config.preset(preset_id)
        order = [first_id]
        if not first.free_only:
            order.extend(c for c in config.model.chain if c != first_id)
            if config.model.preset not in order:
                order.append(config.model.preset)
        rungs: list[tuple[OpenAICompatibleProvider, str]] = []
        for pid in order:
            preset = config.presets.get(pid)
            provider = self._providers.get(preset.provider) if preset else None
            if preset is None or provider is None or not preset.model:
                continue
            rungs.append((provider, preset.model))
        if not rungs:
            raise RuntimeError("no usable model: every preset points at a client without a URL or key")
        return rungs

    @staticmethod
    def room_for(config: RuntimeConfig) -> dict[tuple[str, str], tuple[int, int]]:
        """``(provider id, model) → (context_window, max_output_tokens)`` for every preset, for the chain's fit check."""
        return {(p.provider, p.model): (int(p.context_window), int(p.max_output_tokens)) for p in config.presets.values() if p.model}

    def rungs_for_pair(self, config: RuntimeConfig, provider_id: str, model: str) -> list[tuple[OpenAICompatibleProvider, str]]:
        """An ad-hoc provider/model pair first (a manual ``/model vllm/x``), the chain behind it."""
        target = self._providers.get(provider_id)
        if target is None or not model:
            return self.rungs_for(config)
        rest = [(p, m) for p, m in self.rungs_for(config) if not (p.endpoint.id == provider_id and m == model)]
        return [(target, model), *rest]

    async def aclose(self) -> None:
        retired, self._retired = self._retired, []
        for provider in set([*retired, *self._providers.values()]):
            await provider.aclose()


__all__ = ["ProviderRegistry"]
