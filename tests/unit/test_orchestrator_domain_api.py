"""The public result flow keeps review and operator approval tied to one report."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Header

from daedalus.extensions.api_orchestrator_domain import install_routes
from daedalus.stores.database import Database


@pytest.fixture
async def domain_api(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                     " created_at,updated_at,brief_json) VALUES"
                     " ('task1','Review','review',3,'','[{\"text\":\"Clear result\",\"done\":false}]','[]',"
                     " '2026-01-01','2026-01-01','{}')")
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                     " snapshot_json,created_at) VALUES ('task1',1,'operator','',"
                     " '{\"requirements\":[],\"checklist\":[{\"id\":\"C1\",\"text\":\"Clear result\"}],"
                     "\"acceptance\":\"\",\"depends_on\":[],\"brief\":{}}','2026-01-01')")
    api = FastAPI()

    async def authenticated(x_user: int = Header(1)) -> dict[str, int | str]:
        return {"via": "token", "user_id": x_user}

    install_routes(api, SimpleNamespace(db=db, extensions={}), authenticated)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield db, client
    await db.close()


async def test_exact_result_flow_over_http(domain_api) -> None:  # type: ignore[no-untyped-def]
    db, client = domain_api
    contract = await client.get("/api/board/task1/contract")
    assert contract.status_code == 200
    assert contract.json()["contract_revision"] == 1
    revision = contract.json()["entity_revision"]
    report = "Full original answer"
    digest = hashlib.sha256(report.encode()).hexdigest()
    await db.execute("INSERT INTO files(id,name,mime,size,sha256,origin,origin_ref,created_at)"
                     " VALUES ('file1','answer.txt','text/plain',?,?, 'operator','','2026-01-01')",
                     (len(report.encode()), digest))
    await db.execute("INSERT INTO task_files(task_id,file_id,added_at) VALUES ('task1','file1','2026-01-01')")
    base = "/api/board/task1"
    artifact = await client.post(base + "/artifacts", json={
        "client_operation_id": "artifact1", "expected_entity_revision": revision,
        "artifact_kind": "document", "artifact_key": "answer", "artifact_revision": 1,
        "digest": digest, "size_bytes": len(report.encode()), "file_id": "file1"})
    assert artifact.status_code == 200, artifact.text
    manifest_id = artifact.json()["id"]
    revision = artifact.json()["entity_revision"]
    result = await client.post(base + "/results", headers={"X-User": "1"}, json={
        "client_operation_id": "result1", "expected_entity_revision": revision,
        "contract_revision": 1, "outcome": "complete", "original_text": report,
        "manifest_ids": [manifest_id], "checks": [], "limitations": []})
    assert result.status_code == 200, result.text
    result_id = result.json()["result_id"]
    revision = result.json()["entity_revision"]
    assert (await client.post(base + f"/results/{result_id}/verdicts", json={
        "client_operation_id": "self1", "expected_entity_revision": revision,
        "verification": "verified", "accepted": True, "evidence_ids": []})).status_code == 409
    assert (await client.post(base + f"/results/{result_id}/evidence", json={
        "client_operation_id": "missing-revision", "criterion_id": "C1",
        "manifest_id": manifest_id, "observation": "Read"})).status_code == 422
    original = await client.get(base + f"/results/{result_id}/original")
    assert original.json()["original_text"] == report
    evidence = await client.post(base + f"/results/{result_id}/evidence", headers={"X-User": "2"}, json={
        "client_operation_id": "evidence1", "expected_entity_revision": revision,
        "criterion_id": "C1", "manifest_id": manifest_id, "observation": "Read the uploaded answer"})
    assert evidence.status_code == 200, evidence.text
    revision = evidence.json()["entity_revision"]
    verdict = await client.post(base + f"/results/{result_id}/verdicts", headers={"X-User": "2"}, json={
        "client_operation_id": "verdict1", "expected_entity_revision": revision,
        "verification": "verified", "accepted": True, "head": None, "base": None,
        "evidence_ids": [evidence.json()["evidence_id"]], "reason": "Checked"})
    assert verdict.status_code == 200, verdict.text
    revision = verdict.json()["entity_revision"]
    projected = (await client.get(base + "/results", headers={"X-User": "2"})).json()
    assert projected[0]["current_result_id"] == result_id
    assert projected[0]["verdict_accepted"] is True
    assert projected[0]["self_review_waiver_required"] is False
    assert projected[0]["acceptance_state"] == "handed_in"
    request = {"client_operation_id": "accept1", "expected_entity_revision": revision,
               "verdict_id": verdict.json()["verdict_id"], "contract_revision": 1}
    accepted = await client.post(base + f"/results/{result_id}/accept", headers={"X-User": "2"}, json=request)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["acceptance_state"] == "operator_approved"
    replay = await client.post(base + f"/results/{result_id}/accept", headers={"X-User": "2"}, json=request)
    assert replay.status_code == 200 and replay.json() == accepted.json()
    assert (await db.fetchone("SELECT status FROM board_tasks WHERE id = 'task1'"))["status"] == "done"
