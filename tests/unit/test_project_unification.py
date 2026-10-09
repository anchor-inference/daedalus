from __future__ import annotations

import time as system_time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import daedalus.extensions.api_folder_browser as browser_module
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


async def test_the_folder_browser_is_authenticated_sealed_contained_and_bounded(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = settings.workspaces_dir
    root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / ".hidden").mkdir()
    for index in range(270):
        (root / f"folder-{index:03d}").mkdir()
    try:
        async with await _client(settings, config, db, manager) as client:
            assert (await client.get("/api/folders")).status_code == 401
            home = (await client.get("/api/folders", headers=HEADERS)).json()
            assert any(item["path"] == str(root.resolve()) and item["kind"] == "workspaces" for item in home["roots"])
            assert home["env"] == "container" and home["crumbs"][-1]["path"] == home["path"]

            escaped = await client.get("/api/folders", headers=HEADERS, params={"path": f"{root}/../outside"})
            assert escaped.status_code == 400
            symlinked = await client.get("/api/folders", headers=HEADERS, params={"path": str(root / "escape")})
            assert symlinked.status_code == 403, "a link out of an offered tree resolves outside it"
            sealed = await client.get("/api/folders", headers=HEADERS, params={"path": str(settings.state_dir)})
            assert sealed.status_code == 403
            unoffered = await client.get("/api/folders", headers=HEADERS, params={"path": "/usr"})
            assert unoffered.status_code == 403
            missing = await client.get("/api/folders", headers=HEADERS, params={"path": "/mnt/nowhere/photos"})
            assert missing.status_code == 404
            assert missing.json()["detail"] == {"code": "missing", "message": "/mnt/nowhere/photos does not exist here", "mount": True, "on_host": None}

            real_scandir = browser_module.os.scandir

            def refuse_scan(path: Any) -> Any:
                raise PermissionError(13, "permission denied", path)

            monkeypatch.setattr(browser_module.os, "scandir", refuse_scan)
            unreadable = await client.get("/api/folders", headers=HEADERS, params={"path": str(root)})
            assert unreadable.status_code == 403
            monkeypatch.setattr(browser_module.os, "scandir", real_scandir)

            listing = (await client.get("/api/folders", headers=HEADERS, params={"path": str(root)})).json()
            assert listing["truncated"] is True
            assert len(listing["entries"]) <= 250
            names = {entry["name"] for entry in listing["entries"]}
            assert not names & {"escape", ".hidden"}
            assert all({"name", "path", "mtime", "writable", "readable", "is_git", "kind", "project"} <= set(entry) for entry in listing["entries"])
            data = settings.state_dir.parent.resolve()
            assert listing["crumbs"][0]["path"] == str(data), "the breadcrumb starts at the widest tree that holds the folder"
            top = (await client.get("/api/folders", headers=HEADERS, params={"path": str(data)})).json()
            assert top["parent"] is None, "the listing does not climb above the tree it was offered from"

            ticks = iter((0.0, 1.0))
            monkeypatch.setattr(browser_module, "time", SimpleNamespace(time=system_time.time, monotonic=lambda: next(ticks, 1.0)))
            timed = (await client.get("/api/folders", headers=HEADERS, params={"path": str(root)})).json()
            assert timed["truncated"] is True and timed["entries"] == []
    finally:
        await manager.close()


