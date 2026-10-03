"""A client can inspect committed commands, but anonymous clients cannot read their data."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore


async def test_registered_control_routes_require_auth_and_show_committed_outcomes(settings: Settings, db: Database) -> None:
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=SimpleNamespace(db=db), front=None, extensions={})
    api = build_app(app, "test-token")
    store = ControlStore(db)
    principal = Principal.operator({"via": "token", "user_id": settings.owner_user_id})
    scope = Scope("global", "global")

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        action = await OutboxStore.enqueue(conn, mutation, principal, kind="test.effect", operation="test.queue", payload={"text": "Saved"})
        return {"effect_id": action, "state": "queued"}

    result = await store.mutate(principal, scope, "test.queue", "original", 1, Entity("collection", "global"), {}, effect)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        for url in ["/api/control/revisions", f"/api/control/receipts/{result['receipt_id']}", f"/api/control/effects/{result['effect_id']}"]:
            assert (await client.get(url)).status_code == 401
        client.headers["x-daedalus-token"] = "test-token"
        revisions = await client.get("/api/control/revisions")
        assert revisions.status_code == 200
        assert revisions.json()["collection_revision"] == 2
        receipt = await client.get(f"/api/control/receipts/{result['receipt_id']}")
        assert receipt.status_code == 200
        assert receipt.json()["response"] == result
        outcome = await client.get(f"/api/control/effects/{result['effect_id']}")
        assert outcome.status_code == 200
        assert outcome.json()["state"] == "pending"
        assert "payload_json" not in outcome.json()
        assert (await client.get("/api/control/revisions?project=missing")).status_code == 404
