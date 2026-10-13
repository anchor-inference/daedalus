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
ENTRY_FIELDS = ("type", "status", "tags", "group_id", "fields", "field_issues", "progress", "chip", "links")
"""What a structured entry adds, each only when it says something (a status of planned does not)."""
GROUP_FIELDS = ("id", "name", "icon", "archived", "hidden", "event_count", "progress", "range", "completed_at", "version")


def _db(context: ToolContext) -> Any:
    return services_for(context).extra["manager"].db


def _store(context: ToolContext) -> CalendarStore:
    return CalendarStore(_db(context))


def _planner(context: ToolContext) -> PlannerStore:
    return PlannerStore(_db(context))


def _actor(context: ToolContext) -> str:
    return f"agent:{context.session_id}"


def _compact(item: dict[str, Any], blocks: str = "") -> dict[str, Any]:
    """An occurrence for the agent's context. ``blocks`` is "" (only the progress), "summary" (each
    block's id, kind, title and ticks: what a patch needs) or "full" (the bodies)."""
    # Empty and false fields are left out, except the two an edit or a reading always needs.
    view = {key: item[key] for key in OCCURRENCE_FIELDS if key in item and (item[key] not in (None, "", False) or key in ("version", "all_day"))}
    for key in ENTRY_FIELDS:
        value = item.get(key)
        if value in (None, "", [], {}) or (key == "status" and value == "planned"):
            continue
        view[key] = value
    if blocks == "full" and item.get("blocks"):
        view["blocks"] = item["blocks"]
    elif blocks == "summary" and item.get("block_summary"):
        view["blocks"] = [{key: value for key, value in block.items() if value not in (None, "")} for block in item["block_summary"]]
    return view


def _own_actions(blocks: Any, session_id: str) -> Any:
    """Blocks with each agent action that names no session pointed at the caller's: the agent does
    not know its own session id, and an action it adds is nearly always meant to come back to it."""
    if not isinstance(blocks, list):
        return blocks
    return [
        {**block, "session_id": session_id} if isinstance(block, dict) and block.get("kind") == "action" and block.get("action") == "agent" and not block.get("session_id") else block
        for block in blocks
    ]


def _group(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in GROUP_FIELDS if item.get(key) not in (None, "", False)}


def _refusal(context: ToolContext, exc: Exception, what: str) -> ToolResult:
    return error(context, f"no {what}" if isinstance(exc, KeyError) else str(exc))


def _ids(text: str) -> list[str] | None:
    return [part.strip() for part in text.split(",") if part.strip()] or None


@tool_group("calendar")
@search_hint("calendar events meetings appointments agenda schedule busy встреча событие календарь показать посмотреть расписание планы неделю сегодня завтра занят")
@tool(name="CalendarEvents", description="List calendar occurrences in a time range, recurring series expanded, with each calendar's name. Give timezone-aware ISO 8601 start and end (defaults: now and 7 days later; with group_id alone, the group's whole range). calendars is an optional comma-separated list of calendar ids; hidden calendars are left out unless named. Filter by type, group_id, tag or status. Each event shows its type, fields, status, progress and chip; include_blocks=true adds the blocks' bodies (with their ids, for CalendarBlockPatch). event_id reads that one event in full. query searches titles, descriptions and places instead of a range.")
async def calendar_events(context: ToolContext, start: str = "", end: str = "", calendars: str = "", query: str = "", type: str = "", group_id: str = "", tag: str = "", status: str = "", include_blocks: bool = False, event_id: str = "") -> ToolResult:
    store = _store(context)
    try:
        if event_id:
            found_one = await store.get(event_id.split(":", 1)[0])
            if found_one is None:
                return error(context, f"no event {event_id}")
            view = _compact({**found_one, "event_id": found_one["id"]}, "full")
            view["description"] = found_one["description"]
            return ok(context, json.dumps(view, ensure_ascii=False))
        if query.strip():
            found = await store.search(query, 30)
        else:
            first = datetime.now(UTC)
            if group_id and not start and not end:
                span = (await store.group(group_id))["range"]
                if span is None:
                    return ok(context, json.dumps({"events": []}))
                start, end = span["from"], (moment(span["to"]) + timedelta(seconds=1)).isoformat()
            filters = {"type": type, "group_id": group_id, "tag": tag, "status": status}
            found = await store.occurrences(start or first.isoformat(), end or (first + timedelta(days=7)).isoformat(), _ids(calendars), 300, filters=filters, include_blocks=include_blocks)
    except (ValueError, KeyError) as exc:
        return _refusal(context, exc, f"group {group_id}")
    settings = await store.settings()
    return ok(context, json.dumps({"timezone": settings["timezone"], "events": [_compact(item, "full" if include_blocks else "") for item in found]}, ensure_ascii=False))


