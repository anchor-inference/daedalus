"""Message kinds at the door: files in pieces, tasks with states, and the host's own words, as a guest's agent sees them.

The real door in process, as in ``test_guest_door``: the public port a stranger's agent reaches, and the control
socket the host dials. What a turn does with any of it on the host's side is in ``test_link_kinds_host``.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from guestd import pages
from guestd.files import CHUNK_BYTES, MB, Desk
from guestd.front import MAX_BODY, MAX_PIECE_BODY, Refused, rewrite
from guestd.store import SCHEMA_VERSION, Store, migrate

from tests.unit.test_guest_door import CONTROL, Rig, direct_link


@pytest.fixture
def rig(tmp_path: Path) -> Any:
    made = Rig(tmp_path)
    yield made
    made.store.close()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def announce(rig: Rig, token: str, data: bytes, name: str = "report.pdf", sha: str | None = None) -> Any:
    return rig.public.post("/a/files", json={"name": name, "size": len(data), "sha256": sha or hashlib.sha256(data).hexdigest()}, headers=bearer(token))


def upload(rig: Rig, token: str, data: bytes, name: str = "report.pdf") -> str:
    made = announce(rig, token, data, name)
    assert made.status_code == 201, made.text
    file_id = made.json()["id"]
    for offset in range(0, len(data), CHUNK_BYTES):
        put = rig.public.put(f"/a/files/{file_id}?offset={offset}", content=data[offset:offset + CHUNK_BYTES], headers=bearer(token))
        assert put.status_code == 200, put.text
    return file_id


def feed(rig: Rig) -> dict[str, Any]:
    return rig.control.get("/control/feed?after=0&wait=0", headers=CONTROL).json()


def read(rig: Rig, token: str, after: int = 0) -> dict[str, Any]:
    return rig.public.get(f"/a/messages?after={after}&wait=0", headers=bearer(token)).json()


def opened(rig: Rig, **link: Any) -> tuple[str, str]:
    link_id, invite = direct_link(rig, **link)
    return link_id, rig.redeem(invite)


# -- plain text, as it always was --------------------------------------------------------------------


def test_a_plain_text_message_works_as_before_and_reads_back_as_text(rig: Rig) -> None:
    _, token = opened(rig)
    sent = rig.public.post("/a/messages", json={"text": "port?"}, headers=bearer(token))
    assert sent.status_code == 202 and "task" not in sent.json()
    item = feed(rig)["deliveries"][0]
    assert (item["kind"], item["task"], item["files"], item["role"]) == ("text", "", [], "guest")
    rig.control.post(f"/control/deliveries/{item['message_id']}/reply", json={"ok": True, "text": "8080"}, headers=CONTROL)
    message = read(rig, token)["messages"][0]
    assert (message["kind"], message["text"], message["from"]) == ("text", "8080", "agent") and "files" not in message and "task" not in message


def test_an_unknown_kind_is_named_back(rig: Rig) -> None:
    _, token = opened(rig)
    answer = rig.public.post("/a/messages", json={"kind": "invoice", "text": "x"}, headers=bearer(token))
    assert answer.status_code == 400 and "text, file, task, question, status" in answer.json()["detail"]


# -- a stand-in is text only --------------------------------------------------------------------------


def test_a_stand_in_takes_no_files_and_no_tasks(rig: Rig) -> None:
    _, invite = rig.link()
    token = rig.redeem(invite)
    assert announce(rig, token, b"x").status_code == 403
    for body in ({"kind": "task", "text": "do it"}, {"kind": "file", "files": ["f_0123456789abcdef"]}, {"text": "hi", "files": ["f_0123456789abcdef"]}):
        refused = rig.public.post("/a/messages", json=body, headers=bearer(token))
        assert refused.status_code == 403 and "text messages only" in refused.json()["detail"]
    assert rig.public.post("/a/messages", json={"text": "hi"}, headers=bearer(token)).status_code == 202
    card = rig.public.get(f"/a/{invite}", headers={"accept": "application/json"})
    assert card.status_code == 410  # spent; the card of a waiting stand-in says text only:
    _, fresh = rig.link()
    assert rig.public.get(f"/a/{fresh}", headers={"accept": "application/json"}).json()["kinds"] == ["text"]


def test_the_page_teaches_files_and_tasks_only_where_they_are_taken(rig: Rig) -> None:
    _, stand_in = rig.link()
    _, direct = direct_link(rig, file_mb=10, day_mb=0)
    assert "## Files and tasks" not in rig.public.get(f"/a/{stand_in}").text
    text = rig.public.get(f"/a/{direct}").text
    for needed in ("## Files and tasks", "/a/files", "?offset=", "sha256", '"kind": "task"', '"state": "cancelled"', "X-Next-Offset", "10 MB per file", "no limit a day", str(CHUNK_BYTES)):
        assert needed in text, needed
    card = rig.public.get(f"/a/{direct}", headers={"accept": "application/json"}).json()
    assert card["kinds"] == ["text", "file", "task", "question", "status"] and card["chunk_bytes"] == CHUNK_BYTES and card["file_mb"] == 10


# -- files in pieces ----------------------------------------------------------------------------------


def test_a_file_goes_in_pieces_and_resumes_where_the_door_says_after_a_drop(rig: Rig) -> None:
    _, token = opened(rig)
    data = bytes(range(256)) * (CHUNK_BYTES // 256 * 2 + 3)  # two whole pieces and a tail
    made = announce(rig, token, data)
    assert made.status_code == 201 and made.json()["chunk_bytes"] == CHUNK_BYTES and made.json()["received"] == 0
    file_id = made.json()["id"]
    assert rig.public.put(f"/a/files/{file_id}?offset=0", content=data[:CHUNK_BYTES], headers=bearer(token)).json()["received"] == CHUNK_BYTES
    # The connection dropped on the second piece; the client lost count and sends the first again.
    again = rig.public.put(f"/a/files/{file_id}?offset=0", content=data[:CHUNK_BYTES], headers=bearer(token))
    assert again.status_code == 409 and again.json()["received"] == CHUNK_BYTES, "told where to go on, not written twice"
    assert rig.public.get(f"/a/files/{file_id}", headers=bearer(token)).json()["received"] == CHUNK_BYTES
    for offset in range(CHUNK_BYTES, len(data), CHUNK_BYTES):
        put = rig.public.put(f"/a/files/{file_id}?offset={offset}", content=data[offset:offset + CHUNK_BYTES], headers=bearer(token))
        assert put.status_code == 200
    assert put.json()["state"] == "ready" and put.json()["received"] == len(data)
    assert (rig.store.path.parent / "files" / file_id).read_bytes() == data


def test_bytes_that_do_not_match_the_sha256_are_refused_and_start_again(rig: Rig) -> None:
    _, token = opened(rig)
    made = announce(rig, token, b"the real bytes", sha=hashlib.sha256(b"other bytes!!!").hexdigest())
    file_id = made.json()["id"]
    wrong = rig.public.put(f"/a/files/{file_id}?offset=0", content=b"the real bytes", headers=bearer(token))
    assert wrong.status_code == 422 and wrong.json()["code"] == "sha256" and wrong.json()["received"] == 0
    assert rig.public.get(f"/a/files/{file_id}", headers=bearer(token)).json()["state"] == "uploading"
    refused = rig.public.post("/a/messages", json={"kind": "file", "files": [file_id]}, headers=bearer(token))
    assert refused.status_code == 409, "a file that is not whole and checked cannot be sent"


def test_a_piece_past_the_announced_size_or_larger_than_a_piece_is_refused(rig: Rig) -> None:
    _, token = opened(rig)
    file_id = announce(rig, token, b"abc").json()["id"]
    assert rig.public.put(f"/a/files/{file_id}?offset=0", content=b"abcd", headers=bearer(token)).status_code == 413
    big = announce(rig, token, b"x" * (CHUNK_BYTES + 10)).json()["id"]
    assert rig.public.put(f"/a/files/{big}?offset=0", content=b"x" * (CHUNK_BYTES + 1), headers=bearer(token)).status_code == 413


@pytest.mark.parametrize(("file_mb", "day_mb", "sizes", "status"), [
    (1, 500, [MB + 1], 413),
    (1, 500, [MB], 201),
    (0, 2, [MB, MB, 1], 429),
    (0, 0, [3 * MB, 3 * MB], 201),
])
def test_a_links_file_limits_hold_and_no_limit_means_none(rig: Rig, file_mb: float, day_mb: float, sizes: list[int], status: int) -> None:
    _, token = opened(rig, file_mb=file_mb, day_mb=day_mb)
    answers = [rig.public.post("/a/files", json={"name": "a.bin", "size": size, "sha256": "0" * 64}, headers=bearer(token)) for size in sizes]
    assert answers[-1].status_code == status, answers[-1].text
    assert all(answer.status_code == 201 for answer in answers[:-1])


def test_the_doors_disk_is_bounded_whatever_the_links_allow(rig: Rig) -> None:
    rig.mailbox.desk = Desk(rig.store, rig.mailbox.desk.bodies, disk_bytes=2 * MB)
    _, token = opened(rig, file_mb=0, day_mb=0)
    assert rig.public.post("/a/files", json={"name": "a", "size": 2 * MB, "sha256": "0" * 64}, headers=bearer(token)).status_code == 201
    full = rig.public.post("/a/files", json={"name": "b", "size": 1, "sha256": "0" * 64}, headers=bearer(token))
    assert full.status_code == 507 and full.json()["code"] == "door_full"


def test_a_guests_file_reaches_the_host_and_the_doors_copy_goes_once_it_has_it(rig: Rig) -> None:
    _, token = opened(rig)
    file_id = upload(rig, token, b"%PDF the report", name="../../etc/AGENTS.md")
    sent = rig.public.post("/a/messages", json={"kind": "file", "files": [file_id], "text": "the report"}, headers=bearer(token))
    assert sent.status_code == 202
    item = feed(rig)["deliveries"][0]
    assert item["kind"] == "file" and item["files"][0]["id"] == file_id and item["files"][0]["name"] == "AGENTS.md", "a name is the last part of whatever was sent"
    body = rig.control.get(f"/control/files/{file_id}/content?offset=0", headers=CONTROL)
    assert body.content == b"%PDF the report"
    assert rig.control.post(f"/control/files/{file_id}/fetched", headers=CONTROL).status_code == 200
    assert not (rig.store.path.parent / "files" / file_id).exists()
    assert rig.public.get(f"/a/files/{file_id}", headers=bearer(token)).json()["state"] == "gone"


def test_another_conversations_file_cannot_be_named_or_read(rig: Rig) -> None:
    _, first = opened(rig)
    _, second = opened(rig)
    file_id = upload(rig, first, b"mine")
    assert rig.public.post("/a/messages", json={"kind": "file", "files": [file_id]}, headers=bearer(second)).status_code == 404
    assert rig.public.get(f"/a/files/{file_id}/content", headers=bearer(second)).status_code == 404


def test_a_file_waits_for_the_owner_to_admit_a_first_contact(rig: Rig) -> None:
    _, invite = direct_link(rig, approval=True)
    token = rig.redeem(invite)
    assert announce(rig, token, b"x").status_code == 409
    assert rig.public.post("/a/messages", json={"kind": "task", "text": "do"}, headers=bearer(token)).status_code == 409


# -- tasks --------------------------------------------------------------------------------------------


def test_a_task_moves_accepted_working_done_with_its_result_and_a_file_the_guest_fetches_in_pieces(rig: Rig) -> None:
    _, token = opened(rig)
    set_task = rig.public.post("/a/messages", json={"kind": "task", "text": "summarise the logs"}, headers=bearer(token))
    assert set_task.status_code == 202 and set_task.json()["state"] == "accepted"
    task = set_task.json()["task"]
    assert rig.public.get(f"/a/tasks/{task}", headers=bearer(token)).json()["state"] == "accepted"
    item = feed(rig)["deliveries"][0]
    assert (item["kind"], item["task"]) == ("task", task)
    first = read(rig, token)
    assert [(m["kind"], m["task"], m["state"], m["from"]) for m in first["messages"]] == [("status", task, "working", "door")], "said by the door, with no model turn"
    assert first["stop_polling"] is False, "a task in hand is a reason to keep reading"
    data = b"r" * (CHUNK_BYTES + 5)
    conversation = item["conversation_id"]
    made = rig.control.post(f"/control/conversations/{conversation}/files", json={"name": "summary.txt", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}, headers=CONTROL).json()
    for offset in (0, CHUNK_BYTES):
        assert rig.control.put(f"/control/files/{made['id']}?offset={offset}", content=data[offset:offset + CHUNK_BYTES], headers=CONTROL).status_code == 200
    rig.control.post(f"/control/deliveries/{item['message_id']}/reply", json={"ok": True, "text": "Done: three errors.", "files": [made["id"]], "task_state": "done"}, headers=CONTROL)
    done = read(rig, token, first["cursor"])["messages"][0]
    assert (done["kind"], done["state"], done["text"]) == ("status", "done", "Done: three errors.")
    assert done["files"][0]["name"] == "summary.txt" and done["files"][0]["sha256"] == hashlib.sha256(data).hexdigest()
    got = b""
    while len(got) < len(data):
        piece = rig.public.get(f"/a/files/{made['id']}/content?offset={len(got)}", headers=bearer(token))
        assert piece.status_code == 200 and piece.headers["content-disposition"] == "attachment" and piece.headers["x-content-type-options"] == "nosniff"
        assert int(piece.headers["x-next-offset"]) == len(got) + len(piece.content)
        got += piece.content
    assert got == data
    assert rig.public.get(f"/a/tasks/{task}", headers=bearer(token)).json()["state"] == "done"
    assert rig.public.post("/a/messages", json={"text": "more?", "task": task}, headers=bearer(token)).status_code == 409, "a task that is over takes no more"


def test_a_task_that_needs_input_goes_back_to_work_on_the_guests_answer(rig: Rig) -> None:
    _, token = opened(rig)
    task = rig.public.post("/a/messages", json={"kind": "task", "text": "book a room"}, headers=bearer(token)).json()["task"]
    item = feed(rig)["deliveries"][0]
    rig.control.post(f"/control/deliveries/{item['message_id']}/reply", json={"ok": True, "text": "Which date?", "task_state": "needs_input"}, headers=CONTROL)
    shown = read(rig, token)
    assert shown["messages"][-1]["state"] == "needs_input" and shown["stop_polling"] is True
    assert rig.public.post("/a/messages", json={"text": "Friday", "task": task}, headers=bearer(token)).status_code == 202
    second = feed(rig)["deliveries"]
    assert [d["task"] for d in second] == [task]
    assert rig.store.task(task)["state"] == "working"


def test_the_guest_cancels_a_task_and_the_host_hears_so_and_its_answer_goes_nowhere(rig: Rig) -> None:
    link_id, token = opened(rig)
    task = rig.public.post("/a/messages", json={"kind": "task", "text": "long work"}, headers=bearer(token)).json()["task"]
    item = feed(rig)["deliveries"][0]
    cancel = rig.public.post("/a/messages", json={"kind": "status", "task": task, "state": "cancelled"}, headers=bearer(token))
    assert cancel.status_code == 200 and cancel.json()["state"] == "cancelled"
    events = feed(rig)["events"]
    assert any(e["kind"] == "task_cancelled" and e["detail"] == task and e["link_id"] == link_id for e in events)
    rig.control.post(f"/control/deliveries/{item['message_id']}/reply", json={"ok": True, "text": "finished anyway", "task_state": "done"}, headers=CONTROL)
    texts = [m["text"] for m in read(rig, token)["messages"]]
    assert "finished anyway" not in texts and rig.store.task(task)["state"] == "cancelled"
    assert rig.public.post("/a/messages", json={"kind": "status", "task": task, "state": "cancelled"}, headers=bearer(token)).status_code == 409


def test_a_queued_message_of_a_cancelled_task_is_never_handed_to_the_host(rig: Rig) -> None:
    _, token = opened(rig)
    rig.public.post("/a/messages", json={"text": "first"}, headers=bearer(token))
    task = rig.public.post("/a/messages", json={"kind": "task", "text": "later"}, headers=bearer(token)).json()["task"]
    rig.public.post("/a/messages", json={"kind": "status", "task": task, "state": "cancelled"}, headers=bearer(token))
    assert [d["text"] for d in feed(rig)["deliveries"]] == ["first"]


def test_a_guest_sets_status_only_to_cancel(rig: Rig) -> None:
    _, token = opened(rig)
    task = rig.public.post("/a/messages", json={"kind": "task", "text": "x"}, headers=bearer(token)).json()["task"]
    assert rig.public.post("/a/messages", json={"kind": "status", "task": task, "state": "done"}, headers=bearer(token)).status_code == 400


# -- what the host says outside a turn ----------------------------------------------------------------


def test_the_host_reports_progress_and_cancels_with_no_turn_counted(rig: Rig) -> None:
    link_id, token = opened(rig, turns_total=5)
    task = rig.public.post("/a/messages", json={"kind": "task", "text": "x"}, headers=bearer(token)).json()["task"]
    item = feed(rig)["deliveries"][0]
    used = rig.store.link(link_id)["turns_used"]
    said = rig.control.post(f"/control/conversations/{item['conversation_id']}/messages", json={"kind": "status", "task": task, "state": "working", "text": "3 of 5 sources"}, headers=CONTROL)
    assert said.status_code == 200
    assert read(rig, token)["messages"][-1]["text"] == "3 of 5 sources"
    stop = rig.control.post(f"/control/conversations/{item['conversation_id']}/messages", json={"kind": "status", "task": task, "state": "cancelled"}, headers=CONTROL)
    assert stop.status_code == 200 and rig.store.task(task)["state"] == "cancelled"
    assert rig.store.link(link_id)["turns_used"] == used, "the host's own words cost no turn"
    assert any(e["kind"] == "task_cancelled" for e in feed(rig)["events"])
    refused = rig.control.post(f"/control/conversations/{item['conversation_id']}/messages", json={"kind": "task", "text": "now you"}, headers=CONTROL)
    assert refused.status_code == 422, "no task is set on a guest through this door"


def test_a_resume_is_a_counted_turn_whose_place_the_guest_never_sees(rig: Rig) -> None:
    link_id, token = opened(rig)
    task = rig.public.post("/a/messages", json={"kind": "task", "text": "ask your owner"}, headers=bearer(token)).json()["task"]
    item = feed(rig)["deliveries"][0]
    rig.control.post(f"/control/deliveries/{item['message_id']}/reply", json={"ok": True, "text": "Checking with the owner.", "task_state": "needs_input"}, headers=CONTROL)
    resumed = rig.control.post(f"/control/conversations/{item['conversation_id']}/resume", json={"task": task, "ask": "a1b2c3"}, headers=CONTROL)
    assert resumed.status_code == 200
    again = feed(rig)["deliveries"]
    assert [(d["role"], d["task"], d["ask"], d["text"]) for d in again] == [("owner", task, "a1b2c3", "")]
    assert rig.store.link(link_id)["turns_used"] == 2
    rig.control.post(f"/control/deliveries/{again[0]['message_id']}/reply", json={"ok": True, "text": "The owner says yes.", "task_state": "done"}, headers=CONTROL)
    shown = read(rig, token)["messages"]
    assert [m["text"] for m in shown if m["kind"] == "status"][-1] == "The owner says yes."
    assert all(m["from"] in ("agent", "door") for m in shown)
    full = rig.control.get(f"/control/links/{link_id}", headers=CONTROL).json()["link"]
    assert "owner" not in {m["role"] for m in full["messages"]} and full["tasks"][0]["state"] == "done"


# -- the front, the migration ----------------------------------------------------------------------------


def test_the_front_lets_a_piece_through_and_nothing_larger(rig: Rig) -> None:
    def head(method: str, path: str, length: int) -> bytes:
        return f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {length}\r\n\r\n".encode()

    assert rewrite(head("PUT", "/a/files/f_0123456789abcdef?offset=0", MAX_PIECE_BODY), "192.0.2.1", ())[2] == MAX_PIECE_BODY
    with pytest.raises(Refused):
        rewrite(head("PUT", "/a/files/f_0123456789abcdef?offset=0", MAX_PIECE_BODY + 1), "192.0.2.1", ())
    with pytest.raises(Refused):
        rewrite(head("POST", "/a/files/f_0123456789abcdef", MAX_BODY + 1), "192.0.2.1", ())
    with pytest.raises(Refused):
        rewrite(head("PUT", "/a/messages", MAX_BODY + 1), "192.0.2.1", ())


def test_a_door_database_from_before_kinds_gains_them_once_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE links (id TEXT PRIMARY KEY, invite_hash TEXT NOT NULL UNIQUE, state TEXT NOT NULL, label TEXT NOT NULL, topic TEXT NOT NULL, mode TEXT NOT NULL,
            briefing TEXT NOT NULL, model_path TEXT NOT NULL, model_name TEXT NOT NULL, price_in REAL NOT NULL, price_out REAL NOT NULL, approval INTEGER NOT NULL,
            usd_limit REAL NOT NULL, turns_per_day INTEGER NOT NULL, turns_total INTEGER NOT NULL, invite_expires_at REAL NOT NULL, conversation_hours REAL NOT NULL,
            idle_hours REAL NOT NULL, retention_days REAL NOT NULL, guest_tools TEXT NOT NULL, created_at REAL NOT NULL, closed_at REAL, spent_usd REAL NOT NULL DEFAULT 0,
            reserved_usd REAL NOT NULL DEFAULT 0, turns_used INTEGER NOT NULL DEFAULT 0, turns_today INTEGER NOT NULL DEFAULT 0, turns_day TEXT NOT NULL DEFAULT '');
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, at REAL NOT NULL,
            state TEXT NOT NULL, reserved_usd REAL NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, acknowledged INTEGER NOT NULL DEFAULT 0);
        INSERT INTO links VALUES ('l1', 'h', 'spent', 'x', 't', 'direct', '', '', '', 0, 0, 0, 1, 40, 200, 0, 0, 0, 30, 'read', 1, NULL, 0, 0, 0, 0, '');
        INSERT INTO messages(conversation_id, role, text, at, state) VALUES ('c1', 'guest', 'hello', 1, 'answered');
    """)
    conn.close()
    store = Store(path)
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    link = store.link("l1")
    assert (link["file_mb"], link["day_mb"]) == (50, 500), "a link from before files has the defaults"
    message = store.conn.execute("SELECT kind, task_id, data FROM messages").fetchone()
    assert tuple(message) == ("text", "", "")
    migrate(store.conn)
    assert store.conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    assert store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    store.close()


def test_the_page_names_no_limit_in_words() -> None:
    assert pages.megabytes(0) == "no limit" and pages.megabytes(50) == "50 MB"


def test_a_task_whose_turn_a_restart_cut_off_is_refused_rather_than_working_for_ever(rig: Rig) -> None:
    _, token = opened(rig)
    task = rig.public.post("/a/messages", json={"kind": "task", "text": "x"}, headers=bearer(token)).json()["task"]
    feed(rig)
    assert rig.control.post("/control/deliveries/reset", headers=CONTROL).json()["reset"] == 1
    assert rig.store.task(task)["state"] == "refused"
    assert read(rig, token)["messages"][-1]["state"] == "refused"
