"""A task cancelled while its transaction begins leaves the database usable.

The lock was released only on an Exception; a cancellation is a BaseException, so a run cancelled by a
shutdown while its BEGIN was in flight kept the lock, and every later query waited forever. That hung
the unit suite about one run in ten, silently, in a test's closing manager.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from daedalus.stores.database import Database


async def test_a_cancelled_begin_releases_the_lock_and_leaves_no_transaction_open(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    real = db.conn.execute
    stalled = asyncio.Event()

    async def execute(sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql == "BEGIN IMMEDIATE":
            stalled.set()
            await asyncio.Event().wait()  # a BEGIN still in flight when the cancellation comes
        return await real(sql, *args, **kwargs)

    monkeypatch.setattr(db.conn, "execute", execute)

    async def writer() -> None:
        async with db.transaction() as conn:
            await conn.execute("CREATE TABLE never (x INTEGER)")

    task = asyncio.create_task(writer())
    await stalled.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    monkeypatch.setattr(db.conn, "execute", real)
    assert not db._lock.locked(), "the cancelled BEGIN kept the connection lock"
    async with asyncio.timeout(5):
        assert await db.fetchone("SELECT 1 AS one") is not None
        async with db.transaction() as conn:
            await conn.execute("CREATE TABLE after_cancel (x INTEGER)")
