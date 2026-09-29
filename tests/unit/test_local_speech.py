"""Local speech recognition: the catalog, the download manager, the engine's interface and the API."""

from __future__ import annotations

import asyncio
import bz2
import hashlib
import io
import math
import struct
import tarfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.component_install import Installer
from daedalus.speech import catalog
from daedalus.speech.engine import SAMPLE_RATE, Partial, SpeechError, resolve, rms, to_float
from daedalus.speech.models import DownloadError, Downloads, sha256_of, verify, view
from daedalus.speech.service import LocalSpeech, decode_file, is_ogg
from daedalus.speech.tts_service import LocalTts
from tests.support.waiting import SETTLE, until

# -- the catalog ------------------------------------------------------------------------------


def test_every_entry_is_complete_and_unique() -> None:
    ids = [m.id for m in catalog.MODELS]
    assert len(ids) == len(set(ids)), "two entries share an id; the directory they install into would collide"
    for model in catalog.MODELS:
        assert model.archive.endswith(".tar.bz2"), model.id
        assert model.url.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/"), model.id
        assert model.size_bytes > 1_000_000 and model.unpacked_bytes >= model.size_bytes, model.id
        assert model.memory_mb > 0 and model.licence, model.id
        assert 0 < model.accuracy <= 100 and 0 < model.speed <= 100, model.id
        assert model.languages and model.note, model.id
        assert model.languages_shown >= len(model.languages), model.id
        # A checksum is optional, but one that is there must be a sha256 and not a placeholder.
        assert not model.sha256 or len(model.sha256) == 64, model.id


def test_only_the_two_archives_without_a_published_digest_lack_one() -> None:
    """GitHub reports a digest per release asset, and all but two of the catalog's assets predate it.

    The two that do not carry one say so — ``verified`` is what the picker shows — and are pinned by
    their file list instead, which a substituted archive padded to the published byte count fails.
    """
    hashless = sorted(m.id for m in catalog.MODELS if not m.verified)
    assert hashless == ["moonshine-base-en", "whisper-small"]
    for model in catalog.MODELS:
        assert model.verified != bool(model.contents), f"{model.id}: one check or the other, not both or neither"
        if model.contents:
            assert len(model.contents) == len(set(model.contents)) and all("/" not in n or ".." not in n for n in model.contents)
            assert any(n.endswith(".onnx") for n in model.contents) and any("tokens" in n for n in model.contents)


def test_whisper_is_the_only_kind_that_cannot_detect_its_own_language() -> None:
    """``auto`` pins Whisper to English, so the picker must not offer it as a detection."""
    assert not catalog.get("whisper-small").detects_language
    assert not catalog.get("whisper-tiny").detects_language
    assert catalog.get("gigaam-ru").detects_language and catalog.get("sense-voice").detects_language
    assert catalog.as_json(catalog.get("whisper-small"))["detects_language"] is False


def test_every_kind_is_one_the_engine_can_build() -> None:
    known = {"transducer", "nemo_transducer", "whisper", "moonshine", "sense_voice", "ctc", "t_one_ctc"}
    assert {m.kind for m in catalog.MODELS} <= known


def test_the_catalog_covers_what_the_operator_actually_speaks() -> None:
    """English and Russian both need a streaming model and a careful one; Russian is the owner's."""
    russian = [m for m in catalog.MODELS if "ru" in m.languages or "ru" in m.aliases]
    assert any(m.streaming for m in russian) and any(not m.streaming for m in russian)
    english = [m for m in catalog.MODELS if "en" in m.languages]
    assert any(m.streaming for m in english) and any(m.size_bytes < 150_000_000 for m in english)
    assert any(m.languages_shown >= 25 and m.streaming for m in catalog.MODELS), "no multilingual streaming model"
    assert any(m.kind == "whisper" and m.languages_shown > 50 for m in catalog.MODELS)


def test_recommendations_are_per_language_not_one_flag() -> None:
    assert catalog.recommended("ru").id == "gigaam-ru"
    assert catalog.recommended("en").id == "parakeet-unified-en"
    # A language with no recommendation of its own falls back to a multilingual model that speaks it.
    assert catalog.recommended("de").languages_shown >= 25
    assert catalog.recommended("xx") is None


def test_speaks_accepts_auto_and_rejects_what_it_cannot() -> None:
    gigaam = catalog.get("gigaam-ru")
    assert gigaam.speaks("ru") and gigaam.speaks("auto") and gigaam.speaks("")
    assert not gigaam.speaks("ja")
    assert catalog.get("sense-voice").speaks("yue"), "aliases are part of what a model speaks"
    with pytest.raises(KeyError, match="no speech model"):
        catalog.get("nope")


# -- finding the files inside an unpacked archive ------------------------------------------------


def _files(directory: Path, *names: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"onnx")
    return directory


def test_model_files_are_found_by_shape_and_prefer_the_quantised_build(tmp_path: Path) -> None:
    whisper = _files(tmp_path / "w", "tiny-encoder.onnx", "tiny-encoder.int8.onnx", "tiny-decoder.onnx", "tiny-decoder.int8.onnx", "tiny-tokens.txt")
    found = resolve(whisper, "whisper")
    assert found["encoder"].name == "tiny-encoder.int8.onnx" and found["decoder"].name == "tiny-decoder.int8.onnx"
    assert found["tokens"].name == "tiny-tokens.txt"

    transducer = _files(tmp_path / "t", "encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt")
    assert set(resolve(transducer, "transducer")) == {"encoder", "decoder", "joiner", "tokens"}

    moonshine = _files(tmp_path / "m", "preprocess.onnx", "encode.int8.onnx", "uncached_decode.int8.onnx", "cached_decode.int8.onnx", "tokens.txt")
    assert resolve(moonshine, "moonshine")["preprocessor"].name == "preprocess.onnx"

    single = _files(tmp_path / "s", "model.int8.onnx", "tokens.txt")
    assert set(resolve(single, "sense_voice")) == {"model", "tokens"}


def test_an_incomplete_download_is_refused_where_it_is_unpacked(tmp_path: Path) -> None:
    short = _files(tmp_path / "x", "encoder.onnx", "tokens.txt")
    with pytest.raises(SpeechError, match="decoder"):
        resolve(short, "transducer")
    with pytest.raises(SpeechError, match="tokens"):
        resolve(_files(tmp_path / "y", "encoder.onnx", "decoder.onnx", "joiner.onnx"), "transducer")
    with pytest.raises(SpeechError, match="unknown model kind"):
        resolve(short, "no-such-kind")


# -- samples ------------------------------------------------------------------------------------


def tone(seconds: float, *, hz: int = 220, level: float = 0.3, rate: int = SAMPLE_RATE) -> bytes:
    """A sine, which is loud enough to count as speech to a level detector and is not silence."""
    count = int(seconds * rate)
    return struct.pack(f"<{count}h", *(int(level * 32767 * math.sin(2 * math.pi * hz * i / rate)) for i in range(count)))


def quiet(seconds: float, rate: int = SAMPLE_RATE) -> bytes:
    return b"\x00\x00" * int(seconds * rate)


