"""Cancel only children with explicit, current ownership edges.

The command freezes admission in SQLite before any runtime stop request. A stop request is not
proof of exit; uncertain children remain visible as unknown until a matching runtime is observed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Principal, Scope, one
from daedalus.stores.lifecycle import LifecycleRefused, record_owned_exit, record_owned_no_entry
from daedalus.stores.outbox import Claim, OutboxStore

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Lifecycle:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def preview(self, parent_kind: str, parent_id: str) -> dict[str, Any]:
        project_id, source_revision = await self._source(parent_kind, parent_id)
        async with self.app.db.transaction() as conn:
            source = await one(conn, "SELECT goal_revision revision FROM projects WHERE id = ?"
                               if parent_kind == "project_goal" else
                               "SELECT contract_revision revision FROM board_tasks WHERE id = ?", (parent_id,))
            if source is None:
                raise KeyError(parent_id)
            source_revision = source["revision"]
            parent = await one(conn, "SELECT * FROM lifecycle_parents WHERE parent_kind = ? AND parent_id = ?",
                               (parent_kind, parent_id))
            generation = parent["generation"] if parent else 1
            rows = await self._descendants(conn, parent_kind, parent_id, project_id, generation)
            revision = await ControlStore(self.app.db)._entity(
                conn, Scope("project", project_id),
                Entity("project", project_id) if parent_kind == "project_goal" else Entity("task", parent_id),
            )
        return {"parent_kind": parent_kind, "parent_id": parent_id, "project_id": project_id,
                "source_revision": source_revision, "generation": generation,
                "entity_revision": revision,
                "cancel_state": parent["cancel_state"] if parent else "active",
                "children": [row for row in rows if row["parent_kind"] == parent_kind and row["parent_id"] == parent_id],
                "owned_descendants": rows,
                "preview_fingerprint": self._fingerprint(parent_kind, parent_id, source_revision, generation, rows)}

    @staticmethod
    def _fingerprint(kind: str, identity: str, source_revision: int, generation: int,
                     rows: list[dict[str, Any]]) -> str:
        value = {"parent_kind": kind, "parent_id": identity, "source_revision": source_revision,
                 "generation": generation, "owned_descendants": rows}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    async def _descendants(self, conn: Any, parent_kind: str, parent_id: str,
                           project_id: str, generation: int) -> list[dict[str, Any]]:
        pending = [(parent_kind, parent_id, generation)]
        seen: set[tuple[str, str, int]] = set()
        owned = []
        while pending:
            kind, identity, version = pending.pop(0)
            if (kind, identity, version) in seen or len(seen) >= 256:
                raise LifecycleRefused("ownership graph is cyclic or exceeds its bound")
            seen.add((kind, identity, version))
            async with conn.execute(
                "SELECT * FROM lifecycle_owners WHERE parent_kind = ? AND parent_id = ? AND generation = ?"
                " AND cancel_state NOT IN ('drained','transferred') ORDER BY child_kind,child_id",
                (kind, identity, version),
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                if row["project_id"] != project_id:
                    raise LifecycleRefused("owned child crossed a project boundary")
                owned.append({key: row[key] for key in (
                    "parent_kind", "parent_id", "child_kind", "child_id", "generation", "source_revision", "cancel_state",
                )})
                if row["child_kind"] == "task":
                    child = await one(conn, "SELECT contract_revision FROM board_tasks WHERE id = ?", (row["child_id"],))
                    if child is None:
                        raise LifecycleRefused("owned task no longer exists")
                    parent = await one(conn, "SELECT * FROM lifecycle_parents WHERE parent_kind = 'task' AND parent_id = ?",
                                       (row["child_id"],))
                    if parent is not None and (parent["project_id"] != project_id or parent["contract_revision"] != child["contract_revision"]):
                        raise LifecycleRefused("child task contract changed before cancellation")
                    pending.append(("task", row["child_id"], parent["generation"] if parent else 1))
        return owned

    async def _source(self, parent_kind: str, parent_id: str) -> tuple[str, int]:
        if parent_kind == "project_goal":
            project = await self.app.db.fetchone("SELECT goal_revision FROM projects WHERE id = ?", (parent_id,))
            if project is None:
                raise KeyError(parent_id)
            return parent_id, project["goal_revision"]
        if parent_kind == "task":
            task = await self.app.db.fetchone(
                "SELECT project_id,contract_revision FROM board_tasks WHERE id = ?", (parent_id,),
            )
            if task is None or not task["project_id"]:
                raise KeyError(parent_id)
            return task["project_id"], task["contract_revision"]
        raise LifecycleRefused("unsupported parent kind")

    async def cancel_command(
        self, principal: Principal, parent_kind: str, parent_id: str, reason: str, *,
        expected_entity_revision: int, expected_source_revision: int, client_operation_id: str,
        preview_fingerprint: str,
    ) -> dict[str, Any]:
        if not 1 <= len(reason.strip()) <= 1000:
            raise LifecycleRefused("cancellation needs a bounded reason")
        project_id, _ = await self._source(parent_kind, parent_id)
        entity = Entity("project", project_id) if parent_kind == "project_goal" else Entity("task", parent_id)

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            source = await one(
                conn, "SELECT goal_revision revision FROM projects WHERE id = ?" if parent_kind == "project_goal"
                else "SELECT contract_revision revision FROM board_tasks WHERE id = ?", (parent_id,),
            )
            if source is None or source["revision"] != expected_source_revision:
                raise ControlConflict("parent source revision changed")
            parent = await one(conn, "SELECT * FROM lifecycle_parents WHERE parent_kind = ? AND parent_id = ?", (parent_kind, parent_id))
            generation = parent["generation"] if parent else 1
            rows = await self._descendants(conn, parent_kind, parent_id, project_id, generation)
            observed = self._fingerprint(parent_kind, parent_id, expected_source_revision, generation, rows)
            if not preview_fingerprint or observed != preview_fingerprint:
                raise ControlConflict("owned work changed after the cancellation preview")
            at = _now()
            if parent is None:
                generation = 1
                await conn.execute(
                    "INSERT INTO lifecycle_parents(parent_kind,parent_id,project_id,generation,cancel_state,updated_at,goal_revision,contract_revision) "
                    "VALUES (?,?,?,1,'requested',?,?,?)",
                    (parent_kind, parent_id, project_id, at,
                     expected_source_revision if parent_kind == "project_goal" else None,
                     expected_source_revision if parent_kind == "task" else None),
                )
            else:
                pinned = parent["goal_revision"] if parent_kind == "project_goal" else parent["contract_revision"]
                if parent["project_id"] != project_id or pinned != expected_source_revision or parent["cancel_state"] != "active":
                    raise LifecycleRefused("parent source or cancellation state changed")
                generation = parent["generation"]
                await conn.execute(
                    "UPDATE lifecycle_parents SET cancel_state = 'requested',updated_at = ? "
                    "WHERE parent_kind = ? AND parent_id = ? AND generation = ?",
                    (at, parent_kind, parent_id, generation),
                )
            rows = await self._freeze_children(conn, parent_kind, parent_id, project_id, generation)
            waiting_tasks = {row["child_id"] for row in rows if row["child_kind"] == "task"}
            if parent_kind == "task":
                waiting_tasks.add(parent_id)
            for task_id in waiting_tasks:
                # Pending launches have not contacted a provider. Claimed or unknown ones need the
                # exact attempt fence and exit observation; cancellation cannot infer their absence.
                await conn.execute("UPDATE effect_outbox SET state = 'cancelled',error = ?,completed_at = ?"
                                   " WHERE kind = 'task.launch' AND state = 'pending'"
                                   " AND receipt_id IN (SELECT id FROM operation_receipts WHERE scope_kind = 'project' AND scope_id = ?)"
                                   " AND json_extract(payload_json,'$.control.task_id') = ?",
                                   (reason.strip(), at, project_id, task_id))
            await OutboxStore.enqueue(
                conn, mutation, principal, kind="lifecycle.stop", operation="lifecycle.cancel",
                payload={"parent_kind": parent_kind, "parent_id": parent_id, "project_id": project_id,
                         "generation": generation, "attempts": [
                             {key: row[key] for key in ("parent_kind", "parent_id", "generation", "source_revision", "child_id")}
                             for row in rows if row["child_kind"] == "execution_attempt"]},
                effects=("lifecycle.stop",), task_id=parent_id if parent_kind == "task" else None,
            )
            return {"parent_kind": parent_kind, "parent_id": parent_id,
                    "cancel_state": "requested", "generation": generation,
                    "children": [{"kind": row["child_kind"], "id": row["child_id"], "status": "draining"} for row in rows]}

        response = await ControlStore(self.app.db).mutate(
            principal, Scope("project", project_id), "lifecycle.cancel", client_operation_id,
            expected_entity_revision, entity,
            {"parent_kind": parent_kind, "parent_id": parent_id, "expected_source_revision": expected_source_revision,
             "reason": reason.strip(), "preview_fingerprint": preview_fingerprint}, effect, effects=("lifecycle.stop",),
        )
        self.app.extensions["effects"].notify()
        return response

    async def _freeze_children(
        self, conn: Any, parent_kind: str, parent_id: str, project_id: str, generation: int,
    ) -> list[dict[str, Any]]:
        """Walk only stored ownership edges; shared readers and inferred links are excluded."""
        pending = [(parent_kind, parent_id, generation)]
        seen: set[tuple[str, str, int]] = set()
        owned: list[dict[str, Any]] = []
        while pending:
            kind, identity, version = pending.pop(0)
            if (kind, identity, version) in seen or len(seen) >= 256:
                raise LifecycleRefused("ownership graph is cyclic or exceeds its bound")
            seen.add((kind, identity, version))
            cursor = await conn.execute(
                "SELECT * FROM lifecycle_owners WHERE parent_kind = ? AND parent_id = ? AND generation = ?",
                (kind, identity, version),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                if row["project_id"] != project_id:
                    raise LifecycleRefused("owned child crossed a project boundary")
                if row["cancel_state"] in {"drained", "transferred"}:
                    continue
                if row["cancel_state"] == "active":
                    await conn.execute(
                        "UPDATE lifecycle_owners SET cancel_state = 'requested',updated_at = ? "
                        "WHERE parent_kind = ? AND parent_id = ? AND child_kind = ? AND child_id = ? AND generation = ?",
                        (_now(), kind, identity, row["child_kind"], row["child_id"], row["generation"]),
                    )
                owned.append(dict(row))
                if row["child_kind"] == "task":
                    child = await one(conn, "SELECT contract_revision FROM board_tasks WHERE id = ?", (row["child_id"],))
                    if child is None:
                        continue
                    next_parent = await one(conn, "SELECT * FROM lifecycle_parents WHERE parent_kind = 'task' AND parent_id = ?", (row["child_id"],))
                    if next_parent is None:
                        await conn.execute(
                            "INSERT INTO lifecycle_parents(parent_kind,parent_id,project_id,generation,cancel_state,updated_at,contract_revision) "
                            "VALUES ('task',?,?,1,'requested',?,?)",
                            (row["child_id"], project_id, _now(), child["contract_revision"]),
                        )
                        pending.append(("task", row["child_id"], 1))
                    elif next_parent["project_id"] == project_id and next_parent["contract_revision"] == child["contract_revision"]:
                        await conn.execute(
                            "UPDATE lifecycle_parents SET cancel_state = 'requested',updated_at = ? "
                            "WHERE parent_kind = 'task' AND parent_id = ?",
                            (_now(), row["child_id"]),
                        )
                        pending.append(("task", row["child_id"], next_parent["generation"]))
                    else:
                        raise LifecycleRefused("child task contract changed before cancellation")
        return owned

    async def run(self, claim: Claim, check: Any) -> EffectOutcome:
        await check(claim)
        attempts = claim.payload["attempts"]
        unknown = False
        for owner in attempts:
            outcome = await self._stop_attempt(owner, claim, check)
            if outcome != "drained":
                unknown = True
        await self.drain_verified()
        return EffectOutcome("unknown", "owned runtime exit needs observation") if unknown else EffectOutcome("completed")

    async def _stop_attempt(self, owner: dict[str, Any], claim: Claim, check: Any) -> str:
        attempt_id = owner["child_id"]
        async with self.app.db.transaction() as conn:
            row = await one(
                conn, "SELECT a.id,a.task_id,a.state,a.host_generation,a.provider_session_ref,a.staff_session_id,"
                "a.runtime_kind,a.native_run_id,a.contract_revision,t.current_attempt_id,s.ended_at,s.session_id,s.terminal_id,"
                "o.cancel_state,o.source_revision "
                "FROM execution_attempts a JOIN board_tasks t ON t.id = a.task_id "
                "JOIN lifecycle_owners o ON o.child_kind = 'execution_attempt' AND o.child_id = a.id "
                "AND o.parent_kind = ? AND o.parent_id = ? AND o.generation = ? "
                "LEFT JOIN staff_sessions s ON s.id = a.staff_session_id WHERE a.id = ?",
                (owner["parent_kind"], owner["parent_id"], owner["generation"], attempt_id),
            )
            if (row is None or row["source_revision"] != owner["source_revision"]
                    or row["contract_revision"] != owner["source_revision"]
                    or row["cancel_state"] not in {"requested", "acknowledged", "unknown", "drained"}):
                return "unknown"
            if row["cancel_state"] == "drained":
                return "drained"
            if await record_owned_no_entry(conn, attempt_id=attempt_id):
                return "drained"
            host = await one(conn, "SELECT value FROM kv WHERE key = 'execution_host_generation'")
            if host is None or int(json.loads(host["value"])) != row["host_generation"]:
                return "unknown"
            if row["state"] == "cancelled":
                return "unknown"
            if not row["staff_session_id"] or row["ended_at"]:
                await conn.execute(
                    "UPDATE lifecycle_owners SET cancel_state = 'unknown',last_observed_at = ?,updated_at = ? "
                    "WHERE parent_kind = ? AND parent_id = ? AND child_kind = 'execution_attempt' "
                    "AND child_id = ? AND generation = ?",
                    (_now(), _now(), owner["parent_kind"], owner["parent_id"], attempt_id, owner["generation"]),
                )
                return "unknown"
            if row["state"] == "queued" and not row["provider_session_ref"]:
                changed = await conn.execute(
                    "UPDATE execution_attempts SET state = 'recovering',updated_at = ? "
                    "WHERE id = ? AND state = 'queued' AND provider_session_ref IS NULL",
                    (_now(), attempt_id),
                )
                if changed.rowcount != 1:
                    return "unknown"
                await conn.execute(
                    "UPDATE lifecycle_owners SET cancel_state = 'unknown',last_observed_at = ?,updated_at = ? "
                    "WHERE parent_kind = ? AND parent_id = ? AND child_kind = 'execution_attempt' "
                    "AND child_id = ? AND generation = ?",
                    (_now(), _now(), owner["parent_kind"], owner["parent_id"], attempt_id, owner["generation"]),
                )
                return "unknown"
            await conn.execute(
                "UPDATE lifecycle_owners SET cancel_state = 'acknowledged',observed_identity = ?,updated_at = ? "
                "WHERE parent_kind = ? AND parent_id = ? AND child_kind = 'execution_attempt' "
                "AND child_id = ? AND generation = ? AND cancel_state = 'requested'",
                (json.dumps({"host_generation": row["host_generation"], "provider_session_ref": row["provider_session_ref"],
                             "staff_session_id": row["staff_session_id"]}, sort_keys=True), _now(),
                 owner["parent_kind"], owner["parent_id"], attempt_id, owner["generation"]),
            )
            staff_session_id = row["staff_session_id"]
        team = self.app.extensions.get("staff")
        live = await team.live(staff_session_id) if team is not None else None
        if live is None or live.id != staff_session_id:
            return "unknown"
        await check(claim)
        current = await self.app.db.fetchone(
            "SELECT a.id,a.provider_session_ref,a.host_generation,t.current_attempt_id,s.session_id,s.terminal_id "
            "FROM execution_attempts a JOIN board_tasks t ON t.id = a.task_id "
            "JOIN staff_sessions s ON s.id = a.staff_session_id WHERE a.id = ?", (attempt_id,),
        )
        host = await self.app.db.fetchone("SELECT value FROM kv WHERE key = 'execution_host_generation'")
        if (current is None or host is None or int(json.loads(host["value"])) != row["host_generation"]
                or current["host_generation"] != row["host_generation"]):
            return "unknown"
        observed = f"session:{current['session_id']}" if row["runtime_kind"] == "daedalus" else f"terminal:{current['terminal_id']}"
        if not row["provider_session_ref"] or observed != row["provider_session_ref"]:
            return "unknown"
        try:
            async with team.execution_lock(live.staff.id):
                await check(claim)
                async with asyncio.timeout(30):
                    if row["runtime_kind"] == "daedalus":
                        if not row["native_run_id"]:
                            return "unknown"
                        if not await self.app.manager.stop_run(current["session_id"], row["native_run_id"]):
                            return "unknown"
                    else:
                        await team.runtime(live.staff).stop(live)
        except Exception:
            return "unknown"
        return "unknown"

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        for owner in claim.payload["attempts"]:
            row = await self.app.db.fetchone(
                "SELECT a.state,a.provider_session_ref,s.ended_at,o.cancel_state FROM execution_attempts a "
                "JOIN lifecycle_owners o ON o.child_kind = 'execution_attempt' AND o.child_id = a.id "
                "AND o.parent_kind = ? AND o.parent_id = ? AND o.generation = ? AND o.source_revision = ? "
                "LEFT JOIN staff_sessions s ON s.id = a.staff_session_id WHERE a.id = ?",
                (owner["parent_kind"], owner["parent_id"], owner["generation"], owner["source_revision"], owner["child_id"]),
            )
            if row is None or row["cancel_state"] != "drained":
                return None
        return EffectResolution("completed", {"observed": "all_owned_attempts_terminal"})

    async def drain_verified(self) -> int:
        """Propagate observed child exits; a completed report is not proof its runtime ended."""
        changed = 0
        async with self.app.db.transaction() as conn:
            async with conn.execute("SELECT child_id FROM lifecycle_owners WHERE child_kind = 'execution_attempt'"
                                    " AND cancel_state IN ('requested','acknowledged','unknown')") as cursor:
                unentered = await cursor.fetchall()
            for child in unentered:
                await record_owned_no_entry(conn, attempt_id=child["child_id"])
            cursor = await conn.execute(
                "SELECT a.id,a.staff_session_id,a.host_generation,a.provider_session_ref FROM execution_attempts a"
                " JOIN lifecycle_owners o ON o.child_kind = 'execution_attempt' AND o.child_id = a.id"
                " JOIN runtime_exit_observations e ON e.attempt_id = a.id AND e.host_generation = a.host_generation"
                " AND e.contract_revision = a.contract_revision AND e.provider_session_ref = a.provider_session_ref"
                " WHERE o.cancel_state IN ('requested','acknowledged','unknown')"
                " AND ((a.runtime_kind = 'daedalus' AND e.runtime_ref = a.native_run_id)"
                " OR (a.runtime_kind = 'cli' AND e.runtime_instance = a.runtime_instance))",
            )
            observed = await cursor.fetchall()
            await cursor.close()
            host = await one(conn, "SELECT value FROM kv WHERE key = 'execution_host_generation'")
            for attempt in observed:
                if host is None or json.loads(host["value"]) != attempt["host_generation"]:
                    continue
                await conn.execute("UPDATE staff_sessions SET ended_at = COALESCE(ended_at,?),status = 'exited' WHERE id = ?",
                                   (_now(), attempt["staff_session_id"]))
                await record_owned_exit(conn, attempt_id=attempt["id"], staff_session_id=attempt["staff_session_id"],
                                        host_generation=attempt["host_generation"], provider_session_ref=attempt["provider_session_ref"])
            for kind in ("task", "project_goal"):
                cursor = await conn.execute(
                    "SELECT parent_id,generation FROM lifecycle_parents WHERE parent_kind = ? AND cancel_state = 'requested'",
                    (kind,),
                )
                parents = await cursor.fetchall()
                await cursor.close()
                for parent in parents:
                    unresolved = await one(
                        conn, "SELECT 1 FROM lifecycle_owners WHERE parent_kind = ? AND parent_id = ? "
                        "AND generation = ? AND cancel_state != 'drained' LIMIT 1",
                        (kind, parent["parent_id"], parent["generation"]),
                    )
                    if unresolved is None:
                        await conn.execute(
                            "UPDATE lifecycle_parents SET cancel_state = 'drained',updated_at = ? "
                            "WHERE parent_kind = ? AND parent_id = ? AND generation = ? AND cancel_state = 'requested'",
                            (_now(), kind, parent["parent_id"], parent["generation"]),
                        )
                        changed += 1
                if kind == "task":
                    cursor = await conn.execute(
                        "SELECT o.parent_kind,o.parent_id,o.generation,o.child_id FROM lifecycle_owners o JOIN lifecycle_parents p "
                        "ON p.parent_kind = 'task' AND p.parent_id = o.child_id "
                        "WHERE o.child_kind = 'task' AND o.cancel_state = 'requested' AND p.cancel_state = 'drained'"
                    )
                    tasks = await cursor.fetchall()
                    await cursor.close()
                    for task in tasks:
                        await conn.execute(
                            "UPDATE lifecycle_owners SET cancel_state = 'drained',last_observed_at = ?,updated_at = ? "
                            "WHERE parent_kind = ? AND parent_id = ? AND generation = ? "
                            "AND child_kind = 'task' AND child_id = ? AND cancel_state = 'requested'",
                            (_now(), _now(), task["parent_kind"], task["parent_id"], task["generation"], task["child_id"]),
                        )
                        changed += 1
        return changed

    async def sweep(self) -> None:
        while True:
            try:
                await self.drain_verified()
            except Exception:
                # The requested state remains durable; one bad observation cannot unblock a child.
                logger.exception("owned-child cancellation could not be reconciled")
            await asyncio.sleep(5)


async def install(app: Application) -> list[Any]:
    service = Lifecycle(app)
    app.extensions["effects"].register("lifecycle.stop", service)
    app.extensions["lifecycle"] = service
    return [asyncio.create_task(service.sweep(), name="lifecycle-drain-sweep")]


__all__ = ["Lifecycle", "install"]
