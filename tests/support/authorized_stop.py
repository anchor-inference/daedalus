"""Prove an exact worker exit before a fixture changes its task's execution scope."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.extensions.runtime_observations import admit_native_run, observe_exit
from daedalus.extensions.task_controls import TaskStopEffect, queue_stop
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from tests.support.waiting import until, until_await


async def bind_native_run(team: Any, live: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[str, list[tuple[str, str]]]:
    """Bind one fixture run to its real attempt before a stop command can target it."""
    app = team.app
    dispatcher = app.extensions["effects"]
    if "task.stop" not in dispatcher.handlers:
        dispatcher.register("task.stop", TaskStopEffect(app))
    source = await app.db.fetchone("SELECT tenant_id FROM sessions WHERE id = ?", (live.session.session_id,))
    assert source is not None
    run_id = uuid.uuid4().hex
    at = datetime.now(UTC).isoformat()
    await app.db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                         " VALUES (?,?,?,'running',?,?)",
                         (run_id, source["tenant_id"], live.session.session_id, at, at))
    await admit_native_run(app, live.id, live.session.session_id, run_id)
    state = SimpleNamespace(run_id=run_id, running=True)
    monkeypatch.setattr(app.manager, "live_state", lambda session_id: state if session_id == live.session.session_id else None)
    stops: list[tuple[str, str]] = []

    async def stop_run(session_id: str, exact_run_id: str) -> bool:
        stops.append((session_id, exact_run_id))
        return True

    monkeypatch.setattr(app.manager, "stop_run", stop_run)
    return run_id, stops


async def finish_native_stop(team: Any, live: Any, run_id: str,
                             effect_id: str, stops: list[tuple[str, str]]) -> None:
    """A stop request remains uncertain until its exact runtime exit is observed."""
    app = team.app
    dispatcher = app.extensions["effects"]
    await dispatcher.step()
    await until(lambda: bool(stops), "the exact run received its stop", timeout=10)
    assert stops == [(live.session.session_id, run_id)]

    async def unknown() -> bool:
        effect = await app.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (effect_id,))
        return effect is not None and effect["state"] == "unknown"

    await until_await(unknown, "the uncertain stop stayed durable", timeout=10)
    await app.db.execute("UPDATE runs SET status = 'cancelled' WHERE id = ?", (run_id,))
    assert await observe_exit(app, staff_session_id=live.id, runtime_ref=run_id,
                              observed_status="cancelled")
    await dispatcher.reconcile()
    effect = await app.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (effect_id,))
    assert effect is not None and effect["state"] == "completed"


async def stop_native_task(team: Any, task_id: str, live: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stop through the real receipt, then record the matching host run exit and reconcile it."""
    app = team.app
    with monkeypatch.context() as patch:
        run_id, stops = await bind_native_run(team, live, patch)
        task = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
        assert task is not None and task["project_id"]
        revision = await ControlStore(app.db).revision(Scope("project", task["project_id"]), Entity("task", task_id))
        receipt = await queue_stop(app, task_id, Principal.operator({"via": "token", "user_id": 1}),
                                   client_operation_id=f"fixture-stop:{uuid.uuid4().hex}",
                                   expected_entity_revision=revision, reason="the task changes after its worker exits")
        assert receipt["state"] == "queued"
        await finish_native_stop(team, live, run_id, receipt["effect_id"], stops)
        return receipt
