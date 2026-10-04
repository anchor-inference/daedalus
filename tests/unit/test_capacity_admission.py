"""One host cap covers every staff runtime and preserves control admission."""

from __future__ import annotations

import asyncio

from daedalus.stores.capacity import claim_in, finish_in, snapshot_in
from daedalus.stores.database import Database


async def _project(db: Database, identity: str) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES (?,?,?,?)",
                     (identity, identity, "now", "{}"))


async def _claim(db: Database, identity: str, project: str, *, role: str = "worker",
                 runtime: str = "daedalus", cap: int = 2) -> str | None:
    async with db.transaction() as conn:
        return await claim_in(conn, attempt_id=identity, project_id=project,
                              runtime_kind=runtime, role_class=role, generation=1, cap=cap)


async def test_two_projects_cannot_take_the_same_last_worker_slot(db: Database) -> None:
    await _project(db, "first")
    await _project(db, "second")
    results = await asyncio.gather(_claim(db, "native", "first"),
                                   _claim(db, "terminal", "second", runtime="cli"))
    assert sum(result is None for result in results) == 1
    assert "reserved for coordination or review" in next(result for result in results if result)
    async with db.transaction() as conn:
        view = await snapshot_in(conn, generation=1, cap=2)
    assert view["reserved"] == 1 and view["available"] == 1


async def test_control_work_can_use_the_slot_worker_flood_preserves(db: Database) -> None:
    await _project(db, "one")
    await _project(db, "two")
    assert await _claim(db, "worker", "one") is None
    assert await _claim(db, "waiting", "one") is not None
    assert await _claim(db, "review", "two", role="reviewer") is None
    assert "host slots" in (await _claim(db, "coordinator", "two", role="coordinator") or "")


async def test_an_operator_terminal_also_consumes_host_capacity(db: Database) -> None:
    await _project(db, "one")
    await db.execute("INSERT INTO terminals(id,env,owner_kind,cwd,created_at)"
                     " VALUES ('shell','host','session','/tmp','now')")
    assert "reserved for coordination or review" in (await _claim(db, "worker", "one") or "")
    assert await _claim(db, "reviewer", "one", role="reviewer") is None


async def test_expired_unbound_start_releases_once(db: Database) -> None:
    await _project(db, "one")
    assert await _claim(db, "old", "one", role="reviewer") is None
    await db.execute("UPDATE capacity_reservations SET expires_at = '2000-01-01' WHERE attempt_id = 'old'")
    async with db.transaction() as conn:
        first = await snapshot_in(conn, generation=1, cap=2)
        second = await snapshot_in(conn, generation=1, cap=2)
    assert first == second
    assert first["reserved"] == 0 and first["available"] == 2
    assert (await db.fetchone("SELECT state FROM capacity_reservations WHERE attempt_id = 'old'"))[0] == "released"


async def test_older_project_goes_before_new_worker_after_release(db: Database) -> None:
    await _project(db, "one")
    await _project(db, "two")
    assert await _claim(db, "running", "one") is None
    assert await _claim(db, "older", "two") is not None
    async with db.transaction() as conn:
        await finish_in(conn, "running", started=False)
    assert "older project" in (await _claim(db, "newer", "one") or "")
    assert await _claim(db, "older", "two") is None
