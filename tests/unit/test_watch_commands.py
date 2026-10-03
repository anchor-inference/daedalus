"""Watch commands pin one standing authority and retire pending sends in their receipt transaction."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from daedalus.config import Settings
from daedalus.extensions.notifications import NotificationService
from daedalus.extensions.watch_authority import status as watch_authority_status
from daedalus.extensions.watch_commands import WatchCommands
from daedalus.extensions.watch_delivery import message_id
from daedalus.extensions.watch_egress import WatchEgressDenied, watch_message_guard
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Principal, Scope
from daedalus.stores.database import MIGRATIONS, Database
from daedalus.stores.watch_authority_schema import MIGRATION as WATCH_AUTHORITY_MIGRATION
from tests.unit.test_orchestrator import rig
from tests.unit.test_watches import with_watches


async def test_legacy_watch_is_preserved_for_approval_but_cannot_fire(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "legacy.sqlite")
    with monkeypatch.context() as before_approval:
        before_approval.setattr("daedalus.stores.database.MIGRATIONS",
                               MIGRATIONS[:MIGRATIONS.index(WATCH_AUTHORITY_MIGRATION)])
        await db.open()
        try:
            await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Work','2026-01-01')")
            await db.execute(
                "INSERT INTO watches(id,project_id,pattern_json,action_json,cooldown_s,once,note,created_by,"
                "created_at,enabled,state_json,condition_revision)"
                " VALUES ('wlegacy','project','{\"event\":\"staff_finished\"}','{\"action\":\"notify\"}',"
                "600,0,'old rule','operator',?,1,'{}',1)", (datetime.now(UTC).isoformat(),),
            )
            await db.execute(
                "INSERT INTO watch_deliveries(id,watch_id,condition_revision,source_cursor,dedup_key,status,"
                "project_id,created_at,updated_at)"
                " VALUES ('old-intent','wlegacy',1,'bus:old','old-key','pending','project',?,?)",
                (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
            )
        finally:
            await db.close()
    await db.open()
    try:
        rule = await db.fetchone("SELECT enabled,condition_revision,state_json FROM watches WHERE id = 'wlegacy'")
        assert rule["enabled"] == 0 and rule["condition_revision"] == 2
        assert "needs_approval" in rule["state_json"]
        assert (await db.fetchone("SELECT status FROM watch_deliveries WHERE id = 'old-intent'"))["status"] == "cancelled"
        assert not await db.fetchall("PRAGMA foreign_key_check")
    finally:
        await db.close()
    await db.open()
    try:
        assert (await db.fetchone("SELECT condition_revision FROM watches WHERE id = 'wlegacy'"))[0] == 2
    finally:
        await db.close()


async def test_watch_create_replay_and_stale_revision_keep_one_authority(settings: Settings, db: Database, tmp_path) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        commands = WatchCommands(keeper)
        principal = Principal.operator({"via": "token", "user_id": 1})
        scope = Scope("project", project.id)
        revision = await ControlStore(db).revision(scope, Entity("collection", project.id))
        created = await commands.create(
            principal, project, when={"event": "staff_finished"}, then={"action": "wake"},
            expected_collection_revision=revision, client_operation_id="create-standing-watch",
        )
        original_check = keeper._check_when

        async def changed_environment(_project, _when):
            raise AssertionError("a replay must use its stored receipt before checking changed dependencies")

        keeper._check_when = changed_environment
        replay = await commands.create(
            principal, project, when={"event": "staff_finished"}, then={"action": "wake"},
            expected_collection_revision=revision, client_operation_id="create-standing-watch",
        )
        keeper._check_when = original_check
        assert created == replay
        assert created["receipt_id"] and created["condition_revision"] == 1
        assert (await db.fetchone("SELECT count(*) AS n FROM watches"))["n"] == 1
        assert (await db.fetchone("SELECT count(*) AS n FROM watch_authorities"))["n"] == 1
        with pytest.raises(ControlConflict, match="changed"):
            await commands.create(
                principal, project, when={"event": "staff_question"}, then={"action": "wake"},
                expected_collection_revision=revision, client_operation_id="stale-standing-watch",
            )
        assert (await db.fetchone("SELECT count(*) AS n FROM watches"))["n"] == 1
        assert (await db.fetchone("SELECT count(*) AS n FROM app_events WHERE type = 'project.changed' AND json_extract(payload_json,'$.change') = 'watches'"))["n"] == 1
    finally:
        await keeper.close()
        await r.manager.close()


async def test_disabling_watch_cancels_pending_intent_and_stale_change(settings: Settings, db: Database, tmp_path) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        commands = WatchCommands(keeper)
        principal = Principal.operator({"via": "token", "user_id": 1})
        scope = Scope("project", project.id)
        created = await commands.create(
            principal, project, when={"event": "staff_finished"}, then={"action": "wake"},
            expected_collection_revision=await ControlStore(db).revision(scope, Entity("collection", project.id)),
            client_operation_id="create-before-disable",
        )
        item = keeper.get(project.id, created["id"])
        assert item is not None
        original = keeper.deliveries.deliver

        async def interrupted(_: str) -> bool:
            return False

        keeper.deliveries.deliver = interrupted
        assert await keeper.fire(item, "finished", source_cursor="bus:901")
        keeper.deliveries.deliver = original
        assert (await db.fetchone("SELECT status FROM watch_deliveries WHERE watch_id = ?", (item.id,)))["status"] == "pending"
        changed = await commands.change(
            principal, project, item.id, enabled=False,
            expected_entity_revision=await ControlStore(db).revision(scope, Entity("project", project.id)),
            expected_condition_revision=1, client_operation_id="disable-standing-watch",
        )
        assert changed["enabled"] is False and changed["condition_revision"] == 2
        assert (await db.fetchone("SELECT status FROM watch_deliveries WHERE watch_id = ?", (item.id,)))["status"] == "cancelled"
        assert (await db.fetchone("SELECT revoked_at FROM watch_authorities WHERE watch_id = ? AND condition_revision = 1", (item.id,)))["revoked_at"]
        assert not await keeper.deliveries.deliver((await db.fetchone("SELECT id FROM watch_deliveries WHERE watch_id = ?", (item.id,)))["id"])
        with pytest.raises(ControlConflict, match="condition"):
            await commands.change(
                principal, project, item.id, note="old revision",
                expected_entity_revision=changed["entity_revision"],
                expected_condition_revision=1, client_operation_id="stale-condition",
            )
    finally:
        await keeper.close()
        await r.manager.close()


async def test_revoked_agent_grant_cancels_reserved_watch_before_wake(settings: Settings, db: Database, tmp_path) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        scope = Scope("project", project.id)
        control = ControlStore(db)
        operator = Principal.operator({"via": "token", "user_id": 1})
        grant = await control.issue_grant(
            operator, Principal("agent:watcher", "agent"), scope,
            operations=["watch.create", "watch.deliver"], effects=["watch.wake"],
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        agent = Principal("agent:watcher", "agent", grant["grant_id"], grant["generation"])
        created = await WatchCommands(keeper).create(
            agent, project, when={"event": "staff_finished"}, then={"action": "wake"},
            expected_collection_revision=await control.revision(scope, Entity("collection", project.id)),
            client_operation_id="granted-watch",
        )
        item = keeper.get(project.id, created["id"])
        assert item is not None
        original = keeper.deliveries.deliver

        async def interrupted(_: str) -> bool:
            return False

        keeper.deliveries.deliver = interrupted
        assert await keeper.fire(item, "finished", source_cursor="bus:grant-one")
        keeper.deliveries.deliver = original
        pending = await db.fetchone("SELECT id,status FROM watch_deliveries WHERE watch_id = ?", (item.id,))
        assert pending["status"] == "pending"
        await control.revoke_grant(operator, grant["grant_id"], reason="standing permission withdrawn")
        assert not await keeper.deliveries.deliver(pending["id"])
        assert (await db.fetchone("SELECT status FROM watch_deliveries WHERE id = ?", (pending["id"],)))["status"] == "cancelled"
        assert not await keeper.fire(item, "again", source_cursor="bus:grant-two")
        assert (await db.fetchone("SELECT count(*) AS n FROM app_events WHERE type = 'watch.fired'"))["n"] == 0
        renewed = await WatchCommands(keeper).change(
            operator, project, item.id, enabled=True,
            expected_entity_revision=await control.revision(scope, Entity("project", project.id)),
            expected_condition_revision=item.condition_revision,
            client_operation_id="operator-approves-existing-watch",
        )
        assert renewed["condition_revision"] == item.condition_revision + 1
        assert renewed["enabled"] is True
        current = keeper.get(project.id, item.id)
        assert current is not None
        current.last_fired_at = None
        assert await keeper.fire(current, "approved", source_cursor="bus:operator-approved")
        assert (await db.fetchone("SELECT count(*) AS n FROM app_events WHERE type = 'watch.fired'"))["n"] == 1
    finally:
        await keeper.close()
        await r.manager.close()


async def test_replaced_coordinator_cannot_fire_old_standing_watch(settings: Settings, db: Database, tmp_path) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        old_session = project.settings.orchestrator.session_id
        scope = Scope("project", project.id)
        control = ControlStore(db)
        operator = Principal.operator({"via": "token", "user_id": 1})
        grant = await control.issue_grant(
            operator, Principal(f"orchestrator:{old_session}", "agent"), scope,
            operations=["watch.create", "watch.deliver"], effects=["watch.wake"],
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        principal = Principal(f"orchestrator:{old_session}", "agent", grant["grant_id"], grant["generation"])
        created = await WatchCommands(keeper).create(
            principal, project, when={"event": "staff_finished"}, then={"action": "wake"},
            expected_collection_revision=await control.revision(scope, Entity("collection", project.id)),
            client_operation_id="coordinator-watch",
        )
        item = keeper.get(project.id, created["id"])
        assert item is not None
        assert await keeper.fire(item, "first", source_cursor="bus:office-one")
        before = (await db.fetchone("SELECT count(*) AS n FROM watch_deliveries WHERE watch_id = ?", (item.id,)))["n"]
        replaced = await r.orch.replace(project.id, "fresh context")
        assert replaced.settings.orchestrator.session_id != old_session
        async with db.transaction() as conn:
            assert await watch_authority_status(
                conn, db, watch_id=item.id, condition_revision=item.condition_revision,
                project_id=project.id, action=item.action,
            ) == "needs_approval"
        item.last_fired_at = None
        assert not await keeper.fire(item, "second", source_cursor="bus:office-two")
        assert (await db.fetchone("SELECT count(*) AS n FROM watch_deliveries WHERE watch_id = ?", (item.id,)))["n"] == before
    finally:
        await keeper.close()
        await r.manager.close()


@pytest.mark.parametrize("kind", ["notify", "tell"])
async def test_grant_withdrawal_waits_for_started_send_and_blocks_next_one(
    settings: Settings, db: Database, tmp_path, kind: str,
) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        if kind == "tell":
            await r.manager.staff.hire(project.id, name="Ada")
        control = ControlStore(db)
        operator = Principal.operator({"via": "token", "user_id": 1})
        scope = Scope("project", project.id)
        grant = await control.issue_grant(
            operator, Principal("agent:watcher", "agent"), scope,
            operations=["watch.create", "watch.deliver"], effects=[f"watch.{kind}"],
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        agent = Principal("agent:watcher", "agent", grant["grant_id"], grant["generation"])
        action = ({"action": "notify", "title": "Check"} if kind == "notify" else
                  {"action": "tell", "staff": "Ada", "text": "Check the result"})
        created = await WatchCommands(keeper).create(
            agent, project, when={"event": "staff_finished"},
            then=action,
            expected_collection_revision=await control.revision(scope, Entity("collection", project.id)),
            client_operation_id=f"{kind}-before-withdrawal",
        )
        item = keeper.get(project.id, created["id"])
        original = keeper.deliveries.deliver

        async def interrupted(_: str) -> bool:
            return False

        keeper.deliveries.deliver = interrupted
        assert await keeper.fire(item, "finished", source_cursor=f"bus:{kind}-before-withdrawal")
        keeper.deliveries.deliver = original
        delivery = await db.fetchone("SELECT id FROM watch_deliveries WHERE watch_id = ?", (item.id,))
        entered = asyncio.Event()
        release = asyncio.Event()
        posted = 0

        class DelayedNotification:
            async def post(self, _: object) -> dict[str, int]:
                nonlocal posted
                entered.set()
                await release.wait()
                posted += 1
                return {"id": posted}

        async def delayed_tell(*_args, **kwargs) -> dict[str, str]:
            nonlocal posted
            entered.set()
            await release.wait()
            posted += 1
            return {"state": "submitted", "message_id": kwargs["message_id"]}

        if kind == "notify":
            r.team.app.notifications = DelayedNotification()
        else:
            r.team.tell = delayed_tell
        sending = asyncio.create_task(keeper.deliveries.deliver(delivery["id"]))
        await asyncio.wait_for(entered.wait(), 2)
        withdrawing = asyncio.create_task(control.revoke_grant(
            operator, grant["grant_id"], reason="operator withdrew watch permission",
        ))
        await asyncio.sleep(0)
        assert not withdrawing.done()
        async with db.authority_effect_lock("project", "another-project"):
            assert not withdrawing.done()
        release.set()
        assert await asyncio.wait_for(sending, 2)
        await asyncio.wait_for(withdrawing, 2)
        assert posted == 1
        item.last_fired_at = None
        assert not await keeper.fire(item, "after withdrawal", source_cursor=f"bus:{kind}-after-withdrawal")
        assert posted == 1
    finally:
        await keeper.close()
        await r.manager.close()


async def test_cancelled_local_send_releases_the_authority_fence(
    settings: Settings, db: Database, tmp_path,
) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        control = ControlStore(db)
        operator = Principal.operator({"via": "token", "user_id": 1})
        scope = Scope("project", project.id)
        grant = await control.issue_grant(
            operator, Principal("agent:watcher", "agent"), scope,
            operations=["watch.create", "watch.deliver"], effects=["watch.notify"],
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        agent = Principal("agent:watcher", "agent", grant["grant_id"], grant["generation"])
        created = await WatchCommands(keeper).create(
            agent, project, when={"event": "staff_finished"}, then={"action": "notify", "title": "Check"},
            expected_collection_revision=await control.revision(scope, Entity("collection", project.id)),
            client_operation_id="notification-cancellation",
        )
        item = keeper.get(project.id, created["id"])
        original = keeper.deliveries.deliver

        async def interrupted(_: str) -> bool:
            return False

        keeper.deliveries.deliver = interrupted
        assert await keeper.fire(item, "finished", source_cursor="bus:notification-cancellation")
        keeper.deliveries.deliver = original
        delivery = await db.fetchone("SELECT id FROM watch_deliveries WHERE watch_id = ?", (item.id,))
        entered = asyncio.Event()

        class CancelledNotification:
            async def post(self, _: object) -> None:
                entered.set()
                await asyncio.Future()

        r.team.app.notifications = CancelledNotification()
        sending = asyncio.create_task(keeper.deliveries.deliver(delivery["id"]))
        await asyncio.wait_for(entered.wait(), 2)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        await asyncio.wait_for(control.revoke_grant(
            operator, grant["grant_id"], reason="operator withdrew watch permission",
        ), 2)
        assert (await db.fetchone("SELECT revoked_at FROM actor_grants WHERE id = ?", (grant["grant_id"],)))["revoked_at"]
    finally:
        await keeper.close()
        await r.manager.close()


async def test_queued_cli_message_rechecks_exact_watch_and_session_at_egress(
    settings: Settings, db: Database, tmp_path,
) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        member = await r.manager.staff.hire(project.id, name="Ada")
        control = ControlStore(db)
        operator = Principal.operator({"via": "token", "user_id": 1})
        scope = Scope("project", project.id)
        grant = await control.issue_grant(
            operator, Principal("agent:watcher", "agent"), scope,
            operations=["watch.create", "watch.deliver"], effects=["watch.tell"],
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        agent = Principal("agent:watcher", "agent", grant["grant_id"], grant["generation"])
        created = await WatchCommands(keeper).create(
            agent, project, when={"event": "staff_finished"},
            then={"action": "tell", "staff": "Ada", "text": "Check the result"},
            expected_collection_revision=await control.revision(scope, Entity("collection", project.id)),
            client_operation_id="queued-cli-egress",
        )
        item = keeper.get(project.id, created["id"])
        original = keeper.deliveries.deliver

        async def interrupted(_: str) -> bool:
            return False

        keeper.deliveries.deliver = interrupted
        assert await keeper.fire(item, "finished", source_cursor="bus:queued-cli-egress")
        keeper.deliveries.deliver = original
        delivery = await db.fetchone("SELECT id,action_json FROM watch_deliveries WHERE watch_id = ?", (item.id,))
        stamp = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO staff_sessions(id,staff_id,kind,status_at,started_at) VALUES (?,?, 'cli',?,?)",
            ("ss-watch-egress", member.id, stamp, stamp),
        )
        await db.execute(
            "INSERT INTO staff_messages(id,staff_id,staff_session_id,origin,text,mode,state,created_at,updated_at)"
            " VALUES (?,?,?,'orchestrator','Check the result','now','queued',?,?)",
            (message_id(delivery["id"]), member.id, "ss-watch-egress", stamp, stamp),
        )
        async with watch_message_guard(db, message_id(delivery["id"]), "ss-watch-egress"):
            pass
        with pytest.raises(WatchEgressDenied):
            async with watch_message_guard(db, message_id(delivery["id"]), "another-session"):
                pass
        await control.revoke_grant(operator, grant["grant_id"], reason="watch permission withdrawn")
        with pytest.raises(WatchEgressDenied):
            async with watch_message_guard(db, message_id(delivery["id"]), "ss-watch-egress"):
                pass
    finally:
        await keeper.close()
        await r.manager.close()


@pytest.mark.parametrize("revoke_before_release,cancel_during_release", [(False, False), (True, False), (False, True)])
async def test_held_watch_notification_rechecks_approval_before_due_release(
    settings: Settings, db: Database, tmp_path, revoke_before_release: bool, cancel_during_release: bool,
) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        control = ControlStore(db)
        operator = Principal.operator({"via": "token", "user_id": 1})
        scope = Scope("project", project.id)
        grant = await control.issue_grant(
            operator, Principal("agent:watcher", "agent"), scope,
            operations=["watch.create", "watch.deliver"], effects=["watch.notify"],
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        agent = Principal("agent:watcher", "agent", grant["grant_id"], grant["generation"])
        created = await WatchCommands(keeper).create(
            agent, project, when={"event": "staff_finished"},
            then={"action": "notify", "title": "Check"},
            expected_collection_revision=await control.revision(scope, Entity("collection", project.id)),
            client_operation_id="held-watch-notification",
        )
        item = keeper.get(project.id, created["id"])
        original = keeper.deliveries.deliver

        async def interrupted(_: str) -> bool:
            return False

        keeper.deliveries.deliver = interrupted
        assert await keeper.fire(item, "finished", source_cursor="bus:held-watch-notification")
        keeper.deliveries.deliver = original
        delivery = await db.fetchone("SELECT id FROM watch_deliveries WHERE watch_id = ?", (item.id,))
        await db.execute("UPDATE watch_deliveries SET status = 'reconciling' WHERE id = ?", (delivery["id"],))
        stamp = datetime.now(UTC).isoformat()
        due = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        await db.execute(
            "INSERT INTO notifications(at,updated_at,kind,category,title,body,project_id,source,dedupe_key,"
            " held_until,delivered_json) VALUES (?,?,'watch','orchestrator_report','Check','finished',?,?,?,?,'{}')",
            (stamp, stamp, project.id, f"watch:{item.id}", f"watch-delivery:{delivery['id']}", due),
        )
        if revoke_before_release:
            await control.revoke_grant(operator, grant["grant_id"], reason="watch permission withdrawn")
        service = NotificationService(db, r.manager.bus)
        if revoke_before_release:
            assert await service.release_due() == 1
        else:
            entered = asyncio.Event()
            proceed = asyncio.Event()
            original_delivery = service._deliver

            async def delayed_delivery(*args, **kwargs):
                entered.set()
                await proceed.wait()
                return await original_delivery(*args, **kwargs)

            service._deliver = delayed_delivery
            releasing = asyncio.create_task(service.release_due())
            await asyncio.wait_for(entered.wait(), 2)
            if cancel_during_release:
                releasing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await releasing
                await control.revoke_grant(operator, grant["grant_id"], reason="watch permission withdrawn")
            else:
                withdrawing = asyncio.create_task(control.revoke_grant(
                    operator, grant["grant_id"], reason="watch permission withdrawn",
                ))
                await asyncio.sleep(0)
                assert not withdrawing.done()
                proceed.set()
                assert await asyncio.wait_for(releasing, 2) == 1
                await asyncio.wait_for(withdrawing, 2)
        notification = await db.fetchone(
            "SELECT event_seq,resolved_at,resolution FROM notifications WHERE dedupe_key = ?",
            (f"watch-delivery:{delivery['id']}",),
        )
        if revoke_before_release:
            assert notification["event_seq"] is None and notification["resolved_at"]
            assert notification["resolution"] == "watch approval withdrawn"
            assert (await db.fetchone("SELECT status FROM watch_deliveries WHERE id = ?", (delivery["id"],)))["status"] == "failed"
        elif cancel_during_release:
            assert notification["event_seq"] is None and notification["resolved_at"] is None
            assert await keeper.deliveries.sweep() == 0
            assert (await db.fetchone("SELECT status FROM watch_deliveries WHERE id = ?", (delivery["id"],)))["status"] == "reconciling"
        else:
            assert notification["event_seq"] and notification["resolved_at"] is None
            assert await keeper.deliveries.sweep() == 1
            assert (await db.fetchone("SELECT status FROM watch_deliveries WHERE id = ?", (delivery["id"],)))["status"] == "delivered"
    finally:
        await keeper.close()
        await r.manager.close()
