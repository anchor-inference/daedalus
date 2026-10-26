"""OpenAI-compatible ``ILLMProvider`` adapter.

One adapter serves every configured endpoint (DeepSeek, OpenRouter, a self-hosted
vLLM or llama.cpp, any other ``chat/completions`` server). Vendor differences are confined to
:class:`ProviderEndpoint` flags: how thinking is switched on, which field carries
the reasoning stream, and how usage is shaped.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from protocore.contracts.llm import (
    ILLMProvider,
    LLMContextWindowExceeded,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMResponseUsage,
    LLMTimeoutError,
    ProviderDelta,
    ProviderDeltaKind,
    ProviderFailure,
    ProviderFailureReason,
    classify_transport_exception,
)
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock
from protocore.providers.openai_compat import ReasoningReplay
from protocore.providers.openai_compat.wire import reasoning_envelope, replayed_details

from daedalus import __version__
from daedalus.config import keyproxy_upstream
from daedalus.providers.admission import InferenceAdmission
from daedalus.providers.context_window import stated_window
from daedalus.providers.dsml import DsmlGuard
from daedalus.providers.failure_evidence import FailureEvidence, failure_evidence
from daedalus.providers.llamacpp import tools_to_llamacpp_wire
from daedalus.providers.pricing import ModelPricing, complete_usage
from daedalus.providers.wire import (
    arguments_cut,
    images_left_out,
    messages_to_wire,
    parse_json_arguments,
    place_carried_images,
    tools_to_wire,
)

logger = logging.getLogger(__name__)

ImageLoader = Callable[[str], Awaitable[tuple[bytes, str]]]

_CONTEXT_OVERFLOW_SIZES = re.compile(
    r"maximum context length is (?P<window>\d+) tokens.*?requested (?:a total of )?\d+ tokens"
    r"(?: \((?P<prompt>\d+) (?:tokens )?in the messages, (?P<output>\d+) (?:tokens )?in the completion\))?",
    re.IGNORECASE | re.DOTALL,
)


def context_overflow_sizes(text: str) -> dict[str, int]:
    """The window, prompt and requested output a context refusal states, for the core's retry.

    vLLM and OpenAI-shaped servers say "maximum context length is 256000 tokens. However, you requested
    256023 tokens (190487 in the messages, 65536 in the completion)". The core sizes its one corrective
    retry from these numbers when it has them — the output cap it can still afford — and otherwise
    guesses from its own estimate, which is what ran short in the first place.
    """
    match = _CONTEXT_OVERFLOW_SIZES.search(text)
    if match is None:
        return {}
    sizes = {"context_window": int(match.group("window"))}
    if match.group("prompt") is not None:
        sizes["input_tokens"] = int(match.group("prompt"))
        sizes["requested_output_tokens"] = int(match.group("output"))
    return sizes


_CONTEXT_ERROR_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "reduce the length",
    "prompt is too long",
    "exceeds the model",
)


_PERMANENT_ERROR_TYPES = {
    "invalid_request_error": "format_error",
    "authentication_error": "auth",
    "permission_error": "auth",
    "billing_error": "billing",
    "not_found_error": "model_not_found",
}
"""The error ``type`` a provider writes into the body, for the refusals asking again cannot change.

Read from the body as well as the status because a stream reports its error in a chunk, after the 200."""

_EXHAUSTED_ERROR_TYPES = {"gousagelimiterror", "usage_limit_reached", "usage_not_included"}
"""Error types that mean the account has spent its allowance, carried on a 429 that looks like a blip.

The reset is hours or days away, so retrying the same endpoint only burns backoff; the verdict is
permanent for this run and the chain moves on. ``usage_limit_reached`` is the ChatGPT plan's spent window
behind the key proxy's Codex route, which the Codex CLI itself never retries."""

_EXHAUSTED_ERROR_CODES = {
    "insufficient_quota",
    "billing_hard_limit_reached",
    "billing_not_active",
    "usage_not_included",
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
}
"""Error codes that mean the account has no allowance left, whatever status or stream chunk carried them."""

STREAM_TRUNCATED = "stream_truncated"
"""The error code the key proxy writes when its upstream ended a stream before the final event."""

_STREAM_ERROR_STATUS = {
    "rate_limit_exceeded": 429,
    "slow_down": 429,
    "server_is_overloaded": 529,
    "context_length_exceeded": 400,
}
"""The HTTP status a named error code stands for when it arrives in a stream chunk, after the 200.

Without it every such chunk was classified as a 500: a rate limit was retried as a server error with no
regard for its wait, and a context overflow was retried unchanged instead of being shrunk."""


def stream_error_status(error: Any) -> int:
    """The status to classify a mid-stream error object by: its own when it names one, else its code's."""
    if not isinstance(error, dict):
        return 500
    code = error.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599:
        return code  # OpenRouter writes the upstream's status here
    if isinstance(code, str) and code.isdigit() and 400 <= int(code) <= 599:
        return int(code)
    return _STREAM_ERROR_STATUS.get(str(code or ""), 500)


def _retry_after_seconds(headers: Any, text: str) -> float | None:
    """The wait a refusal states in seconds: the ``Retry-After`` header, else the body's ``retry_after_seconds``."""
    value = headers.get("retry-after") if headers is not None else None
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = 0.0
        if seconds > 0:
            return seconds
    try:
        data = json.loads(text)
    except ValueError:
        return None
    error = data.get("error", data) if isinstance(data, dict) else None
    stated = error.get("retry_after_seconds") if isinstance(error, dict) else None
    if isinstance(stated, (int, float)) and not isinstance(stated, bool) and stated > 0:
        return float(stated)
    return None

_PERMANENT_STATUS = {401: "auth", 402: "billing", 403: "auth", 404: "model_not_found"}

_MODEL_REFUSAL_MARKERS = ("version_too_old", "does not support this model", "model_not_found", "model is not supported")
"""A refusal of the model rather than of the request. Reported as an unknown model, not a malformed request,
because another model of the fallback chain may be served where this one is not — a server that considers
the client too old for one model is the case that made this list. The ChatGPT backend behind the key proxy
answers "The 'gpt-6-sol' model is not supported when using Codex with a ChatGPT account" for a model the
plan does not include; filed as a malformed request, it failed the run instead of moving down the chain."""


