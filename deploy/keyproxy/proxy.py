"""Key proxy: the only process that holds provider API keys.

The agent container talks to ``http://keyproxy:3200/<upstream>/...`` with no credentials;
this service injects the real ``Authorization`` header for each upstream from its own
environment and forwards the request, streaming the response back (SSE included). When
the daily-budget flag exists on the shared state volume, calls to model upstreams are
refused here — below the agent, where a self-modification cannot reach.

Upstreams: ``deepseek`` → https://api.deepseek.com, ``openrouter`` → https://openrouter.ai/api/v1,
``openai`` → https://api.openai.com/v1, ``opencode`` → https://opencode.ai/zen/go/v1 (OpenCode Go). Keys:
``DEEPSEEK_API_KEY``, ``OPENROUTER_API_KEY``, ``OPENAI_API_KEY``, ``OPENCODE_API_KEY``. Extra upstreams: ``KEYPROXY_UPSTREAM_<NAME>=https://host/base`` with
``KEYPROXY_KEY_<NAME>=…`` and, for an API that does not take ``Authorization: Bearer``,
``KEYPROXY_AUTH_<NAME>=<header name>`` (``X-API-KEY`` for Serper, ``x-api-key`` for Exa…);
the key is sent as that header's value.

The budget is checked in two independent ways: the supervisor's flag file, and — when
``KEYPROXY_USD_PER_DAY`` is set — the proxy's own read of today's spend from the read-only
database, which nothing in the agent container can unlink.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from aiohttp import web
from claude import (
    CLAUDE_API,
    CLAUDE_FALLBACK_MODELS,
    ClaudeAuth,
    chat_to_messages,
    claude_usage_view,
    messages_events_to_chunks,
)
from subscriptions import (
    CODEX_BASE,
    CODEX_FALLBACK_MODELS,
    GROK_BASE,
    OPENCODE_GO_BASE,
    CodexAuth,
    GrokAuth,
    OpencodeAuth,
    SubscriptionError,
    chat_to_responses,
    codex_usage_view,
    collect_completion,
    grok_usage_view,
    opencode_usage_view,
    responses_events_to_chunks,
)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s keyproxy %(levelname)s: %(message)s")
logger = logging.getLogger("keyproxy")

DEFAULT_UPSTREAMS = {
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "opencode": ("https://opencode.ai/zen/go/v1", "OPENCODE_API_KEY"),
}
BUDGET_FLAG = Path(os.environ.get("KEYPROXY_BUDGET_FLAG", "/srv/state/BUDGET_EXCEEDED"))
BUDGET_DB = Path(os.environ.get("KEYPROXY_BUDGET_DB", "/srv/state/daedalus.sqlite"))
BUDGET_USD_PER_DAY = float(os.environ.get("KEYPROXY_USD_PER_DAY", "0") or 0)
BUDGET_CACHE_SECONDS = 30.0
BUDGET_EXEMPT_TAILS = (("user", "balance"), ("credits",), ("models",))
"""Reading a balance or the model list spends nothing; only completions are refused over budget.
Matched on path segments (a leading ``v1`` ignored), so a ``…/deepseek/v1`` base_url behaves the same."""
HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "keep-alive", "authorization", "x-api-key", "api-key", "x-goog-api-key", "cookie", "proxy-authorization"}
DROP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding"}
"""The body is streamed decoded, so the upstream's framing and encoding headers no longer describe it."""
_spend_cache: tuple[float, float] = (0.0, 0.0)
_token_cache: tuple[float, str] = (0.0, "")
METERED_HEADER = "x-daedalus-metered"
"""The bot marks its own calls: it records their usage itself. A call without the mark (a shell's curl) is metered here."""
AGENT_API = os.environ.get("KEYPROXY_AGENT_API", "").rstrip("/")
COMPLETION_TAILS = (("chat", "completions"), ("completions",), ("responses",), ("messages",))
CAPTURE_LIMIT = 8 * 1024 * 1024
CODEX_AUTH = CodexAuth(Path(os.environ.get("KEYPROXY_CODEX_AUTH", os.path.expanduser("~/.codex/auth.json"))))
GROK_AUTH = GrokAuth(Path(os.environ.get("KEYPROXY_GROK_AUTH", os.path.expanduser("~/.grok/auth.json"))))
OPENCODE_AUTH = OpencodeAuth()
CLAUDE_AUTH = ClaudeAuth(Path(os.environ.get("KEYPROXY_CLAUDE_AUTH", os.path.expanduser("~/.claude/.credentials.json"))))
"""The operator's subscriptions: served as ``/codex/v1``, ``/grok/v1`` and ``/claude/v1`` with the CLIs' own logins."""
_usage_cache: dict[str, tuple[float, dict[str, Any]]] = {}
USAGE_CACHE_SECONDS = 60.0
_claude_usage_retry_at = 0.0
CLAUDE_USAGE_BACKOFF_SECONDS = 120.0


