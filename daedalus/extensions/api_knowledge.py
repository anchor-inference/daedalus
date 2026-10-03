"""Operator routes for reviewed, project-scoped knowledge and bounded task context."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.task_context import ContextUnavailable, assemble_task_context
from daedalus.stores.control import ControlConflict, ControlDenied, Principal
from daedalus.stores.knowledge import KnowledgeConflict, KnowledgeStore
from daedalus.stores.result_anchors import result_turn_refs
from daedalus.stores.staff_context import staff_context_packet

if TYPE_CHECKING:
    from daedalus.app import Application


class CandidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=12, max_length=600)
    kind: Literal["decision", "preference", "identifier", "howto", "fact"] = "fact"
    source_kind: Literal["file", "manifest", "run"]
    source_id: str = Field(min_length=1, max_length=128)
    scope: Literal["project"] = "project"
    expected_collection_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, strict=True)
    verdict: Literal["review", "promote", "invalidate", "forget", "rollback"]
    reason: str = Field(min_length=1, max_length=1000)
    expected_entity_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)


class RevalidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queue_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1, strict=True)
    expected_entity_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def store() -> KnowledgeStore:
        return KnowledgeStore(app.db)

    async def project_exists(project_id: str) -> None:
        row = await app.db.fetchone("SELECT 1 FROM projects WHERE id = ?", (project_id,))
        if row is None:
            raise HTTPException(404, "no such project")

    @api.get("/api/projects/{project_id}/knowledge")
    async def list_knowledge(project_id: str, limit: int = Query(default=25, ge=1, le=100),
                             before: str | None = Query(default=None, max_length=128),
                             who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(who)
        await project_exists(project_id)
        try:
            return await store().inspection(project_id, limit=limit, before=before)
        except KnowledgeConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/api/projects/{project_id}/knowledge/sources")
    async def knowledge_sources(project_id: str, who: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        Principal.operator(who)
        await project_exists(project_id)
        rows = await app.db.fetchall(
            "SELECT DISTINCT f.id source_id,'file' source_kind,f.name label FROM files f"
            " JOIN file_access a ON a.file_id = f.id WHERE a.scope = ? ORDER BY f.created_at DESC,f.id DESC LIMIT 50",
            (project_id,),
        )
        manifests = await app.db.fetchall(
            "SELECT m.id source_id,'manifest' source_kind,m.artifact_key label FROM artifact_manifests m"
            " LEFT JOIN board_tasks t ON t.id = m.task_id WHERE COALESCE(m.project_id,t.project_id) = ?"
            " AND NOT EXISTS (SELECT 1 FROM artifact_manifests later WHERE later.artifact_key = m.artifact_key"
            " AND later.task_id IS m.task_id AND later.project_id IS m.project_id"
            " AND later.artifact_revision > m.artifact_revision) ORDER BY m.created_at DESC,m.id DESC LIMIT 50",
            (project_id,),
        )
        return [dict(row) for row in (*rows, *manifests)]

    @api.get("/api/projects/{project_id}/knowledge/{fact_id}/history")
    async def fact_history(project_id: str, fact_id: str, limit: int = Query(default=25, ge=1, le=100),
                           before_version: int | None = Query(default=None, ge=1, le=2**63 - 1),
                           _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        await project_exists(project_id)
        rows = await app.db.fetchall(
            "SELECT * FROM knowledge_fact_versions WHERE project_id = ? AND fact_id = ? AND version < ?"
            " ORDER BY version DESC LIMIT ?", (project_id, fact_id, before_version or 2**63 - 1, limit),
        )
        if not rows:
            exists = await app.db.fetchone("SELECT 1 FROM knowledge_fact_versions WHERE project_id = ? AND fact_id = ? LIMIT 1", (project_id, fact_id))
            if exists is None:
                raise HTTPException(404, "no such fact")
        return [dict(row) for row in reversed(rows)]

    @api.get("/api/projects/{project_id}/knowledge/stale")
    async def stale_knowledge(project_id: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        await project_exists(project_id)
        return await store().stale_queue(project_id)

    @api.post("/api/projects/{project_id}/knowledge/{fact_id}/revalidate")
    async def revalidate_knowledge(
        project_id: str, fact_id: str, body: RevalidateBody,
        who: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        await project_exists(project_id)
        try:
            return await store().revalidate_command(
                Principal.operator(who), project_id, fact_id, body.queue_id,
                expected_revision=body.expected_entity_revision,
                expected_version=body.expected_version,
                client_operation_id=body.client_operation_id,
            )
        except (KnowledgeConflict, ControlConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.get("/api/sessions/{session_id}/compaction-captures")
    async def compaction_captures(session_id: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        session = await app.db.fetchone("SELECT 1 FROM sessions WHERE id = ?", (session_id,))
        if session is None:
            raise HTTPException(404, "no such session")
        rows = await app.db.fetchall(
            "SELECT * FROM compaction_captures WHERE session_id = ? ORDER BY created_at DESC LIMIT 100",
            (session_id,),
        )
        return [{**dict(row), "source_refs": json.loads(row["source_refs"]),
                 "contract_refs": json.loads(row["contract_refs"]),
                 "question_refs": json.loads(row["question_refs"])} for row in rows]

    @api.post("/api/projects/{project_id}/knowledge/candidates")
    async def create_candidate(project_id: str, body: CandidateBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await project_exists(project_id)
        try:
            return await store().candidate_command(
                Principal.operator(who), project_id, expected_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id, claim=body.claim, kind=body.kind,
                source_kind=body.source_kind, source_id=body.source_id,
            )
        except (KnowledgeConflict, ControlConflict, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.post("/api/projects/{project_id}/knowledge/{fact_id}/review")
    async def review_fact(project_id: str, fact_id: str, body: ReviewBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await project_exists(project_id)
        try:
            return await store().review_command(
                Principal.operator(who), project_id, fact_id,
                expected_revision=body.expected_entity_revision, expected_version=body.expected_version,
                verdict=body.verdict, reason=body.reason, client_operation_id=body.client_operation_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "no such fact") from exc
        except (KnowledgeConflict, ControlConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.get("/api/board/{task_id}/context")
    async def task_context(task_id: str, role: str = "worker", staff_id: str | None = None,
                           _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            role_hint = ""
            if staff_id is not None:
                member = await app.db.fetchone("SELECT m.role,m.project_id AS member_project,"
                                               " t.project_id AS task_project FROM staff m"
                                               " CROSS JOIN board_tasks t WHERE m.id = ? AND t.id = ?"
                                               " AND m.archived_at IS NULL", (staff_id, task_id))
                if member is None or member["member_project"] != member["task_project"]:
                    raise HTTPException(404, "no such active member on this task")
                role_hint = member["role"]
            return await assemble_task_context(app.db, task_id, role=role, role_hint=role_hint)
        except KeyError as exc:
            raise HTTPException(404, "no such project task") from exc
        except ContextUnavailable as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.get("/api/team/{staff_session_id}/context")
    async def team_context(staff_session_id: str, request: Request) -> dict[str, Any]:
        team = app.extensions.get("staff")
        if team is None:
            raise HTTPException(503, "the team service is unavailable")
        try:
            live = await team.authenticate(staff_session_id,
                                           request.headers.get("x-daedalus-team-token", ""))
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc
        pinned = await staff_context_packet(app.db, staff_session_id)
        if pinned is None or pinned["task_id"] != live.session.task_id:
            raise HTTPException(409, "this session has no pinned task context")
        try:
            current = await assemble_task_context(app.db, pinned["task_id"], role=pinned["role"],
                                                  role_hint=pinned["role_hint"])
        except (KeyError, ContextUnavailable):
            return {"packet": pinned["packet"], "packet_hash": pinned["packet_hash"],
                    "source_current": False, "current_packet_hash": None}
        return {"packet": pinned["packet"], "packet_hash": pinned["packet_hash"],
                "source_current": current["packet_hash"] == pinned["packet_hash"],
                "current_packet_hash": current["packet_hash"]}

    @api.get("/api/board/{task_id}/context-history")
    async def context_history(task_id: str, limit: int = 25,
                              _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not 1 <= limit <= 25:
            raise HTTPException(400, "context history limit must be between 1 and 25")
        task = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
        if task is None or task["project_id"] is None:
            raise HTTPException(404, "no such project task")
        rows = await app.db.fetchall(
            "SELECT p.staff_session_id,p.role,p.role_hint,p.contract_revision,p.packet_hash,"
            " p.packet_json,p.created_at,s.staff_id,s.ended_at"
            " FROM staff_context_packets p JOIN staff_sessions s ON s.id = p.staff_session_id"
            " JOIN staff m ON m.id = s.staff_id"
            " WHERE p.task_id = ? AND s.task_id = ? AND m.project_id = ?"
            " ORDER BY p.created_at DESC,p.staff_session_id DESC LIMIT ?",
            (task_id, task_id, task["project_id"], limit),
        )
        entries = []
        for row in rows:
            packet = json.loads(row["packet_json"])
            try:
                current = await assemble_task_context(app.db, task_id, role=row["role"],
                                                      role_hint=row["role_hint"])
                current_hash = current["packet_hash"]
            except (KeyError, ContextUnavailable):
                current_hash = None
            entries.append({"staff_session_id": row["staff_session_id"], "staff_id": row["staff_id"],
                            "role": row["role"], "role_hint": row["role_hint"],
                            "contract_revision": row["contract_revision"],
                            "packet_hash": row["packet_hash"], "source_refs": packet["source_refs"],
                            "source_current": current_hash == row["packet_hash"],
                            "current_packet_hash": current_hash, "created_at": row["created_at"],
                            "session_ended_at": row["ended_at"]})
        return {"task_id": task_id, "entries": entries}

    @api.get("/api/board/{task_id}/context-history/{staff_session_id}")
    async def context_history_entry(task_id: str, staff_session_id: str,
                                    _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        row = await app.db.fetchone(
            "SELECT p.packet_json,p.packet_hash,p.role,p.role_hint,p.contract_revision,p.created_at,"
            " s.staff_id,s.ended_at FROM staff_context_packets p"
            " JOIN staff_sessions s ON s.id = p.staff_session_id"
            " JOIN staff m ON m.id = s.staff_id"
            " JOIN board_tasks t ON t.id = p.task_id"
            " WHERE p.task_id = ? AND p.staff_session_id = ? AND s.task_id = ?"
            " AND m.project_id = t.project_id AND t.project_id IS NOT NULL",
            (task_id, staff_session_id, task_id),
        )
        if row is None:
            raise HTTPException(404, "no such task context packet")
        try:
            current = await assemble_task_context(app.db, task_id, role=row["role"],
                                                  role_hint=row["role_hint"])
            current_hash = current["packet_hash"]
        except (KeyError, ContextUnavailable):
            current_hash = None
        packet = json.loads(row["packet_json"])
        return {"staff_session_id": staff_session_id, "staff_id": row["staff_id"],
                "role": row["role"], "role_hint": row["role_hint"],
                "contract_revision": row["contract_revision"], "packet_hash": row["packet_hash"],
                "source_refs": packet["source_refs"], "source_current": current_hash == row["packet_hash"],
                "current_packet_hash": current_hash, "created_at": row["created_at"],
                "session_ended_at": row["ended_at"], "packet": packet}

    @api.get("/api/board/{task_id}/results/{result_id}/turns")
    async def result_turns(task_id: str, result_id: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        try:
            return await result_turn_refs(app.db, task_id, result_id)
        except KeyError as exc:
            raise HTTPException(404, "no such result on this task") from exc
