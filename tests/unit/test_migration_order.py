"""A database that already carries the vision model's effort column opens with every migration after it.

The vision effort column and the board's origin index, the task extras and the project approvals were
written at the same time on two lines of work, each appended where the list ended. An installation
upgraded with the vision effort first stands at that column's number; had the others kept the place
they were written in, the one written at that same number would have been taken as applied and never
run. So the column comes first, and this test opens a file left there and asks for what came after.
Each migration is found by what it makes, never by its number, since more are appended after these.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.stores import database as database_module
from daedalus.stores.database import MIGRATIONS, PROJECT_APPROVAL_MIGRATION, Database


def _step(name: str) -> int:
    return next(i for i, step in enumerate(MIGRATIONS) if getattr(getattr(step, "step", None), "__name__", "") == name)


def _sql(fragment: str) -> int:
    return next(i for i, step in enumerate(MIGRATIONS) if isinstance(step, str) and fragment in step)


VISION_EFFORT = _step("_session_vision_effort")
ORIGIN_INDEX = _sql("board_tasks_by_origin")
TASK_EXTRAS = _step("_task_extras")
APPROVALS = next(i for i, step in enumerate(MIGRATIONS) if step is PROJECT_APPROVAL_MIGRATION)


def test_the_vision_effort_column_comes_before_the_board_and_the_approvals() -> None:
    assert VISION_EFFORT < ORIGIN_INDEX < TASK_EXTRAS < APPROVALS


async def _schema(db: Database) -> list[tuple[str, str]]:
    return [(r["name"], r["sql"] or "") for r in await db.fetchall("SELECT name, sql FROM sqlite_master ORDER BY name")]


async def test_a_file_at_the_vision_effort_gets_the_board_and_the_approvals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.sqlite"
    with monkeypatch.context() as patch:
        patch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:VISION_EFFORT + 1])
        db = Database(path, local_env="container")
        await db.open()
        try:
            assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == VISION_EFFORT + 1
            assert "vision_effort" in {r["name"] for r in await db.fetchall("PRAGMA table_info(live_control)")}
            assert not await db.fetchall("SELECT 1 FROM sqlite_master WHERE name IN ('board_tasks_by_origin', 'app_events_by_task', 'project_approval_rules')")
            await db.execute("INSERT INTO board_tasks(id, title, status, priority, created_at, updated_at) VALUES ('t1', 'kept', 'todo', 3, 't', 't')")
        finally:
            await db.close()

    db = Database(path, local_env="container")
    await db.open()
    try:
        assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
        names = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master")}
        assert {"board_tasks_by_origin", "app_events_by_task", "project_approval_rules"} <= names
        columns = {r["name"] for r in await db.fetchall("PRAGMA table_info(board_tasks)")}
        assert {"due_at", "assignee_operator"} <= columns
        assert [r["id"] for r in await db.fetchall("SELECT id FROM board_tasks")] == ["t1"]
        assert [r[0] for r in await db.fetchall("PRAGMA integrity_check")] == ["ok"]
        assert await db.fetchall("PRAGMA foreign_key_check") == []
        schema = await _schema(db)
    finally:
        await db.close()

    again = Database(path, local_env="container")  # a second start runs nothing and changes nothing
    await again.open()
    try:
        assert int((await again.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
        assert await _schema(again) == schema
    finally:
        await again.close()
