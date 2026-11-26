"""Google Antigravity (agy) login as a key-proxy upstream.

The CLI stores OAuth tokens in ``~/.gemini/antigravity-cli/antigravity-oauth-token``.
This module reads and refreshes them through Google's OAuth token endpoint, with the OAuth
client of the installed agy, read out of its binary (no client is written down here), resolves the
account's Cloud Code project via ``v1internal:loadCodeAssist``, lists models and quota
windows via ``v1internal:fetchAvailableModels``, and translates OpenAI chat completions
into ``v1internal:streamGenerateContent?alt=sse`` and back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mmap
import os
import re
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import clients
import httpx
from subscriptions import KEEPALIVE, SubscriptionError, _write_json, failure_chunk

logger = logging.getLogger("keyproxy.antigravity")

ANTIGRAVITY_BASE = "https://daily-cloudcode-pa.googleapis.com"
ANTIGRAVITY_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_PROJECT = "aicode-consumers"
REFRESH_SKEW_SECONDS = 300
REASONING_FORMAT = "google-gemini-v1"
EFFORT_BUDGET = {"low": 2048, "medium": 8192, "high": 16384, "xhigh": 32768, "max": 32768}

ANTIGRAVITY_FALLBACK_MODELS = (
    "gemini-3-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash-tiered",
    "gemini-4-hydrogen-sonar",
    "claude-sonnet-5-5-medium",
    "claude-opus-5-5-medium",
    "gpt-oss-120b-medium",
)


CLIENT_FILE = "daedalus-oauth-client.json"
"""Beside the login: the OAuth client a refresh last succeeded with, so a restart does not scan the CLI again."""
CLIENT_ID_SUFFIX = b".apps.googleusercontent.com"
CLIENT_ID_PATTERN = re.compile(rb"([0-9]{6,})-[a-z0-9]{32}\Z")
CLIENT_SECRET_PREFIX = b"GOCSPX-"
CLIENT_SECRET_BODY = re.compile(rb"[A-Za-z0-9_-]{28}")
CLIENT_REJECTIONS = frozenset({"invalid_client", "unauthorized_client"})
"""Google's answers to a refresh signed by the wrong client: try the next one. Anything else is about the login itself."""
MAX_CLIENT_TRIALS = 24
SHORTEST_PROJECT_NUMBER = 12
CLIENT_MISSING = (
    "antigravity: cannot refresh the login, the Antigravity CLI's OAuth client was not found "
    "(not in the login file, and no agy binary to read it from); install or update agy on the host and sign in with it again"
)


class OAuthClient(tuple[str, str]):
    """``(client_id, client_secret)``; its ``repr`` keeps the secret out of logs and test reports."""

    __slots__ = ()

    def __new__(cls, client_id: str, secret: str) -> OAuthClient:
        return super().__new__(cls, (client_id, secret))

    def __repr__(self) -> str:
        return f"OAuthClient({self[0][:6]}…, ***)"


_verified_client: OAuthClient | None = None
"""The client the last refresh succeeded with. One CLI, one client: every account's login shares it."""
_cli_scans: dict[tuple[str, int, int], list[OAuthClient]] = {}
"""Per binary (path, size, mtime): the pairs read out of it. An update of agy changes the key and scans afresh."""


def client_in_login(data: dict[str, Any]) -> OAuthClient | None:
    """The client stored with the tokens, when the CLI writes one there (some Google CLIs do; agy 1.5 does not)."""
    for holder in (data, data.get("token") or {}):
        if not isinstance(holder, dict):
            continue
        client_id = holder.get("client_id") or holder.get("clientId")
        secret = holder.get("client_secret") or holder.get("clientSecret")
        if isinstance(client_id, str) and isinstance(secret, str) and client_id and secret:
            return OAuthClient(client_id, secret)
    return None


