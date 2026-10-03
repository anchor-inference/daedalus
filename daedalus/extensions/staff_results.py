"""Fence every staff report to its host-owned attempt and commit its meaning once."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

from daedalus.extensions.operator_steps import normalise as normalise_steps
from daedalus.extensions.orchestrator_domain import OriginalReports, add_artifact_manifest, submit_result
from daedalus.host.events import AppEvent
from daedalus.host.worktrees import WorktreeError
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope, now
from daedalus.stores.files import FILE_MAX_BYTES, FILES_TENANT
from daedalus.stores.staff import cap_notes

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.staff_runtime import LiveSession

REPORT_KINDS = frozenset(("checkpoint", "needs_input", "stuck", "done"))


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def _one(conn: aiosqlite.Connection, sql: str, args: tuple[Any, ...]) -> aiosqlite.Row | None:
    async with conn.execute(sql, args) as cursor:
        return await cursor.fetchone()


@dataclass(frozen=True)
class _Artifact:
    name: str
    origin_ref: str
    data: bytes
    digest: str
    mime: str


class StaffReportService:
    """A replay reads the original receipt; new work writes report, evidence and event together."""

    def __init__(self, app: Application) -> None:
        self.app = app
        self.db = app.db
        self.control = ControlStore(app.db)
        self.originals = OriginalReports(app.db.path.parent / "result-originals")

    async def original(self, project_id: str, task_id: str, report_id: str) -> bytes:
        """Return the complete original bytes for an operator-authenticated scoped read."""
        row = await self.db.fetchone(
            "SELECT r.original_text,r.original_blob_ref,r.original_digest,r.original_size_bytes,"
            " v.original_text AS result_text,v.original_blob_ref AS result_blob"
            " FROM staff_report_records r LEFT JOIN result_receipts v ON v.id = r.result_id"
            " WHERE r.id = ? AND r.project_id = ? AND r.task_id = ?",
            (report_id, project_id, task_id))
        if row is None:
            raise KeyError(report_id)
        inline = row["result_text"] if row["result_text"] is not None else row["original_text"]
        blob_ref = row["result_blob"] if row["result_blob"] is not None else row["original_blob_ref"]
        content = inline.encode("utf-8") if inline is not None else self.originals.read(
            blob_ref, row["original_digest"])
        if (len(content) != row["original_size_bytes"] or
                hashlib.sha256(content).hexdigest() != row["original_digest"]):
            raise ValueError("the original report bytes no longer match their receipt")
        return content

    async def _artifacts(self, live: LiveSession, refs: list[str]) -> tuple[list[_Artifact], list[str]]:
        if not refs:
            return [], []
        team = self.app.extensions["staff"]
        folder, cwd = await team.cwd_of(live)
        project = await team.project(live.staff.project_id)
        check = None
        if live.staff.harness == "daedalus":
            services = self.app.manager.locator_services(live.session.session_id or "")

            def check(path: Path) -> str:
                if services is None:
                    return "the worker filesystem scope is unavailable"
                if not services.contains(path) or services.is_protected(path):
                    return "the file is outside the worker's readable scope"
                return ""

        found, notes = await team.handoff.read_artifacts(refs, env=folder.env, cwd=cwd,
                                                          project=project, check=check)
        prepared = []
        for name, path, data in found:
            if len(data) > FILE_MAX_BYTES:
                notes.append(f"{name}: file exceeds the handoff size limit")
                continue
            mime = (mimetypes.guess_type(name)[0] or "application/octet-stream").split(";")[0]
            prepared.append(_Artifact(name, path, data, hashlib.sha256(data).hexdigest(), mime))
        return prepared, notes

    async def submit(
        self, live: LiveSession, kind: str, note: str, *, artifacts: list[str] | None = None,
        remember: str | None = None, evidence: list[dict[str, str]] | None = None,
        acknowledged: list[str] | None = None, operator_steps: dict[str, Any] | None = None,
        call_id: str,
    ) -> tuple[dict[str, Any], AppEvent | None]:
        if kind not in REPORT_KINDS or not isinstance(note, str) or not note.strip():
            raise ValueError("a report needs a supported kind and nonempty note")
        if not call_id or len(call_id) > 160:
            raise ControlDenied("a staff report needs a trusted call id")
        if not live.session.task_id or not live.staff.project_id:
            raise ControlDenied("a staff report needs a project task")
        if (len(artifacts or []) > 20 or len(evidence or []) > 40 or
                len(acknowledged or []) > 40):
            raise ValueError("report attachments, evidence, or acknowledgements exceed their limits")
        if any(not isinstance(item, str) or not item.strip() for item in artifacts or []):
            raise ValueError("report artifact references must be nonempty strings")
        if any(not isinstance(item, dict) for item in evidence or []):
            raise ValueError("report evidence entries must be objects")
        if any(not isinstance(item, str) or not item.strip() for item in acknowledged or []):
            raise ValueError("report acknowledgements must be nonempty strings")
        refs = [item.strip() for item in artifacts or []]
        known_evidence = [dict(item) for item in evidence or []]
        acknowledgements = [item.strip() for item in acknowledged or []]
        steps = normalise_steps(operator_steps) if operator_steps else None
        original = note.encode("utf-8")
        if len(original) > 4 * 1024 * 1024:
            raise ValueError("direct report exceeds 4 MiB; upload it as a scoped artifact")
        original_digest = hashlib.sha256(original).hexdigest()
        request = {"kind": kind, "note": note, "artifacts": refs, "remember": remember or "",
                   "evidence": known_evidence, "acknowledged": acknowledgements,
                   "operator_steps": operator_steps or {}}
        request_digest = hashlib.sha256(_canonical(request).encode("utf-8")).hexdigest()
        executions = self.app.executions
        task_id = live.session.task_id
        row = await self.db.fetchone(
            "SELECT a.id,a.contract_revision,a.host_generation,a.actor_id,a.grant_id,a.grant_generation,"
            " a.state,a.staff_session_id,t.project_id,t.current_attempt_id,t.contract_revision AS current_contract,"
            " s.staff_id,s.task_id AS session_task FROM execution_attempts a"
            " JOIN board_tasks t ON t.id = a.task_id JOIN staff_sessions s ON s.id = a.staff_session_id"
            " WHERE a.staff_session_id = ? AND a.task_id = ?", (live.id, task_id))
        if (row is None or row["project_id"] != live.staff.project_id or
                row["staff_id"] != live.staff.id or row["session_task"] != task_id or
                row["current_attempt_id"] != row["id"] or
                row["current_contract"] != row["contract_revision"] or
                row["actor_id"] != f"staff:{live.staff.id}" or
                not row["grant_id"] or row["grant_generation"] is None):
            raise ControlDenied("the staff session has no current host-attested attempt")
        principal = Principal(row["actor_id"], "agent", row["grant_id"], row["grant_generation"])
        scope = Scope("project", row["project_id"])
        operation = "result.submit" if kind == "done" else "staff.report"
        operation_id = "staff-report:" + hashlib.sha256(f"{row['id']}:{call_id}".encode()).hexdigest()
        payload = {"task_id": task_id, "attempt_id": row["id"], "host_generation": row["host_generation"],
                   "contract_revision": row["contract_revision"], "staff_session_id": live.id,
                   "request_digest": request_digest, "original_digest": original_digest,
                   "original_size_bytes": len(original)}
        saved_revision = None
        async with self.db.transaction() as conn:
            await self.control.authorize(conn, principal, scope, operation, task_id=task_id)
            prior = await _one(conn, "SELECT kind FROM staff_report_records"
                               " WHERE attempt_id = ? AND client_call_id = ?", (row["id"], call_id))
            if prior is not None and prior["kind"] != kind:
                raise ControlConflict("the staff call id belongs to a different report kind")
            saved = await _one(conn, "SELECT request_entity_revision FROM operation_receipts"
                               " WHERE scope_kind = 'project' AND scope_id = ? AND actor_id = ?"
                               " AND operation_kind = ? AND client_operation_id = ?",
                               (scope.id, principal.actor_id, operation, operation_id))
            if saved is not None:
                if saved["request_entity_revision"] is None:
                    raise ControlDenied("the saved report lacks a replay revision")
                saved_revision = int(saved["request_entity_revision"])
            else:
                identity = await executions.check_staff(conn, live.id, operation=operation)
                if (identity.id != row["id"] or identity.host_generation != row["host_generation"] or
                        identity.contract_revision != row["contract_revision"] or
                        identity.principal != principal):
                    raise ControlDenied("the staff attempt changed before the report was staged")
        if saved_revision is not None:
            async def replay_effect(conn: aiosqlite.Connection, mutation: Any) -> dict[str, Any]:
                raise ControlDenied("a saved report was not replayed")

            response = await self.control.mutate(principal, scope, operation, operation_id,
                                                 saved_revision, Entity("task", task_id), payload, replay_effect)
            return response, None
        if kind == "done":
            worktree = await self.app.extensions["staff"].worktree_of(live.session)
            if worktree is not None:
                try:
                    status = await self.app.extensions["staff"].worktrees.status(worktree)
                except WorktreeError as exc:
                    raise RuntimeError(f"the worktree could not be checked: {exc}") from exc
                if status.dirty:
                    raise ValueError(f"{worktree.path} has uncommitted changes; commit them on "
                                     f"{worktree.branch} and report again")
        inline, blob_ref, _, size = self.originals.stage(original)
        staged, not_kept = await self._artifacts(live, refs)
        if steps is not None:
            body = steps.markdown(member=live.staff.name, task=task_id).encode("utf-8")
            name = f"operator-steps-{task_id}-{hashlib.sha256(body).hexdigest()[:12]}.md"
            staged.append(_Artifact(name, "operator_steps", body, hashlib.sha256(body).hexdigest(),
                                    "text/markdown"))
        blobs: FileBlobStore = self.app.manager.files.blobs
        for item in staged:
            await blobs.put(FILES_TENANT, item.data, content_type=item.mime)
        revision = await self.control.revision(scope, Entity("task", task_id))
        event: AppEvent | None = None

        async def effect(conn: aiosqlite.Connection, mutation: Any) -> dict[str, Any]:
            nonlocal event
            identity = await executions.check_staff(conn, live.id, operation=operation)
            if (identity.id != row["id"] or identity.host_generation != row["host_generation"] or
                    identity.contract_revision != row["contract_revision"] or identity.principal != principal):
                raise ControlDenied("the staff report lost its execution authority")
            confirmed, unknown = await self._acknowledge(conn, task_id, live.id,
                                                         identity.contract_revision, acknowledgements)
            checks, unproven, stray = await self._evidence(conn, task_id,
                                                           identity.contract_revision, known_evidence)
            if kind == "done":
                await self._check_inputs(conn, task_id, live.staff.id,
                                         identity.contract_revision, cli=live.staff.harness != "daedalus")
            if remember and remember.strip():
                await self._remember(conn, live.staff.id, remember.strip())
            file_refs = []
            manifest_ids = []
            for index, item in enumerate(staged):
                file_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{mutation.object_id}:{index}:{item.digest}").hex[:12]
                await conn.execute("INSERT INTO files(id,name,mime,size,sha256,origin,origin_ref,created_at)"
                                   " VALUES (?,?,?,?,?,'staff',?,?)",
                                   (file_id, item.name, item.mime, len(item.data), item.digest,
                                    item.origin_ref[:500], now()))
                await conn.execute("INSERT INTO file_access(file_id,scope,added_at,added_by) VALUES (?,?,?,?)",
                                   (file_id, scope.id, now(), principal.actor_id))
                await conn.execute("INSERT INTO task_files(task_id,file_id,added_at,added_by) VALUES (?,?,?,?)",
                                   (task_id, file_id, now(), principal.actor_id))
                await conn.execute("INSERT INTO file_transfers(at,file_id,action,actor,scope,target,size,sha256)"
                                   " VALUES (?,?,'fetched',?,?,?,?,?)",
                                   (now(), file_id, principal.actor_id, scope.id, item.origin_ref[:500],
                                    len(item.data), item.digest))
                file_refs.append({"id": file_id, "name": item.name, "mime": item.mime, "size": len(item.data)})
                manifest_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{mutation.object_id}:manifest:{index}").hex
                await add_artifact_manifest(conn, manifest_id=manifest_id, project_id=scope.id,
                                            task_id=task_id, artifact_kind="document" if item.mime.startswith("text/") else "other",
                                            artifact_key=f"report:{mutation.object_id}:{index}",
                                            artifact_revision=1, digest=item.digest, size_bytes=len(item.data),
                                            file_id=file_id, provenance={"kind": "staff_report", "attempt_id": identity.id})
                manifest_ids.append(manifest_id)
            result_id = None
            if kind == "done":
                original_manifest_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{mutation.object_id}:original").hex
                await add_artifact_manifest(conn, manifest_id=original_manifest_id, project_id=scope.id,
                                            task_id=task_id, artifact_kind="document",
                                            artifact_key=f"report:{mutation.object_id}:original", artifact_revision=1,
                                            digest=original_digest, size_bytes=size,
                                            provenance={"kind": "report_original", "attempt_id": identity.id,
                                                        "original_blob_ref": blob_ref, "inline": inline is not None})
                manifest_ids.insert(0, original_manifest_id)
                await submit_result(conn, result_id=mutation.object_id, task_id=task_id,
                                    attempt_id=identity.id, contract_revision=identity.contract_revision,
                                    outcome="complete", original_text=inline, original_blob_ref=blob_ref,
                                    original_digest=original_digest, original_size_bytes=size,
                                    actor_id=principal.actor_id, manifest_ids=manifest_ids,
                                    checks=checks, limitations=[])
                await conn.execute("UPDATE board_tasks SET status = 'review',"
                                   " merge_state = CASE WHEN branch IS NOT NULL AND branch != '' THEN 'proposed'"
                                   " ELSE merge_state END WHERE id = ?", (task_id,))
                await executions.complete(conn, identity, outcome="complete")
                result_id = mutation.object_id
            elif kind in ("needs_input", "stuck"):
                await conn.execute("UPDATE execution_attempts SET state = 'waiting',updated_at = ?"
                                   " WHERE id = ?", (now(), identity.id))
            event_payload: dict[str, Any] = {"kind": kind,
                "text": note if len(original) <= 6000 else note[:5000] + "\n[full report in report receipt]",
                "actor": "staff", "task_id": task_id, "call_id": call_id,
                "report_id": mutation.object_id, "original_digest": original_digest}
            if refs:
                event_payload["refs"] = refs
            if file_refs:
                event_payload["files"] = file_refs
            if confirmed:
                event_payload["acknowledged"] = confirmed
            if checks:
                event_payload["evidence"] = checks
            if unproven:
                event_payload["unproven"] = unproven
            if steps is not None:
                step_file = next((item for item in file_refs if item["name"].startswith("operator-steps-")), None)
                event_payload["operator_steps"] = {**steps.view(), "file": step_file}
            event = await self.app.manager.bus.persist_in(conn, "staff.report", event_payload,
                                                          project_id=scope.id, session_id=live.session_id,
                                                          staff_id=live.staff.id)
            await conn.execute("INSERT INTO staff_report_records(id,operation_receipt_id,project_id,task_id,"
                               " attempt_id,staff_session_id,client_call_id,kind,request_digest,request_meta_json,"
                               " original_text,original_blob_ref,original_digest,original_size_bytes,result_id,"
                               " event_seq,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (mutation.object_id, mutation.receipt_id, scope.id, task_id, identity.id, live.id,
                                call_id, kind, request_digest,
                                _canonical({key: value for key, value in request.items() if key != "note"}),
                                None if kind == "done" else inline,
                                None if kind == "done" else blob_ref,
                                original_digest, size, result_id, event.seq, now()))
            return {"report_id": mutation.object_id, "result_id": result_id, "task_id": task_id,
                    "kind": kind, "event_seq": event.seq, "confirmed": confirmed, "unknown": unknown,
                    "unproven": unproven, "stray_evidence": stray, "kept_files": file_refs,
                    "not_kept": not_kept, "original_digest": original_digest,
                    "original_size_bytes": size}

        async with self.app.manager.bus.transaction_guard():
            response = await self.control.mutate(principal, scope, operation, operation_id, revision,
                                                 Entity("task", task_id), payload, effect)
            if event is not None:
                self.app.manager.bus.announce_committed(event)
        return response, event

    async def _acknowledge(self, conn: aiosqlite.Connection, task_id: str, staff_session_id: str,
                           contract_revision: int, refs: list[str]) -> tuple[list[str], list[str]]:
        confirmed = []
        unknown = []
        for ref in dict.fromkeys(refs):
            label = ref.upper().removeprefix("R")
            requirement = await _one(conn, "SELECT id,number FROM task_requirements WHERE task_id = ?"
                                     " AND state = 'active' AND (id = ? OR number = ?)",
                                     (task_id, ref, int(label) if label.isdigit() else -1))
            if requirement is None:
                unknown.append(ref)
                continue
            delivery = await _one(conn, "SELECT 1 FROM requirement_deliveries WHERE requirement_id = ?"
                                  " AND staff_session_id = ? AND contract_revision = ?",
                                  (requirement["id"], staff_session_id, contract_revision))
            if delivery is None:
                unknown.append(ref)
                continue
            await conn.execute("UPDATE requirement_deliveries SET acknowledged_at = COALESCE(acknowledged_at, ?)"
                               " WHERE requirement_id = ? AND staff_session_id = ? AND contract_revision = ?",
                               (now(), requirement["id"], staff_session_id, contract_revision))
            confirmed.append(f"R{requirement['number']}")
        return confirmed, unknown

    async def _evidence(self, conn: aiosqlite.Connection, task_id: str, contract_revision: int,
                        entries: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[str], list[str]]:
        row = await _one(conn, "SELECT snapshot_json FROM task_contract_versions WHERE task_id = ?"
                         " AND contract_revision = ?", (task_id, contract_revision))
        if row is None:
            raise ControlDenied("the immutable contract snapshot is missing")
        snapshot = json.loads(row["snapshot_json"])
        checklist = {item["id"] for item in snapshot.get("checklist", [])}
        async with conn.execute("SELECT number,kind FROM task_requirements WHERE task_id = ?"
                                " AND state = 'active'", (task_id,)) as cursor:
            requirement_rows = await cursor.fetchall()
        requirements = {f"R{item['number']}" for item in requirement_rows}
        required_proof = checklist | {f"R{item['number']}" for item in requirement_rows
                                      if item["kind"] != "input"}
        known = checklist | requirements
        checks = []
        stray = []
        for entry in entries:
            identifier = str(entry.get("item") or entry.get("check") or "").strip()
            if identifier not in known:
                stray.append(identifier[:60] or "(unnamed)")
                continue
            checks.append({"item": identifier, "how": str(entry.get("how") or "")[:500],
                           "result": str(entry.get("result") or "")[:500]})
        provided = {entry["item"] for entry in checks}
        return checks, sorted(required_proof - provided), stray

    async def _check_inputs(self, conn: aiosqlite.Connection, task_id: str, staff_id: str,
                            contract_revision: int, *, cli: bool) -> None:
        rows = []
        async with conn.execute("SELECT r.number,r.text,d.path,d.acknowledged_at,d.opened_at"
                                " FROM task_requirements r LEFT JOIN requirement_deliveries d"
                                " ON d.requirement_id = r.id AND d.staff_id = ? AND d.contract_revision = ?"
                                " WHERE r.task_id = ? AND r.state = 'active' AND r.kind = 'input'",
                                (staff_id, contract_revision, task_id)) as cursor:
            rows = await cursor.fetchall()
        grouped: dict[int, list[aiosqlite.Row]] = {}
        for row in rows:
            grouped.setdefault(row["number"], []).append(row)
        missing = [f"R{number}" for number, deliveries in grouped.items()
                   if not any(item["acknowledged_at"] if cli else item["opened_at"] for item in deliveries)]
        if missing:
            raise ValueError("task input was not opened or confirmed: " + ", ".join(missing))

    async def _remember(self, conn: aiosqlite.Connection, staff_id: str, text: str) -> None:
        row = await _one(conn, "SELECT notes FROM staff WHERE id = ?", (staff_id,))
        if row is None:
            raise ControlDenied("staff member was removed")
        limit = self.app.manager.staff._config().notes_max_chars
        notes = cap_notes(f"{row['notes']}\n{text}" if row["notes"] else text, limit)
        await conn.execute("UPDATE staff SET notes = ? WHERE id = ?", (notes, staff_id))


__all__ = ["StaffReportService"]
