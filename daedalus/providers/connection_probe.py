"""Whether a configured provider endpoint answers: the cheapest read of it, as data.

The probe asks for the endpoint's model list through the adapter real requests use, so the address, the
key, the client's TLS and the key proxy in front of it are all in the path. Every outcome is an answer
(``ok`` false and a sentence), never an exception, because a test button that raises when the thing it
tests is down has told the operator nothing. No credential reaches the sentence: a failure is described by
its kind and status, and an endpoint's own words are scrubbed of the key and of anything the redactor knows.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from daedalus.providers.openai_compat import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

PROBE_SECONDS = 8.0
"""A test is a question the operator is waiting on; an endpoint slower than this to list its models would not be pleasant to run on."""
SAID_CHARS = 160


def _said(response: httpx.Response, key: str, redact: Callable[[str], str]) -> str:
    """The endpoint's own error sentence for the failure, scrubbed, or nothing when it wrote none.

    Some endpoints repeat the key they refused ("Incorrect API key provided: sk-…"), so the key is removed
    by value before the redactor looks, and the sentence is cut short."""
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    text = str(message or "").strip()
    if key:
        text = text.replace(key, "…")
    return redact(text)[:SAID_CHARS]


def _failure(status: int, said: str) -> str:
    if status in (401, 403):
        words = f"the endpoint refused the key (HTTP {status})"
    elif status == 404:
        words = "the endpoint has no model list at this address (HTTP 404); its base URL may be missing the /v1 part"
    elif status == 429:
        words = "the endpoint is rate limiting (HTTP 429)"
    elif status >= 500:
        words = f"the endpoint failed on its side (HTTP {status})"
    else:
        words = f"the endpoint answered HTTP {status}"
    return f"{words}: {said}" if said else words


def _count(response: httpx.Response) -> int | None:
    """How many models the answer lists, or ``None`` when it is not a model list (an OpenAI-style ``data``, or llama.cpp's ``models``)."""
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    for field in ("data", "models"):
        listed = body.get(field)
        if isinstance(listed, list):
            return len([row for row in listed if isinstance(row, dict)])
    return None


def _unreachable(exc: httpx.HTTPError, seconds: float) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return f"no answer within {seconds:g} s"
    if isinstance(exc, httpx.ConnectError):
        if "CERTIFICATE" in str(exc).upper():
            return "could not connect: its certificate was not accepted"
        return "could not connect: the connection was refused or the host cannot be reached"
    # The class and nothing else: an exception's text can carry the address the request went to.
    return f"the request failed ({exc.__class__.__name__})"


async def probe(provider: OpenAICompatibleProvider, *, redact: Callable[[str], str], problem: str = "", seconds: float = PROBE_SECONDS) -> dict[str, Any]:
    """``{ok, latency_ms, status, detail, models?}`` for one provider.

    ``problem`` is a reason the caller already knows the request cannot succeed (a key proxy that holds no
    credential for the upstream): it is returned as the answer and nothing is sent. ``latency_ms`` is how
    long the endpoint took to answer, or to give up, and ``None`` where nothing was sent. ``models`` is how
    many models the list carries, present only when the endpoint answered with one."""
    if problem:
        return {"ok": False, "latency_ms": None, "status": None, "detail": problem}
    started = time.perf_counter()
    try:
        response = await provider.probe_models(httpx.Timeout(seconds, connect=min(5.0, seconds)))
    except httpx.HTTPError as exc:
        elapsed = round((time.perf_counter() - started) * 1000)
        logger.info("provider %s did not answer the connection test: %s in %d ms", provider.endpoint.id, exc.__class__.__name__, elapsed)
        return {"ok": False, "latency_ms": elapsed, "status": None, "detail": _unreachable(exc, seconds)}
    elapsed = round((time.perf_counter() - started) * 1000)
    status = response.status_code
    logger.info("provider %s answered the connection test: HTTP %d in %d ms", provider.endpoint.id, status, elapsed)
    if status != 200:
        return {"ok": False, "latency_ms": elapsed, "status": status, "detail": _failure(status, _said(response, provider.endpoint.api_key, redact))}
    models = _count(response)
    if models is None:
        return {"ok": False, "latency_ms": elapsed, "status": status, "detail": "the endpoint answered, but not with a list of models"}
    return {"ok": True, "latency_ms": elapsed, "status": status, "detail": "", "models": models}


__all__ = ["PROBE_SECONDS", "probe"]
