"""Calendar hooks: the transitions of the operator's plans, handed to the agent's standing intents.

The calendar store writes a row into ``calendar_hooks`` in the same transaction as the change that
caused it (a checklist finished, an event's status changed, every event of a group done), so a
transition is recorded exactly when the change is. This loop delivers each row once, through the
same matcher an inbound webhook goes through: the row is claimed before it is matched, so a restart
between the two loses that one delivery rather than repeating it, and a follow-up plan an agent
attached to "the basket is complete" is never generated twice.

The text a pattern matches begins with ``calendar <kind>``, for example::

    calendar checklist.completed: "Order 1" block "Basket" 47/47 (event 3f2…, block blk_…, group grp_…)

so an agent subscribes with ``IntentCreate(pattern="calendar checklist\\.completed.*Basket", …)``.

The same listener tells the app's live stream that the calendar changed (``calendar.changed``), so
an open calendar reads again instead of waiting for its poll.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from daedalus.stores.calendar import listen

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

TICK_SECONDS = 30
"""How often the outbox is read when nothing woke the loop: covers a write made by another process."""
KEEP = timedelta(days=30)


def hook_text(row: Any) -> str:
    detail = json.loads(row["detail_json"] or "{}")
    kind = row["kind"]
    ids = ", ".join(part for part in (
        f"event {row['event_id']}" if row["event_id"] else "",
        f"block {detail['block_id']}" if detail.get("block_id") else "",
        f"group {row['group_id']}" if row["group_id"] else "",
    ) if part)
    if kind == "checklist.completed":
        what = f"{detail.get('title', '')!r} block {detail.get('block_title') or detail.get('block_kind', '')!r} {detail.get('done')}/{detail.get('total')}"
    elif kind == "event.status_changed":
        what = f"{detail.get('title', '')!r} {detail.get('from')} -> {detail.get('to')}"
    else:
        what = f"group {detail.get('name', '')!r}, {detail.get('events')} events done"
    by = f" by {detail['actor']}" if detail.get("actor") else ""
    return f"calendar {kind}: {what}{by} ({ids})"


async def deliver(app: Application | Any) -> int:
    """Hand every undelivered hook to the standing intents; returns how many were delivered."""
    db = app.db
    inbound = app.extensions.get("inbound")
    delivered = 0
    for row in await db.fetchall("SELECT * FROM calendar_hooks WHERE delivered_at IS NULL ORDER BY id LIMIT 200"):
        async with db.transaction() as conn:
            cursor = await conn.execute("UPDATE calendar_hooks SET delivered_at=? WHERE id=? AND delivered_at IS NULL", (datetime.now(UTC).isoformat(), row["id"]))
            fresh = cursor.rowcount == 1
        if not fresh:
            continue
        delivered += 1
        if inbound is None:
            continue
        try:
            await inbound.match_intents("calendar", hook_text(row))
        except Exception:  # noqa: BLE001 — one failed delivery must not hold back the rest
            logger.exception("could not deliver the calendar hook %s", row["id"])
    await db.execute("DELETE FROM calendar_hooks WHERE delivered_at IS NOT NULL AND delivered_at < ?", ((datetime.now(UTC) - KEEP).isoformat(),))
    return delivered


async def _run(app: Application | Any, wake: asyncio.Event) -> None:
    while True:
        wake.clear()
        try:
            await deliver(app)
        except Exception:  # noqa: BLE001 — a failed pass is tried again on the next wake, never a crash
            logger.exception("could not deliver calendar hooks")
        try:
            await asyncio.wait_for(wake.wait(), TICK_SECONDS)
        except TimeoutError:
            pass


async def install(app: Application) -> list[asyncio.Task[None]]:
    wake = asyncio.Event()
    bus = getattr(app.manager, "bus", None) if app.manager is not None else None

    def changed(payload: dict[str, Any]) -> None:
        if bus is not None:
            try:
                bus.publish_soon("calendar.changed", payload)
            except Exception:  # noqa: BLE001 — the live refresh is a courtesy; the poll still comes
                logger.warning("could not announce a calendar change", exc_info=True)
        wake.set()

    listen(app.db, changed)
    return [asyncio.create_task(_run(app, wake), name="calendar-hooks")]


__all__ = ["deliver", "hook_text", "install"]
