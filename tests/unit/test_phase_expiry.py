"""Deadline cleanup reserves one stop and never treats a timeout as physical exit."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Header

from daedalus.extensions.api_lifecycle import register as register_lifecycle_api
from daedalus.extensions.launch_controls import prepare_attempt
from daedalus.extensions.lifecycle import Lifecycle
from daedalus.extensions.phase_expiry import BATCH_SIZE, PhaseExpiry
from daedalus.stores.control import ControlDenied
from daedalus.stores.database import Database
from daedalus.stores.phase_clocks import PHASES
from daedalus.stores.runtime_release import attempt_released_in
from tests.unit.test_launch_controls import OPERATOR, launch_fixture


@pytest.mark.parametrize("phase", PHASES)
async def test_expiry_fences_every_wait_once_even_without_worker_authority(db: Database, phase: str) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        await prepare_attempt(app, OPERATOR, member, task, session, fence_token=secrets.token_urlsafe(32))
        due = datetime.now(UTC)
        await db.execute("UPDATE attempt_phase_clocks SET phase = ?,deadline_at = ?", (phase, due.isoformat()))
        await db.execute("UPDATE execution_attempts SET runtime_entered_at = ?", (due.isoformat(),))
        await db.execute("UPDATE actor_grants SET revoked_at = ?", (due.isoformat(),))
        app.extensions = {}
        service = PhaseExpiry(app)
        assert await service.step(at=due - timedelta(seconds=1)) == 0
        assert await service.step(at=due) == 1
        assert await service.step(at=due + timedelta(days=1)) == 0
        assert (await db.fetchone("SELECT outcome FROM attempt_phase_clocks"))[0] == "timed_out"
        assert (await db.fetchone("SELECT state FROM execution_attempts"))[0] == "recovering"
        assert (await db.fetchone("SELECT cancel_state FROM lifecycle_owners"))[0] == "unknown"
        assert (await db.fetchone("SELECT pause_requested FROM staff_sessions"))[0] == 1
        assert (await db.fetchone("SELECT count(*) FROM attempt_faults"))[0] == 1
        async with db.transaction() as conn:
            with pytest.raises(ControlDenied):
                await app.executions.check_staff(conn, session.id)
    finally:
        app.executions.release()


@pytest.mark.parametrize("previous_host,interrupted", [(False, False), (True, False), (False, True)])
async def test_exact_runtime_gets_one_stop_and_retains_unknown_exit(
    db: Database, previous_host: bool, interrupted: bool,
) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        await prepare_attempt(app, OPERATOR, member, task, session, fence_token=secrets.token_urlsafe(32))
        due = datetime.now(UTC)
        await db.execute("UPDATE attempt_phase_clocks SET deadline_at = ?", (due.isoformat(),))
        await db.execute("INSERT INTO sessions(id,tenant_id,project_id,title,created_at,last_message_at)"
                         " VALUES ('native-session','tenant','project','Work','2026-01-01','2026-01-01')")
        await db.execute("UPDATE staff_sessions SET session_id = 'native-session'")
        await db.execute("UPDATE execution_attempts SET state = 'running',native_run_id = 'run',"
                         "provider_session_ref = 'session:native-session'")

        @asynccontextmanager
        async def execution_lock(staff_id):
            assert staff_id == member.id
            yield

        live = SimpleNamespace(id=session.id, staff=member, session_id="native-session")
        team = SimpleNamespace(execution_lock=execution_lock, live=AsyncMock(return_value=live))
        app.manager = SimpleNamespace(stop_run=AsyncMock(return_value=True))
        if interrupted:
            app.manager.stop_run.side_effect = asyncio.CancelledError
        app.extensions = {"staff": team, "lifecycle": Lifecycle(app)}
        if previous_host:
            app.executions.release()
            app.executions.acquire()
            await app.executions.boot()
        service = PhaseExpiry(app)
        if interrupted:
            with pytest.raises(asyncio.CancelledError):
                await service.step(at=due)
        else:
            assert await service.step(at=due) == 1
        assert await service.step(at=due) == 0
        assert app.manager.stop_run.await_count == (0 if previous_host else 1)
        if not previous_host:
            app.manager.stop_run.assert_awaited_once_with("native-session", "run")
        assert (await db.fetchone("SELECT state FROM execution_attempts"))[0] == "recovering"
        assert (await db.fetchone("SELECT cancel_state FROM lifecycle_owners"))[0] == "unknown"
        assert (await db.fetchone("SELECT count(*) FROM runtime_exit_observations"))[0] == 0
        assert (await db.fetchone("SELECT count(*) FROM effect_outbox"))[0] == 0
        # A lost stop response and a terminal SQL projection do not prove physical exit.
        await db.execute("UPDATE execution_attempts SET state = 'failed'")
        await db.execute("UPDATE staff_sessions SET ended_at = ?", (due.isoformat(),))
        assert (await db.fetchone("SELECT cancel_state FROM lifecycle_owners"))[0] == "unknown"
    finally:
        app.executions.release()


async def test_terminal_attempt_is_not_expired(db: Database) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        identity = await prepare_attempt(app, OPERATOR, member, task, session, fence_token=secrets.token_urlsafe(32))
        async with db.transaction() as conn:
            await app.executions.complete(conn, identity, outcome="complete")
        app.extensions = {}
        assert await PhaseExpiry(app).step(at=datetime.now(UTC) + timedelta(days=1)) == 0
        assert (await db.fetchone("SELECT state FROM execution_attempts"))[0] == "completed"
    finally:
        app.executions.release()


async def test_deadline_batch_makes_progress_without_resending_interrupted_stop(db: Database) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        identity = await prepare_attempt(app, OPERATOR, member, task, session, fence_token=secrets.token_urlsafe(32))
        due = datetime.now(UTC)
        async with db.transaction() as conn:
            await conn.execute("UPDATE attempt_phase_clocks SET deadline_at = ?", (due.isoformat(),))
            for index in range(BATCH_SIZE):
                clone = f"other-{index}"
                await conn.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                                   "fence_token_hash,state,created_at,updated_at)"
                                   " SELECT ?,task_id,contract_revision,host_generation,fence_token_hash,state,"
                                   "created_at,updated_at FROM execution_attempts WHERE id = ?", (clone, identity.id))
                await conn.execute("INSERT INTO attempt_phase_clocks(attempt_id,phase,started_at,deadline_at,outcome)"
                                   " VALUES (?,'prepare',?,?,'active')", (clone, due.isoformat(), due.isoformat()))
        app.extensions = {}
        assert await PhaseExpiry(app).step(at=due) == BATCH_SIZE
        assert (await db.fetchone("SELECT count(*) FROM attempt_phase_clocks WHERE outcome = 'active'"))[0] == 1
        # Recreating the service models recovery after a crash. Previously reserved rows stay
        # unknown rather than re-entering stop delivery; the next overdue row cannot starve.
        assert await PhaseExpiry(app).step(at=due) == 1
        assert await PhaseExpiry(app).step(at=due) == 0
        assert (await db.fetchone("SELECT count(*) FROM attempt_faults"))[0] == BATCH_SIZE + 1
    finally:
        app.executions.release()


@pytest.mark.parametrize("state,entered", [("queued", False), ("queued", True), ("starting", False)])
async def test_only_attested_queued_no_entry_can_drain(db: Database, state: str, entered: bool) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        identity = await prepare_attempt(app, OPERATOR, member, task, session, fence_token=secrets.token_urlsafe(32))
        due = datetime.now(UTC)
        await db.execute("UPDATE attempt_phase_clocks SET deadline_at = ?", (due.isoformat(),))
        await db.execute("UPDATE execution_attempts SET state = ?,runtime_entered_at = ?",
                         (state, due.isoformat() if entered else None))
        app.extensions = {}
        assert await PhaseExpiry(app).step(at=due) == 1
        proven = state == "queued" and not entered
        assert (await db.fetchone("SELECT state FROM execution_attempts"))[0] == ("failed" if proven else "recovering")
        assert (await db.fetchone("SELECT cancel_state FROM lifecycle_owners"))[0] == ("drained" if proven else "unknown")
        assert (await db.fetchone("SELECT count(*) FROM runtime_no_entry_observations"))[0] == int(proven)
        async with db.transaction() as conn:
            assert await attempt_released_in(conn, identity.id) == proven
        assert await PhaseExpiry(app).step(at=due) == 0
    finally:
        app.executions.release()


async def test_terminal_exit_without_containment_keeps_stop_unknown(db: Database) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        identity = await prepare_attempt(app, OPERATOR, member, task, session, fence_token=secrets.token_urlsafe(32))
        due = datetime.now(UTC)
        await db.execute("UPDATE attempt_phase_clocks SET deadline_at = ?", (due.isoformat(),))
        await db.execute("INSERT INTO terminals(id,env,owner_kind,cwd,status,created_at,ptyd_instance)"
                         " VALUES ('terminal','container','staff','/tmp','exited',?,'daemon')", (due.isoformat(),))
        await db.execute("UPDATE staff_sessions SET kind = 'cli',terminal_id = 'terminal',ended_at = ?",
                         (due.isoformat(),))
        await db.execute("UPDATE execution_attempts SET state = 'running',runtime_kind = 'cli',"
                         "runtime_entered_at = ?,provider_session_ref = 'terminal:terminal',runtime_instance = 'daemon'",
                         (due.isoformat(),))
        app.extensions = {"lifecycle": Lifecycle(app)}
        assert await PhaseExpiry(app).step(at=due) == 1
        await db.execute("INSERT INTO runtime_exit_observations(attempt_id,runtime_ref,provider_session_ref,"
                         "staff_session_id,contract_revision,host_generation,runtime_kind,runtime_instance,"
                         "observed_status,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (identity.id, "terminal", "terminal:terminal", session.id,
                          identity.contract_revision, identity.host_generation, "cli", "daemon",
                          "exited", due.isoformat()))
        assert await app.extensions["lifecycle"].reconcile_expired(identity.id, "project") == "unknown"
        assert (await db.fetchone("SELECT cancel_state FROM lifecycle_owners"))[0] == "unknown"

        api = FastAPI()

        async def authenticated() -> dict[str, int | str]:
            return {"via": "token", "user_id": 1}

        register_lifecycle_api(api, app, authenticated)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            item = (await client.get("/api/projects/project/unknown-stops")).json()["items"][0]
            assert item["exit_observed"] is True
            assert item["recovery_blocker"] == "containment_unavailable"
            assert item["exit_evidence"] == {"runtime_ref": "terminal", "host_generation": identity.host_generation,
                                             "contract_revision": identity.contract_revision,
                                             "observed_status": "exited", "observed_at": due.isoformat()}
            assert item["containment_evidence"] is None
            await db.execute("INSERT INTO project_folders(id,project_id,path,env,position,created_at)"
                             " VALUES ('folder','project','/tmp/fixture-worktree','container',0,'2026-01-01')")
            await db.execute("INSERT INTO writer_attempt_bindings(attempt_id,project_id,host_generation,env,"
                             "daemon_instance,folder_id,folder_path_digest,launch_workspace_digest,launch_id,"
                             "state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (identity.id, "project", str(identity.host_generation), "container", "daemon", "folder",
                              "folder-digest", "workspace-digest", "launch", "enforced", due.isoformat(), due.isoformat()))
            item = (await client.get("/api/projects/project/unknown-stops")).json()["items"][0]
            assert item["recovery_blocker"] == "container_not_empty"
            assert item["containment_evidence"] == {"source": "writer", "state": "enforced",
                "host_generation": str(identity.host_generation), "latest_observation": None,
                "release_observation": None, "stale_observation": None}
            assert await app.extensions["lifecycle"].reconcile_expired(identity.id, "project") == "unknown"
            await db.execute("INSERT INTO writer_attempt_observations(id,attempt_id,host_generation,daemon_instance,"
                             "launch_id,observation_kind,enforced,populated,observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                             ("busy", identity.id, str(identity.host_generation), "daemon", "launch",
                              "sample", 1, 1, due.isoformat()))
            item = (await client.get("/api/projects/project/unknown-stops")).json()["items"][0]
            assert item["recovery_blocker"] == "container_not_empty"
            assert item["containment_evidence"]["latest_observation"]["id"] == "busy"
            assert item["containment_evidence"]["release_observation"] is None
            await db.execute("INSERT INTO writer_attempt_observations(id,attempt_id,host_generation,daemon_instance,"
                             "launch_id,observation_kind,enforced,populated,observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                             ("stale", identity.id, str(identity.host_generation + 1), "earlier-daemon", "launch",
                              "exit", 1, 0, due.isoformat()))
            await db.execute("UPDATE writer_attempt_bindings SET state = 'released' WHERE attempt_id = ?",
                             (identity.id,))
            item = (await client.get("/api/projects/project/unknown-stops")).json()["items"][0]
            assert item["recovery_blocker"] == "container_not_empty"
            assert item["containment_evidence"]["release_observation"] is None
            assert item["containment_evidence"]["stale_observation"]["id"] == "stale"
            await db.execute("INSERT INTO writer_attempt_observations(id,attempt_id,host_generation,daemon_instance,"
                             "launch_id,observation_kind,enforced,populated,observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                             ("empty", identity.id, str(identity.host_generation), "daemon", "launch",
                              "exit", 1, 0, due.isoformat()))
            item = (await client.get("/api/projects/project/unknown-stops")).json()["items"][0]
            assert item["recovery_blocker"] == "ready"
            assert item["containment_evidence"]["release_observation"] == {
                "id": "empty", "host_generation": str(identity.host_generation), "observation_kind": "exit",
                "enforced": 1, "populated": 0, "observed_at": due.isoformat()}
            assert "daemon_instance" not in str(item) and "launch_id" not in str(item)
            assert await app.extensions["lifecycle"].reconcile_expired(identity.id, "project") == "drained"
    finally:
        app.executions.release()


@pytest.mark.parametrize("generation_delta", [0, 1])
@pytest.mark.parametrize("owner_revision_delta", [0, 1])
async def test_unknown_stop_no_entry_projection_requires_matching_generation(
    db: Database, generation_delta: int, owner_revision_delta: int,
) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        identity = await prepare_attempt(app, OPERATOR, member, task, session, fence_token=secrets.token_urlsafe(32))
        due = datetime.now(UTC)
        await db.execute("UPDATE attempt_phase_clocks SET deadline_at = ?", (due.isoformat(),))
        await db.execute("UPDATE execution_attempts SET state = 'starting'")
        app.extensions = {}
        assert await PhaseExpiry(app).step(at=due) == 1
        await db.execute("UPDATE execution_attempts SET state = 'failed'")
        await db.execute("UPDATE staff_sessions SET ended_at = ?", (due.isoformat(),))
        if owner_revision_delta:
            await db.execute("UPDATE lifecycle_owners SET source_revision = source_revision + 1")
        await db.execute("INSERT INTO runtime_no_entry_observations(attempt_id,staff_session_id,"
                         "contract_revision,host_generation,runtime_kind,reason,observed_at)"
                         " VALUES (?,?,?,?,?,?,?)",
                         (identity.id, session.id, identity.contract_revision,
                          identity.host_generation + generation_delta, "daedalus", "host refused", due.isoformat()))
        api = FastAPI()

        async def authenticated() -> dict[str, int | str]:
            return {"via": "token", "user_id": 1}

        app.extensions = {"lifecycle": Lifecycle(app)}
        register_lifecycle_api(api, app, authenticated)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            item = (await client.get("/api/projects/project/unknown-stops")).json()["items"][0]
            result = await client.post(f"/api/projects/project/unknown-stops/{identity.id}/reconcile")
        assert item["no_entry_evidence"] == ({"host_generation": identity.host_generation,
            "contract_revision": identity.contract_revision, "observed_at": due.isoformat()}
            if generation_delta == 0 else None)
        assert item["recovery_blocker"] == ("source_revision_changed" if owner_revision_delta else
            "ready" if generation_delta == 0 else "runtime_identity_missing")
        if owner_revision_delta:
            assert result.status_code == 404
        else:
            assert result.status_code == 200
            assert result.json()["cancel_state"] == ("drained" if generation_delta == 0 else "unknown")
    finally:
        app.executions.release()


@pytest.mark.parametrize("previous_host", [False, True])
async def test_unknown_stop_inspection_and_exact_observation_recovery(db: Database, previous_host: bool) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        identity = await prepare_attempt(app, OPERATOR, member, task, session, fence_token=secrets.token_urlsafe(32))
        due = datetime.now(UTC)
        await db.execute("UPDATE attempt_phase_clocks SET deadline_at = ?", (due.isoformat(),))
        await db.execute("UPDATE execution_attempts SET state = 'running',runtime_entered_at = ?,"
                         "native_run_id = 'run',provider_session_ref = 'session:native-session'", (due.isoformat(),))
        await db.execute("INSERT INTO sessions(id,tenant_id,project_id,title,created_at,last_message_at)"
                         " VALUES ('native-session','tenant','project','Work','2026-01-01','2026-01-01')")
        await db.execute("UPDATE staff_sessions SET session_id = 'native-session'")
        app.extensions = {"lifecycle": Lifecycle(app)}
        assert await PhaseExpiry(app).step(at=due) == 1
        if previous_host:
            app.executions.release()
            app.executions.acquire()
            await app.executions.boot()

        api = FastAPI()

        async def authenticated(x_user: int = Header(1)) -> dict[str, int | str]:
            return {"via": "token" if x_user > 0 else "staff", "user_id": x_user}

        register_lifecycle_api(api, app, authenticated)
        path = "/api/projects/project/unknown-stops"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            assert (await client.get(path, headers={"x-user": "-1"})).status_code == 403
            assert (await client.get("/api/projects/missing/unknown-stops")).status_code == 404
            assert (await client.get(path, params={"limit": 101})).status_code == 422
            response = await client.get(path)
            assert response.status_code == 200, response.text
            item = response.json()["items"][0]
            assert item["id"] == identity.id
            assert item["phase"] == "prepare" and item["cancel_state"] == "unknown"
            assert item["generation_matches_host_record"] is (not previous_host)
            assert item["exit_observed"] is False
            assert item["recovery_blocker"] == ("previous_host" if previous_host else "exit_unobserved")
            endpoint = f"{path}/{identity.id}/reconcile"
            assert (await client.post(endpoint, headers={"x-user": "-1"})).status_code == 403
            assert (await client.post(endpoint)).json() == {"cancel_state": "unknown"}
            assert (await client.post(f"{path}/missing/reconcile")).status_code == 404

            await db.execute("INSERT INTO runtime_exit_observations(attempt_id,runtime_ref,provider_session_ref,"
                             "staff_session_id,contract_revision,host_generation,runtime_kind,runtime_instance,"
                             "observed_status,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (identity.id, "other-run", "session:native-session", session.id,
                              identity.contract_revision, identity.host_generation, "daedalus", None,
                              "cancelled", due.isoformat()))
            assert (await client.post(endpoint)).json() == {"cancel_state": "unknown"}
            await db.execute("INSERT INTO runtime_exit_observations(attempt_id,runtime_ref,provider_session_ref,"
                             "staff_session_id,contract_revision,host_generation,runtime_kind,runtime_instance,"
                             "observed_status,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (identity.id, "run", "session:native-session", session.id,
                              identity.contract_revision, identity.host_generation, "daedalus", None,
                              "cancelled", due.isoformat()))
            assert (await client.get(path)).json()["items"][0]["exit_observed"] is True
            if not previous_host:
                await db.execute("INSERT INTO project_folders(id,project_id,path,env,position,created_at)"
                                 " VALUES ('folder','project','/tmp/fixture-worktree','container',0,'2026-01-01')")
                await db.execute("INSERT INTO writer_attempt_bindings(attempt_id,project_id,host_generation,env,"
                                 "daemon_instance,folder_id,folder_path_digest,launch_workspace_digest,launch_id,"
                                 "state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (identity.id, "project", str(identity.host_generation), "container", "daemon", "folder",
                                  "folder-digest", "workspace-digest", "launch", "enforced", due.isoformat(), due.isoformat()))
                assert (await client.get(path)).json()["items"][0]["recovery_blocker"] == "container_not_empty"
                assert (await client.post(endpoint)).json() == {"cancel_state": "unknown"}
                await db.execute("INSERT INTO writer_attempt_observations(id,attempt_id,host_generation,daemon_instance,"
                                 "launch_id,observation_kind,enforced,populated,observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                 ("empty", identity.id, str(identity.host_generation), "daemon", "launch",
                                  "exit", 1, 0, due.isoformat()))
                await db.execute("UPDATE writer_attempt_bindings SET state = 'released' WHERE attempt_id = ?",
                                 (identity.id,))
            assert (await client.get(path)).json()["items"][0]["recovery_blocker"] == (
                "previous_host" if previous_host else "ready")
            assert (await client.post(endpoint)).json() == {"cancel_state": "unknown" if previous_host else "drained"}
            if previous_host:
                assert (await db.fetchone("SELECT cancel_state FROM lifecycle_owners"))[0] == "unknown"
            else:
                assert (await client.get(path)).json()["items"] == []
                assert (await db.fetchone("SELECT state FROM execution_attempts"))[0] == "cancelled"
    finally:
        app.executions.release()


async def test_uncertain_launch_inspection_is_project_scoped_and_does_not_change_claims(db: Database) -> None:
    app, _, _, _ = await launch_fixture(db)
    try:
        await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('other','Other','2026-01-01','{}')")
        await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,created_at,updated_at)"
                         " VALUES ('other-task','Other','todo',3,'other','2026-01-01','2026-01-01')")
        for id_, project, task, state in (("first", "project", "task", "unknown"),
                                           ("second", "project", "task", "claimed"),
                                           ("third", "other", "other-task", "unknown")):
            await db.execute("INSERT INTO operation_receipts(id,scope_kind,scope_id,project_id,actor_id,"
                             "operation_kind,client_operation_id,payload_hash,entity_revision,state,response_json,created_at)"
                             " VALUES (?,'project',?,?,'operator:1','task.launch',?,'hash',1,'committed','{}','2026-01-01')",
                             (id_, project, project, id_))
            await db.execute("INSERT INTO effect_outbox(id,receipt_id,kind,payload_json,state,claim_generation,"
                             "claimed_at,created_at) VALUES (?,?,'task.launch',json_object('control',"
                             "json_object('task_id',?),'data',json_object('attempt_id',?)),?,1,'2026-01-02','2026-01-01')",
                             (id_, id_, task, f"attempt-{id_}", state))

        api = FastAPI()

        async def authenticated(x_user: int = Header(1)) -> dict[str, int | str]:
            return {"via": "token" if x_user > 0 else "staff", "user_id": x_user}

        register_lifecycle_api(api, app, authenticated)
        path = "/api/projects/project/uncertain-launches"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            assert (await client.get(path, headers={"x-user": "-1"})).status_code == 403
            assert (await client.get("/api/projects/missing/uncertain-launches")).status_code == 404
            first = (await client.get(path, params={"limit": 1})).json()
            assert [row["id"] for row in first["items"]] == ["first"]
            assert first["next_after"] == "first"
            assert first["items"][0]["attempt_id"] == "attempt-first"
            assert first["items"][0]["provider_session_recorded"] is False
            assert "payload_json" not in first["items"][0]
            second = (await client.get(path, params={"after": first["next_after"]})).json()
            assert [row["id"] for row in second["items"]] == ["second"]
            assert second["next_after"] is None
            assert (await client.get("/api/projects/other/uncertain-launches")).json()["items"][0]["id"] == "third"
            assert (await client.get(path, params={"limit": 101})).status_code == 422
        assert [row["state"] for row in await db.fetchall("SELECT state FROM effect_outbox ORDER BY id")] == ["unknown", "claimed", "unknown"]
    finally:
        app.executions.release()
