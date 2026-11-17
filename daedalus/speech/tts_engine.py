"""Loading a downloaded voice and turning words into audio.

Everything in here is CPU-bound and blocking, and none of it may run on the event loop: a voice takes
a second or two to load and a paragraph takes about as long again to speak. The public surface is
therefore async and every call crosses into a worker thread, while the loaded voice is cached —
loading Piper for each sentence would cost more than synthesising it.

Two ways out, because there are two things the answer is wanted for. :meth:`TtsEngine.speak` renders
a whole piece of text and hands back samples. :meth:`TtsEngine.stream` renders it a sentence at a
time and yields each as it is finished, so the first words are audible while the rest is still being
made; on a voice that runs four times faster than speech that is the difference between waiting for
one sentence and waiting for a paragraph.

**Phonemisation ships with the model.** Every Piper archive in the catalog carries its own copy of
``espeak-ng-data`` — about nineteen megabytes of it — and Kokoro and KittenTTS carry the same. So a
Russian voice expands "17" to "семнадцатое" and reads a Latin word inside a Russian sentence in
Russian phonemes with nothing installed in the image and no Python fallback anywhere. The one voice
here that does not work that way, Mandarin, ships a lexicon and three rule FSTs instead, and
:func:`resolve` tells the two apart by what is in the directory rather than by anything in the
catalog — the same rule the recognition side follows for the same reason.

One family does none of that. Supertonic carries no phonemiser at all — it indexes unicode straight
into its text encoder — which is how a hundred and forty megabytes reads thirty-one languages and
expands "17" into words in each of them. It is also the one family that can be *told* where the stress
falls, so a Russian answer going to it goes through :mod:`daedalus.speech.ru_stress` first; see
:data:`OUTPUT_GAIN` for the other thing that family needs, which is to be played louder.

Files are found by shape, not by name (see :mod:`daedalus.speech.tts_catalog`) — except Supertonic's,
which are found by name because it has four of them. :func:`resolve` is the whole of that knowledge,
and it is deliberately small.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import logging
import re
import struct
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from daedalus.speech import ru_stress
from daedalus.speech.tts_catalog import TtsVoice

logger = logging.getLogger(__name__)

MIN_SPEED = 0.5
MAX_SPEED = 2.0
"""What ``speed`` may be. Below a half is a recording slowed to unintelligibility and above two is a
sound rather than speech; both ends are what a stuck slider or a bad client sends, not a preference."""

MAX_TEXT_CHARS = 4000
"""One synthesis request. Longer than this is a document being read aloud, which is not what a spoken
conversation is, and it is also a minute of audio held in memory at once."""

SENTENCE_END = re.compile(r"(?<=[.!?…。！？])\s+|\n{2,}")
"""Where one spoken chunk may end. Deliberately blunt: a wrong split costs a breath in the wrong place
and a missed one costs nothing at all, so there is no abbreviation table here to get wrong in two
languages."""

MIN_SENTENCE_CHARS = 12
"""A fragment shorter than this is joined to the next rather than synthesised alone. "Yes." on its own
is a separate model call, a separate audio clip and an audible gap, for three characters of speech."""

MAX_SENTENCE_CHARS = 400
"""A "sentence" longer than this is split on the nearest comma or space. Something without full stops
— a list, a pasted line — would otherwise be one long synthesis and defeat the whole streaming path."""

OPENING_CHARS = 60
"""How much of the first chunk is worth waiting for before any sound comes out.

Everything after the first chunk is made while the one before it is being played, so its length costs
the listener nothing; the first one is the whole of the silence between asking and hearing. On a voice
that renders at a quarter of real time an opening of sixty characters is about half a second, and a
sentence of ninety is about a second — which is the difference between an answer that begins and a
page that appears to have missed the question. The cut is only ever taken at a clause break that is
already in the text: a fragment ending nowhere in particular is read as if it ended, and that is worse
than the wait."""

RULE_FSTS = ("phone.fst", "date.fst", "number.fst")
"""The text-normalisation rules a lexicon-phonemised voice ships, in the order sherpa wants them: how a
character is pronounced, then dates, then numbers."""

SUPERTONIC_GRAPHS = ("text_encoder", "duration_predictor", "vector_estimator", "vocoder")
"""The four ONNX files a Supertonic archive is made of, each under its own name.

