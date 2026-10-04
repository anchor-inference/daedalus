"""An approval follows one committed effect through delivery and revocation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from daedalus.extensions.api_effect_approvals import install_routes
from daedalus.extensions.effects import EffectDispatcher, EffectOutcome
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.effect_approvals import EffectApprovals
from daedalus.stores.outbox import OutboxStore

OPERATOR = Principal.operator({"via": "cookie", "user_id": 1})
SCOPE = Scope("global", "global")


async def queued(db: Database, *, principal: Principal = OPERATOR) -> str:
    control = ControlStore(db)

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        effect_id = await OutboxStore.enqueue(conn, mutation, principal, kind="test.effect", operation="test.queue",
                                              payload={"target": {"recipient": "someone"}, "artifact_revision": "sha256:a"},
                                              approval_required=True)
        return {"effect_id": effect_id}

    result = await control.mutate(principal, SCOPE, "test.queue", "queue1",
                                  await control.revision(SCOPE, Entity("collection", "global")),
                                  Entity("collection", "global"), {}, effect)
    return result["effect_id"]


async def approved(db: Database, effect_id: str) -> dict[str, Any]:
    control = ControlStore(db)
    return await EffectApprovals(db).approve(
        effect_id, OPERATOR, expected_artifact_revision="sha256:a",
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(), client_operation_id="approve1",
        expected_collection_revision=await control.revision(SCOPE, Entity("collection", "global")),
    )


async def test_exact_effect_waits_for_approval_and_rechecks_changed_target(db: Database) -> None:
    effect_id = await queued(db)
    store = OutboxStore(db)
    assert await store.claim(("test.effect",)) is None
    assert (await store.view(effect_id))["state"] == "pending"
    result = await approved(db, effect_id)
    assert result["state"] == "approved" and result["receipt_id"]
    claim = await store.claim(("test.effect",))
    assert claim is not None
    await store.check(claim)
    await db.execute("UPDATE effect_outbox SET payload_json=json_set(payload_json,'$.data.target.recipient','other') WHERE id=?", (effect_id,))
    with pytest.raises(ControlDenied, match="no longer matches"):
        await store.check(claim)


async def test_old_revision_and_revoked_approval_fail_closed(db: Database) -> None:
    effect_id = await queued(db)
    approvals = EffectApprovals(db)
    control = ControlStore(db)
    with pytest.raises(ControlConflict, match="revision has changed"):
        await approvals.approve(effect_id, OPERATOR, expected_artifact_revision="sha256:old",
                                expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                                client_operation_id="old", expected_collection_revision=await control.revision(SCOPE, Entity("collection", "global")))
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    revision = await control.revision(SCOPE, Entity("collection", "global"))
    result = await approvals.approve(effect_id, OPERATOR, expected_artifact_revision="sha256:a",
                                     expires_at=expires_at, client_operation_id="approve1",
                                     expected_collection_revision=revision)
    claim = await OutboxStore(db).claim(("test.effect",))
    assert claim is not None
    revoked = await approvals.revoke(result["approval_id"], OPERATOR, client_operation_id="revoke1",
                                     expected_collection_revision=await control.revision(SCOPE, Entity("collection", "global")))
    assert revoked["state"] == "revoked" and revoked["receipt_id"]
    with pytest.raises(ControlDenied, match="revoked"):
        await OutboxStore(db).check(claim)
    with pytest.raises(ControlConflict, match="revoked"):
        await approvals.approve(effect_id, OPERATOR, expected_artifact_revision="sha256:a",
                                expires_at=expires_at, client_operation_id="approve1",
                                expected_collection_revision=revision)


async def test_revoked_actor_grant_prevents_approved_effect(db: Database) -> None:
    control = ControlStore(db)
    subject = Principal("agent:worker", "agent")
    grant = await control.issue_grant(OPERATOR, subject, SCOPE, operations=["test.queue"], effects=[],
                                      expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    effect_id = await queued(db, principal=Principal(subject.actor_id, "agent", grant["grant_id"], 1))
    await approved(db, effect_id)
    claim = await OutboxStore(db).claim(("test.effect",))
    assert claim is not None
    await control.revoke_grant(OPERATOR, grant["grant_id"], reason="withdrawn")
    with pytest.raises(ControlDenied, match="revoked"):
        await OutboxStore(db).check(claim)


async def test_revoke_waits_for_an_admitted_effect_write(db: Database) -> None:
    effect_id = await queued(db)
    approval = await approved(db, effect_id)
    entered = asyncio.Event()
    release = asyncio.Event()
    revoked_at_write: list[str | None] = []

    class HeldEffect:
        async def run(self, claim: Any, check: Any) -> EffectOutcome:
            await check(claim)
            entered.set()
            await release.wait()
            row = await db.fetchone("SELECT revoked_at FROM effect_approvals WHERE id=?",
                                    (approval["approval_id"],))
            assert row is not None
            revoked_at_write.append(row["revoked_at"])
            return EffectOutcome("completed")

    dispatcher = EffectDispatcher(OutboxStore(db))
    dispatcher.register("test.effect", HeldEffect())
    delivery = asyncio.create_task(dispatcher.step())
    await asyncio.wait_for(entered.wait(), 2)
    revision = await ControlStore(db).revision(SCOPE, Entity("collection", "global"))
    revocation = asyncio.create_task(EffectApprovals(db).revoke(
        approval["approval_id"], OPERATOR, client_operation_id="revoke-during-write",
        expected_collection_revision=revision))
    await asyncio.sleep(0)
    assert not revocation.done()
    release.set()
    assert await delivery
    assert revoked_at_write == [None]
    assert (await revocation)["state"] == "revoked"


async def test_operator_route_requires_revision_and_reports_saved_approval(db: Database) -> None:
    effect_id = await queued(db)
    api = FastAPI()

    async def authenticated() -> dict[str, Any]:
        return {"via": "token", "user_id": 1}

    install_routes(api, SimpleNamespace(db=db, extensions={}), authenticated)
    body = {"decision": "allow", "expected_artifact_revision": "sha256:a",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "client_operation_id": "approve-http"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        assert (await client.post(f"/api/effects/{effect_id}/approve", json=body)).status_code == 422
        revision = await ControlStore(db).revision(SCOPE, Entity("collection", "global"))
        approved_response = await client.post(f"/api/effects/{effect_id}/approve",
                                              headers={"If-Match": str(revision)}, json=body)
        assert approved_response.status_code == 200, approved_response.text
        assert approved_response.json()["receipt_id"]
        stale = await client.post(f"/api/effects/{effect_id}/approve", headers={"If-Match": str(revision)},
                                  json={**body, "client_operation_id": "second"})
        assert stale.status_code == 409
        current = await ControlStore(db).revision(SCOPE, Entity("collection", "global"))
        revoked = await client.post(f"/api/effect-approvals/{approved_response.json()['approval_id']}/revoke",
                                    headers={"If-Match": str(current)}, json={"client_operation_id": "revoke-http"})
        assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
