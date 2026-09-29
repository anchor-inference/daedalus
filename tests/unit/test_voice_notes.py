"""Voice notes that are long, that fail, and that fall back: nothing the operator said is lost.

The hosted endpoint in use refuses a recording over five minutes, or over about ten megabytes, with a
bare HTTP 400. The fake endpoint here refuses exactly that, so a long recording is proved to reach it
in pieces it accepts; a failing one is proved to leave its audio on the disk for a retry.
"""

from __future__ import annotations

import asyncio
import math
import struct
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from daedalus.config import AsrConfig, RuntimeConfig, Settings
from daedalus.extensions.api import KEPT_RECORDINGS, build_app
from daedalus.host.component_install import Installer
from daedalus.speech import chunks
from daedalus.speech.chunks import join_transcripts, split_pcm
from daedalus.speech.service import (
    CLOUD_PIECE_BYTES,
    LocalSpeech,
    Transcriber,
    note_transcribers,
    transcribe_in_pieces,
    transcribe_recording,
)
from daedalus.speech.tts_service import LocalTts
from daedalus.transport.telegram import voice as transport_voice
from daedalus.transport.telegram.voice import TranscriptionError

RATE = 16_000

PROVIDER_MAX_SECONDS = 300
PROVIDER_MAX_BYTES = 10 << 20


