"""A run's engine is told which tool call ids its session has already used.

The transcript keeps every call a compaction folded out of the history, and the
app pairs results with calls by id. The core keeps a new call clear of the ids
in the history it is handed; the ids beyond it come from the host.
"""
from __future__ import annotations

from datetime import UTC, datetime

from daedalus.config import Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.models import model_config


async def _used(db: Database, session_id: str, call_id: str) -> None:
    await db.execute(
        "INSERT INTO tool_calls(session_id, run_id, tool_call_id, name, at, duration_ms, ok) VALUES (?, ?, ?, ?, ?, 1, 1)",
        (session_id, "run-before", call_id, "Write", datetime.now(UTC).isoformat()),
    )


async def test_the_engine_never_issues_an_id_the_session_already_used(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("folded")
        other = await manager.create_session("elsewhere")
        await _used(db, state.session.id, "functions.Write:0")
        await _used(db, other.session.id, "functions.Read:1")

        engine = await manager._build_engine(state, "run-after")

        assert "functions.Write:0" in engine._tool_call_ids_used
        # Another session's calls are not this transcript's, and never collide with it.
        assert "functions.Read:1" not in engine._tool_call_ids_used
    finally:
        await manager.close()
