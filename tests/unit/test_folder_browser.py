"""The folder browser's host side: the machine read through its terminal daemon, and every way that fails."""

from __future__ import annotations

from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api_folder_browser import crumbs
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec
from daedalus.terminals.bridge import HostDaemonOutdated
from tests.unit.test_project_unification import HEADERS, _client


class FakeBridge:
    """The bridge's browsing calls over a dictionary of host folders, or failing as told."""

    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.made: list[tuple[str, str]] = []
        self.folders = {
            "/home/operator": ["projects"],
            "/home/operator/projects": ["esp32-door", "smart-home"],
            "/home/operator/projects/esp32-door": [],
            "/home/operator/projects/smart-home": [],
        }

    def available(self) -> bool:
        return self.failure is None

    async def browse(self, path: str, *, hidden: bool = False, limit: int = 500) -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        path = path or "/home/operator"
        if path not in self.folders:
            raise FileNotFoundError(f"not found: {path}")
        entries = [{"name": name, "path": f"{path}/{name}", "mtime": "2026-10-08T10:00:00.123456789Z", "writable": True,
                    "readable": True, "is_git": name == "esp32-door", "link": False} for name in self.folders[path]]
        return {"path": path, "parent": path.rsplit("/", 1)[0] or "/", "home": "/home/operator", "writable": True,
                "is_git": path.endswith("/esp32-door"), "entries": entries[:limit], "truncated": len(entries) > limit,
                "places": [{"name": "operator", "path": "/home/operator", "kind": "home"}, {"name": "/", "path": "/", "kind": "volume"}]}

    async def stat_folder(self, path: str) -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        return {"exists": path in self.folders, "type": "dir" if path in self.folders else ""}

    async def mkdir(self, path: str, *, actor: str = "operator") -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        created = path not in self.folders
        self.folders.setdefault(path, [])
        self.made.append((path, actor))
        return {"exists": True, "type": "dir", "created": created}


@pytest.fixture
async def manager(settings: Settings, config: RuntimeConfig, db: Database) -> Any:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        yield manager
    finally:
        await manager.close()


async def test_the_host_side_lists_names_owners_and_makes_folders(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> None:
    bridge = FakeBridge()
    manager.host_bridge = bridge  # type: ignore[assignment]
    owned = await manager.projects.create("Умный дом", [FolderSpec("/home/operator/projects/smart-home", env="host")])
    async with await _client(settings, config, db, manager) as client:
        listing = (await client.get("/api/folders", headers=HEADERS, params={"env": "host", "path": "/home/operator/projects"})).json()
        assert listing["env"] == "host"
        assert [c["name"] for c in listing["crumbs"]] == ["~", "projects"]
        rows = {row["name"]: row for row in listing["entries"]}
        assert rows["esp32-door"]["is_git"] is True and rows["esp32-door"]["project"] is None
        assert rows["smart-home"]["project"] == {"id": owned.id, "name": "Умный дом"}
        assert isinstance(rows["esp32-door"]["mtime"], float) and rows["esp32-door"]["mtime"] > 0
        assert listing["roots"][0]["kind"] == "home"
        assert listing["recent"] == [{"name": "smart-home", "path": "/home/operator/projects/smart-home"}]

        checked = (await client.get("/api/folders/check", headers=HEADERS, params={"env": "host", "path": "/home/operator/projects/esp32-door"})).json()
        assert checked["exists"] and checked["is_git"] and checked["project"] is None
        absent = (await client.get("/api/folders/check", headers=HEADERS, params={"env": "host", "path": "/home/operator/nowhere"})).json()
        assert absent["exists"] is False

        made = await client.post("/api/folders/mkdir", headers=HEADERS, json={"env": "host", "path": "/home/operator/projects", "name": "thesis"})
        assert made.status_code == 200 and made.json()["path"] == "/home/operator/projects/thesis"
        assert bridge.made == [("/home/operator/projects/thesis", "operator")]
        again = await client.post("/api/folders/mkdir", headers=HEADERS, json={"env": "host", "path": "/home/operator/projects", "name": "thesis"})
        assert again.status_code == 409

        missing = await client.get("/api/folders", headers=HEADERS, params={"env": "host", "path": "/home/operator/gone"})
        assert missing.status_code == 404 and missing.json()["detail"]["code"] == "missing"


@pytest.mark.parametrize(("failure", "status", "code"), [
    (ConnectionError("the host terminal bridge is not available"), 503, "host_down"),
    (HostDaemonOutdated("the host terminal daemon is older than this version and cannot browse folders"), 501, "host_outdated"),
    (OSError("/home/operator/.ssh is on the deny list"), 403, "refused"),
])
async def test_each_host_failure_has_its_own_answer(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager,
                                                    failure: BaseException, status: int, code: str) -> None:
    manager.host_bridge = FakeBridge(failure)  # type: ignore[assignment]
    async with await _client(settings, config, db, manager) as client:
        response = await client.get("/api/folders", headers=HEADERS, params={"env": "host"})
        assert response.status_code == status
        detail = response.json()["detail"]
        assert detail["code"] == code
        if code == "host_down":
            assert detail["start"] and detail["install"], "a stopped bridge comes with the commands that start and install it"
        if code == "host_outdated":
            assert "install" in detail


async def test_a_container_folder_missing_here_asks_the_host_whether_it_is_there(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> None:
    manager.host_bridge = FakeBridge()  # type: ignore[assignment]
    async with await _client(settings, config, db, manager) as client:
        response = await client.get("/api/folders", headers=HEADERS, params={"path": "/home/operator/projects"})
        assert response.status_code == 404
        assert response.json()["detail"] == {"code": "missing", "message": "/home/operator/projects does not exist here", "mount": True, "on_host": True}
        checked = (await client.get("/api/folders/check", headers=HEADERS, params={"path": "/home/operator/projects"})).json()
        assert checked["exists"] is False and checked["mount"] is True and checked["on_host"] is True


def test_the_breadcrumb_starts_at_home_an_anchor_or_the_root() -> None:
    assert crumbs("/home/operator/projects/x", "/home/operator") == [
        {"name": "~", "path": "/home/operator"}, {"name": "projects", "path": "/home/operator/projects"},
        {"name": "x", "path": "/home/operator/projects/x"}]
    assert [c["name"] for c in crumbs("/srv/projects", "/root")] == ["/", "srv", "projects"]
    assert [c["name"] for c in crumbs("/data/workspaces/a", "/root", "/data")] == ["data", "workspaces", "a"]
    assert [c["path"] for c in crumbs("D:\\work\\site", "C:\\Users\\operator")] == ["D:\\", "D:\\work", "D:\\work\\site"]
