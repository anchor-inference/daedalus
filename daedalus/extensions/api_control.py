"""Authenticated inspection of command revisions, receipts and effect outcomes."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException

from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.outbox import OutboxStore

if TYPE_CHECKING:
    from daedalus.app import Application


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.get("/api/control/revisions")
    async def revisions(project: str | None = None, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(authenticated)
        scope = Scope("project", project) if project else Scope("global", "global")
        store = ControlStore(app.db)
        try:
            async with app.db.transaction() as conn:
                await store.authorize(conn, Principal.operator(authenticated), scope, "control.read")
                collection = await store._entity(conn, scope, Entity("collection", scope.id))
                entity_revision = await store._entity(conn, scope, Entity("project", scope.id)) if project else None
        except KeyError:
            raise HTTPException(404, "no such project") from None
        return {"scope": {"kind": scope.kind, "id": scope.id}, "entity_revision": entity_revision, "collection_revision": collection}

    @api.get("/api/control/receipts/{receipt_id}")
    async def receipt(receipt_id: str, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(authenticated)
        row = await app.db.fetchone("SELECT id,scope_kind,scope_id,actor_id,operation_kind,entity_revision,state,response_json,created_at FROM operation_receipts WHERE id = ?", (receipt_id,))
        if row is None:
            raise HTTPException(404, "no such command receipt")
        data = dict(row)
        data["response"] = json.loads(data.pop("response_json"))
        return data

    @api.get("/api/control/effects/{action_id}")
    async def effect(action_id: str, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(authenticated)
        try:
            return await OutboxStore(app.db).view(action_id)
        except KeyError:
            raise HTTPException(404, "no such effect") from None
