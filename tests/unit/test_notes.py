"""Notes on a chat and on a project: the store's versions and files, the routes the Notes tab calls,
the cascade that takes notes with their chat or project, and the agent's tools."""

from __future__ import annotations

import asyncio
import json
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.project_removal import remove_project
from daedalus.host.events import EventFilter
from daedalus.host.session_runner import SessionManager
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.database import Database
from daedalus.stores.notes import (
    TENANT,
    Conflict,
    NoteError,
    NoteStore,
    NotFound,
    Referenced,
    Scope,
    reference,
    references,
    scopes_for_session,
    store_for,
)
from daedalus.tools import notes as note_tools
from tests.unit.test_projects import HEADERS, _client

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 640, 480) + b"\x08\x02\x00\x00\x00" + b"rest-of-picture"


class Published:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def __call__(self, name: str, payload: dict[str, Any], **ids: Any) -> None:
        self.events.append((name, payload, ids))

    def changes(self) -> list[str]:
        return [payload["change"] for _, payload, _ in self.events]


async def _chat(db: Database, sid: str, *, project: str = "p", ephemeral: bool = False) -> Scope:
    if not await db.fetchone("SELECT 1 FROM projects WHERE id = ?", (project,)):
        await db.execute(
            "INSERT INTO projects(id, name, created_at, settings, system) VALUES (?, ?, 'now', ?, '')",
            (project, project.upper(), json.dumps({"ephemeral": True} if ephemeral else {})),
        )
    await db.execute(
        "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id, kind) VALUES (?, 't', ?, 'now', 'now', '{}', ?, 'chat')",
        (sid, sid, project),
    )
    return Scope("chat", sid)


@pytest.fixture
def published() -> Published:
    return Published()


@pytest.fixture
def store(db: Database, tmp_path: Path, published: Published) -> NoteStore:
    return NoteStore(db, FileBlobStore(tmp_path / "blobs"), publish=published)


# -- the reference form --------------------------------------------------------------------------


def test_a_reference_is_plain_markdown_that_survives_any_file_name() -> None:
    picture = {"id": "abcdefghij12", "name": "shot [1] (final).png", "mime": "image/png"}
    document = {"id": "zyxwvu987654", "name": "отчёт за май.pdf", "mime": "application/pdf"}
    assert reference(picture) == r"![shot \[1\] (final).png](files/abcdefghij12/shot%20%5B1%5D%20%28final%29.png)"
    # Letters of any script stay readable in the path; only what could end or bend the link is encoded.
    assert reference(document) == "[отчёт за май.pdf](files/zyxwvu987654/отчёт%20за%20май.pdf)"
    body = f"See {reference(picture)} and {reference(document)}, again {reference(picture)}; not [x](files/short/x) nor files/abcdefghij99/y"
    # The id is the identity: the name segment may be anything, even missing.
    assert references(body) == ["abcdefghij12", "zyxwvu987654"]
    assert references("[a](files/abcdefghij12) ![b](<files/zyxwvu987654/b c.png>)") == ["abcdefghij12", "zyxwvu987654"]


# -- the store -----------------------------------------------------------------------------------


async def test_new_notes_go_on_top_and_pinned_ones_above_them(db: Database, store: NoteStore) -> None:
    chat = await _chat(db, "s1")
    first = await store.create(chat, title="First")
    second = await store.create(chat, title="Second")
    pinned = await store.create(chat, title="Pinned", pinned=True)
    assert [n["title"] for n in await store.notes(chat)] == ["Pinned", "Second", "First"]
    assert first["version"] == second["version"] == pinned["version"] == 1
    assert first["updated_by"] == "operator" and first["scope"] == "chat" and first["scope_id"] == "s1"