Every other family here is one graph and a tokens file. This one is a text encoder, a duration
predictor, a vector estimator and a vocoder, and it has no tokens file at all: it indexes unicode
directly, which is why it reads thirty-one languages without an espeak-ng copy or a lexicon."""

SUPERTONIC_DATA = (("tts_json", "tts.json"), ("unicode_indexer", "unicode_indexer.bin"), ("voices", "voice.bin"))
"""The three data files beside those graphs: the architecture, the character index, and the style
vectors of the ten preset voices."""

OUTPUT_GAIN: dict[str, float] = {"supertonic": 0.85}
"""What a family's output is multiplied by so that two voices can be compared by ear and not by level.

A voice that is a decibel or two louder than the one beside it wins an A/B for a reason that has
nothing to do with how it sounds, so the level is levelled here rather than left to the listener.
0.85 is measured, not assumed: on the same four sentences through this engine, Supertonic renders at
RMS 0.055 and Piper's Russian at 0.047, and 0.85 is what puts the first onto the second. It is applied
to the samples before they are quantised, and the clamp in :func:`to_pcm16` is what would keep a
gained peak from wrapping round into a click. A family not named here is played as it was rendered."""


class TtsError(RuntimeError):
    """Anything that stops audio coming out: the wheel, the files, or the voice itself."""


def require_sherpa() -> Any:
    """The engine module, or a refusal that says how to get it."""
    try:
        import sherpa_onnx  # Lazy: the engine is an optional extra, absent until the operator downloads a voice
    except ImportError as exc:  # pragma: no cover - exercised only where the extra is absent
        raise TtsError(
            "speaking here needs the speech extra: run `uv sync --extra speech` in the runtime, "
            "or configure a speech endpoint instead (Settings → Voice mode)"
        ) from exc
    return sherpa_onnx


# -- what a voice is made of ------------------------------------------------------------------


@dataclass(frozen=True)
class Files:
    """The files one voice is loaded from, found inside an unpacked archive.

    A family brings the files it has and leaves the rest empty: a Piper voice is one graph and a tokens
    file, and a Supertonic voice is four graphs, two data files and no tokens file anywhere. Nothing
    here is required of every family, which is why every field has a default — what a family does
    require is checked in :func:`resolve`, where the archive is in front of it.
    """

    model: Path | None = None
    tokens: Path | None = None
    data_dir: Path | None = None
    """``espeak-ng-data``, where the voice phonemises through espeak-ng — which is all but one of them."""
    lexicon: Path | None = None
    """``lexicon.txt``, where it does not."""
    voices: Path | None = None
    """The style vectors of a multi-speaker archive: ``voices.bin`` for Kokoro and KittenTTS,
    ``voice.bin`` for Supertonic's ten preset styles."""
    rule_fsts: tuple[Path, ...] = ()
    text_encoder: Path | None = None
    duration_predictor: Path | None = None
    vector_estimator: Path | None = None
    vocoder: Path | None = None
    """Supertonic's four graphs. Empty for every other family."""
    tts_json: Path | None = None
    """``tts.json``: what Supertonic's four graphs are wired into."""
    unicode_indexer: Path | None = None
    """``unicode_indexer.bin``: Supertonic's whole phonemiser, in a quarter of a megabyte."""


def _model_file(directory: Path) -> Path:
    """The one ONNX in the directory, preferring the quantised build where an archive ships both."""
    found = sorted(directory.glob("*.onnx"))
    if not found:
        raise TtsError(f"{directory.name} has no model file — the download is incomplete")
    quantised = [p for p in found if ".int8." in p.name or ".quant." in p.name]
    return (quantised or found)[0]


def _graph(directory: Path, stem: str) -> Path:
    """One named graph, preferring the quantised build where an archive ships both of it.

    The same preference :func:`_model_file` makes, per file rather than per directory: a Supertonic
    archive holds four graphs and may hold either build of each.
    """
    for name in (f"{stem}.int8.onnx", f"{stem}.onnx"):
        found = directory / name
        if found.is_file():
            return found
    raise TtsError(f"{directory.name} has no {stem}.onnx — the download is incomplete")


