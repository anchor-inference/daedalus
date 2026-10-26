"""Key proxy: the only process that holds provider API keys.

The agent container talks to ``http://keyproxy:3200/<upstream>/...`` with no credentials;
this service injects the real ``Authorization`` header for each upstream from its own
environment and forwards the request, streaming the response back (SSE included). When
the daily-budget flag exists on the shared state volume, calls to model upstreams are
refused here — below the agent, where a self-modification cannot reach.

Upstreams: ``deepseek`` → https://api.deepseek.com, ``openrouter`` → https://openrouter.ai/api/v1,
``openai`` → https://api.openai.com/v1, ``anthropic`` → https://api.anthropic.com/v1 (its OpenAI-compatible
chat and models endpoints; the key goes in ``x-api-key`` and the ``anthropic-version`` header is added when the
caller sent none), ``opencode`` → https://opencode.ai/zen/go/v1 (OpenCode Go, the prepaid subscription) and
``opencode_zen`` → https://opencode.ai/zen/v1 (OpenCode Zen, pay per token); one OpenCode key serves both. Keys:
``DEEPSEEK_API_KEY``, ``OPENROUTER_API_KEY``, ``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, ``OPENCODE_API_KEY``. Extra
upstreams: ``KEYPROXY_UPSTREAM_<NAME>=https://host/base`` with
``KEYPROXY_KEY_<NAME>=…`` and, for an API that does not take ``Authorization: Bearer``,
``KEYPROXY_AUTH_<NAME>=<header name>`` (``X-API-KEY`` for Serper, ``x-api-key`` for Exa…);
the key is sent as that header's value.

Keys set from the app: with ``KEYPROXY_KEYS_FILE`` naming the env file the keys came from, the bot
can write one upstream's key with ``PUT /keys/<upstream>`` (and clear it with ``DELETE``) on its own
API token. The proxy writes the file and reads it again on every call, so a key saved in Settings
signs the next request without a restart, and the agent's own configuration never holds it.

The subscriptions are served as their vendors' CLIs would call them, and each CLI's identity is a
profile in ``clients.py``: ``GET /clients`` says, for the bot alone, which version each one presents and
the newest release (asked of the registries every few hours), and ``POST /clients/<name>/update`` and
``/revert`` move it. The profile is stored in the keys file, so it holds from the next call.

The budget is checked in two independent ways: the supervisor's flag file, and — when
``KEYPROXY_USD_PER_DAY`` is set — the proxy's own read of today's spend from the read-only
database, which nothing in the agent container can unlink.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import clients
import httpx
from aiohttp import web
from claude import (
    CLAUDE_API,
    CLAUDE_FALLBACK_MODELS,
    ClaudeAuth,
    chat_to_messages,
    claude_header_windows,
    claude_usage_view,
    messages_events_to_chunks,
    prefix_locked,
    request_betas,
    without_thinking,
)
from subscriptions import (
    CODEX_BASE,
    CODEX_FALLBACK_CATALOGUE,
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
    refused_reasoning,
    responses_events_to_chunks,
    without_reasoning,
)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s keyproxy %(levelname)s: %(message)s")
logger = logging.getLogger("keyproxy")
timing_logger = logging.getLogger("keyproxy.timing")
# One line per upstream request at INFO, while the rest of the process stays at WARNING: the timings
# are what an operator reads to tell a slow upstream from a slow host, and nothing else here is chatty
# enough to drown them. httpx keeps its own per-request INFO lines off because the root stays at WARNING.
timing_logger.setLevel(logging.INFO)

DEFAULT_UPSTREAMS = {
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    "opencode": ("https://opencode.ai/zen/go/v1", "OPENCODE_API_KEY"),
    "opencode_zen": ("https://opencode.ai/zen/v1", "OPENCODE_API_KEY"),
    "zai": ("https://api.z.ai/api/paas/v4", "ZAI_API_KEY"),
    "zai_coding": ("https://api.z.ai/api/coding/paas/v4", "ZAI_CODING_API_KEY"),
    "minimax": ("https://api.minimax.io/v1", "MINIMAX_API_KEY"),
    "minimax_plan": ("https://api.minimax.io/v1", "MINIMAX_PLAN_API_KEY"),
    "moonshot": ("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
    "kimi_coding": ("https://api.kimi.ai/coding/v1", "KIMI_CODING_API_KEY"),
}
SEARCH_UPSTREAMS = {
    "serper": ("https://google.serper.dev", "KEYPROXY_KEY_SERPER"),
    "keenable": ("https://api.keenable.ai/v1", "KEYPROXY_KEY_KEENABLE"),
    "tavily": ("https://api.tavily.com", "KEYPROXY_KEY_TAVILY"),
    "exa": ("https://api.exa.ai", "KEYPROXY_KEY_EXA"),
    "perplexity": ("https://api.perplexity.ai", "KEYPROXY_KEY_PERPLEXITY"),
}
"""The WebSearch backends, built in like the model vendors. They used to be extra upstreams the operator
declared by hand in keyproxy.env (address, key and header), so a key typed into Settings had no
upstream to go to: the proxy answered "unknown upstream" and the only way in was editing the file."""
DEFAULT_UPSTREAMS.update(SEARCH_UPSTREAMS)
PREPAID_UPSTREAMS = ("opencode",)
"""Keyed upstreams that are prepaid plans (OpenCode Go): a call spends the plan's allowance, not money, so the daily
dollar flag does not refuse it, the way it never refuses the subscriptions served through a CLI's login."""
DEFAULT_AUTH_SCHEMES = {"anthropic": "x-api-key", "serper": "X-API-KEY", "keenable": "X-API-Key", "exa": "x-api-key"}
"""Upstreams whose key is not an ``Authorization: Bearer`` one; ``KEYPROXY_AUTH_<NAME>`` still overrides."""
ANTHROPIC_VERSION = "2023-06-01"
"""Anthropic refuses a request without this header, ``/models`` included, and a client written for
the OpenAI shape never sends it."""
BUDGET_FLAG = Path(os.environ.get("KEYPROXY_BUDGET_FLAG", "/srv/state/BUDGET_EXCEEDED"))
BUDGET_DB = Path(os.environ.get("KEYPROXY_BUDGET_DB", "/srv/state/daedalus.sqlite"))
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
# Looked up through settings() at call time, so a key saved from the app shows its allowance at once.
OPENCODE_AUTH = OpencodeAuth(lookup=lambda var: settings().get(var, ""))
CLAUDE_AUTH = ClaudeAuth(Path(os.environ.get("KEYPROXY_CLAUDE_AUTH", os.path.expanduser("~/.claude/.credentials.json"))))
"""The operator's subscriptions: served as ``/codex/v1``, ``/grok/v1`` and ``/claude/v1`` with the CLIs' own logins."""
_usage_cache: dict[str, tuple[float, dict[str, Any]]] = {}
USAGE_CACHE_SECONDS = 60.0
_claude_usage_retry_at = 0.0
_claude_header_snapshot: dict[str, Any] = {}
"""The 5h and weekly windows read off the latest Claude model reply, with when they were read."""
CLAUDE_USAGE_BACKOFF_SECONDS = 120.0


