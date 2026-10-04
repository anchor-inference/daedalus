"""Strict ceilings bind one exact attempt and never turn an unknown stop into release."""

from __future__ import annotations

import hashlib
import json
import secrets
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from daedalus.extensions.api_resource_profiles import register
from daedalus.extensions.launch_controls import launch_resources, prepare_attempt
from daedalus.extensions.task_launch import queue_launch
from daedalus.staff_runtime import BoardTask
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Scope
from daedalus.stores.database import Database
from daedalus.stores.resource_profiles import (
    bind_launch_in,
    disk_snapshot,
    latest_in,
    observe_in,
    record_disk_entry_in,
    released_in,
    set_profile_in,
    strict_target,
    writer_target,
)
from daedalus.stores.staff import StaffStore
from tests.unit.test_launch_controls import OPERATOR, launch_fixture
from tests.unit.test_task_launch import queued_fixture


async def configured(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings)"
                     " VALUES ('project','Work','2026-01-01','{}')")

    async def effect(conn, mutation):
        return await set_profile_in(conn, project_id="project", state="enabled",
                                    memory_bytes=64 << 20, cpu_millis=500,
                                    process_count=8, disk_bytes=0, min_free_disk_bytes=0,
                                    expected_profile_revision=None,
                                    receipt_id=mutation.receipt_id)

    await ControlStore(db).mutate(OPERATOR, Scope("project", "project"), "resource.profile.set",
                                  "profile-1", 1, Entity("project", "project"), {"enabled": True}, effect)


async def test_resource_profile_is_immutable_and_replays_the_exact_revision(db: Database) -> None:
    await configured(db)
    row = await db.fetchone("SELECT receipt_id,revision FROM resource_profile_versions")
    assert row["revision"] == 1
    receipt = await db.fetchone("SELECT operation_kind FROM operation_receipts WHERE id = ?",
                                (row["receipt_id"],))
    assert receipt["operation_kind"] == "resource.profile.set"
    with pytest.raises(Exception, match="immutable"):
        await db.execute("UPDATE resource_profile_versions SET memory_bytes = 1 WHERE project_id = 'project'")
    profile = await db.fetchone("SELECT * FROM resource_profile_versions WHERE project_id = 'project'")
    assert profile["memory_bytes"] == 64 << 20


