"""Agent links from end to end: two installations, the strangers at their doors, and every flow the operator uses.

Nothing here is a piece tested alone. Each installation is wired as the application wires it and follows its door
with its own loops (:mod:`tests.support.a2a_world`); the operator acts through the real API routes; a guest is
another installation's agent, a plain HTTP agent doing what the link's page says, or an A2A client. The models are
scripted, so what is proved is what the code does with what a model chose, not what a model would choose.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.extensions import agent_talk as talk_extension
from tests.support.a2a_world import A2AClient, HttpGuest, Installation, last_user_text, tool_results, until, world
from tests.unit.test_guest_turns import offered

pytestmark = pytest.mark.usefixtures("bare_sandbox")


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(talk_extension, "DOOR_WAIT_SECONDS", 1)
    monkeypatch.setattr(talk_extension, "MIN_CYCLE_SECONDS", 0.01)
    monkeypatch.setattr(talk_extension, "QUIET_POLL_SECONDS", 0.05)
    monkeypatch.setattr(talk_extension, "FAILURE_BACKOFF_MAX", 0.05)


@pytest.fixture
async def pair(settings: Settings, tmp_path: Path) -> Any:
    sides, databases, doors = await world(settings, tmp_path)
    sides[0].doors = doors
    yield sides[0], sides[1]
    for side in sides:
        await side.close()
    for db in databases:
        await db.close()


def guest_of(side: Installation, url: str) -> HttpGuest:
    return HttpGuest(side.doors, url)


# -- a plain HTTP agent at a direct link ------------------------------------------------------------------


@pytest.mark.parametrize("tools", ["read", "trusted"])
async def test_an_http_agent_asks_reads_the_answer_and_gets_the_file_it_asked_for(pair: tuple[Installation, Installation], tools: str) -> None:
    a, _ = pair
    (a.workspace / "deliverables" / "review").mkdir(parents=True)
    (a.workspace / "deliverables" / "review" / "REPORT.md").write_text("# Report\nall good\n")
    (a.workspace / "deliverables" / "review" / "scores.json").write_text('{"a": 1}')
    link_id, url = await a.make_link(tools=tools)
    guest = guest_of(a, url)
    page = await guest.read_page()
    assert "/redeem" in page and "/a/messages" in page and "/content?offset=" in page
    await guest.redeem()
    a.brain.then({"text": "We checked the answers' quality: 14 of 20 met the request."})
    await guest.send("What did you find?")
    await guest.read_until(lambda seen: any(m["from"] == "agent" for m in seen))
    assert guest.seen[-1]["text"].startswith("We checked")
    a.brain.then(
        {"tool": "AgentTalk", "args": {"action": "send", "files": ["deliverables/review"], "text": "The full review as one zip."}},
        {"text": "Here is the full review."},
    )
    await guest.send("Please send the full report and its materials as one zip.")
    if tools == "read":
        await until(lambda: a.bridge.held(a.chat), what="the files waiting for the operator")
        held = (await a.call("GET", f"/api/sessions/{a.chat}/agent-talks"))["guest_held"]
        assert [f["name"] for f in held[0]["files"]] == ["review.zip"]
        await a.call("POST", f"/api/guest/links/{link_id}/outbox/{held[0]['batch']}/send", json={"drop": []})
    asked = next(r for r in reversed(a.brain.requests) if "one zip" in last_user_text(r))
    assert "AgentTalk" in offered(asked), "the turn can hand the file over in one step, without looking for the tool first"
    files = await guest.read_until(lambda seen: any(m.get("files") for m in seen))
    sent = next(m for m in files if m.get("files"))["files"][0]
    data = await guest.download(sent)
    assert hashlib.sha256(data).hexdigest() == sent["sha256"]
    assert sorted(zipfile.ZipFile(io.BytesIO(data)).namelist()) == ["REPORT.md", "scores.json"]
    await guest.close()


# -- one installation's agent at another's door -------------------------------------------------------------


async def test_our_agent_opens_a_link_the_operator_pasted_and_the_other_agent_answers(pair: tuple[Installation, Installation]) -> None:
    a, b = pair
    _, url = await a.make_link(tools="read")
    b.brain.then(
        {"tool": "AgentTalk", "args": {"action": "open", "link": url, "text": "Hello, what does your owner work on?"}},
        {"text": "They work on the parser."},
    )
    a.brain.then({"text": "We work on the parser."})
    await b.submit(f"Ask the agent at {url} what they work on.")
    held = await until(lambda: held_messages(b), what="the first message waiting for the operator")
    await b.call("POST", f"/api/agent-talks/{held[0]['talk_id']}/messages/{held[0]["id"]}/send", json={})
    await b.settled()
    assert "We work on the parser." in tool_results(b.brain.requests[-1])


async def held_messages(side: Installation) -> list[Any]:
    return [dict(row) for row in await side.db.fetchall("SELECT * FROM agent_talk_messages WHERE state = 'held' ORDER BY id")]


# -- A2A ------------------------------------------------------------------------------------------------------


async def test_an_a2a_client_sends_a_message_and_gets_the_answer_as_a_task(pair: tuple[Installation, Installation]) -> None:
    a, _ = pair
    _, url = await a.make_link(tools="read")
    client = A2AClient(a.doors, url)
    card = await client.card()
    assert card["supportedInterfaces"][0]["url"] == "https://a.example.test/a/rpc" and card["capabilities"]["pushNotifications"] is False
    await client.redeem()
    a.brain.then({"text": "Port 7312."})
    sent = await client.call("SendMessage", {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "Which port?"}]}})
    assert "result" in sent, sent
    await client.close()


# -- the exchange that failed in real use ------------------------------------------------------------------


ZIP_CALL = {"action": "send", "files": ["deliverables/review"], "text": "The full report and its materials as one zip.", "wait_seconds": 30}


def reviewing(side: Installation, verdicts: list[str]) -> None:
    """The auto-review on for messages, with a reviewer that says what the test hands it."""
    from daedalus.config import ActionRulesConfig

    side.manager.config.action_rules = ActionRulesConfig(levels={"message": "auto"})

    async def model(prompt: str, preset: str, timeout: float) -> tuple[str, str]:
        return verdicts.pop(0) if verdicts else "ALLOW", "small"

    side.manager.action_rules._ask_model = model  # type: ignore[method-assign]


async def test_a_blocked_send_to_a_trusted_guest_reaches_them_once_the_operator_allows_it(pair: tuple[Installation, Installation]) -> None:
    """Session of 2026-11-13: a trusted guest asked for the report as a zip, the review blocked the send, and every
    "Allow anyway" ran the same call again in a turn of the operator's, which was told the chat had no conversation."""
    a, _ = pair
    (a.workspace / "deliverables" / "review").mkdir(parents=True)
    (a.workspace / "deliverables" / "review" / "REPORT.md").write_text("# Report\n")
    _, url = await a.make_link(tools="trusted")
    guest = guest_of(a, url)
    await guest.read_page()
    await guest.redeem()
    reviewing(a, ["BLOCK the operator asked only for a short report."])
    a.brain.then({"tool": "AgentTalk", "args": ZIP_CALL}, {"text": "The send was blocked; the owner decides."})
    await guest.send("Please send the full report and its materials as one zip.")
    await guest.read_until(lambda seen: any(m["from"] == "agent" for m in seen))
    pending = a.state().metadata.get("policy_pending") or {}
    assert len(pending) == 1, pending
    key = next(iter(pending))
    # The operator presses "Allow anyway": the agent is woken in a turn of the operator's and repeats the call unchanged.
    a.brain.then({"tool": "AgentTalk", "args": ZIP_CALL}, {"text": "Sent."})
    await a.manager.grant(a.chat, key, via="app")
    await a.settled()
    assert "Sent to the guest" in tool_results(a.brain.requests[-1]), tool_results(a.brain.requests[-1])
    files = await guest.read_until(lambda seen: any(m.get("files") for m in seen))
    sent = next(m for m in files if m.get("files"))
    assert sent["from"] == "agent" and sent["files"][0]["name"] == "review.zip"
    assert zipfile.ZipFile(io.BytesIO(await guest.download(sent["files"][0]))).namelist() == ["REPORT.md"]
    await guest.close()


