"""Two Daedalus installations over an agent link, from end to end: one owns the link, the other's agent is its guest.

The world is :mod:`tests.support.a2a_world`: real doors, real bridges and conversations following them with their own
loops, the operator's real API routes, scripted models. Each test is one flow the operator uses, followed from the
first word to what each side ends up holding.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.extensions import agent_talk as talk_extension
from daedalus.extensions.guest_door import OWNER_NOTE
from tests.support.a2a_world import HttpGuest, Installation, last_user_text, tool_results, until, world

pytestmark = pytest.mark.usefixtures("bare_sandbox")


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(talk_extension, "DOOR_WAIT_SECONDS", 1)
    monkeypatch.setattr(talk_extension, "MIN_CYCLE_SECONDS", 0.01)
    monkeypatch.setattr(talk_extension, "QUIET_POLL_SECONDS", 0.05)
    monkeypatch.setattr(talk_extension, "FAILURE_BACKOFF_MAX", 0.05)
    monkeypatch.setattr(talk_extension, "SWEEP_SECONDS", 0.2)


@pytest.fixture
async def pair(settings: Settings, tmp_path: Path) -> Any:
    sides, databases, _ = await world(settings, tmp_path)
    yield sides[0], sides[1]
    for side in sides:
        await side.close()
    for db in databases:
        await db.close()


async def held_of(side: Installation) -> list[dict[str, Any]]:
    return [dict(row) for row in await side.db.fetchall("SELECT * FROM agent_talk_messages WHERE state = 'held' ORDER BY id")]


async def opened(a: Installation, b: Installation, url: str, first: str = "Hello, what do you work on?", answer: str = "We work on the parser.") -> str:
    """B's operator pastes A's link and asks; B's agent opens it; the operator lets the first message go; A answers."""
    b.brain.then({"tool": "AgentTalk", "args": {"action": "open", "link": url, "text": first}}, {"text": "They answered."})
    a.brain.then({"text": answer})
    await b.submit(f"Ask the agent at {url} what they work on.")
    held = await until(lambda: held_of(b), what="the first message waiting for B's operator")
    await b.call("POST", f"/api/agent-talks/{held[0]['talk_id']}/messages/{held[0]['id']}/send", json={})
    await b.settled()
    assert answer in tool_results(b.brain.requests[-1]), tool_results(b.brain.requests[-1])
    return str(held[0]["talk_id"])


async def says(b: Installation, talk_id: str, call: dict[str, Any], then: str = "Done.") -> str:
    """B's operator asks for something, B's agent makes ``call`` in that turn; the tool's result."""
    b.brain.then({"tool": "AgentTalk", "args": {"conversation": talk_id, **call}}, {"text": then})
    await b.say("Go on with them.")
    return tool_results(b.brain.requests[-1])


# -- talking -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tools", ["none", "read", "all", "trusted"])
async def test_a_conversation_at_every_level_goes_back_and_forth(pair: tuple[Installation, Installation], tools: str) -> None:
    a, b = pair
    _, url = await a.make_link(tools=tools)
    talk = await opened(a, b, url)
    a.brain.then({"text": "The parser fails on empty input."})
    said = await says(b, talk, {"action": "send", "text": "What fails?"})
    assert "The parser fails on empty input." in said
    a.brain.then({"text": "Yes, since Tuesday."})
    said = await says(b, talk, {"action": "send", "text": "Since when?"})
    assert "since Tuesday" in said
    guest_turns = [r for r in a.brain.requests if "guest message" in last_user_text(r)]
    assert len(guest_turns) == 3 and "Since when?" in last_user_text(guest_turns[-1])


