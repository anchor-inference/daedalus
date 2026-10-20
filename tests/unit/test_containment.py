"""Containment across a project's folders: what a session may read, and where it may write.

A project has several folders. A session reads every folder of its project this process can reach,
writes the ones not marked read-only, and a read-only folder stays read-only to the file tools, to
``Exec``'s sandbox and to the file pane alike.
"""

from __future__ import annotations

import io
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import ExecToolsConfig, RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.containment import Walls, walls_for, worktree_writable_paths
from daedalus.host.services import PathOutsideProject, SessionServices, locator
from daedalus.host.session_runner import Attachment, SessionManager
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec, Project, ProjectFolder
from daedalus.tools import shell

HEADERS = {"X-Daedalus-Token": "tok"}
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _folder(path: Path, fid: str, *, env: str = "container", readonly: bool = False, position: int = 0) -> ProjectFolder:
    return ProjectFolder(id=fid, project_id="p", path=path, label=fid, env=env, is_git=False, readonly=readonly, position=position, created_at=NOW)


def _project(*folders: ProjectFolder) -> Project:
    return Project(id="p", name="Atlas", created_at=NOW, folders=folders)


@pytest.fixture
def tree(tmp_path: Path) -> SimpleNamespace:
    """Three folders on disk (code, docs read-only, assets) and a host folder, not reachable from here."""
    code, docs, assets = tmp_path / "code", tmp_path / "docs", tmp_path / "assets"
    for path in (code, docs, assets):
        path.mkdir()
    host = tmp_path / "on-the-host"
    host.mkdir()
    return SimpleNamespace(
        code=code,
        docs=docs,
        assets=assets,
        host=host,
        project=_project(
            _folder(code, "f-code"),
            _folder(docs, "f-docs", readonly=True, position=1),
            _folder(assets, "f-assets", position=2),
            _folder(host, "f-host", env="host", position=3),
        ),
    )


# -- the walls, row by row -----------------------------------------------------------------------


def test_an_ordinary_session_reads_every_local_folder_and_writes_the_ones_not_read_only(tree: SimpleNamespace) -> None:
    walls = walls_for(tree.project, folder_id=None, directory=None, local_env="container")
    assert walls.readable == (tree.code, tree.docs, tree.assets)
    assert walls.writable == (tree.code, tree.assets)
    # The folder the session works in changes nothing about the others.
    assert walls_for(tree.project, folder_id="f-assets", directory=None, local_env="container") == walls


def test_a_folder_of_the_other_environment_is_not_readable(tree: SimpleNamespace) -> None:
    """In Docker, a host folder's path names nothing the tools can reach — or an unrelated directory
    of the same name — so it is outside the walls even when a path of that name exists here."""
    walls = walls_for(tree.project, folder_id=None, directory=None, local_env="container")
    assert tree.host not in walls.readable and tree.host not in walls.writable
    native = walls_for(tree.project, folder_id="f-host", directory=None, local_env="host")
    assert native.readable == (tree.host,) and native.writable == (tree.host,)


def test_an_unreachable_folder_is_left_out_but_the_sessions_own_folder_is_kept(tmp_path: Path) -> None:
    """A write under an unmounted mount point would create the path and shadow the real folder later;
    the session's own folder stays, because whether it is there is the run's question, with a message."""
    here, gone, own_gone = tmp_path / "here", tmp_path / "unmounted", tmp_path / "own-unmounted"
    here.mkdir()
    project = _project(_folder(own_gone, "f-own"), _folder(here, "f-here", position=1), _folder(gone, "f-gone", position=2))
    walls = walls_for(project, folder_id=None, directory=None, local_env="container")
    assert walls.readable == (own_gone, here) and walls.writable == (own_gone, here)


def test_a_session_with_its_own_directory_keeps_the_wall_it_had(tree: SimpleNamespace) -> None:
    own = tree.code / ".agents" / "s1"
    walls = walls_for(tree.project, folder_id=None, directory=".agents/s1", local_env="container")
    assert walls == Walls(readable=(own,), writable=(own,))
    # Read-only all the way down: its own directory inside a read-only folder is not writable either.
    in_docs = walls_for(tree.project, folder_id="f-docs", directory=".agents/s1", local_env="container")
    assert in_docs == Walls(readable=(tree.docs / ".agents" / "s1",), writable=())


def test_staff_isolation_shared_is_an_ordinary_session_and_readonly_writes_nothing(tree: SimpleNamespace) -> None:
    ordinary = walls_for(tree.project, folder_id=None, directory=None, local_env="container")
    assert walls_for(tree.project, folder_id=None, directory=None, local_env="container", isolation="shared") == ordinary
    reviewer = walls_for(tree.project, folder_id=None, directory=None, local_env="container", isolation="readonly")
    assert reviewer.readable == ordinary.readable and reviewer.writable == ()
    with pytest.raises(ValueError, match="isolation"):
        walls_for(tree.project, folder_id=None, directory=None, local_env="container", isolation="sideways")


