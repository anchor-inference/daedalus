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


__all__ = ["known_image_input"]