BEARER = "bearer"


def upstreams() -> dict[str, tuple[str, str]]:
    """name → (base_url, api_key) from the environment."""
    out: dict[str, tuple[str, str]] = {}
    for name, (base, env_key) in DEFAULT_UPSTREAMS.items():
        key = os.environ.get(env_key, "")
        if key:
            out[name] = (base, key)
    for var, value in os.environ.items():
        if var.startswith("KEYPROXY_UPSTREAM_") and value:
            name = var[len("KEYPROXY_UPSTREAM_"):].lower()
            out[name] = (value.rstrip("/"), os.environ.get(f"KEYPROXY_KEY_{name.upper()}", ""))
    return out


CLI_LOGINS = ("codex", "grok", "claude")
"""Upstreams whose credential is a CLI's own login on this machine, not a key in the environment."""
API_KEY, CLI_LOGIN, ENDPOINT = "api_key", "cli_login", "endpoint"
"""What an upstream's credential is: a key the proxy holds, a CLI login it reads, or an address that needs neither."""


def credential_readable(auth: Any) -> bool:
    """Whether a CLI's login file is there AND can be read: an unreadable one is not a login.

    ``available()`` on the auth objects answers the first half only, and a credentials file left
    behind by another user — a very ordinary thing in a container that mounts the operator's home —
    reads as logged in right up to the first call, which then fails with something about a token.
    """
    if not auth.available():
        return False
    path = getattr(auth, "path", None)
    if path is None:
        return True  # an env-var login: available() already read what there is to read
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def key_status() -> dict[str, dict[str, Any]]:
    """Per upstream: whether a credential is really there, and what kind it is. Never the credential.

    This is what the bot asks before it tells the operator an endpoint is ready to use. The
    difference from :func:`upstreams` is that the answer names every upstream the proxy can serve,
    including the ones it has no key for — "no key" is the fact worth reporting.
    """
    out: dict[str, dict[str, Any]] = {}
    for name, (_base, env_key) in DEFAULT_UPSTREAMS.items():
        out[name] = {"configured": bool(os.environ.get(env_key, "")), "kind": API_KEY}
    for var, value in os.environ.items():
        if var.startswith("KEYPROXY_UPSTREAM_") and value:
            name = var[len("KEYPROXY_UPSTREAM_"):].lower()
            key = os.environ.get(f"KEYPROXY_KEY_{name.upper()}", "")
            # An extra upstream is an address the operator wrote down; with no key beside it, it is
            # an endpoint that needs none rather than one whose key went missing.
            out[name] = {"configured": True, "kind": API_KEY if key else ENDPOINT}
    for name, auth in (("codex", CODEX_AUTH), ("grok", GROK_AUTH), ("claude", CLAUDE_AUTH)):
        out[name] = {"configured": credential_readable(auth), "kind": CLI_LOGIN}
    return out


def agent_call(request: web.Request) -> bool:
    """Whether this request carries the bot's own API token, which only the bot can read from its database.

    Compared as bytes: headers arrive decoded as latin-1, so a caller can put a non-ASCII character
    in one, and ``compare_digest`` on two ``str`` refuses to compare those at all — a refusal that
    came back as a 500 with a traceback instead of the 403 this is.
    """
    token = agent_api_token()
    offered = request.headers.get("x-daedalus-token", "")
    if not token or not offered:
        return False
    return secrets.compare_digest(offered.encode("utf-8", "surrogateescape"), token.encode("utf-8", "surrogateescape"))


