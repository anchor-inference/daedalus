"""How much a model can hold, found out rather than asked of the operator.

Every preset needs a context window: the run compacts against it and sizes its replies from it. A
number typed by hand is a question most people who use the app cannot answer, and the old answer for
them — 128K for everything — is wrong in both directions: a 1M model compacted at an eighth of its
room, a local server loaded with 8K refused the first long turn.

The window is taken from the first of these that knows it:

1. **The provider's own listing.** Many endpoints say it beside the model id, each in its own field:
   OpenRouter, Together and Moonshot ``context_length``, vLLM ``max_model_len``, Groq
   ``context_window``, Mistral ``max_context_length``, LM Studio ``loaded_context_length`` /
   ``max_context_length``, Anthropic ``max_input_tokens``, Google ``inputTokenLimit``, llama.cpp
   ``n_ctx`` at ``/props``, Ollama ``num_ctx`` or the loaded length at ``/api/ps``, and the Codex login
   ``context_window`` (through the key proxy). The request goes to the provider's configured base
   URL, so an endpoint behind the key proxy is asked through it and its key never leaves the proxy.
2. **models.dev**, the public catalogue the price refresh already downloads once a day.
3. **The bundled table** of model families (``context_windows.toml``), for ids nothing else knows.
4. **A refusal.** A provider that rejects a request with "maximum context length is N tokens" has
   stated the real limit; it is remembered and, when smaller, wins over everything above.

When none of them knows, the preset keeps the number it has (the 128K default, or a seed's guess).
This module is the pure part: parsing, matching and the order. Remembering results and writing them
into the configuration is :mod:`daedalus.extensions.context_windows`.
"""

from __future__ import annotations

import fnmatch
import logging
import math
import re
import tomllib
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

DEFAULT_WINDOW = 128_000
"""What a preset starts with before anything is known; the configuration's own default."""

MANUAL = "manual"
AUTOMATIC_SOURCES = ("provider", "catalog", "table", "error", "default")
"""Every source but ``manual`` is the resolver's and may be replaced when it learns better."""

TABLE_PATH = Path(__file__).with_name("context_windows.toml")

_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
"""Per request. A listing is asked when a preset is saved and in a background sweep, never while a run
waits, but a save waits for it, and an unreachable local server must not hold the screen for long."""


@dataclass(frozen=True, slots=True)
class Found:
    """A window one source reported, with a few words on where exactly it came from."""

    tokens: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Resolved:
    """The window a preset should have, and which source decided it."""

    tokens: int
    source: str
    detail: str = ""


def _positive(value: Any) -> int | None:
    """A token count from JSON, or ``None`` for anything that is not a plausible one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if type(value) in (int, float) and math.isfinite(value) and 1_000 <= value <= 100_000_000:
        return int(value)
    return None


ENTRY_FIELDS: tuple[tuple[str, ...], ...] = (
    ("loaded_context_length",),
    ("max_model_len",),
    ("context_length",),
    ("top_provider", "context_length"),
    ("context_window",),
    ("max_context_length",),
    ("max_input_tokens",),
    ("inputTokenLimit",),
    ("input_token_limit",),
    ("limit", "context"),
)
"""Where each endpoint puts the window in a model entry, most specific first.

``loaded_context_length`` (LM Studio) is the length the server actually loaded, which can be less
than ``max_context_length``, the most the model could take; vLLM's ``max_model_len`` is likewise what
the server was started with. The rest name the model's published limit. llama.cpp's ``n_ctx_train``
is deliberately absent: it is the length the model was trained at, not what the server serves."""


def window_from_entry(entry: Any) -> int | None:
    """The window a model-list entry states, whichever vendor's field it uses."""
    if not isinstance(entry, dict):
        return None
    for path in ENTRY_FIELDS:
        node: Any = entry
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        tokens = _positive(node)
        if tokens is not None:
            return tokens
    return None


def listing_rows(payload: Any) -> list[dict[str, Any]]:
    """The model entries of a listing, in any of the shapes endpoints use.

    ``{"data": [...]}`` (OpenAI and nearly everyone), ``{"models": [...]}`` (Google, the Codex cache,
    Ollama's tags) and a bare list (Together).
    """
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("models")
    else:
        rows = None
    return [r for r in rows or [] if isinstance(r, dict)]


def entry_id(entry: dict[str, Any]) -> str:
    """A listing entry's model id; Google writes ``name: "models/gemini-…"``, the Codex cache ``slug``."""
    raw = entry.get("id") or entry.get("slug") or entry.get("model") or entry.get("name") or ""
    return str(raw).strip().removeprefix("models/")


