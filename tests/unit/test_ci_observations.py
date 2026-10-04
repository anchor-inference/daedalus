"""Signed check facts stay bound to the exact repository, head and run order."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from daedalus.extensions.api_ci import install_routes
from daedalus.extensions.ci_observations import (
    ci_readiness,
    observation_from_webhook,
    record_observation,
    record_signed_delivery,
    set_required_checks,
)
from daedalus.extensions.inbound import webhook_facts
from daedalus.extensions.orchestrator_domain import DomainConflict, record_verdict, replace_contract
from daedalus.stores.database import Database


@pytest.fixture
async def ci_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                     "created_at,updated_at,brief_json) VALUES"
                     "('task1','Review','review',3,'','[]','[]','2026-01-01','2026-01-01','{}')")
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                     "snapshot_json,created_at) VALUES ('task1',1,'operator','','{}','2026-01-01')")
    await db.execute("INSERT INTO ci_required_checks(task_id,contract_revision,provider,repository_id,"
                     "check_name,created_at) VALUES ('task1',1,'github','7','unit','2026-01-01')")
    yield db
    await db.close()


def payload(run_id: int, head: str, conclusion: str, *, status: str = "completed") -> dict:
    return {"repository": {"id": 7, "full_name": "someone/example"},
            "check_run": {"id": run_id, "name": "unit", "head_sha": head,
                          "status": status, "conclusion": conclusion}}


async def test_newer_green_run_survives_late_old_failure_and_stale_head_is_unknown(ci_db: Database) -> None:
    head = "a" * 40
    for delivery, run_id, conclusion in (("new", 20, "success"), ("old", 19, "failure")):
        observation = observation_from_webhook("github", "check_run", delivery,
                                                payload(run_id, head, conclusion),
                                                payload_digest=hashlib.sha256(delivery.encode()).hexdigest())
        assert observation is not None
        async with ci_db.transaction() as conn:
            assert (await record_observation(conn, observation))["recorded"] is True
            assert (await record_observation(conn, observation))["recorded"] is False
    async with ci_db.transaction() as conn:
        current = await ci_readiness(conn, "task1", 1, head)
        stale = await ci_readiness(conn, "task1", 1, "b" * 40)
    assert current["state"] == "passed" and current["checks"][0]["delivery_id"] == "new"
    assert stale["state"] == "blocked" and stale["checks"][0]["state"] == "unknown"


async def test_branch_policy_absence_blocks(ci_db: Database) -> None:
    async with ci_db.transaction() as conn:
        await conn.execute("UPDATE board_tasks SET contract_revision = 2 WHERE id = 'task1'")
        readiness = await ci_readiness(conn, "task1", 2, "a" * 40, require_policy=True)
    assert readiness == {"state": "blocked", "checks": []}


def test_non_github_delivery_cannot_become_a_required_check() -> None:
    body = payload(1, "a" * 40, "success")
    assert observation_from_webhook("other", "check_run", "delivery", body,
                                    payload_digest=hashlib.sha256(b"delivery").hexdigest()) is None


async def test_cancelled_and_missing_identity_never_pass(ci_db: Database) -> None:
    head = "c" * 40
    body = payload(21, head, "cancelled")
    facts = webhook_facts("check_run", body)
    assert facts["repository_id"] == "7" and facts["head_sha"] == head
    assert facts["run_id"] == "21" and facts["check_name"] == "unit"
    observation = observation_from_webhook("github", "check_run", "cancelled", body,
                                            payload_digest=hashlib.sha256(b"cancelled").hexdigest())
    assert observation is not None
    async with ci_db.transaction() as conn:
        await record_observation(conn, observation)
        readiness = await ci_readiness(conn, "task1", 1, head)
    assert readiness["state"] == "blocked" and readiness["checks"][0]["state"] == "cancelled"
    assert observation_from_webhook("github", "check_run", "missing", payload(22, "bad", "success"),
                                    payload_digest=hashlib.sha256(b"missing").hexdigest()) is None
    status = {"repository": {"id": 7}, "id": 31, "sha": head, "context": "unit", "state": "success"}
    status_proof = observation_from_webhook("github", "status", "status31", status,
                                             payload_digest=hashlib.sha256(b"status31").hexdigest())
    assert status_proof is not None and status_proof["conclusion"] == "passed"


async def test_approving_verdict_requires_current_head_check(ci_db: Database) -> None:
    head = "a" * 40
    observation = observation_from_webhook("github", "check_run", "green", payload(20, head, "success"),
                                            payload_digest=hashlib.sha256(b"green").hexdigest())
    assert observation is not None
    async with ci_db.transaction() as conn:
        await record_observation(conn, observation)
        await conn.execute("INSERT INTO result_receipts(id,task_id,contract_revision,outcome,original_text,"
                           "original_digest,original_size_bytes,actor_id,created_at) VALUES"
                           "('result1','task1',1,'complete','Report',?,6,'worker','2026-01-01')",
                           (hashlib.sha256(b"Report").hexdigest(),))
        await conn.execute("INSERT INTO review_evidence(id,result_id,contract_revision,criterion_id,command,"
                           "exit_code,manifest_digest_before,manifest_digest_after,observed_at) VALUES"
                           "('evidence1','result1',1,'C1','check',0,'digest','digest','2026-01-01')")
        with pytest.raises(DomainConflict, match="required CI"):
            await record_verdict(conn, verdict_id="verdict1", result_id="result1", reviewer_actor_id="reviewer",
                                 verification="verified", accepted=True, head="b" * 40, base="c" * 40,
                                 environment_digest=None, evidence_ids=["evidence1"], reason="checked")
        verdict = await record_verdict(conn, verdict_id="verdict1", result_id="result1",
                                       reviewer_actor_id="reviewer", verification="verified", accepted=True,
                                       head=head, base="c" * 40, environment_digest=None,
                                       evidence_ids=["evidence1"], reason="checked")
    assert verdict["accepted"] is True


async def test_signed_delivery_rolls_back_dedupe_if_event_persistence_fails(ci_db: Database) -> None:
    class Bus:
        fail = True
        announced: list[int] = []

        @asynccontextmanager
        async def transaction_guard(self):
            yield

        async def persist_in(self, conn, event_type, facts):
            if self.fail:
                raise RuntimeError("event storage failed")
            cursor = await conn.execute("INSERT INTO app_events(at,type,payload_json) VALUES"
                                        "('2026-01-01',?, '{}')", (event_type,))
            return SimpleNamespace(seq=cursor.lastrowid, payload=facts)

        def announce_committed(self, event):
            self.announced.append(event.seq)

    class Inbound:
        async def record_delivery_in(self, conn, provider, delivery_id):
            cursor = await conn.execute("INSERT OR IGNORE INTO webhook_deliveries(provider,delivery_id,at)"
                                        " VALUES (?,?,'2026-01-01')", (provider, delivery_id))
            return bool(cursor.rowcount)

    bus = Bus()
    body = payload(30, "a" * 40, "success")
    args = {"provider": "github", "event": "check_run", "delivery_id": "delivery30",
            "payload": body, "payload_digest": hashlib.sha256(b"delivery30").hexdigest(),
            "summary": "check completed"}
    with pytest.raises(RuntimeError, match="event storage failed"):
        await record_signed_delivery(ci_db, bus, Inbound(), **args)
    assert await ci_db.fetchall("SELECT * FROM webhook_deliveries WHERE delivery_id = 'delivery30'") == []
    bus.fail = False
    result = await record_signed_delivery(ci_db, bus, Inbound(), **args)
    assert result["fresh"] is True and result["observation_id"] and bus.announced == [result["event_seq"]]
    assert (await record_signed_delivery(ci_db, bus, Inbound(), **args))["fresh"] is False
    assert len(await ci_db.fetchall("SELECT id FROM ci_observations")) == 1


async def test_required_checks_revision_survives_later_contract_change(ci_db: Database) -> None:
    await ci_db.execute("UPDATE board_tasks SET status = 'todo' WHERE id = 'task1'")
    async with ci_db.transaction() as conn:
        revised = await set_required_checks(conn, task_id="task1", provider="github",
                                            repository_id="7", check_names=["unit", "build"],
                                            origin_ref="delivery1")
        assert revised["contract_revision"] == 2
        subsequent = await replace_contract(conn, task_id="task1", requirements=[], checks=[],
                                            acceptance="New criterion", brief={}, origin_kind="operator",
                                            origin_ref="message2", change_kind="semantic")
    assert subsequent["contract_revision"] == 3
    rows = await ci_db.fetchall("SELECT check_name FROM ci_required_checks WHERE task_id = 'task1'"
                                " AND contract_revision = 3 ORDER BY check_name")
    assert [row["check_name"] for row in rows] == ["build", "unit"]


async def test_review_without_verdict_can_set_policy_only_by_returning_result(ci_db: Database) -> None:
    await ci_db.execute("UPDATE board_tasks SET acceptance_state = 'handed_in' WHERE id = 'task1'")
    async with ci_db.transaction() as conn:
        revised = await set_required_checks(conn, task_id="task1", provider="github",
                                            repository_id="7", check_names=["unit", "build"],
                                            origin_ref="receipt")
    assert revised["returned_from_review"] is True
    assert revised["contract_revision"] == 2
    task = await ci_db.fetchone("SELECT status,contract_revision,acceptance_state,current_attempt_id"
                                " FROM board_tasks WHERE id = 'task1'")
    assert (task["status"], task["contract_revision"], task["acceptance_state"],
            task["current_attempt_id"]) == ("todo", 2, "returned", None)
    assert (await ci_db.fetchone("SELECT COUNT(*) FROM task_contract_versions"
                                 " WHERE task_id = 'task1'"))[0] == 2


async def test_policy_change_does_not_return_finished_review(ci_db: Database) -> None:
    await ci_db.execute("UPDATE board_tasks SET status = 'done' WHERE id = 'task1'")
    async with ci_db.transaction() as conn:
        with pytest.raises(DomainConflict, match="reopen the task"):
            await set_required_checks(conn, task_id="task1", provider="github",
                                      repository_id="7", check_names=["unit", "build"],
                                      origin_ref="receipt")


async def test_final_result_survives_late_running_but_conflicting_finals_are_unknown(ci_db: Database) -> None:
    head = "d" * 40
    for delivery, status, conclusion in (("final", "completed", "success"),
                                          ("late-running", "in_progress", None)):
        observation = observation_from_webhook(
            "github", "check_run", delivery, payload(40, head, conclusion, status=status),
            payload_digest=hashlib.sha256(delivery.encode()).hexdigest(),
        )
        async with ci_db.transaction() as conn:
            await record_observation(conn, observation)
    async with ci_db.transaction() as conn:
        assert (await ci_readiness(conn, "task1", 1, head))["state"] == "passed"
        conflict = observation_from_webhook(
            "github", "check_run", "conflict", payload(40, head, "failure"),
            payload_digest=hashlib.sha256(b"conflict").hexdigest(),
        )
        await record_observation(conn, conflict)
        state = await ci_readiness(conn, "task1", 1, head)
    assert state["state"] == "blocked" and state["checks"][0]["state"] == "unknown"


@pytest.mark.parametrize("identity", ["repository", "run", "attempt"])
@pytest.mark.parametrize("bad", [True, False, 0, -1, 2**63])
def test_invalid_numeric_provider_identity_cannot_become_a_check(identity: str, bad: int) -> None:
    body = payload(41, "e" * 40, "success")
    if identity == "repository":
        body["repository"]["id"] = bad
    else:
        body["check_run"]["id" if identity == "run" else "run_attempt"] = bad
    assert observation_from_webhook("github", "check_run", "bad", body,
                                    payload_digest=hashlib.sha256(b"bad").hexdigest()) is None


async def test_required_checks_http_command_replays_and_rejects_stale_or_coerced_input(ci_db: Database) -> None:
    await ci_db.execute("UPDATE board_tasks SET status = 'todo' WHERE id = 'task1'")
    api = FastAPI()
    install_routes(api, SimpleNamespace(db=ci_db), lambda: {"via": "cookie", "user_id": 1})
    revision = (await ci_db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'task1'"))[0]
    body = {"client_operation_id": "set-ci", "expected_entity_revision": revision,
            "provider": "github", "repository_id": "7", "check_names": ["unit", "build"]}
    path = "/api/board/task1/ci/requirements"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        assert (await client.post(path, json={**body, "expected_entity_revision": True})).status_code == 422
        assert (await client.post(path, json={**body, "repository_id": 7})).status_code == 422
        assert (await client.post(path, json={**body, "check_names": []})).status_code == 422
        assert (await client.post(path, json={**body, "ignored": "extra"})).status_code == 422
        first = await client.post(path, json=body)
        assert first.status_code == 200, first.text
        assert first.json()["contract_revision"] == 2
        assert (await client.post(path, json=body)).json() == first.json()
        assert (await client.post(path, json={**body, "client_operation_id": "stale"})).status_code == 409
        assert (await client.post("/api/board/missing/ci/requirements", json=body)).status_code == 404
        state = (await client.get("/api/board/task1/ci", params={"head_sha": "f" * 40})).json()
        assert state["state"] == "blocked" and len(state["checks"]) == 2


async def test_ci_endpoint_uses_the_current_branch_head(ci_db: Database) -> None:
    await ci_db.execute("UPDATE board_tasks SET branch = 'agent/worker/change' WHERE id = 'task1'")

    class Review:
        async def review(self, task_id: str) -> dict:
            assert task_id == "task1"
            return {"head_sha": "a" * 40}

    api = FastAPI()
    app = SimpleNamespace(db=ci_db, extensions={"staff": SimpleNamespace(review=Review())})
    install_routes(api, app, lambda: {"via": "cookie", "user_id": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        stale = await client.get("/api/board/task1/ci", params={"head_sha": "b" * 40})
        assert stale.status_code == 409
        current = (await client.get("/api/board/task1/ci")).json()
        assert current["head_sha"] == "a" * 40 and current["state"] == "blocked"
