"""Committed occurrence identity, interrupted sends and explicit standing approval."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import api_recurring
from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.notifications import NotificationService
from daedalus.extensions.recurring import Recurring, RecurringEffect, repoint_project_schedules_in
from daedalus.extensions.scheduler import Scheduler
from daedalus.host.session_runner import SessionManager
from daedalus.stores.control import ControlDenied, Principal
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.projects import ProjectError


class Front:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail_after_send = False

    async def notify(self, text: str) -> None:
        self.sent.append(text)
        if self.fail_after_send:
            raise RuntimeError("transport reply was lost")

    async def outbox_for_session(self, session_id: str) -> None:
        return None


@pytest.fixture
async def system(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager,
                          front=Front(), extensions={})
    app.notifications = NotificationService(db, manager.bus, front=lambda: None)
    app.extensions["scheduler"] = Scheduler(app)
    app.extensions["recurring"] = Recurring(app)
    dispatcher = EffectDispatcher(OutboxStore(db))
    effect = RecurringEffect(app)
    for kind in ("message", "lazy", "wake", "agent"):
        dispatcher.register(f"schedule.{kind}", effect)
    app.extensions["effects"] = dispatcher
    yield app
    await manager.close()


async def _create(system: Any, key: str = "create") -> dict[str, Any]:
    rev = await system.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind='global' AND scope_id='global'")
    return await system.extensions["recurring"].create(
        Principal("operator:1", "operator"), name="medicine", prompt="take the medicine",
        cron=None, run_at="2026-01-01T00:00:00Z", kind="message", target_session=None,
        project_id=None, expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        expected_collection_revision=rev["revision"], client_operation_id=key,
    )


async def test_cycle_reserves_next_time_and_effect_in_one_commit(system: Any) -> None:
    created = await _create(system)
    cycle = await system.extensions["recurring"].reserve(created["id"])
    assert cycle is not None
    assert (await system.extensions["recurring"].reserve(created["id"])) is None
    row = await system.db.fetchone("SELECT enabled,next_run_at FROM schedules WHERE id = ?", (created["id"],))
    assert row["enabled"] == 0 and row["next_run_at"] is None
    receipt = await system.db.fetchone("SELECT id FROM operation_receipts WHERE id = ?", (cycle["receipt_id"],))
    effect = await system.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (cycle["effect_id"],))
    assert receipt is not None and effect["state"] == "pending"
    assert await system.extensions["effects"].step()
    assert len(system.front.sent) == 1
    row = await system.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (cycle["effect_id"],))
    assert row["state"] == "completed"


async def test_expired_grant_retains_due_occurrence_for_reapproval(system: Any) -> None:
    created = await _create(system)
    row = await system.db.fetchone("SELECT grant_id FROM schedules WHERE id = ?", (created["id"],))
    await system.db.execute("UPDATE actor_grants SET expires_at = ? WHERE id = ?",
                            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), row["grant_id"]))
    assert await system.extensions["recurring"].reserve(created["id"]) is None
    row = await system.db.fetchone("SELECT authority_state,next_run_at FROM schedules WHERE id = ?", (created["id"],))
    assert row["authority_state"] == "needs_approval" and row["next_run_at"] is not None
    assert await system.db.fetchone("SELECT id FROM recurring_cycles WHERE schedule_id = ?", (created["id"],)) is None
    rev = await system.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind='global' AND scope_id='global'")
    approved = await system.extensions["recurring"].approve(
        Principal("operator:1", "operator"), created["id"],
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        expected_collection_revision=rev["revision"], expected_schedule_revision=1,
        client_operation_id="renew",
    )
    assert approved["authority_state"] == "current"
    assert await system.extensions["recurring"].reserve(created["id"]) is not None


async def test_expiry_after_reservation_reopens_same_unentered_effect(system: Any) -> None:
    created = await _create(system)
    cycle = await system.extensions["recurring"].reserve(created["id"])
    grant = await system.db.fetchone("SELECT grant_id FROM schedules WHERE id = ?", (created["id"],))
    await system.db.execute("UPDATE actor_grants SET expires_at = ? WHERE id = ?",
                            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), grant["grant_id"]))
    assert not await system.extensions["effects"].step()
    before = await system.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (cycle["effect_id"],))
    assert before["state"] == "cancelled" and not system.front.sent
    rev = await system.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind='global' AND scope_id='global'")
    await system.extensions["recurring"].approve(
        Principal("operator:1", "operator"), created["id"],
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        expected_collection_revision=rev["revision"], expected_schedule_revision=1,
        client_operation_id="renew-reserved",
    )
    after = await system.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (cycle["effect_id"],))
    assert after["state"] == "pending"
    assert await system.extensions["effects"].step()
    assert len(system.front.sent) == 1


async def test_interrupted_send_is_unknown_and_never_retried_blindly(system: Any) -> None:
    created = await _create(system)
    cycle = await system.extensions["recurring"].reserve(created["id"])
    system.front.fail_after_send = True
    assert await system.extensions["effects"].step()
    assert len(system.front.sent) == 1
    row = await system.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (cycle["effect_id"],))
    assert row["state"] == "unknown"
    assert await system.extensions["effects"].reconcile() == 0
    assert not await system.extensions["effects"].step()
    assert len(system.front.sent) == 1
    history = await system.extensions["recurring"].cycles(created["id"])
    assert history[0]["effect_state"] == "unknown"


async def test_claimed_before_effect_entry_can_resume_without_duplicate(system: Any) -> None:
    created = await _create(system)
    cycle = await system.extensions["recurring"].reserve(created["id"])
    store = system.extensions["effects"].store
    claim = await store.claim(("schedule.message",))
    assert claim is not None
    assert await store.recover() == 1
    assert await system.extensions["effects"].reconcile() == 0
    row = await system.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (cycle["effect_id"],))
    assert row["state"] == "pending"
    assert await system.extensions["effects"].step()
    assert len(system.front.sent) == 1


@pytest.mark.parametrize("run_in", ["self", "new"])
async def test_started_agent_run_is_reconciled_after_restart_from_its_input_receipt(system: Any,
                                                                                      run_in: str) -> None:
    project = await system.manager.projects.create("Workspace")
    owner = await system.manager.create_session("owner", project_id=project.id)
    rev = await system.db.fetchone("SELECT revision FROM domain_collection_revisions"
                                   " WHERE scope_kind='project' AND scope_id=?", (project.id,))
    created = await system.extensions["recurring"].create(
        Principal("operator:1", "operator"), name="check", prompt="check the board",
        cron="* * * * *", run_at=None, kind="agent", target_session=owner.session.id,
        project_id=project.id, expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        expected_collection_revision=rev["revision"], client_operation_id="agent-create",
    )
    past = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    await system.db.execute("UPDATE schedules SET run_in = ?,next_run_at = ? WHERE id = ?",
                            (run_in, past, created["id"]))
    cycle = await system.extensions["recurring"].reserve(created["id"])
    assert cycle is not None
    store = system.extensions["effects"].store
    claim = await store.claim(("schedule.agent",))
    assert claim is not None
    await system.db.execute("UPDATE recurring_cycles SET action_state = 'dispatching' WHERE id = ?", (cycle["id"],))
    assert await store.recover() == 1
    await system.extensions["scheduler"].restore()
    assert created["id"] not in system.extensions["scheduler"]._active
    assert await system.extensions["effects"].reconcile() == 0
    assert (await store.view(cycle["effect_id"]))["state"] == "unknown"

    run_session = owner if run_in == "self" else await system.manager.create_session(
        "[cron] check", project_id=project.id, metadata={"schedule_cycle_id": cycle["id"]},
    )
    moment = datetime.now(UTC).isoformat()
    await system.db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                            " VALUES (?,?,?,?,?,?)", ("scheduled-run", "daedalus", run_session.session.id,
                                                     "running", moment, moment))
    await system.db.execute(
        "INSERT INTO input_receipts(session_id,client_message_id,kind,content_digest,payload,status,"
        "run_id,step_id,queue_revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_session.session.id, f"schedule-cycle:{cycle['id']}", "input", "digest", '{"origin":"schedule"}',
         "accepted", "scheduled-run", "start", 1, moment, moment),
    )
    assert await system.extensions["effects"].reconcile() == 0
    await system.db.execute("UPDATE input_receipts SET status = 'consumed' WHERE session_id = ?"
                            " AND client_message_id = ?", (run_session.session.id, f"schedule-cycle:{cycle['id']}"))
    assert await system.extensions["effects"].reconcile() == 1
    assert (await store.view(cycle["effect_id"]))["state"] == "completed"
    history = await system.extensions["recurring"].cycles(created["id"])
    assert history[0]["action_state"] == "delivered"
    active = await system.db.fetchone("SELECT active_session_id,active_run_id FROM schedules WHERE id = ?",
                                      (created["id"],))
    assert (active["active_session_id"], active["active_run_id"]) == (run_session.session.id, "scheduled-run")
    assert system.extensions["scheduler"]._active_runs[created["id"]] == "scheduled-run"

    # A later due slot cannot be reserved after the effect completed while its run is alive.
    next_due = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await system.db.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (next_due, created["id"]))
    assert await system.extensions["recurring"].reserve(created["id"]) is None
    assert (await system.db.fetchone("SELECT count(*) AS count FROM recurring_cycles WHERE schedule_id = ?",
                                     (created["id"],)))["count"] == 1
    revision = await system.db.fetchone("SELECT revision FROM domain_collection_revisions"
                                        " WHERE scope_kind='project' AND scope_id=?", (project.id,))
    with pytest.raises(ControlDenied, match="still active"):
        await system.extensions["recurring"].run_now(
            Principal("operator:1", "operator"), created["id"],
            expected_collection_revision=revision["revision"], expected_schedule_revision=1,
            client_operation_id="manual-while-active",
        )
    await system.db.execute("UPDATE runs SET status = 'completed' WHERE id = 'scheduled-run'")
    await system.extensions["scheduler"].restore()
    assert created["id"] not in system.extensions["scheduler"]._active
    assert await system.extensions["recurring"].reserve(created["id"]) is not None


async def test_manual_run_uses_distinct_receipt_and_keeps_due_time(system: Any) -> None:
    created = await _create(system)
    before = await system.db.fetchone("SELECT next_run_at FROM schedules WHERE id = ?", (created["id"],))
    rev = await system.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind='global' AND scope_id='global'")
    first = await system.extensions["recurring"].run_now(
        Principal("operator:1", "operator"), created["id"], expected_collection_revision=rev["revision"],
        expected_schedule_revision=1, client_operation_id="manual-run",
    )
    again = await system.extensions["recurring"].run_now(
        Principal("operator:1", "operator"), created["id"], expected_collection_revision=rev["revision"],
        expected_schedule_revision=1, client_operation_id="manual-run",
    )
    assert first == again
    after = await system.db.fetchone("SELECT next_run_at FROM schedules WHERE id = ?", (created["id"],))
    assert before["next_run_at"] == after["next_run_at"]
    assert await system.extensions["effects"].step()
    assert len(system.front.sent) == 1


async def test_revised_schedule_fences_pending_old_effect(system: Any) -> None:
    created = await _create(system)
    cycle = await system.extensions["recurring"].reserve(created["id"])
    rev = await system.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind='global' AND scope_id='global'")
    changed = await system.extensions["recurring"].change(
        Principal("operator:1", "operator"), created["id"], fields={"prompt": "new exact text"},
        expected_collection_revision=rev["revision"], expected_schedule_revision=1,
        client_operation_id="revise",
    )
    assert changed["authority_state"] == "needs_approval"
    effect = await system.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (cycle["effect_id"],))
    assert effect["state"] == "cancelled"
    assert not await system.extensions["effects"].step()
    assert not system.front.sent


async def test_operator_routes_replay_create_and_inspect_uncertain_cycle(system: Any) -> None:
    api = FastAPI()

    async def auth() -> dict[str, Any]:
        return {"via": "token", "user_id": 1}

    api_recurring.register(api, system, auth)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        body = {"name": "medicine", "prompt": "take the medicine", "kind": "message", "cron": None,
                "run_at": "2026-01-01T00:00:00Z", "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "expected_collection_revision": 1, "client_operation_id": "api-create"}
        first = await client.post("/api/recurring", json=body)
        assert first.status_code == 200, first.text
        replay = await client.post("/api/recurring", json=body)
        assert replay.status_code == 200 and replay.json() == first.json()
        schedule_id = first.json()["id"]
        cycle = await system.extensions["recurring"].reserve(schedule_id)
        assert cycle is not None
        history = await client.get(f"/api/recurring/{schedule_id}/cycles")
        assert history.status_code == 200 and history.json()["cycles"][0]["effect_state"] == "pending"
        claim = await system.extensions["effects"].store.claim(("schedule.message",))
        assert claim is not None
        await system.extensions["effects"].store.recover()
        inspect = await client.get(f"/api/recurring/{schedule_id}/cycles")
        assert inspect.json()["cycles"][0]["effect_state"] == "unknown"
        reconcile = await client.post(f"/api/recurring/{schedule_id}/cycles/{cycle['id']}/reconcile", json={
            "outcome": "not_delivered", "reason": "I checked the channel history",
            "expected_collection_revision": inspect.json()["collection_revision"],
            "client_operation_id": "api-reconcile",
        })
        assert reconcile.status_code == 200, reconcile.text
        assert reconcile.json()["evidence_kind"] == "operator_attestation"


async def test_unresolved_cycle_blocks_next_due_time_without_losing_it(system: Any) -> None:
    recurring = system.extensions["recurring"]
    rev = await system.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind='global' AND scope_id='global'")
    created = await recurring.create(
        Principal("operator:1", "operator"), name="hourly", prompt="check the feed",
        cron="* * * * *", run_at=None, kind="message", target_session=None, project_id=None,
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        expected_collection_revision=rev["revision"], client_operation_id="hourly-create",
    )
    await system.db.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?",
                            ((datetime.now(UTC) - timedelta(minutes=2)).isoformat(), created["id"]))
    first = await recurring.reserve(created["id"])
    assert first is not None
    await system.db.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?",
                            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), created["id"]))
    assert await recurring.reserve(created["id"]) is None
    pending = await system.db.fetchone("SELECT next_run_at FROM schedules WHERE id = ?", (created["id"],))
    assert datetime.fromisoformat(pending["next_run_at"]) < datetime.now(UTC)


async def test_project_handoff_revokes_old_schedule_and_preserves_cycle_history(system: Any) -> None:
    project = await system.manager.projects.create("Workspace")
    old = await system.manager.create_session("old coordinator", project_id=project.id,
                                              metadata={"orchestrator_of": project.id})
    new = await system.manager.create_session("new coordinator", project_id=project.id,
                                              metadata={"orchestrator_of": project.id})
    rev = await system.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind='project' AND scope_id=?", (project.id,))
    recurring = system.extensions["recurring"]
    created = await recurring.create(
        Principal("operator:1", "operator"), name="wake", prompt="check result",
        cron=None, run_at="2026-01-01T00:00:00Z", kind="wake", target_session=old.session.id,
        project_id=project.id, expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        expected_collection_revision=rev["revision"], client_operation_id="wake-create",
    )
    cycle = await recurring.reserve(created["id"])
    assert cycle is not None
    async with system.db.authority_effect_lock("project", project.id):
        async with system.db.transaction() as conn:
            changed = await repoint_project_schedules_in(
                conn, project_id=project.id, old_session_id=old.session.id,
                new_session_id=new.session.id,
            )
    assert changed == [created["id"]]
    row = await system.db.fetchone("SELECT target_session,authority_state,grant_id,schedule_revision FROM schedules WHERE id = ?", (created["id"],))
    assert (row["target_session"], row["authority_state"], row["grant_id"], row["schedule_revision"]) == (
        new.session.id, "needs_approval", None, 2,
    )
    effect = await system.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (cycle["effect_id"],))
    historical = await system.db.fetchone("SELECT target_session,schedule_revision FROM recurring_cycles WHERE id = ?", (cycle["id"],))
    assert effect["state"] == "cancelled"
    assert (historical["target_session"], historical["schedule_revision"]) == (old.session.id, 1)
    assert not await system.extensions["effects"].step()


async def test_project_with_automation_history_cannot_be_hard_deleted(system: Any) -> None:
    project = await system.manager.projects.create("Workspace")
    rev = await system.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind='project' AND scope_id=?", (project.id,))
    await system.extensions["recurring"].create(
        Principal("operator:1", "operator"), name="reminder", prompt="inspect work",
        cron=None, run_at="2026-01-01T00:00:00Z", kind="message", target_session=None,
        project_id=project.id, expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        expected_collection_revision=rev["revision"], client_operation_id="project-reminder",
    )
    with pytest.raises(ProjectError, match="automation history"):
        await system.manager.projects.delete(project.id)
