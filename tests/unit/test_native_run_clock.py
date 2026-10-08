"""A native Daedalus member's attempt clock: an admitted run is ready, the model's first answer settles
the clock, and a run the host does stop says why in the member's session.

A native member used to die thirty seconds into good work: nothing moved its attempt past "ready", so
the deadline sweep stopped the run, and the session ended on "the host observed the owned runtime
exit" with no word of the deadline behind it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import Settings
from daedalus.extensions.phase_expiry import PhaseExpiry
from daedalus.extensions.runtime_observations import observe_exit, observe_native_output
from daedalus.staff_runtime import LiveSession
from daedalus.stores.database import Database
from daedalus.stores.staff import Staff
from tests.support.authorized_launch import operator_assignment
from tests.unit.test_orchestrator import Rig, events, rig
from tests.unit.test_orchestrator_team import fake
from tests.unit.test_staff_runtime import board_task, close_team


async def started_run(r: Rig) -> tuple[Staff, LiveSession, str]:
    """A native member at work: its task assigned, and a run admitted through the host's own hook."""
    fake(r)
    member = await r.manager.staff.hire(r.project.id, name="Ada", role="Ops", isolation="shared")
    await operator_assignment(r.team, member, await board_task(r.manager, r.project, "Panel check"))
    live = await r.team.live_of(member)
    assert live is not None and live.session.session_id
    source = await r.manager.db.fetchone("SELECT tenant_id FROM sessions WHERE id = ?", (live.session.session_id,))
    run_id = uuid.uuid4().hex
    stamp = datetime.now(UTC).isoformat()
    await r.manager.db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                               " VALUES (?,?,?,'running',?,?)", (run_id, source["tenant_id"], live.session.session_id, stamp, stamp))
    await r.team.admit_run(live.id, live.session.session_id, run_id)
    return member, live, run_id


async def clocks(r: Rig) -> list[tuple[str, str]]:
    rows = await r.manager.db.fetchall("SELECT phase,outcome FROM attempt_phase_clocks ORDER BY started_at")
    return [(row["phase"], row["outcome"]) for row in rows]


async def test_an_admitted_native_run_outlives_the_ready_deadline_and_its_first_answer_settles_the_clock(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        _member, live, run_id = await started_run(r)
        # The run is ready once admitted: it waits for the model's answer, as long as a member may be silent.
        assert (await clocks(r))[-1] == ("first_output", "active")
        waiting = await r.manager.db.fetchone("SELECT started_at,deadline_at FROM attempt_phase_clocks WHERE phase = 'first_output'")
        allowed = datetime.fromisoformat(waiting["deadline_at"]) - datetime.fromisoformat(waiting["started_at"])
        assert allowed == timedelta(minutes=r.manager.config.staff.silence_minutes)
        r.team.app.extensions.pop("lifecycle", None)
        sweep = PhaseExpiry(r.team.app)
        assert await sweep.step(at=datetime.now(UTC) + timedelta(seconds=45)) == 0
        assert (await r.manager.db.fetchone("SELECT state FROM execution_attempts"))[0] == "running"
        # The model answered: the start is over, and nothing is left for the sweep to stop.
        session_id = live.session.session_id
        r.manager.live_state = lambda sid: SimpleNamespace(metadata={"staff_session_id": live.id}) if sid == session_id else None  # type: ignore[method-assign]
        await r.team.on_turn_event(session_id, TurnEvent(type=EventType.MESSAGE_STOP, run_id=run_id, payload={}))
        assert not await observe_native_output(r.team.app, live.id)
        assert all(outcome == "completed" for _phase, outcome in await clocks(r))
        assert await sweep.step(at=datetime.now(UTC) + timedelta(days=1)) == 0
        assert (await r.manager.db.fetchone("SELECT state FROM execution_attempts"))[0] == "running"
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_run_stopped_at_a_deadline_ends_its_session_with_the_deadline_as_the_reason(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        member, live, run_id = await started_run(r)
        r.team.app.extensions.pop("lifecycle", None)
        assert await PhaseExpiry(r.team.app).step(at=datetime.now(UTC) + timedelta(days=1)) == 1
        await r.manager.db.execute("UPDATE runs SET status = 'cancelled' WHERE id = ?", (run_id,))
        assert await observe_exit(r.team.app, staff_session_id=live.id, runtime_ref=run_id,
                                  observed_status="cancelled", bus=r.manager.bus)
        row = await r.manager.db.fetchone("SELECT status,end_reason FROM staff_sessions WHERE id = ?", (live.id,))
        minutes = r.manager.config.staff.silence_minutes
        assert row["status"] == "exited"
        assert row["end_reason"] == f"the host stopped it: its first_output phase passed its deadline of {minutes * 60} s; the run ended cancelled"
        [ended] = [e for e in await events(r.manager, "staff.status", staff_id=member.id) if e.payload.get("status") == "exited"]
        assert ended.payload["detail"] == row["end_reason"]
    finally:
        await close_team(r.manager)
        await r.manager.close()
