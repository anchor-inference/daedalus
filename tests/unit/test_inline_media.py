"""Inline media is immutable, answer-bound and removed with destructive history edits."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx
import pytest
from protocore.contracts.types import Message, MessageRole, Session, TextBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.host.transcript_view import TranscriptViewBuilder, message_view
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.database import Database
from daedalus.stores.media import MEDIA_TENANT, MediaStore
from daedalus.stores.sqlite import SqliteSessionStore

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


async def _stores(db: Database, tmp_path):  # type: ignore[no-untyped-def]
    media = MediaStore(db, FileBlobStore(tmp_path / "blobs"))
    sessions = SqliteSessionStore(db, view=TranscriptViewBuilder(), media=media)
    await sessions.create(Session(id="media-session", tenant_id="daedalus", title="Media"))
    return media, sessions


async def test_a_final_answer_binds_immutable_media(db: Database, tmp_path) -> None:
    media, sessions = await _stores(db, tmp_path)
    source = tmp_path / "screen.png"
    source.write_bytes(PNG)
    staged = await media.stage(
        "media-session",
        "run-1",
        [{"path": str(source), "alt": "Result screen", "caption": "After saving"}],
        layout="single",
    )
    source.write_bytes(b"changed after admission")
    answer = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=f"Here it is.\n\n{staged['markdown']}\n\nDone.")], metadata={"daedalus.run_id": "run-1"})
    assert await sessions.append_transcript("media-session", [answer]) == 1

    stored = (await sessions.list_transcript("media-session"))[0]
    view = message_view(stored)
    assert view["media"][0]["id"] == staged["id"]
    item = view["media"][0]["items"][0]
    saved = await media.item("media-session", staged["id"], item["id"])
    assert saved is not None
    assert media.blobs.path_of(MEDIA_TENANT, saved["blob_ref"]).read_bytes() == PNG


async def test_abandoned_staged_media_and_its_blob_are_pruned(db: Database, tmp_path) -> None:
    media, _ = await _stores(db, tmp_path)
    source = tmp_path / "screen.png"
    source.write_bytes(PNG)
    staged = await media.stage("media-session", "run-1", [{"path": str(source), "alt": "screen", "caption": ""}], layout="single")
    row = await db.fetchone("SELECT blob_ref FROM media_items WHERE presentation_id = ?", (staged["id"],))
    assert row is not None
    path = media.blobs.path_of(MEDIA_TENANT, str(row["blob_ref"]))
    assert path.is_file()

    assert await media.prune_staged(keep_hours=0) == 1
    assert not path.exists()
    assert await db.fetchone("SELECT 1 FROM media_presentations WHERE id = ?", (staged["id"],)) is None


async def test_an_unstaged_or_cross_session_reference_never_binds(db: Database, tmp_path) -> None:
    media, sessions = await _stores(db, tmp_path)
    missing = "00000000-0000-0000-0000-000000000000"
    answer = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=f"![guess](daedalus-media:{missing})")], metadata={"daedalus.run_id": "run-1"})
    await sessions.append_transcript("media-session", [answer])
    stored = (await sessions.list_transcript("media-session"))[0]
    assert message_view(stored)["media"] == []
    assert await media.item("media-session", missing, missing) is None


async def test_retry_tail_removes_media_access_without_a_backup(db: Database, tmp_path) -> None:
    media, sessions = await _stores(db, tmp_path)
    source = tmp_path / "screen.png"
    source.write_bytes(PNG)
    staged = await media.stage("media-session", "run-1", [{"path": str(source), "alt": "screen", "caption": ""}], layout="single")
    user = Message(role=MessageRole.user, content_blocks=[TextBlock(text="show it")])
    answer = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=staged["markdown"])], metadata={"daedalus.run_id": "run-1"})
    await sessions.append_transcript("media-session", [user, answer])
    rows = await sessions.list_transcript("media-session")
    item = message_view(rows[1])["media"][0]["items"][0]
    await sessions.truncate_history("media-session", "daedalus", int(rows[0].metadata["daedalus.seq"]), [])
    assert await media.item("media-session", staged["id"], item["id"]) is None
    assert not await db.fetchall("SELECT id FROM media_presentations WHERE id = ?", (staged["id"],))


async def test_explicit_fork_gets_scoped_media_references(db: Database, tmp_path) -> None:
    media, sessions = await _stores(db, tmp_path)
    await sessions.create(Session(id="forked-session", tenant_id="daedalus", title="Fork"))
    source = tmp_path / "screen.png"
    source.write_bytes(PNG)
    staged = await media.stage("media-session", "run-1", [{"path": str(source), "alt": "screen", "caption": ""}], layout="single")
    answer = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=staged["markdown"])], metadata={"daedalus.run_id": "run-1"})
    await sessions.append_transcript("media-session", [answer])
    original = (await sessions.list_transcript("media-session"))[0]
    copied = await media.clone_references("media-session", "forked-session", original)
    await sessions.append_transcript("forked-session", [copied])
    forked = message_view((await sessions.list_transcript("forked-session"))[0])
    assert forked["media"][0]["id"] != staged["id"]
    assert staged["id"] not in forked["text"]
    assert await media.item("forked-session", forked["media"][0]["id"], forked["media"][0]["items"][0]["id"])


async def test_an_album_can_be_links_and_does_not_fetch_them(db: Database, tmp_path) -> None:
    media, sessions = await _stores(db, tmp_path)
    staged = await media.stage(
        "media-session",
        "run-1",
        [
            {"url": "https://cdn.example/a.jpg", "alt": "one", "caption": ""},
            {"url": "https://cdn.example/clip", "kind": "video", "alt": "two", "caption": "roll"},
        ],
        layout="album",
    )
    answer = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=staged["markdown"])], metadata={"daedalus.run_id": "run-1"})
    await sessions.append_transcript("media-session", [answer])
    saved = message_view((await sessions.list_transcript("media-session"))[0])["media"][0]
    assert [item["kind"] for item in saved["items"]] == ["image", "video"]
    assert [item["url"] for item in saved["items"]] == ["https://cdn.example/a.jpg", "https://cdn.example/clip"]
    assert media.blobs.path_of(MEDIA_TENANT, "").exists() is False
    with pytest.raises(ValueError, match="http"):
        await media.stage("media-session", "run-1", [{"url": "file:///etc/passwd", "kind": "image", "alt": "", "caption": ""}, {"url": "https://cdn.example/b.png", "alt": "", "caption": ""}], layout="album")


async def test_album_rejects_single_content(db: Database, tmp_path) -> None:
    media, _ = await _stores(db, tmp_path)
    source = tmp_path / "screen.png"
    source.write_bytes(PNG)
    with pytest.raises(ValueError, match="2-10"):
        await media.stage("media-session", "run-1", [{"path": str(source), "alt": "", "caption": ""}], layout="album")


async def test_mixed_album_preserves_media_order_in_answer(db: Database, tmp_path) -> None:
    media, sessions = await _stores(db, tmp_path)
    items = []
    for filename, content in [("photo.png", PNG), ("clip.mp4", b"\x00\x00\x00\x18ftypisom"), ("loop.gif", b"GIF89a\x01\x00\x01\x00")]:
        source = tmp_path / filename
        source.write_bytes(content)
        items.append({"path": str(source), "alt": filename, "caption": ""})
    staged = await media.stage("media-session", "run-1", items, layout="album")
    answer = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=staged["markdown"])], metadata={"daedalus.run_id": "run-1"})
    await sessions.append_transcript("media-session", [answer])
    saved = message_view((await sessions.list_transcript("media-session"))[0])["media"][0]
    assert [item["kind"] for item in saved["items"]] == ["image", "video", "animation"]
    source = tmp_path / "sound.mp3"
    source.write_bytes(b"ID3audio")
    with pytest.raises(ValueError, match="audio"):
        await media.stage("media-session", "run-1", [items[0], {"path": str(source), "alt": "sound", "caption": ""}], layout="album")


async def test_content_api_authenticates_native_media_and_serves_ranges(settings: Settings, db: Database, tmp_path) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    state = await manager.create_session("media API")
    source = tmp_path / "screen.png"
    source.write_bytes(PNG)
    staged = await manager.media.stage(state.session.id, "run-api", [{"path": str(source), "alt": "screen", "caption": ""}], layout="single")
    answer = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=staged["markdown"])], metadata={"daedalus.run_id": "run-api"})
    await manager.sessions.append_transcript(state.session.id, [answer])
    item = message_view((await manager.sessions.list_transcript(state.session.id))[0])["media"][0]["items"][0]
    application = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={})
    app = build_app(application, "media-token")  # type: ignore[arg-type]
    path = f"/api/sessions/{state.session.id}/media/{staged['id']}/{item['id']}/content"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:  # type: ignore[arg-type]
        assert (await client.get(path)).status_code == 401
        access = await client.post("/api/media/access", headers={"X-Daedalus-Token": "media-token"})
        assert access.status_code == 200 and access.cookies.get("daedalus_session")
        response = await client.get(path, headers={"Range": "bytes=0-7"})
        assert response.status_code == 206
        assert response.content == PNG[:8]
        assert response.headers["content-range"] == f"bytes 0-7/{len(PNG)}"
        assert response.headers["x-content-type-options"] == "nosniff"
    await manager.close()


DOCX = b"PK\x03\x04" + b"\x00" * 26 + b"word/document.xml"


async def test_any_file_is_attached_as_a_file_and_a_broken_picture_is_still_refused(db: Database, tmp_path) -> None:
    media, sessions = await _stores(db, tmp_path)
    document = tmp_path / "Акт о приёме.docx"
    document.write_bytes(DOCX)
    staged = await media.stage("media-session", "run-1", [{"path": str(document), "alt": "Акт", "caption": "для проверки"}], layout="single")
    assert staged["kind"] == "file" and staged["markdown"] == f"![Акт](daedalus-media:{staged['id']})"
    answer = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=f"Готово.\n\n{staged['markdown']}")], metadata={"daedalus.run_id": "run-1"})
    await sessions.append_transcript("media-session", [answer])
    item = message_view((await sessions.list_transcript("media-session"))[0])["media"][0]["items"][0]
    assert (item["kind"], item["filename"], item["byte_size"]) == ("file", "Акт о приёме.docx", len(DOCX))
    assert item["mime_type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    # kind=file attaches even a picture as a plain file to download.
    picture = tmp_path / "scan.png"
    picture.write_bytes(PNG)
    assert (await media.stage("media-session", "run-1", [{"path": str(picture), "kind": "file", "alt": "", "caption": ""}], layout="single"))["kind"] == "file"
    # A file named as media that does not decode stays a refusal, not a silent download.
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a png at all")
    with pytest.raises(ValueError, match="damaged"):
        await media.stage("media-session", "run-1", [{"path": str(broken), "alt": "", "caption": ""}], layout="single")
    # An album is pictures and clips; documents go one per call.
    with pytest.raises(ValueError, match="one per call"):
        await media.stage("media-session", "run-1", [{"path": str(picture), "alt": "", "caption": ""}, {"path": str(document), "alt": "", "caption": ""}], layout="album")


async def test_an_attached_file_is_a_download_at_both_addresses(settings: Settings, db: Database, tmp_path) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("file API")
        document = tmp_path / "page.html"
        document.write_bytes(b"<script>alert(1)</script>")
        staged = await manager.media.stage(state.session.id, "run-api", [{"path": str(document), "alt": "page", "caption": ""}], layout="single")
        answer = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=staged["markdown"])], metadata={"daedalus.run_id": "run-api"})
        await manager.sessions.append_transcript(state.session.id, [answer])
        item = message_view((await manager.sessions.list_transcript(state.session.id))[0])["media"][0]["items"][0]
        application = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={})
        app = build_app(application, "media-token")  # type: ignore[arg-type]
        base = f"/api/sessions/{state.session.id}/media/{staged['id']}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:  # type: ignore[arg-type]
            content = await client.get(f"{base}/{item['id']}/content", headers={"X-Daedalus-Token": "media-token"})
            # An attached page opened inline would run on the app's origin.
            assert content.status_code == 200 and content.headers["content-disposition"].startswith("attachment")
            # The preview's address: ``{base}/download?path=<item>/<name>``, with the token a link carries.
            preview = await client.get(f"{base}/download", params={"path": f"{item['id']}/page.html", "token": "media-token"})
            assert preview.status_code == 200 and preview.content == b"<script>alert(1)</script>"
            assert preview.headers["content-disposition"].startswith("attachment")
            assert (await client.get(f"{base}/download", params={"path": "nothing/page.html", "token": "media-token"})).status_code == 404
    finally:
        await manager.close()
