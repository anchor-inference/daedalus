"""Our side of a conversation when the other side is away: a desktop app that was closed, a server whose agent restarts.

The door is the real one, in process, behind a transport that can play every way a door is found missing: nothing
takes the connection, its front answers 502, a tunnel shows its own HTML page, or the connection drops after the
message went. What these prove is that a message the door never saw waits and goes by itself, in order, counted
once, and that one the door may have seen is still never sent twice.
"""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from daedalus.extensions import agent_talk as extension
from daedalus.extensions.agent_talk import AgentTalks, Dialer, DoorAnswer, door_absent
from daedalus.host.guest_turns import GuestTurn
from tests.unit.test_agent_talk import opened, quick, rig_for, until  # noqa: F401 — the fixtures are used by name
from tests.unit.test_guest_turns import settled

pytestmark = pytest.mark.usefixtures("bare_sandbox")

NGROK_PAGE = "<!DOCTYPE html><html><body>The endpoint is offline. ERR_NGROK_3200</body></html>"


class Away(httpx.AsyncBaseTransport):
    """The real door, or one of the ways of its not being there."""

    def __init__(self, door: Any) -> None:
        self.inner = httpx.ASGITransport(door)
        self.mode = ""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "refused":
            raise httpx.ConnectError("connection refused", request=request)
        if self.mode == "front":
            return httpx.Response(502, json={"status": "refused", "detail": "This door is closed for the moment. Try again in a minute."}, headers={"retry-after": "60"})
        if self.mode == "html":
            return httpx.Response(404, headers={"content-type": "text/html"}, text=NGROK_PAGE)
        if self.mode == "cut":
            await self.inner.handle_async_request(request)  # the door took it; the answer is what is lost
            raise httpx.ReadTimeout("the answer never came", request=request)
        return await self.inner.handle_async_request(request)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extension, "RETRY_SECONDS", (0.05,))


async def away_rig(rig_for: Any, script: list[dict[str, Any]] | None = None, *lines: str) -> tuple[Any, Any, Away, str]:
    rig = await rig_for(script or [], *lines, start=False)
    away = Away(rig.door)
    rig.talks.dialer = Dialer(private=True, transport=away)
    state = await rig.chat()
    talk = await opened(rig, state, rig.door.link())
    return rig, state, away, talk


def sent_by_us(rig: Any) -> list[str]:
    store = rig.door.store
    return [m["text"] for m in store.transcript(store.conversation_of_link(store.links()[0]["id"])["id"]) if m["role"] == "guest"]


async def states(rig: Any, talk: str) -> list[str]:
    return [row["state"] for row in await rig.talks.messages(talk) if row["role"] == "us"]