def find_entry(rows: list[dict[str, Any]], model: str) -> dict[str, Any] | None:
    """The entry for ``model``: exact id first, then the same id under a ``vendor/`` prefix."""
    wanted = model.strip().removeprefix("models/")
    by_id = {entry_id(r): r for r in rows}
    if wanted in by_id:
        return by_id[wanted]
    lowered = wanted.lower()
    for key, row in by_id.items():
        if key.lower() == lowered or key.lower().rsplit("/", 1)[-1] == lowered.rsplit("/", 1)[-1]:
            return row
    return None


def normalized_model(model: str) -> str:
    """A model id as the bundled table is keyed: lower case, no ``vendor/`` prefix, no ``:tag``."""
    name = model.strip().lower().rsplit("/", 1)[-1]
    return name.split(":", 1)[0]


@lru_cache(maxsize=1)
def _table() -> tuple[tuple[str, int], ...]:
    try:
        with TABLE_PATH.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        logger.warning("the bundled context-window table is unreadable", exc_info=True)
        return ()
    rows = [(str(k).lower(), int(v)) for k, v in (data.get("windows") or {}).items() if _positive(v)]
    # Most specific first: the pattern with the most literal characters decides.
    return tuple(sorted(rows, key=lambda row: -len(row[0].replace("*", "").replace("?", ""))))


def table_window(model: str) -> int | None:
    """The bundled table's window for ``model``, or ``None`` when no family matches."""
    name = normalized_model(model)
    if not name:
        return None
    for pattern, tokens in _table():
        if fnmatch.fnmatchcase(name, pattern):
            return tokens
    return None


def windows_from_catalog(catalog: dict[str, Any]) -> dict[str, int]:
    """models.dev's ``limit.context`` per model id, across every provider it lists.

    The same model is listed under many hosts, nearly always with the same number; where hosts differ
    the most common value wins and a tie goes to the smaller, the one every host can serve. Ids are
    kept as listed and also without their ``vendor/`` prefix, which is how most endpoints name them.
    """
    seen: dict[str, Counter[int]] = {}
    for provider in catalog.values():
        models = provider.get("models") if isinstance(provider, dict) else None
        if not isinstance(models, dict):
            continue
        for model_id, model in models.items():
            limit = model.get("limit") if isinstance(model, dict) else None
            tokens = _positive(limit.get("context")) if isinstance(limit, dict) else None
            if tokens is None:
                continue
            for key in {str(model_id).lower(), str(model_id).lower().rsplit("/", 1)[-1]}:
                seen.setdefault(key, Counter())[tokens] += 1
    return {key: max(counts, key=lambda t: (counts[t], -t)) for key, counts in seen.items()}


def catalog_window(catalog: dict[str, int], model: str) -> int | None:
    lowered = model.strip().lower()
    return catalog.get(lowered) or catalog.get(lowered.rsplit("/", 1)[-1]) or catalog.get(normalized_model(model))


def decide(*, provider: Found | None, catalog: int | None, table: int | None, refused: int | None) -> Resolved | None:
    """The window from what each source knows, in the order the module describes.

    ``None`` when nothing is known at all: the preset keeps what it has rather than being reset to a
    default that may be further from the truth than a seed's guess. A refusal wins when it is smaller
    than what the sources say, because a provider that refused a request at N cannot be sent more than
    N whatever its listing claims; a larger one changes nothing, since the window was never reached.
    """
    base: Resolved | None = None
    if provider is not None:
        base = Resolved(provider.tokens, "provider", provider.detail)
    elif catalog is not None:
        base = Resolved(catalog, "catalog", "models.dev")
    elif table is not None:
        base = Resolved(table, "table", "")
    if refused is not None and (base is None or refused < base.tokens):
        return Resolved(refused, "error", "")
    return base


_STATED_WINDOW = (
    # OpenAI, vLLM, DeepSeek, Moonshot, OpenRouter: "This model's maximum context length is 131072 tokens."
    re.compile(r"maximum context length is (\d+)", re.IGNORECASE),
    # Anthropic: "prompt is too long: 210582 tokens > 200000 maximum"
    re.compile(r"tokens?\s*>\s*(\d+)\s*maximum", re.IGNORECASE),
    # llama.cpp: "the request exceeds the available context size (8192 tokens)"
    re.compile(r"available context size \((\d+) tokens?\)", re.IGNORECASE),
    # Google: "The input token count (1200000) exceeds the maximum number of tokens allowed (1048576)."
    re.compile(r"maximum number of tokens allowed \((\d+)\)", re.IGNORECASE),
    # LM Studio and others: "context length of only 4096 tokens"
    re.compile(r"context length of (?:only )?(\d+) tokens", re.IGNORECASE),
)


