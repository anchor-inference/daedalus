"""Funded alternatives retain separate admission identities on one task."""

import pytest

from daedalus.host.launch_queue import Entry, LaunchQueue
from daedalus.stores.database import Database


def _entry(staff_id: str, slot_id: str | None = None) -> Entry:
    return Entry("project", staff_id, staff_id, "task", 3, False, "operator",
                 capacity_slot_id=slot_id)


def _queue(active_count: int, *, reservation_valid: bool = True) -> tuple[LaunchQueue, list[str]]:
    started = []

    async def concurrency(project_id: str) -> int:
        assert project_id == "project"
        return 2

    async def active(project_id: str) -> int:
        assert project_id == "project"
        return active_count

    async def active_in(conn: object, project_id: str) -> int:
        assert conn is not None and project_id == "project"
        return active_count

    async def concurrency_in(conn: object, project_id: str) -> int:
        assert conn is not None and project_id == "project"
        return 2

    async def ready(entry: Entry) -> None:
        return None

    async def launch(entry: Entry) -> None:
        started.append(entry.staff_id)

    async def check_reserved(entry: Entry) -> bool:
        return reservation_valid and entry.capacity_slot_id in ("first", "second")

    return LaunchQueue(concurrency=concurrency, active=active, ready=ready, free=ready,
                       launch=launch, capacity=lambda: None, stagger=lambda: 0,
                       check_reserved=check_reserved, active_in=active_in,
                       concurrency_in=concurrency_in), started


async def test_two_funded_members_share_task_without_replacing_each_other() -> None:
    queue, started = _queue(2)
    first = _entry("one", "first")
    second = _entry("two", "second")
    queue.add(first)
    queue.add(second)
    assert [entry.capacity_slot_id for entry in queue.entries("project")] == ["first", "second"]
    await queue.pump("project")
    await queue.pump("project")
    assert started == ["one", "two"]
    assert queue.entries("project") == []
    queue.close()


async def test_unfunded_member_fails_before_launch() -> None:
    queue, started = _queue(2, reservation_valid=False)
    with pytest.raises(ValueError, match="reservation"):
        await queue.offer(_entry("one", "first"))
    assert started == []
    assert queue.entries("project") == []
    queue.close()


async def test_pair_capacity_guard_counts_both_slots_before_commit() -> None:
    queue, _ = _queue(0)
    with pytest.raises(ValueError, match="admission guard"):
        await queue.check_pair_capacity("project")
    async with queue.admission_guard():
        assert await queue.check_pair_capacity("project") is True
        assert await queue.check_pair_capacity_in(object(), "project") is True
    busy, _ = _queue(1)
    async with busy.admission_guard():
        assert await busy.check_pair_capacity("project") is False
        assert await busy.check_pair_capacity_in(object(), "project") is False
    queue.close()
    busy.close()


async def test_ordinary_assignment_replaces_only_its_own_task_entry() -> None:
    queue, _ = _queue(0)
    queue.add(_entry("old"))
    queue.add(_entry("new"))
    assert [entry.staff_id for entry in queue.entries("project")] == ["new"]
    queue.close()


async def test_pair_capacity_reads_only_the_command_transaction(db: Database) -> None:
    async def outside(_: str) -> int:
        pytest.fail("pair capacity opened a separate reader")

    async def ready(_: Entry) -> None:
        return None

    async def launch(_: Entry) -> None:
        pytest.fail("capacity check launched a worker")

    async def active_in(conn, project_id: str) -> int:
        assert project_id == "project"
        async with conn.execute("SELECT value FROM kv WHERE key = 'pair_active'") as cursor:
            row = await cursor.fetchone()
        return int(row["value"])

    async def concurrency_in(conn, project_id: str) -> int:
        assert project_id == "project"
        async with conn.execute("SELECT 2 AS slots") as cursor:
            row = await cursor.fetchone()
        return int(row["slots"])

    queue = LaunchQueue(concurrency=outside, active=outside, ready=ready, free=ready,
                        launch=launch, capacity=lambda: None, stagger=lambda: 0,
                        active_in=active_in, concurrency_in=concurrency_in)
    try:
        async with queue.admission_guard():
            async with db.transaction() as conn:
                await conn.execute("INSERT INTO kv(key,value) VALUES ('pair_active','0')")
                assert await queue.check_pair_capacity_in(conn, "project") is True
                await conn.execute("UPDATE kv SET value = '1' WHERE key = 'pair_active'")
                assert await queue.check_pair_capacity_in(conn, "project") is False
    finally:
        queue.close()
