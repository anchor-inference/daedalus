"""The conditions on a member's card, read from its attempt's phase clocks and report."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daedalus.harness.conditions import blocking, derive, line, load
from daedalus.stores.database import Database


def clock(phase: str, started: str, deadline: str, outcome: str, *, signal: str | None = None,
          progress: str | None = None) -> dict[str, Any]:
    return {"phase": phase, "started_at": f"2026-10-14T{started}+00:00", "deadline_at": f"2026-10-14T{deadline}+00:00",
            "last_signal_at": f"2026-10-14T{signal}+00:00" if signal else None,
            "last_progress_at": f"2026-10-14T{progress}+00:00" if progress else None, "outcome": outcome}


STARTED = [
    clock("prepare", "18:10:03", "18:12:03", "completed"),
    clock("spawn", "18:10:04", "18:12:04", "completed"),
    clock("ready", "18:10:05", "18:10:35", "completed"),
]
ATTEMPT = {"state": "running", "created_at": "2026-10-14T18:10:03+00:00", "updated_at": "2026-10-14T18:10:05+00:00"}


def test_a_member_stopped_at_its_first_output_deadline_says_so() -> None:
    """The incident this exists for: the terminal chattered until the deadline without a word of
    output, the sweep stopped it, and the card read only "exited"."""
    found = derive(clocks=[*STARTED, clock("first_output", "18:10:05", "18:15:05", "timed_out", signal="18:14:43")],
                   attempt={**ATTEMPT, "state": "cancelled"}, report=None, session_status="exited",
                   end_reason="the host stopped it")
    assert [(c.name, c.status) for c in found] == [
        ("prepared", "true"), ("spawned", "true"), ("ready", "true"), ("first_output", "false"), ("reported", "false")]
    first = found[3]
    assert first.reason == "timed_out" and first.deadline is None and first.since == "2026-10-14T18:15:05+00:00"
    assert first.params == {"allowed_s": 300, "last_signal_at": "2026-10-14T18:14:43+00:00"}
    assert "within 300 s" in first.message and "18:14:43" in first.message and "not output" in first.message
    assert blocking(found) is first, "the failed step is read before the missing report it caused"
    assert line(found).startswith("condition: first_output:")
    assert found[4].reason == "ended" and found[4].params["end_reason"] == "the host stopped it"


def test_a_running_wait_carries_its_deadline_and_later_steps_wait_behind_it() -> None:
    now = datetime(2026, 10, 14, 18, 12, 5, tzinfo=UTC)
    found = derive(clocks=[*STARTED, clock("first_output", "18:10:05", "18:15:05", "active")],
                   attempt=ATTEMPT, report=None, session_status="working", now=now)
    first = next(c for c in found if c.name == "first_output")
    assert (first.status, first.reason, first.deadline) == ("false", "waiting", "2026-10-14T18:15:05+00:00")
    assert first.params["elapsed_s"] == 120 and first.params["allowed_s"] == 300
    assert found[-1].name == "reported" and found[-1].status == "unknown"
    assert blocking(found) is first


def test_login_appears_only_when_the_cli_asked_for_one() -> None:
    plain = derive(clocks=STARTED, attempt=ATTEMPT, report=None, session_status="working")
    assert "authenticated" not in [c.name for c in plain]
    asked = derive(clocks=[*STARTED[:2], clock("auth", "18:10:05", "18:20:05", "active")], attempt=ATTEMPT,
                   report=None, session_status="working", now=datetime(2026, 10, 14, 18, 11, tzinfo=UTC))
    names = {c.name: c for c in asked}
    assert names["authenticated"].reason == "waiting"
    assert names["ready"].reason == "not_reached" and names["first_output"].reason == "not_reached"


def test_a_finished_attempt_reads_true_throughout() -> None:
    found = derive(clocks=[*STARTED, clock("first_output", "18:10:05", "18:15:05", "completed", progress="18:10:09"),
                           clock("idle", "18:10:09", "18:15:09", "completed", progress="18:10:09")],
                   attempt={**ATTEMPT, "state": "completed"},
                   report={"outcome": "complete", "created_at": "2026-10-14T18:20:00+00:00"}, session_status="idle")
    assert all(c.status == "true" for c in found) and blocking(found) is None and line(found) == ""
    first = next(c for c in found if c.name == "first_output")
    assert first.params["took_s"] == 4 and first.since == "2026-10-14T18:10:09+00:00"


def test_a_turn_that_ended_without_a_report_is_false() -> None:
    found = derive(clocks=STARTED, attempt=ATTEMPT, report=None, session_status="turn_done_unseen",
                   session_status_at="2026-10-14T18:30:00+00:00")
    assert (found[-1].status, found[-1].reason, found[-1].since) == ("false", "turn_ended", "2026-10-14T18:30:00+00:00")


def test_no_attempt_no_conditions() -> None:
    assert derive(clocks=[], attempt=None, report=None, session_status="idle") == []


async def test_load_reads_the_newest_attempt_of_the_session(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
        await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,created_at,updated_at)"
                         " VALUES ('task','Menu','doing',3,'project','2026-01-01','2026-01-01')")
        await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                         " VALUES ('worker','project','Worker','claude','operator','2026-01-01')")
        await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status,status_at,started_at)"
                         " VALUES ('ss','worker','cli','task','working','2026-01-01','2026-01-01')")
        assert await load(db, staff_session_id="ss", session_status="working") == []
        for attempt, created in (("old", "2026-10-14T17:00:00+00:00"), ("new", "2026-10-14T18:10:03+00:00")):
            await db.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,fence_token_hash,"
                             "state,created_at,updated_at,staff_session_id) VALUES (?,'task',1,1,'x','failed',?,?,'ss')",
                             (attempt, created, created))
        await db.execute("INSERT INTO attempt_phase_clocks(attempt_id,phase,started_at,deadline_at,outcome)"
                         " VALUES ('new','prepare','2026-10-14T18:10:03+00:00','2026-10-14T18:12:03+00:00','timed_out')")
        found = await load(db, staff_session_id="ss", session_status="exited")
        assert found[0].name == "prepared" and found[0].reason == "timed_out"
    finally:
        await db.close()