def test_staff_in_a_worktree_write_it_and_not_the_folder_it_was_made_from(tree: SimpleNamespace) -> None:
    repo = tree.code
    (repo / ".git" / "worktrees" / "ada").mkdir(parents=True)
    (repo / ".git" / "objects").mkdir()
    worktree = repo / ".agents" / "worktrees" / "ada"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {repo / '.git' / 'worktrees' / 'ada'}\n")
    walls = walls_for(tree.project, folder_id=None, directory=None, local_env="container", isolation="worktree", worktree=worktree)
    assert walls.readable == (tree.code, tree.docs, tree.assets)
    assert list(walls.writable) == [*worktree_writable_paths(worktree), tree.assets]
    assert tree.code not in walls.writable
    # The checkout itself is readable and not writable; the worktree inside it is both.
    assert walls.root_of(repo / "README.md") == tree.code and walls.root_of(repo / "README.md", write=True) is None
    assert walls.root_of(worktree / "README.md", write=True) == worktree
    with pytest.raises(ValueError, match="worktree"):
        walls_for(tree.project, folder_id=None, directory=None, local_env="container", isolation="worktree")


def test_a_worktree_of_a_read_only_folder_is_not_writable(tree: SimpleNamespace) -> None:
    worktree = tree.docs / ".agents" / "worktrees" / "ada"
    worktree.mkdir(parents=True)
    walls = walls_for(tree.project, folder_id="f-docs", directory=None, local_env="container", isolation="worktree", worktree=worktree)
    assert walls.writable == (tree.code, tree.assets)


def test_a_worktree_kept_outside_every_folder_is_added_to_what_the_session_reads(tree: SimpleNamespace, tmp_path: Path) -> None:
    worktree = tmp_path / "elsewhere" / "ada"
    worktree.mkdir(parents=True)
    walls = walls_for(tree.project, folder_id="f-assets", directory=None, local_env="container", isolation="worktree", worktree=worktree)
    assert walls.readable == (tree.code, tree.docs, tree.assets, worktree)
    assert walls.writable == (worktree, tree.code)


# -- resolve, contains, and the refusals ---------------------------------------------------------


def _services(tree: SimpleNamespace, session_id: str = "c-1") -> SessionServices:
    return SessionServices(session_id=session_id, workspace_dir=tree.code, walls=walls_for(tree.project, folder_id=None, directory=None, local_env="container"))


def test_resolve_reads_every_folder_and_writes_only_where_the_walls_allow(tree: SimpleNamespace) -> None:
    services = _services(tree)
    assert services.resolve(str(tree.docs / "guide.md")) == tree.docs / "guide.md"
    assert services.resolve(str(tree.assets / "logo.svg"), write=True) == tree.assets / "logo.svg"
    with pytest.raises(PathOutsideProject, match=f"in {tree.docs}, a folder this session may read but not write: it is read-only"):
        services.resolve(str(tree.docs / "guide.md"), write=True)
    with pytest.raises(PathOutsideProject, match="outside this project"):
        services.resolve(str(tree.host / "x"))
    assert services.workspace_writable


def test_a_symlink_out_of_a_folder_is_refused_and_one_into_a_read_only_folder_is_read_only(tree: SimpleNamespace, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tree.code / "escape").symlink_to(outside)
    (tree.code / "manual").symlink_to(tree.docs)
    services = _services(tree)
    with pytest.raises(PathOutsideProject, match="outside this project"):
        services.resolve("escape/secret.txt")
    # A link inside a writable folder is judged by where it points, so it does not open the read-only one.
    assert services.resolve("manual/guide.md") == tree.docs / "guide.md"
    with pytest.raises(PathOutsideProject, match="read-only"):
        services.resolve("manual/guide.md", write=True)


def test_a_session_in_a_read_only_folder_does_not_write_its_workspace(tree: SimpleNamespace) -> None:
    services = SessionServices(session_id="c-2", workspace_dir=tree.docs, walls=walls_for(tree.project, folder_id="f-docs", directory=None, local_env="container"))
    assert not services.workspace_writable
    assert services.sandbox_writable() == [tree.code, tree.assets]


