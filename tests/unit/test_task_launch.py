"""The durable launch command owns admission, retries and the worker's exact attempt."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Header

from daedalus.config import Settings
from daedalus.extensions.api_control import register
from daedalus.extensions.api_lifecycle import register as register_lifecycle_api
from daedalus.extensions.board import Board
from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.launch_controls import launch_attempt, observe_bind, prepare_attempt
from daedalus.extensions.lifecycle import Lifecycle
from daedalus.extensions.runtime_observations import observe_no_entry
from daedalus.extensions.staff import claim_host_slot, finish_host_slot
from daedalus.extensions.task_controls import TaskStopEffect, queue_stop
from daedalus.extensions.task_launch import TaskLaunchEffect, queue_launch
from daedalus.host.launch_queue import LaunchQueue
from daedalus.staff_runtime import BoardTask, Started
from daedalus.stores.control import ControlConflict, ControlDenied, Principal
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.result_anchors import result_turn_refs
from daedalus.stores.staff import StaffStore
from tests.support.waiting import until_await
from tests.unit.test_launch_controls import OPERATOR, launch_fixture
from tests.unit.test_session_runner import ScriptedProvider, _manager
from tests.unit.test_staff_runtime import BRIEF, close_team, project_with, team_for


async def queued_fixture(db: Database):
    app, member, _, session = await launch_fixture(db)
    app.manager = SimpleNamespace(projects=SimpleNamespace(local_env="container"))
    await db.execute("INSERT INTO project_folders(id,project_id,path,env,position,created_at)"
                     " VALUES ('folder','project','/tmp/fixture-worktree','container',0,'2026-01-01')")
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
                           folder_for=lambda *_: SimpleNamespace(id="folder", path=Path("/tmp/fixture-worktree"), env="container"),
                           worktrees=SimpleNamespace(check=AsyncMock(return_value="a" * 40)))
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


@pytest.mark.parametrize("observed", ["none", "entered", "refused"])
async def test_unknown_launch_operator_check_requires_saved_runtime_proof(
    db: Database, observed: str,
) -> None:
    app, dispatcher, team, starts, revision = await queued_fixture(db)
    receipt = await queue_launch(app, "task", OPERATOR, staff_id="worker",
                                 client_operation_id="interrupted", expected_entity_revision=revision)
    store = dispatcher.store
    claim = await store.claim(("task.launch",))
    assert claim is not None
    if observed == "entered":
        assert (await TaskLaunchEffect(app).run(claim, store.check)).state == "completed"
        assert len(starts) == 1
    elif observed == "refused":
        member = await team.member("worker")
        task = await team.task("task")
        session = await StaffStore(db).session("staff-session")
        bound = launch_attempt.set(claim.payload["attempt_id"])
        try:
            identity = await prepare_attempt(app, OPERATOR, member, task, session,
                                             fence_token=secrets.token_urlsafe(32))
        finally:
            launch_attempt.reset(bound)
        assert await observe_no_entry(app, identity, staff_session_id=session.id,
                                      reason="host refused before runtime entry")
    try:
        await db.close()
        await db.open()
        assert await store.recover() == 1
        api = FastAPI()

        async def authenticated(x_user: int = Header(1)):
            return {"via": "token" if x_user > 0 else "staff", "user_id": x_user}

        register_lifecycle_api(api, app, authenticated)
        path = f"/api/projects/project/uncertain-launches/{receipt['effect_id']}/reconcile"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            first = await client.get("/api/projects/project/uncertain-launches")
            assert first.status_code == 200
            item = first.json()["items"][0]
            assert item["state"] == "unknown"
            assert item["no_entry_observed"] is (observed == "refused")
            assert item["provider_session_recorded"] is (observed == "entered")
            assert (await client.post(path.replace("/project/", "/missing/"))).status_code == 404
            assert (await client.post(path, headers={"x-user": "-1"})).status_code == 403
            assert (await db.fetchone("SELECT state FROM effect_outbox WHERE id = ?",
                                      (receipt["effect_id"],)))[0] == "unknown"
            checked = await client.post(path)
            assert checked.status_code == 200
            result = checked.json()
            if observed == "none":
                assert result["state"] == "unknown" and not result["reconciled"]
                assert (await db.fetchone("SELECT state FROM effect_outbox WHERE id = ?",
                                          (receipt["effect_id"],)))[0] == "unknown"
                assert starts == []
            else:
                assert result["state"] == ("completed" if observed == "entered" else "failed")
                assert result["reconciled"]
                assert (await client.post(path)).json()["reconciled"] is False
                assert (await db.fetchone("SELECT count(*) FROM effect_outbox"))[0] == 1
    finally:
        team.queue.close()
        app.executions.release()


async def test_writable_cli_is_contained_where_possible_and_launches_plainly_elsewhere(db: Database) -> None:
    app, _, team, _, revision = await queued_fixture(db)
    await db.execute("UPDATE staff SET harness = 'claude',isolation = 'shared' WHERE id = 'worker'")
    capability = {"available": True, "kind": "cgroup_v2", "sandbox": "ok", "daemon_instance": "daemon-one"}
    terminals = SimpleNamespace(containment_capability=AsyncMock(return_value=capability),
                                preflight_attempt_resources=AsyncMock(return_value={}))
    app.extensions["terminals"] = terminals
    try:
        # A machine that cannot contain a writer still runs it, as an ordinary launch.
        terminals.containment_capability.return_value = {**capability, "available": False}
        plain = await queue_launch(app, "task", OPERATOR, staff_id="worker",
                                   client_operation_id="uncontained", expected_entity_revision=revision)
        effect = await db.fetchone("SELECT payload_json FROM effect_outbox WHERE id = ?", (plain["effect_id"],))
        assert json.loads(effect["payload_json"])["data"].get("resources") is None
        await db.execute("DELETE FROM effect_outbox")
        await db.execute("UPDATE board_tasks SET entity_revision = entity_revision WHERE id = 'task'")
        revision = (await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'task'"))[0]
        terminals.containment_capability.return_value = capability
        result = await queue_launch(app, "task", OPERATOR, staff_id="worker",
                                    client_operation_id="contained", expected_entity_revision=revision)
        effect = await db.fetchone("SELECT payload_json FROM effect_outbox WHERE id = ?", (result["effect_id"],))
        resources = json.loads(effect["payload_json"])["data"]["resources"]
        assert resources["mode"] == "writer"
        assert resources["limits"] == {"memory_bytes": 0, "cpu_millis": 0,
                                       "process_count": 0, "disk_bytes": 0}
        assert resources["daemon_instance"] == "daemon-one"
    finally:
        team.queue.close()
        app.executions.release()


async def test_worktree_cli_keeps_its_existing_launch_path(db: Database) -> None:
    app, _, team, _, revision = await queued_fixture(db)
    await db.execute("UPDATE staff SET harness = 'claude',isolation = 'worktree' WHERE id = 'worker'")
    try:
        result = await queue_launch(app, "task", OPERATOR, staff_id="worker",
                                    client_operation_id="worktree", expected_entity_revision=revision)
        effect = await db.fetchone("SELECT payload_json FROM effect_outbox WHERE id = ?", (result["effect_id"],))
        assert json.loads(effect["payload_json"])["data"]["resources"] is None
    finally:
        team.queue.close()
        app.executions.release()


async def test_queued_worktree_launch_refuses_changed_source_after_command(db: Database) -> None:
    app, dispatcher, team, starts, revision = await queued_fixture(db)
    try:
        result = await queue_launch(app, "task", OPERATOR, staff_id="worker",
                                    client_operation_id="source-pin", expected_entity_revision=revision)
        team.worktrees.check.return_value = "b" * 40
        assert await dispatcher.step()
        assert starts == []
        effect = await dispatcher.store.view(result["effect_id"])
        assert effect["state"] == "failed"
        assert "moved since launch approval" in effect["error"]
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
        projects = SimpleNamespace(local_env="container")

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
        await db.execute("UPDATE execution_attempts SET native_run_id = 'run'")
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
        assert await dispatcher.reconcile() == 0
        from daedalus.extensions.runtime_observations import observe_exit

        session = await db.fetchone("SELECT staff_session_id FROM execution_attempts")
        assert await observe_exit(app, staff_session_id=session["staff_session_id"], runtime_ref="run",
                                  observed_status="cancelled")
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
    team = await team_for(settings, manager)
    dispatcher = team.app.extensions["effects"]
    dispatcher.delivery_ready.clear()
    try:
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
        # The result names the worker's chat turn it was handed in from, so its source is one click away.
        chat = (await db.fetchone("SELECT session_id FROM staff_sessions WHERE task_id = ?", (task["id"],)))[0]
        refs = await result_turn_refs(db, task["id"], result["id"])
        assert [(ref["session_id"], ref["source_current"]) for ref in refs] == [(chat, True)]
        assert (await db.fetchone("SELECT state FROM execution_attempts WHERE task_id = ?", (task["id"],)))[0] == "completed"
        assert (await db.fetchone("SELECT status FROM board_tasks WHERE id = ?", (task["id"],)))[0] == "review"

        async def physically_finished():
            return await db.fetchone("SELECT o.runtime_ref FROM runtime_exit_observations o"
                                     " JOIN execution_attempts a ON a.id = o.attempt_id"
                                     " WHERE a.task_id = ? AND o.runtime_ref = a.native_run_id"
                                     " AND o.staff_session_id = a.staff_session_id"
                                     " AND o.host_generation = a.host_generation", (task["id"],))

        await until_await(physically_finished, "the host observed the exact native run finish")
        assert await team.assign(member, task, **args) == receipt
        assert (await db.fetchone("SELECT count(*) FROM staff_sessions WHERE task_id = ?", (task["id"],)))[0] == 1
    finally:
        await close_team(manager)
        await manager.close()


async def test_lost_provider_start_response_keeps_the_worker_owned(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    team = await team_for(settings, manager)
    dispatcher = team.app.extensions["effects"]
    dispatcher.delivery_ready.clear()
    physical_starts = []

    async def start(request):
        physical_starts.append(request.staff_session_id)
        raise ConnectionError("the provider started work but its response was lost")

    try:
        team.runtimes["daedalus"] = SimpleNamespace(start=start)
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
        await close_team(manager)
        await manager.close()


async def test_lifecycle_cancellation_removes_a_pending_launch_before_admission(db: Database) -> None:
    app, dispatcher, team, starts, revision = await queued_fixture(db)
    service = Lifecycle(app)
    app.extensions["lifecycle"] = service
    dispatcher.register("lifecycle.stop", service)
    try:
        receipt = await queue_launch(app, "task", OPERATOR, staff_id="worker", client_operation_id="pending",
                                     expected_entity_revision=revision)
        stopped = await service.cancel_command(OPERATOR, "task", "task", "stop the planned work",
                                               expected_entity_revision=receipt["entity_revision"],
                                               expected_source_revision=1, client_operation_id="cancel-parent",
                                               preview_fingerprint=(await service.preview("task", "task"))["preview_fingerprint"])
        assert stopped["cancel_state"] == "requested"
        assert (await dispatcher.store.view(receipt["effect_id"]))["state"] == "cancelled"
        assert await dispatcher.step()
        assert starts == []
        assert not await dispatcher.step()
        current = (await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'task'"))[0]
        with pytest.raises(ControlConflict, match="parent was cancelled"):
            await queue_launch(app, "task", OPERATOR, staff_id="worker", client_operation_id="new-launch",
                               expected_entity_revision=current)
    finally:
        team.queue.close()
        app.executions.release()


async def test_parent_cancellation_between_attempt_claim_and_provider_call_fences_launch(db: Database) -> None:
    app, dispatcher, team, starts, revision = await queued_fixture(db)
    service = Lifecycle(app)
    app.extensions["lifecycle"] = service
    dispatcher.register("lifecycle.stop", service)
    member = await team.member("worker")
    session = await StaffStore(db).session("staff-session")

    async def raced(entry):
        await entry.check_authority()
        task = await team.task("task")
        identity = await prepare_attempt(app, entry.principal, member, task, session, fence_token=secrets.token_urlsafe(32))
        current = (await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'task'"))[0]
        await service.cancel_command(OPERATOR, "task", "task", "cancel before provider call",
                                     expected_entity_revision=current, expected_source_revision=1,
                                     client_operation_id="cancel-during-launch",
                                     preview_fingerprint=(await service.preview("task", "task"))["preview_fingerprint"])
        await entry.check_authority()
        starts.append(identity)

    team.queue._launch = raced
    try:
        receipt = await queue_launch(app, "task", OPERATOR, staff_id="worker", client_operation_id="raced",
                                     expected_entity_revision=revision)
        assert await dispatcher.step()
        assert starts == []
        assert (await dispatcher.store.view(receipt["effect_id"]))["state"] == "unknown"
        assert await dispatcher.step()
        attempt = await db.fetchone("SELECT state FROM execution_attempts WHERE task_id = 'task'")
        assert attempt["state"] == "recovering"
        assert await dispatcher.reconcile() == 0
        assert not await dispatcher.step()
        owner = await db.fetchone("SELECT cancel_state FROM lifecycle_owners WHERE child_kind = 'execution_attempt'")
        assert owner["cancel_state"] == "unknown"
    finally:
        team.queue.close()
        app.executions.release()


async def test_a_launch_takes_and_keeps_one_machine_wide_place(db: Database) -> None:
    app, dispatcher, team, starts, revision = await queued_fixture(db)
    app.manager.config = SimpleNamespace(terminals=SimpleNamespace(running_cap=3))
    team.queue._claim_host = lambda entry: claim_host_slot(app, entry)
    team.queue._finish_host = lambda entry, started: finish_host_slot(app, entry, started)
    # The fixture's unbound staff session and an operator's shell take two of the three places;
    # the last one is kept for coordination or review, so the worker waits in the durable order.
    await db.execute("INSERT INTO terminals(id,env,owner_kind,cwd,created_at)"
                     " VALUES ('shell','host','session','/tmp','now')")
    try:
        result = await queue_launch(app, "task", OPERATOR, staff_id="worker", client_operation_id="launch",
                                    expected_entity_revision=revision)
        assert await dispatcher.step()
        view = await dispatcher.store.view(result["effect_id"])
        assert view["state"] == "pending" and starts == []
        waiting = await db.fetchone("SELECT state,reason FROM scheduler_claims")
        assert waiting["state"] == "waiting" and "reserved for coordination" in waiting["reason"]
        await db.execute("UPDATE terminals SET status = 'exited' WHERE id = 'shell'")
        dispatcher.wake.set()
        assert await dispatcher.step()
        assert len(starts) == 1
        reservation = await db.fetchone("SELECT attempt_id,state,role_class FROM capacity_reservations")
        assert (reservation["attempt_id"], reservation["state"], reservation["role_class"]) == (starts[0].id, "active", "worker")
        assert (await db.fetchone("SELECT state FROM scheduler_claims"))[0] == "admitted"
    finally:
        team.queue.close()
        app.executions.release()