def cli_binaries(login: Path) -> list[Path]:
    """Where the agy binary may be, most specific first; only the ones that exist.

    ``KEYPROXY_ANTIGRAVITY_CLI`` names it outright. ``bin/agentapi`` in the CLI's data folder is a
    shell shim agy writes with the binary's absolute path in it, which is right on a native install.
    In the compose stack that path is the host's and means nothing in here, so the stack mounts the
    host's ``~/.local/bin`` (where agy's installer puts it) at this container's home.
    """
    found: list[Path] = []
    explicit = os.environ.get("KEYPROXY_ANTIGRAVITY_CLI", "")
    if explicit:
        found.append(Path(explicit))
    data_dirs = [login.parent]
    # An account's login sits in <data dir>/daedalus-accounts/<id>/, and agy's shim is in the default one.
    if login.parent.parent.name == "daedalus-accounts":
        data_dirs.append(login.parent.parent.parent)
    for data_dir in data_dirs:
        try:
            shim = (data_dir / "bin" / "agentapi").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r'exec\s+"([^"]+)"', shim) or re.search(r"exec\s+(\S+)", shim)
        if match:
            found.append(Path(match.group(1)))
    found.append(Path.home() / ".local" / "bin" / "agy")
    which = shutil.which("agy")
    if which:
        found.append(Path(which))
    unique: list[Path] = []
    for path in found:
        try:
            if path.is_file() and path not in unique:
                unique.append(path)
        except OSError:
            continue
    return unique


def _scan(view: Any, anchor: bytes) -> list[int]:
    offsets, at = [], view.find(anchor)
    while at != -1:
        offsets.append(at)
        at = view.find(anchor, at + 1)
    return offsets


def scan_cli(binary: Path) -> list[OAuthClient]:
    """The OAuth client pairs worth trying from ``binary``, most likely first.

    agy is a Go binary: its string literals are packed end to end with no separator, grouped by the
    linker rather than by the code that uses them, so the id and the secret of one client are not
    near each other and a greedy pattern runs into the next string. Hence fixed-length matching (a
    Google secret is ``GOCSPX-`` and 28 characters, an id's hash 32), and pairs ranked rather than
    known: the id and secret of the same rank first (which is how agy lays them out today), then
    every other combination, then each id without a leading digit a neighbouring string may have
    lent it. The refresh tries them in this order and Google says which one is right.
    """
    with binary.open("rb") as handle:
        try:
            view = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:
            return []
        with view:
            # bytes.find on the mapping is a memchr-speed scan; a regex over 270 MB with a leading
            # repetition took seconds.
            ids: list[str] = []
            stripped: list[str] = []
            for at in _scan(view, CLIENT_ID_SUFFIX):
                head = view[max(0, at - 80):at]
                match = CLIENT_ID_PATTERN.search(head)
                if not match:
                    continue
                client_id = match.group().decode() + CLIENT_ID_SUFFIX.decode()
                if client_id not in ids:
                    ids.append(client_id)
                digits = match.group(1)
                for drop in range(1, len(digits) - SHORTEST_PROJECT_NUMBER + 1):
                    variant = client_id[drop:]
                    if variant not in stripped:
                        stripped.append(variant)
            secrets: list[str] = []
            for at in _scan(view, CLIENT_SECRET_PREFIX):
                body = view[at + len(CLIENT_SECRET_PREFIX):at + len(CLIENT_SECRET_PREFIX) + 28]
                if CLIENT_SECRET_BODY.fullmatch(body):
                    secret = (CLIENT_SECRET_PREFIX + body).decode()
                    if secret not in secrets:
                        secrets.append(secret)
    pairs: list[OAuthClient] = [OAuthClient(i, s) for i, s in zip(ids, secrets, strict=False)]
    pairs += [OAuthClient(i, s) for i in ids for s in secrets if OAuthClient(i, s) not in pairs]
    pairs += [OAuthClient(i, s) for i in stripped if i not in ids for s in secrets]
    return pairs


def cli_clients(binary: Path) -> list[OAuthClient]:
    """:func:`scan_cli`, once per version of the binary."""
    stat = binary.stat()
    key = (str(binary), stat.st_size, stat.st_mtime_ns)
    if key not in _cli_scans:
        _cli_scans[key] = scan_cli(binary)
    return _cli_scans[key]


def _client_rejected(response: httpx.Response) -> bool:
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return False
    return isinstance(error, str) and error in CLIENT_REJECTIONS


