"""Deliver committed standing-watch intents without replaying uncertain external sends."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from daedalus.extensions.notifications import Draft
from daedalus.extensions.watch_authority import authorized as watch_authorized
from daedalus.stores.control import now
from daedalus.stores.staff import StaffError

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)


def message_id(delivery_id: str) -> str:
    return f"sm-{delivery_id}"


class WatchDeliveries:
    def __init__(self, app: Application) -> None:
        self.app = app
        self._lock = asyncio.Lock()

    async def _event(self, delivery: Any, snapshot: dict[str, Any], error: str = "") -> int:
        existing = await self.app.db.fetchone(
            "SELECT seq FROM app_events WHERE type = 'watch.fired'"
            " AND json_extract(payload_json,'$.delivery_id') = ? ORDER BY seq LIMIT 1", (delivery["id"],),
        )
        if existing is not None:
            return int(existing["seq"])
        bus = self.app.manager.bus
        event = await bus.publish(
            "watch.fired",
            {"watch_id": delivery["watch_id"], "delivery_id": delivery["id"],
             "fire_count": snapshot["fire_count"], "pattern": snapshot["pattern"],
             "action": snapshot["action"]["action"], "note": snapshot["note"],
             "detail": delivery["detail"][:500], "actor": "system",
             **({"error": error[:300]} if error else {})},
            project_id=delivery["project_id"], staff_id=delivery["staff_id"],
        )
        if event.seq < 1:
            raise RuntimeError("watch event was not persisted")
        return event.seq

    async def _observed(self, delivery: Any, snapshot: dict[str, Any]) -> bool:
        action = snapshot["action"]["action"]
        if action == "wake":
            event = await self.app.db.fetchone(
                "SELECT seq FROM app_events WHERE type = 'watch.fired'"
                " AND json_extract(payload_json,'$.delivery_id') = ? ORDER BY seq LIMIT 1", (delivery["id"],),
            )
            return event is not None
        if action == "notify":
            notification = await self.app.db.fetchone(
                "SELECT id,event_seq,held_until,resolved_at FROM notifications"
                " WHERE dedupe_key = ? ORDER BY id DESC LIMIT 1",
                (f"watch-delivery:{delivery['id']}",),
            )
            return bool(notification is not None and notification["event_seq"] is not None
                        and notification["held_until"] is None and notification["resolved_at"] is None)
        if action == "tell":
            message = await self.app.db.fetchone(
                "SELECT state FROM staff_messages WHERE id = ?", (message_id(delivery["id"]),),
            )
            return message is not None and message["state"] in ("submitted", "acknowledged")
        return False

    async def _claim(self, delivery_id: str) -> Any | None:
        async with self.app.db.transaction() as conn:
            cursor = await conn.execute("SELECT * FROM watch_deliveries WHERE id = ? AND status = 'pending'", (delivery_id,))
            delivery = await cursor.fetchone()
            await cursor.close()
            if delivery is None:
                return None
            snapshot = json.loads(delivery["action_json"])
            if not snapshot:
                await conn.execute("UPDATE watch_deliveries SET status = 'reconciling',updated_at = ? WHERE id = ?",
                                   (now(), delivery_id))
                return None
            watch = await conn.execute("SELECT enabled,condition_revision,deadline_at,state_json,project_id,action_json FROM watches WHERE id = ?",
                                       (delivery["watch_id"],))
            current = await watch.fetchone()
            await watch.close()
            deadline = delivery["deadline_at"]
            expired = bool(deadline and datetime.fromisoformat(deadline) <= datetime.now(UTC))
            once = bool(current and json.loads(current["state_json"] or "{}").get("stopped") == "once")
            if (current is None or int(current["condition_revision"]) != delivery["condition_revision"]
                    or current["project_id"] != delivery["project_id"]
                    or (not current["enabled"] and not once) or expired
                    or not await watch_authorized(
                        conn, self.app.db, watch_id=delivery["watch_id"],
                        condition_revision=int(delivery["condition_revision"]),
                        project_id=delivery["project_id"],
                        action=json.loads(current["action_json"]),
                    )):
                await conn.execute("UPDATE watch_deliveries SET status = 'cancelled',updated_at = ? WHERE id = ?",
                                   (now(), delivery_id))
                return None
            await conn.execute("UPDATE watch_deliveries SET status = 'reconciling',claimed_at = ?,updated_at = ?"
                               " WHERE id = ? AND status = 'pending'", (now(), now(), delivery_id))
            return delivery

    async def _act(self, delivery: Any, snapshot: dict[str, Any]) -> tuple[str, str | None]:
        # A grant can expire after reservation; the same exact version must still be approved
        # at the boundary where the local wake or external message is attempted.
        async with self.app.db.transaction() as conn:
            if not await watch_authorized(
                conn, self.app.db, watch_id=delivery["watch_id"],
                condition_revision=int(delivery["condition_revision"]),
                project_id=delivery["project_id"], action=snapshot["action"],
            ):
                return "watch standing approval is no longer current", None
        action = snapshot["action"]
        kind = action["action"]
        if kind == "wake":
            event_seq = await self._event(delivery, snapshot)
            return "", f"event:{event_seq}"
        if kind == "notify":
            notifications = self.app.notifications
            if notifications is None:
                return "notifications are unavailable", None
            project = await self.app.manager.projects.get(delivery["project_id"])
            name = project.name if project is not None else delivery["project_id"]
            body = str(action.get("text") or "")
            result = await notifications.post(Draft(
                "orchestrator_report", f"{name}: {action.get('title')}",
                (body + "\n\n" if body else "") + delivery["detail"], kind="watch",
                level=action.get("level") or "normal", project_id=delivery["project_id"],
                link=f"/app/project/{delivery['project_id']}/wakeups",
                source=f"watch:{delivery['watch_id']}",
                dedupe_key=f"watch-delivery:{delivery['id']}",
            ))
            if result is None:
                return "notification was suppressed before persistence", None
            if result.get("delivered", {}).get("held"):
                return "", None
            return "", f"notification:{result['id']}"
        if kind == "tell":
            team = self.app.extensions.get("staff")
            member = await self.app.manager.staff.get(str(action.get("staff_id") or ""))
            if team is None or member is None or not member.active or member.project_id != delivery["project_id"]:
                return "the watched member is unavailable", None
            try:
                receipt = await team.tell(
                    member, str(action.get("text") or ""), when=str(action.get("when") or "now"),
                    by=snapshot["created_by"], message_id=message_id(delivery["id"]),
                )
            except StaffError as exc:
                return str(exc), None
            state = str(receipt.get("state") or "")
            error = str(receipt.get("error") or "delivery failed") if state == "failed" else ""
            return error, str(receipt["message_id"]) if state in ("submitted", "acknowledged") else None
        return "unknown watch action", None

    async def deliver(self, delivery_id: str) -> bool:
        async with self._lock:
            source = await self.app.db.fetchone(
                "SELECT project_id FROM watch_deliveries WHERE id = ?", (delivery_id,),
            )
            if source is None:
                return False
            # Withdrawal acquires the same project fence before committing the new grant
            # generation. The database lock stays free while a local physical send awaits.
            async with self.app.db.authority_effect_lock("project", source["project_id"]):
                return await self._deliver_fenced(delivery_id, source["project_id"])

    async def _deliver_fenced(self, delivery_id: str, project_id: str) -> bool:
        delivery = await self._claim(delivery_id)
        if delivery is None:
            return False
        if delivery["project_id"] != project_id:
            raise RuntimeError("a watch delivery changed its owning project")
        snapshot = json.loads(delivery["action_json"])
        try:
            error, receipt_id = await self._act(delivery, snapshot)
            kind = snapshot["action"]["action"]
            if kind != "wake" and (error or receipt_id):
                event_seq = await self._event(delivery, snapshot, error)
                receipt_id = receipt_id or f"event:{event_seq}"
        except Exception:  # noqa: BLE001 — the external send may have completed before its local receipt
            logger.exception("watch delivery %s has an uncertain outcome", delivery_id)
            await self.app.db.execute(
                "UPDATE watch_deliveries SET last_error = ?,updated_at = ?"
                " WHERE id = ? AND status = 'reconciling'",
                ("delivery outcome is being reconciled", now(), delivery_id),
            )
            return False
        status = "failed" if error else ("delivered" if receipt_id else "reconciling")
        await self.app.db.execute(
            "UPDATE watch_deliveries SET status = ?,receipt_id = ?,last_error = ?,updated_at = ?"
            " WHERE id = ? AND status = 'reconciling'",
            (status, receipt_id, error[:500] if error else
             ("waiting for a confirmed delivery receipt" if status == "reconciling" else ""), now(), delivery_id),
        )
        return status == "delivered"

    async def sweep(self, *, limit: int = 50) -> int:
        """Run new intents; inspect interrupted sends without repeating a possible effect."""
        rows = await self.app.db.fetchall(
            "SELECT * FROM watch_deliveries WHERE status IN ('pending','reconciling')"
            " ORDER BY created_at,id LIMIT ?", (max(1, min(limit, 100)),),
        )
        changed = 0
        for row in rows:
            if row["status"] == "pending":
                changed += int(await self.deliver(row["id"]))
                continue
            retry = False
            async with self._lock:
                latest = await self.app.db.fetchone("SELECT * FROM watch_deliveries WHERE id = ?", (row["id"],))
                if latest is None or latest["status"] != "reconciling":
                    continue
                snapshot = json.loads(latest["action_json"])
                if not snapshot:
                    continue
                event = await self.app.db.fetchone(
                    "SELECT seq,payload_json FROM app_events WHERE type = 'watch.fired'"
                    " AND json_extract(payload_json,'$.delivery_id') = ? ORDER BY seq LIMIT 1", (latest["id"],),
                )
                if event is not None and (snapshot["action"]["action"] == "wake" or json.loads(event["payload_json"]).get("error")):
                    error = str(json.loads(event["payload_json"]).get("error") or "")
                    await self.app.db.execute("UPDATE watch_deliveries SET status = ?,receipt_id = COALESCE(receipt_id,?),last_error = ?,updated_at = ?"
                                              " WHERE id = ? AND status = 'reconciling'",
                                              ("failed" if error else "delivered", f"event:{event['seq']}", error[:500], now(), latest["id"]))
                    changed += 1
                    continue
                if snapshot["action"]["action"] == "tell":
                    message = await self.app.db.fetchone("SELECT state,error FROM staff_messages WHERE id = ?",
                                                         (message_id(latest["id"]),))
                    if message is not None and message["state"] == "failed":
                        await self._event(latest, snapshot, str(message["error"] or "delivery failed"))
                        await self.app.db.execute("UPDATE watch_deliveries SET status = 'failed',receipt_id = ?,last_error = ?,updated_at = ?"
                                                  " WHERE id = ? AND status = 'reconciling'",
                                                  (message_id(latest["id"]), str(message["error"] or "delivery failed")[:500], now(), latest["id"]))
                        changed += 1
                        continue
                if await self._observed(latest, snapshot):
                    receipt_id = None
                    if snapshot["action"]["action"] != "wake":
                        event_seq = await self._event(latest, snapshot)
                        receipt_id = f"event:{event_seq}"
                    if snapshot["action"]["action"] == "tell":
                        receipt_id = message_id(latest["id"])
                    elif snapshot["action"]["action"] == "notify":
                        notification = await self.app.db.fetchone(
                            "SELECT id FROM notifications WHERE dedupe_key = ? ORDER BY id DESC LIMIT 1",
                            (f"watch-delivery:{latest['id']}",),
                        )
                        if notification is not None:
                            receipt_id = f"notification:{notification['id']}"
                    await self.app.db.execute("UPDATE watch_deliveries SET status = 'delivered',receipt_id = COALESCE(receipt_id,?),updated_at = ?"
                                              " WHERE id = ? AND status = 'reconciling'", (receipt_id, now(), latest["id"]))
                    changed += 1
                    continue
                kind = snapshot["action"]["action"]
                if kind == "tell":
                    possible_send = await self.app.db.fetchone("SELECT state FROM staff_messages WHERE id = ?",
                                                               (message_id(latest["id"]),))
                    if possible_send is not None:
                        # A queued or written message may already have reached the runtime; only
                        # its exact receipt or operator reconciliation can decide the outcome.
                        continue
                if kind == "notify":
                    recorded = await self.app.db.fetchone(
                        "SELECT held_until,resolved_at,event_seq FROM notifications WHERE dedupe_key = ?"
                        " ORDER BY id DESC LIMIT 1", (f"watch-delivery:{latest['id']}",),
                    )
                    if recorded is not None:
                        # Persistence is not delivery. A held row may be released only under
                        # its standing approval; an interrupted channel send stays uncertain.
                        if recorded["resolved_at"] and recorded["event_seq"] is None:
                            await self.app.db.execute(
                                "UPDATE watch_deliveries SET status = 'failed',last_error = ?,updated_at = ?"
                                " WHERE id = ? AND status = 'reconciling'",
                                ("notification closed before delivery", now(), latest["id"]),
                            )
                            changed += 1
                        continue
                if kind in ("wake", "notify", "tell"):
                    await self.app.db.execute("UPDATE watch_deliveries SET status = 'pending',updated_at = ?"
                                              " WHERE id = ? AND status = 'reconciling'", (now(), latest["id"]))
                    retry = True
            if retry:
                changed += int(await self.deliver(row["id"]))
        return changed