BEARER = "bearer"


KEYS_FILE = os.environ.get("KEYPROXY_KEYS_FILE", "").strip()
"""The env file the provider keys live in, when the proxy may write it; "" when keys come from the environment alone."""
_keys_file_cache: tuple[int, int, dict[str, str]] = (-1, -1, {})


def parse_env(text: str) -> dict[str, str]:
    """An env file as the launcher reads it: comments skipped, ``export`` allowed, one pair of quotes taken off."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        out[key.strip()] = value
    return out


def file_settings() -> dict[str, str]:
    """What the keys file says now, re-read only when it changed."""
    global _keys_file_cache
    if not KEYS_FILE:
        return {}
    try:
        stat = os.stat(KEYS_FILE)
    except OSError:
        return {}
    if (stat.st_mtime_ns, stat.st_size) != _keys_file_cache[:2]:
        try:
            text = Path(KEYS_FILE).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return _keys_file_cache[2]
        _keys_file_cache = (stat.st_mtime_ns, stat.st_size, parse_env(text))
    return _keys_file_cache[2]


def settings() -> dict[str, str]:
    """The environment with the keys file over it.

    The file wins wherever it names a variable, an empty value included: the process started with
    the keys the file held then, and a key forgotten since must stop signing calls rather than live
    on in the environment until the next restart.
    """
    return {**os.environ, **file_settings()}


def upstream_key(name: str, env: dict[str, str]) -> str:
    """The variable that holds an upstream's key, or "" for a name the proxy holds no key for.

    An extra upstream with no ``KEYPROXY_KEY_<NAME>`` of its own that is also a default one falls
    back to the default's variable. The launcher declares ``KEYPROXY_UPSTREAM_OPENCODE_ZEN`` so the
    address is written down, and without the fallback that declaration hid ``OPENCODE_API_KEY``,
    which serves Zen as much as Go: Zen was called with no key at all and reported as needing none.
    """
    own = f"KEYPROXY_KEY_{name.upper()}"
    if env.get(f"KEYPROXY_UPSTREAM_{name.upper()}"):
        if env.get(own) or name not in DEFAULT_UPSTREAMS:
            return own
    if name in DEFAULT_UPSTREAMS:
        return DEFAULT_UPSTREAMS[name][1]
    return ""


def extra_upstreams(env: dict[str, str]) -> dict[str, str]:
    """name → base URL for every ``KEYPROXY_UPSTREAM_<NAME>`` that carries an address."""
    return {var[len("KEYPROXY_UPSTREAM_"):].lower(): value.rstrip("/") for var, value in env.items() if var.startswith("KEYPROXY_UPSTREAM_") and value}


def upstreams() -> dict[str, tuple[str, str]]:
    """name → (base_url, api_key) from the environment and the keys file."""
    env = settings()
    out: dict[str, tuple[str, str]] = {}
    for name, (base, env_key) in DEFAULT_UPSTREAMS.items():
        key = env.get(env_key, "")
        if key:
            out[name] = (base, key)
    for name, base in extra_upstreams(env).items():
        key = env.get(upstream_key(name, env), "")
        # A default upstream redeclared without a key is still one that needs a key: listing it
        # keyless would forward the call unsigned instead of saying the key is missing.
        if key or name not in DEFAULT_UPSTREAMS:
            out[name] = (base, key)
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
    env = settings()
    out: dict[str, dict[str, Any]] = {}
    for name, (_base, env_key) in DEFAULT_UPSTREAMS.items():
        out[name] = {"configured": bool(env.get(env_key, "")), "kind": API_KEY}
    for name in extra_upstreams(env):
        key = env.get(upstream_key(name, env), "")
        if name in DEFAULT_UPSTREAMS:
            out[name] = {"configured": bool(key), "kind": API_KEY}
        else:
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


def write_env_value(path: str, var: str, value: str) -> None:
    """Set one variable in an env file, leaving every other line as it was.

    Replaced through a temporary file so a reader never sees half of it; where the file is a bind
    mount of its own (the compose stack mounts the one file, not its folder) a rename onto it fails,
    and it is rewritten in place instead.
    """
    target = Path(path)
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for index, line in enumerate(lines):
        stripped = line.lstrip(" \t")
        if stripped.startswith(var + "=") or stripped.startswith("export " + var + "="):
            lines[index] = f"{var}={value}"
            break
    else:
        lines.append(f"{var}={value}")
    text = "\n".join(lines) + "\n"
    global _keys_file_cache
    # Forgotten whatever the clock says: a key replaced by one of the same length within the
    # file system's timestamp resolution would otherwise read as unchanged.
    _keys_file_cache = (-1, -1, {})
    temporary = target.with_name(target.name + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)


async def handle_set_key(request: web.Request) -> web.Response:
    """Store (PUT) or forget (DELETE) one upstream's key, for the bot alone. The key never comes back.

    This is how a key typed into Settings reaches the process that signs calls with it. Before it,
    the app kept the key in its own configuration and sent it to the proxy, which dropped the
    caller's Authorization header as it must and then refused the call for want of a key of its own;
    the Settings card said "key stored" while every other screen, rightly, said "no key".
    """
    if not agent_call(request):
        return web.json_response({"error": "this endpoint answers the agent only"}, status=403)
    name = request.match_info["upstream"].lower()
    if not KEYS_FILE:
        return web.json_response({"error": "this key proxy reads its keys from its environment only; put the key in keyproxy.env"}, status=409)
    if name in CLI_LOGINS:
        return web.json_response({"error": f"{name!r} signs in with its command-line tool, not with a key"}, status=400)
    env = settings()
    var = upstream_key(name, env)
    if not var:
        return web.json_response({"error": f"unknown upstream {name!r}"}, status=404)
    key = ""
    if request.method == "PUT":
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            body = None
        key = str(body.get("key") or "") if isinstance(body, dict) else ""
        # A newline would end the line and turn the rest of the key into a variable of its own.
        key = key.replace("\r", "").replace("\n", "").strip()
        if not key:
            return web.json_response({"error": "no key given"}, status=400)
    try:
        write_env_value(KEYS_FILE, var, key)
    except OSError as exc:
        logger.warning("could not write the keys file: %s", type(exc).__name__)
        return web.json_response({"error": f"the key proxy could not write its keys file ({type(exc).__name__})"}, status=500)
    return web.json_response({"upstream": name, **key_status().get(name, {"configured": bool(key), "kind": API_KEY})})


def budget_usd_per_day() -> float:
    """The proxy's own daily cap, read through the keys file so a change in Settings holds from the next call."""
    try:
        return max(0.0, float(settings().get("KEYPROXY_USD_PER_DAY", "0") or 0))
    except ValueError:
        return 0.0


