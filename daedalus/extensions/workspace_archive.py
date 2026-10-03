"""Private, checksummed project archives with an inactive restore into a new project.

An archive carries authored history and attachment bytes, never authority or a live process. Import
creates new identities so an old session, grant, or worker cannot resume by accident.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from daedalus.extensions.workspace_files import (
    FOLDER_ID,
    WorkspaceFileRefused,
    capture_selected,
    stage_selected,
    validate_selected,
)
from daedalus.host.engine_factory import TENANT
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Principal, Scope, canonical, one
from daedalus.stores.database import Database
from daedalus.stores.files import FILES_TENANT, FileStore

FORMAT_VERSION = 2
MAX_ARCHIVE_BYTES = 256 << 20
MAX_ENTRY_BYTES = 64 << 20
MAX_ROWS = 100_000
MAX_ENTRIES = 20_000
SHA256 = re.compile(r"[0-9a-f]{64}\Z")

# Every query is scoped through the project, its tasks, sessions, or results. The list is deliberately
# explicit: a new credential or runtime table must be reviewed before it could enter an archive.
# Staff context packets stay out: they pin an old launch, and restored staff sessions cannot resume it.
QUERIES: dict[str, str] = {
    "projects": "SELECT * FROM projects WHERE id = ?",
    "project_briefs": "SELECT * FROM project_briefs WHERE project_id = ? ORDER BY section",
    "project_goal_revisions": "SELECT * FROM project_goal_revisions WHERE project_id = ? ORDER BY goal_revision",
    "project_journal": "SELECT * FROM project_journal WHERE project_id = ? ORDER BY id",
    "sessions": "SELECT * FROM sessions WHERE project_id = ? ORDER BY created_at,id",
    "session_messages": "SELECT * FROM session_messages WHERE session_id IN (SELECT id FROM sessions WHERE project_id = ?) ORDER BY seq",
    "runs": "SELECT r.* FROM runs r JOIN sessions s ON s.id = r.session_id WHERE s.project_id = ? ORDER BY r.created_at,r.id",
    "events": "SELECT e.* FROM events e JOIN runs r ON r.id = e.run_id JOIN sessions s ON s.id = r.session_id WHERE s.project_id = ? ORDER BY e.seq",
    "staff": "SELECT * FROM staff WHERE project_id = ? ORDER BY created_at,id",
    "staff_sessions": "SELECT ss.* FROM staff_sessions ss JOIN staff s ON s.id = ss.staff_id WHERE s.project_id = ? ORDER BY ss.started_at,ss.id",
    "staff_messages": "SELECT sm.* FROM staff_messages sm JOIN staff s ON s.id = sm.staff_id WHERE s.project_id = ? ORDER BY sm.created_at,sm.id",
    "staff_role_versions": "SELECT v.* FROM staff_role_versions v JOIN staff s ON s.id = v.staff_id WHERE s.project_id = ? ORDER BY v.staff_id,v.role_revision",
    "dispatches": "SELECT * FROM dispatches WHERE project_id = ? ORDER BY seq",
    "dispatch_messages": "SELECT m.* FROM dispatch_messages m JOIN dispatches d ON d.id = m.dispatch_id WHERE d.project_id = ? ORDER BY m.id",
    "asks": "SELECT * FROM asks WHERE project_id = ? ORDER BY created_at,id",
    "open_loops": "SELECT * FROM open_loops WHERE project_id = ? ORDER BY opened_at,id",
    "board_tasks": "SELECT * FROM board_tasks WHERE project_id = ? ORDER BY created_at,id",
    "task_contract_versions": "SELECT c.* FROM task_contract_versions c JOIN board_tasks t ON t.id = c.task_id WHERE t.project_id = ? ORDER BY c.task_id,c.contract_revision",
    "task_requirements": "SELECT r.* FROM task_requirements r JOIN board_tasks t ON t.id = r.task_id WHERE t.project_id = ? ORDER BY r.task_id,r.number",
    "requirement_deliveries": "SELECT d.* FROM requirement_deliveries d JOIN task_requirements r ON r.id = d.requirement_id WHERE r.project_id = ? ORDER BY d.requirement_id,d.staff_session_id",
    "task_dependency_edges": "SELECT e.* FROM task_dependency_edges e JOIN board_tasks t ON t.id = e.successor_task_id WHERE t.project_id = ? ORDER BY e.id",
    "workflow_steps": "SELECT s.* FROM workflow_steps s JOIN board_tasks t ON t.id = s.task_id WHERE t.project_id = ? ORDER BY s.task_id,s.id",
    "workflow_edges": "SELECT e.* FROM workflow_edges e JOIN board_tasks t ON t.id = e.task_id WHERE t.project_id = ? ORDER BY e.task_id,e.source_step_id,e.target_step_id",
    "board_workflow_runs": "SELECT * FROM board_workflow_runs WHERE project_id = ? ORDER BY created_at,id",
    "board_workflow_steps": "SELECT s.* FROM board_workflow_steps s JOIN board_workflow_runs r ON r.id = s.run_id WHERE r.project_id = ? ORDER BY s.run_id,s.node_id",
    "artifact_manifests": "SELECT a.* FROM artifact_manifests a WHERE a.project_id = ? OR a.task_id IN (SELECT id FROM board_tasks WHERE project_id = ?) ORDER BY a.id",
    "result_receipts": "SELECT r.* FROM result_receipts r JOIN board_tasks t ON t.id = r.task_id WHERE t.project_id = ? ORDER BY r.created_at,r.id",
    "result_artifacts": "SELECT ra.* FROM result_artifacts ra JOIN result_receipts r ON r.id = ra.result_id JOIN board_tasks t ON t.id = r.task_id WHERE t.project_id = ? ORDER BY ra.result_id,ra.manifest_id",
    "result_turn_anchors": "SELECT a.* FROM result_turn_anchors a JOIN result_receipts r ON r.id = a.result_id JOIN board_tasks t ON t.id = r.task_id WHERE t.project_id = ? ORDER BY a.result_id,a.session_id,a.turn_seq",
    "review_evidence": "SELECT v.* FROM review_evidence v JOIN result_receipts r ON r.id = v.result_id JOIN board_tasks t ON t.id = r.task_id WHERE t.project_id = ? ORDER BY v.id",
    "review_verdicts": "SELECT v.* FROM review_verdicts v JOIN result_receipts r ON r.id = v.result_id JOIN board_tasks t ON t.id = r.task_id WHERE t.project_id = ? ORDER BY v.id",
    "review_returns": "SELECT v.* FROM review_returns v JOIN board_tasks t ON t.id = v.task_id WHERE t.project_id = ? ORDER BY v.id",
    "review_comments": "SELECT v.* FROM review_comments v JOIN result_receipts r ON r.id = v.result_id JOIN board_tasks t ON t.id = r.task_id WHERE t.project_id = ? ORDER BY v.id",
    "review_comment_resolutions": "SELECT v.* FROM review_comment_resolutions v JOIN result_receipts r ON r.id = v.result_id JOIN board_tasks t ON t.id = r.task_id WHERE t.project_id = ? ORDER BY v.id",
    "knowledge_fact_versions": "SELECT * FROM knowledge_fact_versions WHERE project_id = ? ORDER BY fact_id,version",
    "knowledge_reviews": "SELECT v.* FROM knowledge_reviews v JOIN knowledge_fact_versions f ON f.fact_id = v.fact_id AND f.version = v.fact_version WHERE f.project_id = ? ORDER BY v.id",
    "knowledge_dependencies": "SELECT v.* FROM knowledge_dependencies v JOIN knowledge_fact_versions f ON f.fact_id = v.fact_id AND f.version = v.fact_version WHERE f.project_id = ? ORDER BY v.fact_id,v.fact_version",
    "files": "SELECT DISTINCT f.* FROM files f JOIN file_access a ON a.file_id = f.id WHERE a.scope = ? ORDER BY f.id",
    "task_files": "SELECT tf.* FROM task_files tf JOIN board_tasks t ON t.id = tf.task_id WHERE t.project_id = ? ORDER BY tf.task_id,tf.file_id",
    "compaction_captures": "SELECT * FROM compaction_captures WHERE project_id = ? ORDER BY created_at,id",
    "watches": "SELECT * FROM watches WHERE project_id = ? ORDER BY created_at,id",
    "watch_deliveries": "SELECT d.* FROM watch_deliveries d JOIN watches w ON w.id = d.watch_id WHERE w.project_id = ? ORDER BY d.created_at,d.id",
    "planning_budgets": "SELECT * FROM planning_budgets WHERE project_id = ? ORDER BY project_id",
    "file_transfers": "SELECT * FROM file_transfers WHERE scope = ? ORDER BY id",
    "app_events": "SELECT * FROM app_events WHERE project_id = ? ORDER BY seq",
    "replan_fingerprints": "SELECT * FROM replan_fingerprints WHERE project_id = ? ORDER BY contract_revision",
    "scope_impacts": "SELECT * FROM scope_impacts WHERE project_id = ? ORDER BY created_at,id",
}

ARCHIVED_ONLY = frozenset({"app_events", "replan_fingerprints", "scope_impacts", "board_workflow_runs", "board_workflow_steps"})

# These columns would carry local locations, resumable authority, or mutable runtime state. They are
# omitted rather than masked inside an otherwise plausible live object.
DROP_COLUMNS: dict[str, set[str]] = {
    "projects": {"settings", "system", "setup_by"},
    "sessions": {"metadata", "share_mode", "share_slug", "share_key"},
    "staff": {"default_folder_id", "agent", "model", "permission_mode", "env", "isolation", "created_by", "authority_json"},
    "staff_sessions": {"terminal_id", "cli_session_id", "transcript_ref", "folder_id", "worktree_path", "branch", "base_ref", "team_token_hash", "launch_cwd", "usage_json"},
    "requirement_deliveries": {"path"},
    "board_tasks": {"session_id", "run_id", "origin_session_id", "folder_id", "branch", "current_attempt_id", "heartbeat_at"},
    "open_loops": {"attempt_id"},
    "task_dependency_edges": {"waiver_receipt_id"},
    "result_receipts": {"attempt_id"},
    "review_verdicts": {"self_review_waiver_receipt_id"},
    "files": {"origin_ref"},
}

RESTORE_ORDER = (
    "sessions", "runs", "events", "staff", "staff_role_versions", "board_tasks", "staff_sessions", "session_messages", "staff_messages",
    "dispatches", "dispatch_messages", "asks", "open_loops",
    "project_briefs", "project_goal_revisions", "project_journal", "files", "task_files", "file_transfers",
    "task_contract_versions", "task_requirements", "requirement_deliveries", "workflow_steps", "workflow_edges",
    "artifact_manifests", "result_receipts", "result_artifacts", "result_turn_anchors", "review_evidence",
    "review_verdicts", "review_returns", "review_comments", "review_comment_resolutions",
    "task_dependency_edges", "knowledge_fact_versions", "knowledge_reviews", "knowledge_dependencies",
    "compaction_captures", "watches", "watch_deliveries", "planning_budgets",
)

ID_TABLES = {
    "sessions", "runs", "events", "staff", "board_tasks", "staff_sessions", "staff_messages",
    "dispatches", "asks", "open_loops", "files", "compaction_captures", "watches", "watch_deliveries",
    "task_requirements", "workflow_steps", "artifact_manifests", "result_receipts",
    "review_evidence", "review_verdicts", "review_returns", "review_comments",
    "review_comment_resolutions", "task_dependency_edges",
}

REFERENCES = {
    "project_id": "projects", "session_id": "sessions", "staff_id": "staff",
    "staff_session_id": "staff_sessions", "task_id": "board_tasks", "predecessor_task_id": "board_tasks",
    "successor_task_id": "board_tasks", "file_id": "files", "original_artifact_file_id": "files",
    "artifact_manifest_id": "artifact_manifests", "manifest_id": "artifact_manifests",
    "result_id": "result_receipts", "required_result_id": "result_receipts",
    "accepted_result_id": "result_receipts", "verdict_id": "review_verdicts",
    "comment_id": "review_comments", "replaces": "task_requirements",
    "assignee_staff_id": "staff", "predecessor_id": "staff_sessions",
    "fact_id": "knowledge_fact_versions",
    "run_id": "runs", "dispatch_id": "dispatches",
    "requirement_id": "task_requirements", "watch_id": "watches",
}


class ArchiveRefused(ValueError):
    """A private archive is incomplete, corrupt, or would restore ambiguous state."""


@dataclass(frozen=True)
class CheckedArchive:
    digest: str
    source_project_id: str
    rows: dict[str, list[dict[str, Any]]]
    blobs: dict[str, bytes]
    report_blobs: dict[str, bytes]
    run_blobs: dict[str, bytes]
    session_blobs: dict[str, bytes]
    counts: dict[str, int]
    config_handles: dict[str, Any]
    folder_handles: list[dict[str, Any]]
    selected_files: list[dict[str, Any]]
    workspace_blobs: dict[str, bytes]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _archive_bytes(rows: dict[str, list[dict[str, Any]]], blobs: dict[str, bytes], reports: dict[str, bytes],
                   config_handles: dict[str, Any] | None = None, run_blobs: dict[str, bytes] | None = None,
                   folder_handles: list[dict[str, Any]] | None = None,
                   session_blobs: dict[str, bytes] | None = None,
                   selected_files: list[dict[str, Any]] | None = None,
                   workspace_blobs: dict[str, bytes] | None = None) -> bytes:
    payload = canonical({"format": FORMAT_VERSION, "privacy": "operator-private", "source_project_id": rows["projects"][0]["id"],
                         "config_handles": config_handles or {}, "folder_handles": folder_handles or [],
                         "selected_files": selected_files or [], "rows": rows}).encode()
    entries = {"workspace.json": payload, **{f"files/{key}": value for key, value in blobs.items()},
               **{f"reports/{key}": value for key, value in reports.items()},
               **{f"run-details/{key}": value for key, value in (run_blobs or {}).items()},
               **{f"session-blobs/{key}": value for key, value in (session_blobs or {}).items()},
               **{f"workspace-files/{key}": value for key, value in (workspace_blobs or {}).items()}}
    manifest = {name: {"sha256": _sha(value), "size": len(value)} for name, value in entries.items()}
    entries["manifest.json"] = canonical({"format": FORMAT_VERSION, "entries": manifest}).encode()
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=False) as archive:
        for name, value in sorted(entries.items()):
            archive.writestr(name, value)
    return target.getvalue()


def check_archive(data: bytes) -> CheckedArchive:
    if not data or len(data) > MAX_ARCHIVE_BYTES:
        raise ArchiveRefused("archive exceeds the private import limit")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(infos) > MAX_ENTRIES or len(names) != len(set(names)) or "manifest.json" not in names:
                raise ArchiveRefused("archive has duplicate or excessive entries")
            if any(info.file_size > MAX_ENTRY_BYTES or info.filename.startswith("/") or ".." in info.filename.split("/") for info in infos):
                raise ArchiveRefused("archive contains an oversized or unsafe entry")
            if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
                raise ArchiveRefused("archive expands beyond the private import limit")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), dict):
                raise ArchiveRefused("archive manifest is invalid")
            if manifest.get("format") != FORMAT_VERSION or set(manifest.get("entries", {})) != set(names) - {"manifest.json"}:
                raise ArchiveRefused("archive manifest is incomplete or unsupported")
            entries = {}
            for name, expected in manifest["entries"].items():
                if name != "workspace.json" and not (re.fullmatch(r"(?:files|reports|run-details|session-blobs)/[0-9a-f]{64}", name)
                                                     or name.startswith("workspace-files/")):
                    raise ArchiveRefused("archive contains an unexpected entry")
                if not isinstance(expected, dict) or set(expected) != {"sha256", "size"}:
                    raise ArchiveRefused("archive entry checksum is invalid")
                value = archive.read(name)
                if len(value) != expected.get("size") or _sha(value) != expected.get("sha256"):
                    raise ArchiveRefused("archive checksum mismatch")
                entries[name] = value
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        if isinstance(exc, ArchiveRefused):
            raise
        raise ArchiveRefused("archive is corrupt") from exc
    try:
        payload = json.loads(entries["workspace.json"])
        rows = payload["rows"]
        if payload["format"] != FORMAT_VERSION or payload["privacy"] != "operator-private" or set(rows) != set(QUERIES):
            raise ArchiveRefused("archive format or table inventory changed")
        if len(rows["projects"]) != 1 or payload["source_project_id"] != rows["projects"][0]["id"]:
            raise ArchiveRefused("archive project identity is inconsistent")
        config = payload.get("config_handles", {})
        if not isinstance(config, dict) or set(config) - {"snapshots", "orchestrator_model", "orchestrator_autonomy", "orchestrator_concurrency_cap"}:
            raise ArchiveRefused("archive contains unreviewed configuration")
        if any(not isinstance(value, (str, int, bool)) for value in config.values()):
            raise ArchiveRefused("archive contains invalid configuration handles")
        if not isinstance(config.get("snapshots", False), bool) or not isinstance(config.get("orchestrator_model", ""), str):
            raise ArchiveRefused("archive contains invalid configuration handles")
        if config.get("orchestrator_autonomy", "normal") not in ("ask", "normal", "full"):
            raise ArchiveRefused("archive contains invalid orchestration policy")
        cap = config.get("orchestrator_concurrency_cap", 10)
        if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 100:
            raise ArchiveRefused("archive contains invalid concurrency cap")
        folders = payload.get("folder_handles", [])
        if not isinstance(folders, list) or len(folders) > 100 or any(
            not isinstance(folder, dict) or set(folder) != {"id", "label", "env", "readonly", "is_git", "position"}
            or not isinstance(folder["id"], str) or FOLDER_ID.fullmatch(folder["id"]) is None
            or not isinstance(folder["label"], str)
            or folder["env"] not in ("host", "container")
            or not isinstance(folder["readonly"], bool) or not isinstance(folder["is_git"], bool)
            or not isinstance(folder["position"], int)
            for folder in folders
        ) or len({folder["id"] for folder in folders}) != len(folders):
            raise ArchiveRefused("archive contains invalid folder handles")
        selected_files = payload.get("selected_files", [])
        workspace_blobs = {name[len("workspace-files/"):]: value for name, value in entries.items()
                           if name.startswith("workspace-files/")}
        try:
            validate_selected(selected_files, workspace_blobs, {folder["id"] for folder in folders})
        except WorkspaceFileRefused as exc:
            raise ArchiveRefused(str(exc)) from exc
        if any(not isinstance(items, list) or any(
            not isinstance(item, dict)
            or set(item) & DROP_COLUMNS.get(table, set())
            or any(not isinstance(key, str) or re.fullmatch(r"[a-z][a-z0-9_]*", key) is None for key in item)
            for item in items) for table, items in rows.items()):
            raise ArchiveRefused("archive contains forbidden fields")
        if sum(map(len, rows.values())) > MAX_ROWS:
            raise ArchiveRefused("archive has too many rows")
        blobs = {name[6:]: value for name, value in entries.items() if name.startswith("files/")}
        reports = {name[8:]: value for name, value in entries.items() if name.startswith("reports/")}
        run_blobs = {name[12:]: value for name, value in entries.items() if name.startswith("run-details/")}
        session_blobs = {name[14:]: value for name, value in entries.items() if name.startswith("session-blobs/")}
        for item in rows["files"]:
            digest = item["sha256"]
            if digest not in blobs or _sha(blobs[digest]) != digest or len(blobs[digest]) != item["size"]:
                raise ArchiveRefused("an attachment is missing or changed")
        for item in rows["result_receipts"]:
            ref = item.get("original_blob_ref")
            if ref and (ref not in reports or _sha(reports[ref]) != item["original_digest"]):
                raise ArchiveRefused("a report original is missing or changed")
        for item in rows["runs"]:
            ref = item.get("detail_blob_ref")
            if re.fullmatch(r"[a-z0-9_-]{1,64}", str(item.get("tenant_id", ""))) is None:
                raise ArchiveRefused("run tenant is invalid")
            if ref and (ref not in run_blobs or _sha(run_blobs[ref]) != ref):
                raise ArchiveRefused("a run detail is missing or changed")
        for item in [*rows["session_messages"], *rows["events"]]:
            field = "message" if "message" in item else "payload"
            for ref in _embedded_blob_refs(item[field]):
                if ref not in session_blobs or _sha(session_blobs[ref]) != ref:
                    raise ArchiveRefused("a transcript blob is missing or changed")
        required_files = {item["sha256"] for item in rows["files"]}
        required_reports = {item["original_blob_ref"] for item in rows["result_receipts"] if item.get("original_blob_ref")}
        required_runs = {item["detail_blob_ref"] for item in rows["runs"] if item.get("detail_blob_ref")}
        required_sessions = set().union(*(
            _embedded_blob_refs(item["message"] if "message" in item else item["payload"])
            for item in [*rows["session_messages"], *rows["events"]]
        )) if rows["session_messages"] or rows["events"] else set()
        if (set(blobs), set(reports), set(run_blobs), set(session_blobs)) != (
            required_files, required_reports, required_runs, required_sessions
        ):
            raise ArchiveRefused("archive contains unreferenced blob entries")
        return CheckedArchive(_sha(data), payload["source_project_id"], rows, blobs, reports, run_blobs, session_blobs,
                              {name: len(items) for name, items in rows.items()}, config, folders,
                              selected_files, workspace_blobs)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ArchiveRefused):
            raise
        raise ArchiveRefused("archive content is invalid") from exc


def _stage(root: Path, digest: str, content: bytes) -> bool:
    if not SHA256.fullmatch(digest) or _sha(content) != digest:
        raise ArchiveRefused("staged bytes fail their checksum")
    root.mkdir(parents=True, exist_ok=True)
    target = root / digest
    if target.exists():
        if _sha(target.read_bytes()) != digest:
            raise ArchiveRefused("an existing blob has changed")
        return False
    descriptor, temporary = tempfile.mkstemp(prefix=".import-", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def _embedded_blob_refs(raw: str) -> set[str]:
    """Find structured content-addressed refs, excluding incidental hashes in authored prose."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return set()
    found: set[str] = set()
    stack = [value]
    visited = 0
    while stack:
        visited += 1
        if visited > MAX_ROWS:
            raise ArchiveRefused("transcript structure is too large")
        item = stack.pop()
        if isinstance(item, dict):
            ref = item.get("blob_ref")
            if isinstance(ref, str) and SHA256.fullmatch(ref):
                found.add(ref)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _historical_capabilities(rows: list[dict[str, Any]]) -> list[str]:
    requested: set[str] = set()
    for row in rows:
        try:
            definition = json.loads(row["definition"])
            nodes = definition.get("nodes", []) if isinstance(definition, dict) else []
            for node in nodes:
                for name in node.get("capabilities", []) if isinstance(node, dict) else []:
                    if isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_.:-]{0,119}", name):
                        requested.add(name)
        except (TypeError, ValueError, AttributeError):
            continue
    return sorted(requested)


