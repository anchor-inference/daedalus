"""The durable record of each launch a project's member route chose.

One row per launch command, keyed by its operation id. The row is claimed before anything is
launched and each attempt is written into it before that attempt's launch is queued, so the record,
not the caller's memory, decides what a repeated command does: a finished one is answered from the
row and touches nothing, and one still claimed (by a caller now running, or by a host that stopped
mid-way) is reported as in flight and never launched a second time.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from daedalus.stores.database import Database

FINAL_STATES = ("accepted", "handoff_required", "no_runtime", "refused", "admission_unknown")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class RouteRecord:
    operation_id: str
    project_id: str
    task_id: str | None
    staff_id: str | None
    state: str
    claim_token: str
    selected_harness: str = ""
    selected_model: str = ""
    reason: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def view(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "task_id": self.task_id,
            "staff_id": self.staff_id,
            "outcome": "in_flight" if self.state == "claimed" else self.state,
            "selected_harness": self.selected_harness,
            "selected_model": self.selected_model,
            "reason": self.reason,
            "attempts": list(self.attempts),
            "updated_at": self.updated_at,
        }


def _record(row: Any) -> RouteRecord:
    try:
        attempts = json.loads(row["attempts_json"] or "[]")
        result = json.loads(row["result_json"] or "{}")
    except ValueError:
        attempts, result = [], {}
    return RouteRecord(
        operation_id=row["operation_id"], project_id=row["project_id"], task_id=row["task_id"],
        staff_id=row["staff_id"], state=row["state"], claim_token=row["claim_token"],
        selected_harness=row["selected_harness"], selected_model=row["selected_model"], reason=row["reason"],
        attempts=attempts if isinstance(attempts, list) else [], result=result if isinstance(result, dict) else {},
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class LaunchRoutes:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def claim(self, operation_id: str, *, project_id: str, task_id: str, staff_id: str,
                    actor_id: str) -> tuple[bool, RouteRecord]:
        """Claim the operation, or read who already has. Only a caller that got ``True`` may launch.

        The insert and the read share one write transaction, so two callers with the same id cannot
        both see an empty row and both go on to launch."""
        token = secrets.token_hex(16)
        at = _now()
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO launch_routes(operation_id,project_id,task_id,staff_id,actor_id,state,claim_token,created_at,updated_at)"
                " VALUES (?,?,?,?,?,'claimed',?,?,?) ON CONFLICT(operation_id) DO NOTHING",
                (operation_id, project_id, task_id, staff_id, actor_id, token, at, at),
            )
            claimed = cursor.rowcount == 1
            await cursor.close()
            cursor = await conn.execute("SELECT * FROM launch_routes WHERE operation_id = ?", (operation_id,))
            row = await cursor.fetchone()
            await cursor.close()
        assert row is not None
        return claimed, _record(row)

    async def save(self, record: RouteRecord) -> None:
        """Write the record as it now stands; only its claimant can, and a final record never changes."""
        record.updated_at = _now()
        await self.db.execute(
            "UPDATE launch_routes SET state = ?,selected_harness = ?,selected_model = ?,reason = ?,attempts_json = ?,"
            " result_json = ?,updated_at = ? WHERE operation_id = ? AND claim_token = ? AND state = 'claimed'",
            (record.state, record.selected_harness, record.selected_model, record.reason[:2000],
             json.dumps(record.attempts), json.dumps(record.result), record.updated_at,
             record.operation_id, record.claim_token),
        )

    async def latest_for_task(self, task_id: str) -> RouteRecord | None:
        row = await self.db.fetchone("SELECT * FROM launch_routes WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (task_id,))
        return _record(row) if row is not None else None

    async def settle_interrupted(self) -> list[RouteRecord]:
        """At start, before any launch of this process: every claim left open belongs to a host that
        stopped mid-way. Each becomes ``admission_unknown`` with what is known about it, and is never
        retried. Whether its launch command committed is read from the command's own receipt."""
        settled: list[RouteRecord] = []
        for row in await self.db.fetchall("SELECT * FROM launch_routes WHERE state = 'claimed'"):
            record = _record(row)
            receipt = await self.db.fetchone(
                "SELECT 1 FROM operation_receipts WHERE scope_kind = 'project' AND scope_id = ? AND actor_id = ?"
                " AND operation_kind = 'task.launch' AND client_operation_id = ?",
                (record.project_id, row["actor_id"], record.operation_id),
            )
            pending = next((a for a in reversed(record.attempts) if a.get("outcome") == "claimed"), None)
            if pending is not None:
                pending["outcome"] = "admission_unknown"
            record.state = "admission_unknown"
            record.reason = ((record.reason + " ") if record.reason else "") + (
                "The host stopped after this launch was queued; it was not launched again — see the task's attempt."
                if receipt is not None else
                "The host stopped before this launch was recorded as queued; nothing was launched again — assign it once more.")
            await self.save(record)
            settled.append(record)
        return settled


__all__ = ["FINAL_STATES", "LaunchRoutes", "RouteRecord"]
