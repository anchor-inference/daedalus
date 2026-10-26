"""Pictures made to fit what the provider of a request accepts and keeps, before they are encoded.

A picture went on the wire as the bytes it was stored with. A phone or a large monitor makes
screenshots of five to nine megabytes, and Anthropic refuses a request outright when one picture is
past its per-image limit (``image exceeds 10 MB maximum: 11804632 bytes``); the picture then stays in
the history, so every following request of the session, a plain "continue" included, was refused
the same way. Larger pixels were never worth sending in the first place: every provider scales a
picture down on its side to a size it keeps, so the bytes past that size cost upload and nothing else.

So each picture is fitted to the request's provider, the way Claude Code fits a pasted screenshot:
scaled down to the size the provider keeps, kept as PNG when that still fits and is not much larger
than a JPEG of it (text on a screenshot stays sharp), else re-encoded as JPEG at falling quality, and
scaled down further only if even that is too large. A picture already within the bounds goes out as the very bytes it was stored with.

The fitted bytes are kept on disk, under the original's SHA-256 and the bounds they were made for.
A provider's prompt cache serves a request only up to the first byte that differs, and an image is
re-sent on every request of a session: fitting it again after an upgrade of the imaging library could
give other bytes and cost the cache everything after it. Read back, the same picture is the same
bytes for as long as the file is there. The original is never touched: the copy in the session's
inbox, and the blob, stay at full resolution for ImageView to crop.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import math
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

FIT_VERSION = 1
"""Part of every kept file's name: a change to how pictures are fitted makes new files, so a session's
pictures change once, on purpose, rather than drift with whatever the code computes today."""

MAX_ENCODED_BYTES = 5 * 1024 * 1024
"""The most one picture may take on the wire, counted as its base64 text, which is what providers
measure. Anthropic's API and the subscription route take 10 MB, Amazon Bedrock (which OpenRouter may
route a Claude model to) 5 MB, OpenAI states no bound; Claude Code holds every picture to 5 MB. The
smallest of them is the one bound, so a session can move between providers and a picture fitted for
one is never refused by another."""


@dataclass(frozen=True, slots=True)
class ImageLimits:
    """The largest picture worth sending to one family of models.

    ``max_edge`` bounds the long side. ``max_patches`` bounds ``ceil(w / patch) × ceil(h / patch)``,
    the squares the provider cuts a picture into and prices it by (0 for no bound).
    ``max_encoded_bytes`` bounds the picture's base64 text."""

    family: str
    max_edge: int
    max_patches: int = 0
    patch: int = 28
    max_encoded_bytes: int = MAX_ENCODED_BYTES

    def key(self) -> str:
        return f"v{FIT_VERSION}-{self.family}-{self.max_edge}-{self.max_patches}x{self.patch}-{self.max_encoded_bytes}"


CLAUDE_HIGH_LIMITS = ImageLimits("claude-high", 2576, max_patches=4784)
"""Claude 4.7 and later: Anthropic keeps a picture whose long side is at most 2576 pixels and that
covers at most 4784 squares of 28 pixels, and scales anything larger down to the largest size within
both (3840×2160 becomes 2576×1449). Claude Code knows the same pair for these models. Past twenty
pictures in one request Anthropic refuses any side over 2000 pixels; a request here never carries more
than twenty (see ``providers.wire.MAX_REQUEST_IMAGES``)."""
CLAUDE_LIMITS = ImageLimits("claude", 1568, max_patches=1568)
"""Every earlier Claude model: a long side of 1568 pixels and 1568 squares (1920×1080 becomes 1456×819).
The computer-use profile of these models sends screenshots within this already, so none is scaled
again and no point the model reads moves."""
OPENAI_LIMITS = ImageLimits("openai", 2048, max_patches=2500, patch=32)
"""OpenAI's patch models at high detail (what ``auto`` picks for a large picture) keep a 2048-pixel
side and 2500 squares of 32 pixels. The older tiled models fit a 2048 square and then scale the short
side to 768; that second step is not taken here, since it would shrink the 1440×900 screenshot the
computer-use models are meant to be shown at its own size."""
DEFAULT_LIMITS = ImageLimits("default", 2048)
"""Any other model: Grok, Gemini, a local llama.cpp or vLLM server. None is known to keep more than a
2048-pixel side for a single picture, and a local processor given a larger one only spends longer
resizing it."""

