"""Fenced delivery of committed effects, with an explicit unknown outcome after a crash.

An interrupted effect is reconciled by its own handler. Moving it straight back to pending
would silently repeat a merge, send or deployment that may already have happened.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import aiosqlite

from daedalus.stores.control import ControlDenied, ControlStore, Mutation, Principal, Scope, canonical, now, one
from daedalus.stores.database import Database


@dataclass(frozen=True, slots=True)
class Claim:
    id: str
    receipt_id: str
    kind: str
    generation: int
    principal: Principal
    scope: Scope
    task_id: str | None
    attempt_id: str | None
    effects: tuple[str, ...]
    operation: str
    payload: dict[str, Any]


class OutboxStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.control = ControlStore(db)

    @staticmethod
    async def enqueue(conn: aiosqlite.Connection, mutation: Mutation, principal: Principal, *, kind: str, operation: str, payload: dict[str, Any], effects: tuple[str, ...] = (), task_id: str | None = None, attempt_id: str | None = None) -> str:
        if not kind or not operation or not isinstance(payload, dict):
            raise ValueError("an effect kind, operation and payload are required")
        action_id = uuid.uuid5(uuid.NAMESPACE_URL, f"effect:{mutation.receipt_id}:{kind}").hex
        envelope = {"data": payload, "control": {"task_id": task_id, "effects": list(effects), "operation": operation}}
        await conn.execute(
            "INSERT INTO effect_outbox(id,receipt_id,grant_id,grant_generation,attempt_id,kind,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (action_id, mutation.receipt_id, principal.grant_id, principal.grant_generation, attempt_id, kind, canonical(envelope), now()),
        )
        return action_id

    async def _decode(self, conn: aiosqlite.Connection, row: aiosqlite.Row) -> Claim:
        receipt = await one(conn, "SELECT * FROM operation_receipts WHERE id = ?", (row["receipt_id"],))
        if receipt is None:
            raise ControlDenied("the effect has no committed command")
        envelope = json.loads(row["payload_json"])
        metadata = envelope["control"]
        if metadata["operation"] != receipt["operation_kind"]:
            raise ControlDenied("the effect does not belong to its committed operation")
        scope = Scope(receipt["scope_kind"], receipt["scope_id"])
        if row["grant_id"]:
            grant = await one(conn, "SELECT origin_class FROM actor_grants WHERE id = ?", (row["grant_id"],))
            principal = Principal(receipt["actor_id"], grant["origin_class"] if grant is not None else "system", row["grant_id"], row["grant_generation"])
        else:
            principal = Principal(receipt["actor_id"], "operator" if receipt["actor_id"].startswith("operator:") else "system")
        return Claim(row["id"], row["receipt_id"], row["kind"], int(row["claim_generation"]), principal, scope, metadata["task_id"], row["attempt_id"], tuple(metadata["effects"]), metadata["operation"], envelope["data"])

    async def _authorize(self, conn: aiosqlite.Connection, row: aiosqlite.Row) -> Claim:
        claim = await self._decode(conn, row)
        await self.control.authorize(conn, claim.principal, claim.scope, claim.operation, task_id=claim.task_id, effects=claim.effects)
        if row["attempt_id"]:
            attempt = await one(conn, "SELECT a.task_id,a.state,a.host_generation,t.current_attempt_id FROM execution_attempts a JOIN board_tasks t ON t.id=a.task_id WHERE a.id = ?", (row["attempt_id"],))
            host = await one(conn, "SELECT value FROM kv WHERE key = 'execution_host_generation'")
            if attempt is None or attempt["task_id"] != claim.task_id or attempt["current_attempt_id"] != row["attempt_id"]:
                raise ControlDenied("the execution attempt has been superseded")
            if attempt["state"] not in ("queued", "starting", "running", "waiting") or host is None or int(json.loads(host["value"])) != attempt["host_generation"]:
                raise ControlDenied("the execution attempt is no longer active on this host generation")
        return claim

    async def unknown(self, kinds: tuple[str, ...], *, limit: int = 100) -> list[Claim]:
        """Commands needing an observed outcome, even if permission to repeat them was revoked.

        These claims cannot pass ``check`` or reach ``run``. They allow a trusted handler to read
        the physical outcome of an earlier command, without authorizing another external effect.
        """
        if not kinds:
            return []
        async with self.db.transaction() as conn:
            placeholders = ",".join("?" for _ in kinds)
            async with conn.execute(f"SELECT * FROM effect_outbox WHERE state = 'unknown' AND kind IN ({placeholders}) ORDER BY created_at,id LIMIT ?", (*kinds, limit)) as cursor:
                rows = await cursor.fetchall()
            return [await self._decode(conn, row) for row in rows]

    async def claim(self, kinds: tuple[str, ...], *, exclude: tuple[str, ...] = ()) -> Claim | None:
        if not kinds:
            return None
        async with self.db.transaction() as conn:
            placeholders = ",".join("?" for _ in kinds)
            async with conn.execute(
                "SELECT e.*,r.scope_id AS receipt_scope_id,t.priority AS task_priority,"
                "t.project_id AS task_project_id FROM effect_outbox e"
                " JOIN operation_receipts r ON r.id = e.receipt_id"
                " LEFT JOIN board_tasks t ON t.id = json_extract(e.payload_json,'$.control.task_id')"
                f" WHERE e.state = 'pending' AND e.kind IN ({placeholders})", kinds,
            ) as cursor:
                rows = await cursor.fetchall()
            cursor_row = await one(conn, "SELECT value FROM kv WHERE key = 'effect_admission_cursor'")
            position = json.loads(cursor_row["value"]) if cursor_row else {"project": "", "foreground": 0}
            foreground = sorted((row for row in rows if row["kind"] != "task.launch"),
                                key=lambda row: ({"task.stop": 0, "review.merge": 1}.get(row["kind"], 2),
                                                 row["created_at"], row["id"]))
            launches = [row for row in rows if row["kind"] == "task.launch"]
            by_project: dict[str, list[aiosqlite.Row]] = {}
            for row in launches:
                project_id = row["task_project_id"] or row["receipt_scope_id"]
                by_project.setdefault(project_id, []).append(row)
            projects = sorted(by_project)
            previous = position.get("project", "")
            projects = [project for project in projects if project > previous] + [project for project in projects if project <= previous]
            launch_order = [row for project in projects for row in sorted(
                by_project[project], key=lambda item: (item["task_priority"] if item["task_priority"] is not None else 9,
                                                       item["created_at"], item["id"]),
            )]
            prefer_foreground = bool(foreground) and (position.get("foreground", 0) < 8 or not launches)
            ordered = foreground + launch_order if prefer_foreground else launch_order + foreground
            for row in ordered:
                if row["id"] in exclude:
                    continue
                try:
                    await self._authorize(conn, row)
                except (ControlDenied, KeyError) as exc:
                    await conn.execute("UPDATE effect_outbox SET state = 'cancelled',error = ?,completed_at = ? WHERE id = ? AND state = 'pending'", (str(exc), now(), row["id"]))
                    continue
                await conn.execute("UPDATE effect_outbox SET state = 'claimed',claim_generation = claim_generation + 1,claimed_at = ? WHERE id = ? AND state = 'pending'", (now(), row["id"]))
                next_position = {
                    "project": (row["task_project_id"] or row["receipt_scope_id"])
                    if row["kind"] == "task.launch" else previous,
                    "foreground": 0 if row["kind"] == "task.launch" else min(8, position.get("foreground", 0) + 1),
                }
                await conn.execute("INSERT INTO kv(key,value) VALUES ('effect_admission_cursor',?)"
                                   " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                   (canonical(next_position),))
                claimed = await one(conn, "SELECT * FROM effect_outbox WHERE id = ?", (row["id"],))
                assert claimed is not None
                return await self._authorize(conn, claimed)
        return None

    async def defer(self, claim: Claim, *, reason: str) -> bool:
        """Release a claim whose trusted handler has not attempted any external effect."""
        if not reason:
            raise ValueError("a deferred command must say what it waits for")
        async with self.db.transaction() as conn:
            row = await one(conn, "SELECT * FROM effect_outbox WHERE id = ?", (claim.id,))
            if row is None or row["state"] != "claimed" or row["claim_generation"] != claim.generation:
                return False
            try:
                await self._authorize(conn, row)
            except (ControlDenied, KeyError) as exc:
                await conn.execute("UPDATE effect_outbox SET state = 'cancelled',error = ?,completed_at = ? WHERE id = ?",
                                   (str(exc), now(), claim.id))
                return False
            await conn.execute("UPDATE effect_outbox SET state = 'pending',claimed_at = NULL,error = ? WHERE id = ? AND claim_generation = ?",
                               (reason, claim.id, claim.generation))
            return True

    async def check(self, claim: Claim) -> None:
        """Recheck ownership and authority immediately before the handler's external effect."""
        async with self.db.transaction() as conn:
            row = await one(conn, "SELECT * FROM effect_outbox WHERE id = ?", (claim.id,))
            if row is None or row["state"] != "claimed" or row["claim_generation"] != claim.generation:
                raise ControlDenied("the effect claim is no longer current")
            current = await self._authorize(conn, row)
            if current != claim:
                raise ControlDenied("the effect's command or authority changed")

    async def finish(self, claim: Claim, *, state: str, error: str | None = None) -> bool:
        if state not in ("completed", "failed", "unknown"):
            raise ValueError("an effect completes, fails or has an unknown outcome")
        async with self.db.transaction() as conn:
            row = await one(conn, "SELECT state,claim_generation FROM effect_outbox WHERE id = ?", (claim.id,))
            if row is None or row["state"] != "claimed" or row["claim_generation"] != claim.generation:
                await conn.execute("INSERT INTO quarantined_attempt_events(attempt_id,event_json,reason,created_at) VALUES (?,?,?,?)", (claim.attempt_id or f"effect:{claim.id}", canonical({"effect_id": claim.id, "generation": claim.generation, "state": state}), "stale effect completion", now()))
                return False
            await conn.execute("UPDATE effect_outbox SET state = ?,error = ?,completed_at = ? WHERE id = ? AND claim_generation = ?", (state, error, now(), claim.id, claim.generation))
            return True

    async def recover(self) -> int:
        """Quarantine interrupted claims; no claim is automatically delivered a second time."""
        async with self.db.transaction() as conn:
            cursor = await conn.execute("UPDATE effect_outbox SET state = 'unknown',error = 'effect interrupted; reconcile before retry',claim_generation = claim_generation + 1 WHERE state = 'claimed'")
            return cursor.rowcount

    async def reconcile(self, action_id: str, *, generation: int, state: str, evidence: dict[str, Any]) -> bool:
        """A trusted effect handler supplies proof of the observed outcome, not a narrative retry."""
        if state not in ("completed", "failed") or not evidence:
            raise ValueError("reconciliation requires a proven completed or failed outcome")
        async with self.db.transaction() as conn:
            row = await one(conn, "SELECT state,claim_generation FROM effect_outbox WHERE id = ?", (action_id,))
            if row is None or row["state"] != "unknown" or row["claim_generation"] != generation:
                return False
            await conn.execute("UPDATE effect_outbox SET state = ?,error = ?,completed_at = ? WHERE id = ? AND claim_generation = ?", (state, canonical({"reconciliation": evidence}), now(), action_id, generation))
            return True

    async def view(self, action_id: str) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT id,receipt_id,attempt_id,kind,state,claim_generation,created_at,claimed_at,completed_at,error FROM effect_outbox WHERE id = ?", (action_id,))
        if row is None:
            raise KeyError(action_id)
        view = dict(row)
        if row["kind"] != "task.launch":
            return view
        task = await self.db.fetchone(
            "SELECT t.id,t.project_id,t.priority FROM effect_outbox e JOIN board_tasks t"
            " ON t.id = json_extract(e.payload_json,'$.control.task_id') WHERE e.id = ?", (action_id,),
        )
        view.update({"task_id": task["id"] if task else None,
                     "priority": task["priority"] if task else None,
                     "wait_position": None, "wait_reason": None, "wait_detail": None})
        if row["state"] != "pending" or task is None:
            return view
        pending = await self.db.fetchall(
            "SELECT e.id,e.created_at,t.priority FROM effect_outbox e JOIN board_tasks t"
            " ON t.id = json_extract(e.payload_json,'$.control.task_id')"
            " WHERE e.state = 'pending' AND e.kind = 'task.launch' AND t.project_id = ?",
            (task["project_id"],),
        )
        ordered = sorted(pending, key=lambda item: (item["priority"], item["created_at"], item["id"]))
        view["wait_position"] = next((index for index, item in enumerate(ordered, 1) if item["id"] == action_id), None)
        if row["error"]:
            try:
                reason = json.loads(row["error"])
            except (TypeError, ValueError):
                reason = {"reason": "unknown", "detail": row["error"]}
            if isinstance(reason, dict):
                view["wait_reason"] = reason.get("reason")
                view["wait_detail"] = reason.get("detail")
        return view
