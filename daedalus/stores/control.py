"""Authorize and commit a domain command once, before dispatching external effects.

Callbacks write only through their supplied connection. Starting a process or making a
network request belongs in the outbox, because SQLite cannot roll back either effect.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import aiosqlite

from daedalus.stores.database import Database


def now() -> str:
    return datetime.now(UTC).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class ControlConflict(ValueError):
    """The command key was reused or the requested state has changed."""

    def __init__(self, reason: str, *, current_revision: int | None = None) -> None:
        super().__init__(reason)
        self.current_revision = current_revision


class ControlDenied(PermissionError):
    """The host has no current grant for this actor and effect."""


@dataclass(frozen=True, slots=True)
class Principal:
    """Host-attested identity; request bodies and tool output cannot construct authority."""

    actor_id: str
    origin_class: Literal["operator", "agent", "plugin", "system"]
    grant_id: str | None = None
    grant_generation: int | None = None

    @classmethod
    def operator(cls, authenticated: dict[str, Any]) -> Principal:
        if authenticated.get("via") not in ("telegram", "token", "cookie") or "user_id" not in authenticated:
            raise ControlDenied("an authenticated operator is required")
        return cls(f"operator:{int(authenticated['user_id'])}", "operator")


@dataclass(frozen=True, slots=True)
class Scope:
    kind: Literal["global", "project"]
    id: str

    def __post_init__(self) -> None:
        if self.kind not in ("global", "project") or not self.id or (self.kind == "global" and self.id != "global"):
            raise ValueError("invalid command scope")


@dataclass(frozen=True, slots=True)
class Entity:
    kind: Literal["task", "project", "collection"]
    id: str


@dataclass(frozen=True, slots=True)
class Mutation:
    receipt_id: str
    object_id: str
    entity_revision: int


Effect = Callable[[aiosqlite.Connection, Mutation], Awaitable[dict[str, Any]]]


async def one(conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
    async with conn.execute(sql, params) as cursor:
        return await cursor.fetchone()


class ControlStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def _entity(self, conn: aiosqlite.Connection, scope: Scope, entity: Entity) -> int:
        if entity.kind == "collection":
            if entity.id != scope.id:
                raise ControlDenied("collection does not belong to the command scope")
            row = await one(conn, "SELECT revision FROM domain_collection_revisions WHERE scope_kind = ? AND scope_id = ?", (scope.kind, scope.id))
            if row is None:
                raise KeyError(scope.id)
            return int(row["revision"])
        if entity.kind == "task":
            row = await one(conn, "SELECT project_id, entity_revision FROM board_tasks WHERE id = ?", (entity.id,))
            if row is None:
                raise KeyError(entity.id)
            if (scope.kind == "project" and row["project_id"] != scope.id) or (scope.kind == "global" and row["project_id"] is not None):
                raise ControlDenied("task does not belong to the command scope")
        elif entity.kind == "project":
            if scope.kind != "project" or entity.id != scope.id:
                raise ControlDenied("project does not belong to the command scope")
            row = await one(conn, "SELECT entity_revision FROM projects WHERE id = ?", (entity.id,))
            if row is None:
                raise KeyError(entity.id)
        else:
            raise ValueError("unknown entity kind")
        return int(row["entity_revision"])

    async def authorize(self, conn: aiosqlite.Connection, principal: Principal, scope: Scope, operation: str, *, task_id: str | None = None, effects: tuple[str, ...] = ()) -> None:
        if not principal.actor_id:
            raise ControlDenied("missing host identity")
        if scope.kind == "project" and await one(conn, "SELECT 1 FROM projects WHERE id = ?", (scope.id,)) is None:
            raise KeyError(scope.id)
        if principal.origin_class == "operator":
            return
        if not principal.grant_id or principal.grant_generation is None:
            raise ControlDenied("the actor has no host-issued grant")
        grant = await one(conn, "SELECT * FROM actor_grants WHERE id = ?", (principal.grant_id,))
        if grant is None or grant["actor_id"] != principal.actor_id or grant["origin_class"] != principal.origin_class:
            raise ControlDenied("grant identity does not match the actor")
        if grant["revoked_at"] or int(grant["generation"]) != principal.grant_generation:
            raise ControlDenied("grant was revoked or replaced")
        if datetime.fromisoformat(grant["expires_at"]) <= datetime.now(UTC):
            raise ControlDenied("grant has expired")
        if grant["scope_kind"] == "project" and (scope.kind != "project" or grant["scope_id"] != scope.id):
            raise ControlDenied("grant belongs to another project")
        if grant["scope_kind"] == "task" and (task_id != grant["task_id"] or grant["project_id"] != (scope.id if scope.kind == "project" else None)):
            raise ControlDenied("grant belongs to another task")
        if operation not in json.loads(grant["operations_json"]):
            raise ControlDenied("operation is outside the approved scope")
        if not set(effects) <= set(json.loads(grant["effects_json"])):
            raise ControlDenied("effect is outside the approved scope")
        if principal.origin_class == "agent" and principal.actor_id.startswith("orchestrator:"):
            if scope.kind != "project":
                raise ControlDenied("a coordinator acts only within its project")
            project = await one(conn, "SELECT settings FROM projects WHERE id = ?", (scope.id,))
            office = json.loads(project["settings"]).get("orchestrator", {}) if project else {}
            if not office.get("enabled") or office.get("session_id") != principal.actor_id.partition(":")[2]:
                raise ControlDenied("the coordinator was disabled or replaced")

    async def mutate(self, principal: Principal, scope: Scope, operation: str, client_operation_id: str, expected_revision: int, entity: Entity, payload: dict[str, Any], effect: Effect, *, effects: tuple[str, ...] = ()) -> dict[str, Any]:
        if not client_operation_id or len(client_operation_id) > 160 or not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,119}", operation):
            raise ValueError("a command identity and operation name are required")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise ValueError("a positive expected revision is required")
        payload_hash = digest({"entity": {"kind": entity.kind, "id": entity.id}, "expected_revision": expected_revision, "payload": payload, "effects": sorted(effects)})
        key = (scope.kind, scope.id, principal.actor_id, operation, client_operation_id)
        async with self.db.transaction() as conn:
            # Revoked authority cannot retrieve a previously successful command as an apparent new approval.
            await self.authorize(conn, principal, scope, operation, task_id=entity.id if entity.kind == "task" else None, effects=effects)
            receipt = await one(conn, "SELECT payload_hash, response_json FROM operation_receipts WHERE scope_kind = ? AND scope_id = ? AND actor_id = ? AND operation_kind = ? AND client_operation_id = ?", key)
            if receipt is not None:
                if receipt["payload_hash"] != payload_hash:
                    raise ControlConflict("the command identity was reused with a different request")
                return json.loads(receipt["response_json"])
            current = await self._entity(conn, scope, entity)
            if current != expected_revision:
                raise ControlConflict("the entity has changed", current_revision=current)
            object_id = uuid.uuid5(uuid.NAMESPACE_URL, canonical(key)).hex
            mutation = Mutation(uuid.uuid4().hex, object_id, current + 1)
            response = await effect(conn, mutation)
            if not isinstance(response, dict) or set(response) & {"receipt_id", "entity_revision"}:
                raise ValueError("the effect must return domain fields, without receipt metadata")
            if entity.kind == "collection":
                await conn.execute("UPDATE domain_collection_revisions SET revision = revision + 1 WHERE scope_kind = ? AND scope_id = ? AND revision = ?", (scope.kind, scope.id, current))
            else:
                table = "board_tasks" if entity.kind == "task" else "projects"
                await conn.execute(f"UPDATE {table} SET entity_revision = entity_revision + 1 WHERE id = ? AND entity_revision = ?", (entity.id, current))
            revision = await self._entity(conn, scope, entity)
            response = {**response, "receipt_id": mutation.receipt_id, "entity_revision": revision}
            await conn.execute(
                "INSERT INTO operation_receipts(id,scope_kind,scope_id,project_id,actor_id,operation_kind,client_operation_id,grant_id,request_entity_revision,payload_hash,entity_revision,state,response_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mutation.receipt_id, *key[:2], scope.id if scope.kind == "project" else None, *key[2:], principal.grant_id, expected_revision, payload_hash, revision, "committed", canonical(response), now()),
            )
            return response

    async def issue_grant(self, issuer: Principal, subject: Principal, scope: Scope, *, operations: list[str], effects: list[str], expires_at: str, task_id: str | None = None) -> dict[str, Any]:
        async with self.db.transaction() as conn:
            return await self.issue_grant_in(conn, issuer, subject, scope, operations=operations, effects=effects, expires_at=expires_at, task_id=task_id)

    async def issue_grant_in(self, conn: aiosqlite.Connection, issuer: Principal, subject: Principal, scope: Scope, *, operations: list[str], effects: list[str], expires_at: str, task_id: str | None = None) -> dict[str, Any]:
        """Issue within the caller's command so a refused attempt cannot leave a usable grant."""
        if issuer.origin_class != "operator" or subject.origin_class == "operator":
            raise ControlDenied("only an operator can issue non-operator authority")
        expires = datetime.fromisoformat(expires_at)
        if expires.tzinfo is None or expires <= datetime.now(UTC):
            raise ValueError("a future timezone-aware expiry is required")
        if not subject.actor_id or not operations or any(not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,119}", op) for op in operations + effects):
            raise ValueError("a subject and explicit operations are required")
        await self.authorize(conn, issuer, scope, "grant.issue")
        if task_id is not None:
            await self._entity(conn, scope, Entity("task", task_id))
        grant_id = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO actor_grants(id,actor_id,origin_class,issuer_id,scope_kind,scope_id,project_id,task_id,operations_json,effects_json,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (grant_id, subject.actor_id, subject.origin_class, issuer.actor_id, "task" if task_id else scope.kind, task_id or scope.id, scope.id if scope.kind == "project" else None, task_id, canonical(sorted(set(operations))), canonical(sorted(set(effects))), expires.astimezone(UTC).isoformat(), now()),
        )
        await conn.execute("INSERT INTO grant_events(grant_id,actor_id,generation,event,at) VALUES (?,?,1,'issued',?)", (grant_id, issuer.actor_id, now()))
        return {"grant_id": grant_id, "generation": 1, "scope": {"kind": "task" if task_id else scope.kind, "id": task_id or scope.id}, "operations": sorted(set(operations)), "effects": sorted(set(effects)), "expires_at": expires.astimezone(UTC).isoformat()}

    async def revoke_grant(self, issuer: Principal, grant_id: str, *, reason: str) -> None:
        if issuer.origin_class != "operator":
            raise ControlDenied("only an operator can revoke authority")
        if not reason.strip():
            raise ValueError("a revocation reason is required")
        async with self.db.transaction() as conn:
            grant = await one(conn, "SELECT generation,revoked_at FROM actor_grants WHERE id = ?", (grant_id,))
            if grant is None:
                raise KeyError(grant_id)
            if grant["revoked_at"]:
                return
            generation = int(grant["generation"]) + 1
            await conn.execute("UPDATE actor_grants SET revoked_at = ?,generation = ? WHERE id = ?", (now(), generation, grant_id))
            await conn.execute("INSERT INTO grant_events(grant_id,actor_id,generation,event,reason,at) VALUES (?,?,?,'revoked',?,?)", (grant_id, issuer.actor_id, generation, reason, now()))
            await conn.execute("UPDATE effect_outbox SET state = 'cancelled',error = ? WHERE grant_id = ? AND state = 'pending'", (reason, grant_id))

    async def revision(self, scope: Scope, entity: Entity) -> int:
        async with self.db.transaction() as conn:
            return await self._entity(conn, scope, entity)
