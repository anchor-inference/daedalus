"""Agent access to the operator's calendar and planner; the same stores back the web app."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.stores import recurrence as rules
from daedalus.stores.calendar import CalendarStore, moment
from daedalus.stores.planner import PlannerStore
from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import error, ok, services_for

OCCURRENCE_FIELDS = ("id", "event_id", "calendar_id", "calendar_name", "title", "start_at", "end_at", "all_day", "start_date", "end_date", "timezone", "location", "recurring", "occurrence_start", "version", "writable", "pending_sync", "conflict")
"""What the agent sees of an occurrence: enough to answer and to edit, without descriptions that
would fill its context on a busy month."""


def _db(context: ToolContext) -> Any:
    return services_for(context).extra["manager"].db


def _store(context: ToolContext) -> CalendarStore:
    return CalendarStore(_db(context))


def _planner(context: ToolContext) -> PlannerStore:
    return PlannerStore(_db(context))


def _compact(item: dict[str, Any]) -> dict[str, Any]:
    # Empty and false fields are left out, except the two an edit or a reading always needs.
    return {key: item[key] for key in OCCURRENCE_FIELDS if key in item and (item[key] not in (None, "", False) or key in ("version", "all_day"))}


def _ids(text: str) -> list[str] | None:
    return [part.strip() for part in text.split(",") if part.strip()] or None


@tool_group("calendar")
@search_hint("calendar events meetings appointments agenda schedule busy встреча событие календарь показать посмотреть расписание планы неделю сегодня завтра занят")
@tool(name="CalendarEvents", description="List calendar occurrences in a time range, recurring series expanded, with each calendar's name. Give timezone-aware ISO 8601 start and end (defaults: now and 7 days later). calendars is an optional comma-separated list of calendar ids; hidden calendars are left out unless named. query searches titles, descriptions and places instead of a range.")
async def calendar_events(context: ToolContext, start: str = "", end: str = "", calendars: str = "", query: str = "") -> ToolResult:
    store = _store(context)
    try:
        if query.strip():
            found = await store.search(query, 30)
        else:
            first = datetime.now(UTC)
            found = await store.occurrences(start or first.isoformat(), end or (first + timedelta(days=7)).isoformat(), _ids(calendars), 300)
    except ValueError as exc:
        return error(context, str(exc))
    settings = await store.settings()
    return ok(context, json.dumps({"timezone": settings["timezone"], "events": [_compact(item) for item in found]}, ensure_ascii=False))


@tool_group("calendar")
@search_hint("calendar list calendars accounts connected providers Google Outlook Yandex iCloud CalDAV subscription календари список календарей аккаунты подключения синхронизация подписка часовой пояс рабочие часы")
@tool(name="CalendarList", description="List the operator's calendars (local ones, connected accounts and read-only subscriptions) with id, kind, colour, visibility, whether events can be written there, and sync status; plus the calendar settings (time zone, working hours). Credentials are never returned.")
async def calendar_list(context: ToolContext) -> ToolResult:
    store = _store(context)
    return ok(context, json.dumps({"calendars": await store.calendars(), "settings": await store.settings()}, ensure_ascii=False))


@tool_group("calendar")
@search_hint("calendar free time slot find available when can meet busy schedule найти свободное время окно когда свободен слот встреча созвон")
@tool(name="CalendarFindTime", description="Find free slots of at least duration_minutes between start and end (defaults: now to 7 days later), inside the operator's working hours unless any_time is true, skipping weekends unless weekends is true. Busy time is every timed event on the visible calendars (or the listed calendar ids) and every time-blocked planner task. Use this before proposing a time; never guess free time.")
async def calendar_find_time(context: ToolContext, duration_minutes: int = 30, start: str = "", end: str = "", calendars: str = "", weekends: bool = False, any_time: bool = False, limit: int = 10) -> ToolResult:
    store, planner = _store(context), _planner(context)
    if not 5 <= duration_minutes <= 24 * 60:
        return error(context, "duration_minutes is 5 to 1440")
    try:
        now_moment = datetime.now(UTC)
        first = max(moment(start), now_moment) if start else now_moment
        last = moment(end) if end else first + timedelta(days=7)
        if last <= first:
            return error(context, "the range needs an end after its start (and after now)")
        if last - first > timedelta(days=62):
            return error(context, "look for free time at most two months ahead at a time")
        settings = await store.settings()
        busy = [(moment(item["start_at"]), moment(item["end_at"])) for item in await store.occurrences(first.isoformat(), last.isoformat(), _ids(calendars)) if not item["all_day"]]
        for task in await planner.tasks("all", start=first.isoformat(), end=last.isoformat(), timezone=settings["timezone"]):
            if task["scheduled_start"] and task["scheduled_end"] and not task["done"]:
                busy.append((moment(task["scheduled_start"]), moment(task["scheduled_end"])))
    except ValueError as exc:
        return error(context, str(exc))
    zone = rules.zone(settings["timezone"])
    work_start = time.fromisoformat(settings["work_start"])
    work_end = time.fromisoformat(settings["work_end"])
    busy.sort()
    slots: list[dict[str, str]] = []
    day = first.astimezone(zone).date()
    while len(slots) < max(1, min(limit, 50)) and datetime.combine(day, time(), zone) < last:
        if weekends or day.weekday() < 5:
            opens = datetime.combine(day, time() if any_time else work_start, zone)
            closes = datetime.combine(day + timedelta(days=1), time(), zone) if any_time else datetime.combine(day, work_end, zone)
            cursor, closes = max(opens, first), min(closes, last)
            for begins, ends in busy:
                if ends <= cursor or begins >= closes:
                    continue
                if begins - cursor >= timedelta(minutes=duration_minutes):
                    slots.append({"start": cursor.astimezone(zone).isoformat(), "end": begins.astimezone(zone).isoformat()})
                cursor = max(cursor, ends)
            if closes - cursor >= timedelta(minutes=duration_minutes):
                slots.append({"start": cursor.astimezone(zone).isoformat(), "end": closes.astimezone(zone).isoformat()})
        day += timedelta(days=1)
    return ok(context, json.dumps({"timezone": settings["timezone"], "working_hours": [settings["work_start"], settings["work_end"]], "slots": slots[:limit]}, ensure_ascii=False))


@tool_group("calendar")
@search_hint("calendar create event meeting appointment add schedule recurring добавить создать встречу событие календарь запланировать назначить повтор каждую неделю")
@tool(name="CalendarCreate", description="Create an event. start_at and end_at are timezone-aware ISO 8601 instants; timezone is the IANA zone the event lives in (a recurring event repeats on that zone's wall clock). For all_day, give the dates' midnights; the end is exclusive. calendar_id comes from CalendarList; omit it for the operator's default local calendar. recurrence is an RRULE such as FREQ=WEEKLY;BYDAY=MO. reminders are minutes before the start; omit them to use the calendar's defaults.")
async def calendar_create(context: ToolContext, title: str, start_at: str, end_at: str, timezone: str = "", calendar_id: str = "", description: str = "", location: str = "", all_day: bool = False, recurrence: str = "", reminders: list[int] | None = None) -> ToolResult:
    store = _store(context)
    body: dict[str, Any] = {"title": title, "start_at": start_at, "end_at": end_at, "timezone": timezone or (await store.settings())["timezone"], "description": description, "location": location, "all_day": all_day, "recurrence": recurrence}
    if calendar_id:
        body["calendar_id"] = calendar_id
    if reminders is not None:
        body["reminders"] = reminders
    try:
        event = await store.create(body)
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, json.dumps(_compact(event), ensure_ascii=False), event_id=event["event_id"])


@tool_group("calendar")
@search_hint("calendar edit move change reschedule event update occurrence перенести изменить событие встречу календарь время дату поправить только эту все")
@tool(name="CalendarUpdate", description="Change an event. Read it with CalendarEvents first and pass its event_id and version; a stale version is refused. For a recurring event, scope='this' with occurrence_start changes that one occurrence; scope='all' changes the whole series (with occurrence_start, the new start_at/end_at are that occurrence's new times and the series moves by the same amount). Omitted fields keep their values.")
async def calendar_update(context: ToolContext, event_id: str, version: int, scope: str = "all", occurrence_start: str = "", title: str | None = None, start_at: str | None = None, end_at: str | None = None, timezone: str | None = None, description: str | None = None, location: str | None = None, all_day: bool | None = None, recurrence: str | None = None, reminders: list[int] | None = None, calendar_id: str | None = None) -> ToolResult:
    changes = {"title": title, "start_at": start_at, "end_at": end_at, "timezone": timezone, "description": description, "location": location, "all_day": all_day, "recurrence": recurrence, "reminders": reminders, "calendar_id": calendar_id}
    body: dict[str, Any] = {"version": version, "scope": scope, **{key: value for key, value in changes.items() if value is not None}}
    if occurrence_start:
        body["occurrence_start"] = occurrence_start
    try:
        event = await _store(context).update(event_id, body)
    except (ValueError, KeyError, RuntimeError) as exc:
        return error(context, str(exc) if not isinstance(exc, KeyError) else f"no event {event_id}")
    return ok(context, json.dumps(_compact(event), ensure_ascii=False))


@tool_group("calendar")
@search_hint("calendar delete cancel event meeting occurrence удалить отменить событие встречу календарь убрать запись только эту")
@tool(name="CalendarDelete", description="Delete an event by event_id and version. For a recurring event, scope='this' with occurrence_start removes one occurrence; scope='all' removes the series. A connected calendar's event is removed from its provider on the next sync.")
async def calendar_delete(context: ToolContext, event_id: str, version: int, scope: str = "all", occurrence_start: str = "") -> ToolResult:
    try:
        await _store(context).delete(event_id, version, scope, occurrence_start or None)
    except (ValueError, KeyError, RuntimeError) as exc:
        return error(context, str(exc) if not isinstance(exc, KeyError) else f"no event {event_id}")
    return ok(context, f"deleted {'one occurrence of ' if scope == 'this' else ''}event {event_id}")


@tool_group("calendar")
@search_hint("planner tasks todo to-do list inbox today overdue upcoming done задачи дела список входящие сегодня просрочено что сделать туду")
@tool(name="PlannerTasks", description="List the operator's personal planner tasks (their own to-dos, not the agent's Board). view is inbox, today, upcoming, overdue, done or all; list_id narrows to one list; or give start and end (ISO 8601) for tasks due or time-blocked in that range. Also returns the task lists.")
async def planner_tasks(context: ToolContext, view: str = "today", list_id: str = "", start: str = "", end: str = "") -> ToolResult:
    planner = _planner(context)
    settings = await _store(context).settings()
    try:
        found = await planner.tasks(view, list_id or None, start or None, end or None, settings["timezone"])
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, json.dumps({"timezone": settings["timezone"], "lists": await planner.lists(), "tasks": found}, ensure_ascii=False))


@tool_group("calendar")
@search_hint("planner add task todo remind deadline due time block добавить задачу дело напомнить срок дедлайн запланировать задачу")
@tool(name="PlannerTaskCreate", description="Add a task to the operator's planner. due_date is YYYY-MM-DD and due_time HH:MM in the calendar's time zone; scheduled_start/scheduled_end (ISO 8601 instants) block time for it on the calendar. priority 0-3; reminders are minutes before the due time or the block; recurrence is an RRULE repeating from the due date. Omit list_id for the Inbox.")
async def planner_task_create(context: ToolContext, title: str, list_id: str = "", notes: str = "", due_date: str = "", due_time: str = "", scheduled_start: str = "", scheduled_end: str = "", priority: int = 0, reminders: list[int] | None = None, recurrence: str = "") -> ToolResult:
    body = {"title": title, "list_id": list_id or None, "notes": notes, "due_date": due_date or None, "due_time": due_time or None, "scheduled_start": scheduled_start or None, "scheduled_end": scheduled_end or None, "priority": priority, "reminders": reminders or [], "recurrence": recurrence}
    try:
        task = await _planner(context).create(body)
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, json.dumps(task, ensure_ascii=False), task_id=task["id"])


@tool_group("calendar")
@search_hint("planner change task edit reschedule move due date delete изменить задачу перенести срок задачи поменять удалить отложить переименовать")
@tool(name="PlannerTaskUpdate", description="Change a planner task by task_id and version (read it with PlannerTasks first). Omitted fields keep their values; pass clear=['due_date', 'scheduled_start', ...] to remove a field. Set delete=true to delete the task instead.")
async def planner_task_update(context: ToolContext, task_id: str, version: int, title: str | None = None, list_id: str | None = None, notes: str | None = None, due_date: str | None = None, due_time: str | None = None, scheduled_start: str | None = None, scheduled_end: str | None = None, priority: int | None = None, reminders: list[int] | None = None, recurrence: str | None = None, clear: list[str] | None = None, delete: bool = False) -> ToolResult:
    planner = _planner(context)
    try:
        if delete:
            await planner.delete(task_id, version)
            return ok(context, f"deleted task {task_id}")
        changes = {"title": title, "list_id": list_id, "notes": notes, "due_date": due_date, "due_time": due_time, "scheduled_start": scheduled_start, "scheduled_end": scheduled_end, "priority": priority, "reminders": reminders, "recurrence": recurrence}
        body: dict[str, Any] = {"version": version, **{key: value for key, value in changes.items() if value is not None}}
        for name in clear or []:
            if name in ("due_date", "due_time", "scheduled_start", "scheduled_end", "duration", "recurrence", "notes"):
                body[name] = "" if name in ("recurrence", "notes") else None
            if name == "scheduled_start":
                body["scheduled_end"] = None
                body["duration"] = None
            if name == "due_date":
                body["due_time"] = None
        task = await planner.update(task_id, body)
    except (ValueError, KeyError, RuntimeError) as exc:
        return error(context, str(exc) if not isinstance(exc, KeyError) else f"no task {task_id}")
    return ok(context, json.dumps(task, ensure_ascii=False))


@tool_group("calendar")
@search_hint("planner complete task done finish check off mark выполнить задачу сделано отметить готово завершить выполнена закрыть сделал")
@tool(name="PlannerTaskComplete", description="Mark a planner task done (or open again with done=false). A repeating task is recorded as done and moves to its next due date.")
async def planner_task_complete(context: ToolContext, task_id: str, done: bool = True) -> ToolResult:
    try:
        task = await _planner(context).complete(task_id, done)
    except KeyError:
        return error(context, f"no task {task_id}")
    return ok(context, json.dumps(task, ensure_ascii=False))


TOOLS = [calendar_events, calendar_list, calendar_find_time, calendar_create, calendar_update, calendar_delete, planner_tasks, planner_task_create, planner_task_update, planner_task_complete]

__all__ = ["TOOLS"]
