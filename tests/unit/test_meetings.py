"""Meeting notes: a recording transcribed in pieces with its progress, notes by the session's model,
the audio gone unless kept, and the action items as one calendar entry with a checklist."""

from __future__ import annotations

import asyncio
import io
import json
import math
import struct
import wave
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import AsrConfig, Settings
from daedalus.extensions import meetings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.calendar import CalendarStore
from daedalus.stores.database import Database
from daedalus.transport.telegram.front import TelegramFront
from tests.support.models import model_config

NOTES = {"title": "Release sync", "summary": "We agreed to ship on Friday.", "decisions": ["Ship on Friday"], "actions": ["Anna: write the changelog by Thursday", {"text": "Run the full suite"}]}


def recording(seconds: float, rate: int = 16000) -> bytes:
    """A mono 16-bit WAV of a quiet tone, so the cutter finds no pause and overlaps its pieces."""
    frames = b"".join(struct.pack("<h", int(800 * math.sin(i / 7))) for i in range(int(seconds * rate)))
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return out.getvalue()


class NotesProvider:
    endpoint = SimpleNamespace(id="local")

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def complete_text(self, request: Any) -> Any:
        self.requests.append(request)
        return SimpleNamespace(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Here they are:\n" + json.dumps(NOTES))]))


def test_the_notes_are_read_from_the_models_answer() -> None:
    notes = meetings.parse_notes("```json\n" + json.dumps(NOTES) + "\n```")
    assert notes["title"] == "Release sync" and notes["actions"] == ["Anna: write the changelog by Thursday", "Run the full suite"]
    assert meetings.parse_notes('{"summary": "only this", "decisions": "not a list"}')["decisions"] == []
    with pytest.raises(ValueError):
        meetings.parse_notes("no notes today")
    text = meetings.notes_markdown(notes)
    assert text.startswith("## Release sync") and "- Ship on Friday" in text and "**Action items**" in text


async def test_the_action_items_become_one_entry_with_a_checklist(db: Database) -> None:
    body = meetings.calendar_body(meetings.parse_notes(json.dumps(NOTES)), date(2026, 10, 20), file="inbox/meeting.md")
    event = await CalendarStore(db, lambda: "").create(body)
    assert event["all_day"] and event["title"] == "Action items: Release sync"
    checklist = next(block for block in event["blocks"] if block["kind"] == "checklist")
    assert [item["text"] for item in checklist["items"]] == ["Anna: write the changelog by Thursday", "Run the full suite"]
    assert not any(item["checked"] for item in checklist["items"])


async def _service(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch, *, chunk: int = 60) -> tuple[meetings.MeetingService, SessionManager, list[Path], NotesProvider]:
    config = model_config(asr=AsrConfig(url="http://127.0.0.1:9", chunk_seconds=chunk))
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    heard: list[Path] = []

    async def transcribe(speech: Any, config: Any, manager: Any, path: Path) -> str:
        heard.append(path)
        assert path.exists()
        await asyncio.sleep(0)
        return f"part {len(heard)} said something"

    monkeypatch.setattr(meetings, "transcribe_recording", transcribe)
    provider = NotesProvider()
    manager.resolve_model = lambda overrides: ([(provider, "notes-model")], None)  # type: ignore[method-assign]
    app = SimpleNamespace(manager=manager, config=config, speech=SimpleNamespace(active=lambda: None, installed=lambda _: None), db=db, extensions={})
    return meetings.MeetingService(app), manager, heard, provider


async def _finished(service: meetings.MeetingService, job: meetings.MeetingJob) -> meetings.MeetingJob:
    for _ in range(500):
        if job.status in ("done", "failed"):
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"the job is still {job.status}")


