"""Bounded comparisons retain uncertainty until both alternatives have evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from daedalus.extensions.comparisons import (
    AdmissionProof,
    ComparisonRefused,
    admit_attempt,
    comparison_readiness,
    create_group,
)
from daedalus.extensions.orchestrator_domain import record_verdict, submit_result
from daedalus.stores.database import Database


@pytest.fixture
async def comparison_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                     "created_at,updated_at,brief_json) VALUES"
                     "('task1','Compare','todo',3,'','[]','[]','2026-01-01','2026-01-01','{}')")
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                     "snapshot_json,created_at) VALUES ('task1',1,'operator','','{}','2026-01-01')")
    yield db
    await db.close()


async def test_group_requires_priced_cap_and_exact_contract(comparison_db: Database) -> None:
    async with comparison_db.transaction() as conn:
        with pytest.raises(ComparisonRefused, match="positive priced"):
            await create_group(conn, group_id="group1", task_id="task1", contract_revision=1,
                               budget_cap_microusd=0, actor_id="operator")
        with pytest.raises(ComparisonRefused, match="current immutable contract"):
            await create_group(conn, group_id="group1", task_id="task1", contract_revision=2,
                               budget_cap_microusd=1000, actor_id="operator")
        created = await create_group(conn, group_id="group1", task_id="task1", contract_revision=1,
                                     budget_cap_microusd=1000, actor_id="operator")
    assert created["state"] == "planned"
    with pytest.raises(ComparisonRefused, match="already has"):
        async with comparison_db.transaction() as conn:
            await create_group(conn, group_id="group2", task_id="task1", contract_revision=1,
                               budget_cap_microusd=1000, actor_id="operator")


async def test_group_without_two_proven_results_stays_blocked(comparison_db: Database) -> None:
    async with comparison_db.transaction() as conn:
        await create_group(conn, group_id="group1", task_id="task1", contract_revision=1,
                           budget_cap_microusd=1000, actor_id="operator")
        readiness = await comparison_readiness(conn, "group1", observed_cost=lambda _conn, _attempt: None,
                                               physical_exit=lambda _conn, _attempt: False)
    assert readiness["state"] == "planned"
    assert readiness["blockers"] == ["alternatives_missing"]
    assert readiness["selected_result_id"] is None
    assert await comparison_db.fetchall("PRAGMA foreign_key_check") == []


async def test_alternative_result_does_not_become_the_task_candidate(comparison_db: Database) -> None:
    async with comparison_db.transaction() as conn:
        await create_group(conn, group_id="group1", task_id="task1", contract_revision=1,
                           budget_cap_microusd=1000, actor_id="operator")
        await conn.execute("UPDATE comparison_groups SET state = 'active' WHERE id = 'group1'")
        await conn.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                           "fence_token_hash,state,created_at,updated_at) VALUES"
                           "('attempt1','task1',1,1,'digest','completed','2026-01-01','2026-01-01')")
        await conn.execute("INSERT INTO comparison_group_attempts(group_id,attempt_id,slot,reserved_microusd)"
                           " VALUES ('group1','attempt1',1,500)")
        original = "First alternative"
        encoded = original.encode()
        response = await submit_result(conn, result_id="result1", task_id="task1", attempt_id="attempt1",
                                       contract_revision=1, outcome="partial", original_text=original,
                                       original_blob_ref=None, original_digest=hashlib.sha256(encoded).hexdigest(),
                                       original_size_bytes=len(encoded), actor_id="worker", manifest_ids=[],
                                       checks=[], limitations=["needs comparison"])
        verdict = await record_verdict(conn, verdict_id="verdict1", result_id="result1",
                                       reviewer_actor_id="reviewer", verification="failed", accepted=False,
                                       head="head1", base="base1", environment_digest=None,
                                       evidence_ids=[], reason="alternative incomplete")
    assert response["acceptance_state"] == "comparison_pending"
    assert verdict["verification"] == "failed"
    task = await comparison_db.fetchone("SELECT current_attempt_id,acceptance_state FROM board_tasks WHERE id = 'task1'")
    assert task is not None and task["current_attempt_id"] is None
    assert task["acceptance_state"] != "handed_in"


async def test_second_reservation_cannot_exceed_cap(comparison_db: Database) -> None:
    await comparison_db.execute("INSERT INTO projects(id,name,created_at,settings)"
                                " VALUES ('project1','Example','2026-01-01','{}')")
    await comparison_db.execute("UPDATE board_tasks SET project_id = 'project1' WHERE id = 'task1'")
    async with comparison_db.transaction() as conn:
        await create_group(conn, group_id="group1", task_id="task1", contract_revision=1,
                           budget_cap_microusd=1000, actor_id="operator")
        for slot in (1, 2):
            staff_id = f"staff{slot}"
            session_id = f"session{slot}"
            attempt_id = f"attempt{slot}"
            await conn.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                               " VALUES (?,?,?,'claude','operator','2026-01-01')",
                               (staff_id, "project1", staff_id))
            await conn.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at,"
                               "worktree_path,branch) VALUES (?,?, 'cli','task1','2026-01-01','2026-01-01',?,?)",
                               (session_id, staff_id, f"/tmp/alternative-{slot}", f"branch-{slot}"))
            await conn.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                               "fence_token_hash,state,created_at,updated_at,staff_session_id)"
                               " VALUES (?,'task1',1,1,'digest','queued','2026-01-01','2026-01-01',?)",
                               (attempt_id, session_id))

        async def budget(_conn, _group, _attempt, _proof) -> bool:
            return True

        async def capacity(_conn, _attempt, _slot) -> bool:
            return True

        def proof(slot: int) -> AdmissionProof:
            return AdmissionProof(f"reservation{slot}", 600, "rate1",
                                  hashlib.sha256(f"/tmp/alternative-{slot}".encode()).hexdigest(),
                                  f"capacity{slot}")

        first = await admit_attempt(conn, group_id="group1", attempt_id="attempt1", slot=1,
                                    proof=proof(1), verify_budget=budget, verify_capacity=capacity)
        assert first["reserved_microusd"] == 600
        with pytest.raises(ComparisonRefused, match="exceed"):
            await admit_attempt(conn, group_id="group1", attempt_id="attempt2", slot=2,
                                proof=proof(2), verify_budget=budget, verify_capacity=capacity)
    assert len(await comparison_db.fetchall("SELECT attempt_id FROM comparison_group_attempts")) == 1