async def test_the_words_carry_a_version_and_a_stale_one_is_refused_with_the_current_note(db: Database, store: NoteStore, published: Published) -> None:
    chat = await _chat(db, "s1")
    note = await store.create(chat, title="Plan", body="- [ ] buy flour")
    saved = await store.update(note["id"], version=1, body="- [x] buy flour")
    assert saved["version"] == 2 and saved["body"] == "- [x] buy flour"
    with pytest.raises(Conflict) as refused:
        await store.update(note["id"], version=1, body="mine")
    assert refused.value.current["version"] == 2 and refused.value.current["body"] == "- [x] buy flour"
    # The operator chose to keep theirs over the newer one.
    kept = await store.update(note["id"], version=1, body="mine", force=True)
    assert kept["version"] == 3 and kept["body"] == "mine"
    # The same words again are no change at all.
    assert (await store.update(note["id"], version=3, body="mine"))["version"] == 3
    # Pin and colour are not words: the version and the edit time stay, so an open editor is not refused.
    meta = await store.set_meta(note["id"], pinned=True, color="teal")
    assert meta["version"] == 3 and meta["pinned"] and meta["color"] == "teal" and meta["updated_at"] == kept["updated_at"]
    with pytest.raises(NoteError):
        await store.set_meta(note["id"], color="chartreuse")
    assert published.changes() == ["created", "updated", "updated", "meta"]
    name, payload, ids = published.events[1]
    assert name == "notes.changed" and payload == {"scope": "chat", "scope_id": "s1", "change": "updated", "by": "operator", "note_id": note["id"], "version": 2}
    assert ids == {"session_id": "s1", "project_id": None}


async def test_a_deleted_note_comes_back_and_is_purged_a_day_later(db: Database, store: NoteStore) -> None:
    chat = await _chat(db, "s1")
    note = await store.create(chat, title="Gone soon")
    await store.delete(note["id"])
    assert await store.notes(chat) == []
    with pytest.raises(NotFound):
        await store.get(note["id"])
    assert (await store.restore(note["id"]))["title"] == "Gone soon"
    await store.delete(note["id"])
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    await db.execute("UPDATE notes SET deleted_at = ? WHERE id = ?", (old, note["id"]))
    await store.create(chat, title="Another")
    assert await db.fetchone("SELECT 1 FROM notes WHERE id = ?", (note["id"],)) is None


async def test_the_order_keeps_a_note_the_reordering_window_had_not_seen(db: Database, store: NoteStore) -> None:
    chat = await _chat(db, "s1")
    a, b, c = [await store.create(chat, title=t) for t in "abc"]
    reordered = await store.reorder(chat, [a["id"], b["id"], "unknown"])
    assert [n["title"] for n in reordered] == ["a", "b", "c"]


async def test_files_are_shared_by_digest_and_one_a_note_shows_is_not_removed_unasked(db: Database, store: NoteStore, tmp_path: Path) -> None:
    chat = await _chat(db, "s1")
    picture = await store.add_file(chat, PNG, name="../../etc/shot.png", mime="image/png")
    twin = await store.add_file(chat, PNG, name="copy.png")
    assert picture["name"] == "shot.png" and (picture["width"], picture["height"]) == (640, 480)
    assert twin["mime"] == "image/png" and twin["id"] != picture["id"]
    blob = store.file_path(await store.file(picture["id"]))
    assert blob.read_bytes() == PNG and blob.parent.name == TENANT
    note = await store.create(chat, title="Shot", body=f"Look: {reference(picture)}")
    with pytest.raises(Referenced) as refused:
        await store.delete_file(picture["id"])
    assert refused.value.notes == [{"id": note["id"], "title": "Shot", "scope": "chat"}]
    await store.delete_file(picture["id"], force=True)
    # The twin still names the same bytes.
    assert blob.exists()
    await store.delete_file(twin["id"])
    assert not blob.exists()
    with pytest.raises(NoteError):
        await store.add_file(chat, b"", name="empty.txt")


async def test_a_note_moved_up_to_its_project_takes_the_chat_files_it_shows(db: Database, store: NoteStore) -> None:
    chat = await _chat(db, "s1")
    project = Scope("project", "p")
    shown = await store.add_file(chat, PNG, name="a.png")
    other = await store.add_file(chat, b"left", name="b.txt")
    note = await store.create(chat, title="Up", body=reference(shown))
    moved = await store.move(note["id"], project)
    assert moved["scope"] == "project" and moved["scope_id"] == "p"
    assert [f["id"] for f in await store.files(project)] == [shown["id"]]
    assert [f["id"] for f in await store.files(chat)] == [other["id"]]


