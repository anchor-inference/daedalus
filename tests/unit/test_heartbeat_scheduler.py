"""Heartbeat gating, schedule kinds (reminder, lazy note, agent) and failure accounting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.heartbeat import Heartbeat, in_active_hours
from daedalus.extensions.notifications import NotificationService
from daedalus.extensions.recurring import Recurring, RecurringEffect
from daedalus.extensions.scheduler import Scheduler
from daedalus.host.session_runner import SessionManager
from daedalus.stores.control import Principal
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore


class FakeOutbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str, *, markdown: bool = True) -> int:
        self.sent.append(text)
        return 1


class FakeFront:
    def __init__(self) -> None:
        self.outbox = FakeOutbox()
        self.notified: list[str] = []

    async def outbox_for_session(self, session_id: str) -> FakeOutbox:
        return self.outbox

    async def notify(self, text: str, *, markdown: bool = True) -> None:
        self.notified.append(text)


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=FakeFront(), extensions={})

    async def save_config(config: RuntimeConfig) -> None:
        app.config = config

    app.save_config = save_config
    app.notifications = NotificationService(db, manager.bus, front=lambda: app.front)
    yield app
    await manager.close()


async def _durable(app: Any, scheduler: Scheduler) -> None:
    app.extensions["scheduler"] = scheduler
    app.extensions["recurring"] = Recurring(app)
    dispatcher = EffectDispatcher(OutboxStore(app.db))
    effect = RecurringEffect(app)
    for kind in ("message", "lazy", "wake", "agent"):
        dispatcher.register(f"schedule.{kind}", effect)
    app.extensions["effects"] = dispatcher


async def _approve(app: Any, schedule_id: str) -> None:
    rev = await app.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind='global' AND scope_id='global'")
    await app.extensions["recurring"].approve(
        Principal("operator:1", "operator"), schedule_id,
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        expected_collection_revision=rev["revision"], expected_schedule_revision=1,
        client_operation_id=f"approve-{schedule_id}",
    )


async def _change(app: Any, schedule_id: str, revision: int, fields: dict[str, Any]) -> dict[str, Any]:
    schedule = await app.db.fetchone("SELECT project_id FROM schedules WHERE id = ?", (schedule_id,))
    project_id = schedule["project_id"] if schedule is not None else None
    scope, scope_id = ("project", project_id) if project_id else ("global", "global")
    row = await app.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind = ? AND scope_id = ?", (scope, scope_id))
    return await app.extensions["recurring"].change(
        Principal("operator:1", "operator"), schedule_id, fields=fields,
        expected_collection_revision=row["revision"], expected_schedule_revision=revision,
        client_operation_id=f"change-{schedule_id}-{revision}",
    )


def test_active_hours_windows() -> None:
    at = lambda h, m=0: datetime(2026, 9, 6, h, m, tzinfo=UTC)  # noqa: E731
    assert in_active_hours(at(9), "08:00-23:00") and not in_active_hours(at(7, 59), "08:00-23:00")
    assert in_active_hours(at(23, 30), "22:00-06:00") and in_active_hours(at(2), "22:00-06:00") and not in_active_hours(at(12), "22:00-06:00")
    assert in_active_hours(at(3), "garbage")


async def test_heartbeat_is_off_without_text_and_respects_interval(app: Any, tmp_path: Any) -> None:
    hb = Heartbeat(app)
    app.config.heartbeat.enabled = True
    app.config.heartbeat.active_hours = "00:00-00:00"
    assert not hb.due()  # empty file
    hb.write("check the mail")
    assert hb.due()
    hb.last_run = datetime.now(UTC) - timedelta(minutes=5)
    assert not hb.due()
    hb.last_run = datetime.now(UTC) - timedelta(minutes=app.config.heartbeat.interval_minutes + 1)
    assert hb.due()
    hb.runs_today = (datetime.now(UTC).strftime("%Y-%m-%d"), app.config.heartbeat.max_runs_per_day)
    assert not hb.due()
    app.config.heartbeat.enabled = False
    assert not hb.due()


async def test_message_reminder_is_delivered_without_a_model_call(app: Any) -> None:
    scheduler = Scheduler(app)
    await _durable(app, scheduler)
    created = await scheduler.create(name="pills", prompt="take the pills", cron=None, run_at="2026-01-01T00:00:00Z", kind="message", created_by_session="s1")
    assert created["kind"] == "message"
    await _approve(app, created["id"])
    row = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],))
    await scheduler.fire(dict(row))
    assert await app.extensions["effects"].step()
    assert app.front.outbox.sent and "take the pills" in app.front.outbox.sent[0]
    [entry] = (await app.notifications.list())["entries"]
    assert (entry["kind"], entry["category"]) == ("reminder", "reminder")
    assert entry["delivered"]["telegram"] == "handled"  # the chat already carried it
    assert entry["delivered"]["push"] == "skipped: no device"
    fired = await app.manager.bus.replay(0, None, limit=10)
    assert [(e.type, e.payload["name"]) for e in fired if e.type == "schedule.fired"] == [("schedule.fired", "pills")]
    row = await app.db.fetchone("SELECT enabled, next_run_at FROM schedules WHERE id = ?", (created["id"],))
    assert row["enabled"] == 0 and row["next_run_at"] is None  # a one-shot is done


async def test_lazy_note_rides_with_the_next_message_and_is_promoted_when_ignored(app: Any) -> None:
    scheduler = Scheduler(app)
    await _durable(app, scheduler)
    created = await scheduler.create(name="ask", prompt="ask about the invoice", cron=None, run_at="2026-01-01T00:00:00Z", kind="lazy", created_by_session="s1")
    await _approve(app, created["id"])
    row = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],))
    await scheduler.fire(dict(row))
    assert await app.extensions["effects"].step()
    assert app.front.outbox.sent == []
    decorated = await scheduler.decorate_prompt("s1", "hi again")
    assert decorated.startswith("[Reminder fired") and decorated.endswith("\n\nhi again") and "ask about the invoice" in decorated
    assert "ask about the invoice" in await scheduler.decorate_prompt("s1", "x")  # not yet committed: a refused start loses nothing
    await scheduler.on_run_started("s1", "run-1")
    assert await scheduler.decorate_prompt("s1", "x") == "x"  # delivered once the run exists
    # a second note that nobody reads for a day is promoted
    await app.db.execute(
        "INSERT INTO lazy_notes(schedule_id, session_id, text, fired_at) VALUES (?, ?, ?, ?)",
        (created["id"], "s1", "stale note", (datetime.now(UTC) - timedelta(hours=30)).isoformat()),
    )
    promoted: list[tuple[str, str]] = []

    async def fake_run(title: str, prompt: str, workspace: Any, metadata: dict[str, Any], **_: Any) -> Any:
        promoted.append((title, prompt))
        return SimpleNamespace(session=SimpleNamespace(id="new"), run_id="r")

    scheduler.run_task_session = fake_run  # type: ignore[method-assign]
    await scheduler._promote_lazy_notes(datetime.now(UTC))
    assert promoted and "stale note" in promoted[0][1]
    row = await app.db.fetchone("SELECT promoted_at FROM lazy_notes WHERE text = 'stale note'")
    assert row["promoted_at"] is not None


async def test_failed_recurring_task_is_switched_off_after_max_failures(app: Any) -> None:
    scheduler = Scheduler(app)
    await _durable(app, scheduler)
    app.config.scheduler.max_failures = 2
    created = await scheduler.create(name="nightly", prompt="do it", cron="0 3 * * *", run_at=None, kind="agent")
    for n in (1, 2):
        scheduler._active[created["id"]] = "sess"
        await scheduler.on_run_finished("sess", f"run{n}", "failed")
        row = await app.db.fetchone("SELECT failure_count, enabled FROM schedules WHERE id = ?", (created["id"],))
        assert row["failure_count"] == n
    assert row["enabled"] == 0
    titles = [e["title"] for e in (await app.notifications.list())["entries"]]
    assert any("switched off" in t for t in titles)
    await _change(app, created["id"], 1, {"enabled": True})
    row = await app.db.fetchone("SELECT failure_count, enabled FROM schedules WHERE id = ?", (created["id"],))
    assert row["enabled"] == 1 and row["failure_count"] == 0


async def test_stay_silent_is_refused_outside_unattended_runs(app: Any) -> None:
    from protocore.contracts.tools import ToolContext

    from daedalus.tools.quiet import stay_silent

    manager: SessionManager = app.manager
    chat = await manager.create_session("chat")
    task = await manager.create_session("[cron] nightly", metadata={"unattended": True})
    for state in (chat, task):
        state.services.extra["manager"] = manager  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r1", session_id=chat.session.id)
    result = await stay_silent().invoke(ctx, {"note": "test"})
    assert result.is_error and "only for unattended" in result.content
    assert "silent_run" not in chat.services.extra  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r2", session_id=task.session.id)
    result = await stay_silent().invoke(ctx, {"note": "checked"})
    assert not result.is_error and task.services.extra["silent_run"] == "r2"  # type: ignore[union-attr]


async def test_agent_task_can_run_in_its_own_session(app: Any) -> None:
    scheduler = Scheduler(app)
    await _durable(app, scheduler)
    manager = app.manager
    owner = await manager.create_session("owner")
    submitted: list[tuple[str, str, str]] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer=False, as_answer=True, origin="operator") -> str:  # type: ignore[no-untyped-def]
        submitted.append((session_id, text, origin))
        return "run-self"

    manager.submit = fake_submit  # type: ignore[method-assign]
    with pytest.raises(ValueError):
        await scheduler.create(name="x", prompt="p", cron="*/20 * * * *", run_at=None, kind="message", run_in="self")
    with pytest.raises(ValueError):
        await scheduler.create(name="x", prompt="p", cron="*/20 * * * *", run_at=None, kind="agent", run_in="self", created_by_session="nope")
    created = await scheduler.create(name="ping", prompt="check the board", cron="*/20 * * * *", run_at=None, kind="agent", run_in="self", created_by_session=owner.session.id)
    assert created["run_in"] == "self" and created["workspace"] == str(owner.workspace)
    row = dict(await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],)))
    rev = await app.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind='project' AND scope_id=?", (row["project_id"],))
    await app.extensions["recurring"].run_now(
        Principal("operator:1", "operator"), created["id"], expected_collection_revision=rev["revision"],
        expected_schedule_revision=1, client_operation_id="run-own-session",
    )
    assert await app.extensions["effects"].step()
    assert submitted and submitted[0][0] == owner.session.id and submitted[0][2] == "schedule" and "in this session" in submitted[0][1]
    assert scheduler._active[created["id"]] == owner.session.id and scheduler._active_runs[created["id"]] == "run-self"
    # another turn of the same session ending is not the task ending
    await scheduler.on_run_finished(owner.session.id, "run-operator", "completed")
    assert created["id"] in scheduler._active
    await scheduler.on_run_finished(owner.session.id, "run-self", "completed")
    assert created["id"] not in scheduler._active
    row = await app.db.fetchone("SELECT active_session_id FROM schedules WHERE id = ?", (created["id"],))
    assert row["active_session_id"] is None


async def test_schedule_update_moves_the_next_run_and_pauses(app: Any) -> None:
    scheduler = Scheduler(app)
    await _durable(app, scheduler)
    created = await scheduler.create(name="digest", prompt="sum up", cron="0 4 * * *", run_at=None, kind="agent")
    await _change(app, created["id"], 1, {"name": "daily digest", "cron": "30 5 * * 1-5"})
    changed = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],))
    assert changed["name"] == "daily digest"
    assert changed["cron"] == "30 5 * * 1-5"
    assert changed["next_run_at"].endswith("05:30:00+00:00")
    await _change(app, created["id"], 2, {"enabled": False})
    paused = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],))
    assert paused["enabled"] == 0
    await _change(app, created["id"], 3, {"run_at": "2030-01-01T09:00:00Z"})
    once = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],))
    assert once["cron"] is None and once["recurring"] == 0 and once["next_run_at"].startswith("2030-01-01T09:00:00")
    await _change(app, created["id"], 4, {"cron": "0 4 * * *"})
    back = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],))
    assert back["cron"] == "0 4 * * *" and back["run_at"] is None and back["recurring"] == 1
    with pytest.raises(ValueError):
        await _change(app, created["id"], 5, {"cron": "not a cron"})
    with pytest.raises(KeyError):
        await _change(app, "nope", 1, {"name": "x"})
