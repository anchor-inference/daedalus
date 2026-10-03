"""Versioned project knowledge whose model-created claims remain candidates.

The source is checked again inside the promotion transaction so a replaced source cannot silently
turn an old claim into context authority.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Any

from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database


class KnowledgeConflict(ValueError):
    """A version, scope or source no longer matches the reviewed request."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _view(row: Any) -> dict[str, Any]:
    return dict(row)


async def enqueue_artifact_change(
    conn: Any, *, task_id: str | None, project_id: str | None,
    artifact_key: str, artifact_revision: int,
) -> int:
    """Queue facts made stale by a committed artifact revision in the same transaction."""
    if task_id is not None:
        cursor = await conn.execute(
            "SELECT m.id,COALESCE(m.project_id,t.project_id) project_id FROM artifact_manifests m "
            "JOIN board_tasks t ON t.id = m.task_id "
            "WHERE m.task_id = ? AND m.artifact_key = ? AND m.artifact_revision = ?",
            (task_id, artifact_key, artifact_revision),
        )
    else:
        cursor = await conn.execute(
            "SELECT id,project_id FROM artifact_manifests WHERE task_id IS NULL AND project_id = ? "
            "AND artifact_key = ? AND artifact_revision = ?",
            (project_id, artifact_key, artifact_revision),
        )
    new = await cursor.fetchone()
    await cursor.close()
    if new is None or (project_id is not None and new["project_id"] != project_id):
        raise KnowledgeConflict("new artifact does not belong to the declared scope")
    scope_sql = "old.task_id = ?" if task_id is not None else "old.task_id IS NULL AND old.project_id = ?"
    scope_id = task_id if task_id is not None else project_id
    cursor = await conn.execute(
        "SELECT d.fact_id,d.fact_version,d.source_id FROM knowledge_dependencies d "
        "JOIN knowledge_fact_versions v ON v.fact_id = d.fact_id AND v.version = d.fact_version "
        "JOIN artifact_manifests old ON old.id = d.source_id "
        "WHERE d.source_kind = 'manifest' AND v.status = 'promoted' AND v.project_id = ? "
        "AND v.version = (SELECT MAX(version) FROM knowledge_fact_versions WHERE fact_id = v.fact_id) "
        "AND old.artifact_key = ? AND old.artifact_revision < ? AND " + scope_sql,
        (new["project_id"], artifact_key, artifact_revision, scope_id),
    )
    dependencies = await cursor.fetchall()
    await cursor.close()
    for item in dependencies:
        queue_id = hashlib.sha256(f"{item['fact_id']}:{item['fact_version']}:{new['id']}".encode()).hexdigest()[:32]
        await conn.execute(
            "INSERT OR IGNORE INTO knowledge_invalidation_queue "
            "(id,fact_id,fact_version,project_id,source_id,replacement_manifest_id,observed_revision,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (queue_id, item["fact_id"], item["fact_version"], new["project_id"],
             item["source_id"], new["id"], artifact_revision, _now()),
        )
    return len(dependencies)


class KnowledgeStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def _source(self, conn: Any, project_id: str, kind: str, source_id: str) -> tuple[str, str]:
        if kind == "file":
            cursor = await conn.execute(
                "SELECT f.sha256 FROM files f JOIN file_access a ON a.file_id = f.id "
                "WHERE f.id = ? AND a.scope = ?", (source_id, project_id)
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KnowledgeConflict("source file is not available in this project")
            return str(row["sha256"]), str(row["sha256"])
        if kind == "manifest":
            cursor = await conn.execute(
                "SELECT m.artifact_key, m.artifact_revision, m.digest, m.task_id FROM artifact_manifests m "
                "LEFT JOIN board_tasks t ON t.id = m.task_id "
                "WHERE m.id = ? AND COALESCE(m.project_id, t.project_id) = ?", (source_id, project_id)
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KnowledgeConflict("artifact manifest is not available in this project")
            if row["task_id"]:
                cursor = await conn.execute(
                    "SELECT MAX(artifact_revision) latest FROM artifact_manifests "
                    "WHERE task_id = ? AND artifact_key = ?", (row["task_id"], row["artifact_key"])
                )
            else:
                cursor = await conn.execute(
                    "SELECT MAX(artifact_revision) latest FROM artifact_manifests "
                    "WHERE project_id = ? AND task_id IS NULL AND artifact_key = ?", (project_id, row["artifact_key"])
                )
            newest = await cursor.fetchone()
            await cursor.close()
            return str(newest["latest"]), str(row["digest"])
        if kind == "run":
            cursor = await conn.execute(
                "SELECT r.updated_at FROM runs r JOIN sessions s ON s.id = r.session_id "
                "WHERE r.id = ? AND s.project_id = ? AND r.status = 'completed'", (source_id, project_id)
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KnowledgeConflict("source run is not completed in this project")
            revision = str(row["updated_at"])
            return revision, hashlib.sha256(revision.encode()).hexdigest()
        raise KnowledgeConflict("unsupported source kind")

    async def candidate(
        self, project_id: str, claim: str, *, kind: str = "fact", source_kind: str,
        source_id: str, actor: str, scope: str = "project",
    ) -> dict[str, Any]:
        claim = claim.strip()
        if not 12 <= len(claim) <= 600 or scope != "project":
            raise ValueError("a project candidate needs a bounded claim and project scope")
        fact_id = secrets.token_hex(12)
        async with self.db.transaction() as conn:
            return await self._candidate(conn, fact_id, project_id, claim, kind, source_kind, source_id, actor, scope)

    async def _candidate(
        self, conn: Any, fact_id: str, project_id: str, claim: str, kind: str,
        source_kind: str, source_id: str, actor: str, scope: str,
    ) -> dict[str, Any]:
        revision, digest = await self._source(conn, project_id, source_kind, source_id)
        await conn.execute(
            "INSERT INTO knowledge_fact_versions "
            "(fact_id, version, project_id, claim, kind, scope, status, source_kind, source_id, "
            "source_revision, source_digest, actor, created_at) VALUES (?, 1, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?)",
            (fact_id, project_id, claim, kind, scope, source_kind, source_id, revision, digest, actor, _now()),
        )
        await conn.execute(
            "INSERT INTO knowledge_dependencies "
            "(fact_id, fact_version, source_kind, source_id, source_revision, source_digest) "
            "VALUES (?, 1, ?, ?, ?, ?)", (fact_id, source_kind, source_id, revision, digest),
        )
        return {"fact_id": fact_id, "version": 1, "status": "candidate", "source_revision": revision}

    async def candidate_command(
        self, principal: Principal, project_id: str, *, expected_revision: int,
        client_operation_id: str, claim: str, kind: str, source_kind: str, source_id: str,
    ) -> dict[str, Any]:
        claim = claim.strip()
        if not 12 <= len(claim) <= 600:
            raise ValueError("candidate claim length is invalid")

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            return await self._candidate(
                conn, mutation.object_id, project_id, claim, kind, source_kind, source_id,
                principal.actor_id, "project",
            )

        return await ControlStore(self.db).mutate(
            principal, Scope("project", project_id), "knowledge.candidate", client_operation_id,
            expected_revision, Entity("collection", project_id),
            {"claim": claim, "kind": kind, "source_kind": source_kind, "source_id": source_id}, effect,
        )

    async def latest(self, fact_id: str, project_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM knowledge_fact_versions WHERE fact_id = ? AND project_id = ? "
            "ORDER BY version DESC LIMIT 1", (fact_id, project_id)
        )
        return _view(row) if row else None

    async def list(self, project_id: str, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT v.* FROM knowledge_fact_versions v JOIN "
            "(SELECT fact_id, MAX(version) version FROM knowledge_fact_versions "
            "WHERE project_id = ? GROUP BY fact_id) h ON h.fact_id = v.fact_id AND h.version = v.version "
            "ORDER BY v.created_at DESC", (project_id,)
        )
        views = [_view(row) for row in rows]
        return views if include_inactive else [row for row in views if row["status"] == "promoted"]

    async def list_with_freshness(self, project_id: str) -> list[dict[str, Any]]:
        rows = await self.list(project_id)
        async with self.db.transaction() as conn:
            for row in rows:
                try:
                    revision, digest = await self._source(conn, project_id, row["source_kind"], row["source_id"])
                except KnowledgeConflict:
                    row["source_status"] = "missing"
                else:
                    row["source_status"] = "current" if revision == row["source_revision"] and digest == row["source_digest"] else "stale"
        return rows

    async def inspection(self, project_id: str, *, limit: int, before: str | None = None) -> dict[str, Any]:
        """Page current fact versions and their source freshness under the same revision snapshot."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("knowledge page size must be between one and one hundred")
        scope = Scope("project", project_id)
        control = ControlStore(self.db)
        async with self.db.transaction() as conn:
            project_revision = await control._entity(conn, scope, Entity("project", project_id))
            collection_revision = await control._entity(conn, scope, Entity("collection", project_id))
            args: list[Any] = [project_id]
            page = ""
            if before:
                cursor = await conn.execute("SELECT created_at,fact_id FROM knowledge_fact_versions"
                                            " WHERE project_id = ? AND fact_id = ? ORDER BY version DESC LIMIT 1",
                                            (project_id, before))
                boundary = await cursor.fetchone()
                await cursor.close()
                if boundary is None:
                    raise KnowledgeConflict("knowledge page cursor is not in this project")
                page = " AND (v.created_at < ? OR (v.created_at = ? AND v.fact_id < ?))"
                args.extend([boundary["created_at"], boundary["created_at"], boundary["fact_id"]])
            args.append(limit + 1)
            cursor = await conn.execute("SELECT v.* FROM knowledge_fact_versions v JOIN"
                                        " (SELECT fact_id,MAX(version) version FROM knowledge_fact_versions"
                                        " WHERE project_id = ? GROUP BY fact_id) h"
                                        " ON h.fact_id = v.fact_id AND h.version = v.version WHERE 1 = 1"
                                        + page + " ORDER BY v.created_at DESC,v.fact_id DESC LIMIT ?", args)
            found = await cursor.fetchall()
            await cursor.close()
            facts = [_view(row) for row in found[:limit]]
            for row in facts:
                try:
                    revision, digest = await self._source(conn, project_id, row["source_kind"], row["source_id"])
                except KnowledgeConflict:
                    row["source_status"] = "missing"
                else:
                    row["source_status"] = "current" if revision == row["source_revision"] and digest == row["source_digest"] else "stale"
        return {"project_id": project_id, "entity_revision": project_revision,
                "collection_revision": collection_revision, "facts": facts,
                "next_before": facts[-1]["fact_id"] if len(found) > limit else None}

    async def review(
        self, fact_id: str, project_id: str, *, expected_version: int, verdict: str,
        actor: str, reason: str,
    ) -> dict[str, Any]:
        if verdict not in {"review", "promote", "invalidate", "forget", "rollback"} or not reason.strip():
            raise ValueError("a review needs a verdict and reason")
        async with self.db.transaction() as conn:
            return await self._review(conn, fact_id, project_id, expected_version, verdict, actor, reason)

    async def _review(
        self, conn: Any, fact_id: str, project_id: str, expected_version: int,
        verdict: str, actor: str, reason: str,
    ) -> dict[str, Any]:
        cursor = await conn.execute(
            "SELECT * FROM knowledge_fact_versions WHERE fact_id = ? AND project_id = ? "
            "ORDER BY version DESC LIMIT 1", (fact_id, project_id)
        )
        current = await cursor.fetchone()
        await cursor.close()
        if current is None:
            raise KeyError(fact_id)
        if current["version"] != expected_version:
            raise KnowledgeConflict("fact version changed")
        if verdict in {"promote", "review"}:
            revision, digest = await self._source(conn, project_id, current["source_kind"], current["source_id"])
            if revision != current["source_revision"] or digest != current["source_digest"]:
                raise KnowledgeConflict("source revision changed")
        if verdict == "rollback":
            cursor = await conn.execute(
                "SELECT * FROM knowledge_fact_versions WHERE fact_id = ? AND version < ? "
                "AND status = 'promoted' ORDER BY version DESC LIMIT 1", (fact_id, expected_version)
            )
            restored = await cursor.fetchone()
            await cursor.close()
            if restored is None:
                raise KnowledgeConflict("no promoted version to restore")
            source = restored
            status = "promoted"
        else:
            source = current
            status = {"review": "reviewed", "promote": "promoted", "invalidate": "invalidated", "forget": "forgotten"}[verdict]
        version = expected_version + 1
        at = _now()
        await conn.execute(
            "INSERT INTO knowledge_fact_versions "
            "(fact_id, version, project_id, claim, kind, scope, status, source_kind, source_id, "
            "source_revision, source_digest, origin_version, actor, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fact_id, version, project_id, source["claim"], source["kind"], source["scope"], status,
             source["source_kind"], source["source_id"], source["source_revision"],
             source["source_digest"], source["version"], actor, reason.strip(), at),
        )
        await conn.execute(
            "INSERT INTO knowledge_reviews (id, fact_id, fact_version, reviewer_actor, verdict, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (secrets.token_hex(12), fact_id, expected_version, actor, verdict, reason.strip(), at),
        )
        await conn.execute(
            "INSERT INTO knowledge_dependencies "
            "(fact_id, fact_version, source_kind, source_id, source_revision, source_digest) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fact_id, version, source["source_kind"], source["source_id"],
             source["source_revision"], source["source_digest"]),
        )
        if verdict in {"invalidate", "forget"}:
            await conn.execute(
                "UPDATE knowledge_invalidation_queue SET status = 'resolved', resolution_version = ?, resolved_at = ? "
                "WHERE fact_id = ? AND fact_version = ? AND status = 'pending'",
                (version, at, fact_id, expected_version),
            )
        return {"fact_id": fact_id, "version": version, "status": status}

    async def stale_queue(self, project_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT q.*,v.claim,m.digest replacement_digest FROM knowledge_invalidation_queue q "
            "JOIN knowledge_fact_versions v ON v.fact_id = q.fact_id AND v.version = q.fact_version "
            "JOIN artifact_manifests m ON m.id = q.replacement_manifest_id "
            "WHERE q.project_id = ? AND q.status = 'pending' ORDER BY q.created_at,q.id LIMIT 200",
            (project_id,),
        )
        return [dict(row) for row in rows]

    async def revalidate_command(
        self, principal: Principal, project_id: str, fact_id: str, queue_id: str, *,
        expected_revision: int, expected_version: int, client_operation_id: str,
    ) -> dict[str, Any]:
        async def effect(conn: Any, _mutation: Any) -> dict[str, Any]:
            cursor = await conn.execute(
                "SELECT * FROM knowledge_invalidation_queue WHERE id = ? AND fact_id = ? "
                "AND project_id = ? AND status = 'pending'", (queue_id, fact_id, project_id),
            )
            queued = await cursor.fetchone()
            await cursor.close()
            if queued is None or queued["fact_version"] != expected_version:
                raise KnowledgeConflict("invalidation queue or fact version changed")
            cursor = await conn.execute(
                "SELECT * FROM knowledge_fact_versions WHERE fact_id = ? AND project_id = ? "
                "ORDER BY version DESC LIMIT 1", (fact_id, project_id),
            )
            current = await cursor.fetchone()
            await cursor.close()
            if current is None or current["version"] != expected_version or current["status"] != "promoted":
                raise KnowledgeConflict("promoted fact version changed")
            try:
                revision, digest = await self._source(conn, project_id, current["source_kind"], current["source_id"])
                stale = revision != current["source_revision"] or digest != current["source_digest"]
            except KnowledgeConflict:
                stale = True
            if stale:
                return await self._review(
                    conn, fact_id, project_id, expected_version, "invalidate", principal.actor_id,
                    "source artifact revision changed",
                )
            await conn.execute(
                "UPDATE knowledge_invalidation_queue SET status = 'dismissed', resolved_at = ? WHERE id = ?",
                (_now(), queue_id),
            )
            return {"fact_id": fact_id, "version": expected_version, "status": "promoted", "queue_status": "dismissed"}

        return await ControlStore(self.db).mutate(
            principal, Scope("project", project_id), "knowledge.revalidate", client_operation_id,
            expected_revision, Entity("project", project_id),
            {"fact_id": fact_id, "queue_id": queue_id, "expected_version": expected_version}, effect,
        )

    async def review_command(
        self, principal: Principal, project_id: str, fact_id: str, *, expected_revision: int,
        expected_version: int, verdict: str, reason: str, client_operation_id: str,
    ) -> dict[str, Any]:
        if verdict not in {"review", "promote", "invalidate", "forget", "rollback"} or not reason.strip():
            raise ValueError("a review needs a verdict and reason")

        async def effect(conn: Any, _mutation: Any) -> dict[str, Any]:
            return await self._review(conn, fact_id, project_id, expected_version, verdict, principal.actor_id, reason)

        return await ControlStore(self.db).mutate(
            principal, Scope("project", project_id), "knowledge.review", client_operation_id,
            expected_revision, Entity("project", project_id),
            {"fact_id": fact_id, "expected_version": expected_version, "verdict": verdict, "reason": reason}, effect,
        )

    async def context_facts(self, project_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        facts = await self.list(project_id, include_inactive=False)
        fresh = []
        async with self.db.transaction() as conn:
            for fact in facts:
                if len(fresh) >= limit:
                    break
                try:
                    revision, digest = await self._source(conn, project_id, fact["source_kind"], fact["source_id"])
                except KnowledgeConflict:
                    continue
                if revision == fact["source_revision"] and digest == fact["source_digest"]:
                    fresh.append(fact)
        return fresh

    async def capture_compaction(
        self, session_id: str, run_id: str, project_id: str | None, summary: str,
        transcript_seqs: list[int], *, contract_refs: list[str] | None = None,
        question_refs: list[str] | None = None, seed_end_seq: int | None = None,
    ) -> dict[str, Any]:
        """Keep the source boundary alongside a summary without treating the summary as evidence."""
        capture_id = secrets.token_hex(12)
        end_seq = max(transcript_seqs, default=0)
        refs = [f"transcript:{session_id}@{seq}" for seq in transcript_seqs]
        digest = hashlib.sha256(summary.encode()).hexdigest()
        async with self.db.transaction() as conn:
            if contract_refs is None:
                tasks = await conn.execute(
                    "SELECT id, contract_revision FROM board_tasks WHERE session_id = ? "
                    "AND status NOT IN ('done', 'dropped')", (session_id,)
                )
                contract_refs = [f"task-contract:{row['id']}@{row['contract_revision']}" for row in await tasks.fetchall()]
                await tasks.close()
                if project_id is not None:
                    loops = await conn.execute(
                        "SELECT id, event_seq FROM open_loops WHERE project_id = ? AND closed_at IS NULL",
                        (project_id,),
                    )
                    contract_refs.extend(f"loop:{row['id']}@{row['event_seq']}" for row in await loops.fetchall())
                    await loops.close()
            if question_refs is None:
                questions = await conn.execute(
                    "SELECT run_id, tool_call_id FROM pending_questions WHERE session_id = ?",
                    (session_id,),
                )
                question_refs = [f"question:{row['run_id']}:{row['tool_call_id']}" for row in await questions.fetchall()]
                await questions.close()
            await conn.execute(
                "INSERT INTO compaction_captures "
                "(id, session_id, run_id, project_id, summary_digest, source_refs, contract_refs, "
                "question_refs, seed_end_seq, current_end_seq, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (capture_id, session_id, run_id, project_id, digest, json.dumps(refs),
                 json.dumps(contract_refs), json.dumps(question_refs), seed_end_seq,
                 end_seq, _now()),
            )
        return {"id": capture_id, "summary_digest": digest, "source_refs": refs,
                "contract_refs": contract_refs, "question_refs": question_refs,
                "seed_end_seq": seed_end_seq, "current_end_seq": end_seq}
