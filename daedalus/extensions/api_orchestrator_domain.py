"""Board-scoped contract, result, and review routes backed by authorized commands."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException

from daedalus.extensions.orchestrator_domain import (
    DomainConflict,
    OrchestratorDomain,
    OriginalReports,
    accept_result,
    add_artifact_manifest,
    add_review_comment,
    add_review_evidence,
    advance_workflow_step,
    apply_goal_revision,
    claim_handoff,
    configure_workflow,
    dependency_readiness,
    next_action_readiness,
    record_verdict,
    replace_contract,
    resolve_dependency,
    resolve_review_comment,
    return_result,
    scope_impact_preview,
    set_next_action,
    submit_result,
    unresolved_review_comments,
    update_role_profile,
    workflow_readiness,
)
from daedalus.extensions.planning import create_plan, token_readiness
from daedalus.host.events import AppEvent
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope
from daedalus.stores.outbox import OutboxStore

if TYPE_CHECKING:
    from daedalus.app import Application


def install_routes(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    """Register domain routes after the host's authentication dependency is available."""
    def store() -> ControlStore:
        return ControlStore(app.db)

    def domain() -> OrchestratorDomain:
        return OrchestratorDomain(app.db)

    async def task_scope(task_id: str) -> Scope:
        task = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
        if task is None:
            raise HTTPException(404, "no such task")
        return Scope("project", task["project_id"]) if task["project_id"] else Scope("global", "global")

    @api.get("/api/projects/{project_id}/scope-revisions")
    async def scope_preview(project_id: str, roots: str,
                            _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async with app.db.transaction() as conn:
            try:
                return await scope_impact_preview(conn, project_id, [part for part in roots.split(",") if part])
            except DomainConflict as exc:
                raise HTTPException(409, str(exc)) from exc

    @api.post("/api/projects/{project_id}/scope-revisions")
    async def scope_apply(project_id: str, body: dict[str, Any],
                          who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            principal = Principal.operator(who)
            revision = body["expected_entity_revision"]
            if isinstance(revision, bool) or not isinstance(revision, int):
                raise ValueError("expected_entity_revision must be an integer")

            async def effect(conn: Any, _: Any) -> dict[str, Any]:
                return await apply_goal_revision(conn, project_id=project_id,
                                                 expected_goal_revision=int(body["expected_goal_revision"]),
                                                 body=body["body"], root_task_ids=list(body["root_task_ids"]),
                                                 origin_kind="operator", origin_ref="",
                                                 control=store, principal=principal)

            return await store().mutate(principal, Scope("project", project_id), "goal.revise",
                                      str(body["client_operation_id"]), revision, Entity("project", project_id),
                                      body, effect)
        except KeyError as exc:
            raise HTTPException(422, f"missing field or project: {exc}") from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ControlConflict, DomainConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/api/projects/{project_id}/plans/readiness")
    async def plan_readiness(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async with app.db.transaction() as conn:
            try:
                return await token_readiness(conn, project_id)
            except DomainConflict as exc:
                raise HTTPException(409, str(exc)) from exc

    @api.post("/api/projects/{project_id}/plans")
    async def post_plan(project_id: str, body: dict[str, Any],
                        who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            revision = body["expected_collection_revision"]
            if isinstance(revision, bool) or not isinstance(revision, int):
                raise ValueError("expected_collection_revision must be an integer")
            async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
                return await create_plan(conn, mutation, project_id=project_id,
                                         tasks=body["tasks"], fanout_reason=body.get("fanout_reason", ""))
            return await store().mutate(Principal.operator(who), Scope("project", project_id),
                                      "plan.create", str(body["client_operation_id"]), revision,
                                      Entity("collection", project_id), body, effect)
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ControlConflict, DomainConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/api/staff/{staff_id}/role")
    async def staff_role(staff_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        row = await app.db.fetchone("SELECT id,project_id,role_revision,purpose,authority_json,"
                                    " output_contract_json,harness,agent,model,effort FROM staff WHERE id = ?", (staff_id,))
        if row is None:
            raise HTTPException(404, "no such member")
        return {"staff_id": row["id"], "project_id": row["project_id"], "role_revision": row["role_revision"],
                "purpose": row["purpose"], "authority": json.loads(row["authority_json"]),
                "output_contract": json.loads(row["output_contract_json"]),
                "runtime": {"harness": row["harness"], "agent": row["agent"],
                            "model": row["model"], "effort": row["effort"]}}

    @api.put("/api/staff/{staff_id}/role")
    async def put_staff_role(staff_id: str, body: dict[str, Any],
                             who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        member = await app.db.fetchone("SELECT project_id FROM staff WHERE id = ?", (staff_id,))
        if member is None:
            raise HTTPException(404, "no such member")
        try:
            principal = Principal.operator(who)

            async def effect(conn: Any, _: Any) -> dict[str, Any]:
                return await update_role_profile(conn, staff_id=staff_id, purpose=body["purpose"],
                                                 authority=body["authority"],
                                                 output_contract=body["output_contract"])

            return await store().mutate(principal, Scope("project", member["project_id"]), "staff.role.replace",
                                      str(body["client_operation_id"]), int(body["expected_entity_revision"]),
                                      Entity("project", member["project_id"]), body, effect)
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ControlConflict, DomainConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    async def mutate(task_id: str, who: dict[str, Any], body: dict[str, Any], operation: str,
                     effect: Any) -> dict[str, Any]:
        scope = await task_scope(task_id)
        try:
            command_id = str(body["client_operation_id"])
            revision = body["expected_entity_revision"]
            if isinstance(revision, bool) or not isinstance(revision, int):
                raise ValueError("expected_entity_revision must be an integer")
            principal = Principal.operator(who)
            bus = app.manager.bus
            events: list[AppEvent] = []

            async def affected(conn: Any) -> dict[str, Any]:
                cursor = await conn.execute(
                    "WITH RECURSIVE affected(id) AS (SELECT ? UNION SELECT t.id FROM board_tasks t"
                    " JOIN json_each(t.depends_on) d JOIN affected a ON d.value = a.id"
                    " WHERE t.project_id IS ?) SELECT t.id,t.title,t.status,t.project_id,t.assignee_staff_id,t.current_attempt_id"
                    " FROM board_tasks t JOIN affected a ON a.id = t.id", (task_id, scope.id if scope.kind == "project" else None),
                )
                rows = await cursor.fetchall()
                await cursor.close()
                return {row["id"]: row for row in rows}

            async def commit(conn: Any, mutation: Any) -> dict[str, Any]:
                before = await affected(conn)
                response = await effect(conn, mutation)
                for identity, task in (await affected(conn)).items():
                    prior = before.get(identity)
                    changed = prior is not None and prior["status"] != task["status"]
                    if identity != task_id and not changed:
                        continue
                    payload = {"task_id": identity, "title": task["title"], "actor": principal.origin_class,
                               "actor_id": principal.actor_id}
                    events.append(await bus.persist_in(conn, "task.changed", payload,
                                                       project_id=task["project_id"], staff_id=task["assignee_staff_id"]))
                    if changed:
                        events.append(await bus.persist_in(conn, "task.moved", {**payload, "from": prior["status"],
                                                           "to": task["status"]}, project_id=task["project_id"],
                                                           staff_id=task["assignee_staff_id"]))
                    if identity == task_id and operation == 'result.accept' and response.get('acceptance_state') == 'operator_approved':
                        decision = {**payload, 'result_id': response['result_id'], 'verdict_id': response['verdict_id'],
                                    'contract_revision': response['contract_revision'], 'attempt_id': task['current_attempt_id'],
                                    'receipt_id': mutation.receipt_id}
                        events.append(await bus.persist_in(conn, 'task.accepted', decision,
                                                           project_id=task['project_id'], staff_id=task['assignee_staff_id']))
                return response

            async with bus.transaction_guard():
                response = await store().mutate(principal, scope, operation, command_id,
                                               revision, Entity("task", task_id), body, commit)
                for event in events:
                    bus.announce_committed(event)
            return response
        except KeyError as exc:
            if str(exc).strip("'") in ("client_operation_id", "expected_entity_revision"):
                raise HTTPException(422, "client_operation_id and expected_entity_revision are required") from exc
            raise HTTPException(404, "linked record was not found") from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ControlConflict as exc:
            raise HTTPException(409, {"reason": str(exc), "current_revision": exc.current_revision}) from exc
        except DomainConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/api/board/{task_id}/contract")
    async def contract(task_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await domain().contract(task_id)
        except KeyError as exc:
            raise HTTPException(404, "no such task") from exc

    @api.put("/api/board/{task_id}/contract")
    async def put_contract(task_id: str, body: dict[str, Any], who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async def effect(conn: Any, _: Any) -> dict[str, Any]:
            return await replace_contract(conn, task_id=task_id, requirements=body.get("requirements", []),
                                          checks=body.get("checks", []), acceptance=body.get("acceptance", ""),
                                          brief=body.get("brief", {}), origin_kind="operator",
                                          origin_ref=str(body.get("origin_ref") or ""),
                                          change_kind=str(body.get("change_kind") or "semantic"))
        return await mutate(task_id, who, body, "contract.replace", effect)

    @api.get("/api/board/{task_id}/results")
    async def results(task_id: str, who: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        try:
            rows = await domain().results(task_id)
            actor = Principal.operator(who).actor_id
            for row in rows:
                receipt = await app.db.fetchone("SELECT actor_id FROM result_receipts WHERE id = ?", (row["result_id"],))
                row["self_review_waiver_required"] = receipt is not None and receipt["actor_id"] == actor
            return rows
        except KeyError as exc:
            raise HTTPException(404, "no such task") from exc

    @api.get("/api/board/{task_id}/results/{result_id}/original")
    async def original(task_id: str, result_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, str]:
        try:
            return {"original_text": (await domain().original(task_id, result_id)).decode("utf-8")}
        except KeyError as exc:
            raise HTTPException(404, "no such result") from exc
        except DomainConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/api/board/{task_id}/artifacts")
    async def artifact(task_id: str, body: dict[str, Any], who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not body.get("file_id"):
            raise HTTPException(422, "artifact registration needs an immutable uploaded file")
        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            cursor = await conn.execute("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
            task = await cursor.fetchone()
            await cursor.close()
            if task is None:
                raise KeyError(task_id)
            return await add_artifact_manifest(conn, manifest_id=mutation.object_id,
                                               project_id=task["project_id"],
                                               task_id=task_id, artifact_kind=body["artifact_kind"],
                                               artifact_key=body["artifact_key"],
                                               artifact_revision=int(body["artifact_revision"]),
                                               digest=body["digest"], size_bytes=int(body["size_bytes"]),
                                               file_id=body.get("file_id"), provenance=body.get("provenance"))
        return await mutate(task_id, who, body, "artifact.record", effect)

    @api.post("/api/board/{task_id}/results")
    async def post_result(task_id: str, body: dict[str, Any], who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        report = str(body.get("original_text") or "").encode("utf-8")
        try:
            original_text, blob_ref, digest, size = OriginalReports(domain().reports.root).stage(report)
        except ValueError as exc:
            raise HTTPException(413 if "4 MiB" in str(exc) else 422, str(exc)) from exc

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            return await submit_result(conn, result_id=mutation.object_id, task_id=task_id,
                                       attempt_id=body.get("attempt_id"), contract_revision=int(body["contract_revision"]),
                                       outcome=body["outcome"], original_text=original_text, original_blob_ref=blob_ref,
                                       original_digest=digest, original_size_bytes=size,
                                       actor_id=Principal.operator(who).actor_id,
                                       manifest_ids=list(body.get("manifest_ids") or []),
                                       checks=list(body.get("checks") or []),
                                       limitations=list(body.get("limitations") or []))
        return await mutate(task_id, who, body, "result.submit", effect)

    @api.get("/api/board/{task_id}/dependencies")
    async def dependencies(task_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async with app.db.transaction() as conn:
            try:
                return await dependency_readiness(conn, task_id)
            except KeyError as exc:
                raise HTTPException(404, "no such task") from exc

    @api.post("/api/board/{task_id}/dependencies/{edge_id}/resolve")
    async def resolve_edge(task_id: str, edge_id: str, body: dict[str, Any],
                           who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            edge = await conn.execute("SELECT successor_task_id FROM task_dependency_edges WHERE id = ?", (edge_id,))
            row = await edge.fetchone()
            await edge.close()
            if row is None or row["successor_task_id"] != task_id:
                raise KeyError(edge_id)
            return await resolve_dependency(conn, edge_id=edge_id, resolution=body["resolution"],
                                            result_id=body.get("result_id"), artifact_digest=body.get("artifact_digest"),
                                            waiver_receipt_id=mutation.receipt_id if body["resolution"] == "waived" else None)
        return await mutate(task_id, who, body, "dependency.resolve", effect)

    @api.post("/api/board/{task_id}/results/{result_id}/verdicts")
    async def verdict(task_id: str, result_id: str, body: dict[str, Any],
                      who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            cursor = await conn.execute("SELECT task_id FROM result_receipts WHERE id = ?", (result_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None or row["task_id"] != task_id:
                raise KeyError(result_id)
            return await record_verdict(conn, verdict_id=mutation.object_id, result_id=result_id,
                                        reviewer_actor_id=Principal.operator(who).actor_id,
                                        verification=body["verification"], accepted=bool(body["accepted"]),
                                        head=body.get("head"), base=body.get("base"),
                                        environment_digest=body.get("environment_digest"),
                                        evidence_ids=list(body.get("evidence_ids") or []), reason=str(body.get("reason") or ""),
                                        self_review_waiver_receipt_id=mutation.receipt_id if body.get("self_review_waiver") is True else None)
        return await mutate(task_id, who, body, "review.verdict", effect)

    @api.post("/api/board/{task_id}/results/{result_id}/evidence")
    async def attest_evidence(task_id: str, result_id: str, body: dict[str, Any],
                              who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            cursor = await conn.execute("SELECT m.digest,m.file_id FROM result_receipts r"
                                        " JOIN result_artifacts a ON a.result_id = r.id"
                                        " JOIN artifact_manifests m ON m.id = a.manifest_id"
                                        " WHERE r.id = ? AND r.task_id = ? AND m.id = ?",
                                        (result_id, task_id, body["manifest_id"]))
            manifest = await cursor.fetchone()
            await cursor.close()
            if manifest is None or manifest["file_id"] is None:
                raise DomainConflict("evidence needs an attached immutable file in the exact result")
            observation = str(body.get("observation") or "").strip()
            if not observation or len(observation) > 1000:
                raise ValueError("manual evidence needs an observation of at most 1000 characters")
            return await add_review_evidence(conn, evidence_id=mutation.object_id, result_id=result_id,
                                             criterion_id=body["criterion_id"],
                                             command=f"operator attestation: {observation}", exit_code=0,
                                             environment_digest=None, manifest_digest_before=manifest["digest"],
                                             manifest_digest_after=manifest["digest"])
        return await mutate(task_id, who, body, "review.evidence.attest", effect)

    @api.get("/api/board/{task_id}/results/{result_id}/evidence")
    async def evidence(task_id: str, result_id: str,
                       _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        result = await app.db.fetchone("SELECT 1 FROM result_receipts WHERE id = ? AND task_id = ?", (result_id, task_id))
        if result is None:
            raise HTTPException(404, "no such result")
        rows = await app.db.fetchall("SELECT id,criterion_id,command,exit_code,environment_digest,"
                                     " manifest_digest_before,manifest_digest_after,observed_at"
                                     " FROM review_evidence WHERE result_id = ? ORDER BY observed_at,id", (result_id,))
        return [{"evidence_id": row["id"], "criterion_id": row["criterion_id"],
                 "observation": row["command"], "exit_code": row["exit_code"],
                 "environment_digest": row["environment_digest"],
                 "manifest_digest_before": row["manifest_digest_before"],
                 "manifest_digest_after": row["manifest_digest_after"],
                 "verification": "stale" if row["manifest_digest_before"] is None or
                 row["manifest_digest_before"] != row["manifest_digest_after"] else
                 "verified" if row["exit_code"] == 0 else "failed",
                 "observed_at": row["observed_at"]} for row in rows]

    @api.post("/api/board/{task_id}/results/{result_id}/comments")
    async def comment(task_id: str, result_id: str, body: dict[str, Any],
                      who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            cursor = await conn.execute("SELECT task_id FROM result_receipts WHERE id = ?", (result_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None or row["task_id"] != task_id:
                raise KeyError(result_id)
            return await add_review_comment(conn, comment_id=mutation.object_id, result_id=result_id,
                                            author_actor_id=Principal.operator(who).actor_id,
                                            source="operator", priority=body["priority"], body=body["body"],
                                            manifest_id=body.get("manifest_id"), path=body.get("path"),
                                            head=body.get("head"), line_start=body.get("line_start"),
                                            line_end=body.get("line_end"), verdict_id=body.get("verdict_id"))
        return await mutate(task_id, who, body, "review.comment", effect)

    @api.get("/api/board/{task_id}/results/{result_id}/comments")
    async def comments(task_id: str, result_id: str,
                       _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        try:
            return await domain().comments(task_id, result_id)
        except KeyError as exc:
            raise HTTPException(404, "no such result") from exc

    @api.post("/api/board/{task_id}/results/{result_id}/comments/{comment_id}/resolve")
    async def comment_resolve(task_id: str, result_id: str, comment_id: str, body: dict[str, Any],
                              who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            cursor = await conn.execute("SELECT task_id FROM result_receipts WHERE id = ?", (result_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None or row["task_id"] != task_id:
                raise KeyError(result_id)
            return await resolve_review_comment(conn, resolution_id=mutation.object_id, comment_id=comment_id,
                                                result_id=result_id, actor_id=Principal.operator(who).actor_id,
                                                resolution=body["resolution"], reason=body["reason"])
        return await mutate(task_id, who, body, "review.comment.resolve", effect)

    @api.post("/api/board/{task_id}/handoffs")
    async def handoff(task_id: str, body: dict[str, Any], who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            return await claim_handoff(conn, claim_id=mutation.object_id, task_id=task_id,
                                       operation_id=mutation.receipt_id, reservation_id=body.get("reservation_id"))
        return await mutate(task_id, who, body, "handoff.claim", effect)

    @api.get("/api/board/{task_id}/workflow")
    async def workflow(task_id: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        async with app.db.transaction() as conn:
            try:
                return await workflow_readiness(conn, task_id)
            except KeyError as exc:
                raise HTTPException(404, "no such task") from exc

    @api.put("/api/board/{task_id}/workflow")
    async def put_workflow(task_id: str, body: dict[str, Any],
                           who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async def effect(conn: Any, _: Any) -> dict[str, Any]:
            return await configure_workflow(conn, task_id=task_id, steps=list(body["steps"]),
                                            edges=[tuple(edge) for edge in body.get("edges", [])])
        return await mutate(task_id, who, body, "workflow.configure", effect)

    @api.post("/api/board/{task_id}/workflow/{step_id}/{action}")
    async def workflow_step(task_id: str, step_id: str, action: str, body: dict[str, Any],
                            who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if action not in ("ack", "start", "complete"):
            raise HTTPException(404, "no such workflow action")
        async def effect(conn: Any, _: Any) -> dict[str, Any]:
            return await advance_workflow_step(conn, task_id=task_id, step_id=step_id, action=action)
        return await mutate(task_id, who, body, "workflow.ack" if action == "ack" else
                            f"workflow.step.{action}", effect)

    @api.get("/api/board/{task_id}/next-action")
    async def next_action(task_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any] | None:
        async with app.db.transaction() as conn:
            try:
                return await next_action_readiness(conn, task_id)
            except KeyError as exc:
                raise HTTPException(404, "no such task") from exc

    @api.get("/api/projects/{project_id}/next-actions")
    async def project_next_actions(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        project = await app.db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
        if project is None:
            raise HTTPException(404, "no such project")
        async with app.db.transaction() as conn:
            cursor = await conn.execute("SELECT id FROM board_tasks WHERE project_id = ? AND status != 'done'"
                                        " ORDER BY updated_at DESC,id", (project_id,))
            tasks = await cursor.fetchall()
            await cursor.close()
            actions = [await next_action_readiness(conn, task["id"]) for task in tasks]
        return {"project_id": project_id, "actions": [action for action in actions if action is not None]}

    @api.put("/api/board/{task_id}/next-action")
    async def put_next_action(task_id: str, body: dict[str, Any],
                              who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            return await set_next_action(conn, action_id=mutation.object_id, task_id=task_id,
                                         kind=body["kind"], owner_kind=body["owner_kind"],
                                         owner_id=body.get("owner_id"), prerequisites=list(body.get("prerequisites") or []),
                                         due_at=body.get("due_at"), context_ref=body.get("context_ref"))
        return await mutate(task_id, who, body, "next_action.replace", effect)

    @api.post("/api/board/{task_id}/results/{result_id}/accept")
    async def accept(task_id: str, result_id: str, body: dict[str, Any],
                     who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        task = await app.db.fetchone("SELECT branch FROM board_tasks WHERE id = ?", (task_id,))
        if task is None:
            raise HTTPException(404, "no such task")
        head_sha = base_sha = merge_sha = None
        if task["branch"]:
            review = getattr(app.extensions.get("staff"), "review", None)
            if review is None:
                raise HTTPException(503, "review service is unavailable")
            try:
                current = await review.review(task_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            head_sha, base_sha, merge_sha = current["head_sha"], current["base_sha"], current["current_sha"]

        async def effect(conn: Any, _: Any) -> dict[str, Any]:
            return await accept_result(conn, task_id=task_id, result_id=result_id, verdict_id=body["verdict_id"],
                                       contract_revision=int(body["contract_revision"]),
                                       current_head=head_sha, current_base=base_sha, current_merge_sha=merge_sha)
        return await mutate(task_id, who, body, "result.accept", effect)

    @api.post("/api/board/{task_id}/results/{result_id}/return")
    async def return_reviewed(task_id: str, result_id: str, body: dict[str, Any],
                              who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            return await return_result(conn, return_id=mutation.object_id, task_id=task_id,
                                       result_id=result_id, verdict_id=body["verdict_id"],
                                       contract_revision=int(body["contract_revision"]),
                                       actor_id=Principal.operator(who).actor_id, reason=body["reason"])
        return await mutate(task_id, who, body, "result.return", effect)

    @api.post("/api/board/{task_id}/results/{result_id}/merge")
    async def queue_merge(task_id: str, result_id: str, body: dict[str, Any],
                          who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        team = app.extensions.get("staff")
        review = getattr(team, "review", None)
        if review is None:
            raise HTTPException(503, "review service is unavailable")
        try:
            inspected = await review.review(task_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        if inspected["blockers"] or not inspected["head_sha"] or not inspected["base_sha"]:
            raise HTTPException(409, {"reason": "merge preflight is blocked", "blockers": inspected["blockers"]})
        principal = Principal.operator(who)
        payload = {"task_id": task_id, "result_id": result_id, "verdict_id": body["verdict_id"],
                   "head_sha": inspected["head_sha"], "base_sha": inspected["base_sha"]}

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            cursor = await conn.execute("SELECT r.task_id,r.contract_revision,r.outcome,r.attempt_id,"
                                        " v.result_id AS verdict_result,v.verification,v.accepted,v.head,v.base,"
                                        " t.contract_revision AS current_revision,t.current_attempt_id,t.status,t.merge_state"
                                        " FROM result_receipts r JOIN review_verdicts v ON v.id = ?"
                                        " JOIN board_tasks t ON t.id = r.task_id WHERE r.id = ?",
                                        (body["verdict_id"], result_id))
            pinned = await cursor.fetchone()
            await cursor.close()
            if (pinned is None or pinned["task_id"] != task_id or pinned["verdict_result"] != result_id or
                    pinned["contract_revision"] != pinned["current_revision"] or pinned["outcome"] != "complete" or
                    pinned["verification"] != "verified" or not pinned["accepted"] or pinned["status"] != "review" or
                    pinned["merge_state"] == "merged" or pinned["head"] != payload["head_sha"] or
                    pinned["base"] != payload["base_sha"] or
                    (pinned["attempt_id"] is not None and pinned["attempt_id"] != pinned["current_attempt_id"])):
                raise DomainConflict("result, verdict, attempt, or reviewed Git identity changed")
            latest = await conn.execute("SELECT id FROM result_receipts WHERE task_id = ? AND contract_revision = ?"
                                        " AND attempt_id IS ? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                                        (task_id, pinned["contract_revision"], pinned["current_attempt_id"]))
            latest_result = await latest.fetchone()
            await latest.close()
            if latest_result is None or latest_result["id"] != result_id or await unresolved_review_comments(conn, result_id):
                raise DomainConflict("a newer result or blocking review comment prevents merge")
            action_id = await OutboxStore.enqueue(conn, mutation, principal, kind="review.merge",
                                                   operation="review.merge", payload=payload,
                                                   effects=("git.merge",), task_id=task_id)
            await conn.execute("INSERT INTO task_merge_receipts(id,task_id,result_id,verdict_id,"
                               " operation_receipt_id,head_sha,base_sha,state,created_at,updated_at)"
                               " VALUES (?,?,?,?,?,?,?,'queued',datetime('now'),datetime('now'))",
                               (action_id, task_id, result_id, body["verdict_id"], mutation.receipt_id,
                                payload["head_sha"], payload["base_sha"]))
            return {"action_id": action_id, "task_id": task_id, "result_id": result_id, "state": "queued"}

        scope = await task_scope(task_id)
        try:
            response = await store().mutate(principal, scope, "review.merge", str(body["client_operation_id"]),
                                          int(body["expected_entity_revision"]), Entity("task", task_id),
                                          body, effect, effects=("git.merge",))
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ControlConflict, DomainConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        dispatcher = app.extensions.get("effects")
        if dispatcher is not None:
            dispatcher.notify()
        return response


__all__ = ["install_routes"]
