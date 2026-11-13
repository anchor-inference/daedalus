"""Two Daedalus installations over an agent link, from end to end: one owns the link, the other's agent is its guest.

The world is :mod:`tests.support.a2a_world`: real doors, real bridges and conversations following them with their own
loops, the operator's real API routes, scripted models. Each test is one flow the operator uses, followed from the
first word to what each side ends up holding.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.extensions import agent_talk as talk_extension
from daedalus.extensions.guest_door import OWNER_NOTE
from daedalus.extensions.link_flows import approval_key_of
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


# -- first contact, contacts, stand-ins ----------------------------------------------------------------------


@pytest.mark.parametrize("decision", ["admit", "refuse"])
async def test_a_first_contact_waits_for_the_owner_who_admits_or_refuses_it(pair: tuple[Installation, Installation], decision: str) -> None:
    a, b = pair
    _, url = await a.make_link(tools="read", approval=True)
    b.brain.then({"tool": "AgentTalk", "args": {"action": "open", "link": url, "text": "Hello, may we talk?"}}, {"text": "Waiting for them."})
    await b.submit(f"Ask the agent at {url} whether we may talk.")
    held = await until(lambda: held_of(b), what="B's first message waiting for B's operator")
    talk = str(held[0]["talk_id"])
    await b.call("POST", f"/api/agent-talks/{talk}/messages/{held[0]['id']}/send", json={})
    await b.settled()
    conversation = await until(lambda: a.store.conn.execute("SELECT id FROM conversations WHERE state = 'pending'").fetchone(), what="A's pending conversation")
    notice = await until(lambda: a.db.fetchone("SELECT * FROM notifications WHERE kind = 'guest_contact'"), what="A's operator told of the first contact")
    assert "a colleague" in notice["title"]
    a.brain.then({"text": "Yes, we may."})
    await a.call("POST", f"/api/guest/conversations/{conversation['id']}/{decision}")
    if decision == "admit":
        await until(lambda: b.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE role = 'them' AND text = 'Yes, we may.'"), what="the answer once admitted")
    else:
        await until(lambda: b.db.fetchone("SELECT 1 FROM agent_talks WHERE id = ? AND state = 'closed'", (talk,)), what="B's conversation closed on the refusal")
        assert (await b.talks.talk(talk))["reason"] == "refused"
        assert not any("guest message" in last_user_text(r) for r in a.brain.requests), "a refused stranger never reached A's agent"


async def test_a_contact_is_made_from_a_conversation_and_either_agent_writes_first(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    _, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    offered = await b.call("POST", "/api/contacts/offer", json={"via": f"talk:{talk}", "name": "A", "tools": "read", "text": "Shall we keep in touch?"})
    assert offered["state"] == "offered"
    incoming = await until(lambda: a.db.fetchone("SELECT * FROM link_contacts WHERE state = 'incoming'"), what="the offer at A")
    accepted = await a.call("POST", f"/api/contacts/{incoming['id']}/accept", json={"session_id": a.chat, "tools": "read", "name": "B"})
    assert accepted["state"] in ("accepting", "active")
    await until(lambda: b.db.fetchone("SELECT 1 FROM link_contacts WHERE state = 'active'"), what="the contact active at B")
    await until(lambda: a.db.fetchone("SELECT 1 FROM link_contacts WHERE state = 'active'"), what="the contact active at A")
    listed = await a.call("GET", "/api/contacts")
    mine = listed["contacts"][0]
    assert mine["state"] == "active" and mine["fingerprint"] and "invite" not in str(mine).replace("invite_hours", "")
    # A's agent writes first, from another chat, by the contact's name.
    other = (await a.manager.create_session("elsewhere", own_directory=True)).session.id
    a.brain.then({"tool": "AgentTalk", "args": {"action": "send", "contact": "B", "text": "A here: the build is green.", "wait_seconds": 0}}, {"text": "Told B."})
    b.brain.then({"text": "Thanks, noted."})
    await a.say("Tell B the build is green.", other)
    await until(lambda: any("the build is green" in last_user_text(r) for r in b.brain.requests), what="A's words starting a turn at B")
    turn = next(r for r in b.brain.requests if "the build is green" in last_user_text(r))
    assert "guest message" in last_user_text(turn), "a contact's words are still a guest's at B"


async def test_a_stand_in_answers_from_its_briefing_and_takes_no_files(pair: tuple[Installation, Installation], monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.extensions import api_guest

    # A model behind the key proxy, which this world has none of: the form's list, as the app would show it.
    monkeypatch.setattr(api_guest, "model_options", lambda _app: [{"id": "cheap", "label": "cheap", "path": "deepseek/v1", "model": "cheap-model", "price_in": 0.1, "price_out": 0.2, "free": False}])
    monkeypatch.setattr(api_guest, "model_choices", lambda _app: (api_guest.model_options(_app), []))
    a, _ = pair
    _, url = await a.make_link(mode="stand_in", briefing="Harbor listens on port 7312.")
    guest = HttpGuest(a.doors, url)
    page = await guest.read_page()
    assert "a few short text messages" in page and "/a/files" not in page, "a stand-in's page says nothing of what it cannot do"
    await guest.redeem()
    a.stand_in_model.lines.append("Harbor listens on port 7312.")
    await guest.send("Which port does Harbor use?")
    seen = await guest.read_until(lambda seen: any(m["from"] == "agent" for m in seen))
    assert "7312" in seen[-1]["text"]
    refused = await guest.client.post("/a/files", json={"name": "x.txt", "size": 1, "sha256": "0" * 64}, headers=guest.headers)
    assert refused.status_code == 403 and "text messages only" in refused.json()["detail"]
    assert not any("guest message" in last_user_text(r) for r in a.brain.requests), "the chat's own agent never hears a stand-in's guest"
    await guest.close()


async def test_a_guest_turn_subscribes_and_writes_first_later(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    _, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    a.brain.then({"tool": "AgentTalk", "args": {"action": "subscribe", "text": "Tell them when the build is green.", "every_minutes": 15, "times": 2}}, {"text": "I will tell you."})
    said = await says(b, talk, {"action": "send", "text": "Tell us when your build is green."})
    assert "I will tell you." in said
    sub = await until(lambda: a.db.fetchone("SELECT * FROM link_subscriptions WHERE state = 'active'"), what="the subscription at A")
    await a.db.execute("UPDATE link_subscriptions SET next_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", sub["id"]))
    a.brain.then({"text": "The build is green now."})
    await a.flows.sweep()
    await until(lambda: b.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE role = 'them' AND text LIKE '%build is green now%'"), seconds=20, what="A writing first to B")


# -- asking several at once ---------------------------------------------------------------------------------


@pytest.fixture
async def three(settings: Settings, tmp_path: Path) -> Any:
    sides, databases, _ = await world(settings, tmp_path, ("a", "b", "c"))
    yield sides
    for side in sides:
        await side.close()
    for db in databases:
        await db.close()


async def test_one_question_to_two_agents_wakes_the_chat_once_with_both_answers(three: list[Installation]) -> None:
    a, b, c = three
    _, url_a = await a.make_link(tools="read")
    _, url_c = await c.make_link(tools="read")
    talk_a = await opened(a, b, url_a)
    talk_c = await opened(c, b, url_c, answer="We work on the compiler.")
    a.brain.then({"text": "Ours costs 10."})
    c.brain.then({"text": "Ours costs 12."})
    b.brain.then(
        {"tool": "AgentTalk", "args": {"action": "ask", "conversations": [talk_a, talk_c], "text": "What does your plan cost?", "deadline_minutes": 30}},
        {"text": "Asked both."},
        {"text": "A is cheaper: 10 against 12."},
    )
    await b.say("Ask both what their plan costs and compare.")
    poll = await until(lambda: b.db.fetchone("SELECT * FROM link_polls WHERE state != 'open'"), seconds=20, what="the poll settled")
    turn = await until(lambda: next((r for r in b.brain.requests if "Ours costs 10." in last_user_text(r) and "Ours costs 12." in last_user_text(r)), None), what="one turn with both answers")
    assert turn is not None and poll is not None
    tab = await b.call("GET", f"/api/sessions/{b.chat}/agent-talks")
    assert tab["polls"] and {e["answer"] for e in tab["polls"][0]["entries"]} >= {"Ours costs 10.", "Ours costs 12."}, tab["polls"]


# -- code and meetings ------------------------------------------------------------------------------------------


async def test_a_patch_the_guest_sent_is_applied_on_a_branch_and_the_guest_hears_the_commit(pair: tuple[Installation, Installation]) -> None:
    import subprocess

    a, b = pair
    repo = a.workspace
    for command in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "someone@example.test"], ["git", "config", "user.name", "someone"]):
        subprocess.run(command, cwd=repo, check=True)
    (repo / "parser.py").write_text("def parse(text):\n    return text.split()\n")
    subprocess.run(["git", "add", "parser.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "parser"], cwd=repo, check=True)
    diff = "--- a/parser.py\n+++ b/parser.py\n@@ -1,2 +1,4 @@\n def parse(text):\n+    if not text:\n+        return []\n     return text.split()\n"
    (b.workspace / "fix.diff").write_text(diff)
    _, url = await a.make_link(tools="trusted")
    talk = await opened(a, b, url)
    await b.talks.configure(talk, review="off")

    def apply(request: Any) -> dict[str, Any]:
        sent = next(line for line in last_user_text(request).splitlines() if "fix.diff" in line)
        path = sent[sent.index("guest-files/"):].split(" ")[0]
        return {"tool": "AgentTalk", "args": {"action": "apply_patch", "files": [path], "branch": "guest/fix-empty"}}

    a.brain.then(apply, {"text": "Applied on guest/fix-empty."})
    said = await says(b, talk, {"action": "send", "text": "A fix for empty input.", "files": ["fix.diff"]})
    assert "commit" in said and "guest/fix-empty" in said, said
    await until(lambda: b.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE role = 'them' AND text = 'Applied on guest/fix-empty.'"), what="the turn's own answer after it")
    log = subprocess.run(["git", "log", "--oneline", "guest/fix-empty"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert len(log.splitlines()) == 2, log
    shown = subprocess.run(["git", "show", "guest/fix-empty:parser.py"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "if not text" in shown
    assert subprocess.run(["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip() == "main", "the operator's tree is untouched"


async def test_free_times_go_to_the_guest_and_a_meeting_waits_for_the_operator(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    _, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    a.brain.then({"tool": "AgentTalk", "args": {"action": "slots", "duration_minutes": 30}}, {"text": "Here are free times."})
    said = await says(b, talk, {"action": "send", "text": "When are you free for half an hour?"})
    assert "to 2" in said, said
    await until(lambda: b.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE role = 'them' AND text = 'Here are free times.'"), what="the turn's own answer after the times")
    card = await until(lambda: b.db.fetchone("SELECT * FROM link_cards WHERE side = 'talk' AND type = 'slots'"), what="the free times as a card at B")
    assert card["direction"] == "theirs"
    a.brain.then({"tool": "AgentTalk", "args": {"action": "meet", "start": "2030-01-07T10:00:00+00:00", "end": "2030-01-07T10:30:00+00:00", "text": "Parser sync"}}, {"text": "The owner decides."})
    said = await says(b, talk, {"action": "send", "text": "Book Monday 10:00 UTC, please."})
    assert "The owner decides." in said
    action = await until(lambda: a.db.fetchone("SELECT * FROM link_actions WHERE kind = 'meeting' AND state = 'held'"), what="the meeting waiting for A's operator")
    await a.manager.grant(a.chat, approval_key_of(action["id"]), via="app")
    await until(lambda: a.db.fetchone("SELECT 1 FROM link_actions WHERE id = ? AND state = 'done'", (action["id"],)), what="the meeting booked at A")
    booked = await until(lambda: b.db.fetchone("SELECT * FROM link_cards WHERE side = 'talk' AND type = 'meeting'"), what="B told it is booked")
    assert '"booked"' in booked["card"] and "Parser sync" in booked["card"]


async def test_a_read_that_left_before_a_send_does_not_clear_the_wait_the_send_set(pair: tuple[Installation, Installation]) -> None:
    """Seen as a send told "nothing is owed" with its answer never read: a read out before the send came back after
    it with "nothing owed", and cleared the wait the send had just set."""
    a, b = pair
    _, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    before = await b.talks.talk(talk)  # the read's view, taken as it left
    await b.db.execute("UPDATE agent_talks SET sends = sends + 1, waiting_for = 'reply', quiet = 0 WHERE id = ?", (talk,))  # the send meanwhile
    stale = talk_extension.DoorAnswer(200, {"status": "open", "messages": [], "cursor": before["cursor"], "stop_polling": True, "waiting_for": "nothing"}, None, True)
    assert await b.talks._took(before, stale) == 0.0, "read again rather than settle on what the door said before the send"
    assert (await b.talks.talk(talk))["waiting_for"] == "reply"


async def test_a_contact_is_made_even_when_the_other_side_redeems_before_the_accept_returns(pair: tuple[Installation, Installation]) -> None:
    """The other side redeems our link as soon as it reads our answer. When that redeem reached us before our own
    accept had recorded itself, the offer was still "incoming", the redeem was ignored, and the contact stayed half made."""
    a, b = pair
    _, url = await a.make_link(tools="read")
    talk = await opened(a, b, url)
    await b.call("POST", "/api/contacts/offer", json={"via": f"talk:{talk}", "name": "A", "tools": "read", "text": "Keep in touch?"})
    incoming = await until(lambda: a.db.fetchone("SELECT * FROM link_contacts WHERE state = 'incoming'"), what="the offer at A")
    posted = a.talks.post_contact

    async def slow_return(*args: Any, **kwargs: Any) -> None:
        await posted(*args, **kwargs)
        # Hold the return until B has redeemed the link the answer carried and A's bridge has heard of it.
        await until(lambda: a.store.conn.execute("SELECT 1 FROM events WHERE kind IN ('opened', 'first_contact')").fetchone(), what="B's redeem at A's door")
        await asyncio.sleep(0.5)

    a.talks.post_contact = slow_return  # type: ignore[method-assign]
    await a.call("POST", f"/api/contacts/{incoming['id']}/accept", json={"session_id": a.chat, "tools": "read", "name": "B"})
    await until(lambda: a.db.fetchone("SELECT 1 FROM link_contacts WHERE state = 'active'"), what="the contact active at A")
