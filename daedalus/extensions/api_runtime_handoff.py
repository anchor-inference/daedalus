"""Operator-reviewed continuation on another runtime and its scoped original report."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.orchestrator_domain import OriginalReports
from daedalus.extensions.runtime_handoff import preview
from daedalus.extensions.task_launch import queue_launch
from daedalus.stores.control import ControlConflict, ControlDenied, Principal, digest, one
from daedalus.stores.runtime_release import attempt_released_in

if TYPE_CHECKING:
    from daedalus.app import Application


class ContinueBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_attempt_id: str = Field(min_length=1, max_length=128)
    target_staff_id: str = Field(min_length=1, max_length=128)
    preview_digest: str = Field(min_length=64, max_length=64)
    expected_entity_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.get("/api/board/{task_id}/handoff-options")
    async def handoff_options(task_id: str,
                              authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(authenticated)
        async with app.db.transaction() as conn:
            task = await one(conn, "SELECT id,project_id,current_attempt_id,contract_revision,entity_revision"
                             " FROM board_tasks WHERE id = ?", (task_id,))
            if task is None or not task["project_id"]:
                raise HTTPException(404, "no such project task")
            source = await one(conn, "SELECT a.id,s.staff_id,m.harness FROM execution_attempts a"
                               " JOIN staff_sessions s ON s.id = a.staff_session_id"
                               " JOIN staff m ON m.id = s.staff_id WHERE a.id = ? AND a.task_id = ?",
                               (task["current_attempt_id"], task_id)) if task["current_attempt_id"] else None
            if source is None:
                return {"task_id": task_id, "source_attempt_id": None, "targets": [],
                        "entity_revision": task["entity_revision"]}
            async with conn.execute("SELECT id,name,harness,permission_mode FROM staff"
                                    " WHERE project_id = ? AND archived_at IS NULL AND harness != ?"
                                    " ORDER BY name,id", (task["project_id"], source["harness"])) as cursor:
                targets = [dict(row) for row in await cursor.fetchall()]
            released = await attempt_released_in(conn, source["id"])
            return {"task_id": task_id, "source_attempt_id": source["id"],
                    "source_harness": source["harness"], "source_released": released,
                    "contract_revision": task["contract_revision"],
                    "entity_revision": task["entity_revision"], "targets": targets}

    @api.get("/api/board/{task_id}/handoff-preview")
    async def handoff_preview(task_id: str, source_attempt_id: str, target_staff_id: str,
                              authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(authenticated)
        try:
            return await preview(app, task_id, source_attempt_id, target_staff_id)
        except KeyError:
            raise HTTPException(404, "no such task") from None
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/api/board/{task_id}/continue-elsewhere")
    async def continue_elsewhere(task_id: str, body: ContinueBody,
                                 authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if "effects" not in app.extensions:
            raise HTTPException(503, "execution controls are unavailable")
        try:
            return await queue_launch(app, task_id, Principal.operator(authenticated),
                                      staff_id=body.target_staff_id,
                                      client_operation_id=body.client_operation_id,
                                      expected_entity_revision=body.expected_entity_revision,
                                      fallback_from_attempt_id=body.source_attempt_id,
                                      fallback_preview_digest=body.preview_digest)
        except KeyError:
            raise HTTPException(404, "no such task") from None
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ControlConflict as exc:
            raise HTTPException(409, {"reason": str(exc), "current_revision": exc.current_revision}) from exc

    @api.get("/api/team/{staff_session_id}/handoff-original")
    async def handoff_original(staff_session_id: str, request: Request) -> Response:
        team = app.extensions.get("staff")
        if team is None:
            raise HTTPException(503, "the team runtime is unavailable")
        try:
            live = await team.authenticate(staff_session_id,
                                           request.headers.get("x-daedalus-team-token", ""))
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc
        async with app.db.transaction() as conn:
            row = await one(conn, "SELECT h.packet_json,r.original_text,r.original_blob_ref,"
                            " r.original_digest,r.original_size_bytes FROM runtime_handoff_sessions l"
                            " JOIN runtime_handoffs h ON h.id = l.handoff_id"
                            " JOIN result_receipts r ON r.id = h.source_result_id"
                            " WHERE l.staff_session_id = ? AND h.task_id = ? AND h.target_staff_id = ?",
                            (staff_session_id, live.session.task_id, live.staff.id))
        if row is None:
            raise HTTPException(404, "this worker has no original handoff report")
        packet = json.loads(row["packet_json"])
        if packet["source_result"]["original_digest"] != row["original_digest"]:
            raise HTTPException(409, "the original report changed")
        if row["original_text"] is not None:
            content = row["original_text"].encode("utf-8")
        else:
            try:
                content = OriginalReports(app.db.path.parent / "result-originals").read(
                    row["original_blob_ref"], row["original_digest"])
            except (ValueError, OSError):
                raise HTTPException(409, "the original report is unavailable") from None
        if len(content) != row["original_size_bytes"] or hashlib.sha256(content).hexdigest() != row["original_digest"]:
            raise HTTPException(409, "the original report bytes changed")
        return Response(content=content, media_type="text/plain; charset=utf-8",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @api.get("/api/team/{staff_session_id}/handoff-packet")
    async def handoff_packet(staff_session_id: str, request: Request) -> Response:
        team = app.extensions.get("staff")
        if team is None:
            raise HTTPException(503, "the team runtime is unavailable")
        try:
            live = await team.authenticate(staff_session_id,
                                           request.headers.get("x-daedalus-team-token", ""))
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc
        async with app.db.transaction() as conn:
            row = await one(conn, "SELECT h.packet_json,h.packet_digest FROM runtime_handoff_sessions l"
                            " JOIN runtime_handoffs h ON h.id = l.handoff_id"
                            " WHERE l.staff_session_id = ? AND h.task_id = ? AND h.target_staff_id = ?",
                            (staff_session_id, live.session.task_id, live.staff.id))
        if row is None:
            raise HTTPException(404, "this worker has no approved handoff packet")
        packet = json.loads(row["packet_json"])
        if digest(packet) != row["packet_digest"]:
            raise HTTPException(409, "the immutable handoff packet changed")
        return Response(content=row["packet_json"], media_type="application/json",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "X-Packet-Digest": row["packet_digest"]})
