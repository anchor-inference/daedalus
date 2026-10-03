"""Versioned contracts, immutable results, and evidence-bound review projections.

Write helpers use a caller-owned SQLite transaction so authorization, the operation receipt,
the domain rows, and the outbox event can commit together.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from daedalus.extensions.task_contract import REQUIREMENT_KINDS, check_items
from daedalus.stores.database import Database

RESULT_OUTCOMES = frozenset(("complete", "partial", "failed", "needs_input", "cancelled"))
MANIFEST_KINDS = frozenset(("code", "document", "research", "export", "media", "other"))
INLINE_REPORT_MAX = 65536
DIRECT_REPORT_MAX = 4 * 1024 * 1024


class DomainConflict(ValueError):
    """A proposed change would contradict the pinned task, result, or review state."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json(raw: Any, fallback: Any) -> Any:
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return fallback


async def _one(conn: aiosqlite.Connection, query: str, args: tuple[Any, ...]) -> aiosqlite.Row | None:
    cursor = await conn.execute(query, args)
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


async def _many(conn: aiosqlite.Connection, query: str, args: tuple[Any, ...]) -> list[aiosqlite.Row]:
    cursor = await conn.execute(query, args)
    try:
        return list(await cursor.fetchall())
    finally:
        await cursor.close()


def _requirements(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(items) > 20:
        raise ValueError("a contract can have at most 20 requirements")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        body = str(item.get("text") or "").strip()
        kind = str(item.get("kind") or "")
        if not body or len(body) > 1000 or kind not in REQUIREMENT_KINDS:
            raise ValueError("each requirement needs text of at most 1000 characters and a valid kind")
        stable_id = hashlib.sha256(f"{index}:{kind}:{body}".encode()).hexdigest()[:10]
        out.append({"id": str(item.get("id") or stable_id), "text": body,
                    "kind": kind, "source": str(item.get("source") or "operator"),
                    "file_id": item.get("file_id")})
    if len({item["id"] for item in out}) != len(out):
        raise ValueError("requirement ids must be unique")
    return out


def _checks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks = check_items(items)
    if len(checks) > 12:
        raise ValueError("a contract can have at most 12 checks")
    normalized = [{"id": str(item.get("id") or f"C{index}"), "text": str(item["text"]).strip()}
                  for index, item in enumerate(checks, 1)]
    if len({item["id"] for item in normalized}) != len(normalized):
        raise ValueError("acceptance check ids must be unique")
    return normalized


async def replace_contract(
    conn: aiosqlite.Connection, *, task_id: str, requirements: list[dict[str, Any]],
    checks: list[dict[str, Any]], acceptance: str, brief: dict[str, str],
    origin_kind: str, origin_ref: str, change_kind: str,
) -> dict[str, Any]:
    """Append a semantic version; a display-only edit keeps the contract revision."""
    task = await _one(conn, "SELECT id, project_id, contract_revision, depends_on FROM board_tasks WHERE id = ?", (task_id,))
    if task is None:
        raise KeyError(task_id)
    old = await _one(conn, "SELECT snapshot_json FROM task_contract_versions WHERE task_id = ? AND contract_revision = ?",
                     (task_id, task["contract_revision"]))
    if old is None:
        raise DomainConflict("the current contract version is missing")
    current = _json(old["snapshot_json"], {})
    snapshot = {"requirements": _requirements(requirements), "checklist": _checks(checks),
                "acceptance": acceptance.strip(), "depends_on": _json(task["depends_on"], []),
                "brief": {key: str(value).strip() for key, value in brief.items()}}
    prior = {key: current.get(key) for key in snapshot}
    changed = _canonical(snapshot) != _canonical(prior)
    if change_kind not in ("semantic", "editorial"):
        raise ValueError("change_kind must be semantic or editorial")
    if change_kind == "editorial" and changed:
        raise DomainConflict("requirements, checks, acceptance, or brief changed semantically")
    revision = int(task["contract_revision"])
    if changed:
        revision += 1
        active = await _many(conn, "SELECT id FROM task_requirements WHERE task_id = ? AND state = 'active'", (task_id,))
        if active:
            await conn.execute("UPDATE task_requirements SET state = 'superseded', updated_at = ?"
                               " WHERE task_id = ? AND state = 'active'", (_now(), task_id))
        row = await _one(conn, "SELECT COALESCE(MAX(number), 0) AS n FROM task_requirements WHERE task_id = ?", (task_id,))
        next_number = int(row["n"]) + 1 if row is not None else 1
        materialized = []
        for index, item in enumerate(snapshot["requirements"]):
            requirement_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{task_id}:{revision}:{index}:{item['id']}").hex[:20]
            await conn.execute("INSERT INTO task_requirements(id,task_id,project_id,number,text,kind,source,state,file_id,"
                               " created_at,updated_at) VALUES (?,?,?,?,?,?,?,'active',?,?,?)",
                               (requirement_id, task_id, task["project_id"] or "", next_number + index,
                                item["text"], item["kind"], item["source"], item["file_id"], _now(), _now()))
            materialized.append({**item, "id": requirement_id})
        snapshot["requirements"] = materialized
        await conn.execute(
            "INSERT INTO task_contract_versions(task_id, contract_revision, origin_kind, origin_ref, snapshot_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, revision, origin_kind, origin_ref, _canonical(snapshot), _now()),
        )
        await conn.execute("UPDATE board_tasks SET contract_revision = ?, acceptance_state = 'returned',"
                           " checklist = ?, acceptance = ?, brief_json = ? WHERE id = ?",
                           (revision, _canonical([{"text": item["text"], "done": False} for item in snapshot["checklist"]]),
                            snapshot["acceptance"], _canonical(snapshot["brief"]), task_id))
        await conn.execute("UPDATE next_actions SET state = 'cancelled' WHERE task_id = ? AND contract_revision < ? AND state = 'active'",
                           (task_id, revision))
    return {"task_id": task_id, "contract_revision": revision, "semantic_change": changed}


async def capture_contract_change(conn: aiosqlite.Connection, task_id: str, *, origin_kind: str,
                                  origin_ref: str = "") -> int:
    """Version a legacy card edit in its own transaction when its requirements or scope changed."""
    task = await _one(conn, "SELECT contract_revision, checklist, acceptance, brief_json, depends_on FROM board_tasks WHERE id = ?",
                      (task_id,))
    if task is None:
        raise KeyError(task_id)
    current = await _one(conn, "SELECT snapshot_json FROM task_contract_versions WHERE task_id = ?"
                         " AND contract_revision = ?", (task_id, task["contract_revision"]))
    if current is None:
        raise DomainConflict("the current contract version is missing")
    requirements = await _many(conn, "SELECT id, text, kind, source, file_id FROM task_requirements"
                               " WHERE task_id = ? AND state = 'active' ORDER BY number", (task_id,))
    checks = check_items(task["checklist"])
    old = _json(current["snapshot_json"], {})
    old_checks = old.get("checklist", [])
    old_by_text: dict[str, list[str]] = {}
    for old_item in old_checks:
        old_by_text.setdefault(str(old_item.get("text") or "").strip(), []).append(str(old_item["id"]))
    check_snapshot = []
    occupied = {str(item.get("id")) for item in old_checks}
    for index, item in enumerate(checks, 1):
        text = str(item["text"]).strip()
        matching = old_by_text.get(text, [])
        identifier = item.get("id") or (matching.pop(0) if matching else None)
        if identifier is None:
            identifier = f"C{index}"
            while identifier in occupied:
                identifier = f"C{int(identifier[1:]) + 1}"
        occupied.add(str(identifier))
        check_snapshot.append({"id": str(identifier), "text": text})
    check_snapshot = _checks(check_snapshot)
    snapshot = {"requirements": [dict(row) for row in requirements], "checklist": check_snapshot,
                "acceptance": task["acceptance"], "depends_on": _json(task["depends_on"], []),
                "brief": _json(task["brief_json"], {})}
    if _canonical(snapshot) == _canonical(old):
        return int(task["contract_revision"])
    revision = int(task["contract_revision"]) + 1
    await conn.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,snapshot_json,created_at)"
                       " VALUES (?,?,?,?,?,?)", (task_id, revision, origin_kind, origin_ref, _canonical(snapshot), _now()))
    await conn.execute("UPDATE board_tasks SET contract_revision = ?, acceptance_state = 'returned' WHERE id = ?",
                       (revision, task_id))
    await conn.execute("UPDATE next_actions SET state = 'cancelled' WHERE task_id = ? AND state = 'active'"
                       " AND contract_revision < ?", (task_id, revision))
    return revision


