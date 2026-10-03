"""Persist a staff report as an exact task result before its presentation is shortened."""

from __future__ import annotations

import hashlib
import uuid
from typing import TYPE_CHECKING, Any

from daedalus.extensions.orchestrator_domain import OriginalReports, add_artifact_manifest, submit_result
from daedalus.stores.control import ControlDenied, ControlStore, Entity, Principal, Scope

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.staff_runtime import LiveSession
    from daedalus.stores.files import StoredFile


async def submit_staff_report(
    app: Application, live: LiveSession, original_note: str, artifacts: list[StoredFile],
    checks: list[dict[str, str]], call_id: str | None,
) -> dict[str, Any]:
    """Use host-authenticated session ownership; a replay returns its original receipt."""
    task_id = live.session.task_id
    if not task_id or not live.staff.project_id:
        raise ControlDenied("a staff result needs a project task")
    executions = getattr(app, "executions", None)
    if executions is None:
        raise ControlDenied("the execution owner is unavailable")
    db = app.db
    row = await db.fetchone(
        "SELECT a.id,a.contract_revision,a.actor_id,a.grant_id,a.grant_generation,a.state,t.project_id"
        " FROM execution_attempts a JOIN board_tasks t ON t.current_attempt_id = a.id"
        " WHERE a.staff_session_id = ? AND a.task_id = ?", (live.id, task_id))
    if (row is None or row["project_id"] != live.staff.project_id or
            row["actor_id"] != f"staff:{live.staff.id}" or not row["grant_id"] or row["grant_generation"] is None):
        raise ControlDenied("the staff session has no current host-attested attempt")
    principal = Principal(row["actor_id"], "agent", row["grant_id"], row["grant_generation"])
    original = original_note.encode("utf-8")
    original_digest = hashlib.sha256(original).hexdigest()
    operation_id = "staff-report:" + hashlib.sha256(
        f"{row['id']}:{call_id or original_digest}".encode()).hexdigest()
    scope = Scope("project", row["project_id"])
    control = ControlStore(db)
    replay_revision = None
    if row["state"] in ("queued", "starting", "running", "waiting"):
        async with db.transaction() as conn:
            await control.authorize(conn, principal, scope, "result.submit", task_id=task_id)
            current = await executions.check_staff(conn, live.id)
            if current.id != row["id"] or current.principal != principal:
                raise ControlDenied("the staff attempt changed before the result was staged")
    else:
        replay = await db.fetchone("SELECT o.request_entity_revision,r.original_digest"
                                   " FROM operation_receipts o JOIN result_receipts r"
                                   " ON r.id = json_extract(o.response_json, '$.result_id')"
                                   " WHERE o.scope_kind = 'project' AND o.scope_id = ? AND o.actor_id = ?"
                                   " AND o.grant_id = ? AND o.operation_kind = 'result.submit'"
                                   " AND o.client_operation_id = ?",
                                   (row["project_id"], principal.actor_id, principal.grant_id, operation_id))
        if (row["state"] != "completed" or replay is None or replay["request_entity_revision"] is None or
                replay["original_digest"] != original_digest):
            raise ControlDenied("the staff attempt is no longer active")
        async with db.transaction() as conn:
            await control.authorize(conn, principal, scope, "result.submit", task_id=task_id)
        replay_revision = int(replay["request_entity_revision"])
    inline, blob_ref, original_digest, size = OriginalReports(db.path.parent / "result-originals").stage(original)
    report_file = await app.manager.files.add(
        original, name=f"result-{operation_id[-12:]}.txt", mime="text/plain", origin="staff",
        origin_ref=live.id, scope=row["project_id"], actor=principal.actor_id)
    files = list({file.id: file for file in [report_file, *artifacts]}.values())
    await app.manager.files.attach_to_task(task_id, files, actor=principal.actor_id)
    revision = replay_revision if replay_revision is not None else await control.revision(scope, Entity("task", task_id))
    payload = {"task_id": task_id, "staff_session_id": live.id, "attempt_id": row["id"],
               "contract_revision": row["contract_revision"], "original_digest": original_digest,
               "original_size_bytes": size, "file_ids": [file.id for file in files], "checks": checks}

    async def write(conn: Any, mutation: Any) -> dict[str, Any]:
        identity = await executions.check_staff(conn, live.id)
        if (identity.id != row["id"] or identity.contract_revision != row["contract_revision"] or
                identity.principal != principal):
            raise ControlDenied("the staff result lost its execution authority")
        manifests = []
        for file in files:
            manifest_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{mutation.object_id}:{file.id}").hex
            await add_artifact_manifest(
                conn, manifest_id=manifest_id, project_id=row["project_id"], task_id=task_id,
                artifact_kind="document" if file.id == report_file.id else "other",
                artifact_key=f"result:{mutation.object_id}:{file.id}", artifact_revision=1,
                digest=file.sha256, size_bytes=file.size, file_id=file.id,
                provenance={"kind": "staff_report", "staff_session_id": live.id, "attempt_id": identity.id})
            manifests.append(manifest_id)
        result = await submit_result(
            conn, result_id=mutation.object_id, task_id=task_id, attempt_id=identity.id,
            contract_revision=identity.contract_revision, outcome="complete", original_text=inline,
            original_blob_ref=blob_ref, original_digest=original_digest, original_size_bytes=size,
            actor_id=principal.actor_id, manifest_ids=manifests, checks=checks, limitations=[])
        await executions.complete(conn, identity, outcome="complete")
        return result

    return await control.mutate(principal, scope, "result.submit", operation_id, revision,
                                Entity("task", task_id), payload, write)


__all__ = ["submit_staff_report"]