def stated_window(text: str) -> int | None:
    """The context window a refusal states, in any of the phrasings providers use; ``None`` without one.

    Wider than the parser the core's corrective retry reads (which also needs the prompt and output
    sizes, and so only knows the vLLM sentence): here only the limit matters.
    """
    for pattern in _STATED_WINDOW:
        found = pattern.search(text or "")
        if found:
            return _positive(int(found.group(1)))
    return None


# -- asking the provider ---------------------------------------------------------------------------


async def _get_json(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> Any:
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


async def _post_json(client: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any]) -> Any:
    try:
        response = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


_NUM_CTX = re.compile(r"^\s*num_ctx\s+(\d+)\s*$", re.MULTILINE)


async def _ollama_window(client: httpx.AsyncClient, root: str, model: str, headers: dict[str, str]) -> Found | None:
    """Ollama's served length: the model's ``num_ctx`` parameter, else the length it is loaded with.

    The trained length in ``model_info`` is not used. Ollama serves the OpenAI API at whatever length
    the server was started with and cuts a longer prompt silently instead of refusing it, so a window
    taken from the trained length would be a window the runs never really had.
    """
    shown = await _post_json(client, root + "/api/show", headers, {"model": model})
    if isinstance(shown, dict) and ("model_info" in shown or "modelfile" in shown or "parameters" in shown):
        found = _NUM_CTX.search(str(shown.get("parameters") or ""))
        if found and _positive(int(found.group(1))):
            return Found(int(found.group(1)), "Ollama num_ctx")
        running = await _get_json(client, root + "/api/ps", headers)
        for row in listing_rows(running):
            if entry_id(row) in (model, f"{model}:latest") or str(row.get("model") or "") == model:
                tokens = _positive(row.get("context_length"))
                if tokens:
                    return Found(tokens, "Ollama, as loaded")
    return None


async def provider_window(
    base_url: str,
    model: str,
    *,
    kind: str = "openai_compat",
    api_key: str = "",
    client: httpx.AsyncClient | None = None,
) -> Found | None:
    """Ask the endpoint at ``base_url`` how long ``model``'s context is; ``None`` when it does not say.

    The listing first (``{base}/models``, and ``{base}/v1/models`` for a base typed without it), then
    the model's own entry (``{base}/models/{id}``: Anthropic answers it with ``max_input_tokens``),
    then, for a self-hosted kind, the servers whose length lives beside their OpenAI API: llama.cpp's
    ``/props``, LM Studio's ``/api/v0/models`` and Ollama's ``/api/show``. Every failure is quiet — a
    404 from a server that is not the one being probed is the expected answer, not an error.
    """
    base = (base_url or "").strip().rstrip("/")
    if not model or not re.match(r"^https?://", base, re.IGNORECASE):
        return None
    headers = {"accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    owns = client is None
    # No redirects: these requests may carry the operator's key, and a redirect is not permission to
    # send it to another origin.
    client = client or httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False)
    try:
        if kind == "llamacpp":
            from daedalus.providers.llamacpp import discover_llamacpp  # Lazy: pulls in the tool-schema code

            discovered = await discover_llamacpp(base, api_key or None, client=client)
            if discovered.context_window:
                return Found(int(discovered.context_window), "llama.cpp /props")
        candidates = [base + "/models"] + ([] if base.endswith("/v1") else [base + "/v1/models"])
        for url in candidates:
            rows = listing_rows(await _get_json(client, url, headers))
            if not rows:
                continue
            entry = find_entry(rows, model)
            tokens = window_from_entry(entry) if entry is not None else None
            if tokens:
                return Found(tokens, "")
            single = await _get_json(client, url + "/" + quote(model, safe=""), headers)
            tokens = window_from_entry(single)
            if tokens:
                return Found(tokens, "")
            break
        if kind in ("openai_compat", "vllm"):
            root = base.removesuffix("/v1")
            lmstudio = await _get_json(client, root + "/api/v0/models/" + quote(model, safe=""), headers)
            tokens = window_from_entry(lmstudio)
            if tokens:
                return Found(tokens, "LM Studio")
            ollama = await _ollama_window(client, root, model, headers)
            if ollama is not None:
                return ollama
    finally:
        if owns:
            await client.aclose()
    return None


__all__ = [
    "AUTOMATIC_SOURCES",
    "DEFAULT_WINDOW",
    "ENTRY_FIELDS",
    "MANUAL",
    "Found",
    "Resolved",
    "catalog_window",
    "decide",
    "entry_id",
    "find_entry",
    "listing_rows",
    "normalized_model",
    "provider_window",
    "stated_window",
    "table_window",
    "window_from_entry",
    "windows_from_catalog",
]