async def add_artifact_manifest(
    conn: aiosqlite.Connection, *, manifest_id: str, project_id: str | None, task_id: str | None,
    artifact_kind: str, artifact_key: str, artifact_revision: int, digest: str, size_bytes: int,
    file_id: str | None = None, provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a content identity; a live path is never a manifest's authority."""
    if project_id is None and task_id is None:
        raise ValueError("an artifact needs project or task scope")
    if artifact_kind not in MANIFEST_KINDS or artifact_revision < 1 or size_bytes < 0:
        raise ValueError("invalid artifact kind, revision, or size")
    if not artifact_key.strip() or len(artifact_key) > 500 or len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest):
        raise ValueError("artifact identity needs a key and sha256 digest")
    if task_id is not None:
        task = await _one(conn, "SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
        if task is None:
            raise KeyError(task_id)
        if project_id is not None and project_id != task["project_id"]:
            raise DomainConflict("artifact project does not match its task")
    if file_id is not None:
        file = await _one(conn, "SELECT sha256, size FROM files WHERE id = ?", (file_id,))
        if file is None or file["sha256"] != digest or int(file["size"]) != size_bytes:
            raise DomainConflict("file bytes do not match the artifact manifest")
        if task_id is not None and await _one(conn, "SELECT 1 FROM task_files WHERE task_id = ? AND file_id = ?",
                                              (task_id, file_id)) is None:
            raise DomainConflict("file is not attached to this task")
    await conn.execute(
        "INSERT INTO artifact_manifests(id, project_id, task_id, artifact_kind, artifact_key, artifact_revision,"
        " digest, size_bytes, file_id, provenance_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (manifest_id, project_id, task_id, artifact_kind, artifact_key, artifact_revision,
         digest, size_bytes, file_id, _canonical(provenance or {}), _now()),
    )
    return {"id": manifest_id, "digest": digest, "artifact_revision": artifact_revision}


class OriginalReports:
    """Content-addressed storage for large original reports, staged before DB reference."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def stage(self, original: bytes) -> tuple[str | None, str | None, str, int]:
        if len(original) > DIRECT_REPORT_MAX:
            raise ValueError("direct report exceeds 4 MiB; upload it as a scoped artifact")
        digest = hashlib.sha256(original).hexdigest()
        if len(original) <= INLINE_REPORT_MAX:
            return original.decode("utf-8"), None, digest, len(original)
        original.decode("utf-8")
        self.root.mkdir(parents=True, exist_ok=True)
        name = self.root / digest
        if not name.exists():
            descriptor, temporary = tempfile.mkstemp(prefix=".report-", dir=self.root)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, name)
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return None, digest, digest, len(original)

    def read(self, blob_ref: str, expected_digest: str) -> bytes:
        if len(blob_ref) != 64 or any(letter not in "0123456789abcdef" for letter in blob_ref):
            raise ValueError("invalid report blob reference")
        original = (self.root / blob_ref).read_bytes()
        if hashlib.sha256(original).hexdigest() != expected_digest:
            raise DomainConflict("original report digest does not match")
        return original


async def submit_result(
    conn: aiosqlite.Connection, *, result_id: str, task_id: str, attempt_id: str | None,
    contract_revision: int, outcome: str, original_text: str | None, original_blob_ref: str | None,
    original_digest: str, original_size_bytes: int, actor_id: str, manifest_ids: list[str],
    checks: list[dict[str, Any]], limitations: list[str], original_artifact_file_id: str | None = None,
) -> dict[str, Any]:
    """Insert a full immutable report only for the task's current contract and attempt."""
    if outcome not in RESULT_OUTCOMES:
        raise ValueError("invalid result outcome")
    if (original_text is None) == (original_blob_ref is None):
        raise ValueError("the original report needs exactly one source")
    task = await _one(conn, "SELECT id, contract_revision, current_attempt_id FROM board_tasks WHERE id = ?", (task_id,))
    if task is None:
        raise KeyError(task_id)
    if int(task["contract_revision"]) != contract_revision:
        raise DomainConflict("the result refers to an older contract")
    if task["current_attempt_id"] != attempt_id:
        raise DomainConflict("the attempt is no longer current")
    if original_text is not None:
        original = original_text.encode("utf-8")
        if len(original) > INLINE_REPORT_MAX or len(original) != original_size_bytes or hashlib.sha256(original).hexdigest() != original_digest:
            raise DomainConflict("inline report digest or size does not match")
    if not manifest_ids and outcome == "complete":
        raise DomainConflict("a complete result needs at least one artifact manifest")
    for manifest_id in manifest_ids:
        manifest = await _one(conn, "SELECT task_id FROM artifact_manifests WHERE id = ?", (manifest_id,))
        if manifest is None or manifest["task_id"] != task_id:
            raise DomainConflict("result artifact is outside its task")
    await conn.execute(
        "INSERT INTO result_receipts(id, task_id, contract_revision, attempt_id, outcome, original_text,"
        " original_blob_ref, original_digest, original_size_bytes, original_artifact_file_id, checks_json,"
        " limitations_json, actor_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (result_id, task_id, contract_revision, attempt_id, outcome, original_text, original_blob_ref,
         original_digest, original_size_bytes, original_artifact_file_id, _canonical(checks),
         _canonical(limitations), actor_id, _now()),
    )
    for manifest_id in manifest_ids:
        await conn.execute("INSERT INTO result_artifacts(result_id, manifest_id) VALUES (?, ?)",
                           (result_id, manifest_id))
    await conn.execute("UPDATE board_tasks SET acceptance_state = 'handed_in' WHERE id = ?", (task_id,))
    return {"result_id": result_id, "task_id": task_id, "contract_revision": contract_revision,
            "outcome": outcome, "original_digest": original_digest, "original_size_bytes": original_size_bytes,
            "verification": "unverified", "acceptance_state": "handed_in"}


