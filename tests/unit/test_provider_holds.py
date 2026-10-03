"""A provider hold is based on a durable, bounded reply from an exact run."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from protocore.contracts.llm import LLMObservabilityContext, LLMRateLimitError, LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.extensions.api_provider_holds import register
from daedalus.extensions.provider_resume import ProviderResumeEffect
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.provider_holds import (
    PROVIDER_RESUME_NOTE,
    ProviderHolds,
    create_hold_in,
    pin_resumed_run_in,
    pinned_target_in,
    recovery_candidate_in,
    resume_target_in,
    validate_hold_in,
)
from daedalus.stores.sqlite import LiveControlStore


class Admission:
    def __init__(self, db: Database) -> None:
        self.holds = ProviderHolds(db)

    async def start(self, endpoint, request, body):
        return None

    async def interrupted(self, reservation, reason):
        return None

    async def failure(self, endpoint, request, reservation, evidence):
        return await self.holds.failure(endpoint, request, reservation, evidence)


async def _run(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at) VALUES"
                     " ('project','work','2026-10-04T12:00:00+00:00')")
    await db.execute("INSERT INTO sessions(id,tenant_id,title,created_at,last_message_at,metadata,project_id)"
                     " VALUES ('session','tenant','work','2026-10-04T12:00:00+00:00',"
                     "'2026-10-04T12:00:00+00:00','{}','project')")
    await db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                     " VALUES ('failed','tenant','session','running','2026-10-04T12:00:00+00:00',"
                     "'2026-10-04T12:00:00+00:00')")


def _request() -> LLMRequest:
    return LLMRequest(model="model", max_tokens=20,
                      messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])],
                      observability=LLMObservabilityContext(session_id="session", run_id="failed"))


@pytest.mark.asyncio
async def test_http_refusal_is_saved_without_body_or_credentials(db: Database) -> None:
    await _run(db)
    body = json.dumps({"error": {"code": "rate_limit_exceeded", "message": "private prompt text"}})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(429, text=body, headers={"x-ratelimit-reset-requests": "30s"})

    provider = OpenAICompatibleProvider(
        ProviderEndpoint(id="vendor", kind="openai_compat", base_url="https://api.openai.com/v1",
                         api_key="secret"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), admission=Admission(db),
    )
    with pytest.raises(LLMRateLimitError):
        async for _ in provider.stream_with_tools(_request()):
            pass
    await provider.aclose()
    row = await db.fetchone("SELECT * FROM provider_failure_observations")
    assert row is not None
    assert (row["session_id"], row["run_id"], row["provider_id"], row["model"]) == (
        "session", "failed", "vendor", "model")
    assert row["failure_class"] == "rate"
    assert row["reset_at"] is not None
    assert "private prompt text" not in Path(db.path).read_bytes().decode("utf-8", "ignore")
    assert "secret" not in Path(db.path).read_bytes().decode("utf-8", "ignore")
    await db.execute("UPDATE runs SET status = 'error' WHERE id = 'failed'")
    async with db.transaction() as conn:
        assert (await validate_hold_in(conn, row["id"]))["id"] == row["id"]


@pytest.mark.asyncio
async def test_later_run_invalidates_a_previous_hold_candidate(db: Database) -> None:
    await _run(db)
    await db.execute("UPDATE runs SET status = 'error' WHERE id = 'failed'")
    await db.execute("INSERT INTO provider_failure_observations(id,session_id,run_id,provider_id,"
                     "provider_kind,model,status,failure_class,reset_at,reset_source,evidence_digest,observed_at)"
                     " VALUES ('refusal','session','failed','vendor','openai_compat','model',429,'rate',"
                     "'2026-10-04T12:01:00+00:00','x-ratelimit-reset-requests',?,'2026-10-04T12:00:00+00:00')",
                     ("a" * 64,))
    await db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                     " VALUES ('newer','tenant','session','running','2026-10-04T12:00:01+00:00',"
                     "'2026-10-04T12:00:01+00:00')")
    async with db.transaction() as conn:
        with pytest.raises(ControlConflict, match="another run"):
            await validate_hold_in(conn, "refusal")


@pytest.mark.asyncio
async def test_hold_command_replays_one_exact_operator_decision(db: Database) -> None:
    await _run(db)
    await db.execute("UPDATE runs SET status = 'error' WHERE id = 'failed'")
    reset = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    await db.execute("INSERT INTO provider_failure_observations(id,session_id,run_id,project_id,"
                     "provider_id,provider_kind,model,status,failure_class,reset_at,reset_source,"
                     "evidence_digest,observed_at) VALUES ('refusal','session','failed','project','vendor',"
                     "'openai_compat','model',429,'rate',?,'x-ratelimit-reset-requests',?,?)",
                     (reset, "a" * 64, datetime.now(UTC).isoformat()))
    control = ControlStore(db)
    actor = Principal.operator({"via": "token", "user_id": 1})

    async def effect(conn, mutation):
        return await create_hold_in(conn, hold_id=mutation.object_id, observation_id="refusal",
                                    receipt_id=mutation.receipt_id)

    args = (actor, Scope("project", "project"), "provider.hold", "same-request", 1,
            Entity("project", "project"), {"observation_id": "refusal"}, effect)
    first = await control.mutate(*args)
    again = await control.mutate(*args)
    assert again == first
    assert first["hold_id"]
    assert (await db.fetchone("SELECT count(*) AS n FROM provider_resume_holds"))["n"] == 1
    assert (await db.fetchone("SELECT count(*) AS n FROM provider_hold_events"))["n"] == 1
    async with db.transaction() as conn:
        assert await recovery_candidate_in(conn, "session", "failed")
        with pytest.raises(ControlConflict, match="effect identity"):
            await resume_target_in(conn, "session", "failed", "vendor", "model", "random")
    await db.execute("INSERT INTO provider_failure_observations(id,session_id,run_id,project_id,"
                     "provider_id,provider_kind,model,status,failure_class,evidence_digest,observed_at)"
                     " VALUES ('later','session','failed','project','vendor','openai_compat',"
                     "'model',401,'auth',?,'2026-10-04T12:00:01+00:00')", ("b" * 64,))
    async with db.transaction() as conn:
        assert await recovery_candidate_in(conn, "session", "failed")


@pytest.mark.asyncio
async def test_worker_observation_is_visible_but_cannot_resume_old_attempt(db: Database) -> None:
    await _run(db)
    await db.execute("UPDATE runs SET status = 'error' WHERE id = 'failed'")
    await db.execute("UPDATE sessions SET metadata = '{\"subagent_of\":\"parent\"}' WHERE id = 'session'")
    await db.execute("INSERT INTO provider_failure_observations(id,session_id,run_id,project_id,"
                     "provider_id,provider_kind,model,status,failure_class,reset_at,reset_source,"
                     "evidence_digest,observed_at) VALUES ('refusal','session','failed','project','vendor',"
                     "'openai_compat','model',429,'rate','2026-10-04T12:01:00+00:00',"
                     "'x-ratelimit-reset-requests',?,'2026-10-04T12:00:00+00:00')", ("a" * 64,))
    async with db.transaction() as conn:
        with pytest.raises(ControlConflict, match="worker or child"):
            await validate_hold_in(conn, "refusal")


@pytest.mark.asyncio
async def test_http_hold_and_resume_are_exact_receipted_commands(db: Database) -> None:
    await _run(db)
    await db.execute("UPDATE runs SET status = 'error' WHERE id = 'failed'")
    reset = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await db.execute("INSERT INTO provider_failure_observations(id,session_id,run_id,project_id,"
                     "provider_id,provider_kind,model,status,failure_class,reset_at,reset_source,"
                     "evidence_digest,observed_at) VALUES ('refusal','session','failed','project','vendor',"
                     "'openai_compat','model',429,'rate',?,'x-ratelimit-reset-requests',?,?)",
                     (reset, "a" * 64, datetime.now(UTC).isoformat()))
    api = FastAPI()
    app = SimpleNamespace(db=db, extensions={"effects": SimpleNamespace(notify=lambda: None)})
    register(api, app, lambda: {"via": "token", "user_id": 1})
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://127.0.0.1")
    path = "/api/providers/vendor/holds"
    before = await client.get("/api/providers/vendor/limits")
    assert before.status_code == 200
    assert before.json()["observations"][0]["hold_available"] is True
    command = {"observation_id": "refusal", "expected_entity_revision": 1,
               "client_operation_id": "same-hold"}
    first = await client.post(path, json=command)
    again = await client.post(path, json=command)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    hold_id = first.json()["hold_id"]
    resume = {"expected_entity_revision": 2, "client_operation_id": "same-resume"}
    queued = await client.post(f"{path}/{hold_id}/resume", json=resume)
    replay = await client.post(f"{path}/{hold_id}/resume", json=resume)
    assert queued.status_code == replay.status_code == 200
    assert queued.json() == replay.json()
    assert queued.json()["state"] == "resume_queued"
    assert (await db.fetchone("SELECT count(*) AS n FROM effect_outbox WHERE kind = 'provider.resume'"))["n"] == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_interrupted_resume_reconciles_only_consumed_pinned_run(db: Database) -> None:
    await _run(db)
    await db.execute("UPDATE runs SET status = 'error' WHERE id = 'failed'")
    reset = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await db.execute("INSERT INTO provider_failure_observations(id,session_id,run_id,project_id,"
                     "provider_id,provider_kind,model,status,failure_class,reset_at,reset_source,"
                     "evidence_digest,observed_at) VALUES ('refusal','session','failed','project','vendor',"
                     "'openai_compat','model',429,'rate',?,'x-ratelimit-reset-requests',?,?)",
                     (reset, "a" * 64, datetime.now(UTC).isoformat()))
    api = FastAPI()
    register(api, SimpleNamespace(db=db, extensions={"effects": SimpleNamespace(notify=lambda: None)}),
             lambda: {"via": "token", "user_id": 1})
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://127.0.0.1")
    first = await client.post("/api/providers/vendor/holds", json={"observation_id": "refusal",
                               "expected_entity_revision": 1, "client_operation_id": "hold"})
    assert first.status_code == 200
    queued = await client.post(f"/api/providers/vendor/holds/{first.json()['hold_id']}/resume",
                               json={"expected_entity_revision": 2, "client_operation_id": "resume"})
    assert queued.status_code == 200
    await client.aclose()
    outbox = OutboxStore(db)
    claimed = await outbox.claim(("provider.resume",))
    assert claimed is not None
    input_id = f"provider-resume:{claimed.id}"
    payload = {"text": PROVIDER_RESUME_NOTE, "steer": False, "as_answer": False,
               "origin": "core", "attachments": []}
    live = LiveControlStore(db)
    await live.accept("session", input_id, "input", payload)
    async with db.transaction() as conn:
        target = await resume_target_in(conn, "session", "failed", "vendor", "model", input_id)
        assert target["id"] == first.json()["hold_id"]
        created = datetime.now(UTC).isoformat()
        await conn.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                           " VALUES ('forged','tenant','session','running',?,?)", (created, created))
        await conn.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                           " VALUES ('continued','tenant','session','running',?,?)", (created, created))
        await pin_resumed_run_in(conn, action_id=claimed.id, session_id="session", run_id="continued")
        assert (await pinned_target_in(conn, "session", "continued"))["model"] == "model"
    # A crash after run creation cannot blindly redeliver the effect. Until input consumption,
    # the exact run remains inspectable but the outbox cannot claim completed delivery.
    await outbox.recover()
    unknown = (await outbox.unknown(("provider.resume",)))[0]
    handler = ProviderResumeEffect(SimpleNamespace(db=db, manager=SimpleNamespace(live=live)))
    assert await handler.reconcile(unknown) is None
    await live.consume("session", input_id, run_id="continued", step_id="resume", message_seq=None)
    await db.execute("UPDATE input_receipts SET run_id = 'forged' WHERE client_message_id = ?", (input_id,))
    assert await handler.reconcile(unknown) is None
    await db.execute("UPDATE input_receipts SET run_id = 'continued' WHERE client_message_id = ?", (input_id,))
    resolution = await handler.reconcile(unknown)
    assert resolution is not None and resolution.state == "completed"
    assert resolution.evidence["run_id"] == "continued"
    assert (await db.fetchone("SELECT state,resumed_run_id FROM provider_resume_holds"))["resumed_run_id"] == "continued"
