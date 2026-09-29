"""Voice notes → text through any OpenAI-compatible transcription endpoint."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from daedalus.config import AsrConfig

logger = logging.getLogger(__name__)


VOICE_NOTE_PREFIX = "🎙 Voice note, transcribed automatically (wording may be imperfect):"


class TranscriptionError(RuntimeError):
    pass


def asr_configured(config: AsrConfig) -> bool:
    """Whether voice notes are transcribed at all: a provider or an endpoint is named."""
    return bool(config.provider or config.url)


def effective_asr(config: AsrConfig, manager: Any) -> AsrConfig:
    """The endpoint to call: a configured provider's base URL and key when ``provider`` is set, else ``url``/``api_key``."""
    if not config.provider:
        return config
    registry = getattr(manager, "providers", None)
    try:
        endpoint = registry.get(config.provider).endpoint if registry is not None else None
    except KeyError:
        endpoint = None
    if endpoint is None or not endpoint.base_url:
        raise TranscriptionError(f"speech-to-text provider {config.provider!r} is not configured")
    return config.model_copy(update={"url": endpoint.base_url, "api_key": endpoint.api_key})


def voice_note_text(transcript: str, caption: str = "") -> str:
    """What the agent reads instead of the audio: the words, marked as a transcript."""
    head = (caption.strip() + "\n\n") if caption.strip() else ""
    return f"{head}{VOICE_NOTE_PREFIX}\n{transcript.strip()}"


async def transcribe(path: Path, config: AsrConfig, *, client: httpx.AsyncClient | None = None, allow_empty: bool = False) -> str:
    """Return the transcript of an audio file, or raise :class:`TranscriptionError`.

    ``allow_empty`` is for one piece of a longer recording, where a stretch of silence honestly has no
    words in it; a whole recording that comes back empty is still a failure.
    """
    if not config.url:
        raise TranscriptionError("no speech-to-text endpoint is configured (settings → asr.url)")
    url = config.url.rstrip("/") + "/audio/transcriptions"
    headers = {"authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    data = {"model": config.model, "response_format": "json"}
    if config.language:
        data["language"] = config.language
    owns = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds, connect=15.0))
    try:
        with path.open("rb") as fh:
            response = await client.post(url, headers=headers, data=data, files={"file": (path.name, fh, "application/octet-stream")})
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"transcription request failed: {type(exc).__name__}") from exc
    finally:
        if owns:
            await client.aclose()
    if response.status_code >= 400:
        # The status alone ("HTTP 400") told the operator nothing about a recording that was simply
        # too long; whatever the endpoint said about it goes with it.
        raise TranscriptionError(f"transcription endpoint answered HTTP {response.status_code}{_said(response)}")
    try:
        text = str(response.json().get("text") or "").strip()
    except ValueError as exc:
        raise TranscriptionError("transcription endpoint returned no JSON") from exc
    if not text and not allow_empty:
        raise TranscriptionError("empty transcript")
    return text


def _said(response: httpx.Response) -> str:
    """The endpoint's own reason for a refusal, short, or nothing."""
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, dict):
        error = body.get("error", body.get("detail", ""))
        body = error.get("message", "") if isinstance(error, dict) else error
    said = " ".join(str(body or "").split())[:200]
    return f": {said}" if said else ""


__all__ = ["VOICE_NOTE_PREFIX", "TranscriptionError", "asr_configured", "effective_asr", "transcribe", "voice_note_text"]