def test_samples_convert_and_measure() -> None:
    floats = to_float(struct.pack("<4h", 0, 32767, -32768, 16384))
    assert floats[0] == 0 and floats[1] == pytest.approx(1.0, abs=1e-4) and floats[2] == -1.0
    assert rms(b"") == 0 and rms(quiet(0.1)) == 0 and rms(tone(0.1)) > 380


# -- the stream session, with a fake recogniser behind the same interface -------------------------


class FakeRecognizer:
    """Stands in for sherpa's recogniser: the engine's wheel is optional and CI has no model.

    It answers the same calls in the same order, which is what the session actually depends on —
    everything else in :class:`StreamSession` is buffering and endpointing, and that is what is
    being tested.
    """

    def __init__(self, text: str = "hello there") -> None:
        self.text = text
        self.decoded = 0

    def create_stream(self) -> Any:
        return type("S", (), {"accept_waveform": lambda self, rate, data: None, "input_finished": lambda self: None, "result": type("R", (), {"text": self.text})()})()

    def decode_stream(self, stream: Any) -> None:
        self.decoded += 1


class FakeEngine:
    """The engine's surface as :class:`StreamSession` uses it, with a batch model's answers."""

    def __init__(self, model: catalog.SpeechModel, text: str = "hello there") -> None:
        self.model = model
        self.lock = threading.Lock()
        self.recognizer = FakeRecognizer(text)
        self.text = text
        self.decodes: list[int] = []

    def decode(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        self.decodes.append(len(pcm16))
        return self.text


def batch_session() -> Any:
    from daedalus.speech.engine import StreamSession

    return StreamSession(FakeEngine(catalog.get("gigaam-ru")))


async def test_a_batch_model_says_nothing_until_the_talking_stops() -> None:
    session = batch_session()
    for _ in range(10):
        answer = await session.feed(tone(0.1))
        assert answer == Partial(text="", final=False), "a batch model has no words mid-sentence and must not invent any"
    # Silence past the endpoint: the buffer is decoded and one utterance comes back.
    finals = [await session.feed(quiet(0.1)) for _ in range(12)]
    assert any(f.final and f.text == "hello there" for f in finals)
    assert session._engine.decodes and session._engine.decodes[0] > SAMPLE_RATE, "the whole utterance is decoded, not one chunk"


async def test_a_cough_is_not_an_utterance() -> None:
    session = batch_session()
    await session.feed(tone(0.1))
    answer = await session.finish()
    assert answer == Partial(text="", final=True) and not session._engine.decodes


async def test_a_batch_model_decodes_a_speaker_who_never_pauses() -> None:
    from daedalus.speech.engine import BATCH_MAX_SECONDS

    session = batch_session()
    answers = [await session.feed(tone(0.5)) for _ in range(int(BATCH_MAX_SECONDS / 0.5) + 2)]
    assert any(a.final for a in answers), "an unbroken monologue must still be transcribed"


async def test_a_blip_followed_by_silence_does_not_wedge_the_session() -> None:
    """The failure this guards: one loud chunk opens the buffer, and then nothing can ever close it.

    Accumulation starts at the first loud chunk, but the flush is behind ``MIN_UTTERANCE_SECONDS``,
    which silence cannot grow — so a cough followed by quiet buffered every later chunk forever and
    the session stopped emitting finals. A real sentence afterwards never reached the agent.
    """
    from daedalus.speech.engine import BATCH_MAX_SECONDS

    session = batch_session()
    await session.feed(tone(0.1))
    for _ in range(int(BATCH_MAX_SECONDS / 0.5) + 2):
        await session.feed(quiet(0.5))
    assert not session._buffer, "the blip was thrown away rather than held against a flush that cannot come"
    assert not session._engine.decodes, "and nothing was sent to the model, because nothing was said"

    # The session is usable again: a sentence now produces a final, which is the half that matters.
    for _ in range(10):
        await session.feed(tone(0.1))
    finals = [await session.feed(quiet(0.1)) for _ in range(12)]
    assert any(f.final and f.text == "hello there" for f in finals)


async def test_a_batch_session_holds_a_bounded_number_of_bytes() -> None:
    """The ceiling is in bytes, so a client that declares a wrong rate cannot lift it."""
    from daedalus.speech.engine import MAX_BUFFER_BYTES

    session = batch_session()
    await session.feed(tone(0.1), 48_000)
    for _ in range(400):
        await session.feed(tone(0.5), 48_000)
        assert len(session._buffer) <= MAX_BUFFER_BYTES


async def test_the_rate_is_fixed_at_the_first_chunk_and_bounded() -> None:
    from daedalus.speech.engine import SpeechError, clamp_rate

    session = batch_session()
    await session.feed(tone(0.1, rate=48_000), 48_000)
    assert session._rate == 48_000
    # A later chunk claiming another rate does not relabel the audio already held.
    await session.feed(tone(0.1, rate=48_000), 8_000)
    assert session._rate == 48_000

    for bad in (0, 1, 10**9, -16_000):
        with pytest.raises(SpeechError):
            clamp_rate(bad)
    assert clamp_rate(44_100) == 44_100


async def test_the_declared_rate_reaches_the_model() -> None:
    """A 48 kHz stream is decoded as 48 kHz. Telling the model 16 kHz is how it hears speech at 3x."""
    session = batch_session()
    rates: list[int] = []
    session._engine.decode = lambda pcm, rate=SAMPLE_RATE: rates.append(rate) or "hello there"  # type: ignore[assignment]
    for _ in range(10):
        await session.feed(tone(0.1, rate=48_000), 48_000)
    for _ in range(12):
        await session.feed(quiet(0.1, 48_000), 48_000)
    assert rates and set(rates) == {48_000}


def test_a_moonshine_archive_does_not_load_the_uncached_graph_as_the_cached_one(tmp_path: Path) -> None:
    """`cached_decode` is a substring of `uncached_decode`; the two are different graphs."""
    from daedalus.speech.engine import resolve

    directory = tmp_path / "moonshine"
    directory.mkdir()
    for name in ("preprocess.onnx", "encode.int8.onnx", "cached_decode.int8.onnx", "uncached_decode.int8.onnx"):
        (directory / name).write_bytes(b"x")
    (directory / "tokens.txt").write_bytes(b"<blk> 0\n")
    files = resolve(directory, "moonshine")
    assert files["cached_decoder"].name == "cached_decode.int8.onnx"
    assert files["uncached_decoder"].name == "uncached_decode.int8.onnx"


async def test_finishing_flushes_what_is_held() -> None:
    session = batch_session()
    for _ in range(6):
        await session.feed(tone(0.1))
    answer = await session.finish()
    assert answer.final and answer.text == "hello there"


# -- the download manager --------------------------------------------------------------------------


def make_archive(name: str, files: dict[str, bytes]) -> bytes:
    """A .tar.bz2 with one top-level directory, exactly as the model zoo ships one."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for filename, body in files.items():
            info = tarfile.TarInfo(f"{name}/{filename}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return bz2.compress(raw.getvalue())


MODEL_FILES = {
    "encoder.int8.onnx": b"E" * 2048,
    "decoder.onnx": b"D" * 2048,
    "joiner.onnx": b"J" * 2048,
    "tokens.txt": b"<blk> 0\n",
}


class Zoo(BaseHTTPRequestHandler):
    """A model host that honours ranges, so resuming is exercised against something that resumes."""

    payload = b""
    served: list[str] = []
    stall_after = 0
    hold = 0.0

    def do_GET(self) -> None:  # noqa: N802
        body = Zoo.payload
        start = 0
        header = self.headers.get("range", "")
        Zoo.served.append(header or "whole")
        if header.startswith("bytes="):
            start = int(header[6:].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            self.send_response(200)
        chunk = body[start:]
        if Zoo.stall_after:
            chunk = chunk[: Zoo.stall_after]
        self.send_header("Content-Length", str(len(body) - start))
        self.end_headers()
        self.wfile.write(chunk)
        self.wfile.flush()
        if Zoo.hold:
            # The rest never comes. A download stuck like this is the only state in which cancelling
            # means anything, so it is the state the test needs.
            time.sleep(Zoo.hold)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def zoo() -> Any:
    server = HTTPServer(("127.0.0.1", 0), Zoo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Zoo.served = []
    Zoo.stall_after = 0
    Zoo.hold = 0.0
    yield f"http://127.0.0.1:{server.server_port}/model.tar.bz2"
    server.shutdown()


def fake_entry(payload: bytes, *, digest: str = "") -> catalog.SpeechModel:
    return catalog.SpeechModel(
        id="test-model", label="Test", archive="test.tar.bz2", kind="transducer", languages=("en",),
        size_bytes=len(payload), unpacked_bytes=len(payload) * 2, memory_mb=10, licence="MIT",
        accuracy=50, speed=50, note="for the tests", sha256=digest,
    )


async def install(tmp_path: Path, url: str, model: catalog.SpeechModel) -> Downloads:
    downloads = Downloads(tmp_path / "stt")
    catalog.BY_ID[model.id] = model
    try:
        downloads.start(model.id, url=url)
        for _ in range(400):
            await asyncio.sleep(0.02)
            state = downloads.progress().get(model.id)
            if state and state.state in ("installed", "failed", "cancelled"):
                break
    finally:
        catalog.BY_ID.pop(model.id, None)
    return downloads


async def test_a_model_is_downloaded_unpacked_under_its_own_id_and_recorded(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("sherpa-onnx-whatever-2026-01-01", MODEL_FILES)
    model = fake_entry(Zoo.payload, digest=hashlib.sha256(Zoo.payload).hexdigest())
    downloads = await install(tmp_path, zoo, model)
    assert downloads.progress()[model.id].state == "installed"
    # The archive's own directory name is the zoo's; what the engine looks for is the catalog id.
    assert (downloads.directory(model.id) / "encoder.int8.onnx").exists()
    assert downloads.is_installed(model.id) and downloads.manifest()[model.id].sha256 == model.sha256
    assert downloads.disk_usage() > 4096
    assert not list((downloads.parts).glob("*.tar.bz2")), "the archive is deleted once it is unpacked"


async def test_a_wrong_checksum_installs_nothing(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("x", MODEL_FILES)
    model = fake_entry(Zoo.payload, digest="0" * 64)
    downloads = await install(tmp_path, zoo, model)
    state = downloads.progress()[model.id]
    assert state.state == "failed" and "checksum" in state.error
    assert not downloads.is_installed(model.id) and not downloads.directory(model.id).exists()


async def test_an_archive_that_is_missing_a_model_file_is_refused(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("x", {"encoder.onnx": b"E" * 2048, "tokens.txt": b"t"})
    downloads = await install(tmp_path, zoo, fake_entry(Zoo.payload))
    state = downloads.progress()["test-model"]
    assert state.state == "failed" and "incomplete" in state.error, "a short archive must fail here, not at the first utterance"
    assert not downloads.is_installed("test-model")


async def test_a_half_finished_download_resumes_rather_than_starting_again(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("x", MODEL_FILES)
    model = fake_entry(Zoo.payload, digest=hashlib.sha256(Zoo.payload).hexdigest())
    downloads = Downloads(tmp_path / "stt")
    downloads.parts.mkdir(parents=True)
    half = len(Zoo.payload) // 2
    (downloads.parts / model.archive).write_bytes(Zoo.payload[:half])
    catalog.BY_ID[model.id] = model
    try:
        downloads.start(model.id, url=zoo)
        await until(lambda: downloads.progress()[model.id].state in ("installed", "failed"), "the download ended")
    finally:
        catalog.BY_ID.pop(model.id, None)
    assert downloads.progress()[model.id].state == "installed"
    assert Zoo.served == [f"bytes={half}-"], "the second attempt asked for the rest, not the whole file"


async def test_a_part_file_longer_than_the_model_is_thrown_away(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("x", MODEL_FILES)
    model = fake_entry(Zoo.payload, digest=hashlib.sha256(Zoo.payload).hexdigest())
    downloads = Downloads(tmp_path / "stt")
    downloads.parts.mkdir(parents=True)
    (downloads.parts / model.archive).write_bytes(b"junk" * len(Zoo.payload))
    catalog.BY_ID[model.id] = model
    try:
        downloads.start(model.id, url=zoo)
        await until(lambda: downloads.progress()[model.id].state in ("installed", "failed"), "the download ended")
    finally:
        catalog.BY_ID.pop(model.id, None)
    assert downloads.progress()[model.id].state == "installed" and Zoo.served == ["whole"]


async def test_a_cancelled_download_keeps_what_arrived(tmp_path: Path, zoo: str) -> None:
    # A payload that never finishes arriving: the host sends part of it and holds the rest, which is
    # the only state in which "cancel" means anything at all.
    Zoo.payload = make_archive("x", MODEL_FILES)
    model = fake_entry(Zoo.payload)
    Zoo.stall_after = 64
    Zoo.hold = 10.0
    downloads = Downloads(tmp_path / "stt")
    catalog.BY_ID[model.id] = model
    try:
        downloads.start(model.id, url=zoo)
        # Cancel it once the request is actually in flight and the host is holding the rest back —
        # which is the state cancelling means anything in, and the thing to wait for.
        await until(lambda: bool(Zoo.served), "the download host was asked for the archive")
        assert downloads.cancel(model.id)
        await until(lambda: downloads.progress()[model.id].state == "cancelled", "the download reported itself cancelled")
        assert downloads.progress()[model.id].state == "cancelled" and not downloads.is_installed(model.id)
        assert not downloads.cancel(model.id), "cancelling what is not running says so rather than pretending"
        with pytest.raises(KeyError):
            downloads.cancel("../../../etc")
    finally:
        catalog.BY_ID.pop(model.id, None)



async def test_progress_reaches_a_watcher_and_a_full_queue_does_not_stall_the_download(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("x", MODEL_FILES)
    model = fake_entry(Zoo.payload)
    downloads = Downloads(tmp_path / "stt")
    catalog.BY_ID[model.id] = model
    seen: list[str] = []
    try:
        async with downloads.watch() as queue:
            downloads.start(model.id, url=zoo)

            def drained() -> bool:
                while not queue.empty():
                    seen.append(queue.get_nowait().state)
                return "installed" in seen or "failed" in seen

            await until(drained, "the download reported itself finished on the watcher")
    finally:
        catalog.BY_ID.pop(model.id, None)
    assert "installed" in seen and seen[0] == "downloading"
    assert not downloads._watchers, "the queue is let go of when the watcher leaves"


def test_deleting_a_model_frees_the_disk_and_forgets_it(tmp_path: Path) -> None:
    downloads = Downloads(tmp_path / "stt")
    directory = downloads.directory("whisper-tiny")
    directory.mkdir(parents=True)
    (directory / "tiny-encoder.onnx").write_bytes(b"x" * 1024)
    downloads._write_manifest({"whisper-tiny": __import__("daedalus.speech.models", fromlist=["Installed"]).Installed(
        id="whisper-tiny", archive="a.tar.bz2", sha256="", disk_bytes=1024)})
    assert downloads.is_installed("whisper-tiny")
    assert asyncio.run(downloads.delete("whisper-tiny")) and not downloads.is_installed("whisper-tiny")
    assert not asyncio.run(downloads.delete("whisper-tiny"))


def test_deleting_a_model_refuses_an_id_that_is_not_one(tmp_path: Path) -> None:
    """``delete`` joins the id to the models root and rmtrees it; a climbing id must not get that far."""
    downloads = Downloads(tmp_path / "stt")
    victim = tmp_path / "IMPORTANT"
    victim.mkdir(parents=True)
    with pytest.raises(KeyError, match="no speech model"):
        asyncio.run(downloads.delete("../../IMPORTANT"))
    assert victim.is_dir()


async def test_deleting_a_model_mid_download_stops_it_and_reclaims_the_part_file(tmp_path: Path, zoo: str) -> None:
    """The task carried on after a delete, re-wrote the manifest and unpacked the model back."""
    Zoo.payload = make_archive("x", MODEL_FILES)
    # The host sends the first bytes and then holds the rest: the download is certainly still in
    # flight when the delete lands, which is the whole of what this covers. A short delay instead
    # would be a race the test loses on a busy host, by finishing the download first.
    Zoo.stall_after = 64
    Zoo.hold = 10.0
    model = fake_entry(Zoo.payload)
    downloads = Downloads(tmp_path / "stt")
    catalog.BY_ID[model.id] = model
    try:
        downloads.start(model.id, url=zoo)
        await until(lambda: bool(Zoo.served), "the download host was asked for the archive")
        await downloads.delete(model.id)
        await until(lambda: model.id not in downloads._running, "the download task let go")
        assert not downloads.is_installed(model.id), "a deleted model must not come back from its own download"
        assert not downloads.directory(model.id).is_dir()
        assert not (downloads.parts / model.archive).exists(), "the part file is not reachable from any UI"
    finally:
        catalog.BY_ID.pop(model.id, None)
        await downloads.close()


async def test_only_one_model_is_fetched_at_a_time(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("x", MODEL_FILES)
    Zoo.hold = 0.2
    from dataclasses import replace as _replace

    first = fake_entry(Zoo.payload)
    downloads = Downloads(tmp_path / "stt")
    catalog.BY_ID[first.id] = first
    other = _replace(first, id="test-model-2")
    catalog.BY_ID[other.id] = other
    try:
        assert downloads.start(first.id, url=zoo).state == "downloading"
        assert downloads.start(other.id, url=zoo).state == "queued"
        assert "test-model" in downloads.progress()[other.id].error
    finally:
        catalog.BY_ID.pop(first.id, None)
        catalog.BY_ID.pop(other.id, None)
        await downloads.close()


def test_a_download_is_refused_where_the_disk_could_not_hold_it(tmp_path: Path) -> None:
    from daedalus.speech.models import DISK_HEADROOM

    downloads = Downloads(tmp_path / "stt")
    huge = fake_entry(b"x" * 16)
    from dataclasses import replace as _replace

    huge = _replace(huge, size_bytes=1 << 50, unpacked_bytes=1 << 50)
    with pytest.raises(DownloadError, match="free"):
        downloads._check_room(huge)
    assert DISK_HEADROOM > 0
    downloads._check_room(fake_entry(b"x" * 16))  # a model that fits raises nothing


def test_an_archive_with_no_digest_is_pinned_by_its_file_list(tmp_path: Path) -> None:
    from dataclasses import replace as _replace

    from daedalus.speech.models import check_contents

    directory = tmp_path / "m"
    directory.mkdir()
    for name in MODEL_FILES:
        (directory / name).write_bytes(b"x")
    model = _replace(fake_entry(b"x"), contents=tuple(sorted(MODEL_FILES)))
    check_contents(directory, model)

    (directory / "extra.onnx").write_bytes(b"x")
    with pytest.raises(DownloadError, match="unexpected extra.onnx"):
        check_contents(directory, model)
    (directory / "extra.onnx").unlink()
    (directory / "tokens.txt").unlink()
    with pytest.raises(DownloadError, match="missing tokens.txt"):
        check_contents(directory, model)
    # An entry that has a digest is checked by it instead, and this says nothing.
    check_contents(directory, fake_entry(b"x", digest="a" * 64))


def test_a_redirect_off_the_zoo_is_refused_and_one_onto_the_asset_host_is_not() -> None:
    from daedalus.speech.models import _host_allowed

    zoo_url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/x.tar.bz2"
    assert _host_allowed(zoo_url, "release-assets.githubusercontent.com")
    assert _host_allowed(zoo_url, "github.com")
    assert not _host_allowed(zoo_url, "githubusercontent.com.example.invalid")
    assert not _host_allowed(zoo_url, "elsewhere.invalid")
    # A download that did not begin at GitHub may not change host at all.
    assert _host_allowed("http://127.0.0.1:9/x", "127.0.0.1")
    assert not _host_allowed("http://127.0.0.1:9/x", "elsewhere.invalid")


def test_a_manifest_entry_whose_files_are_gone_is_not_installed(tmp_path: Path) -> None:
    """Somebody emptied the directory by hand; the manifest must not keep claiming the model is there."""
    from daedalus.speech.models import Installed

    downloads = Downloads(tmp_path / "stt")
    downloads._write_manifest({"whisper-tiny": Installed(id="whisper-tiny", archive="a", sha256="", disk_bytes=1)})
    assert not downloads.is_installed("whisper-tiny")


def test_a_short_or_mistyped_download_is_refused_before_it_is_unpacked(tmp_path: Path) -> None:
    payload = make_archive("x", MODEL_FILES)
    archive = tmp_path / "a.tar.bz2"
    archive.write_bytes(payload)
    verify(archive, fake_entry(payload, digest=hashlib.sha256(payload).hexdigest()))
    with pytest.raises(DownloadError, match="bytes where the catalog says"):
        verify(archive, fake_entry(payload + b"x"))
    assert sha256_of(archive) == hashlib.sha256(payload).hexdigest()


def test_a_tar_that_climbs_out_of_the_directory_is_not_unpacked(tmp_path: Path) -> None:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo("../escaped.onnx")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    payload = bz2.compress(raw.getvalue())
    archive = tmp_path / "a.tar.bz2"
    archive.write_bytes(payload)
    downloads = Downloads(tmp_path / "stt")
    with pytest.raises(DownloadError):
        downloads._unpack(archive, fake_entry(payload))
    assert not (tmp_path / "escaped.onnx").exists()


def test_the_picker_view_joins_the_catalog_to_what_is_on_disk(tmp_path: Path) -> None:
    downloads = Downloads(tmp_path / "stt")
    body = view(downloads, selected="gigaam-ru")
    assert len(body["models"]) == len(catalog.MODELS) and body["selected"] == "gigaam-ru"
    assert body["languages"] and "ru" in body["languages"]
    entry = next(m for m in body["models"] if m["id"] == "gigaam-ru")
    assert entry["selected"] and not entry["installed"] and entry["streaming"] is False
    assert entry["verified"] is True and entry["size_bytes"] > 0


# -- decoding a recording ---------------------------------------------------------------------


def wav_bytes(path: Path, pcm: bytes, rate: int = SAMPLE_RATE) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return path


async def test_a_plain_wav_is_read_without_shelling_out(tmp_path: Path) -> None:
    pcm = tone(0.5, rate=8000)
    samples, rate = await decode_file(wav_bytes(tmp_path / "a.wav", pcm, rate=8000))
    assert samples == pcm and rate == 8000, "the engine resamples; the reader must not second-guess it"


async def test_something_nothing_here_can_decode_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("daedalus.speech.service.shutil.which", lambda name: None)
    blob = tmp_path / "note.m4a"
    blob.write_bytes(b"\x00\x00\x00\x20ftypM4A ")
    with pytest.raises(SpeechError, match="opus-tools"):
        await decode_file(blob)


def test_an_ogg_is_recognised_by_its_bytes_not_its_name(tmp_path: Path) -> None:
    ogg = tmp_path / "voice.bin"
    ogg.write_bytes(b"OggS\x00\x02rest of a voice note")
    assert is_ogg(ogg)
    assert not is_ogg(wav_bytes(tmp_path / "b.wav", tone(0.1)))
    assert not is_ogg(tmp_path / "missing.oga")


# -- precedence and the API ---------------------------------------------------------------------


class FakeManager:
    async def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return []

    async def get_state(self, session_id: str) -> None:
        return None

    class providers:  # noqa: N801
        @staticmethod
        def available() -> list[str]:
            return []


class FakeApp:
    """The application as the API reads it, with a real models directory under tmp_path."""

    def __init__(self, tmp_path: Path, config: RuntimeConfig | None = None) -> None:
        self.settings = Settings(_env_file=None, state_dir=tmp_path / "state")  # type: ignore[call-arg]
        self.config = config or RuntimeConfig()
        self.manager = FakeManager()
        self.front: Any = None
        self.extensions: dict[str, Any] = {}
        self.speech = LocalSpeech(self.settings.state_dir, self.config)
        # The API reads both halves of speech on one request: /api/voice says which recogniser
        # listens and which voice speaks, so a fake with only the listening half falls over there.
        self.tts = LocalTts(self.settings.state_dir, self.config)
        # The installer behind every optional piece, including the speech engine the picker installs
        # before its first download: /api/stt/engine goes through it rather than running its own sync.
        self.components = Installer(self.settings)

    async def save_config(self, config: RuntimeConfig) -> None:
        self.config = config
        self.speech.config = config
        self.tts.config = config


@pytest.fixture
def client(tmp_path: Path) -> Any:
    app = FakeApp(tmp_path)
    with TestClient(build_app(app, "tok")) as c:  # type: ignore[arg-type]
        c.app_state = app  # type: ignore[attr-defined]
        yield c


HEAD = {"X-Daedalus-Token": "tok"}


def test_the_picker_is_served_with_everything_the_page_needs(client: TestClient) -> None:
    body = client.get("/api/stt", headers=HEAD).json()
    assert len(body["models"]) == len(catalog.MODELS)
    whisper = next(m for m in body["models"] if m["id"] == "whisper-small")
    assert whisper["verified"] is False and whisper["detects_language"] is False
    assert next(m for m in body["models"] if m["id"] == "parakeet-unified-en")["verified"] is True
    assert body["selected"] == "" and body["language"] == "auto" and body["threads"] == 2
    assert set(body["decoders"]) == {"opus", "any"} and "en" in body["recommended"]
    assert body["recommended"]["ru"] == "gigaam-ru"


def test_a_model_that_is_not_installed_cannot_be_selected(client: TestClient) -> None:
    refused = client.post("/api/stt/select", json={"model": "gigaam-ru"}, headers=HEAD)
    assert refused.status_code == 409 and "not installed" in refused.json()["detail"]
    assert client.app_state.config.stt.local_model == ""  # type: ignore[attr-defined]


def test_downloading_something_that_is_not_in_the_catalog_is_a_404(client: TestClient) -> None:
    assert client.post("/api/stt/models/not-a-model/download", headers=HEAD).status_code == 404


def test_the_language_and_the_threads_are_saved_on_their_own(client: TestClient) -> None:
    body = client.post("/api/stt/select", json={"language": "ru", "threads": 4}, headers=HEAD).json()
    assert body["language"] == "ru" and body["threads"] == 4
    assert client.app_state.config.stt.local_threads == 4  # type: ignore[attr-defined]
    assert client.post("/api/stt/select", json={"threads": 99}, headers=HEAD).status_code == 422


def test_the_stream_refuses_when_no_model_is_selected(client: TestClient) -> None:
    refused = client.post("/api/voice/listen/open", headers=HEAD)
    assert refused.status_code == 409 and "no local speech model" in refused.json()["detail"]
    refused = client.post("/api/voice/listen?stream=abc&seq=1", content=b"\x00\x00", headers=HEAD)
    assert refused.status_code == 409 and "no local speech model" in refused.json()["detail"]


def test_a_chunk_larger_than_the_limit_is_refused(client: TestClient, tmp_path: Path) -> None:
    _install_fake_model(client.app_state)  # type: ignore[attr-defined]
    huge = b"\x00" * (3 << 20)
    assert client.post("/api/voice/listen?stream=abc&seq=1", content=huge, headers=HEAD).status_code == 413


def _install_fake_model(app: FakeApp) -> None:
    """Make the application believe a model is installed and selected, without one being there."""
    from daedalus.speech.models import Installed

    directory = app.speech.downloads.directory("gigaam-ru")
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
        (directory / name).write_bytes(b"x")
    app.speech.downloads._write_manifest({"gigaam-ru": Installed(id="gigaam-ru", archive="a", sha256="", disk_bytes=4)})
    app.config = app.config.model_copy(update={"stt": app.config.stt.model_copy(update={"local_model": "gigaam-ru"})})
    app.speech.config = app.config


def test_a_selected_model_is_what_the_voice_page_and_the_site_are_told(client: TestClient) -> None:
    _install_fake_model(client.app_state)  # type: ignore[attr-defined]
    asr = client.get("/api/asr", headers=HEAD).json()
    # No endpoint is configured at all, and the microphone must still be offered.
    assert asr["configured"] is True and asr["local"]["active"] is True
    assert asr["local"]["label"] == "GigaAM v3 Russian" and asr["local"]["streaming"] is False


def test_a_downloaded_model_with_no_engine_is_installed_but_not_active(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A native installation whose `uv sync --extra speech` failed has the archive and not the wheel.

    Calling that active takes the browser's own recogniser away from the voice page and replaces it
    with a stream that answers 503 to every chunk — a downgrade from what the operator had before
    they picked a model. ``installed`` stays true so the picker can still say what is missing.
    """
    from daedalus.speech import service as speech_service

    app = client.app_state  # type: ignore[attr-defined]
    _install_fake_model(app)
    monkeypatch.setattr(speech_service, "_engine_present", False)
    local = client.get("/api/asr", headers=HEAD).json()["local"]
    assert local["installed"] is True and local["active"] is False and local["engine_installed"] is False
    # ``available`` stays archive-based on purpose: a local model that fails must not be replaced by a
    # paid endpoint behind the operator's back. What ``active`` decides is narrower — whether the page
    # puts the local model in front of the browser's own recogniser — and that needs the wheel.
    assert app.speech.available() is True


def test_the_engine_probe_is_asked_once_and_can_be_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.speech import service as speech_service

    monkeypatch.setattr(speech_service, "_engine_present", None)
    first = speech_service.engine_present()
    assert speech_service._engine_present is first
    monkeypatch.setattr(speech_service, "_engine_present", not first)
    assert speech_service.engine_present() is (not first), "the answer is cached, not re-imported per poll"
    speech_service.forget_engine()
    assert speech_service._engine_present is None and speech_service.engine_present() is first


def test_an_id_that_left_the_catalog_is_treated_as_no_choice(tmp_path: Path) -> None:
    config = RuntimeConfig()
    config = config.model_copy(update={"stt": config.stt.model_copy(update={"local_model": "retired-model"})})
    app = FakeApp(tmp_path, config)
    assert app.speech.selected() is None and not app.speech.available()


def test_a_voice_notes_endpoint_is_used_even_when_a_local_model_is_installed(tmp_path: Path) -> None:
    """The composer microphone and Telegram notes follow Voice Notes settings, not the voice-page model."""
    from daedalus.speech.service import recogniser_available, transcribe_recording

    app = FakeApp(tmp_path)
    assert not recogniser_available(app.speech, app.config)
    app.config = app.config.model_copy(update={"asr": app.config.asr.model_copy(update={"url": "http://asr.local/v1"})})
    assert recogniser_available(app.speech, app.config)
    _install_fake_model(app)
    wav_bytes(tmp_path / "x.wav", tone(0.2))

    async def boom(path: Path, model: str = "") -> str:
        raise SpeechError("the local model must not be asked")

    app.speech.transcribe_file = boom  # type: ignore[method-assign]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/transcriptions"
        return httpx.Response(200, json={"text": "from the endpoint"})

    from daedalus.transport.telegram import voice as transport_voice

    original = transport_voice.transcribe

    async def patched(path: Path, config: Any, **kw: Any) -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await original(path, config, client=client)

    transport_voice.transcribe = patched  # type: ignore[assignment]
    try:
        words = asyncio.run(transcribe_recording(app.speech, app.config, app.manager, tmp_path / "x.wav"))
    finally:
        transport_voice.transcribe = original  # type: ignore[assignment]
    assert words == "from the endpoint"


def test_the_local_model_transcribes_a_file_only_when_no_endpoint_is_set(tmp_path: Path) -> None:
    from daedalus.speech.service import transcribe_recording

    app = FakeApp(tmp_path)
    _install_fake_model(app)

    async def hear(path: Path, model: str = "") -> str:
        return "from the local model"

    app.speech.transcribe_file = hear  # type: ignore[method-assign]
    words = asyncio.run(transcribe_recording(app.speech, app.config, app.manager, tmp_path / "x.wav"))
    assert words == "from the local model"


def test_an_endpoint_is_used_when_no_local_model_is_selected(tmp_path: Path) -> None:
    from daedalus.speech.service import transcribe_recording

    app = FakeApp(tmp_path)
    app.config = app.config.model_copy(update={"asr": app.config.asr.model_copy(update={"url": "http://asr.local/v1"})})
    wav_bytes(tmp_path / "x.wav", tone(0.2))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/transcriptions"
        return httpx.Response(200, json={"text": "from the endpoint"})

    from daedalus.transport.telegram import voice as transport_voice

    original = transport_voice.transcribe

    async def patched(path: Path, config: Any, **kw: Any) -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await original(path, config, client=client)

    transport_voice.transcribe = patched  # type: ignore[assignment]
    try:
        words = asyncio.run(transcribe_recording(app.speech, app.config, app.manager, tmp_path / "x.wav"))
    finally:
        transport_voice.transcribe = original  # type: ignore[assignment]
    assert words == "from the endpoint"


def test_deleting_the_model_in_use_also_stops_using_it(client: TestClient) -> None:
    app = client.app_state  # type: ignore[attr-defined]
    _install_fake_model(app)
    body = client.delete("/api/stt/models/gigaam-ru", headers=HEAD).json()
    assert body["deleted"] and body["selected"] == ""
    assert app.config.stt.local_model == "" and not app.speech.available()


def test_the_doctor_says_which_of_the_three_things_is_missing(tmp_path: Path) -> None:
    from daedalus.doctor import DoctorContext, _local_speech

    app = FakeApp(tmp_path)
    ctx = DoctorContext(settings=app.settings, config=app.config)
    assert _local_speech(ctx).ok and "none" in _local_speech(ctx).message

    _install_fake_model(app)
    ctx = DoctorContext(settings=app.settings, config=app.config)
    assert "GigaAM v3 Russian" in _local_speech(ctx).message

    config = app.config.model_copy(update={"stt": app.config.stt.model_copy(update={"local_model": "whisper-small"})})
    check = _local_speech(DoctorContext(settings=app.settings, config=config))
    assert not check.ok and "never downloaded" in check.message


# -- the streaming endpoint, fed a synthetic stream ------------------------------------------------


class ScriptedSession:
    """A stream session that answers on a script, so the endpoint's bookkeeping can be tested alone.

    What the endpoint owns is the identity of a stream, its reuse across requests, its closing and
    its pruning — none of which needs a model, and all of which is where the bugs would be.
    """

    opened = 0
    closed = 0

    def __init__(self, words: list[str]) -> None:
        self.words = list(words)
        self.fed = 0
        self.chunks = 0
        self.rates: list[int] = []
        ScriptedSession.opened += 1

    async def feed(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> Partial:
        self.fed += len(pcm16)
        self.chunks += 1
        self.rates.append(sample_rate)
        # Every third chunk ends an utterance, which is roughly how an endpoint detector behaves.
        if self.words and self.chunks % 3 == 0:
            return Partial(text=self.words.pop(0), final=True)
        return Partial(text="…", final=False)

    async def finish(self) -> Partial:
        ScriptedSession.closed += 1
        return Partial(text=self.words.pop(0) if self.words else "", final=True)


@pytest.fixture
def listening(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Any:
    app = client.app_state  # type: ignore[attr-defined]
    _install_fake_model(app)
    ScriptedSession.opened = ScriptedSession.closed = 0
    sessions: list[ScriptedSession] = []

    async def session() -> ScriptedSession:
        made = ScriptedSession(["раз два три", "четыре пять"])
        sessions.append(made)
        return made

    monkeypatch.setattr(app.speech, "session", session)
    return client, sessions


def pcm_chunk(seconds: float = 0.2) -> bytes:
    return tone(seconds)


def open_stream(client: TestClient, rate: int = 16000) -> str:
    """The id the server minted. The page no longer invents one; see /api/voice/listen/open."""
    response = client.post(f"/api/voice/listen/open?rate={rate}", headers=HEAD)
    assert response.status_code == 200, response.text
    return str(response.json()["stream"])


def feed(client: TestClient, stream: str, seq: int, body: bytes = b"", final: bool = False) -> dict[str, Any]:
    url = f"/api/voice/listen?stream={stream}&seq={seq}" + ("&final=true" if final else "")
    return dict(client.post(url, content=body, headers=HEAD).json())


def test_one_stream_id_keeps_one_decoder_across_chunks(listening: Any) -> None:
    client, sessions = listening
    stream = open_stream(client)
    answers = [feed(client, stream, n, pcm_chunk()) for n in (1, 2, 3)]
    assert ScriptedSession.opened == 1, "each chunk must not open a new decoder"
    assert [a["final"] for a in answers] == [False, False, True]
    assert answers[-1]["text"] == "раз два три"
    assert sessions[0].fed == 3 * len(pcm_chunk())


def test_two_pages_listening_at_once_do_not_share_a_decoder(listening: Any) -> None:
    client, _ = listening
    a, b = open_stream(client), open_stream(client)
    assert a != b, "the server mints the id; two pages cannot collide on one"
    feed(client, a, 1, pcm_chunk())
    feed(client, b, 1, pcm_chunk())
    assert ScriptedSession.opened == 2


def test_the_final_chunk_flushes_and_lets_go_of_the_stream(listening: Any) -> None:
    client, _ = listening
    stream = open_stream(client)
    feed(client, stream, 1, pcm_chunk())
    body = feed(client, stream, 2, b"", final=True)
    assert body["final"] and body["text"] == "раз два три" and ScriptedSession.closed == 1
    # The stream is gone with it: a chunk on the same id afterwards is a 404, not a fresh decoder.
    assert client.post(f"/api/voice/listen?stream={stream}&seq=3", content=pcm_chunk(), headers=HEAD).status_code == 404
    assert ScriptedSession.opened == 1


def test_closing_a_stream_decodes_nothing_and_is_idempotent(listening: Any) -> None:
    client, _ = listening
    stream = open_stream(client)
    feed(client, stream, 1, pcm_chunk())
    assert client.post(f"/api/voice/listen/close?stream={stream}", headers=HEAD).json() == {"closed": True}
    assert client.post(f"/api/voice/listen/close?stream={stream}", headers=HEAD).json() == {"closed": False}
    assert ScriptedSession.closed == 0, "closing is letting go, not finishing: nothing is decoded"


def test_a_page_that_never_closes_its_streams_does_not_accumulate_them(listening: Any) -> None:
    from daedalus.extensions.api import LISTEN_MAX_STREAMS

    client, _ = listening
    streams = [open_stream(client) for _ in range(LISTEN_MAX_STREAMS + 3)]
    assert ScriptedSession.opened == LISTEN_MAX_STREAMS + 3
    # What is bounded is how many are held, not how many were opened: the oldest are pruned, and the
    # earliest ids no longer resolve.
    gone = sum(1 for s in streams if client.post(f"/api/voice/listen?stream={s}&seq=1", content=pcm_chunk(), headers=HEAD).status_code == 404)
    assert gone == 3


def test_an_empty_chunk_is_a_keepalive_and_not_an_utterance(listening: Any) -> None:
    client, _ = listening
    assert feed(client, open_stream(client), 1) == {"text": "", "final": False}


def test_the_rate_the_browser_negotiated_is_what_the_model_is_told(listening: Any) -> None:
    """The whole of H2 in one assertion: 48 kHz capture must not be decoded as 16 kHz.

    `new AudioContext({sampleRate})` is a request, and Safari and several Android webviews answer it
    with the hardware's own rate. The page reads it back and declares it here; sherpa resamples, which
    it cannot do if it was never told.
    """
    client, sessions = listening
    stream = open_stream(client, rate=48_000)
    feed(client, stream, 1, pcm_chunk())
    assert sessions[0].rates == [48_000]

    other = open_stream(client, rate=16_000)
    feed(client, other, 1, pcm_chunk())
    assert sessions[1].rates == [16_000]


def test_a_rate_outside_any_capture_device_is_refused_before_a_decoder_is_opened(listening: Any) -> None:
    """The batch endpointer divides by the declared rate; an absurd one makes it never flush."""
    client, _ = listening
    for bad in (0, 10**9, 100):
        refused = client.post(f"/api/voice/listen/open?rate={bad}", headers=HEAD)
        assert refused.status_code == 400, bad
    assert ScriptedSession.opened == 0


def test_a_chunk_out_of_order_closes_the_stream_rather_than_corrupting_the_transcript(listening: Any) -> None:
    client, _ = listening
    stream = open_stream(client)
    feed(client, stream, 1, pcm_chunk())
    out_of_order = client.post(f"/api/voice/listen?stream={stream}&seq=3", content=pcm_chunk(), headers=HEAD)
    assert out_of_order.status_code == 409 and "expected" in out_of_order.json()["detail"]
    # And the stream is gone, so the page cannot carry on feeding a decoder with a hole in it.
    assert client.post(f"/api/voice/listen?stream={stream}&seq=2", content=pcm_chunk(), headers=HEAD).status_code == 404


def test_a_stream_id_the_server_did_not_mint_is_not_a_stream(listening: Any) -> None:
    client, _ = listening
    assert client.post("/api/voice/listen?stream=s1&seq=1", content=pcm_chunk(), headers=HEAD).status_code == 404
    assert ScriptedSession.opened == 0, "a client-chosen id must not open a decoder"


def test_an_abandoned_stream_is_reaped_on_the_next_call_not_on_the_next_open(listening: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.extensions import api as api_module

    client, _ = listening
    stream = open_stream(client)
    feed(client, stream, 1, pcm_chunk())
    monkeypatch.setattr(api_module, "LISTEN_IDLE_SECONDS", 0.0)
    # No new stream is opened; the next chunk on the abandoned one is enough to prune it.
    assert client.post(f"/api/voice/listen?stream={stream}&seq=2", content=pcm_chunk(), headers=HEAD).status_code == 404


# -- the engine on a real model, where one has been fetched -----------------------------------------

REAL_MODELS = Path("/tmp/claude-1000/stt-models")


@pytest.mark.skipif(not (REAL_MODELS / "sherpa-onnx-streaming-zipformer-small-ru-vosk-int8-2025-08-16").is_dir(),
                    reason="no downloaded model here; the catalog's archives are hundreds of megabytes")
async def test_a_real_streaming_model_hears_its_own_test_recording() -> None:
    """The one test that loads sherpa and a model. Skipped everywhere a model was not fetched by hand."""
    from daedalus.speech.engine import Engine

    directory = REAL_MODELS / "sherpa-onnx-streaming-zipformer-small-ru-vosk-int8-2025-08-16"
    engine = Engine(catalog.get("zipformer-ru-small"), directory, threads=2)
    with wave.open(str(next((directory / "test_wavs").glob("*.wav")))) as handle:
        rate = handle.getframerate()
        pcm = handle.readframes(handle.getnframes())
    assert (await engine.transcribe(pcm, rate)).strip(), "the model produced no words at all"

    session = engine.session()
    step = int(rate * 0.1) * 2
    heard = []
    for at in range(0, len(pcm), step):
        answer = await session.feed(pcm[at : at + step], rate)
        if answer.final and answer.text:
            heard.append(answer.text)
    last = await session.finish()
    if last.text:
        heard.append(last.text)
    assert heard, "streaming produced no utterance where the batch decode produced words"


# -- warming the engine, and what the voice page is told ------------------------------------------


class FakeVoice:
    """The voice extension as the API reads it, with nothing of the concierge behind it."""

    async def state(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "session_id": "s1",
            "model": "Qwen",
            "tts": {"configured": False},
            "stt": {"configured": False, "reason": "no endpoint"},
            "agents": [],
            "listening": False,
        }

    def first_audio(self, turn: str, *, clip_ms: int, load_ms: int) -> None:
        """The API tells the extension when a turn was first heard; nothing here is timing anything."""

    def last_turn(self) -> dict[str, Any]:
        return {"turn": "", "first_audio_ms": 0, "clip_ms": 0, "load_ms": 0}


class LoadedEngine:
    """A model that loads instantly, standing in for half a gigabyte of weights."""

    def __init__(self, model: catalog.SpeechModel, directory: Path, *, threads: int = 2, language: str = "") -> None:
        self.model = model
        self.directory = directory
        self.threads = threads
        self.language = language


@pytest.fixture
def warm_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A client whose chosen model loads, over a cache that starts and ends empty.

    The engine cache is process-wide, because the model it holds is; a test that leaves one resident
    is a test that decides what the next one sees.
    """
    from daedalus.speech.engine import CACHE

    monkeypatch.setattr("daedalus.speech.engine.Engine", LoadedEngine)
    CACHE.drop()
    app = FakeApp(tmp_path)
    app.extensions["voice"] = FakeVoice()
    _install_fake_model(app)
    with TestClient(build_app(app, "tok")) as c:  # type: ignore[arg-type]
        c.app_state = app  # type: ignore[attr-defined]
        yield c
    CACHE.drop()


def _until_loaded(client: TestClient, state: str = "ready", tries: int = 200) -> dict[str, Any]:
    """Poll the picker until the background load has got where it is going."""
    for _ in range(tries):
        load = client.get("/api/stt", headers=HEAD).json()["load"]
        if load["state"] == state:
            return load
        time.sleep(0.01)
    raise AssertionError(f"the load never reached {state}")


def test_choosing_a_model_answers_with_the_whole_picker_and_not_half_of_it(warm_client: TestClient) -> None:
    """The crash this fixes: ``select`` used to answer with the models and nothing else.

    The page replaced its whole view with what came back and the next render read ``decoders.opus``
    off an object that no longer had ``decoders``, so pressing "Use this one" took the screen down
    while the server had already saved the choice. Every call that changes something answers with the
    same complete view now.
    """
    whole = set(warm_client.get("/api/stt", headers=HEAD).json())
    chosen = warm_client.post("/api/stt/select", json={"model": "gigaam-ru"}, headers=HEAD).json()
    assert set(chosen) == whole
    assert set(chosen["decoders"]) == {"opus", "any"} and "recommended" in chosen and "engine_installed" in chosen
    assert chosen["selected"] == "gigaam-ru"
    deleted = warm_client.delete("/api/stt/models/gigaam-ru", headers=HEAD).json()
    assert whole <= set(deleted)


def test_choosing_a_model_starts_loading_it_before_anything_is_said(warm_client: TestClient) -> None:
    started = warm_client.post("/api/stt/select", json={"model": "gigaam-ru"}, headers=HEAD).json()
    assert started["load"]["state"] in ("loading", "ready")
    load = _until_loaded(warm_client)
    assert load["model"] == "gigaam-ru" and load["error"] == ""
    # The time it took is kept, because it is the number that says whether the page will feel instant.
    assert load["loaded_in_ms"] >= 0


def test_the_warm_up_call_answers_at_once_and_twice_is_one_load(warm_client: TestClient) -> None:
    first = warm_client.post("/api/stt/engine/warm", headers=HEAD).json()
    second = warm_client.post("/api/stt/engine/warm", headers=HEAD).json()
    assert first["state"] in ("loading", "ready") and second["state"] in ("loading", "ready")
    assert _until_loaded(warm_client)["model"] == "gigaam-ru"


def test_a_load_that_fails_says_so_and_is_not_tried_again_on_every_poll(warm_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = []

    def refuse(*args: Any, **kwargs: Any) -> LoadedEngine:
        attempts.append(1)
        raise SpeechError("the archive is not a model")

    monkeypatch.setattr("daedalus.speech.engine.Engine", refuse)
    warm_client.post("/api/stt/engine/warm", headers=HEAD)
    load = _until_loaded(warm_client, "error")
    assert "not a model" in load["error"]
    for _ in range(3):
        warm_client.post("/api/stt/engine/warm", headers=HEAD)
        warm_client.get("/api/voice", headers=HEAD)
    assert len(attempts) == 1


def test_the_voice_page_is_told_which_recogniser_listens_and_where_its_weights_are(warm_client: TestClient) -> None:
    stt = warm_client.get("/api/voice", headers=HEAD).json()["stt"]
    assert stt["kind"] == "local" and stt["local"]["model"] == "gigaam-ru"
    # Opening the page is the second free moment to load the model, so asking for the page starts it.
    assert stt["state"] in ("loading", "ready")
    _until_loaded(warm_client)
    after = warm_client.get("/api/voice", headers=HEAD).json()["stt"]
    assert after["state"] == "ready" and after["error"] == ""


def test_a_page_with_no_local_model_never_waits_for_one(tmp_path: Path) -> None:
    app = FakeApp(tmp_path)
    app.extensions["voice"] = FakeVoice()
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        stt = client.get("/api/voice", headers=HEAD).json()["stt"]
    # An endpoint and the browser's own recogniser answer the moment they are asked; only a local
    # model has weights to wait for, and the page must not draw a loading state for something that
    # never loads.
    assert stt["kind"] == "browser" and stt["state"] == "ready" and stt["loaded_in_ms"] == 0


async def test_the_engine_publishes_every_step_of_a_load_to_a_watcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the page draws its "loading" on, at the fan-out that feeds the progress stream.

    The stream itself is deliberately endless — it stays open until the page closes it — so it is the
    watcher that is tested rather than the socket: a test that subscribes to an endless response has
    no way to stop reading one. The frame shapes the stream builds out of these are checked in the
    app's own tests, where they are read.
    """
    from daedalus.speech.engine import CACHE

    monkeypatch.setattr("daedalus.speech.engine.Engine", LoadedEngine)
    CACHE.drop()
    app = FakeApp(tmp_path)
    _install_fake_model(app)
    seen: list[dict[str, Any]] = []
    async with CACHE.watch() as queue:
        app.speech.warm()
        while len(seen) < 2:
            seen.append(await asyncio.wait_for(queue.get(), timeout=SETTLE))
    assert [frame["state"] for frame in seen] == ["loading", "ready"]
    assert seen[1]["model"] == "gigaam-ru" and seen[1]["error"] == ""
    assert app.speech.load_state()["state"] == "ready"
    CACHE.drop()
    assert app.speech.load_state()["state"] == "idle"
