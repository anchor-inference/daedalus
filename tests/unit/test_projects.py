"""Projects: the folder is the wall.

A session in a project reads and writes inside its root and nowhere else — through the tools and
through the API. Every session belongs to a project; a private directory is a child inside it.
The rest is the store, the migration, and the paragraph the agent is told.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host import prompts
from daedalus.host.services import PathOutsideProject, SessionServices, Walls, locator
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.projects import ProjectError, ProjectSettings, ProjectStore, normalise_root

HEADERS = {"X-Daedalus-Token": "tok"}


# -- the store and its migration -------------------------------------------------------------


async def test_the_migration_makes_project_membership_required(db: Database) -> None:
    columns = {r[1]: r for r in await db.fetchall("PRAGMA table_info(sessions)")}
    assert "project_id" in columns
    assert columns["project_id"][3] == 1
    tables = {r[0] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "projects" in tables


async def test_a_project_round_trips_and_the_link_to_sessions_is_the_column(db: Database, tmp_path: Path) -> None:
    store = ProjectStore(db)
    root = tmp_path / "site"
    root.mkdir()
    project = await store.create("Bakery site", [str(root)], settings=ProjectSettings(snapshots=True))
    assert project.primary.path == root and project.settings.snapshots is True and project.primary.reachable is True

    await db.execute("INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, project_id) VALUES ('s1', 't', 'one', '', '', ?)", (project.id,))
    assert (await store.for_session("s1")).id == project.id
    assert await store.sessions_of(project.id) == [{"id": "s1", "title": "one"}]
    assert await store.by_session() == {"s1": project.id}

    moved = await store.update(project.id, name="Bakery")
    assert moved.name == "Bakery" and moved.primary.path == root and moved.settings.snapshots is True

    with pytest.raises(ProjectError, match="still has sessions"):
        await store.delete(project.id)
    await db.execute("DELETE FROM sessions WHERE id = 's1'")
    await store.delete(project.id)
    assert root.is_dir()


async def test_a_folder_that_is_not_one_and_a_folder_already_spoken_for_are_refused(db: Database, tmp_path: Path) -> None:
    store = ProjectStore(db)
    with pytest.raises(ProjectError, match="absolute"):
        await store.create("rel", ["some/where"])
    with pytest.raises(ProjectError, match="needs a name"):
        await store.create("  ", [str(tmp_path)])
    (tmp_path / "a-file").write_text("x")
    with pytest.raises(ProjectError, match="a file"):
        await store.create("file", [str(tmp_path / "a-file")])

    outer = tmp_path / "outer"
    (outer / "inner").mkdir(parents=True)
    await store.create("Outer", [str(outer)])
    # Nesting is refused in both directions: a boundary that holds one way is not a boundary.
    with pytest.raises(ProjectError, match="inside the project"):
        await store.create("Inner", [str(outer / "inner")])
    with pytest.raises(ProjectError, match="contains the project"):
        await store.create("Parent", [str(tmp_path)])
    # The same folder is a place two projects may both work on: equal is not nesting.
    again = await store.create("Again", [str(outer)])
    assert [str(f.path) for f in again.folders] == [str(outer)]


def test_a_root_is_normalised_without_touching_the_filesystem() -> None:
    """A project may be added in a container before its folder is mounted, so there is nothing to
    resolve against — and nothing that should refuse the row."""
    assert normalise_root("/srv/nope/../work/") == Path("/srv/work")
    assert normalise_root("/does/not/exist/yet") == Path("/does/not/exist/yet")
    with pytest.raises(ProjectError, match="filesystem root"):
        normalise_root("/")


def test_the_kernels_own_filesystems_are_not_folders_of_work() -> None:
    """``/proc`` has no files in it to edit, and a listing of it is a listing of the machine."""
    for raw in ("/proc", "/proc/1", "/sys", "/sys/class/net", "/dev", "/dev/shm/work", "/run", "/run/user/1000"):
        with pytest.raises(ProjectError, match="kernel"):
            normalise_root(raw)
    # Nothing that merely reads like one: the refusal is by mount point, not by name.
    assert normalise_root("/srv/proc") == Path("/srv/proc")


# -- containment ------------------------------------------------------------------------------


def _services(root: Path, session_id: str = "p-contain") -> SessionServices:
    return SessionServices(session_id=session_id, workspace_dir=root, walls=Walls(readable=(root,), writable=(root,)))


def test_resolve_refuses_every_way_out_of_the_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not yours")
    services = _services(root)

    assert services.resolve("src/main.py") == root / "src" / "main.py"
    assert services.resolve(None) == root
    assert services.resolve(str(root / "src")) == root / "src"

    for attempt in ("../outside/secret.txt", str(outside / "secret.txt"), "src/../../outside", "/etc/passwd"):
        with pytest.raises(PathOutsideProject):
            services.resolve(attempt)

    # A symlink is not a loophole: the check is made on the real path, so a link pointing out of the
    # tree is refused exactly as the path it points at would be.
    (root / "escape").symlink_to(outside)
    with pytest.raises(PathOutsideProject):
        services.resolve("escape/secret.txt")

    # And a path that does not exist yet is judged by where it would land, not by whether it is there.
    assert services.resolve("src/new.py") == root / "src" / "new.py"
    with pytest.raises(PathOutsideProject):
        services.resolve("../new.py")


def test_a_session_without_a_project_resolves_anywhere_as_it_always_did(tmp_path: Path) -> None:
    services = SessionServices(session_id="plain", workspace_dir=tmp_path / "ws")
    assert services.resolve("/etc/hostname") == Path("/etc/hostname")
    assert services.resolve("../up") == tmp_path / "ws" / ".." / "up"


def test_the_worktrees_the_host_opened_stay_reachable_inside_a_project(tmp_path: Path) -> None:
    """A project contains the agent's own work, not the host's: a worktree the host deliberately
    opened for this session is still resolvable, or self-development would stop inside a project."""
    root = tmp_path / "project"
    root.mkdir()
    worktree = tmp_path / "worktrees" / "fix"
    worktree.mkdir(parents=True)
    services = _services(root)
    with pytest.raises(PathOutsideProject):
        services.resolve(str(worktree / "file.py"))
    services.writable.append(worktree)
    assert services.resolve(str(worktree / "file.py")) == worktree / "file.py"


async def test_the_file_tools_refuse_a_path_outside_the_project(tmp_path: Path) -> None:
    from daedalus.tools.files import find_files, read_file, write_file

    root = tmp_path / "project"
    root.mkdir()
    (root / "ok.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not yours", encoding="utf-8")

    locator.register(_services(root, "p-tools"))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="p-tools", metadata={"tool_call_id": "c"})
    try:
        assert "inside" in (await read_file().invoke(ctx, {"path": "ok.txt"})).content
        # Nothing outside the root is read, written or listed, and the tool gives the agent a useful refusal.
        for tool, args in (
            (read_file, {"path": str(outside / "secret.txt")}),
            (write_file, {"path": "../outside/planted.txt", "content": "x"}),
            (find_files, {"pattern": "*.txt", "path": str(outside)}),
        ):
            result = await tool().invoke(ctx, args)
            assert result.is_error and "outside this project" in result.content
        assert not (outside / "planted.txt").exists()
    finally:
        locator.unregister("p-tools")


# -- the API ------------------------------------------------------------------------------------


def _app(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> Any:
    async def create_session(title: str, *, metadata: dict[str, Any] | None = None, project_id: str | None = None, workspace: Path | None = None, own_directory: bool = False) -> Any:
        return await manager.create_session(title, metadata=metadata, project_id=project_id, workspace=workspace, own_directory=own_directory)

    return SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=create_session)


async def _client(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> httpx.AsyncClient:
    api = build_app(_app(settings, config, db, manager), "tok")  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")  # type: ignore[arg-type]


async def test_project_crud_and_a_session_that_works_in_one(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "bakery"
    (root / "menu").mkdir(parents=True)
    (root / "menu" / "items.json").write_text("[]", encoding="utf-8")
    try:
        async with await _client(settings, config, db, manager) as client:
            assert (await client.get("/api/projects")).status_code == 401  # the folders the operator works in; not to anyone

            created = await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(root)}]})
            assert created.status_code == 200
            project = created.json()
            assert [(f["path"], f["reachable"], f["env"], f["position"]) for f in project["folders"]] == [(str(root), True, "container", 0)]
            assert "root" not in project
            assert project["settings"]["snapshots"] is False and project["settings"]["system"] == "" and project["settings"]["ephemeral"] is False
            assert project["settings"]["orchestrator"]["enabled"] is False and project["settings"]["orchestrator"]["concurrency"] == 6

            listing = (await client.get("/api/projects", headers=HEADERS)).json()
            assert [p["id"] for p in listing] == [project["id"]] and listing[0]["sessions"] == []

            bad = await client.post("/api/projects", headers=HEADERS, json={"name": "Nested", "folders": [{"path": str(root / "menu")}]})
            assert bad.status_code == 400 and "inside the project" in bad.json()["detail"]

            made = await client.post("/api/sessions", headers=HEADERS, json={"title": "Menu page", "project_id": project["id"]})
            assert made.status_code == 200
            sid = made.json()["id"]

            detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
            assert detail["workspace"] == str(root) and detail["project"]["name"] == "Bakery"
            listing = (await client.get("/api/sessions", headers=HEADERS)).json()
            rows = listing["sessions"]
            assert [(r["id"], r["project_id"], r["project"]) for r in rows if r["id"] == sid] == [(sid, project["id"], "Bakery")]
            # The folders come back beside the rows, each counting its own agents.
            folder = next(p for p in listing["projects"] if p["id"] == project["id"])
            assert folder["name"] == "Bakery" and folder["total"] == 1 and folder["system"] == ""

            # The file endpoints take their scope from the session, so they show the project and refuse the way out.
            files = (await client.get(f"/api/sessions/{sid}/files", headers=HEADERS)).json()
            assert {e["name"] for e in files["entries"]} >= {"menu"}
            escape = await client.get(f"/api/sessions/{sid}/files", headers=HEADERS, params={"path": "../.."})
            assert escape.status_code == 400
            download = await client.get(f"/api/sessions/{sid}/download", headers=HEADERS, params={"path": "../../etc/hostname"})
            assert download.status_code == 400

            # The other agents of a project are its agents, not the ones that happen to name the same
            # directory: a project session carries no workspace in its metadata to be matched on.
            second = (await client.post("/api/sessions", headers=HEADERS, json={"title": "Photos", "project_id": project["id"]})).json()["id"]
            beside = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()["workspace_sessions"]
            assert [u["id"] for u in beside] == [second]

            renamed = await client.patch(f"/api/projects/{project['id']}", headers=HEADERS,
                                         json={"name": "Bakery site", "snapshots": True,
                                               "expected_entity_revision": project["entity_revision"],
                                               "client_operation_id": "rename-bakery-site"})
            listed = (await client.get("/api/projects", headers=HEADERS)).json()[0]
            assert renamed.status_code == 200 and listed["name"] == "Bakery site" and listed["settings"]["snapshots"] is True
            assert manager.live_state(sid).project.settings.snapshots is True, "a live session must see the change, not the next one to open"

            busy = await client.delete(f"/api/projects/{project['id']}", headers=HEADERS)
            assert busy.status_code == 409 and "2 agents work in" in busy.json()["detail"]
            gone = await client.delete(f"/api/projects/{project['id']}", headers=HEADERS)
            assert gone.status_code == 409
            # Not one file of it was touched.
            assert (root / "menu" / "items.json").is_file()
    finally:
        await manager.close()


async def test_a_session_created_without_a_project_choice_gets_a_project(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        async with await _client(settings, config, db, manager) as client:
            made = await client.post("/api/sessions", headers=HEADERS, json={"title": "Plain"})
            sid = made.json()["id"]
            detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
            assert detail["project"] is not None
            assert Path(detail["workspace"]).parent == settings.workspaces_dir
            assert detail["workspace_own"] is False
            listing = (await client.get("/api/sessions", headers=HEADERS)).json()
            rows = listing["sessions"]
            assert all(r["project_id"] for r in rows if r["id"] == sid)
            assert "free" not in listing
        state = manager.live_state(sid)
        assert state is not None and state.project is not None
        assert state.services is not None and state.services.walls is not None and state.services.walls.readable == (state.workspace,)
        with pytest.raises(PathOutsideProject):
            state.services.resolve("/etc/hostname")
    finally:
        await manager.close()


async def test_an_unreachable_folder_is_reported_and_no_agent_is_started_in_it(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """The Docker seam: the row exists so the launcher can write the bind mount, and the app can say
    what is missing instead of showing a project whose files are mysteriously absent."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        async with await _client(settings, config, db, manager) as client:
            created = await client.post("/api/projects", headers=HEADERS, json={"name": "Not mounted", "folders": [{"path": str(tmp_path / "not-here")}]})
            assert created.status_code == 200 and created.json()["folders"][0]["reachable"] is False
            refused = await client.post("/api/sessions", headers=HEADERS, json={"title": "x", "project_id": created.json()["id"]})
            assert refused.status_code == 409 and "not reachable from here yet" in refused.json()["detail"]
    finally:
        await manager.close()