async def handle_keys(request: web.Request) -> web.Response:
    """What the proxy holds a credential for, per upstream — for the bot alone, and without any key in it."""
    if not agent_call(request):
        return web.json_response({"error": "this endpoint answers the agent only"}, status=403)
    return web.json_response({"upstreams": key_status()})


def auth_scheme(name: str) -> str:
    """How an upstream takes its key: ``bearer`` (``Authorization: Bearer <key>``) or the name of a header that carries the bare key."""
    scheme = os.environ.get(f"KEYPROXY_AUTH_{name.upper()}", "").strip()
    return scheme if scheme and scheme.lower() != BEARER else BEARER


def inject_key(headers: dict[str, str], name: str, key: str) -> None:
    scheme = auth_scheme(name)
    if scheme == BEARER:
        headers["authorization"] = f"Bearer {key}"
    else:
        headers[scheme] = key


def spent_today() -> float:
    """Today's priced spend from the agent's database (read-only), cached briefly."""
    global _spend_cache
    at, value = _spend_cache
    if time.monotonic() - at < BUDGET_CACHE_SECONDS:
        return value
    value = 0.0
    if BUDGET_DB.exists():
        try:
            conn = sqlite3.connect(f"file:{BUDGET_DB}?mode=ro", uri=True, timeout=2.0)
            try:
                row = conn.execute("SELECT sum(cost_usd) FROM usage_events WHERE at >= ?", (datetime.now(UTC).strftime("%Y-%m-%d"),)).fetchone()
            finally:
                conn.close()
            value = float(row[0] or 0.0)
        except sqlite3.Error as exc:
            logger.warning("spend query failed: %s", exc)
    _spend_cache = (time.monotonic(), value)
    return value


def agent_api_token() -> str:
    """The bot's API token, read from its database (read-only) so a metered call can be reported back."""
    global _token_cache
    at, value = _token_cache
    if value and time.monotonic() - at < 300:
        return value
    if BUDGET_DB.exists():
        try:
            conn = sqlite3.connect(f"file:{BUDGET_DB}?mode=ro", uri=True, timeout=2.0)
            try:
                row = conn.execute("SELECT value FROM kv WHERE key = 'api_token'").fetchone()
            finally:
                conn.close()
            value = str(json.loads(row[0])) if row else ""
        except (sqlite3.Error, ValueError) as exc:
            logger.warning("api token read failed: %s", exc)
    _token_cache = (time.monotonic(), value)
    return value


def is_completion(rest: str) -> bool:
    segments = tuple(s for s in rest.strip("/").split("/") if s)
    if segments and segments[0] == "v1":
        segments = segments[1:]
    return any(segments[-len(tail):] == tail for tail in COMPLETION_TAILS if len(segments) >= len(tail))


def parse_usage(body: bytes, content_type: str) -> tuple[str, dict[str, int]] | None:
    """``(model, tokens)`` from a completion response, streamed or not; None when it carries no usage."""
    candidates: list[dict[str, Any]] = []
    if "text/event-stream" in content_type:
        for line in body.decode("utf-8", "replace").splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                candidates.append(json.loads(payload))
            except ValueError:
                continue
    else:
        try:
            candidates.append(json.loads(body))
        except ValueError:
            return None
    model = ""
    usage: dict[str, Any] | None = None
    for item in candidates:
        if not isinstance(item, dict):
            continue
        inner = item.get("response") if isinstance(item.get("response"), dict) else item  # Responses API wraps the final object
        model = inner.get("model") or model
        if isinstance(inner.get("usage"), dict):
            usage = inner["usage"]
    if usage is None:
        return None
    details_in = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    details_out = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    tokens = {
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("prompt_cache_hit_tokens") or usage.get("cache_read_input_tokens") or (details_in.get("cached_tokens") if isinstance(details_in, dict) else 0) or 0),
        "reasoning_tokens": int((details_out.get("reasoning_tokens") if isinstance(details_out, dict) else 0) or 0),
    }
    return model, tokens


