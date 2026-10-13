"""The local voice as the rest of the application sees it.

One object on the application, holding the voices directory and the loaded synthesiser, with the
questions anyone asks it: is a voice of ours in use, read this aloud, and what should the page be
told. Everything that decides *whether* to speak here decides it in :meth:`LocalTts.available`, so the
precedence — a downloaded voice, then the configured endpoint, then the browser's own synthesiser — is
written once and is the same wherever speech comes out.

The audio leaves as a sequence of clips rather than as one file: :meth:`LocalTts.clips` drives
:meth:`TtsEngine.stream`, so each sentence is encoded and sent the moment it is synthesised and the
page can start playing the first one while the rest is still being made. On a voice that runs four
times faster than speech that is the difference between a pause of half a second and a pause of six.
It is also where cancellation lives: the generator stops at a sentence boundary when the client
disconnects or when the operator talks over the answer, so nothing keeps the synthesiser busy for a
request nobody is listening to any more.

Ogg Opus where ``opusenc`` is present — it is, in the runtime image, for the recognition side's sake —
and WAV otherwise. A sentence is about two hundred kilobytes as WAV and twenty as Opus, which matters
on a phone and not at all on a desktop, so the encoder is used when it is there and never required.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from daedalus.config import RuntimeConfig, TtsConfig
from daedalus.speech import tts_catalog as catalog
from daedalus.speech.models import DownloadError, Downloads
from daedalus.speech.tts_catalog import TtsVoice
from daedalus.speech.tts_engine import CACHE, TtsEngine, TtsError, resolve, wav

logger = logging.getLogger(__name__)

ENCODER = "opusenc"
"""The Opus encoder, from opus-tools — the same 1.3 MB package the recognition side needs ``opusdec``
from, so nothing new enters the image for this."""

ENCODE_TIMEOUT = 60.0
"""An encode that has not finished by now is not going to; the input is one answer read aloud."""

OPUS_BITRATE = "32"
"""Kilobits per second. Speech from a synthesiser is clean mono and does not need more; this is about a
tenth of the WAV and still above what anyone can hear the difference of on a phone speaker."""

WAV_TYPE = "audio/wav"
OPUS_TYPE = "audio/ogg"

SEQUENCE_TYPE = "application/x-speech-sequence"
"""The media type of a spoken answer arriving a sentence at a time.

The body is a run of frames: four bytes of length, big-endian, then that many bytes of one complete
little audio file. Each frame is a sentence and is playable on its own, which is the point — the page
turns each one into a clip and plays it while the next is still being synthesised, and no player has
to be able to resume a half-received file. The media type of the frames themselves is in the
``X-Speech-Media-Type`` header, because it is one answer's worth of the same thing.
"""

MEDIA_TYPE_HEADER = "X-Speech-Media-Type"

LENGTH_BYTES = 4
"""The size of a frame's length prefix. Four bytes is far more than a sentence of Opus ever needs."""


def frame(clip: bytes) -> bytes:
    """One clip as it travels inside a :data:`SEQUENCE_TYPE` body."""
    return len(clip).to_bytes(LENGTH_BYTES, "big") + clip


def check_voice(directory: Path, voice: TtsVoice) -> None:
    """Whether an unpacked archive is a loadable voice, as the download manager asks it.

    Imported through :func:`resolve` rather than loading the model: this runs at the end of every
    download, including on an installation whose engine wheel is not installed yet.
    """
    try:
        resolve(directory, voice.kind)
    except TtsError as exc:
        raise DownloadError(str(exc)) from exc


