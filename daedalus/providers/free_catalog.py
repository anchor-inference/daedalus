"""Public free-model discovery; a free listing is evidence of price, not agent compatibility."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from daedalus.config import is_keyproxy_url, keyproxy_upstream

SOURCES = (
    ("kilo", "Kilo Gateway", "https://api.kilo.ai/api/gateway/models", "https://api.kilo.ai/api/gateway", "https://kilo.ai/docs/gateway/authentication", False),
    ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1/models", "https://openrouter.ai/api/v1", "https://openrouter.ai/settings/keys", True),
    ("opencode_zen", "OpenCode Zen", "https://opencode.ai/zen/v1/models", "https://opencode.ai/zen/v1", "https://opencode.ai/auth", True),
)
ZEN_DOCS = "https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/web/src/content/docs/zen.mdx"
REFRESH_SECONDS = 60 * 5


def approved_endpoint(provider: str, actual: str, canonical: str) -> bool:
    """A free listing only protects calls sent to that provider, including its own key-proxy path."""
    return actual.rstrip("/") == canonical.rstrip("/") or (is_keyproxy_url(actual) and keyproxy_upstream(actual) == provider)


def _zero(value: Any) -> bool:
    try:
        return value is not None and float(value) == 0
    except (TypeError, ValueError):
        return False


def zen_free_ids(document: str) -> set[str]:
    """Join Zen's own endpoint and pricing tables by display name, then keep chat-compatible free IDs."""
    model_ids: dict[str, str] = {}
    free_names: set[str] = set()
    for line in document.splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip().strip("`") for cell in line.strip("| ").split("|")]
        if len(cells) >= 3 and cells[2] == "https://opencode.ai/zen/v1/chat/completions":
            model_ids[cells[0]] = cells[1]
        if len(cells) >= 3 and cells[1].lower() == "free" and cells[2].lower() == "free":
            free_names.add(cells[0])
    return {model_ids[name] for name in free_names if name in model_ids}


