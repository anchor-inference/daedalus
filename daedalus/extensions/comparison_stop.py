"""Stop one funded comparison contender without borrowing the shared task's current attempt."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.host.events import AppEvent
from daedalus.stores.comparison_funding import physical_exit_in
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope, now, one
from daedalus.stores.outbox import Claim, OutboxStore

if TYPE_CHECKING:
    from daedalus.app import Application


async def stop_comparison_slot(
    app: Application, *, task_id: str, group_id: str, slot: int, principal: Principal,
    client_operation_id: str, expected_entity_revision: int, reason: str,
) -> dict[str, Any]:
    """Freeze an exact member stop or cancel its not-yet-claimed launch under task CAS."""
    if principal.origin_class != "operator":
        raise ControlDenied("comparison stop needs an authenticated operator")
    if slot not in (1, 2) or not 1 <= len(reason.strip()) <= 1000:
        raise ValueError("stop needs one comparison slot and a bounded reason")
    task = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
    if task is None or not task["project_id"]:
        raise KeyError(task_id)
    bus = app.manager.bus
    events: list[AppEvent] = []

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        member = await one(conn,
            "SELECT g.state AS group_state,g.contract_revision,f.id AS slot_id,f.attempt_id,f.staff_id,"
            " f.host_generation,f.state AS funding_state,a.staff_session_id,a.provider_session_ref,"
            " a.native_run_id,a.runtime_kind,s.session_id,s.ended_at,"
            " e.id AS launch_effect_id,e.state AS launch_state"
            " FROM comparison_groups g JOIN comparison_funding_slots f ON f.group_id = g.id"
            " LEFT JOIN execution_attempts a ON a.id = f.attempt_id"
            " LEFT JOIN staff_sessions s ON s.id = a.staff_session_id"
            " LEFT JOIN effect_outbox e ON e.kind = CASE f.slot WHEN 1 THEN 'comparison.launch.first'"
            " ELSE 'comparison.launch.second' END"
            " AND json_extract(e.payload_json,'$.data.group_id') = g.id"
            " WHERE g.id = ? AND g.task_id = ? AND f.slot = ?", (group_id, task_id, slot),
        )
        if (member is None or member["group_state"] not in ("planned", "active", "ready") or
                member["funding_state"] != "held" or not member["launch_effect_id"]):
            raise ControlConflict("the comparison slot is not open for stopping")
        if member["attempt_id"] is None:
            if member["launch_state"] != "pending":
                raise ControlConflict("the unbound launch must be reconciled before it can be stopped")
            changed = await conn.execute("UPDATE effect_outbox SET state = 'cancelled',error = ?,completed_at = ?"
                                         " WHERE id = ? AND state = 'pending'",
                                         (reason.strip(), now(), member["launch_effect_id"]))
            if changed.rowcount != 1:
                raise ControlConflict("the launch was claimed during stop")
            await changed.close()
            response = {"group_id": group_id, "slot": slot, "task_id": task_id,
                        "launch_effect_id": member["launch_effect_id"], "state": "cancelled_pending"}
        else:
            if await physical_exit_in(conn, member["attempt_id"]):
                response = {"group_id": group_id, "slot": slot, "task_id": task_id,
                            "attempt_id": member["attempt_id"], "state": "already_exited"}
            else:
                host = await app.executions._host(conn)
                expected_ref = f"session:{member['session_id']}"
                if (member["host_generation"] != host or member["runtime_kind"] != "daedalus" or
                        not member["staff_session_id"] or member["ended_at"] or
                        not member["session_id"] or not member["native_run_id"] or
                        member["provider_session_ref"] != expected_ref):
                    raise ControlConflict("the exact native run cannot be reached; reconcile its launch")
                await conn.execute("UPDATE staff_sessions SET pause_requested = 1 WHERE id = ?",
                                   (member["staff_session_id"],))
                target = {"group_id": group_id, "slot": slot, "slot_id": member["slot_id"],
                          "task_id": task_id, "attempt_id": member["attempt_id"],
                          "staff_id": member["staff_id"], "staff_session_id": member["staff_session_id"],
                          "session_id": member["session_id"], "run_id": member["native_run_id"],
                          "provider_session_ref": member["provider_session_ref"],
                          "host_generation": host, "contract_revision": member["contract_revision"],
                          "reason": reason.strip()}
                action_id = await OutboxStore.enqueue(conn, mutation, principal,
                                                      kind="comparison.stop", operation="comparison.stop",
                                                      payload=target, task_id=task_id,
                                                      effects=("execution.stop",))
                response = {"group_id": group_id, "slot": slot, "task_id": task_id,
                            "attempt_id": member["attempt_id"], "effect_id": action_id, "state": "queued"}
        task_row = await one(conn, "SELECT title,project_id,assignee_staff_id FROM board_tasks WHERE id = ?",
                             (task_id,))
        assert task_row is not None
        events.append(await bus.persist_in(conn, "task.changed",
                                           {"task_id": task_id, "title": task_row["title"],
                                            "actor": principal.origin_class, "actor_id": principal.actor_id},
                                           project_id=task_row["project_id"],
                                           staff_id=task_row["assignee_staff_id"]))
        return response

    async with bus.transaction_guard():
        response = await ControlStore(app.db).mutate(
            principal, Scope("project", task["project_id"]), "comparison.stop", client_operation_id,
            expected_entity_revision, Entity("task", task_id),
            {"group_id": group_id, "slot": slot, "reason": reason.strip()}, effect,
            effects=("execution.stop",),
        )
        for event in events:
            bus.announce_committed(event)
    if events:
        app.extensions["effects"].notify()
    return response


class ComparisonStopEffect:
    """Physical exit is the only completion proof; an unknown stop is never resent."""

    def __init__(self, app: Application) -> None:
        self.app = app

    @staticmethod
    async def _bound_in(conn: Any, target: dict[str, Any]) -> bool:
        row = await one(conn, "SELECT f.id FROM comparison_funding_slots f"
                            " JOIN comparison_group_attempts m ON m.group_id = f.group_id"
                            " AND m.slot = f.slot AND m.attempt_id = f.attempt_id"
                            " JOIN execution_attempts a ON a.id = f.attempt_id"
                            " JOIN staff_sessions s ON s.id = a.staff_session_id"
                            " WHERE f.id = ? AND f.group_id = ? AND f.slot = ? AND f.task_id = ?"
                            " AND f.attempt_id = ? AND f.staff_id = ? AND f.host_generation = ?"
                            " AND a.staff_session_id = ? AND a.native_run_id = ?"
                            " AND a.provider_session_ref = ? AND a.contract_revision = ?"
                            " AND a.host_generation = f.host_generation AND a.task_id = f.task_id"
                            " AND a.runtime_kind = 'daedalus' AND s.staff_id = f.staff_id"
                            " AND s.session_id = ?",
                        (target["slot_id"], target["group_id"], target["slot"], target["task_id"],
                         target["attempt_id"], target["staff_id"], target["host_generation"],
                         target["staff_session_id"], target["run_id"],
                         target["provider_session_ref"], target["contract_revision"],
                         target["session_id"]))
        return row is not None

    async def _exited(self, target: dict[str, Any]) -> bool:
        async with self.app.db.transaction() as conn:
            return await self._bound_in(conn, target) and await physical_exit_in(conn, target["attempt_id"])

    async def run(self, claim: Claim, check: Callable[[Claim], Awaitable[None]]) -> EffectOutcome:
        target = claim.payload
        if claim.kind != "comparison.stop" or claim.operation != "comparison.stop" or claim.task_id != target.get("task_id"):
            return EffectOutcome("failed", "comparison stop claim has no matching task command")
        if await self._exited(target):
            return EffectOutcome("completed")
        team = self.app.extensions.get("staff")
        if team is None:
            return EffectOutcome("deferred", "the staff runtime is not available")
        async with team.execution_lock(target["staff_id"]):
            await check(claim)
            async with self.app.db.transaction() as conn:
                if not await self._bound_in(conn, target):
                    return EffectOutcome("failed", "the stopped contender lost its exact group binding")
            current = await self.app.db.fetchone(
                "SELECT a.host_generation,a.native_run_id,a.provider_session_ref,a.staff_session_id,"
                " s.session_id,s.ended_at FROM execution_attempts a"
                " JOIN staff_sessions s ON s.id = a.staff_session_id WHERE a.id = ? AND a.task_id = ?",
                (target["attempt_id"], target["task_id"]),
            )
            generation = await self.app.db.fetchone("SELECT value FROM kv WHERE key = 'execution_host_generation'")
            if (current is None or generation is None or
                    int(json.loads(generation["value"])) != target["host_generation"] or
                    current["host_generation"] != target["host_generation"] or
                    current["native_run_id"] != target["run_id"] or
                    current["provider_session_ref"] != target["provider_session_ref"] or
                    current["staff_session_id"] != target["staff_session_id"] or
                    current["session_id"] != target["session_id"]):
                return EffectOutcome("unknown", "the exact run binding changed before stop")
            if current["ended_at"]:
                return EffectOutcome("unknown", "the session ended without an exact exit observation")
            state = self.app.manager.live_state(target["session_id"])
            if state is None or state.run_id != target["run_id"]:
                return EffectOutcome("unknown", "the bound native run is not observable")
            try:
                await check(claim)
                stopped = await self.app.manager.stop_run(target["session_id"], target["run_id"])
            except Exception as exc:  # noqa: BLE001 — the runtime may have received the stop request.
                return EffectOutcome("unknown", f"native stop outcome is uncertain: {type(exc).__name__}")
            if not stopped:
                return EffectOutcome("unknown", "the bound run changed during stop")
        if await self._exited(target):
            return EffectOutcome("completed")
        return EffectOutcome("unknown", "stop requested; wait for the exact physical exit")

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        if await self._exited(claim.payload):
            return EffectResolution("completed", {"attempt_id": claim.payload["attempt_id"],
                                                  "proof": "matching_runtime_exit_observation"})
        return None


__all__ = ["stop_comparison_slot", "ComparisonStopEffect"]