class WorkspaceArchive:
    def __init__(self, db: Database, files: FileStore, *, restore_root: Path | None = None) -> None:
        self.db = db
        self.files = files
        self.control = ControlStore(db)
        self.restore_root = restore_root or db.path.parent / "restored-workspaces"

    async def export(self, project_id: str, selected_paths: list[dict[str, str]] | None = None) -> bytes:
        rows: dict[str, list[dict[str, Any]]] = {}
        config: dict[str, Any] = {}
        folders: list[dict[str, Any]] = []
        local_folders: dict[str, Path] = {}
        async with self.db.transaction() as conn:
            for table, query in QUERIES.items():
                arguments = (project_id, project_id) if table == "artifact_manifests" else (project_id,)
                cursor = await conn.execute(query, arguments)
                originals = [dict(row) for row in await cursor.fetchall()]
                if table == "projects" and originals:
                    settings = json.loads(originals[0]["settings"])
                    if not isinstance(settings, dict):
                        settings = {}
                    orchestrator = settings.get("orchestrator", {}) if isinstance(settings, dict) else {}
                    if not isinstance(orchestrator, dict):
                        orchestrator = {}
                    cap = orchestrator.get("concurrency_cap", 10)
                    cap = cap if isinstance(cap, int) and not isinstance(cap, bool) and 1 <= cap <= 100 else 10
                    autonomy = str(orchestrator.get("autonomy") or "normal")
                    config = {"snapshots": bool(settings.get("snapshots", False)),
                              "orchestrator_model": str(orchestrator.get("model") or "")[:200],
                              "orchestrator_autonomy": autonomy if autonomy in ("ask", "normal", "full") else "normal",
                              "orchestrator_concurrency_cap": cap}
                rows[table] = [{key: value for key, value in row.items() if key not in DROP_COLUMNS.get(table, set())} for row in originals]
            referenced = {item["file_id"] for item in rows["task_files"]}
            referenced.update(item["file_id"] for item in rows["task_requirements"] if item.get("file_id"))
            referenced.update(item["file_id"] for item in rows["artifact_manifests"] if item.get("file_id"))
            referenced.update(item["original_artifact_file_id"] for item in rows["result_receipts"] if item.get("original_artifact_file_id"))
            known = {item["id"] for item in rows["files"]}
            for file_id in sorted(referenced - known):
                found = await one(conn, "SELECT * FROM files WHERE id = ?", (file_id,))
                if found is None:
                    raise ArchiveRefused("a project file reference is missing")
                rows["files"].append({key: value for key, value in dict(found).items() if key not in DROP_COLUMNS["files"]})
            cursor = await conn.execute(
                "SELECT id,path,label,env,readonly,is_git,position FROM project_folders WHERE project_id = ? ORDER BY position,id",
                (project_id,),
            )
            for row in await cursor.fetchall():
                folders.append({"id": row["id"], "label": row["label"], "env": row["env"],
                                "readonly": bool(row["readonly"]), "is_git": bool(row["is_git"]),
                                "position": int(row["position"])})
                if row["env"] == self.db.local_env:
                    local_folders[row["id"]] = Path(row["path"])
        if not rows["projects"]:
            raise KeyError(project_id)
        try:
            selected_files, workspace_blobs = capture_selected(local_folders, selected_paths or [])
        except WorkspaceFileRefused as exc:
            raise ArchiveRefused(str(exc)) from exc
        blobs: dict[str, bytes] = {}
        for item in rows["files"]:
            if not await self.files.blobs.exists(FILES_TENANT, item["sha256"]):
                raise ArchiveRefused("an attachment is missing")
            content = await self.files.blobs.get(FILES_TENANT, item["sha256"])
            if len(content) != item["size"] or _sha(content) != item["sha256"]:
                raise ArchiveRefused("an attachment is missing or changed")
            blobs[item["sha256"]] = content
        reports: dict[str, bytes] = {}
        for item in rows["result_receipts"]:
            ref = item.get("original_blob_ref")
            if ref:
                if not SHA256.fullmatch(ref):
                    raise ArchiveRefused("report reference is invalid")
                data = (self.db.path.parent / "result-originals" / ref).read_bytes()
                if _sha(data) != item["original_digest"] or len(data) != item["original_size_bytes"]:
                    raise ArchiveRefused("a report original is missing or changed")
                reports[ref] = data
        run_blobs: dict[str, bytes] = {}
        for item in rows["runs"]:
            ref = item.get("detail_blob_ref")
            if ref:
                if not SHA256.fullmatch(ref) or re.fullmatch(r"[a-z0-9_-]{1,64}", item["tenant_id"]) is None:
                    raise ArchiveRefused("run detail reference is invalid")
                if not await self.files.blobs.exists(item["tenant_id"], ref):
                    raise ArchiveRefused("a run detail is missing")
                content = await self.files.blobs.get(item["tenant_id"], ref)
                if _sha(content) != ref:
                    raise ArchiveRefused("a run detail has changed")
                run_blobs[ref] = content
        session_blobs: dict[str, bytes] = {}
        for item in [*rows["session_messages"], *rows["events"]]:
            field = "message" if "message" in item else "payload"
            tenant = item["tenant_id"]
            if re.fullmatch(r"[a-z0-9_-]{1,64}", tenant) is None:
                raise ArchiveRefused("transcript tenant is invalid")
            for ref in _embedded_blob_refs(item[field]):
                if not await self.files.blobs.exists(tenant, ref):
                    raise ArchiveRefused("a transcript blob is missing")
                content = await self.files.blobs.get(tenant, ref)
                if _sha(content) != ref:
                    raise ArchiveRefused("a transcript blob has changed")
                session_blobs[ref] = content
        archive = _archive_bytes(rows, blobs, reports, config, run_blobs, folders, session_blobs,
                                 selected_files, workspace_blobs)
        check_archive(archive)
        return archive

    async def export_command(self, principal: Principal, project_id: str, *, selected_paths: list[dict[str, str]],
                             expected_entity_revision: int, client_operation_id: str) -> dict[str, Any]:
        if principal.origin_class != "operator":
            raise PermissionError("only the operator can export a private workspace")
        scope = Scope("project", project_id)
        key = (scope.kind, scope.id, principal.actor_id, "workspace.export", client_operation_id)
        prior = await self.db.fetchone(
            "SELECT response_json FROM operation_receipts WHERE scope_kind=? AND scope_id=? AND actor_id=?"
            " AND operation_kind=? AND client_operation_id=?", key,
        )
        payload = {"selected_paths": selected_paths}
        if prior is not None:
            async def replay(conn: aiosqlite.Connection, mutation: Any) -> dict[str, Any]:
                raise AssertionError("a durable export receipt must replay before its effect")
            return await self.control.mutate(principal, scope, "workspace.export", client_operation_id,
                                             expected_entity_revision, Entity("project", project_id), payload, replay)
        data = await self.export(project_id, selected_paths)
        checked = check_archive(data)
        artifact = self.files.blobs.path_of("workspace-archives", checked.digest)
        _stage(artifact.parent, checked.digest, data)

        async def effect(conn: aiosqlite.Connection, mutation: Any) -> dict[str, Any]:
            return {"project_id": project_id, "archive_artifact_id": checked.digest,
                    "archive_digest": checked.digest, "format_version": FORMAT_VERSION,
                    "size_bytes": len(data), "source_entity_revision": expected_entity_revision,
                    "row_counts": checked.counts, "selected_files": checked.selected_files,
                    "private": True}

        return await self.control.mutate(principal, scope, "workspace.export", client_operation_id,
                                         expected_entity_revision, Entity("project", project_id), payload, effect)

    async def preview(self, archive: CheckedArchive) -> dict[str, Any]:
        await self._preflight(archive)
        prior = await self.db.fetchone("SELECT project_id FROM workspace_archive_imports WHERE archive_digest = ?", (archive.digest,))
        target_id = uuid.uuid5(uuid.NAMESPACE_URL, "archive:" + archive.digest).hex
        mapping = self._identity_map(archive, target_id)
        return {"valid": prior is None, "archive_digest": archive.digest, "format_version": FORMAT_VERSION,
                "private": True, "source_project_id": archive.source_project_id,
                "row_counts": archive.counts, "collision": prior is not None,
                "config_handles": archive.config_handles,
                "folder_handles": archive.folder_handles,
                "selected_files": archive.selected_files,
                "selected_file_count": len(archive.selected_files),
                "capability_handles": _historical_capabilities(archive.rows["board_workflow_runs"]),
                "archived_only": sorted(ARCHIVED_ONLY),
                "id_map": mapping, "conflicts": ["archive already imported"] if prior else [],
                "missing_secrets": ["provider credentials", "plugin credentials", "webhook secrets"],
                "imported_project_id": prior["project_id"] if prior else None,
                "restores_runtime": False,
                "reconnect_required": ["folders", "provider and plugin credentials", "terminal sessions", "external integrations"]}

    async def replay_import(self, principal: Principal, archive_digest: str, *, expected_collection_revision: int,
                            client_operation_id: str) -> dict[str, Any] | None:
        if principal.origin_class != "operator":
            raise PermissionError("only the operator can restore a private workspace")
        if SHA256.fullmatch(archive_digest) is None:
            raise ArchiveRefused("archive identity is invalid")
        scope = Scope("global", "global")
        identity = uuid.uuid5(uuid.NAMESPACE_URL, "archive:" + archive_digest).hex
        payload = {"archive_digest": archive_digest, "target_project_id": identity}
        prior = await self.db.fetchone(
            "SELECT 1 FROM operation_receipts WHERE scope_kind='global' AND scope_id='global' AND actor_id=?"
            " AND operation_kind='workspace.import' AND client_operation_id=?",
            (principal.actor_id, client_operation_id),
        )
        if prior is None:
            return None
        async def replay(conn: aiosqlite.Connection, mutation: Any) -> dict[str, Any]:
            raise AssertionError("a durable import receipt must replay before its effect")
        return await self.control.mutate(principal, scope, "workspace.import", client_operation_id,
                                         expected_collection_revision, Entity("collection", "global"), payload, replay)

    async def import_command(self, principal: Principal, archive: CheckedArchive, *,
                             expected_collection_revision: int, client_operation_id: str) -> dict[str, Any]:
        if principal.origin_class != "operator":
            raise PermissionError("only the operator can restore a private workspace")
        scope = Scope("global", "global")
        identity = uuid.uuid5(uuid.NAMESPACE_URL, "archive:" + archive.digest).hex
        payload = {"archive_digest": archive.digest, "target_project_id": identity}
        replayed = await self.replay_import(principal, archive.digest,
                                            expected_collection_revision=expected_collection_revision,
                                            client_operation_id=client_operation_id)
        if replayed is not None:
            return replayed
        await self._preflight(archive)
        staged: list[tuple[str, str]] = []
        folder_tree: Path | None = None
        created_folder_tree = False
        async def cleanup() -> None:
            if created_folder_tree and folder_tree is not None:
                shutil.rmtree(folder_tree)
            for kind, digest in staged:
                tenant, ref = digest.split(":", 1) if kind == "run" else (FILES_TENANT, digest)
                if kind == "session":
                    tenant, ref = digest.split(":", 1)
                    in_messages = await self.db.fetchone("SELECT 1 FROM session_messages WHERE message LIKE ? LIMIT 1", (f"%{ref}%",))
                    in_events = await self.db.fetchone("SELECT 1 FROM events WHERE payload LIKE ? LIMIT 1", (f"%{ref}%",))
                    if in_messages is None and in_events is None:
                        self.files.blobs.path_of(tenant, ref).unlink(missing_ok=True)
                        self.files.blobs._paths(tenant, ref)[1].unlink(missing_ok=True)
                    continue
                table = "files" if kind == "file" else "runs" if kind == "run" else "result_receipts"
                column = "sha256" if kind == "file" else "detail_blob_ref" if kind == "run" else "original_blob_ref"
                if await self.db.fetchone(f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (ref,)) is None:
                    path = self.files.blobs.path_of(tenant, ref) if kind in ("file", "run") else self.db.path.parent / "result-originals" / ref
                    path.unlink(missing_ok=True)
                    if kind in ("file", "run"):
                        self.files.blobs._paths(tenant, ref)[1].unlink(missing_ok=True)
        try:
            folder_tree, created_folder_tree = stage_selected(
                self.restore_root, identity, archive.selected_files, archive.workspace_blobs,
                {folder["id"] for folder in archive.folder_handles},
            )
            for item in archive.rows["files"]:
                digest = item["sha256"]
                content = archive.blobs[digest]
                if _stage(self.files.blobs.path_of(FILES_TENANT, digest).parent, digest, content):
                    staged.append(("file", digest))
                _, metadata_path = self.files.blobs._paths(FILES_TENANT, digest)
                if not metadata_path.exists():
                    await self.files.blobs.put(FILES_TENANT, content, content_type=item["mime"])
                    with metadata_path.open("rb") as metadata:
                        os.fsync(metadata.fileno())
            for digest, content in archive.report_blobs.items():
                if _stage(self.db.path.parent / "result-originals", digest, content):
                    staged.append(("report", digest))
            for item in archive.rows["runs"]:
                ref = item.get("detail_blob_ref")
                if ref:
                    tenant = TENANT
                    if _stage(self.files.blobs.path_of(tenant, ref).parent, ref, archive.run_blobs[ref]):
                        staged.append(("run", tenant + ":" + ref))
                    _, metadata_path = self.files.blobs._paths(tenant, ref)
                    if not metadata_path.exists():
                        await self.files.blobs.put(tenant, archive.run_blobs[ref])
                        with metadata_path.open("rb") as metadata:
                            os.fsync(metadata.fileno())
            for ref, content in archive.session_blobs.items():
                if _stage(self.files.blobs.path_of(TENANT, ref).parent, ref, content):
                    staged.append(("session", TENANT + ":" + ref))
                _, metadata_path = self.files.blobs._paths(TENANT, ref)
                if not metadata_path.exists():
                    await self.files.blobs.put(TENANT, content)
                    with metadata_path.open("rb") as metadata:
                        os.fsync(metadata.fileno())
        except BaseException as exc:
            await cleanup()
            if isinstance(exc, WorkspaceFileRefused):
                raise ArchiveRefused(str(exc)) from exc
            raise

        async def effect(conn: aiosqlite.Connection, mutation: Any) -> dict[str, Any]:
            if await one(conn, "SELECT 1 FROM workspace_archive_imports WHERE archive_digest = ?", (archive.digest,)):
                raise ControlConflict("this archive was already imported")
            if await one(conn, "SELECT 1 FROM projects WHERE id = ?", (identity,)):
                raise ControlConflict("archive target identity collides with an existing project")
            id_map = self._identity_map(archive, identity)
            project = archive.rows["projects"][0]
            await conn.execute(
                "INSERT INTO projects(id,name,created_at,settings,system,setup_by,entity_revision,goal_revision) VALUES (?,?,?,?,?,?,?,?)",
                (identity, project["name"] + " (restored)", _now(), canonical({
                    "snapshots": bool(archive.config_handles.get("snapshots", False)),
                    "orchestrator": {"enabled": False, "model": archive.config_handles.get("orchestrator_model", ""),
                                     "autonomy": archive.config_handles.get("orchestrator_autonomy", "normal"),
                                     "concurrency_cap": archive.config_handles.get("orchestrator_concurrency_cap", 10)},
                }), "", "archive", 1, project["goal_revision"]),
            )
            for folder in archive.folder_handles:
                await conn.execute(
                    "INSERT INTO project_folders(id,project_id,path,label,env,is_git,readonly,position,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid5(uuid.NAMESPACE_URL, archive.digest + ":folder:" + folder["id"]).hex,
                     identity, str(folder_tree / folder["id"]), folder["label"], self.db.local_env,
                     0, int(folder["readonly"]), folder["position"], _now()),
                )
            turn_map: dict[tuple[str, int], int] = {}
            for table in RESTORE_ORDER:
                for source in archive.rows[table]:
                    row = self._remap_row(table, source, id_map, archive.digest)
                    if table == "result_turn_anchors":
                        source_key = (source["session_id"], int(source["turn_seq"]))
                        if source_key not in turn_map:
                            raise ArchiveRefused("result evidence names a missing source turn")
                        row["turn_seq"] = turn_map[source_key]
                    columns = tuple(row)
                    cursor = await conn.execute(
                        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        tuple(row.values()),
                    )
                    if table == "session_messages":
                        turn_map[(source["session_id"], int(source["seq"]))] = int(cursor.lastrowid)
            for source in archive.rows["files"]:
                await conn.execute("INSERT INTO file_access(file_id,scope,added_at,added_by) VALUES (?,?,?,?)",
                                   (id_map["files"][source["id"]], identity, _now(), principal.actor_id))
            # The history keeps an acceptance reference, but no imported task is a running process.
            for source in archive.rows["board_tasks"]:
                accepted = source.get("accepted_result_id")
                if accepted:
                    await conn.execute("UPDATE board_tasks SET accepted_result_id = ? WHERE id = ?",
                                       (id_map["result_receipts"][accepted], id_map["board_tasks"][source["id"]]))
            await conn.execute(
                "INSERT INTO workspace_archive_imports(archive_digest,format_version,source_project_id,project_id,row_counts_json,imported_at,actor_id,operation_receipt_id) VALUES (?,?,?,?,?,?,?,?)",
                (archive.digest, FORMAT_VERSION, archive.source_project_id, identity,
                 canonical(archive.counts), _now(), principal.actor_id, mutation.receipt_id),
            )
            return {"project_id": identity, "archive_digest": archive.digest,
                    "restored_rows": {name: count for name, count in archive.counts.items() if name not in ARCHIVED_ONLY},
                    "archived_only": {name: archive.counts[name] for name in ARCHIVED_ONLY},
                    "runtime_state": "inactive", "private": True}

        try:
            return await self.control.mutate(principal, scope, "workspace.import", client_operation_id,
                                             expected_collection_revision, Entity("collection", "global"), payload, effect)
        except BaseException:
            await cleanup()
            raise

    async def _preflight(self, archive: CheckedArchive) -> None:
        mapping = self._identity_map(archive, uuid.uuid5(uuid.NAMESPACE_URL, "archive:" + archive.digest).hex)
        turns = {(item["session_id"], int(item["seq"])) for item in archive.rows["session_messages"]}
        if any((item["session_id"], int(item["turn_seq"])) not in turns for item in archive.rows["result_turn_anchors"]):
            raise ArchiveRefused("result evidence names a missing source turn")
        for table, items in archive.rows.items():
            columns = {row["name"] for row in await self.db.fetchall(f"PRAGMA table_info({table})")}
            if not columns:
                raise ArchiveRefused("archive targets an unavailable schema")
            for item in items:
                if not set(item) <= columns:
                    raise ArchiveRefused("archive contains fields outside the reviewed schema")
                if table not in ARCHIVED_ONLY and table != "projects":
                    self._remap_row(table, item, mapping, archive.digest)

    @staticmethod
    def _identity_map(archive: CheckedArchive, project_id: str) -> dict[str, dict[Any, Any]]:
        mapping: dict[str, dict[Any, Any]] = {"projects": {archive.source_project_id: project_id}}
        for table in ID_TABLES:
            suffix = 12 if table == "files" else 32
            if table == "workflow_steps":
                mapping[table] = {
                    (row["task_id"], row["id"]):
                        uuid.uuid5(uuid.NAMESPACE_URL,
                                   f"{archive.digest}:{table}:{row['task_id']}:{row['id']}").hex[:suffix]
                    for row in archive.rows[table]
                }
            else:
                mapping[table] = {row["id"]: uuid.uuid5(uuid.NAMESPACE_URL, f"{archive.digest}:{table}:{row['id']}").hex[:suffix]
                                  for row in archive.rows[table]}
            if len(mapping[table]) != len(archive.rows[table]) or len(set(mapping[table].values())) != len(mapping[table]):
                raise ArchiveRefused("archive has duplicate row identities")
        mapping["knowledge_fact_versions"] = {
            row["fact_id"]: uuid.uuid5(uuid.NAMESPACE_URL, f"{archive.digest}:fact:{row['fact_id']}").hex
            for row in archive.rows["knowledge_fact_versions"]
        }
        return mapping

    @staticmethod
    def _remap_row(table: str, source: dict[str, Any], mapping: dict[str, dict[Any, Any]], archive_digest: str) -> dict[str, Any]:
        row = dict(source)
        if table == "workflow_steps":
            row["id"] = mapping[table][(source["task_id"], source["id"])]
        elif table in ID_TABLES:
            row["id"] = mapping[table][source["id"]]
        if table == "workflow_edges":
            for field in ("source_step_id", "target_step_id"):
                key = (source["task_id"], source[field])
                if key not in mapping["workflow_steps"]:
                    raise ArchiveRefused("workflow edge leaves its task or names a missing step")
                row[field] = mapping["workflow_steps"][key]
        if table == "project_journal":
            row.pop("id", None)
        if table == "session_messages":
            row.pop("seq", None)
            row["message"] = re.sub(r"\batt:([0-9a-f]{12})\b", lambda m: "att:" + mapping["files"].get(m[1], m[1]), row["message"])
        if table == "sessions":
            row.update(tenant_id=TENANT, metadata="{}", share_mode="local", share_slug=None, share_key=None)
        if table == "runs":
            row["tenant_id"] = TENANT
            if row["status"] in ("queued", "running", "paused"):
                row["status"] = "incomplete"
        if table == "events":
            row.pop("seq", None)
            row["tenant_id"] = TENANT
        if table == "session_messages":
            row["tenant_id"] = TENANT
        if table == "staff":
            row.update(default_folder_id=None, agent="", model="", permission_mode="", env="", isolation="readonly", created_by="operator", archived_at=_now(), authority_json="{}")
        if table == "staff_role_versions":
            row["authority_json"] = "{}"
        if table == "staff_sessions":
            row.update(terminal_id=None, cli_session_id=None, transcript_ref=None, folder_id=None,
                       worktree_path=None, branch=None, base_ref=None, team_token_hash="",
                       launch_cwd=None, usage_json="{}", status="exited", ended_at=_now(), end_reason="restored archive")
        if table == "staff_messages":
            if row["state"] != "acknowledged":
                row["state"] = "failed"
                row["error"] = "restored historical message; delivery was not resumed"
        if table == "requirement_deliveries":
            row["path"] = ""
            if row["message_id"]:
                row["message_id"] = mapping["staff_messages"].get(row["message_id"], "")
        if table == "dispatches" and row["status"] == "open":
            row.update(status="cancelled", result="restored history; dispatch was not resumed", closed_at=_now())
        if table == "dispatch_messages":
            row.pop("id", None)
        if table == "asks" and row["resolved_at"] is None:
            row.update(resolved_at=_now(), resolved_by="system", resolution_json='{"restored_inactive":true}')
        if table == "open_loops":
            row["attempt_id"] = None
            if row["closed_at"] is None:
                row.update(closed_at=_now(), closed_by="system", decision="restored inactive")
        if table == "board_tasks":
            row.update(session_id=None, run_id=None, origin_session_id=None, folder_id=None,
                       branch=None, current_attempt_id=None, heartbeat_at=None, accepted_result_id=None)
            if row["status"] not in ("done", "cancelled"):
                row["status"] = "blocked"
            row["entity_revision"] = 1
            depends = json.loads(row["depends_on"])
            if not isinstance(depends, list) or any(item not in mapping["board_tasks"] for item in depends):
                raise ArchiveRefused("task dependency names a task outside the archive")
            row["depends_on"] = canonical([mapping["board_tasks"][item] for item in depends])
        if table == "task_dependency_edges":
            row["waiver_receipt_id"] = None
        if table == "result_receipts":
            row["attempt_id"] = None
        if table == "review_verdicts":
            row["self_review_waiver_receipt_id"] = None
        if table == "files":
            row["origin"] = "operator"
            row["origin_ref"] = "restored archive " + archive_digest
        if table == "file_transfers":
            row.pop("id", None)
            if row.get("file_id") not in mapping["files"]:
                row["file_id"] = None
            if row["scope"] in mapping["projects"]:
                row["scope"] = mapping["projects"][row["scope"]]
            if row["target"] in mapping["projects"]:
                row["target"] = mapping["projects"][row["target"]]
        if table == "knowledge_fact_versions":
            row["status"] = "invalidated"
            row["reason"] = "restored source requires revalidation"
        if table == "watches":
            row.update(enabled=0, state_json="{}", last_fired_at=None, fire_count=0)
        if table == "watch_deliveries":
            row.update(dedup_key=uuid.uuid5(uuid.NAMESPACE_URL, archive_digest + ":watch:" + row["dedup_key"]).hex,
                       status="delivered", receipt_id=None)
        if table == "planning_budgets":
            row["entity_revision"] = 1
        for column, target in REFERENCES.items():
            if column in row and row[column] is not None:
                if row[column] not in mapping[target]:
                    raise ArchiveRefused(f"archive has an external or missing {column} reference")
                row[column] = mapping[target][row[column]]
        if table == "task_contract_versions":
            snapshot = json.loads(row["snapshot_json"])
            for requirement in snapshot.get("requirements", []):
                if requirement.get("file_id"):
                    if requirement["file_id"] not in mapping["files"]:
                        raise ArchiveRefused("contract names a file outside the archive")
                    requirement["file_id"] = mapping["files"][requirement["file_id"]]
            dependencies = snapshot.get("depends_on", [])
            if any(item not in mapping["board_tasks"] for item in dependencies):
                raise ArchiveRefused("contract dependency names a task outside the archive")
            snapshot["depends_on"] = [mapping["board_tasks"][item] for item in dependencies]
            row["snapshot_json"] = canonical(snapshot)
        return row