async def test_write_edit_and_multiedit_refuse_a_read_only_folder_while_read_works(tree: SimpleNamespace) -> None:
    from daedalus.tools.files import edit_file, multi_edit, read_file, search_files, write_file

    (tree.docs / "guide.md").write_text("hello docs\n", encoding="utf-8")
    locator.register(_services(tree, "c-tools"))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="c-tools", metadata={"tool_call_id": "c"})
    try:
        assert "hello docs" in (await read_file().invoke(ctx, {"path": str(tree.docs / "guide.md")})).content
        assert "guide.md" in (await search_files().invoke(ctx, {"pattern": "hello", "path": str(tree.docs)})).content
        for tool, args in (
            (write_file, {"path": str(tree.docs / "planted.md"), "content": "x"}),
            (edit_file, {"path": str(tree.docs / "guide.md"), "old_string": "hello", "new_string": "bye"}),
            (multi_edit, {"path": str(tree.docs / "guide.md"), "edits": [{"old_string": "hello", "new_string": "bye"}]}),
        ):
            result = await tool().invoke(ctx, args)
            assert result.is_error and "read-only" in result.content and str(tree.docs) in result.content
        assert not (tree.docs / "planted.md").exists()
        assert (tree.docs / "guide.md").read_text(encoding="utf-8") == "hello docs\n"
        # The other writable folder of the project is written as the session's own.
        written = await write_file().invoke(ctx, {"path": str(tree.assets / "note.md"), "content": "ok"})
        assert not written.is_error and (tree.assets / "note.md").read_text(encoding="utf-8") == "ok"
    finally:
        locator.unregister("c-tools")


async def test_the_sandbox_binds_exactly_the_writable_walls(tree: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell, "bwrap_status", lambda: "ok")
    monkeypatch.setattr(shell, "bwrap_path", lambda: "/usr/bin/bwrap")
    services = _services(tree)
    opened = tmp_path / "worktrees" / "fix"
    opened.mkdir(parents=True)
    services.writable.append(opened)
    argv, sandboxed = await shell.sandbox_argv("touch x", ExecToolsConfig(sandbox="workspace"), writable=services.sandbox_writable())
    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    assert sandboxed and binds == [str(tree.code), str(tree.assets), str(opened)]
    # A session working in the read-only folder does not get its own workspace bound behind the walls' back.
    reader = SessionServices(session_id="c-3", workspace_dir=tree.docs, walls=walls_for(tree.project, folder_id="f-docs", directory=None, local_env="container"))
    argv, _ = await shell.sandbox_argv("touch x", ExecToolsConfig(sandbox="workspace"), writable=reader.sandbox_writable())
    assert str(tree.docs) not in [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]


@pytest.mark.skipif(shell.bwrap_status() != "ok", reason="the sandbox cannot create namespaces on this machine")
async def test_exec_cannot_touch_a_file_in_a_read_only_folder_under_the_sandbox(tree: SimpleNamespace) -> None:
    """The real sandbox, where there is one: ``touch`` in the read-only folder fails, in the writable one it works."""
    import asyncio

    services = _services(tree)
    for target, expected in ((tree.docs / "x", False), (tree.assets / "x", True)):
        argv, sandboxed = await shell.sandbox_argv(f"touch {target}", ExecToolsConfig(sandbox="workspace"), writable=services.sandbox_writable())
        assert sandboxed
        proc = await asyncio.create_subprocess_exec(*argv, cwd=str(tree.code))
        assert (await proc.wait() == 0) is expected and target.exists() is expected


# -- sessions in a project with several folders --------------------------------------------------


def _app(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> Any:
    return SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None)