def _error_fields(text: str) -> tuple[str, str, str]:
    """``(type, code, message)`` from an error body; empty strings for what it does not say.

    Accepts the wrapped shape (``{"error": {...}}``) the status path reads and the bare error object a stream
    chunk carries.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return "", "", ""
    if not isinstance(data, dict):
        return "", "", ""
    error = data.get("error", data)
    if error is data and isinstance(data.get("detail"), str):
        # FastAPI-shaped servers, the ChatGPT backend among them, answer ``{"detail": "…"}``.
        return "", "", data["detail"]
    if isinstance(error, str):
        return "", "", error
    if not isinstance(error, dict):
        return "", "", ""
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    code = details.get("error_code") or error.get("code") or ""
    return str(error.get("type") or ""), str(code), str(error.get("message") or "")


def _verdict(reason: str, retryable: bool) -> ProviderFailure:
    return ProviderFailure(reason=ProviderFailureReason(reason), retryable=retryable)


def classify_failure(status: int, text: str) -> ProviderFailure:
    """The core's verdict on a request the endpoint answered with ``status`` and ``text``, which the
    loop reads to retry, step down the fallback chain or give up.

    Rate limits, timeouts, overload and server errors pass on their own and stay retryable. Every other 4xx
    is the request or the account, and so is an error body that names one of those types whatever status
    carried it. Without a verdict every refusal looked like a blip: a provider that rejected the request
    outright was retried, and the operator waited through the backoff for an error already known.
    """
    kind, code, message = _error_fields(text)
    lowered = f"{code} {message}".lower()
    if status in (401, 403):
        return _verdict("auth", False)
    if status == 402 or code in _EXHAUSTED_ERROR_CODES or kind.lower() in _EXHAUSTED_ERROR_TYPES:
        return _verdict("billing", False)
    if status == 429 or kind == "rate_limit_error":
        return _verdict("rate_limit", True)
    if status in (408, 504):
        return _verdict("timeout", True)
    if status == 529 or kind == "overloaded_error":
        return _verdict("overloaded", True)
    permanent = kind in _PERMANENT_ERROR_TYPES or (400 <= status < 500)
    if not permanent:
        return _verdict("server_error", True)
    if any(marker in lowered for marker in _MODEL_REFUSAL_MARKERS):
        return _verdict("model_not_found", False)
    reason = _PERMANENT_STATUS.get(status) or _PERMANENT_ERROR_TYPES.get(kind) or "format_error"
    return _verdict(reason, False)


def failure_text(endpoint_id: str, status: int, text: str) -> str:
    """The error as the operator reads it: the provider's own sentence, not the JSON around it."""
    _kind, code, message = _error_fields(text)
    if not message:
        return f"{endpoint_id}: HTTP {status}: {text[:500]}"
    said = f"{endpoint_id}: HTTP {status}: {message[:500]}"
    # A numeric code only repeats the status (llama.cpp writes one); a named one is what to search for.
    return f"{said} ({code})" if code and not code.isdigit() else said


def message_shape(messages: list[dict[str, Any]]) -> str:
    """One token per wire message — role, and for an assistant turn whether it has text, tool calls and
    reasoning — so a rejected request can be read from the log without its content."""
    parts = []
    for m in messages:
        role = str(m.get("role"))[0]
        if m.get("role") == "assistant":
            role += ("t" if m.get("content") else "") + ("c" if m.get("tool_calls") else "") + ("r" if m.get("reasoning_content") else ("R" if "reasoning_content" in m else "")) + ("e" if m.get("reasoning_details") else "")
        parts.append(role)
    return " ".join(parts)


REASONING_REPLAY_UPSTREAMS = ("codex", "claude")
"""Key-proxy upstreams whose ``reasoning_details`` are kept and sent back.

The Codex route turns the Responses API's encrypted reasoning into these entries and back, so a
reasoning model keeps its thinking across tool steps the way it does under the Codex CLI. The Claude
route does the same with Anthropic's signed thinking blocks, which Claude Code sends back on every
request; without them each tool step of a long task re-derived its plan from the raw tool outputs and
spent most of a minute thinking before a single grep. The core sends the entries back only to the
endpoint and model that wrote them, which is where Claude Code drops them too: after a model switch.
Nothing else is sent them: an OpenAI-compatible server that does not know the field may refuse a
message carrying it, and OpenRouter, which defines the field, merges streamed fragments by rules this
adapter has not been checked against."""

CACHE_KEY_UPSTREAMS = ("codex", "openai")
"""Key-proxy upstreams that take ``prompt_cache_key``: one per session, so the requests of one
conversation reach the same prompt cache instead of whichever machine answers."""


CACHE_ROUTING: ContextVar[str | None] = ContextVar("daedalus_cache_routing", default=None)
"""The conversation a run's requests are routed by in place of its own session, set by the run.

A fork that starts with its source's bytes is cached with the source only on the server that holds
the source's cache: keyed by its own session it lands elsewhere and pays for the whole prefix again."""


CONVERSATION_HEADER_UPSTREAMS = ("grok",)
"""Key-proxy upstreams that route by ``x-grok-conv-id``: one per session, so a conversation keeps reaching
the server that holds its prompt cache. xAI's prompt-caching guide asks for this header (``prompt_cache_key``
is its Responses API spelling; this is Chat Completions), and the Grok CLI sets it on every request to the
chat proxy, giving its side calls ids of their own. A header, not a body field: the cached prefix is untouched."""


def takes_conversation_header(endpoint: ProviderEndpoint) -> bool:
    """Whether a request to this endpoint carries ``x-grok-conv-id``: the Grok subscription route or xAI itself."""
    host = (urlsplit(endpoint.base_url).hostname or "").lower()
    return keyproxy_upstream(endpoint.base_url) in CONVERSATION_HEADER_UPSTREAMS or host == "api.x.ai"


def replays_reasoning(endpoint: ProviderEndpoint) -> bool:
    """Whether this endpoint's reasoning entries are kept on the message and sent back."""
    return keyproxy_upstream(endpoint.base_url) in REASONING_REPLAY_UPSTREAMS


