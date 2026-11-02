"""What the event log keeps: not the streaming fragments, not the same tool surface twice, not forever."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from protocore.contracts.types import Event, Run, RunStatus

from daedalus.stores.database import Database
from daedalus.stores.sqlite import SqliteEventStream, SqliteRunStore


def _event(name: str, run_id: str = "r1", **payload: object) -> Event:
    return Event(id=f"{name}-{id(payload)}", run_id=run_id, name=name, payload=dict(payload))


def test_a_slow_live_subscriber_is_disconnected_instead_of_blocking_the_run() -> None:
    queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=2)
    first = _event("content_block_delta", delta={"text": "one"})
    second = _event("content_block_delta", delta={"text": "two"})
    overflow = _event("content_block_delta", delta={"text": "three"})
    SqliteEventStream._offer(queue, first)
    SqliteEventStream._offer(queue, second)

    SqliteEventStream._offer(queue, overflow)

    assert queue.qsize() == 1
    assert queue.get_nowait() is None


async def test_streaming_fragments_reach_the_watcher_and_not_the_disk(db: Database) -> None:
    events = SqliteEventStream(db)
    seen: list[str] = []
    stream = events.subscribe("r1", "daedalus")

    async def watch() -> None:
        async for event in stream:
            seen.append(event.name)
            if event.name == "message_stop":
                return

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    for _ in range(50):
        await events.emit(_event("content_block_delta", delta={"type": "text_delta", "text": "x"}))
    await events.emit(_event("message_stop"))
    await asyncio.wait_for(task, timeout=5)
    assert seen.count("content_block_delta") == 50 and "message_stop" in seen
    rows = await db.fetchall("SELECT name FROM events")
    assert [r["name"] for r in rows] == ["message_stop"]


async def test_the_tool_surface_is_stored_once_and_named_by_its_digest(db: Database) -> None:
    events = SqliteEventStream(db)
    tools = [{"name": f"Tool{i}", "schema": {"x" * 200: "y" * 200}} for i in range(40)]
    for run in ("r1", "r2", "r3"):
        await events.emit(_event("tool_surface_advertised", run_id=run, tools=tools))
    rows = await db.fetchall("SELECT payload FROM events")
    assert len(rows) == 3 and all("Tool39" not in r["payload"] for r in rows)
    blobs = await db.fetchall("SELECT key, value FROM kv WHERE key LIKE 'event_blob:%'")
    assert len(blobs) == 1 and "Tool39" in blobs[0]["value"]
    stored = sum(len(r["payload"]) for r in rows) + len(blobs[0]["value"])
    assert stored < 3 * len(blobs[0]["value"])  # three runs, one copy


async def test_old_events_are_swept_and_the_blob_they_named_goes_with_them(db: Database) -> None:
    events = SqliteEventStream(db)
    await events.emit(_event("tool_surface_advertised", tools=[{"name": "One"}]))
    await events.emit(_event("message_stop"))
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    await db.execute("UPDATE events SET created_at = ?", (old,))
    fresh = _event("message_stop", run_id="r2")
    await events.emit(fresh)
    dropped = await events.prune(keep_days=7, max_rows=1000)
    assert dropped == 2
    rows = await db.fetchall("SELECT id FROM events")
    assert [r["id"] for r in rows] == [fresh.id]
    assert await db.fetchall("SELECT key FROM kv WHERE key LIKE 'event_blob:%'") == []


async def test_the_sweep_keeps_the_log_under_its_ceiling(db: Database) -> None:
    events = SqliteEventStream(db)
    for i in range(30):
        await events.emit(_event("tool_result", index=i))
    await events.prune(keep_days=365, max_rows=10)
    rows = await db.fetchall("SELECT payload FROM events ORDER BY seq")
    assert len(rows) == 10 and '"index": 29' in rows[-1]["payload"]


async def test_a_freelist_nothing_can_reclaim_is_named_once_with_the_command(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An existing database keeps the auto_vacuum it was created with, so incremental reclaim does
    nothing on it and says nothing. The space is real — hundreds of megabytes on a long-lived
    install — and `daedalus-server db vacuum` is the only way back, which nothing ever mentioned."""
    path = tmp_path / "legacy.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA auto_vacuum=NONE")
    raw.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, body TEXT)")
    raw.executemany("INSERT INTO big(body) VALUES (?)", [("x" * 4000,) for _ in range(4000)])
    raw.commit()
    raw.execute("DELETE FROM big")
    raw.commit()
    raw.close()

    db = Database(path)
    await db.open()
    try:
        assert int((await db.fetchone("PRAGMA auto_vacuum"))[0]) == 0
        with caplog.at_level(logging.WARNING):
            assert await db.reclaim() == 0
            assert await db.reclaim() == 0
        said = [r.getMessage() for r in caplog.records if "daedalus-server db vacuum" in r.getMessage()]
        assert len(said) == 1, "the operator is told once per process, not on every maintenance tick"
        assert "cannot be given back" in said[0]

        # And once the file is rewritten the reclaim works and the warning has nothing to say.
        before, after = await db.vacuum()
        assert after < before
        assert int((await db.fetchone("PRAGMA auto_vacuum"))[0]) == 2
    finally:
        await db.close()


async def test_the_age_sweep_leaves_a_run_that_is_still_going(db: Database) -> None:
    """A loop or a long agent run outlives events_keep_days; sweeping by age alone would take its
    early events out from under the live view while it is still producing more."""
    events = SqliteEventStream(db)
    runs = SqliteRunStore(db)
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    for run_id, status in (("finished", RunStatus.completed), ("ongoing", RunStatus.running)):
        await runs.create(Run(id=run_id, tenant_id="daedalus", session_id="s", status=status))
        await events.emit(_event("run.started", run_id=run_id))
        await db.execute("UPDATE events SET created_at = ? WHERE run_id = ?", (old, run_id))

    dropped = await events.prune(keep_days=30, max_rows=10_000)
    assert dropped == 1
    left = {str(r["run_id"]) for r in await db.fetchall("SELECT DISTINCT run_id FROM events")}
    assert left == {"ongoing"}