@tool_group("calendar")
@search_hint("calendar list calendars accounts connected providers Google Outlook Yandex iCloud CalDAV subscription календари список календарей аккаунты подключения синхронизация подписка часовой пояс рабочие часы")
@tool(name="CalendarList", description="List the operator's calendars (local ones, connected accounts and read-only subscriptions) with id, kind, colour, visibility, whether events can be written there, and sync status; the calendar settings (time zone, working hours); and the groups (plans) with their progress and date range. Credentials are never returned.")
async def calendar_list(context: ToolContext) -> ToolResult:
    store = _store(context)
    groups = [_group(item) for item in await store.groups() if not item["archived"]]
    return ok(context, json.dumps({"calendars": await store.calendars(), "settings": await store.settings(), "groups": groups}, ensure_ascii=False))


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
@tool(name="CalendarCreate", description="Create an event. start_at and end_at are timezone-aware ISO 8601 instants; timezone is the IANA zone the event lives in (a recurring event repeats on that zone's wall clock). For all_day, give the dates' midnights; the end is exclusive. calendar_id comes from CalendarList; omit it for the operator's default local calendar. recurrence is an RRULE such as FREQ=WEEKLY;BYDAY=MO. reminders are minutes before the start; omit them to use the calendar's defaults. description is Markdown. Structure: type (a CalendarTypeList id; without blocks the event starts from the type's default blocks), fields (values for the type's fields_schema), blocks (list of {kind, title?, ...}: markdown {text}, checklist {items:[{text, checked}]}, table {columns:[{key,label,type,aggregate?}], rows:[{cells:{...}}]}, steps {items:[{text, duration_min?}]}, fields {items:[{label,value,unit?}]}, link {url,label}, action {action:url|agent, label, url | session_id+prompt}), status, tags, group_id, links [{kind,ref,label}]. For many events at once use CalendarBulkCreate.")
async def calendar_create(context: ToolContext, title: str, start_at: str, end_at: str, timezone: str = "", calendar_id: str = "", description: str = "", location: str = "", all_day: bool = False, recurrence: str = "", reminders: list[int] | None = None, type: str = "", fields: dict[str, Any] | None = None, blocks: list[dict[str, Any]] | None = None, status: str = "", tags: list[str] | None = None, group_id: str = "", links: list[dict[str, Any]] | None = None) -> ToolResult:
    store = _store(context)
    body: dict[str, Any] = {"title": title, "start_at": start_at, "end_at": end_at, "timezone": timezone or (await store.settings())["timezone"], "description": description, "location": location, "all_day": all_day, "recurrence": recurrence}
    if calendar_id:
        body["calendar_id"] = calendar_id
    if reminders is not None:
        body["reminders"] = reminders
    body.update({key: value for key, value in (("type", type), ("fields", fields), ("blocks", _own_actions(blocks, context.session_id)), ("status", status), ("tags", tags), ("group_id", group_id), ("links", links)) if value})
    try:
        event = await store.create(body, _actor(context))
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, json.dumps(_compact(event, "summary"), ensure_ascii=False), event_id=event["event_id"])


@tool_group("calendar")
@search_hint("bulk batch many at once plan meal plan menu week пачкой много сразу план питания меню на неделю рацион")
@tool(name="CalendarBulkCreate", description="Write a whole plan in one call: up to 200 entries in one transaction, all or none (an error names the bad one). Each item takes CalendarCreate's arguments (title, start_at, end_at, type, fields, blocks, status, tags, group_id, ...); type, group_id and timezone given here apply to every item that has none. For a plan, make its type (CalendarTypeUpsert) and group (CalendarGroupCreate) first.")
async def calendar_bulk_create(context: ToolContext, events: list[dict[str, Any]], type: str = "", group_id: str = "", timezone: str = "") -> ToolResult:
    store = _store(context)
    zone = timezone or (await store.settings())["timezone"]
    items = []
    for item in events if isinstance(events, list) else []:
        if not isinstance(item, dict):
            return error(context, "every event is an object")
        merged = {"timezone": zone, **({"type": type} if type else {}), **({"group_id": group_id} if group_id else {}), **{key: value for key, value in item.items() if value is not None}}
        if "blocks" in merged:
            merged["blocks"] = _own_actions(merged["blocks"], context.session_id)
        items.append(merged)
    try:
        made = await store.bulk_create(items, _actor(context))
    except ValueError as exc:
        return error(context, str(exc))
    listed = [{"event_id": item["event_id"], "title": item["title"], "start_at": item["start_at"], "version": item["version"]} for item in made]
    return ok(context, json.dumps({"created": len(made), "events": listed}, ensure_ascii=False))


