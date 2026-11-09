"""A deleted chat takes its own scratch folder with it, and nothing that is anybody else's.

Every chat that named no project left its folder under the workspaces root when it was deleted, a folder a
guest or a stranger's agent had written in among them. The rule that removes it now is a shape, not a list:
an operator once forbade removing a protected project folder, and a name written down here would protect
that one folder and no other.
"""

from __future__ import annotations

from pathlib import Path

from daedalus.config import Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec, ProjectSettings
from tests.support.models import model_config


async def manager_for(settings: Settings, db: Database) -> SessionManager:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    return manager


async def test_a_scratch_chats_folder_goes_with_it(settings: Settings, db: Database) -> None:
    manager = await manager_for(settings, db)
    state = await manager.create_session("scratch")
    folder = state.workspace
    assert folder == settings.workspaces_dir / state.session.id
    (folder / "inbox" / "left-by-a-guest.txt").write_text("x")
    assert await manager.delete_session(state.session.id)
    assert not folder.exists()
    assert settings.workspaces_dir.is_dir(), "the root itself stays"
    await manager.close()


async def test_kept_when_the_operator_asks_to_keep_it(settings: Settings, db: Database) -> None:
    manager = await manager_for(settings, db)
    state = await manager.create_session("scratch")
    assert await manager.delete_session(state.session.id, delete_workspace=False)
    assert state.workspace.is_dir()
    await manager.close()


async def test_a_real_projects_folder_stays(settings: Settings, db: Database) -> None:
    manager = await manager_for(settings, db)
    project = await manager.projects.create("Work")
    state = await manager.create_session("in a project", project_id=project.id)
    assert await manager.delete_session(state.session.id)
    assert project.primary.path.is_dir(), "a project's folder never goes with a chat"
    await manager.close()


async def test_a_folder_a_second_chat_made_a_project_stays(settings: Settings, db: Database) -> None:
    manager = await manager_for(settings, db)
    first = await manager.create_session("first")
    assert first.project is not None
    second = await manager.create_session("second", project_id=first.project.id)
    assert await manager.delete_session(first.session.id)
    assert await manager.delete_session(second.session.id)
    assert first.workspace.is_dir(), "a scratch project a second chat joined became a project, and its folder is the operator's"
    await manager.close()


async def test_the_folder_a_chat_was_started_in_stays(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await manager_for(settings, db)
    own = tmp_path / "operators-folder"
    own.mkdir()
    (own / "notes.txt").write_text("mine")
    state = await manager.create_session("adopted", workspace=own)
    assert await manager.delete_session(state.session.id)
    assert (own / "notes.txt").read_text() == "mine"
    await manager.close()


async def test_an_ephemeral_project_whose_folder_is_not_the_shape_stays(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A project folder named like a project, but elsewhere: the shape, and not the name, is what decides."""
    manager = await manager_for(settings, db)
    elsewhere = tmp_path / "host" / "project-0bcf847a4280"
    elsewhere.mkdir(parents=True)
    project = await manager.projects.create("protected", [FolderSpec(str(elsewhere))], settings=ProjectSettings(ephemeral=True))
    state = await manager.create_session("chat", project_id=project.id)
    assert await manager.delete_session(state.session.id)
    assert elsewhere.is_dir()
    await manager.close()


async def test_a_folder_that_is_a_link_stays_and_so_does_what_it_points_at(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await manager_for(settings, db)
    state = await manager.create_session("scratch")
    target = tmp_path / "the-operators-data"
    target.mkdir()
    (target / "keep.txt").write_text("mine")
    folder = state.workspace
    for child in sorted(folder.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    folder.rmdir()
    folder.symlink_to(target)
    assert await manager.delete_session(state.session.id)
    assert (target / "keep.txt").read_text() == "mine" and folder.is_symlink()
    await manager.close()


async def test_a_folder_another_session_still_works_in_stays(settings: Settings, db: Database) -> None:
    manager = await manager_for(settings, db)
    state = await manager.create_session("scratch")
    folder = state.workspace
    assert state.project is not None
    other = await manager.projects.create("Elsewhere")
    lodger = await manager.create_session("lodger", project_id=other.id)
    # A session pointed into the folder by hand, as an older layout or an import could leave one.
    await db.execute("UPDATE project_folders SET path = ? WHERE project_id = ?", (str(folder), other.id))
    await manager.projects.list()
    assert await manager.delete_session(state.session.id)
    assert folder.is_dir()
    assert lodger.session.id
    await manager.close()


async def test_the_last_of_several_chats_takes_the_folder(settings: Settings, db: Database) -> None:
    """A subagent shares its parent's scratch project, so the folder goes with whichever of them is last."""
    manager = await manager_for(settings, db)
    parent = await manager.create_session("parent")
    assert parent.project is not None
    child = await manager.create_session("child", project_id=parent.project.id, metadata={"subagent_of": parent.session.id})
    assert await manager.delete_session(parent.session.id)
    assert parent.workspace.is_dir(), "the child still works there"
    assert await manager.delete_session(child.session.id)
    assert not parent.workspace.exists()
    await manager.close()