@pytest.mark.parametrize("mode", ["refused", "front", "html"])
async def test_a_door_that_is_not_there_keeps_the_message_and_says_so(rig_for: Any, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(extension, "RETRY_SECONDS", (30.0,))
    rig, state, away, talk = await away_rig(rig_for)
    away.mode = mode
    outcome = await rig.talks.tool(state.session.id, "send", text="hello there", wait_seconds=0)
    assert not outcome.error, outcome.text
    assert "The other side is offline; the message is kept and will be sent when it is back" in outcome.text
    assert "woken when it is delivered or given up" in outcome.text
    row = (await rig.talks.messages(talk))[0]
    assert (row["state"], row["attempts"]) == ("waiting_door", 1)
    due = datetime.fromisoformat(row["next_at"]) - datetime.now(UTC)
    assert timedelta(seconds=25) < due <= timedelta(seconds=30)
    described = await rig.talks.describe(await rig.talks.talk(talk), messages=True)
    assert described["waiting_for"] == "offline" and described["messages"][0]["next_at"] == row["next_at"]
    assert sent_by_us(rig) == [], "the door never saw it"


async def test_what_waits_goes_in_order_when_the_door_is_back_and_is_counted_once(rig_for: Any) -> None:
    rig, state, away, talk = await away_rig(rig_for)
    await rig.talks.configure(talk, sends_limit=3)
    away.mode = "refused"
    first = await rig.talks.tool(state.session.id, "send", text="first", wait_seconds=0)
    second = await rig.talks.tool(state.session.id, "send", text="second", wait_seconds=0)
    assert "kept" in first.text and "kept" in second.text
    assert await states(rig, talk) == ["waiting_door", "waiting_door"]
    rows = await rig.talks.messages(talk)
    assert rows[1]["attempts"] == 0, "the second never went to the door: it waits behind the first"
    away.mode = ""
    await until(lambda: rig.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE talk_id = ? AND state = 'sent' AND text = 'second'", (talk,)))
    assert sent_by_us(rig) == ["first", "second"]
    assert (await rig.talks.talk(talk))["sends"] == 2


async def test_the_sends_limit_counts_what_waits_and_never_twice(rig_for: Any) -> None:
    rig, state, away, talk = await away_rig(rig_for)
    await rig.talks.configure(talk, sends_limit=2)
    await rig.db.execute("UPDATE agent_talks SET sends = 1 WHERE id = ?", (talk,))
    away.mode = "front"
    assert not (await rig.talks.tool(state.session.id, "send", text="last one", wait_seconds=0)).error
    over = await rig.talks.tool(state.session.id, "send", text="one too many", wait_seconds=0)
    assert over.error and "2 messages" in over.text, "the one waiting is already one of the two"
    await until(lambda: rig.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE talk_id = ? AND attempts >= 3", (talk,)))
    away.mode = ""
    await until(lambda: rig.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE talk_id = ? AND state = 'sent'", (talk,)))
    assert (await rig.talks.talk(talk))["sends"] == 2, "several attempts and one message sent"


async def test_the_wait_between_attempts_grows_to_an_hour(rig_for: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extension, "RETRY_SECONDS", (30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0))
    rig, state, away, talk = await away_rig(rig_for)
    message = await rig.talks._insert_message(talk, "us", "hello", "sending", "")
    waits: list[float] = []
    for _ in range(9):
        before = datetime.now(UTC)
        await rig.talks._keep_for_door(talk, message)
        waits.append(round((datetime.fromisoformat((await rig.talks._message(message))["next_at"]) - before).total_seconds() / 30) * 30)
    assert waits == [30, 60, 120, 300, 600, 1800, 3600, 3600, 3600]
    assert (await rig.talks._message(message))["attempts"] == 9


async def test_a_message_is_given_up_after_three_days_and_the_rest_behind_it_too(rig_for: Any) -> None:
    rig, state, away, talk = await away_rig(rig_for, [{"text": "It did not go; I will tell the operator."}])
    away.mode = "refused"
    await rig.talks.tool(state.session.id, "send", text="first", wait_seconds=0)
    await rig.talks.tool(state.session.id, "send", text="second", wait_seconds=0)
    long_ago = (datetime.now(UTC) - timedelta(hours=73)).isoformat()
    await rig.db.execute("UPDATE agent_talk_messages SET waiting_since = ? WHERE talk_id = ? AND text = 'first'", (long_ago, talk))
    await until(lambda: rig.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE talk_id = ? AND state = 'failed' AND text = 'second'", (talk,)))
    rows = [row for row in await rig.talks.messages(talk) if row["role"] == "us"]
    assert [(row["state"], row["note"]) for row in rows] == [("failed", "gave_up"), ("failed", "gave_up")]
    assert "stayed offline for 72 hours" in rig.talks._send_failure(await rig.talks.talk(talk), rows[0])
    await until(lambda: any(notice.kind == "agent_talk_gave_up" for notice in rig.notices))
    await settled(rig.manager, state)
    assert [row["wake_state"] for row in await rig.talks.messages(talk) if row["role"] == "us"] == ["done", "done"]
    assert any(notice.kind == "agent_talk_gave_up" for notice in rig.notices), "the operator hears of a message that did not go"


async def test_a_message_the_door_may_have_taken_is_never_sent_again(rig_for: Any) -> None:
    rig, state, away, talk = await away_rig(rig_for)
    away.mode = "cut"
    outcome = await rig.talks.tool(state.session.id, "send", text="said once", wait_seconds=0)
    assert outcome.error and "may or may not have arrived" in outcome.text
    away.mode = ""
    await asyncio.sleep(0.3)
    assert await states(rig, talk) == ["unsure"]
    assert sent_by_us(rig) == ["said once"], "the door has it once, and nothing sends it again"


async def test_a_front_that_gave_up_waiting_on_the_door_is_not_a_door_that_is_away() -> None:
    assert door_absent(DoorAnswer(502, {"status": "refused"}))
    assert door_absent(DoorAnswer(530, {}, parsed=False))
    assert door_absent(DoorAnswer(404, {}, parsed=False)), "a tunnel's page in place of the door"
    assert door_absent(DoorAnswer(200, {}, parsed=False))
    assert not door_absent(DoorAnswer(504, {}, parsed=False)), "a gateway timeout came after the door had the request"
    assert not door_absent(DoorAnswer(524, {}, parsed=False))
    assert not door_absent(DoorAnswer(202, {"status": "open", "id": 3}))
    assert not door_absent(DoorAnswer(403, {"status": "limit"}))


async def test_a_page_of_html_is_read_as_no_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(530, headers={"content-type": "text/html"}, text="<html>Cloudflare Tunnel error</html>")

    answer = await Dialer(private=True, transport=httpx.MockTransport(handler)).call("GET", "http://x/")
    assert answer.status == 530 and not answer.parsed and answer.body == {}


async def test_what_waits_survives_a_restart_of_this_side(rig_for: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extension, "RETRY_SECONDS", (30.0,))
    rig, state, away, talk = await away_rig(rig_for)
    away.mode = "refused"
    await rig.talks.tool(state.session.id, "send", text="across a restart", wait_seconds=0)
    for task in list(rig.talks._background):
        task.cancel()
    await asyncio.gather(*rig.talks._background, return_exceptions=True)
    assert await states(rig, talk) == ["waiting_door"]
    await rig.db.execute("UPDATE agent_talk_messages SET next_at = ? WHERE talk_id = ?", (datetime.now(UTC).isoformat(), talk))
    away.mode = ""

    again = AgentTalks(rig.app, rig.talks.dialer)
    rig.app.extensions[extension.EXTENSION_KEY] = again
    serving = asyncio.create_task(again.serve())
    try:
        await until(lambda: again.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE talk_id = ? AND state = 'sent'", (talk,)))
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
    assert sent_by_us(rig) == ["across a restart"]


async def test_a_delivery_after_waiting_wakes_the_chat_once(rig_for: Any) -> None:
    rig, state, away, talk = await away_rig(rig_for, [{"text": "Noted, it went."}])
    away.mode = "refused"
    await rig.talks.tool(state.session.id, "send", text="the waited words", wait_seconds=0)
    await settled(rig.manager, state)
    away.mode = ""
    await until(lambda: rig.provider.requests)
    await settled(rig.manager, state)
    request = rig.provider.requests[0]
    said = "\n".join(block.text for message in request.messages for block in message.content_blocks if hasattr(block, "text"))
    assert "delivered: 'the waited words'" in said and "started by this installation" in said
    assert state.guest_turn is not None and state.guest_turn.kind == "reply" and state.guest_turn.notice
    rig.talks.announce(talk)
    await asyncio.sleep(0.2)
    assert len(rig.provider.requests) == 1, "one word of each message, however often it is asked for"
    assert [row["wake_state"] for row in await rig.talks.messages(talk) if row["role"] == "us"] == ["done"]


async def test_an_owner_who_is_away_is_said_plainly_on_a_send(rig_for: Any) -> None:
    rig = await rig_for([], start=False)
    state = await rig.chat()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.startswith("/a/") and path != "/a/messages":
            return httpx.Response(200, json={"door": "agent-link", "status": "waiting", "mode": "direct", "approval": False, "max_text": 8000})
        if path.endswith("/redeem"):
            return httpx.Response(201, json={"token": "gt_x", "status": "open"})
        if request.method == "POST" and path == "/a/messages":
            return httpx.Response(202, json={"status": "open", "id": 5, "owner_offline": True, "detail": "Received and kept.", "retry_after": 300})
        return httpx.Response(200, json={"status": "open", "messages": [], "waiting_for": "owner_online", "stop_polling": True, "retry_after": 300})

    rig.talks.dialer = Dialer(private=True, transport=httpx.MockTransport(handler))
    talk = await opened(rig, state, f"https://agents.example.test/a/{'q' * 40}")
    outcome = await rig.talks.tool(state.session.id, "send", text="hi", wait_seconds=30)
    assert not outcome.error
    assert outcome.text.startswith("Received and kept; the other side's owner is offline, the reply comes when they are back.")
    assert "The other side is offline" in outcome.text and "A reply is owed" not in outcome.text
    row = await rig.talks.talk(talk)
    assert (row["waiting_for"], row["quiet"], row["sends"]) == ("offline", 1, 1)
    assert (await rig.talks.messages(talk))[0]["state"] == "sent"


async def test_a_read_that_finds_the_owner_away_shows_offline_and_slows_down(rig_for: Any) -> None:
    rig, state, away, talk = await away_rig(rig_for)
    row = await rig.talks.talk(talk)
    pause = await rig.talks._took(row, DoorAnswer(200, {"status": "open", "messages": [], "waiting_for": "owner_online", "stop_polling": True}, 300.0))
    assert pause is not None and pause >= extension.QUIET_POLL_SECONDS
    row = await rig.talks.talk(talk)
    assert (row["waiting_for"], row["quiet"]) == ("offline", 1)
    assert "The other side is offline" in rig.talks._state_line(row)


async def test_reads_that_find_nobody_show_the_other_side_offline(rig_for: Any) -> None:
    rig, state, away, talk = await away_rig(rig_for)
    await rig.db.execute("UPDATE agent_talks SET waiting_for = 'reply' WHERE id = ?", (talk,))
    away.mode = "html"
    rig.talks.follow(talk)
    await until(lambda: rig.db.fetchone("SELECT 1 FROM agent_talks WHERE id = ? AND waiting_for = 'offline'", (talk,)))
    outcome = await rig.talks.tool(state.session.id, "read", wait_seconds=30)
    assert "The other side is offline" in outcome.text, "and a read in a turn does not sit out its wait for it"


def test_a_turn_opened_to_tell_what_became_of_a_message_stays_one_across_a_restart() -> None:
    turn = GuestTurn("t1", "agents.example.test", "none", None, "", "reply", ("r1",), notice=True)
    assert GuestTurn.from_metadata(turn.as_metadata()) == turn
    assert not GuestTurn.from_metadata({**GuestTurn("t1", "a", "none").as_metadata(), "notice": True}).notice, "a guest's own turn is never a notice"


async def test_a_retry_a_restart_cut_short_stops_the_line_behind_it(rig_for: Any) -> None:
    rig, state, away, talk = await away_rig(rig_for, [{"text": "I will tell the operator."}])
    first = await rig.talks._insert_message(talk, "us", "cut mid-way", "sending", "")
    second = await rig.talks._insert_message(talk, "us", "behind it", "waiting_door", "")
    await rig.db.execute("UPDATE agent_talk_messages SET waiting_since = ?, next_at = ? WHERE talk_id = ?", (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(), talk))
    serving = asyncio.create_task(rig.talks.serve())
    try:
        await until(lambda: rig.provider.requests)
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
    await settled(rig.manager, state)
    assert [(row["state"], row["note"]) for row in [await rig.talks._message(first), await rig.talks._message(second)]] == [("unsure", ""), ("failed", "behind")]
    assert sent_by_us(rig) == [], "neither goes again: the first may be there, and the second would follow a message the other side may not have"
    said = "\n".join(block.text for message in rig.provider.requests[0].messages for block in message.content_blocks if hasattr(block, "text"))
    assert "may or may not have arrived" in said and "waited behind a message that did not go" in said