SETTING_NAME = re.compile(r"KEYPROXY_(UPSTREAM|KEY|AUTH)_[A-Z][A-Z0-9_]*")
SECRET_VARS = {env_key for _base, env_key in DEFAULT_UPSTREAMS.values()}
"""Every key variable; a value under one of these names never leaves the proxy, only whether it is set."""


def setting_writable(var: str) -> bool:
    """The variables the app may set: every key, every upstream's address and header, and the daily cap.

    Nothing else in the environment — the proxy's port, the paths it reads logins and the budget
    from — is reachable this way, so a request through the bot cannot point the proxy elsewhere.
    """
    return var in SECRET_VARS or var == "KEYPROXY_USD_PER_DAY" or bool(SETTING_NAME.fullmatch(var))


def setting_secret(var: str) -> bool:
    return var in SECRET_VARS or var.startswith("KEYPROXY_KEY_")


def settings_view() -> dict[str, Any]:
    """Every value the operator can set here, grouped as the Settings screen shows it. Never a key.

    Built-in upstreams are listed whether or not anything is written for them, because "no key" is
    the fact the operator came to change; an extra upstream is listed once its address is written.
    """
    env = settings()
    written = file_settings()

    def entry(var: str, default: str = "") -> dict[str, Any]:
        value = env.get(var, "")
        row: dict[str, Any] = {"name": var, "secret": setting_secret(var), "set": bool(value), "source": "file" if var in written else "environment" if var in os.environ else "default"}
        if not row["secret"]:
            row["value"] = value
            row["default"] = default
        return row

    upstream_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, (base, _env_key) in DEFAULT_UPSTREAMS.items():
        group = "search" if name in SEARCH_UPSTREAMS else "model"
        key_var = upstream_key(name, env)
        upstream_rows.append({
            "upstream": name,
            "group": group,
            "builtin": True,
            # OpenCode Go and Zen share one key: the second one names the first rather than a key of its own.
            "shares_key_with": next((other for other, (_b, other_key) in DEFAULT_UPSTREAMS.items() if other_key == key_var and other != name and other in seen), ""),
            "key": entry(key_var),
            "address": entry(f"KEYPROXY_UPSTREAM_{name.upper()}", base),
            "header": entry(f"KEYPROXY_AUTH_{name.upper()}", DEFAULT_AUTH_SCHEMES.get(name, "")),
        })
        seen.add(name)
    for name in sorted(extra_upstreams(env)):
        if name in DEFAULT_UPSTREAMS:
            continue
        upstream_rows.append({
            "upstream": name,
            "group": "extra",
            "builtin": False,
            "shares_key_with": "",
            "key": entry(upstream_key(name, env)),
            "address": entry(f"KEYPROXY_UPSTREAM_{name.upper()}"),
            "header": entry(f"KEYPROXY_AUTH_{name.upper()}"),
        })
    logins = [{"upstream": name, "configured": credential_readable(auth)} for name, auth in (("codex", CODEX_AUTH), ("grok", GROK_AUTH), ("claude", CLAUDE_AUTH))]
    return {"writable": bool(KEYS_FILE), "upstreams": upstream_rows, "cli_logins": logins, "budget": entry("KEYPROXY_USD_PER_DAY", "0")}