async def add_review_evidence(
    conn: aiosqlite.Connection, *, evidence_id: str, result_id: str, criterion_id: str,
    command: str, exit_code: int | None, environment_digest: str | None,
    manifest_digest_before: str | None, manifest_digest_after: str | None,
    original_verification_id: int | None = None,
) -> dict[str, Any]:
    result = await _one(conn, "SELECT contract_revision FROM result_receipts WHERE id = ?", (result_id,))
    if result is None:
        raise KeyError(result_id)
    await conn.execute(
        "INSERT INTO review_evidence(id, result_id, contract_revision, criterion_id, command, exit_code,"
        " environment_digest, manifest_digest_before, manifest_digest_after, original_verification_id, observed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (evidence_id, result_id, result["contract_revision"], criterion_id, command, exit_code,
         environment_digest, manifest_digest_before, manifest_digest_after, original_verification_id, _now()),
    )
    stale = manifest_digest_before != manifest_digest_after or manifest_digest_before is None
    return {"evidence_id": evidence_id, "result_id": result_id, "criterion_id": criterion_id,
            "verification": "stale" if stale else "verified" if exit_code == 0 else "failed"}


async def record_verdict(
    conn: aiosqlite.Connection, *, verdict_id: str, result_id: str, reviewer_actor_id: str,
    verification: str, accepted: bool, head: str | None, base: str | None,
    environment_digest: str | None, evidence_ids: list[str], reason: str,
    self_review_waiver_receipt_id: str | None = None,
) -> dict[str, Any]:
    """Append judgement without mutating the original result or another verdict."""
    result = await _one(conn, "SELECT task_id, contract_revision, attempt_id, outcome, actor_id FROM result_receipts WHERE id = ?", (result_id,))
    if result is None:
        raise KeyError(result_id)
    task = await _one(conn, "SELECT contract_revision,current_attempt_id,status FROM board_tasks WHERE id = ?", (result["task_id"],))
    if task is None or int(task["contract_revision"]) != int(result["contract_revision"]):
        raise DomainConflict("review target is stale after a contract change")
    if result["attempt_id"] != task["current_attempt_id"]:
        raise DomainConflict("review target came from a superseded attempt")
    latest = await _one(conn, "SELECT id FROM result_receipts WHERE task_id = ? AND contract_revision = ?"
                        " AND attempt_id IS ? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                        (result["task_id"], result["contract_revision"], result["attempt_id"]))
    if latest is None or latest["id"] != result_id:
        raise DomainConflict("a newer result superseded the reviewed result")
    if verification not in ("verified", "failed", "stale"):
        raise ValueError("invalid verification state")
    if reviewer_actor_id == result["actor_id"]:
        waiver = await _one(conn, "SELECT operation_kind, actor_id, response_json FROM operation_receipts WHERE id = ?",
                            (self_review_waiver_receipt_id,)) if self_review_waiver_receipt_id else None
        binding = _json(waiver["response_json"], {}) if waiver is not None else {}
        if (waiver is None or waiver["operation_kind"] != "self_review.waive" or
                waiver["actor_id"] == reviewer_actor_id or binding.get("task_id") != result["task_id"] or
                binding.get("result_id") != result_id or binding.get("contract_revision") != result["contract_revision"] or
                binding.get("approved") is not True):
            raise DomainConflict("the worker cannot independently review its own result")
    if accepted and (verification != "verified" or result["outcome"] != "complete"):
        raise DomainConflict("only a verified complete result can be accepted")
    if accepted and task["status"] != "review":
        raise DomainConflict("a result can be approved only while the task is in review")
    evidence = []
    for evidence_id in evidence_ids:
        row = await _one(conn, "SELECT result_id, contract_revision, exit_code, manifest_digest_before,"
                         " manifest_digest_after FROM review_evidence WHERE id = ?", (evidence_id,))
        if row is None or row["result_id"] != result_id or row["contract_revision"] != result["contract_revision"]:
            raise DomainConflict("evidence does not bind to the reviewed result")
        if accepted and (row["exit_code"] != 0 or row["manifest_digest_before"] is None or
                         row["manifest_digest_before"] != row["manifest_digest_after"]):
            raise DomainConflict("stale or failed evidence cannot support acceptance")
        evidence.append(evidence_id)
    if accepted and not evidence:
        raise DomainConflict("acceptance needs bound evidence")
    if accepted and await unresolved_review_comments(conn, result_id):
        raise DomainConflict("blocking review comments remain unresolved")
    if accepted:
        contract = await _one(conn, "SELECT snapshot_json FROM task_contract_versions"
                              " WHERE task_id = ? AND contract_revision = ?",
                              (result["task_id"], result["contract_revision"]))
        snapshot = _json(contract["snapshot_json"], {}) if contract is not None else {}
        required = {item["id"] for item in snapshot.get("checklist", [])}
        if required:
            covered = set()
            for evidence_id in evidence:
                row = await _one(conn, "SELECT criterion_id FROM review_evidence WHERE id = ?", (evidence_id,))
                if row is not None:
                    covered.add(row["criterion_id"])
            if not required.issubset(covered):
                raise DomainConflict("not every acceptance check has applicable evidence")
    await conn.execute(
        "INSERT INTO review_verdicts(id, result_id, contract_revision, reviewer_actor_id, verification, accepted,"
        " head, base, environment_digest, evidence_json, reason, self_review_waiver_receipt_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (verdict_id, result_id, result["contract_revision"], reviewer_actor_id, verification,
         int(accepted), head, base, environment_digest, _canonical(evidence), reason,
         self_review_waiver_receipt_id, _now()),
    )
    if accepted:
        await conn.execute("UPDATE board_tasks SET acceptance_state = 'accepted' WHERE id = ? AND status = 'review'",
                           (result["task_id"],))
    return {"verdict_id": verdict_id, "result_id": result_id, "verification": verification,
            "accepted": accepted, "contract_revision": int(result["contract_revision"])}


async def add_review_comment(
    conn: aiosqlite.Connection, *, comment_id: str, result_id: str, author_actor_id: str,
    source: str, priority: str, body: str, manifest_id: str | None = None,
    path: str | None = None, head: str | None = None, line_start: int | None = None,
    line_end: int | None = None, verdict_id: str | None = None,
) -> dict[str, Any]:
    """Pin a comment to a result and optional content identity, never a mutable path alone."""
    if priority not in ("blocking", "important", "suggestion") or not body.strip():
        raise ValueError("review comment needs a priority and body")
    result = await _one(conn, "SELECT task_id FROM result_receipts WHERE id = ?", (result_id,))
    if result is None:
        raise KeyError(result_id)
    if priority == "blocking":
        active_merge = await _one(conn, "SELECT 1 FROM task_merge_receipts m JOIN effect_outbox e ON e.id = m.id"
                                  " WHERE m.result_id = ? AND e.state IN ('claimed','completed','unknown') LIMIT 1",
                                  (result_id,))
        if active_merge is not None:
            raise DomainConflict("a claimed merge freezes blocking review comments until reconciliation")
    if manifest_id is not None:
        attached = await _one(conn, "SELECT 1 FROM result_artifacts WHERE result_id = ? AND manifest_id = ?",
                              (result_id, manifest_id))
        if attached is None:
            raise DomainConflict("review comment artifact is outside the result")
    if verdict_id is not None:
        verdict = await _one(conn, "SELECT result_id FROM review_verdicts WHERE id = ?", (verdict_id,))
        if verdict is None or verdict["result_id"] != result_id:
            raise DomainConflict("review comment verdict belongs to another result")
    if (line_start is not None or line_end is not None) and (not path or not head or line_start is None or
                                                           line_start < 1 or (line_end is not None and line_end < line_start)):
        raise ValueError("line comments need a path, head, and ordered positive line numbers")
    await conn.execute("INSERT INTO review_comments(id, result_id, verdict_id, author_actor_id, source, priority,"
                       " body, manifest_id, path, head, line_start, line_end, created_at)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (comment_id, result_id, verdict_id, author_actor_id, source, priority, body.strip(),
                        manifest_id, path, head, line_start, line_end, _now()))
    return {"comment_id": comment_id, "result_id": result_id, "priority": priority, "state": "open"}


