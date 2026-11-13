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
    a.brain.then({"text": 'We checked the answers\' quality: 14 of 20 met the request. <file path="deliverables/review/REPORT.md"/> <run id="v12" label="scored"/>'})
    await guest.send("What did you find?")
    await guest.read_until(lambda seen: any(m["from"] == "agent" for m in seen))
    said = guest.seen[-1]["text"]
    assert said.startswith("We checked") and "deliverables/review/REPORT.md" in said and "<file" not in said and "<run" not in said, said
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


def reviewing(side: Installation, verdicts: list[str]) -> list[str]:
    """The auto-review on for messages, with a reviewer that says what the test hands it; the prompts it was shown."""
    from daedalus.config import ActionRulesConfig

    side.manager.config.action_rules = ActionRulesConfig(levels={"message": "auto"})
    shown: list[str] = []

    async def model(prompt: str, preset: str, timeout: float) -> tuple[str, str]:
        shown.append(prompt)
        return verdicts.pop(0) if verdicts else "ALLOW", "small"

    side.manager.action_rules._ask_model = model  # type: ignore[method-assign]
    return shown


async def asked_for_the_zip(a: Installation, tools: str) -> tuple[str, HttpGuest]:
    (a.workspace / "deliverables" / "review").mkdir(parents=True)
    (a.workspace / "deliverables" / "review" / "REPORT.md").write_text("# Report\n")
    link_id, url = await a.make_link(tools=tools)
    guest = guest_of(a, url)
    await guest.read_page()
    await guest.redeem()
    return link_id, guest


async def zip_arrived(guest: HttpGuest) -> dict[str, Any]:
    files = await guest.read_until(lambda seen: any(m.get("files") for m in seen))
    sent = next(m for m in files if m.get("files"))
    assert sent["from"] == "agent" and sent["files"][0]["name"] == "review.zip"
    assert zipfile.ZipFile(io.BytesIO(await guest.download(sent["files"][0]))).namelist() == ["REPORT.md"]
    return sent


async def test_the_live_incident_on_a_trusted_link_delivers_the_zip_in_the_guests_own_turn(pair: tuple[Installation, Installation]) -> None:
    """Session of 2026-11-13: a trusted guest asked for the report as a zip, the review blocked the send three times,
    and every "Allow anyway" ran the call again in a turn of the operator's that could not reach the guest. A guest the
    operator trusts is served as they would be: no review, no card, the zip with the turn's own answer."""
    a, _ = pair
    _, guest = await asked_for_the_zip(a, "trusted")
    shown = reviewing(a, ["BLOCK the operator asked only for a short report."] * 3)
    a.brain.then({"text": "Noted."})
    await a.say("Give me a very short report with clear numbers.")
    a.brain.then({"tool": "AgentTalk", "args": ZIP_CALL}, {"text": "Here is the full report as one zip."})
    await guest.send("Please send the full report and its materials as one zip.")
    sent = await zip_arrived(guest)
    assert sent["text"] == "Here is the full report as one zip.", "the files came with the turn's own answer, not after it"
    assert [m for m in guest.seen if m["from"] == "agent"] == [sent], "one answer, nothing blocked before it"
    assert not any("AgentTalk" in prompt for prompt in shown), "nothing reviewed the send"
    assert not a.state().metadata.get("policy_pending"), "no Allow card"


async def test_a_bypassed_session_on_a_limited_link_delivers_the_zip_too(pair: tuple[Installation, Installation]) -> None:
    a, _ = pair
    _, guest = await asked_for_the_zip(a, "read")
    shown = reviewing(a, ["BLOCK not asked for."] * 3)
    await a.manager.set_bypass(a.chat, True, via="app")
    a.brain.then({"tool": "AgentTalk", "args": ZIP_CALL}, {"text": "Here is the zip."})
    await guest.send("Please send the full report and its materials as one zip.")
    sent = await zip_arrived(guest)
    assert sent["text"] == "Here is the zip."
    assert not shown and not a.state().metadata.get("policy_pending")
    assert await a.bridge.held(a.chat) == [], "nothing waited for the operator's look"


async def test_an_operators_send_to_a_limited_guest_goes_once_allowed_and_its_files_once_let_go(pair: tuple[Installation, Installation]) -> None:
    """The operator's own turn writes to the guest of the chat's link. A limited guest's send is reviewed as any
    message; "Allow anyway" replays the call, which now reaches the guest, and its files wait for the operator's look."""
    a, _ = pair
    link_id, guest = await asked_for_the_zip(a, "read")
    await guest.send("Hello.")
    a.brain.then({"text": "Hello."})
    await guest.read_until(lambda seen: any(m["from"] == "agent" for m in seen))
    reviewing(a, ["BLOCK not asked for."])
    a.brain.then({"tool": "AgentTalk", "args": ZIP_CALL}, {"text": "Blocked."})
    await a.say("Send our colleague the full review as a zip.")
    key = next(iter(a.state().metadata.get("policy_pending") or {}))
    a.brain.then({"tool": "AgentTalk", "args": ZIP_CALL}, {"text": "It waits for you."})
    await a.manager.grant(a.chat, key, via="app")
    await a.settled()
    assert "wait for the operator" in tool_results(a.brain.requests[-1]), tool_results(a.brain.requests[-1])
    held = await a.bridge.held(a.chat)
    await a.call("POST", f"/api/guest/links/{link_id}/outbox/{held[0]['batch']}/send", json={"drop": []})
    await zip_arrived(guest)
    await guest.close()


