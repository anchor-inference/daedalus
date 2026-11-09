"""OpenRouter's providers of one model: what the listing says, what a choice costs, what a refusal means.

A model on OpenRouter is served by several endpoints, each with its own price, limits and features.
``GET /models/{author}/{slug}/endpoints`` lists them and each carries a ``tag``, the slug that
``provider.only`` takes in a request. The tag is the unit here, not the company: DeepInfra alone has a plain,
a ``flex`` and a ``priority`` endpoint of one model at three prices, and only the tag names one of them.

The choice is sent as ``only`` rather than ``order`` for two reasons, both from OpenRouter's documentation.
``order`` is a preference the router may leave when ``allow_fallbacks`` permits, and a manual ``order``
turns off the sticky routing that keeps a conversation on the provider holding its prompt cache; ``only`` keeps
it among the chosen ones. ``allow_fallbacks: false`` goes with it so the request says outright that a reroute
is unwanted. ``require_parameters`` is left off: every request carries fields some providers ignore, such
as ``usage``, and a provider dropped for one of those would be a refusal with no cause the operator could see.

Prices come out of here as the most a call can cost, because a price recorded here is what a spending cap
reserves before the call and a low one is how the cap gets passed. A listed price can be lower than the bill
in three ways, each handled below: a provider with hours at twice the price (``overrides``), a tag that
also covers other endpoints, and a choice of several providers, of which the router alone picks one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

import httpx

from daedalus.providers.context_window import Found

logger = logging.getLogger(__name__)

MODEL_ID = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:+-]+")
"""``author/slug`` with an optional ``:variant``. Anything else is not on the catalogue's path, and the id goes
into a URL that carries the operator's key."""
ZDR_REFRESH_SECONDS = 3600.0
"""The list covers every model on OpenRouter, over a megabyte of it, and a policy does not change by the minute."""
PRICE_FIELDS = ("input", "output", "cache_hit")


def per_million(pricing: Any) -> dict[str, float]:
    """A price per token (what OpenRouter publishes) as a price per million (what everything here uses)."""
    if not isinstance(pricing, dict):
        return {}
    out: dict[str, float] = {}
    for name, key in (("input", "prompt"), ("output", "completion"), ("cache_hit", "input_cache_read")):
        if isinstance(pricing.get(key), bool):
            continue
        try:
            value = float(pricing[key])
        except (KeyError, TypeError, ValueError):
            continue
        if value >= 0 and math.isfinite(value * 1_000_000):
            out[name] = round(value * 1_000_000, 6)
    return out


def routing_body(only: Sequence[str]) -> dict[str, Any]:
    """The ``provider`` object of a request limited to ``only``."""
    return {"only": list(only), "allow_fallbacks": False}


def _highest_rates(pricing: Any) -> tuple[dict[str, float], bool]:
    """The dearest rates of an endpoint, and whether they differ by the hour.

    ``overrides`` holds a provider's windows by time of day and day of the week: DeepSeek's own endpoint is
    listed at its cheap rate and charges double at peak. The listed price is already after ``discount``.
    """
    rates = per_million(pricing)
    varies = False
    for window in pricing.get("overrides") or [] if isinstance(pricing, dict) else []:
        other = per_million(window)
        for name, value in other.items():
            if name in rates and value != rates[name]:
                varies = True
                rates[name] = max(rates[name], value)
    return rates, varies


def _tool_choice(raw: dict[str, Any], tools: bool) -> str:
    """``full`` when every way the host forces a tool call works; ``partial`` where one is dropped.

    A run asks for ``required``, and a forced retry for one named function. Relace was seen to answer a
    ``required`` it does not list, so this is what the provider promises rather than a refusal to come.
    """
    if not tools:
        return "none"
    choice = raw.get("supports_tool_choice")
    if not isinstance(choice, dict):
        return "full"
    return "full" if choice.get("auto") and choice.get("required") and choice.get("function") else "partial"