@tool_group("calendar")
@search_hint("calendar edit move change reschedule event update occurrence перенести изменить событие встречу календарь время дату поправить только эту все")
@tool(name="CalendarUpdate", description="Change an event. Read it with CalendarEvents first and pass its event_id and version; a stale version is refused. For a recurring event, scope='this' with occurrence_start changes that one occurrence; scope='all' changes the whole series (with occurrence_start, the new start_at/end_at are that occurrence's new times and the series moves by the same amount). Omitted fields keep their values. type, fields, blocks, status, tags, group_id and links belong to the whole series; blocks replaces every block (to tick or edit a few, use CalendarBlockPatch); an empty type or group_id clears it.")
async def calendar_update(context: ToolContext, event_id: str, version: int, scope: str = "all", occurrence_start: str = "", title: str | None = None, start_at: str | None = None, end_at: str | None = None, timezone: str | None = None, description: str | None = None, location: str | None = None, all_day: bool | None = None, recurrence: str | None = None, reminders: list[int] | None = None, calendar_id: str | None = None, type: str | None = None, fields: dict[str, Any] | None = None, blocks: list[dict[str, Any]] | None = None, status: str | None = None, tags: list[str] | None = None, group_id: str | None = None, links: list[dict[str, Any]] | None = None) -> ToolResult:
    changes = {"title": title, "start_at": start_at, "end_at": end_at, "timezone": timezone, "description": description, "location": location, "all_day": all_day, "recurrence": recurrence, "reminders": reminders, "calendar_id": calendar_id,
               "fields": fields, "blocks": _own_actions(blocks, context.session_id), "status": status, "tags": tags, "links": links}
    body: dict[str, Any] = {"version": version, "scope": scope, **{key: value for key, value in changes.items() if value is not None}}
    # An empty string clears the type or the group; None (left out) keeps it.
    for key, value in (("type", type), ("group_id", group_id)):
        if value is not None:
            body[key] = value or None
    if occurrence_start:
        body["occurrence_start"] = occurrence_start
    try:
        event = await _store(context).update(event_id, body, _actor(context))
    except (ValueError, KeyError, RuntimeError) as exc:
        return error(context, str(exc) if not isinstance(exc, KeyError) else f"no event {event_id}")
    return ok(context, json.dumps(_compact(event, "summary"), ensure_ascii=False))


@tool_group("calendar")
@search_hint("check item tick checklist block table row cell patch mark bought отметить пункт галочка чеклист купил добавить пункт строку таблицы ячейку список покупок")
@tool(name="CalendarBlockPatch", description="Change an event's blocks in place without rewriting the event. ops is a list applied in order, all or nothing: {op:'set_checked', block_id, item_id, checked} (checklist or steps item; for a table row give row_id and column), {op:'add_item', block_id, item, index?}, {op:'update_item', block_id, item_id, patch}, {op:'remove_item', block_id, item_id}, {op:'add_row', block_id, row:{cells}}, {op:'update_cell', block_id, row_id, column, value}, {op:'remove_row', block_id, row_id}, {op:'reorder', block_id, ids}, {op:'add_block', block, index?}, {op:'update_block', block_id, patch}, {op:'remove_block', block_id}, {op:'reorder_blocks', ids}. Ticks need no version; for other ops pass version to refuse a stale edit. Block and item ids come from CalendarEvents with include_blocks=true.")
async def calendar_block_patch(context: ToolContext, event_id: str, ops: list[dict[str, Any]], version: int | None = None) -> ToolResult:
    try:
        if isinstance(ops, list):
            ops = [{**op, "block": _own_actions([op["block"]], context.session_id)[0]} if isinstance(op, dict) and isinstance(op.get("block"), dict) else op for op in ops]
        event = await _store(context).patch_blocks(event_id, ops, version, _actor(context))
    except (ValueError, KeyError, RuntimeError) as exc:
        return _refusal(context, exc, f"event {event_id}")
    return ok(context, json.dumps({"event_id": event["event_id"], "version": event["version"], "progress": event["progress"], "blocks": _compact(event, "summary").get("blocks", [])}, ensure_ascii=False))


