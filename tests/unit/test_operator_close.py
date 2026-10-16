"""The operator closes or removes a card past whatever its review and its worker still hold."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from daedalus.extensions.api_board import register
from daedalus.host.events import EventBus
from daedalus.stores.database import Database


async def card(db: Database, task_id: str, status: str, *, depends_on: str | None = None) -> None:
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,created_at,"
                     " updated_at,project_id,brief_json) VALUES (?,?,?,3,'',?,?,'2026-01-01','2026-01-01','project','{}')",
                     (task_id, f"Card {task_id}", status, json.dumps([{"id": "C1", "text": "Works", "done": False}]),
                      json.dumps([depends_on] if depends_on else [])))
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,snapshot_json,"
                     " created_at) VALUES (?,1,'operator','',?,'2026-01-01')",
                     (task_id, json.dumps({"requirements": [], "checklist": [{"id": "C1", "text": "Works"}], "brief": {}})))
    if depends_on:
        await db.execute("INSERT INTO task_dependency_edges(id,successor_task_id,predecessor_task_id,kind,"
                         " resolution_state,created_at) VALUES (?,?,?,'required','awaiting_result','2026-01-01')",
                         (f"edge-{task_id}", task_id, depends_on))


async def revision(db: Database, task_id: str) -> int:
    return int((await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = ?", (task_id,)))[0])


def client_for(db: Database) -> httpx.AsyncClient:
    api = FastAPI()
    register(api, SimpleNamespace(db=db, manager=SimpleNamespace(bus=EventBus(db)), extensions={}),
             lambda: {"via": "cookie", "user_id": 1})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")


async def test_a_card_in_review_with_a_live_worker_closes_as_done_and_frees_what_waits_on_it(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    await card(db, "deploy", "review")
    await card(db, "announce", "blocked", depends_on="deploy")
    await db.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,fence_token_hash,state,"
                     " created_at,updated_at) VALUES ('attempt','deploy',1,1,'hash','running','2026-01-01','2026-01-01')")
    await db.execute("UPDATE board_tasks SET current_attempt_id = 'attempt', acceptance_state = 'handed_in' WHERE id = 'deploy'")
    await db.execute("INSERT INTO open_loops(project_id,task_id,cause,opened_at) VALUES ('project','deploy','report_done','2026-01-01')")
    async with client_for(db) as client:
        # The ordinary move is still refused: only the operator's close goes past the review.
        moved = await client.put("/api/board/deploy", json={"status": "todo", "client_operation_id": "move",
                                                            "expected_entity_revision": await revision(db, "deploy")})
        assert moved.status_code == 409
        closed = await client.post("/api/board/deploy/close", json={
            "client_operation_id": "close", "expected_entity_revision": await revision(db, "deploy"),
            "status": "done", "reason": "The deploy is live; I checked it myself."})
        assert closed.status_code == 200, closed.text
    task = await db.fetchone("SELECT status,acceptance_state,accepted_result_id,current_attempt_id,notes FROM board_tasks WHERE id='deploy'")
    assert (task["status"], task["acceptance_state"], task["current_attempt_id"]) == ("done", "operator_approved", None)
    assert "closed by the operator as done" in task["notes"]
    result = await db.fetchone("SELECT actor_id,outcome,original_text FROM result_receipts WHERE id = ?", (task["accepted_result_id"],))
    assert result["actor_id"] == "operator:1" and result["outcome"] == "complete"
    assert "I checked it myself" in result["original_text"]
    assert (await db.fetchone("SELECT state FROM execution_attempts WHERE id='attempt'"))[0] == "superseded"
    assert (await db.fetchone("SELECT closed_by FROM open_loops WHERE task_id='deploy'"))[0] == "operator"
    assert (await db.fetchone("SELECT status FROM board_tasks WHERE id='announce'"))[0] == "todo"
    assert (await db.fetchone("PRAGMA foreign_key_check")) is None


async def test_a_handed_in_result_is_what_the_operator_accepts_when_there_is_one(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    await card(db, "answer", "review")
    await db.execute("INSERT INTO result_receipts(id,task_id,contract_revision,outcome,original_text,original_digest,"
                     " original_size_bytes,actor_id,created_at) VALUES ('handed','answer',1,'complete','The answer',"
                     " 'digest',10,'staff:member','2026-01-01')")
    async with client_for(db) as client:
        closed = await client.post("/api/board/answer/close", json={
            "client_operation_id": "close", "expected_entity_revision": await revision(db, "answer"),
            "status": "done", "reason": "Answered."})
        assert closed.status_code == 200, closed.text
        assert closed.json()["command"]["result_id"] == "handed"
        again = await client.post("/api/board/answer/close", json={
            "client_operation_id": "close-again", "expected_entity_revision": await revision(db, "answer"),
            "status": "done", "reason": "Answered."})
        assert again.status_code == 409


async def test_removing_a_card_drops_it_and_takes_it_off_every_list(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    await card(db, "stale", "review")
    await card(db, "kept", "todo")
    async with client_for(db) as client:
        removed = await client.delete("/api/board/stale", params={
            "client_operation_id": "remove", "expected_entity_revision": await revision(db, "stale"),
            "reason": "No longer wanted"})
        assert removed.status_code == 200, removed.text
        assert removed.json()["task"]["status"] == "dropped"
        assert removed.json()["task"]["removed_at"]
        twice = await client.delete("/api/board/stale", params={
            "client_operation_id": "remove-again", "expected_entity_revision": await revision(db, "stale")})
        assert twice.status_code == 409
    # The history stays: the row and its contract are still there, only the lists skip it.
    assert await db.fetchone("SELECT 1 FROM task_contract_versions WHERE task_id = 'stale'")
    from daedalus.extensions.board import Board

    listed = await Board(SimpleNamespace(db=db, manager=None, extensions={})).list(project_id="project")
    assert [task["id"] for task in listed] == ["kept"]


async def test_a_coordinator_drops_a_handed_in_card_without_a_verdict(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    await card(db, "settled", "review")
    await db.execute("INSERT INTO open_loops(project_id,task_id,cause,opened_at) VALUES ('project','settled','report_done','2026-01-01')")
    async with client_for(db) as client:
        dropped = await client.put("/api/board/settled", json={
            "status": "dropped", "client_operation_id": "drop", "expected_entity_revision": await revision(db, "settled")})
        assert dropped.status_code == 200, dropped.text
    assert (await db.fetchone("SELECT status FROM board_tasks WHERE id='settled'"))[0] == "dropped"
    assert (await db.fetchone("SELECT closed_at FROM open_loops WHERE task_id='settled'"))[0] is not None
