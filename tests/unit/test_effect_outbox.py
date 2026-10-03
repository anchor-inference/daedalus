"""Outbox retries never reinterpret an unknown external effect as safe to repeat."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from daedalus.extensions.effects import EffectDispatcher, EffectOutcome, EffectResolution
from daedalus.stores.control import ControlDenied, ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore

OPERATOR = Principal.operator({"via": "cookie", "user_id": 1})
SCOPE = Scope("global", "global")


async def enqueue(db: Database, *, principal: Principal = OPERATOR, command: str = "command") -> str:
    store = ControlStore(db)

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        action_id = await OutboxStore.enqueue(conn, mutation, principal, kind="test.effect", operation="test.queue", payload={"text": "Original"})
        return {"effect_id": action_id, "state": "queued"}

    result = await store.mutate(principal, SCOPE, "test.queue", command, await store.revision(SCOPE, Entity("collection", "global")), Entity("collection", "global"), {}, effect)
    return result["effect_id"]


async def test_two_consumers_cannot_claim_one_effect(db: Database) -> None:
    action_id = await enqueue(db)
    second = Database(db.path, workspaces_dir=db.workspaces_dir)
    await second.open()
    try:
        a, b = await asyncio.gather(OutboxStore(db).claim(("test.effect",)), OutboxStore(second).claim(("test.effect",)))
        assert (a is None) != (b is None)
        claim = a or b
        assert claim.id == action_id
        await OutboxStore(db).check(claim)
        assert await OutboxStore(db).finish(claim, state="completed")
        assert await OutboxStore(second).claim(("test.effect",)) is None
    finally:
        await second.close()


async def test_crash_after_claim_remains_unknown_until_proven_reconciliation(db: Database) -> None:
    action_id = await enqueue(db)
    store = OutboxStore(db)
    claim = await store.claim(("test.effect",))
    assert claim is not None
    assert await store.recover() == 1
    assert await store.recover() == 0
    assert await store.claim(("test.effect",)) is None
    with pytest.raises(ControlDenied, match="no longer current"):
        await store.check(claim)
    assert not await store.finish(claim, state="completed")
    row = await store.view(action_id)
    assert row["state"] == "unknown"
    assert not await store.reconcile(action_id, generation=claim.generation, state="completed", evidence={"commit": "old"})
    assert await store.reconcile(action_id, generation=row["claim_generation"], state="completed", evidence={"observed_commit": "exact", "command": action_id})
    assert (await store.view(action_id))["state"] == "completed"
    assert (await db.fetchone("SELECT count(*) FROM quarantined_attempt_events"))[0] == 1


async def test_grant_revocation_after_claim_prevents_dispatch(db: Database) -> None:
    control = ControlStore(db)
    subject = Principal("agent:worker", "agent")
    grant = await control.issue_grant(OPERATOR, subject, SCOPE, operations=["test.queue"], effects=[], expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    principal = Principal(subject.actor_id, "agent", grant["grant_id"], 1)
    await enqueue(db, principal=principal)
    store = OutboxStore(db)
    claim = await store.claim(("test.effect",))
    assert claim is not None
    await control.revoke_grant(OPERATOR, grant["grant_id"], reason="operator withdrew permission")
    with pytest.raises(ControlDenied, match="revoked"):
        await store.check(claim)


async def test_revoked_pending_effect_is_not_claimed(db: Database) -> None:
    control = ControlStore(db)
    subject = Principal("agent:worker", "agent")
    grant = await control.issue_grant(OPERATOR, subject, SCOPE, operations=["test.queue"], effects=[], expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    action_id = await enqueue(db, principal=Principal(subject.actor_id, "agent", grant["grant_id"], 1))
    await control.revoke_grant(OPERATOR, grant["grant_id"], reason="cancelled")
    assert await OutboxStore(db).claim(("test.effect",)) is None
    assert (await OutboxStore(db).view(action_id))["state"] == "cancelled"


async def test_missing_handler_keeps_effect_pending(db: Database) -> None:
    action_id = await enqueue(db)
    store = OutboxStore(db)
    assert await store.claim(("other.effect",)) is None
    assert (await store.view(action_id))["state"] == "pending"
    with pytest.raises(ValueError, match="proof|proven"):
        await store.reconcile(action_id, generation=0, state="completed", evidence={})


async def test_dispatcher_does_not_repeat_an_opaque_handler_failure(db: Database) -> None:
    action_id = await enqueue(db)
    calls = 0

    class BrokenHandler:
        async def run(self, claim: Any, check: Any) -> EffectOutcome:
            nonlocal calls
            await check(claim)
            calls += 1
            raise RuntimeError("connection lost after effect")

    dispatcher = EffectDispatcher(OutboxStore(db))
    dispatcher.register("test.effect", BrokenHandler())
    assert await dispatcher.step()
    assert (await dispatcher.store.view(action_id))["state"] == "unknown"
    assert not await dispatcher.step()
    assert calls == 1


async def test_stopping_a_handler_records_unknown_before_database_close(db: Database) -> None:
    action_id = await enqueue(db)
    started = asyncio.Event()

    class InterruptedHandler:
        async def run(self, claim: Any, check: Any) -> EffectOutcome:
            await check(claim)
            started.set()
            await asyncio.Event().wait()
            return EffectOutcome("completed")

    dispatcher = EffectDispatcher(OutboxStore(db))
    dispatcher.register("test.effect", InterruptedHandler())
    running = asyncio.create_task(dispatcher.step())
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert (await dispatcher.store.view(action_id))["state"] == "unknown"


async def test_revoked_unknown_effect_is_observed_without_running_it_again(db: Database) -> None:
    control = ControlStore(db)
    subject = Principal("agent:worker", "agent")
    grant = await control.issue_grant(OPERATOR, subject, SCOPE, operations=["test.queue"], effects=[], expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    action_id = await enqueue(db, principal=Principal(subject.actor_id, "agent", grant["grant_id"], 1))
    store = OutboxStore(db)
    claim = await store.claim(("test.effect",))
    assert claim is not None
    await store.recover()
    await control.revoke_grant(OPERATOR, grant["grant_id"], reason="permission withdrawn after an interrupted effect")
    observed = []

    class ReadOnlyReconciler:
        async def run(self, claim: Any, check: Any) -> EffectOutcome:
            pytest.fail("an uncertain command was delivered twice")

        async def reconcile(self, claim: Any) -> EffectResolution:
            with pytest.raises(ControlDenied):
                await store.check(claim)
            observed.append(claim.id)
            return EffectResolution("completed", {"observed_commit": "exact", "command": claim.id})

    dispatcher = EffectDispatcher(store)
    dispatcher.register("test.effect", ReadOnlyReconciler())
    assert await dispatcher.reconcile() == 1
    assert await dispatcher.reconcile() == 0
    assert not await dispatcher.step()
    assert observed == [action_id]
    assert (await store.view(action_id))["state"] == "completed"


async def test_no_physical_proof_keeps_outcome_unknown(db: Database) -> None:
    action_id = await enqueue(db)
    store = OutboxStore(db)
    await store.claim(("test.effect",))
    await store.recover()

    class UncertainHandler:
        async def run(self, claim: Any, check: Any) -> EffectOutcome:
            pytest.fail("unknown is not pending")

        async def reconcile(self, claim: Any) -> None:
            return None

    dispatcher = EffectDispatcher(store)
    dispatcher.register("test.effect", UncertainHandler())
    assert await dispatcher.reconcile() == 0
    assert not await dispatcher.step()
    assert (await store.view(action_id))["state"] == "unknown"


async def test_outbox_cannot_change_the_committed_operation(db: Database) -> None:
    action_id = await enqueue(db)
    await db.execute("UPDATE effect_outbox SET payload_json = json_set(payload_json, '$.control.operation', 'different.command') WHERE id = ?", (action_id,))
    store = OutboxStore(db)
    assert await store.claim(("test.effect",)) is None
    row = await store.view(action_id)
    assert row["state"] == "cancelled"
    assert "committed operation" in row["error"]