@tool_group("calendar")
@search_hint("history journal who checked when log feedback ticked история журнал кто отметил когда что отметил пользователь")
@tool(name="CalendarEventHistory", description="The journal of one event, newest first: who (operator, or agent:<session>) did what and when, down to each ticked item. Use it to see what the operator marked.")
async def calendar_event_history(context: ToolContext, event_id: str, limit: int = 50) -> ToolResult:
    try:
        entries = await _store(context).history(event_id, limit)
    except KeyError:
        return error(context, f"no event {event_id}")
    return ok(context, json.dumps({"event_id": event_id, "entries": entries}, ensure_ascii=False))


@tool_group("calendar")
@search_hint("types schema fields template list kinds типы шаблоны поля схема список типов какие виды записей")
@tool(name="CalendarTypeList", description="List the calendar's event types: id, name, icon, colour, version, fields_schema, default_blocks, display (chip template, badge), aggregates, status_flow, quick_actions.")
async def calendar_type_list(context: ToolContext) -> ToolResult:
    found = [{key: value for key, value in item.items() if key not in ("created_at", "updated_at") and value not in ("", [], {})} for item in await _store(context).type_list()]
    return ok(context, json.dumps({"types": found}, ensure_ascii=False))


@tool_group("calendar")
@search_hint("define type fields schema template meal workout тип определить новый поля шаблон приём пищи тренировка вид")
@tool(name="CalendarTypeUpsert", description="Create or change an event type (id is a slug such as 'meal'). fields_schema maps a field name to {type: string|number|integer|boolean|enum|date|money, unit?, values? (enum), required?}. default_blocks are the blocks a new event of the type starts with. display: {chip: '{meal} · {kcal} kcal', badge: 'progress:checklist'}. aggregates: [{field, op: sum|avg|count|min|max, per: day|week, label, target?: [min, max]}] are shown under each day. Arguments left out keep the type's current values; any change raises its version. Values that no longer fit a changed schema are kept and flagged, never dropped.")
async def calendar_type_upsert(context: ToolContext, type_id: str, name: str = "", icon: str | None = None, color: str | None = None, fields_schema: dict[str, Any] | None = None, default_blocks: list[dict[str, Any]] | None = None, display: dict[str, Any] | None = None, aggregates: list[dict[str, Any]] | None = None, status_flow: list[str] | None = None, quick_actions: list[str] | None = None) -> ToolResult:
    store = _store(context)
    current = (await store.types()).get(type_id, {})
    given = {"name": name or None, "icon": icon, "color": color, "fields_schema": fields_schema, "default_blocks": default_blocks, "display": display, "aggregates": aggregates, "status_flow": status_flow, "quick_actions": quick_actions}
    body = {**{key: value for key, value in current.items() if key in given}, **{key: value for key, value in given.items() if value is not None}}
    try:
        saved = await store.upsert_type(type_id, body, _actor(context))
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, json.dumps({"id": saved["id"], "name": saved["name"], "version": saved["version"], "fields": sorted(saved["fields_schema"])}, ensure_ascii=False))


@tool_group("calendar")
@search_hint("delete remove type kind template удалить тип убрать тип стереть шаблон больше не нужен вид")
@tool(name="CalendarTypeDelete", description="Delete an event type. Its events become ordinary events and keep their fields and blocks.")
async def calendar_type_delete(context: ToolContext, type_id: str) -> ToolResult:
    try:
        await _store(context).delete_type(type_id)
    except KeyError:
        return error(context, f"no type {type_id}")
    return ok(context, f"deleted type {type_id}")