async def report_direct_usage(client: httpx.AsyncClient, provider_id: str, request_model: str, body: bytes, content_type: str, started: float) -> None:
    """Tell the bot about a metered call so its spend counters and caps see it."""
    parsed = parse_usage(body, content_type)
    if parsed is None:
        logger.warning("direct call to %s carried no usage; not recorded", provider_id)
        return
    model, tokens = parsed
    token = agent_api_token()
    if not token:
        logger.warning("direct call to %s not recorded: no api token", provider_id)
        return
    payload = {"provider_id": provider_id, "model": model or request_model, **tokens, "duration_ms": int((time.monotonic() - started) * 1000)}
    try:
        response = await client.post(f"{AGENT_API}/api/usage/direct", json=payload, headers={"x-daedalus-token": token}, timeout=10.0)
        if response.status_code >= 300:
            logger.warning("direct usage not recorded: HTTP %s %s", response.status_code, response.text[:200])
    except httpx.HTTPError as exc:
        logger.warning("direct usage not recorded: %s", type(exc).__name__)


def budget_exceeded() -> bool:
    if BUDGET_FLAG.exists():
        return True
    return BUDGET_USD_PER_DAY > 0 and spent_today() > BUDGET_USD_PER_DAY


def budget_exempt(rest: str) -> bool:
    segments = tuple(s for s in rest.split("/") if s)
    if segments and segments[0] == "v1":
        segments = segments[1:]
    return any(segments[-len(tail):] == tail for tail in BUDGET_EXEMPT_TAILS if len(segments) >= len(tail))


def target_url(base: str, rest: str, query: str) -> str:
    return base.rstrip("/") + "/" + rest.lstrip("/") + (f"?{query}" if query else "")


def subscriptions_status() -> dict[str, Any]:
    return {"codex": {"logged_in": CODEX_AUTH.available()}, "grok": {"logged_in": GROK_AUTH.available()}, "claude": {"logged_in": CLAUDE_AUTH.available()}}


async def _sse_lines(response: httpx.Response) -> Any:
    async for line in response.aiter_lines():
        yield line


def _stamp_grok_model(headers: dict[str, str], body: bytes) -> None:
    """Copy the JSON model onto ``x-grok-model-override`` when the caller did not set one."""
    if any(key.lower() == "x-grok-model-override" for key in headers):
        return
    if not body:
        return
    try:
        model = str(json.loads(body).get("model") or "")
    except (ValueError, AttributeError, UnicodeError):
        return
    if model:
        headers["x-grok-model-override"] = model


def served_headers(upstream: str, model: str) -> dict[str, str]:
    """Which upstream, and which model name, this response was really produced against.

    The proxy routes by the name in the path and never substitutes one upstream or model for
    another — but the two subscription upstreams do not return an OpenAI-shaped body, so their
    chunks are rebuilt here with the *requested* model written into them. Read back by a caller,
    that field can only ever say what the caller already asked for. These headers say what the
    proxy actually sent, so the difference is visible if one ever appears.
    """
    return {"x-keyproxy-upstream": upstream, **({"x-keyproxy-model": model} if model else {})}


async def handle_claude(request: web.Request, rest: str) -> web.StreamResponse:
    """Anthropic Messages behind the Claude Code login, presented as chat completions."""
    client: httpx.AsyncClient = request.app["client"]
    tail = rest.strip("/").removeprefix("v1/")
    try:
        headers = await CLAUDE_AUTH.headers(client)
    except SubscriptionError as exc:
        return web.json_response({"error": {"message": str(exc), "type": "subscription_error"}}, status=502)
    if tail == "models":
        models = list(CLAUDE_FALLBACK_MODELS)
        try:
            response = await client.get(CLAUDE_API + "/v1/models", headers={**headers, "accept": "application/json"})
            if response.status_code == 200:
                models = sorted({str(m.get("id") or "") for m in (response.json().get("data") or []) if isinstance(m, dict) and m.get("id")} | set(models))
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
        return web.json_response({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "anthropic"} for m in models if m]})
    if tail != "chat/completions":
        return web.json_response({"error": {"message": f"claude serves chat/completions and models, not {tail!r}", "type": "invalid_request_error"}}, status=404)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": {"message": "body must be JSON", "type": "invalid_request_error"}}, status=400)
    model = str(body.get("model") or "")
    stream = bool(body.get("stream", False))
    payload, names = chat_to_messages(body)
    upstream = client.build_request("POST", CLAUDE_API + "/v1/messages?beta=true", json=payload, headers={**headers, "accept": "text/event-stream", "content-type": "application/json"})
    try:
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        return web.json_response({"error": {"message": f"upstream unreachable: {type(exc).__name__}", "type": "proxy_error"}}, status=502)
    if response.status_code >= 400:
        raw = (await response.aread()).decode("utf-8", "replace")
        await response.aclose()
        try:
            payload_err = json.loads(raw)
        except ValueError:
            payload_err = {"error": {"message": raw[:400], "type": "upstream_error"}}
        return web.json_response(payload_err, status=response.status_code)
    chunks = messages_events_to_chunks(_sse_lines(response), model=model, names=names)
    served = served_headers("claude", str(payload.get("model") or model))
    try:
        if not stream:
            return web.json_response(await collect_completion(chunks, model=model), headers=served)
        out = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", **served})
        await out.prepare(request)
        async for chunk in chunks:
            await out.write(chunk.encode("utf-8"))
        await out.write_eof()
        return out
    except (ConnectionResetError, asyncio.CancelledError):
        return web.Response(status=499)
    finally:
        await response.aclose()