async def test_a_plain_chat_has_no_project_scope(db: Database) -> None:
    await _chat(db, "plain", project="scratch", ephemeral=True)
    await _chat(db, "member", project="firm")
    chat, project, about = await scopes_for_session(db, "plain")
    assert chat == Scope("chat", "plain") and project is None and about is None
    chat, project, about = await scopes_for_session(db, "member")
    assert project == Scope("project", "firm") and about == {"id": "firm", "name": "FIRM"}
    with pytest.raises(NotFound):
        await scopes_for_session(db, "nobody")


async def test_notes_and_files_go_with_their_chat_and_the_bytes_are_pruned(db: Database, store: NoteStore) -> None:
    chat = await _chat(db, "s1")
    await store.create(chat, title="Chat note")
    kept = await store.create(Scope("project", "p"), title="Project note")
    file = await store.add_file(chat, PNG, name="a.png")
    blob = store.file_path(await store.file(file["id"]))
    await db.execute("DELETE FROM sessions WHERE id = 's1'")
    assert await db.fetchone("SELECT 1 FROM notes WHERE session_id = 's1'") is None
    assert await db.fetchone("SELECT 1 FROM note_files WHERE session_id = 's1'") is None
    assert (await store.get(kept["id"]))["title"] == "Project note"
    assert await store.prune_blobs() == 1 and not blob.exists()


async def test_a_note_belongs_to_exactly_one_owner(db: Database) -> None:
    await _chat(db, "s1")
    with pytest.raises(Exception, match="CHECK"):
        await db.execute("INSERT INTO notes(id, session_id, project_id, created_at, updated_at) VALUES ('x', 's1', 'p', 'now', 'now')")


# -- the routes ----------------------------------------------------------------------------------


@pytest.fixture
async def manager(settings: Settings, config: RuntimeConfig, db: Database) -> Any:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    yield manager
    await manager.close()


async def test_the_tab_lists_both_scopes_and_edits_with_versions(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager, tmp_path: Path) -> None:
    root = tmp_path / "firm"
    root.mkdir()
    async with await _client(settings, config, db, manager) as client:
        assert (await client.get("/api/sessions/x/notes")).status_code == 401
        project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Firm", "folders": [{"path": str(root)}]})).json()
        member = (await manager.create_session("Member", project_id=project["id"])).session.id
        plain = (await manager.create_session("Plain")).session.id

        listing = (await client.get(f"/api/sessions/{plain}/notes", headers=HEADERS)).json()
        assert listing["project"] is None and listing["project_scope"] is None and listing["chat"] == {"notes": [], "files": []}
        assert (await client.post(f"/api/sessions/{plain}/notes", headers=HEADERS, json={"scope": "project", "title": "x"})).status_code == 400
        assert (await client.get("/api/sessions/nobody/notes", headers=HEADERS)).status_code == 404

        made = await client.post(f"/api/sessions/{member}/notes", headers=HEADERS, json={"scope": "project", "title": "Brief", "body": "# Goal"})
        assert made.status_code == 201, made.text
        note = made.json()
        chat_note = (await client.post(f"/api/sessions/{member}/notes", headers=HEADERS, json={"title": "Mine"})).json()
        listing = (await client.get(f"/api/sessions/{member}/notes", headers=HEADERS)).json()
        assert listing["project"] == {"id": project["id"], "name": "Firm"}
        assert [n["id"] for n in listing["project_scope"]["notes"]] == [note["id"]]
        assert [n["id"] for n in listing["chat"]["notes"]] == [chat_note["id"]]

        assert (await client.patch(f"/api/notes/{note['id']}", headers=HEADERS, json={"body": "no version"})).status_code == 400
        saved = await client.patch(f"/api/notes/{note['id']}", headers=HEADERS, json={"body": "# Goal\n\nShip", "version": 1})
        assert saved.status_code == 200 and saved.json()["version"] == 2
        stale = await client.patch(f"/api/notes/{note['id']}", headers=HEADERS, json={"body": "other window", "version": 1})
        assert stale.status_code == 409
        assert stale.json()["detail"]["note"]["body"] == "# Goal\n\nShip" and stale.json()["detail"]["note"]["version"] == 2
        forced = await client.patch(f"/api/notes/{note['id']}", headers=HEADERS, json={"body": "other window", "version": 1, "force": True})
        assert forced.json()["version"] == 3
        pinned = await client.patch(f"/api/notes/{note['id']}", headers=HEADERS, json={"pinned": True, "color": "blue"})
        assert pinned.json()["pinned"] is True and pinned.json()["version"] == 3

        copy = (await client.post(f"/api/notes/{note['id']}/duplicate", headers=HEADERS, json={"title": "Brief (copy)"})).json()
        assert copy["body"] == "other window" and copy["scope"] == "project"
        moved = (await client.post(f"/api/notes/{copy['id']}/move", headers=HEADERS, json={"session_id": member, "scope": "chat"})).json()
        assert moved["scope"] == "chat" and moved["scope_id"] == member
        order = await client.post(f"/api/sessions/{member}/notes/order", headers=HEADERS, json={"scope": "chat", "ids": [chat_note["id"], copy["id"]]})
        assert [n["id"] for n in order.json()["notes"]] == [chat_note["id"], copy["id"]]

        assert (await client.delete(f"/api/notes/{chat_note['id']}", headers=HEADERS)).status_code == 200
        assert (await client.get(f"/api/notes/{chat_note['id']}", headers=HEADERS)).status_code == 404
        assert (await client.post(f"/api/notes/{chat_note['id']}/restore", headers=HEADERS)).json()["title"] == "Mine"


