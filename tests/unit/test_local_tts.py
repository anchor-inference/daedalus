"""Local speech synthesis: the catalog, the download manager's second kind, the engine and the API.

The engine is exercised against a fake backend put in ``sys.modules`` under sherpa's own name, so
every test here runs on a machine with no model files and no wheel — which is what CI is. One test at
the end loads a real voice and measures it, and skips itself where none has been downloaded.
"""

from __future__ import annotations

import asyncio
import bz2
import hashlib
import io
import os
import struct
import sys
import tarfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.speech import ru_stress
from daedalus.speech import tts_catalog as catalog
from daedalus.speech.models import DownloadError, Downloads, Installed, view
from daedalus.speech.service import LocalSpeech
from daedalus.speech.tts_engine import (
    CACHE,
    MAX_TEXT_CHARS,
    OUTPUT_GAIN,
    SUPERTONIC_GRAPHS,
    TtsCache,
    TtsEngine,
    TtsError,
    resolve,
    sentences,
    spoken,
    to_pcm16,
    wav,
)
from daedalus.speech.tts_service import (
    LENGTH_BYTES,
    MEDIA_TYPE_HEADER,
    SEQUENCE_TYPE,
    LocalTts,
    check_voice,
    encode,
)
from tests.support.waiting import SETTLE, until

# -- the catalog ------------------------------------------------------------------------------

GENDERS = {"female", "male", "mixed"}


def test_every_entry_is_complete_and_unique() -> None:
    ids = [v.id for v in catalog.VOICES]
    assert len(ids) == len(set(ids)), "two entries share an id; the directory they install into would collide"
    archives = [v.archive for v in catalog.VOICES]
    assert len(archives) == len(set(archives)), "two entries share an archive; one download would serve both"
    for voice in catalog.VOICES:
        assert voice.archive.endswith(".tar.bz2"), voice.id
        assert voice.url.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"), voice.id
        assert voice.size_bytes > 1_000_000 and voice.unpacked_bytes >= voice.size_bytes, voice.id
        assert voice.memory_mb > 0 and voice.licence and voice.note, voice.id
        assert 0 < voice.quality <= 100 and 0 < voice.speed <= 100, voice.id
        assert voice.rtf > 0, voice.id
        assert voice.gender in GENDERS, voice.id
        assert voice.sample_rate in (16_000, 22_050, 24_000, 44_100), voice.id
        assert len(voice.speakers) == len(set(voice.speakers)), voice.id


def test_every_archive_carries_a_published_checksum() -> None:
    """Unlike the recognition catalog, this one has no entry verified by size and a file list.

    Every asset in the zoo's ``tts-models`` release was uploaded after GitHub began reporting a digest
    per asset, so there is no reason for an entry here to be weaker — and a new entry that has no
    digest should fail this rather than quietly join the list.
    """
    for voice in catalog.VOICES:
        assert len(voice.sha256) == 64 and set(voice.sha256) <= set("0123456789abcdef"), voice.id
        assert voice.contents == (), f"{voice.id}: a digest and a pinned file list is one check too many"


def test_nothing_in_the_catalog_is_larger_than_the_operator_asked_for() -> None:
    """Two hundred megabytes on disk was the brief; a voice past that belongs on a server.

    One entry is over it and is named here rather than let through by a looser rule. Kokoro's float
    build is three hundred and fifty megabytes and is the only build of it worth having: the hundred
    and fifty megabyte quantised archive renders at twice the cost of the file it was meant to make
    cheaper. The exception is the entry, not the size, so a new voice still has to fit.
    """
    for voice in catalog.VOICES:
        cap = 400 << 20 if voice.id == "en-kokoro" else 200 << 20
        assert voice.unpacked_bytes < cap, f"{voice.id} takes {voice.unpacked_bytes >> 20} MB on disk"


def test_the_catalog_covers_what_the_operator_actually_speaks() -> None:
    """Russian first, in both genders, and English with a choice of how much it costs to run."""
    russian = [v for v in catalog.VOICES if v.language == "ru"]
    assert len(russian) >= 3
    assert {v.gender for v in russian} >= {"male", "female"}, "Russian needs a voice of each"
    assert all(v.keeps_up for v in russian), "no Russian voice may be slower than talking"
    english = [v for v in catalog.VOICES if v.language == "en"]
    assert len(english) >= 3 and any(v.multi for v in english), "English should offer more than one voice per file"
    assert len(catalog.languages()) >= 8
    assert len(catalog.VOICES) >= 8


def test_one_voice_is_slower_than_speech_and_is_the_only_one() -> None:
    """The fact that changes how the page feels, pinned so a new entry cannot join it unnoticed."""
    slow = sorted(v.id for v in catalog.VOICES if not v.keeps_up)
    assert slow == ["en-ryan"]
    assert all(v.rtf > 1 for v in catalog.VOICES if not v.keeps_up)


def test_the_speed_bar_is_derived_from_the_measured_factor_and_not_invented() -> None:
    """``speed = 100·(1 − e^(−(1/rtf)/6))`` — the same derivation the module docstring states."""
    import math

    for voice in catalog.VOICES:
        assert voice.speed == round(100 * (1 - math.exp(-(1 / voice.rtf) / 6))), voice.id


def test_every_language_has_a_sample_to_play_and_a_recommendation() -> None:
    for code in catalog.languages():
        assert code in catalog.SAMPLES, f"{code} has no sample sentence, so its cards cannot be heard"
        assert catalog.recommended(code) is not None, f"{code} has no recommended voice"
        assert catalog.recommended(code).language == code
    for voice in catalog.VOICES:
        assert voice.sample() and len(voice.sample()) < 200
    assert catalog.recommended("ru").id == "multi-supertonic"
    assert catalog.recommended("en").id == "en-supertonic2"
    assert catalog.recommended("xx") is None


def test_a_sample_carries_a_number_so_the_listener_hears_how_it_reads_one() -> None:
    """The whole question about a synthesiser is whether "17" becomes a word or a pair of digits."""
    for code, text in catalog.SAMPLES.items():
        assert any(ch.isdigit() for ch in text) or code == "zh", f"{code}'s sample has nothing to expand"


def test_every_kind_is_one_the_engine_can_build() -> None:
    assert {v.kind for v in catalog.VOICES} <= {"vits", "kokoro", "kitten", "supertonic"}
    assert all(v.multi for v in catalog.VOICES if v.kind in ("kokoro", "kitten", "supertonic"))


def test_an_unknown_id_names_what_is_actually_on_offer() -> None:
    with pytest.raises(KeyError) as raised:
        catalog.get("nope")
    assert "ru-dmitri" in str(raised.value)


def test_the_card_the_app_reads_carries_what_it_draws() -> None:
    both = catalog.as_json(catalog.get("multi-supertonic"))
    assert both["languages"][:2] == ["ru", "en"] and both["new"] is True
    assert both["sample_rate"] == 44_100 and len(both["speakers"]) == 10
    assert catalog.as_json(catalog.get("ru-irina"))["languages"] == []
    body = catalog.as_json(catalog.get("en-kokoro"))
    assert body["speakers"][0] == "af" and len(body["speakers"]) == 11
    assert body["keeps_up"] is True and body["gender"] == "mixed" and body["sample_rate"] == 24_000
    assert catalog.as_json(catalog.get("multi-supertonic"))["recommended_for"] == ["ru"]


def test_a_voice_speaks_one_language_and_says_so() -> None:
    dmitri = catalog.get("ru-dmitri")
    assert dmitri.speaks("ru") and dmitri.speaks("ru-RU") and dmitri.speaks("")
    assert not dmitri.speaks("en")
    assert dmitri.languages == (), "a single-language voice has nothing to list"


def test_the_one_voice_that_speaks_several_languages_says_all_of_them() -> None:
    """The assumption the rest of the catalog rests on, and the one entry that breaks it."""
    both = catalog.get("multi-supertonic")
    assert both.speaks("ru") and both.speaks("en") and both.speaks("ru-RU") and both.speaks("en-GB")
    assert not both.speaks("zh"), "it reads thirty-one languages; it is offered in the ones with a sample"
    assert both.language in both.languages, "the language it is filed under has to be one it speaks"
    for voice in catalog.VOICES:
        assert not voice.languages or voice.language in voice.languages, voice.id
        assert len(voice.languages) != 1, f"{voice.id}: one language is said by `language` alone"
    assert set(catalog.languages()) >= set(both.languages), "the filter cannot offer fewer than a voice reads"


def test_the_new_ones_are_the_ones_that_are_new() -> None:
    """An editorial flag with a shelf life, pinned so it is not left on a voice nobody calls new."""
    assert sorted(v.id for v in catalog.VOICES if v.new) == ["en-supertonic2", "multi-supertonic"]
    assert catalog.VOICES[0].id == "multi-supertonic", "the Russian recommendation heads the list"


def test_the_russian_default_is_the_one_the_owner_chose_and_piper_is_still_there() -> None:
    """Supertonic is recommended; the Piper voices stay, because its licence is not everybody's."""
    assert catalog.recommended("ru").id == "multi-supertonic"
    assert "OpenRAIL-M" in catalog.recommended("ru").licence
    piper = [v for v in catalog.VOICES if v.language == "ru" and v.kind == "vits"]
    assert len(piper) >= 3 and any("CC0" in v.licence for v in piper)


