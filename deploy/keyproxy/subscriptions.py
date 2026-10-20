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
from collections.abc import AsyncIterator, Callable
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


REASONING_FORMAT = "openai-responses-v1"
"""The ``format`` of a ``reasoning_details`` entry that holds one Responses reasoning item.

The name and the entry's shape are OpenRouter's convention for carrying a provider's reasoning
through a chat completion and back; the bot keeps the entries on the assistant message and sends
them again unchanged, so the proxy is the only party that reads inside one."""


def reasoning_detail(item: dict[str, Any], index: int) -> dict[str, Any] | None:
    """One finished Responses reasoning item → the ``reasoning_details`` entry the chat stream carries.

    Only an item with its encrypted copy is worth keeping. With ``store`` off the backend keeps
    nothing, so a reasoning item sent back without ``encrypted_content`` names reasoning that no
    longer exists and the request is refused.
    """
    encrypted = item.get("encrypted_content")
    if not isinstance(encrypted, str) or not encrypted:
        return None
    detail: dict[str, Any] = {"type": "reasoning.encrypted", "format": REASONING_FORMAT, "index": index, "data": encrypted, "summary": item.get("summary") or []}
    if item.get("id"):
        detail["id"] = str(item["id"])
    if item.get("content"):
        detail["content"] = item["content"]
    return detail


def reasoning_items_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    """The Responses reasoning items an assistant message's ``reasoning_details`` replay, in order.

    Sent without the item ``id``, as the Codex CLI sends them: with ``store`` off an id points at
    nothing the backend kept, and the encrypted copy is the whole of the item.
    """
    out: list[dict[str, Any]] = []
    for detail in message.get("reasoning_details") or []:
        if not isinstance(detail, dict) or detail.get("format") != REASONING_FORMAT or detail.get("type") != "reasoning.encrypted":
            continue
        if not isinstance(detail.get("data"), str) or not detail["data"]:
            continue
        item: dict[str, Any] = {"type": "reasoning", "summary": detail.get("summary") or [], "encrypted_content": detail["data"]}
        if detail.get("content"):
            item["content"] = detail["content"]
        out.append(item)
    return out


def without_reasoning(sent: dict[str, Any]) -> dict[str, Any]:
    """The same Responses body with every replayed reasoning item left out.

    For the one retry after the backend refuses the encrypted reasoning it was handed (a copy made
    for another model, or one it can no longer read): the model loses that reasoning, which is what
    happened on every request before it was replayed, instead of the run failing.
    """
    return {**sent, "input": [item for item in sent.get("input") or [] if item.get("type") != "reasoning"]}


def refused_reasoning(status: int, text: str) -> bool:
    """Whether a refusal is about the replayed reasoning rather than anything else in the request."""
    lowered = text.lower()
    return status == 400 and ("encrypted" in lowered or "reasoning" in lowered)


