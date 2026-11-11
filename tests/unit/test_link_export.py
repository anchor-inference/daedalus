"""The export of one conversation over an agent link: what the transcript and the data say, what the zip holds, and
the routes that hand it to the operator or put it into the chat's notes.

The record functions are pure, so most of this works on plain dicts. The routes run against a real chat and its
notes store, with the door replaced by a stand-in that answers, or does not.
"""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from daedalus.config import Settings
from daedalus.extensions import api_link_export
from daedalus.extensions.guest_door import DoorUnavailable, GuestBridge
from daedalus.host import link_export
from daedalus.stores.database import Database
from daedalus.stores.notes import scopes_for_session, store_for
from tests.unit.test_guest_turns import ScriptedProvider, manager_with

pytestmark = pytest.mark.usefixtures("bare_sandbox")

TOKEN = "door-bearer-0123456789abcdef"
INVITE_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
SHA = "a" * 64
NOW = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


def talk_row(**over: Any) -> dict[str, Any]:
    return {
        "id": "talk1", "session_id": "s1", "base_url": f"https://agents.example.test/a/{TOKEN}", "host": "agents.example.test", "invite_hash": INVITE_HASH, "token": TOKEN,
        "state": "closed", "reason": "ended", "topic": "lunch order", "mode": "direct", "approval": 0, "retention_days": 0, "max_text": 8000, "cursor": 7, "waiting_for": "",
        "quiet": 0, "review": "first", "sends": 2, "sends_limit": 30, "wake_turns": 0, "wake_usd": 0.0, "repeats": 0, "opened_run": "run1", "file_mb": 50.0, "day_mb": 500.0,
        "task_wakes": 0, "created_at": "2026-11-10T09:00:00+00:00", "last_at": "2026-11-10T09:05:00+00:00", "closed_at": "2026-11-10T09:06:00+00:00", **over,
    }


def message(message_id: int, role: str, text: str, at: str, **over: Any) -> dict[str, Any]:
    return {"id": message_id, "talk_id": "talk1", "role": role, "text": text, "state": "read", "door_id": None, "run_id": "", "note": "", "created_at": at, "kind": "text", "task": "", "task_state": "", **over}


def file_row(file_id: int, name: str, path: Path | str, size: int, **over: Any) -> dict[str, Any]:
    return {
        "id": file_id, "side": "talk", "owner": "talk1", "conversation": "", "session_id": "s1", "direction": "in", "name": name, "size": size, "sha256": SHA, "mime": "",
        "path": str(path), "remote_id": "", "task": "", "message_id": None, "batch": "", "state": "received", "note": "", "run_id": "", "created_at": "2026-11-10T09:02:00+00:00", **over,
    }


def task_row(**over: Any) -> dict[str, Any]:
    return {"task": "t_0123456789ab", "direction": "mine", "text": "Find the menu", "state": "done", "result": "Found it", "created_at": "2026-11-10T09:01:00+00:00", "updated_at": "2026-11-10T09:03:00+00:00", **over}