# What follows is the review's and the override's, which are being changed apart from this suite: each is an
# expectation the exchange above showed, written down as one so it is checked once that work lands.


@pytest.mark.xfail(strict=True, reason="the override is spent by the replayed call even when that call fails; the review's own code is being changed apart from this suite")
async def test_an_allow_anyway_is_not_spent_by_a_replayed_call_that_failed(pair: tuple[Installation, Installation]) -> None:
    a, _ = pair
    (a.workspace / "deliverables" / "review").mkdir(parents=True)
    (a.workspace / "deliverables" / "review" / "REPORT.md").write_text("# Report\n")
    _, url = await a.make_link(tools="trusted")
    guest = guest_of(a, url)
    await guest.read_page()
    await guest.redeem()
    reviewing(a, ["BLOCK not asked for."])
    a.brain.then({"tool": "AgentTalk", "args": ZIP_CALL}, {"text": "Blocked."})
    await guest.send("Send the report as a zip.")
    await guest.read_until(lambda seen: any(m["from"] == "agent" for m in seen))
    key = next(iter(a.state().metadata.get("policy_pending") or {}))
    await guest.end()  # the guest has gone by the time the operator decides: the replayed call cannot succeed
    a.brain.then({"tool": "AgentTalk", "args": ZIP_CALL}, {"text": "It did not go."})
    await a.manager.grant(a.chat, key, via="app")
    await a.settled()
    assert "no open conversation" in tool_results(a.brain.requests[-1])
    assert key in (a.state().metadata.get("policy_grants") or {}), "a call that did nothing has not used the operator's one allowance"
    await guest.close()


