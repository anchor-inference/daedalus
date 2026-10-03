"""Comparison admission either funds both alternatives or leaves neither runnable."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from daedalus.extensions.api_comparisons import install_routes
from daedalus.extensions.comparison_commands import (
    choose_comparison,
    close_comparison,
    comparison_history,
    queue_comparison,
)
from daedalus.extensions.comparison_launch import ComparisonLaunchEffect
from daedalus.extensions.comparison_stop import stop_comparison_slot
from daedalus.extensions.comparisons import ComparisonRefused
from daedalus.extensions.staff import Team
from daedalus.host.events import EventBus
from daedalus.host.launch_queue import LaunchQueue
from daedalus.providers.pricing import ModelPricing
from daedalus.stores.control import ControlConflict, Principal
from daedalus.stores.database import Database
from daedalus.stores.inference_budget import BudgetRefused
from daedalus.stores.outbox import OutboxStore


class PairQueue:
    def __init__(self) -> None:
        self.available = True
        self.wakes: list[str] = []

    @asynccontextmanager
    async def admission_guard(self):
        yield

    async def check_pair_capacity_in(self, _conn, _project_id: str, *, count: int) -> bool:
        assert count == 2
        return self.available

    def pump_soon(self, project_id: str) -> None:
        self.wakes.append(project_id)


class Dispatcher:
    def __init__(self) -> None:
        self.notifications = 0

    def notify(self) -> None:
        self.notifications += 1


@pytest.fixture
async def pair_app(tmp_path: Path):
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project1','Example','2026-01-01','{}')")
    await db.execute("INSERT INTO project_folders(id,project_id,path,env,created_at)"
                     " VALUES ('folder1','project1','/tmp/comparison-source','host','2026-01-01')")
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                     "created_at,updated_at,brief_json,project_id,folder_id) VALUES"
                     "('task1','Compare','todo',3,'','[]','[]','2026-01-01','2026-01-01','{}','project1','folder1')")
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                     "snapshot_json,created_at) VALUES"
                     "('task1',1,'operator','','{\"folder_id\":\"folder1\"}','2026-01-01')")
    for number in (1, 2):
        await db.execute("INSERT INTO staff(id,project_id,name,harness,model,isolation,created_by,created_at)"
                         " VALUES (?, 'project1', ?, 'daedalus', 'standard', 'worktree', 'operator','2026-01-01')",
                         (f"staff{number}", f"Worker {number}"))
    await db.execute("INSERT INTO kv(key,value) VALUES ('execution_host_generation','1')")
    queue = PairQueue()
    dispatcher = Dispatcher()
    endpoint = SimpleNamespace(id="provider", kind="openrouter",
                               pricing={"model": ModelPricing(input=1.0, output=1.0,
                                                               input_limit=1000, limit_source="provider")})
    manager = SimpleNamespace(db=db, bus=EventBus(db),
                              config=SimpleNamespace(presets={"standard": SimpleNamespace(max_output_tokens=100)},
                                                     limits=SimpleNamespace(total_since="", usd_total=0,
                                                                            usd_total_per_provider={})),
                              settings=SimpleNamespace(usd_per_day=0),
                              resolve_model=lambda _overrides: ([(SimpleNamespace(endpoint=endpoint), "model")],
                                                                SimpleNamespace(max_output_tokens=100)))

    async def host(_conn):
        return 1

    app = SimpleNamespace(db=db, manager=manager, executions=SimpleNamespace(_host=host),
                          extensions={"staff": SimpleNamespace(queue=queue), "effects": dispatcher})
    yield app
    await db.close()


def request(*, budget: int = 10_000, second: int = 5_000):
    return dict(task_id="task1", principal=Principal("operator:1", "operator"),
                client_operation_id="compare1", expected_entity_revision=1, contract_revision=1,
                budget_cap_microusd=budget,
                alternatives=({"staff_id": "staff1", "allowance_microusd": 5_000},
                              {"staff_id": "staff2", "allowance_microusd": second}))


async def test_pair_commits_two_funded_intents_and_replays_exact_response(pair_app):
    result = await queue_comparison(pair_app, **request())
    assert result["state"] == "queued"
    assert result["reserved_microusd"] == 10_000
    assert [slot["slot"] for slot in result["slots"]] == [1, 2]
    assert len({slot["slot_id"] for slot in result["slots"]}) == 2
    assert len(await pair_app.db.fetchall("SELECT id FROM comparison_funding_slots")) == 2
    assert len(await pair_app.db.fetchall("SELECT id FROM effect_outbox WHERE kind LIKE 'comparison.launch.%'")) == 2
    assert (await pair_app.db.fetchone("SELECT current_attempt_id,entity_revision FROM board_tasks"
                                      " WHERE id = 'task1'"))["entity_revision"] == 2
    assert await queue_comparison(pair_app, **request()) == result
    assert len(await pair_app.db.fetchall("SELECT id FROM operation_receipts"
                                          " WHERE operation_kind = 'comparison.launch'")) == 1
    events = await pair_app.db.fetchall("SELECT seq,type FROM app_events ORDER BY seq")
    assert [row["type"] for row in events] == ["task.changed"]
    assert pair_app.manager.bus.head == events[-1]["seq"]
    assert pair_app.extensions["effects"].notifications == 2


async def test_pair_refusal_rolls_back_group_funding_and_intents(pair_app):
    with pytest.raises(ValueError, match="within the group cap"):
        await queue_comparison(pair_app, **request(budget=9_000))
    with pytest.raises(Exception, match="allocation cannot cover"):
        await queue_comparison(pair_app, **request(second=1))
    assert await pair_app.db.fetchall("SELECT id FROM comparison_groups") == []
    assert await pair_app.db.fetchall("SELECT id FROM comparison_funding_slots") == []
    assert await pair_app.db.fetchall("SELECT id FROM effect_outbox WHERE kind LIKE 'comparison.launch.%'") == []
    assert await pair_app.db.fetchall("SELECT seq FROM app_events") == []
    assert (await pair_app.db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'task1'"))[0] == 1
    assert pair_app.extensions["effects"].notifications == 0


async def test_pair_requires_capacity_and_current_contract(pair_app):
    pair_app.extensions["staff"].queue.available = False
    with pytest.raises(ComparisonRefused, match="capacity"):
        await queue_comparison(pair_app, **request())
    pair_app.extensions["staff"].queue.available = True
    await pair_app.db.execute("UPDATE board_tasks SET entity_revision = 2 WHERE id = 'task1'")
    with pytest.raises(ControlConflict, match="entity has changed"):
        await queue_comparison(pair_app, **request())
    assert await pair_app.db.fetchall("SELECT id FROM comparison_groups") == []


async def test_pair_refuses_set_aside_task_before_reserving_money(pair_app):
    await pair_app.db.execute("UPDATE board_tasks SET status = 'blocked' WHERE id = 'task1'")
    revision = (await pair_app.db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'task1'"))[0]
    with pytest.raises(ComparisonRefused, match="return the task to todo"):
        await queue_comparison(pair_app, **request() | {"expected_entity_revision": revision})
    assert await pair_app.db.fetchall("SELECT id FROM comparison_funding_slots") == []


async def test_launch_effect_checks_pinned_task_and_rate_before_physical_start(pair_app):
    await queue_comparison(pair_app, **request())
    claim = await OutboxStore(pair_app.db).claim(("comparison.launch.first",))
    assert claim is not None
    effect = ComparisonLaunchEffect(pair_app)
    await effect.validate(claim)
    await pair_app.db.execute("UPDATE board_tasks SET title = 'Changed after reservation' WHERE id = 'task1'")
    with pytest.raises(Exception, match="launch identities changed"):
        await effect.validate(claim)


async def test_command_uses_queue_capacity_on_its_existing_transaction(pair_app):
    async def forbidden(_project_id):
        raise AssertionError("capacity must not open a second database transaction")

    team = object.__new__(Team)

    async def active_in(conn, project_id):
        return await Team._active_in(team, conn, project_id)

    async def concurrency_in(conn, project_id):
        return await Team._concurrency_in(team, conn, project_id)

    async def no_wait(_entry):
        return None

    async def no_launch(_entry):
        raise AssertionError("admission may only commit an outbox intent")

    queue = LaunchQueue(concurrency=forbidden, active=forbidden, ready=no_wait, free=no_wait,
                        launch=no_launch, capacity=lambda: None, stagger=lambda: 0,
                        active_in=active_in, concurrency_in=concurrency_in)
    pair_app.extensions["staff"].queue = queue
    result = await queue_comparison(pair_app, **request())
    assert len(result["slots"]) == 2
    async with pair_app.db.transaction() as conn:
        assert await active_in(conn, "project1") == 2
    queue.close()


async def test_choose_releases_both_verified_allocations_and_replays(pair_app):
    launched = await queue_comparison(pair_app, **request())
    async with pair_app.db.transaction() as conn:
        await conn.execute("UPDATE comparison_groups SET state = 'active',reserved_microusd = 10000 WHERE id = ?",
                           (launched["group_id"],))
        for slot in launched["slots"]:
            number = slot["slot"]
            session_id = f"session{number}"
            path = f"/tmp/comparison-worker-{number}"
            ref = f"provider-{number}"
            run = f"run-{number}"
            attempt_id = slot["attempt_id"]
            await conn.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at,"
                               "folder_id,worktree_path,branch,base_ref,ended_at) VALUES"
                               "(?,?,'daedalus','task1','2026-01-01','2026-01-01','folder1',?,?,?,"
                               "'2026-01-02')", (session_id, slot["staff_id"], path, f"branch-{number}", "base"))
            await conn.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                               "fence_token_hash,state,created_at,updated_at,staff_session_id,runtime_kind,"
                               "provider_session_ref,native_run_id) VALUES"
                               "(?,'task1',1,1,'digest','completed','2026-01-01','2026-01-02',?,'daedalus',?,?)",
                               (attempt_id, session_id, ref, run))
            await conn.execute("UPDATE comparison_funding_slots SET attempt_id = ? WHERE id = ?",
                               (attempt_id, slot["slot_id"]))
            await conn.execute("INSERT INTO comparison_group_attempts(group_id,attempt_id,slot,"
                               "reserved_microusd,worktree_identity) VALUES (?,?,?,?,?)",
                               (launched["group_id"], attempt_id, number, slot["allowance_microusd"],
                                hashlib.sha256(path.encode()).hexdigest()))
            await conn.execute("INSERT INTO runtime_exit_observations(attempt_id,runtime_ref,"
                               "provider_session_ref,staff_session_id,contract_revision,host_generation,"
                               "runtime_kind,observed_status,observed_at) VALUES"
                               "(?,?,?,?,1,1,'daedalus','exited','2026-01-02')",
                               (attempt_id, run, ref, session_id))
            body = f"Result {number}"
            await conn.execute("INSERT INTO result_receipts(id,task_id,contract_revision,attempt_id,"
                               "outcome,original_text,original_digest,original_size_bytes,actor_id,created_at)"
                               " VALUES (?,'task1',1,?,'complete',?,?,?,'worker','2026-01-02')",
                               (f"result{number}", attempt_id, body, hashlib.sha256(body.encode()).hexdigest(),
                                len(body)))
        await conn.execute("INSERT INTO review_verdicts(id,result_id,contract_revision,reviewer_actor_id,"
                           "verification,accepted,head,base,created_at) VALUES"
                           "('verdict1','result1',1,'reviewer','verified',1,'head','base','2026-01-02')")

    command = dict(task_id="task1", group_id=launched["group_id"],
                   principal=Principal("operator:1", "operator"), client_operation_id="choose1",
                   expected_entity_revision=2, result_id="result1", verdict_id="verdict1")
    chosen = await choose_comparison(pair_app, **command)
    assert chosen["state"] == "chosen"
    assert chosen["observed_cost_microusd"] == 0
    assert await choose_comparison(pair_app, **command) == chosen
    slots = await pair_app.db.fetchall("SELECT state FROM comparison_funding_slots ORDER BY slot")
    assert [slot["state"] for slot in slots] == ["released", "released"]
    task = await pair_app.db.fetchone("SELECT current_attempt_id,status,branch,entity_revision"
                                     " FROM board_tasks WHERE id = 'task1'")
    assert tuple(task) == (launched["slots"][0]["attempt_id"], "review", "branch-1", 3)
    events = await pair_app.db.fetchall("SELECT type,payload_json FROM app_events ORDER BY seq")
    assert [row["type"] for row in events] == ["task.changed", "task.changed", "task.moved"]
    assert '"from": "todo"' in events[-1]["payload_json"]
    assert '"to": "review"' in events[-1]["payload_json"]


async def test_close_cancels_only_unclaimed_launches_and_releases_unstarted_pair(pair_app):
    launched = await queue_comparison(pair_app, **request())
    command = dict(task_id="task1", group_id=launched["group_id"],
                   principal=Principal("operator:1", "operator"), client_operation_id="close1",
                   expected_entity_revision=2)
    closed = await close_comparison(pair_app, **command)
    assert closed["state"] == "blocked"
    assert closed["cancelled_launch_ids"] == [slot["effect_id"] for slot in launched["slots"]]
    assert await close_comparison(pair_app, **command) == closed
    assert pair_app.extensions["staff"].queue.wakes == ["project1"]
    assert [row["state"] for row in await pair_app.db.fetchall(
        "SELECT state FROM effect_outbox WHERE kind LIKE 'comparison.launch.%' ORDER BY kind"
    )] == ["cancelled", "cancelled"]
    assert [row["state"] for row in await pair_app.db.fetchall(
        "SELECT state FROM comparison_funding_slots ORDER BY slot"
    )] == ["released", "released"]
    assert [row["type"] for row in await pair_app.db.fetchall(
        "SELECT type FROM app_events ORDER BY seq"
    )] == ["task.changed", "task.changed"]
    await queue_comparison(pair_app, **request() | {"client_operation_id": "compare2",
                                                "expected_entity_revision": 3})
    assert len(await pair_app.db.fetchall("SELECT id FROM comparison_groups")) == 2


async def test_history_pages_latest_state_and_rejects_foreign_cursor(pair_app):
    first = await queue_comparison(pair_app, **request())
    await close_comparison(pair_app, task_id="task1", group_id=first["group_id"],
                           principal=Principal("operator:1", "operator"),
                           client_operation_id="close1", expected_entity_revision=2)
    second = await queue_comparison(pair_app, **request() | {"client_operation_id": "compare2",
                                                  "expected_entity_revision": 3})
    page = await comparison_history(pair_app, task_id="task1", limit=1)
    assert [row["group_id"] for row in page["groups"]] == [second["group_id"]]
    assert page["groups"][0]["state"] == "planned"
    assert [slot["launch_state"] for slot in page["groups"][0]["slots"]] == ["pending", "pending"]
    assert [slot["funding_state"] for slot in page["groups"][0]["slots"]] == ["held", "held"]
    assert page["next_before"] == second["group_id"]
    older = await comparison_history(pair_app, task_id="task1", limit=1, before=page["next_before"])
    assert [row["group_id"] for row in older["groups"]] == [first["group_id"]]
    assert older["groups"][0]["state"] == "blocked"
    assert [slot["launch_state"] for slot in older["groups"][0]["slots"]] == ["cancelled", "cancelled"]
    assert older["next_before"] is None
    with pytest.raises(KeyError):
        await comparison_history(pair_app, task_id="task1", before="another-task-group")


async def test_operator_history_endpoint_paginates_and_validates_cursor(pair_app):
    first = await queue_comparison(pair_app, **request())

    async def auth():
        return {"via": "token", "user_id": 1}

    api = FastAPI()
    install_routes(api, pair_app, auth)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://127.0.0.1") as client:
        response = await client.get("/api/board/task1/comparisons", params={"limit": 1})
        assert response.status_code == 200
        assert response.json()["groups"][0]["group_id"] == first["group_id"]
        assert (await client.get("/api/board/task1/comparisons", params={"limit": 0})).status_code == 422
        assert (await client.get("/api/board/task1/comparisons", params={"before": "foreign"})).status_code == 404


async def test_event_insert_failure_rolls_back_pair_and_receipt(pair_app):
    await pair_app.db.execute("CREATE TRIGGER refuse_comparison_event BEFORE INSERT ON app_events"
                              " BEGIN SELECT RAISE(ABORT,'event refused'); END")
    with pytest.raises(Exception, match="event refused"):
        await queue_comparison(pair_app, **request())
    assert await pair_app.db.fetchall("SELECT id FROM comparison_groups") == []
    assert await pair_app.db.fetchall("SELECT id FROM comparison_funding_slots") == []
    assert await pair_app.db.fetchall("SELECT id FROM effect_outbox") == []
    assert await pair_app.db.fetchall("SELECT id FROM operation_receipts") == []
    assert pair_app.manager.bus.head == 0


async def test_close_keeps_uncertain_launches_and_funding(pair_app):
    launched = await queue_comparison(pair_app, **request())
    await pair_app.db.execute("UPDATE effect_outbox SET state = 'unknown' WHERE id = ?",
                              (launched["slots"][0]["effect_id"],))
    with pytest.raises(ComparisonRefused, match="reconciled"):
        await close_comparison(pair_app, task_id="task1", group_id=launched["group_id"],
                               principal=Principal("operator:1", "operator"),
                               client_operation_id="close1", expected_entity_revision=2)
    assert [row["state"] for row in await pair_app.db.fetchall(
        "SELECT state FROM comparison_funding_slots ORDER BY slot"
    )] == ["held", "held"]
    assert (await pair_app.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?",
                                       (launched["slots"][1]["effect_id"],)))[0] == "pending"


async def test_slot_stop_cancels_only_its_pending_launch_with_receipt(pair_app):
    launched = await queue_comparison(pair_app, **request())
    command = dict(task_id="task1", group_id=launched["group_id"], slot=1,
                   principal=Principal("operator:1", "operator"),
                   client_operation_id="stop-one", expected_entity_revision=2, reason="Pause first worker")
    stopped = await stop_comparison_slot(pair_app, **command)
    assert stopped["state"] == "cancelled_pending"
    assert await stop_comparison_slot(pair_app, **command) == stopped
    assert [row["state"] for row in await pair_app.db.fetchall(
        "SELECT state FROM effect_outbox WHERE kind LIKE 'comparison.launch.%' ORDER BY kind"
    )] == ["cancelled", "pending"]
    assert len(await pair_app.db.fetchall("SELECT id FROM operation_receipts"
                                          " WHERE operation_kind = 'comparison.stop'")) == 1
    assert [row["type"] for row in await pair_app.db.fetchall("SELECT type FROM app_events ORDER BY seq")] == [
        "task.changed", "task.changed",
    ]


async def test_slot_stop_refuses_unbound_uncertain_launch(pair_app):
    launched = await queue_comparison(pair_app, **request())
    await pair_app.db.execute("UPDATE effect_outbox SET state = 'unknown' WHERE id = ?",
                              (launched["slots"][0]["effect_id"],))
    with pytest.raises(ControlConflict, match="reconciled"):
        await stop_comparison_slot(pair_app, task_id="task1", group_id=launched["group_id"], slot=1,
                                   principal=Principal("operator:1", "operator"),
                                   client_operation_id="stop-uncertain", expected_entity_revision=2,
                                   reason="Stop first worker")
    assert await pair_app.db.fetchall("SELECT id FROM operation_receipts"
                                      " WHERE operation_kind = 'comparison.stop'") == []
    assert [row["state"] for row in await pair_app.db.fetchall(
        "SELECT state FROM comparison_funding_slots ORDER BY slot"
    )] == ["held", "held"]


async def test_close_keeps_bound_worker_without_physical_exit(pair_app):
    launched = await queue_comparison(pair_app, **request())
    first = launched["slots"][0]
    await pair_app.db.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                              "fence_token_hash,state,created_at,updated_at) VALUES"
                              "(?,'task1',1,1,'digest','running','2026-01-01','2026-01-01')",
                              (first["attempt_id"],))
    await pair_app.db.execute("UPDATE comparison_funding_slots SET attempt_id = ? WHERE id = ?",
                              (first["attempt_id"], first["slot_id"]))
    with pytest.raises(BudgetRefused, match="physical exit"):
        await close_comparison(pair_app, task_id="task1", group_id=launched["group_id"],
                               principal=Principal("operator:1", "operator"),
                               client_operation_id="close1", expected_entity_revision=2)
    assert [row["state"] for row in await pair_app.db.fetchall(
        "SELECT state FROM comparison_funding_slots ORDER BY slot"
    )] == ["held", "held"]
    assert [row["state"] for row in await pair_app.db.fetchall(
        "SELECT state FROM effect_outbox WHERE kind LIKE 'comparison.launch.%' ORDER BY kind"
    )] == ["pending", "pending"]


async def test_close_releases_trusted_bound_failure_before_provider_boundary(pair_app):
    launched = await queue_comparison(pair_app, **request())
    first = launched["slots"][0]
    await pair_app.db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at,ended_at)"
                              " VALUES ('failed-session','staff1','daedalus','task1','2026-01-01',"
                              "'2026-01-01','2026-01-02')")
    await pair_app.db.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                              "fence_token_hash,state,created_at,updated_at,staff_session_id,runtime_kind)"
                              " VALUES (?,'task1',1,1,'digest','failed','2026-01-01','2026-01-02',"
                              "'failed-session','daedalus')", (first["attempt_id"],))
    await pair_app.db.execute("UPDATE comparison_funding_slots SET attempt_id = ? WHERE id = ?",
                              (first["attempt_id"], first["slot_id"]))
    await pair_app.db.execute("UPDATE effect_outbox SET state = 'failed' WHERE id = ?",
                              (first["effect_id"],))
    closed = await close_comparison(pair_app, task_id="task1", group_id=launched["group_id"],
                                    principal=Principal("operator:1", "operator"),
                                    client_operation_id="close1", expected_entity_revision=2)
    assert closed["state"] == "blocked"
    assert closed["cancelled_launch_ids"] == [launched["slots"][1]["effect_id"]]
    assert [row["state"] for row in await pair_app.db.fetchall(
        "SELECT state FROM comparison_funding_slots ORDER BY slot"
    )] == ["released", "released"]


async def test_close_keeps_failed_worker_after_provider_boundary(pair_app):
    launched = await queue_comparison(pair_app, **request())
    first = launched["slots"][0]
    await pair_app.db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at,ended_at)"
                              " VALUES ('uncertain-session','staff1','daedalus','task1','2026-01-01',"
                              "'2026-01-01','2026-01-02')")
    await pair_app.db.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                              "fence_token_hash,state,created_at,updated_at,staff_session_id,runtime_kind)"
                              " VALUES (?,'task1',1,1,'digest','failed','2026-01-01','2026-01-02',"
                              "'uncertain-session','daedalus')", (first["attempt_id"],))
    await pair_app.db.execute("UPDATE comparison_funding_slots SET attempt_id = ?,launch_started_at = '2026-01-01'"
                              " WHERE id = ?", (first["attempt_id"], first["slot_id"]))
    await pair_app.db.execute("UPDATE effect_outbox SET state = 'failed' WHERE id = ?",
                              (first["effect_id"],))
    with pytest.raises(BudgetRefused, match="physical exit or unstarted boundary"):
        await close_comparison(pair_app, task_id="task1", group_id=launched["group_id"],
                               principal=Principal("operator:1", "operator"),
                               client_operation_id="close1", expected_entity_revision=2)
    assert [row["state"] for row in await pair_app.db.fetchall(
        "SELECT state FROM comparison_funding_slots ORDER BY slot"
    )] == ["held", "held"]
