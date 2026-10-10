"""The agent's tools that open a file, in a session working on the host from the container: each one
reads the file where the session's commands run, by the same relative path Exec resolves, and hands
on a copy fetched here where something of this process has to open it."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from daedalus.browser.model import NotFound, Owner
from daedalus.config import Settings
from daedalus.host.filesystem import ShellFS
from daedalus.host.host_exec import HostExecBackend
from daedalus.stores.database import Database
from tests.unit.test_host_exec import ShellHost, container_only, context, host_disk
from tests.unit.test_session_runner import ScriptedProvider, _manager

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3000  # binary, and past one piece of a shell read


@pytest.fixture
async def host_chat(settings: Settings, db: Database, tmp_path: Path) -> Any:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        disk = host_disk(tmp_path)
        host = ShellHost(disk)
        manager.host_bridge = host  # type: ignore[assignment]
        state = await manager.create_session("Errand", on_host=True)
        folder = disk / ".local/state/daedalus/chats" / state.session.id
        (folder / "site" / "clips").mkdir(parents=True)
        (folder / "chk1.png").write_bytes(PNG)
        (folder / "site" / "clips" / "x.jpg").write_bytes(b"\xff\xd8\xff" + os.urandom(5000))
        (folder / "report.md").write_text("# report\n")
        yield manager, state, folder, host
    finally:
        await manager.close()


async def test_the_shell_reads_and_writes_bytes_whatever_they_are(host_chat: Any) -> None:
    _, state, folder, _ = host_chat
    fs = state.services.fs
    assert isinstance(fs, ShellFS)
    target = state.services.resolve("chk1.png")
    assert await fs.read_bytes(target, limit=len(PNG)) == PNG, "byte for byte across several pieces"
    with pytest.raises(ValueError, match="more than"):
        await fs.read_bytes(target, limit=100)
    with pytest.raises(FileNotFoundError):
        await fs.read_bytes(state.services.resolve("missing.png"), limit=100)
    await fs.write_bytes(state.services.resolve("out/blob.bin"), PNG)
    assert (folder / "out/blob.bin").read_bytes() == PNG


async def test_a_large_file_reads_where_sigpipe_is_ignored(host_chat: Any) -> None:
    """The host daemon runs as a systemd unit, which ignores SIGPIPE: ``tail`` cut off by ``head``
    then printed "Broken pipe" into the base64 and every file past one piece failed at byte 0."""
    _, state, _, host = host_chat
    host.ignore_sigpipe = True
    assert await state.services.fs.read_bytes(state.services.resolve("chk1.png"), limit=len(PNG)) == PNG


async def test_send_file_without_a_chat_reads_nothing_and_says_where_the_file_is(host_chat: Any) -> None:
    from daedalus.host.services import NO_CHAT_BOUND
    from daedalus.tools.chat import send_file

    _, state, _, host = host_chat

    async def unbound(fetch: Any, caption: str | None) -> str:
        return NO_CHAT_BOUND

    state.services.send_file = unbound
    before = len(host.calls)
    result = await send_file().invoke(context(state.session.id), {"path": "chk1.png"})
    assert not result.is_error, result.content
    assert "file card in the app" in result.content and "no Telegram chat" in result.content
    assert not any("base64" in " ".join(argv) for argv, _, _ in host.calls[before:]), "no copy is fetched for nobody"


async def test_image_view_looks_at_an_image_on_the_host_by_a_relative_path(host_chat: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.tools import vision

    _, state, folder, _ = host_chat
    seen: list[tuple[bytes, str]] = []

    async def fake_look(_vision: Any, _manager: Any, data: bytes, mime: str, task: str, **_: Any) -> tuple[str, str]:
        seen.append((data, mime))
        return "a chart", "eyes"

    monkeypatch.setattr(vision, "look", fake_look)
    ctx = context(state.session.id)
    for path, data, mime in (("chk1.png", PNG, "image/png"), ("site/clips/x.jpg", (folder / "site/clips/x.jpg").read_bytes(), "image/jpeg")):
        result = await vision.image_view().invoke(ctx, {"path": path, "task": "what is it"})
        assert not result.is_error, result.content
        assert "a chart" in result.content
        assert seen[-1] == (data, mime), "the image's bytes from the host, not a file of this process"
    missing = await vision.image_view().invoke(ctx, {"path": "gone.png", "task": "x"})
    assert missing.is_error and "no such file" in missing.content


async def test_send_file_and_attach_media_hand_on_a_copy_fetched_from_the_host(host_chat: Any) -> None:
    from daedalus.tools.chat import attach_media, send_file

    _, state, folder, _ = host_chat
    services = state.services
    sent: list[tuple[Path, bytes]] = []
    attached: list[list[dict[str, str]]] = []

    async def deliver(fetch: Any, caption: str | None) -> str:
        path = await fetch()
        sent.append((path, path.read_bytes()))
        return "delivered"

    async def stage(items: list[dict[str, str]], layout: str) -> dict[str, Any]:
        attached.append(items)
        return {"kind": "image", "markdown": "![x](media:1)", "id": "p1"}

    services.send_file = deliver
    services.attach_media = stage
    ctx = context(state.session.id)
    result = await send_file().invoke(ctx, {"path": "report.md"})
    assert not result.is_error, result.content
    [(path, data)] = sent
    assert data == b"# report\n" and path.name == "report.md"
    assert state.workspace in path.parents, "the copy is kept in the session's own directory here"
    assert (await send_file().invoke(ctx, {"path": "nope.md"})).is_error

    media = await attach_media().invoke(ctx, {"items": [{"path": "chk1.png"}, {"path": "site/clips/x.jpg"}], "layout": "album"})
    assert not media.is_error, media.content
    [items] = attached
    assert Path(items[0]["path"]).read_bytes() == PNG
    assert Path(items[1]["path"]).read_bytes() == (folder / "site/clips/x.jpg").read_bytes()


async def test_spawn_agent_and_a_schedule_get_local_copies_of_host_files(host_chat: Any) -> None:
    from daedalus.tools.chat import spawn_agent
    from daedalus.tools.scheduling import schedule_create

    _, state, _, _ = host_chat
    services = state.services
    handed: dict[str, Any] = {}

    async def spawn(**kwargs: Any) -> str:
        handed["spawn"] = kwargs["files"]
        return "s2"

    async def schedule(action: str, **kwargs: Any) -> dict[str, Any]:
        handed["schedule"] = kwargs["files"]
        return {"id": "p1", "next_run_at": "soon", "kind": "agent"}

    services.spawn_agent = spawn
    services.schedule = schedule
    ctx = context(state.session.id)
    made = await spawn_agent().invoke(ctx, {"title": "Helper", "brief": "help", "files": ["report.md"]})
    assert not made.is_error, made.content
    assert Path(handed["spawn"][0]).read_text() == "# report\n"
    refused = await spawn_agent().invoke(ctx, {"title": "Helper", "brief": "help", "files": ["nope.md"]})
    assert refused.is_error and "do not exist" in refused.content

    planned = await schedule_create().invoke(ctx, {"name": "daily", "prompt": "go", "cron": "0 9 * * *", "files": ["chk1.png"]})
    assert not planned.is_error, planned.content
    assert Path(handed["schedule"][0]).read_bytes() == PNG


async def test_the_browser_uploads_and_downloads_where_the_session_works(host_chat: Any) -> None:
    from daedalus.tools.browser import SessionFiles

    manager, state, folder, _ = host_chat
    files = SessionFiles(state.services, manager, Owner(kind="session", id=state.session.id))
    name, data = await files.read("site/clips/x.jpg")
    assert name == "x.jpg" and data == (folder / "site/clips/x.jpg").read_bytes()
    with pytest.raises(NotFound):
        await files.read("nope.bin")
    said = await files.save("page.pdf", b"%PDF-1.7 bytes", None)
    assert (folder / "downloads/page.pdf").read_bytes() == b"%PDF-1.7 bytes" and "downloads/page.pdf" in said
    await files.save("page.pdf", b"second", None)
    assert (folder / "downloads/page-1.pdf").read_bytes() == b"second", "an existing name is not overwritten"


async def test_the_session_runs_on_the_host_and_keeps_its_fetched_files_here(host_chat: Any) -> None:
    _, state, _, _ = host_chat
    assert isinstance(state.services.exec_backend, HostExecBackend)
    assert state.services.extra["local_home"] == state.workspace