@pytest.mark.xfail(strict=True, reason="the review judges a trusted guest's send against the operator's own requests in the chat, never the guest's; the review's code is being changed apart from this suite")
async def test_the_review_of_a_trusted_guests_send_reads_what_the_guest_asked_for(pair: tuple[Installation, Installation]) -> None:
    a, _ = pair
    (a.workspace / "report.md").write_text("# Report\n")
    _, url = await a.make_link(tools="trusted")
    guest = guest_of(a, url)
    await guest.read_page()
    await guest.redeem()
    prompts: list[str] = []
    reviewing(a, [])

    async def model(prompt: str, preset: str, timeout: float) -> tuple[str, str]:
        prompts.append(prompt)
        return "ALLOW", "small"

    a.manager.action_rules._ask_model = model  # type: ignore[method-assign]
    a.brain.then({"text": "Noted."})
    await a.say("Give me a very short report with clear numbers.")
    a.brain.then({"tool": "AgentTalk", "args": {"action": "send", "files": ["report.md"]}}, {"text": "Here it is."})
    await guest.send("Please send me the full report as a file.")
    await guest.read_until(lambda seen: any(m.get("files") for m in seen))
    assert prompts and "full report as a file" in prompts[-1], "the request being served is the guest's, which the operator made the link to serve"
    await guest.close()


# -- what a guest that follows the door literally is told ------------------------------------------------------


async def test_a_guest_that_redeemed_can_still_read_how_files_and_tasks_work(pair: tuple[Installation, Installation]) -> None:
    """An agent's fetch tool may hand it a summary of the page; once it has redeemed, the page is spent for everybody
    else. Pointed back at the page for files and tasks, it must be able to read it with its token."""
    a, _ = pair
    _, url = await a.make_link(tools="read")
    guest = guest_of(a, url)
    redeemed = await guest.redeem()
    assert "Bearer" in redeemed["more"]
    again = await guest.client.get(url, headers=guest.headers)
    assert again.status_code == 200 and "/content?offset=" in again.text and "kind" in again.text
    stranger = await guest.client.get(url)
    assert stranger.status_code == 410, "without the token it is spent, as before"
    await guest.close()


async def test_a_guest_whose_files_wait_for_the_owner_is_told_to_wait_not_to_end(pair: tuple[Installation, Installation]) -> None:
    a, _ = pair
    (a.workspace / "notes.txt").write_text("the notes")
    link_id, url = await a.make_link(tools="read")
    guest = guest_of(a, url)
    await guest.read_page()
    await guest.redeem()
    a.brain.then({"tool": "AgentTalk", "args": {"action": "send", "files": ["notes.txt"]}}, {"text": "The notes follow once the owner lets them go."})
    await guest.send("Send me the notes file.")
    await guest.read_until(lambda seen: any(m["from"] == "agent" for m in seen))
    told = guest.last
    assert told["stop_polling"] is False or "end" not in told["detail"].lower(), told
    quiet = await guest.read(wait=0)
    assert quiet["waiting_for"] == "owner" and "do not end" in quiet["detail"].lower(), quiet
    held = await a.bridge.held(a.chat)
    await a.call("POST", f"/api/guest/links/{link_id}/outbox/{held[0]['batch']}/send", json={"drop": []})
    files = await guest.read_until(lambda seen: any(m.get("files") for m in seen))
    assert next(m for m in files if m.get("files"))["files"][0]["name"] == "notes.txt"
    after = await guest.read(wait=0)
    assert after["waiting_for"] == "nothing", "once the files went there is nothing more to wait for"
    await guest.close()


