"""A2A at the door: the protocol's calls as an A2A 1.0 client makes them, translated onto the same mailbox.

Hand-written JSON-RPC in the shapes of the A2A 1.0 specification (``lf.a2a.v1``: ProtoJSON field names, enum
names as strings), because the official client is not among this project's dependencies. The real door in process,
as in ``test_guest_door``: the public port a stranger's agent reaches and the control socket the host dials.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from guestd import a2a
from guestd import mailbox as mailbox_module
from guestd.control import ControlDoor
from guestd.files import CHUNK_BYTES
from guestd.front import STREAM_HEAD, Front
from guestd.limits import Rate

from tests.unit.test_guest_door import CONTROL, Rig, direct_link
from tests.unit.test_link_kinds_door import bearer, feed, upload

BASE = "https://agents.example.test"


@pytest.fixture
def rig(tmp_path: Path) -> Any:
    # Generous rates, so a test may make many calls; the rate itself is tested on its own below.
    made = Rig(tmp_path, sends=Rate(100), reads=Rate(100))
    yield made
    made.store.close()


def call(rig: Rig, token: str | None, method: str, params: dict[str, Any] | None = None, *, version: str | None = "1.0", call_id: Any = 1) -> Any:
    headers = {**(bearer(token) if token else {}), **({"A2A-Version": version} if version else {})}
    return rig.public.post("/a/rpc", json={"jsonrpc": "2.0", "id": call_id, "method": method, "params": params or {}}, headers=headers)


def result(rig: Rig, token: str, method: str, params: dict[str, Any] | None = None) -> Any:
    answer = call(rig, token, method, params)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert "error" not in body, body
    return body["result"]


def error(rig: Rig, token: str | None, method: str, params: dict[str, Any] | None = None, **options: Any) -> dict[str, Any]:
    answer = call(rig, token, method, params, **options)
    body = answer.json()
    assert "result" not in body and body["jsonrpc"] == "2.0", body
    return body["error"]


def reason(err: dict[str, Any]) -> str:
    return err["data"][0]["reason"]


def message(text: str = "", *, message_id: str = "m-1", parts: list[dict[str, Any]] | None = None, **fields: Any) -> dict[str, Any]:
    return {"messageId": message_id, "role": "ROLE_USER", "parts": parts if parts is not None else [{"text": text}], **fields}


def send(rig: Rig, token: str, text: str = "what port?", *, at_once: bool = True, **fields: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"message": message(text, **fields)}
    if at_once:
        params["configuration"] = {"returnImmediately": True}
    return result(rig, token, "SendMessage", params)["task"]


def opened(rig: Rig, **link: Any) -> tuple[str, str]:
    link_id, invite = direct_link(rig, **link)
    return link_id, rig.redeem(invite)


def reply(rig: Rig, item: dict[str, Any], **body: Any) -> None:
    answered = rig.control.post(f"/control/deliveries/{item['message_id']}/reply", json={"ok": True, **body}, headers=CONTROL)
    assert answered.status_code == 200, answered.text


def get(rig: Rig, token: str, task_id: str, **params: Any) -> dict[str, Any]:
    return result(rig, token, "GetTask", {"id": task_id, **params})


# -- the card --------------------------------------------------------------------------------------------------


def test_a_links_card_says_how_to_talk_to_it_and_nothing_of_its_owner(rig: Rig) -> None:
    _, invite = direct_link(rig, label="Masha from accounting", topic="Questions about the build server. Nothing else.")
    for path in (f"/a/{invite}/.well-known/agent-card.json", f"/a/{invite}/agent-card.json"):
        answer = rig.public.get(path)
        assert answer.status_code == 200 and answer.headers["content-type"].startswith("application/json")
        assert answer.headers["x-content-type-options"] == "nosniff"
    card = answer.json()
    for required in ("name", "description", "supportedInterfaces", "version", "capabilities", "defaultInputModes", "defaultOutputModes", "skills"):
        assert card[required], required
    assert card["name"] == "Questions about the build server" and card["description"].startswith("Questions about the build server.")
    assert card["supportedInterfaces"] == [{"url": f"{BASE}/a/rpc", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}]
    assert card["capabilities"]["streaming"] is True and card["capabilities"]["pushNotifications"] is False and card["capabilities"]["extendedAgentCard"] is True
    scheme = card["securitySchemes"]["conversationToken"]["httpAuthSecurityScheme"]
    assert scheme["scheme"] == "Bearer" and f"POST {BASE}/a/{invite}/redeem" in scheme["description"]
    assert card["securityRequirements"] == [{"schemes": {"conversationToken": {"list": []}}}]
    assert card["documentationUrl"] == f"{BASE}/a/{invite}"
    assert {skill["id"] for skill in card["skills"]} == {"conversation", "task"}
    limits = card["capabilities"]["extensions"][0]
    assert limits["uri"] == a2a.LIMITS_EXTENSION and limits["params"]["fileMb"] == 50 and limits["params"]["inlineFileBytes"] == a2a.INLINE_FILE_BYTES
    text = json.dumps(card)
    assert "Masha" not in text and "accounting" not in text, "the label is the operator's own note"
    assert "provider" not in card and "iconUrl" not in card


def test_a_stand_ins_card_offers_text_and_no_tasks_or_files(rig: Rig) -> None:
    _, invite = rig.link()
    card = rig.public.get(f"/a/{invite}/.well-known/agent-card.json").json()
    assert [skill["id"] for skill in card["skills"]] == ["conversation"] and "*/*" not in card["defaultInputModes"]
    params = card["capabilities"]["extensions"][0]["params"]
    assert params["mode"] == "stand_in" and "fileMb" not in params
    assert "The service listens on port 8080" not in json.dumps(card), "the briefing is the stand-in's alone"


def test_a_spent_links_card_is_shown_only_to_the_guest_holding_its_token(rig: Rig) -> None:
    link_id, invite = direct_link(rig)
    token = rig.redeem(invite)
    path = f"/a/{invite}/.well-known/agent-card.json"
    assert rig.public.get(path).status_code == 410
    shown = rig.public.get(path, headers=bearer(token))
    assert shown.status_code == 200 and shown.json()["capabilities"]["extensions"][0]["params"]["conversation"] == "open"
    _, other_invite = direct_link(rig)
    other = rig.redeem(other_invite)
    assert rig.public.get(path, headers=bearer(other)).status_code == 410, "another conversation's token opens nothing here"
    rig.control.post(f"/control/links/{link_id}/revoke", headers=CONTROL)
    assert rig.public.get(path, headers=bearer(token)).status_code == 410
    assert rig.public.get("/a/" + "x" * 40 + "/.well-known/agent-card.json").status_code == 404


def test_the_page_the_json_card_and_the_redeem_point_to_a2a(rig: Rig) -> None:
    _, invite = direct_link(rig)
    assert f"{BASE}/a/{invite}/.well-known/agent-card.json" in rig.public.get(f"/a/{invite}").text
    facts = rig.public.get(f"/a/{invite}", headers={"accept": "application/json"}).json()
    assert facts["a2a"] == {"agent_card": f"{BASE}/a/{invite}/.well-known/agent-card.json", "rpc": f"{BASE}/a/rpc", "version": "1.0"}
    assert "/a/rpc" in rig.public.post(f"/a/{invite}/redeem").json()["a2a"]


def test_the_extended_card_is_the_links_card_with_the_conversation(rig: Rig) -> None:
    _, token = opened(rig, topic="Build questions.")
    card = result(rig, token, "GetExtendedAgentCard")
    assert card["description"] == "Build questions." and card["capabilities"]["extensions"][0]["params"]["conversation"] == "open"


# -- JSON-RPC itself -------------------------------------------------------------------------------------------


def test_a_call_without_a_good_token_is_a_401_that_says_how_to_get_one(rig: Rig) -> None:
    answer = call(rig, None, "GetTask", {"id": "t_000000000000"})
    assert answer.status_code == 401 and answer.headers["www-authenticate"] == "Bearer"
    assert "Bearer <token>" in answer.json()["error"]["message"]
    wrong = call(rig, "not-a-token", "GetTask", {"id": "t_000000000000"})
    assert wrong.status_code == 401 and "token" in wrong.json()["error"]["message"]


def test_malformed_calls_get_json_rpc_errors(rig: Rig) -> None:
    _, token = opened(rig)
    headers = bearer(token)
    assert rig.public.post("/a/rpc", content=b"{not json", headers=headers).json()["error"]["code"] == -32700
    assert rig.public.post("/a/rpc", json=[{"jsonrpc": "2.0", "id": 1, "method": "GetTask"}], headers=headers).json()["error"]["code"] == -32600
    assert rig.public.post("/a/rpc", json={"jsonrpc": "1.0", "id": 1, "method": "GetTask"}, headers=headers).json()["error"]["code"] == -32600
    bad_params = rig.public.post("/a/rpc", json={"jsonrpc": "2.0", "id": 7, "method": "GetTask", "params": [1]}, headers=headers).json()
    assert bad_params["error"]["code"] == -32602 and bad_params["id"] == 7
    assert error(rig, token, "Teleport")["code"] == -32601
    assert rig.public.get("/a/rpc", headers=headers).status_code == 405
    big = rig.public.post("/a/rpc", content=b"x" * (70 * 1024), headers=headers)
    assert big.json()["error"]["code"] == -32600


def test_only_version_one_is_spoken_and_the_old_method_names_are_told_so(rig: Rig) -> None:
    _, token = opened(rig)
    assert error(rig, token, "GetTask", {"id": "t_0"}, version="0.3")["code"] == -32009
    old = error(rig, token, "message/send", {"message": {}}, version=None)
    assert old["code"] == -32009 and "1.0" in old["message"]
    assert error(rig, token, "GetTask", {"id": "t_000000000000"}, version="1.0.2")["code"] == -32001, "a patch version is the same version"
    assert error(rig, token, "GetTask", {"id": "t_000000000000"}, version=None)["code"] == -32001, "no header and a 1.0 method is a 1.0 call"
    by_query = rig.public.post("/a/rpc?A2A-Version=0.3", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}}, headers=bearer(token))
    assert by_query.json()["error"]["code"] == -32009


def test_sending_over_a2a_counts_against_the_same_rate_as_the_plain_door(tmp_path: Path) -> None:
    rig = Rig(tmp_path, sends=Rate(2))
    try:
        _, token = opened(rig)
        rig.say(token, "plain")
        send(rig, token, "one", message_id="1")
        busy = error(rig, token, "SendMessage", {"message": message("two", message_id="2")})
        assert reason(busy) == "BUSY" and busy["data"][0]["metadata"]["retryAfter"]
    finally:
        rig.store.close()


def test_push_notifications_are_refused_because_the_door_cannot_call_out(rig: Rig) -> None:
    _, token = opened(rig)
    for method in a2a.PUSH_METHODS:
        assert error(rig, token, method, {"taskId": "t_0", "url": "https://example.test/hook"})["code"] == -32003
    push = error(rig, token, "SendMessage", {"message": message("hi"), "configuration": {"taskPushNotificationConfig": {"url": "https://example.test/hook"}}})
    assert push["code"] == -32003 and feed(rig)["deliveries"] == [], "nothing was sent"


# -- tasks on a direct link ------------------------------------------------------------------------------------


def test_a_message_sets_a_task_that_the_turn_completes_with_its_result(rig: Rig) -> None:
    _, token = opened(rig)
    conversation_id = rig.store.conversation_by_token(token)["id"]
    task = send(rig, token, "Build the report", message_id="client-1")
    assert task["id"].startswith("t_") and task["contextId"] == conversation_id and task["status"]["state"] == "TASK_STATE_SUBMITTED"
    item = feed(rig)["deliveries"][0]
    assert (item["kind"], item["task"], item["text"]) == ("task", task["id"], "Build the report"), "the host gets what a task from the plain door is"
    assert get(rig, token, task["id"])["status"]["state"] == "TASK_STATE_WORKING"
    reply(rig, item, text="Done: report.txt", task_state="done")
    done = get(rig, token, task["id"])
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert done["status"]["message"]["role"] == "ROLE_AGENT" and done["status"]["message"]["parts"] == [{"text": "Done: report.txt"}]
    assert done["artifacts"][0]["name"] == "result" and done["artifacts"][0]["parts"] == [{"text": "Done: report.txt"}]
    history = done["history"]
    assert history[0] == {"messageId": "client-1", "contextId": conversation_id, "taskId": task["id"], "role": "ROLE_USER", "parts": [{"text": "Build the report"}]}
    assert [m["role"] for m in history] == ["ROLE_USER", "ROLE_AGENT", "ROLE_AGENT"] and history[1]["metadata"] == {"from": "door"}
    assert len(get(rig, token, task["id"], historyLength=1)["history"]) == 1
    assert "history" not in get(rig, token, task["id"], historyLength=0)
    assert error(rig, token, "GetTask", {"id": task["id"], "historyLength": -1})["code"] == -32602


def test_a_blocking_send_waits_for_the_task_and_answers_with_where_it_is(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a, "HOLD_SECONDS", 0.2)
    monkeypatch.setattr(a2a, "LOOK_SECONDS", 0.05)
    _, token = opened(rig)
    task = send(rig, token, "slow work", at_once=False)
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED" and "history" in task


def test_the_same_message_sent_twice_is_one_task_and_one_turn(rig: Rig) -> None:
    _, token = opened(rig)
    first = send(rig, token, "once", message_id="same")
    again = send(rig, token, "once", message_id="same")
    assert first["id"] == again["id"]
    assert len(feed(rig)["deliveries"]) == 1


def test_a_task_that_needs_input_takes_the_answer_and_a_finished_one_takes_nothing(rig: Rig) -> None:
    _, token = opened(rig)
    task = send(rig, token, "deploy it")
    item = feed(rig)["deliveries"][0]
    reply(rig, item, text="Which branch?", task_state="needs_input")
    asked = get(rig, token, task["id"])
    assert asked["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and asked["status"]["message"]["parts"] == [{"text": "Which branch?"}]
    follow = send(rig, token, "main", message_id="m-2", taskId=task["id"])
    assert follow["id"] == task["id"]
    second = feed(rig)["deliveries"][0]
    assert (second["kind"], second["task"], second["text"]) == ("text", task["id"], "main")
    reply(rig, second, text="Deployed.", task_state="done")
    refused = error(rig, token, "SendMessage", {"message": message("again", message_id="m-3", taskId=task["id"])})
    assert refused["code"] == -32004 and "TASK_STATE_COMPLETED" in refused["message"]
    assert error(rig, token, "SendMessage", {"message": message("x", message_id="m-4", taskId="t_ffffffffffff")})["code"] == -32001


def test_cancelling_a_task_stops_it_and_tells_the_host(rig: Rig) -> None:
    link_id, token = opened(rig)
    task = send(rig, token, "long work")
    feed(rig)
    cancelled = result(rig, token, "CancelTask", {"id": task["id"]})
    assert cancelled["status"]["state"] == "TASK_STATE_CANCELED" and cancelled["status"]["message"]["parts"] == [{"text": "You cancelled this task."}]
    assert "task_cancelled" in rig.events(link_id)
    assert error(rig, token, "CancelTask", {"id": task["id"]})["code"] == -32002
    assert error(rig, token, "CancelTask", {"id": "t_000000000000"})["code"] == -32001
    assert error(rig, token, "CancelTask", {})["code"] == -32602


def test_a_turn_that_failed_is_a_failed_task_and_one_the_agent_refused_is_rejected(rig: Rig) -> None:
    _, token = opened(rig)
    failed = send(rig, token, "a", message_id="a")
    rig.control.post(f"/control/deliveries/{feed(rig)['deliveries'][0]['message_id']}/reply", json={"ok": False}, headers=CONTROL)
    assert get(rig, token, failed["id"])["status"]["state"] == "TASK_STATE_FAILED"
    rejected = send(rig, token, "b", message_id="b")
    reply(rig, feed(rig)["deliveries"][0], text="Not something I do.", task_state="refused")
    assert get(rig, token, rejected["id"])["status"]["state"] == "TASK_STATE_REJECTED"


def test_the_context_is_the_conversation_and_another_is_refused(rig: Rig) -> None:
    _, token = opened(rig)
    conversation_id = rig.store.conversation_by_token(token)["id"]
    assert send(rig, token, "x", contextId=conversation_id)["contextId"] == conversation_id
    assert reason(error(rig, token, "SendMessage", {"message": message("y", message_id="m-9", contextId="elsewhere")})) == "CONTEXT"


def test_a_message_is_checked_before_anything_is_sent(rig: Rig) -> None:
    _, token = opened(rig)
    for bad in (
        {"message": "hi"}, {"message": {**message("hi"), "role": "ROLE_AGENT"}}, {"message": {**message("hi"), "messageId": ""}},
        {"message": message(parts=[])}, {"message": message(parts=[{"text": "a", "data": {}}])}, {"message": message(parts=[{"text": 5}])},
        {"message": message(parts=[{"text": "   "}])}, {"message": message("x" * 8001)},
    ):
        assert error(rig, token, "SendMessage", bad)["code"] == -32602, bad
    assert feed(rig)["deliveries"] == []


def test_data_parts_become_part_of_the_text(rig: Rig) -> None:
    _, token = opened(rig)
    send(rig, token, parts=[{"text": "Use these"}, {"data": {"port": 8080, "name": "ёлка"}}])
    text = feed(rig)["deliveries"][0]["text"]
    assert text.startswith("Use these\n\n```json\n") and '"port": 8080' in text and "ёлка" in text


def test_list_tasks_lists_the_conversations_tasks_newest_first(rig: Rig) -> None:
    _, token = opened(rig)
    first = send(rig, token, "one", message_id="1")
    rig.clock.now += 5
    second = send(rig, token, "two", message_id="2")
    listed = result(rig, token, "ListTasks")
    assert [task["id"] for task in listed["tasks"]] == [second["id"], first["id"]] and listed["totalSize"] == 2 and listed["nextPageToken"] == ""
    assert "history" not in listed["tasks"][0] and "artifacts" not in listed["tasks"][0]
    paged = result(rig, token, "ListTasks", {"pageSize": 1})
    assert len(paged["tasks"]) == 1 and paged["nextPageToken"] == "1"
    assert result(rig, token, "ListTasks", {"pageSize": 1, "pageToken": "1"})["tasks"][0]["id"] == first["id"]
    assert result(rig, token, "ListTasks", {"status": "TASK_STATE_COMPLETED"})["tasks"] == []
    assert error(rig, token, "ListTasks", {"pageSize": 0})["code"] == -32602
    assert reason(error(rig, token, "ListTasks", {"contextId": "elsewhere"})) == "CONTEXT"


# -- level rules and limits --------------------------------------------------------------------------------


def test_a_stand_in_answers_one_message_at_a_time_and_takes_no_files(rig: Rig) -> None:
    _, invite = rig.link()
    token = rig.redeem(invite)
    task = send(rig, token, "Which port?")
    assert task["id"].startswith("m_") and task["status"]["state"] == "TASK_STATE_SUBMITTED"
    asyncio.run(rig.turn())
    done = get(rig, token, task["id"])
    assert done["status"]["state"] == "TASK_STATE_COMPLETED" and done["artifacts"][0]["name"] == "answer"
    assert done["history"][0]["parts"] == [{"text": "Which port?"}] and done["history"][1]["role"] == "ROLE_AGENT"
    refused = error(rig, token, "SendMessage", {"message": message(message_id="f", parts=[{"raw": base64.b64encode(b"hi").decode(), "filename": "a.txt"}])})
    assert refused["code"] == -32005 and "text messages only" in refused["message"]
    assert error(rig, token, "CancelTask", {"id": task["id"]})["code"] == -32002
    assert reason(error(rig, token, "SendMessage", {"message": message("more", message_id="n", taskId=task["id"])})) == "TASK_SETTLED"


def test_a_stand_in_that_failed_is_a_failed_task(rig: Rig) -> None:
    _, invite = rig.link()
    token = rig.redeem(invite)
    rig.model.fail = True
    task = send(rig, token, "Which port?")
    asyncio.run(rig.turn())
    failed = get(rig, token, task["id"])
    assert failed["status"]["state"] == "TASK_STATE_FAILED" and failed["status"]["message"]["metadata"] == {"from": "door"}


def test_before_the_owner_admits_only_a_first_text_is_kept(rig: Rig) -> None:
    _, token = opened(rig, approval=True)
    task = send(rig, token, "hello")
    assert task["id"].startswith("m_") and task["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert "decides whether to admit" in task["status"]["message"]["parts"][0]["text"]
    assert reason(error(rig, token, "SendMessage", {"message": message("more", message_id="2")})) == "PENDING"
    file = {"raw": base64.b64encode(b"x").decode(), "filename": "x.txt"}
    assert reason(error(rig, token, "SendMessage", {"message": message(message_id="3", parts=[file])})) == "PENDING"


def test_a_links_turn_limit_holds_over_a2a(rig: Rig) -> None:
    _, token = opened(rig, turns_total=1)
    send(rig, token, "one", message_id="1")
    item = feed(rig)["deliveries"][0]
    reply(rig, item, text="ok", task_state="done")
    limited = error(rig, token, "SendMessage", {"message": message("two", message_id="2")})
    assert limited["code"] == -32000 and reason(limited) == "LIMIT" and limited["data"][0]["metadata"]["limit"] == "turns_total"


def test_a_closed_conversation_takes_nothing_and_its_open_tasks_say_so(rig: Rig) -> None:
    link_id, token = opened(rig)
    task = send(rig, token, "work")
    rig.control.post(f"/control/links/{link_id}/revoke", headers=CONTROL)
    assert get(rig, token, task["id"])["status"]["state"] in ("TASK_STATE_FAILED", "TASK_STATE_CANCELED")
    assert reason(error(rig, token, "SendMessage", {"message": message("more", message_id="2")})) == "CLOSED"


# -- files -------------------------------------------------------------------------------------------------


def test_a_small_file_inside_a_message_reaches_the_host_whole(rig: Rig) -> None:
    _, token = opened(rig)
    data = b"col1,col2\n1,2\n" * 50
    send(rig, token, parts=[{"text": "the table"}, {"raw": base64.b64encode(data).decode(), "filename": "table.csv", "mediaType": "text/csv"}])
    item = feed(rig)["deliveries"][0]
    assert [(f["name"], f["size"], f["sha256"], f["state"]) for f in item["files"]] == [("table.csv", len(data), hashlib.sha256(data).hexdigest(), "ready")]
    content = rig.control.get(f"/control/files/{item['files'][0]['id']}/content?offset=0", headers=CONTROL)
    assert content.content == data


def test_a_file_over_the_inline_bound_or_the_links_limit_is_refused_with_the_way_round(rig: Rig) -> None:
    _, token = opened(rig, file_mb=0.01)
    big = base64.b64encode(b"x" * (a2a.INLINE_FILE_BYTES + 1)).decode()
    over = error(rig, token, "SendMessage", {"message": message(parts=[{"raw": big, "filename": "big.bin"}])})
    assert reason(over) == "INLINE_LIMIT" and "/a/files" in over["message"]
    limited = error(rig, token, "SendMessage", {"message": message(message_id="2", parts=[{"raw": base64.b64encode(b"y" * 20_000).decode(), "filename": "y.bin"}])})
    assert limited["code"] == -32602 and reason(limited) == "FILE_LIMIT"
    assert error(rig, token, "SendMessage", {"message": message(message_id="3", parts=[{"raw": "@@not base64@@"}])})["code"] == -32602
    assert feed(rig)["deliveries"] == []


def test_a_larger_file_is_uploaded_in_pieces_and_named_by_its_address(rig: Rig) -> None:
    _, token = opened(rig)
    data = bytes(range(256)) * 4096
    file_id = upload(rig, token, data, "dump.bin")
    send(rig, token, parts=[{"text": "see the dump"}, {"url": f"{BASE}/a/files/{file_id}", "filename": "dump.bin"}])
    assert [f["id"] for f in feed(rig)["deliveries"][0]["files"]] == [file_id]
    elsewhere = error(rig, token, "SendMessage", {"message": message(message_id="2", parts=[{"url": "https://files.example.test/a/files/f_0123456789abcdef"}])})
    assert reason(elsewhere) == "FETCH" and "no network" in elsewhere["message"]
    unknown = error(rig, token, "SendMessage", {"message": message(message_id="3", parts=[{"url": f"{BASE}/a/files/f_0123456789abcdef"}])})
    assert unknown["code"] == -32602


def test_the_agents_files_are_url_parts_the_guest_downloads_with_its_token(rig: Rig) -> None:
    _, token = opened(rig)
    task = send(rig, token, "make a picture")
    item = feed(rig)["deliveries"][0]
    data = b"\x89PNG" + b"\0" * 100
    made = rig.control.post(f"/control/conversations/{item['conversation_id']}/files", json={"name": "chart.png", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}, headers=CONTROL)
    file_id = made.json()["id"]
    rig.control.put(f"/control/files/{file_id}?offset=0", content=data, headers=CONTROL)
    reply(rig, item, text="Here.", files=[file_id], task_state="done")
    done = get(rig, token, task["id"])
    part = done["artifacts"][0]["parts"][1]
    assert part["url"] == f"{BASE}/a/files/{file_id}/content" and part["filename"] == "chart.png" and part["mediaType"] == "image/png"
    assert part["metadata"]["sha256"] == hashlib.sha256(data).hexdigest() and part["metadata"]["size"] == len(data) and part["metadata"]["pieceBytes"] == CHUNK_BYTES
    assert rig.public.get(part["url"].removeprefix(BASE), headers=bearer(token)).content == data
    assert rig.public.get(part["url"].removeprefix(BASE)).status_code == 401


# -- streams -------------------------------------------------------------------------------------------------


def events_of(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def test_a_stream_of_a_settled_task_is_the_task_and_then_its_end(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a, "STREAM_SECONDS", 0.3)
    monkeypatch.setattr(a2a, "LOOK_SECONDS", 0.05)
    _, invite = rig.link()
    token = rig.redeem(invite)
    task = send(rig, token, "Which port?")
    asyncio.run(rig.turn())
    with rig.public.stream("POST", "/a/rpc", json={"jsonrpc": "2.0", "id": "s", "method": "SubscribeToTask", "params": {"id": task["id"]}}, headers=bearer(token)) as answer:
        body = answer.read().decode()
    assert answer.headers["content-type"].startswith("application/json"), "a settled task has nothing to follow"
    assert json.loads(body)["error"]["code"] == -32004
    with rig.public.stream("POST", "/a/rpc", json={"jsonrpc": "2.0", "id": "s", "method": "SendStreamingMessage", "params": {"message": message("again", message_id="2")}},
                           headers=bearer(token)) as answer:
        assert answer.headers["content-type"].startswith("text/event-stream") and answer.headers["x-accel-buffering"] == "no"
        first = events_of(answer.read().decode())
    assert first[0]["id"] == "s" and first[0]["result"]["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"


def test_a_stream_refused_before_it_starts_is_a_plain_json_rpc_error(rig: Rig) -> None:
    _, invite = rig.link()
    token = rig.redeem(invite)
    answer = rig.public.post("/a/rpc", json={"jsonrpc": "2.0", "id": 3, "method": "SendStreamingMessage", "params": {"message": message(parts=[{"raw": "aGk=", "filename": "x"}])}},
                             headers=bearer(token))
    assert answer.headers["content-type"].startswith("application/json") and answer.json()["error"]["code"] == -32005
    assert error(rig, token, "SubscribeToTask", {"id": "t_000000000000"})["code"] == -32001


async def test_a_stream_brings_each_change_of_a_task_in_order_and_ends_when_it_settles(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a, "LOOK_SECONDS", 0.02)
    _, token = await asyncio.to_thread(opened, rig)
    task = await asyncio.to_thread(send, rig, token, "make it")
    conversation_id = rig.store.conversation_by_token(token)["id"]
    control = ControlDoor(rig.store, rig.mailbox, "control-token")
    stream = a2a.A2A(rig.mailbox).follow(conversation_id, task["id"], seconds=5)
    seen = [await anext(stream)]
    assert seen[0]["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
    item = control.claim()[0]
    seen.append(await anext(stream))
    assert seen[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_WORKING"
    control.say(conversation_id, {"kind": "status", "task": task["id"], "state": "working", "text": "3 of 5 sources"})
    seen.append(await anext(stream))
    assert seen[-1]["statusUpdate"]["status"]["message"]["parts"] == [{"text": "3 of 5 sources"}]
    control.reply(item["message_id"], {"ok": True, "text": "All five.", "task_state": "done"})
    rest = [event async for event in stream]
    assert [next(iter(event)) for event in rest] == ["artifactUpdate", "statusUpdate"]
    assert rest[0]["artifactUpdate"]["artifact"]["parts"] == [{"text": "All five."}] and rest[0]["artifactUpdate"]["lastChunk"] is True
    assert rest[1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED" and rest[1]["statusUpdate"]["contextId"] == conversation_id


async def test_a_quiet_stream_says_it_is_alive_and_ends_on_its_own_bound(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a, "LOOK_SECONDS", 0.02)
    monkeypatch.setattr(a2a, "KEEPALIVE_SECONDS", 0.05)
    _, token = await asyncio.to_thread(opened, rig)
    task = await asyncio.to_thread(send, rig, token, "make it")
    conversation_id = rig.store.conversation_by_token(token)["id"]
    events = [event async for event in a2a.A2A(rig.mailbox).follow(conversation_id, task["id"], seconds=0.3)]
    assert "task" in events[0] and None in events[1:] and all(event is None for event in events[1:])


# -- the owner's side offline ----------------------------------------------------------------------------------


def test_a_guest_hears_that_the_owners_side_is_offline_and_that_the_message_waits(rig: Rig) -> None:
    rig.mailbox.host_away_seconds = mailbox_module.HOST_AWAY_SECONDS
    _, token = opened(rig)
    rig.clock.now += mailbox_module.HOST_AWAY_SECONDS + 1
    sent = rig.say(token, "anyone there?")
    assert sent.status_code == 202 and sent.json()["owner_offline"] is True and "offline" in sent.json()["detail"]
    assert sent.headers["retry-after"] == str(mailbox_module.OFFLINE_RETRY_SECONDS)
    quiet = rig.read(token).json()
    assert quiet["waiting_for"] == "owner_online" and quiet["stop_polling"] is True and "kept here" in quiet["detail"]
    task = send(rig, token, "and a task")
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED" and task["status"]["message"]["parts"][0]["text"] == mailbox_module.OWNER_OFFLINE
    # The host comes back: its first call says so, and what waited is handed out, one turn at a time as ever.
    assert [d["text"] for d in feed(rig)["deliveries"]] == ["anyone there?"]
    assert "message" not in get(rig, token, task["id"])["status"]
    assert rig.read(token).json()["waiting_for"] == "reply"


def test_a_stand_in_is_never_offline_because_the_door_answers_it_itself(rig: Rig) -> None:
    rig.mailbox.host_away_seconds = mailbox_module.HOST_AWAY_SECONDS
    _, invite = rig.link()
    token = rig.redeem(invite)
    rig.clock.now += mailbox_module.HOST_AWAY_SECONDS + 1
    assert "owner_offline" not in rig.say(token, "port?").json()


# -- the front -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("head", "streams"), [
    (b"POST /a/rpc HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\nContent-Length: 2\r\n\r\n", True),
    (b"POST /a/rpc HTTP/1.1\r\nhost: x\r\naccept: application/json, text/event-stream\r\n\r\n", True),
    (b"POST /a/rpc HTTP/1.1\r\nHost: x\r\nAccept: application/json\r\n\r\n", False),
    (b"GET /a/messages HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n", False),
    (b"POST /a/rpcx HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n", False),
    (b"POST /a/rpc HTTP/1.1\r\nHost: x\r\nX-Accept: text/event-stream\r\n\r\n", False),
])
def test_only_an_a2a_stream_is_given_a_streams_life(head: bytes, streams: bool) -> None:
    assert bool(STREAM_HEAD.match(head)) is streams


async def test_a_stream_outlives_a_plain_requests_life_and_a_plain_request_does_not(tmp_path: Path) -> None:
    async def door(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = next((int(line.split(b":")[1]) for line in head.lower().split(b"\r\n") if line.startswith(b"content-length:")), 0)
        if length:
            await reader.readexactly(length)
        await asyncio.sleep(0.4)
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\n{}")
        await writer.drain()
        writer.close()

    path = str(tmp_path / "public.sock")
    inner = await asyncio.start_unix_server(door, path)
    front = Front(path, life_seconds=0.2, stream_seconds=2.0)
    outer = await asyncio.start_server(front.handle, "127.0.0.1", 0, limit=16 * 1024)
    port = outer.sockets[0].getsockname()[1]

    async def ask(accept: bytes) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"POST /a/rpc HTTP/1.1\r\nHost: x\r\nAccept: " + accept + b"\r\nContent-Length: 2\r\n\r\n{}")
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        return answer

    try:
        assert (await ask(b"text/event-stream")).startswith(b"HTTP/1.1 200")
        assert not (await ask(b"application/json")).startswith(b"HTTP/1.1 200")
    finally:
        outer.close()
        inner.close()