_FAMILIES: tuple[tuple[re.Pattern[str], ImageLimits], ...] = (
    # The same reading of a Claude id as ``model_capabilities``: 4.7 and on, every 5.x and later, and
    # the fable and mythos lines.
    (re.compile(r"claude-(?:opus|sonnet|haiku)-(?:4[-.](?:[7-9]|[1-9]\d)(?!\d)|[5-9](?!\d)|[1-9]\d(?!\d))"), CLAUDE_HIGH_LIMITS),
    (re.compile(r"claude-(?:fable|mythos)\b"), CLAUDE_HIGH_LIMITS),
    (re.compile(r"claude|anthropic/"), CLAUDE_LIMITS),
    (re.compile(r"^(?:gpt-|o[1-9](?:-|$)|codex|chatgpt)|openai/"), OPENAI_LIMITS),
)


def limits_for(model: str) -> ImageLimits:
    """The bounds a picture sent to ``model`` is fitted to, read off the model id.

    The id, not the endpoint: every endpoint is OpenAI-shaped, and one of them (OpenRouter, the key
    proxy) reaches several vendors. A vendor prefix (``anthropic/claude-…``) is allowed."""
    name = str(model or "").strip().lower()
    for pattern, limits in _FAMILIES:
        if pattern.search(name):
            return limits
    return DEFAULT_LIMITS


def encoded_size(data: bytes) -> int:
    """The length of ``data`` as base64, which is what a request carries and a provider counts."""
    return 4 * ((len(data) + 2) // 3)


_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniffed_mime(data: bytes, claimed: str) -> str:
    """The type the picture's own bytes declare, else the one it was stored under.

    Anthropic checks the bytes against the declared type and refuses a mismatch with a 400: a JPEG
    an image tool had saved as ``24.png`` failed on every vision model in turn."""
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return claimed


@dataclass(frozen=True, slots=True)
class FittedImage:
    """A picture as it goes on the wire. ``width``/``height`` are its size when it could be read (0 when
    not); ``original`` is the stored picture's size when the two differ, else ``None``. ``fits`` is
    false only for a picture that could not be brought under the byte bound (one that cannot be
    decoded): it must not be sent. ``changed`` says the bytes are not the stored ones."""

    data: bytes
    mime: str
    width: int = 0
    height: int = 0
    original: tuple[int, int] | None = None
    fits: bool = True

    @property
    def changed(self) -> bool:
        return self.original is not None


def target_size(width: int, height: int, limits: ImageLimits) -> tuple[int, int]:
    """The size a ``width``×``height`` picture is scaled to: the largest of its aspect within every
    bound, never larger than it is. The provider's own rule, so the picture sent is the one it keeps."""

    long_side = max(width, height)

    def scaled(new_long: int) -> tuple[int, int]:
        # Scaled by the long side, the short one rounded half up, as Anthropic's examples come out
        # (1075×1520 is kept at 924×1307).
        if new_long >= long_side:
            return width, height
        short = max(1, math.floor(min(width, height) * new_long / long_side + 0.5))
        return (new_long, short) if width >= height else (short, new_long)

    def within(size: tuple[int, int]) -> bool:
        if max(size) > limits.max_edge:
            return False
        if limits.max_patches:
            return math.ceil(size[0] / limits.patch) * math.ceil(size[1] / limits.patch) <= limits.max_patches
        return True

    if within((width, height)):
        return width, height
    # The longest long side whose size is within: every bound only gets easier as it falls.
    low, high = 1, long_side
    while low < high:
        middle = (low + high + 1) // 2
        if within(scaled(middle)):
            low = middle
        else:
            high = middle - 1
    return scaled(low)


SENDABLE = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
"""The types every provider here takes. Anything else (a TIFF, a BMP, a HEIC a phone sent) is
re-encoded, since sent as it is it would be refused."""

JPEG_QUALITIES = (85, 75, 60, 45)
"""The qualities a picture too large as PNG is tried at, best first. 85 keeps text on a screenshot
readable; under 45 artefacts eat small type, and scaling down reads better than going lower."""
SHRINK_STEP = 0.75
"""How much each further pass scales a picture down that no quality brought under the bound."""


def fit_image(data: bytes, mime: str, limits: ImageLimits) -> FittedImage:
    """``data`` fitted to ``limits``; pure and deterministic for a given imaging library.

    A picture that cannot be decoded goes out as it is when it is within the byte bound (the provider
    is the judge of it then) and is marked as not fitting otherwise."""
    from PIL import Image, ImageOps  # Lazy: only a request with a picture needs the imaging library

    mime = sniffed_mime(data, mime)
    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
            # The first frame only: an animated GIF past the bounds is sent as its first picture.
            image = probe.copy()
    except Exception as exc:  # noqa: BLE001 — any undecodable picture is judged on its bytes alone
        logger.info("a picture of %d bytes could not be decoded to be fitted: %s", len(data), exc)
        return FittedImage(data, mime, fits=encoded_size(data) <= limits.max_encoded_bytes)
    goal = target_size(width, height, limits)
    if goal == (width, height) and mime in SENDABLE and encoded_size(data) <= limits.max_encoded_bytes:
        return FittedImage(data, mime, width, height)
    # A photograph's orientation lives in its EXIF tag, which re-encoding drops; turned upright first,
    # the picture still shows what the operator saw.
    image = ImageOps.exif_transpose(image) if mime == "image/jpeg" else image
    if image.size != (width, height):
        goal = target_size(*image.size, limits)
    alpha = image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)
    image = image.convert("RGBA" if alpha else "RGB")
    while True:
        scaled = image if goal == image.size else image.resize(goal, Image.Resampling.LANCZOS)
        encoded = _encode(scaled, prefer_png=mime != "image/jpeg", limit=limits.max_encoded_bytes)
        if encoded is not None:
            out, out_mime = encoded
            return FittedImage(out, out_mime, scaled.width, scaled.height, original=(width, height))
        if max(goal) <= 64:
            return FittedImage(data, mime, width, height, fits=False)
        goal = (max(1, math.floor(goal[0] * SHRINK_STEP)), max(1, math.floor(goal[1] * SHRINK_STEP)))


