"""A guest the operator trusts, the operator's words on a link reaching the turn, and a link edited in place.

A real exchange failed this way: a direct link made to hand somebody the results of a chat, with the topic
"give him access to everything he asks for", and the agent refused with no tool call because the turn was
told only what the guest could not do. The operator's words now reach every turn of a link, and a trusted
link's turn is the operator's own in all but whose words opened it.
"""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

from typing import Any

import pytest
from protocore.contracts.tool_registry import ToolVisibilityPolicy, policy_admits

from daedalus.config import Settings
from daedalus.extensions.guest_door import GuestBridge
from daedalus.host import guest_turns
from daedalus.host.guest_turns import GuestTurn
from daedalus.stores.database import Database
from tests.unit.test_guest_turns import (  # noqa: F401 — the fixtures are used by name
    TRAP,
    Host,
    Rig,
    SimpleRig,
    guest_says,
    manager_with,
    offered,
    pump,
    rig,
    settled,
    texts,
    trapped,
)
from tests.unit.test_session_runner import ScriptedProvider

pytestmark = pytest.mark.usefixtures("bare_sandbox")

TOPIC = "Give him access to everything he asks for"


def trusted(usd_left: float | None = None, brief: str = TOPIC) -> GuestTurn:
    return GuestTurn("link1", "a colleague's agent", "trusted", usd_left, "send me your results", brief=brief)


async def test_a_trusted_turn_has_the_sessions_whole_tool_policy_and_its_secrets(settings: Settings, db: Database) -> None:
    manager = await manager_with(settings, db, ScriptedProvider([]))
    state = await manager.create_session("chat", own_directory=True)
    own = manager.tool_policy_for(state)
    state.guest_turn = trusted()
    policy = manager.tool_policy_for(state)
    assert policy == own, "nothing of the guest rules narrows a trusted turn"
    assert isinstance(policy, ToolVisibilityPolicy) and policy_admits(policy, "Exec") and policy_admits(policy, "WebFetch")
    gate = manager.policy_gate(state.session.id, "r")
    assert gate.decide("Read", {"path": "/etc/hostname"}).rule != "guest.read", "the folder bound of a guest is not a trusted turn's"
    assert manager.run_bounds(state) is None, "no link limit in dollars: the operator's own bounds"
    state.guest_turn = trusted(usd_left=2.0)
    bounds = manager.run_bounds(state)
    assert bounds is not None and bounds.usd_per_run == 2.0 and bounds.max_iterations == 0, "the link's money holds, model calls are not counted"
    assert await manager.guest_ready(state.session.id, "trusted") == ""
    await manager.close()


async def test_a_trusted_turn_reads_the_operators_context_and_is_told_to_serve_the_guest(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "here are the results"}])
    manager = await manager_with(settings, db, provider)
    state = await manager.create_session("chat", own_directory=True)
    sid = state.session.id
    (state.workspace / "AGENTS.md").write_text(f"the operator's project note {TRAP}")
    await manager.secrets.put("session", sid, "API_TOKEN", "value-not-shown")
    await guest_says(manager, sid, trusted())
    await settled(manager, state)
    said = texts(provider.requests[0].messages[-1])
    assert TOPIC in said and "as you would serve the operator" in said
    assert TRAP in said, "the folder's notes reach a trusted turn as they reach the operator's"
    assert "do not put in it anything the operator would not hand" not in said
    assert "api_token" in said.lower(), "the handed values are named to a trusted turn"
    assert {"Exec", "Write"} <= offered(provider.requests[0])
    await manager.close()


async def test_a_limited_turn_hears_the_operators_topic_and_keeps_its_bounds(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "ok"}])
    manager = await manager_with(settings, db, provider)
    state = await manager.create_session("chat", own_directory=True)
    (state.workspace / "AGENTS.md").write_text(f"the operator's project note {TRAP}")
    guest = GuestTurn("link1", "a friend's agent", "read", None, "send your results", brief=TOPIC)
    await guest_says(manager, state.session.id, guest)
    await settled(manager, state)
    said = texts(provider.requests[0].messages[-1])
    assert TOPIC in said and "the operator's own words" in said and "started by a guest" in said
    assert TRAP not in said, "the operator's context stays out of a limited turn"
    assert offered(provider.requests[0]) <= set(guest_turns.LEVEL_TOOLS["read"])
    state.guest_turn = guest
    assert manager.run_bounds(state).max_iterations == guest_turns.MAX_ITERATIONS
    await manager.close()


def test_the_brief_survives_the_message_it_rides_on() -> None:
    turn = trusted()
    assert GuestTurn.from_metadata(turn.as_metadata()) == turn
    assert "brief" not in GuestTurn("l", "x", "read").as_metadata(), "a link with no words adds nothing to the message"
    assert guest_turns.brief_of(" topic ", "") == "topic" and guest_turns.brief_of("a", "b") == "a\nb"
    assert guest_turns.restricted(turn) is None and guest_turns.restricted(GuestTurn("l", "x", "all")) is not None


