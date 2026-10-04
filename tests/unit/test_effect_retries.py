"""Retry accounting stays durable and never repeats a write with an uncertain outcome."""

from __future__ import annotations

from pathlib import Path

import httpx

from daedalus.extensions.effects import EffectDispatcher, EffectOutcome, EffectResolution
from daedalus.stores.database import Database
from daedalus.stores.outbox import Claim, OutboxStore


async def _effect(db: Database, kind: str, name: str) -> None:
    await db.execute(
        "INSERT INTO operation_receipts(id,scope_kind,scope_id,actor_id,operation_kind,"
        "client_operation_id,payload_hash,entity_revision,state,response_json,created_at)"
        " VALUES (?,'global','global','operator:1',?,?,? ,1,'committed','{}','2026-01-01')",
        (name, kind, name, "hash"),
    )
    await db.execute(
        "INSERT INTO effect_outbox(id,receipt_id,kind,payload_json,created_at) VALUES (?,?,?,?,?)",
        (name, name, kind, '{"data":{},"control":{"task_id":null,"effects":[],"operation":"' + kind + '"}}', "2026-01-01"),
    )


class RetryHandler:
    def __init__(self, error_class: str) -> None:
        self.error_class = error_class
        self.calls = 0

    async def run(self, claim: Claim, check) -> EffectOutcome:
        await check(claim)
        self.calls += 1
        return EffectOutcome("retry", error_class=self.error_class)


