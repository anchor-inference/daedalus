"""A persisted running row is not a live process after a connection or host loss."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from daedalus.extensions.runtime_state import native_observation, task_runtime_state, terminal_observation


class Rows:
    def __init__(self, rows: dict[str, dict | None]) -> None:
        self.rows = rows

    async def fetchone(self, sql: str, params: tuple[str, ...] = ()) -> dict | None:
        if "FROM board_tasks" in sql:
            return self.rows.get("task")
        if "FROM execution_attempts" in sql:
            return self.rows.get("attempt")
        if "FROM staff_sessions" in sql:
            return self.rows.get("staff")
        if "FROM runs" in sql:
            return self.rows.get("run")
        return None


def test_running_row_needs_matching_live_run() -> None:
    run = {"status": "running", "updated_at": "2026-10-03T00:00:00+00:00"}
    assert native_observation(None, run, "run-one")[0] == "unknown"
    assert native_observation(SimpleNamespace(run_id="run-two", running=True), run, "run-one")[0] == "unknown"
    assert native_observation(SimpleNamespace(run_id="run-one", running=True), run, "run-one")[0] == "running"
    assert native_observation(None, {**run, "status": "completed"}, "run-one")[0] == "ended"


def test_terminal_row_needs_daemon_projection() -> None:
    assert terminal_observation({"status": "running", "live": None})[0] == "unknown"
    assert terminal_observation({"status": "running", "live": {"clients": 0}})[0] == "running"
    assert terminal_observation({"status": "exited", "exited_at": "now"}) == ("ended", "terminal_exit", "now")


@pytest.mark.asyncio
async def test_task_state_never_calls_start_or_reconnect() -> None:
    db = Rows({"task": {"id": "task-one", "session_id": "session-one", "run_id": "run-one", "current_attempt_id": "attempt-one"},
               "attempt": {"id": "attempt-one", "host_generation": 4, "state": "running"}, "staff": None,
               "run": {"status": "running", "updated_at": "2026-10-03T00:00:00+00:00"}})
    manager = SimpleNamespace(live_state=lambda _: None)
    state = await task_runtime_state(SimpleNamespace(db=db, manager=manager, extensions={}), "task-one")
    assert state["process_state"] == "unknown"
    assert state["attempt_id"] == "attempt-one"
    assert state["host_generation"] == 4
    assert state["observation"] == "unknown"


@pytest.mark.asyncio
async def test_remote_attempt_does_not_inherit_local_running_proof() -> None:
    db = Rows({"task": {"id": "task-one", "session_id": "session-one", "run_id": "run-one", "current_attempt_id": "attempt-one"},
               "attempt": {"id": "attempt-one", "host_generation": 2, "state": "running", "remote_host_id": "host-one"},
               "staff": None, "run": {"status": "running", "updated_at": "now"}})
    manager = SimpleNamespace(live_state=lambda _: SimpleNamespace(run_id="run-one", running=True))
    state = await task_runtime_state(SimpleNamespace(db=db, manager=manager, extensions={}), "task-one")
    assert state["process_state"] == "unknown"
    assert state["remote_host_id"] == "host-one"
    assert state["proof"] == "remote_execution_unconfirmed"