async def handle_codex(request: web.Request, rest: str) -> web.StreamResponse:
    """The ChatGPT backend speaks only the Responses API: translate chat completions both ways."""
    client: httpx.AsyncClient = request.app["client"]
    tail = rest.strip("/").removeprefix("v1/")
    try:
        headers = await CODEX_AUTH.headers(client)
    except SubscriptionError as exc:
        return web.json_response({"error": {"message": str(exc), "type": "subscription_error"}}, status=502)
    if tail == "models":
        models = list(CODEX_FALLBACK_MODELS)
        try:
            usage = await _codex_usage(client, headers)
            models = sorted(set(models) | set(usage.get("models") or []))  # the plan's usage table names only some of them
        except SubscriptionError:
            pass
        return web.json_response({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "openai"} for m in models]})
    if tail != "chat/completions":
        return web.json_response({"error": {"message": f"codex serves chat/completions and models, not {tail!r}", "type": "invalid_request_error"}}, status=404)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": {"message": "body must be JSON", "type": "invalid_request_error"}}, status=400)
    model = str(body.get("model") or "")
    stream = bool(body.get("stream", False))
    sent = chat_to_responses(body)
    upstream = client.build_request("POST", CODEX_BASE + "/codex/responses", json=sent, headers={**headers, "accept": "text/event-stream", "content-type": "application/json"})
    try:
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        return web.json_response({"error": {"message": f"upstream unreachable: {type(exc).__name__}", "type": "proxy_error"}}, status=502)
    if response.status_code >= 400:
        raw = (await response.aread()).decode("utf-8", "replace")
        await response.aclose()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"error": {"message": raw[:400], "type": "upstream_error"}}
        return web.json_response(payload, status=response.status_code)
    chunks = responses_events_to_chunks(_sse_lines(response), model=model)
    served = served_headers("codex", str(sent.get("model") or model))
    try:
        if not stream:
            return web.json_response(await collect_completion(chunks, model=model), headers=served)
        out = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", **served})
        await out.prepare(request)
        async for chunk in chunks:
            await out.write(chunk.encode("utf-8"))
        await out.write_eof()
        return out
    except (ConnectionResetError, asyncio.CancelledError):
        return web.Response(status=499)
    finally:
        await response.aclose()


