"""An orchestrator's wake-ups: set by itself or by the operator, fired by the scheduler as a project event,
delivered by its wake queue, and carried over to whoever holds the office."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.llm import LLMRequest, ProviderDelta

from daedalus.config import Settings
from daedalus.extensions import wakeups
from daedalus.extensions.api import build_app
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.extensions.scheduler import Scheduler
from daedalus.stores.database import Database
from tests.support.waiting import until_await
from tests.unit.test_orchestrator import Rig, _idle, events, events_messages, rig
from tests.unit.test_session_runner import ScriptedProvider


class Notes:
    """The notifications service as the scheduler and the team use it."""

    def __init__(self) -> None:
        self.posted: list[Any] = []

    async def post(self, draft: Any) -> None:
        self.posted.append(draft)

    async def prune(self, keep_days: int) -> int:
        return 0


def with_scheduler(r: Rig) -> Scheduler:
    app = r.team.app
    app.notifications = Notes()
    app.front = None
    scheduler = Scheduler(app)
    # The database housekeeping has its own tests; here it would only slow the tick down.
    scheduler._maintained_at = datetime.now(UTC)
    app.extensions["scheduler"] = scheduler
    return scheduler


async def make_due(db: Database, wakeup_id: str) -> None:
    """The moment has come: the scheduler's clock is the wall clock, so the row is moved instead."""
    await db.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", ((datetime.now(UTC) - timedelta(seconds=5)).isoformat(), wakeup_id))


