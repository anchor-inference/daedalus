"""Operator memory pages expose current sources without adopting stale or foreign facts."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from daedalus.extensions.api_knowledge import register
from daedalus.stores.database import Database
from daedalus.stores.knowledge import KnowledgeStore


async def test_memory_page_review_replay_and_source_loss(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now'),('other','Other','now')")
    await db.execute("INSERT INTO files(id,name,mime,size,sha256,origin,created_at)"
                     " VALUES ('proof','proof.txt','text/plain',1,'digest','operator','now'),"
                     " ('foreign','private.txt','text/plain',1,'other','operator','now')")
    await db.execute("INSERT INTO file_access(file_id,scope,added_at) VALUES ('proof','project','now'),('foreign','other','now')")
    api = FastAPI()
    register(api, SimpleNamespace(db=db), lambda: {"via": "cookie", "user_id": 1})
    base = "/api/projects/project/knowledge"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        sources = (await client.get(base + "/sources")).json()
        assert sources == [{"source_id": "proof", "source_kind": "file", "label": "proof.txt"}]
        initial = (await client.get(base)).json()
        assert initial["facts"] == [] and initial["collection_revision"] == 1
        args = {"claim": "The project has an explicitly checked source", "source_kind": "file", "source_id": "proof",
                "expected_collection_revision": initial["collection_revision"], "client_operation_id": "candidate"}
        assert (await client.post(base + "/candidates", json={**args, "expected_collection_revision": True})).status_code == 422
        candidate = await client.post(base + "/candidates", json=args)
        assert candidate.status_code == 200, candidate.text
        assert (await client.post(base + "/candidates", json=args)).json() == candidate.json()
        page = (await client.get(base)).json()
        fact = page["facts"][0]
        assert fact["source_status"] == "current" and fact["status"] == "candidate"
        path = base + f"/{fact['fact_id']}/review"
        for verdict in ("review", "promote"):
            body = {"verdict": verdict, "reason": "Source checked by operator", "expected_version": fact["version"],
                    "expected_entity_revision": page["entity_revision"], "client_operation_id": verdict}
            response = await client.post(path, json=body)
            assert response.status_code == 200, response.text
            assert (await client.post(path, json=body)).json() == response.json()
            page = (await client.get(base)).json()
            fact = page["facts"][0]
        assert fact["status"] == "promoted" and len(await KnowledgeStore(db).context_facts("project")) == 1
        history = (await client.get(base + f"/{fact['fact_id']}/history")).json()
        assert [row["status"] for row in history] == ["candidate", "reviewed", "promoted"]
        empty_history = await client.get(base + f"/{fact['fact_id']}/history", params={"before_version": 1})
        assert empty_history.status_code == 200 and empty_history.json() == []
        assert (await client.get(base + "/missing/history")).status_code == 404
        await db.execute("DELETE FROM file_access WHERE file_id = 'proof'")
        assert (await client.get(base)).json()["facts"][0]["source_status"] == "missing"
        assert await KnowledgeStore(db).context_facts("project") == []
        stale = await client.post(path, json={"verdict": "promote", "reason": "Try obsolete proof",
                                             "expected_version": fact["version"], "expected_entity_revision": page["entity_revision"],
                                             "client_operation_id": "stale-source"})
        assert stale.status_code == 409


async def test_memory_pagination_is_bounded_and_project_scoped(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now'),('other','Other','now')")
    await db.execute("INSERT INTO files(id,name,mime,size,sha256,origin,created_at)"
                     " VALUES ('proof','proof.txt','text/plain',1,'digest','operator','now')")
    await db.execute("INSERT INTO file_access(file_id,scope,added_at) VALUES ('proof','project','now'),('proof','other','now')")
    store = KnowledgeStore(db)
    for index in range(3):
        await store.candidate("project", f"Fact number {index} needs review", kind="fact", source_kind="file", source_id="proof", actor="operator")
    other = await store.candidate("other", "A fact from another project", kind="fact", source_kind="file", source_id="proof", actor="operator")
    api = FastAPI()
    register(api, SimpleNamespace(db=db), lambda: {"via": "cookie", "user_id": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        base = "/api/projects/project/knowledge"
        seen: list[str] = []
        before = None
        while True:
            page = (await client.get(base, params={"limit": 1, **({"before": before} if before else {})})).json()
            assert len(page["facts"]) == 1
            seen.extend(fact["fact_id"] for fact in page["facts"])
            before = page["next_before"]
            if before is None:
                break
        assert len(seen) == len(set(seen)) == 3 and other["fact_id"] not in seen
        assert (await client.get(base, params={"before": other["fact_id"]})).status_code == 409
        assert (await client.get(base, params={"limit": 101})).status_code == 422