async def handle_settings(request: web.Request) -> web.Response:
    """Every settable value, for the bot alone: addresses and headers in full, keys only as set or not."""
    if not agent_call(request):
        return web.json_response({"error": "this endpoint answers the agent only"}, status=403)
    return web.json_response(settings_view())


async def handle_set_setting(request: web.Request) -> web.Response:
    """Write one variable of the keys file, for the bot alone; an empty value clears it.

    The file is read again on every call, so what is written here holds from the next request: an
    address, a header, a key or the daily cap, with no restart of anything.
    """
    if not agent_call(request):
        return web.json_response({"error": "this endpoint answers the agent only"}, status=403)
    var = request.match_info["name"].upper()
    if not setting_writable(var):
        return web.json_response({"error": f"{var} is not a setting of the key proxy"}, status=404)
    if not KEYS_FILE:
        return web.json_response({"error": "this key proxy reads its keys from its environment only; put the value in keyproxy.env"}, status=409)
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    value = str(body.get("value") or "") if isinstance(body, dict) else ""
    # A newline would end the line and turn the rest into a variable of its own.
    value = value.replace("\r", "").replace("\n", "").strip()
    if value and var.startswith("KEYPROXY_UPSTREAM_") and not value.startswith(("http://", "https://")):
        return web.json_response({"error": "an upstream address starts with http:// or https://"}, status=400)
    if value and var.startswith("KEYPROXY_AUTH_") and not re.fullmatch(r"[A-Za-z0-9-]+", value):
        return web.json_response({"error": "a header name is letters, digits and dashes"}, status=400)
    if value and var == "KEYPROXY_USD_PER_DAY":
        try:
            if float(value) < 0:
                raise ValueError
        except ValueError:
            return web.json_response({"error": "the daily cap is a number of dollars, 0 for none"}, status=400)
    try:
        write_env_value(KEYS_FILE, var, value)
    except OSError as exc:
        logger.warning("could not write the keys file: %s", type(exc).__name__)
        return web.json_response({"error": f"the key proxy could not write its keys file ({type(exc).__name__})"}, status=500)
    return web.json_response(settings_view())


def _write_client_setting(var: str, value: str) -> None:
    write_env_value(KEYS_FILE, var, value)


# The imitated clients' profiles live in the keys file too: the one file this process may write, read
# again on every call, and kept across restarts in a container and in a native installation alike.
clients.configure(read=settings, write=_write_client_setting, writable=lambda: bool(KEYS_FILE))


def logged_in(name: str) -> bool | None:
    """Whether the subscription a client profile signs for has a readable login; None for a client without one."""
    auth = {"codex": CODEX_AUTH, "grok": GROK_AUTH, "claude": CLAUDE_AUTH}.get(clients.SPECS[name].upstream)
    return credential_readable(auth) if auth is not None else None


def clients_view() -> dict[str, Any]:
    return {"writable": clients.writable(), "check_seconds": clients.CHECK_SECONDS, "clients": [clients.view(name, logged_in(name)) for name in clients.SPECS]}


async def handle_clients(request: web.Request) -> web.Response:
    """Each imitated client: the version in force, the newest release and what an update would move. For the bot alone."""
    if not agent_call(request):
        return web.json_response({"error": "this endpoint answers the agent only"}, status=403)
    return web.json_response(clients_view())


async def handle_clients_check(request: web.Request) -> web.Response:
    """Ask the registries now rather than at the next due check, for the bot alone."""
    if not agent_call(request):
        return web.json_response({"error": "this endpoint answers the agent only"}, status=403)
    await clients.check(request.app["client"], force=True)
    return web.json_response(clients_view())