def _no_engine(manager: SessionManager) -> Any:
    """Let submit run its guards and its bookkeeping without starting a model loop."""

    async def start(state: Any, message: Any) -> str:
        return "run-1"

    return start


async def test_a_folder_of_ours_is_made_on_demand_and_one_of_theirs_is_not(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """The distinction the guard was missing.

    A project created by name is anchored under the workspaces tree, which nothing outside this
    installation ever writes to: a row there without its folder is our own bookkeeping having lost
    a directory, and it is put back. A folder the operator pointed at is theirs, and a missing one
    is the mount or the disk being absent, which no amount of mkdir fixes.
    """
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    manager._start_run = _no_engine(manager)  # type: ignore[method-assign]
    try:
        ours = await manager.projects.create("Voice")
        assert ours.primary.managed is True and ours.primary.path.parent == settings.workspaces_dir
        assert ours.primary.path.is_dir() and (ours.primary.path / "inbox").is_dir()

        state = await manager.create_session("Errand", project_id=ours.id)
        shutil.rmtree(ours.primary.path)
        assert (await manager.projects.get(ours.id)).primary.reachable is False
        assert await manager.submit(state.session.id, "go") == "run-1"
        assert ours.primary.path.is_dir() and (ours.primary.path / "inbox").is_dir()

        chosen = tmp_path / "chosen"
        chosen.mkdir()
        theirs = await manager.projects.create("Elsewhere", [str(chosen)])
        assert theirs.primary.managed is False
        gone = await manager.create_session("Theirs", project_id=theirs.id)
        shutil.rmtree(chosen)
        with pytest.raises(RuntimeError, match=str(chosen)):
            await manager.submit(gone.session.id, "go")
        assert not chosen.exists()
    finally:
        await manager.close()


async def test_a_system_project_whose_folder_an_upgrade_never_made_gets_one(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    """The live failure: the migration writes the Voice row from the sessions it finds, and the root
    it names is a per-session directory that may never have existed on disk. ``ensure_system`` found
    the row, returned it, and made nothing — so every utterance was refused."""
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    store = ProjectStore(db, managed_root=settings.workspaces_dir)
    root = settings.workspaces_dir / "abc123def456"
    await db.execute("INSERT INTO projects(id, name, created_at, settings, system) VALUES ('p1', 'Voice', '2020-01-01T00:00:00+00:00', '{\"system\":\"voice\"}', 'voice')")
    await db.execute(
        "INSERT INTO project_folders(id, project_id, path, env, created_at) VALUES ('f-p1', 'p1', ?, 'container', '2020-01-01T00:00:00+00:00')",
        (str(root),),
    )
    assert not root.exists()
    project = await store.ensure_system("voice", name="Voice", root=settings.workspaces_dir / "unused")
    assert project.id == "p1" and project.primary.path == root
    assert root.is_dir() and (root / "inbox").is_dir()


async def test_the_folders_of_ours_are_put_back_when_the_manager_starts(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        ours = await manager.projects.create("Ours")
        (tmp_path / "theirs").mkdir()
        theirs = await manager.projects.create("Theirs", [str(tmp_path / "theirs")])
        shutil.rmtree(ours.primary.path)
        (tmp_path / "theirs").rmdir()
    finally:
        await manager.close()
    again = SessionManager(settings, config, db=db)
    await again.start()
    try:
        assert (await again.projects.get(ours.id)).primary.reachable is True
        assert (await again.projects.get(theirs.id)).primary.reachable is False
    finally:
        await again.close()


# -- the prompt ----------------------------------------------------------------------------------


def test_the_agent_is_told_where_the_project_is_and_that_it_stays_inside_it() -> None:
    root = Path("/work/bakery")
    section = prompts.environment_section(
        workspace=root, bot_repo=Path("/srv/daedalus"), core_repo=Path("/srv/core"), session_title="Menu", model="m", project="Bakery site"
    )
    assert "project Bakery site" in section and str(root) in section
    assert "refused" in section and "ask" in section
    # A session with no project says nothing about one.
    plain = prompts.environment_section(workspace=root, bot_repo=Path("/srv/daedalus"), core_repo=Path("/srv/core"), session_title="Menu", model="m")
    assert "project" not in plain.lower()


async def test_a_project_is_not_snapshotted_unless_the_operator_asked(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """A project root is somebody's repository. Snapshotting it before every turn and after every run
    is a cost the undo does not repay, so it is opt-in — and where it is opted in, it behaves as a
    workspace does."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("hello", encoding="utf-8")
    try:
        project = await manager.projects.create("Repo", [str(root)])
        state = await manager.create_session("in the project", project_id=project.id)
        assert await manager.checkpoint(state, kind="before") is None
        assert not (root / ".checkpoints").exists()

        updated = await manager.projects.update(project.id, snapshots=True)
        await manager.reload_project(updated, project.id)
        sha = await manager.checkpoint(state, kind="before")
        assert sha and (root / ".checkpoints" / "HEAD").exists()

        # A session with a workspace of its own snapshots as it always has, with nothing asked for.
        plain = await manager.create_session("on its own")
        assert await manager.checkpoint(plain, kind="before")
    finally:
        await manager.close()


# -- the children a project session makes ----------------------------------------------------------


async def test_a_subagent_of_a_project_session_is_in_the_project(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """The commonest way to make a new worker is the one that must not leave the wall behind: a child
    that carried the leader's directory without the leader's project resolved anything at all."""
    from daedalus.extensions.subagents import Subagents

    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    root.mkdir()
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={})
    try:
        submitted: list[str] = []

        async def fake_submit(session_id: str, text: str, attachments: Any = (), **kw: Any) -> str:
            submitted.append(session_id)
            return "run-x"

        manager.submit = fake_submit  # type: ignore[method-assign]
        project = await manager.projects.create("Repo", [str(root)])
        leader = await manager.create_session("lead", project_id=project.id)
        result = await Subagents(app).spawn(leader_id=leader.session.id, task="count the files", name="counter")

        child = await manager.get_state(result["session_id"])
        assert child is not None and child.workspace == root
        assert child.project is not None and child.project.id == project.id
        assert child.services is not None and child.services.walls is not None and child.services.walls.readable == (root,)
        with pytest.raises(PathOutsideProject):
            child.services.resolve("/etc/passwd")
        # And it is one of the project's agents in every list, not a session that merely names the folder.
        assert result["session_id"] in {s["id"] for s in await manager.projects.sessions_of(project.id)}
        assert "workspace" not in child.metadata, "the project is the one source for where a session works"
        assert submitted == [result["session_id"]]
    finally:
        await manager.close()


async def test_a_subagent_of_a_plain_session_still_shares_its_directory(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    from daedalus.extensions.subagents import Subagents

    manager = SessionManager(settings, config, db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={})
    try:

        async def fake_submit(session_id: str, text: str, attachments: Any = (), **kw: Any) -> str:
            return "run-x"

        manager.submit = fake_submit  # type: ignore[method-assign]
        leader = await manager.create_session("lead")
        result = await Subagents(app).spawn(leader_id=leader.session.id, task="count", name="counter")
        child = await manager.get_state(result["session_id"])
        assert child is not None and child.workspace == leader.workspace and child.project == leader.project
        assert child.services is not None and child.services.walls is not None and child.services.walls.readable == (leader.workspace,)
    finally:
        await manager.close()


async def test_a_fork_of_a_project_session_stays_in_it_and_copies_nothing(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """A fork used to copy the whole of the operator's repository into the state directory and leave
    the copy uncontained. Two sessions of one project share the folder; there is nothing to copy."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]", encoding="utf-8")
    (root / "big.bin").write_bytes(b"x" * 4096)
    try:
        async with await _client(settings, config, db, manager) as client:
            project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Repo", "folders": [{"path": str(root)}]})).json()
            sid = (await client.post("/api/sessions", headers=HEADERS, json={"title": "work", "project_id": project["id"]})).json()["id"]
            forked = await client.post(f"/api/sessions/{sid}/fork", headers=HEADERS, json={"seq": 1})
            assert forked.status_code == 200
            body = forked.json()
            assert body["workspace_copied"] is False and body["workspace_shared"] is True

            fork = await manager.get_state(body["id"])
            assert fork is not None and fork.workspace == root
            assert fork.project is not None and fork.project.id == project["id"]
            assert fork.services is not None and fork.services.walls is not None and fork.services.walls.readable == (root,)
            with pytest.raises(PathOutsideProject):
                fork.services.resolve("/etc/passwd")
            # Not one byte of the folder was duplicated into the state directory.
            assert not (manager.workspace_for(body["id"]) / ".git").exists()
    finally:
        await manager.close()


async def test_a_fork_of_a_private_directory_gets_a_private_sibling(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        async with await _client(settings, config, db, manager) as client:
            project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Research"})).json()
            sid = (await client.post("/api/sessions", headers=HEADERS, json={"title": "one", "project_id": project["id"], "own_directory": True})).json()["id"]
            source = await manager.get_state(sid)
            assert source is not None
            (source.workspace / "result.txt").write_text("kept apart", encoding="utf-8")

            response = await client.post(f"/api/sessions/{sid}/fork", headers=HEADERS, json={"seq": 1})
            assert response.status_code == 200
            body = response.json()
            fork = await manager.get_state(body["id"])
            assert fork is not None and fork.project is not None and fork.project.id == project["id"]
            assert fork.workspace != source.workspace and fork.workspace.parent == source.workspace.parent
            assert (fork.workspace / "result.txt").read_text(encoding="utf-8") == "kept apart"
            assert body["workspace_copied"] is True and body["workspace_shared"] is False
    finally:
        await manager.close()


async def test_a_fork_of_a_workspace_over_the_size_cap_is_refused_by_name(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    """A fork copies a whole directory tree. ``ops.checkpoint_max_gb`` guarded the snapshot and said
    nothing about this one, so a session on a large tree wrote it twice without being asked."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    manager.config.ops.checkpoint_max_gb = 1e-6  # a kilobyte
    try:
        source = await manager.create_session("big")
        (source.workspace / "payload.bin").write_bytes(b"x" * 8192)
        target = await manager.create_session("fork")
        with pytest.raises(RuntimeError, match="over ops.checkpoint_max_gb"):
            await manager.fork_into(source.session.id, 1, target)
        assert not (target.workspace / "payload.bin").exists()
    finally:
        await manager.close()


async def test_a_spawned_agent_of_a_project_session_stays_in_the_project(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """SpawnAgent resolved its files correctly and then handed the absolute paths to a child with no
    project: one tool call moved the operator's repository into an uncontained session."""
    from daedalus.transport.telegram.front import TelegramFront

    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    root.mkdir()
    (root / "howto.md").write_text("step 1", encoding="utf-8")
    try:

        async def fake_submit(session_id: str, text: str, attachments: Any = (), **kw: Any) -> str:
            return "run-x"

        manager.submit = fake_submit  # type: ignore[method-assign]
        front = TelegramFront.__new__(TelegramFront)
        front.manager = manager

        async def create_session_topic(title: str, *, metadata: dict[str, Any] | None = None, project_id: str | None = None, **kw: Any) -> Any:
            return await manager.create_session(title, metadata=metadata, project_id=project_id), None

        front.create_session_topic = create_session_topic  # type: ignore[method-assign]

        project = await manager.projects.create("Repo", [str(root)])
        parent = await manager.create_session("parent", project_id=project.id)
        child_id = await front._service_spawn_agent(
            parent.session.id, title="Helper", brief="help", files=[str(root / "howto.md")],
            first_message=None, preset=None, mode=None, mcp=[], peer_name=None,
        )
        child = await manager.get_state(child_id)
        assert child is not None and child.project is not None and child.project.id == project.id
        assert child.workspace == root
        with pytest.raises(PathOutsideProject):
            child.services.resolve("/etc/passwd")  # type: ignore[union-attr]
        # The file was already where the new agent works, so it was not copied anywhere.
        assert not (root / "inbox" / "howto.md").exists()
    finally:
        await manager.close()


async def test_a_scheduled_task_of_a_project_session_fires_in_the_project(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    from daedalus.extensions.scheduler import Scheduler

    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    root.mkdir()
    (root / "notes.md").write_text("n", encoding="utf-8")
    fired: list[str] = []

    async def create_session(title: str, *, metadata: dict[str, Any] | None = None, project_id: str | None = None, workspace: Path | None = None) -> Any:
        return await manager.create_session(title, metadata=metadata, project_id=project_id, workspace=workspace)

    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, create_session=create_session)
    try:

        async def fake_submit(session_id: str, text: str, attachments: Any = (), **kw: Any) -> str:
            fired.append(session_id)
            return "run-x"

        manager.submit = fake_submit  # type: ignore[method-assign]
        scheduler = Scheduler(app)  # type: ignore[arg-type]
        project = await manager.projects.create("Repo", [str(root)])
        owner = await manager.create_session("owner", project_id=project.id)

        created = await scheduler.create(
            name="nightly", prompt="tidy up", cron="0 3 * * *", run_at=None,
            files=[str(root / "notes.md")], created_by_session=owner.session.id,
        )
        # The task runs in the folder, and the file it was given is not copied out of it.
        assert created["workspace"] == str(root)
        assert not (settings.workspaces_dir / f"sched-{created['id']}").exists()

        row = dict(await db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],)))
        session_id = await scheduler._fire_agent(row)
        task = await manager.get_state(session_id)
        assert task is not None and task.project is not None and task.project.id == project.id
        assert task.workspace == root and fired == [session_id]
        with pytest.raises(PathOutsideProject):
            task.services.resolve("/etc/passwd")  # type: ignore[union-attr]
    finally:
        await manager.close()


# -- the folder is the operator's ------------------------------------------------------------------


async def test_an_unmounted_root_is_never_created_and_keeps_saying_it_is_not_mounted(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """``mkdir(parents=True)`` on an unmounted root made the whole path, and the mount point became a
    local empty folder that answered ``reachable`` for ever after — erasing the one signal the Docker
    story rests on, and shadowing the real folder when it came back."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "mounted"
    root.mkdir()
    try:
        project = await manager.projects.create("Mounted", [str(root)])
        state = await manager.create_session("worker", project_id=project.id)
        sid = state.session.id
        assert (root / "inbox").is_dir()

        # The mount goes away and the process is restarted: the state is loaded from the database again.
        shutil.rmtree(root)
        manager._states.pop(sid)
        assert (await manager.projects.get(project.id)).primary.reachable is False
        reloaded = await manager.get_state(sid)
        assert reloaded is not None
        assert not root.exists(), "the folder the operator added is theirs to create"
        assert (await manager.projects.get(project.id)).primary.reachable is False
    finally:
        await manager.close()


async def test_a_project_may_not_be_the_installation_or_the_whole_home_folder(db: Database, tmp_path: Path) -> None:
    """A project root is reachable to every agent in it and is an open root to the policy, so the
    state directory, the checkouts and the home folder as a whole are refused — in both directions."""
    state_dir = tmp_path / "own" / "state"
    workspaces = tmp_path / "own" / "workspaces"
    home = tmp_path / "own" / "home"
    for path in (state_dir, workspaces, home, home / "work"):
        path.mkdir(parents=True, exist_ok=True)
    store = ProjectStore(db, reserved=[state_dir, workspaces], home=home)

    with pytest.raises(ProjectError, match="belongs to the installation"):
        await store.create("State", [str(state_dir)])
    with pytest.raises(ProjectError, match="is inside"):
        await store.create("Inside", [str(state_dir / "blobs")])
    with pytest.raises(ProjectError, match="contains"):
        await store.create("Above", [str(tmp_path / "own")])
    with pytest.raises(ProjectError, match="your home folder"):
        await store.create("Home", [str(home)])
    # A folder inside the home folder is the ordinary case and stays allowed.
    assert (await store.create("Work", [str(home / "work")])).primary.path == home / "work"


async def test_a_project_root_is_immutable_and_a_nonempty_project_cannot_be_removed(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """A project keeps its directory for life and cannot strand sessions when removed."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    try:
        async with await _client(settings, config, db, manager) as client:
            project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Repo", "folders": [{"path": str(root)}]})).json()
            sid = (await client.post("/api/sessions", headers=HEADERS, json={"title": "worker", "project_id": project["id"]})).json()["id"]
            manager.live_state(sid).pending = SimpleNamespace(payload={})  # a question outstanding: the turn is in flight

            listing = (await client.get("/api/projects", headers=HEADERS)).json()
            assert listing[0]["sessions"] == [{"id": sid, "title": "worker", "running": True}]

            moved = await client.patch(f"/api/projects/{project['id']}", headers=HEADERS,
                                       json={"root": str(tmp_path / "elsewhere"),
                                             "expected_entity_revision": project["entity_revision"],
                                             "client_operation_id": "move-repo-root"})
            assert moved.status_code == 422, "project roots are immutable and no obsolete setting is accepted"
            removed = await client.delete(f"/api/projects/{project['id']}", headers=HEADERS, params={"detach": 1})
            assert removed.status_code == 409 and "works in Repo" in removed.json()["detail"]
            # A rename touches no directory and is not refused.
            assert (await client.patch(f"/api/projects/{project['id']}", headers=HEADERS,
                                       json={"name": "Repo 2", "expected_entity_revision": project["entity_revision"],
                                             "client_operation_id": "rename-repo-two"})).status_code == 200

            manager.live_state(sid).pending = None
            await manager.delete_session(sid)
            assert (await client.delete(f"/api/projects/{project['id']}", headers=HEADERS, params={"detach": 1})).status_code == 200
    finally:
        await manager.close()


def test_a_resolved_path_comes_back_as_the_path_that_was_judged(tmp_path: Path) -> None:
    """``contains`` judges the real path; returning the candidate left a window in which a name
    inside the root could be turned into a link out of it before the caller opened it."""
    root = Path(os.path.realpath(tmp_path)) / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "real.txt").write_text("inside", encoding="utf-8")
    (root / "link.txt").symlink_to(root / "src" / "real.txt")
    services = _services(root)
    assert services.resolve("link.txt") == root / "src" / "real.txt"


async def test_the_agent_directories_are_excluded_from_the_operators_checkout(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """A project root is somebody's repository: what a session writes into it must not turn up in
    their ``git status`` or be swept into a commit by ``git add -A``."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    try:
        project = await manager.projects.create("Repo", [str(root)])
        await manager.create_session("worker", project_id=project.id)
        written = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        for name in ("inbox/", ".exec/", ".jobs/", ".services/", ".checkpoints/"):
            assert f"/{name}" in written
        # Loading another session of the same project does not write the block a second time.
        await manager.create_session("second", project_id=project.id)
        assert (root / ".git" / "info" / "exclude").read_text(encoding="utf-8") == written

        # A root that is not a git repository has nothing to write and nothing to worry about.
        plain = tmp_path / "docs"
        plain.mkdir()
        other = await manager.projects.create("Docs", [str(plain)])
        await manager.create_session("third", project_id=other.id)
        assert not (plain / ".git").exists()
    finally:
        await manager.close()


async def test_a_service_started_in_a_project_cannot_choose_a_directory_outside_it(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """Every other path-taking tool goes through the session's resolve; ``ServiceStart`` took its cwd
    raw, which made it the one way into the filesystem the project's containment did not judge — and
    a service is the one thing here that outlives the turn."""
    from daedalus.extensions.services import Services
    from tests.support.notifications import RecordingNotifications

    settings.services_port_range = "18140-18142"
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    (root / "site").mkdir(parents=True)
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={})
    app.notifications = RecordingNotifications()
    try:
        services = Services(app)  # type: ignore[arg-type]
        project = await manager.projects.create("Repo", [str(root)])
        state = await manager.create_session("host", project_id=project.id)
        with pytest.raises(PathOutsideProject):
            await services.start(state.session.id, name="escape", command="sleep 30", cwd="/", port=None)
        started = await services.start(state.session.id, name="inside", command="sleep 30", cwd="site", port=None)
        assert started["cwd"] == str(root / "site")
        await services.stop(state.session.id, "inside")
    finally:
        await manager.close()


# -- folders, the brief, the journal and the orchestrator's compare-and-set -------------------


async def test_no_two_folders_anywhere_nest_and_none_repeats_in_one_project(db: Database, tmp_path: Path) -> None:
    """The nesting rule is over every folder of every project, the same project's included; the same
    folder may be in several projects, once in each."""
    store = ProjectStore(db, reserved=[tmp_path / "state"])
    (tmp_path / "site" / "assets").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    site = await store.create("Site", [str(tmp_path / "site")])
    other = await store.create("Other", [str(tmp_path / "docs")])
    with pytest.raises(ProjectError, match="inside the project Site"):
        await store.add_folder(site.id, str(tmp_path / "site" / "assets"))
    with pytest.raises(ProjectError, match="inside the project Site"):
        await store.add_folder(other.id, str(tmp_path / "site" / "assets"))
    shared = await store.add_folder(site.id, str(tmp_path / "docs"))
    assert shared.project_id == site.id and await store.holders(tmp_path / "docs", besides=site.id) == ["Other"]
    with pytest.raises(ProjectError, match="Site already has the folder"):
        await store.add_folder(site.id, str(tmp_path / "docs"))
    grouped = await store.create("Grouped", [str(tmp_path / "group" / "inner")])
    with pytest.raises(ProjectError, match="contains the project Grouped"):
        await store.add_folder(other.id, str(tmp_path / "group"))
    with pytest.raises(ProjectError, match="belongs to the installation"):
        await store.add_folder(site.id, str(tmp_path / "state" / "blobs"))
    with pytest.raises(ProjectError, match="nest"):
        await store.create("Both", [str(tmp_path / "a"), str(tmp_path / "a" / "b")])
    with pytest.raises(TypeError):
        await store.create("One path", str(tmp_path / "c"))  # type: ignore[arg-type]
    # Nothing half-made was left behind by the refusals.
    assert sorted(f.path for p in await store.list() for f in p.folders) == [tmp_path / "docs", tmp_path / "docs", tmp_path / "group" / "inner", tmp_path / "site"]
    assert grouped.primary.reachable is False, "a folder that is not there yet is kept, and says so"


async def test_folders_are_added_ordered_and_removed_but_never_the_last(db: Database, tmp_path: Path) -> None:
    store = ProjectStore(db)
    for name in ("site", "docs", "data"):
        (tmp_path / name).mkdir()
    (tmp_path / "docs" / ".git").mkdir()
    project = await store.create("Site", [str(tmp_path / "site")])
    docs = await store.add_folder(project.id, str(tmp_path / "docs"), label="Docs")
    data = await store.add_folder(project.id, str(tmp_path / "data"), readonly=True)
    assert docs.is_git and not data.is_git and data.readonly and not data.writable and data.reachable
    loaded = await store.get(project.id)
    assert [f.path.name for f in loaded.folders] == ["site", "docs", "data"] and loaded.primary.path.name == "site"

    moved = await store.update_folder(project.id, data.id, position=0, readonly=False, label="Data")
    assert moved.position == 0 and moved.label == "Data" and moved.writable
    loaded = await store.get(project.id)
    assert [f.path.name for f in loaded.folders] == ["data", "site", "docs"] and [f.position for f in loaded.folders] == [0, 1, 2]

    await store.remove_folder(project.id, loaded.folders[1].id)
    loaded = await store.get(project.id)
    assert [(f.path.name, f.position) for f in loaded.folders] == [("data", 0), ("docs", 1)]
    await store.remove_folder(project.id, docs.id)
    with pytest.raises(ProjectError, match="only folder"):
        await store.remove_folder(project.id, data.id)
    assert (tmp_path / "site").is_dir() and (tmp_path / "docs").is_dir(), "forgetting a folder touches nothing on disk"
    kinds = [(e.author, e.kind) for e in await store.journal(project.id)]
    assert kinds and set(kinds) == {("system", "folder")}, "every folder change is in the journal without anybody writing it"


async def test_a_host_folder_is_kept_but_is_not_this_process_s_to_reach(db: Database, tmp_path: Path) -> None:
    store = ProjectStore(db, local_env="container")
    (tmp_path / "site").mkdir()
    (tmp_path / "tools").mkdir()
    project = await store.create("Site", [str(tmp_path / "site")])
    host = await store.add_folder(project.id, str(tmp_path / "tools"), env="host")
    assert host.env == "host" and not host.is_git
    assert await store.ensure_reachable(host) is False, "a path that happens to exist here is not the host's folder"
    assert store.roots == (tmp_path / "site",)
    loaded = await store.get(project.id)
    assert loaded.local_folders("container") == (loaded.primary,)
    with pytest.raises(ProjectError, match="container or on the host"):
        await store.add_folder(project.id, str(tmp_path / "elsewhere"), env="cloud")


async def test_is_git_is_looked_at_again_on_the_way_up(db: Database, tmp_path: Path) -> None:
    store = ProjectStore(db)
    (tmp_path / "site").mkdir()
    project = await store.create("Site", [str(tmp_path / "site")])
    assert not project.primary.is_git
    (tmp_path / "site" / ".git").mkdir()
    refreshed = {p.id: p for p in await store.ensure_roots()}
    assert refreshed[project.id].primary.is_git


async def test_the_orchestrator_is_set_by_compare_and_set_and_one_racer_wins(db: Database, tmp_path: Path) -> None:
    import asyncio

    store = ProjectStore(db)
    (tmp_path / "site").mkdir()
    project = await store.create("Site", [str(tmp_path / "site")])
    assert project.settings.orchestrator.session_id == ""
    results = await asyncio.gather(*[store.set_orchestrator(project.id, expect="", value=f"orch-{n}") for n in range(6)])
    assert results.count(True) == 1
    winner = (await store.get(project.id)).settings.orchestrator.session_id
    assert winner == f"orch-{results.index(True)}"
    assert await store.set_orchestrator(project.id, expect="", value="late") is False, "a stale expectation loses"
    assert await store.set_orchestrator(project.id, expect=winner, value="replacement") is True
    assert (await store.get(project.id)).settings.orchestrator.session_id == "replacement"


async def test_the_orchestrator_settings_stay_within_their_rules(db: Database, settings: Settings, tmp_path: Path) -> None:
    store = ProjectStore(db, managed_root=settings.workspaces_dir)
    (tmp_path / "site").mkdir()
    project = await store.create("Site", [str(tmp_path / "site")])
    orchestrator = project.settings.orchestrator
    assert (orchestrator.enabled, orchestrator.autonomy, orchestrator.concurrency, orchestrator.concurrency_cap) == (False, "normal", 6, 10)
    changed = await store.update_orchestrator(project.id, enabled=True, model="strong", autonomy="full", concurrency=8)
    assert changed.settings.orchestrator.enabled and changed.settings.orchestrator.concurrency == 8
    with pytest.raises(ProjectError, match="between 1 and the cap of 10"):
        await store.update_orchestrator(project.id, concurrency=11)
    lowered = await store.update_orchestrator(project.id, concurrency_cap=4)
    assert (lowered.settings.orchestrator.concurrency, lowered.settings.orchestrator.concurrency_cap) == (4, 4), "a lowered cap takes the limit down with it"
    with pytest.raises(ProjectError, match="autonomy"):
        await store.update_orchestrator(project.id, autonomy="reckless")
    # A rename or a snapshot switch does not write the orchestrator block back from a stale copy.
    await store.set_orchestrator(project.id, expect="", value="orch")
    renamed = await store.update(project.id, name="Renamed", snapshots=True)
    assert renamed.settings.orchestrator.session_id == "orch" and renamed.settings.orchestrator.model == "strong"

    scratch = await store.create("A chat", settings=ProjectSettings(snapshots=True, ephemeral=True))
    with pytest.raises(ProjectError, match="keep it as a project first"):
        await store.update_orchestrator(scratch.id, enabled=True)
    kept = await store.update(scratch.id, ephemeral=False)
    assert (await store.update_orchestrator(kept.id, enabled=True)).settings.orchestrator.enabled


async def test_only_the_operator_writes_what_may_be_granted_without_them(db: Database, tmp_path: Path) -> None:
    store = ProjectStore(db)
    (tmp_path / "site").mkdir()
    project = await store.create("Site", [str(tmp_path / "site")])
    brief = await store.brief(project.id)
    assert list(brief) == ["goals", "constraints", "preferences", "done_when", "allowed_without_operator", "notes"]
    assert all(section.body == "" for section in brief.values())
    await store.set_brief(project.id, "preferences", "a compact menu", "orchestrator")
    with pytest.raises(ProjectError, match="impact preview"):
        await store.set_brief(project.id, "goals", "a menu page", "orchestrator")
    with pytest.raises(ProjectError, match="only the operator"):
        await store.set_brief(project.id, "allowed_without_operator", "anything at all", "orchestrator")
    with pytest.raises(ProjectError, match="only the operator"):
        await store.set_brief(project.id, "allowed_without_operator", "anything at all", "system")
    await store.set_brief(project.id, "allowed_without_operator", "run the test suite", "operator")
    with pytest.raises(ProjectError, match="sections"):
        await store.set_brief(project.id, "wishes", "x", "operator")
    brief = await store.brief(project.id)
    assert (brief["preferences"].body, brief["preferences"].updated_by) == ("a compact menu", "orchestrator")
    assert brief["goals"].body == ""
    assert (brief["allowed_without_operator"].body, brief["allowed_without_operator"].updated_by) == ("run the test suite", "operator")


async def test_the_journal_pages_newest_first(db: Database, tmp_path: Path) -> None:
    store = ProjectStore(db)
    (tmp_path / "site").mkdir()
    project = await store.create("Site", [str(tmp_path / "site")])
    written = [await store.record(project.id, "orchestrator", "decision", f"step {n}", {"n": n}) for n in range(5)]
    first = await store.journal(project.id, limit=2)
    assert [e.text for e in first] == ["step 4", "step 3"]
    second = await store.journal(project.id, before=first[-1].id, limit=2)
    assert [e.text for e in second] == ["step 2", "step 1"]
    last = await store.journal(project.id, before=second[-1].id, limit=2)
    assert [e.text for e in last] == ["step 0"] and last[0].refs == {"n": 0} and last[0].id == written[0].id
    with pytest.raises(ProjectError, match="journal entry"):
        await store.record(project.id, "staff", "note", "x")


async def test_a_session_works_in_the_folder_it_is_given(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        (tmp_path / "site").mkdir()
        (tmp_path / "docs").mkdir()
        (tmp_path / "tools").mkdir()
        project = await manager.projects.create("Site", [str(tmp_path / "site")])
        docs = await manager.projects.add_folder(project.id, str(tmp_path / "docs"))
        host = await manager.projects.add_folder(project.id, str(tmp_path / "tools"), env="host")
        project = await manager.projects.get(project.id)
        assert project is not None

        plain = await manager.create_session("plain", project_id=project.id)
        assert plain.workspace == tmp_path / "site"
        in_docs = await manager.create_session("docs", project_id=project.id, folder_id=docs.id, own_directory=True)
        assert in_docs.workspace == tmp_path / "docs" / ".agents" / in_docs.session.id
        assert in_docs.services is not None and in_docs.services.walls is not None and in_docs.services.walls.readable == (in_docs.workspace,)
        with pytest.raises(ValueError, match="host folder"):
            await manager.create_session("host", project_id=project.id, folder_id=host.id)
        with pytest.raises(ValueError, match="not a folder of"):
            await manager.create_session("nowhere", project_id=project.id, folder_id="f-nothing")

        # Loaded afresh the folder comes from the metadata, and a child keeps it.
        manager._states.clear()
        again = await manager.get_state(in_docs.session.id)
        assert again is not None and again.workspace == in_docs.workspace
        moved = await manager.attach_project(in_docs.session.id, project)
        assert moved.workspace == tmp_path / "site", "a session moved into a project starts in its primary folder"
    finally:
        await manager.close()


async def test_a_chat_s_own_project_is_ephemeral_and_goes_with_it(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        state = await manager.create_session("a chat")
        assert state.project is not None and state.project.settings.ephemeral and state.project.primary.managed
        pid = state.project.id
        await manager.delete_session(state.session.id)
        assert await manager.projects.get(pid) is None
        assert not await db.fetchall("SELECT id FROM project_folders WHERE project_id = ?", (pid,))
    finally:
        await manager.close()
