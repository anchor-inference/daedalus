"""A project cap follows its goal and every priced request without losing uncertain spend."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from protocore.contracts.llm import LLMObservabilityContext, LLMProviderError

from daedalus.config import RuntimeConfig
from daedalus.extensions.api_goal_budget import register
from daedalus.extensions.task_launch import queue_launch
from daedalus.stores.control import ControlConflict, canonical
from daedalus.stores.database import Database
from daedalus.stores.goal_budget import (
    charge_for_session_in,
    goal_constraints_in,
    pin_reservation_in,
    set_budget_in,
    view_in,
)
from daedalus.stores.inference_budget import BudgetRefused, InferenceBudget
from tests.unit.test_inference_admission import answer, endpoint, provider, request
from tests.unit.test_launch_controls import OPERATOR
from tests.unit.test_task_launch import queued_fixture

QUOTE = {"provider_id": "test", "model": "model", "rate": "pinned"}


async def project(db: Database, identity: str = "project") -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES (?,?,?,?)",
                     (identity, "Project", "2026-01-01", "{}"))
    await db.execute("INSERT INTO project_goal_revisions(project_id,goal_revision,body,origin_kind,created_at)"
                     " VALUES (?,1,'A checkable goal','operator','2026-01-01')", (identity,))


async def reserve(db: Database, identity: str, project_id: str = "project", *,
                  amount: int = 70, coordinator: bool = False) -> dict[str, str]:
    async with db.transaction() as conn:
        charge = await charge_for_session_in(conn, "coordinator" if coordinator else "worker")
        assert charge is not None and charge.project_id == project_id
        constraints = await goal_constraints_in(conn, project_id, coordinator=coordinator)
        response = await InferenceBudget(db).reserve_in(
            conn, reservation_id=identity, provider_id="test", model="model",
            session_id="coordinator" if coordinator else "worker", run_id=None,
            request_digest=hashlib.sha256(identity.encode()).hexdigest(), quoted_microusd=amount,
            rate_version=hashlib.sha256(canonical(QUOTE).encode()).hexdigest(), quote=QUOTE,
            constraints=constraints,
        )
        await pin_reservation_in(conn, identity, charge)
        await InferenceBudget(db).start_in(conn, identity)
        return response


async def test_goal_cap_counts_concurrent_held_quotes_and_keeps_its_scope_after_revision(db: Database) -> None:
    await project(db)
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,created_at,last_message_at)"
                     " VALUES ('worker','tenant','project','now','now')")
    async with db.transaction() as conn:
        await set_budget_in(conn, project_id="project", budget_id="budget", expected_goal_revision=1,
                            limit_usd="0.000100", coordination_limit_usd="0.000080")
    outcomes = await asyncio.gather(reserve(db, "first"), reserve(db, "second"), return_exceptions=True)
    assert sum(isinstance(outcome, BudgetRefused) for outcome in outcomes) == 1
    assert (await db.fetchone("SELECT count(*) FROM goal_budget_admissions"))[0] == 1
    async with db.transaction() as conn:
        await InferenceBudget(db).unknown_in(conn, "first", "response lost")
        await conn.execute("INSERT INTO project_goal_revisions(project_id,goal_revision,body,origin_kind,created_at)"
                           " VALUES ('project',2,'Revised goal','operator','now')")
        await conn.execute("UPDATE projects SET goal_revision = 2 WHERE id = 'project'")
        view = await view_in(conn, "project")
        assert view is not None
        assert view["current_goal_revision"] == 2 and view["activated_goal_revision"] == 1
        assert view["total"]["held_usd"] == "0.000070"
        assert view["total"]["uncertain_usd"] == "0.000070"
        assert view["total"]["state"] == "uncertain"
        assert view["total"]["available_usd"] == "0.000030"
    with pytest.raises(BudgetRefused, match="available balance"):
        await reserve(db, "after-revision")


async def test_settled_charge_remains_after_run_and_session_rows_are_removed(db: Database) -> None:
    await project(db)
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,created_at,last_message_at)"
                     " VALUES ('worker','tenant','project','now','now')")
    async with db.transaction() as conn:
        await set_budget_in(conn, project_id="project", budget_id="budget", expected_goal_revision=1,
                            limit_usd="0.000050", coordination_limit_usd="0.000020")
    await reserve(db, "priced", amount=40)
    async with db.transaction() as conn:
        cursor = await conn.execute("INSERT INTO usage_events(at,provider_id,model,purpose,session_id,cost_usd,raw,"
                                    "inference_reservation_id) VALUES ('now','test','model','work','worker',?,?,'priced')",
                                    (0.000025, '{"prompt_tokens":1,"completion_tokens":1}'))
        seq = cursor.lastrowid
        await cursor.close()
        await InferenceBudget(db).settle_in(conn, "priced", seq)
    await db.execute("DELETE FROM sessions WHERE id = 'worker'")
    async with db.transaction() as conn:
        balance = await view_in(conn, "project")
        assert balance is not None
        assert balance["total"]["spent_usd"] == "0.000025"
        assert balance["total"]["held_usd"] == "0.000000"
        assert balance["total"]["available_usd"] == "0.000025"


async def test_coordinator_and_subagent_share_a_smaller_current_office_cap(db: Database) -> None:
    await project(db)
    await db.execute("UPDATE projects SET settings = ? WHERE id = 'project'",
                     (json.dumps({"orchestrator": {"enabled": True, "session_id": "coordinator"}}),))
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,created_at,last_message_at,metadata)"
                     " VALUES ('coordinator','tenant','project','now','now',?)",
                     (json.dumps({"orchestrator_of": "project"}),))
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,created_at,last_message_at,metadata)"
                     " VALUES ('child','tenant','project','now','now',?)",
                     (json.dumps({"subagent_of": "coordinator"}),))
    async with db.transaction() as conn:
        await set_budget_in(conn, project_id="project", budget_id="budget", expected_goal_revision=1,
                            limit_usd="0.000200", coordination_limit_usd="0.000080")
        charge = await charge_for_session_in(conn, "child")
        assert charge is not None and charge.root_session_id == "coordinator" and charge.coordinator
    await reserve(db, "coordinator-first", coordinator=True)
    with pytest.raises(BudgetRefused, match="goalcoord:budget"):
        await reserve(db, "coordinator-second", coordinator=True)
    await db.execute("UPDATE projects SET settings = ? WHERE id = 'project'",
                     (json.dumps({"orchestrator": {"enabled": True, "session_id": "replacement"}}),))
    async with db.transaction() as conn:
        with pytest.raises(BudgetRefused, match="retired or foreign coordinator"):
            await charge_for_session_in(conn, "child")


async def test_activation_refuses_preexisting_unscoped_inflight_project_work(db: Database) -> None:
    await project(db)
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,created_at,last_message_at)"
                     " VALUES ('worker','tenant','project','now','now')")
    async with db.transaction() as conn:
        await InferenceBudget(db).reserve_in(
            conn, reservation_id="older", provider_id="test", model="model", session_id="worker", run_id=None,
            request_digest=hashlib.sha256(b"older").hexdigest(), quoted_microusd=70,
            rate_version=hashlib.sha256(canonical(QUOTE).encode()).hexdigest(), quote=QUOTE, constraints=(),
        )
        await InferenceBudget(db).start_in(conn, "older")
    async with db.transaction() as conn:
        with pytest.raises(BudgetRefused, match="settle or stop"):
            await set_budget_in(conn, project_id="project", budget_id="budget", expected_goal_revision=1,
                                limit_usd="1.000000", coordination_limit_usd="0.500000")
    assert await db.fetchone("SELECT 1 FROM project_goal_budgets") is None


async def test_http_budget_command_replays_and_exposes_exact_balance(db: Database) -> None:
    await project(db)
    app = SimpleNamespace(db=db)
    api = FastAPI()

    async def authenticated() -> dict[str, object]:
        return {"via": "cookie", "user_id": 1}

    register(api, app, authenticated)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        initial = await client.get("/api/projects/project/budget")
        assert initial.status_code == 200 and initial.json()["configured"] is False
        body = {"limit_usd": "2.000000", "coordination_limit_usd": "0.500000",
                "expected_goal_revision": 1, "expected_entity_revision": initial.json()["entity_revision"],
                "client_operation_id": "set-project-budget"}
        created = await client.put("/api/projects/project/budget", json=body)
        assert created.status_code == 200, created.text
        assert created.json()["total"]["available_usd"] == "2.000000"
        assert (await client.put("/api/projects/project/budget", json=body)).json() == created.json()
        stale = await client.put("/api/projects/project/budget", json={**body, "client_operation_id": "stale"})
        assert stale.status_code == 409
        invalid = await client.put("/api/projects/project/budget", json={
            **body, "client_operation_id": "invalid-money", "expected_entity_revision": created.json()["entity_revision"],
            "limit_usd": "0.0000001",
        })
        assert invalid.status_code == 422
    assert (await db.fetchone("SELECT count(*) FROM operation_receipts WHERE operation_kind = 'budget.set'"))[0] == 1


async def test_native_provider_reserves_project_goal_before_transport(db: Database) -> None:
    await project(db)
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,created_at,last_message_at)"
                     " VALUES ('worker','tenant','project','now','now')")
    await db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                     " VALUES ('run','tenant','worker','running','now','now')")
    async with db.transaction() as conn:
        await set_budget_in(conn, project_id="project", budget_id="budget", expected_goal_revision=1,
                            limit_usd="0.000070", coordination_limit_usd="0.000070")
    config = RuntimeConfig()
    config.limits.usd_total = 0
    config.limits.usd_per_run = 0
    manager = SimpleNamespace(db=db, config=config, settings=SimpleNamespace(usd_per_day=0),
                              live_state=lambda _: None, mode_for=lambda _: None)
    sends = []
    adapter = provider(manager, lambda sent: sends.append(sent) or answer(), configured=endpoint())
    observed = request(observability=LLMObservabilityContext(tenant_id="tenant", session_id="worker", run_id="run"))
    try:
        await adapter.complete_text(observed)
        assert (await db.fetchone("SELECT goal_revision,charge_class FROM goal_budget_admissions"))[:] == (1, "work")
        assert (await db.fetchone("SELECT scope_key FROM inference_reservation_scopes"
                                 " WHERE scope_key LIKE 'goal:%'"))[0] == "goal:budget"
        with pytest.raises(LLMProviderError, match="goal:budget"):
            await adapter.complete_text(observed)
        assert len(sends) == 1
    finally:
        await adapter.aclose()


async def test_unpriced_project_model_is_refused_before_provider_transport(db: Database) -> None:
    await project(db)
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,created_at,last_message_at)"
                     " VALUES ('worker','tenant','project','now','now')")
    async with db.transaction() as conn:
        await set_budget_in(conn, project_id="project", budget_id="budget", expected_goal_revision=1,
                            limit_usd="1.000000", coordination_limit_usd="0.100000")
    config = RuntimeConfig()
    config.limits.usd_total = 0
    manager = SimpleNamespace(db=db, config=config, settings=SimpleNamespace(usd_per_day=0),
                              live_state=lambda _: None, mode_for=lambda _: None)
    sends = []
    unpriced = endpoint()
    unpriced.pricing = {}
    adapter = provider(manager, lambda sent: sends.append(sent) or answer(), configured=unpriced)
    try:
        observed = request(observability=LLMObservabilityContext(tenant_id="tenant", session_id="worker"))
        with pytest.raises(LLMProviderError, match="priced, provider-enforced"):
            await adapter.complete_text(observed)
        assert sends == []
        assert (await db.fetchone("SELECT count(*) FROM inference_reservations"))[0] == 0
    finally:
        await adapter.aclose()


async def test_capped_project_refuses_unpriced_cli_before_task_assignment(db: Database) -> None:
    app, _, team, starts, revision = await queued_fixture(db)
    try:
        await db.execute("INSERT INTO project_goal_revisions(project_id,goal_revision,body,origin_kind,created_at)"
                         " VALUES ('project',1,'A checkable goal','operator','now')")
        async with db.transaction() as conn:
            await set_budget_in(conn, project_id="project", budget_id="budget", expected_goal_revision=1,
                                limit_usd="1.000000", coordination_limit_usd="0.500000")
        await db.execute("UPDATE staff SET harness = 'claude' WHERE id = 'worker'")
        with pytest.raises(ControlConflict, match="priced native worker"):
            await queue_launch(app, "task", OPERATOR, staff_id="worker", client_operation_id="cli-launch",
                               expected_entity_revision=revision)
        assert (await db.fetchone("SELECT assignee_staff_id FROM board_tasks WHERE id = 'task'"))[0] is None
        assert (await db.fetchone("SELECT count(*) FROM effect_outbox"))[0] == 0
        assert starts == []
    finally:
        team.queue.close()
        app.executions.release()
