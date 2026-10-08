"""A plain chat started on the host from the container: its scratch folder is made on the host and
removed with it, it is refused natively and while the daemon is down, and the session says where
it runs so the app can mark it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import HostChatRefused, HostUnreachable, SessionManager, host_chat_path
from daedalus.stores.database import Database
from tests.unit.test_host_exec import HOST_ROOT, ShellHost, container_only, context, host_disk
from tests.unit.test_project_folders_api import HEADERS, _client
from tests.unit.test_session_runner import ScriptedProvider, _manager


def test_only_the_exact_scratch_shape_counts_as_a_host_chat_folder() -> None:
    assert host_chat_path(Path("/home/someone/.local/state/daedalus/chats/abc"), "abc")
    assert not host_chat_path(Path("/home/someone/.local/state/daedalus/chats/abc"), "other")
    assert not host_chat_path(Path("/home/someone/projects/abc"), "abc"), "an operator's folder is never one"
    assert not host_chat_path(Path("daedalus/chats/abc"), "abc")


async def test_a_host_chat_works_in_its_own_folder_on_the_host_and_takes_it_when_deleted(settings: Settings, db: Database, tmp_path: Path) -> None:
    from daedalus.tools.files import write_file

    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        disk = host_disk(tmp_path)
        host = ShellHost(disk)
        manager.host_bridge = host  # type: ignore[assignment]
        state = await manager.create_session("Errand", on_host=True)
        sid = state.session.id
        project = state.project
        assert project is not None and project.id == sid and project.settings.ephemeral
        folder = project.primary
        assert folder.env == "host" and folder.path == Path(HOST_ROOT) / ".local/state/daedalus/chats" / sid
        assert (disk / ".local/state/daedalus/chats" / sid).is_dir(), "the folder is made on the host before a command needs it"
        assert manager.env_of(sid, state.metadata, project) == "host"
        assert state.services is not None and state.services.workspace_dir == folder.path

        wrote = await write_file().invoke(context(sid), {"path": "note.txt", "content": "on the host"})
        assert not wrote.is_error, wrote.content
        assert (disk / ".local/state/daedalus/chats" / sid / "note.txt").read_text() == "on the host"

        assert await manager.delete_session(sid)
        assert await manager.projects.get(sid) is None
        assert not (disk / ".local/state/daedalus/chats" / sid).exists(), "the scratch folder goes with the chat"
        assert (disk / "README.md").exists(), "nothing else on the host is touched"
    finally:
        await manager.close()


async def test_a_host_chat_is_refused_natively_with_a_project_and_while_the_daemon_is_down(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        host = ShellHost(host_disk(tmp_path), up=False)
        manager.host_bridge = host  # type: ignore[assignment]
        with pytest.raises(HostUnreachable, match="not answering"):
            await manager.create_session("Errand", on_host=True)
        assert await db.fetchone("SELECT 1 FROM sessions") is None and await db.fetchone("SELECT 1 FROM projects WHERE json_extract(settings, '$.ephemeral') = 1") is None
        assert not host.calls, "nothing is asked of a daemon that is down"

        host.up = True
        other = await manager.projects.create("Labs", [str(tmp_path / "labs")])
        with pytest.raises(HostChatRefused, match="names no project"):
            await manager.create_session("Errand", on_host=True, project_id=other.id)

        manager.projects.local_env = "host"
        with pytest.raises(HostChatRefused, match="runs on the host already"):
            await manager.create_session("Errand", on_host=True)
    finally:
        await manager.close()


@pytest.fixture
async def served(settings: Settings, config: RuntimeConfig, db: Database) -> Any:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        async with _client(settings, config, db, manager) as client:
            yield manager, client
    finally:
        await manager.close()


async def test_the_api_starts_a_host_chat_refuses_it_with_the_reason_and_marks_it(served: Any, tmp_path: Path) -> None:
    manager, client = served
    container_only(manager)
    down = await client.post("/api/sessions", headers=HEADERS, json={"title": "Errand", "env": "host"})
    assert down.status_code == 409 and "host terminal daemon is not answering" in down.json()["detail"]

    disk = host_disk(tmp_path)
    manager.host_bridge = ShellHost(disk)
    started = await client.post("/api/sessions", headers=HEADERS, json={"title": "Errand", "env": "host"})
    assert started.status_code == 200, started.text
    sid = started.json()["id"]
    plain = (await client.post("/api/sessions", headers=HEADERS, json={"title": "Here"})).json()["id"]

    detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
    assert detail["env"] == "host" and detail["project"]["folders"][0]["env"] == "host"
    assert (await client.get(f"/api/sessions/{plain}", headers=HEADERS)).json()["env"] == "container"
    rows = {row["id"]: row for row in (await client.get("/api/sessions", headers=HEADERS)).json()["sessions"]}
    assert rows[sid]["env"] == "host" and rows[plain]["env"] == "container"

    project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Labs"})).json()
    named = await client.post("/api/sessions", headers=HEADERS, json={"title": "x", "env": "host", "project_id": project["id"]})
    assert named.status_code == 400 and "names no project" in named.json()["detail"]

    manager.projects.local_env = "host"
    native = await client.post("/api/sessions", headers=HEADERS, json={"title": "x", "env": "host"})
    assert native.status_code == 409 and "runs on the host already" in native.json()["detail"]
    manager.projects.local_env = "container"


async def test_a_kept_host_chat_keeps_its_folder(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        disk = host_disk(tmp_path)
        manager.host_bridge = ShellHost(disk)  # type: ignore[assignment]
        state = await manager.create_session("Errand", on_host=True)
        sid = state.session.id
        await manager.projects.update(sid, ephemeral=False)
        await manager.reload_project(await manager.projects.get(sid), sid)
        assert await manager.delete_session(sid)
        assert await manager.projects.get(sid) is not None, "a chat kept as a project stays a project"
        assert (disk / ".local/state/daedalus/chats" / sid).is_dir(), "and its folder on the host stays"
    finally:
        await manager.close()