def _supertonic(directory: Path) -> Files:
    """A Supertonic archive: four graphs and three data files, every one of them required.

    There is no tokens file and no phonemiser directory to find, so unlike every other family nothing
    here is discovered by shape — the whole archive is named, and a missing name is a short download.
    """
    graphs = {stem: _graph(directory, stem) for stem in SUPERTONIC_GRAPHS}
    data: dict[str, Path] = {}
    for field, name in SUPERTONIC_DATA:
        found = directory / name
        if not found.is_file():
            raise TtsError(f"{directory.name} has no {name} — the download is incomplete")
        data[field] = found
    return Files(**graphs, **data)


def resolve(directory: Path, kind: str) -> Files:
    """What to load a voice from, discovered inside the unpacked archive.

    This is also the integrity check a download gets after it is unpacked: an archive that arrived
    short raises here rather than at the first sentence somebody asks to hear.
    """
    if kind == "supertonic":
        # Before the model file and before the tokens check, because this family has neither in the
        # shape the rest of them do.
        return _supertonic(directory)
    model = _model_file(directory)
    tokens = directory / "tokens.txt"
    if not tokens.is_file():
        raise TtsError(f"{directory.name} has no tokens file — the download is incomplete")
    espeak = directory / "espeak-ng-data"
    lexicon = directory / "lexicon.txt"
    if kind in ("kokoro", "kitten"):
        voices = directory / "voices.bin"
        if not voices.is_file():
            raise TtsError(f"{directory.name} has no voices.bin — the download is incomplete")
        if not espeak.is_dir():
            raise TtsError(f"{directory.name} has no espeak-ng-data — the download is incomplete")
        return Files(model=model, tokens=tokens, data_dir=espeak, voices=voices)
    if kind != "vits":
        raise TtsError(f"{directory.name} is a {kind} voice, which this engine does not load")
    if espeak.is_dir():
        return Files(model=model, tokens=tokens, data_dir=espeak)
    if lexicon.is_file():
        rules = tuple(directory / name for name in RULE_FSTS if (directory / name).is_file())
        return Files(model=model, tokens=tokens, lexicon=lexicon, rule_fsts=rules)
    raise TtsError(
        f"{directory.name} has neither espeak-ng-data nor a lexicon, so nothing here can turn text "
        "into phonemes for it — the download is incomplete"
    )


def _config(voice: TtsVoice, files: Files, threads: int) -> Any:
    """sherpa's configuration for one voice. The only place that knows the four families apart."""
    sherpa = require_sherpa()
    model = sherpa.OfflineTtsModelConfig(num_threads=max(1, threads), provider="cpu")
    if voice.kind == "supertonic":
        model.supertonic = sherpa.OfflineTtsSupertonicModelConfig(
            text_encoder=str(files.text_encoder), duration_predictor=str(files.duration_predictor),
            vector_estimator=str(files.vector_estimator), vocoder=str(files.vocoder),
            tts_json=str(files.tts_json), unicode_indexer=str(files.unicode_indexer),
            voice_style=str(files.voices),
        )
    elif voice.kind == "kokoro":
        model.kokoro = sherpa.OfflineTtsKokoroModelConfig(
            model=str(files.model), voices=str(files.voices), tokens=str(files.tokens),
            data_dir=str(files.data_dir),
        )
    elif voice.kind == "kitten":
        model.kitten = sherpa.OfflineTtsKittenModelConfig(
            model=str(files.model), voices=str(files.voices), tokens=str(files.tokens),
            data_dir=str(files.data_dir),
        )
    elif files.data_dir is not None:
        model.vits = sherpa.OfflineTtsVitsModelConfig(
            model=str(files.model), tokens=str(files.tokens), data_dir=str(files.data_dir),
        )
    else:
        model.vits = sherpa.OfflineTtsVitsModelConfig(
            model=str(files.model), tokens=str(files.tokens), lexicon=str(files.lexicon),
        )
    return sherpa.OfflineTtsConfig(
        model=model,
        rule_fsts=",".join(str(p) for p in files.rule_fsts),
        # One sentence per call. The splitting is done here instead, because this engine has to know
        # where the boundaries are anyway to stream, and two different ideas of where a sentence ends
        # would mean audio arriving in chunks nobody asked for.
        max_num_sentences=1,
    )


