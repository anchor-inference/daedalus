"""The preview and launch packet share exact scoped, fresh source identities."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI

from daedalus.extensions import api_knowledge
from daedalus.extensions.task_context import ContextUnavailable, assemble_task_context, render_task_context
from daedalus.stores.database import Database
from daedalus.stores.knowledge import KnowledgeStore
from daedalus.stores.staff_context import pin_staff_context, staff_context_packet


async def task(db: Database, task_id: str, project_id: str, title: str, *, depends_on: list[str] | None = None) -> None:
    snapshot = {"requirements": [{"id": "boundary", "text": "Keep the approved menu constraint"}],
                "checklist": [], "acceptance": "Review the menu", "depends_on": depends_on or [],
                "brief": {"boundaries": "Never change the approved menu currency"}}
    await db.execute("INSERT INTO board_tasks(id,project_id,title,status,priority,acceptance,checklist,depends_on,"
                     "created_at,updated_at,brief_json) VALUES (?,?,?,'todo',3,'','[]',?,'now','now','{}')",
                     (task_id, project_id, title, json.dumps(depends_on or [])))
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                     "snapshot_json,created_at) VALUES (?,1,'operator','',?,'now')",
                     (task_id, json.dumps(snapshot)))
    for dependency_id in depends_on or []:
        await db.execute("INSERT INTO task_dependency_edges(id,successor_task_id,predecessor_task_id,"
                         "kind,resolution_state,created_at) VALUES (?, ?, ?, 'required','awaiting_result','now')",
                         (f"edge:{task_id}:{dependency_id}", task_id, dependency_id))


async def test_context_restricts_scope_relevance_and_freshness(db: Database) -> None:
    for project_id in ("menu-project", "other-project"):
        await db.execute("INSERT INTO projects(id,name,created_at) VALUES (?,?,'now')",
                         (project_id, project_id))
    await task(db, "menu-task", "menu-project", "Prepare the menu")
    await task(db, "other-task", "other-project", "Unrelated archive")
    for file_id, scope in (("111111111111", "menu-project"), ("222222222222", "other-project")):
        await db.execute("INSERT INTO files(id,name,mime,size,sha256,origin,created_at)"
                         " VALUES (?,'proof.txt','text/plain',1,?,'operator','now')",
                         (file_id, "a" * 64 if scope == "menu-project" else "b" * 64))
        await db.execute("INSERT INTO file_access(file_id,scope,added_at) VALUES (?,?,'now')",
                         (file_id, scope))
    store = KnowledgeStore(db)
    relevant = await store.candidate("menu-project", "The menu must keep its approved currency",
                                     source_kind="file", source_id="111111111111", actor="extractor")
    irrelevant = await store.candidate("menu-project", "Payroll retention follows another rule",
                                       source_kind="file", source_id="111111111111", actor="extractor")
    cross_project = await store.candidate("other-project", "The menu must use a foreign project fact",
                                          source_kind="file", source_id="222222222222", actor="extractor")
    for fact_id, project_id in ((relevant["fact_id"], "menu-project"),
                                (irrelevant["fact_id"], "menu-project"),
                                (cross_project["fact_id"], "other-project")):
        await store.review(fact_id, project_id, expected_version=1, verdict="promote",
                           actor="operator", reason="checked")
    await db.execute("INSERT INTO artifact_manifests(id,project_id,artifact_kind,artifact_key,"
                     "artifact_revision,digest,size_bytes,created_at)"
                     " VALUES ('archive','menu-project','document','old-archive',1,?,1,'now')", ("c" * 64,))
    await db.execute("INSERT INTO artifact_manifests(id,task_id,artifact_kind,artifact_key,"
                     "artifact_revision,digest,size_bytes,created_at)"
                     " VALUES ('menu-source','menu-task','document','menu',1,?,1,'now')", ("d" * 64,))
    packet = await assemble_task_context(db, "menu-task", role="worker", role_hint="Menu reviewer")
    assert [item["fact_id"] for item in packet["facts"]] == [relevant["fact_id"]]
    assert [item["id"] for item in packet["artifacts"]] == ["menu-source"]
    assert "approved menu constraint" in render_task_context(packet)
    assert all("other-project" not in ref for ref in packet["source_refs"])
    await db.execute("DELETE FROM file_access WHERE file_id = '111111111111'")
    stale = await assemble_task_context(db, "menu-task", role="worker", role_hint="Menu reviewer")
    assert stale["facts"] == [] and stale["packet_hash"] != packet["packet_hash"]


async def test_dependency_requires_accepted_current_result_and_pins_digest(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now')")
    await task(db, "source-task", "project", "Approved source")
    await task(db, "worker-task", "project", "Use approved source", depends_on=["source-task"])
    with pytest.raises(ContextUnavailable, match="required dependency"):
        await assemble_task_context(db, "worker-task")
    digest = hashlib.sha256(b"Approved source").hexdigest()
    await db.execute("INSERT INTO result_receipts(id,task_id,contract_revision,outcome,original_text,"
                     "original_digest,original_size_bytes,actor_id,created_at)"
                     " VALUES ('result','source-task',1,'complete','Approved source',?,15,'worker','now')",
                     (digest,))
    await db.execute("UPDATE board_tasks SET status='done',accepted_result_id='result',"
                     "accepted_contract_revision=1 WHERE id='source-task'")
    packet = await assemble_task_context(db, "worker-task")
    assert packet["dependencies"][0]["result_digest"] == digest
    assert any(ref.startswith("accepted-result:source-task@1:result#") for ref in packet["source_refs"])
    await db.execute("UPDATE board_tasks SET accepted_result_id=NULL WHERE id='source-task'")
    with pytest.raises(ContextUnavailable, match="required dependency"):
        await assemble_task_context(db, "worker-task")


async def test_pinned_packet_is_immutable_and_task_bound(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now')")
    await task(db, "task", "project", "Review menu")
    await db.execute("INSERT INTO staff(id,project_id,name,color,role,harness,agent,model,effort,"
                     "permission_mode,env,default_folder_id,isolation,instructions,notes,one_off,created_by,created_at)"
                     " VALUES ('member','project','Worker','#fff','Menu reviewer','daedalus','','','','','',NULL,"
                     "'readonly','','',0,'operator','now')")
    await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status,status_at,started_at)"
                     " VALUES ('session','member','daedalus','task','starting','now','now')")
    packet = await assemble_task_context(db, "task", role_hint="Menu reviewer")
    await pin_staff_context(db, "session", packet, role_hint="Menu reviewer")
    assert (await staff_context_packet(db, "session"))["packet_hash"] == packet["packet_hash"]

    class TeamAuth:
        async def authenticate(self, session_id: str, token: str):
            if session_id != "session" or token != "valid-token":
                raise PermissionError("wrong team token")
            return SimpleNamespace(session=SimpleNamespace(task_id="task"))

    api = FastAPI()
    api_knowledge.register(api, SimpleNamespace(db=db, extensions={"staff": TeamAuth()}),
                           lambda: {"via": "token", "user_id": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://127.0.0.1") as client:
        preview = await client.get("/api/board/task/context", params={"staff_id": "member"})
        assert preview.status_code == 200 and preview.json()["packet_hash"] == packet["packet_hash"]
        denied = await client.get("/api/team/session/context")
        assert denied.status_code == 401
        first = await client.get("/api/team/session/context", headers={"x-daedalus-team-token": "valid-token"})
        assert first.status_code == 200 and first.json()["source_current"]
        assert first.json()["packet"] == packet
        await db.execute("UPDATE board_tasks SET title='Changed menu' WHERE id='task'")
        stale = await client.get("/api/team/session/context", headers={"x-daedalus-team-token": "valid-token"})
        assert stale.status_code == 200 and not stale.json()["source_current"]
        assert stale.json()["packet_hash"] == packet["packet_hash"]
        assert stale.json()["current_packet_hash"] != packet["packet_hash"]
        history = await client.get("/api/board/task/context-history")
        assert history.status_code == 200
        assert len(history.json()["entries"]) == 1
        assert not history.json()["entries"][0]["source_current"]
        assert history.json()["entries"][0]["source_refs"] == packet["source_refs"]
        detail = await client.get("/api/board/task/context-history/session")
        assert detail.status_code == 200 and detail.json()["packet"] == packet
        foreign = await client.get("/api/board/another/context-history/session")
        assert foreign.status_code == 404
    with pytest.raises(aiosqlite.IntegrityError):
        await pin_staff_context(db, "session", packet, role_hint="Menu reviewer")
    current = await assemble_task_context(db, "task", role_hint="Menu reviewer")
    assert current["packet_hash"] != packet["packet_hash"]