# -- what an archive has to hold ---------------------------------------------------------------

ESPEAK = {"espeak-ng-data/phonindex": b"P" * 64, "espeak-ng-data/ru_dict": b"R" * 64}


def lay_out(directory: Path, files: dict[str, bytes]) -> Path:
    for name, body in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return directory


def test_a_piper_voice_is_recognised_by_the_espeak_data_beside_it(tmp_path: Path) -> None:
    found = resolve(lay_out(tmp_path, {"ru_RU-irina-medium.onnx": b"M", "tokens.txt": b"t", **ESPEAK}), "vits")
    assert found.model.name == "ru_RU-irina-medium.onnx"
    assert found.data_dir is not None and found.data_dir.name == "espeak-ng-data"
    assert found.lexicon is None and found.voices is None and found.rule_fsts == ()


def test_a_lexicon_voice_is_recognised_by_its_lexicon_and_takes_its_rules(tmp_path: Path) -> None:
    """Mandarin phonemises through a lexicon and three normalisation FSTs; nothing says so in the catalog."""
    files = {
        "zh_CN-xiao_ya-medium.onnx": b"M", "tokens.txt": b"t", "lexicon.txt": b"l",
        "phone.fst": b"1", "date.fst": b"2", "number.fst": b"3",
    }
    found = resolve(lay_out(tmp_path, files), "vits")
    assert found.lexicon is not None and found.data_dir is None
    assert [p.name for p in found.rule_fsts] == ["phone.fst", "date.fst", "number.fst"]


def test_a_multi_speaker_voice_needs_its_style_vectors(tmp_path: Path) -> None:
    files = {"model.int8.onnx": b"M", "tokens.txt": b"t", "voices.bin": b"V", **ESPEAK}
    assert resolve(lay_out(tmp_path, files), "kokoro").voices is not None
    (tmp_path / "voices.bin").unlink()
    with pytest.raises(TtsError, match="voices.bin"):
        resolve(tmp_path, "kitten")


def test_the_quantised_build_is_the_one_that_is_loaded(tmp_path: Path) -> None:
    """Where an archive ships both, int8 is what the catalog's size and speed numbers describe."""
    files = {"model.onnx": b"M", "model.int8.onnx": b"Q", "tokens.txt": b"t", **ESPEAK}
    assert resolve(lay_out(tmp_path, files), "vits").model.name == "model.int8.onnx"


def test_an_archive_that_arrived_short_is_refused_here_rather_than_at_the_first_sentence(tmp_path: Path) -> None:
    # A directory each: what is missing is the whole of every case, and a leftover file from the
    # previous one would make the next pass for the wrong reason.
    with pytest.raises(TtsError, match="no model file"):
        resolve(lay_out(tmp_path / "a", {"tokens.txt": b"t", **ESPEAK}), "vits")
    with pytest.raises(TtsError, match="tokens"):
        resolve(lay_out(tmp_path / "b", {"a.onnx": b"M", **ESPEAK}), "vits")
    with pytest.raises(TtsError, match="neither espeak-ng-data nor a lexicon"):
        resolve(lay_out(tmp_path / "c", {"a.onnx": b"M", "tokens.txt": b"t"}), "vits")
    with pytest.raises(TtsError, match="does not load"):
        resolve(lay_out(tmp_path / "d", {"a.onnx": b"M", "tokens.txt": b"t", **ESPEAK}), "zipvoice")


SUPERTONIC = {
    "text_encoder.int8.onnx": b"T", "duration_predictor.int8.onnx": b"D",
    "vector_estimator.int8.onnx": b"V", "vocoder.int8.onnx": b"C",
    "tts.json": b"{}", "unicode_indexer.bin": b"U", "voice.bin": b"S",
}


def test_a_supertonic_voice_is_four_graphs_and_no_tokens_file_at_all(tmp_path: Path) -> None:
    """The shape every other entry here has — one model, one tokens file — is not this one's."""
    found = resolve(lay_out(tmp_path, SUPERTONIC), "supertonic")
    assert found.text_encoder is not None and found.text_encoder.name == "text_encoder.int8.onnx"
    assert found.duration_predictor is not None and found.vector_estimator is not None
    assert found.vocoder is not None and found.vocoder.name == "vocoder.int8.onnx"
    assert found.tts_json is not None and found.unicode_indexer is not None
    assert found.voices is not None and found.voices.name == "voice.bin"
    assert found.tokens is None and found.model is None and found.data_dir is None


def test_a_supertonic_archive_missing_any_one_file_is_refused_and_the_file_is_named(tmp_path: Path) -> None:
    """Seven files, and a download that arrived with six says which one it is short of."""
    for missing, expected in (
        ("text_encoder.int8.onnx", "text_encoder"), ("duration_predictor.int8.onnx", "duration_predictor"),
        ("vector_estimator.int8.onnx", "vector_estimator"), ("vocoder.int8.onnx", "vocoder"),
        ("tts.json", "tts.json"), ("unicode_indexer.bin", "unicode_indexer.bin"), ("voice.bin", "voice.bin"),
    ):
        short = {name: body for name, body in SUPERTONIC.items() if name != missing}
        with pytest.raises(TtsError, match=expected):
            resolve(lay_out(tmp_path / missing, short), "supertonic")


def test_the_quantised_build_of_each_supertonic_graph_is_the_one_that_is_loaded(tmp_path: Path) -> None:
    """Per graph rather than per directory: the archive may ship either build of any of the four."""
    both = dict(SUPERTONIC)
    both["vocoder.onnx"] = b"F"
    del both["vector_estimator.int8.onnx"]
    both["vector_estimator.onnx"] = b"F"
    found = resolve(lay_out(tmp_path, both), "supertonic")
    assert found.vocoder is not None and found.vocoder.name == "vocoder.int8.onnx"
    assert found.vector_estimator is not None and found.vector_estimator.name == "vector_estimator.onnx"


def test_the_download_managers_check_speaks_in_its_own_terms(tmp_path: Path) -> None:
    """The manager catches ``DownloadError``; the engine raises ``TtsError``. This is the join."""
    with pytest.raises(DownloadError, match="no model file"):
        check_voice(lay_out(tmp_path, {"tokens.txt": b"t"}), catalog.get("ru-irina"))


# -- the download manager, on its second kind ---------------------------------------------------