# -- the text ---------------------------------------------------------------------------------

_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]*)\)")
_BARE_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s{0,4}[-*+]\s+", re.M)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)")
_SPACE = re.compile(r"[ \t]+")


def spoken(text: str) -> str:
    """Text as it should be read aloud: no markdown, no code blocks, no addresses.

    The concierge already writes in spoken sentences, so this is a guard rather than a rewriter — what
    it removes is the punctuation a synthesiser would otherwise pronounce or stumble over. A fenced
    block becomes the phrase "code block" because reading a diff aloud is worse than saying there was
    one, and a link becomes its own text because a URL read character by character is unbearable.
    """
    body = _FENCE.sub(" code block. ", text)
    body = _INLINE_CODE.sub(r"\1", body)
    body = _LINK.sub(r"\1", body)
    body = _BARE_URL.sub("a link", body)
    body = _HEADING.sub("", body)
    body = _BULLET.sub("", body)
    body = _EMPHASIS.sub("", body)
    body = _SPACE.sub(" ", body)
    return "\n".join(line.strip() for line in body.splitlines()).strip()


def _cut_long(part: str) -> list[str]:
    """Break something with no full stop in it on a comma, then on a space, then not at all."""
    if len(part) <= MAX_SENTENCE_CHARS:
        return [part]
    out: list[str] = []
    rest = part
    while len(rest) > MAX_SENTENCE_CHARS:
        window = rest[:MAX_SENTENCE_CHARS]
        cut = max(window.rfind(", "), window.rfind("; "), window.rfind(" — "))
        if cut < MIN_SENTENCE_CHARS:
            cut = window.rfind(" ")
        if cut < MIN_SENTENCE_CHARS:
            cut = MAX_SENTENCE_CHARS
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return out


def _open_sooner(first: str) -> list[str]:
    """The opening chunk, cut at a clause break if that makes the first sound arrive sooner.

    One cut, at the last comma, semicolon or dash that leaves a fragment worth speaking and still
    inside :data:`OPENING_CHARS`. Where the sentence has no such break — and many short ones do not —
    it is left whole rather than broken somewhere a reader would hear as a mistake.
    """
    if len(first) <= OPENING_CHARS:
        return [first]
    window = first[:OPENING_CHARS]
    cut = max(window.rfind(", "), window.rfind("; "), window.rfind(" — "), window.rfind(": "))
    if cut < MIN_SENTENCE_CHARS:
        return [first]
    head, tail = first[:cut + 1].strip(), first[cut + 1:].strip()
    return [head, tail] if tail else [first]


def sentences(text: str) -> list[str]:
    """The text as the chunks it will be spoken in, in order.

    A short fragment is carried into the next chunk rather than spoken alone: every chunk is a model
    call and a clip boundary, and "Yes." as its own clip is an audible stutter for three characters.
    The first chunk is the exception and is made *shorter* where the text allows it, because it is the
    only one the listener waits through in silence.
    """
    parts: list[str] = []
    for raw in SENTENCE_END.split(text):
        piece = raw.strip()
        if piece:
            parts.extend(_cut_long(piece))
    out: list[str] = []
    for piece in parts:
        if out and len(out[-1]) < MIN_SENTENCE_CHARS:
            out[-1] = f"{out[-1]} {piece}"
        else:
            out.append(piece)
    return _open_sooner(out[0]) + out[1:] if out else out


# -- audio ------------------------------------------------------------------------------------


def to_pcm16(samples: Any, gain: float = 1.0) -> bytes:
    """sherpa's floats as the 16-bit samples everything downstream speaks in.

    Clamped rather than scaled: a synthesiser occasionally puts a sample just past full scale, and
    normalising the whole clip to it would make one loud consonant quieten the sentence around it.

    ``gain`` is the family's own level put beside everyone else's (see :data:`OUTPUT_GAIN`) and is a
    constant per family rather than anything measured on the clip, for the same reason: a per-clip
    normalisation makes a quiet sentence and a loud one come out at the same level, which is a
    different voice, not a fairer one.
    """
    out = array.array("h", (max(-32768, min(32767, int(value * gain * 32767))) for value in samples))
    if struct.pack("=h", 1) != b"\x01\x00":  # pragma: no cover - no big-endian machine runs this
        out.byteswap()
    return out.tobytes()


