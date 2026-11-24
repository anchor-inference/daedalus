"""The board's origin index: made by its migration, kept on a second start, and used by the lookup it is for."""

from __future__ import annotations

from pathlib import Path

from daedalus.stores.database import MIGRATIONS, Database

PLAN_SQL = "EXPLAIN QUERY PLAN SELECT * FROM board_tasks WHERE origin_session_id IN (?, ?) AND removed_at IS NULL"


async def test_the_origin_index_is_made_once_and_the_rows_survive(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    db = Database(path, local_env="container")
    await db.open()
    for n in range(3):
        await db.execute(
            "INSERT INTO board_tasks(id, title, status, priority, origin_session_id, created_at, updated_at) VALUES (?, ?, 'todo', 3, ?, 't', 't')",
            (f"t{n}", f"task {n}", None if n == 0 else f"s{n}"),
        )
    # Put the file back where the previous release left it: no index, one migration behind.
    await db.execute("DROP INDEX board_tasks_by_origin")
    await db.execute(f"UPDATE schema_version SET version = {len(MIGRATIONS) - 1}")
    await db.close()

    db = Database(path, local_env="container")
    await db.open()
    try:
        assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
        assert [r["id"] for r in await db.fetchall("SELECT id FROM board_tasks ORDER BY id")] == ["t0", "t1", "t2"]
        plan = " ".join(str(r["detail"]) for r in await db.fetchall(PLAN_SQL, ("s1", "s2")))
        assert "board_tasks_by_origin" in plan
        assert [r[0] for r in await db.fetchall("PRAGMA integrity_check")] == ["ok"]
        assert await db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        await db.close()

    again = Database(path, local_env="container")  # a second start runs nothing and breaks nothing
    await again.open()
    try:
        assert int((await again.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
        assert len(await again.fetchall("SELECT name FROM sqlite_master WHERE name = 'board_tasks_by_origin'")) == 1
    finally:
        await again.close()