def make_archive(name: str, files: dict[str, bytes]) -> bytes:
    """A .tar.bz2 with one top-level directory, exactly as the model zoo ships one."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for filename, body in files.items():
            info = tarfile.TarInfo(f"{name}/{filename}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return bz2.compress(raw.getvalue())


VOICE_FILES = {"ru_RU-test-medium.onnx": b"M" * 2048, "tokens.txt": b"<blk> 0\n", **ESPEAK}


class Zoo(BaseHTTPRequestHandler):
    """A voice host that honours ranges, so resuming is exercised against something that resumes."""

    payload = b""
    served: list[str] = []

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
        self.send_header("Content-Length", str(len(body) - start))
        self.end_headers()
        self.wfile.write(body[start:])
        self.wfile.flush()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def zoo() -> Any:
    server = HTTPServer(("127.0.0.1", 0), Zoo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Zoo.served = []
    yield f"http://127.0.0.1:{server.server_port}/voice.tar.bz2"
    server.shutdown()


def fake_voice(payload: bytes, *, digest: str = "") -> catalog.TtsVoice:
    return catalog.TtsVoice(
        id="test-voice", label="Test", archive="test.tar.bz2", kind="vits", language="ru", gender="female",
        size_bytes=len(payload), unpacked_bytes=len(payload) * 2, memory_mb=10, sample_rate=22_050,
        licence="MIT", quality=50, speed=50, rtf=0.25, note="for the tests", sha256=digest,
    )


def downloads_for(tmp_path: Path) -> Downloads:
    """The manager as the synthesis side builds it: its own tree, its own catalog, its own loader check."""
    return Downloads(tmp_path / "models" / "tts", lookup=catalog.get, resolver=check_voice)


async def install(tmp_path: Path, url: str, voice: catalog.TtsVoice) -> Downloads:
    downloads = downloads_for(tmp_path)
    catalog.BY_ID[voice.id] = voice
    try:
        downloads.start(voice.id, url=url)
        for _ in range(400):
            await asyncio.sleep(0.02)
            state = downloads.progress().get(voice.id)
            if state and state.state in ("installed", "failed", "cancelled"):
                break
    finally:
        catalog.BY_ID.pop(voice.id, None)
    return downloads


async def test_a_voice_is_downloaded_unpacked_under_its_own_id_and_recorded(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("vits-piper-ru_RU-test-medium-int8", VOICE_FILES)
    voice = fake_voice(Zoo.payload, digest=hashlib.sha256(Zoo.payload).hexdigest())
    downloads = await install(tmp_path, zoo, voice)
    assert downloads.progress()[voice.id].state == "installed", downloads.progress()[voice.id].error
    # The archive's own directory name is the zoo's; what the engine looks for is the catalog id.
    assert (downloads.directory(voice.id) / "tokens.txt").exists()
    assert (downloads.directory(voice.id) / "espeak-ng-data" / "phonindex").exists()
    assert downloads.is_installed(voice.id)
    assert not list(downloads.parts.glob("*.tar.bz2")), "the archive is deleted once it is unpacked"


async def test_an_archive_with_no_phonemiser_in_it_installs_nothing(tmp_path: Path, zoo: str) -> None:
    """The resolver is the last gate: a voice that unpacked short must not become a selectable entry."""
    Zoo.payload = make_archive("x", {"a.onnx": b"M" * 64, "tokens.txt": b"t"})
    voice = fake_voice(Zoo.payload, digest=hashlib.sha256(Zoo.payload).hexdigest())
    downloads = await install(tmp_path, zoo, voice)
    state = downloads.progress()[voice.id]
    assert state.state == "failed" and "espeak-ng-data" in state.error
    assert not downloads.is_installed(voice.id) and not downloads.directory(voice.id).exists()


async def test_a_wrong_checksum_installs_nothing(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("x", VOICE_FILES)
    downloads = await install(tmp_path, zoo, fake_voice(Zoo.payload, digest="0" * 64))
    state = downloads.progress()["test-voice"]
    assert state.state == "failed" and "checksum" in state.error


async def test_a_half_finished_download_is_resumed_rather_than_begun_again(tmp_path: Path, zoo: str) -> None:
    Zoo.payload = make_archive("x", VOICE_FILES)
    voice = fake_voice(Zoo.payload, digest=hashlib.sha256(Zoo.payload).hexdigest())
    downloads = downloads_for(tmp_path)
    downloads.parts.mkdir(parents=True, exist_ok=True)
    (downloads.parts / voice.archive).write_bytes(Zoo.payload[: len(Zoo.payload) // 2])
    await install(tmp_path, zoo, voice)
    assert Zoo.served and Zoo.served[0].startswith("bytes="), "the whole archive was fetched again"


async def test_the_two_kinds_keep_separate_trees_and_separate_manifests(tmp_path: Path, zoo: str) -> None:
    """One download manager, two directories. An id in both catalogs would still be two installs."""
    Zoo.payload = make_archive("x", VOICE_FILES)
    voice = fake_voice(Zoo.payload, digest=hashlib.sha256(Zoo.payload).hexdigest())
    voices = await install(tmp_path, zoo, voice)
    models = Downloads(tmp_path / "models" / "stt")
    assert voices.root != models.root
    assert voices.is_installed(voice.id) and models.manifest() == {}


def test_the_picker_view_is_the_same_function_for_both_catalogs(tmp_path: Path) -> None:
    body = view(
        downloads_for(tmp_path), selected="ru-dmitri",
        entries=catalog.VOICES, to_json=catalog.as_json, all_languages=catalog.languages,
    )
    assert len(body["models"]) == len(catalog.VOICES)
    assert body["languages"] == catalog.languages() and body["selected"] == "ru-dmitri"
    assert next(m for m in body["models"] if m["id"] == "ru-dmitri")["selected"] is True
    assert all(m["installed"] is False for m in body["models"])
    # And the recognition catalog still answers with its own entries and its own extra fields.
    from daedalus.speech import catalog as speech_catalog

    plain = view(Downloads(tmp_path / "stt"))
    assert len(plain["models"]) == len(speech_catalog.MODELS) and "verified" in plain["models"][0]


async def test_an_id_that_is_not_a_voice_cannot_reach_the_disk(tmp_path: Path) -> None:
    """The id goes through the catalog first, so a climbing one never reaches a joined path."""
    downloads = downloads_for(tmp_path)
    for bad in ("../../etc", "..", "ru-dmitri/../.."):
        with pytest.raises(KeyError):
            await downloads.delete(bad)
        with pytest.raises(KeyError):
            downloads.start(bad)


# -- the text ------------------------------------------------------------------------------------


def test_markdown_and_code_are_not_read_out_loud() -> None:
    body = spoken("# Heading\n\nSee **this** and `that`, at https://example.invalid/x\n\n```py\nprint(1)\n```\n- one\n")
    assert "#" not in body and "**" not in body and "`" not in body
    assert "https" not in body and "a link" in body
    assert "code block" in body and "print(1)" not in body
    assert "this" in body and "that" in body and "one" in body


def test_a_link_keeps_its_words_and_loses_its_address() -> None:
    assert spoken("see [the report](https://example.invalid/r) now") == "see the report now"


def test_the_text_is_cut_where_a_breath_goes() -> None:
    assert sentences("One sentence here. Another one there! And a third?") == [
        "One sentence here.", "Another one there!", "And a third?",
    ]
    assert sentences("Привет. Сегодня 17 сентября, и всё готово.") == ["Привет. Сегодня 17 сентября, и всё готово."]


def test_a_fragment_too_short_to_be_its_own_clip_is_carried_into_the_next() -> None:
    """"Yes." alone is a model call, a clip boundary and an audible gap, for three characters."""
    assert sentences("Yes. The deploy finished and the tests passed.") == [
        "Yes. The deploy finished and the tests passed."
    ]


def test_something_with_no_full_stop_in_it_is_still_cut() -> None:
    """A pasted list would otherwise be one long synthesis, and the streaming path would buy nothing."""
    parts = sentences("word, " * 200)
    assert len(parts) > 1 and all(len(p) <= 400 for p in parts)
    assert "".join(p.replace(" ", "") for p in parts) == ("word," * 200).replace(" ", "")


def test_nothing_is_lost_or_invented_between_the_sentences() -> None:
    text = "Первое предложение. Второе! Третье?"
    assert "".join(sentences(text)).replace(" ", "") == text.replace(" ", "")


# -- where the stress falls ------------------------------------------------------------------------

ACUTE = "\u0301"


def test_the_words_an_assistant_says_every_day_are_stressed_where_they_belong() -> None:
    """A dozen of them, written out, because a table of stresses is only worth the words in it."""
    for plain, marked in (
        ("привет", "приве" + ACUTE + "т"),
        ("сегодня", "сего" + ACUTE + "дня"),
        ("задачи", "зада" + ACUTE + "чи"),
        ("проверил", "прове" + ACUTE + "рил"),
        ("результат", "результа" + ACUTE + "т"),
        ("конфигурацию", "конфигура" + ACUTE + "цию"),
        ("минут", "мину" + ACUTE + "т"),
        ("работа", "рабо" + ACUTE + "та"),
        ("вопрос", "вопро" + ACUTE + "с"),
        ("хорошо", "хорошо" + ACUTE),
        ("спасибо", "спаси" + ACUTE + "бо"),
        ("пожалуйста", "пожа" + ACUTE + "луйста"),
    ):
        assert ru_stress.mark(plain) == marked, plain


def test_the_words_the_table_used_to_get_wrong_are_written_out_here() -> None:
    """Hand-checked, one line each, because a wrong entry is worse than no entry.

    The module exists so a Russian voice does not sound foreign, and a table that puts the acute on
    the wrong vowel *forces* the mistake on a model that would often have got it right on its own.
    """
    for plain, marked in (
        ("август", "а" + ACUTE + "вгуст"),
        ("именно", "и" + ACUTE + "менно"),
        ("файлы", "фа" + ACUTE + "йлы"),
        ("падали", "па" + ACUTE + "дали"),
        ("пару", "па" + ACUTE + "ру"),
        ("просто", "про" + ACUTE + "сто"),
    ):
        assert ru_stress.mark(plain) == marked, plain
    # And the ones that are two words in writing, which the table may not decide between: «начал»
    # is also the genitive plural of «начало», «простой» an adjective and a noun, «пары» a pair and
    # a steam, «начало» a beginning and a verb.
    for ambiguous in ("начал", "начало", "простой", "пары"):
        assert ru_stress.mark(ambiguous) == ambiguous, ambiguous
        assert ambiguous not in ru_stress.TABLE


def test_every_entry_in_the_table_is_read_back_the_way_it_was_written() -> None:
    """The table is a list to read, so the only thing holding its shape is that it is read back."""
    for entry in ru_stress.WORDS.split():
        plain = entry.replace(ru_stress.MARK, "")
        if plain in ru_stress.HOMOGRAPHS:
            continue
        at = entry.find(ru_stress.MARK)
        assert 0 <= at < len(plain), entry
        assert plain[at] in ru_stress.VOWELS, f"{entry}: the mark is not in front of a vowel"
        assert ru_stress.mark(plain) == plain[: at + 1] + ACUTE + plain[at + 1 :], entry


def test_a_capital_letter_and_a_sentence_around_it_survive_the_marking() -> None:
    said = ru_stress.mark("Привет! Я проверил тесты, осталось 3 задачи.")
    assert said.startswith("Приве" + ACUTE + "т!")
    assert "3 зада" + ACUTE + "чи." in said
    assert said.count(ACUTE) == 4, "every word it knows, and only those"


def test_a_word_that_is_two_words_in_writing_is_left_for_the_model_to_decide() -> None:
    """«замок» is a castle or a lock depending on the sentence, and no table can read the sentence."""
    for ambiguous in ("замок", "мука", "уже", "дома", "слова"):
        assert ru_stress.mark(ambiguous) == ambiguous
        assert ambiguous not in ru_stress.TABLE


def test_nothing_that_is_not_a_russian_word_is_touched() -> None:
    """A Latin word inside a Russian sentence is not ours to stress, and neither is a number."""
    said = ru_stress.mark("Я запустил deploy через CI и проверил лог в Kubernetes 17 раз.")
    assert "deploy" in said and "CI" in said and "Kubernetes" in said and "17" in said
    assert "запусти" + ACUTE + "л" in said and "че" + ACUTE + "рез" in said


def test_a_word_with_one_vowel_or_a_letter_that_is_already_stressed_is_left_alone() -> None:
    assert ru_stress.mark("я не там") == "я не там", "one vowel decides nothing"
    assert ru_stress.mark("ещё") == "ещё", "ё is a stressed vowel already written as one"


def test_marking_text_twice_is_marking_it_once() -> None:
    """The mark is part of a word, not a break in one; otherwise the halves would be marked again."""
    once = ru_stress.mark("Привет! Через пару минут скажу результат.")
    assert ru_stress.mark(once) == once
    assert ACUTE + ACUTE not in once


def test_the_table_refuses_to_contradict_itself() -> None:
    """Every row parses, no row claims a consonant, and a form claimed twice is dropped, not guessed."""
    for entry in ru_stress.WORDS.split():
        at = entry.find(ru_stress.MARK)
        plain = entry.replace(ru_stress.MARK, "")
        assert 0 <= at < len(plain), entry
        assert plain[at] in ru_stress.VOWELS, f"{entry}: the mark is not in front of a vowel"
        assert sum(ch in ru_stress.VOWELS for ch in plain) > 1, f"{entry}: one vowel needs no mark"
    assert not ru_stress.HOMOGRAPHS & set(ru_stress.TABLE)
    assert len(ru_stress.TABLE) > 200


def test_only_the_family_that_reads_the_mark_is_handed_one() -> None:
    """espeak-ng has no rule for a combining acute, so a Piper voice must never see one."""
    assert ru_stress.applies(catalog.get("multi-supertonic"))
    assert not ru_stress.applies(catalog.get("ru-irina")), "Piper phonemises through espeak-ng"
    assert not ru_stress.applies(catalog.get("en-supertonic2")), "Russian stress is not English"
    assert not ru_stress.applies(catalog.get("uk-lada")), "the same letters, a different language"


# -- the audio -------------------------------------------------------------------------------------


def test_samples_are_clamped_rather_than_normalised() -> None:
    """A synthesiser occasionally goes just past full scale; quietening the sentence around it is worse."""
    pcm = to_pcm16([0.0, 1.0, -1.0, 2.0, -2.0, 0.5])
    values = struct.unpack("<6h", pcm)
    assert values == (0, 32767, -32767, 32767, -32768, 16383)


def test_a_wav_says_how_long_it_is_and_what_rate_it_is_at() -> None:
    body = wav(to_pcm16([0.0] * 100), 22_050)
    with wave.open(io.BytesIO(body), "rb") as handle:
        assert handle.getnchannels() == 1 and handle.getsampwidth() == 2
        assert handle.getframerate() == 22_050 and handle.getnframes() == 100


async def test_without_an_encoder_the_clip_is_a_wav_and_never_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("daedalus.speech.tts_service.encoder_present", lambda: False)
    clip, media = await encode(to_pcm16([0.0] * 32), 22_050)
    assert media == "audio/wav" and clip.startswith(b"RIFF")


async def test_an_encoder_that_fails_still_produces_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """A silent voice page is a much worse outcome than a clip ten times larger than it needed to be."""
    monkeypatch.setattr("daedalus.speech.tts_service.encoder_present", lambda: True)
    monkeypatch.setattr("daedalus.speech.tts_service.ENCODER", "definitely-not-a-program-here")
    clip, media = await encode(to_pcm16([0.0] * 32), 22_050)
    assert media == "audio/wav" and clip.startswith(b"RIFF")


async def test_an_encoder_that_hangs_is_killed_rather_than_left_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """`wait_for` cancels the wait, not the child: without the kill one is left per request, forever."""
    killed: list[bool] = []

    class Hung:
        returncode = None

        async def communicate(self, data: bytes) -> tuple[bytes, bytes]:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")  # pragma: no cover

        def kill(self) -> None:
            killed.append(True)

        async def wait(self) -> int:
            return -9

    async def spawn(*args: Any, **kwargs: Any) -> Hung:
        return Hung()

    monkeypatch.setattr("daedalus.speech.tts_service.encoder_present", lambda: True)
    monkeypatch.setattr("daedalus.speech.tts_service.ENCODE_TIMEOUT", 0.05)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    clip, media = await encode(to_pcm16([0.0] * 32), 22_050)
    assert killed == [True], "the encoder was left running with its pipes open"
    assert media == "audio/wav" and clip.startswith(b"RIFF")


# -- the engine, against a backend that is not sherpa ----------------------------------------------


class FakeAudio:
    def __init__(self, samples: list[float], rate: int) -> None:
        self.samples = samples
        self.sample_rate = rate


class FakeTts:
    """Everything the engine asks of sherpa's ``OfflineTts``, and a record of what it was asked."""

    made: list[Any] = []
    calls: list[tuple[str, int, float]] = []
    rate = 22_050
    speakers = 1

    def __init__(self, config: Any) -> None:
        self.config = config
        FakeTts.made.append(config)

    @property
    def sample_rate(self) -> int:
        return FakeTts.rate

    @property
    def num_speakers(self) -> int:
        return FakeTts.speakers

    def generate(self, text: str, sid: int = 0, speed: float = 1.0, callback: Any = None) -> FakeAudio:
        FakeTts.calls.append((text, sid, speed))
        # One sample per character, so a caller can tell one sentence's audio from another's length.
        return FakeAudio([0.25] * len(text), FakeTts.rate)


