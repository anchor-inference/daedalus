"""Showing a folder or a file in the operator's file manager.

The route exists for the operator at their own computer: a native installation, asked from the same
machine. These pin the three things that keep it from being anything else — it is not there off a
native installation or for a remote caller, it reveals nothing outside the folders a session or a
project owns, and the command it starts is the platform's own file manager with the file selected,
never the file itself. No command is really started: a fake runner records what would have been.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from daedalus.extensions import api_reveal
from daedalus.extensions.api import build_app
from daedalus.host.reveal import (
    RevealRefused,
    Target,
    confine,
    is_local_client,
    platform_family,
    reveal,
    reveal_command,
)
from daedalus.stores.database import Database
from tests.unit.test_selfdev_mode import _api_app, _config, _settings

# -- the path ------------------------------------------------------------------------------------


def test_a_path_is_confined_to_the_folders_it_was_asked_about(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "site").mkdir(parents=True)
    (workspace / "site" / "menu.html").write_text("<p>")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")

    assert confine("site/menu.html", [workspace]) == Target((workspace / "site" / "menu.html").resolve(), False)
    assert confine("", [workspace]) == Target(workspace.resolve(), True)
    assert confine(str(workspace / "site"), [workspace]).directory
    for escape in ("../outside/secret.txt", str(outside / "secret.txt"), "site/../../outside", "/"):
        with pytest.raises(RevealRefused):
            confine(escape, [workspace])
    # A sibling whose name starts like the root's is not inside it.
    (tmp_path / "workspace-2").mkdir()
    with pytest.raises(RevealRefused):
        confine(str(tmp_path / "workspace-2"), [workspace])
    with pytest.raises(RevealRefused, match="does not exist"):
        confine("site/gone.html", [workspace])
    with pytest.raises(RevealRefused):
        confine("site\x00/menu.html", [workspace])


def test_a_symlink_out_of_the_folder_is_outside_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, workspace / "link")
    with pytest.raises(RevealRefused):
        confine("link", [workspace])
    # A root reached through a symlink still contains its own files.
    os.symlink(workspace, tmp_path / "alias")
    (workspace / "a.txt").write_text("a")
    assert confine("a.txt", [tmp_path / "alias"]).path == (workspace / "a.txt").resolve()


def test_a_relative_path_is_taken_from_the_named_folder(tmp_path: Path) -> None:
    workspace, project = tmp_path / "ws", tmp_path / "project"
    (project / "src").mkdir(parents=True)
    workspace.mkdir()
    assert confine("src", [workspace, project], base=project).path == (project / "src").resolve()
    with pytest.raises(RevealRefused):
        confine("src", [workspace, project])


# -- the command ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "directory", "expected"),
    [
        ("windows", False, 'explorer.exe /select,"{path}"'),
        ("windows", True, 'explorer.exe "{path}"'),
        ("macos", False, ["open", "-R", "{path}"]),
        ("macos", True, ["open", "{path}"]),
        ("linux", False, ["xdg-open", "{parent}"]),
        ("linux", True, ["xdg-open", "{path}"]),
    ],
)
def test_each_platform_shows_the_file_in_its_own_file_manager(tmp_path: Path, platform: str, directory: bool, expected: object) -> None:
    path = tmp_path / ("folder" if directory else "notes.txt")
    started: list[object] = []
    reveal(Target(path, directory), platform=platform, runner=started.append)  # type: ignore[arg-type]
    fill = lambda text: text.format(path=path, parent=tmp_path)  # noqa: E731
    assert started == [fill(expected) if isinstance(expected, str) else [fill(part) for part in expected]]  # type: ignore[union-attr]


def test_a_windows_path_with_a_quote_is_refused_rather_than_split(tmp_path: Path) -> None:
    with pytest.raises(RevealRefused):
        reveal_command(Target(Path('C:/a" & calc "/b.txt'), False), "windows")


def test_the_platform_and_the_local_caller() -> None:
    assert platform_family("win32") == "windows" and platform_family("darwin") == "macos" and platform_family("linux") == "linux"
    for host in ("127.0.0.1", "127.8.0.1", "::1", "localhost"):
        assert is_local_client(host), host
    for host in ("192.168.1.20", "10.0.0.2", "203.0.113.9", "", None, "testclient", "::ffff:203.0.113.9"):
        assert not is_local_client(host), host


# -- the route -----------------------------------------------------------------------------------


class _Projects:
    local_env = "host"

    def __init__(self, projects: dict[str, object]) -> None:
        self.projects = projects

    async def get(self, project_id: str) -> object | None:
        return self.projects.get(project_id)


def _folder(id_: str, path: Path, env: str = "host") -> SimpleNamespace:
    return SimpleNamespace(id=id_, path=path, env=env, reachable=path.is_dir())


def _project(*folders: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        folders=folders,
        local_folders=lambda env: tuple(f for f in folders if f.env == env),
        folder=lambda folder_id: next((f for f in folders if f.id == folder_id), None),
    )


def _api(tmp_path: Path, *, native: bool) -> tuple[FastAPI, list[object], Path, Path]:
    workspace = tmp_path / "ws"
    (workspace / "site").mkdir(parents=True)
    (workspace / "site" / "menu.html").write_text("<p>")
    project_dir = tmp_path / "project"
    (project_dir / "src").mkdir(parents=True)
    (project_dir / "src" / "main.py").write_text("print()")
    container = _folder("f-container", tmp_path / "elsewhere", env="container")
    project = _project(_folder("f1", project_dir), container)
    states = {"s1": SimpleNamespace(workspace=workspace, services=None, project=project)}

    async def get_state(session_id: str) -> object | None:
        return states.get(session_id)

    manager = SimpleNamespace(get_state=get_state, projects=_Projects({"p1": project}))
    started: list[object] = []
    api = FastAPI()
    app = SimpleNamespace(manager=manager, settings=SimpleNamespace(native=native))
    api_reveal.register(api, app, lambda: {"via": "token"}, runner=started.append, platform="linux")  # type: ignore[arg-type]
    return api, started, workspace, project_dir


async def _post(api: FastAPI, body: dict[str, object], client: tuple[str, int] = ("127.0.0.1", 50000)) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api, client=client), base_url="http://test") as http:
        return await http.post("/api/reveal", json=body)


async def test_the_route_reveals_a_session_file_and_a_project_folder(tmp_path: Path) -> None:
    api, started, workspace, project_dir = _api(tmp_path, native=True)
    answer = await _post(api, {"session_id": "s1", "path": "site/menu.html"})
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"path": str((workspace / "site" / "menu.html").resolve()), "directory": False, "opened": True, "platform": "linux"}
    assert started == [["xdg-open", str((workspace / "site").resolve())]]

    # The workspace itself, a file in another folder of the session's project, a project's folder.
    assert (await _post(api, {"session_id": "s1"})).json()["directory"] is True
    assert (await _post(api, {"session_id": "s1", "folder_id": "f1", "path": "src/main.py"})).status_code == 200
    assert (await _post(api, {"session_id": "s1", "path": str(project_dir / "src" / "main.py")})).status_code == 200
    folder = await _post(api, {"project_id": "p1"})
    assert folder.status_code == 200 and folder.json()["path"] == str(project_dir.resolve())
    assert started[-1] == ["xdg-open", str(project_dir.resolve())]


async def test_the_desktop_window_gets_the_checked_path_and_opens_it_itself(tmp_path: Path) -> None:
    api, started, workspace, _ = _api(tmp_path, native=True)
    answer = await _post(api, {"session_id": "s1", "path": "site/menu.html", "run": False})
    assert answer.status_code == 200 and answer.json()["opened"] is False
    assert answer.json()["path"] == str((workspace / "site" / "menu.html").resolve())
    assert started == []


async def test_the_route_refuses_what_it_must_not_show(tmp_path: Path) -> None:
    api, started, _, _ = _api(tmp_path, native=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    for body, status in [
        ({"session_id": "s1", "path": "../outside.txt"}, 403),
        ({"session_id": "s1", "path": str(outside)}, 403),
        ({"session_id": "s1", "path": "site/gone.html"}, 403),
        ({"session_id": "nope"}, 404),
        ({"project_id": "nope"}, 404),
        # A folder of the container environment is not a folder on this machine.
        ({"project_id": "p1", "folder_id": "f-container"}, 404),
        ({"session_id": "s1", "folder_id": "f-container"}, 404),
        ({"session_id": "s1", "project_id": "p1"}, 400),
        ({}, 400),
    ]:
        answer = await _post(api, body)
        assert answer.status_code == status, (body, answer.text)
    assert started == []


async def test_the_route_is_not_there_off_a_native_installation_or_for_a_remote_caller(tmp_path: Path) -> None:
    server, started, _, _ = _api(tmp_path / "server", native=False)
    assert (await _post(server, {"session_id": "s1"})).status_code == 404
    native, started_native, _, _ = _api(tmp_path / "native", native=True)
    remote = await _post(native, {"session_id": "s1"}, client=("192.168.1.20", 50000))
    assert remote.status_code == 403
    assert started == [] and started_native == []


@pytest.mark.parametrize(("native", "client", "available"), [(False, "127.0.0.1", False), (True, "127.0.0.1", True), (True, "192.168.1.20", False)])
async def test_the_capabilities_say_whether_to_offer_it(tmp_path: Path, db: Database, native: bool, client: str, available: bool) -> None:
    api = build_app(_api_app(_settings(tmp_path, native=native), _config("auto"), db), "tok")  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api, client=(client, 50000)), base_url="http://test") as http:  # type: ignore[arg-type]
        body = (await http.get("/api/capabilities", headers={"X-Daedalus-Token": "tok"})).json()
    assert body["reveal"] == {"available": available, "platform": platform_family()}
