"""Both published schema histories converge without losing calendar data."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from daedalus.stores import database as schema
from daedalus.stores.calendar_diagram_schema import migration as calendar_schema


async def test_calendar_history_replays_missing_orchestration_migrations(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "calendar-history.sqlite"
    migrations = schema.MIGRATIONS
    monkeypatch.setattr(schema, "MIGRATIONS", migrations[:schema.BRANCH_BASE_SCHEMA])
    db = schema.Database(path)
    await db.open()
    await db.close()

    # The other branch recorded calendar and diagram migrations in the same two
    # version slots later used by orchestration. Its rows must survive replay.
    with sqlite3.connect(path) as conn:
        conn.executescript(calendar_schema(schema.Database(path)))
        conn.execute("INSERT INTO calendar_accounts(id,provider,name,credentials_json,created_at)"
                     " VALUES ('account','google','Work','{}','2026-01-01')")
        conn.execute("INSERT INTO calendar_events(id,account_id,title,start_at,end_at,updated_at)"
                     " VALUES ('event','account','Review','2026-01-02','2026-01-03','2026-01-01')")
        conn.execute("INSERT INTO diagrams(id,title,scene_json,created_at,updated_at)"
                     " VALUES ('diagram','Plan','{}','2026-01-01','2026-01-01')")
        conn.execute("INSERT INTO diagram_revisions(diagram_id,version,title,scene_json,saved_at)"
                     " VALUES ('diagram',1,'Plan','{}','2026-01-01')")
        conn.execute("UPDATE schema_version SET version = ?", (schema.BRANCH_BASE_SCHEMA + 2,))

    monkeypatch.setattr(schema, "MIGRATIONS", migrations)
    db = schema.Database(path)
    await db.open()
    assert (await db.fetchone("SELECT version FROM schema_version"))["version"] == len(migrations)
    assert (await db.fetchone("SELECT name FROM calendar_accounts WHERE id = 'account'"))["name"] == "Work"
    assert (await db.fetchone("SELECT title FROM calendar_events WHERE id = 'event'"))["title"] == "Review"
    assert (await db.fetchone("SELECT title FROM diagrams WHERE id = 'diagram'"))["title"] == "Plan"
    assert (await db.fetchone("SELECT count(*) AS n FROM diagram_revisions WHERE diagram_id = 'diagram'"))["n"] == 1
    assert await db.fetchone("SELECT name FROM sqlite_master WHERE name = 'domain_collection_revisions'")
    assert [tuple(row) for row in await db.fetchall("PRAGMA integrity_check")] == [("ok",)]
    assert await db.fetchall("PRAGMA foreign_key_check") == []
    await db.close()
    db = schema.Database(path)
    await db.open()
    assert (await db.fetchone("SELECT count(*) AS n FROM diagram_revisions WHERE diagram_id = 'diagram'"))["n"] == 1
    await db.close()


async def test_orchestration_history_adds_calendar_schema(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "orchestration-history.sqlite"
    migrations = schema.MIGRATIONS
    monkeypatch.setattr(schema, "MIGRATIONS", migrations[:-1])
    db = schema.Database(path)
    await db.open()
    await db.close()

    monkeypatch.setattr(schema, "MIGRATIONS", migrations)
    db = schema.Database(path)
    await db.open()
    assert (await db.fetchone("SELECT version FROM schema_version"))["version"] == len(migrations)
    for name in ("calendar_accounts", "calendar_events", "diagrams", "diagram_revisions"):
        assert await db.fetchone("SELECT name FROM sqlite_master WHERE name = ?", (name,))
    assert [tuple(row) for row in await db.fetchall("PRAGMA integrity_check")] == [("ok",)]
    assert await db.fetchall("PRAGMA foreign_key_check") == []
    await db.close()
