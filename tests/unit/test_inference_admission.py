"""Every provider call path reserves before transport and settles only observed priced usage."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from protocore.contracts.llm import LLMObservabilityContext, LLMProviderError, LLMRequest, LLMTimeoutError
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import RuntimeConfig
from daedalus.extensions.runtime_observations import admit_native_run
from daedalus.host.inference_admission import HostInferenceAdmission
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.providers.pricing import ModelPricing
from daedalus.stores.control import ControlStore, Principal
from daedalus.stores.database import Database
from daedalus.stores.sqlite import SqliteUsageSink
from tests.unit.test_execution_ownership import owner
from tests.unit.test_runtime_observations import native_session


@pytest.fixture
def manager(db: Database):
    config = RuntimeConfig()
    config.limits.usd_per_run = 0
    config.limits.usd_total = 0.0001
    return SimpleNamespace(db=db, config=config, settings=SimpleNamespace(usd_per_day=0),
                           live_state=lambda _: None, mode_for=lambda _: None)


def request(**kwargs) -> LLMRequest:
    return LLMRequest(model="model", max_tokens=20,
                      messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])],
                      **kwargs)


def endpoint(kind: str = "openai_compat") -> ProviderEndpoint:
    return ProviderEndpoint(id="test", kind=kind, base_url="http://127.0.0.1", pricing={
        "model": ModelPricing(input=1, output=1, input_limit=50, limit_source="test-provider-ceiling"),
    })


def answer(raw=None, text="hello") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 2, "completion_tokens": 3} if raw is None else raw})


def provider(manager, handler, *, configured=None):
    return OpenAICompatibleProvider(configured or endpoint(),
                                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                    usage_sink=SqliteUsageSink(manager.db), admission=HostInferenceAdmission(manager))


async def test_native_worker_can_finish_its_reply_but_revocation_prevents_another_send(manager) -> None:
    store, identity, _ = await owner(manager.db)
    manager.execution_store = store
    sends = []
    adapter = provider(manager, lambda sent: sends.append(sent) or answer())
    observed = request(observability=LLMObservabilityContext(tenant_id="tenant", session_id="native", run_id="run"))
    try:
        await native_session(manager.db)
        await admit_native_run(SimpleNamespace(db=manager.db, executions=store), "staff-session", "native", "run")
        await adapter.complete_text(observed)
        async with manager.db.transaction() as conn:
            await store.complete(conn, identity, outcome="complete")
        await adapter.complete_text(observed)
        await ControlStore(manager.db).revoke_grant(Principal.operator({"via": "cookie", "user_id": 1}),
                                                  identity.principal.grant_id, reason="withdraw approval")
        with pytest.raises(LLMProviderError, match="inference authority"):
            await adapter.complete_text(observed)
        assert len(sends) == 2
        assert len(await manager.db.fetchall("SELECT id FROM inference_reservations")) == 2
    finally:
        await adapter.aclose()
        store.release()


@pytest.mark.parametrize("change", ["contract", "replacement", "ended", "run", "cancelled", "host", "owner", "child"])
@pytest.mark.parametrize("kind", ["openai_compat", "llamacpp"])
async def test_stale_worker_and_child_calls_are_fenced_before_transport(manager, change, kind) -> None:
    store, identity, _ = await owner(manager.db)
    manager.execution_store = store
    sends = []
    adapter = provider(manager, lambda sent: sends.append(sent) or answer(), configured=endpoint(kind))
    session_id, run_id = "native", "run"
    try:
        await native_session(manager.db)
        await admit_native_run(SimpleNamespace(db=manager.db, executions=store), "staff-session", "native", "run")
        if change == "contract":
            await manager.db.execute("UPDATE board_tasks SET contract_revision = 2 WHERE id = 'task'")
        elif change == "replacement":
            await manager.db.execute("UPDATE board_tasks SET current_attempt_id = NULL WHERE id = 'task'")
        elif change == "ended":
            await manager.db.execute("UPDATE staff_sessions SET ended_at = 'now' WHERE id = 'staff-session'")
        elif change == "run":
            await manager.db.execute("UPDATE runs SET status = 'completed' WHERE id = 'run'")
        elif change == "cancelled":
            await manager.db.execute("UPDATE execution_attempts SET state = 'cancelled' WHERE id = ?", (identity.id,))
        elif change == "host":
            store.release()
            store.acquire()
            await store.boot()
        elif change == "owner":
            manager.execution_store = None
        elif change == "child":
            session_id, run_id = "child", "child-run"
            await manager.db.execute("INSERT INTO sessions(id,tenant_id,project_id,created_at,last_message_at,metadata)"
                                     " VALUES ('child','tenant','project','now','now',?)", (json.dumps({"subagent_of": "native"}),))
            await manager.db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                                     " VALUES ('child-run','tenant','child','running','now','now')")
            await ControlStore(manager.db).revoke_grant(Principal.operator({"via": "cookie", "user_id": 1}),
                                                      identity.principal.grant_id, reason="withdraw parent approval")
        observed = request(observability=LLMObservabilityContext(tenant_id="tenant", session_id=session_id, run_id=run_id))
        with pytest.raises(LLMProviderError, match="inference authority"):
            await adapter.complete_text(observed)
        assert sends == []
        assert await manager.db.fetchall("SELECT id FROM inference_reservations") == []
    finally:
        await adapter.aclose()
        store.release()


@pytest.mark.parametrize("method", ["complete_text", "complete_structured", "stream_with_tools"])
async def test_every_call_path_reserves_before_transport_and_settles_actual_usage(manager, method) -> None:
    async def respond(sent):
        row = await manager.db.fetchone("SELECT state,quoted_microusd FROM inference_reservations")
        assert row[:] == ("inflight", 70)
        body = json.loads(sent.content)
        assert body["max_tokens"] == 20
        if method == "stream_with_tools":
            chunks = [{"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}]},
                      {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}]
            return httpx.Response(200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)+"data: [DONE]\n\n")
        return answer(text='{"answer":"hello"}' if method == "complete_structured" else "hello")

    adapter = provider(manager, respond)
    try:
        if method == "stream_with_tools":
            assert [delta async for delta in adapter.stream_with_tools(request())]
        elif method == "complete_structured":
            await adapter.complete_structured(request(), {})
        else:
            await adapter.complete_text(request())
        row = await manager.db.fetchone("SELECT state,actual_microusd,usage_event_seq FROM inference_reservations")
        assert row["state"] == "settled" and row["actual_microusd"] == 5 and row["usage_event_seq"] is not None
        usage = await manager.db.fetchone("SELECT inference_reservation_id,cost_usd FROM usage_events")
        assert usage["inference_reservation_id"] and usage["cost_usd"] == 0.000005
    finally:
        await adapter.aclose()


async def test_parallel_provider_calls_cannot_cross_the_same_remaining_balance(manager) -> None:
    sent = asyncio.Event()
    release = asyncio.Event()
    sends = []

    async def respond(req):
        sends.append(req)
        sent.set()
        await release.wait()
        return answer()

    adapter = provider(manager, respond)
    running = asyncio.create_task(adapter.complete_text(request()))
    try:
        await sent.wait()
        with pytest.raises(LLMProviderError, match="available balance"):
            await adapter.complete_text(request())
        assert len(sends) == 1
        release.set()
        await running
        await adapter.complete_text(request())
        assert len(sends) == 2
    finally:
        release.set()
        await running
        await adapter.aclose()


async def test_lost_response_stays_unknown_after_restart_but_does_not_hold_the_cap(manager) -> None:
    # A lost response used to hold its worst case for good, so a few of them closed the cap for every
    # later call. The retry goes ahead; the lost call's spend stays recorded as unknown.
    sends = []

    def lost(req):
        sends.append(req)
        if len(sends) == 1:
            raise httpx.ReadTimeout("the response was lost")
        return answer()

    adapter = provider(manager, lost)
    try:
        with pytest.raises(LLMTimeoutError):
            await adapter.complete_text(request())
        assert (await manager.db.fetchone("SELECT state FROM inference_reservations"))[0] == "unknown"
        await manager.db.close()
        await manager.db.open()
        await adapter.complete_text(request())
        assert len(sends) == 2
        states = [row[0] for row in await manager.db.fetchall("SELECT state FROM inference_reservations ORDER BY created_at")]
        assert sorted(states) == ["settled", "unknown"]
    finally:
        await adapter.aclose()


async def test_missing_usage_is_unknown_not_zero_and_does_not_block_the_next_call(manager) -> None:
    adapter = provider(manager, lambda _: answer(raw={}))
    try:
        await adapter.complete_text(request())
        assert (await manager.db.fetchone("SELECT state FROM inference_reservations"))[0] == "unknown"
        assert (await manager.db.fetchone("SELECT cost_usd FROM usage_events"))[0] is None
        await adapter.complete_text(request())
        assert (await manager.db.fetchone("SELECT count(*) FROM usage_events WHERE cost_usd IS NULL"))[0] == 2
    finally:
        await adapter.aclose()


async def test_a_priced_call_over_a_measured_cap_is_still_refused_with_the_balance(manager) -> None:
    await manager.db.execute("INSERT INTO usage_events(at,provider_id,model,purpose,cost_usd,raw)"
                             " VALUES ('2026-01-01','test','model','text',0.00005,'{}')")
    sends = []
    adapter = provider(manager, lambda req: sends.append(req) or answer())
    try:
        with pytest.raises(LLMProviderError, match=r"\$0\.000050 is the available balance"):
            await adapter.complete_text(request())
        assert sends == []
    finally:
        await adapter.aclose()


async def test_rate_card_is_pinned_even_if_registry_prices_change_during_transport(manager) -> None:
    configured = endpoint()

    def respond(_):
        configured.pricing["model"] = ModelPricing(input=999, output=999, input_limit=50, limit_source="new")
        return answer()

    adapter = provider(manager, respond, configured=configured)
    try:
        await adapter.complete_text(request())
        assert (await manager.db.fetchone("SELECT actual_microusd FROM inference_reservations"))[0] == 5
    finally:
        await adapter.aclose()


async def test_capped_unknown_input_bound_goes_ahead_unreserved(manager) -> None:
    # A subscription model publishes no per-token price or input ceiling. Refusing it under any cap
    # stopped every coordinator on one; it runs, and its spend shows as unknown.
    sends = []
    configured = endpoint()
    configured.pricing["model"].input_limit = None
    adapter = provider(manager, lambda req: sends.append(req) or answer(), configured=configured)
    try:
        await adapter.complete_text(request())
        assert len(sends) == 1 and await manager.db.fetchall("SELECT id FROM inference_reservations") == []
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("raw", [{}, {"prompt_tokens": 2, "completion_tokens": 3}])
async def test_known_local_model_does_not_depend_on_an_unknown_hosted_balance(manager, raw) -> None:
    await manager.db.execute("INSERT INTO usage_events(at,provider_id,model,purpose,cost_usd,raw)"
                             " VALUES ('2026-01-01','hosted','other','text',NULL,'{}')")
    manager.config.limits.usd_total = 0.000001
    adapter = provider(manager, lambda _: answer(raw=raw), configured=endpoint("llamacpp"))
    try:
        await adapter.complete_text(request())
        assert (await manager.db.fetchone("SELECT state,actual_microusd FROM inference_reservations"))[:] == ("settled", 0)
    finally:
        await adapter.aclose()


async def test_child_call_consumes_each_ancestor_session_balance(manager) -> None:
    manager.config.limits.usd_total = 0
    await manager.db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Project','2026-01-01','{}')")
    for identity, metadata in (("parent", {"usd_cap": 0.0001}), ("child", {"subagent_of": "parent"})):
        await manager.db.execute("INSERT INTO sessions(id,tenant_id,created_at,last_message_at,project_id,metadata)"
                                 " VALUES (?,'tenant','2026-01-01','2026-01-01','project',?)", (identity, json.dumps(metadata)))
    parent_request = request(observability=LLMObservabilityContext(tenant_id="tenant", session_id="parent"))
    child_request = request(observability=LLMObservabilityContext(tenant_id="tenant", session_id="child"))
    calls = []
    adapter = provider(manager, lambda req: calls.append(req) or answer(
        raw={"prompt_tokens": 2, "completion_tokens": 3, "cost": 0.00005}))
    try:
        await adapter.complete_text(child_request)
        row = await manager.db.fetchone("SELECT scope_key FROM inference_reservation_scopes WHERE scope_key = 'session:parent'")
        assert row is not None
        with pytest.raises(LLMProviderError, match="session:parent.*available balance"):
            await adapter.complete_text(parent_request)
        assert len(calls) == 1
    finally:
        await adapter.aclose()


async def test_closing_a_partial_stream_keeps_its_possible_charge_and_closes_transport(manager) -> None:
    closed = []

    class Partial(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    adapter = provider(manager, lambda _: httpx.Response(200, stream=Partial()))
    stream = adapter.stream_with_tools(request())
    try:
        assert (await anext(stream)).content == "hello"
        await stream.aclose()
        assert closed == [True]
        assert (await manager.db.fetchone("SELECT state FROM inference_reservations"))[0] == "unknown"
        assert await manager.db.fetchall("SELECT seq FROM usage_events") == []
    finally:
        await stream.aclose()
        await adapter.aclose()


async def test_cancelled_text_call_cannot_refund_a_send_that_already_started(manager) -> None:
    sent = asyncio.Event()

    async def respond(_):
        if not sent.is_set():
            sent.set()
            await asyncio.Event().wait()
        return answer()

    adapter = provider(manager, respond)
    running = asyncio.create_task(adapter.complete_text(request()))
    try:
        await sent.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert (await manager.db.fetchone("SELECT state FROM inference_reservations"))[0] == "unknown"
        # The cancelled call is not refunded as zero, and it does not stop the next one either.
        await adapter.complete_text(request())
    finally:
        await adapter.aclose()


async def test_a_missing_output_rate_cannot_turn_a_priced_input_into_a_free_reply(manager) -> None:
    sends = []
    configured = endpoint()
    configured.pricing["model"] = ModelPricing.from_entry({"input": 1, "input_limit": 50, "limit_source": "test"})
    adapter = provider(manager, lambda req: sends.append(req) or answer(), configured=configured)
    try:
        # The call runs unreserved, and its cost stays unknown rather than becoming zero.
        await adapter.complete_text(request())
        assert len(sends) == 1
        assert await manager.db.fetchall("SELECT id FROM inference_reservations") == []
        assert configured.pricing["model"].cost({"input_tokens": 2, "output_tokens": 3}) is None
    finally:
        await adapter.aclose()
