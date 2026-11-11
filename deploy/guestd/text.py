"""Making a stranger's text inert before anything stores or draws it."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

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


CARD_BYTES = 12 * 1024
"""The largest card a message carries, as JSON with everything past ASCII escaped, which is how it travels. A summary
or a list of free times fits several times over; a message at its limit and a card at its limit together stay under
the 64 KiB a request body may be."""
CARD_TYPE = re.compile(r"[a-z_]{1,24}")
CARD_DEPTH = 4
CARD_ITEMS = 60
CARD_TEXT = 4000


class CardRefused(ValueError):
    """A card that makes no sense: the words say why, for the agent that sent it."""


def card_of(value: Any) -> dict[str, Any] | None:
    """A card a message carries beside its words: a summary, free times, a meeting. ``None`` when there is none.

    The door does not know the kinds of card and does not need to: both sides read their own, and an agent that
    knows none reads the words. What the door holds to is that a card is small, plain data and inert in every
    string, because it is drawn in the app on the other side exactly as a message is.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("type"), str) or not CARD_TYPE.fullmatch(value["type"]):
        raise CardRefused('a card is an object whose "type" is a short lowercase word')
    clean = _plain(value, 0)
    if len(json.dumps(clean)) > CARD_BYTES:
        raise CardRefused(f"a card is at most {CARD_BYTES} bytes of JSON")
    return clean


def _plain(value: Any, depth: int) -> Any:
    if depth > CARD_DEPTH:
        raise CardRefused(f"a card is nested at most {CARD_DEPTH} deep")
    if isinstance(value, str):
        return inert(value)[:CARD_TEXT]
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, list):
        return [_plain(item, depth + 1) for item in value[:CARD_ITEMS]]
    if isinstance(value, dict):
        return {one_line(str(key), 40): _plain(item, depth + 1) for key, item in list(value.items())[:CARD_ITEMS]}
    raise CardRefused("a card holds text, numbers, lists and objects only")


__all__ = ["CARD_BYTES", "CardRefused", "card_of", "inert", "one_line"]