class _Config:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class FakeSherpa:
    OfflineTts = FakeTts
    OfflineTtsConfig = _Config
    OfflineTtsModelConfig = _Config
    OfflineTtsVitsModelConfig = _Config
    OfflineTtsKokoroModelConfig = _Config
    OfflineTtsKittenModelConfig = _Config
    OfflineTtsSupertonicModelConfig = _Config


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TtsEngine:
    monkeypatch.setitem(sys.modules, "sherpa_onnx", FakeSherpa)
    FakeTts.made, FakeTts.calls, FakeTts.rate, FakeTts.speakers = [], [], 22_050, 1
    lay_out(tmp_path, {"ru_RU-test-medium.onnx": b"M", "tokens.txt": b"t", **ESPEAK})
    return TtsEngine(catalog.get("ru-irina"), tmp_path, threads=3)


async def test_a_whole_answer_comes_back_as_samples_at_the_models_own_rate(engine: TtsEngine) -> None:
    pcm, rate = await engine.speak("One sentence here. Another one there.")
    assert rate == 22_050 and len(pcm) == 2 * len("One sentence here.Another one there.")
    assert [text for text, _, _ in FakeTts.calls] == ["One sentence here.", "Another one there."]


async def test_the_first_sentence_arrives_before_the_last_is_made(engine: TtsEngine) -> None:
    """The whole point of the streaming path: audio for sentence one while sentence three is working."""
    seen: list[int] = []
    async for chunk in engine.stream("First sentence here. Second sentence there. Third one now."):
        seen.append(len(FakeTts.calls))
        assert chunk, "an empty chunk is not audio and should not have been yielded"
    assert seen == [1, 2, 3], "a chunk only arrived after everything had been synthesised"


async def test_the_speed_and_the_speaker_reach_every_sentence(engine: TtsEngine) -> None:
    FakeTts.speakers = 8
    await engine.speak("The first sentence here. The second sentence there.", speaker=3, speed=1.4)
    assert [(sid, speed) for _, sid, speed in FakeTts.calls] == [(3, 1.4), (3, 1.4)]


async def test_a_speed_outside_what_stays_intelligible_is_brought_back_in(engine: TtsEngine) -> None:
    await engine.speak("Something to say.", speed=9.0)
    await engine.speak("Something else.", speed=0.01)
    assert [speed for _, _, speed in FakeTts.calls] == [2.0, 0.5]


def test_a_speaker_is_found_by_name_and_an_unknown_one_is_the_first(engine: TtsEngine) -> None:
    """A name the model does not know must become speaker zero, not an error on the voice page."""
    FakeTts.speakers = 8
    kitten = TtsEngine.speaker_id
    engine.voice = catalog.get("en-kitten")  # type: ignore[misc]
    assert kitten(engine, "expr-voice-2-m") == 0
    assert kitten(engine, "expr-voice-4-f") == 5
    assert kitten(engine, "nobody") == 0 and kitten(engine, "") == 0
    assert kitten(engine, "3") == 3 and kitten(engine, "99") == 0
    assert kitten(engine, 2) == 2 and kitten(engine, -1) == 0