async def resolve_review_comment(
    conn: aiosqlite.Connection, *, resolution_id: str, comment_id: str, result_id: str,
    actor_id: str, resolution: str, reason: str,
) -> dict[str, Any]:
    """Append a resolution so the original review feedback remains inspectable."""
    if resolution not in ("resolved", "reopened", "waived") or not reason.strip():
        raise ValueError("resolution needs a valid state and reason")
    comment = await _one(conn, "SELECT result_id FROM review_comments WHERE id = ?", (comment_id,))
    if comment is None or comment["result_id"] != result_id:
        raise DomainConflict("comment and result do not match")
    await conn.execute("INSERT INTO review_comment_resolutions(id, comment_id, result_id, actor_id, resolution,"
                       " reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (resolution_id, comment_id, result_id, actor_id, resolution, reason.strip(), _now()))
    return {"comment_id": comment_id, "resolution_id": resolution_id, "state": resolution}


async def unresolved_review_comments(conn: aiosqlite.Connection, result_id: str) -> list[dict[str, Any]]:
    rows = await _many(conn,
        "SELECT c.id, c.priority, c.body, (SELECT r.resolution FROM review_comment_resolutions r"
        " WHERE r.comment_id = c.id ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS resolution"
        " FROM review_comments c WHERE c.result_id = ? AND c.priority = 'blocking'", (result_id,))
    return [{"comment_id": row["id"], "priority": row["priority"], "body": row["body"]}
            for row in rows if row["resolution"] not in ("resolved", "waived")]


async def accept_result(
    conn: aiosqlite.Connection, *, task_id: str, result_id: str, verdict_id: str,
    contract_revision: int, current_head: str | None, current_base: str | None,
    current_merge_sha: str | None = None,
) -> dict[str, Any]:
    """Bind acceptance to the exact immutable result and the current review evidence."""
    task = await _one(conn, "SELECT status, contract_revision, acceptance_state, branch, merge_state, checklist, current_attempt_id"
                      " FROM board_tasks WHERE id = ?", (task_id,))
    if task is None:
        raise KeyError(task_id)
    if task["status"] != "review" or int(task["contract_revision"]) != contract_revision:
        raise DomainConflict("the task is not reviewing this contract revision")
    if task["branch"] and task["merge_state"] != "merged":
        raise DomainConflict("branch work needs its separate reviewed operator merge first")
    result = await _one(conn, "SELECT task_id, contract_revision, attempt_id, outcome FROM result_receipts WHERE id = ?", (result_id,))
    if result is None or result["task_id"] != task_id or result["contract_revision"] != contract_revision or result["outcome"] != "complete":
        raise DomainConflict("the accepted result is missing, partial, or stale")
    latest = await _one(conn, "SELECT id FROM result_receipts WHERE task_id = ? AND contract_revision = ?"
                        " AND attempt_id IS ? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                        (task_id, contract_revision, task["current_attempt_id"]))
    if result["attempt_id"] != task["current_attempt_id"] or latest is None or latest["id"] != result_id:
        raise DomainConflict("a newer result or attempt superseded the reviewed result")
    verdict = await _one(conn, "SELECT result_id, contract_revision, verification, accepted, head, base FROM review_verdicts WHERE id = ?",
                         (verdict_id,))
    if verdict is None or verdict["result_id"] != result_id or verdict["contract_revision"] != contract_revision:
        raise DomainConflict("the verdict belongs to another result or contract")
    if verdict["verification"] != "verified" or not verdict["accepted"]:
        raise DomainConflict("the result has no approving verified verdict")
    if await unresolved_review_comments(conn, result_id):
        raise DomainConflict("blocking review comments remain unresolved")
    if verdict["head"] != current_head or verdict["base"] != current_base:
        raise DomainConflict("the reviewed head or base changed")
    if task["branch"]:
        merged = await _one(conn, "SELECT head_sha,base_sha,merge_sha,state FROM task_merge_receipts"
                            " WHERE task_id = ? AND result_id = ? AND verdict_id = ?",
                            (task_id, result_id, verdict_id))
        if (merged is None or merged["state"] != "merged" or merged["head_sha"] != current_head or
                merged["base_sha"] != current_base or merged["merge_sha"] != current_merge_sha):
            raise DomainConflict("the folder lacks a matching durable merge receipt")
    checked = [{**item, "done": True, "review_verdict_id": verdict_id} for item in check_items(task["checklist"])]
    await conn.execute("UPDATE board_tasks SET accepted_result_id = ?, accepted_contract_revision = ?,"
                       " acceptance_state = 'operator_approved', status = 'done', checklist = ? WHERE id = ?",
                       (result_id, contract_revision, _canonical(checked), task_id))
    return {"task_id": task_id, "result_id": result_id, "verdict_id": verdict_id,
            "contract_revision": contract_revision, "acceptance_state": "operator_approved"}


async def return_result(
    conn: aiosqlite.Connection, *, return_id: str, task_id: str, result_id: str,
    verdict_id: str, contract_revision: int, actor_id: str, reason: str,
) -> dict[str, Any]:
    """Send exactly the reviewed result back while fencing its current attempt."""
    if not reason.strip() or len(reason) > 2000:
        raise ValueError("return needs a reason of at most 2000 characters")
    task = await _one(conn, "SELECT contract_revision,status,current_attempt_id FROM board_tasks WHERE id = ?", (task_id,))
    result = await _one(conn, "SELECT task_id,contract_revision,attempt_id FROM result_receipts WHERE id = ?", (result_id,))
    verdict = await _one(conn, "SELECT result_id,contract_revision FROM review_verdicts WHERE id = ?", (verdict_id,))
    if (task is None or task["status"] != "review" or task["contract_revision"] != contract_revision or
            result is None or result["task_id"] != task_id or result["contract_revision"] != contract_revision or
            verdict is None or verdict["result_id"] != result_id or verdict["contract_revision"] != contract_revision or
            (result["attempt_id"] is not None and result["attempt_id"] != task["current_attempt_id"])):
        raise DomainConflict("return target is not the current reviewed result")
    latest = await _one(conn, "SELECT id FROM result_receipts WHERE task_id = ? AND contract_revision = ?"
                        " AND attempt_id IS ? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                        (task_id, contract_revision, task["current_attempt_id"]))
    if latest is None or latest["id"] != result_id:
        raise DomainConflict("a newer result superseded the return target")
    await conn.execute("INSERT INTO review_returns(id,task_id,result_id,verdict_id,contract_revision,actor_id,reason,created_at)"
                       " VALUES (?,?,?,?,?,?,?,?)",
                       (return_id, task_id, result_id, verdict_id, contract_revision, actor_id, reason.strip(), _now()))
    if task["current_attempt_id"]:
        await conn.execute("UPDATE execution_attempts SET state = 'superseded',updated_at = ?"
                           " WHERE id = ? AND state IN ('queued','starting','running','waiting','recovering')",
                           (_now(), task["current_attempt_id"]))
    await conn.execute("UPDATE board_tasks SET status = 'todo',acceptance_state = 'returned',"
                       " current_attempt_id = NULL,notes = substr(notes || ?, -8000) WHERE id = ?",
                       (f"\n[{_now()[:16]}] returned: {reason.strip()}", task_id))
    return {"return_id": return_id, "task_id": task_id, "result_id": result_id,
            "verdict_id": verdict_id, "acceptance_state": "returned", "status": "todo"}


async def dependency_readiness(conn: aiosqlite.Connection, task_id: str) -> dict[str, Any]:
    """Use accepted predecessor results; historical ID-only edges stay unresolved."""
    task = await _one(conn, "SELECT id, contract_revision FROM board_tasks WHERE id = ?", (task_id,))
    if task is None:
        raise KeyError(task_id)
    edges = await _many(conn,
        "SELECT e.*, p.accepted_result_id, p.accepted_contract_revision, p.contract_revision AS current_revision"
        " FROM task_dependency_edges e JOIN board_tasks p ON p.id = e.predecessor_task_id"
        " WHERE e.successor_task_id = ? AND e.resolution_state != 'cancelled' ORDER BY e.id", (task_id,))
    states: list[dict[str, Any]] = []
    fingerprint_parts: list[dict[str, Any]] = [{"task_id": task_id, "contract_revision": task["contract_revision"]}]
    for edge in edges:
        state = "ready"
        if edge["resolution_state"] == "unresolved_legacy":
            state = "unknown_legacy"
        elif edge["resolution_state"] == "waived":
            if edge["waiver_receipt_id"] is None:
                state = "missing_waiver"
        elif edge["kind"] == "required":
            if edge["accepted_result_id"] is None:
                state = "awaiting_accepted_result"
            elif edge["accepted_contract_revision"] != edge["current_revision"]:
                state = "stale_predecessor_contract"
            elif edge["required_result_id"] is not None and edge["required_result_id"] != edge["accepted_result_id"]:
                state = "wrong_accepted_result"
            elif edge["required_contract_revision"] is not None and edge["required_contract_revision"] != edge["accepted_contract_revision"]:
                state = "wrong_contract_revision"
            elif edge["required_artifact_digest"] is not None:
                artifact = await _one(conn,
                    "SELECT 1 FROM result_artifacts a JOIN artifact_manifests m ON m.id = a.manifest_id"
                    " WHERE a.result_id = ? AND m.digest = ?",
                    (edge["accepted_result_id"], edge["required_artifact_digest"]))
                if artifact is None:
                    state = "missing_required_artifact"
        item = {"edge_id": edge["id"], "predecessor_task_id": edge["predecessor_task_id"],
                "kind": edge["kind"], "state": state, "accepted_result_id": edge["accepted_result_id"]}
        states.append(item)
        fingerprint_parts.append(item)
    fingerprint = hashlib.sha256(_canonical(fingerprint_parts).encode()).hexdigest()
    return {"task_id": task_id, "ready": all(edge["state"] == "ready" or edge["kind"] == "optional"
                                                for edge in states), "edges": states,
            "dependency_fingerprint": fingerprint}


async def resolve_dependency(
    conn: aiosqlite.Connection, *, edge_id: str, resolution: str,
    result_id: str | None = None, artifact_digest: str | None = None,
    waiver_receipt_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a legacy edge only with an exact accepted result or explicit waiver."""
    edge = await _one(conn, "SELECT * FROM task_dependency_edges WHERE id = ?", (edge_id,))
    if edge is None:
        raise KeyError(edge_id)
    if resolution == "waived":
        if waiver_receipt_id is None:
            raise DomainConflict("a waiver needs its authorized operation receipt")
        await conn.execute("UPDATE task_dependency_edges SET resolution_state = 'waived', waiver_receipt_id = ?"
                           " WHERE id = ?", (waiver_receipt_id, edge_id))
    elif resolution == "satisfied":
        predecessor = await _one(conn,
            "SELECT accepted_result_id, accepted_contract_revision, contract_revision FROM board_tasks WHERE id = ?",
            (edge["predecessor_task_id"],))
        if predecessor is None or predecessor["accepted_result_id"] != result_id or result_id is None:
            raise DomainConflict("the predecessor has not accepted this exact result")
        if predecessor["accepted_contract_revision"] != predecessor["contract_revision"]:
            raise DomainConflict("the predecessor acceptance is stale")
        if artifact_digest is not None:
            artifact = await _one(conn,
                "SELECT 1 FROM result_artifacts a JOIN artifact_manifests m ON m.id = a.manifest_id"
                " WHERE a.result_id = ? AND m.digest = ?", (result_id, artifact_digest))
            if artifact is None:
                raise DomainConflict("the accepted result has no matching artifact")
        await conn.execute("UPDATE task_dependency_edges SET resolution_state = 'satisfied',"
                           " required_result_id = ?, required_contract_revision = ?, required_artifact_digest = ?"
                           " WHERE id = ?", (result_id, predecessor["contract_revision"], artifact_digest, edge_id))
    else:
        raise ValueError("resolution must be satisfied or waived")
    return {"edge_id": edge_id, "resolution_state": resolution}


async def claim_handoff(
    conn: aiosqlite.Connection, *, claim_id: str, task_id: str,
    operation_id: str, reservation_id: str | None,
) -> dict[str, Any]:
    """Reserve one handoff for the current dependency fingerprint, without launching a worker."""
    readiness = await dependency_readiness(conn, task_id)
    if not readiness["ready"]:
        raise DomainConflict("dependency readiness is not proven")
    fingerprint = readiness["dependency_fingerprint"]
    existing = await _one(conn, "SELECT id, state, reservation_id FROM handoff_claims"
                          " WHERE task_id = ? AND dependency_fingerprint = ?", (task_id, fingerprint))
    if existing is not None:
        return {"claim_id": existing["id"], "task_id": task_id, "state": existing["state"],
                "reservation_id": existing["reservation_id"], "replayed": True}
    await conn.execute("INSERT INTO handoff_claims(id, task_id, dependency_fingerprint, reservation_id,"
                       " operation_id, state, created_at) VALUES (?, ?, ?, ?, ?, 'claimed', ?)",
                       (claim_id, task_id, fingerprint, reservation_id, operation_id, _now()))
    return {"claim_id": claim_id, "task_id": task_id, "state": "claimed",
            "reservation_id": reservation_id, "replayed": False}


async def configure_workflow(
    conn: aiosqlite.Connection, *, task_id: str, steps: list[dict[str, Any]],
    edges: list[tuple[str, str]],
) -> dict[str, Any]:
    """Install a bounded typed DAG before work starts; gates cannot execute arbitrary code."""
    task = await _one(conn, "SELECT contract_revision, status FROM board_tasks WHERE id = ?", (task_id,))
    if task is None:
        raise KeyError(task_id)
    if task["status"] not in ("todo", "blocked"):
        raise DomainConflict("workflow topology is fixed once work starts")
    if not 1 <= len(steps) <= 12:
        raise ValueError("workflow needs one to twelve steps")
    step_by_id = {str(step.get("id")): step for step in steps}
    if len(step_by_id) != len(steps) or any(not step_id or step_id == "None" for step_id in step_by_id):
        raise ValueError("workflow step ids must be distinct")
    for step in steps:
        if step.get("kind") not in ("work", "review", "wait", "human"):
            raise ValueError("workflow step kind is invalid")
        gate = step.get("gate") or {}
        if set(gate) - {"accepted_result_id", "artifact_digest", "not_before"}:
            raise ValueError("workflow gate contains an unsupported condition")
    outgoing: dict[str, list[str]] = {step_id: [] for step_id in step_by_id}
    for source, target in edges:
        if source not in step_by_id or target not in step_by_id or source == target:
            raise ValueError("workflow edge has an invalid endpoint")
        outgoing[source].append(target)
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise ValueError("workflow has a cycle")
        if step_id in visited:
            return
        visiting.add(step_id)
        for target in outgoing[step_id]:
            visit(target)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in step_by_id:
        visit(step_id)
    await conn.execute("DELETE FROM workflow_steps WHERE task_id = ?", (task_id,))
    for step_id, step in step_by_id.items():
        await conn.execute("INSERT INTO workflow_steps(id, task_id, step_kind, state, contract_revision, gate_json)"
                           " VALUES (?, ?, ?, 'pending', ?, ?)",
                           (step_id, task_id, step["kind"], task["contract_revision"], _canonical(step.get("gate") or {})))
    for source, target in edges:
        await conn.execute("INSERT INTO workflow_edges(source_step_id, target_step_id) VALUES (?, ?)", (source, target))
    return {"task_id": task_id, "contract_revision": task["contract_revision"],
            "step_count": len(steps), "edge_count": len(edges)}


async def workflow_readiness(conn: aiosqlite.Connection, task_id: str) -> list[dict[str, Any]]:
    """Project step readiness from current contract, predecessor steps, and typed gates."""
    task = await _one(conn, "SELECT contract_revision, accepted_result_id FROM board_tasks WHERE id = ?", (task_id,))
    if task is None:
        raise KeyError(task_id)
    steps = await _many(conn, "SELECT * FROM workflow_steps WHERE task_id = ? ORDER BY id", (task_id,))
    states = {step["id"]: step["state"] for step in steps}
    result: list[dict[str, Any]] = []
    for step in steps:
        blockers: list[str] = []
        if step["contract_revision"] != task["contract_revision"]:
            blockers.append("stale_contract")
        incoming = await _many(conn, "SELECT source_step_id FROM workflow_edges WHERE target_step_id = ?", (step["id"],))
        if any(states.get(edge["source_step_id"]) != "complete" for edge in incoming):
            blockers.append("predecessor_step_incomplete")
        gate = _json(step["gate_json"], {})
        if gate.get("accepted_result_id") and gate["accepted_result_id"] != task["accepted_result_id"]:
            blockers.append("accepted_result_missing")
        if gate.get("artifact_digest"):
            artifact = await _one(conn, "SELECT 1 FROM result_artifacts a JOIN artifact_manifests m ON m.id = a.manifest_id"
                                  " WHERE a.result_id = ? AND m.digest = ?",
                                  (task["accepted_result_id"], gate["artifact_digest"]))
            if artifact is None:
                blockers.append("artifact_missing")
        if gate.get("not_before") and gate["not_before"] > _now():
            blockers.append("not_before")
        result.append({"step_id": step["id"], "kind": step["step_kind"], "state": step["state"],
                       "ready": step["state"] in ("pending", "ready") and not blockers,
                       "blockers": blockers})
    return result


async def advance_workflow_step(
    conn: aiosqlite.Connection, *, task_id: str, step_id: str, action: str,
) -> dict[str, Any]:
    """Move one step only when its current contract and all typed gates permit it."""
    if action not in ("ack", "start", "complete"):
        raise ValueError("workflow action is invalid")
    step = await _one(conn, "SELECT id,step_kind,state FROM workflow_steps WHERE id = ? AND task_id = ?",
                      (step_id, task_id))
    if step is None:
        raise KeyError(step_id)
    projected = next(item for item in await workflow_readiness(conn, task_id) if item["step_id"] == step_id)
    if action == "ack":
        if step["step_kind"] != "human" or step["state"] not in ("pending", "ready"):
            raise DomainConflict("only a pending human step accepts an operator acknowledgement")
        if projected["blockers"]:
            raise DomainConflict("the human step is blocked by an upstream gate")
        await conn.execute("UPDATE workflow_steps SET state = 'complete',entity_revision = entity_revision + 1"
                           " WHERE id = ? AND task_id = ?", (step_id, task_id))
        return {"task_id": task_id, "step_id": step_id, "state": "complete", "acknowledged": True}
    if projected["blockers"] or (not projected["ready"] and not (action == "complete" and step["state"] == "running")):
        raise DomainConflict("workflow step is blocked by its current gates or predecessor")
    if action == "start" and step["step_kind"] not in ("work", "review"):
        raise DomainConflict("this step does not have a running state")
    state = "running" if action == "start" else "complete"
    await conn.execute("UPDATE workflow_steps SET state = ?,entity_revision = entity_revision + 1"
                       " WHERE id = ? AND task_id = ?", (state, step_id, task_id))
    return {"task_id": task_id, "step_id": step_id, "state": state}


async def set_next_action(
    conn: aiosqlite.Connection, *, action_id: str, task_id: str, kind: str,
    owner_kind: str, owner_id: str | None, prerequisites: list[dict[str, str]],
    due_at: str | None = None, context_ref: str | None = None,
) -> dict[str, Any]:
    """Replace the one durable next action for the current contract revision."""
    if kind not in ("answer_question", "provide_input", "review", "retry", "assign", "wait"):
        raise ValueError("invalid next action kind")
    if owner_kind not in ("operator", "orchestrator", "staff", "system") or (owner_kind == "staff" and not owner_id):
        raise ValueError("invalid next action owner")
    if len(prerequisites) > 12 or any(set(item) != {"kind", "ref"} or item["kind"] not in
                                      ("dependency_ready", "result_verified", "artifact", "ask_answered") or
                                      not item["ref"] for item in prerequisites):
        raise ValueError("invalid next action prerequisites")
    task = await _one(conn, "SELECT contract_revision FROM board_tasks WHERE id = ?", (task_id,))
    if task is None:
        raise KeyError(task_id)
    await conn.execute("UPDATE next_actions SET state = 'cancelled' WHERE task_id = ? AND state = 'active'", (task_id,))
    await conn.execute("INSERT INTO next_actions(id,task_id,contract_revision,kind,owner_kind,owner_id,"
                       " prerequisites_json,due_at,context_ref,state,created_at)"
                       " VALUES (?,?,?,?,?,?,?,?,?,'active',?)",
                       (action_id, task_id, task["contract_revision"], kind, owner_kind, owner_id,
                        _canonical(prerequisites), due_at, context_ref, _now()))
    return {"action_id": action_id, "task_id": task_id, "contract_revision": task["contract_revision"],
            "kind": kind, "owner_kind": owner_kind, "owner_id": owner_id, "state": "active"}


async def next_action_readiness(conn: aiosqlite.Connection, task_id: str) -> dict[str, Any] | None:
    task = await _one(conn, "SELECT contract_revision FROM board_tasks WHERE id = ?", (task_id,))
    if task is None:
        raise KeyError(task_id)
    action = await _one(conn, "SELECT * FROM next_actions WHERE task_id = ? AND state = 'active'"
                        " ORDER BY created_at DESC,id DESC LIMIT 1", (task_id,))
    if action is None:
        return None
    blockers: list[str] = []
    if action["contract_revision"] != task["contract_revision"]:
        blockers.append("stale_contract")
    for item in _json(action["prerequisites_json"], []):
        if item["kind"] == "dependency_ready":
            if not (await dependency_readiness(conn, task_id))["ready"]:
                blockers.append("dependency_not_ready")
        elif item["kind"] == "result_verified":
            verdict = await _one(conn, "SELECT 1 FROM review_verdicts WHERE result_id = ?"
                                 " AND verification = 'verified' AND accepted = 1", (item["ref"],))
            if verdict is None:
                blockers.append("result_not_verified")
        elif item["kind"] == "artifact":
            manifest = await _one(conn, "SELECT 1 FROM artifact_manifests WHERE id = ? AND task_id = ?",
                                  (item["ref"], task_id))
            if manifest is None:
                blockers.append("artifact_missing")
        elif item["kind"] == "ask_answered":
            ask = await _one(conn, "SELECT 1 FROM asks WHERE id = ? AND task_id = ? AND resolved_at IS NOT NULL"
                             " AND answered_contract_revision = ?", (item["ref"], task_id, task["contract_revision"]))
            if ask is None:
                blockers.append("ask_unanswered_or_stale")
    return {"action_id": action["id"], "task_id": task_id, "contract_revision": action["contract_revision"],
            "kind": action["kind"], "owner_kind": action["owner_kind"], "owner_id": action["owner_id"],
            "due_at": action["due_at"], "context_ref": action["context_ref"],
            "enabled": not blockers, "blockers": blockers}


async def scope_impact_preview(conn: aiosqlite.Connection, project_id: str,
                               root_task_ids: list[str]) -> dict[str, Any]:
    """Follow accepted-result dependencies from explicitly named changed work."""
    if not root_task_ids:
        existing = await _one(conn, "SELECT id FROM board_tasks WHERE project_id = ? LIMIT 1", (project_id,))
        if existing is not None:
            raise DomainConflict("scope change needs explicit affected roots")
        return {"project_id": project_id, "affected_task_ids": [], "affected_attempts": [], "root_task_ids": []}
    roots = set(root_task_ids)
    for task_id in roots:
        task = await _one(conn, "SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
        if task is None or task["project_id"] != project_id:
            raise DomainConflict("affected root is outside the project")
    affected = set(roots)
    frontier = list(roots)
    while frontier:
        predecessor = frontier.pop()
        edges = await _many(conn, "SELECT e.successor_task_id FROM task_dependency_edges e"
                            " JOIN board_tasks t ON t.id = e.successor_task_id"
                            " WHERE e.predecessor_task_id = ? AND e.kind = 'required'"
                            " AND e.resolution_state != 'cancelled' AND t.project_id = ?",
                            (predecessor, project_id))
        for edge in edges:
            successor = edge["successor_task_id"]
            if successor not in affected:
                affected.add(successor)
                frontier.append(successor)
    attempts = await _many(conn, "SELECT id,task_id FROM execution_attempts WHERE task_id IN ("
                           + ",".join("?" for _ in affected) + ") AND state IN ('queued','starting','running','waiting','recovering')",
                           tuple(sorted(affected)))
    return {"project_id": project_id, "affected_task_ids": sorted(affected),
            "affected_attempts": [dict(row) for row in attempts],
            "root_task_ids": sorted(roots)}


async def apply_goal_revision(
    conn: aiosqlite.Connection, *, project_id: str, expected_goal_revision: int,
    body: str, root_task_ids: list[str], origin_kind: str, origin_ref: str,
) -> dict[str, Any]:
    """Advance a project goal and fence only its explicit dependency closure."""
    project = await _one(conn, "SELECT goal_revision FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise KeyError(project_id)
    if project["goal_revision"] != expected_goal_revision:
        raise DomainConflict("goal changed since the impact preview")
    prior = await _one(conn, "SELECT body FROM project_goal_revisions WHERE project_id = ? AND goal_revision = ?",
                       (project_id, expected_goal_revision))
    if prior is None:
        raise DomainConflict("current goal revision is missing")
    if prior["body"] == body:
        return {"project_id": project_id, "goal_revision": expected_goal_revision,
                "affected_task_ids": [], "semantic_change": False}
    preview = await scope_impact_preview(conn, project_id, root_task_ids)
    revision = expected_goal_revision + 1
    await conn.execute("INSERT INTO project_goal_revisions(project_id,goal_revision,body,origin_kind,origin_ref,created_at)"
                       " VALUES (?,?,?,?,?,?)", (project_id, revision, body, origin_kind, origin_ref, _now()))
    await conn.execute("UPDATE projects SET goal_revision = ? WHERE id = ?", (revision, project_id))
    await conn.execute("UPDATE planning_budgets SET goal_contract_revision = ?,entity_revision = entity_revision + 1"
                       " WHERE project_id = ?", (revision, project_id))
    await conn.execute("INSERT INTO project_briefs(project_id,section,body,updated_at,updated_by)"
                       " VALUES (?,'goals',?,?,?) ON CONFLICT(project_id,section) DO UPDATE SET"
                       " body = excluded.body,updated_at = excluded.updated_at,updated_by = excluded.updated_by",
                       (project_id, body, _now(), origin_kind if origin_kind in ("operator", "orchestrator", "system") else "system"))
    for task_id in preview["affected_task_ids"]:
        await conn.execute("INSERT INTO scope_impacts(id,project_id,contract_revision,impact_kind,target_key,"
                           " affected_attempt_id,disposition,reason,created_at)"
                           " SELECT ?,?,?, 'task', ?, current_attempt_id,'replan','goal revision changed',?"
                           " FROM board_tasks WHERE id = ?",
                           (uuid.uuid4().hex, project_id, revision, task_id, _now(), task_id))
        await conn.execute("UPDATE execution_attempts SET state = 'superseded',updated_at = ?"
                           " WHERE task_id = ? AND state IN ('queued','starting','running','waiting','recovering')",
                           (_now(), task_id))
        await conn.execute("UPDATE board_tasks SET current_attempt_id = NULL, accepted_result_id = NULL,"
                           " accepted_contract_revision = NULL, acceptance_state = 'returned',"
                           " entity_revision = entity_revision + 1 WHERE id = ?", (task_id,))
    return {"project_id": project_id, "goal_revision": revision,
            "affected_task_ids": preview["affected_task_ids"], "semantic_change": True}


async def check_planning_capacity(conn: aiosqlite.Connection, project_id: str,
                                  dependencies: list[str], *, additional_tasks: int = 1) -> dict[str, int]:
    """Reserve a finite task count and dependency depth under the caller's write transaction."""
    budget = await _one(conn, "SELECT max_depth,max_tasks FROM planning_budgets WHERE project_id = ?", (project_id,))
    if budget is None:
        raise DomainConflict("the project has no planning budget")
    tasks = await _many(conn, "SELECT id,depends_on FROM board_tasks WHERE project_id = ?", (project_id,))
    if len(tasks) + additional_tasks > budget["max_tasks"]:
        raise DomainConflict("the project task count exceeds its planning budget")
    graph = {row["id"]: _json(row["depends_on"], []) for row in tasks}

    def depth(task_id: str, visited: set[str]) -> int:
        if task_id not in graph or task_id in visited:
            raise DomainConflict("planning dependency is outside the project or cyclic")
        if len(visited) >= budget["max_depth"]:
            raise DomainConflict("the plan exceeds its dependency depth budget")
        upstream = graph[task_id]
        return 1 + max((depth(parent, visited | {task_id}) for parent in upstream), default=0)

    new_depth = 1 + max((depth(dependency, set()) for dependency in dependencies), default=0)
    if new_depth > budget["max_depth"]:
        raise DomainConflict("the plan exceeds its dependency depth budget")
    return {"depth": new_depth, "remaining_tasks": budget["max_tasks"] - len(tasks) - additional_tasks}


async def update_role_profile(
    conn: aiosqlite.Connection, *, staff_id: str, purpose: str,
    authority: dict[str, Any], output_contract: dict[str, Any],
) -> dict[str, Any]:
    """Version the member's job separately from its runtime harness and live attempts."""
    if not purpose.strip() or len(purpose) > 1000 or len(_canonical(authority)) > 4000 or len(_canonical(output_contract)) > 4000:
        raise ValueError("role purpose or typed authority/output contract is invalid")
    member = await _one(conn, "SELECT project_id,role_revision,purpose,authority_json,output_contract_json,"
                        " archived_at FROM staff WHERE id = ?", (staff_id,))
    if member is None:
        raise KeyError(staff_id)
    if member["archived_at"] is not None:
        raise DomainConflict("an archived member cannot receive a new role")
    if (member["purpose"] == purpose.strip() and _json(member["authority_json"], {}) == authority and
            _json(member["output_contract_json"], {}) == output_contract):
        return {"staff_id": staff_id, "role_revision": member["role_revision"], "semantic_change": False}
    revision = int(member["role_revision"]) + 1
    await conn.execute("UPDATE staff SET role_revision = ?,purpose = ?,authority_json = ?,output_contract_json = ?"
                       " WHERE id = ?", (revision, purpose.strip(), _canonical(authority),
                                          _canonical(output_contract), staff_id))
    await conn.execute("INSERT INTO staff_role_versions(staff_id,role_revision,purpose,authority_json,"
                       " output_contract_json,created_at) VALUES (?,?,?,?,?,?)",
                       (staff_id, revision, purpose.strip(), _canonical(authority),
                        _canonical(output_contract), _now()))
    return {"staff_id": staff_id, "role_revision": revision, "semantic_change": True}


class OrchestratorDomain:
    """Read projections over immutable records; writes are the transaction helpers above."""

    def __init__(self, db: Database, reports: OriginalReports | None = None) -> None:
        self.db = db
        self.reports = reports or OriginalReports(db.path.parent / "result-originals")

    async def contract(self, task_id: str) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT b.id, b.contract_revision, b.entity_revision, v.origin_kind, v.origin_ref, v.snapshot_json"
            " FROM board_tasks b JOIN task_contract_versions v ON v.task_id = b.id"
            " AND v.contract_revision = b.contract_revision WHERE b.id = ?", (task_id,),
        )
        if row is None:
            raise KeyError(task_id)
        return {"task_id": row["id"], "contract_revision": int(row["contract_revision"]),
                "entity_revision": int(row["entity_revision"]), "origin": {"kind": row["origin_kind"], "ref": row["origin_ref"]},
                **_json(row["snapshot_json"], {})}

    async def results(self, task_id: str) -> list[dict[str, Any]]:
        task = await self.db.fetchone("SELECT contract_revision, current_attempt_id, accepted_result_id, acceptance_state"
                                      " FROM board_tasks WHERE id = ?", (task_id,))
        if task is None:
            raise KeyError(task_id)
        rows = await self.db.fetchall("SELECT * FROM result_receipts WHERE task_id = ? ORDER BY created_at DESC, rowid DESC", (task_id,))
        current_result_id = next((row["id"] for row in rows if int(row["contract_revision"]) == int(task["contract_revision"])
                                  and row["attempt_id"] == task["current_attempt_id"]), None)
        out = []
        for row in rows:
            verdict = await self.db.fetchone("SELECT id, verification, accepted, contract_revision, head, base"
                                             " FROM review_verdicts WHERE result_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (row["id"],))
            manifests = await self.db.fetchall("SELECT m.id, m.artifact_kind, m.artifact_key, m.artifact_revision,"
                                               " m.digest, m.size_bytes FROM result_artifacts a JOIN artifact_manifests m"
                                               " ON m.id = a.manifest_id WHERE a.result_id = ?", (row["id"],))
            stale = int(row["contract_revision"]) != int(task["contract_revision"])
            out.append({"result_id": row["id"], "task_id": task_id, "attempt_id": row["attempt_id"],
                        "contract_revision": int(row["contract_revision"]), "outcome": row["outcome"],
                        "original_digest": row["original_digest"], "original_size_bytes": row["original_size_bytes"],
                        "original_preview": (row["original_text"] or "")[:1000],
                        "artifacts": [dict(manifest) for manifest in manifests],
                        "checks": _json(row["checks_json"], []), "limitations": _json(row["limitations_json"], []),
                        "verification": "stale" if stale else verdict["verification"] if verdict else "unverified",
                        "verdict_id": verdict["id"] if verdict else None,
                        "verdict_accepted": bool(verdict["accepted"]) if verdict else False,
                        "verdict_head": verdict["head"] if verdict else None,
                        "verdict_base": verdict["base"] if verdict else None,
                        "current_result_id": current_result_id,
                        "acceptance_state": task["acceptance_state"] if task["accepted_result_id"] == row["id"] else "handed_in",
                        "accepted": task["accepted_result_id"] == row["id"], "created_at": row["created_at"]})
        return out

    async def original(self, task_id: str, result_id: str) -> bytes:
        row = await self.db.fetchone("SELECT original_text, original_blob_ref, original_digest, original_size_bytes"
                                     " FROM result_receipts WHERE task_id = ? AND id = ?", (task_id, result_id))
        if row is None:
            raise KeyError(result_id)
        original = row["original_text"].encode("utf-8") if row["original_text"] is not None else self.reports.read(
            row["original_blob_ref"], row["original_digest"])
        if len(original) != row["original_size_bytes"] or hashlib.sha256(original).hexdigest() != row["original_digest"]:
            raise DomainConflict("original report bytes changed")
        return original

    async def comments(self, task_id: str, result_id: str) -> list[dict[str, Any]]:
        result = await self.db.fetchone("SELECT 1 FROM result_receipts WHERE id = ? AND task_id = ?", (result_id, task_id))
        if result is None:
            raise KeyError(result_id)
        rows = await self.db.fetchall(
            "SELECT c.*,m.digest AS manifest_digest,"
            " (SELECT r.resolution FROM review_comment_resolutions r WHERE r.comment_id = c.id"
            " ORDER BY r.created_at DESC,r.id DESC LIMIT 1) AS resolution"
            " FROM review_comments c LEFT JOIN artifact_manifests m ON m.id = c.manifest_id"
            " WHERE c.result_id = ? ORDER BY c.created_at,c.id", (result_id,))
        return [{"comment_id": row["id"], "result_id": result_id, "verdict_id": row["verdict_id"],
                 "author_actor_id": row["author_actor_id"], "source": row["source"],
                 "priority": row["priority"], "body": row["body"], "manifest_id": row["manifest_id"],
                 "manifest_digest": row["manifest_digest"], "path": row["path"], "head": row["head"],
                 "line_start": row["line_start"], "line_end": row["line_end"],
                 "state": row["resolution"] or "open", "created_at": row["created_at"]} for row in rows]


__all__ = ["DomainConflict", "OriginalReports", "OrchestratorDomain", "replace_contract",
           "add_artifact_manifest", "submit_result", "add_review_evidence", "record_verdict", "accept_result",
           "dependency_readiness", "resolve_dependency", "claim_handoff", "configure_workflow",
           "workflow_readiness"]