PNG_OVER_JPEG = 2.0
"""How many times the size of its best JPEG a PNG may be and still be chosen.

A screenshot of text and flat colour is smaller as PNG than as JPEG, or not much larger, and keeps its
type sharp. A photograph or a game's rendered frame is five to eight times larger as PNG: kept as PNG
merely because one picture fits the per-image bound, seven of them were past the request's budget for
pictures and five were left out. Twice is far from both."""


def _encode(image: object, *, prefer_png: bool, limit: int) -> tuple[bytes, str] | None:
    """The picture as PNG when it was not a photograph, fits, and is not much larger than its JPEG;
    else as the best JPEG that fits."""
    from PIL import Image  # Lazy: only a picture that must be re-encoded needs the imaging library

    assert isinstance(image, Image.Image)
    flat = image
    if image.mode == "RGBA":
        # JPEG has no transparency: a transparent area turns white, as on the page it was drawn for.
        flat = Image.new("RGB", image.size, (255, 255, 255))
        flat.paste(image, mask=image.getchannel("A"))
    jpegs: list[bytes] = []
    for quality in JPEG_QUALITIES:
        buffer = io.BytesIO()
        flat.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=False)
        jpegs.append(buffer.getvalue())
        if encoded_size(jpegs[-1]) <= limit:
            break
    if prefer_png:
        buffer = io.BytesIO()
        # Level 6, the library's default: level 9 took seven times as long (1.5 s for one frame of
        # 1.15 megapixels) for five per cent fewer bytes.
        image.save(buffer, format="PNG", compress_level=6)
        png = buffer.getvalue()
        if encoded_size(png) <= limit and len(png) <= PNG_OVER_JPEG * len(jpegs[0]):
            return png, "image/png"
    if encoded_size(jpegs[-1]) <= limit:
        return jpegs[-1], "image/jpeg"
    return None