def wav(pcm16: bytes, rate: int) -> bytes:
    """The samples as a WAV file, header and all. Mono, 16-bit, at the voice's own rate."""
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm16)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16
    ) + b"data" + struct.pack("<I", len(pcm16))
    return header + pcm16


class TtsEngine:
    """A loaded voice, shared by everything that wants audio out of words.

    One synthesiser serves every caller and the lock is what makes that safe: sherpa's is not
    re-entrant, so requests are serialised. They are short — a sentence at a time — so queueing behind
    one costs less than holding a second copy of the model.
    """

    def __init__(self, voice: TtsVoice, directory: Path, *, threads: int = 2) -> None:
        self.voice = voice
        self.directory = directory
        self.threads = threads
        self.lock = threading.Lock()
        sherpa = require_sherpa()
        self.files = resolve(directory, voice.kind)
        self.tts = sherpa.OfflineTts(_config(voice, self.files, threads))

    @property
    def sample_rate(self) -> int:
        """What it actually synthesises at, read back from the loaded model rather than the catalog."""
        return int(self.tts.sample_rate)

    @property
    def num_speakers(self) -> int:
        return int(self.tts.num_speakers)

    def speaker_id(self, speaker: str | int) -> int:
        """A speaker as the model numbers it, from a name, an index, or nothing.

        Out of range becomes the first speaker rather than an error: a voice the operator picked before
        the catalog changed under them should still talk.
        """
        if isinstance(speaker, int):
            return speaker if 0 <= speaker < self.num_speakers else 0
        name = (speaker or "").strip()
        if not name:
            return 0
        if name in self.voice.speakers:
            index = self.voice.speakers.index(name)
            return index if index < self.num_speakers else 0
        if name.isdigit():
            index = int(name)
            return index if index < self.num_speakers else 0
        return 0

    def render(self, text: str, *, speaker: str | int = "", speed: float = 1.0) -> bytes:
        """One chunk of text as samples. Blocking; callers hold the lock."""
        body = text.strip()
        if not body:
            return b""
        rate = max(MIN_SPEED, min(MAX_SPEED, speed))
        if ru_stress.applies(self.voice):
            body = ru_stress.mark(body)
        audio = self.tts.generate(body, sid=self.speaker_id(speaker), speed=rate)
        return to_pcm16(audio.samples, OUTPUT_GAIN.get(self.voice.kind, 1.0))

    def pieces(self, body: str, *, speaker: str | int = "", speed: float = 1.0) -> Iterator[bytes]:
        """The text as samples, a sentence at a time, in order. Blocking; holds the lock per sentence.

        ``body`` is text :func:`spoken` has already been through — the caller has it in hand and
        applying it twice is work for nothing and a trap the moment a rule in there stops being
        idempotent.

        The lock is taken and released per sentence rather than held across the whole text, so a long
        answer being read aloud does not block the sample the operator just asked to hear.
        """
        for piece in sentences(body):
            with self.lock:
                chunk = self.render(piece, speaker=speaker, speed=speed)
            if chunk:
                yield chunk

    async def speak(self, text: str, *, speaker: str | int = "", speed: float = 1.0) -> tuple[bytes, int]:
        """A whole piece of text as samples and the rate they are at."""
        body = spoken(text)
        if len(body) > MAX_TEXT_CHARS:
            raise TtsError(f"this is {len(body)} characters to read aloud; {MAX_TEXT_CHARS} is the most at once")

        def run() -> bytes:
            return b"".join(self.pieces(body, speaker=speaker, speed=speed))

        return await asyncio.to_thread(run), self.sample_rate

    async def stream(
        self,
        text: str,
        *,
        speaker: str | int = "",
        speed: float = 1.0,
        cancelled: Callable[[], bool] | None = None,
    ) -> AsyncIterator[bytes]:
        """The same audio, yielded a sentence at a time as each is finished.

        Each sentence is synthesised on a worker thread and handed over before the next is begun, so
        the caller has the first words after one sentence's work rather than after the whole text's.

        ``cancelled`` is asked at every sentence boundary, and the generator being closed — which is
        what a client disconnecting does to it — stops it at the same place. Either way the work that
        is abandoned is one sentence's, and the lock is never held across the gap: a request nobody is
        listening to any more cannot keep the next one waiting.
        """
        body = spoken(text)
        if len(body) > MAX_TEXT_CHARS:
            raise TtsError(f"this is {len(body)} characters to read aloud; {MAX_TEXT_CHARS} is the most at once")
        for piece in sentences(body):
            if cancelled is not None and cancelled():
                return

            def run(part: str = piece) -> bytes:
                with self.lock:
                    return self.render(part, speaker=speaker, speed=speed)

            chunk = await asyncio.to_thread(run)
            if cancelled is not None and cancelled():
                return
            if chunk:
                yield chunk


