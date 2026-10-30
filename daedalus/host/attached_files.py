"""The files an operator's message carried, as the app draws them.

The model is told about an attachment in the text itself, as a list the host appends: a path in the
session's inbox (``Attached files:``) or, for an orchestrator, a handle in the file store (``Attached
files (kept by handle; …):``). That list is for the model and stays exactly as it is. The app needs
something else — a name, a type and a size to draw a thumbnail or a card, and an address to fetch the
bytes from — so the host records that beside the text, under :data:`METADATA_KEY`, when the message
is sent.

Every message written before the host recorded it has only the list, and the operator's history is
full of them, so :func:`of` reads the list back when the record is missing. The app never parses a
path: an inbox entry names its file by the name it has in the inbox, which is the only thing the
attachment route will serve.
"""

from __future__ import annotations

import re
from typing import Any

METADATA_KEY = "daedalus.attachments"
"""A user message's attachments: ``[{name, mime, size, file}]`` for a file in the session's inbox,
``[{name, mime, size, kept}]`` for one kept by handle."""

_PLAIN_HEADER = re.compile(r"^Attached files:\s*$")
_KEPT_HEADER = re.compile(r"^Attached files \(kept by handle[^\n]*\):\s*$")
# The tail every line ends with, ``(<mime>, <n> bytes)``, then whatever the host added after it (a
# cookie file's note). Everything before it is the path or the name, which may hold spaces, braces,
# brackets and parentheses of its own — a Windows clipboard paste is named ``{GUID}.png``, a second
# download ``photo (1).png`` — so the path ends where a well-formed tail begins, not at the first
# parenthesis.
_TAIL = r" \((?P<mime>[\w.+-]+/[\w.+-]+(?:;[^,()]*)?), (?P<size>\d+) bytes\)(?P<rest>.*)$"
_PLAIN_LINE = re.compile(r"^- (?P<path>.+?)" + _TAIL)
_KEPT_LINE = re.compile(r"^- att:(?P<id>[0-9a-f]{12}) (?P<name>.+?)" + _TAIL)
_LONGEST_LINE = 4096
"""How long one item may grow while its broken pieces are joined back together."""


def inbox_entry(name: str, stored: str, mime: str, size: int) -> dict[str, Any]:
    """A file copied into the session's inbox: shown by the name it was sent with, served by ``stored``."""
    return {"name": name, "mime": mime, "size": size, "file": stored}


def kept_entry(file_id: str, name: str, mime: str, size: int) -> dict[str, Any]:
    """A file kept by handle in the file store: served by its id."""
    return {"name": name, "mime": mime, "size": size, "kept": file_id}


def inbox_name_ok(name: str) -> bool:
    """Whether ``name`` can only mean a file directly in an inbox: no separator, no parent, not one of
    the hidden partial copies the inbox writes before a file is complete."""
    # A dot file is an attachment like any other (an operator sends ``.env`` to have it checked); only
    # the copy in progress, ``.<name>.<uuid>.part``, and the two names of directories are refused.
    partial = name.startswith(".") and name.endswith(".part")
    return bool(name) and name == name.strip() and name not in (".", "..") and not partial and not any(c in name for c in "/\\\0")


def _base_name(path: str) -> str:
    # Either separator: the path is the one the tools were given, and a session working on a Windows
    # host names its inbox with backslashes.
    return re.split(r"[\\/]", path.rstrip("/\\"))[-1]


def _items(lines: list[str], start: int, pattern: re.Pattern[str]) -> list[re.Match[str]]:
    """The list's items from ``start``, each line joined to the ones after it when it is incomplete on its
    own: a text that went through a wrapping editor or a transport may have broken one item in two."""
    found: list[re.Match[str]] = []
    at = start
    while at < len(lines) and lines[at].startswith("- "):
        line, end = lines[at], at + 1
        match = pattern.match(line)
        while match is None and end < len(lines) and not lines[end].startswith("- ") and lines[end].strip() and len(line) < _LONGEST_LINE:
            line = f"{line} {lines[end].strip()}"
            end += 1
            match = pattern.match(line)
        if match is None:
            # A line the host writes with no tail (``- <name>: not kept — …``): not a file, but still the list.
            end = at + 1
        else:
            found.append(match)
        at = end
    return found


def parse(text: str) -> list[dict[str, Any]]:
    """The attachments named by the host's list in ``text``, or none. The last list is the host's: it
    is appended after the operator's words, which could themselves quote one."""
    lines = text.split("\n")
    for index in range(len(lines) - 1, -1, -1):
        if _PLAIN_HEADER.match(lines[index]):
            out = []
            for match in _items(lines, index + 1, _PLAIN_LINE):
                stored = _base_name(match["path"])
                if inbox_name_ok(stored):
                    out.append(inbox_entry(stored, stored, match["mime"], int(match["size"])))
            return out
        if _KEPT_HEADER.match(lines[index]):
            return [kept_entry(match["id"], match["name"], match["mime"], int(match["size"])) for match in _items(lines, index + 1, _KEPT_LINE)]
    return []


def of(metadata: Any, text: str) -> list[dict[str, Any]]:
    """A message's attachments: the host's record when it made one, else read back from the list."""
    recorded = metadata.get(METADATA_KEY) if isinstance(metadata, dict) else None
    if isinstance(recorded, list):
        return [dict(entry) for entry in recorded if isinstance(entry, dict) and entry.get("name")]
    return parse(text) if "Attached files" in text else []