class FittedImages:
    """Fitted pictures kept on disk, so a picture fitted once is the same bytes on every request.

    Kept under ``root`` by the SHA-256 of the original and :meth:`ImageLimits.key`. A picture that
    needed no change is kept as a note saying so, not as a second copy. A file that cannot be written
    leaves the picture fitted for this request only; the next one fits it again."""

    def __init__(self, root: Path) -> None:
        self.root = root
        # One picture fitted at a time: two requests built at once for the same session would
        # otherwise both fit the same picture, and the work is a second or less per picture.
        self._lock = asyncio.Lock()

    async def fit(self, digest: str | None, load: Callable[[], Awaitable[tuple[bytes, str]]], limits: ImageLimits) -> FittedImage:
        """The picture ``load`` reads (whose SHA-256 is ``digest``) fitted to ``limits``.

        ``load`` is called only when it must be: a picture kept fitted is read from the kept file,
        not from the original, so a session's nine-megabyte screenshots are not read on every request.
        Without a ``digest`` it is loaded to be hashed."""
        data: bytes | None = None
        mime = ""
        if not (digest and re.fullmatch(r"[0-9a-f]{64}", digest)):
            data, mime = await load()
            digest = hashlib.sha256(data).hexdigest()
        key = f"{digest}.{limits.key()}"
        async with self._lock:
            note = await asyncio.to_thread(self._read, key)
            if note is not None and note.changed:
                return note
            if data is None:
                data, mime = await load()
            if note is not None:
                return FittedImage(data, note.mime, note.width, note.height, fits=note.fits)
            fitted = await asyncio.to_thread(fit_image, data, mime, limits)
            await asyncio.to_thread(self._write, key, fitted)
            return fitted

    def _paths(self, key: str) -> tuple[Path, Path]:
        folder = self.root / key[:2]
        return folder / f"{key}.json", folder / f"{key}.bin"

    def _read(self, key: str) -> FittedImage | None:
        """What is kept for ``key``: the fitted picture, or for one that needed no change a stand-in
        with empty bytes whose ``changed`` is false (the caller sends the original)."""
        note_path, data_path = self._paths(key)
        try:
            note = json.loads(note_path.read_text(encoding="utf-8"))
            if not note.get("changed"):
                return FittedImage(b"", str(note["mime"]), int(note.get("width") or 0), int(note.get("height") or 0),
                                   fits=bool(note.get("fits", True)))
            stored = data_path.read_bytes()
            if hashlib.sha256(stored).hexdigest() != note.get("sha256"):
                return None
            original = note.get("original") or [0, 0]
            return FittedImage(stored, str(note["mime"]), int(note["width"]), int(note["height"]),
                               original=(int(original[0]), int(original[1])))
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _write(self, key: str, fitted: FittedImage) -> None:
        note_path, data_path = self._paths(key)
        note: dict[str, object] = {"mime": fitted.mime, "width": fitted.width, "height": fitted.height,
                                   "changed": fitted.changed, "fits": fitted.fits}
        try:
            note_path.parent.mkdir(parents=True, exist_ok=True)
            if fitted.changed:
                note["original"] = list(fitted.original or (0, 0))
                note["sha256"] = hashlib.sha256(fitted.data).hexdigest()
                _atomic_write(data_path, fitted.data)
            _atomic_write(note_path, json.dumps(note, sort_keys=True).encode("utf-8"))
        except OSError as exc:
            logger.warning("a fitted picture could not be kept in %s: %s", self.root, exc)


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".fit-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def data_url(fitted: FittedImage) -> str:
    return f"data:{fitted.mime};base64,{base64.b64encode(fitted.data).decode('ascii')}"


__all__ = [
    "CLAUDE_HIGH_LIMITS",
    "CLAUDE_LIMITS",
    "DEFAULT_LIMITS",
    "MAX_ENCODED_BYTES",
    "OPENAI_LIMITS",
    "FittedImage",
    "FittedImages",
    "ImageLimits",
    "data_url",
    "encoded_size",
    "fit_image",
    "limits_for",
    "sniffed_mime",
    "target_size",
]