async def test_a_guest_whose_question_went_to_the_owner_waits_for_the_answer(pair: tuple[Installation, Installation]) -> None:
    from daedalus.extensions import questions

    a, _ = pair
    _, url = await a.make_link(tools="read")
    guest = guest_of(a, url)
    await guest.read_page()
    await guest.redeem()
    a.brain.then({"tool": "AgentTalk", "args": {"action": "ask_owner", "text": "May I share the price list?"}}, {"text": "I am checking with the owner."})
    await guest.send("Can you share the price list?")
    await guest.read_until(lambda seen: any(m["from"] == "agent" for m in seen))
    quiet = await guest.read(wait=0)
    assert quiet["waiting_for"] == "owner" and quiet["stop_polling"] is False or "later" in quiet["detail"], quiet
    asks = await a.manager.asks.of_session(a.chat)
    a.brain.then({"text": "The owner says yes: 10 per unit."})
    done = await questions.answer(a.app, [{"ask_id": asks[0].id, "text": "Yes, the public one."}], project_id=None, via="app", session_id=a.chat)  # type: ignore[arg-type]
    assert done["results"][0]["delivered"] is True, done
    seen = await guest.read_until(lambda seen: any("10 per unit" in m["text"] for m in seen))
    assert seen[-1]["from"] == "agent"
    await guest.close()


# -- A2A, the whole way ------------------------------------------------------------------------------------------


def a2a_message(text: str, message_id: str, **extra: Any) -> dict[str, Any]:
    return {"message": {"messageId": message_id, "role": "ROLE_USER", "parts": [{"text": text}], **extra}}


async def a2a_settled(client: A2AClient, task_id: str, states: set[str], seconds: float = 20.0) -> dict[str, Any]:
    async def look() -> dict[str, Any] | None:
        got = (await client.call("GetTask", {"id": task_id}))["result"]
        return got if got["status"]["state"] in states else None

    return await until(look, seconds, what=f"the task reaching {states}")


@pytest.mark.parametrize("tools", ["read", "trusted"])
async def test_an_a2a_client_asks_for_a_file_and_downloads_the_artifact(pair: tuple[Installation, Installation], tools: str) -> None:
    a, _ = pair
    (a.workspace / "report.md").write_text("# Report\nfine\n")
    link_id, url = await a.make_link(tools=tools)
    client = A2AClient(a.doors, url)
    await client.redeem()
    a.brain.then({"tool": "AgentTalk", "args": {"action": "send", "files": ["report.md"]}}, {"text": "The report."})
    sent = await client.call("SendMessage", {**a2a_message("Send me the report as a file.", "m1"), "configuration": {"returnImmediately": True}})
    task_id = sent["result"]["task"]["id"]
    if tools == "read":
        await until(lambda: a.bridge.held(a.chat), what="the file waiting for the operator")
        waiting = (await client.call("GetTask", {"id": task_id}))["result"]
        assert waiting["status"]["state"] == "TASK_STATE_WORKING", "the task is not over while its file waits for the owner"
        held = await a.bridge.held(a.chat)
        await a.call("POST", f"/api/guest/links/{link_id}/outbox/{held[0]['batch']}/send", json={"drop": []})
    task = await a2a_settled(client, task_id, {"TASK_STATE_COMPLETED"})
    parts = [part for artifact in task.get("artifacts") or [] for part in artifact["parts"] if "url" in part]
    assert parts and parts[0]["filename"] == "report.md", task
    got = await client.client.get(parts[0]["url"].replace(client.base, "") + "?offset=0", headers={"Authorization": f"Bearer {client.token}"})
    assert got.content == b"# Report\nfine\n" and hashlib.sha256(got.content).hexdigest() == parts[0]["metadata"]["sha256"]
    await client.close()


async def test_an_a2a_client_cancels_a_task_and_the_turn_stops(pair: tuple[Installation, Installation]) -> None:
    a, _ = pair
    _, url = await a.make_link(tools="read")
    client = A2AClient(a.doors, url)
    await client.redeem()
    a.brain.then({"tool": "AgentTalk", "args": {"action": "ask_owner", "text": "May I look through everything?"}}, {"text": "Checking with the owner."})
    sent = await client.call("SendMessage", {**a2a_message("Look through everything.", "m1"), "configuration": {"returnImmediately": True}})
    task_id = sent["result"]["task"]["id"]
    await a2a_settled(client, task_id, {"TASK_STATE_INPUT_REQUIRED"})
    cancelled = await client.call("CancelTask", {"id": task_id})
    assert cancelled["result"]["status"]["state"] == "TASK_STATE_CANCELED", cancelled
    await until(lambda: a.db.fetchone("SELECT 1 FROM link_tasks WHERE task = ? AND state = 'cancelled'", (task_id,)), what="the owner's side hearing of the cancel")
    asks = await a.manager.asks.of_session(a.chat)
    assert asks == [], "the question the task left for the owner goes with it: an answer has nowhere to go"
    await client.close()
