"""The schedule card in a conversation's side panel runs and deletes its schedule on the real router.

The card sent ``POST /api/schedules/{id}/run`` and ``DELETE /api/schedules/{id}``. Those routes went
when schedules became revisioned recurring work under ``/api/recurring``, and the card was left behind:
every press reached no route at all, FastAPI answered ``404 {"detail": "Not Found"}``, and the toast
showed exactly that for a schedule that plainly existed. The row here has the shape of the one the
operator met it on: an agent task that runs in the conversation that made it, inside a project.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.notifications import NotificationService
from daedalus.extensions.recurring import Recurring, RecurringEffect
from daedalus.extensions.scheduler import Scheduler
from daedalus.host.session_runner import SessionManager
from daedalus.stores.control import Principal
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore

H = {"X-Daedalus-Token": "tok"}


@pytest.fixture
async def host(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None,
                          extensions={}, guard=None, create_session=manager.create_session)
    app.notifications = NotificationService(db, manager.bus, front=lambda: None)
    app.extensions["scheduler"] = Scheduler(app)
    app.extensions["recurring"] = Recurring(app)
    dispatcher = EffectDispatcher(OutboxStore(db))
    effect = RecurringEffect(app)
    for kind in ("message", "lazy", "wake", "agent"):
        dispatcher.register(f"schedule.{kind}", effect)
    app.extensions["effects"] = dispatcher
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        yield app, client
    await manager.close()


async def _project_schedule(app: Any) -> tuple[str, str]:
    """An agent task in a project conversation that runs in that same conversation, every ten hours."""
    project = await app.manager.projects.create("legion")
    session = await app.manager.create_session("site check", project_id=project.id)
    revision = await app.db.fetchone("SELECT revision FROM domain_collection_revisions"
                                     " WHERE scope_kind='project' AND scope_id=?", (project.id,))
    created = await app.extensions["recurring"].create(
        Principal("operator:1", "operator"), name="site check + release (every 10 h)", prompt="check the site",
        cron="7 */10 * * *", run_at=None, kind="agent", target_session=session.session.id,
        project_id=project.id, expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
        expected_collection_revision=revision["revision"], client_operation_id="card-create",
    )
    await app.db.execute("UPDATE schedules SET run_in = 'self', created_by_session = ? WHERE id = ?",
                         (session.session.id, created["id"]))
    return created["id"], session.session.id


async def _card_row(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
    """The schedule as the card holds it: the list, narrowed to this conversation the way the app does."""
    listed = await client.get("/api/schedules", headers=H)
    assert listed.status_code == 200, listed.text
    mine = [s for s in listed.json() if session_id in (s["target_session"], s["created_by_session"], s.get("active_session_id"))]
    assert len(mine) == 1
    return mine[0]


async def _command_body(client: httpx.AsyncClient, row: dict[str, Any], operation: str) -> dict[str, Any]:
    """What `useScheduleActions` sends: the revisions the operator saw and one identity per command."""
    history = await client.get(f"/api/recurring/{row['id']}/cycles", headers=H)
    assert history.status_code == 200, history.text
    assert history.json()["scope"]["kind"] == "project"
    return {"expected_schedule_revision": row["schedule_revision"],
            "expected_collection_revision": history.json()["collection_revision"],
            "client_operation_id": operation}


async def test_the_routes_the_card_used_to_call_are_not_served(host: Any) -> None:
    # The fault as the operator met it, kept so the cause stays on record: neither path is a route.
    app, client = host
    schedule_id, _ = await _project_schedule(app)
    run = await client.post(f"/api/schedules/{schedule_id}/run", headers=H)
    delete = await client.delete(f"/api/schedules/{schedule_id}", headers=H)
    assert (run.status_code, run.json()) == (404, {"detail": "Not Found"})
    assert delete.status_code in (404, 405)


async def test_run_now_from_the_card_starts_an_occurrence(host: Any) -> None:
    app, client = host
    schedule_id, session_id = await _project_schedule(app)
    row = await _card_row(client, session_id)
    body = await _command_body(client, row, "card-run")
    ran = await client.post(f"/api/recurring/{schedule_id}/run", json=body, headers=H)
    assert ran.status_code == 200, ran.text
    cycle = await app.db.fetchone("SELECT manual,kind,target_session FROM recurring_cycles WHERE schedule_id = ?", (schedule_id,))
    assert (cycle["manual"], cycle["kind"], cycle["target_session"]) == (1, "agent", session_id)


async def test_delete_from_the_card_removes_the_schedule(host: Any) -> None:
    app, client = host
    schedule_id, session_id = await _project_schedule(app)
    row = await _card_row(client, session_id)
    body = await _command_body(client, row, "card-remove")
    removed = await client.post(f"/api/recurring/{schedule_id}/remove", json=body, headers=H)
    assert removed.status_code == 200, removed.text
    gone = await app.db.fetchone("SELECT deleted_at FROM schedules WHERE id = ?", (schedule_id,))
    assert gone["deleted_at"]
    assert all(s["id"] != schedule_id for s in (await client.get("/api/schedules", headers=H)).json())
    # A second press on a card that has not refreshed yet: the host says the schedule is not there,
    # in its own words, which the app turns into "Schedule not found".
    again = await client.post(f"/api/recurring/{schedule_id}/remove", json=await _command_body(client, row, "card-remove-2"), headers=H)
    assert again.status_code == 404 and again.json()["detail"] != "Not Found"