class LocalTts:
    """The local voices: which is chosen, whether it is here, and what it sounds like.

    Held by the application for its lifetime. Loading is lazy — an installation that never chooses a
    voice never imports the engine — and the loaded voice is dropped whenever the choice changes, so
    the process holds one at a time.
    """

    def __init__(self, state_dir: Path, config: RuntimeConfig) -> None:
        self.downloads = Downloads(state_dir / "models" / "tts", lookup=catalog.get, resolver=check_voice, installed=self._installed)
        self.config = config
        self._speaking = 0
        """Bumped by :meth:`interrupt`. Everything being synthesised stops at its next sentence."""

    # -- what is in use ---------------------------------------------------------------------

    @property
    def settings(self) -> TtsConfig:
        return self.config.companion.tts

    def selected(self) -> TtsVoice | None:
        """The chosen voice, or None. An id that is no longer in the catalog counts as no choice."""
        chosen = self.settings.local_voice
        if not chosen:
            return None
        try:
            return catalog.get(chosen)
        except KeyError:
            logger.warning("configured local voice %r is not in the catalog; ignoring it", chosen)
            return None

    def active(self) -> TtsVoice | None:
        """The chosen voice if it is actually installed — the only case in which anything local speaks."""
        voice = self.selected()
        return voice if voice is not None and self.downloads.is_installed(voice.id) else None

    def available(self) -> bool:
        """Whether an answer would be spoken here rather than sent anywhere."""
        return self.active() is not None

    def speaker(self) -> str:
        return (self.settings.local_speaker or "").strip()

    def speed(self) -> float:
        return self.settings.local_speed

    # -- using it ---------------------------------------------------------------------------

    async def engine(self) -> TtsEngine:
        """The loaded voice, loading it first if this is the first sentence since it was chosen."""
        voice = self.active()
        if voice is None:
            raise TtsError("no voice is installed and selected")
        return await CACHE.get(voice, self.downloads.directory(voice.id), threads=self.settings.local_threads)

    def warm(self) -> dict[str, object]:
        """Start loading the chosen voice now, and answer with where that got to.

        A voice is a second or two of building a synthesiser, and until this existed that second or
        two was paid by the first answer the operator asked for: the words were on the screen and
        nothing was said for as long as the load took, and by the time it spoke the next answer was
        already being written. So the load is started at the three moments it is free — when the voice
        is chosen, when the process starts with one already configured, and when the voice page is
        opened — and the page is told to wait rather than left to discover it.

        Returns at once. Nothing is warmed where no voice is chosen, where it was never downloaded or
        where there is no engine to read it with; the state says so and the page falls back to
        whatever else can speak.
        """
        from daedalus.speech.service import engine_present  # Lazy: one import probe, cached, shared with recognition

        voice = self.active()
        if voice is None or not engine_present():
            return CACHE.state().as_json()
        return CACHE.warm(voice, self.downloads.directory(voice.id), threads=self.settings.local_threads).as_json()

    def load_state(self) -> dict[str, object]:
        """Where the resident voice is in its loading — ``idle``, ``loading``, ``ready`` or ``error``."""
        return CACHE.state().as_json()

    async def speak(self, text: str) -> tuple[bytes, int]:
        """A piece of text as samples and their rate, in the chosen voice."""
        engine = await self.engine()
        return await engine.speak(text, speaker=self.speaker(), speed=self.speed())

    async def audio(self, text: str) -> tuple[bytes, str]:
        """The same, as one whole file a browser plays, and its media type.

        For the short things that are asked for all at once — a sample, a test. The answer the
        operator is waiting to hear goes through :meth:`clips` instead.
        """
        pcm, rate = await self.speak(text)
        if not pcm:
            raise TtsError("there was nothing to say")
        return await encode(pcm, rate)

    def interrupt(self) -> None:
        """Stop reading aloud: the operator talked over the answer, or asked for another one.

        Synchronous and cheap, because the barge-in endpoint should not have to wait for anything.
        What it does is move the mark every running :meth:`clips` compares itself against; each stops
        at the end of the sentence it is in the middle of, which is as fine a grain as a synthesiser
        that renders whole sentences can offer.
        """
        self._speaking += 1

    def _still_wanted(self) -> Callable[[], bool]:
        """A question a synthesis loop can ask between sentences: has anything cancelled me?"""
        mine = self._speaking
        return lambda: self._speaking != mine

    async def clips(self, text: str) -> AsyncIterator[tuple[bytes, str]]:
        """The text read aloud, one playable clip per sentence, each as soon as it exists.

        The caller gets the first clip after one sentence's work rather than after the whole text's,
        and stopping is a matter of not asking for the next one: closing this generator — which is
        what a client going away does — ends the synthesis at that boundary and releases the engine.
        """
        engine = await self.engine()
        rate = engine.sample_rate
        spoke = False
        async for pcm in engine.stream(text, speaker=self.speaker(), speed=self.speed(), cancelled=self._still_wanted()):
            if not pcm:
                continue
            spoke = True
            yield await encode(pcm, rate)
        if not spoke:
            raise TtsError("there was nothing to say")

    async def sample(self, voice_id: str, language: str = "") -> tuple[bytes, str]:
        """One short phrase, so a voice can be heard before it is chosen.

        ``language`` is what the picker is being filtered by — the language the operator is choosing
        a voice *for*, which for a voice that speaks thirty-one of them is not the one it is filed
        under. A voice that does not speak it reads its own.

        Loads the voice being sampled, which means sampling a second voice drops the first — that is
        the same single-resident rule everything else here follows, and hearing two voices at once is
        not something anybody asks for.
        """
        voice = catalog.get(voice_id)
        if not self.downloads.is_installed(voice.id):
            raise TtsError(f"{voice.label} is not downloaded yet")
        engine = await CACHE.get(voice, self.downloads.directory(voice.id), threads=self.settings.local_threads)
        speaker = self.speaker() if voice.id == self.settings.local_voice else ""
        pcm, rate = await engine.speak(voice.sample(language), speaker=speaker, speed=self.speed())
        if not pcm:
            raise TtsError(f"{voice.label} produced no audio for its own sample")
        clip = await encode(pcm, rate)
        self._rewarm_chosen(after=voice.id)
        return clip

    def _rewarm_chosen(self, *, after: str) -> None:
        """Put the voice that speaks back in memory, after another one was auditioned in its place.

        Sampling loads the voice being sampled, and there is only ever one resident: without this the
        operator's next answer pays a second or two to reload the voice they had already chosen, in
        the middle of a sentence they are waiting for. Done in the background, so the sample they
        asked for is not held up by it, and only when the two are actually different.
        """
        chosen = self.active()
        if chosen is None or chosen.id == after:
            return
        self.warm()

    def _installed(self, voice_id: str) -> None:
        """A voice has just finished downloading: if it is the chosen one, the cache starts again.

        A load that failed — on a short archive, on a disk that filled — is not retried while the
        cache still holds that failure for the same voice, and only forgetting it clears that. The
        delete endpoint forgets; downloading again did not, so a voice re-fetched after a bad first
        install stayed "error" until the operator re-selected it.
        """
        if voice_id == self.settings.local_voice:
            self.forget()

    def forget(self) -> None:
        """Drop the loaded voice: the choice changed, or the files were deleted underneath it."""
        CACHE.drop()

    # -- what the page and the doctor show ----------------------------------------------------

    def state(self) -> dict[str, object]:
        """One line about local speech, for the voice page and for ``/api/tts``.

        ``installed`` and ``active`` are different questions and the page needs both: the archive can
        be on the disk while the wheel that reads it is not, which is what a native installation whose
        ``uv sync --extra speech`` failed looks like.
        """
        from daedalus.speech.service import engine_present  # Lazy: one import probe, cached, shared with recognition

        voice = self.selected()
        installed = voice is not None and self.downloads.is_installed(voice.id)
        engine = engine_present()
        load = CACHE.state()
        error = load.error if load.voice == (voice.id if voice else "") else ""
        if voice is None and self.settings.local_voice:
            # A configured id the catalog no longer has. Nothing local speaks, but "idle" would say
            # the operator never chose one, which is the opposite of what happened.
            phase = "error"
            error = f"{self.settings.local_voice} is not in the catalog any more; choose a voice again"
        elif voice is None:
            phase = "idle"
        elif installed and engine:
            phase = load.state if load.voice == voice.id else "idle"
        else:
            phase = "error"
            error = error or ("the speech engine is not installed" if installed else f"{voice.label} was never downloaded")
        return {
            "voice": voice.id if voice else "",
            "label": voice.label if voice else "",
            "language": voice.language if voice else "",
            "speaker": self.speaker(),
            "speed": self.speed(),
            "threads": self.settings.local_threads,
            "installed": installed,
            "active": installed and engine,
            "engine_installed": engine,
            "state": phase,
            "error": error,
            "loaded_in_ms": load.loaded_in_ms if load.voice == (voice.id if voice else "") else 0,
            "loaded": CACHE.loaded(),
            "encoder": encoder_present(),
        }


