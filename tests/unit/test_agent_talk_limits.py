"""Our side of a conversation with another agent, with its limits lifted by the operator.

The cap on messages sent and the turns and dollars a reply may wake are the operator's to set, and "no
limit" is one of the choices: 0 for the sends, -1 for waking. Waking switched off is still 0.
"""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from daedalus.extensions.agent_talk import MAX_SENDS, NO_LIMIT
from tests.unit.test_agent_talk import (  # noqa: F401 — the fixtures are used by name
    api_for,
    opened,
    quick,
    rig_for,
    until,
    waking_chat,
)
from tests.unit.test_guest_turns import settled

pytestmark = pytest.mark.usefixtures("bare_sandbox")


async def test_with_no_cap_a_conversation_sends_past_thirty(rig_for: Any) -> None:
    rig = await rig_for([], "ok")
    state = await rig.chat()
    talk = await opened(rig, state, rig.door.link())
    await rig.talks.configure(talk, sends_limit=0)
    await rig.db.execute("UPDATE agent_talks SET sends = ? WHERE id = ?", (MAX_SENDS + 5, talk))
    outcome = await rig.talks.tool(state.session.id, "send", text="again", wait_seconds=0)
    assert not outcome.error, outcome.text
    described = await rig.talks.describe(await rig.talks.talk(talk))
    assert described["sends_limit"] == 0 and described["sends"] == MAX_SENDS + 6


async def test_a_cap_the_operator_set_holds_at_its_own_number(rig_for: Any) -> None:
    rig = await rig_for([], "ok")
    state = await rig.chat()
    talk = await opened(rig, state, rig.door.link())
    await rig.talks.configure(talk, sends_limit=2)
    await rig.db.execute("UPDATE agent_talks SET sends = 2 WHERE id = ?", (talk,))
    outcome = await rig.talks.tool(state.session.id, "send", text="again", wait_seconds=0)
    assert outcome.error and "2 messages" in outcome.text


async def test_waking_with_no_limit_is_never_counted_down(rig_for: Any) -> None:
    rig = await rig_for([{"text": "noted"}], "here you are", start=False)
    state, talk = await waking_chat(rig, turns=NO_LIMIT, usd=NO_LIMIT)

    async def priced(_session_id: str) -> tuple[float, float]:
        return (1.0, 1.0)

    rig.manager.guest_price = priced
    rig.door.start()
    await until(lambda: rig.provider.requests)
    await settled(rig.manager, state)
    row = await rig.talks.talk(talk)
    assert (row["wake_turns"], row["wake_usd"]) == (NO_LIMIT, NO_LIMIT), "a priced chat with no budget set still wakes, and nothing is spent down"
    assert state.guest_turn is not None and state.guest_turn.usd_left is None


async def held_message(rig: Any) -> tuple[Any, str, Any, Any]:
    state = await rig.chat()
    talk = await opened(rig, state, rig.door.link(), review="all")
    task = asyncio.create_task(rig.talks.tool(state.session.id, "send", text="the agent's words", wait_seconds=30))
    await until(lambda: rig.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE state = 'held'"))
    return state, talk, (await rig.talks.messages(talk))[0], task


def sent_by_us(rig: Any) -> list[str]:
    store = rig.door.store
    return [m["text"] for m in store.transcript(store.conversation_of_link(store.links()[0]["id"])["id"]) if m["role"] == "guest"]


async def test_the_operator_can_send_their_own_words_in_place_of_a_held_message(rig_for: Any) -> None:
    rig = await rig_for([], "ok")
    state, talk, message, task = await held_message(rig)
    client = api_for(rig)
    sent = await client.post(f"/api/agent-talks/{talk}/messages/{message['id']}/send", json={"text": "  the operator's words  "})
    assert sent.status_code == 200, sent.text
    await task
    assert sent_by_us(rig) == ["the operator's words"]
    assert [m["text"] for m in sent.json()["messages"] if m["role"] == "us"] == ["the operator's words"], "the conversation shows what really went"
    again = await client.post(f"/api/agent-talks/{talk}/messages/{message['id']}/send", json={"text": "twice"})
    assert again.status_code == 409, "a held message is claimed once, edited or not"
    assert not state.metadata.get("policy_pending")


async def test_an_edit_that_cannot_go_leaves_the_message_waiting(rig_for: Any) -> None:
    rig = await rig_for([], "ok")
    _, talk, message, task = await held_message(rig)
    client = api_for(rig)
    assert (await client.post(f"/api/agent-talks/{talk}/messages/{message['id']}/send", json={"text": "   "})).status_code == 422
    assert (await client.post(f"/api/agent-talks/{talk}/messages/{message['id']}/send", json={"text": "x" * 9000})).status_code == 422
    assert (await rig.talks._message(message["id"]))["state"] == "held"
    assert (await client.post(f"/api/agent-talks/{talk}/messages/{message['id']}/send")).status_code == 200, "Send with no body sends the row as it is"
    await task
    assert sent_by_us(rig) == ["the agent's words"]
