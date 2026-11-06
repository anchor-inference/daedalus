"""Making a stranger's text inert before anything stores or draws it."""

from __future__ import annotations

import re
import unicodedata

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
"""Terminal escapes, whole: a stripped ESC alone would leave the rest of the sequence as text."""

INVISIBLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f\u2800\u3164\ufe00-\ufe0f\ufeff\uffa0"
    "\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
"""What draws as nothing: zero-width and direction marks, soft hyphen, fillers, the blank braille cell, variation
selectors and the whole tag and selector plane. Whole sentences fit in these, one character a byte, and a
model reads them perfectly well while the operator sees an empty line. Every other format character goes too,
by category, in :func:`inert`."""


def inert(text: str) -> str:
    """``text`` with what could act on a reader or a terminal removed; line breaks and tabs stay."""
    text = ANSI.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = INVISIBLE.sub("", text)
    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf")).strip()


def one_line(text: str, limit: int) -> str:
    """Operator-written or guest-written text that is shown as a single line."""
    return " ".join(inert(text).split())[:limit]


__all__ = ["inert", "one_line"]
