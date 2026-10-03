"""Agent access to the operator's calendar; the same store backs the web app."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.stores.calendar import CalendarStore
from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import error, ok, services_for


def _store(context: ToolContext) -> CalendarStore:
    manager = services_for(context).extra["manager"]
    return CalendarStore(manager.db)


@tool_group("calendar")
@search_hint("calendar events meetings appointments agenda schedule встреча событие календарь показать посмотреть расписание планы неделю сегодня")
@tool(name="CalendarEvents", description="List calendar events in a time range. Give timezone-aware ISO 8601 start and end; defaults to the next 30 days. Connected calendars appear alongside local events.")
async def calendar_events(context: ToolContext, start: str = "", end: str = "") -> ToolResult:
    first = datetime.now(UTC)
    try:
        events = await _store(context).events(start or first.isoformat(), end or (first + timedelta(days=30)).isoformat())
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, json.dumps(events, ensure_ascii=False))


@tool_group("calendar")
@search_hint("calendar create event meeting reminder appointment add schedule добавить создать встречу событие календарь запланировать назначить запись")
@tool(name="CalendarCreate", description="Create an event. start_at and end_at are timezone-aware ISO 8601 instants; timezone is an IANA zone. Omit account_id for a local event, or use a connected account id to sync it.")
async def calendar_create(context: ToolContext, title: str, start_at: str, end_at: str, timezone: str = "UTC", description: str = "", location: str = "", all_day: bool = False, recurrence: str = "", account_id: str | None = None) -> ToolResult:
    try:
        event = await _store(context).save({"title": title, "start_at": start_at, "end_at": end_at, "timezone": timezone, "description": description, "location": location, "all_day": all_day, "recurrence": recurrence, "account_id": account_id})
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, json.dumps(event, ensure_ascii=False), event_id=event["id"])


@tool_group("calendar")
@search_hint("calendar edit move change reschedule event update перенести изменить событие встречу календарь время дату поправить")
@tool(name="CalendarUpdate", description="Change a calendar event by id. Read its version with CalendarEvents first; a stale version is refused. Give the complete title, start and end from the event you read. Omitted optional fields keep their current values.")
async def calendar_update(context: ToolContext, event_id: str, version: int, title: str, start_at: str, end_at: str, timezone: str | None = None, description: str | None = None, location: str | None = None, all_day: bool | None = None, recurrence: str | None = None) -> ToolResult:
    try:
        changes = {"timezone": timezone, "description": description, "location": location, "all_day": all_day, "recurrence": recurrence}
        body = {"version": version, "title": title, "start_at": start_at, "end_at": end_at, **{key: value for key, value in changes.items() if value is not None}}
        event = await _store(context).save(body, event_id)
    except (ValueError, KeyError, RuntimeError) as exc:
        return error(context, str(exc))
    return ok(context, json.dumps(event, ensure_ascii=False))


@tool_group("calendar")
@search_hint("calendar delete cancel event meeting удалить отменить событие встречу календарь убрать запись расписание")
@tool(name="CalendarDelete", description="Delete a calendar event by id and version. A connected event is removed from its provider on the next sync.")
async def calendar_delete(context: ToolContext, event_id: str, version: int) -> ToolResult:
    try:
        await _store(context).delete(event_id, version)
    except (KeyError, RuntimeError) as exc:
        return error(context, str(exc))
    return ok(context, f"deleted event {event_id}")


@tool_group("calendar")
@search_hint("calendar accounts connected providers Google Yandex Outlook calendars подключения календари аккаунты синхронизация источник провайдер список связь")
@tool(name="CalendarAccounts", description="List connected calendar accounts and sync status. Credentials are never returned.")
async def calendar_accounts(context: ToolContext) -> ToolResult:
    return ok(context, json.dumps(await _store(context).accounts(), ensure_ascii=False))


TOOLS = [calendar_events, calendar_create, calendar_update, calendar_delete, calendar_accounts]

__all__ = ["TOOLS"]