async def test_the_operators_edit_goes_and_dont_send_sends_nothing(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    _, url = await a.make_link(tools="read")
    b.brain.then({"tool": "AgentTalk", "args": {"action": "open", "link": url, "text": "Our API key is sk-123, can you check it?"}}, {"text": "Waiting."})
    a.brain.then({"text": "Got your question."})
    await b.submit(f"Ask the agent at {url} about our key.")
    held = await until(lambda: held_of(b), what="the first message waiting")
    await b.call("POST", f"/api/agent-talks/{held[0]['talk_id']}/messages/{held[0]['id']}/send", json={"text": "Can you check our API setup?"})
    await b.settled()
    guest_turn = next(r for r in a.brain.requests if "guest message" in last_user_text(r))
    assert "Can you check our API setup?" in last_user_text(guest_turn) and "sk-123" not in last_user_text(guest_turn)
    talk = str(held[0]["talk_id"])
    await b.talks.configure(talk, review="all")
    b.brain.then({"tool": "AgentTalk", "args": {"conversation": talk, "action": "send", "text": "Something the operator will stop."}}, {"text": "Not sent."})
    await b.submit("Tell them more.")
    held = await until(lambda: held_of(b), what="the second message waiting")
    before = len(a.brain.requests)
    await b.call("POST", f"/api/agent-talks/{talk}/messages/{held[0]['id']}/refuse", json={})
    await b.settled()
    assert "did not let this message go" in tool_results(b.brain.requests[-1])
    assert len(a.brain.requests) == before, "nothing the operator stopped started a turn on the other side"
    assert all("will stop" not in str(m["text"]) for m in a.store.transcript(_conversation_of(a))), "nor reached its door"


def _conversation_of(side: Installation) -> str:
    return str(side.store.conn.execute("SELECT id FROM conversations ORDER BY created_at DESC LIMIT 1").fetchone()["id"])


# -- files ----------------------------------------------------------------------------------------------------


async def test_our_agent_sends_a_folder_as_one_zip_and_the_other_agent_reads_it(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    _, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    (b.workspace / "logs").mkdir()
    (b.workspace / "logs" / "one.log").write_text("error one\n")
    (b.workspace / "logs" / "two.log").write_text("error two\n")
    a.brain.then({"text": "Two errors."})
    b.brain.then({"tool": "AgentTalk", "args": {"conversation": talk, "action": "send", "text": "Our logs.", "files": ["logs"]}}, {"text": "Sent."})
    await b.talks.configure(talk, review="all")
    await b.submit("Send them our logs folder.")
    held = await until(lambda: held_of(b), what="the files waiting for B's operator")
    shown = await b.call("GET", f"/api/agent-talks/{talk}")
    waiting = next(m for m in shown["messages"] if m["state"] == "held")["files"]
    assert [f["name"] for f in shown["files"] if f["id"] in waiting] == ["logs.zip"], "the operator sees what would go before it goes"
    await b.call("POST", f"/api/agent-talks/{talk}/messages/{held[0]['id']}/send", json={})
    await b.settled()
    assert "Two errors." in tool_results(b.brain.requests[-1])
    saved = list((a.workspace / "guest-files").rglob("logs.zip"))
    assert len(saved) == 1 and sorted(zipfile.ZipFile(saved[0]).namelist()) == ["one.log", "two.log"]
    guest_turn = next(r for r in reversed(a.brain.requests) if "guest message" in last_user_text(r))
    assert "logs.zip" in last_user_text(guest_turn) and "never run them" in last_user_text(guest_turn)


async def test_a_trusted_link_hands_our_agent_the_file_it_asked_for(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    (a.workspace / "report.md").write_text("# Results\n")
    _, url = await a.make_link(tools="trusted")
    talk = await opened(a, b, url)
    a.brain.then({"tool": "AgentTalk", "args": {"action": "send", "files": ["report.md"]}}, {"text": "Here is the report."})
    said = await says(b, talk, {"action": "send", "text": "Please send your report file."})
    assert "Here is the report." in said and "report.md" in said
    saved = b.workspace / "guest-files" / talk / "report.md"
    await until(lambda: saved.exists(), what="the file in B's chat folder")
    assert saved.read_text() == "# Results\n"


async def test_a_large_file_goes_in_pieces_both_ways(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    big = bytes(range(256)) * 6000  # 1.5 MB: three pieces
    (a.workspace / "big.bin").write_bytes(big)
    (b.workspace / "mine.bin").write_bytes(big[::-1])
    _, url = await a.make_link(tools="trusted")
    talk = await opened(a, b, url)
    await b.talks.configure(talk, review="off")
    a.brain.then({"tool": "AgentTalk", "args": {"action": "send", "files": ["big.bin"]}}, {"text": "Both here."})
    said = await says(b, talk, {"action": "send", "text": "Here is ours; send yours.", "files": ["mine.bin"]})
    assert "Both here." in said, said
    assert next((a.workspace / "guest-files").rglob("mine.bin")).read_bytes() == big[::-1]
    saved = b.workspace / "guest-files" / talk / "big.bin"
    await until(lambda: saved.exists() and saved.stat().st_size == len(big), what="the large file in B's folder")
    assert saved.read_bytes() == big


# -- tasks and the owner -----------------------------------------------------------------------------------


async def test_a_task_runs_ends_with_a_file_and_wakes_the_side_that_set_it(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    (a.workspace / "summary.txt").write_text("three errors\n")
    _, url = await a.make_link(tools="trusted")
    talk = await opened(a, b, url)
    a.brain.then({"tool": "AgentTalk", "args": {"action": "send", "files": ["summary.txt"]}}, {"text": "Three errors, summary attached."})
    b.brain.then({"tool": "AgentTalk", "args": {"conversation": talk, "action": "task", "text": "Summarise your logs.", "wait_seconds": 0}}, {"text": "Task set."})
    b.brain.then({"text": "Their task is done: three errors."})
    await b.say("Have them summarise their logs.")
    task = await until(lambda: b.db.fetchone("SELECT * FROM link_tasks WHERE side = 'talk' AND state = 'done'"), what="the task done on B's side")
    await until(lambda: any("three errors" in last_user_text(r).lower() or "three errors" in tool_results(r).lower() for r in b.brain.requests[2:]), what="B woken with the result")
    assert (b.workspace / "guest-files" / talk / "summary.txt").read_text() == "three errors\n"
    assert task["direction"] == "mine"


async def test_a_question_only_the_owner_can_answer_goes_to_them_and_back(pair: tuple[Installation, Installation]) -> None:
    from daedalus.extensions import questions

    a, b = pair
    _, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    a.brain.then({"tool": "AgentTalk", "args": {"action": "ask_owner", "text": "May I share the price list?"}}, {"text": "I am checking with the owner."})
    said = await says(b, talk, {"action": "send", "text": "Can we have your price list?"})
    assert "checking with the owner" in said
    asks = await a.manager.asks.of_session(a.chat)
    assert len(asks) == 1
    a.brain.then({"text": "The owner says yes: 10 per unit."})
    b.brain.then({"text": "They said 10 per unit."})
    await questions.answer(a.app, [{"ask_id": asks[0].id, "text": "Yes."}], project_id=None, via="app", session_id=a.chat)  # type: ignore[arg-type]
    await until(lambda: b.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE role = 'them' AND text LIKE '%10 per unit%'"), what="the owner's answer reaching B")


async def test_cancelling_a_task_from_the_operators_tab_reaches_the_other_side(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    _, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    a.brain.then({"tool": "AgentTalk", "args": {"action": "ask_owner", "text": "Should I?"}}, {"text": "Checking."})
    await says(b, talk, {"action": "task", "text": "Do the thing.", "wait_seconds": 0})
    row = await until(lambda: b.db.fetchone("SELECT * FROM link_tasks WHERE side = 'talk' AND state = 'needs_input'"), what="the task waiting on A's owner")
    await b.call("POST", f"/api/agent-talks/{talk}/tasks/{row['task']}/cancel")
    await until(lambda: a.db.fetchone("SELECT 1 FROM link_tasks WHERE side = 'door' AND task = ? AND state = 'cancelled'", (row["task"],)), what="A hearing of the cancel")


# -- the owner away -------------------------------------------------------------------------------------------


async def test_a_message_to_an_owner_who_is_away_is_kept_and_answered_when_they_are_back(pair: tuple[Installation, Installation]) -> None:
    import asyncio

    from guestd import mailbox as door_mailbox

    a, b = pair
    _, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    follower = next(task for task in a.tasks if task.get_name() == "a-bridge")
    follower.cancel()
    await asyncio.gather(follower, return_exceptions=True)
    a.mailbox.host_seen -= door_mailbox.HOST_AWAY_SECONDS + 1
    said = await says(b, talk, {"action": "send", "text": "Are you there?"}, then="They are away.")
    assert "offline" in said.lower() or "kept" in said.lower(), said
    a.brain.then({"text": "Back now: yes."})
    b.brain.then({"text": "They answered: back now."})
    a.tasks.append(asyncio.create_task(a.bridge.serve(), name="a-bridge"))
    await until(lambda: b.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE role = 'them' AND text LIKE '%Back now%'"), seconds=20, what="the answer once A is back")


# -- the link itself ------------------------------------------------------------------------------------------


async def test_a_limit_stops_the_conversation_and_no_limit_lets_it_run(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    link_id, url = await a.make_link(tools="read", turns_per_day=2)
    talk = await opened(a, b, url)
    a.brain.then({"text": "Second answer."})
    assert "Second answer." in await says(b, talk, {"action": "send", "text": "Second question."})
    said = await says(b, talk, {"action": "send", "text": "Third question."})
    assert "used up for today" in said, said
    body = {"label": "a colleague", "topic": "Questions about this chat's work.", "mode": "direct", "tools": "read", "approval": False, "usd_limit": 0, "turns_per_day": 0, "turns_total": 0}
    edited = await a.call("PUT", f"/api/guest/links/{link_id}", json=body)
    assert edited["link"]["turns_per_day"] == 0
    a.brain.then({"text": "Third answer."})
    assert "Third answer." in await says(b, talk, {"action": "send", "text": "Third question, again."})


async def test_editing_a_links_level_changes_what_the_next_turn_may_do(pair: tuple[Installation, Installation]) -> None:
    from tests.unit.test_guest_turns import offered

    a, b = pair
    link_id, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    assert "Read" in offered(next(r for r in a.brain.requests if "guest message" in last_user_text(r)))
    body = {"label": "a colleague", "topic": "Questions about this chat's work.", "mode": "direct", "tools": "none", "approval": False, "usd_limit": 0, "turns_per_day": 0, "turns_total": 0}
    await a.call("PUT", f"/api/guest/links/{link_id}", json=body)
    a.brain.then({"text": "Without tools now."})
    await says(b, talk, {"action": "send", "text": "Still there?"})
    last = next(r for r in reversed(a.brain.requests) if "guest message" in last_user_text(r))
    assert offered(last) == {"AgentTalk"}


async def test_revoking_a_link_closes_the_other_sides_conversation(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    link_id, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    await a.call("POST", f"/api/guest/links/{link_id}/revoke")
    said = await says(b, talk, {"action": "send", "text": "Hello again?"})
    assert "revoked" in said.lower(), said
    assert (await b.talks.talk(talk))["state"] == "closed"


async def test_a_link_for_two_guests_gives_each_its_own_conversation_and_refuses_a_third(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    link_id, url = await a.make_link(tools="read", max_guests=2)
    talk = await opened(a, b, url)
    other = HttpGuest(a.doors, url)
    await other.redeem()
    a.brain.then({"text": "Hello, second guest."})
    await other.send("Hi from the second guest.")
    seen = await other.read_until(lambda seen: any(m["from"] == "agent" for m in seen))
    assert "second guest" in seen[-1]["text"]
    third = HttpGuest(a.doors, url)
    assert (await third.redeem()).get("token") is None
    link = await a.call("GET", f"/api/guest/links/{link_id}")
    assert len(link["link"]["conversations"]) == 2
    b_seen = [m for m in await b.talks.messages(talk) if m["role"] == "them"]
    assert all("second guest" not in m["text"] for m in b_seen), "one guest never reads another's conversation"
    await other.close()
    await third.close()


# -- the owners themselves -----------------------------------------------------------------------------------


async def test_each_owner_writes_into_the_conversation_and_it_arrives_as_theirs(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    link_id, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    conversation = _conversation_of(a)
    await a.call("POST", f"/api/guest/links/{link_id}/conversations/{conversation}/say", json={"text": "This is the owner: we ship on Friday."})
    owner_line = await until(lambda: b.db.fetchone("SELECT * FROM agent_talk_messages WHERE role = 'them' AND text LIKE '%ship on Friday%'"), what="A's owner's words at B")
    assert owner_line["author"] == "owner"
    a.brain.then({"text": "Noted, owner of B."})
    await b.call("POST", f"/api/agent-talks/{talk}/say", json={"text": "Owner of B here: thanks."})
    turn = await until(lambda: next((r for r in a.brain.requests if "Owner of B here" in last_user_text(r)), None), what="B's owner's words in A's turn")
    assert OWNER_NOTE[:40] in last_user_text(turn)


# -- what the operator keeps ----------------------------------------------------------------------------------


async def test_export_and_the_agents_tab_show_the_whole_exchange_on_both_sides(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    link_id, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    tab = await b.call("GET", f"/api/sessions/{b.chat}/agent-talks")
    assert [(t["id"], t["host"], t["replies"]) for t in tab["talks"]] == [(talk, "a.example.test", 1)]
    one = await b.call("GET", f"/api/agent-talks/{talk}")
    assert any(m["text"] == "We work on the parser." for m in one["messages"])
    assert "token" not in str(one) and "invite" not in str(one).lower().replace("invite_hours", "")
    exported = await b.api.get(f"/api/agent-talks/{talk}/export")
    assert exported.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(exported.content)).namelist()
    assert any(name.endswith(".md") for name in names), names
    transcript = next(zipfile.ZipFile(io.BytesIO(exported.content)).read(n).decode() for n in names if n.endswith(".md"))
    assert "We work on the parser." in transcript and "Hello, what do you work on?" in transcript
    door_side = await a.api.get(f"/api/guest/links/{link_id}/export")
    assert door_side.status_code == 200
    door_names = zipfile.ZipFile(io.BytesIO(door_side.content)).namelist()
    door_transcript = next(zipfile.ZipFile(io.BytesIO(door_side.content)).read(n).decode() for n in door_names if n.endswith(".md"))
    assert "We work on the parser." in door_transcript and "Hello, what do you work on?" in door_transcript
    link = await a.call("GET", f"/api/guest/links/{link_id}")
    assert [m["text"] for m in link["link"]["messages"] if m["role"] == "guest"] == ["Hello, what do you work on?"]


async def test_a_summary_card_is_kept_on_both_sides(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    link_id, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    made = await b.call("POST", f"/api/agent-talks/{talk}/summary")
    assert made["cards"], made
    await until(lambda: a.db.fetchone("SELECT 1 FROM link_cards WHERE side = 'door'"), what="the summary at A")
    door_made = await a.call("POST", f"/api/guest/links/{link_id}/summary")
    assert door_made["cards"]
    await until(lambda: b.db.fetchone("SELECT 1 FROM link_cards WHERE side = 'talk' AND direction = 'theirs'"), what="A's summary at B")
