"""The terminals routes as the app calls them, over a real session manager and an in-process daemon."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import terminals as terminals_extension
from daedalus.extensions.api import build_app
from daedalus.host.policy import DENY
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec
from daedalus.terminals import endpoint
from daedalus.terminals.model import Owner
from daedalus.terminals.owners import ManagerOwners
from daedalus.terminals.service import Terminals
from tests.support.fake_ptyd import FakePtyd

H = {"X-Daedalus-Token": "tok"}


@pytest.fixture
def run_dir() -> Iterable[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))
    yield path / "run"
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def terminal_settings(settings: Settings, run_dir: Path) -> Settings:
    return settings.model_copy(update={"terminals_container_dir": run_dir, "services_public_host": "192.0.2.1"})


@pytest.fixture
async def daemon(run_dir: Path) -> AsyncIterator[FakePtyd]:
    fake = await FakePtyd(run_dir).start()
    yield fake
    await fake.stop()


@pytest.fixture
async def manager(terminal_settings: Settings, db: Database) -> AsyncIterator[SessionManager]:
    made = SessionManager(terminal_settings, RuntimeConfig(), db=db)
    await made.start()
    yield made
    await made.close()


@pytest.fixture
async def app(terminal_settings: Settings, db: Database, manager: SessionManager, daemon: FakePtyd) -> AsyncIterator[Any]:
    application = SimpleNamespace(settings=terminal_settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    tasks = await terminals_extension.install(application)  # type: ignore[arg-type]
    assert await application.extensions["terminals"].wait_available("container")
    yield application
    for task in tasks:
        task.cancel()
    await application.extensions["terminals"].close()


@pytest.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        yield c


async def test_the_listing_names_both_environments_and_the_cap(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/terminals", headers=H)).json()
    container, host = body["envs"]
    assert container["env"] == "container" and container["available"] and container["port_range"] == "8120-8139"
    assert container["public_host"] == "192.0.2.1" and container["preview_poll_ms"] == 3000 and container["version"] == "fake"
    assert host["env"] == "host" and not host["available"] and host["reason"] == "not_configured"
    assert body["terminals"] == [] and body["capacity"] == {"running": 0, "cap": 20, "queued": 0}
    assert (await client.get("/api/terminals")).status_code == 401


async def test_a_sessions_terminal_starts_in_its_workspace_and_ends_with_it(client: httpx.AsyncClient, manager: SessionManager, db: Database) -> None:
    state = await manager.create_session("Checkout page")
    sid = state.session.id
    made = await client.post("/api/terminals", json={"env": "container", "owner_kind": "session", "owner_id": sid}, headers=H)
    assert made.status_code == 201, made.text
    view = made.json()
    assert view["owner"] == {"kind": "session", "id": sid, "label": "Checkout page"}
    assert view["cwd"] == str(state.workspace) and view["project_id"] == state.project.id and view["cwd_fallback"] is False  # type: ignore[union-attr]
    assert view["title"].startswith("bash · ")
    listing = (await client.get("/api/sessions", headers=H)).json()
    assert next(s for s in listing["sessions"] if s["id"] == sid)["terminals"] == 1
    deleted = (await client.delete(f"/api/sessions/{sid}", headers=H)).json()
    assert deleted == {"deleted": True, "terminals_ended": 1}
    row = await db.fetchone("SELECT status FROM terminals WHERE id = ?", (view["id"],))
    assert row["status"] == "exited"


async def test_deleting_a_session_through_the_manager_ends_its_terminals_too(app: Any, manager: SessionManager, db: Database) -> None:
    state = await manager.create_session("elsewhere")
    service: Terminals = app.extensions["terminals"]
    from daedalus.terminals.model import TerminalSpec  # noqa: PLC0415 — only this test builds a spec by hand

    view = await service.create(TerminalSpec(env="container", owner=Owner("session", state.session.id)))
    assert await manager.delete_session(state.session.id)
    row = await db.fetchone("SELECT status FROM terminals WHERE id = ?", (view["id"],))
    assert row["status"] == "exited"


async def test_deleting_a_project_ends_its_own_terminals(client: httpx.AsyncClient, manager: SessionManager, db: Database, tmp_path: Path) -> None:
    folder = tmp_path / "shop"
    folder.mkdir()
    project = await manager.projects.create("Shop", [FolderSpec(str(folder))])
    view = (await client.post("/api/terminals", json={"env": "container", "owner_kind": "project", "owner_id": project.id}, headers=H)).json()
    assert view["cwd"] == str(folder) and view["owner"]["label"] == "Shop"
    assert (await client.delete(f"/api/projects/{project.id}", headers=H)).status_code == 200
    row = await db.fetchone("SELECT status FROM terminals WHERE id = ?", (view["id"],))
    assert row["status"] == "exited"


async def test_the_cap_asks_the_operator_and_confirming_goes_past_it(client: httpx.AsyncClient, app: Any) -> None:
    app.config.terminals.running_cap = 1
    body = {"env": "container", "owner_kind": "free", "cwd": "/tmp"}
    assert (await client.post("/api/terminals", json=body, headers=H)).status_code == 201
    refused = await client.post("/api/terminals", json=body, headers=H)
    assert refused.status_code == 409
    assert refused.json()["code"] == "over_cap" and refused.json()["running"] == 1 and refused.json()["cap"] == 1 and "confirm" in refused.json()["detail"]
    assert (await client.post("/api/terminals", json={**body, "confirm": True}, headers=H)).status_code == 201
    listing = (await client.get("/api/terminals", headers=H)).json()
    assert listing["capacity"]["running"] == 2 and listing["envs"][0]["running"] == 2


async def test_a_terminals_life_through_the_routes(client: httpx.AsyncClient, daemon: FakePtyd) -> None:
    view = (await client.post("/api/terminals", json={"env": "container", "owner_kind": "free", "cwd": "/tmp", "title": "psql"}, headers=H)).json()
    tid = view["id"]
    assert view["title"] == "psql" and view["live"]["keyboard"] == {"owner": "auto", "until": None}
    assert (await client.patch(f"/api/terminals/{tid}", json={"title": "db"}, headers=H)).json()["title"] == "db"
    assert (await client.post(f"/api/terminals/{tid}/signal", json={"signal": "INT"}, headers=H)).json() == {"ok": True}
    assert daemon.terminals[tid].signals == ["INT"]
    assert (await client.delete(f"/api/terminals/{tid}", headers=H)).status_code == 409
    restarted = (await client.post(f"/api/terminals/{tid}/restart", headers=H)).json()
    assert restarted["id"] != tid and restarted["title"] == "db" and restarted["cwd"] == "/tmp"
    old = (await client.get(f"/api/terminals/{tid}", headers=H)).json()
    assert old["status"] == "exited"
    ended = (await client.post(f"/api/terminals/{restarted['id']}/kill", headers=H)).json()
    assert ended["status"] == "exited"
    assert (await client.delete(f"/api/terminals/{tid}", headers=H)).status_code == 200
    audit = (await client.get(f"/api/terminals/{tid}/audit", headers=H)).json()["entries"]
    assert [e["action"] for e in audit] == ["remove", "restart", "kill", "signal", "update", "create"]
    assert audit[1]["detail"] == {"new_id": restarted["id"]}
    assert (await client.get("/api/terminals/nope00000000/audit", headers=H)).status_code == 404


async def test_the_commands_route_answers_for_a_shell_and_refuses_for_a_program(client: httpx.AsyncClient, daemon: FakePtyd) -> None:
    view = (await client.post("/api/terminals", json={"env": "container", "owner_kind": "free", "cwd": "/tmp"}, headers=H)).json()
    refused = await client.get(f"/api/terminals/{view['id']}/commands", headers=H)
    assert refused.status_code == 501 and refused.json()["code"] == "unsupported"
    daemon.terminals[view["id"]].commands = [{"n": n, "command": f"step {n}", "exit_code": 0} for n in range(1, 4)]
    answer = await client.get(f"/api/terminals/{view['id']}/commands?last=2&output=1", headers=H)
    assert answer.status_code == 200 and [c["n"] for c in answer.json()["commands"]] == [2, 3]
    assert (await client.get(f"/api/terminals/{view['id']}/commands?last=0", headers=H)).status_code == 422
    assert (await client.get("/api/terminals/nope00000000/commands", headers=H)).status_code == 404


async def test_refusals_carry_their_status(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/terminals/nope00000000", headers=H)).status_code == 404
    assert (await client.post("/api/terminals/nope00000000/kill", headers=H)).status_code == 404
    host = await client.post("/api/terminals", json={"env": "host", "owner_kind": "free"}, headers=H)
    assert host.status_code == 503 and host.json()["code"] == "unavailable"
    assert (await client.post("/api/terminals", json={"env": "container", "owner_kind": "free", "owner_id": "x"}, headers=H)).status_code == 400
    assert (await client.post("/api/terminals", json={"env": "container", "owner_kind": "session", "owner_id": "missing"}, headers=H)).status_code == 404
    assert (await client.post("/api/terminals", json={"env": "container", "owner_kind": "free", "cwd": "relative"}, headers=H)).status_code == 400
    assert (await client.post("/api/terminals", json={"env": "moon", "owner_kind": "free"}, headers=H)).status_code == 422
    view = (await client.post("/api/terminals", json={"env": "container", "owner_kind": "free", "cwd": "/tmp"}, headers=H)).json()
    # The screen is the emulator's, which this daemon does not have yet: said, not faked.
    screen = await client.get(f"/api/terminals/{view['id']}/screen", headers=H)
    assert screen.status_code == 501 and screen.json()["code"] == "unsupported"


async def test_the_load_route_projects_a_cap(client: httpx.AsyncClient) -> None:
    await client.post("/api/terminals", json={"env": "container", "owner_kind": "free", "cwd": "/tmp"}, headers=H)
    load = (await client.get("/api/terminals/load", params={"cap": 10}, headers=H)).json()
    assert load["running"] == 1 and load["cap"] == 20 and load["queued"] == []
    # The terminal's process and the daemon that holds its emulator are both what it costs now.
    assert load["used"]["rss_bytes"] == (50 << 20) + (30 << 20) and load["used"]["daemon_rss_bytes"] == 30 << 20
    assert load["used"]["mem_total_bytes"] == 16 << 30 and load["used"]["cpus"] == 8
    assert load["likely"]["basis"] == "default" and load["projection"]["cap"] == 10
    assert load["projection"]["terminals_rss_bytes"] == (80 << 20) + 9 * load["likely"]["rss_bytes"]
    assert load["thresholds"] == {"warn": 70.0, "bad": 90.0}
    assert (await client.get("/api/terminals/load", params={"cap": 0}, headers=H)).status_code == 422


async def test_the_run_directory_is_sealed_from_the_agent(manager: SessionManager, run_dir: Path) -> None:
    assert run_dir in manager.settings.sealed_paths and run_dir in manager.protected_paths()
    policy = manager.policy()
    assert policy.evaluate("Exec", {"command": f"cat {run_dir}/token"}).action == DENY


async def test_a_daemons_state_directory_joins_the_sealed_set(manager: SessionManager, run_dir: Path, tmp_path: Path) -> None:
    # Reported by the daemon in daemon.info: its launches' overlay files and dial sockets live there.
    state = tmp_path / "ptyd-state"
    endpoint.remember_state_dir(run_dir, str(state))
    try:
        assert str(state) in manager.policy().sealed
    finally:
        endpoint.remember_state_dir(run_dir, "")
    assert str(state) not in manager.policy().sealed


async def test_a_host_terminal_of_a_container_session_starts_in_a_host_folder_or_at_home(manager: SessionManager, tmp_path: Path) -> None:
    state = await manager.create_session("here")
    owners = ManagerOwners(manager)
    owner = Owner("session", state.session.id)
    assert await owners.default_cwd("container", owner, state.project.id) == str(state.workspace)  # type: ignore[union-attr]
    # The container's paths mean nothing on the host: with no host folder in the project, home.
    assert await owners.default_cwd("host", owner, state.project.id) is None  # type: ignore[union-attr]
    (tmp_path / "mixed").mkdir()
    project = await manager.projects.create("Mixed", [FolderSpec(str(tmp_path / "mixed")), FolderSpec("/home/someone/code", env="host")])
    assert await owners.default_cwd("host", Owner("project", project.id), project.id) == "/home/someone/code"
    assert await owners.sandbox_writable("container", owner, state.project.id, "") == [str(p) for p in state.services.sandbox_writable()]  # type: ignore[union-attr]


async def test_a_sandboxed_terminal_writes_what_its_owner_may(manager: SessionManager, tmp_path: Path) -> None:
    owners = ManagerOwners(manager)
    # A session here: exactly what its agent's own sandbox may write.
    state = await manager.create_session("boxed")
    session = Owner("session", state.session.id)
    assert await owners.sandbox_writable("container", session, state.project.id, "/elsewhere") == [str(p) for p in state.services.sandbox_writable()]  # type: ignore[union-attr]
    # A project: its writable folders of that environment, never a read-only one or another environment's.
    for name in ("code", "docs"):
        (tmp_path / name).mkdir()
    project = await manager.projects.create("Walls", [FolderSpec(str(tmp_path / "code")), FolderSpec(str(tmp_path / "docs"), readonly=True), FolderSpec("/home/someone/code", env="host")])
    assert await owners.sandbox_writable("container", Owner("project", project.id), project.id, str(tmp_path)) == [str(tmp_path / "code")]
    assert await owners.sandbox_writable("host", Owner("project", project.id), project.id, "/home/someone") == ["/home/someone/code"]
    # A project with nothing writable there, and a free terminal: only the directory it starts in.
    (tmp_path / "docs2").mkdir()
    only_docs = await manager.projects.create("Docs", [FolderSpec(str(tmp_path / "docs2"), readonly=True)])
    assert await owners.sandbox_writable("container", Owner("project", only_docs.id), only_docs.id, "/start") == ["/start"]
    assert await owners.sandbox_writable("container", Owner("free"), None, "/start") == ["/start"]
    assert await owners.sandbox_writable("container", Owner("free"), None, "") == []


async def test_a_staff_sandbox_writes_only_the_active_task_workspace(manager: SessionManager, tmp_path: Path) -> None:
    owners = ManagerOwners(manager)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    project = await manager.projects.create("Two folders", [FolderSpec(str(first)), FolderSpec(str(second))])
    folder = project.folders[0]
    member = await manager.staff.hire(project.id, name="Worker", harness="claude", isolation="shared")
    owner = Owner("staff", member.id)
    with pytest.raises(ValueError, match="current task workspace"):
        await owners.sandbox_writable("container", owner, project.id, str(first))
    session = await manager.staff.claim_session(member.id, kind="cli", folder_id=folder.id, launch_cwd=str(first))
    assert await owners.sandbox_writable("container", owner, project.id, str(first)) == [str(first)]
    with pytest.raises(ValueError, match="current task workspace"):
        await owners.sandbox_writable("container", owner, project.id, str(second))
    await manager.staff.end_session(session.id, "done")

    reader = await manager.staff.hire(project.id, name="Reader", harness="claude", isolation="readonly")
    await manager.staff.claim_session(reader.id, kind="cli", folder_id=folder.id, launch_cwd=str(first))
    assert await owners.sandbox_writable("container", Owner("staff", reader.id), project.id, str(first)) == []

    tree = tmp_path / "tree"
    tree.mkdir()
    await manager.db.execute("UPDATE project_folders SET is_git = 1 WHERE id = ?", (folder.id,))
    worktree_member = await manager.staff.hire(project.id, name="Branch", harness="claude", isolation="worktree")
    await manager.staff.claim_session(worktree_member.id, kind="cli", folder_id=folder.id,
                                      launch_cwd=str(tree / "src"), worktree_path=str(tree))
    assert await owners.sandbox_writable("container", Owner("staff", worktree_member.id), project.id,
                                         str(tree / "src")) == [str(tree)]


async def test_the_doctor_reports_each_environment(terminal_settings: Settings, app: Any, run_dir: Path, tmp_path: Path) -> None:
    from daedalus.doctor import DoctorContext, _terminals  # noqa: PLC0415 — the probe alone, not the whole doctor

    live = await _terminals(DoctorContext(settings=terminal_settings, config=app.config, extensions=app.extensions))
    # The fixture has a bot and no public address, which is what the last check warns about.
    assert [(c.name, c.ok) for c in live] == [("terminals (container)", True), ("terminal sandbox (container)", False), ("terminals in Telegram", False)]
    assert live[0].message == "ptyd fake (protocol 1), 0 running"
    # Without a running application the doctor connects on its own; an empty host directory is
    # "not installed", which on the host is information rather than a warning.
    empty = tmp_path / "host-terminals"
    empty.mkdir()
    alone = await _terminals(DoctorContext(settings=terminal_settings.model_copy(update={"terminals_host_dir": empty}), config=app.config))
    by_name = {c.name: c for c in alone}
    assert by_name["terminals (container)"].ok
    host = by_name["terminals (host)"]
    assert not host.ok and host.severity == "info" and host.message.startswith("not installed") and "setup.sh" in host.fix_hint