async def _client(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> httpx.AsyncClient:
    api = build_app(_app(settings, config, db, manager), "tok")  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")  # type: ignore[arg-type]


async def test_a_session_gets_the_walls_of_its_project_and_a_read_only_primary_gets_no_inbox(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    code, docs = tmp_path / "code", tmp_path / "docs"
    (code / ".git").mkdir(parents=True)
    (docs / ".git").mkdir(parents=True)
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        project = await manager.projects.create("Atlas", [FolderSpec(str(code)), FolderSpec(str(docs), readonly=True)])
        state = await manager.create_session("Writer", project_id=project.id)
        assert state.services is not None and state.services.walls == Walls(readable=(code, docs), writable=(code,))
        assert (code / "inbox").is_dir()

        reader = await manager.create_session("Reader", project_id=project.id, folder_id=project.folders[1].id)
        assert reader.workspace == docs and reader.services is not None and not reader.services.workspace_writable
        # Nothing of ours is written into a folder the operator closed: no inbox, no note in its git exclude.
        assert not (docs / "inbox").exists() and not (docs / ".git" / "info" / "exclude").exists()
        # And a snapshot, which would be the one thing written into it, is not taken.
        assert await manager.checkpoint(reader, kind="before") is None
        # An attachment would land in the folder's inbox, so it is refused with the reason.
        staged = tmp_path / "photo.png"
        staged.write_bytes(b"png")
        with pytest.raises(RuntimeError, match="read-only for Reader, so attachments have nowhere to go"):
            await manager.submit(reader.session.id, "look", [Attachment(path=staged, name="photo.png")])
        assert not (docs / "inbox").exists()

        own = await manager.create_session("Own", project_id=project.id, own_directory=True)
        assert own.services is not None and own.services.walls == Walls(readable=(own.workspace,), writable=(own.workspace,))

        # A folder made read-only later is read-only to the sessions already running once the project is reloaded.
        changed = await manager.projects.update_folder(project.id, project.folders[0].id, readonly=True)
        assert changed.readonly
        await manager.reload_project(await manager.projects.get(project.id), project.id)
        assert manager.live_state(state.session.id).services.walls.writable == ()  # type: ignore[union-attr]
    finally:
        await manager.close()


async def test_the_file_pane_switches_folders_within_the_walls(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    code, docs, outside = tmp_path / "code", tmp_path / "docs", tmp_path / "outside"
    for path in (code, docs, outside):
        path.mkdir()
    (code / "main.py").write_text("print('code')\n", encoding="utf-8")
    (docs / "guide.md").write_text("the guide\n", encoding="utf-8")
    (outside / "secret.txt").write_text("not yours\n", encoding="utf-8")
    (docs / "escape").symlink_to(outside)
    host = tmp_path / "host-side"
    host.mkdir()
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        project = await manager.projects.create("Atlas", [FolderSpec(str(code)), FolderSpec(str(docs), label="Docs", readonly=True)])
        on_host = await manager.projects.add_folder(project.id, str(host), env="host")
        docs_id = project.folders[1].id
        sid = (await manager.create_session("Writer", project_id=project.id)).session.id
        own = (await manager.create_session("Own", project_id=project.id, own_directory=True)).session.id
        async with await _client(settings, config, db, manager) as client:
            detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
            assert detail["folder_id"] == project.folders[0].id
            assert [(f["id"], f["label"], f["readonly"], f["writable"]) for f in detail["folders"]] == [(project.folders[0].id, "", False, True), (docs_id, "Docs", True, False)]
            # The host folder is not offered to the pane of a container session, and an own-directory session is offered none.
            assert on_host.id not in {f["id"] for f in detail["folders"]}
            assert (await client.get(f"/api/sessions/{own}", headers=HEADERS)).json()["folders"] == []

            base = f"/api/sessions/{sid}/folders/{docs_id}"
            listing = (await client.get(f"{base}/files", headers=HEADERS)).json()
            assert {e["name"] for e in listing["entries"]} == {"guide.md"}, "a link out of the folder is not listed"
            assert (await client.get(f"{base}/download", headers=HEADERS, params={"path": "guide.md"})).text == "the guide\n"
            found = (await client.get(f"{base}/files/search", headers=HEADERS, params={"q": "guide"})).json()
            assert [r["path"] for r in found["results"]] == ["guide.md"]
            # The way out is refused under the folder exactly as under the session's own.
            assert (await client.get(f"{base}/files", headers=HEADERS, params={"path": "../outside"})).status_code == 400
            assert (await client.get(f"{base}/download", headers=HEADERS, params={"path": "escape/secret.txt"})).status_code == 400
            # The session's own folder answers at both addresses.
            assert {e["name"] for e in (await client.get(f"/api/sessions/{sid}/files", headers=HEADERS)).json()["entries"]} >= {"main.py"}
            mine = (await client.get(f"/api/sessions/{sid}/folders/{project.folders[0].id}/files", headers=HEADERS)).json()
            assert {e["name"] for e in mine["entries"]} >= {"main.py"}

            # An upload goes where the agent could write, and nowhere else.
            refused = await client.post(f"{base}/files/upload", headers=HEADERS, files={"files": ("x.txt", io.BytesIO(b"x"))})
            assert refused.status_code == 403 and "read-only" in refused.json()["detail"]
            assert not (docs / "x.txt").exists()
            uploaded = await client.post(f"/api/sessions/{sid}/folders/{project.folders[0].id}/files/upload", headers=HEADERS, files={"files": ("x.txt", io.BytesIO(b"x"))})
            assert uploaded.status_code == 200 and (code / "x.txt").read_bytes() == b"x"

            # A folder outside the walls, and a folder of no project, are refused.
            assert (await client.get(f"/api/sessions/{sid}/folders/{on_host.id}/files", headers=HEADERS)).status_code == 403
            assert (await client.get(f"/api/sessions/{own}/folders/{docs_id}/files", headers=HEADERS)).status_code == 403
            assert (await client.get(f"/api/sessions/{sid}/folders/f-nothing/files", headers=HEADERS)).status_code == 404
    finally:
        await manager.close()


def test_the_real_path_is_what_the_walls_judge(tree: SimpleNamespace) -> None:
    """A folder given through a link is the folder it points at: the walls compare real paths on both sides."""
    alias = tree.code.parent / "code-link"
    os.symlink(tree.code, alias)
    walls = Walls(readable=(alias,), writable=())
    assert walls.root_of(tree.code / "a.py") == alias
