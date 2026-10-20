"""The workspaces' snapshot stores live in the sealed state directory, not in the workspaces.

A store inside the workspace was a git directory the agent could write: a hook planted in it ran as
the host at the next snapshot, and a rewritten history decided what the operator's undo put back.
These check the one migration that moves the existing stores out, and the sweep that removes a store
once its workspace is gone, which deleting the workspace used to do by itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.checkpoint_retention import RetentionBounds
from daedalus.host.checkpoints import WORKSPACE_FILE, Checkpoints, store_dir
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import MIGRATIONS, Database

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost"}


def _legacy_store(workspace: Path, text: str) -> str:
    """A store where the old code kept it, ``<workspace>/.checkpoints``, with one snapshot of ``text``."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.md").write_text(text)
    git = ["git", "--git-dir", str(workspace / ".checkpoints"), "--work-tree", str(workspace)]
    subprocess.run([*git, "init", "-q"], check=True)
    (workspace / ".checkpoints" / "info").mkdir(exist_ok=True)
    (workspace / ".checkpoints" / "info" / "exclude").write_text(".checkpoints/\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "before"], check=True, env={"PATH": "/usr/bin:/bin", **IDENTITY})
    return subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


async def _reopen_one_schema_back(settings: Settings, db: Database) -> Database:
    await db.execute(f"UPDATE schema_version SET version = {len(MIGRATIONS) - 1}")
    await db.close()
    reopened = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
    await reopened.open()
    return reopened


async def test_the_migration_moves_every_store_out_of_its_workspace(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    state = await manager.create_session("old store")
    workspace = state.workspace
    await manager.close()
    sha = _legacy_store(workspace, "first draft")
    orphan = settings.workspaces_dir / "left-behind"
    orphan_sha = _legacy_store(orphan, "nobody's")

    db = await _reopen_one_schema_back(settings, db)
    try:
        assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
        for folder, head in ((workspace, sha), (orphan, orphan_sha)):
            checkpoints = Checkpoints(folder, settings.checkpoints_dir)
            assert not (folder / ".checkpoints").exists()
            assert checkpoints.git_dir.is_relative_to(settings.state_dir)
            assert await checkpoints.head() == head
            assert (checkpoints.git_dir / WORKSPACE_FILE).read_text().strip() == str(folder.resolve())
        # The moved store still undoes: the snapshot taken where it used to live puts the file back.
        (workspace / "notes.md").write_text("later")
        await Checkpoints(workspace, settings.checkpoints_dir).restore(sha)
        assert (workspace / "notes.md").read_text() == "first draft"
        assert not (workspace / ".checkpoints").exists()

        db = await _reopen_one_schema_back(settings, db)
        assert await Checkpoints(workspace, settings.checkpoints_dir).head()
    finally:
        await db.close()


async def test_a_store_goes_when_its_managed_workspace_is_deleted(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("short-lived")
        assert await manager.checkpoint(state, kind="before")
        managed = store_dir(settings.checkpoints_dir, state.workspace)
        elsewhere = tmp_path / "usb-disk" / "project"
        await Checkpoints(elsewhere, settings.checkpoints_dir).snapshot("first")
        unmounted = store_dir(settings.checkpoints_dir, elsewhere)
        subprocess.run(["rm", "-rf", str(elsewhere)], check=True)
        # The chat is deleted, and its folder goes whichever way it goes (the orphan sweep, by hand).
        assert await manager.delete_session(state.session.id)
        subprocess.run(["rm", "-rf", str(state.workspace)], check=True)
        await manager.checkpoint_retention.run(RetentionBounds(keep_days=30, total_max_gb=0, keep_last=2))
        assert not managed.exists()
        assert (unmounted / "HEAD").exists(), "a folder that is not there may only be unmounted; its undo stays"
    finally:
        await manager.close()


@pytest.mark.parametrize("name", ["a", "a b", "отчёт"])
def test_two_folders_of_one_name_never_share_a_store(tmp_path: Path, name: str) -> None:
    first, second = tmp_path / "one" / name, tmp_path / "two" / name
    root = tmp_path / "checkpoints"
    assert store_dir(root, first) != store_dir(root, second)
    assert store_dir(root, first).parent == root
