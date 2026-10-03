"""Current execution observations, kept separate from the browser's transport state.

A persisted running row after a host restart is not proof that its process still exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from daedalus.app import Application


TERMINAL_RUNS = {"completed", "partial", "error", "cancelled"}


def native_observation(live: Any, run: Any, run_id: str | None) -> tuple[str, str, str | None]:
    """Only a matching in-process run or a durable terminal row proves a process outcome."""
    if run_id and live is not None and live.run_id == run_id and live.running:
        return "running", "live_run", datetime.now(UTC).isoformat()
    if run is not None and run["status"] in TERMINAL_RUNS:
        return "ended", "terminal_run", str(run["updated_at"])
    return "unknown", "unconfirmed_run", str(run["updated_at"]) if run is not None else None


def terminal_observation(view: dict[str, Any] | None) -> tuple[str, str, str | None]:
    """A stored running terminal needs a live daemon projection to count as running."""
    if view is None:
        return "unknown", "terminal_unavailable", None
    if view.get("status") == "running" and view.get("live") is not None:
        return "running", "live_terminal", datetime.now(UTC).isoformat()
    if view.get("status") in ("exited", "closed", "failed"):
        return "ended", "terminal_exit", view.get("exited_at")
    return "unknown", "terminal_unconfirmed", view.get("last_output_at")


async def task_runtime_state(app: Application, task_id: str) -> dict[str, Any]:
    task = await app.db.fetchone("SELECT id,session_id,run_id,current_attempt_id FROM board_tasks WHERE id = ?", (task_id,))
    if task is None:
        raise KeyError(task_id)
    attempt = await app.db.fetchone("SELECT * FROM execution_attempts WHERE id = ?", (task["current_attempt_id"],)) if task["current_attempt_id"] else None
    remote_host_id = attempt["remote_host_id"] if attempt is not None and "remote_host_id" in attempt.keys() else None
    staff = await app.db.fetchone("SELECT id,kind,session_id,terminal_id,status,ended_at FROM staff_sessions WHERE task_id = ? ORDER BY started_at DESC,id DESC LIMIT 1", (task_id,))
    session_id = str(staff["session_id"] or task["session_id"] or "") if staff else str(task["session_id"] or "")
    terminal_id = str(staff["terminal_id"] or "") if staff else ""
    if remote_host_id:
        process_state, proof, observed_at = "unknown", "remote_execution_unconfirmed", None
        run_id = None
    elif staff and staff["kind"] == "cli":
        terminals = app.extensions.get("terminals")
        try:
            terminal = await terminals.get(terminal_id) if terminals is not None and terminal_id else None
        except Exception:  # noqa: BLE001 — a failed live probe leaves process identity unknown
            terminal = None
        process_state, proof, observed_at = terminal_observation(terminal)
        if staff["ended_at"] and process_state == "unknown":
            process_state, proof, observed_at = "ended", "staff_session_end", staff["ended_at"]
        run_id = None
    else:
        run_id = str(task["run_id"] or "") or None
        run = await app.db.fetchone("SELECT status,updated_at FROM runs WHERE id = ? AND session_id = ?", (run_id, session_id)) if run_id and session_id else None
        live = app.manager.live_state(session_id) if app.manager is not None and session_id else None
        process_state, proof, observed_at = native_observation(live, run, run_id)
    return {"task_id": task_id, "session_id": session_id or None, "staff_session_id": staff["id"] if staff else None,
            "terminal_id": terminal_id or None, "run_id": run_id, "attempt_id": attempt["id"] if attempt else None,
            "host_generation": int(attempt["host_generation"]) if attempt else None,
            "remote_host_id": remote_host_id,
            "attempt_state": attempt["state"] if attempt else None, "process_state": process_state,
            "observation": "current" if proof in ("live_run", "live_terminal", "terminal_run", "terminal_exit", "staff_session_end") else "unknown",
            "observed_at": observed_at, "proof": proof}


async def session_runtime_state(app: Application, session_id: str) -> dict[str, Any]:
    session = await app.db.fetchone("SELECT id FROM sessions WHERE id = ?", (session_id,))
    if session is None:
        raise KeyError(session_id)
    live = app.manager.live_state(session_id) if app.manager is not None else None
    run_id = str(live.run_id or "") if live is not None else ""
    if not run_id:
        latest = await app.db.fetchone("SELECT id FROM runs WHERE session_id = ? ORDER BY created_at DESC,id DESC LIMIT 1", (session_id,))
        run_id = str(latest["id"]) if latest is not None else ""
    run = await app.db.fetchone("SELECT status,updated_at FROM runs WHERE id = ? AND session_id = ?", (run_id, session_id)) if run_id else None
    process_state, proof, observed_at = native_observation(live, run, run_id)
    return {"session_id": session_id, "run_id": run_id or None, "process_state": process_state,
            "observation": "current" if proof in ("live_run", "terminal_run") else "unknown",
            "observed_at": observed_at, "proof": proof}