@tool_group("calendar")
@search_hint("group plan container trip course menu week группа план поездка курс меню на неделю объединить связанные")
@tool(name="CalendarGroupCreate", description="Create a group: a plan that holds related events ('Menu 14-20 October', 'Trip'). Put events in it with group_id. A group can carry its own description (Markdown) and blocks (a budget table, a summary).")
async def calendar_group_create(context: ToolContext, name: str, icon: str = "", color: str = "", description: str = "", blocks: list[dict[str, Any]] | None = None) -> ToolResult:
    try:
        group = await _store(context).create_group({"name": name, "icon": icon, "color": color, "description": description, "blocks": _own_actions(blocks or [], context.session_id)}, _actor(context))
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, json.dumps(_group(group), ensure_ascii=False), group_id=group["id"])


@tool_group("calendar")
@search_hint("group rename archive hide show plan изменить группу переименовать архивировать скрыть показать план спрятать")
@tool(name="CalendarGroupUpdate", description="Change a group: name, icon, color, description, blocks; archived=true files it away; hidden=true hides its events from the calendar views (hidden=false shows them again). Omitted arguments keep their values.")
async def calendar_group_update(context: ToolContext, group_id: str, name: str | None = None, icon: str | None = None, color: str | None = None, description: str | None = None, blocks: list[dict[str, Any]] | None = None, archived: bool | None = None, hidden: bool | None = None) -> ToolResult:
    changes = {"name": name, "icon": icon, "color": color, "description": description, "blocks": blocks, "archived": archived, "hidden": hidden}
    try:
        group = await _store(context).update_group(group_id, {key: value for key, value in changes.items() if value is not None})
    except (ValueError, KeyError, RuntimeError) as exc:
        return _refusal(context, exc, f"group {group_id}")
    return ok(context, json.dumps(_group(group), ensure_ascii=False))


@tool_group("calendar")
@search_hint("group delete remove plan whole удалить группу план целиком убрать стереть весь сразу")
@tool(name="CalendarGroupDelete", description="Delete a group. events='keep' (default) leaves its events in the calendar as ordinary ones; events='delete' deletes them too.")
async def calendar_group_delete(context: ToolContext, group_id: str, events: str = "keep") -> ToolResult:
    try:
        await _store(context).delete_group(group_id, events, _actor(context))
    except (ValueError, KeyError, RuntimeError) as exc:
        return _refusal(context, exc, f"group {group_id}")
    return ok(context, f"deleted group {group_id}" + (" and its events" if events == "delete" else ""))


@tool_group("calendar")
@search_hint("group shift move plan days later earlier postpone сдвинуть группу перенести план на дни позже раньше")
@tool(name="CalendarGroupShift", description="Move every event of a group by whole days (negative moves earlier), each keeping its time of day in its zone.")
async def calendar_group_shift(context: ToolContext, group_id: str, days: int) -> ToolResult:
    try:
        group = await _store(context).shift_group(group_id, days, _actor(context))
    except (ValueError, KeyError) as exc:
        return _refusal(context, exc, f"group {group_id}")
    return ok(context, json.dumps(_group(group), ensure_ascii=False))


@tool_group("calendar")
@search_hint("group duplicate copy plan next week repeat menu дублировать группу копировать план на следующую неделю повторить меню")
@tool(name="CalendarGroupDuplicate", description="Copy a group with all its events offset_days later (7 for next week) into a new group: ticks cleared, statuses back to planned. Returns the new group.")
async def calendar_group_duplicate(context: ToolContext, group_id: str, offset_days: int, name: str = "") -> ToolResult:
    try:
        group = await _store(context).duplicate_group(group_id, offset_days, _actor(context), name or None)
    except (ValueError, KeyError) as exc:
        return _refusal(context, exc, f"group {group_id}")
    return ok(context, json.dumps(_group(group), ensure_ascii=False), group_id=group["id"])


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
        task = await _planner(context).complete(task_id, done, (await _store(context).settings())["timezone"])
    except KeyError:
        return error(context, f"no task {task_id}")
    return ok(context, json.dumps(task, ensure_ascii=False))


TOOLS = [
    calendar_events, calendar_list, calendar_find_time, calendar_create, calendar_update, calendar_delete,
    calendar_bulk_create, calendar_block_patch, calendar_event_history, calendar_type_list, calendar_type_upsert, calendar_type_delete,
    calendar_group_create, calendar_group_update, calendar_group_delete, calendar_group_shift, calendar_group_duplicate,
    planner_tasks, planner_task_create, planner_task_update, planner_task_complete,
]

__all__ = ["TOOLS"]
