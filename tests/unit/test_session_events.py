"""What the session runner tells the event bus, and the unread-result mark it keeps."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.presence import PresenceReport
from daedalus.host.session_runner import SessionManager, SessionState
from daedalus.stores.database import Database
from tests.support.waiting import until_await
from tests.unit.test_components import HEAD, FakeApp
from tests.unit.test_session_runner import ScriptedProvider, _manager, _wait_finished

ASK = {"tool": "AskUser", "args": {"questions": [{"question": "Color?", "options": [{"label": "Red", "description": "warm"}, {"label": "Blue"}]}]}}


async def events(manager: SessionManager, *types: str, session_id: str | None = None) -> list[AppEvent]:
    return await manager.bus.replay(0, EventFilter(types=types, session_id=session_id), limit=1000)


async def run_once(manager: SessionManager, state: SessionState, text: str = "go", **submit: Any) -> str:
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, text, **submit)
    return (await waiter)[-1][2]


async def attend(manager: SessionManager, session_id: str, *, client: str = "tab-1") -> None:
    await manager.presence.report(PresenceReport(client=client, visible=True, focused=True, sessions=(session_id,)))


async def test_a_run_with_a_question_tells_its_whole_story(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([ASK, {"text": "you chose Blue"}]))
    try:
        state = await manager.create_session("colours")
        sid = state.session.id
        assert await run_once(manager, state, "ask me") == "awaiting"
        started = await events(manager, "run.started")
        assert len(started) == 1 and started[0].session_id == sid
        assert started[0].payload == {"run_id": state.run_id, "origin": "operator", "title": "colours"}
        pending = (await events(manager, "ask.pending"))[0].payload
        call = state.pending.tool_call_id if state.pending else ""
        assert pending["request_id"] == call and pending["request_ref"] == f"ask:{sid}:{call}"
        assert pending["questions"] == [{"question": "Color?", "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": ""}], "multi": False, "custom": False}]
        assert pending["operator_facing"] is True and pending["telegram"] is True
        assert not await events(manager, "run.finished"), "a run parked on a question has not finished"

        waiter = asyncio.create_task(_wait_finished(manager))
        await manager.answer(sid, [{"selected": ["Blue"]}], via="app")
        assert (await waiter)[-1][2] == "completed"
        assert len(await events(manager, "run.started")) == 1, "answering continues the run; it starts none"
        answered = (await events(manager, "ask.answered"))[0].payload
        assert answered == {"request_id": call, "request_ref": f"ask:{sid}:{call}", "via": "app"}
        statuses = [e.payload["status"] for e in await events(manager, "session.status")]
        assert statuses == ["running", "waiting", "running", "idle"]
        finished = (await events(manager, "run.finished"))[0].payload
        assert finished["status"] == "completed" and finished["run_id"] == state.run_id
        assert finished["summary"] == "you chose Blue" and finished["duration_s"] >= 0
        assert finished["watched"] is False and finished["operator_facing"] is True and finished["telegram"] is True
    finally:
        await manager.close()


async def test_a_restored_question_is_announced_again(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([ASK]))
    state = await manager.create_session("t")
    assert await run_once(manager, state) == "awaiting"
    await manager.close()
    manager2 = await _manager(settings, db, ScriptedProvider([{"text": "ok"}]))
    try:
        await manager2.resume_unfinished()
        refs = [e.payload["request_ref"] for e in await events(manager2, "ask.pending")]
        assert len(refs) == 2 and refs[0] == refs[1], "the same request, which a subscriber dedupes"
    finally:
        await manager2.close()


async def test_finished_says_who_was_watching_and_who_the_run_was_for(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([{"text": "one"}, {"text": "two"}, {"text": "three"}, {"text": "four"}, {"text": "five"}]))
    try:
        watched = await manager.create_session("watched")
        await attend(manager, watched.session.id)
        await run_once(manager, watched)
        child = await manager.create_session("[sub] x", metadata={"subagent_of": watched.session.id})
        await run_once(manager, child, as_answer=False, origin="subagent-task:x")
        beat = await manager.create_session("heartbeat", metadata={"heartbeat": True, "unattended": True})
        await run_once(manager, beat, as_answer=False, origin="heartbeat")
        scheduled = await manager.create_session("nightly")
        await run_once(manager, scheduled, as_answer=False, origin="schedule")
        site = await manager.create_session("site", metadata={"telegram_detached": True})
        await run_once(manager, site)
        by_session = {e.session_id: e.payload for e in await events(manager, "run.finished")}
        assert by_session[watched.session.id]["watched"] is True and by_session[watched.session.id]["operator_facing"] is True
        assert by_session[child.session.id]["operator_facing"] is False
        assert by_session[beat.session.id]["operator_facing"] is False
        assert by_session[scheduled.session.id]["operator_facing"] is False and by_session[scheduled.session.id]["origin"] == "schedule"
        assert by_session[watched.session.id]["telegram"] is True and by_session[site.session.id]["telegram"] is False
        # A subagent never speaks in Telegram, whatever its leader does.
        assert by_session[child.session.id]["telegram"] is False
    finally:
        await manager.close()


async def test_an_unseen_result_is_marked_and_cleared_by_looking_or_writing(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([{"text": "one"}, {"text": "two"}, {"text": "three"}, {"text": "four"}]))
    try:
        site = await manager.create_session("site", metadata={"telegram_detached": True})
        sid = site.session.id
        await run_once(manager, site)
        mark = site.session.metadata.get("unread_result")
        assert mark and mark["run_id"] == site.run_id
        catalog = {row["id"]: row for row in await manager.session_catalog()}
        assert catalog[sid]["unread_result"] is True and catalog[sid]["needs_attention"] is True
        stored = await manager.sessions.get(sid, "daedalus")
        assert stored.metadata.get("unread_result"), "the mark survives a restart"
        assert [e.payload["unread"] for e in await events(manager, "session.unread_result")] == [True]

        # Opening the session clears it, through the presence event.
        await attend(manager, sid)

        async def cleared() -> bool:
            return not (await manager.sessions.get(sid, "daedalus")).metadata.get("unread_result")

        await until_await(cleared, "opening the session cleared the mark")
        assert not site.metadata.get("unread_result")
        assert [e.payload["unread"] for e in await events(manager, "session.unread_result")] == [True, False]

        # Watched while it ran: no mark.
        await run_once(manager, site)
        assert not site.metadata.get("unread_result")

        # Nobody watching: marked again. Writing to the session clears it; the run that message starts
        # ends unseen as well and leaves a mark of its own.
        manager.presence._clients.clear()
        await run_once(manager, site)
        assert site.metadata.get("unread_result")
        before = len(await events(manager, "session.unread_result"))
        await run_once(manager, site, "thanks")
        assert [e.payload["unread"] for e in await events(manager, "session.unread_result")][before:] == [False, True]

        # Delivered to Telegram: the operator has it there, so no mark.
        bound = await manager.create_session("bound")
        await run_once(manager, bound)
        assert not bound.metadata.get("unread_result")
    finally:
        await manager.close()


async def test_clear_unread_works_on_a_session_that_is_not_loaded(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        state = await manager.create_session("cold", metadata={"unread_result": {"run_id": "r1", "at": "2026-09-24T00:00:00Z"}})
        sid = state.session.id
        manager._states.pop(sid)
        assert await manager.clear_unread(sid) is True
        assert not (await manager.sessions.get(sid, "daedalus")).metadata.get("unread_result")
        assert await manager.clear_unread(sid) is False
        assert await manager.clear_unread("no-such-session") is False
    finally:
        await manager.close()


async def test_a_refused_call_is_one_request_until_it_is_answered(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        state = await manager.create_session("policy")
        sid = state.session.id
        manager.config.policy.egress_allow = ["github.com"]
        gate = manager.policy_gate(sid, "run-1")
        refused = gate.decide("Exec", {"command": "curl https://other.example/"})
        assert refused.action == "ask"
        gate.decide("Exec", {"command": "curl https://other.example/"})  # the agent retries: the same request

        async def published(count: int, *types: str) -> bool:
            return len(await events(manager, *types)) >= count

        await until_await(lambda: published(1, "permission.pending"), "the refusal was published")
        await manager.flush_background()
        await asyncio.sleep(0.05)
        pending = await events(manager, "permission.pending")
        assert len(pending) == 1
        payload = pending[0].payload
        assert payload["request_id"] == refused.key and payload["request_ref"] == f"policy:{sid}:{refused.key}"
        assert payload["kind"] == "policy" and payload["tool"] == "Exec" and "other.example" in payload["text"]
        assert payload["risk"] == "routine" and payload["quick"] is True

        await manager.grant(sid, refused.key, via="app")
        resolved = await events(manager, "permission.resolved")
        assert [(e.payload["decision"], e.payload["via"]) for e in resolved] == [("allow", "app")]
        assert refused.key not in (state.metadata.get("policy_pending") or {})
        assert gate.decide("Exec", {"command": "curl https://other.example/"}).action == "allow"
        again = gate.decide("Exec", {"command": "curl https://other.example/"})  # the grant is spent: a new request
        assert again.action == "ask"
        await until_await(lambda: published(2, "permission.pending"), "the new refusal was published")

        result = await manager.refuse(sid, again.key, via="telegram")
        assert result["refuses"]["tool"] == "Exec"
        resolved = await events(manager, "permission.resolved")
        assert [(e.payload["decision"], e.payload["via"]) for e in resolved] == [("allow", "app"), ("deny", "telegram")]
        assert again.key not in (state.metadata.get("policy_pending") or {})
        stored = await manager.sessions.get(sid, "daedalus")
        assert again.key not in (stored.metadata.get("policy_pending") or {})
    finally:
        await manager.close()


async def test_a_rule_about_the_machine_itself_is_elevated_and_never_quick(settings: Settings, db: Database) -> None:
    from daedalus.host.policy import Decision

    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        state = await manager.create_session("home")
        payload = manager._permission_payload(state, Decision("ask", "home", "host.home", key="0123456789ab"), {"tool": "Read", "text": "~/notes"})
        assert payload["risk"] == "elevated" and payload["quick"] is False
    finally:
        await manager.close()


def test_the_refuse_route_answers_from_the_app(tmp_path: Path) -> None:
    app = FakeApp(tmp_path, native=True)
    calls: list[tuple[str, str, str]] = []

    async def refuse(session_id: str, key: str, *, via: str) -> dict[str, Any]:
        if session_id == "missing":
            raise KeyError(session_id)
        if key != "0123456789ab":
            raise ValueError("an approval key is 12 hex characters")
        calls.append((session_id, key, via))
        return {"key": key, "refuses": None}

    app.manager.refuse = refuse  # type: ignore[attr-defined]
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        assert client.post("/api/sessions/s1/policy/refuse", json={"key": "0123456789ab"}).status_code == 401
        answer = client.post("/api/sessions/s1/policy/refuse", json={"key": "0123456789ab"}, headers=HEAD)
        assert answer.status_code == 200 and answer.json() == {"key": "0123456789ab", "refuses": None}
        assert client.post("/api/sessions/s1/policy/refuse", json={"key": "x"}, headers=HEAD).status_code == 400
        assert client.post("/api/sessions/missing/policy/refuse", json={"key": "0123456789ab"}, headers=HEAD).status_code == 404
    assert calls == [("s1", "0123456789ab", "app")]


async def test_a_session_made_or_deleted_anywhere_is_told_to_the_open_lists(settings: Settings, db: Database) -> None:
    """A chat made without a first message, by the agent, by a subagent, an import or a fork used to
    reach the sidebar only at its next minute's poll: nothing on the bus said it existed."""
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        leader = await manager.create_session("leader")
        child = await manager.create_session("[sub] helper", metadata={"subagent_of": leader.session.id}, project_id=leader.project.id)
        made = await events(manager, "session.created")
        assert [(e.session_id, e.payload) for e in made] == [
            (leader.session.id, {"title": "leader"}),
            (child.session.id, {"title": "[sub] helper", "parent": leader.session.id}),
        ]
        assert made[1].project_id == leader.project.id
        assert await manager.delete_session(child.session.id)
        [gone] = await events(manager, "session.deleted")
        assert gone.session_id == child.session.id and gone.payload == {"title": "[sub] helper"}
    finally:
        await manager.close()


async def test_a_grant_starts_the_agent_on_the_call_it_was_refused(settings: Settings, db: Database) -> None:
    """The operator allowed a refused call in the app and the agent sat idle until they also wrote
    "go on". The grant now tells the agent, which starts its turn."""
    provider = ScriptedProvider([{"text": "done"}])
    manager = await _manager(settings, db, provider)
    try:
        state = await manager.create_session("granted")
        sid = state.session.id
        manager.config.policy.egress_allow = ["github.com"]
        refused = manager.policy_gate(sid, "run-1").decide("Exec", {"command": "curl https://other.example/"})
        assert refused.action == "ask"
        await manager.flush_background()

        await manager.grant(sid, refused.key, via="app")

        def told() -> bool:
            return any("The operator allowed" in str(r.messages) and "other.example" in str(r.messages) for r in provider.requests)

        await until_await(lambda: asyncio.sleep(0, told()), "the agent was told about the grant")
        asked = len(provider.requests)

        # A key the host never saw refused grants on trust and resumes nothing.
        await manager.grant(sid, "0123456789ab", via="app")
        await asyncio.sleep(0.2)
        assert len(provider.requests) == asked
    finally:
        await manager.close()
