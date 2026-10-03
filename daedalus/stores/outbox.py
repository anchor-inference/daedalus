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

    async def _authorize(self, conn: aiosqlite.Connection, row: aiosqlite.Row) -> Claim:
        receipt = await one(conn, "SELECT * FROM operation_receipts WHERE id = ?", (row["receipt_id"],))
        if receipt is None:
            raise ControlDenied("the effect has no committed command")
        envelope = json.loads(row["payload_json"])
        metadata = envelope["control"]
        scope = Scope(receipt["scope_kind"], receipt["scope_id"])
        if row["grant_id"]:
            grant = await one(conn, "SELECT origin_class FROM actor_grants WHERE id = ?", (row["grant_id"],))
            if grant is None:
                raise ControlDenied("the effect's authority no longer exists")
            principal = Principal(receipt["actor_id"], grant["origin_class"], row["grant_id"], row["grant_generation"])
        else:
            if not receipt["actor_id"].startswith("operator:"):
                raise ControlDenied("an ungranted actor cannot dispatch an effect")
            principal = Principal(receipt["actor_id"], "operator")
        await self.control.authorize(conn, principal, scope, metadata["operation"], task_id=metadata["task_id"], effects=tuple(metadata["effects"]))
        if row["attempt_id"]:
            attempt = await one(conn, "SELECT a.task_id,a.state,a.host_generation,t.current_attempt_id FROM execution_attempts a JOIN board_tasks t ON t.id=a.task_id WHERE a.id = ?", (row["attempt_id"],))
            host = await one(conn, "SELECT value FROM kv WHERE key = 'execution_host_generation'")
            if attempt is None or attempt["task_id"] != metadata["task_id"] or attempt["current_attempt_id"] != row["attempt_id"]:
                raise ControlDenied("the execution attempt has been superseded")
            if attempt["state"] not in ("queued", "starting", "running", "waiting") or host is None or int(json.loads(host["value"])) != attempt["host_generation"]:
                raise ControlDenied("the execution attempt is no longer active on this host generation")
        return Claim(row["id"], row["receipt_id"], row["kind"], int(row["claim_generation"]), principal, scope, metadata["task_id"], row["attempt_id"], tuple(metadata["effects"]), metadata["operation"], envelope["data"])

    async def claim(self, kinds: tuple[str, ...]) -> Claim | None:
        if not kinds:
            return None
        async with self.db.transaction() as conn:
            placeholders = ",".join("?" for _ in kinds)
            async with conn.execute(f"SELECT * FROM effect_outbox WHERE state = 'pending' AND kind IN ({placeholders}) ORDER BY created_at,id", kinds) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                try:
                    await self._authorize(conn, row)
                except (ControlDenied, KeyError) as exc:
                    await conn.execute("UPDATE effect_outbox SET state = 'cancelled',error = ?,completed_at = ? WHERE id = ? AND state = 'pending'", (str(exc), now(), row["id"]))
                    continue
                await conn.execute("UPDATE effect_outbox SET state = 'claimed',claim_generation = claim_generation + 1,claimed_at = ? WHERE id = ? AND state = 'pending'", (now(), row["id"]))
                claimed = await one(conn, "SELECT * FROM effect_outbox WHERE id = ?", (row["id"],))
                assert claimed is not None
                return await self._authorize(conn, claimed)
        return None

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
        return dict(row)
