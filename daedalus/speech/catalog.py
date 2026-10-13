"""The list of speech models the operator can choose from, and what is known about each.

Every entry is one archive from the sherpa-onnx model zoo — a GitHub release of the engine's own
project, so the download host is the same people who wrote the runtime. An archive unpacks into a
directory of ONNX files whose names differ from family to family; the catalog deliberately does not
record those names. It records the *kind* of model, and :mod:`daedalus.speech.engine` finds the files
inside the unpacked directory by their shape. A zoo that renames ``encoder.onnx`` to
``encoder.int8.onnx`` in a later build then costs nothing, where a hard-coded file list would have
turned into a model that downloads and refuses to load.

``sha256`` is the archive's digest. Most of it comes from GitHub, which reports a ``digest`` for every
release asset uploaded since mid-2025 and which covers all but two of the entries below; the rest was
hashed here, and where both exist they agree. Two archives predate the field and have none — see
``contents``, which pins their file list so that size alone is not the whole of the check — and
``SpeechModel.verified`` says which is which, so the picker can tell the operator plainly rather than
implying every download is checksummed. Sizes are the compressed archive; ``unpacked_bytes`` is what
the disk actually gives up, and ``memory_mb`` is roughly what the loaded model costs in RAM.

Scores are the same two 0-100 bars the operator sees: ``accuracy`` is how well it is expected to hear,
``speed`` how much faster than real time it runs on an ordinary CPU. They are editorial — read them as
"this one is quicker than that one", not as a measurement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

ZOO = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
"""Where every archive below comes from."""

Kind = str
"""How the engine loads a model. One of:

``transducer``      encoder/decoder/joiner, streaming (Zipformer, T-one, NeMo fast-conformer).
``nemo_transducer`` the same three files, NeMo's own flavour, batch only (GigaAM).
``ctc``             a single ``model.onnx`` with a CTC head, batch (NeMo CTC, Dolphin).
``t_one_ctc``       T-one's single streaming CTC model file.
``whisper``         Whisper's encoder/decoder pair, batch, multilingual.
``moonshine``       Moonshine's four-file preprocess/encode/decode set, batch.
``sense_voice``     SenseVoice's single model file, batch.
"""

STREAMING_KINDS = frozenset({"transducer", "t_one_ctc"})
"""Kinds that produce words while the operator is still talking. The rest need the whole utterance."""


@dataclass(frozen=True)
class SpeechModel:
    """One downloadable model, exactly as the picker shows it."""

    id: str
    """Stable, short, ours. Config and the installed-model directory are named by it, so it never changes."""
    label: str
    archive: str
    """Basename of the ``.tar.bz2`` in the zoo release; the download URL is ``ZOO`` plus this."""
    kind: Kind
    languages: tuple[str, ...]
    """ISO 639-1 codes. ``("*",)`` means the model detects the language itself across a long list."""
    size_bytes: int
    """The archive as downloaded, for the progress bar and the disk estimate."""
    unpacked_bytes: int
    """What it occupies once unpacked. The download is deleted afterwards, so this is the lasting cost."""
    memory_mb: int
    """Roughly what the loaded model adds to the process."""
    licence: str
    accuracy: int
    speed: int
    note: str
    """One line of UI copy: what this model is for, in the operator's terms."""
    sha256: str = ""
    """Empty where neither GitHub nor a hashing run here produced one; see the module docstring."""
    contents: tuple[str, ...] = ()
    """Every regular file the archive holds, relative to its one top-level directory, sorted.

    Pinned only for the entries with no digest, where it is what stands in for one: a substituted
    archive would have to match the published byte count *and* carry exactly this file list, which a
    padded swap does not. Empty means the digest carries the check instead; see ``models.verify``."""
    recommended_for: tuple[str, ...] = ()
    """Languages this is the suggested choice for. Drives the "recommended" badge, which is per language:
    the best English model and the best Russian one are different models, and a single flag cannot say so."""
    language_count: int = 0
    """How many languages the model claims, where that is more than ``languages`` lists individually."""
    aliases: tuple[str, ...] = field(default=())
    """Extra language codes the model handles that are not worth listing in the badge row."""

    @property
    def streaming(self) -> bool:
        return self.kind in STREAMING_KINDS

    @property
    def verified(self) -> bool:
        """Whether a published digest stands behind this archive. False means size and file list only."""
        return bool(self.sha256)

    @property
    def detects_language(self) -> bool:
        """Whether asking this model for "auto" actually lets it decide.

        Whisper does not: sherpa's wrapper wants a language at load time and detecting one costs a
        pass over audio that is usually a single sentence, so an unset language becomes English. The
        picker reads this to say so on the card rather than offering a choice the model ignores.
        """
        return self.kind != "whisper"

    @property
    def url(self) -> str:
        return f"{ZOO}/{self.archive}"

    @property
    def languages_shown(self) -> int:
        return self.language_count or len(self.languages)

    def speaks(self, language: str) -> bool:
        """Whether this model claims a language. ``"auto"`` or empty asks nothing of it."""
        if not language or language == "auto":
            return True
        return "*" in self.languages or language in self.languages or language in self.aliases