def _tone(seconds: float, rate: int = RATE) -> bytes:
    one = struct.pack(f"<{rate}h", *(int(9000 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(rate)))
    whole, part = divmod(int(seconds * rate), rate)
    return one * whole + one[: part * 2]


def _quiet(seconds: float, rate: int = RATE) -> bytes:
    return b"\x00\x00" * int(seconds * rate)


def _talk(seconds: float, rate: int = RATE) -> bytes:
    """Speech-like: four and a half seconds of sound, then half a second's pause, over and over."""
    phrase = _tone(4.5, rate) + _quiet(0.5, rate)
    count = int(seconds // 5) + 1
    return (phrase * count)[: int(seconds * rate) * 2]


def _wav(path: Path, pcm: bytes, rate: int = RATE) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return path


def _duration(blob: bytes) -> float:
    import io

    with wave.open(io.BytesIO(blob), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


# -- cutting and joining ------------------------------------------------------------------------


def test_a_recording_that_fits_is_one_piece_untouched() -> None:
    pcm = _talk(20)
    pieces = split_pcm(pcm, RATE, 60)
    assert len(pieces) == 1 and pieces[0].pcm16 is pcm and not pieces[0].overlaps


def test_a_long_recording_is_cut_in_its_pauses_and_nothing_is_lost_or_doubled() -> None:
    pcm = _talk(7 * 60)
    pieces = split_pcm(pcm, RATE, 120)
    assert len(pieces) == 4
    assert b"".join(p.pcm16 for p in pieces) == pcm, "cut in pauses, the pieces are the recording exactly"
    assert all(len(p.pcm16) / 2 / RATE <= 120 for p in pieces)
    assert not any(p.overlaps for p in pieces)
    for piece in pieces[:-1]:
        tail = piece.pcm16[-int(0.01 * RATE) * 2 :]
        assert max(abs(v) for v in struct.unpack(f"<{len(tail) // 2}h", tail)) == 0, "every cut falls in silence"


def test_talk_with_no_pause_is_cut_at_the_edge_with_an_overlap() -> None:
    pcm = _tone(250)
    pieces = split_pcm(pcm, RATE, 100)
    assert [p.overlaps for p in pieces] == [False, True, True]
    shared = int(chunks.OVERLAP_SECONDS * RATE) * 2
    assert sum(len(p.pcm16) for p in pieces) == len(pcm) + shared * 2
    assert pieces[1].pcm16[:shared] == pieces[0].pcm16[-shared:]


def test_the_join_skips_silence_and_drops_words_heard_twice_only_at_an_overlap() -> None:
    assert join_transcripts(["Hello there,", "", "  ", "how are you"]) == "Hello there, how are you"
    assert join_transcripts(["we went to the", "the shop"], [False, True]) == "we went to the shop"
    assert join_transcripts(["it was that", "That day it rained"], [False, True]) == "it was that day it rained"
    # Cut in a pause, a word said twice was said twice.
    assert join_transcripts(["I said that", "that twice"], [False, False]) == "I said that that twice"


# -- the endpoint, with the provider's limits -------------------------------------------------


class Provider:
    """An ``/audio/transcriptions`` endpoint that refuses what the real one refuses, and names pieces."""

    def __init__(self, fail: int | None = None) -> None:
        self.durations: list[float] = []
        self.fail = fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(self.fail, json={"error": {"message": "upstream is down", "code": self.fail}})
        body = request.content
        start = body.index(b"RIFF")
        blob = body[start : body.rindex(b"\r\n--")]
        seconds = _duration(blob)
        if seconds > PROVIDER_MAX_SECONDS or len(blob) > PROVIDER_MAX_BYTES:
            return httpx.Response(400, json={"error": {"message": "Provider returned 400", "code": 400}})
        self.durations.append(seconds)
        return httpx.Response(200, json={"text": f"piece{len(self.durations)}"})


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Provider:
    fake = Provider()
    original = transport_voice.transcribe

    async def through_fake(path: Path, config: Any, **kw: Any) -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            return await original(path, config, client=client, **kw)

    monkeypatch.setattr(transport_voice, "transcribe", through_fake)
    return fake


ENDPOINT = AsrConfig(url="http://asr.test/v1", model="qwen/qwen3-asr-flash")


def test_the_400_is_a_recording_over_the_providers_limit_and_says_so(tmp_path: Path, provider: Provider) -> None:
    long = _wav(tmp_path / "long.wav", _talk(6 * 60))
    with pytest.raises(TranscriptionError, match="HTTP 400: Provider returned 400"):
        asyncio.run(transport_voice.transcribe(long, ENDPOINT))


def test_a_long_recording_reaches_the_endpoint_in_pieces_it_accepts(tmp_path: Path, provider: Provider) -> None:
    long = _wav(tmp_path / "long.wav", _talk(7 * 60))
    words = asyncio.run(transcribe_in_pieces(long, ENDPOINT))
    assert words == "piece1 piece2 piece3", "in order, joined"
    assert all(d <= ENDPOINT.chunk_seconds for d in provider.durations)
    assert sum(provider.durations) == pytest.approx(7 * 60, abs=0.1)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["long.wav"], "the pieces are cleaned up, the recording is not"


def test_a_heavy_recording_is_cut_by_its_size_as_well_as_its_length(tmp_path: Path, provider: Provider) -> None:
    # Two minutes at 48 kHz is eleven and a half megabytes: under the length limit, over the size one.
    heavy = _wav(tmp_path / "heavy.wav", _talk(120, 48_000), 48_000)
    assert heavy.stat().st_size > PROVIDER_MAX_BYTES
    asyncio.run(transcribe_in_pieces(heavy, ENDPOINT))
    assert len(provider.durations) == 2
    assert all(d * 48_000 * 2 <= CLOUD_PIECE_BYTES for d in provider.durations)


def test_a_short_recording_goes_whole(tmp_path: Path, provider: Provider) -> None:
    short = _wav(tmp_path / "short.wav", _talk(30))
    assert asyncio.run(transcribe_in_pieces(short, ENDPOINT)) == "piece1"
    assert provider.durations == [pytest.approx(30, abs=0.01)]


# -- which transcriber, and the fallback -------------------------------------------------------


class FakeManager:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    async def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return []

    async def get_state(self, session_id: str) -> Any:
        return SimpleNamespace(workspace=self.workspace) if session_id == "s1" else None

    class providers:  # noqa: N801
        @staticmethod
        def available() -> list[str]:
            return []


class FakeApp:
    def __init__(self, tmp_path: Path, asr: dict[str, Any] | None = None) -> None:
        self.settings = Settings(_env_file=None, state_dir=tmp_path / "state")  # type: ignore[call-arg]
        config = RuntimeConfig()
        self.config = config.model_copy(update={"asr": config.asr.model_copy(update=asr or {})})
        self.manager = FakeManager(tmp_path / "workspace")
        self.front: Any = None
        self.extensions: dict[str, Any] = {}
        self.speech = LocalSpeech(self.settings.state_dir, self.config)
        self.tts = LocalTts(self.settings.state_dir, self.config)
        self.components = Installer(self.settings)
        self.heard: list[str] = []

    async def save_config(self, config: RuntimeConfig) -> None:
        self.config = config
        self.speech.config = config
        self.tts.config = config

    def install(self, model_id: str) -> None:
        """Make a catalog model look downloaded, and answer for it without an engine."""
        from daedalus.speech.models import Installed

        directory = self.speech.downloads.directory(model_id)
        directory.mkdir(parents=True, exist_ok=True)
        manifest = self.speech.downloads.manifest()
        manifest[model_id] = Installed(id=model_id, archive="a", sha256="", disk_bytes=1)
        self.speech.downloads._write_manifest(manifest)

        async def hear(path: Path, model: str = "") -> str:
            self.heard.append(model)
            return f"heard locally by {model}"

        self.speech.transcribe_file = hear  # type: ignore[method-assign]


def test_the_chain_is_the_transcriber_then_the_fallback_and_only_what_can_run(tmp_path: Path) -> None:
    app = FakeApp(tmp_path, {"url": "http://asr.test/v1", "fallback": "gigaam-ru"})
    assert note_transcribers(app.speech, app.config) == [Transcriber("cloud")], "a fallback that is not installed is left out"
    app.install("gigaam-ru")
    assert note_transcribers(app.speech, app.config) == [Transcriber("cloud"), Transcriber("local", "gigaam-ru")]
    app.config = app.config.model_copy(update={"asr": app.config.asr.model_copy(update={"transcriber": "gigaam-ru", "fallback": "cloud"})})
    assert note_transcribers(app.speech, app.config) == [Transcriber("local", "gigaam-ru"), Transcriber("cloud")]
    app.config = app.config.model_copy(update={"asr": app.config.asr.model_copy(update={"fallback": "gigaam-ru"})})
    assert note_transcribers(app.speech, app.config) == [Transcriber("local", "gigaam-ru")], "the same one twice is once"


def test_a_local_transcriber_is_asked_with_its_own_model(tmp_path: Path) -> None:
    app = FakeApp(tmp_path, {"transcriber": "gigaam-ru"})
    app.install("gigaam-ru")
    words = asyncio.run(transcribe_recording(app.speech, app.config, app.manager, _wav(tmp_path / "a.wav", _talk(3))))
    assert words == "heard locally by gigaam-ru" and app.heard == ["gigaam-ru"]


def test_when_the_endpoint_fails_the_local_fallback_hears_it(tmp_path: Path, provider: Provider) -> None:
    provider.fail = 400
    app = FakeApp(tmp_path, {"url": "http://asr.test/v1", "fallback": "gigaam-ru"})
    app.install("gigaam-ru")
    words = asyncio.run(transcribe_recording(app.speech, app.config, app.manager, _wav(tmp_path / "a.wav", _talk(3))))
    assert words == "heard locally by gigaam-ru"


def test_when_both_fail_both_reasons_are_given(tmp_path: Path, provider: Provider) -> None:
    provider.fail = 503
    app = FakeApp(tmp_path, {"url": "http://asr.test/v1", "fallback": "gigaam-ru"})
    app.install("gigaam-ru")

    async def deaf(path: Path, model: str = "") -> str:
        return ""

    app.speech.transcribe_file = deaf  # type: ignore[method-assign]
    with pytest.raises(TranscriptionError) as caught:
        asyncio.run(transcribe_recording(app.speech, app.config, app.manager, _wav(tmp_path / "a.wav", _talk(3))))
    assert "HTTP 503: upstream is down" in str(caught.value) and "heard nothing" in str(caught.value)


# -- the route keeps what it could not transcribe -----------------------------------------------

HEAD = {"X-Daedalus-Token": "tok"}


def test_a_failed_transcription_keeps_the_audio_and_a_retry_by_name_uses_it(tmp_path: Path, provider: Provider) -> None:
    provider.fail = 400
    app = FakeApp(tmp_path, {"url": "http://asr.test/v1"})
    audio = _wav(tmp_path / "note.wav", _talk(4)).read_bytes()
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        failed = client.post("/api/sessions/s1/transcribe", files={"audio": ("recording.wav", audio, "audio/wav")}, headers=HEAD)
        assert failed.status_code == 502
        detail = failed.json()["detail"]
        assert "HTTP 400" in detail["message"]
        kept = tmp_path / "workspace" / "inbox" / KEPT_RECORDINGS / detail["recording"]
        assert kept.read_bytes() == audio, "the recording is on the disk, whole"

        provider.fail = None
        again = client.post("/api/sessions/s1/transcribe", data={"recording": detail["recording"]}, headers=HEAD)
        assert again.status_code == 200 and "piece1" in again.json()["text"]
        assert not kept.exists(), "once its words are out, the recording goes"

        gone = client.post("/api/sessions/s1/transcribe", data={"recording": detail["recording"]}, headers=HEAD)
        assert gone.status_code == 404
        assert client.post("/api/sessions/s1/transcribe", data={"recording": "../../etc/passwd"}, headers=HEAD).status_code == 404


def test_a_kept_recording_can_be_let_go(tmp_path: Path, provider: Provider) -> None:
    provider.fail = 500
    app = FakeApp(tmp_path, {"url": "http://asr.test/v1"})
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        name = client.post("/api/sessions/s1/transcribe", files={"audio": ("r.wav", _wav(tmp_path / "n.wav", _talk(2)).read_bytes(), "audio/wav")}, headers=HEAD).json()["detail"]["recording"]
        assert client.delete(f"/api/sessions/s1/transcribe/{name}", headers=HEAD).json() == {"deleted": True}
        assert not any((tmp_path / "workspace" / "inbox" / KEPT_RECORDINGS).iterdir())


def test_the_site_is_told_the_chain(tmp_path: Path) -> None:
    app = FakeApp(tmp_path, {"url": "http://asr.test/v1", "fallback": "gigaam-ru"})
    app.install("gigaam-ru")
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        body = client.get("/api/asr", headers=HEAD).json()
    assert body["configured"] is True and body["transcriber"] == "cloud" and body["fallback"] == "gigaam-ru"
    assert body["chain"] == [{"kind": "cloud", "model": ""}, {"kind": "local", "model": "gigaam-ru"}]


def test_a_local_model_hears_a_long_recording_in_pieces(tmp_path: Path) -> None:
    from daedalus.speech.service import LOCAL_PIECE_SECONDS

    app = FakeApp(tmp_path)
    app.install("gigaam-ru")
    del app.speech.transcribe_file  # the real one this time; only the engine is a fake
    lengths: list[float] = []

    class Engine:
        async def transcribe(self, pcm16: bytes, sample_rate: int) -> str:
            lengths.append(len(pcm16) / 2 / sample_rate)
            return f"part{len(lengths)}"

    async def engine(model: Any = None) -> Engine:
        assert model is not None and model.id == "gigaam-ru"
        return Engine()

    app.speech.engine = engine  # type: ignore[method-assign]
    words = asyncio.run(app.speech.transcribe_file(_wav(tmp_path / "a.wav", _talk(100)), model="gigaam-ru"))
    assert words == "part1 part2 part3 part4"
    assert all(length <= LOCAL_PIECE_SECONDS for length in lengths) and sum(lengths) == pytest.approx(100, abs=0.01)
