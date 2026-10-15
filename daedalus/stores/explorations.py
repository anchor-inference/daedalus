"""What an open exploration holds of its project's goal budget, read in the budget's own snapshot.

An exploration reserves its whole budget when it is launched, so the work it races cannot be
starved halfway by other spending of the project. The hold is what the budget still has room for:
the reservation less what its attempts' calls were already charged, which the goal ledger counts
as spent on its own. A call one of its attempts is about to make is covered by that remainder
rather than counted a second time beside it.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

HOLDING = ("launching", "running")
"""States in which an exploration's attempts may still spend. Once they have all reported, or the
deadline or the budget stopped them, the rest of the reservation goes back to the project."""


async def _spent_in(conn: aiosqlite.Connection, exploration_id: str) -> int:
    """Microdollars the exploration's attempts were charged, from their own sessions' usage."""
    async with conn.execute(
        "SELECT coalesce(sum(CAST(round(u.cost_usd * 1000000) AS INTEGER)), 0) AS spent"
        " FROM exploration_attempts a JOIN staff_sessions s ON s.staff_id = a.staff_id AND s.task_id = a.task_id"
        " JOIN usage_events u ON u.session_id = s.session_id"
        " WHERE a.exploration_id = ? AND u.cost_usd IS NOT NULL", (exploration_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row["spent"] or 0) if row is not None else 0


async def held_in(conn: aiosqlite.Connection, scope_key: str) -> int:
    """What open explorations hold under one goal budget's balance; nothing under any other cap."""
    kind, _, budget_id = scope_key.partition(":")
    if kind != "goal" or not budget_id:
        return 0
    async with conn.execute(
        "SELECT id,budget_microusd FROM explorations WHERE goal_budget_id = ? AND state IN (?,?)",
        (budget_id, *HOLDING),
    ) as cursor:
        rows = await cursor.fetchall()
    total = 0
    for row in rows:
        total += max(0, int(row["budget_microusd"]) - await _spent_in(conn, row["id"]))
    return total


async def cover_in(conn: aiosqlite.Connection, session_id: str | None) -> dict[str, int]:
    """The goal balances a session's next call is already held under, and how much room is left.

    Only an attempt's own session is covered: a subagent it starts is charged to the project like
    any other call, which keeps the exception narrow and never lets a call escape a cap."""
    if not session_id:
        return {}
    async with conn.execute(
        "SELECT e.id,e.goal_budget_id,e.budget_microusd FROM staff_sessions s"
        " JOIN exploration_attempts a ON a.staff_id = s.staff_id AND a.task_id = s.task_id"
        " JOIN explorations e ON e.id = a.exploration_id"
        " WHERE s.session_id = ? AND e.goal_budget_id IS NOT NULL AND e.state IN (?,?)",
        (session_id, *HOLDING),
    ) as cursor:
        rows = await cursor.fetchall()
    cover: dict[str, int] = {}
    for row in rows:
        room = max(0, int(row["budget_microusd"]) - await _spent_in(conn, row["id"]))
        key = f"goal:{row['goal_budget_id']}"
        cover[key] = cover.get(key, 0) + room
    return cover


async def spent_by_attempt_in(conn: aiosqlite.Connection, exploration_id: str) -> dict[int, dict[str, Any]]:
    """Each attempt's spend: dollars its Daedalus sessions were charged, and what a command-line
    agent reported last. A cost nobody priced is said to be unpriced, never zero."""
    out: dict[int, dict[str, Any]] = {}
    async with conn.execute(
        "SELECT a.slot,s.kind,s.session_id,s.usage_json FROM exploration_attempts a"
        " JOIN staff_sessions s ON s.staff_id = a.staff_id AND s.task_id = a.task_id"
        " WHERE a.exploration_id = ?", (exploration_id,),
    ) as cursor:
        sessions = await cursor.fetchall()
    for row in sessions:
        entry = out.setdefault(int(row["slot"]), {"usd": 0.0, "unpriced": False})
        if row["kind"] == "daedalus" and row["session_id"]:
            async with conn.execute(
                "SELECT coalesce(sum(cost_usd), 0) AS usd, sum(cost_usd IS NULL) AS unpriced"
                " FROM usage_events WHERE session_id = ?", (row["session_id"],),
            ) as cursor:
                usage = await cursor.fetchone()
            if usage is not None:
                entry["usd"] += float(usage["usd"] or 0.0)
                entry["unpriced"] = entry["unpriced"] or bool(usage["unpriced"])
            continue
        try:
            snapshot = json.loads(row["usage_json"] or "{}")
        except (TypeError, ValueError):
            snapshot = {}
        if not isinstance(snapshot, dict):
            continue
        if snapshot.get("cost_usd") is not None:
            entry["usd"] += float(snapshot["cost_usd"])
        elif snapshot:
            entry["unpriced"] = True
    return out


__all__ = ["HOLDING", "cover_in", "held_in", "spent_by_attempt_in"]