async def test_a_document_is_not_read_aloud(engine: TtsEngine) -> None:
    with pytest.raises(TtsError, match="most at once"):
        await engine.speak("word. " * ((MAX_TEXT_CHARS // 6) + 10))


def test_a_piper_voice_is_handed_its_own_espeak_data_and_the_thread_count(engine: TtsEngine) -> None:
    config = FakeTts.made[-1]
    assert config.model.num_threads == 3 and config.model.provider == "cpu"
    assert config.model.vits.data_dir.endswith("espeak-ng-data")
    assert config.max_num_sentences == 1, "sherpa must not do its own splitting; this engine already did"


def test_a_lexicon_voice_is_handed_its_rules_instead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sherpa_onnx", FakeSherpa)
    FakeTts.made = []
    files = {"zh.onnx": b"M", "tokens.txt": b"t", "lexicon.txt": b"l", "phone.fst": b"1", "number.fst": b"2"}
    TtsEngine(catalog.get("zh-xiaoya"), lay_out(tmp_path, files))
    config = FakeTts.made[-1]
    assert config.model.vits.lexicon.endswith("lexicon.txt")
    assert "phone.fst" in config.rule_fsts and "number.fst" in config.rule_fsts


def test_a_kokoro_voice_is_handed_its_style_vectors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sherpa_onnx", FakeSherpa)
    FakeTts.made = []
    files = {"model.int8.onnx": b"M", "tokens.txt": b"t", "voices.bin": b"V", **ESPEAK}
    TtsEngine(catalog.get("en-kokoro"), lay_out(tmp_path, files))
    assert FakeTts.made[-1].model.kokoro.voices.endswith("voices.bin")


def test_a_supertonic_voice_is_handed_all_four_graphs_and_its_style_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "sherpa_onnx", FakeSherpa)
    FakeTts.made = []
    TtsEngine(catalog.get("multi-supertonic"), lay_out(tmp_path, SUPERTONIC), threads=2)
    built = FakeTts.made[-1].model.supertonic
    assert built.text_encoder.endswith("text_encoder.int8.onnx")
    assert built.duration_predictor.endswith("duration_predictor.int8.onnx")
    assert built.vector_estimator.endswith("vector_estimator.int8.onnx")
    assert built.vocoder.endswith("vocoder.int8.onnx")
    assert built.tts_json.endswith("tts.json") and built.unicode_indexer.endswith("unicode_indexer.bin")
    assert built.voice_style.endswith("voice.bin"), "the ten styles are where the speakers come from"
    assert not hasattr(FakeTts.made[-1].model, "vits"), "nothing else may be configured beside it"


def test_a_family_that_renders_quietly_is_played_at_the_same_level_as_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise an A/B between two voices is decided by their level rather than by how they sound."""
    monkeypatch.setitem(sys.modules, "sherpa_onnx", FakeSherpa)
    FakeTts.made, FakeTts.calls, FakeTts.rate, FakeTts.speakers = [], [], 44_100, 10
    engine = TtsEngine(catalog.get("multi-supertonic"), lay_out(tmp_path, SUPERTONIC), threads=2)
    quiet = engine.render("A sentence to say out loud.")
    plain = to_pcm16([0.25] * (len(quiet) // 2))
    assert OUTPUT_GAIN["supertonic"] != 1.0
    assert len(quiet) == len(plain), "the gain must not change how many samples come out"
    assert quiet != plain, "the family's gain was not applied"
    expected = int(0.25 * OUTPUT_GAIN["supertonic"] * 32767)
    assert struct.unpack("<h", quiet[:2])[0] == expected


def test_the_gain_cannot_push_a_sample_past_full_scale() -> None:
    """A clamp rather than a wrap: past full scale the sample is loud, not a click in the other direction."""
    assert to_pcm16([0.9, -0.9], 2.0) == struct.pack("<hh", 32767, -32768)
    assert to_pcm16([0.5], 1.0) == to_pcm16([0.25], 2.0)


async def test_a_voice_dropped_while_it_was_loading_does_not_become_resident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator deleted it, or chose another, in the second and a bit the load takes."""
    monkeypatch.setitem(sys.modules, "sherpa_onnx", FakeSherpa)
    lay_out(tmp_path, {"ru_RU-test-medium.onnx": b"M", "tokens.txt": b"t", **ESPEAK})
    cache = TtsCache()
    started = threading.Event()
    dropped = threading.Event()

    class Slow(TtsEngine):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # Held open by the test rather than by a sleep: the whole point is that the drop lands
            # in the middle of the load, and a duration says "in the middle" only on an idle host.
            started.set()
            dropped.wait(SETTLE)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("daedalus.speech.tts_engine.TtsEngine", Slow)
    loading = asyncio.create_task(cache.get(catalog.get("ru-irina"), tmp_path, threads=2))
    await asyncio.to_thread(started.wait, SETTLE)
    cache.drop()
    dropped.set()
    engine = await loading
    assert engine is not None, "the caller that asked for the voice was left with nothing"
    assert cache.loaded() == "", "the drop was overwritten by the load it arrived in the middle of"


# -- the application's view of it -----------------------------------------------------------------


class FakeManager:
    providers: Any = None

    def reload_config(self, config: RuntimeConfig) -> None:
        pass


class FakeApp:
    """The application as the API reads it, with a real voices directory under tmp_path."""

    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings(_env_file=None, state_dir=tmp_path / "state")  # type: ignore[call-arg]
        self.config = RuntimeConfig()
        self.manager = FakeManager()
        self.front: Any = None
        self.extensions: dict[str, Any] = {}
        self.tts = LocalTts(self.settings.state_dir, self.config)
        # The API builds both halves of speech; the recognition one is here only so that the routes
        # that touch it do not fall over while the synthesis ones are being exercised.
        self.speech = LocalSpeech(self.settings.state_dir, self.config)

    async def save_config(self, config: RuntimeConfig) -> None:
        self.config = config
        self.tts.config = config
        self.speech.config = config


@pytest.fixture
def client(tmp_path: Path) -> Any:
    app = FakeApp(tmp_path)
    with TestClient(build_app(app, "tok")) as c:  # type: ignore[arg-type]
        c.app_state = app  # type: ignore[attr-defined]
        yield c


HEAD = {"X-Daedalus-Token": "tok"}


def pretend_installed(app: FakeApp, voice_id: str = "ru-dmitri") -> None:
    """Put a voice on the disk without downloading one, so the API can be exercised offline."""
    voice = catalog.get(voice_id)
    lay_out(app.tts.downloads.directory(voice.id), {"v.onnx": b"M", "tokens.txt": b"t", **ESPEAK})
    record = Installed(id=voice.id, archive=voice.archive, sha256=voice.sha256, disk_bytes=4096)
    manifest = app.tts.downloads.manifest()
    manifest[voice.id] = record
    app.tts.downloads._write_manifest(manifest)


def test_the_picker_is_served_with_everything_the_page_needs(client: TestClient) -> None:
    body = client.get("/api/tts", headers=HEAD).json()
    assert len(body["models"]) == len(catalog.VOICES)
    assert body["selected"] == "" and body["state"]["voice"] == "" and body["state"]["threads"] == 2
    assert body["recommended"]["ru"] == "multi-supertonic" and body["recommended"]["en"] == "en-supertonic2"
    assert body["languages"] == catalog.languages()
    ryan = next(m for m in body["models"] if m["id"] == "en-ryan")
    assert ryan["keeps_up"] is False, "the one voice slower than speech has to say so on its card"
    kokoro = next(m for m in body["models"] if m["id"] == "en-kokoro")
    assert kokoro["keeps_up"] is True and len(kokoro["speakers"]) == 11
    both = next(m for m in body["models"] if m["id"] == "multi-supertonic")
    assert both["languages"] == ["ru", "en", "de", "es", "fr", "it", "pl", "pt", "uk"]
    assert both["new"] is True and "OpenRAIL-M" in both["licence"]


def test_a_voice_that_is_not_downloaded_cannot_be_chosen(client: TestClient) -> None:
    refused = client.post("/api/tts/select", json={"voice": "ru-dmitri"}, headers=HEAD)
    assert refused.status_code == 409 and "not downloaded" in refused.json()["detail"]
    assert client.app_state.config.companion.tts.local_voice == ""  # type: ignore[attr-defined]


def test_downloading_something_that_is_not_a_voice_is_a_404(client: TestClient) -> None:
    assert client.post("/api/tts/voices/not-a-voice/download", headers=HEAD).status_code == 404
    assert client.delete("/api/tts/voices/not-a-voice", headers=HEAD).status_code == 404
    assert client.post("/api/tts/voices/not-a-voice/sample", headers=HEAD).status_code == 404


def test_choosing_a_voice_saves_it_and_warms_it(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    app = client.app_state  # type: ignore[attr-defined]
    pretend_installed(app)
    warmed: list[int] = []

    def warm() -> dict[str, object]:
        warmed.append(1)
        return {"state": "loading", "voice": "ru-dmitri", "loaded_in_ms": 0, "error": ""}

    monkeypatch.setattr(app.tts, "warm", warm)
    body = client.post("/api/tts/select", json={"voice": "ru-dmitri"}, headers=HEAD).json()
    assert body["selected"] == "ru-dmitri" and warmed == [1], "the voice loads when it is chosen, not mid-answer"
    assert body["load"]["state"] == "loading", "and the answer says the load has started, so the page can wait for it"
    assert app.config.companion.tts.local_voice == "ru-dmitri"


def test_the_speed_and_the_threads_are_saved_on_their_own(client: TestClient) -> None:
    body = client.post("/api/tts/select", json={"speed": 1.25, "threads": 4}, headers=HEAD).json()
    assert body["state"]["speed"] == 1.25 and body["state"]["threads"] == 4
    assert client.post("/api/tts/select", json={"speed": 9.0}, headers=HEAD).status_code == 422
    assert client.post("/api/tts/select", json={"threads": 99}, headers=HEAD).status_code == 422


def test_a_speaker_does_not_survive_a_change_of_voice(client: TestClient) -> None:
    """A name belongs to the model that has it; carried across, it silently becomes speaker zero."""
    app = client.app_state  # type: ignore[attr-defined]
    pretend_installed(app, "en-kokoro")
    client.post("/api/tts/select", json={"voice": "en-kokoro"}, headers=HEAD)
    client.post("/api/tts/select", json={"speaker": "af_sarah"}, headers=HEAD)
    assert app.config.companion.tts.local_speaker == "af_sarah"
    pretend_installed(app, "ru-dmitri")
    client.post("/api/tts/select", json={"voice": "ru-dmitri"}, headers=HEAD)
    assert app.config.companion.tts.local_speaker == ""


def test_deleting_the_voice_in_use_stops_using_it(client: TestClient) -> None:
    app = client.app_state  # type: ignore[attr-defined]
    pretend_installed(app)
    client.post("/api/tts/select", json={"voice": "ru-dmitri"}, headers=HEAD)
    body = client.delete("/api/tts/voices/ru-dmitri", headers=HEAD).json()
    assert body["deleted"] is True and body["selected"] == ""
    assert app.config.companion.tts.local_voice == "" and not app.tts.downloads.is_installed("ru-dmitri")


def test_a_sample_of_a_voice_that_is_not_here_is_refused_rather_than_downloaded(client: TestClient) -> None:
    refused = client.post("/api/tts/voices/ru-irina/sample", headers=HEAD)
    assert refused.status_code == 409 and "not downloaded" in refused.json()["detail"]


def test_a_sample_is_the_voices_own_language_through_the_real_engine(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    app = client.app_state  # type: ignore[attr-defined]
    pretend_installed(app, "ru-irina")
    monkeypatch.setitem(sys.modules, "sherpa_onnx", FakeSherpa)
    monkeypatch.setattr("daedalus.speech.tts_service.encoder_present", lambda: False)
    FakeTts.calls = []
    app.tts.forget()
    answer = client.post("/api/tts/voices/ru-irina/sample", headers=HEAD)
    assert answer.status_code == 200 and answer.headers["content-type"].startswith("audio/wav")
    assert answer.content.startswith(b"RIFF") and len(answer.content) > 44
    assert FakeTts.calls and "Привет" in FakeTts.calls[0][0]


def test_a_multilingual_voice_samples_in_the_language_it_is_being_picked_for(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Filter the picker to Polish, press Play on a voice that reads Polish, hear Polish.

    A multilingual voice is filed under one language and used to read its sample in that one, so an
    operator narrowing the list to Polish pressed Play on the voice the picker had correctly offered
    them and heard Russian.
    """
    app = client.app_state  # type: ignore[attr-defined]
    voice = catalog.get("multi-supertonic")
    lay_out(app.tts.downloads.directory(voice.id), {
        **{f"{stem}.onnx": b"M" for stem in SUPERTONIC_GRAPHS},
        "tts.json": b"{}", "unicode_indexer.bin": b"i", "voice.bin": b"v",
    })
    manifest = app.tts.downloads.manifest()
    manifest[voice.id] = Installed(id=voice.id, archive=voice.archive, sha256=voice.sha256, disk_bytes=4096)
    app.tts.downloads._write_manifest(manifest)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", FakeSherpa)
    monkeypatch.setattr("daedalus.speech.tts_service.encoder_present", lambda: False)
    # The Russian sample goes through the stress dictionary on its way, so it is matched on a word
    # the dictionary does not touch rather than on its first one.
    for language, expected in (("pl", "Cześć"), ("uk", "Привіт"), ("", "Вот как звучит")):
        FakeTts.calls = []
        app.tts.forget()
        query = f"?language={language}" if language else ""
        answer = client.post(f"/api/tts/voices/multi-supertonic/sample{query}", headers=HEAD)
        assert answer.status_code == 200, answer.text
        assert FakeTts.calls and expected in FakeTts.calls[0][0], (language, FakeTts.calls[0][0][:30])
    # A language it does not read is not one it can be auditioned in: it reads its own.
    FakeTts.calls = []
    app.tts.forget()
    assert client.post("/api/tts/voices/multi-supertonic/sample?language=zh", headers=HEAD).status_code == 200
    assert "Вот как звучит" in FakeTts.calls[0][0]


def test_downloading_the_chosen_voice_again_clears_a_load_that_failed(client: TestClient) -> None:
    """A voice whose first load failed stays failed until something forgets it — a re-download does.

    ``warm`` declines while the cache holds an error for the same voice, and only ``forget``/``drop``
    clears it. Delete called forget; the download path did not, so a voice re-fetched after a short
    archive stayed "error" until the operator re-selected it.
    """
    app = client.app_state  # type: ignore[attr-defined]
    app.config.companion.tts.local_voice = "ru-irina"
    forgotten: list[str] = []
    original = app.tts.forget
    app.tts.forget = lambda: (forgotten.append("ru-irina"), original())[1]  # type: ignore[method-assign]
    app.tts.downloads.installed("ru-irina")
    assert forgotten == ["ru-irina"], "the chosen voice arriving must clear whatever was held for it"
    app.tts.downloads.installed("ru-dmitri")
    assert forgotten == ["ru-irina"], "another voice arriving is nothing to do with the one in use"


# -- the precedence -------------------------------------------------------------------------------


def with_voice_page(app: FakeApp, spoken_here: list[str], timed: list[dict[str, Any]] | None = None) -> None:
    """The voice extension, reduced to the two things the TTS endpoint asks of it."""
    timed = [] if timed is None else timed

    class Extension:
        async def speech(self, text: str) -> tuple[Any, str]:
            spoken_here.append(text)

            async def chunks() -> Any:
                yield b"from-the-endpoint"

            return chunks(), "audio/mpeg"

        async def interrupt(self) -> bool:
            return True

        async def voice_state(self) -> dict[str, Any]:
            return {"enabled": True, "session_id": "", "model": "", "tts": {"configured": True, "reason": ""},
                    "stt": {"configured": False}, "agents": [], "listening": False}

        def first_audio(self, turn: str, *, clip_ms: int, load_ms: int) -> None:
            timed.append({"turn": turn, "clip_ms": clip_ms, "load_ms": load_ms})

        def last_turn(self) -> dict[str, Any]:
            return timed[-1] if timed else {"turn": "", "first_audio_ms": 0, "clip_ms": 0, "load_ms": 0}

    app.extensions["companion"] = Extension()


def clips_of(body: bytes) -> list[bytes]:
    """The per-sentence clips out of a `SEQUENCE_TYPE` body, as the page reads them."""
    out, at = [], 0
    while at < len(body):
        size = int.from_bytes(body[at:at + LENGTH_BYTES], "big")
        at += LENGTH_BYTES
        out.append(body[at:at + size])
        at += size
    return out


def test_with_nothing_configured_the_browser_is_told_to_speak(client: TestClient) -> None:
    with_voice_page(client.app_state, [])  # type: ignore[attr-defined]
    client.app_state.config.companion.tts.url = ""  # type: ignore[attr-defined]
    answer = client.post("/api/companion/tts", json={"text": "a sentence"}, headers=HEAD)
    assert answer.status_code == 404 and "browser" in answer.json()["detail"]


def test_an_endpoint_speaks_where_no_voice_is_downloaded(client: TestClient) -> None:
    app = client.app_state  # type: ignore[attr-defined]
    sent: list[str] = []
    with_voice_page(app, sent)
    app.config.companion.tts.url = "http://speech.invalid/v1"
    answer = client.post("/api/companion/tts", json={"text": "a sentence"}, headers=HEAD)
    assert answer.status_code == 200 and answer.content == b"from-the-endpoint" and sent == ["a sentence"]


def test_a_downloaded_voice_speaks_ahead_of_the_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    app = client.app_state  # type: ignore[attr-defined]
    sent: list[str] = []
    with_voice_page(app, sent)
    app.config.companion.tts.url = "http://speech.invalid/v1"
    pretend_installed(app)
    client.post("/api/tts/select", json={"voice": "ru-dmitri"}, headers=HEAD)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", FakeSherpa)
    monkeypatch.setattr("daedalus.speech.tts_service.encoder_present", lambda: False)
    app.tts.forget()
    answer = client.post("/api/companion/tts", json={"text": "One sentence here. Another one there."}, headers=HEAD)
    assert answer.status_code == 200 and answer.headers["content-type"].startswith(SEQUENCE_TYPE)
    assert answer.headers[MEDIA_TYPE_HEADER] == "audio/wav"
    clips = clips_of(answer.content)
    assert len(clips) == 2, "the two sentences did not arrive as two clips"
    assert all(clip.startswith(b"RIFF") for clip in clips)
    assert sent == [], "the metered endpoint was called while a voice was downloaded here"


def ready_to_speak(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Any:
    """An application with a voice on the disk, the fake engine behind it, and nothing synthesised yet."""
    app = client.app_state  # type: ignore[attr-defined]
    with_voice_page(app, [])
    pretend_installed(app)
    client.post("/api/tts/select", json={"voice": "ru-dmitri"}, headers=HEAD)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", FakeSherpa)
    monkeypatch.setattr("daedalus.speech.tts_service.encoder_present", lambda: False)
    app.tts.forget()
    FakeTts.calls.clear()
    return app


async def test_a_sentence_is_handed_over_the_moment_it_is_made_rather_than_at_the_end(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the page is waiting for: clip one exists while sentence three has not been started."""
    app = ready_to_speak(client, monkeypatch)
    seen: list[int] = []
    async for clip, media_type in app.tts.clips("First sentence here. Second sentence there. Third one now."):
        seen.append(len(FakeTts.calls))
        assert clip.startswith(b"RIFF") and media_type == "audio/wav"
    assert seen == [1, 2, 3], f"a clip only arrived after everything had been synthesised: {seen}"


async def test_a_listener_that_goes_away_stops_the_synthesiser_at_the_next_sentence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing the generator is what a disconnected client does to it, and it must be enough."""
    app = ready_to_speak(client, monkeypatch)
    text = " ".join(f"Sentence number {n} here." for n in range(1, 21))
    clips = app.tts.clips(text)
    await anext(clips)
    await clips.aclose()
    assert len(FakeTts.calls) == 1, "sentences were synthesised for a listener that had gone"


async def test_an_interrupt_stops_an_answer_that_is_being_read_aloud(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = ready_to_speak(client, monkeypatch)
    text = " ".join(f"Sentence number {n} here." for n in range(1, 21))
    got = 0
    async for _clip, _type in app.tts.clips(text):
        got += 1
        if got == 2:
            app.tts.interrupt()
    assert got == 2, f"the barge-in did not stop the reading at the next sentence: {got} clips"
    assert len(FakeTts.calls) == 2


async def test_an_interrupt_does_not_stop_the_answer_that_comes_after_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mark moves once; the next thing asked for is a new answer and is not born cancelled."""
    app = ready_to_speak(client, monkeypatch)
    app.tts.interrupt()
    got = [clip async for clip, _ in app.tts.clips("One sentence here. Another one there.")]
    assert len(got) == 2


async def test_hearing_another_voice_puts_the_chosen_one_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sampling takes the one resident slot; the next answer must not pay to reload what was chosen."""
    app = ready_to_speak(client, monkeypatch)
    pretend_installed(app, "ru-irina")
    assert client.post("/api/tts/voices/ru-irina/sample", headers=HEAD).status_code == 200
    await until(lambda: CACHE.loaded() == "ru-dmitri", "the chosen voice was put back after the sample")


def test_the_barge_in_endpoint_stops_the_run_and_the_reading(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    app = ready_to_speak(client, monkeypatch)
    before = app.tts._speaking
    assert client.post("/api/companion/interrupt", json={}, headers=HEAD).json() == {"stopped": True}
    assert app.tts._speaking != before, "the endpoint stopped the run but left the synthesiser reading"


def test_a_local_voice_that_fails_is_not_replaced_by_a_metered_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator chose it. A failure hidden behind a paid fallback is a failure nobody fixes."""
    app = client.app_state  # type: ignore[attr-defined]
    sent: list[str] = []
    with_voice_page(app, sent)
    app.config.companion.tts.url = "http://speech.invalid/v1"
    pretend_installed(app)
    client.post("/api/tts/select", json={"voice": "ru-dmitri"}, headers=HEAD)

    async def refuse(text: str) -> tuple[bytes, str]:
        raise TtsError("the model would not load")

    async def refuse_clips(text: str) -> Any:
        raise TtsError("the model would not load")
        yield b""  # pragma: no cover - the raise above is the whole of it

    monkeypatch.setattr(app.tts, "audio", refuse)
    monkeypatch.setattr(app.tts, "clips", refuse_clips)
    answer = client.post("/api/companion/tts", json={"text": "a sentence"}, headers=HEAD)
    assert answer.status_code == 503 and "would not load" in answer.json()["detail"]
    assert sent == []


def test_a_voice_that_breaks_in_a_way_nobody_expected_still_says_why(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not every failure out of a native wheel is a TtsError; none of them may be a bare 500."""
    app = client.app_state  # type: ignore[attr-defined]
    sent: list[str] = []
    with_voice_page(app, sent)
    app.config.companion.tts.url = "http://speech.invalid/v1"
    pretend_installed(app)
    client.post("/api/tts/select", json={"voice": "ru-dmitri"}, headers=HEAD)

    async def explode(text: str) -> Any:
        raise RuntimeError("the wheel is built for another processor")
        yield b""  # pragma: no cover - the raise above is the whole of it

    monkeypatch.setattr(app.tts, "clips", explode)
    answer = client.post("/api/companion/tts", json={"text": "a sentence"}, headers=HEAD)
    assert answer.status_code == 503 and "another processor" in answer.json()["detail"]
    assert sent == []


def test_an_oversized_sentence_is_refused_before_anything_is_synthesised(client: TestClient) -> None:
    app = client.app_state  # type: ignore[attr-defined]
    with_voice_page(app, [])
    pretend_installed(app)
    client.post("/api/tts/select", json={"voice": "ru-dmitri"}, headers=HEAD)
    assert client.post("/api/companion/tts", json={"text": "x" * 4000}, headers=HEAD).status_code == 413
    assert client.post("/api/companion/tts", json={"text": "  "}, headers=HEAD).status_code == 400


def test_the_page_is_told_which_of_the_three_is_speaking(client: TestClient) -> None:
    app = client.app_state  # type: ignore[attr-defined]
    with_voice_page(app, [])
    body = client.get("/api/companion/voice", headers=HEAD).json()
    assert body["tts"]["kind"] == "endpoint" and body["tts"]["state"] == "ready"
    pretend_installed(app)
    client.post("/api/tts/select", json={"voice": "ru-dmitri"}, headers=HEAD)
    body = client.get("/api/companion/voice", headers=HEAD).json()
    assert body["tts"]["kind"] == "local" and body["tts"]["voice"] == "Dmitri (Russian)"
    assert body["tts"]["state"] in ("loading", "ready", "error")
    assert body["tts"]["local"]["installed"] is True


# -- warming the voice ------------------------------------------------------------------------------
#
# A voice is a second or two of building a synthesiser, and until it was warmed that second or two was
# paid by the first answer: the words were on the screen and nothing was said, and by the time the
# voice spoke the operator had already asked again. Everything below is about paying it earlier.


async def test_the_cache_reports_where_a_load_is_and_publishes_every_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sherpa_onnx", FakeSherpa)
    FakeTts.made, FakeTts.calls = [], []
    lay_out(tmp_path, {"v.onnx": b"M", "tokens.txt": b"t", **ESPEAK})
    cache = TtsCache()
    assert cache.state().as_json() == {"state": "idle", "voice": "", "loaded_in_ms": 0, "error": ""}
    seen: list[dict[str, object]] = []
    async with cache.watch() as queue:
        await cache.get(catalog.get("ru-dmitri"), tmp_path, threads=2)
        while not queue.empty():
            seen.append(queue.get_nowait())
    assert [frame["state"] for frame in seen] == ["loading", "ready"]
    assert cache.state().state == "ready" and cache.state().voice == "ru-dmitri"
    assert cache.loaded() == "ru-dmitri"
    # The load was timed, and a second call is the same voice rather than a second load.
    assert int(cache.state().loaded_in_ms) >= 0
    made = len(FakeTts.made)
    await cache.get(catalog.get("ru-dmitri"), tmp_path, threads=2)
    assert len(FakeTts.made) == made, "a voice already in memory is not built again"
    cache.drop()
    assert cache.state().state == "idle" and cache.loaded() == ""


async def ready_state(cache: TtsCache) -> Any:
    """Wait for a load to finish by watching it, rather than by sleeping for a guessed while."""
    async with cache.watch() as queue:
        while cache.state().state == "loading":
            await queue.get()
    return cache.state()


async def test_warming_returns_at_once_and_twice_is_one_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sherpa_onnx", FakeSherpa)
    FakeTts.made, FakeTts.calls = [], []
    lay_out(tmp_path, {"v.onnx": b"M", "tokens.txt": b"t", **ESPEAK})
    cache = TtsCache()
    voice = catalog.get("ru-dmitri")
    first = cache.warm(voice, tmp_path, threads=2)
    second = cache.warm(voice, tmp_path, threads=2)
    assert first.state == "loading" and second.state == "loading", "warming answers before the voice is built"
    assert (await ready_state(cache)).state == "ready"
    assert len(FakeTts.made) == 1, "warming twice is one load, not two"
    # And a third, with the voice already in memory, is not a load at all.
    assert cache.warm(voice, tmp_path, threads=2).state == "ready"
    assert len(FakeTts.made) == 1


async def test_a_voice_that_will_not_load_says_so_once_and_is_not_retried_on_every_poll(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tries: list[int] = []

    class Broken:
        def __init__(self, config: Any) -> None:
            tries.append(1)
            raise RuntimeError("this build of the wheel cannot read that model")

    class BrokenSherpa(FakeSherpa):
        OfflineTts = Broken

    monkeypatch.setitem(sys.modules, "sherpa_onnx", BrokenSherpa)
    lay_out(tmp_path, {"v.onnx": b"M", "tokens.txt": b"t", **ESPEAK})
    cache = TtsCache()
    voice = catalog.get("ru-dmitri")
    cache.warm(voice, tmp_path, threads=2)
    state = await ready_state(cache)
    assert state.state == "error" and "cannot read that model" in state.error
    # The voice page asks on every poll, and a load that failed fails the same way every time.
    cache.warm(voice, tmp_path, threads=2)
    cache.warm(voice, tmp_path, threads=2)
    assert tries == [1], "the failure stands until the selection changes; it is not retried per poll"


def test_the_voice_page_and_the_picker_are_told_where_the_load_is(client: TestClient) -> None:
    app = client.app_state  # type: ignore[attr-defined]
    CACHE.drop()  # the cache is process-wide, because the voice it holds is
    with_voice_page(app, [])
    pretend_installed(app)
    body = client.post("/api/tts/select", json={"voice": "ru-dmitri"}, headers=HEAD).json()
    assert body["load"]["state"] in ("loading", "ready", "error")
    picker = client.get("/api/tts", headers=HEAD).json()
    assert set(picker["load"]) == {"state", "voice", "loaded_in_ms", "error"}
    page = client.get("/api/companion/voice", headers=HEAD).json()
    assert page["tts"]["kind"] == "local"
    assert page["tts"]["state"] in ("loading", "ready", "error")
    assert "loaded_in_ms" in page["tts"] and "last_turn" in page["tts"]


def test_the_warm_route_loads_the_voice_without_anything_waiting_to_be_spoken(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    app = client.app_state  # type: ignore[attr-defined]
    CACHE.drop()
    pretend_installed(app)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", FakeSherpa)
    FakeTts.made = []
    client.post("/api/tts/select", json={"voice": "ru-dmitri"}, headers=HEAD)
    answer = client.post("/api/tts/engine/warm", headers=HEAD)
    assert answer.status_code == 200
    assert set(answer.json()) == {"state", "voice", "loaded_in_ms", "error"}
    # The route answers before the voice is built — that is the whole point of it — so the load is
    # given the event loop by asking again rather than by sleeping for a guessed while.
    for _ in range(50):
        if client.post("/api/tts/engine/warm", headers=HEAD).json()["state"] != "loading":
            break
    assert CACHE.loaded() == "ru-dmitri", "the voice is in memory before the first answer asks for it"
    app.tts.forget()


def test_nothing_is_warmed_where_no_voice_was_ever_chosen(tmp_path: Path) -> None:
    CACHE.drop()
    local = LocalTts(tmp_path, RuntimeConfig())
    assert local.warm() == {"state": "idle", "voice": "", "loaded_in_ms": 0, "error": ""}
    assert local.load_state()["state"] == "idle"


def test_a_voice_the_catalog_no_longer_has_is_ignored_rather_than_crashing(tmp_path: Path) -> None:
    config = RuntimeConfig()
    config.companion.tts.local_voice = "a-voice-that-was-removed"
    local = LocalTts(tmp_path, config)
    assert local.selected() is None and local.available() is False
    # The page is told nothing local speaks and why; the doctor says the same in a sentence.
    state = local.state()
    assert state["voice"] == "" and state["state"] == "error"
    assert "not in the catalog" in str(state["error"])


# -- one real voice, where one has been downloaded ---------------------------------------------------

REAL = os.environ.get("DAEDALUS_TTS_MODELS", "")


@pytest.mark.skipif(not REAL, reason="set DAEDALUS_TTS_MODELS to a directory of unpacked voices to measure one")
def test_a_real_voice_speaks_and_keeps_up_with_a_person_talking() -> None:
    """The measurement the catalog's numbers come from, against whatever is actually on this disk.

    Skipped in CI, where no voice is downloaded. It asserts the two things a number in the catalog is
    a claim about: that the audio is roughly as long as the sentence deserves, and that the first
    sentence of a three-sentence answer is ready long before the last one is.
    """
    root = Path(REAL)
    by_archive = {v.archive.removesuffix(".tar.bz2"): v for v in catalog.VOICES}
    found = [p for p in sorted(root.iterdir()) if p.is_dir() and p.name in by_archive]
    if not found:
        pytest.skip(f"no unpacked catalog voice in {root}")

    async def run(engine: TtsEngine, text: str) -> tuple[float, float, float]:
        started = time.perf_counter()
        first, total = 0.0, 0
        async for chunk in engine.stream(text):
            first = first or time.perf_counter() - started
            total += len(chunk)
        return first, time.perf_counter() - started, total / 2 / engine.sample_rate

    for directory in found:
        voice = by_archive[directory.name]
        engine = TtsEngine(voice, directory, threads=2)
        assert engine.sample_rate == voice.sample_rate, f"{voice.id}: the catalog's rate is not the model's"
        assert engine.num_speakers == max(1, len(voice.speakers)), f"{voice.id}: the catalog's speaker list is wrong"
        first, elapsed, seconds = asyncio.run(run(engine, voice.sample()))
        print(f"\n{voice.id}: first sentence {first * 1000:.0f} ms, {elapsed:.2f} s of work for "
              f"{seconds:.2f} s of audio, RTF {elapsed / seconds:.3f}")
        # A voice whose phonemiser dropped the text still produces a clip; it is just far too short
        # for what it was given. That is exactly how one catalog entry was found to be unusable.
        assert seconds / len(voice.sample()) > 0.02, f"{voice.id}: far too little audio per character"
        assert first < elapsed, f"{voice.id}: nothing was handed over before the whole answer was made"
        if voice.keeps_up:
            assert elapsed / seconds < 1.0, f"{voice.id} is marked as keeping up and does not here"


LONG_RUSSIAN = (
    "Сегодня утром пришли три письма, и все три просят одного и того же — подтвердить счёт. "
    "Я отложил их в отдельную папку, чтобы вы посмотрели, когда будет минута. "
    "Ещё один агент закончил разбор логов за неделю и ждёт вашего слова, прежде чем что-то удалять. "
    "Остальное может подождать до завтра, ничего срочного там нет. "
    "Счета я свёл в одну таблицу, чтобы их было видно рядом, а не по одному письму за раз, "
    "и подписал каждую строку парой слов сразу."
)
"""Four hundred and forty-three characters of ordinary Russian: about what one answer runs to."""


@pytest.mark.skipif(not REAL, reason="set DAEDALUS_TTS_MODELS to a directory of unpacked voices to measure one")
def test_a_real_voice_starts_talking_long_before_it_has_finished_reading(tmp_path: Path) -> None:
    """The number the whole streaming path exists for: how long a person waits before hearing a word.

    A paragraph of Russian, through the service the endpoint uses, with the first clip timed. The
    ceiling is generous because this runs on whatever the machine is; what it is guarding against is
    the old behaviour, where nothing at all came back until the last sentence was rendered.
    """
    root = Path(REAL)
    by_archive = {v.archive.removesuffix(".tar.bz2"): v for v in catalog.VOICES}
    found = [p for p in sorted(root.iterdir()) if p.is_dir() and p.name in by_archive]
    if not found:
        pytest.skip(f"no unpacked catalog voice in {root}")
    # A Russian paragraph is timed through a voice that speaks Russian. Reading it with an English
    # one measures a synthesiser spelling its way through a foreign alphabet, which is slower than
    # anything this is a claim about and is not what anybody's machine would be doing.
    directory = next((p for p in found if by_archive[p.name].speaks("ru")), found[0])
    voice = by_archive[directory.name]

    state = tmp_path / "state"
    into = state / "models" / "tts" / voice.id
    into.parent.mkdir(parents=True, exist_ok=True)
    into.symlink_to(directory)
    config = RuntimeConfig()
    config.companion.tts.local_voice = voice.id
    config.companion.tts.local_threads = 2
    tts = LocalTts(state, config)
    tts.downloads._write_manifest({voice.id: Installed(id=voice.id, archive=voice.archive, sha256=voice.sha256, disk_bytes=1)})
    assert tts.available()

    async def run() -> tuple[float, float, int]:
        # Loaded first, as choosing a voice on the page loads it: what is being measured is the wait
        # before an answer, not the wait before the first answer of the process's life.
        await tts.warm()
        started = time.perf_counter()
        first, count = 0.0, 0
        async for _clip, _type in tts.clips(LONG_RUSSIAN):
            first = first or time.perf_counter() - started
            count += 1
        return first, time.perf_counter() - started, count

    first, elapsed, count = asyncio.run(run())
    print(f"\n{voice.id}: {len(LONG_RUSSIAN)} characters, first clip {first * 1000:.0f} ms, "
          f"{count} clips, {elapsed:.2f} s in all")
    assert count > 1, "a paragraph came back as one clip; nothing was being streamed"
    assert first < elapsed / 3, "the first clip took as long as the whole answer"
    assert first < 0.9, f"the first words took {first:.2f} s; the page is silent for that long"
