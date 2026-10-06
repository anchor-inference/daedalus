"""A cross-runtime continuation pins prior evidence without importing a provider conversation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import aiosqlite

from daedalus.extensions.task_context import ContextUnavailable, assemble_task_context
from daedalus.harness.capabilities import capabilities
from daedalus.stores.control import ControlConflict, canonical, digest, now, one
from daedalus.stores.goal_budget import view_in
from daedalus.stores.resource_profiles import latest_in, strict_target
from daedalus.stores.runtime_release import attempt_released_in
from daedalus.stores.staff import daedalus_reaches


async def snapshot_in(conn: aiosqlite.Connection, *, task_id: str, source_attempt_id: str,
                      target_staff_id: str, context_hash: str) -> dict[str, Any]:
    """Describe the exact old attempt, target and immutable task evidence in one SQL snapshot."""
    task = await one(conn, "SELECT * FROM board_tasks WHERE id = ?", (task_id,))
    if task is None or not task["project_id"]:
        raise KeyError(task_id)
    source = await one(conn, "SELECT a.id,a.contract_revision,a.state,a.runtime_kind,a.host_generation,"
                       " a.staff_session_id,s.staff_id,s.folder_id,s.branch,s.base_ref,s.worktree_path,"
                       " s.launch_cwd,m.harness AS source_harness,m.permission_mode AS source_permission"
                       " FROM execution_attempts a JOIN staff_sessions s ON s.id = a.staff_session_id"
                       " JOIN staff m ON m.id = s.staff_id WHERE a.id = ? AND a.task_id = ?",
                       (source_attempt_id, task_id))
    if source is None or task["current_attempt_id"] != source_attempt_id:
        raise ControlConflict("the source attempt is not the task's current attempt")
    target = await one(conn, "SELECT * FROM staff WHERE id = ? AND project_id = ? AND archived_at IS NULL",
                       (target_staff_id, task["project_id"]))
    if target is None:
        raise ControlConflict("the selected worker is not active in this project")
    contract = await one(conn, "SELECT snapshot_json FROM task_contract_versions"
                         " WHERE task_id = ? AND contract_revision = ?", (task_id, task["contract_revision"]))
    if contract is None or source["contract_revision"] != task["contract_revision"]:
        raise ControlConflict("the source and target need the same current task contract")
    if target["harness"] == source["source_harness"]:
        raise ControlConflict("choose a worker using another runtime for this continuation")
    if await one(conn, "SELECT 1 FROM staff_sessions WHERE staff_id = ? AND ended_at IS NULL",
                 (target_staff_id,)):
        raise ControlConflict("release the alternate worker's current session before transferring work")
    if task["status"] not in ("todo", "blocked", "doing"):
        raise ControlConflict("return the reviewed result before continuing elsewhere")
    if await one(conn, "SELECT 1 FROM comparison_groups WHERE task_id = ?"
                 " AND state IN ('planned','active','ready')", (task_id,)):
        raise ControlConflict("close the undecided comparison before continuing elsewhere")
    released = await attempt_released_in(conn, source_attempt_id)
    latest = await one(conn, "SELECT id,outcome,original_digest,original_size_bytes,created_at"
                       " FROM result_receipts WHERE task_id = ? AND attempt_id = ?"
                       " AND contract_revision = ? ORDER BY created_at DESC,id DESC LIMIT 1",
                       (task_id, source_attempt_id, task["contract_revision"]))
    manifests = []
    async with conn.execute("SELECT id,artifact_key,artifact_revision,artifact_kind,digest,file_id"
                            " FROM artifact_manifests WHERE task_id = ? ORDER BY artifact_key,artifact_revision DESC,id",
                            (task_id,)) as cursor:
        rows = await cursor.fetchall()
    seen = set()
    for row in rows:
        if row["artifact_key"] not in seen:
            manifests.append(dict(row))
            seen.add(row["artifact_key"])
    if len(manifests) > 80:
        raise ControlConflict("too many current artifact manifests for a bounded handoff")
    async with conn.execute("SELECT id,cause,summary,contract_revision,attempt_id FROM open_loops"
                            " WHERE task_id = ? AND closed_at IS NULL ORDER BY opened_at,id LIMIT 21",
                            (task_id,)) as cursor:
        loops = [dict(row) for row in await cursor.fetchall()]
    if len(loops) > 20:
        raise ControlConflict("too many open obligations for a bounded handoff")
    folder = None
    for folder_id in (task["folder_id"], target["default_folder_id"]):
        if folder_id:
            folder = await one(conn, "SELECT id,path,env FROM project_folders"
                               " WHERE id = ? AND project_id = ?", (folder_id, task["project_id"]))
            if folder is not None:
                break
    if folder is None:
        folder = await one(conn, "SELECT id,path,env FROM project_folders"
                           " WHERE project_id = ? ORDER BY position LIMIT 1", (task["project_id"],))
    if folder is None:
        raise ControlConflict("the target worker has no selected project folder")
    if source["folder_id"] != folder["id"]:
        raise ControlConflict("the target must use the source attempt's project folder")
    budget = await view_in(conn, task["project_id"])
    # A command-line worker's spend and a lost reply's cost are unknown to the goal budget; they
    # are shown as such and no longer refuse the handoff. Only a balance that measured spend has
    # used up does.
    if (budget is not None and budget["total"]["available_usd"] is not None
            and float(budget["total"]["available_usd"]) <= 0):
        raise ControlConflict("the project goal budget is spent; raise it on the project's budget to hand this task on")
    snapshot = json.loads(contract["snapshot_json"])
    if not isinstance(snapshot, dict):
        raise ControlConflict("the current task contract is malformed")
    workspace = {"folder_id": source["folder_id"], "branch": source["branch"],
                 "base_ref": source["base_ref"],
                 "worktree_path_digest": hashlib.sha256((source["worktree_path"] or "").encode()).hexdigest()
                 if source["worktree_path"] else None,
                 "launch_cwd_digest": hashlib.sha256((source["launch_cwd"] or "").encode()).hexdigest()
                 if source["launch_cwd"] else None}
    packet = {"task_id": task_id, "project_id": task["project_id"],
              "contract_revision": task["contract_revision"], "context_hash": context_hash,
              "source_attempt_id": source_attempt_id, "source_harness": source["source_harness"],
              "source_result": dict(latest) if latest is not None else None,
              "artifacts": manifests, "criteria": snapshot.get("checklist", []),
              "requirements": snapshot.get("requirements", []),
              "open_obligations": loops,
              "workspace": workspace, "target_staff_id": target_staff_id,
              "target_harness": target["harness"], "target_permission_mode": target["permission_mode"],
              "source_permission_mode": source["source_permission"],
              "target_folder_id": folder["id"], "target_env": folder["env"],
              "history_portability": "none", "workspace_transfer": "none",
              "cost_state": "priced_at_native_inference" if budget is not None else "unpriced_or_external"}
    if len(canonical(packet).encode()) > 32768:
        raise ControlConflict("the handoff evidence exceeds its bounded context packet")
    return {"task_id": task_id, "project_id": task["project_id"],
            "source_attempt_id": source_attempt_id, "source_released": released,
            "target_staff_id": target_staff_id, "target_harness": target["harness"],
            "target_env": folder["env"], "target_folder_id": folder["id"],
            "target_member_digest": digest({field: target[field] for field in
                ("project_id", "harness", "agent", "model", "effort", "permission_mode",
                 "default_folder_id", "isolation", "instructions", "role_revision")}),
            "contract_revision": task["contract_revision"], "entity_revision": task["entity_revision"],
            "packet": packet, "packet_digest": digest(packet),
            "budget": budget, "source_state": source["state"]}


async def preview(app: Any, task_id: str, source_attempt_id: str,
                  target_staff_id: str) -> dict[str, Any]:
    try:
        context = await assemble_task_context(app.db, task_id, role="worker")
    except (KeyError, ContextUnavailable) as exc:
        raise ControlConflict(f"the current task context is unavailable: {exc}") from exc
    async with app.db.transaction() as conn:
        view = await snapshot_in(conn, task_id=task_id, source_attempt_id=source_attempt_id,
                                 target_staff_id=target_staff_id, context_hash=context["packet_hash"])
        profile = await latest_in(conn, view["project_id"])
    capability = await target_capability(app, view["target_harness"], view["target_env"], target_staff_id)
    view["capability"] = capability
    resource = {"state": "none"}
    if profile is not None and profile["state"] == "enabled":
        terminals = app.extensions.get("terminals")
        try:
            if view["target_harness"] == "daedalus":
                strict_target(profile, env=view["target_env"], harness=view["target_harness"], capability={})
            if terminals is None:
                raise ControlConflict("the terminal daemon cannot prove resource containment")
            observed = await terminals.containment_capability(view["target_env"])
            pinned = strict_target(profile, env=view["target_env"],
                                   harness=view["target_harness"], capability=observed)
            await terminals.preflight_attempt_resources(view["target_env"], limits=pinned["limits"],
                                                        daemon_instance=pinned["daemon_instance"])
            resource = {"state": "ready", **pinned}
        except (ControlConflict, OSError, RuntimeError) as exc:
            resource = {"state": "blocked", "profile_revision": profile["revision"], "reason": str(exc)}
    view["resource"] = resource
    view["preview_digest"] = digest({key: view[key] for key in
                                     ("source_attempt_id", "source_released", "target_staff_id",
                                      "target_member_digest", "contract_revision", "entity_revision",
                                      "packet_digest", "budget", "capability", "resource")})
    view["blockers"] = ([] if view["source_released"] else ["source_runtime_not_released"])
    if not capability["available"]:
        view["blockers"].append("target_runtime_unavailable")
    if resource["state"] == "blocked":
        view["blockers"].append("resource_boundary_unavailable")
    view["ready"] = not view["blockers"]
    return view


async def target_capability(app: Any, harness: str, env: str, staff_id: str) -> dict[str, Any]:
    """An unavailable catalog is unknown, never an approval to start an alternate CLI."""
    if harness == "daedalus":
        manager = getattr(app, "manager", None)
        staff = app.extensions.get("staff")
        member = await staff.member(staff_id) if staff is not None else None
        config = getattr(manager, "config", None)
        presets = getattr(config, "presets", {}) if config is not None else {}
        ready = (manager is not None and staff is not None and member is not None
                 and daedalus_reaches(env, manager.projects.local_env)
                 and (env == manager.projects.local_env or not manager.host_unreachable())
                 and bool(getattr(config, "has_model", False))
                 and (not member.model or member.model in presets))
        return {"available": ready, "harness": harness, "env": env,
                "reason": "" if ready else "native worker model or local runtime is unavailable",
                "model": member.model if member is not None else "",
                "cost": "quoted again at provider admission"}
    manager = app.extensions.get("harness")
    if manager is None:
        return {"available": False, "harness": harness, "env": env,
                "reason": "CLI capability catalog is unavailable", "cost": "unknown subscription price"}
    try:
        entry = next((item for item in await manager.harnesses(env)
                      if item.get("harness") == harness), None)
    except Exception:
        entry = None
    if entry is None:
        return {"available": False, "harness": harness, "env": env,
                "reason": "CLI capability was not observed", "cost": "unknown subscription price"}
    reason = str(entry.get("unavailable") or "")
    if not entry.get("installed") or not entry.get("adapter") or not entry.get("supported"):
        reason = reason or "CLI adapter or installed version is unsupported"
    if entry.get("version_guard") == "unverified":
        reason = reason or "the installed CLI version has no passing self-check"
    try:
        profile = capabilities(harness)
    except KeyError:
        reason = reason or "the alternate CLI has no host capability definition"
        profile = None
    if profile is not None and profile.team_tools == "none":
        reason = reason or "the alternate CLI cannot receive the scoped handoff tools"
    return {"available": not reason, "harness": harness, "env": env, "reason": reason,
            "version": str(entry.get("installed_version") or ""),
            "login": str(entry.get("logged_in") or "unknown"),
            "team_tools": profile.team_tools if profile is not None else "unknown",
            "permissions": profile.permissions if profile is not None else "unknown",
            "cost": "unknown subscription price"}


def render(packet: dict[str, Any], *, handoff_id: str, team_url: str) -> str:
    """Point to full original evidence; a bounded prompt cannot impersonate a provider transcript."""
    return ("Operator-approved continuation on a different runtime. Prior provider conversation is NOT portable. "
            "The prior workspace branch and base are provenance, not copied edits. Inspect the immutable "
            "artifact manifest references and all current requirements and criteria before working; "
            "no criterion is pre-accepted and an artifact reference alone does not prove its bytes are present. "
            f"Handoff {handoff_id} pins source attempt {packet['source_attempt_id']} and contract "
            f"revision {packet['contract_revision']}; packet SHA-256 {digest(packet)}. "
            f"Read the complete packet at {team_url}/handoff-packet with your team token. "
            f"The complete original prior report, when present, is available with your team token at {team_url}/handoff-original. "
            "Do not infer worktree changes were moved; ask the operator for an explicit workspace transfer if needed.")


async def bind_session(db: Any, handoff_id: str, staff_session_id: str) -> None:
    async with db.transaction() as conn:
        row = await one(conn, "SELECT h.task_id,h.target_staff_id,h.target_attempt_id,h.source_attempt_id,"
                        " h.contract_revision AS pinned_contract_revision,t.contract_revision,t.current_attempt_id,"
                        " s.staff_id,s.task_id AS session_task FROM runtime_handoffs h"
                        " JOIN board_tasks t ON t.id = h.task_id JOIN staff_sessions s ON s.id = ?"
                        " WHERE h.id = ?", (staff_session_id, handoff_id))
        if (row is None or row["task_id"] != row["session_task"]
                or row["target_staff_id"] != row["staff_id"]
                or row["contract_revision"] != row["pinned_contract_revision"]
                or row["current_attempt_id"] != row["source_attempt_id"]
                or not await attempt_released_in(conn, row["source_attempt_id"])):
            raise ControlConflict("the handoff cannot bind this worker session")
        await conn.execute("INSERT INTO runtime_handoff_sessions(handoff_id,staff_session_id,created_at)"
                           " VALUES (?,?,?)", (handoff_id, staff_session_id, now()))


async def packet_for_launch(db: Any, handoff_id: str, *, task_id: str,
                            target_staff_id: str) -> dict[str, Any]:
    async with db.transaction() as conn:
        row = await one(conn, "SELECT packet_json,packet_digest,task_id,target_staff_id,contract_revision,"
                        " source_attempt_id FROM runtime_handoffs WHERE id = ?", (handoff_id,))
        task = await one(conn, "SELECT contract_revision,current_attempt_id FROM board_tasks WHERE id = ?", (task_id,))
        if (row is None or task is None or row["task_id"] != task_id
                or row["target_staff_id"] != target_staff_id
                or row["contract_revision"] != task["contract_revision"]
                or task["current_attempt_id"] != row["source_attempt_id"]
                or not await attempt_released_in(conn, row["source_attempt_id"])):
            raise ControlConflict("the approved handoff no longer matches the released source")
        packet = json.loads(row["packet_json"])
        if digest(packet) != row["packet_digest"]:
            raise ControlConflict("the immutable handoff packet changed")
        budget = await view_in(conn, packet["project_id"])
        # As at approval: unknown spend and a command-line target do not stop the handoff, only a
        # goal budget that measured spend has used up.
        if (budget is not None and budget["total"]["available_usd"] is not None
                and float(budget["total"]["available_usd"]) <= 0):
            raise ControlConflict("the project goal budget is spent; raise it on the project's budget to hand this task on")
        return packet
