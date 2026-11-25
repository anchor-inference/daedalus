"""A chat's attachments as one list: what the operator sent and what the agent handed over, each with
the address that already serves its bytes."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResultBlock, ToolUseBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host import attached_files
from daedalus.host.session_attachments import listing
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

H = {"X-Daedalus-Token": "tok"}


@pytest.fixture
async def manager(settings: Settings, db: Database) -> Any:
    made = SessionManager(settings, RuntimeConfig(), db=db)
    await made.start()
    yield made
    await made.close()


@pytest.fixture
async def client(settings: Settings, db: Database, manager: SessionManager) -> Any:
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        yield c


def _user(text: str, *attachments: dict[str, Any]) -> Message:
    metadata = {"daedalus.origin": "operator", **({attached_files.METADATA_KEY: list(attachments)} if attachments else {})}
    return Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata=metadata)


def _send(call_id: str, path: str, **arguments: Any) -> Message:
    return Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id=call_id, name="SendFile", arguments_json=json.dumps({"path": path, **arguments}))])


def _result(call_id: str, content: str, *, error: bool = False) -> Message:
    return Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id=call_id, content=content, is_error=error)])


async def _seqs(manager: SessionManager, sid: str) -> list[int]:
    return [int(v["seq"]) for v in await manager.sessions.list_transcript_views(sid)]


async def test_the_list_holds_what_both_sides_attached_and_where_each_is_served(client: httpx.AsyncClient, manager: SessionManager) -> None:
    state = await manager.create_session("files")
    sid = state.session.id
    media = {"id": "11111111-1111-1111-1111-111111111111", "layout": "single", "items": [
        {"id": "22222222-2222-2222-2222-222222222222", "kind": "file", "mime_type": "application/pdf", "filename": "Quarterly report.pdf", "byte_size": 4096, "width": None, "height": None, "alt": "", "caption": "", "url": ""},
    ]}
    answer = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Here it is.")], metadata={"daedalus.media": [media]})
    await manager.sessions.append_transcript(sid, [
        _user("look at these", {"name": "shot (1).png", "mime": "image/png", "size": 1200, "file": "shot (1).png"}, {"name": "notes.md", "mime": "text/markdown", "size": 30, "kept": "0123456789ab"}),
        _send("call-a", "out/report.csv", caption="the numbers"),
        _result("call-a", "shown report.csv (2048 bytes) to the operator as a file card in the app"),
        answer,
        _user("thanks"),
    ])
    body = (await client.get(f"/api/sessions/{sid}/attachments", headers=H)).json()
    seqs = await _seqs(manager, sid)
    assert [(a["source"], a["name"], a["size"], a["mime"], a["message_seq"]) for a in body["attachments"]] == [
        ("operator", "shot (1).png", 1200, "image/png", seqs[0]),
        ("operator", "notes.md", 30, "text/markdown", seqs[0]),
        ("agent", "report.csv", 2048, "text/csv", seqs[1]),
        ("agent", "Quarterly report.pdf", 4096, "application/pdf", seqs[3]),
    ]
    urls = [a["url"] for a in body["attachments"]]
    assert urls == [
        f"/api/sessions/{sid}/attachments/download?path=shot%20%281%29.png",
        "/api/files/0123456789ab/download?path=notes.md",
        f"/api/sessions/{sid}/sent/call-a/download?path=report.csv",
        f"/api/sessions/{sid}/media/{media['id']}/download?path={media['items'][0]['id']}/Quarterly%20report.pdf",
    ]
    assert all(a["created_at"] for a in body["attachments"]) and len({a["id"] for a in body["attachments"]}) == 4


async def test_every_address_listed_is_one_the_host_already_serves(client: httpx.AsyncClient, manager: SessionManager) -> None:
    state = await manager.create_session("served")
    sid = state.session.id
    inbox = state.workspace / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "my file.txt").write_text("from the operator", encoding="utf-8")
    (state.workspace / "report.txt").write_text("from the agent", encoding="utf-8")
    await manager.sessions.append_transcript(sid, [
        _user("two files", {"name": "my file.txt", "mime": "text/plain", "size": 17, "file": "my file.txt"}),
        _send("call-1", "report.txt"),
        _result("call-1", "shown report.txt (14 bytes) to the operator as a file card in the app"),
    ])
    listed = (await client.get(f"/api/sessions/{sid}/attachments", headers=H)).json()["attachments"]
    assert len(listed) == 2
    fetched = {a["name"]: (await client.get(a["url"], headers=H)).text for a in listed}
    assert fetched == {"my file.txt": "from the operator", "report.txt": "from the agent"}


async def test_a_message_written_before_the_host_recorded_its_files_still_lists_them(client: httpx.AsyncClient, manager: SessionManager) -> None:
    state = await manager.create_session("old")
    sid = state.session.id
    text = "see this\n\nAttached files:\n- /data/workspaces/s1/inbox/Отчёт (финал).pdf (application/pdf, 5120 bytes)"
    await manager.sessions.append_transcript(sid, [_user(text)])
    [entry] = (await client.get(f"/api/sessions/{sid}/attachments", headers=H)).json()["attachments"]
    assert (entry["source"], entry["name"], entry["size"], entry["mime"]) == ("operator", "Отчёт (финал).pdf", 5120, "application/pdf")
    assert entry["url"].endswith("/attachments/download?path=%D0%9E%D1%82%D1%87%D1%91%D1%82%20%28%D1%84%D0%B8%D0%BD%D0%B0%D0%BB%29.pdf")


async def test_a_send_that_failed_or_has_no_answer_yet_attached_nothing(client: httpx.AsyncClient, manager: SessionManager) -> None:
    state = await manager.create_session("failed")
    sid = state.session.id
    await manager.sessions.append_transcript(sid, [
        _send("bad", "missing.txt"),
        _result("bad", "no such file: missing.txt", error=True),
        _send("pending", "later.txt"),
        _send("folder", "build/out", zip=True),
        _result("folder", "shown out.zip (900 bytes) — 4 files to the operator as a file card in the app"),
        _send("unsized", "a.bin"),
        _result("unsized", "sent a.bin"),
    ])
    listed = (await client.get(f"/api/sessions/{sid}/attachments", headers=H)).json()["attachments"]
    assert [(a["name"], a["size"], a["mime"]) for a in listed] == [("out.zip", 900, "application/zip"), ("a.bin", None, "application/octet-stream")]


async def test_a_reply_to_a_question_that_carried_files_lists_them_as_the_operators(client: httpx.AsyncClient, manager: SessionManager) -> None:
    state = await manager.create_session("answered")
    sid = state.session.id
    answers = [{"question": "Which one?", "selected": [], "custom": "this\n\nAttached files:\n- /w/inbox/pick.png (image/png, 77 bytes)"}]
    result = Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(
        tool_call_id="ask-1", content=json.dumps({"answers": answers, "source": "user"}), metadata={attached_files.METADATA_KEY: [attached_files.inbox_entry("pick.png", "pick.png", "image/png", 77)]},
    )])
    await manager.sessions.append_transcript(sid, [
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="ask-1", name="AskUser", arguments_json="{}")]),
        result,
    ])
    [entry] = (await client.get(f"/api/sessions/{sid}/attachments", headers=H)).json()["attachments"]
    assert (entry["source"], entry["name"], entry["id"]) == ("operator", "pick.png", "answer:ask-1:0")


async def test_a_chat_with_nothing_attached_and_a_session_that_is_not_there(client: httpx.AsyncClient, manager: SessionManager) -> None:
    state = await manager.create_session("empty")
    await manager.sessions.append_transcript(state.session.id, [_user("just words"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="I will SendFile it later.")])])
    assert (await client.get(f"/api/sessions/{state.session.id}/attachments", headers=H)).json() == {"attachments": []}
    assert (await client.get("/api/sessions/nope/attachments", headers=H)).status_code == 404
    assert (await client.get(f"/api/sessions/{state.session.id}/attachments")).status_code == 401


def test_the_listing_skips_what_it_cannot_name() -> None:
    views = [{"seq": 4, "role": "user", "created_at": "t", "attachments": [{"name": ""}, "junk", {"name": "ok.txt", "size": True}]}]
    [entry] = listing("s1", views, [])
    assert (entry["name"], entry["size"]) == ("ok.txt", None)  # a boolean is not a size
