"""An alternate worker inherits evidence only after the source runtime is released."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from daedalus.extensions.api_runtime_handoff import register
from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.launch_controls import prepare_attempt
from daedalus.extensions.runtime_handoff import packet_for_launch, preview, snapshot_in
from daedalus.extensions.runtime_observations import observe_no_entry
from daedalus.extensions.task_launch import TaskLaunchEffect, queue_launch
from daedalus.host.launch_queue import LaunchQueue
from daedalus.staff_runtime import BoardTask
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.resource_profiles import set_profile_in
from daedalus.stores.staff import StaffStore
from tests.unit.test_execution_ownership import owner

OPERATOR = Principal.operator({"via": "cookie", "user_id": 1})


async def handoff_fixture(db: Database):
    store, identity, _ = await owner(db)
    await db.execute("INSERT INTO project_folders(id,project_id,path,env,position,created_at)"
                     " VALUES ('folder','project','/tmp/fixture-worktree','container',0,'2026-01-01')")
    await db.execute("UPDATE board_tasks SET folder_id = 'folder' WHERE id = 'task'")
    await db.execute("UPDATE board_tasks SET brief_json = ? WHERE id = 'task'",
                     (json.dumps({field: "Complete the reviewed task" for field in
                                  ("objective", "deliverable", "boundaries", "done_when")}),))
    await db.execute("UPDATE staff_sessions SET folder_id = 'folder' WHERE id = 'staff-session'")
    await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                     " VALUES ('alternate','project','Alternate','claude','operator','2026-01-01')")

    async def harnesses(env):
        assert env == "container"
        return [{"harness": "claude", "installed": True, "adapter": True, "supported": True,
                 "version_guard": "verified", "installed_version": "1", "logged_in": "yes"}]

    app = SimpleNamespace(db=db, executions=store,
                          extensions={"harness": SimpleNamespace(harnesses=harnesses),
                                      "effects": SimpleNamespace(notify=lambda: None)},
                          manager=SimpleNamespace(projects=SimpleNamespace(local_env="container")))
    staff = StaffStore(db)

    async def task(task_id):
        return await db.fetchone("SELECT * FROM board_tasks WHERE id = ?", (task_id,))

    async def project(project_id):
        return SimpleNamespace(id=project_id)

    app.extensions["staff"] = SimpleNamespace(member=staff.get, task=task, project=project,
        folder_for=lambda *_: SimpleNamespace(id="folder", path=Path("/tmp/fixture-worktree"), env="container"),
        worktrees=SimpleNamespace(check=AsyncMock(return_value="a" * 40)))
    return app, identity


async def test_unobserved_source_refuses_handoff_without_assignment_or_receipt(db: Database) -> None:
    app, identity = await handoff_fixture(db)
    try:
        view = await preview(app, "task", identity.id, "alternate")
        assert not view["ready"]
        assert view["blockers"] == ["source_runtime_not_released"]
        with pytest.raises(ControlConflict, match="source is not released"):
            await queue_launch(app, "task", OPERATOR, staff_id="alternate",
                               client_operation_id="continue", expected_entity_revision=view["entity_revision"],
                               fallback_from_attempt_id=identity.id,
                               fallback_preview_digest=view["preview_digest"])
        assert (await db.fetchone("SELECT assignee_staff_id FROM board_tasks WHERE id = 'task'"))[0] is None
        assert (await db.fetchone("SELECT count(*) FROM operation_receipts"))[0] == 0
    finally:
        app.executions.release()


async def test_released_source_queues_one_immutable_cross_runtime_handoff(db: Database) -> None:
    app, identity = await handoff_fixture(db)
    try:
        assert await observe_no_entry(app, identity, staff_session_id="staff-session", reason="host refused entry")
        view = await preview(app, "task", identity.id, "alternate")
        assert view["ready"] and view["packet"]["history_portability"] == "none"
        assert view["packet"]["workspace_transfer"] == "none"
        args = {"staff_id": "alternate", "client_operation_id": "continue",
                "expected_entity_revision": view["entity_revision"],
                "fallback_from_attempt_id": identity.id,
                "fallback_preview_digest": view["preview_digest"]}
        response = await queue_launch(app, "task", OPERATOR, **args)
        assert response["state"] == "queued"
        assert await queue_launch(app, "task", OPERATOR, **args) == response
        assert (await db.fetchone("SELECT count(*) FROM runtime_handoffs"))[0] == 1
        assert (await db.fetchone("SELECT count(*) FROM effect_outbox"))[0] == 1
        row = await db.fetchone("SELECT source_attempt_id,target_attempt_id,packet_digest,operation_receipt_id"
                                " FROM runtime_handoffs")
        assert row["source_attempt_id"] == identity.id
        assert row["target_attempt_id"] == response["new_attempt_id"]
        assert row["packet_digest"] == view["packet_digest"]
        assert row["operation_receipt_id"] == response["receipt_id"]
        assert (await db.fetchone("SELECT status,assignee_staff_id FROM board_tasks WHERE id = 'task'"))["assignee_staff_id"] == "alternate"
        assert (await packet_for_launch(db, response["handoff_id"], task_id="task",
                                        target_staff_id="alternate"))["source_attempt_id"] == identity.id
    finally:
        app.executions.release()


async def test_changed_worker_or_contract_invalidates_reviewed_handoff(db: Database) -> None:
    app, identity = await handoff_fixture(db)
    try:
        assert await observe_no_entry(app, identity, staff_session_id="staff-session", reason="host refused entry")
        view = await preview(app, "task", identity.id, "alternate")
        await db.execute("UPDATE staff SET permission_mode = 'dangerous' WHERE id = 'alternate'")
        with pytest.raises(ControlConflict, match="stale"):
            await queue_launch(app, "task", OPERATOR, staff_id="alternate",
                               client_operation_id="stale", expected_entity_revision=view["entity_revision"],
                               fallback_from_attempt_id=identity.id,
                               fallback_preview_digest=view["preview_digest"])
        assert (await db.fetchone("SELECT count(*) FROM runtime_handoffs"))[0] == 0
        await db.execute("UPDATE board_tasks SET contract_revision = 2 WHERE id = 'task'")
        async with db.transaction() as conn:
            with pytest.raises(ControlConflict, match="same current task contract"):
                await snapshot_in(conn, task_id="task", source_attempt_id=identity.id,
                                  target_staff_id="alternate", context_hash="context")
    finally:
        app.executions.release()


async def test_cli_alternate_enters_with_a_new_attempt_and_no_provider_resume(db: Database) -> None:
    app, source = await handoff_fixture(db)
    queue = None
    try:
        assert await observe_no_entry(app, source, staff_session_id="staff-session", reason="host refused entry")
        view = await preview(app, "task", source.id, "alternate")
        staff = StaffStore(db)
        started = []

        async def task(task_id):
            row = await db.fetchone("SELECT * FROM board_tasks WHERE id = ?", (task_id,))
            return BoardTask(row["id"], row["title"], row["status"], project_id=row["project_id"],
                             assignee_staff_id=row["assignee_staff_id"], **json.loads(row["brief_json"]))

        async def launch(entry):
            assert entry.fallback_decision_id and entry.resume_from is None
            await entry.check_authority()
            packet = await packet_for_launch(db, entry.fallback_decision_id,
                                             task_id=entry.task_id, target_staff_id=entry.staff_id)
            assert packet["source_attempt_id"] == source.id
            await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,folder_id,status_at,started_at)"
                             " VALUES ('alternate-session','alternate','cli','task','folder','2026-01-01','2026-01-01')")
            member = await staff.get("alternate")
            session = await staff.session("alternate-session")
            identity = await prepare_attempt(app, entry.principal, member, await task(entry.task_id), session,
                                             fence_token=secrets.token_urlsafe(32))
            started.append(identity)

        async def one(_):
            return 1

        async def zero(_):
            return 0

        async def no_terminals():
            return 0

        async def ready(_):
            return None

        team = SimpleNamespace(member=staff.get, task=task,
                               folder_for=lambda *_: SimpleNamespace(id="folder", path=Path("/tmp/fixture-worktree"), env="container"),
                               worktrees=SimpleNamespace(check=AsyncMock(return_value="a" * 40)))

        async def project(_):
            return SimpleNamespace(id="project")

        team.project = project
        queue = LaunchQueue(concurrency=one, active=zero, ready=ready, free=ready,
                            launch=launch, capacity=lambda: SimpleNamespace(running=no_terminals, cap=lambda: 2,
                                waiting=lambda: 0, unavailable=lambda _: None), stagger=lambda: 0)
        team.queue = queue
        app.extensions["staff"] = team
        dispatcher = EffectDispatcher(OutboxStore(db))
        dispatcher.register("task.launch", TaskLaunchEffect(app))
        app.extensions["effects"] = dispatcher
        response = await queue_launch(app, "task", OPERATOR, staff_id="alternate",
                                      client_operation_id="continue", expected_entity_revision=view["entity_revision"],
                                      fallback_from_attempt_id=source.id,
                                      fallback_preview_digest=view["preview_digest"])
        assert await dispatcher.step()
        assert (await dispatcher.store.view(response["effect_id"]))["state"] == "completed"
        assert len(started) == 1 and started[0].id == response["new_attempt_id"]
        assert started[0].id != source.id
        assert (await db.fetchone("SELECT current_attempt_id FROM board_tasks WHERE id = 'task'"))[0] == started[0].id
    finally:
        if queue is not None:
            queue.close()
        app.executions.release()


async def test_original_report_route_returns_complete_bytes_only_to_bound_worker(db: Database) -> None:
    app, source = await handoff_fixture(db)
    try:
        report = "Original result: cafés, edge cases and unresolved work.\n"
        encoded = report.encode("utf-8")
        original_digest = hashlib.sha256(encoded).hexdigest()
        await db.execute("INSERT INTO result_receipts(id,task_id,contract_revision,attempt_id,outcome,"
                         "original_text,original_digest,original_size_bytes,actor_id,created_at)"
                         " VALUES ('result','task',1,?,'partial',?,?,?, 'staff:worker','2026-01-01')",
                         (source.id, report, original_digest, len(encoded)))
        assert await observe_no_entry(app, source, staff_session_id="staff-session", reason="host refused entry")
        view = await preview(app, "task", source.id, "alternate")
        response = await queue_launch(app, "task", OPERATOR, staff_id="alternate",
                                      client_operation_id="continue", expected_entity_revision=view["entity_revision"],
                                      fallback_from_attempt_id=source.id,
                                      fallback_preview_digest=view["preview_digest"])
        await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,folder_id,status_at,started_at)"
                         " VALUES ('alternate-session','alternate','cli','task','folder','2026-01-01','2026-01-01')")
        await db.execute("INSERT INTO runtime_handoff_sessions(handoff_id,staff_session_id,created_at)"
                         " VALUES (?, 'alternate-session','2026-01-01')", (response["handoff_id"],))

        async def authenticate(session_id, token):
            if session_id != "alternate-session" or token != "exact-token":
                raise PermissionError("invalid team token")
            return SimpleNamespace(session=SimpleNamespace(task_id="task"), staff=SimpleNamespace(id="alternate"))

        app.extensions["staff"] = SimpleNamespace(authenticate=authenticate)
        api = FastAPI()

        async def operator():
            return {"via": "cookie", "user_id": 1}

        register(api, app, operator)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            denied = await client.get("/api/team/alternate-session/handoff-original")
            assert denied.status_code == 401
            valid = await client.get("/api/team/alternate-session/handoff-original",
                                     headers={"x-daedalus-team-token": "exact-token"})
            assert valid.status_code == 200 and valid.content == encoded
            assert valid.headers["cache-control"] == "no-store"
            packet = await client.get("/api/team/alternate-session/handoff-packet",
                                      headers={"x-daedalus-team-token": "exact-token"})
            assert packet.status_code == 200
            assert packet.json()["source_result"]["original_digest"] == original_digest
            assert packet.headers["x-packet-digest"] == view["packet_digest"]
            await db.execute("UPDATE result_receipts SET original_text = 'changed' WHERE id = 'result'")
            changed = await client.get("/api/team/alternate-session/handoff-original",
                                       headers={"x-daedalus-team-token": "exact-token"})
            assert changed.status_code == 409
    finally:
        app.executions.release()


async def test_cli_source_can_be_reviewed_for_native_continuation_with_model_preflight(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    await db.execute("INSERT INTO project_folders(id,project_id,path,env,position,created_at)"
                     " VALUES ('folder','project','/tmp/fixture-worktree','container',0,'2026-01-01')")
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,folder_id,created_at,updated_at)"
                     " VALUES ('task','Work','doing',3,'project','folder','2026-01-01','2026-01-01')")
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,snapshot_json,created_at)"
                     " VALUES ('task',1,'operator','','{}','2026-01-01')")
    await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                     " VALUES ('source','project','Source','claude','operator','2026-01-01')")
    await db.execute("INSERT INTO staff(id,project_id,name,harness,isolation,created_by,created_at)"
                     " VALUES ('native','project','Native','daedalus','shared','operator','2026-01-01')")
    await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,folder_id,status_at,started_at)"
                     " VALUES ('source-session','source','cli','task','folder','2026-01-01','2026-01-01')")
    subject = Principal("staff:source", "agent")
    grant = await ControlStore(db).issue_grant(OPERATOR, subject, Scope("project", "project"),
                                               operations=["result.submit"], effects=[], task_id="task",
                                               expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    worker = Principal(subject.actor_id, "agent", grant["grant_id"], grant["generation"])
    store = ExecutionStore(db)
    store.acquire()
    try:
        await store.boot()
        async with db.transaction() as conn:
            source = await store.create(conn, attempt_id="source-attempt", task_id="task", contract_revision=1,
                                        launcher=OPERATOR, worker=worker, staff_session_id="source-session",
                                        runtime_kind="cli", fence_token=secrets.token_urlsafe(32))
        staff = StaffStore(db)

        async def member(member_id):
            return await staff.get(member_id)

        app = SimpleNamespace(db=db, executions=store,
                              manager=SimpleNamespace(projects=SimpleNamespace(local_env="container"),
                                                      config=SimpleNamespace(has_model=False, presets={})),
                              extensions={"staff": SimpleNamespace(member=member),
                                          "effects": SimpleNamespace(notify=lambda: None)})
        assert await observe_no_entry(app, source, staff_session_id="source-session", reason="CLI refused entry")
        blocked = await preview(app, "task", source.id, "native")
        assert not blocked["ready"] and blocked["blockers"] == ["target_runtime_unavailable"]
        app.manager.config.has_model = True
        ready = await preview(app, "task", source.id, "native")
        assert ready["ready"] and ready["packet"]["source_harness"] == "claude"
        assert ready["packet"]["target_harness"] == "daedalus"
        receipt = await queue_launch(app, "task", OPERATOR, staff_id="native",
                                     client_operation_id="native-continuation",
                                     expected_entity_revision=ready["entity_revision"],
                                     fallback_from_attempt_id=source.id,
                                     fallback_preview_digest=ready["preview_digest"])
        assert receipt["state"] == "queued" and receipt["new_attempt_id"] != source.id
    finally:
        store.release()


async def test_http_preview_and_command_bind_operator_and_replay_before_cas(db: Database) -> None:
    app, source = await handoff_fixture(db)
    try:
        assert await observe_no_entry(app, source, staff_session_id="staff-session", reason="host refused entry")
        api = FastAPI()

        async def operator():
            return {"via": "cookie", "user_id": 1}

        register(api, app, operator)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            options = await client.get("/api/board/task/handoff-options")
            assert options.status_code == 200
            assert options.json()["source_attempt_id"] == source.id
            assert options.json()["source_released"]
            view = await client.get("/api/board/task/handoff-preview",
                                    params={"source_attempt_id": source.id, "target_staff_id": "alternate"})
            assert view.status_code == 200 and view.json()["ready"]
            body = {"source_attempt_id": source.id, "target_staff_id": "alternate",
                    "preview_digest": view.json()["preview_digest"],
                    "expected_entity_revision": view.json()["entity_revision"],
                    "client_operation_id": "http-continuation"}
            assert (await client.post("/api/board/task/continue-elsewhere", json={**body,
                    "actor_id": "operator:other"})).status_code == 422
            first = await client.post("/api/board/task/continue-elsewhere", json=body)
            assert first.status_code == 200 and first.json()["state"] == "queued"
            replay = await client.post("/api/board/task/continue-elsewhere", json=body)
            assert replay.status_code == 200 and replay.json() == first.json()
            stale = await client.post("/api/board/task/continue-elsewhere", json={**body,
                                      "client_operation_id": "new-command"})
            assert stale.status_code == 409
    finally:
        app.executions.release()


async def test_alternate_worker_cannot_silently_end_another_live_session(db: Database) -> None:
    app, source = await handoff_fixture(db)
    try:
        assert await observe_no_entry(app, source, staff_session_id="staff-session", reason="host refused entry")
        await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at)"
                         " VALUES ('other-live','alternate','cli',NULL,'2026-01-01','2026-01-01')")
        with pytest.raises(ControlConflict, match="release the alternate worker"):
            await preview(app, "task", source.id, "alternate")
        assert (await db.fetchone("SELECT ended_at FROM staff_sessions WHERE id = 'other-live'"))[0] is None
        assert (await db.fetchone("SELECT count(*) FROM effect_outbox"))[0] == 0
    finally:
        app.executions.release()


async def test_preview_exposes_resource_boundary_and_binds_its_observation(db: Database) -> None:
    app, source = await handoff_fixture(db)
    try:
        assert await observe_no_entry(app, source, staff_session_id="staff-session", reason="host refused entry")

        async def set_profile(conn, mutation):
            return await set_profile_in(conn, project_id="project", state="enabled",
                                        memory_bytes=64 << 20, cpu_millis=500,
                                        process_count=8, disk_bytes=0, min_free_disk_bytes=0,
                                        expected_profile_revision=None, receipt_id=mutation.receipt_id)

        await ControlStore(db).mutate(OPERATOR, Scope("project", "project"), "resource.profile.set",
                                      "resource-boundary", 1, Entity("project", "project"),
                                      {"enabled": True}, set_profile)
        observed = {"available": False, "kind": "cgroup_v2", "sandbox": "ok",
                    "daemon_instance": "instance"}

        async def capability(_):
            return observed

        app.extensions["terminals"] = SimpleNamespace(containment_capability=capability,
                                                      preflight_attempt_resources=AsyncMock(return_value=None))
        blocked = await preview(app, "task", source.id, "alternate")
        assert not blocked["ready"] and blocked["resource"]["state"] == "blocked"
        assert "resource_boundary_unavailable" in blocked["blockers"]
        observed = {**observed, "available": True}
        ready = await preview(app, "task", source.id, "alternate")
        assert ready["ready"] and ready["resource"]["state"] == "ready"
        assert ready["preview_digest"] != blocked["preview_digest"]
        app.extensions["terminals"].preflight_attempt_resources.assert_awaited_once()
    finally:
        app.executions.release()
