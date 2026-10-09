"""Claude Code login as a key-proxy upstream.

The CLI stores OAuth tokens in ``~/.claude/.credentials.json``. This module
reads them, refreshes them the way the CLI does (and writes the new pair back
so the CLI keeps working), and translates OpenAI chat completions into
Anthropic ``/v1/messages`` using the CLI's identity headers and system prefix.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from subscriptions import KEEPALIVE, SubscriptionError, _write_json

logger = logging.getLogger("keyproxy.claude")

CLAUDE_API = "https://api.anthropic.com"
CLAUDE_TOKEN_URL = "https://claude.ai/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# The server refuses models to CLI versions it considers too old: a stale default here answers every request
# with a 400 invalid_request_error (claude_code_version_too_old), not with a degraded reply. Raise it when that
# refusal appears; the attribution stamp below follows the version on its own.
CLAUDE_CLI_VERSION = os.environ.get("KEYPROXY_CLAUDE_CLI_VERSION", "2.1.282")
CLAUDE_ENTRYPOINT = os.environ.get("KEYPROXY_CLAUDE_ENTRYPOINT", "sdk-cli")
# The CLI's attribution stamp: sha256(salt + three characters of the first user text + version)[:3].
# A fixed "9d8" belonged to 2.1.270. KEYPROXY_CLAUDE_VERSION_HASH still pins one stamp for every call.
_ATTRIBUTION_SALT = "59cf53e54c78"
_ATTRIBUTION_INDEXES = (4, 7, 20)
CLAUDE_BETAS = (
    "oauth-2025-04-20,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
    "context-management-2025-06-27,prompt-caching-scope-2026-01-05,claude-code-20250219,"
    "extended-cache-ttl-2025-04-11"
)
CLAUDE_IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
CLAUDE_FALLBACK_MODELS = ("claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-5", "claude-fable-5-1")
REFRESH_SKEW_SECONDS = 300
TOOL_PREFIX = "mcp_"
EFFORT_BUDGET = {"low": 2048, "medium": 8192, "high": 16384, "xhigh": 31999, "max": 31999}
EFFORT_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-fable-5-1", "claude-opus-4-8", "claude-sonnet-4-6")
MAX_OUTPUT = 32000
CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}
"""Anthropic prompt-cache marker. Prefix order is tools → system → messages; at most four per request."""
_UNCACHABLE_BLOCKS = frozenset({"thinking", "redacted_thinking"})


def _cli_headers() -> dict[str, str]:
    return {
        "User-Agent": f"claude-cli/{CLAUDE_CLI_VERSION} (external, {CLAUDE_ENTRYPOINT})",
        "x-app": "cli",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": CLAUDE_BETAS,
        "anthropic-dangerous-direct-browser-access": "true",
        "X-Stainless-Lang": "js",
        "X-Stainless-OS": "Linux",
        "X-Stainless-Arch": "x64",
        "X-Stainless-Package-Version": "0.112.1",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v26.3.0",
    }


def attribution_hash(text: str) -> str:
    """The three hex characters Claude Code writes after ``cc_version``.

    Taken from the CLI (2.1.278): the salt, the characters at indexes 4, 7 and 20 of the
    first user text (``0`` when the text is shorter), then the CLI version. A missing
    character is ``0``, not omitted, which is what makes a short prompt still stamp.
    """
    pinned = os.environ.get("KEYPROXY_CLAUDE_VERSION_HASH", "").strip()
    if pinned:
        return pinned
    chars = "".join(text[i] if i < len(text) else "0" for i in _ATTRIBUTION_INDEXES)
    return hashlib.sha256(f"{_ATTRIBUTION_SALT}{chars}{CLAUDE_CLI_VERSION}".encode()).hexdigest()[:3]


def _billing_header(user_text: str) -> str:
    return f"x-anthropic-billing-header: cc_version={CLAUDE_CLI_VERSION}.{attribution_hash(user_text)}; cc_entrypoint={CLAUDE_ENTRYPOINT};"


class ClaudeAuth:
    """``~/.claude/.credentials.json``: the Claude Code OAuth pair."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    def available(self) -> bool:
        if not self.path.is_file():
            return False
        try:
            return bool((self._load().get("claudeAiOauth") or {}).get("accessToken"))
        except (OSError, ValueError, SubscriptionError):
            return False

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SubscriptionError(f"claude login unreadable at {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise SubscriptionError("claude: not logged in (run `claude auth login` on the host)")
        return data

    async def token(self, client: httpx.AsyncClient) -> str:
        data = self._load()
        oauth = dict(data.get("claudeAiOauth") or {})
        access = str(oauth.get("accessToken") or "")
        if not access:
            raise SubscriptionError("claude: not logged in (run `claude auth login` on the host)")
        exp_ms = int(oauth.get("expiresAt") or 0)
        if exp_ms and exp_ms / 1000 - REFRESH_SKEW_SECONDS > time.time():
            return access
        async with self._lock:
            data = self._load()
            oauth = dict(data.get("claudeAiOauth") or {})
            access = str(oauth.get("accessToken") or "")
            exp_ms = int(oauth.get("expiresAt") or 0)
            if exp_ms and exp_ms / 1000 - REFRESH_SKEW_SECONDS > time.time():
                return access
            refresh = str(oauth.get("refreshToken") or "")
            if not refresh:
                raise SubscriptionError("claude: login expired (run `claude auth login` on the host)")
            response = await client.post(
                CLAUDE_TOKEN_URL,
                content=urlencode({"grant_type": "refresh_token", "refresh_token": refresh, "client_id": CLAUDE_CLIENT_ID}),
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    "accept": "application/json",
                    **_cli_headers(),
                },
            )
            if response.status_code != 200:
                raise SubscriptionError(f"claude token refresh failed: HTTP {response.status_code} {response.text[:160]}")
            fresh = response.json()
            oauth["accessToken"] = str(fresh.get("access_token") or access)
            if fresh.get("refresh_token"):
                oauth["refreshToken"] = str(fresh["refresh_token"])
            if fresh.get("expires_in"):
                oauth["expiresAt"] = int((time.time() + int(fresh["expires_in"])) * 1000)
            if fresh.get("refresh_token_expires_in"):
                oauth["refreshTokenExpiresAt"] = int((time.time() + int(fresh["refresh_token_expires_in"])) * 1000)
            data["claudeAiOauth"] = oauth
            _write_json(self.path, data)
            logger.warning("claude token refreshed")
            return str(oauth["accessToken"])

    async def headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        return {"authorization": f"Bearer {await self.token(client)}", **_cli_headers()}

    async def usage_headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        """Quota endpoints 429 when sent the full inference beta list; keep this to OAuth identity."""
        return {
            "authorization": f"Bearer {await self.token(client)}",
            "User-Agent": f"claude-cli/{CLAUDE_CLI_VERSION} (external, {CLAUDE_ENTRYPOINT})",
            "x-app": "cli",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "accept": "application/json",
        }


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
        return [{"type": "text", "text": content}]
    out: list[dict[str, Any]] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            out.append({"type": "text", "text": str(block.get("text") or "")})
        elif block.get("type") == "image_url":
            url = block.get("image_url", {}).get("url") if isinstance(block.get("image_url"), dict) else block.get("image_url")
            if isinstance(url, str) and url.startswith("data:") and "," in url:
                header, data = url.split(",", 1)
                media = header[5:].split(";")[0] or "image/png"
                out.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
            elif url:
                out.append({"type": "image", "source": {"type": "url", "url": str(url)}})
    return out or [{"type": "text", "text": ""}]


