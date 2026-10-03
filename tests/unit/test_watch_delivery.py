"""Standing-watch actions have a durable intent before delivery and a bounded recovery path."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from daedalus.config import Settings
from daedalus.extensions.watch_delivery import WatchDeliveries, message_id
from daedalus.stores.database import Database
from tests.unit.test_orchestrator import events, rig
from tests.unit.test_watches import Clock, with_watches


async def test_reserved_wake_survives_interruption_and_keeps_one_event(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        item = await keeper.create(project, when={"event": "staff_finished"}, then={"action": "wake"})
        original = keeper.deliveries.deliver

        async def interrupted(_: str) -> bool:
            return False

        keeper.deliveries.deliver = interrupted
        assert await keeper.fire(item, "finished", source_cursor="bus:300")
        pending = await db.fetchone("SELECT id,status FROM watch_deliveries WHERE watch_id = ?", (item.id,))
        assert pending["status"] == "pending"
        keeper.deliveries.deliver = original
        assert await keeper.deliveries.sweep() == 1
        fired = await events(r.manager, "watch.fired")
        assert len(fired) == 1 and fired[0].payload["delivery_id"] == pending["id"]
        await db.execute("UPDATE watch_deliveries SET status = 'reconciling' WHERE id = ?", (pending["id"],))
        assert await keeper.deliveries.sweep() == 1
        assert len(await events(r.manager, "watch.fired")) == 1
    finally:
        await keeper.close()
        await r.manager.close()


async def test_expired_watch_cannot_reserve_or_wake(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    r = await rig(settings, db, tmp_path)
    clock = Clock()
    keeper = await with_watches(r, clock)
    try:
        project = await r.orch.enable(r.project.id)
        item = await keeper.create(project, when={"event": "staff_finished"}, then={"action": "wake"},
                                   deadline_at=(clock.now + timedelta(minutes=1)).isoformat())
        clock.advance(minutes=2)
        assert not await keeper.fire(item, "finished", source_cursor="bus:301")
        assert item.view()["stopped"] == "expired"
        assert await db.fetchone("SELECT id FROM watch_deliveries WHERE watch_id = ?", (item.id,)) is None
        assert await events(r.manager, "watch.fired") == []
    finally:
        await keeper.close()
        await r.manager.close()


async def test_removed_watch_cancels_pending_intent_but_keeps_receipt(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        item = await keeper.create(project, when={"event": "staff_finished"}, then={"action": "wake"})

        async def interrupted(_: str) -> bool:
            return False

        keeper.deliveries.deliver = interrupted
        assert await keeper.fire(item, "finished", source_cursor="bus:400")
        assert await keeper.remove(project.id, item.id)
        # A removed rule is not permission to erase the old attempt or wake from it.
        keeper.deliveries = WatchDeliveries(r.team.app)
        assert await keeper.deliveries.sweep() == 0
        row = await db.fetchone("SELECT status FROM watch_deliveries WHERE watch_id = ?", (item.id,))
        assert row["status"] == "cancelled"
        assert await events(r.manager, "watch.fired") == []
    finally:
        await keeper.close()
        await r.manager.close()


async def test_notification_reconciliation_does_not_send_twice(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        item = await keeper.create(project, when={"event": "staff_finished"},
                                   then={"action": "notify", "title": "Check", "text": "A member finished"})
        assert await keeper.fire(item, "finished", source_cursor="bus:401")
        assert len(r.team.app.notifications.posted) == 1
        await db.execute("UPDATE watch_deliveries SET status = 'reconciling' WHERE watch_id = ?", (item.id,))
        assert await keeper.deliveries.sweep() == 1
        assert len(r.team.app.notifications.posted) == 1
        assert len(await events(r.manager, "watch.fired")) == 1
    finally:
        await keeper.close()
        await r.manager.close()


async def test_failed_delivery_keeps_operator_visible_reason(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        item = await keeper.create(project, when={"event": "staff_finished"},
                                   then={"action": "notify", "title": "Check"})
        r.team.app.notifications = None
        assert await keeper.fire(item, "finished", source_cursor="bus:402")
        delivery = await db.fetchone(
            "SELECT status,last_error FROM watch_deliveries WHERE watch_id = ?", (item.id,),
        )
        assert delivery["status"] == "failed"
        assert delivery["last_error"] == "notifications are unavailable"
        assert len(await events(r.manager, "watch.fired")) == 1
    finally:
        await keeper.close()
        await r.manager.close()


async def test_suppressed_notification_is_not_a_delivered_watch(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        item = await keeper.create(project, when={"event": "staff_finished"},
                                   then={"action": "notify", "title": "Check"})

        class Suppressed:
            async def post(self, _: object) -> None:
                return None

        r.team.app.notifications = Suppressed()
        assert await keeper.fire(item, "finished", source_cursor="bus:403")
        row = await db.fetchone("SELECT id,status,receipt_id,last_error FROM watch_deliveries WHERE watch_id = ?", (item.id,))
        assert row["status"] == "failed" and row["receipt_id"].startswith("event:")
        assert row["last_error"] == "notification was suppressed before persistence"
        assert await db.fetchone("SELECT id FROM notifications WHERE dedupe_key = ?", (f"watch-delivery:{row['id']}",)) is None
        await db.execute("UPDATE watch_deliveries SET status = 'reconciling' WHERE watch_id = ?", (item.id,))
        assert await keeper.deliveries.sweep() == 1
        assert (await db.fetchone("SELECT status FROM watch_deliveries WHERE watch_id = ?", (item.id,)))["status"] == "failed"
    finally:
        await keeper.close()
        await r.manager.close()


async def test_queued_staff_message_cannot_be_mistaken_for_delivery(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        member = await r.manager.staff.hire(project.id, name="Ada")
        item = await keeper.create(project, when={"event": "staff_finished"},
                                   then={"action": "tell", "staff": "Ada", "text": "Check the result"})

        async def interrupted(_: str) -> bool:
            return False

        keeper.deliveries.deliver = interrupted
        assert await keeper.fire(item, "finished", source_cursor="bus:404")
        row = await db.fetchone("SELECT id FROM watch_deliveries WHERE watch_id = ?", (item.id,))
        at = "2026-01-01T00:00:00+00:00"
        await db.execute(
            "INSERT INTO staff_messages(id,staff_id,origin,text,mode,state,attempts,created_at,updated_at)"
            " VALUES (?,?,'operator','Check the result','now','queued',0,?,?)",
            (message_id(row["id"]), member.id, at, at),
        )
        keeper.deliveries = WatchDeliveries(r.team.app)
        await db.execute("UPDATE watch_deliveries SET status = 'reconciling' WHERE id = ?", (row["id"],))
        assert await keeper.deliveries.sweep() == 0
        assert (await db.fetchone("SELECT status FROM watch_deliveries WHERE id = ?", (row["id"],)))["status"] == "reconciling"
        assert await events(r.manager, "watch.fired") == []
        await db.execute("UPDATE staff_messages SET state = 'submitted' WHERE id = ?", (message_id(row["id"]),))
        assert await keeper.deliveries.sweep() == 1
        assert (await db.fetchone("SELECT status FROM watch_deliveries WHERE id = ?", (row["id"],)))["status"] == "delivered"
        assert len(await events(r.manager, "watch.fired")) == 1
    finally:
        await keeper.close()
        await r.manager.close()