def encoder_present() -> bool:
    """Whether Opus can be produced here. False only means bigger clips, never no clips."""
    return shutil.which(ENCODER) is not None


async def encode(pcm16: bytes, rate: int) -> tuple[bytes, str]:
    """Samples as a playable file: Ogg Opus where the encoder is here, WAV where it is not.

    An encoder that is present and fails still produces audio — the WAV is already in hand — because a
    silent voice page is a much worse outcome than a clip ten times larger than it needed to be.
    """
    if not encoder_present():
        return wav(pcm16, rate), WAV_TYPE
    try:
        process = await asyncio.create_subprocess_exec(
            ENCODER, "--quiet", "--raw", "--raw-bits", "16", "--raw-rate", str(rate),
            "--raw-chan", "1", "--bitrate", OPUS_BITRATE, "-", "-",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(pcm16), timeout=ENCODE_TIMEOUT)
        except TimeoutError:
            # `wait_for` cancels the wait, not the child: without this the encoder is left running
            # with its pipes open, one per request, and nothing ever reaps it.
            process.kill()
            with contextlib.suppress(ProcessLookupError, OSError):
                await process.wait()
            logger.warning("opusenc did not finish within %.0fs; sending WAV instead", ENCODE_TIMEOUT)
            return wav(pcm16, rate), WAV_TYPE
        if process.returncode or not out:
            logger.warning("opusenc refused the audio (%s); sending WAV instead", err.decode(errors="replace").strip()[:200])
            return wav(pcm16, rate), WAV_TYPE
        return out, OPUS_TYPE
    except OSError as exc:
        logger.warning("opusenc could not be run (%s); sending WAV instead", exc)
        return wav(pcm16, rate), WAV_TYPE


__all__ = [
    "ENCODER",
    "MEDIA_TYPE_HEADER",
    "OPUS_TYPE",
    "SEQUENCE_TYPE",
    "WAV_TYPE",
    "LocalTts",
    "check_voice",
    "encode",
    "encoder_present",
    "frame",
]
