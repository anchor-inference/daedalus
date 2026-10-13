"""Meeting notes from a recording: the transcript, then a summary, the decisions and the action items.

A recording arrives from the composer (an attached audio file, or a long recording the operator
turned into a meeting) or from Telegram (an audio file, or a voice note over the dictation limit).
It is decoded and cut into pieces the transcriber accepts, each piece transcribed in turn so the
progress can be shown, and the session's own model then writes the notes from the transcript.

The audio is deleted as soon as its words are in hand, unless the operator asked to keep it, in
which case it goes to the session's inbox beside the transcript. The transcript and the notes are
written to the inbox as one Markdown file, so the agent can read them when the operator brings the
notes into the conversation, and the action items can become one calendar entry with a checklist.

Jobs live in memory: a restart in the middle of one loses its progress, and the operator sends the
recording again. Nothing about a meeting is worth a table that outlives the process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from protocore.contracts.llm import LLMObservabilityContext, LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.host.engine_factory import TENANT
from daedalus.speech.chunks import join_transcripts, split_pcm, wav_bytes
from daedalus.speech.service import (
    CLOUD_PIECE_BYTES,
    SpeechError,
    decode_file,
    recogniser_available,
    transcribe_recording,
)

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

PURPOSE = "meeting_notes"
"""The label the notes' model call is booked under in Usage."""

UPLOAD_MAX = 1 << 30
"""The largest recording the composer may send: three hours of a phone's recording is well under it."""

JOBS_KEPT = 20
"""Finished jobs kept for the screen to read back; older ones are let go first."""

TRANSCRIPT_CHARS = 400_000
"""What of a transcript the notes are written from. Three hours of speech is about two hundred
thousand characters; past this the middle is left out and the notes say so."""

NOTES_PROMPT = """You write the notes of a meeting from its transcript. Answer with JSON only, in this shape:
{{"title": "a short name for the meeting", "summary": "a few sentences", "decisions": ["one decision per entry"], "actions": ["one action item per entry, with the owner and the date when the transcript names them"]}}
Write in the language of the transcript. Leave a list empty rather than inventing entries. {extra}
Transcript:
"""


@dataclass
class MeetingJob:
    id: str
    session_id: str
    name: str
    status: str = "queued"
    """``queued``, ``transcribing``, ``writing``, ``done`` or ``failed``."""
    done: int = 0
    total: int = 0
    seconds: float = 0.0
    notes: dict[str, Any] = field(default_factory=dict)
    file: str = ""
    """The Markdown file in the session's inbox, relative to the workspace."""
    kept: str = ""
    """Where the recording was kept, relative to the workspace; empty when it was deleted."""
    error: str = ""
    calendar_event: str = ""
    started: float = field(default_factory=time.monotonic)

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id, "session_id": self.session_id, "name": self.name, "status": self.status,
            "done": self.done, "total": self.total, "seconds": round(self.seconds), "notes": self.notes,
            "file": self.file, "kept": self.kept, "error": self.error, "calendar_event": self.calendar_event,
        }


