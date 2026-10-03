"""Exercise operator result decisions through current receipts and the public domain API."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from fastapi import FastAPI

from daedalus.extensions.api_orchestrator_domain import install_routes


def _client(team: Any) -> httpx.AsyncClient:
    api = FastAPI()

    async def authenticated() -> dict[str, Any]:
        return {"via": "token", "user_id": 1}

    install_routes(api, team.app, authenticated)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")


async def accept_branchless_result(team: Any, task_id: str) -> str:
    """Attest an actual attached file against every check, then accept its exact result."""
    db = team.app.db
    task = await db.fetchone("SELECT entity_revision,contract_revision,branch FROM board_tasks WHERE id = ?",
                             (task_id,))
    assert task is not None and not task["branch"], "the branch must use a reviewed merge receipt"
    result = await db.fetchone("SELECT id FROM result_receipts WHERE task_id = ?"
                               " ORDER BY created_at DESC,rowid DESC LIMIT 1", (task_id,))
    assert result is not None, "the worker must submit an immutable result"
    result_id = result["id"]
    manifest = await db.fetchone(
        "SELECT m.id FROM result_artifacts a JOIN artifact_manifests m ON m.id = a.manifest_id"
        " WHERE a.result_id = ? AND m.file_id IS NOT NULL LIMIT 1", (result_id,))
    assert manifest is not None, "the report needs a real attached file for operator attestation"
    version = await db.fetchone("SELECT snapshot_json FROM task_contract_versions"
                                " WHERE task_id = ? AND contract_revision = ?",
                                (task_id, task["contract_revision"]))
    assert version is not None
    base = f"/api/board/{task_id}/results/{result_id}"
    revision = task["entity_revision"]
    evidence_ids = []
    async with _client(team) as client:
        for criterion in json.loads(version["snapshot_json"])["checklist"]:
            evidence = await client.post(base + "/evidence", json={
                "client_operation_id": f"result-evidence:{uuid.uuid4().hex}",
                "expected_entity_revision": revision, "criterion_id": criterion["id"],
                "manifest_id": manifest["id"], "observation": "The submitted artifact meets this check",
            })
            assert evidence.status_code == 200, evidence.text
            revision = evidence.json()["entity_revision"]
            evidence_ids.append(evidence.json()["evidence_id"])
        verdict = await client.post(base + "/verdicts", json={
            "client_operation_id": f"result-verdict:{uuid.uuid4().hex}",
            "expected_entity_revision": revision, "verification": "verified", "accepted": True,
            "head": None, "base": None, "evidence_ids": evidence_ids,
            "reason": "The submitted result and every acceptance check were verified",
        })
        assert verdict.status_code == 200, verdict.text
        accepted = await client.post(base + "/accept", json={
            "client_operation_id": f"result-accept:{uuid.uuid4().hex}",
            "expected_entity_revision": verdict.json()["entity_revision"],
            "verdict_id": verdict.json()["verdict_id"],
            "contract_revision": task["contract_revision"],
        })
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["result_id"] == result_id
    return result_id


async def return_reviewed_result(team: Any, task_id: str, *, reason: str) -> str:
    """Reject and return the current immutable report without claiming verification."""
    db = team.app.db
    result = await db.fetchone("SELECT id,contract_revision FROM result_receipts"
                               " WHERE task_id = ? ORDER BY created_at DESC,rowid DESC LIMIT 1", (task_id,))
    task = await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = ?", (task_id,))
    assert result is not None and task is not None
    base = f"/api/board/{task_id}/results/{result['id']}"
    async with _client(team) as client:
        verdict = await client.post(base + "/verdicts", json={
            "client_operation_id": f"result-return-verdict:{uuid.uuid4().hex}",
            "expected_entity_revision": task["entity_revision"],
            "verification": "failed", "accepted": False, "head": None, "base": None,
            "evidence_ids": [], "reason": reason,
        })
        assert verdict.status_code == 200, verdict.text
        returned = await client.post(base + "/return", json={
            "client_operation_id": f"result-return:{uuid.uuid4().hex}",
            "expected_entity_revision": verdict.json()["entity_revision"],
            "verdict_id": verdict.json()["verdict_id"],
            "contract_revision": result["contract_revision"], "reason": reason,
        })
        assert returned.status_code == 200, returned.text
        assert returned.json()["result_id"] == result["id"]
    return result["id"]


__all__ = ["accept_branchless_result", "return_reviewed_result"]
