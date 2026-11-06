"""Making a stranger's text inert before anything stores or draws it."""

from __future__ import annotations

import re
import unicodedata

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
"""Terminal escapes, whole: a stripped ESC alone would leave the rest of the sequence as text."""

INVISIBLE = re.compile("[​-‍⁠﻿‪-‮⁦-⁩\U000e0000-\U000e007f]")
"""Zero-width characters, direction overrides and the Unicode tag block. They draw as nothing, so they
can carry words the operator never sees and a model reads perfectly well."""


def inert(text: str) -> str:
    """``text`` with what could act on a reader or a terminal removed; line breaks and tabs stay."""
    text = ANSI.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = INVISIBLE.sub("", text)
    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc").strip()


def one_line(text: str, limit: int) -> str:
    """Operator-written or guest-written text that is shown as a single line."""
    return " ".join(inert(text).split())[:limit]


__all__ = ["inert", "one_line"]