async def test_transient_read_has_a_durable_bounded_budget(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    db = Database(path)
    await db.open()
    try:
        await _effect(db, "provider.catalog.read", "read")
        handler = RetryHandler("rate_limit")
        dispatcher = EffectDispatcher(OutboxStore(db))
        dispatcher.register("provider.catalog.read", handler)
        assert await dispatcher.step()
        first = await db.fetchone("SELECT state,retry_at FROM effect_outbox WHERE id = 'read'")
        assert first["state"] == "pending" and first["retry_at"]
        assert not await dispatcher.step()
        await db.close()

        db = Database(path)
        await db.open()
        dispatcher = EffectDispatcher(OutboxStore(db))
        dispatcher.register("provider.catalog.read", handler)
        for ordinal in (2, 3):
            await db.execute("UPDATE effect_outbox SET retry_at = '2026-01-01' WHERE id = 'read'")
            assert await dispatcher.step()
            row = await db.fetchone("SELECT state FROM effect_outbox WHERE id = 'read'")
            assert row["state"] == ("pending" if ordinal == 2 else "failed")
        rows = await db.fetchall("SELECT ordinal,budget_left,error_class,state FROM retry_attempts WHERE effect_id = 'read' ORDER BY ordinal")
        assert [(r["ordinal"], r["budget_left"], r["error_class"], r["state"]) for r in rows] == [
            (1, 2, "rate_limit", "scheduled"), (2, 1, "rate_limit", "scheduled"),
            (3, 0, "rate_limit", "failed"),
        ]
        assert handler.calls == 3 and not await dispatcher.step()
    finally:
        await db.close()


async def test_auth_error_is_terminal_and_uncertain_write_requires_reconciliation(db: Database) -> None:
    await _effect(db, "provider.catalog.read", "auth")
    await _effect(db, "provider.message.send", "send")
    dispatcher = EffectDispatcher(OutboxStore(db))
    auth = RetryHandler("authentication")
    send = RetryHandler("rate_limit")
    dispatcher.register("provider.catalog.read", auth)
    dispatcher.register("provider.message.send", send)
    assert await dispatcher.step()
    assert await dispatcher.step()
    auth_row = await db.fetchone("SELECT state,retry_at FROM effect_outbox WHERE id = 'auth'")
    send_row = await db.fetchone("SELECT state,retry_at FROM effect_outbox WHERE id = 'send'")
    assert (auth_row["state"], auth_row["retry_at"]) == ("failed", None)
    assert (send_row["state"], send_row["retry_at"]) == ("unknown", None)
    assert not await dispatcher.step()
    assert auth.calls == send.calls == 1


async def test_admission_deferral_does_not_spend_a_retry(db: Database) -> None:
    await _effect(db, "provider.catalog.read", "deferred")
    store = OutboxStore(db)
    claim = await store.claim(("provider.catalog.read",))
    assert claim is not None
    assert await store.defer(claim, reason="provider not ready")
    assert not await db.fetchall("SELECT ordinal FROM retry_attempts WHERE effect_id = 'deferred'")
    again = await store.claim(("provider.catalog.read",))
    assert again is not None
    assert (await db.fetchone("SELECT ordinal FROM retry_attempts WHERE effect_id = 'deferred'"))["ordinal"] == 1


class ResumeStatus:
    def __init__(self) -> None:
        self.reads = 0

    async def run(self, claim: Claim, check) -> EffectOutcome:
        raise AssertionError("an unknown resume must never be sent again")

    async def reconcile(self, claim: Claim) -> EffectResolution:
        self.reads += 1
        if self.reads < 3:
            request = httpx.Request("GET", "http://127.0.0.1/status")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("rate limited", request=request, response=response)
        return EffectResolution("completed", {"proof": "exact_input_receipt"})


async def test_ambiguous_resume_retries_only_its_status_read_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    db = Database(path)
    await db.open()
    try:
        await _effect(db, "provider.resume", "resume")
        store = OutboxStore(db)
        claim = await store.claim(("provider.resume",))
        assert claim is not None
        assert await store.finish(claim, state="unknown", error="resume response lost")
        status = ResumeStatus()
        dispatcher = EffectDispatcher(store)
        dispatcher.register("provider.resume", status)
        assert await dispatcher.reconcile() == 0
        first = await db.fetchone("SELECT state,retry_at,retry_blocked FROM effect_outbox WHERE id = 'resume'")
        assert first["state"] == "unknown" and first["retry_at"] and not first["retry_blocked"]
        assert await dispatcher.reconcile() == 0 and status.reads == 1
        await db.close()

        db = Database(path)
        await db.open()
        dispatcher = EffectDispatcher(OutboxStore(db))
        dispatcher.register("provider.resume", status)
        for expected in (2, 3):
            await db.execute("UPDATE effect_outbox SET retry_at = '2026-01-01' WHERE id = 'resume'")
            assert await dispatcher.reconcile() == (1 if expected == 3 else 0)
        row = await db.fetchone("SELECT state FROM effect_outbox WHERE id = 'resume'")
        assert row["state"] == "completed" and status.reads == 3
        attempts = await db.fetchall("SELECT ordinal,state FROM retry_attempts WHERE effect_id = 'resume' AND phase = 'status_read' ORDER BY ordinal")
        assert [(r["ordinal"], r["state"]) for r in attempts] == [(1, "scheduled"), (2, "scheduled"), (3, "completed")]
        assert await dispatcher.reconcile() == 0
    finally:
        await db.close()


async def test_status_read_auth_error_blocks_automatic_reconciliation(db: Database) -> None:
    await _effect(db, "provider.resume", "denied")
    store = OutboxStore(db)
    claim = await store.claim(("provider.resume",))
    assert claim is not None
    assert await store.finish(claim, state="unknown")

    class DeniedStatus:
        reads = 0

        async def run(self, claim, check):
            raise AssertionError("resume must not repeat")

        async def reconcile(self, claim):
            self.reads += 1
            raise PermissionError("status access revoked")

    handler = DeniedStatus()
    dispatcher = EffectDispatcher(store)
    dispatcher.register("provider.resume", handler)
    assert await dispatcher.reconcile() == 0
    assert await dispatcher.reconcile() == 0 and handler.reads == 1
    row = await db.fetchone("SELECT state,retry_blocked FROM effect_outbox WHERE id = 'denied'")
    assert (row["state"], row["retry_blocked"]) == ("unknown", 1)
