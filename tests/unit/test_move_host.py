"""Moving a session into a project whose folder is on the host, from the container: it is decided by
the host terminal daemon answering, exactly as starting a session there is, and the moved session
works on the host as one started in the project does. A move once asked only whether the folder was
mounted here and refused every host project with "mount it and restart"."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.host_exec import HostExecBackend
from daedalus.host.session_runner import HOME_KEY, FolderUnworkable, SessionManager, SessionState, host_home_name
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec
from tests.unit.test_host_exec import HOST_ROOT, ShellHost, container_only, context, host_disk, host_project
from tests.unit.test_project_folders_api import HEADERS, _client
from tests.unit.test_session_runner import ScriptedProvider, _manager

PLACE_KEYS = ("directory", "folder_id", HOME_KEY)
"""The metadata that decides where a session works; everything else of a moved chat is its own."""


def where(manager: SessionManager, state: SessionState) -> dict[str, Any]:
    """Everything that says where a session works and how its tools get there, with its id taken out."""
    sid = state.session.id
    services = state.services
    assert services is not None
    return {
        "metadata": {key: str(state.metadata.get(key, "")).replace(sid, "<id>") for key in PLACE_KEYS},
        "workspace": str(state.workspace).replace(sid, "<id>"),
        "work_dir": str(manager.work_dir(state)).replace(sid, "<id>"),
        "env": manager.env_of(sid, state.metadata, state.project),
        "backend": type(services.exec_backend).__name__,
        "walls": services.walls is None,
    }


async def test_a_chat_moved_into_a_host_project_works_there_as_one_started_in_it(settings: Settings, db: Database, tmp_path: Path) -> None:
    from daedalus.tools.files import write_file

    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        disk = host_disk(tmp_path)
        manager.host_bridge = ShellHost(disk)  # type: ignore[assignment]
        project = await host_project(manager)
        chat = await manager.create_session("Notes")
        sid = chat.session.id
        moved = await manager.attach_project(sid, project)
        fresh = await manager.create_session("Fresh", project_id=project.id)
        assert where(manager, moved) == where(manager, fresh)
        assert moved.workspace == settings.workspaces_dir / host_home_name(sid) and moved.workspace.is_dir()
        assert isinstance(moved.services.exec_backend, HostExecBackend) and manager.env_of(sid, moved.metadata, moved.project) == "host"  # type: ignore[union-attr]
        wrote = await write_file().invoke(context(sid), {"path": "moved.txt", "content": "on the host"})
        assert not wrote.is_error, wrote.content
        assert (disk / "moved.txt").read_text() == "on the host", "its next tool call works in the host folder"

        own = await manager.create_session("Errand")
        own_moved = await manager.attach_project(own.session.id, project, own_directory=True)
        own_fresh = await manager.create_session("Errand fresh", project_id=project.id, own_directory=True)
        assert where(manager, own_moved) == where(manager, own_fresh)
        assert manager.work_dir(own_moved) == Path(HOST_ROOT) / ".agents" / own.session.id
        assert (disk / ".agents" / own.session.id / "inbox").is_dir(), "its own directory is made on the host, as for one started there"

        garage = tmp_path / "garage"
        garage.mkdir()
        local = await manager.projects.create("Garage", [str(garage)])
        back = await manager.attach_project(own.session.id, local)
        fresh_local = await manager.create_session("Garage fresh", project_id=local.id)
        assert where(manager, back) == where(manager, fresh_local), "moved out, nothing of the host is left on it"
        assert back.workspace == garage and not isinstance(back.services.exec_backend, HostExecBackend)  # type: ignore[union-attr]
    finally:
        await manager.close()


async def test_a_move_into_a_host_project_is_refused_unchanged_while_the_daemon_is_down(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        project = await host_project(manager)
        chat = await manager.create_session("Notes")
        before = chat.project
        host = ShellHost(host_disk(tmp_path), up=False)
        manager.host_bridge = host  # type: ignore[assignment]
        with pytest.raises(FolderUnworkable, match=r"/home/someone/labs is on the host, and .*not answering"):
            await manager.attach_project(chat.session.id, project, own_directory=True)
        assert not host.calls
        assert chat.project is before and (await manager.project_of(chat.session.id)).id == before.id  # type: ignore[union-attr]
    finally:
        await manager.close()


async def test_a_host_chat_moves_into_a_project_and_another_chat_into_it(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        disk = host_disk(tmp_path)
        manager.host_bridge = ShellHost(disk)  # type: ignore[assignment]
        errand = await manager.create_session("Errand", on_host=True)
        guest = await manager.create_session("Guest")
        joined = await manager.attach_project(guest.session.id, errand.project)  # type: ignore[arg-type]
        assert manager.env_of(guest.session.id, joined.metadata, joined.project) == "host"
        assert manager.work_dir(joined) == errand.project.primary.path  # type: ignore[union-attr]

        # Beside the host chats' own folders rather than above them: a project may not hold another's.
        project = await manager.projects.create("Sources", [FolderSpec(f"{HOST_ROOT}/src", env="host")])
        moved = await manager.attach_project(errand.session.id, project)
        assert manager.work_dir(moved) == Path(HOST_ROOT) / "src" and manager.env_of(errand.session.id, moved.metadata, moved.project) == "host"
    finally:
        await manager.close()


async def test_natively_a_container_folder_is_refused_with_the_way_round(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        boxed = await manager.projects.create("Boxed", [FolderSpec(str(tmp_path / "boxed"), env="container")])
        chat = await manager.create_session("Notes")
        manager.projects.local_env = "host"
        with pytest.raises(FolderUnworkable, match="is a container folder; an agent started here cannot work in it"):
            await manager.attach_project(chat.session.id, boxed)
        manager.projects.local_env = "container"
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


async def test_the_api_moves_a_chat_into_a_host_project_or_names_the_real_problem(served: Any, tmp_path: Path) -> None:
    manager, client = served
    container_only(manager)
    project = await host_project(manager)
    sid = (await client.post("/api/sessions", headers=HEADERS, json={"title": "Notes"})).json()["id"]
    url = f"/api/sessions/{sid}/project"

    for own in (False, True):
        down = await client.post(url, headers=HEADERS, json={"project_id": project.id, "own_directory": own})
        assert down.status_code == 409, down.text
        detail = down.json()["detail"]
        assert "/home/someone/labs is on the host" in detail and "systemctl --user start daedalus-ptyd" in detail
        assert "mount it" not in detail, "a host folder is never asked to be mounted here"

    disk = host_disk(tmp_path)
    manager.host_bridge = ShellHost(disk)
    moved = await client.post(url, headers=HEADERS, json={"project_id": project.id, "own_directory": True})
    assert moved.status_code == 200, moved.text
    assert moved.json()["workspace"] == f"{HOST_ROOT}/.agents/{sid}", "the answer names where it works, on the host"
    assert (disk / ".agents" / sid / "inbox").is_dir()
    detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
    assert detail["env"] == "host"

    garage = tmp_path / "garage"
    garage.mkdir()
    local = (await client.post("/api/projects", headers=HEADERS, json={"name": "Garage", "folders": [{"path": str(garage)}]})).json()
    back = await client.post(url, headers=HEADERS, json={"project_id": local["id"]})
    assert back.status_code == 200 and back.json()["workspace"] == str(garage)
    assert (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()["env"] == "container"

    manager.projects.local_env = "host"
    boxed = await client.post(url, headers=HEADERS, json={"project_id": local["id"]})
    assert boxed.status_code == 409 and "is a container folder" in boxed.json()["detail"]
    manager.projects.local_env = "container"