def endpoint_row(raw: Any, zdr: frozenset[str] | None = None) -> dict[str, Any] | None:
    """One endpoint as the app shows it; ``None`` when it has no tag or no price, and so cannot be chosen.

    ``zdr`` is the tags of this model on the zero-retention list, or ``None`` when that list could not be read.
    """
    if not isinstance(raw, dict):
        return None
    slug = str(raw.get("tag") or "").strip()
    rates, varies = _highest_rates(raw.get("pricing"))
    if not slug or "input" not in rates or "output" not in rates:
        return None
    parameters = raw.get("supported_parameters")
    parameters = parameters if isinstance(parameters, list) else []
    tools = "tools" in parameters
    quantization = raw.get("quantization")
    uptime = next((raw[k] for k in ("uptime_last_1d", "uptime_last_30m") if type(raw.get(k)) in (int, float)), None)
    return {
        "slug": slug,
        "name": str(raw.get("provider_name") or slug),
        "quantization": str(quantization) if quantization and quantization != "unknown" else None,
        "tier": raw.get("service_tier") or None,
        "input": rates["input"],
        "output": rates["output"],
        "cache_hit": rates.get("cache_hit"),
        "varies": varies,
        "context_length": raw.get("context_length") if type(raw.get("context_length")) is int else None,
        "max_output_tokens": raw.get("max_completion_tokens") if type(raw.get("max_completion_tokens")) is int else None,
        "tools": tools,
        "tool_choice": _tool_choice(raw, tools),
        "reasoning": "reasoning" in parameters or "reasoning_effort" in parameters,
        "caching": bool(raw.get("supports_implicit_caching")) or "cache_hit" in rates,
        "uptime": round(float(uptime), 2) if uptime is not None else None,
        "zdr": None if zdr is None else slug in zdr,
    }


def covers(slug: str, row: dict[str, Any]) -> bool:
    """Whether naming ``slug`` in ``only`` can reach this endpoint.

    A slug also matches the variants under it: ``fireworks`` takes in ``fireworks/us``, listed at half as much
    again. OpenRouter's documentation excepts tier endpoints, which a base slug does not select.
    """
    target = str(row["slug"])
    return target == slug or (target.startswith(slug + "/") and not row.get("tier"))


def bill_for(rows: Sequence[dict[str, Any]], only: Sequence[str]) -> dict[str, float | None]:
    """The most a token can cost across the endpoints ``only`` reaches, field by field.

    ``cache_hit`` is ``None`` when any of them publishes no such rate. Left at the others' rate, a call
    served where no cache price exists would be estimated at a cache discount it does not get; absent, a cached
    token is counted at the fresh rate (:meth:`ModelPricing.cost`), which is the upper bound.
    """
    reached = [row for row in rows if any(covers(slug, row) for slug in only)]
    if not reached:
        return {}
    hits = [row.get("cache_hit") for row in reached]
    return {
        "input": max(float(row["input"]) for row in reached),
        "output": max(float(row["output"]) for row in reached),
        "cache_hit": None if any(hit is None for hit in hits) else max(float(hit) for hit in hits if hit is not None),
    }


def gone(rows: Sequence[dict[str, Any]], only: Sequence[str]) -> list[str]:
    """The chosen slugs the listing no longer serves."""
    return [slug for slug in only if not any(covers(slug, row) for row in rows)]


def priced_entry(prior: Any, bill: dict[str, float | None]) -> dict[str, Any]:
    """``bill`` as a ``[providers.*.pricing]`` entry, keeping what the listing does not speak for.

    The operator's input ceiling stays. An old ``cache_hit`` does not: an unknown one has to be absent, or
    the previous provider's discount outlives it.
    """
    if isinstance(prior, dict) and prior.get("manual"):
        # The operator typed this price in the model's settings: a choice of providers does not move it.
        return dict(prior)
    entry = dict(prior) if isinstance(prior, dict) else {}
    for name in PRICE_FIELDS:
        entry.pop(name, None)
        if bill.get(name) is not None:
            entry[name] = bill[name]
    return entry


def lowest_window(rows: Sequence[dict[str, Any]], only: Sequence[str]) -> int | None:
    """The window every chosen endpoint can hold: alone among the numbers here, the smallest is the safe one."""
    windows = [row["context_length"] for row in rows if row.get("context_length") and any(covers(slug, row) for slug in only)]
    return min(windows) if windows else None


def lowest_output(rows: Sequence[dict[str, Any]], only: Sequence[str]) -> int | None:
    """The reply length every chosen endpoint allows; a longer bound is cut short by the router, not refused."""
    limits = [row["max_output_tokens"] for row in rows if row.get("max_output_tokens") and any(covers(slug, row) for slug in only)]
    return min(limits) if limits else None


