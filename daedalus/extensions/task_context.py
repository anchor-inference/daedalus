"""A bounded, versioned task packet shared by operator preview and worker launch."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from daedalus.extensions.orchestrator_domain import dependency_readiness
from daedalus.stores.knowledge import KnowledgeConflict, KnowledgeStore


class ContextUnavailable(ValueError):
    """A task source cannot be pinned without omitting an obligation."""


MAX_CONTRACT_CHARS = 12_000
MAX_PACKET_CHARS = 32_000
MAX_FACTS = 8
MAX_ARTIFACTS = 12
MAX_DEPENDENCIES = 20
WORD = re.compile(r"[\w]{4,}", re.UNICODE)


async def _one(conn: Any, sql: str, args: tuple[Any, ...]) -> Any:
    async with conn.execute(sql, args) as cursor:
        return await cursor.fetchone()


async def _many(conn: Any, sql: str, args: tuple[Any, ...]) -> list[Any]:
    async with conn.execute(sql, args) as cursor:
        return list(await cursor.fetchall())


def _words(value: str) -> set[str]:
    return {word.casefold() for word in WORD.findall(value)}


def render_task_context(packet: dict[str, Any]) -> str:
    """Keep the complete contract and accepted dependencies ahead of optional references."""
    body = {key: packet[key] for key in ("task_id", "role", "role_hint", "title", "contract_revision", "contract",
                                          "dependencies", "facts", "artifacts", "source_refs", "packet_hash")}
    return "Current task context (pinned sources; recheck if the task changes):\n" + json.dumps(
        body, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )


async def assemble_task_context(db: Any, task_id: str, *, role: str = "worker",
                                role_hint: str = "") -> dict[str, Any]:
    """Read one SQLite snapshot; reject missing mandatory proof before choosing optional sources."""
    if role not in {"worker", "reviewer", "orchestrator"}:
        raise ValueError("unsupported context role")
    store = KnowledgeStore(db)
    async with db.transaction() as conn:
        task = await _one(conn, "SELECT id,project_id,title,contract_revision FROM board_tasks WHERE id = ?",
                          (task_id,))
        if task is None or task["project_id"] is None:
            raise KeyError(task_id)
        contract = await _one(conn, "SELECT snapshot_json FROM task_contract_versions"
                              " WHERE task_id = ? AND contract_revision = ?",
                              (task_id, task["contract_revision"]))
        if contract is None:
            raise ContextUnavailable("the current task contract is missing")
        if len(contract["snapshot_json"]) > MAX_CONTRACT_CHARS:
            raise ContextUnavailable("the complete task contract exceeds the launch context limit")
        snapshot = json.loads(contract["snapshot_json"])
        if not isinstance(snapshot, dict):
            raise ContextUnavailable("the current task contract is malformed")
        dependency_ids = snapshot.get("depends_on", [])
        if not isinstance(dependency_ids, list) or len(dependency_ids) > MAX_DEPENDENCIES or any(
                not isinstance(item, str) or not item for item in dependency_ids):
            raise ContextUnavailable("the task contract has invalid dependencies")
        if len(dependency_ids) != len(set(dependency_ids)):
            raise ContextUnavailable("the task contract has invalid dependencies")
        requirements = snapshot.get("requirements", [])
        if not isinstance(requirements, list):
            raise ContextUnavailable("the task contract has invalid requirements")
        dependencies = []
        selected_tasks = {task_id}
        refs = [f"task-contract:{task_id}@{task['contract_revision']}#"
                f"{hashlib.sha256(contract['snapshot_json'].encode()).hexdigest()}"]
        refs.append(f"task-title:{task_id}#{hashlib.sha256(task['title'].encode()).hexdigest()}")
        readiness = await dependency_readiness(conn, task_id)
        if not readiness["ready"]:
            raise ContextUnavailable("a required dependency is not accepted at its current contract")
        active_edges = {item["predecessor_task_id"]: item for item in readiness["edges"]
                        if item["kind"] == "required"}
        if any(dependency_id not in active_edges for dependency_id in dependency_ids):
            raise ContextUnavailable("a task dependency lacks an explicit reviewed edge")
        dependency_ids = list(dict.fromkeys([*dependency_ids, *active_edges]))
        if len(dependency_ids) > MAX_DEPENDENCIES:
            raise ContextUnavailable("the task has too many required dependencies for one packet")
        refs.append(f"dependency-gates:{task_id}#{readiness['dependency_fingerprint']}")
        for dependency_id in dependency_ids:
            edge = active_edges[dependency_id]
            if edge["state"] != "ready":
                raise ContextUnavailable("a task dependency lacks accepted result proof")
            row = await _one(conn, "SELECT id,project_id,title,status,contract_revision,accepted_contract_revision,"
                             " accepted_result_id FROM board_tasks WHERE id = ?", (dependency_id,))
            if row is None or row["project_id"] != task["project_id"]:
                raise ContextUnavailable("a dependency is missing or belongs to another project")
            edge_row = await _one(conn, "SELECT resolution_state,waiver_receipt_id FROM task_dependency_edges"
                                  " WHERE id = ?", (edge["edge_id"],))
            if edge_row is not None and edge_row["resolution_state"] == "waived":
                if not edge_row["waiver_receipt_id"]:
                    raise ContextUnavailable("a waived dependency has no operator receipt")
                dependencies.append({"task_id": dependency_id, "state": "waived",
                                     "waiver_receipt_id": edge_row["waiver_receipt_id"]})
                refs.append(f"dependency-waiver:{edge['edge_id']}#{edge_row['waiver_receipt_id']}")
                continue
            if row["status"] != "done" or not row["accepted_result_id"] or row["accepted_contract_revision"] != row["contract_revision"]:
                raise ContextUnavailable("a dependency lacks an accepted current result")
            result = await _one(conn, "SELECT id,original_digest,contract_revision FROM result_receipts"
                                " WHERE id = ? AND task_id = ?", (row["accepted_result_id"], dependency_id))
            if result is None or result["contract_revision"] != row["contract_revision"]:
                raise ContextUnavailable("an accepted dependency result is missing")
            dependencies.append({"task_id": dependency_id, "title": row["title"],
                                 "contract_revision": row["contract_revision"],
                                 "result_id": result["id"], "result_digest": result["original_digest"]})
            refs.append(f"accepted-result:{dependency_id}@{row['contract_revision']}:{result['id']}#"
                        f"{result['original_digest']}")
            selected_tasks.add(dependency_id)

        relevant_text = [task["title"], str(snapshot.get("acceptance") or ""), role_hint]
        relevant_text.extend(str(item.get("text") or "") for item in requirements
                             if isinstance(item, dict))
        brief = snapshot.get("brief")
        if isinstance(brief, dict):
            relevant_text.extend(str(value) for value in brief.values() if isinstance(value, str))
        terms = _words(" ".join(relevant_text))
        latest = await _many(conn, "SELECT v.* FROM knowledge_fact_versions v JOIN"
                             " (SELECT fact_id,MAX(version) version FROM knowledge_fact_versions"
                             " WHERE project_id = ? GROUP BY fact_id) h"
                             " ON h.fact_id = v.fact_id AND h.version = v.version"
                             " WHERE v.project_id = ? AND v.status = 'promoted'"
                             " ORDER BY v.created_at DESC,v.fact_id LIMIT 100",
                             (task["project_id"], task["project_id"]))
        facts = []
        selected_manifest_ids = set()
        for fact in latest:
            if len(facts) >= MAX_FACTS:
                break
            source_task = None
            if fact["source_kind"] == "manifest":
                manifest = await _one(conn, "SELECT task_id FROM artifact_manifests WHERE id = ?",
                                      (fact["source_id"],))
                source_task = manifest["task_id"] if manifest is not None else None
            if source_task not in selected_tasks and not (_words(fact["claim"]) & terms):
                continue
            try:
                revision, digest = await store._source(conn, task["project_id"],
                                                       fact["source_kind"], fact["source_id"])
            except KnowledgeConflict:
                continue
            if revision != fact["source_revision"] or digest != fact["source_digest"]:
                continue
            facts.append({"fact_id": fact["fact_id"], "version": fact["version"], "kind": fact["kind"],
                          "claim": fact["claim"], "source_kind": fact["source_kind"],
                          "source_id": fact["source_id"], "source_revision": revision,
                          "source_digest": digest})
            refs.append(f"knowledge:{fact['fact_id']}@{fact['version']}#"
                        f"{fact['source_kind']}:{fact['source_id']}@{revision}:{digest}")
            if fact["source_kind"] == "manifest":
                selected_manifest_ids.add(fact["source_id"])

        artifacts = []
        for selected_task in [task_id, *dependency_ids]:
            rows = await _many(conn, "SELECT m.id,m.artifact_key,m.artifact_revision,m.digest,m.artifact_kind"
                               " FROM artifact_manifests m WHERE m.task_id = ?"
                               " AND m.artifact_revision = (SELECT MAX(n.artifact_revision)"
                               " FROM artifact_manifests n WHERE n.task_id = m.task_id"
                               " AND n.artifact_key = m.artifact_key)"
                               " ORDER BY m.created_at DESC,m.id DESC LIMIT ?",
                               (selected_task, MAX_ARTIFACTS))
            for row in rows:
                if len(artifacts) >= MAX_ARTIFACTS:
                    break
                artifacts.append({"id": row["id"], "task_id": selected_task, "key": row["artifact_key"],
                                  "revision": row["artifact_revision"], "kind": row["artifact_kind"],
                                  "digest": row["digest"]})
            if len(artifacts) >= MAX_ARTIFACTS:
                break
        for manifest_id in sorted(selected_manifest_ids):
            if len(artifacts) >= MAX_ARTIFACTS or any(item["id"] == manifest_id for item in artifacts):
                continue
            row = await _one(conn, "SELECT m.id,m.task_id,m.artifact_key,m.artifact_revision,m.digest,m.artifact_kind"
                             " FROM artifact_manifests m WHERE m.id = ? AND m.task_id IS NULL"
                             " AND m.project_id = ?", (manifest_id, task["project_id"]))
            if row is not None:
                artifacts.append({"id": row["id"], "task_id": None, "key": row["artifact_key"],
                                  "revision": row["artifact_revision"], "kind": row["artifact_kind"],
                                  "digest": row["digest"]})
        refs.extend(f"manifest:{item['id']}@{item['revision']}#{item['digest']}" for item in artifacts)
        body = {"task_id": task_id, "role": role, "role_hint": role_hint, "title": task["title"],
                "contract_revision": task["contract_revision"], "contract": snapshot,
                "dependencies": dependencies, "facts": facts, "artifacts": artifacts,
                "source_refs": refs}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":")).encode()).hexdigest()
        packet = {**body, "packet_hash": "sha256:" + digest}
        if len(render_task_context(packet)) > MAX_PACKET_CHARS:
            raise ContextUnavailable("the complete task context exceeds the launch context limit")
        return packet
