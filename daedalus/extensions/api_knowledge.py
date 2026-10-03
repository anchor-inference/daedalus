"""Operator routes for reviewed, project-scoped knowledge and bounded task context."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.stores.control import ControlConflict, ControlDenied, Principal
from daedalus.stores.knowledge import KnowledgeConflict, KnowledgeStore

if TYPE_CHECKING:
    from daedalus.app import Application


class CandidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=12, max_length=600)
    kind: Literal["decision", "preference", "identifier", "howto", "fact"] = "fact"
    source_kind: Literal["file", "manifest", "run"]
    source_id: str = Field(min_length=1, max_length=128)
    scope: Literal["project"] = "project"
    expected_collection_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    verdict: Literal["review", "promote", "invalidate", "forget", "rollback"]
    reason: str = Field(min_length=1, max_length=1000)
    expected_entity_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    store = KnowledgeStore(app.db)

    async def project_exists(project_id: str) -> None:
        row = await app.db.fetchone("SELECT 1 FROM projects WHERE id = ?", (project_id,))
        if row is None:
            raise HTTPException(404, "no such project")

    @api.get("/api/projects/{project_id}/knowledge")
    async def list_knowledge(project_id: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        await project_exists(project_id)
        return await store.list_with_freshness(project_id)

    @api.get("/api/projects/{project_id}/knowledge/{fact_id}/history")
    async def fact_history(project_id: str, fact_id: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        await project_exists(project_id)
        rows = await app.db.fetchall(
            "SELECT * FROM knowledge_fact_versions WHERE project_id = ? AND fact_id = ? ORDER BY version",
            (project_id, fact_id),
        )
        if not rows:
            raise HTTPException(404, "no such fact")
        return [dict(row) for row in rows]

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
            return await store.candidate_command(
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
            return await store.review_command(
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
    async def task_context(task_id: str, role: str = "worker", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if role not in {"worker", "reviewer", "orchestrator"}:
            raise HTTPException(400, "unsupported role")
        task = await app.db.fetchone("SELECT id, project_id, title, acceptance, depends_on FROM board_tasks WHERE id = ?", (task_id,))
        if task is None or task["project_id"] is None:
            raise HTTPException(404, "no such project task")
        facts = await store.context_facts(str(task["project_id"]))
        deps = json.loads(task["depends_on"] or "[]")
        packet = {
            "task_id": task_id, "role": role, "title": task["title"], "acceptance": task["acceptance"],
            "dependencies": deps, "facts": [
                {"fact_id": row["fact_id"], "version": row["version"], "claim": row["claim"],
                 "source": f"{row['source_kind']}:{row['source_id']}@{row['source_revision']}"}
                for row in facts
            ],
        }
        digest = hashlib.sha256(json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {**packet, "source_refs": [item["source"] for item in packet["facts"]], "packet_hash": "sha256:" + digest}
