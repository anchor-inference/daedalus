"""One host cap covers every staff runtime and preserves control admission."""

from __future__ import annotations

import asyncio

from daedalus.stores.capacity import claim_in, finish_in, snapshot_in, withdraw_in
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


async def test_an_agent_terminal_consumes_host_capacity_and_an_operator_shell_does_not(db: Database) -> None:
    await _project(db, "one")
    await db.execute("INSERT INTO terminals(id,env,owner_kind,cwd,created_at,created_by)"
                     " VALUES ('mine','host','free','/tmp','now','operator')")
    async with db.transaction() as conn:
        assert (await snapshot_in(conn, generation=1, cap=2))["active"] == 0
    await db.execute("INSERT INTO terminals(id,env,owner_kind,cwd,created_at,created_by)"
                     " VALUES ('shell','host','session','/tmp','now','agent')")
    assert "reserved for coordination or review" in (await _claim(db, "worker", "one") or "")
    assert await _claim(db, "reviewer", "one", role="reviewer") is None


async def _member(db: Database, name: str, *, kind: str, status: str, ended: bool = False) -> None:
    """A staff member with one session bound to an attempt whose claim is active."""
    await db.execute("INSERT OR IGNORE INTO board_tasks(id,title,status,priority,project_id,created_at,updated_at)"
                     " VALUES ('card','Card','doing',3,'one','now','now')")
    await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                     " VALUES (?,'one',?,?,'operator','now')", (name, name, "claude" if kind == "cli" else "daedalus"))
    await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status,status_at,started_at,ended_at)"
                     " VALUES (?,?,?,'card',?,'now','now',?)", (f"ss-{name}", name, kind, status, "now" if ended else None))
    await db.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,fence_token_hash,"
                     "state,created_at,updated_at,staff_session_id) VALUES (?,'card',1,1,'x','completed','now','now',?)",
                     (f"at-{name}", f"ss-{name}"))
    await db.execute("INSERT INTO capacity_reservations(id,attempt_id,project_id,host_id,runtime_kind,role_class,"
                     "state,expires_at,generation,created_at) VALUES (?,?,'one','local',?,'worker','active','now',1,'now')",
                     (f"r-{name}", f"at-{name}", "cli" if kind == "cli" else "daedalus"))


async def test_only_staff_that_is_really_there_holds_a_place(db: Database) -> None:
    """Finished work held places for good while its claims waited for an exit nobody recorded, and
    idle or suspended members held them too, until no card could start on a host doing nothing."""
    await _project(db, "one")
    await _member(db, "gone", kind="cli", status="exited", ended=True)
    await _member(db, "done", kind="daedalus", status="idle")
    await _member(db, "asleep", kind="cli", status="suspended")
    await _member(db, "busy", kind="daedalus", status="working")
    await _member(db, "open", kind="cli", status="idle")
    async with db.transaction() as conn:
        view = await snapshot_in(conn, generation=1, cap=4)
    assert (view["active"], view["reserved"]) == (2, 0)
    gone = await db.fetchone("SELECT state FROM capacity_reservations WHERE id = 'r-gone'")
    assert gone[0] == "released"


async def test_a_members_own_session_does_not_count_against_its_next_card(db: Database) -> None:
    """An idle member on a full host could not be given its next card: its own session, which that
    start ends, was counted as the place the start needed."""
    await _project(db, "one")
    await _member(db, "mail", kind="cli", status="idle")
    await _member(db, "other", kind="cli", status="idle")
    async with db.transaction() as conn:
        assert (await snapshot_in(conn, generation=1, cap=3))["available"] == 1
        admitted = await claim_in(conn, attempt_id="next", project_id="one", runtime_kind="cli",
                                  role_class="worker", generation=1, cap=3, replacing="mail")
    assert admitted is None


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


async def test_a_launch_gone_to_wait_for_something_else_gives_up_its_place_in_line(db: Database) -> None:
    """A launch that once waited for a host place kept the head of the line after it went on to wait
    for something else (its member busy, its dependencies, its project's own limit): every other
    project's worker was refused with "an older project is waiting" while that one could not start."""
    await _project(db, "one")
    await _project(db, "two")
    assert await _claim(db, "running", "one") is None
    assert await _claim(db, "older", "two") is not None
    async with db.transaction() as conn:
        await withdraw_in(conn, "older")
        await finish_in(conn, "running", started=False)
    assert await _claim(db, "newer", "one") is None
    # Back in line once it waits for a place again, behind whoever came meanwhile.
    async with db.transaction() as conn:
        await finish_in(conn, "newer", started=False)
    assert await _claim(db, "older", "two") is None