async def test_wake_me_wakes_the_scripted_orchestrator_with_its_note_once(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The acceptance: ``WakeMe(in_minutes=1)`` wakes the scripted orchestrator with the note."""
    script = [
        {"tool": "WakeMe", "args": {"note": "Check whether Ada's migration finished", "in_minutes": 1}},
        {"text": "I will look again in a minute."},
        {"text": "Looking at Ada's migration now."},
    ]
    r = await rig(settings, db, tmp_path, script)
    scheduler = with_scheduler(r)
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        await r.manager.submit(sid, "Keep an eye on the migration")
        await until_await(lambda: _idle(r.manager, sid), "the first turn ended")
        [wakeup] = await wakeups.wakeups(r.team.app, r.project.id)
        assert wakeup["note"] == "Check whether Ada's migration finished" and wakeup["set_by"] == "orchestrator"
        due = datetime.fromisoformat(wakeup["next_run_at"]) - datetime.now(UTC)
        assert timedelta(seconds=30) < due <= timedelta(minutes=1)
        assert f"[{wakeup['id']}]" in await r.orch.project_state(await r.refreshed(), session_id=sid)

        await scheduler.tick()
        assert await events(r.manager, "schedule.fired") == [], "not due yet"
        await make_due(db, wakeup["id"])
        await scheduler.tick()
        await scheduler.tick()
        [fired] = await events(r.manager, "schedule.fired")
        assert fired.project_id == r.project.id and fired.session_id is None
        assert fired.payload == {"schedule_id": wakeup["id"], "name": wakeup["note"], "kind": "wake", "note": wakeup["note"], "set_by": "orchestrator"}

        async def woken() -> bool:
            return bool(await events_messages(r.manager, sid)) and await _idle(r.manager, sid)

        await until_await(woken, "the wake-up woke the orchestrator")
        [batch] = await events_messages(r.manager, sid)
        assert f"your wake-up [{wakeup['id']}] fired: Check whether Ada's migration finished" in batch
        # A one-off that fired is done: gone from the list and from the state block.
        assert await wakeups.wakeups(r.team.app, r.project.id) == []
    finally:
        await r.manager.close()


class HeldProvider(ScriptedProvider):
    """A scripted model whose first answer waits for the test, so the orchestrator is busy meanwhile."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        super().__init__(script)
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def stream_with_tools(self, request: LLMRequest) -> AsyncIterator[ProviderDelta]:
        if len(self.requests) == 0:
            self.started.set()
            await self.release.wait()
        async for delta in super().stream_with_tools(request):
            yield delta


async def test_a_wake_up_reaches_a_busy_orchestrator(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    held = HeldProvider([{"text": "Working on the plan."}, {"text": "Now the wake-up."}])
    r.manager.providers.rungs_for = lambda config, preset=None: [(held, "scripted-model")]  # type: ignore[method-assign]
    scheduler = with_scheduler(r)
    try:
        project = await r.orch.enable(r.project.id)
        sid = project.settings.orchestrator.session_id
        wakeup = await wakeups.set_wakeup(r.team.app, project, note="Is the build green?", in_minutes=5)
        assert wakeup["set_by"] == "operator"
        await r.manager.submit(sid, "Plan the week")
        await held.started.wait()
        await make_due(db, wakeup["id"])
        await scheduler.tick()
        assert (await r.orch.target_state(r.project.id)) == "running"

        async def delivered() -> bool:
            return any("Is the build green?" in str(m.content_blocks) for request in held.requests[1:] for m in request.messages)

        held.release.set()
        await until_await(delivered, "the wake-up reached the busy orchestrator")
        await until_await(lambda: _idle(r.manager, sid), "the orchestrator settled")
    finally:
        held.release.set()
        await r.manager.close()


async def test_cron_advances_and_the_bounds_hold(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    scheduler = with_scheduler(r)
    try:
        project = await r.orch.enable(r.project.id)
        sid = project.settings.orchestrator.session_id
        app = r.team.app
        daily = await wakeups.set_wakeup(app, project, note="Morning round", cron="0 7 * * *", by_session=sid)
        first = daily["next_run_at"]
        await make_due(db, daily["id"])
        await scheduler.tick()
        [again] = await wakeups.wakeups(app, r.project.id)
        assert again["enabled"] and again["next_run_at"] > datetime.now(UTC).isoformat() and again["next_run_at"][:10] >= first[:10]
        assert len(await events(r.manager, "schedule.fired")) == 1

        refusals = [
            ({"note": "x", "cron": "* * * * *"}, "at most every 10 minutes"),
            ({"note": "x", "cron": "not a cron"}, "not a cron expression"),
            ({"note": "x", "in_minutes": 0}, "between 1 and"),
            ({"note": "x", "at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}, "already passed"),
            ({"note": "x", "at": "tomorrow"}, "not a moment"),
            ({"note": "x", "in_minutes": 5, "cron": "0 7 * * *"}, "exactly one"),
            ({"note": "x"}, "exactly one"),
            ({"note": "  ", "in_minutes": 5}, "needs a note"),
        ]
        for args, expected in refusals:
            with pytest.raises(wakeups.WakeupRefused, match=expected):
                await wakeups.set_wakeup(app, project, **args)
        r.manager.config.orchestrator.wakeups_max = 2
        await wakeups.set_wakeup(app, project, note="second", in_minutes=30)
        with pytest.raises(wakeups.WakeupRefused, match="already has 2 wake-ups"):
            await wakeups.set_wakeup(app, project, note="third", in_minutes=30)
        # An ordinary session cannot make itself a wake-up through the scheduler either.
        ordinary = await r.manager.create_session("work", project_id=r.project.id)
        with pytest.raises(ValueError, match="belongs to a project's orchestrator"):
            await scheduler.create(name="w", prompt="w", cron=None, run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(), kind="wake", created_by_session=ordinary.session.id)
    finally:
        await r.manager.close()


async def test_cancel_and_the_tool_refusals(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    with_scheduler(r)
    try:
        project = await r.orch.enable(r.project.id)
        sid = project.settings.orchestrator.session_id
        said = await r.call(sid, "wake_me", note="Look at the queue", at=(datetime.now(UTC) + timedelta(hours=2)).isoformat())
        assert said.startswith("wake-up set: [") and "Unwatch(" in said
        [wakeup] = await wakeups.wakeups(r.team.app, r.project.id)
        with pytest.raises(Refused, match="has no wake-up or watch 'nope'"):
            await r.call(sid, "unwatch", id="nope", client_operation_id="missing-wakeup")
        assert await r.call(sid, "unwatch", id=wakeup["id"], client_operation_id="cancel-wakeup") == f"wake-up {wakeup['id']} cancelled"
        assert await wakeups.wakeups(r.team.app, r.project.id) == []
        with pytest.raises(Refused, match="already passed"):
            await r.call(sid, "wake_me", note="late", at="2000-01-01T00:00")
        # Another project's wake-up is not this one's to cancel.
        other = await r.manager.projects.create("Other", [])
        assert not await wakeups.cancel(r.team.app, other.id, wakeup["id"])
    finally:
        await r.manager.close()


async def test_a_replaced_or_restarted_orchestrator_receives_its_predecessors_wake_ups(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    scheduler = with_scheduler(r)
    try:
        project = await r.orch.enable(r.project.id)
        old = project.settings.orchestrator.session_id
        wakeup = await wakeups.set_wakeup(r.team.app, project, note="Review the release notes", in_minutes=10, by_session=old)
        new = (await r.orch.replace(r.project.id, "fresh context")).settings.orchestrator.session_id
        row = await db.fetchone("SELECT target_session FROM schedules WHERE id = ?", (wakeup["id"],))
        assert row is not None and row["target_session"] == new
        assert f"[{wakeup['id']}]" in await r.orch.project_state(await r.refreshed(), session_id=new)

        # Off: a due wake-up has nobody to wake and says so quietly, instead of waking a retired chat.
        await r.orch.disable(r.project.id)
        assert [w["id"] for w in await wakeups.wakeups(r.team.app, r.project.id)] == [wakeup["id"]]
        second = await db.fetchone("SELECT * FROM schedules WHERE id = ?", (wakeup["id"],))
        assert second is not None
        await scheduler.fire(dict(second), advance=False)
        assert await events(r.manager, "schedule.fired") == []
        assert r.team.app.notifications.posted[-1].kind == "wake_dropped"

        # On again: the new holder of the office gets the wake-ups set before.
        third = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        assert third not in (old, new)
        row = await db.fetchone("SELECT target_session FROM schedules WHERE id = ?", (wakeup["id"],))
        assert row is not None and row["target_session"] == third
        await make_due(db, wakeup["id"])
        await scheduler.tick()
        [fired] = await events(r.manager, "schedule.fired")
        assert fired.project_id == r.project.id and fired.payload["set_by"] == "orchestrator"
    finally:
        await r.manager.close()


async def test_the_wake_up_routes(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    with_scheduler(r)
    try:
        app = SimpleNamespace(settings=settings, config=r.manager.config, db=db, manager=r.manager, front=None, extensions=r.team.app.extensions, guard=None, notifications=None)
        api = build_app(app, "tok")  # type: ignore[arg-type]
        headers = {"X-Daedalus-Token": "tok"}
        base = f"/api/projects/{r.project.id}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            refused = await client.post(f"{base}/wakeups", json={"note": "x", "in_minutes": 5}, headers=headers)
            assert refused.status_code == 400 and "no orchestrator" in refused.json()["detail"]
            await r.orch.enable(r.project.id)
            made = await client.post(f"{base}/wakeups", json={"note": "Ask Ada about the tests", "in_minutes": 45}, headers=headers)
            assert made.status_code == 200, made.text
            assert made.json()["set_by"] == "operator" and made.json()["note"] == "Ask Ada about the tests"
            assert (await client.post(f"{base}/wakeups", json={"note": "x", "cron": "* * * * *"}, headers=headers)).status_code == 400
            assert (await client.post(f"{base}/wakeups", json={"note": "x", "in_minutes": 5, "extra": 1}, headers=headers)).status_code == 422
            listed = await client.get(f"{base}/wakeups", headers=headers)
            assert listed.status_code == 200 and [w["id"] for w in listed.json()["wakeups"]] == [made.json()["id"]]
            assert (await client.delete(f"{base}/wakeups/nope", headers=headers)).status_code == 404
            assert (await client.delete(f"{base}/wakeups/{made.json()['id']}", headers=headers)).json() == {"deleted": True}
            assert (await client.get(f"{base}/wakeups", headers=headers)).json()["wakeups"] == []
        changes = [e.payload.get("change") for e in await events(r.manager, "project.changed")]
        assert changes.count("wakeups") == 2
    finally:
        await r.manager.close()


def test_a_long_note_is_named_at_a_word_and_marked_as_cut() -> None:
    assert wakeups.short_name("Check the render") == "Check the render"
    long = "Check the render of both videos after the edits against the storyboard and tell the operator"
    name = wakeups.short_name(long)
    assert name.endswith("…")
    assert len(name) <= wakeups.NAME_MAX
    assert long.startswith(name[:-1])
    assert long[len(name) - 1] == " "
    unbroken = "x" * 120
    assert wakeups.short_name(unbroken) == "x" * (wakeups.NAME_MAX - 1) + "…"