async def handle_client_action(request: web.Request) -> web.Response:
    """Update one client to the newest release (or to the body's ``version``), or revert its last update. For the bot alone."""
    if not agent_call(request):
        return web.json_response({"error": "this endpoint answers the agent only"}, status=403)
    name, action = request.match_info["name"].lower(), request.match_info["action"]
    try:
        if action == "update":
            try:
                body = await request.json() if request.can_read_body else {}
            except (ValueError, UnicodeDecodeError):
                body = {}
            target = str(body.get("version") or "").strip() if isinstance(body, dict) else ""
            result = await clients.update(name, request.app["client"], target)
        else:
            result = await clients.revert(name)
    except clients.ClientError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)
    except OSError as exc:
        logger.warning("could not write the keys file: %s", type(exc).__name__)
        return web.json_response({"error": f"the key proxy could not write its keys file ({type(exc).__name__})"}, status=500)
    result["client"]["logged_in"] = logged_in(name)
    return web.json_response(result)


def auth_scheme(name: str) -> str:
    """How an upstream takes its key: ``bearer`` (``Authorization: Bearer <key>``) or the name of a header that carries the bare key."""
    scheme = settings().get(f"KEYPROXY_AUTH_{name.upper()}", "").strip() or DEFAULT_AUTH_SCHEMES.get(name, "")
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
    cap = budget_usd_per_day()
    return cap > 0 and spent_today() > cap


def budget_exempt(rest: str) -> bool:
    segments = tuple(s for s in rest.split("/") if s)
    if segments and segments[0] == "v1":
        segments = segments[1:]
    return any(segments[-len(tail):] == tail for tail in BUDGET_EXEMPT_TAILS if len(segments) >= len(tail))


def free_model_request(body: bytes) -> bool:
    """Whether the call names a free model (OpenRouter's ``:free``, OpenCode's ``-free``).

    The daily flag counts priced spend; a free model adds none, so refusing it once the day's paid
    spend passed the cap stopped the operator's free presets along with the paid ones.
    """
    try:
        model = json.loads(body or b"{}").get("model")
    except (ValueError, AttributeError):
        return False
    return isinstance(model, str) and model.endswith((":free", "-free"))


def target_url(base: str, rest: str, query: str) -> str:
    return base.rstrip("/") + "/" + rest.lstrip("/") + (f"?{query}" if query else "")


def subscriptions_status() -> dict[str, Any]:
    return {"codex": {"logged_in": CODEX_AUTH.available()}, "grok": {"logged_in": GROK_AUTH.available()}, "claude": {"logged_in": CLAUDE_AUTH.available()}}


async def _sse_lines(response: httpx.Response, timing: UpstreamTiming | None = None) -> Any:
    async for line in response.aiter_lines():
        if timing is not None:
            timing.received(len(line) + 1)  # the newline the decoder took off still came over the wire
        yield line


def slow_first_byte_seconds() -> float:
    """From how long without a first byte an upstream request is logged as a warning rather than a note."""
    try:
        return max(0.0, float(os.environ.get("KEYPROXY_SLOW_FIRST_BYTE_SECONDS", "30") or 30))
    except ValueError:
        return 30.0


class UpstreamTiming:
    """When one upstream request was answered, for the log; never a header's value or a byte of the body.

    Three moments are kept because each names a different fault: response headers late is an upstream
    slow to accept the request, headers on time and the first body byte late is one that accepted it
    and then sat silent (a cold Grok request once waited 103.7 s here while its peers took 8 to 13 s),
    and both on time with a long total is a stream that is merely long. The bot's own records see only
    the first chunk it could parse, which cannot tell the first two apart.
    """

    __slots__ = ("upstream", "method", "path", "model", "started", "headers_at", "first_byte_at", "status", "size", "reported")

    def __init__(self, upstream: str, method: str, path: str, model: str = "") -> None:
        self.upstream = upstream
        self.method = method
        self.path = path.strip("/")
        self.model = model
        self.started = time.monotonic()
        self.headers_at: float | None = None
        self.first_byte_at: float | None = None
        self.status = 0
        self.size = 0
        self.reported = False

    def answered(self, status: int) -> None:
        self.headers_at = time.monotonic()
        self.status = status

    def received(self, size: int) -> None:
        if size <= 0:
            return
        if self.first_byte_at is None:
            self.first_byte_at = time.monotonic()
        self.size += size

    def milliseconds(self, at: float | None) -> int | None:
        return None if at is None else int((at - self.started) * 1000)

    def view(self, outcome: str) -> dict[str, Any]:
        return {
            "upstream": self.upstream, "method": self.method, "path": self.path, "model": self.model,
            "status": self.status, "headers_ms": self.milliseconds(self.headers_at),
            "first_byte_ms": self.milliseconds(self.first_byte_at), "total_ms": self.milliseconds(time.monotonic()),
            "bytes": self.size, "outcome": outcome,
        }

    def report(self, outcome: str = "done") -> dict[str, Any]:
        """Write the one line for this request; a second call (a handler's ``finally`` after its own report) is a no-op."""
        view = self.view(outcome)
        if self.reported:
            return view
        self.reported = True
        waited = view["first_byte_ms"] if view["first_byte_ms"] is not None else view["total_ms"]
        level = logging.WARNING if waited >= slow_first_byte_seconds() * 1000 else logging.INFO
        fields = " ".join(f"{key}={'-' if value is None or value == '' else value}" for key, value in view.items() if key not in ("upstream", "method", "path"))
        timing_logger.log(level, "upstream %s %s %s %s", self.upstream, self.method, self.path, fields)
        return view


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