def _wire_tool_name(name: str) -> str:
    name = str(name or "")
    if not name or name.startswith(TOOL_PREFIX) or name.startswith("mcp__"):
        return name
    return TOOL_PREFIX + name


def _with_cache(block: dict[str, Any]) -> dict[str, Any]:
    return {**block, "cache_control": dict(CACHE_CONTROL)}


def _stampable(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict) and last.get("type") in _UNCACHABLE_BLOCKS:
            return False
        return True
    return bool(isinstance(content, str) and content)


def _stamp_last_block(message: dict[str, Any]) -> None:
    content = message.get("content")
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1] = _with_cache(content[-1])
    elif isinstance(content, str) and content:
        message["content"] = [_with_cache({"type": "text", "text": content})]


def _apply_prompt_cache(system: list[dict[str, Any]], tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> None:
    """Lock the stable prefix and the conversation tail (≤4 breakpoints).

    The last tool and the identity system block are byte-stable across turns.
    The harness prompt (system[2]) carries a clock, so it is *not* a breakpoint:
    a miss there would also drop the conversation cache. The last two stampable
    messages let a tool-call loop reuse the growing transcript.
    """
    if tools:
        tools[-1] = _with_cache(tools[-1])
    if len(system) >= 2:
        system[1] = _with_cache(system[1])
    elif system:
        system[-1] = _with_cache(system[-1])
    stampable = [message for message in messages if _stampable(message)]
    for message in stampable[-2:]:
        _stamp_last_block(message)


def _parse_args(raw: Any) -> Any:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except ValueError:
            return {"_raw": raw}
    return {}


def chat_to_messages(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """OpenAI chat-completions body → Anthropic ``/v1/messages`` plus wire-name → original-name."""
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            items.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in body.get("messages") or []:
        role = message.get("role")
        if role == "system":
            text = _text_of(message.get("content"))
            if text.strip():
                instructions.append(text)
        elif role == "user":
            flush_results()
            items.append({"role": "user", "content": _user_content(message.get("content"))})
        elif role == "assistant":
            flush_results()
            content: list[dict[str, Any]] = []
            text = _text_of(message.get("content"))
            if text:
                content.append({"type": "text", "text": text})
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                original = str(fn.get("name") or "")
                wire = _wire_tool_name(original)
                if original:
                    names[wire] = original
                content.append({"type": "tool_use", "id": str(call.get("id") or ""), "name": wire, "input": _parse_args(fn.get("arguments"))})
            if content:
                items.append({"role": "assistant", "content": content})
        elif role == "tool":
            pending_results.append({"type": "tool_result", "tool_use_id": str(message.get("tool_call_id") or ""), "content": _text_of(message.get("content"))})
    flush_results()
    if not items:
        items.append({"role": "user", "content": [{"type": "text", "text": ""}]})

    model = str(body.get("model") or "")
    max_tokens = min(int(body.get("max_tokens") or 8192), MAX_OUTPUT)
    effort = str(body.get("reasoning_effort") or "")
    if effort:
        budget = max(1024, EFFORT_BUDGET.get(effort, 8192))
        if max_tokens <= budget:
            max_tokens = min(MAX_OUTPUT, budget + 1024)
    first_user = next((_text_of(item.get("content")) for item in items if item.get("role") == "user"), "")
    system: list[dict[str, Any]] = [
        {"type": "text", "text": _billing_header(first_user)},
        {"type": "text", "text": CLAUDE_IDENTITY},
    ]
    if instructions:
        system.append({"type": "text", "text": "\n\n".join(instructions)})
    tools: list[dict[str, Any]] = []
    for tool in body.get("tools") or []:
        fn = tool.get("function") or {}
        original = str(fn.get("name") or tool.get("name") or "")
        if not original:
            continue
        wire = _wire_tool_name(original)
        names[wire] = original
        tools.append({"name": wire, "description": fn.get("description") or tool.get("description") or "", "input_schema": fn.get("parameters") or tool.get("input_schema") or {"type": "object", "properties": {}}})
    _apply_prompt_cache(system, tools, items)
    out: dict[str, Any] = {
        "model": model,
        "max_tokens": max(1, max_tokens),
        "stream": True,
        "system": system,
        "messages": items,
    }
    if tools:
        out["tools"] = tools
    choice = body.get("tool_choice")
    forced = isinstance(choice, dict) and (choice.get("function") or {}).get("name")
    if forced:
        out["tool_choice"] = {"type": "tool", "name": _wire_tool_name(choice["function"]["name"])}
    elif isinstance(choice, str) and choice in ("auto", "none", "any", "required"):
        out["tool_choice"] = {"type": "any"} if choice == "required" else {"type": choice}
    if effort and not forced:
        budget = max(1024, EFFORT_BUDGET.get(effort, 8192))
        # Omitted display keeps time-to-first-token low but makes the model think in silence for a
        # minute or more; messages_events_to_chunks forwards upstream liveness so the caller's idle
        # watchdog does not read that silence as a dead stream.
        out["thinking"] = {"type": "enabled", "budget_tokens": min(budget, max_tokens - 1), "display": "omitted"}
        if any(model.startswith(prefix) for prefix in EFFORT_MODELS) and effort in EFFORT_BUDGET:
            out["output_config"] = {"effort": effort}
    return out, names


def _chunk(completion_id: str, model: str, delta: dict[str, Any], finish: str | None = None, usage: dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {"id": completion_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _usage(input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> dict[str, Any]:
    """OpenAI-shaped usage. ``prompt_tokens`` is the whole prompt; Anthropic's ``input_tokens`` is sometimes only the uncached tail."""
    cached = cache_read + cache_write
    prompt = input_tokens if input_tokens >= cached else input_tokens + cached
    return {
        "prompt_tokens": prompt,
        "completion_tokens": output_tokens,
        "total_tokens": prompt + output_tokens,
        "prompt_tokens_details": {"cached_tokens": cache_read},
        "completion_tokens_details": {"reasoning_tokens": 0},
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
    }


async def messages_events_to_chunks(lines: AsyncIterator[str], *, model: str, names: dict[str, str] | None = None) -> AsyncIterator[str]:
    """Anthropic Messages SSE → chat-completion chunks.

    An upstream event that translates to no chunk (ping, the thinking block's start, its signature,
    block stops) becomes a keepalive comment instead, so the caller still sees the stream is alive
    while the model thinks silently. It follows real upstream events only, never a timer: a hung
    upstream stays silent and is still caught by the caller's idle watchdog.
    """
    names = names or {}
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    calls: dict[int, int] = {}
    finish = "stop"
    input_tokens = output_tokens = cache_read = cache_write = 0
    failed: str | None = None
    event_name = ""
    async for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        kind = event.get("type") or event_name
        spoke = False
        if kind == "error":
            err = event.get("error") or event
            failed = str(err.get("message") or err or "the backend reported a failure")
        elif kind == "message_start":
            usage = (event.get("message") or {}).get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                index = int(event.get("index") or len(calls))
                calls[index] = index
                original = names.get(str(block.get("name") or ""), str(block.get("name") or ""))
                finish = "tool_calls"
                spoke = True
                yield _chunk(completion_id, model, {"tool_calls": [{"index": index, "id": str(block.get("id") or ""), "type": "function", "function": {"name": original, "arguments": ""}}]})
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta" and delta.get("text"):
                spoke = True
                yield _chunk(completion_id, model, {"role": "assistant", "content": delta["text"]})
            elif dtype == "thinking_delta" and delta.get("thinking"):
                spoke = True
                yield _chunk(completion_id, model, {"reasoning_content": delta["thinking"]})
            elif dtype == "input_json_delta" and delta.get("partial_json"):
                index = int(event.get("index") or (len(calls) - 1 if calls else 0))
                spoke = True
                yield _chunk(completion_id, model, {"tool_calls": [{"index": index, "function": {"arguments": delta["partial_json"]}}]})
        elif kind == "message_delta":
            usage = event.get("usage") or {}
            if usage.get("output_tokens") is not None:
                output_tokens = int(usage.get("output_tokens") or 0)
            if usage.get("input_tokens") is not None:
                input_tokens = int(usage.get("input_tokens") or input_tokens)
            reason = (event.get("delta") or {}).get("stop_reason")
            if reason == "max_tokens":
                finish = "length"
            elif reason == "tool_use":
                finish = "tool_calls"
            elif reason in ("end_turn", "stop_sequence"):
                finish = "stop"
        elif kind in ("error",) or event_name == "error":
            err = event.get("error") or event
            failed = str(err.get("message") or err or "the backend reported a failure")
        if not spoke:
            yield KEEPALIVE
    if failed:
        yield f"data: {json.dumps({'error': {'message': failed, 'type': 'upstream_error'}})}\n\n"
    else:
        yield _chunk(completion_id, model, {}, finish=finish, usage=_usage(input_tokens, output_tokens, cache_read, cache_write))
    yield "data: [DONE]\n\n"


def _iso_to_unix(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return value


def claude_usage_view(data: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    windows = []
    for name, key in (("5h", "five_hour"), ("weekly", "seven_day")):
        w = data.get(key) or {}
        if isinstance(w, dict) and w:
            windows.append({"name": name, "used_percent": float(w.get("utilization") or 0), "resets_at": _iso_to_unix(w.get("resets_at"))})
    for limit in data.get("limits") or []:
        if not isinstance(limit, dict) or limit.get("kind") == "session" or limit.get("kind") == "weekly_all":
            continue
        scope = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
        if scope:
            windows.append({"name": str(scope), "used_percent": float(limit.get("percent") or 0), "resets_at": _iso_to_unix(limit.get("resets_at"))})
    org = (profile or {}).get("organization") or {}
    extra = data.get("extra_usage") or {}
    reached = any(float(w.get("used_percent") or 0) >= 100 for w in windows)
    return {
        "provider": "claude",
        "plan": org.get("rate_limit_tier") or org.get("organization_type") or "claude",
        "limit_reached": reached,
        "windows": windows,
        "extra_usage": bool(extra.get("is_enabled")),
    }


__all__ = [
    "CLAUDE_API",
    "CLAUDE_FALLBACK_MODELS",
    "ClaudeAuth",
    "attribution_hash",
    "chat_to_messages",
    "claude_usage_view",
    "messages_events_to_chunks",
]
