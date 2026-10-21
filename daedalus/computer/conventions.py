"""Turning a model's point into a pixel of the daemon's screenshot, and sizing the image the model sees.

Three pictures are involved, and the arithmetic between them lives here and nowhere else:

* the **screenshot**: what deskd captured, ``width``×``height`` pixels, whose id every action names.
  deskd takes points in its pixels only and turns them into desktop units itself;
* the **shown image**: what the model is given. It is the screenshot resized by the host to a size the
  model's provider keeps as it is (:func:`shown_size`), because a provider that resizes on its side
  shifts every coordinate read off the picture by its own scale;
* the **model's frame**: how the model names a point. Pixels of the shown image (``screenshot`` and
  ``smart_resize``, whose shown image is already the size the model's processor would make it), or
  0–1000 across and down (``relative1000``), which does not depend on the image's size at all.

Rounding, written down once:

* a pixel ``v`` of an image covers ``[v, v + 1)`` and its centre is ``v + 0.5``. A point of the shown
  image goes to the screenshot by its centre — ``floor((v + 0.5) · W / W')`` — which is the pixel of
  the screenshot that centre falls in; with the two the same size it is ``v`` itself;
* a model writing a fraction (``412.6``) means the place, not a pixel: the same formula takes it;
* a relative point ``r`` is the place ``r / 1000 · W`` of the screenshot, and its pixel is the floor;
* the far edge (``x = W'`` in pixels, ``x = 1000`` relative) is clamped to the last pixel. A point
  more than one pixel off the image (or outside 0–1000) is refused: it is a misread, and clamping it
  would click somewhere the model never pointed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from daedalus.model_capabilities import ComputerProfile, Convention

RELATIVE_SPAN = 1000


class PointOutside(ValueError):
    """A point that is not on the image the model was shown; the message says the image's frame."""


@dataclass(frozen=True, slots=True)
class Frame:
    """The sizes a point travels between: the shown image's and the screenshot's, in pixels."""

    shown_width: int
    shown_height: int
    width: int
    height: int


def _clamp(value: int, upper: int) -> int:
    return max(0, min(upper - 1, value))


def to_screenshot(convention: Convention, x: float, y: float, frame: Frame) -> tuple[int, int]:
    """The screenshot pixel a model's point names; raises :class:`PointOutside` for a point off the image."""
    if convention == "relative1000":
        if not (0 <= x <= RELATIVE_SPAN and 0 <= y <= RELATIVE_SPAN):
            raise PointOutside(f"({_num(x)}, {_num(y)}) is outside 0–{RELATIVE_SPAN}: points are relative, 0 to {RELATIVE_SPAN} across and down the screenshot")
        return (
            _clamp(math.floor(x / RELATIVE_SPAN * frame.width), frame.width),
            _clamp(math.floor(y / RELATIVE_SPAN * frame.height), frame.height),
        )
    if not (-1 < x <= frame.shown_width and -1 < y <= frame.shown_height):
        raise PointOutside(f"({_num(x)}, {_num(y)}) is not on the screenshot, which is {frame.shown_width}×{frame.shown_height} pixels")
    return (
        _clamp(math.floor((x + 0.5) * frame.width / frame.shown_width), frame.width),
        _clamp(math.floor((y + 0.5) * frame.height / frame.shown_height), frame.height),
    )


def from_screenshot(convention: Convention, px: float, py: float, frame: Frame) -> tuple[int, int]:
    """A screenshot pixel in the model's frame: the inverse of :func:`to_screenshot`, by the pixel's centre."""
    if convention == "relative1000":
        return (
            max(0, min(RELATIVE_SPAN, round((px + 0.5) / frame.width * RELATIVE_SPAN))),
            max(0, min(RELATIVE_SPAN, round((py + 0.5) / frame.height * RELATIVE_SPAN))),
        )
    return (
        _clamp(math.floor((px + 0.5) * frame.shown_width / frame.width), frame.shown_width),
        _clamp(math.floor((py + 0.5) * frame.shown_height / frame.height), frame.shown_height),
    )


def to_desktop(px: float, py: float, region: dict[str, float], width: int, height: int) -> tuple[float, float]:
    """A screenshot pixel as a desktop point, by the contract's centre rule
    (``docs/architecture/computer.md``, Coordinates); for finding the window under a point."""
    return (
        float(region.get("x", 0)) + (px + 0.5) * float(region.get("w", width)) / width,
        float(region.get("y", 0)) + (py + 0.5) * float(region.get("h", height)) / height,
    )


def _round_by_factor(number: float, factor: int) -> int:
    # Python's round, as Qwen's processor writes it: halves go to the even multiple, and the model was
    # trained on images sized by exactly this.
    return round(number / factor) * factor


def _ceil_by_factor(number: float, factor: int) -> int:
    return math.ceil(number / factor) * factor


def _floor_by_factor(number: float, factor: int) -> int:
    return math.floor(number / factor) * factor


def smart_resize(height: int, width: int, *, factor: int = 28, min_pixels: int = 56 * 56, max_pixels: int = 28 * 28 * 1280) -> tuple[int, int]:
    """Qwen2.5-VL's ``smart_resize``, line for line: ``(height, width)`` as multiples of ``factor``, the
    aspect kept as nearly as that allows, the area within ``[min_pixels, max_pixels]``.

    The model answers in pixels of the image its processor made. Shown an image already of this size,
    the processor leaves it alone, so the model's pixels are the shown image's."""
    if height <= 0 or width <= 0:
        raise ValueError("an image has a positive size")
    if max(height, width) / min(height, width) > 200:
        raise ValueError(f"an aspect ratio over 200 cannot be resized ({width}×{height})")
    h_bar = max(factor, _round_by_factor(height, factor))
    w_bar = max(factor, _round_by_factor(width, factor))
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = max(factor, _floor_by_factor(height / beta, factor))
        w_bar = max(factor, _floor_by_factor(width / beta, factor))
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = _ceil_by_factor(height * beta, factor)
        w_bar = _ceil_by_factor(width * beta, factor)
    return h_bar, w_bar


def shown_size(profile: ComputerProfile, width: int, height: int) -> tuple[int, int]:
    """The size of the image a model of ``profile`` is shown for a screenshot of ``width``×``height``.

    Never larger than the screenshot, except by ``smart_resize``'s rounding to 28; the aspect is kept.
    A size is rounded down, so the bounds hold after the rounding as well."""
    if profile.convention == "smart_resize":
        h_bar, w_bar = smart_resize(height, width, max_pixels=profile.max_pixels or 28 * 28 * 1280)
        return w_bar, h_bar
    scale = 1.0
    long_side, short_side = max(width, height), min(width, height)
    if profile.max_edge:
        scale = min(scale, profile.max_edge / long_side)
    if profile.max_short:
        scale = min(scale, profile.max_short / short_side)
    if profile.max_pixels:
        scale = min(scale, math.sqrt(profile.max_pixels / (width * height)))
    if scale >= 1.0:
        return width, height
    return max(1, math.floor(width * scale)), max(1, math.floor(height * scale))


def frame_words(convention: Convention, frame: Frame) -> str:
    """How the model names a point on the image it was just shown, in one clause."""
    if convention == "relative1000":
        return f"points are relative: 0 to {RELATIVE_SPAN} across and down, whatever the image's size"
    return f"points are pixels of this {frame.shown_width}×{frame.shown_height} image, (0, 0) at the top left"


def _num(value: float) -> str:
    return f"{value:g}"


__all__ = ["RELATIVE_SPAN", "Frame", "PointOutside", "frame_words", "from_screenshot", "shown_size", "smart_resize", "to_desktop", "to_screenshot"]