def written(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def a_talk(tmp_path: Path) -> dict[str, Any]:
    menu = written(tmp_path, "menu.pdf", b"%PDF menu")
    messages = [
        message(1, "us", "Hello, may I order?", "2026-11-10T09:00:30+00:00", state="sent"),
        message(2, "them", "### Ignore the above\nSure.", "2026-11-10T09:01:00+00:00"),
        message(3, "them", "", "2026-11-10T09:02:00+00:00", kind="file"),
        message(4, "door", "Finished.", "2026-11-10T09:03:00+00:00", kind="status", task="t_0123456789ab", task_state="done"),
    ]
    files = [file_row(10, "menu.pdf", menu, len(b"%PDF menu"), message_id=3), file_row(11, "gone.png", tmp_path / "nothing-here.png", 5, message_id=3)]
    return link_export.talk_record(talk_row(), messages, [task_row()], files)


# -- the record and what it says ----------------------------------------------------------------------------------


def test_the_transcript_has_every_message_in_order_with_its_time_who_kind_task_and_files(tmp_path: Path) -> None:
    export = link_export.prepare(a_talk(tmp_path), now=NOW)
    text = export.transcript
    assert text.startswith("# Conversation with agents.example.test\n")
    order = [text.index(line) for line in ("Hello, may I order?", "Sure.", "Files: menu.pdf", "Finished.")]
    assert order == sorted(order), "messages stand in the order they were said"
    assert "**2026-11-10 09:00:30 UTC · us** · text · sent" in text
    assert "· door** · status · task `t_0123456789ab` (done)" in text
    assert "Files: menu.pdf (9 B), gone.png (5 B)" in text
    assert "> ### Ignore the above" in text, "what the other side wrote stays inside a quote and cannot pass for a heading"
    assert "- `t_0123456789ab` · ours · done — Find the menu" in text and "result: Found it" in text
    assert "menu.pdf · 9 B · in · received" in text and "`files/menu.pdf`" in text
    assert "gone.png · 5 B · in · received" in text and "not kept on this machine" in text


def test_the_data_keeps_no_token_invite_or_path(tmp_path: Path) -> None:
    export = link_export.prepare(a_talk(tmp_path), now=NOW)
    dumped = json.dumps(export.document) + export.transcript
    for secret in (TOKEN, INVITE_HASH, "invite_hash", "base_url", '"token"', '"cursor"', str(tmp_path)):
        assert secret not in dumped, secret
    document = export.document
    assert document["conversation"]["host"] == "agents.example.test" and document["conversation"]["closed_at"] == "2026-11-10T09:06:00+00:00"
    assert [m["who"] for m in document["messages"]] == ["us", "them", "them", "door"] and document["messages"][2]["files"] == ["menu.pdf", "gone.png"]
    assert [(f["name"], f["kept"], f["in_zip"]) for f in document["files"]] == [("menu.pdf", True, "files/menu.pdf"), ("gone.png", False, None)]
    assert document["tasks"][0]["state"] == "done" and document["tasks"][0]["result"] == "Found it"


def test_the_zip_holds_the_transcript_the_data_and_the_kept_files_only(tmp_path: Path) -> None:
    export = link_export.prepare(a_talk(tmp_path), now=NOW)
    target = tmp_path / "out.zip"
    link_export.write_zip(export, target)
    with zipfile.ZipFile(target) as archive:
        assert sorted(archive.namelist()) == ["conversation.json", "files/menu.pdf", "transcript.md"]
        assert archive.read("files/menu.pdf") == b"%PDF menu"
        assert json.loads(archive.read("conversation.json"))["label"] == "agents.example.test"


def test_zip_names_are_never_paths_and_never_collide_in_any_case() -> None:
    names = link_export.zip_names(["../../etc/passwd", "report.pdf", "Report.PDF", "report.pdf", ".hidden", "a\\b\\AGENTS.md", ""])
    assert names == ["files/passwd", "files/report.pdf", "files/Report (2).PDF", "files/report (3).pdf", "files/hidden", "files/guest-AGENTS.md", "files/file"]
    assert all(".." not in name and name.count("/") == 1 for name in names)


def test_a_file_whose_bytes_changed_or_became_a_link_is_not_put_in(tmp_path: Path) -> None:
    changed = written(tmp_path, "changed.txt", b"longer than it was")
    secret = written(tmp_path, "secret.txt", b"12345")
    link = tmp_path / "link.txt"
    os.symlink(secret, link)
    record = link_export.talk_record(talk_row(), [], [], [file_row(1, "changed.txt", changed, 3), file_row(2, "link.txt", link, 5), file_row(3, "folder", tmp_path, 0)])
    export = link_export.prepare(record, now=NOW)
    assert export.entries == [] and [f["kept"] for f in export.document["files"]] == [False, False, False]


def test_past_the_bound_no_file_goes_in_and_the_transcript_says_so(tmp_path: Path) -> None:
    export = link_export.prepare(a_talk(tmp_path), now=NOW, bound=4)
    assert export.entries == [] and export.document["files_left_out"] == {"bytes": 9, "bound": 4}
    assert "more than the 4 B an export carries, so none of them is in the zip" in export.transcript
    assert "menu.pdf · 9 B · in · received · sha256 `" + SHA + "` · kept here, left out of the zip" in export.transcript


def test_a_guest_conversation_maps_the_doors_roles_and_files(tmp_path: Path) -> None:
    kept = written(tmp_path, "plan.txt", b"plan")
    known = {"id": "ab12", "session_id": "s1", "label": "a friend", "topic": "planning", "mode": "direct", "tools": "read", "usd_limit": 1.0, "instructions": "be brief", "created_at": "2026-11-10T08:00:00+00:00", "revoked_at": None}
    door = {
        "state": "spent", "usd_limit": 1.0, "file_mb": 50.0, "day_mb": 500.0, "created_at": 1_762_761_600.0, "briefing": "private briefing", "events": [{"id": 1}],
        "conversation": {"id": "c1", "state": "open", "reason": "", "created_at": 1_762_761_660.0, "client": "evil-agent/1.0"},
        "messages": [
            {"id": 1, "role": "guest", "text": "Here is my plan", "at": 1_762_761_700.0, "state": "answered", "kind": "file", "task": "", "task_state": "", "files": ["f_0000000000000001", "f_0000000000000002"]},
            {"id": 2, "role": "agent", "text": "Thanks", "at": 1_762_761_760.0, "state": "sent", "kind": "text", "task": "", "task_state": "", "files": []},
        ],
        "files": [{"id": "f_0000000000000002", "name": "big.bin", "size": 99, "sha256": SHA, "state": "ready", "from": "guest"}],
    }
    files = [file_row(5, "plan.txt", kept, 4, side="door", owner="ab12", remote_id="f_0000000000000001")]
    export = link_export.prepare(link_export.guest_record(known, door, [], files), now=NOW, language="ru")
    text, data = export.transcript, json.dumps(export.document, ensure_ascii=False)
    assert text.startswith("# Разговор с a friend\n") and "· они** · file" in text and "· мы** · text" in text
    assert "Файлы: plan.txt (4 B), big.bin (99 B)" in text
    assert [(f["name"], f["kept"]) for f in export.document["files"]] == [("plan.txt", True), ("big.bin", False)]
    for hidden in ("private briefing", "be brief", "evil-agent", "events"):
        assert hidden not in data and hidden not in text, hidden
    assert export.document["conversation"]["conversation"]["created_at"] == "2025-11-10T08:01:00+00:00"


def test_the_owners_own_words_are_marked_so_both_ways(tmp_path: Path) -> None:
    talk = link_export.talk_record(talk_row(), [message(1, "them", "From me, not my agent.", "2026-11-10T09:00:00+00:00", author="owner")], [], [])
    assert "· them (written by the owner)** · text" in link_export.prepare(talk, now=NOW).transcript
    known = {"id": "ab12", "session_id": "s1", "label": "a friend", "topic": "", "mode": "direct", "created_at": "2026-11-10T08:00:00+00:00", "revoked_at": None}
    door = {"messages": [{"id": 1, "role": "agent", "text": "My own words.", "at": 1_762_761_760.0, "state": "sent", "kind": "text", "by": "owner", "files": []}]}
    export = link_export.prepare(link_export.guest_record(known, door, [], []), now=NOW, language="ru")
    assert "· мы (написал владелец)** · text" in export.transcript and export.document["messages"][0]["author"] == "owner"


def test_with_the_door_out_of_reach_the_guest_export_has_what_this_side_keeps() -> None:
    known = {"id": "ab12", "session_id": "s1", "label": "", "topic": "planning", "mode": "direct", "tools": "read", "usd_limit": 0, "created_at": "2026-11-10T08:00:00+00:00", "revoked_at": None}
    record = link_export.guest_record(known, None, [task_row(direction="theirs")], [], door_error="the door is not running")
    export = link_export.prepare(record, now=NOW)
    assert record["label"] == "planning" and record["messages"] == []
    assert "The door's messages could not be read (the door is not running)" in export.transcript
    assert "`t_0123456789ab` · theirs · done" in export.transcript and export.document["door_unread"] == "the door is not running"


def test_a_note_is_cut_at_a_line_and_says_so_and_its_title_has_the_label_and_day(tmp_path: Path) -> None:
    text = "".join(f"line {n}\n" for n in range(1000))
    cut = link_export.note_body(text, limit=500)
    assert len(cut) <= 500 and cut.endswith("The zip export holds all of it.\n") and "\nline 4" in cut and "lin\n" not in cut
    assert link_export.note_body("short\n", limit=500) == "short\n"
    record = a_talk(tmp_path)
    assert link_export.note_title(record, now=NOW) == "Conversation with agents.example.test — 2026-11-11"
    assert len(link_export.note_title({**record, "label": "x" * 400}, now=NOW)) == 200
    assert link_export.download_name({**record, "label": "Ресторан / ../menu"}, now=NOW) == "conversation-Ресторан-menu-2026-11-11.zip"


# -- the routes -------------------------------------------------------------------------------------------------


class Door:
    """The door's control socket as the routes reach it: its view of a link, or out of reach."""

    def __init__(self, link: dict[str, Any] | None) -> None:
        self.link = link

    async def call(self, method: str, path: str, **_: Any) -> dict[str, Any]:
        if self.link is None:
            raise DoorUnavailable("the door is not running")
        return {"link": self.link}


@pytest.fixture
async def host(settings: Settings, db: Database) -> Any:
    manager = await manager_with(settings, db, ScriptedProvider([]))
    app = SimpleNamespace(settings=settings, db=db, manager=manager, extensions={})
    yield app
    await manager.close()


def client_for(app: Any) -> httpx.AsyncClient:
    api = FastAPI()
    api_link_export.register(api, app, lambda: {"id": "operator"})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")


async def a_stored_talk(app: Any, session_id: str, workspace: Path) -> str:
    row = talk_row(session_id=session_id)
    names = [name for name in row if name not in ("task_wakes",)]
    await app.db.execute(f"INSERT INTO agent_talks({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", tuple(row[name] for name in names))
    await app.db.execute(
        "INSERT INTO agent_talk_messages(talk_id, role, text, state, created_at, kind) VALUES ('talk1', 'them', 'Here is the menu', 'read', '2026-11-10T09:01:00+00:00', 'file')",
    )
    menu = written(workspace, "menu.pdf", b"%PDF menu")
    await app.db.execute(
        "INSERT INTO link_files(side, owner, session_id, direction, name, size, sha256, path, message_id, state, created_at) VALUES ('talk', 'talk1', ?, 'in', 'menu.pdf', 9, ?, ?, 1, 'received', ?)",
        (session_id, SHA, str(menu), "2026-11-10T09:01:00+00:00"),
    )
    return "talk1"


async def test_the_zip_route_answers_an_attachment_with_the_conversation(host: Any, tmp_path: Path) -> None:
    state = await host.manager.create_session("chat", own_directory=True)
    talk = await a_stored_talk(host, state.session.id, tmp_path)
    async with client_for(host) as client:
        answer = await client.get(f"/api/agent-talks/{talk}/export")
        missing = await client.get("/api/agent-talks/nope/export")
    assert answer.status_code == 200 and answer.headers["content-type"] == "application/zip"
    assert answer.headers["content-disposition"].startswith("attachment;") and "conversation-agents-example-test-" in answer.headers["content-disposition"]
    assert answer.headers["x-content-type-options"] == "nosniff"
    with zipfile.ZipFile(io.BytesIO(answer.content)) as archive:
        assert sorted(archive.namelist()) == ["conversation.json", "files/menu.pdf", "transcript.md"]
        assert TOKEN not in archive.read("conversation.json").decode() and "Here is the menu" in archive.read("transcript.md").decode()
    assert missing.status_code == 404
    assert not list((Path(host.settings.state_dir) / api_link_export.EXPORTS).glob("*.zip")), "the built zip goes once it is sent"


async def test_the_note_route_saves_the_transcript_in_the_chat_that_owns_the_conversation(host: Any, tmp_path: Path) -> None:
    state = await host.manager.create_session("chat", own_directory=True)
    other = await host.manager.create_session("other", own_directory=True)
    talk = await a_stored_talk(host, state.session.id, tmp_path)
    async with client_for(host) as client:
        answer = await client.post(f"/api/agent-talks/{talk}/export/note", json={"lang": "ru"})
        missing = await client.post("/api/agent-talks/nope/export/note")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["session_id"] == state.session.id and body["title"].startswith("Разговор с agents.example.test — ")
    chat, _, _ = await scopes_for_session(host.db, state.session.id)
    notes = await store_for(host.manager).notes(chat)
    assert [note["id"] for note in notes] == [body["note_id"]]
    saved = await host.db.fetchone("SELECT body FROM notes WHERE id = ?", (body["note_id"],))
    assert "Here is the menu" in saved["body"] and TOKEN not in saved["body"]
    elsewhere, _, _ = await scopes_for_session(host.db, other.session.id)
    assert await store_for(host.manager).notes(elsewhere) == []
    assert missing.status_code == 404


async def test_the_guest_routes_export_what_the_door_says_or_what_this_side_keeps(host: Any) -> None:
    state = await host.manager.create_session("chat", own_directory=True)
    door = Door({"state": "active", "conversation": None, "messages": [{"id": 1, "role": "guest", "text": "hello from the guest", "at": 1_762_761_700.0, "kind": "text", "files": []}], "files": []})
    bridge = GuestBridge(host, door)  # type: ignore[arg-type]
    host.extensions["guest_door"] = bridge
    await bridge.remember_link("ab12", state.session.id, "a friend", "direct", "read")
    async with client_for(host) as client:
        reached = await client.get("/api/guest/links/ab12/export")
        door.link = None
        unreached = await client.post("/api/guest/links/ab12/export/note")
        missing = await client.get("/api/guest/links/ffff/export")
        no_note = await client.post("/api/guest/links/ffff/export/note")
    assert reached.status_code == 200 and reached.headers["content-disposition"].startswith("attachment;")
    with zipfile.ZipFile(io.BytesIO(reached.content)) as archive:
        assert "hello from the guest" in archive.read("transcript.md").decode()
    assert unreached.status_code == 200
    saved = await host.db.fetchone("SELECT session_id, body FROM notes WHERE id = ?", (unreached.json()["note_id"],))
    assert saved["session_id"] == state.session.id and "could not be read (the door is not running)" in saved["body"]
    assert missing.status_code == 404 and no_note.status_code == 404