def free_rows(provider: str, payload: dict[str, Any], *, zen_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Use provider-published free evidence; never turn an absent price into zero."""
    listed = payload.get("data")
    if not isinstance(listed, list):
        return []
    rows = []
    for raw in listed:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        model_id = raw["id"].strip()
        if not model_id:
            continue
        if provider == "kilo":
            free = raw.get("isFree") is True
            mechanism = "zero_price"
            privacy = raw.get("mayTrainOnYourPrompts") is True
        elif provider == "openrouter":
            pricing = raw.get("pricing") or {}
            free = isinstance(pricing, dict) and _zero(pricing.get("prompt")) and _zero(pricing.get("completion"))
            mechanism = "zero_price"
            privacy = False
        else:
            # Zen's /models returns IDs alone. Join it to the official published price and
            # endpoint tables; a suffix alone cannot establish either fact.
            free = model_id in (zen_ids or set())
            mechanism = "zero_price"
            privacy = True
        if not free:
            continue
        entry: dict[str, Any] = {"id": model_id, "name": str(raw.get("name") or model_id)}
        top = raw.get("top_provider") if isinstance(raw.get("top_provider"), dict) else {}
        context = raw.get("context_length") or top.get("context_length")
        if isinstance(context, int | float) and context > 0:
            entry["context_length"] = int(context)
        output = top.get("max_completion_tokens")
        if isinstance(output, int | float) and output > 0:
            entry["max_output_tokens"] = int(output)
        architecture = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
        modalities = architecture.get("input_modalities")
        entry["images"] = isinstance(modalities, list) and "image" in modalities
        parameters = raw.get("supported_parameters")
        tools_reported = isinstance(parameters, list) and "tools" in parameters
        entry["reasoning"] = isinstance(parameters, list) and ("reasoning" in parameters or "reasoning_effort" in parameters)
        rows.append({
            **entry,
            "provider": provider,
            "mechanism": mechanism,
            "tools_reported": tools_reported,
            "agent_ready": False,
            "may_train": privacy,
        })
    return rows


class FreeCatalog:
    """Refresh fixed public endpoints with a last-known-good snapshot and bounded failure cost."""

    def __init__(self) -> None:
        self._snapshot: dict[str, Any] = {"providers": [], "updated_at": None, "stale": True}
        self._expires = 0.0
        self._lock = asyncio.Lock()
        self._probed: set[tuple[str, str]] = set()

    def mark_probed(self, provider: str, model: str) -> None:
        self._probed.add((provider, model))

    def view(self) -> dict[str, Any]:
        providers = [{**p, "models": [{**m, "agent_ready": (p["id"], m["id"]) in self._probed}
                                    for m in p["models"]]} for p in self._snapshot["providers"]]
        return {**self._snapshot, "providers": providers}

    async def get(self, *, client: httpx.AsyncClient | None = None, force: bool = False) -> dict[str, Any]:
        if not force and time.monotonic() < self._expires:
            return self.view()
        async with self._lock:
            if not force and time.monotonic() < self._expires:
                return self.view()
            own_client = client is None
            if client is None:
                client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0), follow_redirects=False)
            try:
                async def fetch(url: str, accept: str) -> httpx.Response | None:
                    for attempt in range(2):
                        try:
                            response = await client.get(url, headers={"accept": accept})
                            response.raise_for_status()
                            return response
                        except httpx.HTTPError:
                            if attempt == 0:
                                await asyncio.sleep(0.2)
                    return None

                async def read(source: tuple[str, str, str, str, str, bool]) -> dict[str, Any] | None:
                    provider, name, url, base_url, key_url, key_required = source
                    response = await fetch(url, "application/json")
                    if response is None:
                        return None
                    try:
                        payload = response.json()
                    except ValueError:
                        return None
                    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                        return None
                    zen_ids = None
                    if provider == "opencode_zen":
                        docs = await fetch(ZEN_DOCS, "text/plain")
                        if docs is None:
                            return None
                        zen_ids = zen_free_ids(docs.text)
                    return {"id": provider, "name": name, "base_url": base_url, "key_url": key_url, "fresh": True,
                            "key_required": key_required, "models": free_rows(provider, payload, zen_ids=zen_ids)}

                fetched = await asyncio.gather(*(read(source) for source in SOURCES))
            finally:
                if own_client:
                    await client.aclose()
            previous = {row["id"]: row for row in self._snapshot["providers"]}
            providers = [row or ({**previous[source[0]], "fresh": False} if source[0] in previous else None)
                         for source, row in zip(SOURCES, fetched, strict=True)]
            success = any(row is not None for row in fetched)
            if success:
                self._snapshot = {"providers": [row for row in providers if row is not None],
                                  "updated_at": int(time.time()), "stale": any(row is None for row in fetched)}
            else:
                self._snapshot = {**self._snapshot, "providers": [{**row, "fresh": False} for row in self._snapshot["providers"]],
                                  "stale": True}
            self._expires = time.monotonic() + (REFRESH_SECONDS if success else 60)
            return self.view()


async def probe_agent_cycle(base_url: str, model: str, api_key: str = "", *, client: httpx.AsyncClient | None = None) -> bool:
    """Require a tool call and an answer after its result before offering a model to an agent."""
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=5.0), follow_redirects=False)
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Call add_numbers with a=2 and b=3, then report its result."}]
    tools = [{"type": "function", "function": {"name": "add_numbers", "description": "Add two integers",
              "parameters": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                             "required": ["a", "b"]}}}]
    try:
        response = await client.post(base_url.rstrip("/") + "/chat/completions", headers=headers,
                                     json={"model": model, "messages": messages, "tools": tools,
                                           "tool_choice": "required", "max_tokens": 512})
        response.raise_for_status()
        answer = response.json()["choices"][0]["message"]
        calls = answer.get("tool_calls") or []
        if not calls or calls[0].get("function", {}).get("name") != "add_numbers":
            return False
        if json.loads(calls[0]["function"]["arguments"]) != {"a": 2, "b": 3}:
            return False
        messages.extend([answer, {"role": "tool", "tool_call_id": calls[0]["id"], "content": "5"}])
        followup = await client.post(base_url.rstrip("/") + "/chat/completions", headers=headers,
                                     json={"model": model, "messages": messages, "max_tokens": 128})
        followup.raise_for_status()
        final = followup.json()["choices"][0]["message"]
        return "5" in str(final.get("content") or "")
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return False
    finally:
        if own_client:
            await client.aclose()


catalog = FreeCatalog()
