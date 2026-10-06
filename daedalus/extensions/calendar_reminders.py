"""Reminders for calendar events and planner tasks, delivered through the app's notifications.

Every half minute the loop looks for reminders whose moment has come and posts one notification
for each, linking to the event or task in the app. Each one is written to
``calendar_reminders_sent`` *before* it is posted: a restart between the two loses that one
reminder rather than repeating every reminder of the last hour, and a reminder repeated after each
restart is the worse failure for something that interrupts the operator.

A reminder is fired only while it is fresh: up to :data:`GRACE` after its moment. One that fell due
while the process was down for longer than that is skipped, because "the meeting starts in 10
minutes" arriving three hours later is noise, and the operator can see the day in the calendar.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from daedalus.extensions.notifications import Draft
from daedalus.host.notify_text import render
from daedalus.stores import recurrence
from daedalus.stores.calendar import CalendarStore, day_anchor
from daedalus.stores.planner import PlannerStore

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

TICK_SECONDS = 30
GRACE = timedelta(hours=1)
"""How late a reminder may still be delivered: covers a restart or a sleeping laptop, not an outage."""
KEEP = timedelta(days=45)
"""How long a delivery is remembered; longer than the longest lead plus the grace, so an edit that
moves an event cannot bring back a reminder already sent for the same moment."""


def _when(start: datetime, end: datetime | None, zone: Any, today: Any) -> str:
    local = start.astimezone(zone)
    text = local.strftime("%H:%M") if local.date() == today else local.strftime("%Y-%m-%d %H:%M")
    if end is not None and end > start:
        text += "–" + end.astimezone(zone).strftime("%H:%M")
    return text


def _lead(minutes: int, language: str) -> str:
    return render("calendar.reminder.lead.now", language) if minutes == 0 else render("calendar.reminder.lead", language, minutes=minutes)


async def due(store: CalendarStore, planner: PlannerStore, moment: datetime, language: str = "") -> list[tuple[str, str, Draft]]:
    """The reminders to deliver at ``moment``: ``(key, fire_at, draft)``, not yet filtered by the log."""
    settings = await store.settings()
    zone = recurrence.zone(settings["timezone"])
    today = moment.astimezone(zone).date()
    found: list[tuple[str, str, Draft]] = []
    lead = await store.longest_lead()
    # All calendars, hidden ones too: hiding a calendar declutters the view, it does not mean the
    # operator wants to miss its meetings. A subscription has no reminders unless they gave it some.
    every = [row["id"] for row in await store.db.fetchall("SELECT id FROM calendars")]
    first = moment - GRACE - timedelta(days=1)
    last = moment + timedelta(minutes=lead) + timedelta(days=1)
    for item in await store.occurrences(first.isoformat(), last.isoformat(), every) if every else []:
        if not item["reminders"]:
            continue
        start = datetime.fromisoformat(item["start_at"])
        anchor = day_anchor(item["start_at"], settings) if item["all_day"] else start
        for minutes in item["reminders"]:
            fire = anchor - timedelta(minutes=minutes)
            if not moment - GRACE <= fire <= moment:
                continue
            key = f"event:{item['event_id']}:{item['occurrence_start'] or item['start_at']}:{minutes}"
            if item["all_day"]:
                body = render("calendar.reminder.all_day", language, day=item["start_date"])
            else:
                when = _when(start, datetime.fromisoformat(item["end_at"]), zone, today) + f" ({_lead(minutes, language)})"
                body = render("calendar.reminder.timed.place" if item["location"] else "calendar.reminder.timed", language, when=when, place=item["location"])
            found.append((key, fire.isoformat(), Draft(
                "reminder", item["title"], body, kind="calendar_reminder", link="/app/calendar?event=" + quote(item["id"], safe=""),
                dedupe_key=key, source="calendar",
            )))
    for task in await planner.reminder_candidates():
        if task["scheduled_start"]:
            anchor, end, template = datetime.fromisoformat(task["scheduled_start"]), datetime.fromisoformat(task["scheduled_end"]) if task["scheduled_end"] else None, "task.reminder.block"
        elif task["due_time"]:
            hours, minutes_part = (int(part) for part in task["due_time"].split(":"))
            anchor = datetime.fromisoformat(task["due_date"]).replace(hour=hours, minute=minutes_part, tzinfo=zone).astimezone(UTC)
            end, template = None, "task.reminder.due"
        else:
            anchor, end, template = day_anchor(task["due_date"], settings), None, "task.reminder.due"
        for minutes in task["reminders"]:
            fire = anchor - timedelta(minutes=minutes)
            if not moment - GRACE <= fire <= moment:
                continue
            key = f"task:{task['id']}:{anchor.isoformat()}:{minutes}"
            when = task["due_date"] if not task["scheduled_start"] and not task["due_time"] else _when(anchor, end, zone, today)
            found.append((key, fire.isoformat(), Draft(
                "reminder", task["title"], render(template, language, when=when), kind="task_reminder",
                link="/app/calendar?task=" + quote(task["id"], safe=""), dedupe_key=key, source="calendar",
            )))
    return found


async def deliver(app: Application | Any, store: CalendarStore, planner: PlannerStore, moment: datetime | None = None) -> int:
    """Post every reminder that is due and not yet delivered; returns how many were posted."""
    moment = moment or datetime.now(UTC)
    language = app.notifications.language()
    posted = 0
    for key, fire_at, draft in await due(store, planner, moment, language):
        async with store.db.transaction() as conn:
            cursor = await conn.execute("INSERT OR IGNORE INTO calendar_reminders_sent(key, fire_at, sent_at) VALUES(?,?,?)", (key, fire_at, moment.isoformat()))
            fresh = cursor.rowcount == 1
        if not fresh:
            continue
        try:
            await app.notifications.post(draft)
            posted += 1
        except Exception:  # noqa: BLE001 — one undeliverable reminder must not stop the others
            logger.exception("could not post the reminder %s", key)
    await store.db.execute("DELETE FROM calendar_reminders_sent WHERE sent_at < ?", ((moment - KEEP).isoformat(),))
    return posted


def _zone_hint(app: Application | Any) -> str:
    presence = getattr(app.notifications, "presence", None)
    return presence.locale()[1] if presence is not None else ""


async def _watch(app: Application) -> None:
    store = CalendarStore(app.db, lambda: _zone_hint(app))
    planner = PlannerStore(app.db)
    while True:
        try:
            await deliver(app, store, planner)
        except Exception:  # noqa: BLE001 — a failed pass is tried again on the next tick, never a crash
            logger.exception("could not check calendar reminders")
        await asyncio.sleep(TICK_SECONDS)


async def install(app: Application) -> list[asyncio.Task[None]]:
    if app.notifications is None:
        return []
    return [asyncio.create_task(_watch(app), name="calendar-reminders")]


__all__ = ["GRACE", "deliver", "due", "install"]
