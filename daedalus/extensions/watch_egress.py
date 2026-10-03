"""Fence a watch-owned queued message again where the CLI actually writes it."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from daedalus.extensions.watch_authority import authorized
from daedalus.stores.control import one
from daedalus.stores.database import Database


class WatchEgressDenied(RuntimeError):
    """The standing approval was withdrawn before the queued message reached its channel."""

    def __init__(self, reason: str, *, delivery_id: str | None = None) -> None:
        super().__init__(reason)
        self.delivery_id = delivery_id


@asynccontextmanager
async def watch_message_guard(db: Database, message_id: str, staff_session_id: str) -> AsyncIterator[None]:
    """Hold an exact project's approval fence through one terminal write or structured send.

    Other staff messages have no watch delivery row and keep their ordinary path. A queued
    watch message is checked after waiting for a CLI window, so withdrawal never waits for
    an idle terminal; each later paste or Enter gets a fresh check.
    """
    delivery_id = message_id.removeprefix("sm-") if message_id.startswith("sm-") else ""
    row = await db.fetchone("SELECT project_id FROM watch_deliveries WHERE id = ?", (delivery_id,)) if delivery_id else None
    if row is None:
        yield
        return
    project_id = str(row["project_id"])
    async with db.authority_effect_lock("project", project_id):
        async with db.transaction() as conn:
            source = await one(conn, "SELECT d.*,w.enabled,w.condition_revision AS current_revision,"
                               " w.project_id AS current_project,w.state_json FROM watch_deliveries d"
                               " LEFT JOIN watches w ON w.id = d.watch_id WHERE d.id = ?", (delivery_id,))
            if (source is None or source["project_id"] != project_id or source["current_project"] != project_id
                    or source["current_revision"] != source["condition_revision"]
                    or source["status"] not in ("pending", "reconciling")
                    or (source["deadline_at"] is not None and
                        datetime.fromisoformat(source["deadline_at"]) <= datetime.now(UTC))
                    or not source["enabled"] and json.loads(source["state_json"] or "{}").get("stopped") != "once"):
                raise WatchEgressDenied("watch permission changed before queued message delivery",
                                        delivery_id=delivery_id)
            snapshot = json.loads(source["action_json"] or "{}")
            action = snapshot.get("action")
            message = await one(conn, "SELECT m.staff_id,m.staff_session_id,m.state,m.text,m.mode,m.origin,s.project_id"
                                " FROM staff_messages m JOIN staff s ON s.id = m.staff_id WHERE m.id = ?",
                                (message_id,))
            if (not isinstance(action, dict) or action.get("action") != "tell"
                    or message is None or message["staff_id"] != action.get("staff_id")
                    or message["staff_session_id"] != staff_session_id or message["project_id"] != project_id
                    or message["text"] != action.get("text")
                    or message["mode"] != action.get("when")
                    or message["origin"] != snapshot.get("created_by")
                    or message["state"] not in ("queued", "written", "submitted")
                    or not await authorized(conn, db, watch_id=source["watch_id"],
                                            condition_revision=int(source["condition_revision"]),
                                            project_id=project_id, action=action)):
                raise WatchEgressDenied("watch permission changed before queued message delivery",
                                        delivery_id=delivery_id)
        yield


@asynccontextmanager
async def watch_notification_guard(db: Database, notification_id: int) -> AsyncIterator[None]:
    """Recheck a held watch notification when its deferred channel release actually begins."""
    row = await db.fetchone("SELECT project_id,dedupe_key FROM notifications WHERE id = ?", (notification_id,))
    if row is None or not str(row["dedupe_key"] or "").startswith("watch-delivery:"):
        yield
        return
    delivery_id = str(row["dedupe_key"]).removeprefix("watch-delivery:")
    project_id = str(row["project_id"] or "")
    if not project_id:
        raise WatchEgressDenied("a watch notification lost its project", delivery_id=delivery_id)
    async with db.authority_effect_lock("project", project_id):
        async with db.transaction() as conn:
            source = await one(conn, "SELECT d.*,w.enabled,w.condition_revision AS current_revision,"
                               " w.project_id AS current_project,w.state_json,n.project_id AS notification_project,"
                               " n.dedupe_key,n.source,n.resolved_at FROM notifications n"
                               " LEFT JOIN watch_deliveries d ON n.dedupe_key = 'watch-delivery:' || d.id"
                               " LEFT JOIN watches w ON w.id = d.watch_id WHERE n.id = ?", (notification_id,))
            if (source is None or source["id"] != delivery_id or source["project_id"] != project_id
                    or source["notification_project"] != project_id or source["current_project"] != project_id
                    or source["current_revision"] != source["condition_revision"]
                    or source["source"] != f"watch:{source['watch_id']}" or source["resolved_at"]
                    or source["status"] not in ("reconciling", "delivered")
                    or (source["deadline_at"] is not None and
                        datetime.fromisoformat(source["deadline_at"]) <= datetime.now(UTC))
                    or not source["enabled"] and json.loads(source["state_json"] or "{}").get("stopped") != "once"):
                raise WatchEgressDenied("watch permission changed before notification release",
                                        delivery_id=delivery_id)
            snapshot = json.loads(source["action_json"] or "{}")
            action = snapshot.get("action")
            if (not isinstance(action, dict) or action.get("action") != "notify"
                    or not await authorized(conn, db, watch_id=source["watch_id"],
                                            condition_revision=int(source["condition_revision"]),
                                            project_id=project_id, action=action)):
                raise WatchEgressDenied("watch permission changed before notification release",
                                        delivery_id=delivery_id)
        yield
