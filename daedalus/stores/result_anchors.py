"""Exact, append-only links from a result to verified source turns in its project."""

from __future__ import annotations

import hashlib
from typing import Any

import aiosqlite

from daedalus.stores.database import Database


class ResultAnchorRefused(ValueError):
    """A source turn is missing or belongs to a different project than the result."""


async def add_result_turn_anchor(
    conn: aiosqlite.Connection, result_id: str, session_id: str, turn_seq: int,
) -> dict[str, Any]:
    """Bind an existing host turn to a result inside the result command's transaction."""
    if type(turn_seq) is not int or turn_seq < 1:
        raise ResultAnchorRefused("a positive turn sequence is required")
    async with conn.execute(
        "SELECT r.id FROM result_receipts r JOIN board_tasks t ON t.id = r.task_id "
        "JOIN sessions s ON s.project_id = t.project_id WHERE r.id = ? AND s.id = ?",
        (result_id, session_id),
    ) as cursor:
        scoped = await cursor.fetchone()
    if scoped is None:
        raise ResultAnchorRefused("result and source session do not share a project")
    async with conn.execute(
        "SELECT message FROM session_messages WHERE session_id = ? AND seq = ?",
        (session_id, turn_seq),
    ) as cursor:
        turn = await cursor.fetchone()
    if turn is None:
        raise ResultAnchorRefused("source turn does not exist")
    source_digest = hashlib.sha256(turn["message"].encode()).hexdigest()
    await conn.execute(
        "INSERT INTO result_turn_anchors(result_id,session_id,turn_seq,source_digest) VALUES (?,?,?,?)",
        (result_id, session_id, turn_seq, source_digest),
    )
    return {"result_id": result_id, "session_id": session_id, "turn_seq": turn_seq,
            "source_digest": source_digest}


async def result_turn_refs(db: Database, task_id: str, result_id: str) -> list[dict[str, Any]]:
    result = await db.fetchone("SELECT 1 FROM result_receipts WHERE id = ? AND task_id = ?", (result_id, task_id))
    if result is None:
        raise KeyError(result_id)
    rows = await db.fetchall(
        "SELECT a.session_id,a.turn_seq,a.source_digest,m.message FROM result_turn_anchors a "
        "LEFT JOIN session_messages m ON m.session_id = a.session_id AND m.seq = a.turn_seq "
        "WHERE a.result_id = ? ORDER BY a.session_id,a.turn_seq", (result_id,),
    )
    return [
        {"session_id": row["session_id"], "turn_seq": row["turn_seq"],
         "source_ref": f"session:{row['session_id']}@{row['turn_seq']}",
         "source_digest": row["source_digest"],
         "source_current": row["message"] is not None and hashlib.sha256(row["message"].encode()).hexdigest() == row["source_digest"]}
        for row in rows
    ]
