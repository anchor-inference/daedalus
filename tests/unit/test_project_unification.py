from __future__ import annotations

import time as system_time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import daedalus.extensions.api as api_module
from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

HEADERS = {"X-Daedalus-Token": "tok"}


def _app(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> Any:
    async def create_session(title: str, **kwargs: Any) -> Any:
        return await manager.create_session(title, **kwargs)

    return SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=create_session)


async def _client(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, config, db, manager), "tok")), base_url="http://test")  # type: ignore[arg-type]


async def test_a_name_creates_the_project_folder_and_every_session_has_a_project(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        async with await _client(settings, config, db, manager) as client:
            response = await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery"})
            assert response.status_code == 200
            project = response.json()
            assert Path(project["folders"][0]["path"]).parent == settings.workspaces_dir
            assert Path(project["folders"][0]["path"]).is_dir()
            assert project["settings"]["snapshots"] is True

            shared = (await client.post("/api/sessions", headers=HEADERS, json={"title": "Menu", "project_id": project["id"]})).json()
            private = (await client.post("/api/sessions", headers=HEADERS, json={"title": "Prices", "project_id": project["id"], "own_directory": True})).json()
            shared_state = manager.live_state(shared["id"])
            private_state = manager.live_state(private["id"])
            assert shared_state is not None and shared_state.workspace == Path(project["folders"][0]["path"])
            assert private_state is not None and private_state.workspace.parent.parent == Path(project["folders"][0]["path"])
            assert private_state.project is not None and private_state.project.id == project["id"]
            assert private_state.services is not None and private_state.services.walls is not None and private_state.services.walls.readable == (private_state.workspace,)

            listing = (await client.get("/api/sessions", headers=HEADERS)).json()
            assert "free" not in listing
            assert all(row["project_id"] for row in listing["sessions"])

            # The folder is under the workspaces tree, so it is the installation's to keep: moved
            # away, it is simply made again rather than refused. A folder the operator pointed at is
            # the opposite case and still refuses — tests/unit/test_projects.py has both sides.
            root = Path(project["folders"][0]["path"])
            root.rename(root.with_name(root.name + "-away"))
            assert await manager.projects.ensure_reachable((await manager.projects.get(project["id"])).primary) is True
            assert root.is_dir() and (root / "inbox").is_dir()
    finally:
        await manager.close()


async def test_the_directory_browser_is_authenticated_sealed_contained_and_bounded(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = settings.workspaces_dir
    root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    for index in range(270):
        (root / f"folder-{index:03d}").mkdir()
    try:
        async with await _client(settings, config, db, manager) as client:
            assert (await client.get("/api/project-directories")).status_code == 401
            roots = (await client.get("/api/project-directories", headers=HEADERS)).json()["roots"]
            assert any(item["path"] == str(root.resolve()) for item in roots)
            assert (await client.get("/api/project-directories", headers=HEADERS)).json()["docker"] is (not settings.native)

            escaped = await client.get("/api/project-directories", headers=HEADERS, params={"root": str(root), "path": "../outside"})
            assert escaped.status_code == 400
            symlinked = await client.get("/api/project-directories", headers=HEADERS, params={"root": str(root), "path": str(root / "escape")})
            assert symlinked.status_code == 403

            sealed = await client.get(
                "/api/project-directories",
                headers=HEADERS,
                params={"root": str(settings.state_dir.parent.resolve()), "path": str(settings.state_dir)},
            )
            assert sealed.status_code == 403
            unoffered = await client.get("/api/project-directories", headers=HEADERS, params={"root": str(outside), "path": str(outside)})
            assert unoffered.status_code == 403

            real_scandir = api_module.os.scandir

            def refuse_scan(path: Any) -> Any:
                raise PermissionError(13, "permission denied", path)

            monkeypatch.setattr(api_module.os, "scandir", refuse_scan)
            unreadable = await client.get("/api/project-directories", headers=HEADERS, params={"root": str(root), "path": str(root)})
            assert unreadable.status_code == 403
            monkeypatch.setattr(api_module.os, "scandir", real_scandir)

            listing = (await client.get("/api/project-directories", headers=HEADERS, params={"root": str(root), "path": str(root)})).json()
            assert listing["truncated"] is True
            assert len(listing["entries"]) <= 250
            assert "escape" not in {entry["name"] for entry in listing["entries"]}
            assert all(set(entry) == {"name", "path", "readable", "writable", "project_id"} for entry in listing["entries"])

            ticks = iter((0.0, 1.0))
            monkeypatch.setattr(api_module, "time", SimpleNamespace(time=system_time.time, monotonic=lambda: next(ticks, 1.0)))
            timed = (await client.get("/api/project-directories", headers=HEADERS, params={"root": str(root), "path": str(root)})).json()
            assert timed["truncated"] is True and timed["entries"] == []
    finally:
        await manager.close()


@pytest.mark.parametrize("delete_workspace", [False, True])
async def test_last_chat_removes_its_scratch_project_but_keeps_files(settings: Settings, config: RuntimeConfig, db: Database, delete_workspace: bool) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        chat = await manager.create_session("automatic")
        pid = chat.project.id
        (chat.workspace / "keep.txt").write_text("keep")
        await manager.close()
        manager = SessionManager(settings, config, db=db)
        await manager.start()
        await manager.delete_session(chat.session.id, delete_workspace=delete_workspace)
        assert await manager.projects.get(pid) is None
        assert chat.workspace not in manager.projects.roots
        assert (chat.workspace / "keep.txt").read_text() == "keep"
    finally:
        await manager.close()


@pytest.mark.parametrize("creator_first", [False, True])
@pytest.mark.parametrize("delete_workspace", [False, True])
async def test_a_second_chat_keeps_the_scratch_project_for_good(settings: Settings, config: RuntimeConfig, db: Database, creator_first: bool, delete_workspace: bool) -> None:
    """Two chats make a project, and deleting them both, the one whose id it took first or last,
    leaves an empty project: once listed as a project it is not taken away with its chats."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        creator = await manager.create_session("automatic")
        pid = creator.project.id
        sibling = await manager.create_session("sibling", project_id=pid)
        (creator.workspace / "keep.txt").write_text("keep")
        await manager.projects.update(pid, name="renamed", snapshots=False)
        first, last = (creator, sibling) if creator_first else (sibling, creator)
        await manager.delete_session(first.session.id, delete_workspace=delete_workspace)
        assert await manager.projects.get(pid) is not None
        await manager.close()
        manager = SessionManager(settings, config, db=db)
        await manager.start()
        await manager.delete_session(last.session.id, delete_workspace=delete_workspace)
        kept = await manager.projects.get(pid)
        assert kept is not None and not kept.settings.ephemeral
        assert (creator.workspace / "keep.txt").read_text() == "keep"
    finally:
        await manager.close()


async def test_empty_explicit_and_system_projects_remain(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        explicit = await manager.projects.create("explicit")
        system = await manager.projects.ensure_system("voice", name="Voice", root=settings.workspaces_dir / "voice")
        for project in (explicit, system):
            state = await manager.create_session("member", project_id=project.id)
            await manager.delete_session(state.session.id)
            assert await manager.projects.get(project.id) is not None
            assert project.primary.path.exists()
    finally:
        await manager.close()
