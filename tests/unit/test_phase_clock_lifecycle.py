"""Observed lifecycle events preserve distinct attempt waits and honest progress."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from daedalus.config import HarnessConfig
from daedalus.extensions.launch_controls import prepare_attempt
from daedalus.extensions.staff import Ingress
from daedalus.stores.database import Database
from daedalus.stores.phase_clocks import PhaseClocks
from tests.unit.test_launch_controls import OPERATOR, launch_fixture


async def test_cli_ready_and_chatter_do_not_count_as_first_output(db: Database) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        identity = await prepare_attempt(app, OPERATOR, member, task, session,
                                         fence_token=secrets.token_urlsafe(32))
        assert (await db.fetchone("SELECT phase FROM attempt_phase_clocks WHERE outcome = 'active'"))[0] == "prepare"
        async with db.transaction() as conn:
            assert await PhaseClocks(app.executions).advance(conn, identity,
                                                              from_phase="prepare", to_phase="spawn")
        team = SimpleNamespace(app=app, manager=SimpleNamespace(config=SimpleNamespace(harness=HarnessConfig())))
        ingress = Ingress(team)
        live = SimpleNamespace(id=session.id)
        await ingress.phase_event(live, "ready")
        before = await db.fetchone("SELECT deadline_at FROM attempt_phase_clocks"
                                   " WHERE phase = 'first_output'")
        await ingress.phase_event(live, "notification")
        waiting = await db.fetchone("SELECT deadline_at,last_signal_at,outcome FROM attempt_phase_clocks"
                                    " WHERE phase = 'first_output'")
        assert waiting["deadline_at"] == before["deadline_at"]
        assert waiting["last_signal_at"] is not None
        assert waiting["outcome"] == "active"
        await ingress.phase_event(live, "turn_completed", meaningful_output=True)
        idle = await db.fetchone("SELECT last_signal_at,last_progress_at FROM attempt_phase_clocks"
                                 " WHERE phase = 'idle'")
        assert idle["last_signal_at"] is None
        assert idle["last_progress_at"] is not None
        rows = await db.fetchall("SELECT phase,outcome FROM attempt_phase_clocks ORDER BY started_at")
        assert [(row["phase"], row["outcome"]) for row in rows] == [
            ("prepare", "completed"), ("spawn", "completed"), ("ready", "completed"),
            ("first_output", "completed"), ("idle", "completed")]
    finally:
        app.executions.release()


async def test_result_settles_the_active_clock_in_the_same_transaction(db: Database) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        identity = await prepare_attempt(app, OPERATOR, member, task, session,
                                         fence_token=secrets.token_urlsafe(32))
        async with db.transaction() as conn:
            await app.executions.complete(conn, identity, outcome="complete")
        row = await db.fetchone("SELECT a.state,c.outcome FROM execution_attempts a"
                                " JOIN attempt_phase_clocks c ON c.attempt_id = a.id")
        assert (row["state"], row["outcome"]) == ("completed", "completed")
    finally:
        app.executions.release()


async def test_late_output_keeps_expired_wait_and_does_not_break_ingress(db: Database) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        identity = await prepare_attempt(app, OPERATOR, member, task, session,
                                         fence_token=secrets.token_urlsafe(32))
        team = SimpleNamespace(app=app, manager=SimpleNamespace(config=SimpleNamespace(harness=HarnessConfig())))
        ingress = Ingress(team)
        live = SimpleNamespace(id=session.id)
        async with db.transaction() as conn:
            await PhaseClocks(app.executions).advance(conn, identity, from_phase="prepare", to_phase="spawn")
        await ingress.phase_event(live, "ready")
        deadline = await db.fetchone("SELECT deadline_at FROM attempt_phase_clocks WHERE phase = 'first_output'")
        async with db.transaction() as conn:
            assert await PhaseClocks(app.executions).expire(
                conn, identity, at=datetime.fromisoformat(deadline["deadline_at"]).astimezone(UTC) + timedelta(seconds=1)
            ) == "first_output"
        await ingress.phase_event(live, "turn_completed", meaningful_output=True)
        assert (await db.fetchone("SELECT outcome FROM attempt_phase_clocks WHERE phase = 'first_output'"))[0] == "timed_out"
        assert (await db.fetchone("SELECT count(*) FROM attempt_phase_clocks WHERE phase = 'idle'"))[0] == 0
    finally:
        app.executions.release()