async def test_a_long_recording_is_transcribed_in_pieces_and_then_let_go(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    service, manager, heard, provider = await _service(settings, db, monkeypatch, chunk=60)
    try:
        state = await manager.create_session("s")
        audio = state.workspace / "inbox" / ".meetings" / "abc.wav"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(recording(150))
        job = await _finished(service, service.start(state.session.id, audio, name="standup.wav", keep=False))
        assert job.status == "done", job.error
        assert job.total == 3 and job.done == 3 and len(heard) == 3 and round(job.seconds) == 150
        assert not audio.exists() and not any(path.exists() for path in heard)
        assert job.kept == "" and job.notes["title"] == "Release sync"
        written = (state.workspace / job.file).read_text(encoding="utf-8")
        assert "## Transcript" in written and "part 1 said something" in written and "- Ship on Friday" in written
        request = provider.requests[0]
        assert request.observability.call_purpose == meetings.PURPOSE and request.observability.call_category == "assist"
        assert "part 3 said something" in request.messages[0].content_blocks[0].text
    finally:
        await manager.close()


async def test_a_recording_asked_to_be_kept_stays_in_the_inbox(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    service, manager, _, _ = await _service(settings, db, monkeypatch)
    try:
        state = await manager.create_session("s")
        audio = state.workspace / "inbox" / "note.wav"
        audio.write_bytes(recording(5))
        job = await _finished(service, service.start(state.session.id, audio, name="note.wav", keep=True))
        assert job.status == "done" and job.kept.startswith("inbox/meeting-") and (state.workspace / job.kept).exists()
        assert job.total == 1
    finally:
        await manager.close()


async def test_a_failure_is_the_jobs_and_the_audio_still_goes(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    service, manager, _, _ = await _service(settings, db, monkeypatch)
    try:
        async def refuse(*_: Any) -> str:
            raise RuntimeError("the endpoint said no")

        monkeypatch.setattr(meetings, "transcribe_recording", refuse)
        state = await manager.create_session("s")
        audio = state.workspace / "inbox" / "note.wav"
        audio.write_bytes(recording(5))
        reported: list[str] = []

        async def on_done(job: meetings.MeetingJob) -> None:
            reported.append(job.status)

        job = await _finished(service, service.start(state.session.id, audio, name="note.wav", keep=False, on_done=on_done))
        assert job.status == "failed" and "the endpoint said no" in job.error and not audio.exists()
        await asyncio.sleep(0.01)
        assert reported == ["failed"]
        service.app.config.assist.meeting_max_seconds = 60
        long = state.workspace / "inbox" / "long.wav"
        long.write_bytes(recording(90))
        failed = await _finished(service, service.start(state.session.id, long, name="long.wav", keep=False))
        assert failed.status == "failed" and "assist.meeting_max_seconds" in failed.error
    finally:
        await manager.close()


async def test_the_routes_take_a_recording_and_put_its_items_on_the_calendar(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    service, manager, _, _ = await _service(settings, db, monkeypatch)
    try:
        state = await manager.create_session("s")
        sid = state.session.id
        submitted: list[tuple[str, str]] = []

        async def submit(session_id: str, text: str, *args: Any, **kwargs: Any) -> str:
            submitted.append((session_id, text))
            return "run-1"

        manager.submit = submit  # type: ignore[method-assign]
        app = SimpleNamespace(settings=settings, config=service.app.config, db=db, manager=manager, front=None, extensions={"meetings": service}, guard=None, speech=service.app.speech)
        service.app = app
        api = build_app(app, "tok")  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test", headers={"X-Daedalus-Token": "tok"}) as client:
            started = await client.post(f"/api/sessions/{sid}/meeting", files={"audio": ("call.wav", recording(5), "audio/wav")}, data={"keep": "false"})
            assert started.status_code == 200, started.text
            job_id = started.json()["id"]
            await _finished(service, service.get(job_id))
            view = (await client.get(f"/api/sessions/{sid}/meeting/{job_id}")).json()
            assert view["status"] == "done" and view["notes"]["actions"]
            assert (await client.get(f"/api/sessions/other/meeting/{job_id}")).status_code == 404
            assert (await client.post(f"/api/sessions/{sid}/meeting/{job_id}/calendar", json={"day": "20 Oct"})).status_code == 400
            added = (await client.post(f"/api/sessions/{sid}/meeting/{job_id}/calendar", json={"day": "2026-10-20"})).json()
            assert added["day"] == "2026-10-20" and added["event_id"]
            event = await CalendarStore(db, lambda: "").get(added["event_id"])
            assert event is not None and event["all_day"] and event["start_at"].startswith("2026-10-20")
            sent = (await client.post(f"/api/sessions/{sid}/meeting/{job_id}/send")).json()
            assert sent == {"run_id": "run-1"} and "Ship on Friday" in submitted[0][1] and view["file"] in submitted[0][1]
    finally:
        await manager.close()


def test_telegram_offers_notes_for_audio_files_and_long_voice_notes() -> None:
    front = SimpleNamespace(config=SimpleNamespace(asr=AsrConfig(max_seconds=600)))
    audio = SimpleNamespace(audio=SimpleNamespace(mime_type="audio/mpeg", duration=3600), document=None, voice=None)
    document = SimpleNamespace(audio=None, document=SimpleNamespace(mime_type="audio/x-m4a"), voice=None)
    short = SimpleNamespace(audio=None, document=None, voice=SimpleNamespace(duration=40))
    long = SimpleNamespace(audio=None, document=None, voice=SimpleNamespace(duration=1800))
    pdf = SimpleNamespace(audio=None, document=SimpleNamespace(mime_type="application/pdf"), voice=None)
    offered = [TelegramFront._meeting_audio(front, message) for message in (audio, document, short, long, pdf)]  # type: ignore[arg-type]
    assert offered == [True, True, False, True, False]
