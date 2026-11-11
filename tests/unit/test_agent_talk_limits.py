"""Our side of a conversation with another agent, with its limits lifted by the operator.

The cap on messages sent and the turns and dollars a reply may wake are the operator's to set, and "no
limit" is one of the choices: 0 for the sends, -1 for waking. Waking switched off is still 0.
"""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

from typing import Any

import pytest

from daedalus.extensions.agent_talk import MAX_SENDS, NO_LIMIT
from tests.unit.test_agent_talk import (  # noqa: F401 — the fixtures are used by name
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
