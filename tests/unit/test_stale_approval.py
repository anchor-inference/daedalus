"""An approval request is offered for as long as it is open, and closed for good once it is not.

The app found a refused call in the transcript and offered it as an open card unless that one screen
had pressed it, so a request answered days before — or finished by hand, then followed by other runs —
came back on every reload. The session detail now says which requests are open, the host closes the
unanswered ones when the next run starts and when it starts itself, and a stale button grants nothing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from daedalus.config import PolicyRuleConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.models import model_config
from tests.unit.test_session_runner import ScriptedProvider
from tests.unit.test_steer_queue import _client

REFUSED = {"tool": "Exec", "args": {"command": "echo approved-once"}}


async def _manager(settings: Settings, db: Database, provider: ScriptedProvider) -> SessionManager:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    manager.providers.rungs_for = lambda config: [(provider, "scripted-model")]  # type: ignore[method-assign]
    # An operator rule makes a harmless command a question, so an allowed retry runs and touches nothing.
    manager.config.policy.rules = [PolicyRuleConfig(id="ask.echo", tool="Exec", pattern="approved-once", action="ask")]
    return manager


async def _run(manager: SessionManager, session_id: str, text: str) -> None:
    done = asyncio.Event()

    async def finished(sid: str, run_id: str, status: str) -> None:
        if sid == session_id:
            done.set()

    manager.on_finished(finished)
    await manager.submit(session_id, text)
    await asyncio.wait_for(done.wait(), timeout=30)
    state = await manager.get_state(session_id)
    assert state is not None
    while state.running:
        await asyncio.sleep(0.01)


async def _open(client: httpx.AsyncClient, session_id: str) -> list[str]:
    """What a fresh page load of the chat is told is still waiting on the operator."""
    response = await client.get(f"/api/sessions/{session_id}", headers={"x-daedalus-token": "tok"})
    assert response.status_code == 200
    return list(response.json()["open_approvals"])


async def _refused_in_a_run(manager: SessionManager, provider: ScriptedProvider) -> tuple[str, str]:
    """A chat whose run was refused a call and ended its turn to wait, as the refusal tells it to."""
    state = await manager.create_session("approvals")
    sid = state.session.id
    provider.script[:0] = [REFUSED, {"text": "waiting for your answer"}]
    await _run(manager, sid, "fetch the page")
    pending = state.metadata.get("policy_pending") or {}
    assert len(pending) == 1, pending
    return sid, next(iter(pending))


async def _resolutions(db: Database, key: str) -> list[dict[str, Any]]:
    rows = await db.fetchall("SELECT payload_json FROM app_events WHERE type = 'permission.resolved' ORDER BY seq")
    return [p for p in (json.loads(r["payload_json"]) for r in rows) if p["request_id"] == key]


async def test_a_genuinely_open_request_is_offered_after_its_run_ends_and_its_allow_still_works(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([])
    manager = await _manager(settings, db, provider)
    try:
        sid, key = await _refused_in_a_run(manager, provider)
        async with _client(settings, db, manager) as client:
            # The run that asked is over, and the request is exactly what the session waits on.
            assert await _open(client, sid) == [key]
            provider.script[:0] = [REFUSED, {"text": "fetched"}]
            done = asyncio.Event()

            async def finished(session_id: str, run_id: str, status: str) -> None:
                done.set()

            manager.on_finished(finished)
            granted = await client.post(f"/api/sessions/{sid}/policy/grant", json={"key": key}, headers={"x-daedalus-token": "tok"})
            assert granted.status_code == 200 and granted.json()["grants"]["approves"]["tool"] == "Exec"
            await asyncio.wait_for(done.wait(), timeout=30)
            # The grant resumed the agent, its retry went through once, and a reload offers nothing.
            results = [r for m in await manager.transcript(sid) for r in m.content_blocks if getattr(r, "kind", "") == "tool_result"]
            assert results[-1].is_error is False
            assert await _open(client, sid) == []
            assert [p["decision"] for p in await _resolutions(db, key)] == ["allow"]
    finally:
        await manager.close()


async def test_an_answered_request_is_not_offered_again_on_a_reload(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([])
    manager = await _manager(settings, db, provider)
    try:
        sid, key = await _refused_in_a_run(manager, provider)
        async with _client(settings, db, manager) as client:
            refused = await client.post(f"/api/sessions/{sid}/policy/refuse", json={"key": key}, headers={"x-daedalus-token": "tok"})
            assert refused.status_code == 200
            for _reload in range(2):
                assert await _open(client, sid) == []
    finally:
        await manager.close()


async def test_a_second_device_answering_closes_the_request_for_the_first(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([])
    manager = await _manager(settings, db, provider)
    try:
        sid, key = await _refused_in_a_run(manager, provider)
        async with _client(settings, db, manager) as phone, _client(settings, db, manager) as desk:
            assert await _open(desk, sid) == [key] and await _open(phone, sid) == [key]
            await phone.post(f"/api/sessions/{sid}/policy/refuse", json={"key": key}, headers={"x-daedalus-token": "tok"})
            # The desk hears it on the app's event stream and reads the detail again.
            assert [(p["decision"], p["via"]) for p in await _resolutions(db, key)] == [("deny", "app")]
            assert await _open(desk, sid) == []
    finally:
        await manager.close()


async def test_a_later_run_closes_the_request_its_predecessor_left_unanswered(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([])
    manager = await _manager(settings, db, provider)
    try:
        sid, key = await _refused_in_a_run(manager, provider)
        async with _client(settings, db, manager) as client:
            assert await _open(client, sid) == [key]
            # The operator did it by hand and went on: the next run is not waiting on the request.
            provider.script[:0] = [{"text": "noted"}]
            await _run(manager, sid, "never mind, I did it myself")
            assert await _open(client, sid) == []
            assert [(p["decision"], p["via"]) for p in await _resolutions(db, key)] == [("expired", "run")]
            stored = await db.fetchone("SELECT metadata FROM sessions WHERE id = ?", (sid,))
            assert stored is not None and not json.loads(stored["metadata"]).get("policy_pending")
            # A refusal the new run raises itself is a new request, offered again.
            provider.script[:0] = [REFUSED, {"text": "waiting again"}]
            await _run(manager, sid, "try once more")
            assert await _open(client, sid) == [key]
    finally:
        await manager.close()


async def test_a_restart_closes_the_requests_the_previous_process_left_open(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([])
    manager = await _manager(settings, db, provider)
    try:
        sid, key = await _refused_in_a_run(manager, provider)
    finally:
        await manager.close()
    restarted = await _manager(settings, db, provider)
    try:
        async with _client(settings, db, restarted) as client:
            assert await restarted.retire_requests_left_open() == 1
            assert await _open(client, sid) == []
            assert [(p["decision"], p["via"]) for p in await _resolutions(db, key)] == [("expired", "restart")]
            # Nothing left to close on the next start.
            assert await restarted.retire_requests_left_open() == 0
    finally:
        await restarted.close()


async def test_allowing_one_of_two_open_requests_leaves_the_other_to_be_answered(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([])
    manager = await _manager(settings, db, provider)
    try:
        state = await manager.create_session("two cards")
        sid = state.session.id
        other = {"tool": "Exec", "args": {"command": "echo approved-once, the other one"}}
        provider.script[:0] = [REFUSED, other, {"text": "waiting for both"}]
        await _run(manager, sid, "do both")
        first, second = list(state.metadata["policy_pending"])
        async with _client(settings, db, manager) as client:
            provider.script[:0] = [{"text": "done with the first"}]
            done = asyncio.Event()

            async def finished(session_id: str, run_id: str, status: str) -> None:
                done.set()

            manager.on_finished(finished)
            await client.post(f"/api/sessions/{sid}/policy/grant", json={"key": first}, headers={"x-daedalus-token": "tok"})
            await asyncio.wait_for(done.wait(), timeout=30)
            # The run the grant started is the answer to one card, not a reason to drop the other.
            assert await _open(client, sid) == [second]
            refused = await client.post(f"/api/sessions/{sid}/policy/refuse", json={"key": second}, headers={"x-daedalus-token": "tok"})
            assert refused.status_code == 200 and refused.json()["refuses"]["tool"] == "Exec"
    finally:
        await manager.close()
