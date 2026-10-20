"""Stored opaque reasoning moved onto the core's record format.

An assistant turn written through the Codex route keeps the encrypted reasoning the model will reason
from on its next request, as a record on ``Message.reasoning_items`` naming the endpoint and model it
came from. The host named that record ``chat.reasoning_details``; the core's adapter, whose replay the
host now uses, names it ``openai_compat.reasoning_details`` and sends back only records of its own
format. Left as they were, every stored turn would lose its reasoning at the upgrade and a reasoning
model would start the next tool step of each open session from nothing.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite
from protocore.providers.openai_compat.wire import REASONING_ENVELOPE

OLD_FORMAT = "chat.reasoning_details"

TABLES = (
    ("session_messages", "seq", "message"),
    ("transcript", "seq", "message"),
    ("snapshots", "run_id", "snapshot"),
)
"""The columns that hold assistant messages: the history, the transcript and a paused run's snapshot."""


def rewritten(value: Any) -> tuple[Any, bool]:
    """``value`` with every old-format record in a ``reasoning_items`` list renamed, and whether any was."""
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
        if key == "reasoning_items" and isinstance(item, list):
            records = []
            for record in item:
                if isinstance(record, dict) and record.get("format") == OLD_FORMAT:
                    record = {**record, "format": REASONING_ENVELOPE}
                    changed = True
                records.append(record)
            out[key] = records
            continue
        new, moved = rewritten(item)
        out[key] = new
        changed = changed or moved
    return out, changed


async def move_reasoning_records(conn: aiosqlite.Connection) -> int:
    """Rename every stored old-format reasoning record; returns how many rows changed. Run again it finds none."""
    count = 0
    for table, key, column in TABLES:
        cursor = await conn.execute(
            f"SELECT {key}, {column} FROM {table} WHERE {column} LIKE '%{OLD_FORMAT}%'"  # noqa: S608 — names from TABLES
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
