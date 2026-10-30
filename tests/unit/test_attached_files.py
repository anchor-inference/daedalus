"""The files an operator's message carried, as the app is told about them and served them.

The model reads a list in the text; the app reads a record beside it, or — for the history written
before there was one — the list read back. Names in that history are whatever the operator's machine
called them: a Windows paste is ``{GUID}.png``, a download ``photo (1).png``, a document has spaces
and Cyrillic in it.
"""

from __future__ import annotations

from typing import Any

from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import Settings
from daedalus.host import attached_files
from daedalus.host.session_runner import Attachment
from daedalus.host.transcript_view import message_view
from daedalus.stores.database import Database
from tests.unit.test_session_runner import ScriptedProvider
from tests.unit.test_steer_queue import PIXEL, H, _client, _manager

GUID = "{2C33CD0A-830B-4C8A-B4EE-59B46E35D59E}.png"


def test_a_plain_list_from_a_host_folder_is_read_back_by_inbox_name() -> None:
    text = (
        "check this out (the tester wrote it). Just check it.\n\n"
        "Attached files:\n"
        f"- /home/someone/work/inbox/{GUID} (image/png, 182143 bytes)\n"
        "- /data/workspaces/s1/inbox/Отчёт за квартал (финал).pdf (application/pdf, 5120 bytes)\n"
        "- C:\\Users\\someone\\Daedalus\\projects\\p\\inbox\\photo (1).png (image/png, 12 bytes)\n"
        "- /data/workspaces/s1/inbox/cookies.txt (text/plain, 40 bytes) — cookies for shop.test; BrowserCookies can load them into the browser"
    )
    assert attached_files.parse(text) == [
        {"name": GUID, "mime": "image/png", "size": 182143, "file": GUID},
        {"name": "Отчёт за квартал (финал).pdf", "mime": "application/pdf", "size": 5120, "file": "Отчёт за квартал (финал).pdf"},
        {"name": "photo (1).png", "mime": "image/png", "size": 12, "file": "photo (1).png"},
        {"name": "cookies.txt", "mime": "text/plain", "size": 40, "file": "cookies.txt"},
    ]


def test_an_item_broken_over_two_lines_is_joined_back() -> None:
    text = f"Attached files:\n- /home/someone/work/inbox/{GUID}\n(image/png, 182143 bytes)\n- /w/inbox/a.txt (text/plain, 3 bytes)"
    assert [entry["file"] for entry in attached_files.parse(text)] == [GUID, "a.txt"]


def test_a_kept_list_names_each_file_by_its_handle_and_skips_a_refused_one() -> None:
    text = (
        "Pass this on\n\nAttached files (kept by handle; Read one with Peek(op='read', path=…); hand them to staff with Assign or Tell (files=[…])):\n"
        "- att:0123456789ab спека free pro.md (text/markdown, 64 bytes)\n"
        "- huge.bin: not kept — larger than the store takes\n"
        "- att:fedcba987654 estimate.xlsx (application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, 2048 bytes)"
    )
    assert attached_files.parse(text) == [
        {"name": "спека free pro.md", "mime": "text/markdown", "size": 64, "kept": "0123456789ab"},
        {"name": "estimate.xlsx", "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "size": 2048, "kept": "fedcba987654"},
    ]


def test_the_list_read_is_the_hosts_own_the_last_one() -> None:
    quoted = "He wrote:\nAttached files:\n- /x/inbox/old.png (image/png, 1 bytes)\n\nAttached files:\n- /w/inbox/new.png (image/png, 2 bytes)"
    assert [entry["file"] for entry in attached_files.parse(quoted)] == ["new.png"]
    assert attached_files.parse("no files here") == []
    # A name that could step out of the inbox is never offered.
    assert attached_files.parse("Attached files:\n- /w/inbox/.. (image/png, 2 bytes)") == []


def test_the_view_prefers_the_record_and_falls_back_to_the_list() -> None:
    recorded = [attached_files.inbox_entry("shot.png", "shot-1a2b3c4d.png", "image/png", 9)]
    text = "look\n\nAttached files:\n- /w/inbox/shot-1a2b3c4d.png (image/png, 9 bytes)"
    with_record = Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": "operator", attached_files.METADATA_KEY: recorded})
    assert message_view(with_record)["attachments"] == recorded
    old = Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": "operator"})
    assert message_view(old)["attachments"] == [{"name": "shot-1a2b3c4d.png", "mime": "image/png", "size": 9, "file": "shot-1a2b3c4d.png"}]
    # The model's text is left exactly as it was.
    assert message_view(old)["text"] == text
    answer = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=text)])
    assert message_view(answer)["attachments"] == []


async def test_a_sent_file_is_recorded_on_the_message_and_served_only_from_the_inbox(settings: Settings, db: Database, tmp_path: Any) -> None:
    manager = await _manager(settings, db, ScriptedProvider([{"text": "seen"}]))
    state = await manager.create_session("files")
    sid = state.session.id
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "a").write_bytes(PIXEL)
    (staged / "b").write_bytes(b"%PDF-1.4 tiny")
    await manager.submit(sid, "two files", [
        Attachment(path=staged / "a", name=GUID, mime_type="image/png"),
        Attachment(path=staged / "b", name="Отчёт.pdf", mime_type="application/pdf"),
    ])
    await state.task
    shown = next(m for m in await manager.transcript_page(sid) if m["role"] == "user" and not m.get("internal"))
    assert shown["attachments"] == [
        {"name": GUID, "mime": "image/png", "size": len(PIXEL), "file": GUID},
        {"name": "Отчёт.pdf", "mime": "application/pdf", "size": 13, "file": "Отчёт.pdf"},
    ]
    assert f"inbox/{GUID} (image/png, {len(PIXEL)} bytes)" in shown["text"]
    (tmp_path / "secret.txt").write_text("not yours")
    (state.workspace / "inbox" / "planted.txt").symlink_to(tmp_path / "secret.txt")
    (state.workspace / "notes.txt").write_text("workspace, not inbox")
    async with _client(settings, db, manager) as client:
        url = f"/api/sessions/{sid}/attachments/download"
        served = await client.get(url, params={"path": "Отчёт.pdf"}, headers=H)
        assert served.status_code == 200 and served.content == b"%PDF-1.4 tiny"
        assert served.headers["x-content-type-options"] == "nosniff"
        assert (await client.get(url, params={"path": GUID}, headers=H)).content == PIXEL
        # A dot file sent on purpose is served; the inbox's own copy in progress is not.
        (state.workspace / "inbox" / ".env").write_text("A=1\n")
        assert (await client.get(url, params={"path": ".env"}, headers=H)).content == b"A=1\n"
        for bad, status in (("../notes.txt", 400), ("..", 400), (".a.png.0f3c.part", 400), ("sub/x", 400), ("planted.txt", 403), ("gone.png", 404)):
            answered = await client.get(url, params={"path": bad}, headers=H)
            assert answered.status_code == status, (bad, answered.status_code, answered.text)
        assert (await client.get(url, params={"path": GUID})).status_code == 401
        assert (await client.get("/api/sessions/nope/attachments/download", params={"path": GUID}, headers=H)).status_code == 404
    await manager.close()