def chat_to_responses(body: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI chat-completions body → the Responses body the Codex backend takes.

    With ``store`` off the backend forgets a response as soon as it has streamed it, so the model's
    reasoning survives to the next request only if the client asks for the encrypted copy and hands
    it back. Without that a reasoning model started every tool step from nothing — the Codex CLI
    replays it on every request, and this does the same: ``include`` asks for it, and the reasoning
    items an assistant turn carries in ``reasoning_details`` go back in front of that turn's output,
    in the order the backend produced them.

    The system prompt travels as a ``developer`` message at the head of ``input``, not in the
    top-level ``instructions`` field: the CLI stopped sending that field (0.162) and the backend is
    free to refuse it once the older CLIs are gone. The message gets the CLI's stable id, derived from
    the conversation and the text, so the same prompt names the same item on every request.
    """
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in body.get("messages") or []:
        role = message.get("role")
        if role == "system":
            instructions.append(_text_of(message.get("content")))
        elif role == "user":
            items.append({"type": "message", "role": "user", "content": _user_content(message.get("content"))})
        elif role == "assistant":
            items.extend(reasoning_items_of(message))
            text = _text_of(message.get("content"))
            if text:
                items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                items.append({"type": "function_call", "call_id": str(call.get("id") or ""), "name": str(fn.get("name") or ""), "arguments": fn.get("arguments") if isinstance(fn.get("arguments"), str) else json.dumps(fn.get("arguments") or {})})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": str(message.get("tool_call_id") or ""), "output": _text_of(message.get("content"))})
    cache_key = body.get("prompt_cache_key")
    base = "\n\n".join(i for i in instructions if i.strip())
    if base:
        thread = uuid.uuid5(uuid.NAMESPACE_OID, cache_key if isinstance(cache_key, str) else "")
        items.insert(0, {"type": "message", "id": f"msg_{uuid.uuid5(thread, base)}", "role": "developer", "content": [{"type": "input_text", "text": base}]})
    out: dict[str, Any] = {
        "model": str(body.get("model") or ""),
        "input": items,
        "store": False,
        "stream": True,
        "parallel_tool_calls": True,
        "include": ["reasoning.encrypted_content"],
    }
    if isinstance(cache_key, str) and cache_key:
        # The backend routes requests that share a key to the same cache; the CLI sends its
        # conversation id here, and without one most requests of a long session missed the cache.
        out["prompt_cache_key"] = cache_key
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


STREAM_TRUNCATED = "stream_truncated"
"""The error ``code`` of a stream whose upstream ended before its final event.

The bot reads it as a dropped connection and asks again. Before it the proxy wrote a normal finish and
``[DONE]`` after any end of the upstream body, so a cut answer read as a complete one and a half-written
tool call ran with its braces repaired."""

_RETRY_IN = re.compile(r"try again in\s*(\d+(?:\.\d+)?)\s*(ms|s|seconds?)\b", re.IGNORECASE)


def retry_after_seconds(error: dict[str, Any], *, now: float | None = None) -> float | None:
    """The wait a refusal states, in seconds from now; ``None`` when it states none.

    Read the way the Codex CLI reads a failed response: a ``retry-after`` among the headers the error
    carries, then the reset time of a spent window, then the "try again in 11.054s" its rate-limit
    message spells out. Passed on so a rate limit is retried after the wait it named rather than on
    the bot's own short backoff, which only collected more refusals.
    """
    headers = error.get("headers") if isinstance(error.get("headers"), dict) else {}
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                break
            if seconds > 0:
                return seconds
    resets = error.get("resets_at")
    if isinstance(resets, (int, float)) and not isinstance(resets, bool):
        left = float(resets) - (time.time() if now is None else now)
        if left > 0:
            return left
    found = _RETRY_IN.search(str(error.get("message") or ""))
    if found:
        value = float(found.group(1))
        return value / 1000 if found.group(2).lower() == "ms" else value
    return None


def failure_chunk(error: Any, fallback: str) -> str:
    """An upstream failure as the one error chunk the bot classifies: its ``type``, ``code``, message and wait.

    The code is what tells a spent quota from a rate limit from an overloaded server. Only the message
    used to travel, so the bot filed every failure as a server error and retried a spent quota."""
    error = error if isinstance(error, dict) else {"message": str(error or fallback)}
    out: dict[str, Any] = {"message": str(error.get("message") or fallback), "type": str(error.get("type") or "upstream_error")}
    if isinstance(error.get("code"), (str, int)) and not isinstance(error.get("code"), bool) and str(error["code"]):
        out["code"] = error["code"]
    wait = retry_after_seconds(error)
    if wait is not None:
        out["retry_after_seconds"] = round(wait, 3)
    return f"data: {json.dumps({'error': out}, ensure_ascii=False)}\n\n"


def truncated_chunk(upstream: str) -> str:
    return f"data: {json.dumps({'error': {'message': f'the {upstream} stream ended before its final event', 'type': STREAM_TRUNCATED, 'code': STREAM_TRUNCATED}})}\n\n"


def _incomplete_finish(response: dict[str, Any]) -> str:
    """The chat finish for a response the backend stopped short: a filtered answer, otherwise a cut one."""
    reason = (response.get("incomplete_details") or {}).get("reason")
    return "content_filter" if reason == "content_filter" else "length"


async def responses_events_to_chunks(lines: AsyncIterator[str], *, model: str) -> AsyncIterator[str]:
    """Responses SSE lines → chat-completion chunks (text, reasoning summaries, tool calls, usage, finish).

    A finished reasoning item with its encrypted copy becomes one ``reasoning_details`` entry
    (:func:`reasoning_detail`), which the caller keeps on the assistant turn and sends back.

    Events with nothing to translate (created, in_progress, reasoning item boundaries) become a
    keepalive comment, so a long silent reasoning phase is not mistaken for a dead stream. These
    follow real upstream events, never a timer.

    The answer is finished only by a terminal event. ``response.incomplete`` (or a call item that
    ended ``incomplete``) finishes it as ``length``, so the bot never runs the cut call; a body that
    ends with no terminal event at all becomes a :data:`STREAM_TRUNCATED` error and no ``[DONE]``.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    calls: dict[str, int] = {}
    arguments: dict[int, str] = {}
    reasoning_items = 0
    finish = "stop"
    usage: dict[str, Any] | None = None
    failed: Any = None
    terminal = False
    cut_call = False
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
            arguments[index] = arguments.get(index, "") + event["delta"]
            spoke = True
            yield _chunk(completion_id, model, {"tool_calls": [{"index": index, "function": {"arguments": event["delta"]}}]})
        elif kind == "response.output_item.done" and (event.get("item") or {}).get("type") == "reasoning":
            # The finished reasoning item, encrypted: handed to the caller whole, to be sent back.
            detail = reasoning_detail(event.get("item") or {}, reasoning_items)
            if detail is not None:
                reasoning_items += 1
                spoke = True
                yield _chunk(completion_id, model, {"reasoning_details": [detail]})
        elif kind in ("response.function_call_arguments.done", "response.output_item.done"):
            item = event.get("item") or {}
            if kind == "response.output_item.done" and item.get("type") == "function_call" and item.get("status") == "incomplete":
                # The backend stopped this call part-way (the output budget, a filter): its arguments
                # are not the ones the model meant, whatever the response says at the end.
                cut_call = True
            if kind == "response.function_call_arguments.done" or item.get("type") == "function_call":
                index = calls.get(str(event.get("item_id") or item.get("id")))
                final = event.get("arguments") if kind == "response.function_call_arguments.done" else item.get("arguments")
                if index is not None and isinstance(final, str):
                    current = arguments.get(index, "")
                    if final.startswith(current) and len(final) > len(current):
                        # Some Codex streams put the complete arguments only on the final item.
                        # Preserve streamed deltas, then fill in the missing suffix exactly once.
                        missing = final[len(current):]
                        arguments[index] = final
                        if final != "{}":
                            logger.warning("Codex function call %d omitted %d streamed argument characters; recovered from final item", index, len(missing))
                        spoke = True
                        yield _chunk(completion_id, model, {"tool_calls": [{"index": index, "function": {"arguments": missing}}]})
        elif kind in ("response.completed", "response.incomplete"):
            terminal = True
            response = event.get("response") or {}
            usage = responses_usage(response.get("usage") or {})
            if kind == "response.incomplete" or response.get("status") == "incomplete":
                finish = _incomplete_finish(response)
        elif kind in ("response.failed", "error"):
            terminal = True
            # A bare ``error`` event carries its code and message at the top level, not under ``error``.
            failed = (event.get("response") or {}).get("error") or event.get("error") or {k: event[k] for k in ("code", "message") if k in event}
        if not spoke:
            yield KEEPALIVE
    if failed is not None:
        yield failure_chunk(failed, "the backend reported a failure")
        return
    if not terminal:
        yield truncated_chunk("Codex")
        return
    if cut_call and finish not in ("length", "content_filter"):
        finish = "length"
    yield _chunk(completion_id, model, {}, finish=finish, usage=usage)
    yield "data: [DONE]\n\n"


async def collect_completion(chunks: AsyncIterator[str], *, model: str) -> dict[str, Any]:
    """The non-streaming shape, assembled from the streamed chunks."""
    text_parts: list[str] = []
    reasoning: list[str] = []
    details: list[dict[str, Any]] = []
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
            details.extend(delta.get("reasoning_details") or [])
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
    if details:
        message["reasoning_details"] = details
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
    "REASONING_FORMAT",
    "reasoning_detail",
    "reasoning_items_of",
    "refused_reasoning",
    "responses_events_to_chunks",
    "retry_after_seconds",
    "STREAM_TRUNCATED",
    "failure_chunk",
    "truncated_chunk",
    "without_reasoning",
]


OPENCODE_GO_BASE = "https://opencode.ai/zen/go/v1"


class OpencodeAuth:
    """OpenCode Go: a plain API key, no login to refresh.

    ``lookup`` reads the key at each call. The proxy passes one that sees its keys file, because a key
    saved from the app lands there and never in this process's environment: read from the environment,
    the allowance stayed "not connected" until a restart.
    """

    def __init__(self, env_var: str = "OPENCODE_API_KEY", lookup: Callable[[str], str] | None = None) -> None:
        self.env_var = env_var
        self.lookup = lookup or (lambda var: os.environ.get(var, ""))

    def available(self) -> bool:
        return bool(self.lookup(self.env_var))

    async def headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        return {"authorization": f"Bearer {self.lookup(self.env_var)}"}


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

