"""The durable launch command owns admission, retries and the worker's exact attempt."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from daedalus.config import Settings
from daedalus.extensions.api_control import register
from daedalus.extensions.board import Board
from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.launch_controls import observe_bind, prepare_attempt
from daedalus.extensions.task_controls import TaskStopEffect, queue_stop
from daedalus.extensions.task_launch import TaskLaunchEffect, queue_launch
from daedalus.host.launch_queue import LaunchQueue
from daedalus.staff_runtime import BoardTask, Started
from daedalus.stores.control import ControlConflict, ControlDenied, Principal
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.staff import StaffStore
from tests.support.waiting import until_await
from tests.unit.test_launch_controls import OPERATOR, launch_fixture
from tests.unit.test_session_runner import ScriptedProvider, _manager
from tests.unit.test_staff_runtime import BRIEF, project_with, team_for


async def queued_fixture(db: Database):
    app, member, _, session = await launch_fixture(db)
    brief = {field: "An explicit task contract" for field in ("objective", "deliverable", "boundaries", "done_when")}
    await db.execute("UPDATE board_tasks SET status = 'todo',brief_json = ? WHERE id = 'task'", (json.dumps(brief),))
    dispatcher = EffectDispatcher(OutboxStore(db))
    dispatcher.register("task.launch", TaskLaunchEffect(app))
    app.extensions = {"effects": dispatcher}
    staff = StaffStore(db)
    starts = []

    async def task(task_id):
        row = await db.fetchone("SELECT * FROM board_tasks WHERE id = ?", (task_id,))
        return BoardTask(row["id"], row["title"], row["status"], project_id=row["project_id"],
                         assignee_staff_id=row["assignee_staff_id"], **json.loads(row["brief_json"]))

    async def project(project_id):
        return SimpleNamespace(id=project_id)

    async def launch(entry):
        await entry.check_authority()
        current = await task(entry.task_id)
        identity = await prepare_attempt(app, entry.principal, member, current, session, fence_token=secrets.token_urlsafe(32))
        await entry.check_authority()
        starts.append(identity)
        await db.execute("INSERT INTO sessions(id,tenant_id,project_id,title,created_at,last_message_at)"
                         " VALUES ('native-session','tenant','project','Work','2026-01-01','2026-01-01')")
        await staff.started(session.id, session_id="native-session")
        await observe_bind(app, identity, session, Started(None, None, None, "native-session"))

    async def concurrency(_):
        return 1

    async def active(_):
        return len(starts)

    async def ready(_):
        return None

    team = SimpleNamespace(member=staff.get, task=task, project=project,
                           folder_for=lambda *_: SimpleNamespace(id="folder", env="container"))
    team.queue = LaunchQueue(concurrency=concurrency, active=active, ready=ready, free=ready,
                             launch=launch, capacity=lambda: None, stagger=lambda: 0)
    app.extensions["staff"] = team
    revision = (await db.fetchone("SELECT entity_revision FROM board_tasks"))[0]
    return app, dispatcher, team, starts, revision


async def test_lost_response_replays_one_receipt_and_one_observed_attempt(db: Database) -> None:
    app, dispatcher, team, starts, revision = await queued_fixture(db)
    try:
        args = {"staff_id": "worker", "client_operation_id": "launch-once", "expected_entity_revision": revision}
        result = await queue_launch(app, "task", OPERATOR, **args)
        assert await dispatcher.step()
        assert len(starts) == 1
        assert (await dispatcher.store.view(result["effect_id"]))["state"] == "completed"
        assert await queue_launch(app, "task", OPERATOR, **args) == result
        assert not await dispatcher.step()
        assert len(starts) == 1
        assert starts[0].id == (await db.fetchone("SELECT current_attempt_id FROM board_tasks"))[0]
        assert (await db.fetchone("SELECT provider_session_ref FROM execution_attempts"))[0] == "session:native-session"
    finally:
        team.queue.close()
        app.executions.release()


async def test_busy_admission_leaves_only_the_durable_command(db: Database) -> None:
    app, dispatcher, team, starts, revision = await queued_fixture(db)

    async def occupied(_):
        return 1

    team.queue._active = occupied
    try:
        result = await queue_launch(app, "task", OPERATOR, staff_id="worker", client_operation_id="launch", expected_entity_revision=revision)
        assert await dispatcher.step()
        assert (await dispatcher.store.view(result["effect_id"]))["state"] == "pending"
        assert team.queue.entries("project") == []
        await team.queue.pump()
        assert starts == []
        assert not await dispatcher.step()
        current = (await db.fetchone("SELECT entity_revision FROM board_tasks"))[0]
        with pytest.raises(ControlConflict, match="queued or uncertain"):
            await queue_launch(app, "task", OPERATOR, staff_id="worker", client_operation_id="second", expected_entity_revision=current)
        assert (await db.fetchone("SELECT count(*) FROM effect_outbox"))[0] == 1
    finally:
        team.queue.close()
        app.executions.release()


async def test_changed_contract_cancels_before_the_worker_is_started(db: Database) -> None:
    app, dispatcher, team, starts, revision = await queued_fixture(db)
    try:
        result = await queue_launch(app, "task", OPERATOR, staff_id="worker", client_operation_id="launch", expected_entity_revision=revision)
        await db.execute("UPDATE board_tasks SET contract_revision = 2 WHERE id = 'task'")
        assert await dispatcher.step()
        assert (await dispatcher.store.view(result["effect_id"]))["state"] == "failed"
        assert starts == []
        assert (await db.fetchone("SELECT count(*) FROM actor_grants"))[0] == 0
    finally:
        team.queue.close()
        app.executions.release()


async def test_stopping_a_pending_launch_prevents_every_future_pump(db: Database) -> None:
    app, dispatcher, team, starts, revision = await queued_fixture(db)
    try:
        launch = await queue_launch(app, "task", OPERATOR, staff_id="worker", client_operation_id="launch", expected_entity_revision=revision)
        stop_args = {"client_operation_id": "stop", "expected_entity_revision": launch["entity_revision"]}
        stopped = await queue_stop(app, "task", OPERATOR, **stop_args)
        assert stopped["cancelled_launches"] == [launch["effect_id"]]
        assert stopped["state"] == "completed"
        assert await queue_stop(app, "task", OPERATOR, **stop_args) == stopped
        assert not await dispatcher.step()
        await team.queue.pump()
        assert starts == []
        assert (await dispatcher.store.view(launch["effect_id"]))["state"] == "cancelled"
    finally:
        team.queue.close()
        app.executions.release()


async def test_replacement_waits_for_physical_stop_proof(db: Database) -> None:
    app, dispatcher, team, starts, revision = await queued_fixture(db)
    stopped = []
    state = SimpleNamespace(run_id="run", running=True)

    class Manager:
        def live_state(self, session_id):
            return state if session_id == "native-session" else None

        async def stop_run(self, session_id, run_id):
            stopped.append((session_id, run_id))
            return True

    app.manager = Manager()
    dispatcher.register("task.stop", TaskStopEffect(app))
    try:
        await queue_launch(app, "task", OPERATOR, staff_id="worker", client_operation_id="launch", expected_entity_revision=revision)
        assert await dispatcher.step()
        await db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                         " VALUES ('run','tenant','native-session','running','2026-01-01','2026-01-01')")
        current = (await db.fetchone("SELECT entity_revision FROM board_tasks"))[0]
        stop = await queue_stop(app, "task", OPERATOR, client_operation_id="stop", expected_entity_revision=current)
        assert (await db.fetchone("SELECT state FROM execution_attempts"))[0] == "recovering"
        with pytest.raises(ControlConflict, match="reconcile"):
            await queue_launch(app, "task", OPERATOR, staff_id="worker", client_operation_id="replacement",
                               expected_entity_revision=stop["entity_revision"])
        assert await dispatcher.step()
        assert stopped == [("native-session", "run")]
        assert (await dispatcher.store.view(stop["effect_id"]))["state"] == "unknown"
        assert (await db.fetchone("SELECT state FROM execution_attempts"))[0] == "recovering"
        await db.execute("UPDATE runs SET status = 'cancelled' WHERE id = 'run'")
        assert await dispatcher.reconcile() == 1
        assert (await db.fetchone("SELECT state FROM execution_attempts"))[0] == "cancelled"
        assert (await db.fetchone("SELECT status FROM board_tasks"))[0] != "done"
        assert len(starts) == 1
    finally:
        team.queue.close()
        app.executions.release()


async def test_unattested_launcher_cannot_claim_operator_origin(db: Database) -> None:
    app, _, team, starts, revision = await queued_fixture(db)
    try:
        with pytest.raises(ControlDenied, match="host-issued"):
            await queue_launch(app, "task", Principal("agent:worker", "agent"), staff_id="worker",
                               client_operation_id="forged", expected_entity_revision=revision)
        assert starts == []
        assert (await db.fetchone("SELECT count(*) FROM operation_receipts"))[0] == 0
    finally:
        team.queue.close()
        app.executions.release()


async def test_http_launch_requires_cas_and_replays_the_authenticated_command(db: Database) -> None:
    app, _, team, starts, revision = await queued_fixture(db)

    def auth():
        return {"via": "cookie", "user_id": 1}

    api = FastAPI()
    register(api, app, auth)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            missing = await client.post("/api/board/task/launch", json={"staff_id": "worker"})
            assert missing.status_code == 422
            body = {"staff_id": "worker", "client_operation_id": "launch", "expected_entity_revision": revision}
            first = await client.post("/api/board/task/launch", json=body)
            assert first.status_code == 200, first.text
            second = await client.post("/api/board/task/launch", json=body)
            assert first.json() == second.json()
            forged = await client.post("/api/board/task/launch", json={**body, "origin": "operator"})
            assert forged.status_code == 422
            stale = await client.post("/api/board/task/launch", json={**body, "client_operation_id": "other"})
            assert stale.status_code == 409
            assert starts == []
    finally:
        team.queue.close()
        app.executions.release()


async def test_actual_native_worker_launch_and_report_preserve_the_full_original(settings: Settings, db: Database, tmp_path: Path) -> None:
    original = "A complete research report " + "x" * 70000
    provider = ScriptedProvider([{"tool": "Report", "args": {"kind": "done", "note": original}},
                                 {"text": "The report is handed in for review."}])
    manager = await _manager(settings, db, provider)
    executions = ExecutionStore(db)
    executions.acquire()
    team = await team_for(settings, manager)
    try:
        await executions.boot()
        team.app.executions = executions
        dispatcher = EffectDispatcher(OutboxStore(db))
        dispatcher.register("task.launch", TaskLaunchEffect(team.app))
        team.app.extensions["effects"] = dispatcher
        folder = tmp_path / "research"
        folder.mkdir()
        project = await project_with(manager, folder)
        member = await manager.staff.hire(project.id, name="Worker", role="Research", isolation="readonly")
        task = await Board(team.app).add(title="Research report", project_id=project.id, brief=BRIEF, operator=True)
        args = {"principal": OPERATOR, "client_operation_id": "native-launch", "expected_entity_revision": task["entity_revision"]}
        receipt = await team.assign(member, task, **args)
        assert receipt["state"] == "queued"
        assert await dispatcher.step()
        assert (await dispatcher.store.view(receipt["effect_id"]))["state"] == "completed"

        async def handed_in():
            return (await db.fetchone("SELECT count(*) FROM result_receipts WHERE task_id = ?", (task["id"],)))[0] == 1

        await until_await(handed_in, "the actual native worker handed in its immutable report")
        from daedalus.extensions.orchestrator_domain import OrchestratorDomain

        result = await db.fetchone("SELECT id FROM result_receipts WHERE task_id = ?", (task["id"],))
        assert await OrchestratorDomain(db).original(task["id"], result["id"]) == original.encode()
        assert (await db.fetchone("SELECT state FROM execution_attempts WHERE task_id = ?", (task["id"],)))[0] == "completed"
        assert (await db.fetchone("SELECT status FROM board_tasks WHERE id = ?", (task["id"],)))[0] == "review"
        assert await team.assign(member, task, **args) == receipt
        assert (await db.fetchone("SELECT count(*) FROM staff_sessions WHERE task_id = ?", (task["id"],)))[0] == 1
    finally:
        team.queue.close()
        await manager.close()
        executions.release()


async def test_lost_provider_start_response_keeps_the_worker_owned(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    executions = ExecutionStore(db)
    executions.acquire()
    team = await team_for(settings, manager)
    physical_starts = []

    async def start(request):
        physical_starts.append(request.staff_session_id)
        raise ConnectionError("the provider started work but its response was lost")

    try:
        await executions.boot()
        team.app.executions = executions
        team.runtimes["daedalus"] = SimpleNamespace(start=start)
        dispatcher = EffectDispatcher(OutboxStore(db))
        dispatcher.register("task.launch", TaskLaunchEffect(team.app))
        team.app.extensions["effects"] = dispatcher
        folder = tmp_path / "research"
        folder.mkdir()
        project = await project_with(manager, folder)
        member = await manager.staff.hire(project.id, name="Worker", role="Research", isolation="readonly")
        task = await Board(team.app).add(title="Research report", project_id=project.id, brief=BRIEF, operator=True)
        args = {"principal": OPERATOR, "client_operation_id": "uncertain-launch", "expected_entity_revision": task["entity_revision"]}
        receipt = await team.assign(member, task, **args)
        assert await dispatcher.step()
        assert (await dispatcher.store.view(receipt["effect_id"]))["state"] == "unknown"
        assert len(physical_starts) == 1
        session = await manager.staff.session(physical_starts[0])
        assert session is not None and session.ended_at is None and session.status == "no_signal"
        current = await db.fetchone("SELECT status,assignee_staff_id,entity_revision FROM board_tasks WHERE id = ?", (task["id"],))
        assert (current["status"], current["assignee_staff_id"]) == ("doing", member.id)
        assert (await db.fetchone("SELECT state FROM execution_attempts WHERE task_id = ?", (task["id"],)))[0] == "recovering"
        assert await team.assign(member, task, **args) == receipt
        assert await dispatcher.reconcile() == 0
        assert not await dispatcher.step()
        with pytest.raises(ControlConflict):
            await team.assign(member, task, principal=OPERATOR, client_operation_id="another-launch",
                              expected_entity_revision=current["entity_revision"])
        assert len(physical_starts) == 1
    finally:
        team.queue.close()
        await manager.close()
        executions.release()