async def test_profile_http_receipt_replay_and_stale_project_revision(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings)"
                     " VALUES ('project','Work','2026-01-01','{}')")
    api = FastAPI()
    register(api, SimpleNamespace(db=db, extensions={}),
             lambda: {"via": "cookie", "user_id": 1})
    body = {"state": "enabled", "memory_bytes": 64 << 20, "cpu_millis": 500,
            "process_count": 8, "disk_bytes": 0, "min_free_disk_bytes": 0,
            "expected_profile_revision": None,
            "expected_entity_revision": 1,
            "client_operation_id": "resource-profile-1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        first = await client.put("/api/projects/project/resource-profile", json=body)
        assert first.status_code == 200
        again = await client.put("/api/projects/project/resource-profile", json=body)
        assert again.status_code == 200 and again.json() == first.json()
        stale = await client.put("/api/projects/project/resource-profile",
                                 json={**body, "client_operation_id": "resource-profile-2"})
        assert stale.status_code == 409
        read = await client.get("/api/projects/project/resource-profile")
        assert read.status_code == 200
        assert read.json()["profile_revision"] == 1
        assert read.json()["limits"]["process_count"] == 8
    assert (await db.fetchone("SELECT count(*) FROM resource_profile_versions"))[0] == 1


async def test_strict_preflight_refuses_native_disk_and_unproved_daemon(db: Database) -> None:
    await configured(db)
    async with db.transaction() as conn:
        profile = await latest_in(conn, "project")
    assert profile is not None
    with pytest.raises(ControlConflict, match="in-process"):
        strict_target(profile, env="container", harness="daedalus",
                      capability={"available": True, "kind": "cgroup_v2", "sandbox": "ok",
                                  "daemon_instance": "instance"})
    with pytest.raises(ControlConflict, match="delegated"):
        strict_target(profile, env="container", harness="claude",
                      capability={"available": False, "kind": "cgroup_v2", "sandbox": "ok",
                                  "daemon_instance": "instance"})
    accepted = strict_target(profile, env="container", harness="claude",
                             capability={"available": True, "kind": "cgroup_v2", "sandbox": "ok",
                                         "daemon_instance": "instance"})
    assert accepted["limits"] == {"memory_bytes": 64 << 20, "cpu_millis": 500,
                                  "process_count": 8, "disk_bytes": 0}


async def test_writer_only_attempt_binds_without_a_resource_profile(db: Database) -> None:
    app, _, task, _ = await launch_fixture(db)
    try:
        await db.execute("INSERT INTO project_folders(id,project_id,path,env,position,created_at)"
                         " VALUES ('folder','project','/tmp/fixture-worktree','container',0,'2026-01-01')")
        await db.execute("UPDATE staff SET harness = 'claude',isolation = 'shared' WHERE id = 'worker'")
        await db.execute("UPDATE staff_sessions SET kind = 'cli' WHERE id = 'staff-session'")
        member = await StaffStore(db).get("worker")
        session = await StaffStore(db).session("staff-session")
        target = writer_target(env="container", harness="claude",
                               capability={"available": True, "kind": "cgroup_v2", "sandbox": "ok",
                                           "daemon_instance": "daemon-one"})
        target.update({"folder_id": "folder", "workspace_path_digest": "folder-digest",
                       "launch_workspace_digest": "workspace-digest"})
        token = launch_resources.set(target)
        try:
            identity = await prepare_attempt(app, OPERATOR, member,
                                             BoardTask(task.id, task.title, task.status,
                                                       project_id=task.project_id, assignee_staff_id=member.id),
                                             session, fence_token=secrets.token_urlsafe(32))
        finally:
            launch_resources.reset(token)
        row = await db.fetchone("SELECT state,daemon_instance,folder_id FROM writer_attempt_bindings"
                                " WHERE attempt_id = ?", (identity.id,))
        assert (row["state"], row["daemon_instance"], row["folder_id"]) == ("reserved", "daemon-one", "folder")
        assert (await db.fetchone("SELECT count(*) FROM attempt_resource_bindings"))[0] == 0
    finally:
        app.executions.release()


async def test_resource_binding_needs_exact_launch_and_empty_observation(db: Database) -> None:
    app, _, task, _ = await launch_fixture(db)
    try:
        async def effect(conn, mutation):
            return await set_profile_in(conn, project_id="project", state="enabled",
                                        memory_bytes=64 << 20, cpu_millis=500,
                                        process_count=8, disk_bytes=0, min_free_disk_bytes=0,
                                        expected_profile_revision=None,
                                        receipt_id=mutation.receipt_id)

        project = await db.fetchone("SELECT entity_revision FROM projects WHERE id = 'project'")
        await ControlStore(db).mutate(OPERATOR, Scope("project", "project"), "resource.profile.set",
                                      "profile-1", project["entity_revision"],
                                      Entity("project", "project"), {"enabled": True}, effect)
        await db.execute("UPDATE staff SET harness = 'claude' WHERE id = 'worker'")
        await db.execute("UPDATE staff_sessions SET kind = 'cli' WHERE id = 'staff-session'")
        member = await StaffStore(db).get("worker")
        session = await StaffStore(db).session("staff-session")
        assert member is not None and session is not None
        target = {"profile_revision": 1, "env": "container", "daemon_instance": "daemon-one",
                  "limits": {"memory_bytes": 64 << 20, "cpu_millis": 500,
                             "process_count": 8, "disk_bytes": 0}, "min_free_disk_bytes": 0}
        token = launch_resources.set(target)
        try:
            identity = await prepare_attempt(app, OPERATOR, member,
                                             BoardTask(task.id, task.title, task.status,
                                                       project_id=task.project_id,
                                                       assignee_staff_id=member.id),
                                             session, fence_token=secrets.token_urlsafe(32))
        finally:
            launch_resources.reset(token)
        async with db.transaction() as conn:
            bound = await bind_launch_in(conn, attempt_id=identity.id,
                                         host_generation=identity.host_generation, launch_id="launch-one")
            with pytest.raises(ControlConflict, match="different resource launch"):
                await bind_launch_in(conn, attempt_id=identity.id,
                                     host_generation=identity.host_generation, launch_id="launch-two")
            assert not await released_in(conn, identity.id)
            evidence = {"scope": bound["scope"], "daemon_instance": "daemon-one", "enforced": True,
                        "populated": True, "memory_peak_bytes": 1024, "cpu_usage_usec": 20,
                        "processes": 1, "oom_kills": 0, "pids_max_events": 0}
            await observe_in(conn, attempt_id=identity.id, launch_id="launch-one", kind="spawn",
                             evidence=evidence, terminal_id="terminal-one")
            assert not await released_in(conn, identity.id)
            with pytest.raises(ControlConflict, match="different terminal daemon"):
                await observe_in(conn, attempt_id=identity.id, launch_id="launch-one", kind="exit",
                                 evidence={**evidence, "daemon_instance": "daemon-two", "populated": False})
            await observe_in(conn, attempt_id=identity.id, launch_id="launch-one", kind="exit",
                             evidence={**evidence, "populated": False})
            assert await released_in(conn, identity.id)
        row = await db.fetchone("SELECT limits_json,state FROM attempt_resource_bindings WHERE attempt_id = ?",
                                (identity.id,))
        assert row["state"] == "released" and json.loads(row["limits_json"])["cpu_millis"] == 500
    finally:
        app.executions.release()


async def test_unproved_strict_launch_leaves_no_assignment_or_outbox(db: Database) -> None:
    app, _, team, _, revision = await queued_fixture(db)
    try:
        async def effect(conn, mutation):
            return await set_profile_in(conn, project_id="project", state="enabled",
                                        memory_bytes=64 << 20, cpu_millis=500,
                                        process_count=8, disk_bytes=0, min_free_disk_bytes=0,
                                        expected_profile_revision=None,
                                        receipt_id=mutation.receipt_id)

        project = await db.fetchone("SELECT entity_revision FROM projects WHERE id = 'project'")
        await ControlStore(db).mutate(OPERATOR, Scope("project", "project"), "resource.profile.set",
                                      "profile-1", project["entity_revision"],
                                      Entity("project", "project"), {"enabled": True}, effect)
        with pytest.raises(ControlConflict, match="in-process"):
            await queue_launch(app, "task", OPERATOR, staff_id="worker",
                               client_operation_id="strict-native", expected_entity_revision=revision)
        await db.execute("UPDATE staff SET harness = 'claude' WHERE id = 'worker'")
        with pytest.raises(ControlConflict, match="terminal daemon"):
            await queue_launch(app, "task", OPERATOR, staff_id="worker",
                               client_operation_id="strict-cli", expected_entity_revision=revision)
        task = await db.fetchone("SELECT assignee_staff_id FROM board_tasks WHERE id = 'task'")
        assert task["assignee_staff_id"] is None
        assert (await db.fetchone("SELECT count(*) FROM effect_outbox"))[0] == 0
    finally:
        team.queue.close()
        app.executions.release()


async def test_exact_cli_preflight_is_pinned_in_outbox_and_replay_needs_no_new_daemon(db: Database) -> None:
    app, _, team, _, revision = await queued_fixture(db)
    try:
        async def effect(conn, mutation):
            return await set_profile_in(conn, project_id="project", state="enabled",
                                        memory_bytes=64 << 20, cpu_millis=500,
                                        process_count=8, disk_bytes=0, min_free_disk_bytes=0,
                                        expected_profile_revision=None,
                                        receipt_id=mutation.receipt_id)

        project = await db.fetchone("SELECT entity_revision FROM projects WHERE id = 'project'")
        await ControlStore(db).mutate(OPERATOR, Scope("project", "project"), "resource.profile.set",
                                      "profile-1", project["entity_revision"],
                                      Entity("project", "project"), {"enabled": True}, effect)
        await db.execute("UPDATE staff SET harness = 'claude' WHERE id = 'worker'")
        seen = []

        async def capability(env):
            assert env == "container"
            return {"available": True, "kind": "cgroup_v2", "sandbox": "ok",
                    "daemon_instance": "daemon-one"}

        async def preflight(env, *, limits, daemon_instance, workspace_path=None, min_free_disk_bytes=0):
            seen.append((env, limits, daemon_instance))
            return {}

        app.extensions["terminals"] = SimpleNamespace(
            containment_capability=capability, preflight_attempt_resources=preflight)
        command = {"staff_id": "worker", "client_operation_id": "strict-launch",
                   "expected_entity_revision": revision}
        result = await queue_launch(app, "task", OPERATOR, **command)
        assert result["state"] == "queued" and len(seen) == 1
        outbox = await db.fetchone("SELECT payload_json FROM effect_outbox WHERE id = ?", (result["effect_id"],))
        target = json.loads(outbox["payload_json"])["data"]
        assert target["resources"]["daemon_instance"] == "daemon-one"
        assert target["resources"]["limits"]["process_count"] == 8
        app.extensions.pop("terminals")
        assert await queue_launch(app, "task", OPERATOR, **command) == result
        assert len(seen) == 1
        assert (await db.fetchone("SELECT assignee_staff_id FROM board_tasks WHERE id = 'task'"))[0] == "worker"
    finally:
        team.queue.close()
        app.executions.release()


def test_selected_workspace_observation_checks_threshold_and_volume() -> None:
    path = "/workspace/project/.agents/worktrees/worker"
    observed = {"available": True, "free_bytes": 20 << 20, "device_id": "1:2",
                "mount_id": "17", "observed_at": "2026-01-01T00:00:00Z",
                "path_digest": hashlib.sha256(path.encode()).hexdigest(), "workspace_exists": False}
    admitted = disk_snapshot(observed, path=path, minimum=10 << 20)
    assert admitted["free_bytes"] == 20 << 20 and admitted["mount_id"] == "17"
    with pytest.raises(ControlConflict, match="less free space"):
        disk_snapshot({**observed, "free_bytes": 9 << 20}, path=path, minimum=10 << 20)
    with pytest.raises(ControlConflict, match="volume changed"):
        disk_snapshot({**observed, "mount_id": "18"}, path=path,
                      minimum=10 << 20, admitted=admitted)
    with pytest.raises(ControlConflict, match="incomplete or stale"):
        disk_snapshot({**observed, "path_digest": "other"}, path=path, minimum=10 << 20)
    with pytest.raises(ControlConflict, match="unknown"):
        disk_snapshot({"available": False, "reason": "remote unavailable"}, path=path,
                      minimum=10 << 20)


async def test_insufficient_selected_workspace_refuses_before_assignment_or_outbox(db: Database) -> None:
    app, _, team, _, revision = await queued_fixture(db)
    try:
        async def effect(conn, mutation):
            return await set_profile_in(conn, project_id="project", state="enabled",
                                        memory_bytes=64 << 20, cpu_millis=500, process_count=8,
                                        disk_bytes=0, min_free_disk_bytes=32 << 20,
                                        expected_profile_revision=None, receipt_id=mutation.receipt_id)

        project = await db.fetchone("SELECT entity_revision FROM projects WHERE id = 'project'")
        await ControlStore(db).mutate(OPERATOR, Scope("project", "project"), "resource.profile.set",
                                      "disk-profile", project["entity_revision"], Entity("project", "project"),
                                      {"minimum": 32 << 20}, effect)
        await db.execute("UPDATE staff SET harness = 'claude' WHERE id = 'worker'")

        async def capability(_env):
            return {"available": True, "kind": "cgroup_v2", "sandbox": "ok",
                    "daemon_instance": "daemon-one"}

        async def preflight(_env, *, limits, daemon_instance, workspace_path, min_free_disk_bytes):
            assert workspace_path.startswith("/tmp/fixture-worktree")
            assert min_free_disk_bytes == 32 << 20
            return {"disk": {"available": True, "free_bytes": 16 << 20, "device_id": "1:2",
                             "mount_id": "17", "observed_at": "2026-01-01T00:00:00Z",
                             "path_digest": hashlib.sha256(workspace_path.encode()).hexdigest(),
                             "workspace_exists": True}}

        app.extensions["terminals"] = SimpleNamespace(
            containment_capability=capability, preflight_attempt_resources=preflight)
        with pytest.raises(ControlConflict, match="less free space"):
            await queue_launch(app, "task", OPERATOR, staff_id="worker",
                               client_operation_id="disk-low", expected_entity_revision=revision)
        assert (await db.fetchone("SELECT assignee_staff_id FROM board_tasks WHERE id = 'task'"))[0] is None
        assert (await db.fetchone("SELECT count(*) FROM effect_outbox"))[0] == 0
    finally:
        team.queue.close()
        app.executions.release()


async def test_disk_preflight_is_bound_to_one_attempt_and_entry_observation(db: Database) -> None:
    app, _, task, _ = await launch_fixture(db)
    try:
        await db.execute("INSERT INTO project_folders(id,project_id,path,env,position,created_at)"
                         " VALUES ('folder','project','/workspace/project','container',0,'2026-01-01')")

        async def effect(conn, mutation):
            return await set_profile_in(conn, project_id="project", state="enabled",
                                        memory_bytes=64 << 20, cpu_millis=500, process_count=8,
                                        disk_bytes=0, min_free_disk_bytes=10 << 20,
                                        expected_profile_revision=None, receipt_id=mutation.receipt_id)

        project = await db.fetchone("SELECT entity_revision FROM projects WHERE id = 'project'")
        await ControlStore(db).mutate(OPERATOR, Scope("project", "project"), "resource.profile.set",
                                      "disk-profile", project["entity_revision"], Entity("project", "project"),
                                      {"minimum": 10 << 20}, effect)
        await db.execute("UPDATE staff SET harness = 'claude' WHERE id = 'worker'")
        await db.execute("UPDATE staff_sessions SET kind = 'cli' WHERE id = 'staff-session'")
        member = await StaffStore(db).get("worker")
        session = await StaffStore(db).session("staff-session")
        assert member is not None and session is not None
        path = "/workspace/project/.agents/worktrees/worker"
        observed = {"available": True, "free_bytes": 20 << 20, "device_id": "1:2", "mount_id": "17",
                    "observed_at": "2026-01-01T00:00:00Z", "path_digest": hashlib.sha256(path.encode()).hexdigest(),
                    "workspace_exists": True}
        pinned = disk_snapshot(observed, path=path, minimum=10 << 20)
        target = {"profile_revision": 1, "env": "container", "daemon_instance": "daemon-one",
                  "limits": {"memory_bytes": 64 << 20, "cpu_millis": 500, "process_count": 8, "disk_bytes": 0},
                  "min_free_disk_bytes": 10 << 20, "folder_id": "folder",
                  "disk_admission": pinned, "disk_preparation": pinned}
        token = launch_resources.set(target)
        try:
            identity = await prepare_attempt(app, OPERATOR, member,
                                             BoardTask(task.id, task.title, task.status,
                                                       project_id=task.project_id,
                                                       assignee_staff_id=member.id),
                                             session, fence_token=secrets.token_urlsafe(32))
        finally:
            launch_resources.reset(token)
        async with db.transaction() as conn:
            await record_disk_entry_in(conn, attempt_id=identity.id, observation=pinned)
            with pytest.raises(ControlConflict, match="volume or path changed"):
                await record_disk_entry_in(conn, attempt_id=identity.id,
                                           observation={**pinned, "mount_id": "18"})
        row = await db.fetchone("SELECT admission_json,preparation_json FROM attempt_disk_preflights"
                                " WHERE attempt_id = ?", (identity.id,))
        assert json.loads(row["admission_json"])["mount_id"] == "17"
        assert (await db.fetchone("SELECT count(*) FROM attempt_disk_entry_observations"
                                  " WHERE attempt_id = ?", (identity.id,)))[0] == 1
    finally:
        app.executions.release()