def refusal_headers(headers: httpx.Headers) -> dict[str, str]:
    """The upstream's stated wait, passed on with a refusal the proxy re-wraps.

    The bot waits out a rate limit for as long as ``Retry-After`` says; dropped here, it retried a 429
    on its own short backoff and spent the attempts before the window reopened."""
    wait = headers.get("retry-after")
    return {"Retry-After": wait} if wait else {}


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
        limits: dict[str, dict[str, int]] = {}
        try:
            response = await client.get(CLAUDE_API + "/v1/models?limit=1000", headers={**headers, "accept": "application/json"})
            if response.status_code == 200:
                listed = [m for m in (response.json().get("data") or []) if isinstance(m, dict) and m.get("id")]
                models = sorted({str(m["id"]) for m in listed} | set(models))
                # The Models API states each model's window and output cap; passed on, so the bot need
                # not guess them for a model added through the subscription.
                limits = {str(m["id"]): {k: m[k] for k in ("max_input_tokens", "max_tokens") if type(m.get(k)) is int} for m in listed}
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
        return web.json_response({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "anthropic", **limits.get(m, {})} for m in models if m]})
    if tail != "chat/completions":
        return web.json_response({"error": {"message": f"claude serves chat/completions and models, not {tail!r}", "type": "invalid_request_error"}}, status=404)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": {"message": "body must be JSON", "type": "invalid_request_error"}}, status=400)
    model = str(body.get("model") or "")
    stream = bool(body.get("stream", False))
    payload, names = chat_to_messages(body)
    headers["anthropic-beta"] = request_betas(payload)
    healed = False
    while True:
        upstream = client.build_request("POST", CLAUDE_API + "/v1/messages?beta=true", json=payload, headers={**headers, "accept": "text/event-stream", "content-type": "application/json"})
        timing = UpstreamTiming("claude", "POST", "v1/messages", str(payload.get("model") or model))
        try:
            response = await client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            timing.report(f"unreachable:{type(exc).__name__}")
            return web.json_response({"error": {"message": f"upstream unreachable: {type(exc).__name__}", "type": "proxy_error"}}, status=502)
        timing.answered(response.status_code)
        if response.status_code < 400:
            break
        raw_bytes = await response.aread()
        timing.received(len(raw_bytes))
        timing.report("refused")
        raw = raw_bytes.decode("utf-8", "replace")
        await response.aclose()
        locked, first = prefix_locked(response.status_code, raw, response.headers)
        if locked and not healed:
            # Thinking signed over a prefix the bot has since rewritten is refused before any output on an
            # account the preserved-thinking check is enforced on. Claude Code answers this by dropping
            # that block and every later one and asking once more; the model loses that reasoning, not
            # the turn. Said aloud, since a history edited under replayed thinking should be rare.
            payload, removed = without_thinking(payload, first)
            if removed:
                logger.warning("claude refused replayed thinking bound to another prefix (%s); retrying without %d block(s)", raw[:200], removed)
                healed = True
                continue
        break
    if response.status_code >= 400:
        await clients.verify("claude", response.status_code, raw)
        try:
            payload_err = json.loads(raw)
        except ValueError:
            payload_err = {"error": {"message": raw[:400], "type": "upstream_error"}}
        return web.json_response(payload_err, status=response.status_code, headers=refusal_headers(response.headers))
    await clients.verify("claude", response.status_code)
    if windows := claude_header_windows(response.headers):
        _claude_header_snapshot.update(windows=windows, at=time.monotonic())
    chunks = messages_events_to_chunks(_sse_lines(response, timing), model=model, names=names)
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
        timing.report("caller_gone")
        return web.Response(status=499)
    except httpx.HTTPError as exc:
        timing.report(f"upstream_dropped:{type(exc).__name__}")
        raise
    finally:
        timing.report()
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
        windows = {**CODEX_FALLBACK_CATALOGUE, **codex_cached_windows(CODEX_AUTH.path.parent / "models_cache.json")}
        models = sorted(set(models) | {m for m, w in windows.items() if w.get("listed")})
        return web.json_response({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "openai", **{k: v for k, v in windows.get(m, {}).items() if k != "listed"}} for m in models]})
    if tail != "chat/completions":
        return web.json_response({"error": {"message": f"codex serves chat/completions and models, not {tail!r}", "type": "invalid_request_error"}}, status=404)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": {"message": "body must be JSON", "type": "invalid_request_error"}}, status=400)
    model = str(body.get("model") or "")
    stream = bool(body.get("stream", False))
    sent = chat_to_responses(body)
    request_headers = {**headers, "accept": "text/event-stream", "content-type": "application/json"}
    if sent.get("prompt_cache_key"):
        # The CLI names its conversation in these headers as well as in the body's cache key. It wrote
        # ``session_id`` up to about 0.160; 0.163 writes ``session-id`` and ``thread-id``, both the thread.
        request_headers["session-id"] = request_headers["thread-id"] = str(sent["prompt_cache_key"])
    replayed = any(item.get("type") == "reasoning" for item in sent["input"])
    while True:
        upstream = client.build_request("POST", CODEX_BASE + "/codex/responses", json=sent, headers=request_headers)
        timing = UpstreamTiming("codex", "POST", "codex/responses", str(sent.get("model") or model))
        try:
            response = await client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            timing.report(f"unreachable:{type(exc).__name__}")
            return web.json_response({"error": {"message": f"upstream unreachable: {type(exc).__name__}", "type": "proxy_error"}}, status=502)
        timing.answered(response.status_code)
        if response.status_code < 400:
            await clients.verify("codex", response.status_code)
            break
        raw_bytes = await response.aread()
        timing.received(len(raw_bytes))
        timing.report("refused")
        raw = raw_bytes.decode("utf-8", "replace")
        await response.aclose()
        if replayed and refused_reasoning(response.status_code, raw):
            # Reasoning the backend will not read back costs the model its earlier thinking, not
            # the turn: ask once more without it, and say so, since it should not keep happening.
            logger.warning("codex refused the replayed reasoning (%s); retrying without it", raw[:200])
            sent, replayed = without_reasoning(sent), False
            continue
        await clients.verify("codex", response.status_code, raw)
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"error": {"message": raw[:400], "type": "upstream_error"}}
        return web.json_response(payload, status=response.status_code, headers=refusal_headers(response.headers))
    chunks = responses_events_to_chunks(_sse_lines(response, timing), model=model)
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
        timing.report("caller_gone")
        return web.Response(status=499)
    except httpx.HTTPError as exc:
        timing.report(f"upstream_dropped:{type(exc).__name__}")
        raise
    finally:
        timing.report()
        await response.aclose()