async def test_files_upload_serve_and_warn_before_a_shown_one_is_removed(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> None:
    chat = (await manager.create_session("Plain")).session.id
    async with await _client(settings, config, db, manager) as client:
        sent = await client.post(
            f"/api/sessions/{chat}/notes/files", headers=HEADERS, data={"scope": "chat"},
            files=[("files", ("shot.png", PNG, "image/png")), ("files", ("page.html", b"<script>alert(1)</script>", "text/html"))],
        )
        assert sent.status_code == 201, sent.text
        picture, page = sent.json()["files"]
        assert picture["width"] == 640 and page["mime"] == "text/html"

        served = await client.get(f"/api/notes/files/{picture['id']}/download", params={"path": "shot.png"}, headers=HEADERS)
        assert served.status_code == 200 and served.content == PNG
        assert served.headers["content-disposition"].startswith("inline") and served.headers["x-content-type-options"] == "nosniff"
        # Markup the agent may have attached is never shown in place.
        markup = await client.get(f"/api/notes/files/{page['id']}/download", headers=HEADERS)
        assert markup.headers["content-disposition"].startswith("attachment")
        assert (await client.get("/api/notes/files/nothing/download", headers=HEADERS)).status_code == 404

        note = (await client.post(f"/api/sessions/{chat}/notes", headers=HEADERS, json={"title": "Shot", "body": reference(picture)})).json()
        assert (await client.get(f"/api/notes/files/{picture['id']}/references", headers=HEADERS)).json()["notes"] == [{"id": note["id"], "title": "Shot", "scope": "chat"}]
        refused = await client.delete(f"/api/notes/files/{picture['id']}", headers=HEADERS)
        assert refused.status_code == 409 and refused.json()["detail"]["notes"][0]["id"] == note["id"]
        assert (await client.delete(f"/api/notes/files/{picture['id']}", params={"force": "true"}, headers=HEADERS)).status_code == 200
        listing = (await client.get(f"/api/sessions/{chat}/notes", headers=HEADERS)).json()
        assert [f["id"] for f in listing["chat"]["files"]] == [page["id"]]


async def test_deleting_the_chat_or_the_project_takes_its_notes(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager, tmp_path: Path) -> None:
    root = tmp_path / "firm"
    root.mkdir()
    store = store_for(manager)
    async with await _client(settings, config, db, manager) as client:
        project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Firm", "folders": [{"path": str(root)}]})).json()
        member = (await manager.create_session("Member", project_id=project["id"])).session.id
        plain = (await manager.create_session("Plain")).session.id
        for chat in (member, plain):
            await client.post(f"/api/sessions/{chat}/notes", headers=HEADERS, json={"title": "Chat"})
            await client.post(f"/api/sessions/{chat}/notes/files", headers=HEADERS, data={"scope": "chat"}, files=[("files", ("a.txt", chat.encode(), "text/plain"))])
        await client.post(f"/api/sessions/{member}/notes", headers=HEADERS, json={"scope": "project", "title": "Project"})

        await manager.delete_session(plain)
        assert await db.fetchone("SELECT 1 FROM notes WHERE session_id = ?", (plain,)) is None
        await manager.delete_session(member)
        assert await db.fetchone("SELECT count(*) AS n FROM notes WHERE project_id = ?", (project["id"],)) is not None
        assert (await db.fetchone("SELECT count(*) AS n FROM notes WHERE project_id = ?", (project["id"],)))["n"] == 1
        await remove_project(SimpleNamespace(manager=manager, db=db, settings=settings, config=config, extensions={}), project["id"])  # type: ignore[arg-type]
        assert (await db.fetchone("SELECT count(*) AS n FROM notes"))["n"] == 0
        assert (await db.fetchone("SELECT count(*) AS n FROM note_files"))["n"] == 0
        assert await store.prune_blobs() == 2


# -- the agent's tools ---------------------------------------------------------------------------


async def test_the_agent_reads_and_writes_the_notes_of_its_chat_and_project(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "firm"
    root.mkdir()
    (root / "chart.png").write_bytes(PNG)
    async with await _client(settings, config, db, manager) as client:
        project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Firm", "folders": [{"path": str(root)}]})).json()
    member = (await manager.create_session("Member", project_id=project["id"])).session.id
    stranger = (await manager.create_session("Stranger")).session.id
    foreign = await store_for(manager).create(Scope("chat", stranger), title="Not yours")

    async def fs_is_file(path: Path) -> bool:
        return path.is_file()

    async def fs_read(path: Path, *, limit: int) -> bytes:
        return path.read_bytes()[:limit]

    services = SimpleNamespace(
        max_tool_output_chars=60_000, extra={"manager": manager}, is_protected=lambda _: False,
        resolve=lambda path: root / path, fs=SimpleNamespace(is_file=fs_is_file, read_bytes=fs_read),
    )
    monkeypatch.setattr("daedalus.tools._common.services_for", lambda _: services)
    monkeypatch.setattr(note_tools, "services_for", lambda _: services)
    context = ToolContext(tenant_id="operator", run_id="run", session_id=member, metadata={"tool_call_id": "call-1"})

    async def call(tool: Any, **arguments: Any) -> Any:
        return await tool().invoke(context, arguments)

    async with manager.bus.subscribe(EventFilter(types=("notes.changed",))) as subscription:
        made = await call(note_tools.note_write, scope="project", title="Findings", body="- [ ] check the oven")
        assert not made.is_error, made.content
        event = await asyncio.wait_for(subscription.__anext__(), 2)
    assert event.payload["by"] == "agent" and event.project_id == project["id"]
    note_id = made.metadata["note_id"]

    listed = await call(note_tools.notes_list)
    assert "Findings" in listed.content and "Not yours" not in listed.content
    read = await call(note_tools.note_read, note_id=note_id)
    assert "version 1" in read.content and "- [ ] check the oven" in read.content
    assert (await call(note_tools.note_read, note_id=foreign["id"])).is_error

    attached = await call(note_tools.note_attach, path="chart.png", scope="project")
    assert not attached.is_error and "![chart.png](files/" in attached.content
    markdown = attached.content.split("show it with: ")[1]
    edited = await call(note_tools.note_write, note_id=note_id, version=1, find="- [ ] check", replace="- [x] check", append=markdown)
    assert not edited.is_error, edited.content
    saved = await store_for(manager).get(note_id)
    assert saved["body"].startswith("- [x] check the oven\n\n![chart.png](files/") and saved["updated_by"] == "agent" and saved["version"] == 2

    # The operator saved in between: the agent is told to read again, nothing is overwritten.
    await store_for(manager).update(note_id, version=2, body="operator's words")
    stale = await call(note_tools.note_write, note_id=note_id, version=2, append="late")
    assert stale.is_error and "NoteRead it again" in stale.content
    assert (await store_for(manager).get(note_id))["body"] == "operator's words"
    ambiguous = await call(note_tools.note_write, note_id=note_id, version=3, find="o", replace="0")
    assert ambiguous.is_error and "matches" in ambiguous.content

    deleted = await call(note_tools.note_delete, note_id=note_id)
    assert not deleted.is_error and (await store_for(manager).get(note_id, deleted=True))["deleted_at"]
    plain_context = ToolContext(tenant_id="operator", run_id="run", session_id=stranger, metadata={})
    refused = await note_tools.notes_list().invoke(plain_context, {"scope": "project"})
    assert refused.is_error and "no project" in refused.content