async def test_an_allow_anyway_is_not_spent_by_a_replayed_call_that_failed(pair: tuple[Installation, Installation]) -> None:
    a, _ = pair
    _, guest = await asked_for_the_zip(a, "read")
    a.brain.then({"text": "Hello."})
    await guest.send("Hello.")
    await guest.read_until(lambda seen: any(m["from"] == "agent" for m in seen))
    reviewing(a, ["BLOCK not asked for."])
    a.brain.then({"tool": "AgentTalk", "args": ZIP_CALL}, {"text": "Blocked."})
    await a.say("Send our colleague the full review as a zip.")
    key = next(iter(a.state().metadata.get("policy_pending") or {}))
    await guest.end()  # the guest has gone by the time the operator decides: the replayed call cannot succeed
    a.brain.then({"tool": "AgentTalk", "args": ZIP_CALL}, {"text": "It did not go."})
    await a.manager.grant(a.chat, key, via="app")
    await a.settled()
    assert "no open conversation" in tool_results(a.brain.requests[-1])
    assert key in (a.state().metadata.get("policy_grants") or {}), "a call that did nothing has not used the operator's one allowance"
    await guest.close()


async def test_keep_blocked_sends_nothing_to_the_guest(pair: tuple[Installation, Installation]) -> None:
    a, _ = pair
    _, guest = await asked_for_the_zip(a, "read")
    a.brain.then({"text": "Hello."})
    await guest.send("Hello.")
    await guest.read_until(lambda seen: any(m["from"] == "agent" for m in seen))
    reviewing(a, ["BLOCK not asked for."])
    a.brain.then({"tool": "AgentTalk", "args": ZIP_CALL}, {"text": "Blocked."})
    await a.say("Send our colleague the full review as a zip.")
    key = next(iter(a.state().metadata.get("policy_pending") or {}))
    await a.manager.refuse(a.chat, key, via="app")
    await a.settled()
    assert not any(m.get("files") for m in (await guest.read(wait=0))["messages"] + guest.seen), "nothing the operator kept blocked went"
    assert not await a.db.fetchall("SELECT 1 FROM link_files WHERE direction = 'out'")
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


# -- a plain HTTP agent: files in pieces, tasks, an owner away ---------------------------------------------------


async def test_an_http_agent_uploads_a_large_file_resumes_after_a_drop_and_the_turn_reads_it(pair: tuple[Installation, Installation]) -> None:
    a, _ = pair
    _, url = await a.make_link(tools="read")
    guest = guest_of(a, url)
    await guest.read_page()
    await guest.redeem()
    big = bytes(range(256)) * 5000  # 1.28 MB: three pieces of 512 KiB
    file_id = await guest.upload("data.bin", big, stop_after=1)
    half = (await guest.client.get(f"/a/files/{file_id}", headers=guest.headers)).json()
    assert half["received"] == 512 * 1024 and half["state"] != "ready", half
    done = await guest.resume_upload(file_id, big)
    assert done["state"] == "ready", done
    a.brain.then({"text": "Got 1280000 bytes."})
    sent = await guest.send("Here is the data.", kind="file", files=[file_id])
    assert sent.get("id"), sent
    await guest.read_until(lambda seen: any(m["from"] == "agent" for m in seen))
    saved = next((a.workspace / "guest-files").rglob("data.bin"))
    assert saved.read_bytes() == big
    turn = next(r for r in reversed(a.brain.requests) if "guest message" in last_user_text(r))
    assert "guest-files/" in last_user_text(turn) and "1280000 bytes" in last_user_text(turn)
    await guest.close()


async def test_an_http_agent_downloads_a_large_file_and_resumes_where_it_stopped(pair: tuple[Installation, Installation]) -> None:
    a, _ = pair
    big = bytes(range(256)) * 5000
    (a.workspace / "big.bin").write_bytes(big)
    _, url = await a.make_link(tools="trusted")
    guest = guest_of(a, url)
    await guest.read_page()
    await guest.redeem()
    a.brain.then({"tool": "AgentTalk", "args": {"action": "send", "files": ["big.bin"]}}, {"text": "Attached."})
    await guest.send("Send the big file.")
    seen = await guest.read_until(lambda seen: any(m.get("files") for m in seen))
    file = next(m for m in seen if m.get("files"))["files"][0]
    got = bytearray()
    await guest.download(file, stop_after=1, into=got)
    assert len(got) == 512 * 1024
    data = await guest.download(file, start=len(got), into=got)
    assert data == big and hashlib.sha256(data).hexdigest() == file["sha256"]
    await guest.close()