@dataclass
class TtsLoadState:
    """Where the resident voice is in its loading, as the page draws it.

    The same four words the recognition side uses for its models, and deliberately so: ``idle`` is
    nothing chosen or nothing loaded yet, ``loading`` is the synthesiser being built, ``ready`` is a
    voice that will speak the next sentence immediately, and ``error`` is a load that failed with the
    reason. The page needs the difference because the first answer after a restart waits seconds on a
    cold voice and none at all on a warm one, and an answer written on the screen and not spoken for
    fifteen seconds is indistinguishable from one that will never be spoken.
    """

    state: str = "idle"
    voice: str = ""
    loaded_in_ms: int = 0
    error: str = ""

    def as_json(self) -> dict[str, object]:
        return {"state": self.state, "voice": self.voice, "loaded_in_ms": self.loaded_in_ms, "error": self.error}


class TtsCache:
    """The one loaded voice, kept between sentences and swapped when the operator picks another.

    Loading is slow and happens under a lock, so two sentences arriving together load once and both
    wait. Choosing another voice drops the old one; there is never more than one resident.

    Nothing here unloads a voice that has gone quiet. A Piper voice costs about thirty-five megabytes
    of memory and a second and a half to build, and the operator who is going to say something else
    is the operator who just said something: paying that load again to save the memory between two
    utterances is the trade nobody wants. The voice is let go when it is replaced, deleted or
    deselected, and at no other time.

    Because loading is slow it is also *watchable*: :meth:`warm` starts it with no sentence behind it,
    and every state it passes through is published to :meth:`watch`, so the page can say "loading the
    voice" with something moving rather than leaving the operator looking at an answer nobody reads.
    """

    def __init__(self) -> None:
        self._engine: TtsEngine | None = None
        self._key: tuple[str, int] | None = None
        self._lock = asyncio.Lock()
        self._fields = threading.Lock()
        self._generation = 0
        """Bumped by every :meth:`drop`. A load that finishes after one publishes nothing."""
        self._state = TtsLoadState()
        self._warming: asyncio.Task[Any] | None = None
        self._watchers: list[asyncio.Queue[dict[str, object]]] = []

    async def get(self, voice: TtsVoice, directory: Path, *, threads: int) -> TtsEngine:
        key = (voice.id, threads)
        async with self._lock:
            with self._fields:
                if self._engine is not None and self._key == key:
                    return self._engine
                self._engine = None
                self._key = None
                mine = self._generation
            logger.warning("loading local voice %s (%d threads)", voice.id, threads)
            self._publish(TtsLoadState(state="loading", voice=voice.id))
            began = time.monotonic()
            try:
                engine = await asyncio.to_thread(TtsEngine, voice, directory, threads=threads)
            except Exception as exc:
                self._publish(TtsLoadState(state="error", voice=voice.id, error=str(exc)))
                raise
            took = int((time.monotonic() - began) * 1000)
            with self._fields:
                if self._generation != mine:
                    # The voice was deleted or swapped while this was loading. The caller still gets
                    # what it asked for — the request is already in flight and a half-spoken answer
                    # helps nobody — but nothing resident is left behind pointing at a voice the
                    # operator has since said they do not want.
                    logger.warning("local voice %s finished loading after it was dropped; not keeping it", voice.id)
                    return engine
                self._engine, self._key = engine, key
            logger.warning("local voice %s loaded in %d ms", voice.id, took)
            self._publish(TtsLoadState(state="ready", voice=voice.id, loaded_in_ms=took))
            return engine

    def warm(self, voice: TtsVoice, directory: Path, *, threads: int) -> TtsLoadState:
        """Start loading with nothing waiting on it, and answer with where that got to.

        Called when a voice is chosen, when the process starts with one already configured, and when
        the voice page is opened — the three moments the voice is about to be needed and the only ones
        at which two seconds of loading cost nobody anything. Returns at once: a second call while the
        first is still running is the same load, not another one.
        """
        if self._engine is not None and self._key is not None and self._key == (voice.id, threads):
            return self.state()
        if self._warming is not None and not self._warming.done():
            return self.state()
        if self._state.state == "error" and self._state.voice == voice.id:
            # A load that failed fails the same way every time, and the voice page asks on every poll.
            # The failure stands until something changes the selection, which drops the cache and with
            # it this state.
            return self.state()
        self._publish(TtsLoadState(state="loading", voice=voice.id))

        async def load() -> None:
            try:
                await self.get(voice, directory, threads=threads)
            except Exception as exc:  # noqa: BLE001 - a warm-up failure is reported, never raised at a caller that did not ask
                logger.warning("warming the local voice %s failed: %s", voice.id, exc)

        self._warming = asyncio.ensure_future(load())
        return self.state()

    def state(self) -> TtsLoadState:
        """Where the resident voice is. Cheap enough to answer on every poll of the voice page."""
        return self._state

    def loaded(self) -> str:
        """The id of the resident voice, or empty. For the doctor line and the settings page."""
        with self._fields:
            return self._engine.voice.id if self._engine is not None else ""

    def drop(self) -> None:
        """Let go of the voice — the operator deleted it or chose another.

        Synchronous, because it is called from ``save_config`` and from the delete endpoint, neither of
        which should have to be async for this. A drop cannot wait for a load it arrives in the middle
        of — the whole point of it is to be answerable now — so it marks the generation instead, and a
        loader that comes back into a newer generation hands its engine to its own caller and leaves
        the cache empty rather than writing over the drop.
        """
        with self._fields:
            self._generation += 1
            self._engine = None
            self._key = None
        if self._warming is not None and not self._warming.done():
            # A load in flight is a load of the voice that was just let go of. The thread building it
            # cannot be stopped — so for the second or two it takes to finish, two voices are
            # resident at once, which on a small host is worth knowing: Kokoro beside Supertonic is
            # the worst of it — and what it produces is discarded by the generation above; what must
            # not survive is the *guard*, or the next warm-up — of the voice the operator has just
            # chosen — would see a load already running and decline to start one, and the new voice
            # would be built by the first answer after all.
            self._warming.cancel()
            self._warming = None
        self._publish(TtsLoadState())

    # -- watching a load ----------------------------------------------------------------------

    def _publish(self, state: TtsLoadState) -> None:
        # Only changes go out, for the same reason the recognition cache publishes only changes:
        # `warm` says "loading" when it starts the task and `get` says it again when the load really
        # begins, which is one fact twice, and a page that has to de-duplicate a stream for itself is
        # a page that will de-duplicate it wrongly.
        if (state.state, state.voice, state.loaded_in_ms, state.error) == (self._state.state, self._state.voice, self._state.loaded_in_ms, self._state.error):
            return
        self._state = state
        for queue in list(self._watchers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(state.as_json())

    @contextlib.asynccontextmanager
    async def watch(self) -> AsyncIterator[dict[str, object]]:
        """A queue of load states for as long as the caller holds it.

        Bounded and dropping rather than blocking: a page that stopped reading must not be able to
        hold up a load everything else is waiting on.
        """
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=16)
        self._watchers.append(queue)
        try:
            yield queue
        finally:
            with contextlib.suppress(ValueError):
                self._watchers.remove(queue)


CACHE = TtsCache()

__all__ = [
    "CACHE",
    "MAX_SPEED",
    "MAX_TEXT_CHARS",
    "MIN_SPEED",
    "OPENING_CHARS",
    "OUTPUT_GAIN",
    "Files",
    "TtsCache",
    "TtsLoadState",
    "TtsEngine",
    "TtsError",
    "require_sherpa",
    "resolve",
    "sentences",
    "spoken",
    "to_pcm16",
    "wav",
]
