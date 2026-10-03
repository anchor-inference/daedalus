"""Approved provider continuations keep one durable model across delivery and restart."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from protocore.contracts.types import Run, RunStatus

from daedalus.config import ModelPresetConfig, Settings
from daedalus.extensions.api_provider_holds import register
from daedalus.extensions.provider_resume import ProviderResumeEffect
from daedalus.host.engine_factory import TENANT
from daedalus.host.inference_admission import HostInferenceAdmission
from daedalus.host.session_runner import SessionManager
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.providers.pricing import ModelPricing
from daedalus.stores.control import ControlStore, Entity, Scope
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.provider_holds import PROVIDER_RESUME_NOTE
from tests.support.models import model_config
from tests.support.waiting import until


def attach_adapters(manager: SessionManager, handler):
    seen = []

    def fallback(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "fallback"}, "finish_reason": "stop"}]})

    def adapter(identity, model, transport):
        return OpenAICompatibleProvider(
            ProviderEndpoint(id=identity, kind="openai_compat", base_url="http://127.0.0.1/v1",
                             pricing={model: ModelPricing(input=1, output=1, input_limit=1_000_000,
                                                          limit_source="test-provider-ceiling")}),
            client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
            usage_sink=manager.usage, admission=HostInferenceAdmission(manager),
        )

    manager.providers._providers = {
        "held": adapter("held", "held-model", handler),
        "fallback": adapter("fallback", "fallback-model", fallback),
    }
    return seen


async def prepared(settings: Settings, db: Database, handler, *, approve: bool = True):
    config = model_config()
    config.limits.usd_per_run = 0
    config.limits.usd_total = 0
    config.presets = {
        "held": ModelPresetConfig(provider="held", model="held-model"),
        "fallback": ModelPresetConfig(provider="fallback", model="fallback-model"),
    }
    config.model.preset = "held"
    config.model.chain = ["fallback"]
    settings.usd_per_day = 0
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    seen = attach_adapters(manager, handler)
    state = await manager.create_session("Held provider work")
    session = await db.fetchone("SELECT project_id FROM sessions WHERE id = ?", (state.session.id,))
    project_id = session["project_id"] or None
    failed = Run(id="failed", tenant_id=TENANT, session_id=state.session.id, status=RunStatus.error)
    await manager.runs.create(failed)
    state.run_id = failed.id
    observed = datetime.now(UTC)
    await db.execute(
        "INSERT INTO provider_failure_observations(id,session_id,run_id,project_id,provider_id,provider_kind,"
        "model,status,failure_class,reset_at,reset_source,evidence_digest,observed_at,provider_source_digest)"
        " VALUES ('refusal',?,'failed',?,'held','openai_compat','held-model',429,'rate',?,?,?,?,?)",
        (state.session.id, project_id, (observed - timedelta(seconds=1)).isoformat(),
         "test-provider-declared-reset", "a" * 64, observed.isoformat(),
         manager.providers.get("held").endpoint.source_digest()),
    )
    outbox = OutboxStore(db)
    if not approve:
        return manager, state, None, outbox, seen
    scope = Scope("project", project_id) if project_id else Scope("global", "global")
    entity = Entity("project", project_id) if project_id else Entity("collection", "global")
    async with db.transaction() as conn:
        revision = await ControlStore(db)._entity(conn, scope, entity)
    api = FastAPI()
    register(api, SimpleNamespace(db=db, extensions={"effects": SimpleNamespace(notify=lambda: None)}),
             lambda: {"via": "token", "user_id": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://127.0.0.1") as client:
        saved = await client.post("/api/providers/held/holds", json={
            "observation_id": "refusal", "expected_entity_revision": revision, "client_operation_id": "hold",
        })
        assert saved.status_code == 200, saved.text
        queued = await client.post(f"/api/providers/held/holds/{saved.json()['hold_id']}/resume", json={
            "expected_entity_revision": saved.json()["entity_revision"], "client_operation_id": "resume",
        })
        assert queued.status_code == 200, queued.text
    claim = await outbox.claim(("provider.resume",))
    assert claim is not None
    return manager, state, claim, outbox, seen


def successful(request):
    body = json.loads(request.content)
    assert body["model"] == "held-model"
    assert any(PROVIDER_RESUME_NOTE in str(item) for item in body["messages"])
    text = 'data: {"choices":[{"delta":{"content":"accepted reply"},"finish_reason":null}]}\n\n'
    text += 'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
    text += 'data: [DONE]\n\n'
    return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})


async def test_actual_manager_delivery_pins_one_model_and_exact_replay(settings, db):
    sent = []
    manager, state, claim, outbox, fallback = await prepared(
        settings, db, lambda request: sent.append(request) or successful(request),
    )
    try:
        handler = ProviderResumeEffect(SimpleNamespace(db=db, manager=manager))
        result = await handler.run(claim, outbox.check)
        assert result.state == "completed"
        await until(lambda: not state.running)
        assert len(sent) == 1 and fallback == []
        pin = await db.fetchone("SELECT run_id FROM provider_hold_events WHERE event = 'run_pinned'")
        assert pin is not None and pin["run_id"] != "failed"
        receipt = await manager.live.receipt(state.session.id, f"provider-resume:{claim.id}")
        assert receipt is not None and receipt["status"] == "consumed" and receipt["run_id"] == pin["run_id"]
        replay = await manager.resume_provider_hold(
            state.session.id, "failed", "held", "held-model", client_message_id=f"provider-resume:{claim.id}",
        )
        assert replay == pin["run_id"] and len(sent) == 1
        assert (await db.fetchone("SELECT count(*) FROM runs WHERE id != 'failed'"))[0] == 1
    finally:
        await manager.close()


async def test_crash_after_run_pin_rebuilds_approved_model_without_default_chain(settings, db, monkeypatch):
    sent = []
    manager, state, claim, outbox, fallback = await prepared(
        settings, db, lambda request: sent.append(request) or successful(request),
    )
    original = manager._publish

    async def crash(state, name, payload):
        if name == "run.started":
            raise RuntimeError("lost process after durable pin before drive")
        return await original(state, name, payload)

    monkeypatch.setattr(manager, "_publish", crash)
    try:
        result = await ProviderResumeEffect(SimpleNamespace(db=db, manager=manager)).run(claim, outbox.check)
        assert result.state == "unknown" and sent == [] and fallback == []
        pin = await db.fetchone("SELECT run_id FROM provider_hold_events WHERE event = 'run_pinned'")
        assert pin is not None
        manager.config.model.preset = "fallback"
        configuration = manager.config
        await manager.close()
        manager = SessionManager(settings, configuration, db=db)
        await manager.start()
        new_fallback = attach_adapters(manager, lambda request: sent.append(request) or successful(request))
        resumed = await manager.resume_unfinished()
        assert resumed == [pin["run_id"]]
        state = await manager.get_state(state.session.id)
        assert state is not None and state.configured_model == "held:held-model"
        await until(lambda: not state.running)
        resolution = await ProviderResumeEffect(SimpleNamespace(db=db, manager=manager)).reconcile(claim)
        assert resolution is not None and resolution.state == "completed"
        assert len(sent) == 1 and fallback == [] and new_fallback == []
        assert (await db.fetchone("SELECT count(*) FROM runs WHERE id != 'failed'"))[0] == 1
    finally:
        await manager.close()


@pytest.mark.parametrize("field,value", [("api_key", "replacement-test-key"),
                                         ("base_url", "http://127.0.0.1/new-endpoint")])
async def test_changed_provider_connection_is_refused_before_input_or_transport(settings, db, field, value):
    sent = []
    manager, state, claim, outbox, fallback = await prepared(
        settings, db, lambda request: sent.append(request) or successful(request),
    )
    try:
        setattr(manager.providers.get("held").endpoint, field, value)
        with pytest.raises(RuntimeError, match="connection changed"):
            await manager.resume_provider_hold(
                state.session.id, "failed", "held", "held-model", client_message_id=f"provider-resume:{claim.id}",
            )
        assert sent == [] and fallback == []
        assert await manager.live.receipt(state.session.id, f"provider-resume:{claim.id}") is None
        assert (await db.fetchone("SELECT count(*) FROM runs"))[0] == 1
    finally:
        await manager.close()


async def test_declared_reset_suppresses_timer_before_operator_chooses_hold(settings, db):
    manager, state, claim, outbox, fallback = await prepared(settings, db, successful, approve=False)
    delivered = []

    async def submit(*args, **kwargs):
        delivered.append(args)
        return "unexpected"

    manager.submit = submit
    try:
        await manager._recover_from_outage(state, "failed", 0)
        assert delivered == [] and fallback == []
    finally:
        await manager.close()


async def test_model_change_waits_for_continuation_pin_and_cannot_override_it(settings, db, monkeypatch):
    building, allow_build, sending, allow_response, changing = (asyncio.Event() for _ in range(5))
    sent = []

    async def transport(request):
        sent.append(request)
        sending.set()
        await allow_response.wait()
        return successful(request)

    manager, state, claim, outbox, fallback = await prepared(settings, db, transport)
    original = manager._build_engine

    async def paused_build(*args, **kwargs):
        building.set()
        await allow_build.wait()
        return await original(*args, **kwargs)

    async def change_model():
        changing.set()
        await manager.set_model(state.session.id, model_name="fallback-model", provider="fallback")

    monkeypatch.setattr(manager, "_build_engine", paused_build)
    resume = asyncio.create_task(manager.resume_provider_hold(
        state.session.id, "failed", "held", "held-model", client_message_id=f"provider-resume:{claim.id}",
    ))
    change = None
    try:
        await asyncio.wait_for(building.wait(), 5)
        change = asyncio.create_task(change_model())
        await asyncio.wait_for(changing.wait(), 5)
        assert not change.done()
        allow_build.set()
        run_id = await asyncio.wait_for(resume, 5)
        with pytest.raises(RuntimeError, match="continuation is pinned"):
            await asyncio.wait_for(change, 5)
        await asyncio.wait_for(sending.wait(), 5)
        assert state.run_id == run_id and state.configured_model == "held:held-model"
        overrides = await manager.live.load(state.session.id)
        assert all(overrides[key] is None for key in ("model_name", "provider", "preset"))
        allow_response.set()
        await until(lambda: not state.running)
        assert len(sent) == 1 and fallback == []
    finally:
        allow_build.set()
        allow_response.set()
        await asyncio.gather(resume, *([change] if change is not None else []), return_exceptions=True)
        await manager.close()