async def test_a_file_over_the_links_limit_is_refused_and_no_limit_takes_it(pair: tuple[Installation, Installation]) -> None:
    a, _ = pair
    _, url = await a.make_link(tools="read", file_mb=1)
    guest = guest_of(a, url)
    await guest.redeem()
    big = b"x" * (1024 * 1024 + 1)
    refused = await guest.client.post("/a/files", json={"name": "big.bin", "size": len(big), "sha256": hashlib.sha256(big).hexdigest()}, headers=guest.headers)
    assert refused.status_code == 413 and "1 MB" in refused.json()["detail"], refused.json()
    _, url = await a.make_link(tools="read", file_mb=0, day_mb=0)
    unbounded = guest_of(a, url)
    await unbounded.redeem()
    file_id = await unbounded.upload("big.bin", big)
    assert (await unbounded.client.get(f"/a/files/{file_id}", headers=unbounded.headers)).json()["state"] == "ready"
    await guest.close()
    await unbounded.close()


async def test_an_http_agents_task_goes_working_needs_input_and_done_and_can_be_cancelled(pair: tuple[Installation, Installation]) -> None:
    from daedalus.extensions import questions

    a, _ = pair
    _, url = await a.make_link(tools="read")
    guest = guest_of(a, url)
    await guest.read_page()
    await guest.redeem()
    a.brain.then({"tool": "AgentTalk", "args": {"action": "ask_owner", "text": "Which branch?"}}, {"text": "Asking the owner which branch."})
    task = (await guest.send("Build the release.", kind="task"))["task"]
    await guest.read_until(lambda seen: any(m.get("state") == "needs_input" for m in seen))
    assert (await guest.client.get(f"/a/tasks/{task}", headers=guest.headers)).json()["state"] == "needs_input"
    quiet = await guest.read(wait=0)
    assert quiet["waiting_for"] == "owner" and "Do not end" in quiet["detail"], quiet
    asks = await a.manager.asks.of_session(a.chat)
    a.brain.then({"text": "Built from main."})
    await questions.answer(a.app, [{"ask_id": asks[0].id, "text": "main"}], project_id=None, via="app", session_id=a.chat)  # type: ignore[arg-type]
    seen = await guest.read_until(lambda seen: any(m.get("state") == "done" for m in seen))
    assert [m["state"] for m in seen if m.get("task") == task] == ["working", "needs_input", "working", "done"]
    assert next(m for m in seen if m.get("state") == "done")["text"] == "Built from main."
    a.brain.then({"tool": "AgentTalk", "args": {"action": "ask_owner", "text": "Really?"}}, {"text": "Checking."})
    second = (await guest.send("Deploy it.", kind="task"))["task"]
    await guest.read_until(lambda seen: any(m.get("task") == second and m.get("state") == "needs_input" for m in seen))
    cancelled = await guest.send(kind="status", task=second, state="cancelled")
    assert cancelled["state"] == "cancelled", cancelled
    await until(lambda: a.db.fetchone("SELECT 1 FROM link_tasks WHERE task = ? AND state = 'cancelled'", (second,)), what="the owner's side hearing of it")
    assert await a.manager.asks.of_session(a.chat) == []
    await guest.close()


async def test_an_http_agent_is_told_the_owner_is_offline_and_answered_when_they_are_back(pair: tuple[Installation, Installation]) -> None:
    import asyncio

    from guestd import mailbox as door_mailbox

    a, _ = pair
    _, url = await a.make_link(tools="read")
    guest = guest_of(a, url)
    await guest.read_page()
    await guest.redeem()
    follower = next(task for task in a.tasks if task.get_name() == "a-bridge")
    follower.cancel()
    await asyncio.gather(follower, return_exceptions=True)
    a.mailbox.host_seen -= door_mailbox.HOST_AWAY_SECONDS + 1
    sent = await guest.send("Anyone there?")
    assert sent.get("owner_offline") is True and "kept" in sent["detail"], sent
    quiet = await guest.read(wait=0)
    assert quiet["waiting_for"] == "owner_online" and quiet["stop_polling"] is True, quiet
    a.brain.then({"text": "Back now."})
    a.tasks.append(asyncio.create_task(a.bridge.serve(), name="a-bridge"))
    seen = await guest.read_until(lambda seen: any(m["from"] == "agent" for m in seen))
    assert seen[-1]["text"] == "Back now."
    await guest.close()

