"""Read opens the files Skill hands out, in every session, and nothing writes them.

The defect these guard: Skill printed the absolute path of a skill's gallery entry and the skill said to
Read it before building, but the skills folder was outside the session's walls, so Read refused "outside
this project" and the agent read the file with ``cat`` in Exec instead.
"""

from __future__ import annotations

from pathlib import Path

from daedalus.config import Settings
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec
from daedalus.tools.files import read_file, write_file
from tests.unit.test_job_wake import context
from tests.unit.test_session_runner import ScriptedProvider, _manager


def a_skill_file(settings: Settings) -> Path:
    return next(path for path in sorted(settings.skills_dir.glob("*/*.md")) if path.is_file())


async def test_a_skill_file_reads_in_a_project_and_outside_one_and_is_never_written(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        code = tmp_path / "code"
        code.mkdir()
        project = await manager.projects.create("Atlas", [FolderSpec(str(code))])
        sessions = [(await manager.create_session("Plain")).session.id, (await manager.create_session("Atlas chat", project_id=project.id)).session.id]
        target = a_skill_file(settings)
        for sid in sessions:
            read = await read_file().invoke(context(sid), {"path": str(target)})
            assert not read.is_error, read.content
            assert target.read_text(encoding="utf-8").splitlines()[0] in read.content
            written = await write_file().invoke(context(sid), {"path": str(target), "content": "changed"})
            assert written.is_error and "read-only" in written.content
        assert target.read_text(encoding="utf-8") != "changed"
    finally:
        await manager.close()


async def test_a_refusal_does_not_point_at_the_shell(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        code = tmp_path / "code"
        code.mkdir()
        elsewhere = tmp_path / "elsewhere.txt"
        elsewhere.write_text("x")
        project = await manager.projects.create("Atlas", [FolderSpec(str(code))])
        sid = (await manager.create_session("Atlas chat", project_id=project.id)).session.id
        refused = await read_file().invoke(context(sid), {"path": str(elsewhere)})
        assert refused.is_error and "outside this project" in refused.content
        assert "Do not reach it another way (cat or cp in Exec)" in refused.content and "ask the operator" in refused.content
    finally:
        await manager.close()