EUROPEAN_25 = (
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hu", "it",
    "lv", "lt", "mt", "pl", "pt", "ro", "ru", "sk", "sl", "es", "sv", "uk",
)
"""What NVIDIA's multilingual Parakeet and Nemotron models are trained on."""

NEMOTRON_28 = (
    "en", "es", "fr", "it", "pt", "nl", "de", "tr", "ru", "ar", "hi", "ja", "ko", "vi",
    "uk", "pl", "sv", "cs", "nb", "da", "bg", "fi", "hr", "sk", "zh", "hu", "ro", "et",
)
"""The twenty-eight Nemotron 3.5 is trained on — a wider net than the European set, reaching Arabic,
Hindi and the CJK languages, and the reason it is the multilingual default here."""

WHISPER_MAJOR = (
    "en", "ru", "de", "es", "fr", "it", "pt", "nl", "pl", "uk", "tr", "ar",
    "zh", "ja", "ko", "hi", "he", "cs", "sv", "da", "fi", "el", "hu", "ro", "vi", "th", "id",
)
"""The languages worth putting in Whisper's badge row. It claims ninety-nine; these are the ones an
operator is likely to filter by, and ``language_count`` carries the real number."""

MODELS: tuple[SpeechModel, ...] = (
    # -- English, streaming, small enough to be the obvious first choice -----------------------
    SpeechModel(
        id="zipformer-en-small",
        label="Zipformer English (small)",
        archive="sherpa-onnx-streaming-zipformer-en-kroko-2025-08-06.tar.bz2",
        kind="transducer",
        languages=("en",),
        size_bytes=57_267_600,
        unpacked_bytes=60_000_000,
        memory_mb=180,
        licence="Apache-2.0",
        accuracy=78,
        speed=97,
        note="Tiny, instant, English. The one to try first — it costs less than a photograph.",
        sha256="c8676e5ff9ac2a85296e53ee0fd4d5fb1db6770e7a7647166eeafe349ade6834",
    ),
    SpeechModel(
        id="parakeet-unified-en",
        label="Parakeet Unified English 0.6B",
        archive="sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-streaming-560ms.tar.bz2",
        kind="transducer",
        languages=("en",),
        size_bytes=501_360_769,
        unpacked_bytes=520_000_000,
        memory_mb=900,
        licence="CC-BY-4.0",
        accuracy=90,
        speed=79,
        note="Fast and accurate live English. The best English there is that still runs on a CPU.",
        recommended_for=("en",),
        sha256="dd2c2698f102eafbf0ee54bdfd7cd842ec00fa6cf2475cbbb048887f794ff52e",
    ),
    SpeechModel(
        id="moonshine-base-en",
        label="Moonshine Base English",
        archive="sherpa-onnx-moonshine-base-en-int8.tar.bz2",
        kind="moonshine",
        languages=("en",),
        size_bytes=250_807_309,
        unpacked_bytes=260_000_000,
        memory_mb=400,
        licence="MIT",
        accuracy=80,
        speed=99,
        note="English, whole utterances at a time. Very quick, and small enough for a modest machine.",
        contents=(
            "LICENSE", "README.md", "cached_decode.int8.onnx", "encode.int8.onnx",
            "preprocess.onnx", "test_wavs/0.wav", "test_wavs/1.wav", "test_wavs/8k.wav",
            "test_wavs/trans.txt", "tokens.txt", "uncached_decode.int8.onnx",
        ),
    ),
    SpeechModel(
        id="moonshine-tiny-en",
        label="Moonshine Tiny English",
        archive="sherpa-onnx-moonshine-tiny-en-int8.tar.bz2",
        kind="moonshine",
        languages=("en",),
        size_bytes=107_600_538,
        unpacked_bytes=112_000_000,
        memory_mb=220,
        licence="MIT",
        accuracy=74,
        speed=100,
        note="The smallest thing here that still hears English. For a machine with little to spare.",
        sha256="d5fe6ec4334fef36255b2a4010412cad4c007e33103fec62fb5d17cad88086f2",
    ),
    # -- multilingual, streaming --------------------------------------------------------------
    SpeechModel(
        id="nemotron-streaming-multi",
        label="Nemotron 3.5 Streaming 0.6B",
        archive="sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-560ms-int8-2026-06-11.tar.bz2",
        kind="transducer",
        languages=NEMOTRON_28,
        language_count=28,
        size_bytes=475_271_763,
        unpacked_bytes=495_000_000,
        memory_mb=950,
        licence="NVIDIA Open Model Licence",
        accuracy=82,
        speed=84,
        note="Live transcription in twenty-eight languages, Russian among them. The multilingual default.",
        recommended_for=("multi",),
        sha256="c6bf5e0df765f9d5b43bc9e0536d4b4b3e7d40bdf5ecf13e45f134c51c05ae3a",
    ),
    SpeechModel(
        id="fast-conformer-multi",
        label="Fast Conformer (10 languages)",
        archive="sherpa-onnx-nemo-fast-conformer-transducer-be-de-en-es-fr-hr-it-pl-ru-uk-20k-int8.tar.bz2",
        kind="nemo_transducer",
        languages=("be", "de", "en", "es", "fr", "hr", "it", "pl", "ru", "uk"),
        size_bytes=106_546_673,
        unpacked_bytes=112_000_000,
        memory_mb=300,
        licence="CC-BY-4.0",
        accuracy=76,
        speed=92,
        note="Ten European languages including Russian, in a hundred megabytes. A good compromise.",
        sha256="06072bad277f0f4c29cc866d7c62b0e47936da39afafeae453faa925025ccad6",
    ),
    # -- Russian ------------------------------------------------------------------------------
    SpeechModel(
        id="gigaam-ru",
        label="GigaAM v3 Russian",
        archive="sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16.tar.bz2",
        kind="nemo_transducer",
        languages=("ru",),
        size_bytes=170_197_019,
        unpacked_bytes=178_000_000,
        memory_mb=380,
        licence="MIT",
        accuracy=91,
        speed=94,
        note="The best Russian here, and it writes the punctuation itself. Whole utterances, not live.",
        recommended_for=("ru",),
        sha256="f9620a0099019c6afcee26525ef9ed3297fa50dd5691c1902af0c948fc1a470b",
    ),
    SpeechModel(
        id="t-one-ru",
        label="T-one Russian (streaming)",
        archive="sherpa-onnx-streaming-t-one-russian-2025-09-08.tar.bz2",
        kind="t_one_ctc",
        languages=("ru",),
        size_bytes=128_468_156,
        unpacked_bytes=134_000_000,
        memory_mb=320,
        licence="Apache-2.0",
        accuracy=83,
        speed=95,
        note="Russian as it is spoken — words appear while the sentence is still going.",
        sha256="b9c907450e99a6e5049e279bf18368a17db0bdc5e63b7fa978943138debbe3ae",
    ),
    SpeechModel(
        id="zipformer-ru-small",
        label="Zipformer Russian (small, streaming)",
        archive="sherpa-onnx-streaming-zipformer-small-ru-vosk-int8-2025-08-16.tar.bz2",
        kind="transducer",
        languages=("ru",),
        size_bytes=24_110_855,
        unpacked_bytes=26_000_000,
        memory_mb=140,
        licence="Apache-2.0",
        accuracy=72,
        speed=98,
        note="Twenty-four megabytes of live Russian. Rough around the edges, and almost free to run.",
        sha256="6ba68a01ff3c5445aaf2d61e9b97b026f1149dcc9049d11af3f44f55176341d8",
    ),
    # -- Whisper: the widest net, one utterance at a time --------------------------------------
    SpeechModel(
        id="whisper-small",
        label="Whisper Small",
        archive="sherpa-onnx-whisper-small.tar.bz2",
        kind="whisper",
        languages=WHISPER_MAJOR,
        language_count=99,
        size_bytes=639_387_718,
        unpacked_bytes=660_000_000,
        memory_mb=1100,
        licence="MIT",
        accuracy=80,
        speed=78,
        note="Ninety-nine languages, good Russian, and slower than everything above it.",
        contents=(
            "small-decoder.int8.onnx", "small-decoder.onnx", "small-encoder.int8.onnx",
            "small-encoder.onnx", "small-tokens.txt", "test_wavs/0.wav", "test_wavs/1.wav",
            "test_wavs/8k.wav", "test_wavs/trans.txt",
        ),
    ),
    SpeechModel(
        id="whisper-tiny",
        label="Whisper Tiny",
        archive="sherpa-onnx-whisper-tiny.tar.bz2",
        kind="whisper",
        languages=WHISPER_MAJOR,
        language_count=99,
        size_bytes=116_204_861,
        unpacked_bytes=122_000_000,
        memory_mb=260,
        licence="MIT",
        accuracy=61,
        speed=100,
        note="Whisper's whole language list at a size that fits anywhere. It mishears a lot.",
        sha256="c46116994e539aa165266d96b325252728429c12535eb9d8b6a2b10f129e66b1",
    ),
    # -- east Asian ---------------------------------------------------------------------------
    SpeechModel(
        id="sense-voice",
        label="SenseVoice Small",
        archive="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2",
        kind="sense_voice",
        languages=("zh", "en", "ja", "ko"),
        aliases=("yue",),
        size_bytes=1_047_870_769,
        unpacked_bytes=1_080_000_000,
        memory_mb=1200,
        licence="Apache-2.0",
        accuracy=81,
        speed=98,
        note="Chinese, Cantonese, English, Japanese and Korean, and quick about all five.",
        recommended_for=("zh", "ja", "ko"),
        sha256="f6b2a72ebcb1ac7a764d4cfccd886e6bcb2a95c4657c2199d0ba95ed4b9ea71a",
    ),
)

