"""What a model is known to accept when the endpoint serving it does not say.

The subscription routes on the key proxy (Claude, Codex, Grok) answer ``/models`` with bare ids, and
so does any plain OpenAI-compatible server. Add a model then had nothing to prefill the Images switch
from, left it off, and every Claude model but the one the operator had turned on by hand for image
understanding was saved as not taking pictures. The vision fallback then refused to let the session's
own model look, although every one of them reads images. The families below are the ones whose image
input is a published, stable fact; anything else stays unknown and is the operator's to set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_IMAGE_FAMILIES = (
    # Every Claude model from the third generation on reads images, Haiku included.
    re.compile(r"^claude-(?:3|haiku|sonnet|opus|fable|mythos)\b"),
    # GPT-4o, GPT-4.1 and every GPT from the fifth on, the Codex variants among them. ``gpt-oss`` is
    # text-only and does not match, nor does a plain ``gpt-4`` or ``gpt-3.5``.
    re.compile(r"^gpt-(?:4o|4\.1|[5-9]|\d{2,})(?![a-z])"),
    re.compile(r"^o[34](?:-|$)"),
    # Grok reads images from the fourth generation on and in the earlier ``-vision`` models; the
    # ``grok-code`` models are text-only.
    re.compile(r"^grok-(?:[4-9]|\d{2,})(?!\d)(?!.*code)"),
    re.compile(r"^grok-\d+(?:\.\d+)?-vision"),
)


def known_image_input(model: str) -> bool | None:
    """True when the model id belongs to a family known to take images; ``None`` when nothing is known.

    Never False: an unrecognised id says nothing about the model, and the caller keeps what it had.
    A vendor prefix (``anthropic/claude-…`` on OpenRouter) and a date suffix are both allowed."""
    name = str(model or "").strip().lower().rsplit("/", 1)[-1]
    if not name:
        return None
    return True if any(family.search(name) for family in _IMAGE_FAMILIES) else None


Convention = Literal["screenshot", "relative1000", "smart_resize"]
"""How a model names a point on a picture it was shown:

* ``screenshot`` — pixels of the image it was shown (Claude, OpenAI's computer-use models);
* ``relative1000`` — 0 to 1000 across and down whatever the image's size (Qwen3-VL, UI-TARS 1.0 and
  Doubao, GLM-4.5V and 4.1V, Gemini);
* ``smart_resize`` — pixels of the image as the model's own processor resized it to multiples of 28
  (Qwen2.5-VL, UI-TARS-1.5). The host shows it an image already of that size, so its pixels are the
  shown image's and the processor leaves it alone.
"""


@dataclass(frozen=True, slots=True)
class ComputerProfile:
    """How a model family reads a screenshot: the convention of its points and the image size it is shown.

    ``max_edge`` bounds the long side of the image the model is shown and ``max_pixels`` its area;
    ``max_short`` bounds the short side where a vendor names a box (OpenAI's 1440×900). The host never
    lets a provider resize a screenshot on its side, because every coordinate read off the picture
    would then be off by the provider's scale: the image is sent already at a size the provider keeps.
    ``known`` is false for the fallback a model with images but no known family gets."""

    family: str
    convention: Convention
    max_edge: int
    max_pixels: int = 0
    """0 = no bound on the area beyond what ``max_edge`` gives."""
    max_short: int = 0
    known: bool = True


CLAUDE_PROFILE = ComputerProfile("claude", "screenshot", 1568, max_pixels=1_150_000)
"""Anthropic keeps an image whose long side is at most 1568 pixels and whose area is at most about
1.15 megapixels (about 1568 tokens) and scales anything larger down on its side, which would move every
point the model reads. A 16:10 screen comes out near 1356×847."""
OPENAI_PROFILE = ComputerProfile("openai", "screenshot", 1440, max_short=900)
"""OpenAI's computer-use guidance: a screenshot within 1440×900, sent at its own detail."""
RELATIVE_PROFILE = ComputerProfile("relative1000", "relative1000", 1440)
"""A relative point means the same at any size, so the size only bounds the tokens."""
SMART28_PROFILE = ComputerProfile("smart28", "smart_resize", 1568, max_pixels=28 * 28 * 1280)
"""Qwen2.5-VL's processor with the agent deployments' ``max_pixels`` of 1280 patches of 28×28. A
server configured with a larger bound leaves the image alone as well, since it is already a multiple of
28 and inside it; one configured with a smaller bound would resize it again, which is the operator's
to avoid."""
UNKNOWN_PROFILE = ComputerProfile("unknown", "screenshot", 1568, max_pixels=1_150_000, known=False)
"""A model that takes images but whose way with points is not known: pixels of the screenshot, sized
as Claude's, which no provider resizes; how well it points is unknown and the tool says so."""

_COMPUTER_FAMILIES: tuple[tuple[re.Pattern[str], ComputerProfile], ...] = (
    # Claude 4.5 and later (opus/sonnet/haiku 4-5, 4-6 …, every 5.x and on), and the fable and mythos
    # lines. ``claude-opus-4-1``, ``claude-opus-4-20250514`` (whose date is no minor version) and the
    # 3.x models read images and fall back to the unknown profile.
    (re.compile(r"^claude-(?:opus|sonnet|haiku)-(?:4[-.](?:[5-9]|[1-9]\d)(?!\d)|[5-9](?!\d)|[1-9]\d(?!\d))"), CLAUDE_PROFILE),
    (re.compile(r"^claude-(?:fable|mythos)\b"), CLAUDE_PROFILE),
    # GPT-5.4 and later minor versions, and every GPT from the sixth on.
    (re.compile(r"^gpt-5[.-](?:[4-9]|\d{2,})(?!\d)"), OPENAI_PROFILE),
    (re.compile(r"^gpt-(?:[6-9]|[1-9]\d)(?!\d)"), OPENAI_PROFILE),
    # UI-TARS-1.5 is trained on Qwen2.5-VL and answers in its resized pixels; it is matched before the
    # plain UI-TARS line, whose 1.0 models and Doubao answer 0–1000.
    (re.compile(r"ui-?tars-?1\.5"), SMART28_PROFILE),
    (re.compile(r"qwen2\.5-vl"), SMART28_PROFILE),
    (re.compile(r"ui-?tars"), RELATIVE_PROFILE),
    (re.compile(r"qwen3(?:\.\d+)?-vl"), RELATIVE_PROFILE),
    (re.compile(r"^glm-4\.(?:5|1)v"), RELATIVE_PROFILE),
    (re.compile(r"^gemini-"), RELATIVE_PROFILE),
)


def known_computer_use(model: str) -> ComputerProfile | None:
    """The computer-use profile of a model's family, or ``None`` when the family is not known.

    ``None`` says nothing about whether the model can do it: a model with its Images switch on and no
    profile is offered the tools under :data:`UNKNOWN_PROFILE`. A vendor prefix and a date suffix are
    allowed, as for :func:`known_image_input`."""
    name = str(model or "").strip().lower().rsplit("/", 1)[-1]
    if not name:
        return None
    for pattern, profile in _COMPUTER_FAMILIES:
        if pattern.search(name):
            return profile
    return None


__all__ = [
    "CLAUDE_PROFILE",
    "OPENAI_PROFILE",
    "RELATIVE_PROFILE",
    "SMART28_PROFILE",
    "UNKNOWN_PROFILE",
    "ComputerProfile",
    "Convention",
    "known_computer_use",
    "known_image_input",
]
