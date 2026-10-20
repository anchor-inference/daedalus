"""Stored messages and queue items moved onto the core's own names for the images they carry.

The host kept a user message's pictures under a bare ``metadata["image_refs"]`` as ``{"ref", "mime"}``
pairs, and a queued item's picture as ``attachments[].image_ref``. The core reads neither: its key is
``protocore.image_refs`` holding ``ImageRefBlock`` dumps, and a queued attachment names its picture by
``blob_ref``. Left in the old shape, every picture already in a session would silently stop reaching
the model, and a screenshot waiting in a queue at the upgrade would be placed as its caption alone.
Rewriting the rows once was chosen over a reader that accepts both shapes, which would have to live
for as long as any old row might.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite
from protocore.contracts.types import IMAGE_REFS_METADATA_KEY, ImageRefBlock

OLD_KEY = "image_refs"
"""What the host called the list before; only ever found inside a message's ``metadata``."""

TABLES = (
    ("session_messages", "seq", "message"),
    ("transcript", "seq", "message"),
    ("snapshots", "run_id", "snapshot"),
    ("live_control", "session_id", "steer_queue"),
    ("live_control", "session_id", "follow_up_queue"),
    ("input_receipts", "rowid", "payload"),
)
"""Every column that holds a message or a queue item as JSON: the history and the transcript, a
paused run's snapshot (its history and its own copy of the queues), the queues themselves and the
receipts of input waiting in them."""


def _image(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or not entry.get("ref"):
        return None
    return ImageRefBlock(blob_ref=str(entry["ref"]), mime_type=str(entry.get("mime") or "image/png")).model_dump(mode="json")


def rewritten(value: Any) -> tuple[Any, bool]:
    """``value`` with every old-shape image name in the core's shape, and whether anything changed.

    Only the two places the host wrote are touched: a ``metadata`` mapping's ``image_refs`` list, and
    an ``attachments`` entry's ``image_ref``. The same words inside a tool's output or a file's text
    are data and stay as they are.
    """
    if isinstance(value, list):
        changed = False
        out_list = []
        for item in value:
            new, moved = rewritten(item)
            out_list.append(new)
            changed = changed or moved
        return out_list, changed
    if not isinstance(value, dict):
        return value, False
    changed = False
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key == "metadata" and isinstance(item, dict) and OLD_KEY in item:
            metadata = {k: v for k, v in item.items() if k != OLD_KEY}
            images = [image for entry in item.get(OLD_KEY) or [] if (image := _image(entry)) is not None]
            if images:
                metadata[IMAGE_REFS_METADATA_KEY] = [*(metadata.get(IMAGE_REFS_METADATA_KEY) or []), *images]
            item, changed = metadata, True
        elif key == "attachments" and isinstance(item, list):
            attachments = []
            for entry in item:
                if isinstance(entry, dict) and "image_ref" in entry:
                    entry = dict(entry)
                    entry["blob_ref"] = entry.pop("image_ref")
                    changed = True
                attachments.append(entry)
            item = attachments
        new, moved = rewritten(item)
        out[key] = new
        changed = changed or moved
    return out, changed


async def move_image_keys(conn: aiosqlite.Connection) -> int:
    """Rewrite every stored row that still names its images the host's old way; returns how many.

    Rows are picked by a substring the old shape always contains, so the cost is a scan, not a parse
    of every message; a row the substring finds that holds the words only as data parses and is left
    alone. Run again it finds nothing to change.
    """
    count = 0
    for table, key, column in TABLES:
        cursor = await conn.execute(
            f"SELECT {key}, {column} FROM {table} WHERE {column} LIKE '%\"image_ref%'"  # noqa: S608 — names from TABLES
        )
        for row_key, raw in await cursor.fetchall():
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                continue
            new, changed = rewritten(value)
            if changed:
                await conn.execute(f"UPDATE {table} SET {column} = ? WHERE {key} = ?", (json.dumps(new, ensure_ascii=False, separators=(",", ":")), row_key))  # noqa: S608
                count += 1
    return count
