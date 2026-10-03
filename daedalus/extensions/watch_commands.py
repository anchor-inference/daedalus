"""Receipt-backed changes to standing watches and their exact approved action versions."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from daedalus.extensions.watch_authority import action_kind, approve, revoke
from daedalus.extensions.watches import NOTE_MAX, Watch, WatchRefused, _parse
from daedalus.host.events import AppEvent
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Principal, Scope, digest, now, one

if TYPE_CHECKING:
    from daedalus.extensions.watches import Watches


class WatchCommands:
    def __init__(self, keeper: Watches) -> None:
        self.keeper = keeper
        self.db = keeper.manager.db
        self.bus = keeper.manager.bus
        self.control = ControlStore(self.db)

    async def _replay(self, principal: Principal, project_id: str, operation: str,
                      client_operation_id: str, expected_revision: int, entity: Entity,
                      payload: dict[str, Any], *, action: dict[str, Any] | None = None) -> dict[str, Any] | None:
        scope = Scope("project", project_id)
        async with self.db.transaction() as conn:
            row = await one(
                conn, "SELECT payload_hash,response_json FROM operation_receipts"
                " WHERE scope_kind = 'project' AND scope_id = ? AND actor_id = ?"
                " AND operation_kind = ? AND client_operation_id = ?",
                (project_id, principal.actor_id, operation, client_operation_id),
            )
            if row is None:
                return None
            effects = (action_kind(action),) if action is not None and principal.origin_class == "agent" else ()
            await self.control.authorize(conn, principal, scope, operation, effects=effects)
            expected_hash = digest({"entity": {"kind": entity.kind, "id": entity.id},
                                    "expected_revision": expected_revision, "payload": payload,
                                    "effects": sorted(effects)})
            if row["payload_hash"] != expected_hash:
                raise ControlConflict("the command identity was reused with a different request")
            response = json.loads(row["response_json"])
        await self._announce_receipt(project_id, response["receipt_id"])
        return response

    async def _announce_receipt(self, project_id: str, receipt_id: str) -> None:
        async with self.bus.transaction_guard():
            row = await self.db.fetchone(
                "SELECT * FROM app_events WHERE type = 'project.changed' AND project_id = ?"
                " AND json_extract(payload_json,'$.receipt_id') = ?",
                (project_id, receipt_id),
            )
            if row is not None and int(row["seq"]) > self.bus.head:
                self.bus.announce_committed(AppEvent(
                    seq=int(row["seq"]), at=str(row["at"]), type="project.changed",
                    payload=json.loads(row["payload_json"]), project_id=project_id,
                ))

    async def _commit(self, principal: Principal, project_id: str, operation: str,
                      client_operation_id: str, expected_revision: int, entity: Entity,
                      payload: dict[str, Any], effect: Any, *, action: dict[str, Any] | None = None,
                      watch_id: str | None = None) -> dict[str, Any]:
        events: list[AppEvent] = []

        async def apply(conn: Any, mutation: Any) -> dict[str, Any]:
            response = await effect(conn, mutation)
            events.append(await self.bus.persist_in(
                conn, "project.changed",
                {"change": "watches", "actor": principal.origin_class, "receipt_id": mutation.receipt_id},
                project_id=project_id,
            ))
            return response

        effects = (action_kind(action),) if action is not None and principal.origin_class == "agent" else ()
        # The fire path takes these locks in this order. A revision cannot retire while its
        # physical delivery is in flight, nor may a pending delivery start after retirement.
        async with self.keeper._firing, self.keeper.deliveries._lock, self.bus.transaction_guard():
            response = await self.control.mutate(
                principal, Scope("project", project_id), operation, client_operation_id,
                expected_revision, entity, payload, apply, effects=effects,
            )
            resolved_id = watch_id or response.get("id")
            if resolved_id:
                watch = await self.keeper._reload(resolved_id)
                if watch is not None and watch.event == "terminal_output" and watch.enabled:
                    self.keeper._follow(watch)
                else:
                    self.keeper._unfollow(resolved_id)
            if not events:
                row = await self.db.fetchone(
                    "SELECT * FROM app_events WHERE type = 'project.changed' AND project_id = ?"
                    " AND json_extract(payload_json,'$.receipt_id') = ?",
                    (project_id, response["receipt_id"]),
                )
                if row is not None and int(row["seq"]) > self.bus.head:
                    events.append(AppEvent(
                        seq=int(row["seq"]), at=str(row["at"]), type="project.changed",
                        payload=json.loads(row["payload_json"]), project_id=project_id,
                    ))
            for event in events:
                self.bus.announce_committed(event)
            return response

    async def create(self, principal: Principal, project: Any, *, when: dict[str, Any],
                     then: dict[str, Any], cooldown_minutes: float = 10, once: bool = False,
                     note: str = "", deadline_at: str | None = None,
                     expected_collection_revision: int, client_operation_id: str) -> dict[str, Any]:
        payload = {"when": when, "then": then, "cooldown_minutes": cooldown_minutes,
                   "once": once, "note": note, "deadline_at": deadline_at}
        replay = await self._replay(
            principal, project.id, "watch.create", client_operation_id,
            expected_collection_revision, Entity("collection", project.id), payload, action=then,
        )
        if replay is not None:
            return replay
        pattern = await self.keeper._check_when(project, when)
        action = await self.keeper._check_then(project, then)
        cooldown_s = self.keeper._check_cooldown(cooldown_minutes)
        text = " ".join(note.split())
        if len(text) > NOTE_MAX:
            raise WatchRefused(f"a note is at most {NOTE_MAX} characters")
        if deadline_at is not None:
            deadline = _parse(deadline_at)
            if deadline is None or deadline <= self.keeper.clock() or deadline > self.keeper.clock() + timedelta(days=365):
                raise WatchRefused("watch deadline must be within the next year")
            deadline_at = deadline.isoformat()
        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            current_project = await one(conn, "SELECT entity_revision FROM projects WHERE id = ?", (project.id,))
            if current_project["entity_revision"] != project.entity_revision:
                raise ControlConflict("the project changed during watch validation", current_revision=current_project["entity_revision"])
            cursor = await conn.execute("SELECT count(*) AS n FROM watches WHERE project_id = ? AND enabled = 1", (project.id,))
            active = await cursor.fetchone()
            await cursor.close()
            if active["n"] >= self.keeper.config.max_per_project:
                raise WatchRefused(f"the project already has {self.keeper.config.max_per_project} watches switched on")
            watch_id = "w" + mutation.object_id[:16]
            at = self.keeper.clock().isoformat()
            await conn.execute(
                "INSERT INTO watches(id,project_id,pattern_json,action_json,cooldown_s,once,note,created_by,"
                "created_at,enabled,state_json,condition_revision,deadline_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,1,'{}',1,?)",
                (watch_id, project.id, json.dumps(pattern), json.dumps(action), cooldown_s,
                 int(once), text, "operator" if principal.origin_class == "operator" else "orchestrator", at, deadline_at),
            )
            await approve(conn, watch_id, 1, project.id, principal, action)
            row = await one(conn, "SELECT * FROM watches WHERE id = ?", (watch_id,))
            return Watch.from_row(row).view()

        return await self._commit(
            principal, project.id, "watch.create", client_operation_id,
            expected_collection_revision, Entity("collection", project.id), payload, effect, action=action,
        )

    async def change(self, principal: Principal, project: Any, watch_id: str, *,
                     enabled: bool | None = None, note: str | None = None,
                     cooldown_minutes: float | None = None, when: dict[str, Any] | None = None,
                     then: dict[str, Any] | None = None, deadline_at: str | None = None,
                     expected_entity_revision: int, expected_condition_revision: int,
                     client_operation_id: str) -> dict[str, Any]:
        payload = {"watch_id": watch_id, "enabled": enabled, "note": note,
                   "cooldown_minutes": cooldown_minutes, "when": when, "then": then,
                   "deadline_at": deadline_at, "expected_condition_revision": expected_condition_revision}
        replay = await self._replay(
            principal, project.id, "watch.change", client_operation_id,
            expected_entity_revision, Entity("project", project.id), payload, action=then,
        )
        if replay is not None:
            return replay
        pattern = await self.keeper._check_when(project, when) if when is not None else None
        action = await self.keeper._check_then(project, then) if then is not None else None
        cooldown_s = self.keeper._check_cooldown(cooldown_minutes) if cooldown_minutes is not None else None
        text = " ".join(note.split()) if note is not None else None
        if text is not None and len(text) > NOTE_MAX:
            raise WatchRefused(f"a note is at most {NOTE_MAX} characters")
        if deadline_at is not None and deadline_at != "":
            deadline = _parse(deadline_at)
            if deadline is None or deadline <= self.keeper.clock() or deadline > self.keeper.clock() + timedelta(days=365):
                raise WatchRefused("watch deadline must be within the next year")
            deadline_at = deadline.isoformat()
        async def effect(conn: Any, _: Any) -> dict[str, Any]:
            if pattern is not None or action is not None:
                current_project = await one(conn, "SELECT entity_revision FROM projects WHERE id = ?", (project.id,))
                if current_project["entity_revision"] != expected_entity_revision:
                    raise ControlConflict("the project changed during watch validation", current_revision=current_project["entity_revision"])
            row = await one(conn, "SELECT * FROM watches WHERE id = ? AND project_id = ?", (watch_id, project.id))
            if row is None:
                raise KeyError(watch_id)
            current = Watch.from_row(row)
            if current.condition_revision != expected_condition_revision:
                raise ControlConflict("the watch condition has changed", current_revision=current.condition_revision)
            new_action = action if action is not None else current.action
            if principal.origin_class == "agent":
                await self.control.authorize(
                    conn, principal, Scope("project", project.id), "watch.deliver",
                    effects=(action_kind(new_action),),
                )
            if enabled and not current.enabled:
                cursor = await conn.execute("SELECT count(*) AS n FROM watches WHERE project_id = ? AND enabled = 1", (project.id,))
                active = await cursor.fetchone()
                await cursor.close()
                if active["n"] >= self.keeper.config.max_per_project:
                    raise WatchRefused(f"the project already has {self.keeper.config.max_per_project} watches switched on")
            state = dict(current.state)
            if enabled:
                for key in ("stopped", "fires", "last_error"):
                    state.pop(key, None)
            revision = current.condition_revision + 1
            await conn.execute(
                "UPDATE watches SET pattern_json = ?,action_json = ?,cooldown_s = ?,once = ?,note = ?,"
                "enabled = ?,state_json = ?,condition_revision = ?,deadline_at = ? WHERE id = ?",
                (json.dumps(pattern or current.pattern), json.dumps(new_action),
                 cooldown_s if cooldown_s is not None else current.cooldown_s, int(current.once),
                 text if text is not None else current.note,
                 int(enabled) if enabled is not None else int(current.enabled), json.dumps(state), revision,
                 current.deadline_at if deadline_at is None else deadline_at or None, watch_id),
            )
            await revoke(conn, watch_id, current.condition_revision)
            await approve(conn, watch_id, revision, project.id, principal, new_action)
            await conn.execute("UPDATE watch_deliveries SET status = 'cancelled',updated_at = ?"
                               " WHERE watch_id = ? AND status = 'pending'", (now(), watch_id))
            changed = await one(conn, "SELECT * FROM watches WHERE id = ?", (watch_id,))
            return Watch.from_row(changed).view()

        return await self._commit(
            principal, project.id, "watch.change", client_operation_id, expected_entity_revision,
            Entity("project", project.id), payload, effect, action=action, watch_id=watch_id,
        )

    async def remove(self, principal: Principal, project_id: str, watch_id: str, *,
                     expected_entity_revision: int, expected_condition_revision: int,
                     client_operation_id: str) -> dict[str, Any]:
        payload = {"watch_id": watch_id, "expected_condition_revision": expected_condition_revision}
        replay = await self._replay(
            principal, project_id, "watch.remove", client_operation_id,
            expected_entity_revision, Entity("project", project_id), payload,
        )
        if replay is not None:
            return replay

        async def effect(conn: Any, _: Any) -> dict[str, Any]:
            row = await one(conn, "SELECT condition_revision FROM watches WHERE id = ? AND project_id = ?",
                            (watch_id, project_id))
            if row is None:
                raise KeyError(watch_id)
            if row["condition_revision"] != expected_condition_revision:
                raise ControlConflict("the watch condition has changed", current_revision=row["condition_revision"])
            await revoke(conn, watch_id, expected_condition_revision)
            await conn.execute("UPDATE watch_deliveries SET status = 'cancelled',updated_at = ?"
                               " WHERE watch_id = ? AND status = 'pending'", (now(), watch_id))
            await conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
            return {"deleted": True, "watch_id": watch_id}

        return await self._commit(
            principal, project_id, "watch.remove", client_operation_id, expected_entity_revision,
            Entity("project", project_id), payload, effect, watch_id=watch_id,
        )