def antigravity_user_agent() -> str:
    """The Antigravity CLI User-Agent expected by daily-cloudcode-pa."""
    version = clients.version("antigravity")
    return f"antigravity/cli/{version} (aidev_client; os_type=linux; arch=amd64; cl=1026970498; auth_method=consumer)"


class AntigravityAuth:
    """``~/.gemini/antigravity-cli/antigravity-oauth-token``: Google OAuth token pair."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._project_cache: tuple[str, str] | None = None

    def available(self) -> bool:
        return self.path.is_file()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SubscriptionError(f"antigravity login unreadable at {self.path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("token"), dict):
            raise SubscriptionError("antigravity: not logged in (run `agy` on the host)")
        return data

    @staticmethod
    def _expires(token_obj: dict[str, Any]) -> float:
        raw = str(token_obj.get("expiry") or "")
        if not raw:
            return 0.0
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    async def token(self, client: httpx.AsyncClient) -> str:
        data = self._load()
        tok = dict(data["token"])
        access = str(tok.get("access_token") or "")
        if not access:
            raise SubscriptionError("antigravity: not logged in (run `agy` on the host)")
        expires = self._expires(tok)
        if expires and expires - REFRESH_SKEW_SECONDS > time.time():
            return access
        async with self._lock:
            data = self._load()
            tok = dict(data["token"])
            access = str(tok.get("access_token") or "")
            expires = self._expires(tok)
            if expires and expires - REFRESH_SKEW_SECONDS > time.time():
                return access
            refresh = str(tok.get("refresh_token") or "")
            if not refresh:
                raise SubscriptionError("antigravity: missing refresh_token (run `agy` on the host)")
            fresh = await self._refresh(client, data, refresh)
            tok["access_token"] = fresh.get("access_token") or access
            if fresh.get("refresh_token"):
                tok["refresh_token"] = fresh["refresh_token"]
            if fresh.get("expires_in"):
                expires_in = int(fresh["expires_in"])
                tok["expires_in"] = expires_in
                tok["expiry"] = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat().replace("+00:00", "Z")
            data["token"] = tok
            _write_json(self.path, data)
            logger.warning("antigravity token refreshed")
            return str(tok["access_token"])

    @property
    def client_file(self) -> Path:
        return self.path.parent / CLIENT_FILE

    def _stored_client(self) -> OAuthClient | None:
        try:
            data = json.loads(self.client_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return client_in_login(data) if isinstance(data, dict) else None

    def _remember(self, found: OAuthClient) -> None:
        global _verified_client
        _verified_client = found
        if self._stored_client() == found:
            return
        try:
            _write_json(self.client_file, {"client_id": found[0], "client_secret": found[1]})
        except OSError as exc:
            # A read-only data folder costs one scan of the CLI per process, nothing more.
            logger.warning("antigravity: could not keep the OAuth client beside the login: %s", exc)

    def _forget(self, found: OAuthClient) -> None:
        global _verified_client
        if _verified_client == found:
            _verified_client = None
        if self._stored_client() == found:
            try:
                self.client_file.unlink()
            except OSError:
                pass

    async def _candidates(self, data: dict[str, Any]) -> AsyncIterator[tuple[OAuthClient, bool]]:
        """Each client worth a refresh, cheapest source first, with whether it is a remembered one.

        Lazy on purpose: the binary is only read when nothing remembered works, which is once per
        update of agy rather than once per refresh."""
        in_login = client_in_login(data)
        if in_login is not None:
            yield in_login, False
        for remembered in (_verified_client, self._stored_client()):
            if remembered is not None:
                yield remembered, True
        for binary in cli_binaries(self.path):
            try:
                pairs = await asyncio.to_thread(cli_clients, binary)
            except OSError as exc:
                logger.warning("antigravity: cannot read %s for its OAuth client: %s", binary, exc)
                continue
            for pair in pairs:
                yield pair, False

    async def _refresh(self, client: httpx.AsyncClient, data: dict[str, Any], refresh: str) -> dict[str, Any]:
        """Refresh with the CLI's own OAuth client, found rather than written down here.

        The client used to be a literal in this file, copied out of agy; that put a Google client
        secret into a public repository, and it went stale whenever agy changed its client."""
        tried: set[OAuthClient] = set()
        last = ""
        async for found, remembered in self._candidates(data):
            if found in tried:
                continue
            if len(tried) >= MAX_CLIENT_TRIALS:
                break
            tried.add(found)
            response = await client.post(
                ANTIGRAVITY_TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": found[0], "client_secret": found[1]},
                headers={"accept": "application/json"},
            )
            if response.status_code == 200:
                self._remember(found)
                return response.json()
            if _client_rejected(response):
                last = f"HTTP {response.status_code} {response.text[:160]}"
                if remembered:
                    self._forget(found)
                continue
            raise SubscriptionError(f"antigravity token refresh failed: HTTP {response.status_code} {response.text[:160]}")
        if not tried:
            raise SubscriptionError(CLIENT_MISSING)
        raise SubscriptionError(
            f"antigravity: Google refused every OAuth client found for the Antigravity CLI ({len(tried)} tried, last {last}); "
            "update agy on the host and sign in with it again"
        )

    async def headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        return {
            "authorization": f"Bearer {await self.token(client)}",
            "content-type": "application/json",
            "user-agent": antigravity_user_agent(),
        }

    async def project_and_tier(self, client: httpx.AsyncClient, headers: dict[str, str] | None = None) -> tuple[str, str]:
        """Resolve and cache ``(cloudaicompanionProject, tier_name)`` via ``loadCodeAssist``."""
        if self._project_cache is not None:
            return self._project_cache
        hdrs = headers or await self.headers(client)
        try:
            response = await client.post(
                f"{ANTIGRAVITY_BASE}/v1internal:loadCodeAssist",
                headers=hdrs,
                json={"metadata": {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}},
            )
            if response.status_code == 200:
                payload = response.json()
                project = str(payload.get("cloudaicompanionProject") or DEFAULT_PROJECT)
                tier_obj = payload.get("paidTier") or payload.get("currentTier") or {}
                tier = str(tier_obj.get("name") or tier_obj.get("id") or "Antigravity")
                self._project_cache = (project, tier)
                return self._project_cache
        except (httpx.HTTPError, ValueError):
            pass
        return DEFAULT_PROJECT, "Antigravity"


# -- models & usage -------------------------------------------------------------------


async def fetch_antigravity_models(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.post(f"{ANTIGRAVITY_BASE}/v1internal:fetchAvailableModels", headers=headers, json={})
    if response.status_code != 200:
        raise SubscriptionError(f"antigravity models: HTTP {response.status_code}")
    data = response.json()
    return data if isinstance(data, dict) else {}


def antigravity_models_view(data: dict[str, Any] | None) -> dict[str, Any]:
    """Convert ``fetchAvailableModels`` output into an OpenAI ``/v1/models`` response."""
    models_map = (data or {}).get("models")
    rows: list[dict[str, Any]] = []
    if isinstance(models_map, dict) and models_map:
        for model_id, info in sorted(models_map.items()):
            if not isinstance(info, dict) or model_id.startswith("tab_") or model_id.endswith("-disabled"):
                continue
            entry: dict[str, Any] = {
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "google",
            }
            if isinstance(info.get("maxTokens"), int) and info["maxTokens"] > 0:
                entry["context_length"] = info["maxTokens"]
            if isinstance(info.get("maxOutputTokens"), int) and info["maxOutputTokens"] > 0:
                entry["max_output_tokens"] = info["maxOutputTokens"]
            entry["input_modalities"] = ["text", "image"] if info.get("supportsImages") else ["text"]
            if info.get("supportsThinking"):
                entry["supported_parameters"] = ["reasoning"]
            rows.append(entry)
    if not rows:
        rows = [{"id": m, "object": "model", "created": 0, "owned_by": "google"} for m in ANTIGRAVITY_FALLBACK_MODELS]
    return {"object": "list", "data": rows}


def antigravity_usage_view(data: dict[str, Any], tier: str = "") -> dict[str, Any]:
    """Build quota windows from ``fetchAvailableModels`` ``quotaInfo`` entries."""
    models_map = data.get("models") if isinstance(data, dict) else None
    buckets: dict[str, tuple[float, str | None]] = {}
    if isinstance(models_map, dict):
        for model_id, info in models_map.items():
            if not isinstance(info, dict) or model_id.startswith("tab_") or model_id.endswith("-disabled"):
                continue
            qi = info.get("quotaInfo")
            # The answer is proto3 JSON, which leaves a zero out: a window that is used up arrives as
            # ``{"resetTime": …}`` with no ``remainingFraction`` at all. Reading that as "no quota
            # here" dropped the Gemini windows from the usage at exactly the moment they ran out. A
            # quota with a reset and no fraction is an empty one; one with neither says nothing.
            if not isinstance(qi, dict) or ("remainingFraction" not in qi and not qi.get("resetTime")):
                continue
            if model_id.startswith("gemini-4-argon") or model_id == "gemini-pro-orchestrator-g1-pro":
                family = "Gemini 4 Argon (weekly)"
            elif model_id.startswith("gemini"):
                family = "Gemini (5h)"
            else:
                family = "Claude / GPT (5h)"
            frac = float(qi.get("remainingFraction") or 0.0)
            reset = qi.get("resetTime")
            prev = buckets.get(family)
            if prev is None or frac < prev[0]:
                buckets[family] = (frac, str(reset) if reset else None)
    order = ("Gemini (5h)", "Gemini 4 Argon (weekly)", "Claude / GPT (5h)")
    windows = [
        {"name": family, "used_percent": round(max(0.0, min(100.0, (1.0 - buckets[family][0]) * 100.0)), 1), "resets_at": buckets[family][1]}
        for family in order
        if family in buckets
    ]
    limit_reached = any(w["used_percent"] >= 100.0 for w in windows)
    return {"provider": "antigravity", "plan": tier or "Antigravity", "limit_reached": limit_reached, "windows": windows}


# -- OpenAI chat completions <-> v1internal Gemini -----------------------------------


_STRIP_SCHEMA_KEYS = frozenset({"$schema", "additionalProperties", "strict", "$id", "$defs", "definitions"})


def _clean_schema(schema: Any, *, root: bool = False) -> Any:
    """Strip JSON Schema keywords rejected by Cloud Code / Vertex validators and ensure root object type."""
    if isinstance(schema, list):
        return [_clean_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}} if root else schema
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in _STRIP_SCHEMA_KEYS:
            continue
        out[k] = _clean_schema(v)
    if root:
        out.setdefault("type", "object")
        if out.get("type") == "object":
            out.setdefault("properties", {})
    return out


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def _user_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else [{"text": ""}]
    parts: list[dict[str, Any]] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = str(block.get("text") or "")
            if text:
                parts.append({"text": text})
        elif btype == "image_url":
            url = str((block.get("image_url") or {}).get("url") or "")
            if url.startswith("data:") and ";base64," in url:
                header, b64 = url.split(";base64,", 1)
                mime = header[5:] or "image/png"
                parts.append({"inlineData": {"mimeType": mime, "data": b64}})
    return parts or [{"text": ""}]


def _signatures_of(details: Any) -> dict[str, str]:
    sigs: dict[str, str] = {}
    if not isinstance(details, list):
        return sigs
    for entry in details:
        if isinstance(entry, dict) and entry.get("format") == REASONING_FORMAT and entry.get("id") and entry.get("data"):
            sigs[str(entry["id"])] = str(entry["data"])
    return sigs


def chat_to_antigravity(body: dict[str, Any], *, project: str) -> dict[str, Any]:
    """Translate an OpenAI ``/v1/chat/completions`` body to Antigravity ``v1internal``."""
    model = str(body.get("model") or "gemini-3-flash")
    system_texts: list[str] = []
    contents: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        if role in ("system", "developer"):
            text = _text_of(msg.get("content"))
            if text:
                system_texts.append(text)
            continue
        if role == "user":
            parts = _user_parts(msg.get("content"))
            if contents and contents[-1]["role"] == "user":
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": "user", "parts": parts})
            continue
        if role == "assistant":
            parts = []
            sigs = _signatures_of(msg.get("reasoning_details"))
            text = _text_of(msg.get("content"))
            if text:
                parts.append({"text": text})
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tc_id = str(tc.get("id") or "") or f"call_{uuid.uuid4().hex[:24]}"
                fn = tc.get("function") or {}
                fn_name = str(fn.get("name") or "")
                if fn_name:
                    call_names[tc_id] = fn_name
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except (ValueError, TypeError):
                    args = {}
                # The id pairs a call with its result. Claude behind this endpoint refuses a tool_use
                # without one ("tool_use.id: Field required"), and Gemini accepts it, so it is always sent.
                part: dict[str, Any] = {"functionCall": {"name": fn_name, "args": args, "id": tc_id}}
                sig = sigs.get(tc_id)
                if sig:
                    part["thoughtSignature"] = sig
                elif not model.startswith("claude-"):
                    # History carried over from another model (e.g. Claude/Codex) lacks Gemini thought signatures.
                    part["thoughtSignature"] = "skip_thought_signature_validator"
                parts.append(part)
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue
        if role == "tool":
            tc_id = str(msg.get("tool_call_id") or "")
            fn_name = str(msg.get("name") or call_names.get(tc_id) or "tool")
            text = _text_of(msg.get("content"))
            fr_part: dict[str, Any] = {"functionResponse": {"name": fn_name, "response": {"result": text}, "id": tc_id}}
            if contents and contents[-1]["role"] == "user":
                contents[-1]["parts"].append(fr_part)
            else:
                contents.append({"role": "user", "parts": [fr_part]})

    req: dict[str, Any] = {"contents": contents or [{"role": "user", "parts": [{"text": ""}]}]}
    if system_texts:
        req["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_texts)}]}

    tools_in = body.get("tools") or []
    decls = []
    for tool in tools_in:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function") or {}
        decl: dict[str, Any] = {
            "name": str(fn.get("name") or ""),
            "parametersJsonSchema": _clean_schema(fn.get("parameters"), root=True),
        }
        if fn.get("description"):
            decl["description"] = str(fn["description"])
        decls.append(decl)
    if decls:
        req["tools"] = [{"functionDeclarations": decls}]

    tool_choice = body.get("tool_choice")
    unforceable_claude = model.startswith(("claude-opus-5-5", "claude-sonnet-5-5"))
    if tool_choice is not None and decls:
        if tool_choice == "auto":
            req["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        elif tool_choice == "none":
            req["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
        elif tool_choice == "required":
            if unforceable_claude:
                req["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
                req["contents"][-1]["parts"].append({"text": "[Call one of the provided tools next; do not answer in plain text.]"})
            else:
                req["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
        elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            fname = str((tool_choice.get("function") or {}).get("name") or "")
            if fname:
                if unforceable_claude:
                    req["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
                    req["contents"][-1]["parts"].append({"text": f"[Call the `{fname}` tool next; do not answer in plain text.]"})
                else:
                    req["toolConfig"] = {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [fname]}}

    gen_cfg: dict[str, Any] = {}
    if body.get("temperature") is not None:
        gen_cfg["temperature"] = float(body["temperature"])
    max_tok = body.get("max_completion_tokens") or body.get("max_tokens")
    if max_tok is not None:
        gen_cfg["maxOutputTokens"] = int(max_tok)
    stop = body.get("stop")
    if isinstance(stop, str) and stop:
        gen_cfg["stopSequences"] = [stop]
    elif isinstance(stop, list) and stop:
        gen_cfg["stopSequences"] = [str(s) for s in stop if s]

    effort = str(body.get("reasoning_effort") or "").lower()
    if effort in EFFORT_BUDGET:
        gen_cfg["thinkingConfig"] = {"includeThoughts": True, "thinkingBudget": EFFORT_BUDGET[effort]}
    elif effort == "none":
        gen_cfg["thinkingConfig"] = {"includeThoughts": False, "thinkingBudget": 0}
    if gen_cfg:
        req["generationConfig"] = gen_cfg

    return {
        "project": project,
        "model": model,
        "request": req,
        "userAgent": "antigravity",
        "requestType": "agent",
        "requestId": f"agent-{uuid.uuid4()}",
    }


def _sse_line(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def antigravity_sse_to_chunks(lines: AsyncIterator[str], model: str, completion_id: str) -> AsyncIterator[str]:
    """Translate Antigravity ``v1internal:streamGenerateContent?alt=sse`` into OpenAI SSE."""
    created = int(time.time())
    tool_index = 0
    saw_tool_calls = False
    last_usage: dict[str, Any] | None = None
    finish_sent = False

    def chunk(delta: dict[str, Any], finish_reason: str | None = None, usage: dict[str, Any] | None = None) -> str:
        body: dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            body["usage"] = usage
        return _sse_line(body)

    yield chunk({"role": "assistant", "content": ""})
    async for raw in lines:
        if not raw.startswith("data:"):
            continue
        data_str = raw[5:].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            event = json.loads(data_str)
        except ValueError:
            continue
        if isinstance(event, dict) and "error" in event:
            err = event["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            yield failure_chunk(completion_id, model, str(msg or "upstream error"))
            yield "data: [DONE]\n\n"
            return
        resp = event.get("response") if isinstance(event, dict) else None
        if not isinstance(resp, dict):
            continue
        um = resp.get("usageMetadata")
        if isinstance(um, dict):
            prompt_tok = int(um.get("promptTokenCount") or 0)
            cand_tok = int(um.get("candidatesTokenCount") or 0)
            thought_tok = int(um.get("thoughtsTokenCount") or 0)
            cached_tok = int(um.get("cachedContentTokenCount") or 0)
            last_usage = {
                "prompt_tokens": prompt_tok,
                "completion_tokens": cand_tok + thought_tok,
                "total_tokens": int(um.get("totalTokenCount") or (prompt_tok + cand_tok + thought_tok)),
            }
            if thought_tok:
                last_usage["completion_tokens_details"] = {"reasoning_tokens": thought_tok}
            if cached_tok:
                last_usage["prompt_tokens_details"] = {"cached_tokens": cached_tok}

        for cand in resp.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            parts = (cand.get("content") or {}).get("parts") or []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("thought") and "text" in part:
                    text = str(part.get("text") or "")
                    if text:
                        yield chunk({"reasoning_content": text})
                elif "text" in part:
                    text = str(part.get("text") or "")
                    if text:
                        yield chunk({"content": text})
                elif "functionCall" in part:
                    fc = part["functionCall"] or {}
                    call_id = f"call_{uuid.uuid4().hex[:24]}"
                    name = str(fc.get("name") or "")
                    args_str = json.dumps(fc.get("args") or {})
                    delta: dict[str, Any] = {
                        "tool_calls": [{
                            "index": tool_index,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": args_str},
                        }]
                    }
                    sig = part.get("thoughtSignature")
                    if sig:
                        delta["reasoning_details"] = [{
                            "type": "reasoning.encrypted",
                            "format": REASONING_FORMAT,
                            "id": call_id,
                            "data": str(sig),
                        }]
                    tool_index += 1
                    saw_tool_calls = True
                    yield chunk(delta)
            fr = cand.get("finishReason")
            if fr:
                mapped = "tool_calls" if saw_tool_calls else {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter", "RECITATION": "content_filter"}.get(str(fr), "stop")
                yield chunk({}, finish_reason=mapped, usage=last_usage)
                finish_sent = True

    if not finish_sent:
        yield chunk({}, finish_reason="tool_calls" if saw_tool_calls else "stop", usage=last_usage)
    yield "data: [DONE]\n\n"


__all__ = [
    "ANTIGRAVITY_BASE",
    "ANTIGRAVITY_FALLBACK_MODELS",
    "AntigravityAuth",
    "KEEPALIVE",
    "OAuthClient",
    "REASONING_FORMAT",
    "antigravity_sse_to_chunks",
    "antigravity_usage_view",
    "antigravity_user_agent",
    "chat_to_antigravity",
    "cli_binaries",
    "client_in_login",
    "scan_cli",
    "fetch_antigravity_models",
    "antigravity_models_view",
]