BY_ID: dict[str, SpeechModel] = {m.id: m for m in MODELS}


def get(model_id: str) -> SpeechModel:
    """The entry with this id, or ``KeyError`` naming what is actually on offer."""
    try:
        return BY_ID[model_id]
    except KeyError:
        raise KeyError(f"no speech model {model_id!r}; the catalog has {', '.join(sorted(BY_ID))}") from None


def languages() -> list[str]:
    """Every language any model claims, sorted, for the picker's filter."""
    seen: set[str] = set()
    for model in MODELS:
        seen.update(code for code in model.languages if code != "*")
    return sorted(seen)


def recommended(language: str = "") -> SpeechModel | None:
    """The suggested model for a language: its own recommendation, else the multilingual one."""
    wanted = (language or "").split("-")[0].lower()
    for model in MODELS:
        if wanted and wanted in model.recommended_for:
            return model
    for model in MODELS:
        if "multi" in model.recommended_for and model.speaks(wanted):
            return model
    return None


def as_json(model: SpeechModel) -> dict[str, object]:
    """One entry as the Mini App reads it."""
    return {
        "id": model.id,
        "label": model.label,
        "kind": model.kind,
        "streaming": model.streaming,
        "languages": list(model.languages),
        "language_count": model.languages_shown,
        "size_bytes": model.size_bytes,
        "disk_bytes": model.unpacked_bytes,
        "memory_mb": model.memory_mb,
        "licence": model.licence,
        "accuracy": model.accuracy,
        "speed": model.speed,
        "note": model.note,
        "url": model.url,
        "recommended_for": list(model.recommended_for),
        "verified": model.verified,
        "detects_language": model.detects_language,
    }


def wizard_json() -> str:
    """The catalog as the launcher's setup wizard carries it, byte for byte: ``desktop/speech_models.json``.

    The wizard runs before this program is installed, so it cannot ask for the list; it carries a
    copy, and ``tests/unit/test_speech_setup_model.py`` holds the copy to this table. Only what the
    wizard draws is in it — no archive names or digests, which are this side's business.
    """
    rows = [
        {
            "id": model.id,
            "label": model.label,
            "languages": [code for code in model.languages if code != "*"],
            "language_count": model.languages_shown,
            "size_bytes": model.size_bytes,
            "memory_mb": model.memory_mb,
            "accuracy": model.accuracy,
            "speed": model.speed,
            "recommended_for": list(model.recommended_for),
        }
        for model in MODELS
    ]
    return json.dumps(rows, indent=1, ensure_ascii=False) + "\n"


__all__ = ["BY_ID", "MODELS", "STREAMING_KINDS", "ZOO", "SpeechModel", "as_json", "get", "languages", "recommended", "wizard_json"]
