"""Subscription upstreams for the key proxy: the operator's Codex (ChatGPT) and Grok logins.

The CLIs (``codex``, ``grok``) leave their OAuth tokens in ``~/.codex/auth.json`` and
``~/.grok/auth.json``. This module reads them, refreshes them the way the CLIs do (the
refreshed tokens are written back so the CLIs keep working), sends the identity headers
each backend expects, and — for Codex, whose backend speaks only the Responses API —
translates an OpenAI chat completion into a Responses request and the streamed answer back.

Claude Code is handled in ``claude.py`` the same way (``~/.claude/.credentials.json``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("keyproxy.subscriptions")

CODEX_BASE = "https://chatgpt.com/backend-api"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_HEADERS = {"OpenAI-Beta": "responses=experimental", "originator": "codex_cli_rs"}
GROK_BASE = "https://cli-chat-proxy.grok.com/v1"
GROK_ISSUER = "https://auth.x.ai"
REFRESH_SKEW_SECONDS = 300
USAGE_CACHE_SECONDS = 60
MODELS_CACHE_SECONDS = 300
# The usage table names only models already called. Astra is the CLI's current default and
# was missing from this list, so a usage call that failed hid it.
# The models ``codex debug models`` offered with visibility "list" on 0.157.1. The usage table
# names only models that have already been called, so a new one stays invisible until it is here.
CODEX_FALLBACK_MODELS = ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5")


class SubscriptionError(RuntimeError):
    pass


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else {}
    except (IndexError, ValueError):
        return {}


def _installed_grok_version() -> str:
    """The grok CLI version the chat proxy expects on ``x-grok-client-version``.

    A constant here went stale at 0.2.101 while the CLI moved to 1.x, and this proxy has
    already seen that host answer 426 "CLI version outdated" for a missing version. The
    binary is mounted next to the login (``<grok home>/bin/grok`` → ``grok-<version>-…``),
    so the version is read from that name. ``KEYPROXY_GROK_CLIENT_VERSION`` wins when set.
    """
    override = os.environ.get("KEYPROXY_GROK_CLIENT_VERSION", "").strip()
    if override:
        return override
    auth = Path(os.environ.get("KEYPROXY_GROK_AUTH", os.path.expanduser("~/.grok/auth.json")))
    try:
        name = (auth.parent / "bin" / "grok").resolve().name
    except OSError:
        name = ""
    found = re.search(r"(\d+\.\d+\.\d+)", name)
    return found.group(1) if found else "1.0.40"


def _grok_headers(version: str) -> dict[str, str]:
    return {
        "User-Agent": f"grok-shell/{version} (linux)",
        "x-grok-client-identifier": "grok-shell",
        "x-grok-client-version": version,
        "x-grok-client-mode": "interactive",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-authenticateresponse": "authenticate-response",
    }


GROK_CLIENT_VERSION = _installed_grok_version()
GROK_HEADERS = _grok_headers(GROK_CLIENT_VERSION)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


class CodexAuth:
    """``~/.codex/auth.json``: a ChatGPT OAuth token pair plus the account id."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    def available(self) -> bool:
        return self.path.is_file()

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SubscriptionError(f"codex login unreadable at {self.path}: {exc}") from exc

    async def credentials(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """(access token, account id), refreshed when the token is within the skew of its expiry."""
        data = self._load()
        tokens = data.get("tokens") or {}
        access = str(tokens.get("access_token") or "")
        if not access:
            raise SubscriptionError("codex: not logged in (run `codex login` on the host)")
        exp = int(_jwt_claims(access).get("exp") or 0)
        if exp and exp - REFRESH_SKEW_SECONDS > time.time():
            return access, str(tokens.get("account_id") or _jwt_claims(access).get("https://api.openai.com/auth", {}).get("chatgpt_account_id") or "")
        async with self._lock:
            data = self._load()  # the CLI may have refreshed meanwhile
            tokens = data.get("tokens") or {}
            access = str(tokens.get("access_token") or "")
            exp = int(_jwt_claims(access).get("exp") or 0)
            if exp and exp - REFRESH_SKEW_SECONDS > time.time():
                return access, str(tokens.get("account_id") or "")
            response = await client.post(CODEX_TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": tokens.get("refresh_token") or "", "client_id": CODEX_CLIENT_ID}, headers={"accept": "application/json"})
            if response.status_code != 200:
                raise SubscriptionError(f"codex token refresh failed: HTTP {response.status_code} {response.text[:160]}")
            fresh = response.json()
            tokens["access_token"] = fresh.get("access_token") or access
            if fresh.get("refresh_token"):
                tokens["refresh_token"] = fresh["refresh_token"]
            if fresh.get("id_token"):
                tokens["id_token"] = fresh["id_token"]
            data["tokens"] = tokens
            data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _write_json(self.path, data)
            logger.warning("codex token refreshed")
            return str(tokens["access_token"]), str(tokens.get("account_id") or "")

    async def headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        access, account = await self.credentials(client)
        out = {"authorization": f"Bearer {access}", **CODEX_HEADERS}
        if account:
            out["chatgpt-account-id"] = account
        return out


class GrokAuth:
    """``~/.grok/auth.json``: one OIDC session per principal; the first entry is the operator's."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._token_endpoint = ""

    def available(self) -> bool:
        return self.path.is_file()

    def _load(self) -> tuple[dict[str, Any], str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SubscriptionError(f"grok login unreadable at {self.path}: {exc}") from exc
        if not isinstance(data, dict) or not data:
            raise SubscriptionError("grok: not logged in (run `grok login` on the host)")
        key = next(iter(data))
        return data, key, dict(data[key])

    @staticmethod
    def _expires(entry: dict[str, Any]) -> float:
        raw = str(entry.get("expires_at") or "")
        if not raw:
            return 0.0
        try:
            from datetime import datetime

            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    async def token(self, client: httpx.AsyncClient) -> str:
        data, key, entry = self._load()
        if not entry.get("key"):
            raise SubscriptionError("grok: not logged in (run `grok login` on the host)")
        expires = self._expires(entry)
        if not expires or expires - REFRESH_SKEW_SECONDS > time.time():
            return str(entry["key"])
        async with self._lock:
            data, key, entry = self._load()
            expires = self._expires(entry)
            if not expires or expires - REFRESH_SKEW_SECONDS > time.time():
                return str(entry["key"])
            if not self._token_endpoint:
                issuer = str(entry.get("oidc_issuer") or GROK_ISSUER).rstrip("/")
                discovery = await client.get(issuer + "/.well-known/openid-configuration", headers={"accept": "application/json"})
                self._token_endpoint = str(discovery.json().get("token_endpoint") or issuer + "/oauth2/token")
            response = await client.post(
                self._token_endpoint,
                data={"grant_type": "refresh_token", "client_id": str(entry.get("oidc_client_id") or ""), "refresh_token": str(entry.get("refresh_token") or "")},
                headers={"accept": "application/json", "x-grok-client-version": GROK_CLIENT_VERSION, "x-grok-client-surface": "cli"},
            )
            if response.status_code != 200:
                raise SubscriptionError(f"grok token refresh failed: HTTP {response.status_code} {response.text[:160]}")
            fresh = response.json()
            entry["key"] = fresh.get("access_token") or entry["key"]
            if fresh.get("refresh_token"):
                entry["refresh_token"] = fresh["refresh_token"]
            if fresh.get("expires_in"):
                from datetime import UTC, datetime, timedelta

                entry["expires_at"] = (datetime.now(UTC) + timedelta(seconds=int(fresh["expires_in"]))).isoformat().replace("+00:00", "Z")
            data[key] = entry
            _write_json(self.path, data)
            logger.warning("grok token refreshed")
            return str(entry["key"])

    async def headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        return {"authorization": f"Bearer {await self.token(client)}", **GROK_HEADERS}


# -- Codex: chat completions ⇄ Responses ---------------------------------------------------------------


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def _user_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    out: list[dict[str, Any]] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            out.append({"type": "input_text", "text": str(block.get("text") or "")})
        elif block.get("type") == "image_url":
            url = block.get("image_url", {}).get("url") if isinstance(block.get("image_url"), dict) else block.get("image_url")
            if url:
                out.append({"type": "input_image", "image_url": str(url), "detail": "auto"})
    return out or [{"type": "input_text", "text": ""}]


def chat_to_responses(body: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI chat-completions body → the Responses body the Codex backend takes."""
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in body.get("messages") or []:
        role = message.get("role")
        if role == "system":
            instructions.append(_text_of(message.get("content")))
        elif role == "user":
            items.append({"type": "message", "role": "user", "content": _user_content(message.get("content"))})
        elif role == "assistant":
            text = _text_of(message.get("content"))
            if text:
                items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                items.append({"type": "function_call", "call_id": str(call.get("id") or ""), "name": str(fn.get("name") or ""), "arguments": fn.get("arguments") if isinstance(fn.get("arguments"), str) else json.dumps(fn.get("arguments") or {})})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": str(message.get("tool_call_id") or ""), "output": _text_of(message.get("content"))})
    out: dict[str, Any] = {
        "model": str(body.get("model") or ""),
        "instructions": "\n\n".join(i for i in instructions if i.strip()) or "You are a helpful assistant.",
        "input": items,
        "store": False,
        "stream": True,
        "parallel_tool_calls": True,
    }
    tools = []
    for tool in body.get("tools") or []:
        fn = tool.get("function") or {}
        if fn.get("name"):
            tools.append({"type": "function", "name": fn["name"], "description": fn.get("description") or "", "parameters": fn.get("parameters") or {"type": "object", "properties": {}}, "strict": False})
    if tools:
        out["tools"] = tools
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and (choice.get("function") or {}).get("name"):
        out["tool_choice"] = {"type": "function", "name": choice["function"]["name"]}
    elif isinstance(choice, str) and choice in ("auto", "none", "required"):
        out["tool_choice"] = choice
    # The ChatGPT backend rejects max_output_tokens ("Unsupported parameter"); the plan's own limits apply.
    effort = body.get("reasoning_effort")
    if effort:
        out["reasoning"] = {"effort": str(effort), "summary": "auto"}
    return out


def _chunk(completion_id: str, model: str, delta: dict[str, Any], finish: str | None = None, usage: dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {"id": completion_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def responses_usage(usage: dict[str, Any]) -> dict[str, Any]:
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": int(usage.get("total_tokens") or prompt + completion),
        "prompt_tokens_details": {"cached_tokens": int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)},
        "completion_tokens_details": {"reasoning_tokens": int((usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)},
    }


# An SSE comment: every OpenAI-compatible client ignores it, and collect_completion reads only
# ``data: `` lines. Sent for an upstream event that carries nothing to translate, so a model that
# reasons silently still shows the caller a live stream.
KEEPALIVE = ": keepalive\n\n"


async def responses_events_to_chunks(lines: AsyncIterator[str], *, model: str) -> AsyncIterator[str]:
    """Responses SSE lines → chat-completion chunks (text, reasoning summaries, tool calls, usage, finish).

    Events with nothing to translate (created, in_progress, reasoning item boundaries) become a
    keepalive comment, so a long silent reasoning phase is not mistaken for a dead stream. These
    follow real upstream events, never a timer.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    calls: dict[str, int] = {}
    finish = "stop"
    usage: dict[str, Any] | None = None
    failed: str | None = None
    async for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        spoke = False
        if kind == "response.output_text.delta" and event.get("delta"):
            spoke = True
            yield _chunk(completion_id, model, {"role": "assistant", "content": event["delta"]})
        elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta") and event.get("delta"):
            spoke = True
            yield _chunk(completion_id, model, {"reasoning_content": event["delta"]})
        elif kind == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                index = len(calls)
                calls[str(item.get("id") or item.get("call_id") or index)] = index
                spoke = True
                yield _chunk(completion_id, model, {"tool_calls": [{"index": index, "id": str(item.get("call_id") or ""), "type": "function", "function": {"name": str(item.get("name") or ""), "arguments": ""}}]})
                finish = "tool_calls"
        elif kind == "response.function_call_arguments.delta" and event.get("delta"):
            index = calls.get(str(event.get("item_id")), len(calls) - 1 if calls else 0)
            spoke = True
            yield _chunk(completion_id, model, {"tool_calls": [{"index": index, "function": {"arguments": event["delta"]}}]})
        elif kind == "response.completed":
            usage = responses_usage((event.get("response") or {}).get("usage") or {})
            status = (event.get("response") or {}).get("status")
            if status == "incomplete":
                finish = "length"
        elif kind in ("response.failed", "error"):
            err = (event.get("response") or {}).get("error") or event.get("error") or {}
            failed = str(err.get("message") or err or "the backend reported a failure")
        if not spoke:
            yield KEEPALIVE
    if failed:
        yield f"data: {json.dumps({'error': {'message': failed, 'type': 'upstream_error'}})}\n\n"
    else:
        yield _chunk(completion_id, model, {}, finish=finish, usage=usage)
    yield "data: [DONE]\n\n"


async def collect_completion(chunks: AsyncIterator[str], *, model: str) -> dict[str, Any]:
    """The non-streaming shape, assembled from the streamed chunks."""
    text_parts: list[str] = []
    reasoning: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish = "stop"
    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    async for chunk in chunks:
        if not chunk.startswith("data: ") or chunk.strip() == "data: [DONE]":
            continue
        payload = json.loads(chunk[6:])
        if "error" in payload:
            error = payload["error"]
            continue
        if payload.get("usage") is not None:
            usage = payload["usage"]
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                text_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            for call in delta.get("tool_calls") or []:
                slot = tool_calls.setdefault(int(call.get("index", 0)), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                slot["function"]["arguments"] += fn.get("arguments") or ""
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    if error:
        return {"error": error}
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion", "created": int(time.time()), "model": model, "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


# -- usage --------------------------------------------------------------------------------------------


def _quota_window_name(key: str, window: dict[str, Any]) -> str:
    """Name a Codex quota window from its length, not from which slot it sits in.

    Plus used to put five hours in ``primary_window`` and the week in ``secondary_window``.
    A plan with a single window puts that week in ``primary_window`` and leaves the other
    null, so a fixed label reported the week as "5h".
    """
    fallback = {"primary_window": "5h", "secondary_window": "weekly"}.get(key, key)
    raw = window.get("limit_window_seconds")
    if raw is None:
        return fallback
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return fallback
    if seconds <= 6 * 3600:
        return "5h"
    if seconds <= 8 * 86400:
        return "weekly"
    if seconds <= 32 * 86400:
        return "monthly"
    return fallback


def codex_usage_view(data: dict[str, Any]) -> dict[str, Any]:
    limits = data.get("rate_limit") or {}
    windows = []
    for key in ("primary_window", "secondary_window"):
        w = limits.get(key) or {}
        if isinstance(w, dict) and w:
            windows.append({"name": _quota_window_name(key, w), "used_percent": float(w.get("used_percent") or 0), "resets_at": w.get("reset_at"), "window_seconds": w.get("limit_window_seconds")})
    models = sorted(k for k, v in (data.get("model_usage") or {}).items() if isinstance(v, dict))
    return {"provider": "codex", "plan": data.get("plan_type"), "limit_reached": bool(limits.get("limit_reached")), "windows": windows, "models": models}


def grok_usage_view(billing: dict[str, Any]) -> dict[str, Any]:
    config = billing.get("config") or {}
    period = config.get("currentPeriod") or {}
    products = [{"product": p.get("product"), "used_percent": p.get("usagePercent")} for p in config.get("productUsage") or [] if isinstance(p, dict)]
    resets = period.get("end")
    windows = [{"name": "weekly", "used_percent": float(config.get("creditUsagePercent") or 0), "resets_at": resets}]
    return {"provider": "grok", "plan": "SuperGrok / X Premium", "limit_reached": float(config.get("creditUsagePercent") or 0) >= 100, "windows": windows, "products": products}


__all__ = [
    "CODEX_BASE",
    "CODEX_FALLBACK_MODELS",
    "GROK_BASE",
    "CodexAuth",
    "GrokAuth",
    "SubscriptionError",
    "chat_to_responses",
    "codex_usage_view",
    "collect_completion",
    "grok_usage_view",
    "KEEPALIVE",
    "responses_events_to_chunks",
]


OPENCODE_GO_BASE = "https://opencode.ai/zen/go/v1"


class OpencodeAuth:
    """OpenCode Go: a plain API key from the environment, no login to refresh."""

    def __init__(self, env_var: str = "OPENCODE_API_KEY") -> None:
        self.env_var = env_var

    def available(self) -> bool:
        return bool(os.environ.get(self.env_var, ""))

    async def headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        return {"authorization": f"Bearer {os.environ.get(self.env_var, '')}"}


def opencode_usage_view(data: dict[str, Any]) -> dict[str, Any]:
    """``GET /zen/go/v1/usage`` → the three allowance windows (5 h rolling, weekly, monthly) as percent used."""
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else data
    names = (("rolling", "5 h"), ("weekly", "weekly"), ("monthly", "monthly"))
    windows = []
    for key, name in names:
        w = usage.get(key) if isinstance(usage, dict) else None
        if not isinstance(w, dict):
            continue
        windows.append({"name": name, "used_percent": float(w.get("percent") or 0), "resets_at": w.get("resetsAt"), "status": w.get("status")})
    reached = any(w["used_percent"] >= 100 or (w.get("status") not in (None, "ok")) for w in windows)
    return {"provider": "opencode", "plan": "OpenCode Go", "limit_reached": reached, "windows": windows}

