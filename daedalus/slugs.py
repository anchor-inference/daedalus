"""The slug of an agent's page: the address segment under ``/pages/`` and ``/app/p/``.

Letters of any script are kept. A slug held only Latin letters and digits, so a page made from a
widget titled in Russian came out as ``widget-2``, and the operator could not tell such pages apart
in the address bar or the navigation. A slug is still safe as a folder name and a URL segment: it
holds letters, digits, combining marks and hyphens only, so never a separator, a dot or a space; it
is lower-case and in NFC, so one title gives one slug however it was typed.
"""

from __future__ import annotations

import unicodedata

PAGE_SLUG_MAX = 63
"""Characters, not bytes: a Cyrillic slug of this length is about 380 bytes once percent-encoded."""

_STARTS = frozenset({"Ll", "Lm", "Lo", "Nd"})
_CONTINUES = _STARTS | {"Mn", "Mc"}
"""Combining marks continue a word (Devanagari vowel signs, Cyrillic й written decomposed) but
cannot begin one. Upper- and title-case letters are left out: a slug is lower-case."""


def _continues(char: str) -> bool:
    return char == "-" or unicodedata.category(char) in _CONTINUES


def is_page_slug(slug: object) -> bool:
    """Whether ``slug`` is a page's slug: 1 to 63 lower-case letters, digits, marks and hyphens,
    starting with a letter or digit, in NFC. A leading underscore stays reserved for the host's own
    paths beside the pages (``/pages/_kit/``)."""
    return (
        isinstance(slug, str)
        and 0 < len(slug) <= PAGE_SLUG_MAX
        and unicodedata.category(slug[0]) in _STARTS
        and all(_continues(char) for char in slug)
        and unicodedata.is_normalized("NFC", slug)
    )


def page_slug_from(text: str, *, limit: int = 48) -> str:
    """``text`` as a slug's words: lower-case, NFC, every run of other characters one hyphen; may be empty."""
    lowered = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).lower())
    out: list[str] = []
    for char in lowered:
        if char != "-" and _continues(char):
            out.append(char)
        elif out and out[-1] != "-":
            out.append("-")
    words = "".join(out).strip("-")[:limit].strip("-")
    # A mark the cut or the stripping left at the front cannot begin a slug.
    while words and unicodedata.category(words[0]) not in _STARTS:
        words = words[1:].lstrip("-")
    return words


__all__ = ["PAGE_SLUG_MAX", "is_page_slug", "page_slug_from"]
