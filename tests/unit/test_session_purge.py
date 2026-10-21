"""Deleting a session together with the ledger rows that name it."""

from __future__ import annotations

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database


async def _refusal(manager: SessionManager, session_id: str, run_id: str) -> None:
    """A provider's refusal recorded against a run: an immutable ledger row that names its session
    and run without a cascade."""
    await manager.db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                             " VALUES (?,'tenant',?,'error','2026-10-04T12:00:00+00:00','2026-10-04T12:00:00+00:00')",
                             (run_id, session_id))
    await manager.db.execute("INSERT INTO provider_failure_observations(id,session_id,run_id,provider_id,"
                             "provider_kind,model,status,failure_class,evidence_digest,observed_at)"
                             " VALUES (?,?,?,'vendor','openai_compat','model',401,'auth',?,'2026-10-04T12:00:00+00:00')",
                             (f"refusal-{run_id}", session_id, run_id, "a" * 64))


async def _triggers(db: Database) -> set[str]:
    return {row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'trigger'")}


async def test_a_chat_that_met_a_provider_refusal_can_be_deleted(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    """The refusal's record refused a plain DELETE and named its run without a cascade, so deleting
    such a chat failed on the foreign key after the deletion hooks had already run."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        chat = await manager.create_session("Refused once")
        await _refusal(manager, chat.session.id, "run-refused")
        guards = await _triggers(db)
        assert await manager.delete_session(chat.session.id)
        assert await db.fetchone("SELECT 1 FROM sessions WHERE id = ?", (chat.session.id,)) is None
        assert await db.fetchone("SELECT 1 FROM provider_failure_observations WHERE session_id = ?", (chat.session.id,)) is None
        assert await _triggers(db) == guards, "the ledgers keep their protection after the purge"
        with pytest.raises(Exception, match="retained"):
            other = await manager.create_session("Another")
            await _refusal(manager, other.session.id, "run-other")
            await db.execute("DELETE FROM provider_failure_observations WHERE run_id = 'run-other'")
    finally:
        await manager.close()