def parse_notes(answer: str) -> dict[str, Any]:
    """The model's JSON as the notes, every field present and of its type; raises when there is none."""
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("the model did not answer with the notes")
    raw = json.loads(answer[start:end + 1])
    if not isinstance(raw, dict):
        raise ValueError("the model did not answer with the notes")

    def entries(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            text = item.get("text") if isinstance(item, dict) else item
            if isinstance(text, str | int | float) and str(text).strip():
                out.append(" ".join(str(text).split())[:2000])
        return out[:200]

    return {
        "title": " ".join(str(raw.get("title") or "").split())[:120],
        "summary": str(raw.get("summary") or "").strip()[:8000],
        "decisions": entries(raw.get("decisions")),
        "actions": entries(raw.get("actions")),
    }


def notes_markdown(notes: dict[str, Any], *, labels: dict[str, str] | None = None) -> str:
    """The notes as the chat and the file show them."""
    words = {"summary": "Summary", "decisions": "Decisions", "actions": "Action items", **(labels or {})}
    parts = [f"## {notes.get('title') or 'Meeting notes'}", "", notes.get("summary") or ""]
    for key in ("decisions", "actions"):
        items = notes.get(key) or []
        if items:
            parts += ["", f"**{words[key]}**", *[f"- {item}" for item in items]]
    return "\n".join(parts).strip() + "\n"


def calendar_body(notes: dict[str, Any], day: date, *, file: str = "") -> dict[str, Any]:
    """One all-day entry on ``day``: the summary and decisions as text, the action items as a checklist."""
    title = notes.get("title") or "Meeting"
    text = notes.get("summary") or ""
    if notes.get("decisions"):
        text += "\n\n**Decisions**\n" + "\n".join(f"- {item}" for item in notes["decisions"])
    blocks: list[dict[str, Any]] = []
    if text.strip():
        blocks.append({"kind": "markdown", "text": text.strip()})
    blocks.append({"kind": "checklist", "title": "Action items", "items": [{"text": item} for item in notes.get("actions") or []]})
    return {
        "title": f"Action items: {title}"[:200],
        "all_day": True,
        "start_date": day.isoformat(),
        "end_date": (day + timedelta(days=1)).isoformat(),
        "description": f"Meeting notes in {file}" if file else "",
        "blocks": blocks,
        "tags": ["meeting"],
    }


class MeetingService:
    """The meeting jobs of one installation."""

    def __init__(self, app: Application | Any) -> None:
        self.app = app
        self.jobs: dict[str, MeetingJob] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def manager(self) -> Any:
        return self.app.manager

    def available(self) -> bool:
        return recogniser_available(self.app.speech, self.app.config)

    def get(self, job_id: str, session_id: str | None = None) -> MeetingJob:
        job = self.jobs.get(job_id)
        if job is None or (session_id is not None and job.session_id != session_id):
            raise KeyError(job_id)
        return job

    def start(self, session_id: str, recording: Path, *, name: str, keep: bool, on_done: Any = None) -> MeetingJob:
        """Begin the notes of one recording; the job runs in the background and ``on_done(job)`` hears its end."""
        finished = [job for job in self.jobs.values() if job.status in ("done", "failed")]
        for old in sorted(finished, key=lambda job: job.started)[: max(0, len(self.jobs) - JOBS_KEPT + 1)]:
            self.jobs.pop(old.id, None)
        job = MeetingJob(id=secrets.token_hex(6), session_id=session_id, name=name)
        self.jobs[job.id] = job
        task = asyncio.create_task(self._run(job, recording, keep, on_done), name=f"meeting:{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: MeetingJob, recording: Path, keep: bool, on_done: Any) -> None:
        try:
            transcript = await self.transcribe(job, recording)
            state = await self.manager.get_state(job.session_id)
            if state is None:
                raise RuntimeError("the session is gone")
            inbox = state.workspace / "inbox"
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            if keep:
                kept = inbox / f"meeting-{stamp}{recording.suffix.lower()}"
                await asyncio.to_thread(recording.replace, kept)
                job.kept = str(kept.relative_to(state.workspace))
            job.status = "writing"
            notes = await self.write_notes(job.session_id, transcript)
            job.notes = notes
            document = inbox / f"meeting-{stamp}.md"
            body = notes_markdown(notes) + "\n---\n\n## Transcript\n\n" + transcript + "\n"
            await asyncio.to_thread(document.write_text, body, encoding="utf-8")
            job.file = str(document.relative_to(state.workspace))
            job.status = "done"
        except Exception as exc:  # noqa: BLE001 — every failure is the job's, shown where it was asked for
            logger.warning("meeting notes %s failed: %s", job.id, exc)
            job.status = "failed"
            job.error = str(exc) or type(exc).__name__
        finally:
            if not job.kept:
                # The words are in hand or the job failed: either way the audio does not stay unasked.
                recording.unlink(missing_ok=True)
        if on_done is not None:
            try:
                await on_done(job)
            except Exception:  # noqa: BLE001 — a reply that could not be sent leaves the job readable
                logger.warning("could not report meeting notes %s", job.id, exc_info=True)

    async def transcribe(self, job: MeetingJob, recording: Path) -> str:
        """The recording's words, a piece at a time so ``job`` says how far along it is.

        A recording nothing here can decode goes to the transcriber whole, as one piece: the endpoint
        reads more containers than this host does, and a short enough one still works.
        """
        job.status = "transcribing"
        config = self.app.config
        speech = self.app.speech
        try:
            pcm, rate = await decode_file(recording)
        except SpeechError:
            job.total = 1
            words = await transcribe_recording(speech, config, self.manager, recording)
            job.done = 1
            return words
        job.seconds = len(pcm) / 2 / max(rate, 1)
        if job.seconds > config.assist.meeting_max_seconds:
            raise RuntimeError(f"the recording is {job.seconds / 60:.0f} min long; meeting notes take up to {config.assist.meeting_max_seconds // 60} min (assist.meeting_max_seconds)")
        window = min(float(config.asr.chunk_seconds), CLOUD_PIECE_BYTES / (2 * max(rate, 1)))
        pieces = split_pcm(pcm, rate, window)
        job.total = len(pieces)
        heard: list[str] = []
        for number, piece in enumerate(pieces):
            part = recording.with_name(f"{recording.stem}.meeting{number:03d}.wav")
            await asyncio.to_thread(part.write_bytes, wav_bytes(piece.pcm16, rate))
            try:
                heard.append(await transcribe_recording(speech, config, self.manager, part))
            except Exception as exc:
                message = str(exc)
                if "empty" in message or "heard nothing" in message:
                    heard.append("")  # a silent stretch of a meeting is not a failure of it
                else:
                    raise RuntimeError(f"piece {number + 1} of {len(pieces)}: {message}") from exc
            finally:
                part.unlink(missing_ok=True)
            job.done = number + 1
        words = join_transcripts(heard, [piece.overlaps for piece in pieces])
        if not words.strip():
            raise RuntimeError("nothing was heard in the recording")
        return words

    async def write_notes(self, session_id: str, transcript: str) -> dict[str, Any]:
        """The summary, decisions and action items, by the session's own model."""
        manager = self.manager
        rungs, _ = manager.resolve_model(await manager.live.load(session_id))
        if not rungs:
            raise RuntimeError("no model is configured")
        provider, model = rungs[0]
        if manager.budget_exceeded() and not manager.provider_costs_nothing(provider.endpoint.id, model):
            raise RuntimeError("the daily inference budget is spent; the transcript is kept in the session inbox")
        extra = ""
        if len(transcript) > TRANSCRIPT_CHARS:
            half = TRANSCRIPT_CHARS // 2
            transcript = transcript[:half] + "\n[… the middle of the meeting is left out …]\n" + transcript[-half:]
            extra = "The middle of the transcript was left out for length; say so in the summary."
        request = LLMRequest(
            model=model,
            messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text=NOTES_PROMPT.format(extra=extra) + transcript)])],
            max_tokens=3000,
            temperature=0.2,
            extra={"enable_thinking": False},
            observability=LLMObservabilityContext(tenant_id=TENANT, session_id=session_id, call_purpose=PURPOSE, call_category="assist"),
        )
        response = await asyncio.wait_for(provider.complete_text(request), timeout=300)
        answer = "".join(block.text for block in response.message.content_blocks if isinstance(block, TextBlock))
        return parse_notes(answer)

    async def add_to_calendar(self, job: MeetingJob, day: date) -> dict[str, Any]:
        """The action items as one entry with a checklist on ``day``."""
        from daedalus.extensions.api_calendar import zone_hint  # Lazy: the routes module imports this one
        from daedalus.stores.calendar import OPERATOR, CalendarStore

        if job.status != "done":
            raise RuntimeError("the notes are not written yet")
        event = await CalendarStore(self.app.db, zone_hint(self.app)).create(calendar_body(job.notes, day, file=job.file), OPERATOR)
        job.calendar_event = str(event.get("id") or "")
        return event

    async def send_to_chat(self, job: MeetingJob, *, via: str = "app") -> str:
        """The notes as the operator's message in the session, with the file the transcript is in."""
        if job.status != "done":
            raise RuntimeError("the notes are not written yet")
        text = notes_markdown(job.notes) + f"\nThe full transcript is in {job.file}."
        return await self.manager.submit(job.session_id, text, follow_up=True, via=via)


def safe_suffix(name: str, content_type: str = "") -> str:
    """The recording's extension as a file name may carry it here, or a neutral one."""
    suffix = Path(name or "").suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        return suffix
    guess = {"audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/webm": ".webm"}
    return guess.get(content_type.split(";")[0].strip().lower(), ".audio")


async def install(app: Application) -> list[asyncio.Task[None]]:
    service = MeetingService(app)
    app.extensions["meetings"] = service
    front = getattr(app, "front", None)
    if front is not None:
        front.meetings = service
        front.callback_hooks["mt"] = front.on_meeting_callback
    return []


__all__ = ["MeetingJob", "MeetingService", "calendar_body", "notes_markdown", "parse_notes", "safe_suffix"]