async def test_the_folder_browser_names_owners_makes_folders_and_keeps_favourites(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = settings.workspaces_dir
    root.mkdir(parents=True, exist_ok=True)
    try:
        async with await _client(settings, config, db, manager) as client:
            kept = (await client.post("/api/projects", headers=HEADERS, json={"name": "Умный дом", "folder_name": "umnyj-dom"})).json()
            assert kept["folders"][0]["path"] == str(root / "umnyj-dom")
            assert kept["settings"]["ephemeral"] is False, "a project made on purpose is a project at once"
            chat = await manager.create_session("Разбор выписки")
            (root / "umnyj-dom" / ".git").mkdir()

            listing = (await client.get("/api/folders", headers=HEADERS, params={"path": str(root)})).json()
            rows = {entry["name"]: entry for entry in listing["entries"]}
            assert rows["umnyj-dom"]["kind"] == "folder" and rows["umnyj-dom"]["project"]["name"] == "Умный дом"
            assert rows["umnyj-dom"]["is_git"] is True
            scratch = rows[chat.project.primary.path.name]
            assert scratch["kind"] == "chat" and scratch["chat_title"] == "Разбор выписки"
            assert listing["recent"][0] == {"name": "umnyj-dom", "path": str(root / "umnyj-dom")}

            checked = (await client.get("/api/folders/check", headers=HEADERS, params={"path": str(root / "umnyj-dom")})).json()
            assert checked["exists"] and checked["is_git"] and checked["project"]["id"] == kept["id"]
            absent = (await client.get("/api/folders/check", headers=HEADERS, params={"path": "/mnt/nowhere"})).json()
            assert absent["exists"] is False and absent["mount"] is True and absent["project"] is None

            made = await client.post("/api/folders/mkdir", headers=HEADERS, json={"env": "container", "path": str(root), "name": "photo-archive"})
            assert made.status_code == 200 and (root / "photo-archive").is_dir()
            again = await client.post("/api/folders/mkdir", headers=HEADERS, json={"env": "container", "path": str(root), "name": "photo-archive"})
            assert again.status_code == 409
            for bad in ("../up", "a/b", ".secret", ""):
                refused = await client.post("/api/folders/mkdir", headers=HEADERS, json={"env": "container", "path": str(root), "name": bad})
                assert refused.status_code in (400, 422), bad
            host = await client.get("/api/folders", headers=HEADERS, params={"env": "host"})
            assert host.status_code == 503 and host.json()["detail"]["code"] == "host_down"

            pinned = (await client.put("/api/folders/favourites", headers=HEADERS, json={"env": "container", "path": str(root), "on": True})).json()
            assert pinned["favourites"] == [{"env": "container", "path": str(root), "name": root.name}]
            both = (await client.put("/api/folders/favourites", headers=HEADERS, json={"env": "host", "path": "/home/operator/projects", "on": True})).json()
            assert [row["env"] for row in both["favourites"]] == ["host", "container"], "the machine's favourites are kept beside the container's"
            assert (await client.get("/api/folders", headers=HEADERS, params={"path": str(root)})).json()["favourites"] == both["favourites"]
            await client.put("/api/folders/favourites", headers=HEADERS, json={"env": "host", "path": "/home/operator/projects", "on": False})
            unpinned = (await client.put("/api/folders/favourites", headers=HEADERS, json={"env": "container", "path": str(root), "on": False})).json()
            assert unpinned["favourites"] == []
    finally:
        await manager.close()


async def test_a_named_folder_is_made_once_and_never_adopted(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        async with await _client(settings, config, db, manager) as client:
            (settings.workspaces_dir / "taken").mkdir(parents=True)
            taken = await client.post("/api/projects", headers=HEADERS, json={"name": "Taken", "folder_name": "taken"})
            assert taken.status_code == 400 and "already exists" in taken.json()["detail"]
            for bad in ("..", "a/b", ".dot", "x" * 81):
                assert (await client.post("/api/projects", headers=HEADERS, json={"name": "Bad", "folder_name": bad})).status_code in (400, 422), bad
            both = await client.post("/api/projects", headers=HEADERS, json={"name": "Both", "folder_name": "both", "folders": [{"path": str(settings.workspaces_dir / "taken")}]})
            assert both.status_code == 400
            plain = (await client.post("/api/projects", headers=HEADERS, json={"name": "Plain", "snapshots": False})).json()
            assert plain["folders"][0]["path"] == str(settings.workspaces_dir / plain["id"]) and plain["settings"]["snapshots"] is False
    finally:
        await manager.close()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("creator_first", [False, True])
@pytest.mark.parametrize("delete_workspace", [False, True])
async def test_last_member_removes_automatic_project_but_keeps_files(settings: Settings, config: RuntimeConfig, db: Database, creator_first: bool, delete_workspace: bool, legacy: bool) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        creator = await manager.create_session("automatic")
        pid = creator.project.id
        if legacy:
            await db.execute("UPDATE projects SET settings = json_remove(settings, '$.ephemeral') WHERE id = ?", (pid,))
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
        assert await manager.projects.get(pid) is None
        assert creator.workspace not in manager.projects.roots
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
