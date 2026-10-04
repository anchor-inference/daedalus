"""Serialize contained writers even when different projects name the same physical folder."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from daedalus.stores.control import ControlConflict, canonical, now, one
from daedalus.stores.executions import ACTIVE, ExecutionStore
from daedalus.stores.runtime_release import attempt_released_in

KEY = "contained_writer_lease"
OWNERS_KEY = "writer_effect_owners:"

# A native or ordinary CLI worker has no attempt row while it prepares its folder. Keep a
# contained claim from entering that gap in this host process. A restart still needs physical
# owner evidence; this lock deliberately makes no claim about abandoned external effects.
_startup_admission = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class WriterLease:
    project_id: str
    revision: int
    host_generation: int
    token: str


@dataclass(frozen=True, slots=True)
class WriterEffect:
    id: str
    kind: str
    owner_instance: str
    operation_id: str


class WriterLeases:
    """A global claim for contained CLI attempts with a kernel-owned process group.

    A missing attempt proves no provider entered, but interrupted file handoff or git preparation
    may still write. Registered host effects keep their own identity and cannot be freed by the
    worker's exit. Once preparation begins, unknown outcomes survive host restarts.
    """

    def __init__(self, executions: ExecutionStore) -> None:
        self.executions = executions
        self.db = executions.db

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    async def _read(conn: Any) -> dict[str, Any] | None:
        row = await one(conn, "SELECT value FROM kv WHERE key = ?", (KEY,))
        return json.loads(row["value"]) if row is not None else None

    @staticmethod
    async def _write(conn: Any, record: dict[str, Any]) -> None:
        await conn.execute("INSERT INTO kv(key,value) VALUES (?,?)"
                           " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY, canonical(record)))

    @staticmethod
    async def _owners(conn: Any, record: dict[str, Any]) -> list[dict[str, Any]]:
        row = await one(conn, "SELECT value FROM kv WHERE key = ?", (OWNERS_KEY + record["token_hash"],))
        return json.loads(row["value"]) if row is not None else []

    @staticmethod
    async def _write_owners(conn: Any, record: dict[str, Any], owners: list[dict[str, Any]]) -> None:
        await conn.execute("INSERT INTO kv(key,value) VALUES (?,?)"
                           " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (OWNERS_KEY + record["token_hash"], canonical(owners)))

    @classmethod
    def _matches(cls, record: dict[str, Any] | None, lease: WriterLease) -> bool:
        return bool(record and record["state"] == "held" and record["project_id"] == lease.project_id
                    and record["revision"] == lease.revision and record["host_generation"] == lease.host_generation
                    and record["token_hash"] == cls._hash(lease.token))

    @staticmethod
    async def refuse_uncontained_in(conn: Any) -> None:
        """Stop an uncontained writable launch while a contained writer claim is held."""
        record = await WriterLeases._read(conn)
        if record is not None and record["state"] == "held":
            raise ControlConflict("a contained writer still owns writable project folders")

    async def refuse_uncontained(self) -> None:
        async with self.db.transaction() as conn:
            await self.refuse_uncontained_in(conn)

    @asynccontextmanager
    async def uncontained_start(self) -> AsyncIterator[None]:
        """Serialize uncontained preparation against a contained claim until startup settles."""
        async with _startup_admission:
            await self.refuse_uncontained()
            yield

    async def acquire(self, project_id: str) -> WriterLease:
        async with _startup_admission:
            return await self._acquire(project_id)

    async def _acquire(self, project_id: str) -> WriterLease:
        token = secrets.token_urlsafe(32)
        async with self.db.transaction() as conn:
            generation = await self.executions._host(conn)
            record = await self._read(conn)
            if record is not None and record["state"] == "held":
                # A pre-preparation reservation may be reclaimed after host restart. Once anything
                # could have written, only exact no-entry or whole-container exit proof can free it.
                if record.get("handoffs") or await self._owners(conn, record):
                    released = False
                elif record["attempt_id"]:
                    released = await attempt_released_in(conn, record["attempt_id"])
                else:
                    released = not record["effects_started"] and record["host_generation"] != generation
                if not released:
                    raise ControlConflict("a contained writer still owns writable project folders")
            # An attempt launched before this gate was installed has no claim row. Its cgroup
            # remains an owner until the same exact release proof is durable.
            async with conn.execute("SELECT attempt_id FROM attempt_resource_bindings"
                                    " UNION ALL SELECT attempt_id FROM writer_attempt_bindings") as cursor:
                existing = await cursor.fetchall()
            for row in existing:
                if not await attempt_released_in(conn, row["attempt_id"]):
                    raise ControlConflict("an earlier contained worker has not proved its exit")
            # An ordinary native or worktree CLI attempt has no kernel process-owner proof.
            # Its active row is enough to refuse a new contained writer, even across projects.
            states = (*ACTIVE, "recovering")
            active = await one(conn, "SELECT 1 FROM execution_attempts a"
                               " JOIN staff_sessions s ON s.id = a.staff_session_id"
                               " JOIN staff m ON m.id = s.staff_id"
                               " WHERE a.state IN (?,?,?,?,?) AND m.isolation != 'readonly'"
                               " AND NOT EXISTS (SELECT 1 FROM attempt_resource_bindings r WHERE r.attempt_id = a.id)"
                               " AND NOT EXISTS (SELECT 1 FROM writer_attempt_bindings w WHERE w.attempt_id = a.id)"
                               " LIMIT 1", states)
            if active is not None:
                raise ControlConflict("an uncontained writable worker has not ended")
            revision = int(record["revision"]) + 1 if record is not None else 1
            await self._write(conn, {"project_id": project_id, "revision": revision,
                                     "host_generation": generation, "token_hash": self._hash(token),
                                     "attempt_id": None, "effects_started": False, "handoffs": [],
                                     "state": "held", "changed_at": now()})
        return WriterLease(project_id, revision, generation, token)

    async def register_effect(self, lease: WriterLease, *, kind: str, owner_instance: str,
                              operation_id: str) -> WriterEffect:
        """Persist one owner's identity before external admission; retries keep the original effect.

        This records identity, not permission to replay an external effect. Command results are
        not barriers, so every registered owner stays held even after the bound worker's exit.
        """
        if kind not in ("process", "file") or any(not value or len(value) > 256
                                                  for value in (owner_instance, operation_id)):
            raise ValueError("an effect kind and bounded exact owner and operation identities are required")
        async with self.db.transaction() as conn:
            generation = await self.executions._host(conn)
            record = await self._read(conn)
            if generation != lease.host_generation or not self._matches(record, lease):
                raise ControlConflict("the writer claim changed before effect registration")
            owners = await self._owners(conn, record)
            for entry in owners:
                if entry["operation_id"] == operation_id:
                    if (entry["kind"], entry["owner_instance"]) != (kind, owner_instance):
                        raise ControlConflict("the effect operation changed its physical owner")
                    return WriterEffect(entry["id"], kind, owner_instance, operation_id)
            if len(owners) >= 256:
                raise ControlConflict("the writer claim has too many unresolved physical owners")
            effect = WriterEffect(secrets.token_urlsafe(24), kind, owner_instance, operation_id)
            owners.append({"id": effect.id, "kind": kind, "owner_instance": owner_instance,
                           "operation_id": operation_id, "state": "reserved", "result_digest": None,
                           "observed_at": now()})
            record["effects_started"] = True
            record["changed_at"] = now()
            await self._write_owners(conn, record, owners)
            await self._write(conn, record)
        return effect

    async def observe_effect(self, lease: WriterLease, effect: WriterEffect, *, state: str,
                             result_digest: str | None = None) -> None:
        """Record entry, revocation, a returned result, or uncertainty without certifying quiescence."""
        if state not in ("entered", "revoking", "returned", "unknown"):
            raise ValueError("only entry, revocation, command result and unknown observations are supported")
        if state == "returned":
            if result_digest is None or len(result_digest) != 64 or any(c not in "0123456789abcdef" for c in result_digest):
                raise ValueError("a returned effect requires the exact result digest")
        elif result_digest is not None:
            raise ValueError("only a returned result has a result digest")
        async with self.db.transaction() as conn:
            generation = await self.executions._host(conn)
            record = await self._read(conn)
            if generation != lease.host_generation or not self._matches(record, lease):
                raise ControlConflict("the effect observation belongs to an earlier writer claim")
            owners = await self._owners(conn, record)
            entry = next((entry for entry in owners if entry["id"] == effect.id), None)
            if entry is None or (entry["kind"], entry["owner_instance"], entry["operation_id"]) != (
                    effect.kind, effect.owner_instance, effect.operation_id):
                raise ControlConflict("the effect observation changed its physical owner")
            if entry["state"] == state and entry["result_digest"] == result_digest:
                return
            if (entry["state"] in ("returned", "unknown")
                    or (state == "returned" and entry["state"] not in ("entered", "revoking"))
                    or (state == "revoking" and (effect.kind != "file" or entry["state"] != "entered"))
                    or (state == "entered" and entry["state"] != "reserved")):
                raise ControlConflict("the effect observation changed an immutable outcome")
            entry["state"], entry["result_digest"], entry["observed_at"] = state, result_digest, now()
            await self._write_owners(conn, record, owners)

    async def enter_file_owner(self, lease: WriterLease, effect: WriterEffect) -> None:
        """Claim one local barrier object exactly once; a replay must not reopen admission."""
        async with self.db.transaction() as conn:
            owners, entry = await self._file_owner(conn, lease, effect)
            if entry["state"] != "reserved":
                raise ControlConflict("the local file owner already entered or has an unknown outcome")
            entry["state"], entry["observed_at"] = "entered", now()
            await self._write_owners(conn, {"token_hash": self._hash(lease.token)}, owners)

    async def check_file_admission(self, lease: WriterLease, effect: WriterEffect) -> None:
        """Reject stale or revoked admission before submitting another local write thread."""
        async with self.db.transaction() as conn:
            _, entry = await self._file_owner(conn, lease, effect)
            if entry["state"] != "entered":
                raise ControlConflict("the local file owner is no longer admitting mutations")

    async def _file_owner(self, conn: Any, lease: WriterLease,
                          effect: WriterEffect) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        generation = await self.executions._host(conn)
        record = await self._read(conn)
        if generation != lease.host_generation or not self._matches(record, lease):
            raise ControlConflict("the local file owner belongs to an earlier writer claim")
        owners = await self._owners(conn, record)
        entry = next((entry for entry in owners if entry["id"] == effect.id), None)
        if effect.kind != "file" or entry is None or (entry["kind"], entry["owner_instance"], entry["operation_id"]) != (
                effect.kind, effect.owner_instance, effect.operation_id):
            raise ControlConflict("the local file owner identity changed")
        return owners, entry

    async def finish_file_barrier(self, lease: WriterLease, effect: WriterEffect, receipt: dict[str, Any]) -> None:
        """Persist a local mutation barrier receipt; this still does not release the writer claim."""
        expected = {"effect_id": effect.id, "owner_instance": effect.owner_instance,
                    "project_id": lease.project_id, "claim_revision": lease.revision,
                    "host_generation": lease.host_generation}
        if (any(receipt.get(key) != value for key, value in expected.items())
                or set(receipt) != set(expected) | {"mutations"}
                or not isinstance(receipt["mutations"], list)
                or len(receipt["mutations"]) > 256
                or any(state not in ("written", "failed") for state in receipt["mutations"])):
            raise ValueError("the local barrier receipt must name this exact settled mutation frontier")
        digest = hashlib.sha256(canonical(receipt).encode()).hexdigest()
        async with self.db.transaction() as conn:
            owners, entry = await self._file_owner(conn, lease, effect)
            if entry["state"] != "revoking":
                raise ControlConflict("the local file owner was not durably revoked")
            entry["state"], entry["result_digest"], entry["observed_at"] = "returned", digest, now()
            await self._write_owners(conn, {"token_hash": self._hash(lease.token)}, owners)
            await conn.execute("INSERT INTO kv(key,value) VALUES (?,?)", ("local_file_barrier:" + effect.id, canonical(receipt)))

    async def effect_owners(self) -> list[dict[str, Any]]:
        """Inspect held physical owners after restart without exposing the claim's secret."""
        async with self.db.transaction() as conn:
            await self.executions._host(conn)
            record = await self._read(conn)
            return await self._owners(conn, record) if record is not None and record["state"] == "held" else []

    async def begin_effects(self, lease: WriterLease) -> None:
        """Cross the uncertain boundary before worktree preparation or file handoff."""
        async with self.db.transaction() as conn:
            await self.executions._host(conn)
            record = await self._read(conn)
            if not self._matches(record, lease) or record["effects_started"]:
                raise ControlConflict("the writer lease changed before preparation")
            record["effects_started"] = True
            record["changed_at"] = now()
            await self._write(conn, record)

    async def bind(self, lease: WriterLease, attempt_id: str) -> None:
        """Bind before runtime entry; only an exact CLI containment binding is accepted."""
        async with self.db.transaction() as conn:
            await self.executions._host(conn)
            row = await one(conn, "SELECT a.runtime_kind,r.state,r.host_generation FROM execution_attempts a"
                            " JOIN (SELECT attempt_id,state,host_generation FROM attempt_resource_bindings"
                            " UNION ALL SELECT attempt_id,state,host_generation FROM writer_attempt_bindings) r"
                            " ON r.attempt_id = a.id"
                            " WHERE a.id = ? AND a.task_id IN (SELECT id FROM board_tasks WHERE project_id = ?)",
                            (attempt_id, lease.project_id))
            if row is None or row["runtime_kind"] != "cli" or row["state"] != "reserved" or int(row["host_generation"]) != lease.host_generation:
                raise ControlConflict("the writer has no exact CLI containment binding")
            record = await self._read(conn)
            if not self._matches(record, lease) or not record["effects_started"] or record["attempt_id"]:
                raise ControlConflict("the writer lease changed before runtime entry")
            record["attempt_id"] = attempt_id
            record["changed_at"] = now()
            await self._write(conn, record)

    async def begin_handoff(self, attempt_id: str) -> str:
        """Reserve a host file copy against the exact still-running contained attempt."""
        handoff = secrets.token_urlsafe(24)
        async with self.db.transaction() as conn:
            generation = await self.executions._host(conn)
            record = await self._read(conn)
            if (record is None or record["state"] != "held" or record["host_generation"] != generation
                    or record["attempt_id"] != attempt_id or await attempt_released_in(conn, attempt_id)):
                raise ControlConflict("the contained writer no longer owns its file handoff")
            record["handoffs"].append(handoff)
            record["changed_at"] = now()
            await self._write(conn, record)
        return handoff

    async def finish_handoff(self, attempt_id: str, handoff: str) -> None:
        """A completed awaited copy can be removed; an interrupted copy stays unknown."""
        async with self.db.transaction() as conn:
            generation = await self.executions._host(conn)
            record = await self._read(conn)
            if (record is None or record["state"] != "held" or record["host_generation"] != generation
                    or record["attempt_id"] != attempt_id or handoff not in record["handoffs"]):
                raise ControlConflict("the file handoff no longer belongs to this writer")
            record["handoffs"].remove(handoff)
            record["changed_at"] = now()
            await self._write(conn, record)

    async def release_if_safe(self, lease: WriterLease) -> bool:
        """Release only this generation's claim after exact exit proof or before any effect."""
        async with self.db.transaction() as conn:
            generation = await self.executions._host(conn)
            if generation != lease.host_generation:
                return False
            record = await self._read(conn)
            if not self._matches(record, lease):
                return False
            if record.get("handoffs") or await self._owners(conn, record):
                return False
            if record["attempt_id"]:
                if not await attempt_released_in(conn, record["attempt_id"]):
                    return False
            elif record["effects_started"]:
                return False
            record["state"] = "released"
            record["changed_at"] = now()
            await self._write(conn, record)
            return True
