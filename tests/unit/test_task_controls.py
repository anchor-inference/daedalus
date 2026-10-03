"""A task stop never means a later run or another task's process was stopped."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.task_controls import TaskStopEffect, queue_stop
from daedalus.host.session_runner import SessionManager
from daedalus.stores.control import ControlConflict, Principal
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore

OPERATOR = Principal.operator({"via": "cookie", "user_id": 1})


class Engine:
    def __init__(self) -> None:
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


class NativeManager:
    stop_run = SessionManager.stop_run

    def __init__(self, db: Database) -> None:
        self.db = db
        self._states = {"session": SimpleNamespace(run_id="run", running=True, engine=Engine())}

    def live_state(self, session_id: str) -> Any:
        return self._states.get(session_id)


async def native_app(db: Database, settings: Settings) -> Any:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,created_at,last_message_at) VALUES ('session','daedalus','project','2026-01-01','2026-01-01')")
    await db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at) VALUES ('run','daedalus','session','running','2026-01-01','2026-01-01')")
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,session_id,run_id,created_at,updated_at) VALUES ('task','Work','doing',3,'project','session','run','2026-01-01','2026-01-01')")
    app = SimpleNamespace(db=db, settings=settings, config=RuntimeConfig(), front=None, manager=NativeManager(db), extensions={})
    dispatcher = EffectDispatcher(OutboxStore(db))
    dispatcher.register("task.stop", TaskStopEffect(app))
    app.extensions["effects"] = dispatcher
    return app


async def test_stop_receipt_replays_without_stopping_an_unobserved_execution_twice(db: Database, settings: Settings) -> None:
    app = await native_app(db, settings)
    args = {"client_operation_id": "stop-once", "expected_entity_revision": 1}
    receipt = await queue_stop(app, "task", OPERATOR, **args)
    assert receipt["state"] == "queued"
    dispatcher = app.extensions["effects"]
    assert await dispatcher.step()
    assert app.manager.live_state("session").engine.stops == 1
    assert (await dispatcher.store.view(receipt["effect_id"]))["state"] == "unknown"
    assert await queue_stop(app, "task", OPERATOR, **args) == receipt
    assert not await dispatcher.step()
    assert await dispatcher.reconcile() == 0
    await db.execute("UPDATE runs SET status = 'cancelled' WHERE id = 'run'")
    assert await dispatcher.reconcile() == 1
    assert (await dispatcher.store.view(receipt["effect_id"]))["state"] == "completed"
    assert app.manager.live_state("session").engine.stops == 1


async def test_replacement_between_authorization_and_stop_is_never_stopped(db: Database, settings: Settings) -> None:
    app = await native_app(db, settings)
    receipt = await queue_stop(app, "task", OPERATOR, client_operation_id="bound", expected_entity_revision=1)
    claim = await app.extensions["effects"].store.claim(("task.stop",))
    assert claim is not None
    original = app.manager.live_state("session").engine
    replacement = Engine()

    async def check_then_replace(claim: Any) -> None:
        await app.extensions["effects"].store.check(claim)
        app.manager._states["session"] = SimpleNamespace(run_id="later", running=True, engine=replacement)

    outcome = await TaskStopEffect(app).run(claim, check_then_replace)
    assert outcome.state == "unknown"
    assert original.stops == replacement.stops == 0
    assert (await app.extensions["effects"].store.view(receipt["effect_id"]))["state"] == "claimed"


async def test_session_running_unrelated_work_is_refused_before_receipt(db: Database, settings: Settings) -> None:
    app = await native_app(db, settings)
    app.manager.live_state("session").run_id = "different-task"
    with pytest.raises(ControlConflict, match="other work"):
        await queue_stop(app, "task", OPERATOR, client_operation_id="wrong", expected_entity_revision=1)
    assert (await db.fetchone("SELECT count(*) FROM effect_outbox"))[0] == 0
    assert (await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id='task'"))[0] == 1


async def test_http_stop_requires_auth_revision_and_operation_identity(db: Database, settings: Settings) -> None:
    app = await native_app(db, settings)
    api = build_app(app, "test-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        url = "/api/board/task/stop"
        body = {"client_operation_id": "http-stop", "expected_entity_revision": 1}
        assert (await client.post(url, json=body)).status_code == 401
        client.headers["x-daedalus-token"] = "test-token"
        assert (await client.post(url, json={})).status_code == 422
        first = await client.post(url, json=body)
        assert first.status_code == 200
        assert (await client.post(url, json=body)).json() == first.json()
        assert (await client.post(url, json={**body, "client_operation_id": "stale"})).status_code == 409
        assert (await client.post(url, json={**body, "actor_id": "operator:1"})).status_code == 422


async def test_offline_cli_is_unknown_until_exit_is_observed(db: Database, settings: Settings) -> None:
    app = await native_app(db, settings)
    terminal = {"status": "lost"}

    class Terminals:
        async def get(self, terminal_id: str) -> dict[str, str]:
            return terminal

    app.extensions["terminals"] = Terminals()
    await queue_stop(app, "task", OPERATOR, client_operation_id="cli-proof", expected_entity_revision=1)
    claim = await app.extensions["effects"].store.claim(("task.stop",))
    assert claim is not None
    cli = replace(claim, payload={"kind": "cli", "terminal_id": "terminal"})
    handler = TaskStopEffect(app)
    assert await handler.observe(cli) is None
    terminal["status"] = "running"
    assert await handler.observe(cli) is None
    terminal["status"] = "exited"
    resolution = await handler.observe(cli)
    assert resolution is not None and resolution.evidence["terminal_id"] == "terminal"


async def test_cli_retasking_and_stop_use_the_same_execution_lock(db: Database, settings: Settings) -> None:
    app = await native_app(db, settings)
    await queue_stop(app, "task", OPERATOR, client_operation_id="cli-lock", expected_entity_revision=1)
    claim = await app.extensions["effects"].store.claim(("task.stop",))
    assert claim is not None
    cli = replace(claim, payload={"kind": "cli", "task_id": "task", "attempt_id": None, "terminal_id": "terminal", "staff_session_id": "staff-session", "staff_id": "member"})
    lock = asyncio.Lock()
    live = SimpleNamespace(session=SimpleNamespace(task_id="task"), terminal_id="terminal", staff=SimpleNamespace(id="member"))
    calls = []

    class Runtime:
        async def stop(self, live: Any) -> None:
            assert lock.locked()
            calls.append(live)

    class Team:
        def execution_lock(self, staff_id: str) -> asyncio.Lock:
            return lock

        async def live(self, session_id: str) -> Any:
            return live

        def runtime(self, staff: Any) -> Runtime:
            return Runtime()

    app.extensions["staff"] = Team()

    async def check(claim: Any) -> None:
        assert lock.locked()

    result = await TaskStopEffect(app).run(cli, check)
    assert result.state == "unknown"
    assert len(calls) == 1
