"""Durable approval for a committed effect's exact payload and artifact revision."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from daedalus.stores.control import (
    ControlConflict,
    ControlDenied,
    ControlStore,
    Entity,
    Principal,
    Scope,
    canonical,
    now,
    one,
)
from daedalus.stores.database import Database


class ApprovalPending(ControlDenied):
    """The effect is still waiting for its first operator approval."""


def _fingerprint(row: aiosqlite.Row) -> tuple[str, str, str]:
    envelope = json.loads(row["payload_json"])
    data = envelope.get("data")
    if not isinstance(data, dict) or not envelope.get("control", {}).get("approval_required"):
        raise ControlDenied("this effect has no approval gate")
    target = data.get("target")
    revision = data.get("artifact_revision")
    if target is None or not isinstance(revision, str) or not revision:
        raise ControlDenied("the effect has no immutable target and artifact revision")
    operation = hashlib.sha256(canonical({"kind": row["kind"], "receipt_id": row["receipt_id"],
                                          "payload": envelope}).encode()).hexdigest()
    target_digest = hashlib.sha256(canonical(target).encode()).hexdigest()
    return operation, target_digest, revision


class EffectApprovals:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.control = ControlStore(db)

    async def approve(self, effect_id: str, approver: Principal, *, expected_artifact_revision: str,
                      expires_at: str, client_operation_id: str, expected_collection_revision: int) -> dict[str, Any]:
        if approver.origin_class != "operator":
            raise ControlDenied("only an authenticated operator can approve an effect")
        effect = await self.db.fetchone("SELECT e.*,r.scope_kind,r.scope_id FROM effect_outbox e"
                                        " JOIN operation_receipts r ON r.id=e.receipt_id WHERE e.id=?", (effect_id,))
        if effect is None:
            raise KeyError(effect_id)
        scope = Scope(effect["scope_kind"], effect["scope_id"])
        expires = datetime.fromisoformat(expires_at)
        if expires.tzinfo is None or expires <= datetime.now(UTC):
            raise ValueError("a future timezone-aware expiry is required")

        async def commit(conn: aiosqlite.Connection, mutation: Any) -> dict[str, Any]:
            row = await one(conn, "SELECT e.*,r.scope_kind,r.scope_id,r.actor_id FROM effect_outbox e"
                            " JOIN operation_receipts r ON r.id=e.receipt_id WHERE e.id=?", (effect_id,))
            if row is None or row["state"] != "pending" or (row["scope_kind"], row["scope_id"]) != (scope.kind, scope.id):
                raise ControlConflict("the effect is no longer pending in this scope")
            operation, target, revision = _fingerprint(row)
            if revision != expected_artifact_revision:
                raise ControlConflict("the artifact revision has changed")
            approval_id = uuid.uuid4().hex
            try:
                await conn.execute(
                    "INSERT INTO effect_approvals(id,effect_id,operation_digest,actor_id,target_digest,"
                    "artifact_revision,scope_json,expires_at,approved_by,receipt_id,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (approval_id, effect_id, operation, row["actor_id"], target, revision,
                     canonical({"kind": scope.kind, "id": scope.id}), expires.astimezone(UTC).isoformat(),
                     approver.actor_id, mutation.receipt_id, now()),
                )
            except aiosqlite.IntegrityError as exc:
                raise ControlConflict("this exact effect was already approved") from exc
            return {"approval_id": approval_id, "effect_digest": operation, "state": "approved"}

        result = await self.control.mutate(approver, scope, "effect.approve", client_operation_id,
                                           expected_collection_revision, Entity("collection", scope.id),
                                           {"effect_id": effect_id, "expected_artifact_revision": expected_artifact_revision,
                                            "expires_at": expires_at}, commit)
        # Receipt replay must not present a revoked or changed approval as newly usable.
        async with self.db.transaction() as conn:
            current = await one(conn, "SELECT * FROM effect_outbox WHERE id=?", (effect_id,))
            if current is None:
                raise ControlConflict("the effect no longer exists")
            try:
                await self.check(conn, current)
            except ControlDenied as exc:
                raise ControlConflict(str(exc)) from exc
        return result

    async def revoke(self, approval_id: str, approver: Principal, *, client_operation_id: str,
                     expected_collection_revision: int) -> dict[str, Any]:
        if approver.origin_class != "operator":
            raise ControlDenied("only an authenticated operator can revoke an effect approval")
        row = await self.db.fetchone("SELECT scope_json FROM effect_approvals WHERE id=?", (approval_id,))
        if row is None:
            raise KeyError(approval_id)
        scope_data = json.loads(row["scope_json"])
        scope = Scope(scope_data["kind"], scope_data["id"])

        async def commit(conn: aiosqlite.Connection, _: Any) -> dict[str, Any]:
            row = await one(conn, "SELECT scope_json,revoked_at FROM effect_approvals WHERE id=?", (approval_id,))
            if row is None:
                raise KeyError(approval_id)
            if json.loads(row["scope_json"]) != scope_data:
                raise ControlConflict("the approval scope changed")
            if row["revoked_at"]:
                raise ControlConflict("the approval was already revoked")
            await conn.execute("UPDATE effect_approvals SET revoked_at=? WHERE id=?", (now(), approval_id))
            return {"approval_id": approval_id, "state": "revoked"}

        async with self.db.authority_effect_lock(scope.kind, scope.id):
            return await self.control.mutate(approver, scope, "effect.revoke", client_operation_id,
                                             expected_collection_revision, Entity("collection", scope.id),
                                             {"approval_id": approval_id}, commit)

    @staticmethod
    async def check(conn: aiosqlite.Connection, row: aiosqlite.Row) -> None:
        operation, target, revision = _fingerprint(row)
        receipt = await one(conn, "SELECT actor_id,scope_kind,scope_id FROM operation_receipts WHERE id=?", (row["receipt_id"],))
        if receipt is None:
            raise ControlDenied("the approval's command is missing")
        approval = await one(conn, "SELECT * FROM effect_approvals WHERE effect_id=? AND operation_digest=?"
                             " AND actor_id=? AND target_digest=? AND artifact_revision=? AND revoked_at IS NULL",
                             (row["id"], operation, receipt["actor_id"], target, revision))
        if approval is None:
            prior = await one(conn, "SELECT 1 FROM effect_approvals WHERE effect_id=?", (row["id"],))
            if prior is None:
                raise ApprovalPending("waiting for approval of the exact effect and artifact revision")
            raise ControlDenied("the effect approval was revoked or no longer matches")
        if json.loads(approval["scope_json"]) != {"kind": receipt["scope_kind"], "id": receipt["scope_id"]}:
            raise ControlDenied("the approval belongs to another scope")
        if datetime.fromisoformat(approval["expires_at"]) <= datetime.now(UTC):
            raise ControlDenied("the effect approval has expired")