def takes_cache_key(endpoint: ProviderEndpoint) -> bool:
    """Whether a request to this endpoint carries ``prompt_cache_key``."""
    host = (urlsplit(endpoint.base_url).hostname or "").lower()
    return keyproxy_upstream(endpoint.base_url) in CACHE_KEY_UPSTREAMS or host == "api.openai.com"


DEEPSEEK_SHAPED = ("deepseek", "opencode")
"""Endpoints that speak DeepSeek's thinking dialect: the ``thinking`` object and ``reasoning_effort`` in DeepSeek's
names. Their reasoning replay is ``required`` unless configured otherwise (see ``config.reasoning_replay_for``)."""

DEEPSEEK_EFFORTS = {"minimal": "low", "low": "low", "medium": "high", "high": "high", "xhigh": "max"}
"""DeepSeek knows ``low``, ``high`` and ``max`` and treats anything else as ``high``: a preset that asks for
``medium`` means the model's default, and ``minimal`` is the step below it, so both are sent as what they mean."""


def apply_cache_control(messages: list[dict[str, Any]], breakpoints: Any, *, index_map: list[int] | None = None) -> None:
    """Translate the core's cache breakpoints into OpenRouter's ``cache_control`` blocks.

    OpenRouter forwards ``cache_control`` to Anthropic (and ignores it elsewhere); DeepSeek and vLLM cache
    prefixes on their own and need nothing. A breakpoint names a message index; the message's text content
    becomes a content-part list whose last part carries the marker.
    """
    for bp in breakpoints:
        index = getattr(bp, "message_index", None)
        if index is None:
            continue
        if index_map is not None:
            if not (0 <= index < len(index_map)):
                continue
            index = index_map[index]
            if index < 0:
                continue
        if not (0 <= index < len(messages)):
            continue
        entry = messages[index]
        if entry.get("role") == "tool":
            continue  # a tool result's content must stay a string on the wire
        content = entry.get("content")
        if isinstance(content, str):
            if not content:
                continue
            entry["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict) and last.get("type") == "text":
                last["cache_control"] = {"type": "ephemeral"}


@dataclass(slots=True)
class ProviderEndpoint:
    id: str
    kind: str
    base_url: str
    api_key: str = ""
    timeout_seconds: float = 600.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    pricing: dict[str, ModelPricing] = field(default_factory=dict)
    temperature: float | None = None
    """When set, every request to this endpoint samples at this temperature (a benchmark pin)."""
    subscription: bool = False
    """A prepaid plan (``billing = "subscription"``): every call is recorded at $0 and no dollar cap counts it."""
    first_byte_timeout_seconds: float = 0.0
    """How long a streamed request may get no byte back at all before it is abandoned and sent again; 0 never."""
    first_byte_retries: int = 1
    """How many times such a request is sent again before the silence is raised as a timeout."""
    reasoning_replay: ReasoningReplay = ReasoningReplay.when_present
    """Whether an assistant turn's reasoning text goes back to this endpoint, in the core's terms (see
    ``providers.wire.messages_to_wire``)."""

    def source_digest(self) -> str:
        """Bind a saved provider decision to this connection without retaining its credentials."""
        source = {"id": self.id, "kind": self.kind, "base_url": self.base_url,
                  "api_key": self.api_key, "extra_headers": self.extra_headers,
                  "temperature": self.temperature}
        return hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def pricing_for(self, model: str) -> ModelPricing | None:
        if model in self.pricing:
            return self.pricing[model]
        best = max((key for key in self.pricing if model.startswith(key)), key=len, default=None)
        return self.pricing[best] if best is not None else None

    def measures_spend(self, model: str) -> bool:
        """Whether a dollar cap can count what a call to this model costs.

        Not for the operator's own hardware, a prepaid plan (OpenCode Go), a model published at a
        zero price or under a free name, or one with no price at all: a subscription login (Grok,
        Codex, Claude) or a self-hosted server. OpenRouter is the exception to the last, since it
        reports each call's cost itself.
        """
        if self.kind == "llamacpp" or self.subscription or model.endswith((":free", "-free")):
            return False
        price = self.pricing_for(model)
        if price is not None and price.input is not None and price.output is not None:
            return bool(price.input or price.output or price.cache_hit)
        return self.kind == "openrouter"


@dataclass(slots=True)
class UsageRecord:
    provider_id: str
    model: str
    purpose: str
    raw: dict[str, Any]
    normalized: dict[str, Any]
    cost_usd: float | None
    duration_ms: int
    run_id: str | None
    session_id: str | None
    inference_reservation_id: str | None = None


class UsageSink(Protocol):
    async def record(self, record: UsageRecord) -> None: ...


class OpenAICompatibleProvider(ILLMProvider):
    """Streams ``chat/completions`` SSE into core :class:`ProviderDelta` envelopes."""

    def __init__(
        self,
        endpoint: ProviderEndpoint,
        *,
        client: httpx.AsyncClient | None = None,
        usage_sink: UsageSink | None = None,
        image_loader: ImageLoader | None = None,
        images_for: Callable[[str], bool] | None = None,
        admission: InferenceAdmission | None = None,
        context_refusal: Callable[[str, str, int], None] | None = None,
    ) -> None:
        """``images_for(model)`` says whether that model takes image parts (a preset setting).

        ``context_refusal(endpoint id, model, window)`` hears every refusal that states the model's real
        context window, so the window the presets run with can be corrected from it."""
        self.endpoint = endpoint
        self._context_refusal = context_refusal
        self._images_for = images_for or (lambda _model: False)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(endpoint.timeout_seconds, connect=30.0)
        )
        self._usage_sink = usage_sink
        self._image_loader = image_loader
        self._admission = admission

    # -- ILLMProvider -----------------------------------------------------------

    async def stream_with_tools(self, request: LLMRequest) -> AsyncIterator[ProviderDelta]:
        """Yield deltas in the order the core requires: content, tool closes, usage, finish.

        The core stops reading at ``finish``, so the stream is drained to its end
        (usage arrives in a trailing chunk) before ``usage`` and ``finish`` are yielded.
        """
        body = await self._build_body(request, stream=True)
        started = time.monotonic()
        reservation = await self._admit(request, body)
        try:
            async with aclosing(self._stream(request, body, started, reservation)) as stream:
                async for delta in stream:
                    yield delta
        finally:
            await self._interrupted(reservation)

    async def _stream(self, request: LLMRequest, body: dict[str, Any], started: float,
                      reservation: str | None) -> AsyncIterator[ProviderDelta]:
        usage_raw: dict[str, Any] | None = None
        open_tools: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        # Whether the stream said it was over. A body that ends with neither ``[DONE]`` nor a finish
        # reason, or whose last frame is cut inside its JSON, was cut in transit: it used to be read as
        # an ordinary stop, so a cut answer was stored as final and a half-written call ran.
        saw_done = False
        garbled_last = False
        guard = DsmlGuard()
        keep_reasoning = replays_reasoning(self.endpoint)
        details: list[dict[str, Any]] = []
        try:
            silent = 0
            while True:
                opened = await self._open_stream(request, body, silent)
                if opened is not None:
                    break
                silent += 1
                if silent > self.endpoint.first_byte_retries:
                    raise LLMTimeoutError(
                        f"{self.endpoint.id}: no response byte within {self.endpoint.first_byte_timeout_seconds:.0f}s"
                        f" on {silent} identical attempt(s)", classified=_verdict("timeout", True))
                # Said to the core before the next attempt waits its own window: its idle watchdog
                # counts from the last delta, and two silent windows back to back would otherwise
                # pass its baseline and be cut as a stalled stream half-way through the resend.
                yield ProviderDelta(kind=ProviderDeltaKind.progress)
            response, lines = opened
            try:
                if response.status_code >= 400:
                    raw = await response.aread()
                    if response.status_code == 400:
                        logger.warning("%s rejected a request; message shape: %s", self.endpoint.id, message_shape(body["messages"]))
                    text = raw.decode("utf-8", "replace")
                    await self._observe_failure(request, reservation, response.status_code, text, response.headers)
                    self._raise_for_status(response.status_code, text, response.headers)
                async for data in _sse_data(lines):
                    if data is None:
                        # A comment line (the key proxy's keepalive, OpenRouter's processing note):
                        # no content, but proof the upstream is alive during a silent think.
                        yield ProviderDelta(kind=ProviderDeltaKind.progress)
                        continue
                    if data == "[DONE]":
                        saw_done = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        # Skipped, but remembered: as the last frame it is the cut tail of a dropped
                        # body, and whatever it carried is lost.
                        logger.warning("%s: an unreadable stream frame (%d chars): %r", self.endpoint.id, len(data), data[:120])
                        garbled_last = True
                        continue
                    garbled_last = False
                    if not isinstance(chunk, dict):
                        continue
                    if "error" in chunk and not chunk.get("choices"):
                        self._raise_stream_error(chunk["error"])
                    if chunk.get("usage"):
                        usage_raw = chunk["usage"]
                    spoke = bool(chunk.get("usage"))  # the trailing usage chunk is bookkeeping, not liveness
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            yield ProviderDelta(kind=ProviderDeltaKind.thinking, content=reasoning)
                        if keep_reasoning and isinstance(delta.get("reasoning_details"), list):
                            # Whole entries, one per finished reasoning item: nothing to merge.
                            details.extend(d for d in delta["reasoning_details"] if isinstance(d, dict))
                        content = delta.get("content")
                        if content:
                            shown = guard.feed(content)
                            if shown:
                                yield ProviderDelta(kind=ProviderDeltaKind.text, content=shown)
                        for tc in delta.get("tool_calls") or []:
                            for out in self._tool_call_chunk(open_tools, tc, started):
                                yield out
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        spoke = spoke or bool(reasoning or content or delta.get("tool_calls") or delta.get("reasoning_details") or choice.get("finish_reason"))
                    if not spoke:
                        # A role-only or empty-delta chunk still resets the core's idle clock;
                        # without it a model that reasons silently was killed as a stalled stream.
                        yield ProviderDelta(kind=ProviderDeltaKind.progress)
            finally:
                await response.aclose()
        except httpx.HTTPError as exc:
            raise self._transport_failure(exc) from exc
        if not saw_done and (finish_reason is None or garbled_last):
            # The same answer as a connection the transport saw drop: retried, and no call it opened
            # is ever closed, so none of them can run.
            raise LLMProviderError(
                f"{self.endpoint.id}: the stream ended before the answer was finished", classified=_verdict("connection", True))
        rest, recovered = guard.finish()
        if rest:
            yield ProviderDelta(kind=ProviderDeltaKind.text, content=rest)
        for call in recovered:
            # The model wrote its native tool markup as text: surface it as real calls.
            index = len(open_tools)
            call_id = f"call_dsml_{index}_{int(started * 1000)}"
            open_tools[index] = {"id": call_id, "name": call.name, "args": call.arguments_json, "started": True}
            yield ProviderDelta(kind=ProviderDeltaKind.tool_use_start, tool_call_id=call_id, tool_name=call.name)
            yield ProviderDelta(kind=ProviderDeltaKind.tool_use_input, tool_call_id=call_id, tool_input_delta=call.arguments_json)
        if recovered:
            finish_reason = "tool_calls"
        resolved_finish = finish_reason or ("tool_calls" if open_tools else "stop")
        for delta_out in self._close_tools(open_tools, resolved_finish):
            yield delta_out
        normalized = normalize_usage(usage_raw or {})
        await self._record_usage(request, "stream", usage_raw or {}, normalized, started, reservation)
        yield ProviderDelta(kind=ProviderDeltaKind.usage, usage=normalized)
        kept = (reasoning_envelope(self.endpoint.id, request.model, details, normalized["reasoning_tokens"]),) if details else None
        yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason=_map_finish(resolved_finish), reasoning_items=kept)

    async def _open_stream(self, request: LLMRequest, body: dict[str, Any],
                           attempt: int) -> tuple[httpx.Response, AsyncIterator[str]] | None:
        """Send the request and wait for its first line back; ``None`` when nothing came in time.

        The deadline covers the response headers and the first line of the body together, because an
        upstream that answers its headers at once and then says nothing is as stuck as one that never
        answers (the Grok subscription route held a cold request 103.7 s this way while its peers
        took 8 to 13 s). Any line counts, an SSE comment or an empty one included: the key proxy turns
        every upstream event a silent thinker sends (Anthropic's ping, a Responses ``created``) into a
        keepalive comment, so a model that thinks for a minute is never taken for a dead one. A line,
        not a byte, is what is waited for because servers flush whole lines; nothing writes half of one
        and then pauses.

        Abandoning such a request and sending the same one again is safe because nothing came back:
        no partial output reached the run, so none can be repeated. The body is the caller's own dict,
        serialised the same way each time, so a provider's prompt cache sees the identical prefix. A
        refusal (status 400 and up) is an answer, so its body is read with no deadline.
        """
        timeout = self.endpoint.first_byte_timeout_seconds
        sent = self._client.build_request("POST", self._url("/chat/completions"), json=body, headers=self._headers(request))
        began = time.monotonic()
        response: httpx.Response | None = None
        lines: AsyncIterator[str] | None = None
        answered_at: float | None = None
        try:
            async with asyncio.timeout(timeout if timeout > 0 else None) as deadline:
                response = await self._client.send(sent, stream=True)
                answered_at = time.monotonic()
                lines = response.aiter_lines()
                if response.status_code >= 400:
                    deadline.reschedule(None)
                    return response, lines
                first = await anext(lines, None)
        except TimeoutError:
            if response is not None:
                await response.aclose()
            if not deadline.expired():
                raise
            headers = "no headers" if answered_at is None else f"headers after {answered_at - began:.1f}s"
            logger.warning("%s: no byte of the %s response within %.0fs (%s); sending the identical request again (%d of %d)",
                           self.endpoint.id, request.model, timeout, headers, attempt + 1, self.endpoint.first_byte_retries)
            return None
        except BaseException:
            if response is not None:
                await response.aclose()
            raise
        if attempt:
            logger.warning("%s: the resent %s request answered after %.1fs", self.endpoint.id, request.model, time.monotonic() - began)
        return response, _after(first, lines)

    def _tool_call_chunk(
        self, open_tools: dict[int, dict[str, Any]], tc: dict[str, Any], started: float
    ) -> list[ProviderDelta]:
        out: list[ProviderDelta] = []
        index = int(tc.get("index", len(open_tools)))
        fn = tc.get("function") or {}
        if index not in open_tools:
            open_tools[index] = {
                "id": tc.get("id") or f"call_{index}_{int(started * 1000)}",
                "name": fn.get("name") or "",
                "args": "",
                "started": False,
            }
        slot = open_tools[index]
        if tc.get("id"):
            slot["id"] = tc["id"]
        if fn.get("name"):
            slot["name"] = fn["name"]
        if not slot["started"] and slot["name"]:
            slot["started"] = True
            out.append(
                ProviderDelta(
                    kind=ProviderDeltaKind.tool_use_start,
                    tool_call_id=slot["id"],
                    tool_name=slot["name"],
                )
            )
        args_piece = fn.get("arguments") or ""
        if args_piece:
            slot["args"] += args_piece
            if slot["started"]:
                out.append(
                    ProviderDelta(
                        kind=ProviderDeltaKind.tool_use_input,
                        tool_call_id=slot["id"],
                        tool_input_delta=args_piece,
                    )
                )
        return out

    async def complete_structured(
        self, request: LLMRequest, response_schema: dict[str, Any]
    ) -> LLMResponse:
        """JSON-mode completion: the object is validated here and returned as its JSON text.

        Nothing in the core's compaction asks for this any more — a summary is requested as plain
        text (``complete_text``) — so a reply that is not a whole object is refused as it stands."""
        body = await self._build_body(request, stream=False)
        body["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        reservation = await self._admit(request, body)
        try:
            data = await self._post(body, request, reservation)
            text = _message_text(data)
            usage_raw = data.get("usage") or {}
            normalized = normalize_usage(usage_raw)
            cost = await self._record_usage(request, "structured", usage_raw, normalized, started, reservation)
            finish = (data.get("choices") or [{}])[0].get("finish_reason") or "stop"
            parsed = parse_json_text(text)
            if parsed is None:
                # The head and the tail are what tell a summary that outgrew its cap from a refusal or a loop.
                logger.warning("%s: structured reply unusable (finish=%s, %s chars): head=%r tail=%r", self.endpoint.id, finish, len(text), text[:160], text[-160:])
                raise LLMProviderError(
                    f"{self.endpoint.id}: structured response is not JSON"
                    + (" (output truncated by max_tokens)" if finish == "length" else "")
                )
            return LLMResponse(
                message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=json.dumps(parsed, ensure_ascii=False))]),
                stop_reason=StopReason.max_tokens if finish == "length" else StopReason.end_turn,
                usage=LLMResponseUsage(
                    input_tokens=normalized["input_tokens"],
                    output_tokens=normalized["output_tokens"],
                    cache_read_input_tokens=normalized["cache_read_input_tokens"],
                    response_cost_usd=cost,
                ),
            )
        finally:
            await self._interrupted(reservation)

    async def complete_text(self, request: LLMRequest) -> LLMResponse:
        body = await self._build_body(request, stream=False)
        started = time.monotonic()
        reservation = await self._admit(request, body)
        try:
            data = await self._post(body, request, reservation)
            text = _message_text(data)
            usage_raw = data.get("usage") or {}
            normalized = normalize_usage(usage_raw)
            cost = await self._record_usage(request, "text", usage_raw, normalized, started, reservation)
            finish = (data.get("choices") or [{}])[0].get("finish_reason") or "stop"
            return LLMResponse(
                message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=text)]),
                stop_reason=StopReason.max_tokens if finish == "length" else StopReason.end_turn,
                usage=LLMResponseUsage(
                    input_tokens=normalized["input_tokens"],
                    output_tokens=normalized["output_tokens"],
                    cache_read_tokens=normalized["cache_read_tokens"],
                    cached_tokens=normalized["cache_read_tokens"],
                    response_cost_usd=cost,
                ),
            )
        finally:
            await self._interrupted(reservation)

    def count_tokens(self, text: str, model: str | None = None) -> int:
        # A cheap estimate; the loop re-anchors on the provider's reported prompt size.
        return max(1, len(text) // 3)

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals ----------------------------------------------------------------

    async def _build_body(self, request: LLMRequest, *, stream: bool) -> dict[str, Any]:
        extra = dict(request.extra or {})
        model = request.model
        wire: list[dict[str, Any]] = []
        wire_index: list[int] = []  # core message index -> index of its LAST wire entry (a tool message fans out into several)
        keep_reasoning = replays_reasoning(self.endpoint)
        images = self.accepts_images(model)
        loader = _remembering(self._image_loader) if images and self._image_loader is not None else self._image_loader
        omitted = await images_left_out(request.messages, loader) if images and loader is not None else frozenset()
        thinking = bool(extra.get("enable_thinking", False))
        for message in request.messages:
            entries = await messages_to_wire([message], image_loader=loader, supports_images=images, images_omitted=omitted,
                                             reasoning_replay=self.endpoint.reasoning_replay, thinking=thinking)
            if keep_reasoning and entries and message.reasoning_items:
                # An assistant message is one wire entry; its reasoning goes back on it, in order.
                details = replayed_details(message, self.endpoint.id, model)
                if details:
                    entries[-1]["reasoning_details"] = details
            wire.extend(entries)
            wire_index.append(len(wire) - 1 if entries else -1)  # -1: this message put nothing on the wire
        wire, wire_index = place_carried_images(wire, wire_index)
        body: dict[str, Any] = {
            "model": model,
            "messages": wire,
            "max_tokens": request.max_tokens,
            "stream": stream,
        }
        # The core leaves the temperature unset unless a caller states one, so the server's own
        # generation config (or the model's recommended sampling) applies; a null on the wire is not
        # "unset" to every server, so the key is left out rather than sent empty.
        temperature = self.endpoint.temperature if self.endpoint.temperature is not None else request.temperature
        if temperature is not None:
            body["temperature"] = temperature
        if stream:
            body["stream_options"] = {"include_usage": True}
        if request.tools:
            body["tools"] = tools_to_llamacpp_wire(request.tools) if self.endpoint.kind == "llamacpp" else tools_to_wire(request.tools)
            forced = extra.get("forced_tool_choice")
            if forced:
                body["tool_choice"] = {"type": "function", "function": {"name": forced}}
            elif extra.get("tool_choice_required"):
                # "Some tool, no prose": the core asks for it when a run must act rather than write.
                body["tool_choice"] = "required"
            elif isinstance(extra.get("tool_choice"), dict):
                body["tool_choice"] = extra["tool_choice"]
        effort = str(extra.get("reasoning_effort") or "medium")
        self._apply_thinking(body, thinking=thinking, effort=effort)
        if extra.get("json_object") and self.endpoint.kind in DEEPSEEK_SHAPED:
            # A caller that parses the answer as JSON itself asks for it here. DeepSeek-shaped endpoints
            # otherwise scatter stray characters inside the object ('{"ops":#…') even on a trivial
            # prompt; the others were never seen to, and how a proxied subscription route reads the
            # field is unknown, so they are left as they answer best.
            body["response_format"] = {"type": "json_object"}
        breakpoints = extra.get("cache_breakpoints")
        if breakpoints and self.endpoint.kind == "openrouter":
            apply_cache_control(body["messages"], breakpoints, index_map=wire_index)
        if takes_cache_key(self.endpoint):
            obs = request.observability
            cache_key = CACHE_ROUTING.get() or (obs.session_id if obs is not None else None) or (obs.run_id if obs is not None else None)
            if cache_key:
                body["prompt_cache_key"] = cache_key
        if self.endpoint.kind == "llamacpp":
            # llama.cpp owns one prompt cache per slot. Asking it to reuse the common prefix makes a
            # continuing conversation behave like hosted providers' automatic prefix caches; this
            # field is deliberately confined to llama.cpp because generic OpenAI servers may reject
            # extension parameters they do not know.
            body["cache_prompt"] = True
        return body

    def accepts_images(self, model: str) -> bool:
        return self._image_loader is not None and bool(self._images_for(model))

    def _apply_thinking(self, body: dict[str, Any], *, thinking: bool, effort: str) -> None:
        kind = self.endpoint.kind
        if kind == "llamacpp":
            # The loaded chat template reads these as jinja variables. Sending nothing leaves the
            # template's own default, which for Bonsai is xhigh even when the preset asked for low.
            # `high` is not a Bonsai effort (low | medium | xhigh) and raises in the template, so it
            # is sent as xhigh: the next step up, the same mapping DeepSeek uses for medium -> high.
            kwargs = dict(body.get("chat_template_kwargs") or {})
            kwargs["enable_thinking"] = thinking
            if thinking:
                mapped = {"minimal": "low", "low": "low", "medium": "medium", "high": "xhigh", "xhigh": "xhigh", "max": "xhigh"}.get(effort, effort)
                kwargs["reasoning_effort"] = mapped
                body["reasoning_effort"] = mapped
            body["chat_template_kwargs"] = kwargs
            return
        if kind in DEEPSEEK_SHAPED:
            # OpenCode Go passes DeepSeek's fields through unchanged; its other models take the same shape.
            body["thinking"] = {"type": "enabled" if thinking else "disabled"}
            if thinking:
                body["reasoning_effort"] = DEEPSEEK_EFFORTS.get(effort, effort)
        elif kind == "openrouter":
            body["reasoning"] = {"effort": effort} if thinking else {"enabled": False}
            body["usage"] = {"include": True}
        elif kind == "vllm":
            body["chat_template_kwargs"] = {"enable_thinking": thinking}
        else:
            if thinking:
                body["reasoning_effort"] = effort

    async def _post(self, body: dict[str, Any], request: LLMRequest | None = None,
                    reservation: str | None = None) -> dict[str, Any]:
        try:
            response = await self._client.post(
                self._url("/chat/completions"), json=body, headers=self._headers(request)
            )
        except httpx.HTTPError as exc:
            raise self._transport_failure(exc) from exc
        if response.status_code >= 400:
            if request is not None:
                await self._observe_failure(request, reservation, response.status_code, response.text, response.headers)
            self._raise_for_status(response.status_code, response.text, response.headers)
        data = response.json()
        if "error" in data and not data.get("choices"):
            self._raise_stream_error(data["error"])
        return data

    def _raise_stream_error(self, error: Any) -> None:
        """An error the provider reported inside a 200 (a stream chunk, or the key proxy's assembled body)."""
        if isinstance(error, dict) and error.get("code") == STREAM_TRUNCATED:
            raise LLMProviderError(f"{self.endpoint.id}: {error.get('message') or STREAM_TRUNCATED}",
                                   classified=_verdict("connection", True))
        self._raise_for_status(stream_error_status(error), json.dumps(error))

    def _raise_for_status(self, status: int, text: str, headers: httpx.Headers | None = None) -> None:
        lowered = text.lower()
        if self.endpoint.kind == "llamacpp" and status == 503:
            # No free inference slot and a model which is still loading or waking both answer 503.
            # Rate-limit errors are the core's retryable provider-busy condition, so the existing
            # in-run retry and the host's longer outage recovery do the waiting instead of ending the
            # operator's task.
            raise LLMRateLimitError(f"{self.endpoint.id}: llama.cpp busy: {text[:300]}", classified=_verdict("rate_limit", True))
        if status in (400, 413, 422) and any(m in lowered for m in _CONTEXT_ERROR_MARKERS):
            raise LLMContextWindowExceeded(f"{self.endpoint.id}: {text[:300]}", **context_overflow_sizes(text))
        verdict = classify_failure(status, text)
        wait = _retry_after_seconds(headers, text)
        if wait is not None:
            verdict = verdict.model_copy(update={"retry_after_seconds": wait})
        if verdict.reason is ProviderFailureReason.rate_limit:
            raise LLMRateLimitError(f"{self.endpoint.id}: rate limited: {text[:300]}", classified=verdict)
        if verdict.reason is ProviderFailureReason.timeout:
            raise LLMTimeoutError(f"{self.endpoint.id}: HTTP {status}: {text[:300]}", classified=verdict)
        raise LLMProviderError(failure_text(self.endpoint.id, status, text), classified=verdict)

    def _transport_failure(self, exc: httpx.HTTPError) -> LLMProviderError | LLMTimeoutError:
        """A request that never got an answer, with the core's verdict on why.

        A refused, reset or unreachable connection is ``connection``, which moves the run to the next
        model of its fallback chain; it used to be raised bare, and an error without a verdict never
        moves the chain, so a dead endpoint with a healthy fallback ended the run instead.
        """
        verdict = classify_transport_exception(exc)
        if isinstance(exc, httpx.TimeoutException) or (verdict is not None and verdict.reason is ProviderFailureReason.timeout):
            return LLMTimeoutError(f"{self.endpoint.id}: {exc}", classified=verdict or _verdict("timeout", True))
        return LLMProviderError(f"{self.endpoint.id}: transport error: {exc}", classified=verdict)

    async def _observe_failure(self, request: LLMRequest, reservation: str | None, status: int,
                               text: str, headers: httpx.Headers) -> FailureEvidence:
        evidence = failure_evidence(status, text, headers, provider_kind=self.endpoint.kind,
                                    provider_host=(urlsplit(self.endpoint.base_url).hostname or "").lower())
        if self._context_refusal is not None and status in (400, 413, 422):
            window = stated_window(text)
            if window:
                try:
                    self._context_refusal(self.endpoint.id, request.model, window)
                except Exception:  # noqa: BLE001 — learning the window is a side effect; the refusal is the error
                    logger.exception("could not record the context window %s stated for %s", window, request.model)
        if self._admission is not None and hasattr(self._admission, "failure"):
            try:
                await self._admission.failure(self.endpoint, request, reservation, evidence)
            except Exception:  # noqa: BLE001 — the provider refusal remains the user's primary error
                logger.exception("could not retain a provider failure observation for %s", self.endpoint.id)
        return evidence

    def _url(self, path: str) -> str:
        return self.endpoint.base_url.rstrip("/") + path

    def _headers(self, request: LLMRequest | None = None) -> dict[str, str]:
        # The key proxy meters calls that do not carry this mark (a shell's curl); the bot records its own.
        headers = {"content-type": "application/json", "x-daedalus-metered": "1", **self.endpoint.extra_headers}
        if self.endpoint.api_key:
            headers["authorization"] = f"Bearer {self.endpoint.api_key}"
        if self.endpoint.kind == "opencode":
            # The gateway routes and caches by conversation: one stable id per session, and a client name.
            headers["user-agent"] = f"daedalus/{__version__}"
            obs = request.observability if request is not None else None
            session = (obs.session_id if obs is not None else None) or (obs.run_id if obs is not None else None)
            headers["x-opencode-session"] = session or self.endpoint.id
        if takes_conversation_header(self.endpoint):
            obs = request.observability if request is not None else None
            conversation = CACHE_ROUTING.get() or (obs.session_id if obs is not None else None) or (obs.run_id if obs is not None else None)
            # No fallback to a fixed value: one id shared by every caller would pin them all to one server.
            if conversation:
                headers["x-grok-conv-id"] = conversation
        return headers

    def _close_tools(
        self, open_tools: dict[int, dict[str, Any]], finish_reason: str
    ) -> list[ProviderDelta]:
        out: list[ProviderDelta] = []
        for index in sorted(open_tools):
            slot = open_tools[index]
            if not slot["started"]:
                slot["started"] = True
                out.append(
                    ProviderDelta(
                        kind=ProviderDeltaKind.tool_use_start,
                        tool_call_id=slot["id"],
                        tool_name=slot["name"] or "unknown",
                    )
                )
            raw_args = slot["args"]
            try:
                json.loads(raw_args or "{}")
                # A call with no arguments at all in an answer the cap cut is the call it cut.
                partial = finish_reason == "length" and not raw_args.strip()
            except json.JSONDecodeError:
                partial = True
            out.append(
                ProviderDelta(
                    kind=ProviderDeltaKind.tool_use_stop,
                    tool_call_id=slot["id"],
                    tool_name=slot["name"] or "unknown",
                    tool_input_final=parse_json_arguments(raw_args),
                    is_block_end=True,
                    # A cut call under any finish but an ordinary stop goes to the core's output-cap
                    # recovery, which never dispatches it. Reading only "length" here let a cut call
                    # under a tool-use finish run with its braces repaired: the core's truncated-call
                    # policy looks only at a stop, and nothing looked at a tool-use finish.
                    truncated_by_output_cap=partial and (finish_reason == "length" or (finish_reason != "stop" and arguments_cut(raw_args))),
                    args_partial_truncated=partial,
                )
            )
        open_tools.clear()
        return out

    async def _admit(self, request: LLMRequest, body: dict[str, Any]) -> str | None:
        if self._admission is None:
            return None
        return await self._admission.start(self.endpoint, request, body)

    async def _interrupted(self, reservation: str | None) -> None:
        if reservation is not None:
            assert self._admission is not None
            await self._admission.interrupted(reservation, "the request ended without a settled usage observation")

    async def _record_usage(
        self,
        request: LLMRequest,
        purpose: str,
        raw: dict[str, Any],
        normalized: dict[str, Any],
        started: float,
        reservation: str | None = None,
    ) -> float | None:
        model = request.model
        cost: float | None = None
        if reservation is not None:
            assert self._admission is not None
            cost = await self._admission.cost(reservation, raw, normalized)
        elif self.endpoint.kind == "llamacpp":
            # This is inference on the operator's own hardware, not a hosted token sale. Recording
            # zero rather than an unknown price keeps the usage ledger complete while ensuring every
            # spending cap treats the call as free.
            cost = 0.0
        elif self.endpoint.subscription:
            # The plan is paid for up front: a call spends its allowance, not money. A gateway that
            # reports a list-price cost in the usage would otherwise book that as dollars charged.
            cost = 0.0
        elif raw.get("cost") is not None:
            cost = float(raw["cost"])
        else:
            pricing = self.endpoint.pricing_for(model)
            if pricing is not None and complete_usage(raw):
                cost = pricing.cost(normalized)
        normalized["cost_usd"] = cost
        if self._usage_sink is not None:
            obs = request.observability
            if obs is not None and obs.call_category in ("assist", "memory") and obs.call_purpose:
                # A helper around the operator's messages (the next-message guess, the autopilot judge,
                # meeting notes) and memory's own calls (the background pass, the classifier, the edit
                # in words) are booked under their own names, so Usage shows what each one costs apart
                # from the runs, and a daily cap can be summed from the ledger.
                purpose = obs.call_purpose
            elif purpose == "stream" and obs is not None and obs.call_category == "compaction":
                # The host streams its summaries, and a 'stream' row is what the session's prompt size is
                # read from: a summary of a 30k-token part booked as one made the next check see a
                # session of 30k tokens.
                purpose = "compaction"
            await self._usage_sink.record(
                UsageRecord(
                    provider_id=self.endpoint.id,
                    model=model,
                    purpose=purpose,
                    raw=raw,
                    normalized=normalized,
                    cost_usd=cost,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    run_id=obs.run_id if obs else None,
                    session_id=obs.session_id if obs else None,
                    inference_reservation_id=reservation,
                )
            )
        return cost


def normalize_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Map every OpenAI-compatible usage shape onto the keys the core reads."""
    prompt = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
    completion = int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
    details = raw.get("prompt_tokens_details") or {}
    cache_hit = int(
        raw.get("prompt_cache_hit_tokens")
        or details.get("cached_tokens")
        or raw.get("cache_read_input_tokens")
        or 0
    )
    completion_details = raw.get("completion_tokens_details") or {}
    reasoning = int(completion_details.get("reasoning_tokens") or 0)
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "cache_read_tokens": cache_hit,
        "cache_read_input_tokens": cache_hit,
        "cache_creation_input_tokens": int(raw.get("cache_creation_input_tokens") or 0),
        "reasoning_tokens": reasoning,
        "total_tokens": int(raw.get("total_tokens") or prompt + completion),
    }


def _map_finish(reason: str | None) -> Any:
    mapping = {
        "stop": "stop",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "tool_use": "tool_use",
        "length": "length",
        "content_filter": "content_filter",
    }
    return mapping.get(reason or "stop", "stop")


def _message_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_text(text: str) -> dict[str, Any] | None:
    """Parse a JSON object from model output, stripping code fences and prose."""
    candidates = [text.strip()]
    candidates.extend(m.group(1).strip() for m in _FENCE_RE.finditer(text))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _remembering(loader: ImageLoader) -> ImageLoader:
    """``loader`` for one request, reading each picture once: measured for the window, then sent."""
    loaded: dict[str, tuple[bytes, str]] = {}

    async def load(blob_ref: str) -> tuple[bytes, str]:
        if blob_ref not in loaded:
            loaded[blob_ref] = await loader(blob_ref)
        return loaded[blob_ref]

    return load


async def _after(first: str | None, rest: AsyncIterator[str]) -> AsyncIterator[str]:
    """``first`` (the line the watchdog already read; ``None`` for a body that ended empty), then ``rest``."""
    if first is None:
        return
    yield first
    async for line in rest:
        yield line


async def _sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str | None]:
    """Yield the ``data:`` payload of each SSE event, and ``None`` for each comment line.

    A comment carries no data but is the only sign of life some upstreams give while they think.
    """
    buffer: list[str] = []
    async for line in lines:
        if line == "":
            if buffer:
                yield "\n".join(buffer)
                buffer = []
            continue
        if line.startswith(":"):
            yield None
            continue
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
    if buffer:
        yield "\n".join(buffer)


__all__ = [
    "CACHE_ROUTING",
    "ImageLoader",
    "ModelPricing",
    "OpenAICompatibleProvider",
    "ProviderEndpoint",
    "UsageRecord",
    "UsageSink",
    "normalize_usage",
    "parse_json_text",
]
