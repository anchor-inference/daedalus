"""Operator decisions on an observed provider limit and its exact failed run."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.provider_holds import create_hold_in, failure_view_in, queue_resume_in, validate_hold_in

if TYPE_CHECKING:
    from daedalus.app import Application


class HoldBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_entity_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.get("/api/providers/{provider_id}/limits")
    async def limits(provider_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async with app.db.transaction() as conn:
            async with conn.execute(
                "SELECT o.id,o.session_id,o.run_id,o.project_id,o.provider_id,o.model,o.status,"
                "o.failure_class,o.provider_code,o.reset_at,o.reset_source,o.retry_after_at,o.observed_at,"
                "h.id AS hold_id,CASE WHEN e.state IN ('failed','cancelled') AND h.state = 'resume_queued'"
                " THEN 'invalidated' WHEN e.state = 'unknown' THEN 'unknown' ELSE h.state END AS hold_state"
                " FROM provider_failure_observations o"
                " LEFT JOIN provider_resume_holds h ON h.observation_id = o.id"
                " LEFT JOIN effect_outbox e ON e.id = h.resume_action_id"
                " WHERE o.provider_id = ? ORDER BY o.rowid DESC LIMIT 50", (provider_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            observations = []
            for row in rows:
                view = dict(row)
                view["hold_available"] = False
                if not row["hold_id"]:
                    try:
                        await validate_hold_in(conn, row["id"])
                    except ControlConflict as exc:
                        view["hold_blocker"] = str(exc)
                    else:
                        view["hold_available"] = True
                observations.append(view)
        return {"provider_id": provider_id, "observations": observations}

    @api.post("/api/providers/{provider_id}/holds")
    async def hold(provider_id: str, body: HoldBody,
                   who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        observation = await app.db.fetchone(
            "SELECT provider_id,project_id FROM provider_failure_observations WHERE id = ?",
            (body.observation_id,),
        )
        if observation is None or observation["provider_id"] != provider_id:
            raise HTTPException(404, "no such provider failure")
        scope = Scope("project", observation["project_id"]) if observation["project_id"] else Scope("global", "global")
        entity = Entity("project", scope.id) if scope.kind == "project" else Entity("collection", "global")
        principal = Principal.operator(who)

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            current = await failure_view_in(conn, body.observation_id)
            if current["provider_id"] != provider_id or current["project_id"] != observation["project_id"]:
                raise ControlConflict("the provider observation changed scope")
            return await create_hold_in(conn, hold_id=mutation.object_id, observation_id=body.observation_id,
                                        receipt_id=mutation.receipt_id)

        try:
            return await ControlStore(app.db).mutate(
                principal, scope, "provider.hold", body.client_operation_id,
                body.expected_entity_revision, entity, body.model_dump(), effect,
            )
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/api/providers/{provider_id}/holds/{hold_id}/resume")
    async def resume(provider_id: str, hold_id: str, body: ResumeBody,
                     who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        held = await app.db.fetchone("SELECT provider_id,project_id,session_id,failed_run_id,model FROM provider_resume_holds"
                                     " WHERE id = ?", (hold_id,))
        if held is None or held["provider_id"] != provider_id:
            raise HTTPException(404, "no such provider hold")
        scope = Scope("project", held["project_id"]) if held["project_id"] else Scope("global", "global")
        entity = Entity("project", scope.id) if scope.kind == "project" else Entity("collection", "global")
        principal = Principal.operator(who)
        payload = {"hold_id": hold_id, "provider_id": provider_id, **body.model_dump()}

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            pinned = await conn.execute("SELECT * FROM provider_resume_holds WHERE id = ?", (hold_id,))
            row = await pinned.fetchone()
            await pinned.close()
            if row is None or row["provider_id"] != provider_id or row["scope_kind"] != scope.kind or row["scope_id"] != scope.id:
                raise ControlConflict("the provider hold changed scope")
            action_id = await OutboxStore.enqueue(
                conn, mutation, principal, kind="provider.resume", operation="provider.resume",
                payload={"hold_id": hold_id, "session_id": row["session_id"],
                         "failed_run_id": row["failed_run_id"], "provider_id": provider_id,
                         "model": row["model"]},
                effects=("provider.send",),
            )
            return await queue_resume_in(conn, hold_id=hold_id, action_id=action_id,
                                         receipt_id=mutation.receipt_id)

        try:
            result = await ControlStore(app.db).mutate(
                principal, scope, "provider.resume", body.client_operation_id,
                body.expected_entity_revision, entity, payload, effect, effects=("provider.send",),
            )
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        app.extensions["effects"].notify()
        return result
