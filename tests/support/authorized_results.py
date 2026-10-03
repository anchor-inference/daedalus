"""Exercise operator result decisions through current receipts and the public domain API."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from fastapi import FastAPI

from daedalus.extensions.api_orchestrator_domain import install_routes


def operator_domain_client(app: Any) -> httpx.AsyncClient:
    api = FastAPI()

    async def authenticated() -> dict[str, Any]:
        return {"via": "token", "user_id": 1}

    install_routes(api, app, authenticated)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")


async def submit_manual_result(app: Any, task_id: str, *, note: str) -> str:
    """Use the authenticated manual-result route without inventing a worker or file."""
    task = await app.db.fetchone("SELECT entity_revision,contract_revision FROM board_tasks WHERE id = ?", (task_id,))
    assert task is not None
    base = f"/api/board/{task_id}"
    async with operator_domain_client(app) as client:
        result = await client.post(base + "/results", json={
            "client_operation_id": f"manual-result:{uuid.uuid4().hex}",
            "expected_entity_revision": task["entity_revision"],
            "contract_revision": task["contract_revision"], "outcome": "complete",
            "original_text": note,
            "manifest_ids": [], "checks": [], "limitations": [],
        })
        assert result.status_code == 200, result.text
        return result.json()["result_id"]


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
    origin = await db.fetchone("SELECT actor_id FROM result_receipts WHERE id = ?", (result_id,))
    assert origin is not None
    manual = origin["actor_id"].startswith("operator:") and manifest is None
    assert manual or manifest is not None, "worker reports need an attached file"
    version = await db.fetchone("SELECT snapshot_json FROM task_contract_versions"
                                " WHERE task_id = ? AND contract_revision = ?",
                                (task_id, task["contract_revision"]))
    assert version is not None
    base = f"/api/board/{task_id}/results/{result_id}"
    revision = task["entity_revision"]
    evidence_ids = []
    async with operator_domain_client(team.app) as client:
        snapshot = json.loads(version["snapshot_json"])
        for criterion in [*snapshot["checklist"], *snapshot["requirements"]] or [{"id": "completion"}]:
            route = "/attest" if manual and not criterion.get("file_id") else "/evidence"
            payload = {
                "client_operation_id": f"result-evidence:{uuid.uuid4().hex}",
                "expected_entity_revision": revision, "criterion_id": criterion["id"],
                "observation": "I checked this part of the reported work",
            }
            if route == "/evidence":
                assert manifest is not None
                payload["manifest_id"] = manifest["id"]
            evidence = await client.post(base + route, json=payload)
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
    async with operator_domain_client(team.app) as client:
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


async def reopen_accepted_result(app: Any, task_id: str, *, reason: str) -> dict[str, Any]:
    """Reopen only the task's current accepted result through its authenticated receipt."""
    task = await app.db.fetchone("SELECT entity_revision,contract_revision,accepted_result_id"
                                 " FROM board_tasks WHERE id = ?", (task_id,))
    assert task is not None and task["accepted_result_id"]
    verdict = await app.db.fetchone("SELECT id FROM review_verdicts WHERE result_id = ?"
                                    " AND accepted = 1 ORDER BY created_at DESC,rowid DESC LIMIT 1",
                                    (task["accepted_result_id"],))
    assert verdict is not None
    async with operator_domain_client(app) as client:
        response = await client.post(
            f"/api/board/{task_id}/results/{task['accepted_result_id']}/reopen",
            json={"client_operation_id": f"result-reopen:{uuid.uuid4().hex}",
                  "expected_entity_revision": task["entity_revision"],
                  "verdict_id": verdict["id"], "contract_revision": task["contract_revision"],
                  "reason": reason},
        )
        assert response.status_code == 200, response.text
        return response.json()


__all__ = ["accept_branchless_result", "operator_domain_client", "return_reviewed_result",
           "reopen_accepted_result", "submit_manual_result"]