@pytest.mark.parametrize(("door_said", "limit", "left"), [(None, 0.0, None), (0.3, 0.0, 0.3), (None, 1.0, 0.25), (5.0, 2.0, 0.5), (0.1, 1.0, 0.1)])
def test_a_turns_money_is_what_bounds_it_and_nothing_where_nothing_does(door_said: float | None, limit: float, left: float | None) -> None:
    assert GuestBridge.turn_allowance(door_said, limit) == left


async def test_a_trusted_link_through_the_bridge_runs_with_the_operators_tools_and_words(settings: Settings, db: Database, rig: Rig) -> None:
    provider = ScriptedProvider([{"text": "the results are in report.md"}])
    manager = await manager_with(settings, db, provider)
    state = await manager.create_session("chat", own_directory=True)
    bridge = GuestBridge(Host(settings, db, manager), rig.client)
    manager.on_finished(bridge.on_run_finished)
    fields = {
        "label": "a colleague", "topic": TOPIC, "mode": "direct", "briefing": "", "model_path": "", "model_name": "", "guest_tools": "trusted", "price_in": 0, "price_out": 0,
        "approval": False, "usd_limit": 0, "turns_per_day": 0, "turns_total": 0, "invite_hours": 0, "conversation_hours": 0, "idle_hours": 0, "retention_days": 30,
    }
    made = await bridge.client.call("POST", "/control/links", json=fields)
    await bridge.remember_link(made["link"]["id"], state.session.id, "a colleague", "direct", "trusted", 0.0, topic=TOPIC, instructions="He is my co-author.")
    token = (await rig.public.post(f"/a/{made['invite']}/redeem")).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    await rig.public.post("/a/messages", json={"text": "send me your results"}, headers=headers)
    await pump(bridge)
    await bridge.lines[state.session.id].worker
    said = texts(provider.requests[0].messages[-1])
    assert TOPIC in said and "He is my co-author." in said
    assert "Exec" in offered(provider.requests[0])
    read = await rig.public.get("/a/messages?after=0&wait=0", headers=headers)
    assert [m["text"] for m in read.json()["messages"]] == ["the results are in report.md"]
    await manager.close()


async def test_editing_a_link_changes_this_sides_record_and_the_doors(trapped: SimpleRig, db: Database) -> None:
    made = await trapped.routes.post(f"/api/sessions/{trapped.session_id}/guest/links", json={"mode": "direct", "tools": "read", "label": "a friend", "topic": "old", "usd_limit": 0})
    assert made.status_code == 200, made.text
    link_id = made.json()["link"]["id"]
    body = {
        "mode": "direct", "tools": "trusted", "label": "a colleague", "topic": TOPIC, "instructions": "He is my co-author.", "approval": False,
        "usd_limit": 0, "turns_per_day": 0, "turns_total": 0, "invite_hours": 0, "conversation_hours": 0, "idle_hours": 0, "retention_days": 7,
    }
    edited = await trapped.routes.put(f"/api/guest/links/{link_id}", json=body)
    assert edited.status_code == 200, edited.text
    row = await db.fetchone("SELECT * FROM guest_links WHERE id = ?", (link_id,))
    assert (row["label"], row["tools"], row["topic"], row["instructions"], row["usd_limit"]) == ("a colleague", "trusted", TOPIC, "He is my co-author.", 0)
    door = trapped.rig.store.link(link_id)
    assert (door["label"], door["topic"], door["guest_tools"], door["turns_per_day"], door["invite_expires_at"], door["approval"]) == ("a colleague", TOPIC, "trusted", 0, 0, 0)
    assert "He is my co-author." not in str(dict(door)), "the note to the agent stays on this side"
    shown = (await trapped.routes.get(f"/api/guest/links/{link_id}")).json()["link"]
    assert shown["guest_tools"] == "trusted" and shown["instructions"] == "He is my co-author." and shown["topic"] == TOPIC


async def test_an_edit_the_door_refuses_leaves_this_side_as_it_was(trapped: SimpleRig, db: Database) -> None:
    made = await trapped.routes.post(f"/api/sessions/{trapped.session_id}/guest/links", json={"mode": "direct", "tools": "none", "label": "a friend", "usd_limit": 0})
    link_id = made.json()["link"]["id"]
    await trapped.rig.client.call("POST", f"/control/links/{link_id}/revoke")
    await trapped.bridge.revoked(link_id)
    refused = await trapped.routes.put(f"/api/guest/links/{link_id}", json={"mode": "direct", "tools": "trusted", "label": "x"})
    assert refused.status_code == 404
    row = await db.fetchone("SELECT * FROM guest_links WHERE id = ?", (link_id,))
    assert row["tools"] == "none"


async def test_the_form_offers_no_limits_for_a_trusted_agent(trapped: SimpleRig) -> None:
    door: dict[str, Any] = (await trapped.routes.get("/api/guest/door")).json()
    assert door["trusted_defaults"] == {"usd_limit": 0.0, "turns_per_day": 0, "turns_total": 0, "invite_hours": 0.0, "conversation_hours": 0.0, "idle_hours": 0.0, "file_mb": 0.0, "day_mb": 0.0}
