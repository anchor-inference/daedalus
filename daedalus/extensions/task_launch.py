"""A queued launch retains its authenticated command until capacity and ownership are proven."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.extensions.launch_controls import launch_attempt
from daedalus.extensions.orchestrator_domain import dependency_readiness, next_action_readiness
from daedalus.extensions.runtime_handoff import (
    packet_for_launch,
    snapshot_in,
    target_capability,
)
from daedalus.extensions.runtime_handoff import preview as handoff_preview
from daedalus.host.launch_queue import Entry
from daedalus.host.worktrees import WORKTREES_DIR, WorktreeError, WorktreeRefused, staff_slug
from daedalus.stores.control import (
    ControlConflict,
    ControlDenied,
    ControlStore,
    Entity,
    Principal,
    Scope,
    canonical,
    digest,
    now,
    one,
)
from daedalus.stores.executions import ACTIVE
from daedalus.stores.goal_budget import requires_priced_native_in
from daedalus.stores.goal_budget import view_in as goal_budget_view_in
from daedalus.stores.outbox import Claim, OutboxStore
from daedalus.stores.resource_profiles import (
    disk_snapshot,
    latest_in,
    record_disk_entry_in,
    strict_target,
    writer_target,
)
from daedalus.stores.runtime_release import attempt_released_in, no_entry_in
from daedalus.stores.staff import DAEDALUS_EFFORTS, SetupFailed, StaffError, daedalus_cannot_reach, daedalus_reaches
from daedalus.stores.update_drains import UpdateDrainActive, assert_admission_open_in

if TYPE_CHECKING:
    from daedalus.app import Application


def task_digest(task: Any) -> str:
    return digest({field: task[field] for field in ("project_id", "title", "brief_json", "depends_on",
                                            "contract_revision", "assignee_staff_id")})


def member_digest(member: Any) -> str:
    return digest({field: member[field] for field in ("project_id", "harness", "agent", "model", "effort", "permission_mode", "default_folder_id", "isolation", "instructions", "role_revision")})


async def queue_launch(app: Application, task_id: str, principal: Principal, *, staff_id: str,
                       client_operation_id: str, expected_entity_revision: int,
                       resume_from: str | None = None, effort: str | None = None,
                       fallback_from_attempt_id: str | None = None,
                       fallback_preview_digest: str | None = None,
                       handoff_fingerprint: str | None = None) -> dict[str, Any]:
    row = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
    if row is None:
        raise KeyError(task_id)
    if not row["project_id"]:
        raise ControlConflict("a worker task needs a project")
    scope = Scope("project", row["project_id"])
    if fallback_from_attempt_id and (resume_from or not fallback_preview_digest):
        raise ControlConflict("a runtime handoff needs its reviewed preview, not provider-session resume")
    operation = "task.continue" if fallback_from_attempt_id else "task.launch"
    payload = {"staff_id": staff_id, "resume_from": resume_from, "effort": effort,
               "fallback_from_attempt_id": fallback_from_attempt_id,
               "fallback_preview_digest": fallback_preview_digest}
    if handoff_fingerprint is not None:
        payload["handoff_fingerprint"] = handoff_fingerprint
    control = ControlStore(app.db)
    resources: dict[str, Any] | None = None
    async with app.db.transaction() as conn:
        await control.authorize(conn, principal, scope, operation, task_id=task_id,
                                effects=("execution.start",))
        request_hash = digest({"entity": {"kind": "task", "id": task_id},
                               "expected_revision": expected_entity_revision, "payload": payload,
                               "effects": ["execution.start"]})
        receipt = await one(conn, "SELECT payload_hash,response_json FROM operation_receipts"
                            " WHERE scope_kind = 'project' AND scope_id = ? AND actor_id = ?"
                            " AND operation_kind = ? AND client_operation_id = ?",
                            (scope.id, principal.actor_id, operation, client_operation_id))
        if receipt is not None:
            if receipt["payload_hash"] != request_hash:
                raise ControlConflict("the command identity was reused with a different request")
            return json.loads(receipt["response_json"])
        profile = await latest_in(conn, scope.id)
        member_row = await one(conn, "SELECT harness,name,isolation,default_folder_id FROM staff"
                               " WHERE id = ? AND project_id = ? AND archived_at IS NULL",
                               (staff_id, scope.id))
        if member_row is None:
            raise ControlDenied("the worker is not an active member of this project")
        if (profile is not None and profile["state"] == "enabled"
                or member_row["harness"] != "daedalus" and member_row["isolation"] == "shared"):
            task_row = await one(conn, "SELECT folder_id FROM board_tasks WHERE id = ?", (task_id,))
            folder = None
            for folder_id in (task_row["folder_id"], member_row["default_folder_id"]):
                if folder_id:
                    folder = await one(conn, "SELECT id,path,env FROM project_folders WHERE id = ?"
                                       " AND project_id = ?", (folder_id, scope.id))
                    if folder is not None:
                        break
            if folder is None:
                folder = await one(conn, "SELECT id,path,env FROM project_folders WHERE project_id = ?"
                                   " ORDER BY position LIMIT 1", (scope.id,))
            if folder is None:
                raise ControlConflict("the project has no worker folder")
            env, harness = folder["env"], member_row["harness"]
            workspace_path = str(Path(folder["path"]) / WORKTREES_DIR / staff_slug(member_row["name"])) if member_row["isolation"] == "worktree" else folder["path"]
        else:
            env = harness = ""
    fallback = None
    if fallback_from_attempt_id:
        if principal.origin_class != "operator":
            raise ControlDenied("cross-runtime continuation requires an operator's reviewed choice")
        fallback = await handoff_preview(app, task_id, fallback_from_attempt_id, staff_id)
        if fallback["preview_digest"] != fallback_preview_digest or not fallback["ready"]:
            raise ControlConflict("the runtime handoff preview is stale or its source is not released")
    strict_profile = profile is not None and profile["state"] == "enabled"
    writer_only = not strict_profile and member_row["harness"] != "daedalus" and member_row["isolation"] == "shared"
    if writer_only and await app.db.fetchone("SELECT 1 FROM task_files WHERE task_id = ? LIMIT 1", (task_id,)):
        raise ControlConflict("shared writer containment cannot own initial file delivery; "
                              "use an isolated Git worktree member for a task with files")
    if strict_profile or writer_only:
        if strict_profile and harness == "daedalus":
            strict_target(profile, env=env, harness=harness, capability={})
        terminals = app.extensions.get("terminals")
        if terminals is None:
            raise ControlConflict("the terminal daemon cannot prove resource containment")
        capability = await terminals.containment_capability(env)
        resources = (strict_target(profile, env=env, harness=harness, capability=capability)
                     if strict_profile else writer_target(env=env, harness=harness, capability=capability))
        resources["folder_id"] = folder["id"]
        resources["workspace_path_digest"] = digest(folder["path"])
        resources["launch_workspace_digest"] = digest(workspace_path)
        try:
            preflight = await terminals.preflight_attempt_resources(
                env, limits=resources["limits"], daemon_instance=resources["daemon_instance"],
                workspace_path=workspace_path if resources["min_free_disk_bytes"] else None,
                min_free_disk_bytes=resources["min_free_disk_bytes"])
        except Exception as exc:
            raise ControlConflict("selected workspace resource preflight is unavailable") from exc
        if resources["min_free_disk_bytes"]:
            resources["disk_admission"] = disk_snapshot(preflight.get("disk") if preflight else None,
                                                         path=workspace_path, minimum=resources["min_free_disk_bytes"])

    # Pin the source before recording the durable launch. Replayed effects must check this same
    # commit, even if the host restarts while the assignment waits for capacity.
    source_head: str | None = None
    source_folder_id: str | None = None
    source_path_digest: str | None = None
    source_env: str | None = None
    selected_member = await app.db.fetchone("SELECT isolation FROM staff WHERE id = ? AND project_id = ?",
                                            (staff_id, scope.id))
    if selected_member is not None and selected_member["isolation"] == "worktree":
        team = app.extensions.get("staff")
        if team is None:
            raise ControlConflict("the staff runtime is not ready to inspect the selected workspace")
        member_for_source = await team.member(staff_id)
        task_for_source = await team.task(task_id)
        project_for_source = await team.project(row["project_id"])
        folder_for_source = team.folder_for(project_for_source, member_for_source, task_for_source)
        source_folder_id = folder_for_source.id
        source_path_digest = digest(str(folder_for_source.path))
        source_env = folder_for_source.env
        try:
            source_head = await team.worktrees.check(folder_for_source)
        except WorktreeRefused as exc:
            from daedalus.extensions.staff import no_worktree  # Lazy: staff installs the launch handler

            raise StaffError(no_worktree(member_for_source, task_for_source, exc)) from exc
        except WorktreeError as exc:
            raise StaffError(f"the selected workspace cannot be inspected yet: {exc}") from exc

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        await assert_admission_open_in(conn)
        task = await one(conn, "SELECT * FROM board_tasks WHERE id = ?", (task_id,))
        member = await one(conn, "SELECT * FROM staff WHERE id = ?", (staff_id,))
        assert task is not None
        if handoff_fingerprint is not None:
            readiness = await dependency_readiness(conn, task_id)
            if (not readiness["edges"] or not readiness["ready"]
                    or readiness["dependency_fingerprint"] != handoff_fingerprint):
                raise ControlConflict("the handoff dependencies changed before assignment")
            action = await one(conn, "SELECT id,kind,owner_kind,owner_id FROM next_actions"
                               " WHERE task_id = ? AND state = 'active' ORDER BY created_at DESC,id DESC LIMIT 1",
                               (task_id,))
            if (action is None or action["kind"] != "assign" or action["owner_kind"] != "staff"
                    or action["owner_id"] != staff_id):
                raise ControlConflict("the approved next assignment changed before handoff")
            if await one(conn, "SELECT 1 FROM handoff_claims WHERE task_id = ? AND dependency_fingerprint = ?",
                         (task_id, handoff_fingerprint)):
                raise ControlConflict("this dependency handoff was already claimed")
        if member is None or member["project_id"] != task["project_id"] or member["archived_at"]:
            raise ControlDenied("the worker is not an active member of this project")
        if effort is not None and (member["harness"] != "daedalus" or effort not in DAEDALUS_EFFORTS[1:]):
            raise ControlConflict("an assignment's effort is a Daedalus effort: off, low, medium, high or xhigh")
        folder = None
        for folder_id in (task["folder_id"], member["default_folder_id"]):
            if folder_id:
                folder = await one(conn, "SELECT id,path,env FROM project_folders"
                                   " WHERE id = ? AND project_id = ?", (folder_id, task["project_id"]))
                if folder is not None:
                    break
        if folder is None:
            folder = await one(conn, "SELECT id,path,env FROM project_folders"
                               " WHERE project_id = ? ORDER BY position LIMIT 1", (task["project_id"],))
        if folder is None:
            raise StaffError("the project has no folder to work in")
        if source_head is not None and (folder["id"] != source_folder_id
                or digest(folder["path"]) != source_path_digest or folder["env"] != source_env):
            raise ControlConflict("the selected workspace changed before the launch was recorded")
        if member["harness"] == "daedalus" and not daedalus_reaches(folder["env"], app.manager.projects.local_env):
            # A queued command must refuse an unreachable folder before it records an assignment.
            raise StaffError(f"{member['name']} cannot work on task {task_id}: "
                             + daedalus_cannot_reach(folder["path"], folder["env"],
                                                     app.manager.projects.local_env))
        if member["harness"] != "daedalus" and await requires_priced_native_in(conn, task["project_id"]):
            raise ControlConflict("a dollar-capped project needs priced native worker admission")
        current_profile = await latest_in(conn, task["project_id"])
        if resources is None:
            if current_profile is not None and current_profile["state"] == "enabled":
                raise ControlConflict("the resource profile changed before launch")
        elif (folder["env"] != resources["env"] or folder["id"] != resources["folder_id"]
              or digest(folder["path"]) != resources["workspace_path_digest"]
              or (resources.get("mode") == "writer" and current_profile is not None and current_profile["state"] == "enabled")
              or (resources.get("mode") != "writer" and (current_profile is None or current_profile["state"] != "enabled"
                  or int(current_profile["revision"]) != resources["profile_revision"]
                  or int(current_profile["min_free_disk_bytes"]) != resources["min_free_disk_bytes"]))):
            raise ControlConflict("the resource profile or worker environment changed before launch")
        if fallback is not None:
            current = await snapshot_in(conn, task_id=task_id, source_attempt_id=fallback_from_attempt_id,
                                        target_staff_id=staff_id,
                                        context_hash=fallback["packet"]["context_hash"])
            if (not current["source_released"] or current["packet_digest"] != fallback["packet_digest"]
                    or current["target_member_digest"] != fallback["target_member_digest"]
                    or current["budget"] != fallback["budget"]):
                raise ControlConflict("the reviewed runtime handoff changed before the command committed")
            if task["status"] == "doing":
                await conn.execute("UPDATE board_tasks SET status = 'blocked' WHERE id = ?", (task_id,))
        if task["status"] not in ("todo", "blocked") and fallback is None:
            raise ControlConflict("reopen the task before launching a new attempt")
        if await one(conn, "SELECT 1 FROM comparison_groups WHERE task_id = ? AND state IN ('planned','active','ready')", (task_id,)):
            raise ControlConflict('reconcile the undecided comparison before launching an ordinary attempt')
        parent = await one(conn, "SELECT cancel_state FROM lifecycle_parents WHERE parent_kind = 'task' AND parent_id = ?", (task_id,))
        if parent is not None and parent["cancel_state"] != "active":
            raise ControlConflict("the task parent was cancelled; revise its contract before admitting new work")
        if await one(conn, "SELECT 1 FROM task_contract_versions WHERE task_id = ? AND contract_revision = ?",
                     (task_id, task["contract_revision"])) is None:
            raise ControlConflict("the task needs an immutable contract before launch")
        if task["current_attempt_id"]:
            prior = await one(conn, "SELECT state FROM execution_attempts WHERE id = ?", (task["current_attempt_id"],))
            if prior is not None and prior["state"] in (*ACTIVE, "recovering"):
                raise ControlConflict("stop or reconcile the previous execution before launching again")
        if await one(conn, "SELECT 1 FROM effect_outbox WHERE kind = 'task.launch' AND state IN ('pending','claimed','unknown')"
                     " AND json_extract(payload_json,'$.control.task_id') = ?", (task_id,)):
            raise ControlConflict("this task already has a queued or uncertain launch")
        await conn.execute("UPDATE board_tasks SET assignee_staff_id = ? WHERE id = ?", (staff_id, task_id))
        effort_key = f"staff_effort:{staff_id}:{task_id}"
        if effort is None:
            await conn.execute("DELETE FROM kv WHERE key = ?", (effort_key,))
        else:
            await conn.execute("INSERT INTO kv(key,value) VALUES (?,?)"
                               " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                               (effort_key, canonical(effort)))
        assigned = await one(conn, "SELECT * FROM board_tasks WHERE id = ?", (task_id,))
        action_id = uuid.uuid5(uuid.NAMESPACE_URL, f"effect:{mutation.receipt_id}:task.launch").hex
        attempt_id = uuid.uuid5(uuid.NAMESPACE_URL, f"attempt:{action_id}").hex
        target = {"task_id": task_id, "staff_id": staff_id, "resume_from": resume_from, "effort": effort,
                  "previous_attempt_id": task["current_attempt_id"], "attempt_id": attempt_id,
                  "folder_id": task["folder_id"], "source_head": source_head,
                  "source_folder_id": source_folder_id, "source_path_digest": source_path_digest,
                  "source_env": source_env, "task_digest": task_digest(assigned), "member_digest": member_digest(member),
                  "resources": resources, "fallback_decision_id": mutation.object_id if fallback is not None else None,
                  "fallback_capability_digest": digest(fallback["capability"]) if fallback is not None else None,
                  "handoff_fingerprint": handoff_fingerprint,
                  "handoff_action_id": action["id"] if handoff_fingerprint is not None else None}
        if fallback is not None:
            await conn.execute("INSERT INTO runtime_handoffs(id,task_id,project_id,source_attempt_id,"
                               "source_result_id,target_staff_id,target_attempt_id,contract_revision,"
                               "operation_receipt_id,preview_digest,packet_digest,packet_json,created_at)"
                               " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (mutation.object_id, task_id, task["project_id"], fallback_from_attempt_id,
                                fallback["packet"]["source_result"]["id"] if fallback["packet"]["source_result"] else None,
                                staff_id, attempt_id, task["contract_revision"], mutation.receipt_id,
                                fallback_preview_digest, fallback["packet_digest"], canonical(fallback["packet"]), now()))
        if handoff_fingerprint is not None:
            await conn.execute("INSERT INTO handoff_claims(id,task_id,dependency_fingerprint,attempt_id,"
                               " reservation_id,operation_id,state,created_at)"
                               " VALUES (?,?,?,?,?,?,'claimed',?)",
                               (mutation.object_id, task_id, handoff_fingerprint, None,
                                action_id, None, now()))
        await OutboxStore.enqueue(conn, mutation, principal, kind="task.launch", operation=operation,
                                  payload=target, task_id=task_id, effects=("execution.start",))
        return {"task_id": task_id, "effect_id": action_id, "state": "queued",
                **({"claim_id": mutation.object_id} if handoff_fingerprint is not None else {}),
                **({"handoff_id": mutation.object_id, "new_attempt_id": attempt_id} if fallback is not None else {})}

    result = await control.mutate(principal, scope, operation, client_operation_id,
                                              expected_entity_revision, Entity("task", task_id), payload,
                                              effect, effects=("execution.start",))
    if handoff_fingerprint is not None:
        await app.db.execute("UPDATE handoff_claims SET operation_id ="
                             " (SELECT receipt_id FROM effect_outbox WHERE id = handoff_claims.reservation_id)"
                             " WHERE id = ? AND operation_id IS NULL", (result["claim_id"],))
    app.extensions["effects"].notify()
    return result


class TaskLaunchEffect:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def validate(self, claim: Claim) -> None:
        target = claim.payload
        task = await self.app.db.fetchone("SELECT * FROM board_tasks WHERE id = ?", (target["task_id"],))
        member = await self.app.db.fetchone("SELECT * FROM staff WHERE id = ?", (target["staff_id"],))
        if task is None or member is None or member["archived_at"] or task_digest(task) != target["task_digest"] or member_digest(member) != target["member_digest"]:
            raise ControlDenied("the task contract or worker configuration changed after the launch command")
        current_effort = await self.app.db.fetchone("SELECT value FROM kv WHERE key = ?",
                                                    (f"staff_effort:{target['staff_id']}:{target['task_id']}",))
        if (json.loads(current_effort["value"]) if current_effort else None) != target.get("effort"):
            raise ControlDenied("the assignment effort changed after the launch command")
        if target.get("handoff_fingerprint"):
            async with self.app.db.transaction() as conn:
                readiness = await dependency_readiness(conn, task["id"])
                action = await next_action_readiness(conn, task["id"])
                valid_handoff = (readiness["ready"] and
                                 readiness["dependency_fingerprint"] == target["handoff_fingerprint"] and
                                 action is not None and action["enabled"] and
                                 action["action_id"] == target["handoff_action_id"] and
                                 action["kind"] == "assign" and action["owner_kind"] == "staff" and
                                 action["owner_id"] == member["id"])
            if not valid_handoff:
                await self.app.db.execute("UPDATE handoff_claims SET state = 'failed'"
                                          " WHERE reservation_id = ? AND state = 'claimed'", (claim.id,))
                raise ControlDenied("the approved next assignment changed before runtime entry")
        if "folder_id" not in target:
            raise ControlDenied("the queued launch has no pinned folder")
        if task["folder_id"] != target["folder_id"]:
            # The host fills an initially unspecified folder while claiming its own session.
            # A changed folder before that claim, or a different claimed folder, is stale work.
            attempt = await self.app.db.fetchone(
                "SELECT s.folder_id FROM execution_attempts a JOIN staff_sessions s"
                " ON s.id = a.staff_session_id WHERE a.id = ?", (target["attempt_id"],),
            )
            if (target["folder_id"] is not None or task["current_attempt_id"] != target["attempt_id"]
                    or task["status"] != "doing" or attempt is None
                    or task["folder_id"] != attempt["folder_id"]):
                raise ControlDenied("the task folder changed after the launch command")
        if task["current_attempt_id"] not in (target["previous_attempt_id"], target["attempt_id"]):
            raise ControlDenied("another attempt replaced this launch command")
        if target.get("fallback_decision_id"):
            row = await self.app.db.fetchone("SELECT source_attempt_id,target_staff_id,target_attempt_id,"
                                             " contract_revision FROM runtime_handoffs WHERE id = ?",
                                             (target["fallback_decision_id"],))
            if (row is None or row["source_attempt_id"] != target["previous_attempt_id"]
                    or row["target_staff_id"] != target["staff_id"]
                    or row["target_attempt_id"] != target["attempt_id"]
                    or row["contract_revision"] != task["contract_revision"]):
                raise ControlDenied("the approved handoff no longer matches this launch")
        if task["status"] not in ("todo", "blocked", "doing"):
            raise ControlDenied("the task was closed or handed in before launch")
        profile = await self.app.db.fetchone("SELECT revision,state,min_free_disk_bytes FROM resource_profile_versions"
                                            " WHERE project_id = ? ORDER BY revision DESC LIMIT 1",
                                            (task["project_id"],))
        resources = target.get("resources")
        if resources is None:
            if profile is not None and profile["state"] == "enabled":
                raise ControlDenied("the resource profile changed after the launch command")
        elif ((resources.get("mode") == "writer" and profile is not None and profile["state"] == "enabled")
              or (resources.get("mode") != "writer" and (profile is None or profile["state"] != "enabled"
                  or profile["revision"] != resources["profile_revision"]
                  or profile["min_free_disk_bytes"] != resources["min_free_disk_bytes"]))):
            raise ControlDenied("the resource profile changed after the launch command")

    async def run(self, claim: Claim, check: Callable[[Claim], Awaitable[None]]) -> EffectOutcome:
        try:
            async with self.app.db.transaction() as conn:
                await assert_admission_open_in(conn)
        except UpdateDrainActive as exc:
            return EffectOutcome("deferred", canonical({"reason": "update_drain", "detail": str(exc)}))
        team = self.app.extensions.get("staff")
        if team is None:
            return EffectOutcome("deferred", "the staff runtime is not ready")
        try:
            await self.validate(claim)
            resource = claim.payload.get("resources")
            if resource is not None:
                capability = await self.app.extensions["terminals"].containment_capability(resource["env"])
                if (not capability.get("available") or capability.get("sandbox") != "ok"
                        or capability.get("daemon_instance") != resource["daemon_instance"]):
                    raise ControlDenied("the terminal daemon cannot enforce the pinned resource profile")
            member = await team.member(claim.payload["staff_id"])
            task = await team.task(claim.payload["task_id"])
            assert task is not None
            if task.missing():
                return EffectOutcome("failed", "the task brief is incomplete")
            project = await team.project(member.project_id)
            folder = team.folder_for(project, member, task)
            if resource is not None:
                workspace_path = str(Path(folder.path) / WORKTREES_DIR / staff_slug(member.name)) if member.isolation == "worktree" else str(folder.path)
                if (folder.id != resource["folder_id"] or digest(str(folder.path)) != resource["workspace_path_digest"]
                        or digest(workspace_path) != resource["launch_workspace_digest"]):
                    raise ControlDenied("the selected workspace changed after resource admission")
                try:
                    preflight = await self.app.extensions["terminals"].preflight_attempt_resources(
                        resource["env"], limits=resource["limits"], daemon_instance=resource["daemon_instance"],
                        workspace_path=workspace_path if resource["min_free_disk_bytes"] else None,
                        min_free_disk_bytes=resource["min_free_disk_bytes"])
                except Exception as exc:
                    raise ControlDenied("selected workspace resource preflight is unavailable") from exc
                if resource["min_free_disk_bytes"]:
                    disk_snapshot(preflight.get("disk") if preflight else None,
                                  path=workspace_path, minimum=resource["min_free_disk_bytes"],
                                  admitted=resource["disk_admission"])
            if claim.payload.get("fallback_decision_id"):
                await packet_for_launch(self.app.db, claim.payload["fallback_decision_id"],
                                        task_id=task.id, target_staff_id=member.id)
                capability = await target_capability(self.app, member.harness, folder.env, member.id)
                if (not capability["available"] or digest(capability) != claim.payload["fallback_capability_digest"]):
                    raise ControlDenied("the approved alternate runtime capability changed")
            if member.isolation == "worktree":
                if (folder.id != claim.payload.get("source_folder_id")
                        or digest(str(folder.path)) != claim.payload.get("source_path_digest")
                        or folder.env != claim.payload.get("source_env")):
                    return EffectOutcome("failed", "the selected workspace changed since launch approval")
                from daedalus.extensions.staff import no_worktree  # Lazy: staff installs the launch handler

                expected_head = claim.payload.get("source_head")
                if not expected_head:
                    return EffectOutcome("failed", "the queued launch has no pinned source commit")
                try:
                    current_head = await team.worktrees.check(folder)
                except WorktreeRefused as exc:
                    return EffectOutcome("failed", no_worktree(member, task, exc))
                except WorktreeError:
                    return EffectOutcome("deferred", "the selected workspace cannot be inspected yet")
                if current_head != expected_head:
                    return EffectOutcome("failed", "the selected workspace moved since launch approval; assign again")
            if claim.payload["resume_from"]:
                await team._resume_source(member, task, folder, claim.payload["resume_from"])
            async with self.app.db.transaction() as conn:
                readiness = await dependency_readiness(conn, task.id)
            if not readiness["ready"]:
                return EffectOutcome("deferred", "the task waits for current accepted dependency results")
        except (ControlDenied, ValueError, KeyError) as exc:
            return EffectOutcome("failed", str(exc))

        async def authorized() -> None:
            await check(claim)
            await self.validate(claim)
            if resource is not None and resource["min_free_disk_bytes"]:
                try:
                    preflight = await self.app.extensions["terminals"].preflight_attempt_resources(
                        resource["env"], limits=resource["limits"], daemon_instance=resource["daemon_instance"],
                        workspace_path=workspace_path, min_free_disk_bytes=resource["min_free_disk_bytes"])
                except Exception as exc:
                    raise ControlDenied("selected workspace free space is unknown before runtime entry") from exc
                snapshot = disk_snapshot(preflight.get("disk") if preflight else None,
                    path=workspace_path, minimum=resource["min_free_disk_bytes"], admitted=resource["disk_admission"])
                resource["disk_preparation"] = snapshot
            if claim.payload.get("fallback_decision_id"):
                capability = await target_capability(self.app, member.harness, folder.env, member.id)
                if (not capability["available"] or digest(capability) != claim.payload["fallback_capability_digest"]):
                    raise ControlDenied("the approved alternate runtime capability changed before entry")
            async with self.app.db.transaction() as conn:
                await assert_admission_open_in(conn)
                parent = await one(conn, "SELECT cancel_state FROM lifecycle_parents WHERE parent_kind = 'task' AND parent_id = ?",
                                   (claim.payload["task_id"],))
                if parent is not None and parent["cancel_state"] != "active":
                    raise ControlDenied("the parent cancelled this launch before provider admission")
                if claim.payload.get("fallback_decision_id"):
                    handoff = await one(conn, "SELECT project_id,target_staff_id FROM runtime_handoffs WHERE id = ?",
                                        (claim.payload["fallback_decision_id"],))
                    if handoff is None or handoff["target_staff_id"] != member.id:
                        raise ControlDenied("the alternate worker lost its approved handoff")
                    if await one(conn, "SELECT 1 FROM staff_sessions WHERE staff_id = ? AND ended_at IS NULL"
                                 " AND id != COALESCE((SELECT staff_session_id FROM execution_attempts"
                                 " WHERE id = ?),'')", (member.id, claim.payload["attempt_id"])):
                        raise ControlDenied("the alternate worker is already in another session")
                    budget = await goal_budget_view_in(conn, handoff["project_id"])
                    if budget is not None and (member.harness != "daedalus"
                            or budget["total"]["state"] != "known"
                            or float(budget["total"]["available_usd"] or "0") <= 0):
                        raise ControlDenied("the alternate runtime cannot enter under the current goal budget")
                attempt = await one(conn, "SELECT id FROM execution_attempts WHERE id = ?", (claim.payload["attempt_id"],))
                if attempt is not None:
                    await self.app.executions._check(conn, attempt["id"], operation="result.submit")
                    if resource is not None and resource["min_free_disk_bytes"]:
                        if snapshot["workspace_exists"] is not True:
                            raise ControlDenied("the selected worktree does not exist before runtime entry")
                        await record_disk_entry_in(conn, attempt_id=attempt["id"], observation=snapshot)
                elif claim.payload.get("fallback_decision_id"):
                    handoff = await one(conn, "SELECT source_attempt_id FROM runtime_handoffs WHERE id = ?",
                                        (claim.payload["fallback_decision_id"],))
                    if handoff is None or not await attempt_released_in(conn, handoff["source_attempt_id"]):
                        raise ControlDenied("the prior runtime is not physically released")

        entry = Entry(project.id, member.id, member.name, task.id, task.priority,
                      member.harness != "daedalus", "operator" if claim.principal.origin_class == "operator" else "orchestrator",
                      env=folder.env, resume_from=claim.payload["resume_from"], source_head=claim.payload.get("source_head"),
                      principal=claim.principal, check_authority=authorized,
                      resources=claim.payload.get("resources"),
                      fallback_decision_id=claim.payload.get("fallback_decision_id"),
                      attempt_id=claim.payload["attempt_id"],
                      role_class="reviewer" if member.role.strip().lower() == "reviewer" else "worker")
        bound = launch_attempt.set(claim.payload["attempt_id"])
        try:
            admission = await team.queue.offer(entry)
        except UpdateDrainActive as exc:
            async with self.app.db.transaction() as conn:
                refused = await no_entry_in(conn, claim.payload["attempt_id"])
            if refused:
                return EffectOutcome("deferred", canonical({"reason": "update_drain", "detail": str(exc)}))
            raise
        except SetupFailed as exc:
            # Refused before the worker had a session to enter: a known failure, not an uncertain
            # one, or the card could not be assigned again until someone reconciled it.
            return EffectOutcome("failed", str(exc))
        except Exception:
            async with self.app.db.transaction() as conn:
                refused = await no_entry_in(conn, claim.payload["attempt_id"])
            if refused:
                return EffectOutcome("failed", "the host refused this launch before runtime entry")
            raise
        finally:
            launch_attempt.reset(bound)
        if admission.state == "queued":
            return EffectOutcome("deferred", canonical({"reason": admission.reason or "capacity",
                                                       "detail": admission.detail or "waiting for worker capacity"}))
        if claim.payload.get("handoff_fingerprint"):
            await self.app.db.execute("UPDATE handoff_claims SET state = 'launched',attempt_id = ?"
                                      " WHERE reservation_id = ? AND state = 'claimed'",
                                      (claim.payload["attempt_id"], claim.id))
        return EffectOutcome("completed")

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        async with self.app.db.transaction() as conn:
            if await no_entry_in(conn, claim.payload["attempt_id"]):
                return EffectResolution("failed", {"attempt_id": claim.payload["attempt_id"], "proof": "host_no_entry"})
        attempt = await self.app.db.fetchone("SELECT id,provider_session_ref,state,staff_session_id FROM execution_attempts WHERE id = ? AND task_id = ?",
                                            (claim.payload["attempt_id"], claim.payload["task_id"]))
        if attempt is not None and attempt["provider_session_ref"]:
            return EffectResolution("completed", {"attempt_id": attempt["id"], "provider_session_ref": attempt["provider_session_ref"]})
        return None