async def _codex_usage(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    cached = _usage_cache.get("codex")
    if cached and time.monotonic() - cached[0] < USAGE_CACHE_SECONDS:
        return cached[1]
    response = await client.get(CODEX_BASE + "/wham/usage", headers={**headers, "accept": "application/json"})
    if response.status_code != 200:
        raise SubscriptionError(f"codex usage: HTTP {response.status_code}")
    view = codex_usage_view(response.json())
    _usage_cache["codex"] = (time.monotonic(), view)
    return view


async def _grok_usage(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    cached = _usage_cache.get("grok")
    if cached and time.monotonic() - cached[0] < USAGE_CACHE_SECONDS:
        return cached[1]
    response = await client.get(GROK_BASE + "/billing?format=credits", headers={**headers, "accept": "application/json"})
    if response.status_code != 200:
        raise SubscriptionError(f"grok usage: HTTP {response.status_code}")
    view = grok_usage_view(response.json())
    _usage_cache["grok"] = (time.monotonic(), view)
    return view


async def _opencode_usage(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    cached = _usage_cache.get("opencode")
    if cached and time.monotonic() - cached[0] < USAGE_CACHE_SECONDS:
        return cached[1]
    response = await client.get(OPENCODE_GO_BASE + "/usage", headers={**headers, "accept": "application/json"})
    if response.status_code != 200:
        raise SubscriptionError(f"opencode usage: HTTP {response.status_code}")
    view = opencode_usage_view(response.json())
    _usage_cache["opencode"] = (time.monotonic(), view)
    return view


async def _claude_profile(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    try:
        pr = await client.get(CLAUDE_API + "/api/oauth/profile", headers={**headers, "accept": "application/json"})
        if pr.status_code == 200:
            return pr.json()
    except (httpx.HTTPError, ValueError):
        return {}
    return {}


async def _claude_usage(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    """Quota for the Usage screen. The usage endpoint rate-limits independently of inference;
    a 429 is served from the last good snapshot (or profile) instead of hammering it."""
    global _claude_usage_retry_at
    cached = _usage_cache.get("claude")
    now = time.monotonic()
    if cached and now - cached[0] < USAGE_CACHE_SECONDS:
        return cached[1]
    if now < _claude_usage_retry_at and cached:
        return cached[1]
    light = await CLAUDE_AUTH.usage_headers(client)
    if now < _claude_usage_retry_at:
        profile = await _claude_profile(client, light)
        return claude_usage_view({}, profile)
    usage = await client.get(CLAUDE_API + "/api/oauth/usage", headers=light)
    if usage.status_code == 429:
        retry = usage.headers.get("retry-after")
        try:
            wait = max(CLAUDE_USAGE_BACKOFF_SECONDS, float(retry or 0))
        except ValueError:
            wait = CLAUDE_USAGE_BACKOFF_SECONDS
        _claude_usage_retry_at = now + wait
        if cached:
            return cached[1]
        return claude_usage_view({}, await _claude_profile(client, light))
    if usage.status_code != 200:
        raise SubscriptionError(f"claude usage: HTTP {usage.status_code}")
    view = claude_usage_view(usage.json(), await _claude_profile(client, light))
    _usage_cache["claude"] = (now, view)
    return view


async def handle_subscriptions_usage(request: web.Request) -> web.Response:
    """Subscription quota windows, for the Usage screen; a missing login is reported, not an error."""
    client: httpx.AsyncClient = request.app["client"]
    out: dict[str, Any] = {}
    for name, auth, fetch in (("codex", CODEX_AUTH, _codex_usage), ("grok", GROK_AUTH, _grok_usage), ("claude", CLAUDE_AUTH, _claude_usage), ("opencode", OPENCODE_AUTH, _opencode_usage)):
        if not auth.available():
            out[name] = {"provider": name, "logged_in": False}
            continue
        try:
            out[name] = {"logged_in": True, **await fetch(client, await auth.headers(client))}
        except (SubscriptionError, httpx.HTTPError, ValueError) as exc:
            out[name] = {"provider": name, "logged_in": True, "error": str(exc)[:200]}
    return web.json_response(out)


async def handle(request: web.Request) -> web.StreamResponse:
    name = request.match_info["upstream"].lower()
    rest = request.match_info.get("rest", "")
    if name == "codex":
        return await handle_codex(request, rest)
    if name == "claude":
        return await handle_claude(request, rest)
    table = upstreams()
    client: httpx.AsyncClient = request.app["client"]
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    if name == "grok":
        # A subscription, not a key: no USD budget applies, the CLI's identity travels with the call.
        try:
            headers.update(await GROK_AUTH.headers(client))
        except SubscriptionError as exc:
            return web.json_response({"error": {"message": str(exc), "type": "subscription_error"}}, status=502)
        base, key = GROK_BASE, ""
        rest = rest.strip("/").removeprefix("v1/")
    elif name not in table:
        # A name the proxy knows but has no key for is a different fact from a name it never heard
        # of, and 404 on both made a missing key look like a vendor whose /models path moved.
        if name in DEFAULT_UPSTREAMS or name in CLI_LOGINS:
            return web.json_response({"error": {"message": f"upstream {name!r} has no credential in the key proxy", "type": "no_credential"}}, status=503)
        return web.json_response({"error": f"unknown upstream {name!r}"}, status=404)
    else:
        base, key = table[name]
        if budget_exceeded() and not budget_exempt(rest):
            return web.json_response({"error": {"message": "daily budget exceeded; refused by the key proxy", "type": "budget_exceeded"}}, status=402)
        if key:
            inject_key(headers, name, key)
    body = await request.read()
    if name == "grok":
        # The CLI chat proxy routes on this header. The JSON model is not what it documents
        # as the route, and the current CLI sends the header on every completion.
        _stamp_grok_model(headers, body)
    meter = bool(AGENT_API) and METERED_HEADER not in request.headers and request.method == "POST" and is_completion(rest)
    request_model = ""
    if meter:
        try:
            request_model = str(json.loads(body).get("model") or "")
        except (ValueError, AttributeError):
            request_model = ""
    started = time.monotonic()
    captured = bytearray()
    try:
        upstream = client.build_request(request.method, target_url(base, rest, request.query_string), headers=headers, content=body)
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        return web.json_response({"error": {"message": f"upstream unreachable: {type(exc).__name__}", "type": "proxy_error"}}, status=502)
    out = web.StreamResponse(status=response.status_code)
    for k, v in response.headers.items():
        if k.lower() not in DROP_RESPONSE_HEADERS:
            out.headers[k] = v
    out.headers.update(served_headers(name, request_model))
    await out.prepare(request)
    try:
        # aiter_bytes() decodes gzip/br on the way through; aiter_raw() would hand the client compressed bytes with the header gone.
        async for chunk in response.aiter_bytes():
            if meter and len(captured) < CAPTURE_LIMIT:
                captured.extend(chunk)
            await out.write(chunk)
        await out.write_eof()
    except (ConnectionResetError, asyncio.CancelledError):
        pass  # the caller went away mid-stream
    finally:
        await response.aclose()
    if meter and response.status_code < 300:
        asyncio.create_task(report_direct_usage(client, name, request_model, bytes(captured), response.headers.get("content-type", ""), started))
    return out


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "upstreams": sorted(upstreams()), "subscriptions": subscriptions_status(), "budget_exceeded": budget_exceeded(), "spent_today_usd": round(spent_today(), 4) if BUDGET_USD_PER_DAY > 0 else None})


def make_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app["client"] = httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=30.0, pool=10.0), limits=httpx.Limits(max_connections=64, max_keepalive_connections=16), follow_redirects=False)
    app.router.add_get("/healthz", health)
    app.router.add_get("/keys", handle_keys)
    app.router.add_get("/subscriptions/usage", handle_subscriptions_usage)
    app.router.add_route("*", "/{upstream}/{rest:.*}", handle)

    async def close(app: web.Application) -> None:
        await app["client"].aclose()

    app.on_cleanup.append(close)
    return app


def utf8_streams() -> None:
    """Make standard output and error UTF-8, replacing what cannot be written rather than raising.

    Its log is a file the launcher keeps, which on Windows outside UTF-8 mode is in the locale's code
    page; a provider's error text carrying anything else must not end the proxy.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        reconfigure = getattr(stream, "reconfigure", None)
        if encoding != "utf8" and reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    utf8_streams()
    port = int(os.environ.get("KEYPROXY_PORT", "3200"))
    # A proxy that injects provider keys must not be one the local network can call, so the default
    # is the loopback interface and reaching further is something a caller asks for by name. In a
    # container every interface is the container's own private one and the agent reaches this by
    # service name, so deploy/compose.yaml sets KEYPROXY_HOST there; the launcher sets it too, and a
    # checkout older than either of them now binds the safe side rather than the whole machine.
    host = os.environ.get("KEYPROXY_HOST", "").strip() or "127.0.0.1"
    if not upstreams():
        logger.warning("no upstream keys configured; every call will be refused")
    logger.warning("listening on %s:%d", host, port)
    web.run_app(make_app(), host=host, port=port, print=None, access_log=None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
