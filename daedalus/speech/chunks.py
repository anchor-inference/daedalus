"""A long recording as pieces a recogniser accepts, and their words put back together.

Neither kind of recogniser takes a voice note of any length. The hosted endpoint the operator uses
answers a request over five minutes, or over about ten megabytes, with a bare HTTP 400 — measured
against it, not read off a page — and a local batch model decodes a whole file in one pass, so ten
minutes of audio is ten minutes of attention in memory at once. So a recording is cut before it is
sent, the pieces are transcribed in order, and their words are joined.

A cut is placed where the speaker paused: the quietest stretch near the end of each window. Cutting
in silence splits no word, so the pieces need no overlap and the joined text no repair. Only when the
whole search stretch is speech — someone talking without a breath for ten seconds — is the cut made
at the window's edge, and then the next piece starts a little earlier so the word that was cut in
half is heard whole by one of them; the join drops the words the two pieces then both heard.

Pure Python over ``array``: numpy is not a dependency of the host, and scanning a few seconds of
samples per cut is cheap enough not to need it.
"""

from __future__ import annotations

import array
import io
import re
import sys
import wave
from dataclasses import dataclass

FRAME_SECONDS = 0.02
"""The unit of loudness a cut is chosen at: short enough to land between two words."""

SEARCH_SECONDS = 10.0
"""How far back from a window's end a pause is looked for. A window is never shorter than four times
this, so a cut always keeps most of the window."""

QUIET_LEVEL = 380
"""Mean loudness in 16-bit sample units below which a frame counts as a pause. The same one per cent of
full scale the live listener's endpointer uses (``engine.SILENCE_RMS``)."""

OVERLAP_SECONDS = 0.6
"""What two pieces share when no pause could be found: longer than a syllable, shorter than a phrase."""

JOIN_WORDS = 8
"""The longest run of words the join will treat as heard twice at a seam."""


@dataclass(frozen=True)
class Piece:
    """One stretch of a recording, and whether it begins inside the stretch before it."""

    pcm16: bytes
    overlaps: bool = False


def _samples(pcm16: bytes) -> array.array:
    samples = array.array("h")
    samples.frombytes(pcm16[: len(pcm16) - len(pcm16) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _quietest(samples: array.array, start: int, stop: int, frame: int) -> tuple[int, int]:
    """The start of the quietest frame in ``[start, stop)``, and its mean loudness."""
    best_at, best_level = stop, sys.maxsize
    at = start
    while at + frame <= stop:
        level = sum(map(abs, samples[at : at + frame])) // frame
        # Ties go to the later frame, so a long pause is cut at its end and the piece keeps its tail.
        if level <= best_level:
            best_at, best_level = at, level
        at += frame
    return best_at, best_level


def split_pcm(pcm16: bytes, sample_rate: int, window_seconds: float) -> list[Piece]:
    """Cut 16-bit little-endian mono samples into pieces of at most ``window_seconds``, in order.

    A recording that fits is returned whole, as one piece, untouched.
    """
    samples = _samples(pcm16)
    window = max(int(window_seconds * sample_rate), 1)
    if len(samples) <= window:
        return [Piece(pcm16)]
    frame = max(int(FRAME_SECONDS * sample_rate), 1)
    search = min(int(SEARCH_SECONDS * sample_rate), window // 4)
    overlap = int(OVERLAP_SECONDS * sample_rate)
    pieces: list[Piece] = []
    begin, overlaps = 0, False
    while len(samples) - begin > window:
        end = begin + window
        at, level = _quietest(samples, end - search, end, frame)
        if level <= QUIET_LEVEL:
            # Cut in the middle of the quiet frame: neither piece ends on a click.
            cut = at + frame // 2
            pieces.append(Piece(_little_endian(samples[begin:cut]), overlaps))
            begin, overlaps = cut, False
        else:
            pieces.append(Piece(_little_endian(samples[begin:end]), overlaps))
            begin, overlaps = end - overlap, True
    pieces.append(Piece(_little_endian(samples[begin:]), overlaps))
    return pieces


def _little_endian(samples: array.array) -> bytes:
    if sys.byteorder == "big":
        samples = array.array("h", samples)
        samples.byteswap()
    return samples.tobytes()


def wav_bytes(pcm16: bytes, sample_rate: int) -> bytes:
    """Samples as a WAV file in memory: the one container every recogniser here reads."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm16)
    return buffer.getvalue()


def _word_key(word: str) -> str:
    return re.sub(r"[^\w]", "", word.casefold())


def join_transcripts(parts: list[str], overlaps: list[bool] | None = None) -> str:
    """The words of consecutive pieces as one text.

    Pieces that heard nothing — a stretch of silence cut out on its own — are skipped. Where a piece
    overlapped the one before (``overlaps[i]``), the tail of one and the head of the next are the same
    words; the longest such run (up to :data:`JOIN_WORDS`, compared without case or punctuation) is
    kept once. A seam cut in a pause is left alone: "that that" said across it was said twice.
    """
    words: list[str] = []
    for index, part in enumerate(parts):
        incoming = part.split()
        if not incoming:
            continue
        shared = 0
        seam = bool(overlaps and index < len(overlaps) and overlaps[index])
        for size in range(min(JOIN_WORDS, len(words), len(incoming)) if seam else 0, 0, -1):
            tail = [_word_key(w) for w in words[-size:]]
            head = [_word_key(w) for w in incoming[:size]]
            if tail == head and any(tail):
                shared = size
                break
        words.extend(incoming[shared:])
    return " ".join(words)


__all__ = ["FRAME_SECONDS", "Piece", "OVERLAP_SECONDS", "QUIET_LEVEL", "SEARCH_SECONDS", "join_transcripts", "split_pcm", "wav_bytes"]