def refused_route(text: str) -> str | None:
    """A sentence for the operator when this error body says no chosen provider can serve the model, else ``None``.

    The router answers 404 with the failed step and the slugs in ``metadata``, and this is the one case where
    its own words ("No allowed providers…") do not say what to do. Read off a real refusal, November 2026.
    """
    try:
        body = json.loads(text)
    except ValueError:
        return None
    error = body.get("error", body) if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None
    metadata = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
    message = str(error.get("message") or "")
    if metadata.get("failed_routing_step") != "Filter by Allowed Providers" and "provider.only" not in message:
        return None
    asked = [str(slug) for slug in metadata.get("requested_providers") or []]
    named = ", ".join(asked) if asked else "the ones chosen"
    return f"none of the providers chosen for this model can serve it now ({named}). The request was not sent anywhere else; change the choice in the model's settings"


class ZdrList:
    """The zero-retention list, read once an hour. Best effort: without it the app shows no such mark."""

    def __init__(self) -> None:
        self.at = 0.0
        self.tags: dict[str, frozenset[str]] = {}
        self._lock = asyncio.Lock()

    async def of(self, client: httpx.AsyncClient, base: str, headers: dict[str, str], model: str) -> frozenset[str] | None:
        async with self._lock:
            if time.monotonic() - self.at >= ZDR_REFRESH_SECONDS or not self.at:
                try:
                    response = await client.get(f"{base}/endpoints/zdr", headers=headers)
                    response.raise_for_status()
                    data = response.json()["data"]
                    table: dict[str, set[str]] = {}
                    for row in data:
                        table.setdefault(str(row.get("model_id")), set()).add(str(row.get("tag")))
                except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
                    logger.warning("the zero-retention list could not be read: %s", type(exc).__name__)
                    return None
                self.tags = {name: frozenset(found) for name, found in table.items()}
                self.at = time.monotonic()
        return self.tags.get(model, frozenset())


ZDR = ZdrList()


async def fetch_listing(base_url: str, api_key: str, model: str, *, client: httpx.AsyncClient | None = None, zero_retention: bool = False) -> dict[str, Any] | None:
    """The endpoints of ``model``, cheapest first, or ``None`` when the listing could not be read.

    A model the catalogue does not know comes back with no rows, which is an answer: a daily check must tell
    that apart from a network that is down, because only the first means the chosen providers are gone.
    Each row's ``bill`` is what ticking it alone records, the variants it covers included.
    """
    if not MODEL_ID.fullmatch(model):
        return None
    base = base_url.rstrip("/")
    headers = {"accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    owned = client is None
    active = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False)
    try:
        try:
            response = await active.get(f"{base}/models/{quote(model, safe='/:+')}/endpoints", headers=headers)
            if response.status_code == 404:
                return {"model": model, "images": False, "rows": []}
            response.raise_for_status()
            data = response.json()["data"]
            endpoints = data["endpoints"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning("the providers of %s could not be read: %s", model, type(exc).__name__)
            return None
        if not isinstance(endpoints, list):
            return None
        zdr = await ZDR.of(active, base, headers, model) if zero_retention else None
    finally:
        if owned:
            await active.aclose()
    rows = [row for row in (endpoint_row(raw, zdr) for raw in endpoints) if row is not None]
    rows.sort(key=lambda row: (row["input"] + row["output"] * 4, row["slug"]))
    for row in rows:
        row["bill"] = bill_for(rows, [row["slug"]])
    architecture = data.get("architecture") if isinstance(data, dict) else None
    modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
    return {"model": model, "images": isinstance(modalities, list) and "image" in modalities, "rows": rows}


async def route_window(base_url: str, api_key: str, model: str, only: Sequence[str]) -> Found | None:
    """The context window of a limited model: the smallest of its chosen endpoints, not the model's largest."""
    listing = await fetch_listing(base_url, api_key, model)
    tokens = lowest_window(listing["rows"], only) if listing else None
    return Found(tokens, "the chosen OpenRouter providers") if tokens else None


__all__ = [
    "ZDR",
    "bill_for",
    "covers",
    "endpoint_row",
    "fetch_listing",
    "gone",
    "lowest_output",
    "lowest_window",
    "per_million",
    "priced_entry",
    "refused_route",
    "route_window",
    "routing_body",
]