def codex_cached_windows(path: Path) -> dict[str, dict[str, Any]]:
    """The context windows the Codex CLI's own model catalogue states, by model slug.

    The CLI keeps what the backend told it in ``models_cache.json`` beside its login, which is mounted
    here for the login's sake: each model's ``context_window`` (the length the CLI runs it at) and
    ``max_context_window`` (the most the backend accepts). Read rather than asked again, because it
    is the CLI's request to make and this one would need to pass for the CLI to make it. ``listed`` says
    the CLI offers the model in its picker; ``reasoning_efforts`` and ``default_reasoning_effort`` are the
    efforts it offers for the model. A file that is missing or unreadable is no windows.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in (data.get("models") if isinstance(data, dict) else None) or []:
        if not isinstance(row, dict) or not row.get("slug"):
            continue
        entry: dict[str, Any] = {k: row[k] for k in ("context_window", "max_context_window") if type(row.get(k)) is int and row[k] > 0}
        efforts = [str(level["effort"]) for level in row.get("supported_reasoning_levels") or [] if isinstance(level, dict) and level.get("effort")]
        if efforts:
            entry["reasoning_efforts"] = efforts
        if isinstance(row.get("default_reasoning_level"), str) and row["default_reasoning_level"]:
            entry["default_reasoning_effort"] = row["default_reasoning_level"]
        entry["listed"] = row.get("visibility") == "list"
        out[str(row["slug"])] = entry
    return out


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


def _with_header_windows(view: dict[str, Any], taken: float) -> dict[str, Any]:
    """``view`` with its 5h and weekly windows replaced by the reply headers' when those are newer.

    The usage endpoint can refuse for hours, and a snapshot from before that is stale by the same
    hours; the reply headers move with every model call. Per-model windows (Fable) exist only in the
    endpoint's answer, so they are kept from it.
    """
    windows = _claude_header_snapshot.get("windows")
    if not windows or _claude_header_snapshot.get("at", 0.0) <= taken:
        return view
    rest = [w for w in view.get("windows") or [] if w.get("name") not in ("5h", "weekly")]
    merged = [*windows, *rest]
    return {**view, "windows": merged, "limit_reached": any(float(w.get("used_percent") or 0) >= 100 for w in merged)}


async def _claude_usage(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    """Quota for the Usage screen. The usage endpoint rate-limits independently of inference;
    a 429 is served from the last good snapshot (or profile) instead of hammering it, and the
    5h and weekly windows come from the latest model reply's headers when those are newer."""
    global _claude_usage_retry_at
    cached = _usage_cache.get("claude")
    now = time.monotonic()
    if cached and now - cached[0] < USAGE_CACHE_SECONDS:
        return _with_header_windows(cached[1], cached[0])
    if now < _claude_usage_retry_at and cached:
        return _with_header_windows(cached[1], cached[0])
    light = await CLAUDE_AUTH.usage_headers(client)
    if now < _claude_usage_retry_at:
        profile = await _claude_profile(client, light)
        return _with_header_windows(claude_usage_view({}, profile), 0.0)
    usage = await client.get(CLAUDE_API + "/api/oauth/usage", headers=light)
    if usage.status_code == 429:
        retry = usage.headers.get("retry-after")
        try:
            wait = max(CLAUDE_USAGE_BACKOFF_SECONDS, float(retry or 0))
        except ValueError:
            wait = CLAUDE_USAGE_BACKOFF_SECONDS
        _claude_usage_retry_at = now + wait
        if cached:
            return _with_header_windows(cached[1], cached[0])
        return _with_header_windows(claude_usage_view({}, await _claude_profile(client, light)), 0.0)
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
        if name not in PREPAID_UPSTREAMS and budget_exceeded() and not budget_exempt(rest) and not free_model_request(await request.read()):
            return web.json_response({"error": {"message": "daily budget exceeded; refused by the key proxy", "type": "budget_exceeded"}}, status=402)
        if key:
            inject_key(headers, name, key)
        if name == "anthropic" and "anthropic-version" not in {h.lower() for h in headers}:
            headers["anthropic-version"] = ANTHROPIC_VERSION
    body = await request.read()
    if name == "grok":
        # The CLI chat proxy routes on this header. The JSON model is not what it documents
        # as the route, and the current CLI sends the header on every completion.
        _stamp_grok_model(headers, body)
    meter = bool(AGENT_API) and METERED_HEADER not in request.headers and request.method == "POST" and is_completion(rest)
    request_model = ""
    if meter or (request.method == "POST" and is_completion(rest)):
        # Read for the timing line too: which model was slow is the first thing anyone asks of it.
        try:
            request_model = str(json.loads(body).get("model") or "")
        except (ValueError, AttributeError, UnicodeDecodeError):
            request_model = ""
    started = time.monotonic()
    captured = bytearray()
    timing = UpstreamTiming(name, request.method, rest, request_model)
    try:
        upstream = client.build_request(request.method, target_url(base, rest, request.query_string), headers=headers, content=body)
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        timing.report(f"unreachable:{type(exc).__name__}")
        return web.json_response({"error": {"message": f"upstream unreachable: {type(exc).__name__}", "type": "proxy_error"}}, status=502)
    timing.answered(response.status_code)
    if name == "grok" and request.method == "POST" and is_completion(rest):
        # The body is streamed through untouched, so only the status is judged: 426 is how the chat
        # proxy refuses a client version it no longer takes.
        await clients.verify("grok", response.status_code)
    out = web.StreamResponse(status=response.status_code)
    for k, v in response.headers.items():
        if k.lower() not in DROP_RESPONSE_HEADERS:
            out.headers[k] = v
    out.headers.update(served_headers(name, request_model))
    await out.prepare(request)
    outcome = "done"
    try:
        # aiter_bytes() decodes gzip/br on the way through; aiter_raw() would hand the client compressed bytes with the header gone.
        async for chunk in response.aiter_bytes():
            timing.received(len(chunk))
            if meter and len(captured) < CAPTURE_LIMIT:
                captured.extend(chunk)
            await out.write(chunk)
        await out.write_eof()
    except (ConnectionResetError, asyncio.CancelledError):
        outcome = "caller_gone"  # the caller went away mid-stream, or before the first byte
    except httpx.HTTPError as exc:
        # Raised on, as before: swallowed, aiohttp would end the chunked body cleanly and a stream the
        # upstream dropped would reach the caller as one that finished.
        outcome = f"upstream_dropped:{type(exc).__name__}"
        raise
    finally:
        timing.report(outcome)
        await response.aclose()
    if meter and response.status_code < 300:
        asyncio.create_task(report_direct_usage(client, name, request_model, bytes(captured), response.headers.get("content-type", ""), started))
    return out


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "upstreams": sorted(upstreams()), "subscriptions": subscriptions_status(), "budget_exceeded": budget_exceeded(), "spent_today_usd": round(spent_today(), 4) if budget_usd_per_day() > 0 else None})


def make_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app["client"] = httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=30.0, pool=10.0), limits=httpx.Limits(max_connections=64, max_keepalive_connections=16), follow_redirects=False)
    app.router.add_get("/healthz", health)
    app.router.add_get("/keys", handle_keys)
    app.router.add_put("/keys/{upstream}", handle_set_key)
    app.router.add_delete("/keys/{upstream}", handle_set_key)
    app.router.add_get("/settings", handle_settings)
    app.router.add_put("/settings/{name}", handle_set_setting)
    app.router.add_get("/subscriptions/usage", handle_subscriptions_usage)
    app.router.add_get("/clients", handle_clients)
    app.router.add_post("/clients/check", handle_clients_check)
    app.router.add_post(r"/clients/{name}/{action:update|revert}", handle_client_action)
    app.router.add_route("*", "/{upstream}/{rest:.*}", handle)

    async def start_watch(app: web.Application) -> None:
        app["client_watch"] = asyncio.create_task(clients.watch(lambda: app["client"]), name="client-releases")

    async def close(app: web.Application) -> None:
        watch = app.get("client_watch")
        if watch is not None:
            watch.cancel()
        await app["client"].aclose()

    app.on_startup.append(start_watch)
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
