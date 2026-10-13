"""Structured calendar entries: blocks and their ops, typed fields, types, groups, hooks, history, export."""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from protocore.contracts.tools import ToolContext

from daedalus.extensions import calendar_hooks
from daedalus.extensions.api_calendar import register
from daedalus.extensions.calendar_sync import CalendarSync, exported
from daedalus.stores import calendar_blocks as entries
from daedalus.stores.calendar import CalendarStore, listen
from daedalus.stores.database import Database

OAUTH = {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}
MEAL = {
    "name": "Meal", "icon": "\U0001f37d", "color": "#2bb673",
    "fields_schema": {"kcal": {"type": "number", "unit": "kcal"}, "protein_g": {"type": "number", "unit": "g"}, "meal": {"type": "enum", "values": ["Breakfast", "Lunch", "Dinner"]}},
    "default_blocks": [{"kind": "fields", "source": "fields"}, {"kind": "checklist", "title": "Products"}, {"kind": "steps", "title": "Recipe"}],
    "display": {"chip": "{meal} · {kcal} kcal", "badge": "progress:checklist"},
    "aggregates": [{"field": "kcal", "op": "sum", "per": "day", "label": "kcal a day", "target": [1900, 2100]}],
}


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "entries.sqlite")
    await database.open()
    try:
        yield database
    finally:
        await database.close()


def _context(session: str = "s1") -> ToolContext:
    return ToolContext(tenant_id="operator", run_id="run", session_id=session)


def _tools(monkeypatch, db):
    from daedalus.tools import calendar as calendar_tools  # Lazy: the tool module reaches the session services.

    monkeypatch.setattr(calendar_tools, "_db", lambda _context: db)
    return calendar_tools


async def _hooks(db):
    return [(row["kind"], json.loads(row["detail_json"])) for row in await db.fetchall("SELECT * FROM calendar_hooks ORDER BY id")]


def _basket() -> dict:
    return {"kind": "table", "title": "Basket", "columns": [
        {"key": "name", "label": "Item", "type": "text"}, {"key": "qty", "label": "Qty", "type": "text"},
        {"key": "price", "label": "Price", "type": "money", "unit": "₸", "aggregate": "sum"}, {"key": "got", "label": "✓", "type": "checkbox", "aggregate": "count"},
    ], "rows": [
        {"id": "r1", "cells": {"name": "Buckwheat 1 kg", "qty": "1", "price": 435, "got": False}},
        {"id": "r2", "cells": {"name": "Milk", "qty": "2", "price": 650, "got": True}},
    ]}


# -- the block engine ------------------------------------------------------------------------


def test_ids_are_generated_where_missing_and_kept_where_given():
    blocks = entries.normalize_blocks([
        {"kind": "checklist", "items": ["eggs", {"text": "milk", "checked": True}, {"id": "keep", "text": "bread"}]},
        {"id": "b2", "kind": "table", "columns": [{"key": "name"}], "rows": [{"cells": {"name": "x"}}, {"id": "r", "cells": {}}]},
        {"id": "b2", "kind": "markdown", "text": "a duplicate id is replaced"},
    ])
    checklist, table, text = blocks
    assert checklist["id"].startswith("blk_") and [item["text"] for item in checklist["items"]] == ["eggs", "milk", "bread"]
    assert all(item["id"] for item in checklist["items"]) and checklist["items"][2]["id"] == "keep" and checklist["items"][1]["checked"] is True
    assert table["id"] == "b2" and table["rows"][0]["id"].startswith("row_") and table["rows"][1]["id"] == "r"
    assert text["id"] != "b2"


def test_every_op_changes_what_it_names_and_journals_it():
    blocks = entries.normalize_blocks([
        {"id": "c", "kind": "checklist", "title": "Products", "items": [{"id": "i1", "text": "eggs"}, {"id": "i2", "text": "milk"}]},
        _basket() | {"id": "t"},
        {"id": "s", "kind": "steps", "items": [{"id": "s1", "text": "boil", "duration_min": 15}]},
    ])
    ops = [
        {"op": "set_checked", "block_id": "c", "item_id": "i1", "checked": True},
        {"op": "set_checked", "block_id": "t", "row_id": "r1", "column": "got", "checked": True},
        {"op": "set_checked", "block_id": "s", "item_id": "s1", "checked": True},
        {"op": "add_item", "block_id": "c", "item": {"text": "salt"}, "index": 0},
        {"op": "update_item", "block_id": "c", "item_id": "i2", "patch": {"text": "oat milk", "note": "1 l"}},
        {"op": "remove_item", "block_id": "c", "item_id": "i1"},
        {"op": "add_row", "block_id": "t", "row": {"cells": {"name": "Rice", "price": 500, "got": False}}},
        {"op": "update_cell", "block_id": "t", "row_id": "r2", "column": "price", "value": 700},
        {"op": "remove_row", "block_id": "t", "row_id": "r1"},
        {"op": "reorder", "block_id": "c", "ids": ["i2"]},
        {"op": "add_block", "block": {"kind": "markdown", "text": "note"}, "index": 0},
        {"op": "update_block", "block_id": "c", "patch": {"title": "Shopping", "collapsed": True, "kind": "table", "id": "x"}},
        {"op": "remove_block", "block_id": "s"},
        {"op": "reorder_blocks", "ids": ["c", "t"]},
    ]
    after, journal = entries.apply_ops(blocks, ops)
    assert [block["kind"] for block in after] == ["checklist", "table", "markdown"]
    checklist, table, _ = after
    assert checklist["title"] == "Shopping" and checklist["collapsed"] is True and checklist["id"] == "c"
    assert [item["text"] for item in checklist["items"]] == ["oat milk", "salt"] and checklist["items"][0]["note"] == "1 l"
    assert [row["cells"]["name"] for row in table["rows"]] == ["Milk", "Rice"] and table["rows"][0]["cells"]["price"] == 700
    assert [entry["action"] for entry in journal] == [op["op"] for op in ops]
    assert journal[0]["item_id"] == "i1" and journal[0]["summary"] == "checked eggs" and journal[1]["summary"] == "checked Buckwheat 1 kg"
    # The input was not touched: a patch that fails later must leave nothing behind.
    assert blocks[0]["items"][0]["checked"] is False


def test_a_failing_op_leaves_the_whole_patch_unapplied_and_unknown_kinds_are_not_edited():
    blocks = entries.normalize_blocks([{"id": "c", "kind": "checklist", "items": [{"id": "i", "text": "a"}]}, {"id": "u", "kind": "kanban", "lanes": [{"x": 1}]}])
    with pytest.raises(ValueError, match="no item nope"):
        entries.apply_ops(blocks, [{"op": "set_checked", "block_id": "c", "item_id": "i", "checked": True}, {"op": "remove_item", "block_id": "c", "item_id": "nope"}])
    with pytest.raises(ValueError, match="does not edit"):
        entries.apply_ops(blocks, [{"op": "add_item", "block_id": "u", "item": {"text": "x"}}])
    with pytest.raises(ValueError, match="checkbox column"):
        entries.apply_ops(entries.normalize_blocks([_basket() | {"id": "t"}]), [{"op": "set_checked", "block_id": "t", "row_id": "r1", "column": "price", "checked": True}])
    with pytest.raises(ValueError, match="checked, true or false"):
        entries.apply_ops(blocks, [{"op": "set_checked", "block_id": "c", "item_id": "i", "checked": "yes"}])
    # The unknown block can still be moved and removed as a whole.
    after, _ = entries.apply_ops(blocks, [{"op": "reorder_blocks", "ids": ["u"]}])
    assert after[0] == {"id": "u", "kind": "kanban", "lanes": [{"x": 1}]}


def test_progress_and_chip_follow_the_badge_and_the_template():
    blocks = entries.normalize_blocks([_basket(), {"id": "c", "kind": "checklist", "items": [{"text": "a", "checked": True}, {"text": "b"}, {"text": "c"}]}])
    assert entries.event_progress(blocks) == {"done": 1, "total": 3}
    assert entries.event_progress(blocks, "progress:table") == {"done": 1, "total": 2}
    assert entries.event_progress(blocks, "progress:all") == {"done": 2, "total": 5}
    assert entries.chip("{meal} · {kcal} kcal", "x", {"meal": "Breakfast", "kcal": 518.0}, "planned", None) == "Breakfast · 518 kcal"
    assert entries.chip("{meal} · {missing}", "x", {"meal": "Lunch"}, "planned", None) == "Lunch"
    assert entries.chip("{title} {progress}", "Order", {}, "planned", {"done": 3, "total": 7}) == "Order 3/7"
    assert entries.chip("", "x", {}, "planned", None) is None


# -- fields and types ------------------------------------------------------------------------


def test_fields_that_do_not_fit_are_kept_and_named():
    schema = entries.check_type(MEAL | {"fields_schema": {**MEAL["fields_schema"], "servings": {"type": "integer", "required": True}, "on": {"type": "date"}, "paid": {"type": "money"}, "vegan": {"type": "boolean"}}}, "meal")["fields_schema"]
    stored, issues = entries.check_fields({"kcal": "518", "protein_g": "lots", "meal": "Brunch", "servings": 2.0, "on": "2026-10-14", "paid": {"amount": 435, "currency": "KZT"}, "vegan": "no", "old": 1}, schema)
    assert stored == {"kcal": 518, "protein_g": "lots", "meal": "Brunch", "servings": 2, "on": "2026-10-14", "paid": {"amount": 435, "currency": "KZT"}, "vegan": "no", "old": 1}
    assert issues == {"protein_g": "expected a number", "meal": "not one of the allowed values", "vegan": "expected true or false", "old": "not in the type's schema"}
    assert entries.check_fields({"servings": 2.5, "on": "14.10"}, schema)[1] == {"servings": "expected an integer", "on": "expected a date (YYYY-MM-DD)"}
    assert entries.check_fields({}, schema)[1] == {"servings": "required"}
    # Without a type nothing is checked, and nothing is dropped.
    assert entries.check_fields({"anything": [1]}, None) == ({"anything": [1]}, {})


@pytest.mark.parametrize(("body", "type_id", "message"), [
    ({"name": "x"}, "Meal Plan", "slug"),
    ({"name": ""}, "meal", "name is required"),
    ({"name": "x", "fields_schema": {"kcal": {"type": "float"}}}, "meal", "type is one of"),
    ({"name": "x", "fields_schema": {"m": {"type": "enum", "values": []}}}, "meal", "1 to 100 values"),
    ({"name": "x", "fields_schema": {"note": {"type": "string"}}, "aggregates": [{"field": "note", "op": "sum"}]}, "meal", "number, integer or money"),
    ({"name": "x", "aggregates": [{"op": "count", "per": "month"}]}, "meal", "per day or per week"),
    ({"name": "x", "status_flow": ["planned", "maybe"]}, "meal", "status_flow"),
    ({"name": "x", "color": "green"}, "meal", "#rrggbb"),
    ({"name": "x", "fields_schema": {f"f{index}": {"type": "enum", "values": ["v" * 300] * 3} for index in range(40)}}, "meal", "32 KB"),
])
def test_a_type_definition_is_validated(body, type_id, message):
    with pytest.raises(ValueError, match=message):
        entries.check_type(body, type_id)


@pytest.mark.asyncio
async def test_starter_types_are_seeded_and_types_are_versioned_and_limited(db, monkeypatch):
    store = CalendarStore(db)
    assert [item["id"] for item in await store.type_list()] == ["appointment", "routine", "shopping", "todo-list"]
    made = await store.upsert_type("meal", MEAL, "agent:s1")
    assert made["version"] == 1 and made["owner"] == "agent:s1" and made["fields_schema"]["kcal"]["unit"] == "kcal"
    assert (await store.upsert_type("meal", MEAL))["version"] == 1, "the same definition again is not a change"
    changed = await store.upsert_type("meal", MEAL | {"fields_schema": {"kcal": {"type": "number"}}})
    assert changed["version"] == 2 and changed["owner"] == "agent:s1"
    monkeypatch.setattr(entries, "MAX_TYPES", 5)
    with pytest.raises(ValueError, match="already 5 calendar types"):
        await store.upsert_type("one-too-many", {"name": "x"})


# -- events with entries ---------------------------------------------------------------------


async def _meal(store: CalendarStore, **extra) -> dict:
    body = {"title": "Breakfast", "start_at": "2026-10-14T08:00:00+05:00", "end_at": "2026-10-14T08:30:00+05:00", "timezone": "Asia/Almaty", "type": "meal", "fields": {"kcal": 518, "protein_g": 17, "meal": "Breakfast"}}
    return await store.create(body | extra)


@pytest.mark.asyncio
async def test_a_typed_event_starts_from_the_types_blocks_and_shows_its_chip(db):
    store = CalendarStore(db)
    await store.upsert_type("meal", MEAL)
    made = await _meal(store)
    assert [block["kind"] for block in made["blocks"]] == ["fields", "checklist", "steps"]
    assert len({block["id"] for block in made["blocks"]}) == 3
    assert made["chip"] == "Breakfast · 518 kcal" and made["status"] == "planned" and made["type_version"] == 1 and made["field_issues"] == {}
    other = await _meal(store)
    assert {block["id"] for block in other["blocks"]}.isdisjoint({block["id"] for block in made["blocks"]})
    with pytest.raises(ValueError, match="no calendar type 'nope'"):
        await store.create({"title": "x", "start_at": "2026-10-14T08:00:00Z", "end_at": "2026-10-14T09:00:00Z", "type": "nope"})


@pytest.mark.asyncio
async def test_an_unknown_block_and_a_field_dropped_from_the_schema_survive_open_and_save(db):
    """Acceptance 6: neither is lost when the event is read and saved back as it was read."""
    store = CalendarStore(db)
    await store.upsert_type("meal", MEAL)
    future = {"id": "fut", "kind": "timeline", "title": "From a newer app", "lanes": [{"at": "08:00", "what": "coffee", "nested": {"deep": [1, 2]}}], "collapsed": True}
    made = await _meal(store, blocks=[{"kind": "checklist", "items": ["eggs"]}, future], fields={"kcal": 518, "meal": "Breakfast", "fibre_g": 4})
    # The type drops fibre_g (it never had it) and kcal changes sort: both values stay, flagged.
    await store.upsert_type("meal", MEAL | {"fields_schema": {"kcal": {"type": "integer"}, "meal": MEAL["fields_schema"]["meal"]}})
    opened = await store.get(made["event_id"])
    assert opened["fields"] == {"kcal": 518, "meal": "Breakfast", "fibre_g": 4}
    assert opened["field_issues"] == {"fibre_g": "not in the type's schema"} and opened["type_outdated"] is True
    assert opened["blocks"][1] == future
    saved = await store.update(made["event_id"], {"version": opened["version"], "title": opened["title"], "blocks": opened["blocks"], "fields": opened["fields"]})
    assert saved["blocks"][1] == future and saved["fields"]["fibre_g"] == 4
    ticked = await store.patch_blocks(made["event_id"], [{"op": "set_checked", "block_id": saved["blocks"][0]["id"], "item_id": saved["blocks"][0]["items"][0]["id"], "checked": True}])
    assert ticked["blocks"][1] == future
    # The type deleted, the event is an ordinary one that still has everything it had.
    await store.delete_type("meal")
    plain = await store.get(made["event_id"])
    assert plain["type"] is None and plain["fields"]["fibre_g"] == 4 and plain["blocks"][1] == future


@pytest.mark.asyncio
async def test_a_tick_needs_no_version_but_a_structural_op_honours_it(db):
    store = CalendarStore(db)
    made = await store.create({"title": "List", "start_at": "2026-10-14T08:00:00Z", "end_at": "2026-10-14T09:00:00Z", "blocks": [{"id": "c", "kind": "checklist", "items": [{"id": "a", "text": "a"}, {"id": "b", "text": "b"}]}]})
    stale = made["version"]
    first = await store.patch_blocks(made["event_id"], [{"op": "set_checked", "block_id": "c", "item_id": "a", "checked": True}], version=stale)
    second = await store.patch_blocks(made["event_id"], [{"op": "set_checked", "block_id": "c", "item_id": "b", "checked": True}], version=stale)
    assert [item["checked"] for item in second["blocks"][0]["items"]] == [True, True] and second["version"] == first["version"] + 1
    with pytest.raises(RuntimeError, match="changed elsewhere"):
        await store.patch_blocks(made["event_id"], [{"op": "add_item", "block_id": "c", "item": {"text": "c"}}], version=stale)
    grown = await store.patch_blocks(made["event_id"], [{"op": "add_item", "block_id": "c", "item": {"text": "c"}}], version=second["version"])
    assert len(grown["blocks"][0]["items"]) == 3
    with pytest.raises(ValueError, match="no block nope"):
        await store.patch_blocks(made["event_id"], [{"op": "remove_block", "block_id": "nope"}])
    assert len((await store.get(made["event_id"]))["blocks"][0]["items"]) == 3


@pytest.mark.asyncio
async def test_a_checklist_finished_fires_its_hook_once_per_transition(db):
    """Acceptance 4, second half: ``checklist.completed`` fires exactly once."""
    store = CalendarStore(db)
    made = await store.create({"title": "Order", "start_at": "2026-10-14T08:00:00Z", "end_at": "2026-10-14T09:00:00Z", "blocks": [{"id": "c", "kind": "checklist", "title": "Basket", "items": [{"id": "a", "text": "a"}, {"id": "b", "text": "b"}]}, _basket() | {"id": "t"}]})
    event_id = made["event_id"]

    async def tick(*ops):
        await store.patch_blocks(event_id, [{"op": "set_checked", "block_id": block, "item_id": item, "checked": state} for block, item, state in ops])

    await tick(("c", "a", True))
    assert await _hooks(db) == []
    await tick(("c", "b", True))
    await tick(("c", "b", True))  # Ticking a ticked item again is no transition.
    await store.patch_blocks(event_id, [{"op": "update_block", "block_id": "c", "patch": {"title": "Basket"}}])
    completed = [hook for hook in await _hooks(db) if hook[0] == "checklist.completed"]
    assert len(completed) == 1 and completed[0][1] | {"actor": "operator"} == {"title": "Order", "block_id": "c", "block_title": "Basket", "block_kind": "checklist", "done": 2, "total": 2, "actor": "operator"}
    await tick(("c", "b", False))
    await tick(("c", "b", True))
    assert len([hook for hook in await _hooks(db) if hook[0] == "checklist.completed"]) == 2, "unticked and finished again is a second transition"
    # A table's first checkbox column counts as its checklist.
    await store.patch_blocks(event_id, [{"op": "set_checked", "block_id": "t", "row_id": "r1", "column": "got", "checked": True}])
    assert [hook[1]["block_id"] for hook in await _hooks(db) if hook[0] == "checklist.completed"] == ["c", "c", "t"]
    # A block that arrives finished is not a transition.
    await store.create({"title": "Done already", "start_at": "2026-10-14T08:00:00Z", "end_at": "2026-10-14T09:00:00Z", "blocks": [{"kind": "checklist", "items": [{"text": "x", "checked": True}]}]})
    assert len([hook for hook in await _hooks(db) if hook[0] == "checklist.completed"]) == 3


@pytest.mark.asyncio
async def test_status_and_group_hooks_fire_once_and_are_delivered_once(db):
    store = CalendarStore(db)
    group = await store.create_group({"name": "Menu"})
    first = await store.create({"title": "Lunch", "start_at": "2026-10-14T08:00:00Z", "end_at": "2026-10-14T09:00:00Z", "group_id": group["id"]})
    second = await store.create({"title": "Dinner", "start_at": "2026-10-14T18:00:00Z", "end_at": "2026-10-14T19:00:00Z", "group_id": group["id"]})
    await store.set_status(first["event_id"], "done")
    await store.set_status(first["event_id"], "done")
    await store.set_status(second["event_id"], "skipped", actor="agent:s9")
    kinds = [kind for kind, _ in await _hooks(db)]
    assert kinds == ["event.status_changed", "event.status_changed", "group.completed"]
    assert (await _hooks(db))[1][1] == {"title": "Dinner", "from": "planned", "to": "skipped", "actor": "agent:s9"}
    assert (await store.group(group["id"]))["completed_at"] and (await store.group(group["id"]))["progress"] == {"done": 2, "total": 2}
    # A new planned event reopens the group; finishing it again is a second completion.
    third = await store.create({"title": "Snack", "start_at": "2026-10-14T15:00:00Z", "end_at": "2026-10-14T15:30:00Z", "group_id": group["id"]})
    assert (await store.group(group["id"]))["completed_at"] is None
    await store.set_status(third["event_id"], "done")
    assert [kind for kind, _ in await _hooks(db)].count("group.completed") == 2

    matched = []

    async def match_intents(source, text):
        matched.append((source, text))
        return []

    app = SimpleNamespace(db=db, extensions={"inbound": SimpleNamespace(match_intents=match_intents)})
    assert await calendar_hooks.deliver(app) == 5
    assert await calendar_hooks.deliver(app) == 0, "a delivered hook is never delivered again"
    assert len(matched) == 5 and all(source == "calendar" for source, _ in matched)
    assert matched[0][1] == f"calendar event.status_changed: 'Lunch' planned -> done by operator (event {first['event_id']}, group {group['id']})"
    assert matched[2][1].startswith(f"calendar group.completed: group 'Menu', 2 events done (group {group['id']})")


@pytest.mark.asyncio
async def test_the_journal_names_the_operator_and_the_agent(db):
    store = CalendarStore(db)
    made = await store.create({"title": "List", "start_at": "2026-10-14T08:00:00Z", "end_at": "2026-10-14T09:00:00Z", "blocks": [{"id": "c", "kind": "checklist", "items": [{"id": "a", "text": "eggs"}]}]}, actor="agent:s1")
    await store.patch_blocks(made["event_id"], [{"op": "set_checked", "block_id": "c", "item_id": "a", "checked": True}])
    updated = await store.update(made["event_id"], {"version": (await store.get(made["event_id"]))["version"], "title": "Shopping list", "tags": ["week-42"]}, actor="agent:s1")
    assert updated["tags"] == ["week-42"]
    history = await store.history(made["event_id"])
    assert [(entry["actor"], entry["action"], entry["summary"]) for entry in history] == [
        ("agent:s1", "updated", "changed title, tags"), ("operator", "set_checked", "checked eggs"), ("agent:s1", "created", "List"),
    ]
    assert history[1]["block_id"] == "c" and history[1]["item_id"] == "a"


@pytest.mark.asyncio
async def test_filters_hidden_groups_and_light_lists(db):
    store = CalendarStore(db)
    await store.upsert_type("meal", MEAL)
    group = await store.create_group({"name": "Menu"})
    await _meal(store, group_id=group["id"], tags=["food", "week-42"])
    await store.create({"title": "Dentist", "start_at": "2026-10-14T10:00:00Z", "end_at": "2026-10-14T11:00:00Z", "status": "done"})
    span = ("2026-10-13T00:00:00Z", "2026-10-16T00:00:00Z")

    async def titles(**filters):
        return [item["title"] for item in await store.occurrences(*span, filters=filters)]

    assert await titles() == ["Breakfast", "Dentist"]
    assert await titles(type="meal") == ["Breakfast"] and await titles(tag="week-42") == ["Breakfast"] and await titles(status="done") == ["Dentist"]
    listed = (await store.occurrences(*span))[0]
    assert "blocks" not in listed and [block["kind"] for block in listed["block_summary"]] == ["fields", "checklist", "steps"]
    assert listed["progress"] == {"done": 0, "total": 0} and listed["chip"] == "Breakfast · 518 kcal"
    await store.update_group(group["id"], {"hidden": True})
    assert await titles() == ["Dentist"]
    assert await titles(group_id=group["id"]) == ["Breakfast"], "a hidden group's events are shown when it is asked for"
    assert [item["title"] for item in await store.occurrences(*span, include_hidden_groups=True)] == ["Breakfast", "Dentist"]


@pytest.mark.asyncio
async def test_aggregates_sum_a_types_field_per_day_against_its_target(db):
    store = CalendarStore(db)
    await store.save_settings({"timezone": "Asia/Almaty"})
    await store.upsert_type("meal", MEAL | {"aggregates": [*MEAL["aggregates"], {"field": "protein_g", "op": "avg", "per": "week", "label": "protein"}, {"field": None, "op": "count", "per": "day", "label": "meals"}]})
    for start, kcal, status in (("2026-10-14T08:00:00+05:00", 518, "planned"), ("2026-10-14T13:00:00+05:00", 900, "done"), ("2026-10-14T19:00:00+05:00", 610, "planned"),
                                ("2026-10-14T23:30:00+05:00", 400, "skipped"), ("2026-10-15T00:30:00+05:00", 2000, "planned")):
        await store.create({"title": "Meal", "start_at": start, "end_at": start.replace(":00+05", ":20+05"), "type": "meal", "fields": {"kcal": kcal, "protein_g": 20}, "status": status})
    found = await store.aggregates("2026-10-13T00:00:00Z", "2026-10-20T00:00:00Z")
    days = {day["date"]: {item["label"]: item for item in day["items"]} for day in found["days"]}
    # 00:30 in Almaty is the 15th although it is still the 14th in UTC; the skipped one does not count.
    assert days["2026-10-14"]["kcal a day"] | {} == {"type": "meal", "field": "kcal", "op": "sum", "label": "kcal a day", "value": 2028, "unit": "kcal", "target": [1900, 2100], "ok": True}
    assert days["2026-10-14"]["meals"]["value"] == 3 and days["2026-10-15"]["kcal a day"]["value"] == 2000
    # Asked by local dates with the end exclusive, as the app asks: the 15th is not in a range ending on it.
    local = await store.aggregates("2026-10-14", "2026-10-15")
    assert [day["date"] for day in local["days"]] == ["2026-10-14"] and local["days"][0]["items"][0]["value"] == 2028
    assert found["weeks"] == [{"start": "2026-10-12", "items": [{"type": "meal", "field": "protein_g", "op": "avg", "label": "protein", "value": 20, "unit": "g"}]}]


@pytest.mark.asyncio
async def test_a_group_shifts_on_the_wall_clock_and_duplicates_with_ticks_cleared(db):
    store = CalendarStore(db)
    group = await store.create_group({"name": "Training", "blocks": [{"kind": "checklist", "items": [{"text": "buy shoes", "checked": True}]}]})
    # Berlin leaves summer time on 25 October: a 08:00 session stays at 08:00.
    made = await store.create({"title": "Run", "start_at": "2026-10-24T08:00:00+02:00", "end_at": "2026-10-24T09:00:00+02:00", "timezone": "Europe/Berlin", "group_id": group["id"], "status": "done",
                               "blocks": [{"id": "c", "kind": "checklist", "items": [{"text": "warm up", "checked": True}]}]})
    await store.create({"title": "Rest day", "all_day": True, "start_date": "2026-10-25", "end_date": "2026-10-26", "group_id": group["id"]})
    shifted = await store.shift_group(group["id"], 2)
    run = await store.get(made["event_id"])
    assert run["start_at"] == "2026-10-26T07:00:00+00:00" and run["version"] == made["version"] + 1
    assert shifted["range"]["from"] == "2026-10-26T07:00:00+00:00"
    copy = await store.duplicate_group(group["id"], 7)
    assert copy["id"] != group["id"] and copy["name"] == "Training" and copy["event_count"] == 2 and copy["progress"] == {"done": 0, "total": 2}
    assert copy["blocks"][0]["items"][0]["checked"] is False
    copied = await store.occurrences("2026-11-01T00:00:00Z", "2026-11-05T00:00:00Z", filters={"group_id": copy["id"]}, include_blocks=True)
    assert [(item["title"], item["start_at"], item["status"]) for item in copied] == [("Run", "2026-11-02T07:00:00+00:00", "planned"), ("Rest day", "2026-11-03T00:00:00+00:00", "planned")]
    assert copied[0]["blocks"][0]["items"][0]["checked"] is False
    assert (await store.get(made["event_id"]))["blocks"][0]["items"][0]["checked"] is True, "the original keeps its ticks"
    with pytest.raises(ValueError, match="not 0"):
        await store.shift_group(group["id"], 0)


@pytest.mark.asyncio
async def test_deleting_a_group_keeps_or_deletes_its_events(db):
    store = CalendarStore(db)
    kept, gone = await store.create_group({"name": "Keep"}), await store.create_group({"name": "Gone"})
    first = await store.create({"title": "Stays", "start_at": "2026-10-14T08:00:00Z", "end_at": "2026-10-14T09:00:00Z", "group_id": kept["id"]})
    await store.create({"title": "Goes", "start_at": "2026-10-14T10:00:00Z", "end_at": "2026-10-14T11:00:00Z", "group_id": gone["id"]})
    await store.delete_group(kept["id"], "keep")
    await store.delete_group(gone["id"], "delete")
    remaining = await store.occurrences("2026-10-14T00:00:00Z", "2026-10-15T00:00:00Z")
    assert [(item["title"], item["group_id"]) for item in remaining] == [("Stays", None)]
    assert (await store.history(first["event_id"]))[0]["action"] == "ungrouped"
    assert await store.groups() == []
    with pytest.raises(KeyError):
        await store.group(kept["id"])


@pytest.mark.asyncio
async def test_every_committed_change_is_announced(db):
    store = CalendarStore(db)
    heard = []
    stop = listen(db, heard.append)
    made = await store.create({"title": "x", "start_at": "2026-10-14T08:00:00Z", "end_at": "2026-10-14T09:00:00Z", "blocks": [{"id": "c", "kind": "checklist", "items": [{"id": "a", "text": "a"}]}]})
    await store.patch_blocks(made["event_id"], [{"op": "set_checked", "block_id": "c", "item_id": "a", "checked": True}])
    with pytest.raises(ValueError):
        await store.patch_blocks(made["event_id"], [{"op": "set_checked", "block_id": "nope", "item_id": "a", "checked": True}])
    stop()
    await store.set_status(made["event_id"], "done")
    assert [item["change"] for item in heard] == ["created", "blocks"] and heard[1]["event_id"] == made["event_id"]


# -- the agent's tools -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_plans_a_week_of_meals_in_three_calls(db, monkeypatch):
    """Acceptance 3: one CalendarTypeUpsert, one CalendarBulkCreate of 35 events in a group, and the
    sum of the type's field under each day."""
    tools = _tools(monkeypatch, db)
    context = _context()
    await CalendarStore(db).save_settings({"timezone": "Asia/Almaty"})
    made = await tools.calendar_type_upsert().invoke(context, {"type_id": "meal", **{key: MEAL[key] for key in ("name", "fields_schema", "default_blocks", "display", "aggregates")}})
    assert not made.is_error, made.content
    assert json.loads(made.content) == {"id": "meal", "name": "Meal", "version": 1, "fields": ["kcal", "meal", "protein_g"]}
    group = await tools.calendar_group_create().invoke(context, {"name": "Menu 14-20 October"})
    group_id = json.loads(group.content)["id"]
    plan = []
    for day in range(14, 21):
        for hour, meal, kcal in ((8, "Breakfast", 500), (11, "Lunch", 300), (13, "Lunch", 700), (16, "Dinner", 200), (19, "Dinner", 300)):
            plan.append({"title": f"{meal} {day}", "start_at": f"2026-10-{day}T{hour:02d}:00:00+05:00", "end_at": f"2026-10-{day}T{hour:02d}:30:00+05:00",
                         "fields": {"kcal": kcal, "protein_g": 20, "meal": meal}, "blocks": [{"kind": "checklist", "title": "Products", "items": ["eggs", "bread"]}]})
    created = await tools.calendar_bulk_create().invoke(context, {"events": plan, "type": "meal", "group_id": group_id})
    assert not created.is_error, created.content
    body = json.loads(created.content)
    assert body["created"] == 35 and len(body["events"]) == 35 and set(body["events"][0]) == {"event_id", "title", "start_at", "version"}
    found = await CalendarStore(db).aggregates("2026-10-14T00:00:00+05:00", "2026-10-21T00:00:00+05:00", group_id=group_id)
    assert [(day["date"], day["items"][0]["value"], day["items"][0]["ok"]) for day in found["days"]] == [(f"2026-10-{day}", 2000, True) for day in range(14, 21)]
    listed = json.loads((await tools.calendar_events().invoke(context, {"group_id": group_id})).content)["events"]
    assert len(listed) == 35 and listed[0]["chip"] == "Breakfast · 500 kcal" and listed[0]["progress"] == {"done": 0, "total": 2}
    assert all("blocks" not in item and "description" not in item for item in listed), "a light list carries no bodies"
    assert {entry["actor"] for entry in await CalendarStore(db).history(body["events"][0]["event_id"])} == {"agent:s1"}


@pytest.mark.asyncio
async def test_a_bulk_create_with_one_bad_event_creates_none(db, monkeypatch):
    tools = _tools(monkeypatch, db)
    plan = [{"title": f"Item {index}", "start_at": "2026-10-14T08:00:00Z", "end_at": "2026-10-14T09:00:00Z"} for index in range(5)]
    plan[3]["status"] = "maybe"
    failed = await tools.calendar_bulk_create().invoke(_context(), {"events": plan})
    assert failed.is_error and "event 4 (Item 3)" in failed.content
    assert await db.fetchone("SELECT count(*) AS n FROM calendar_events") is not None
    assert (await db.fetchone("SELECT count(*) AS n FROM calendar_events"))["n"] == 0


@pytest.mark.asyncio
async def test_the_operators_tick_reaches_the_agent_at_once(db, monkeypatch):
    """Acceptance 4, first half: what the operator ticks in the app is in CalendarEvents and
    CalendarEventHistory on the agent's next call, with nothing in between to wait for."""
    tools = _tools(monkeypatch, db)
    api = FastAPI()
    register(api, SimpleNamespace(db=db, notifications=None), lambda: {"user_id": "operator"})
    context = _context()
    made = json.loads((await tools.calendar_create().invoke(context, {"title": "Order", "start_at": "2026-10-14T08:00:00Z", "end_at": "2026-10-14T09:00:00Z", "blocks": [_basket()]})).content)
    block_id = made["blocks"][0]["id"]
    assert made["blocks"][0] == {"id": block_id, "kind": "table", "title": "Basket", "done": 1, "total": 2}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://testserver") as client:
        ticked = await client.patch(f"/api/calendar/events/{made['event_id']}/blocks", json={"ops": [{"op": "set_checked", "block_id": block_id, "row_id": "r1", "column": "got", "checked": True}]})
        assert ticked.status_code == 200, ticked.text
    seen = json.loads((await tools.calendar_events().invoke(context, {"event_id": made["event_id"]})).content)
    assert seen["progress"] == {"done": 2, "total": 2} and seen["blocks"][0]["rows"][0]["cells"]["got"] is True
    history = json.loads((await tools.calendar_event_history().invoke(context, {"event_id": made["event_id"]})).content)["entries"]
    assert history[0] | {"at": None} == {"at": None, "actor": "operator", "action": "set_checked", "block_id": block_id, "item_id": "r1", "summary": "checked Buckwheat 1 kg"}
    assert history[-1]["actor"] == "agent:s1"


@pytest.mark.asyncio
async def test_the_agents_block_patch_and_type_upsert_keep_what_they_were_not_told(db, monkeypatch):
    tools = _tools(monkeypatch, db)
    context = _context("s7")
    await tools.calendar_type_upsert().invoke(context, {"type_id": "meal", **{key: MEAL[key] for key in ("name", "fields_schema", "display", "aggregates")}})
    renamed = json.loads((await tools.calendar_type_upsert().invoke(context, {"type_id": "meal", "name": "Meal plan"})).content)
    assert renamed["version"] == 2 and renamed["fields"] == ["kcal", "meal", "protein_g"]
    made = json.loads((await tools.calendar_create().invoke(context, {"title": "Order", "start_at": "2026-10-14T08:00:00Z", "end_at": "2026-10-14T09:00:00Z",
                                                                      "blocks": [{"kind": "checklist", "items": ["a"]}, {"kind": "action", "action": "agent", "label": "Rebuild", "prompt": "rebuild the basket"}]})).content)
    stored = await CalendarStore(db).get(made["event_id"])
    assert stored["blocks"][1]["session_id"] == "s7", "an agent action with no session comes back to the session that made it"
    patched = await tools.calendar_block_patch().invoke(context, {"event_id": made["event_id"], "ops": [{"op": "set_checked", "block_id": made["blocks"][0]["id"], "item_id": stored["blocks"][0]["items"][0]["id"], "checked": True}]})
    assert not patched.is_error, patched.content
    assert json.loads(patched.content)["progress"] == {"done": 1, "total": 1}
    refused = await tools.calendar_block_patch().invoke(context, {"event_id": made["event_id"], "ops": [{"op": "remove_block", "block_id": "nope"}]})
    assert refused.is_error and "no block nope" in refused.content


# -- routes ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routes_follow_the_entries_contract(db):
    sent = []

    async def submit(session_id, text, **kwargs):
        sent.append((session_id, text, kwargs))
        return "run-1"

    api = FastAPI()
    register(api, SimpleNamespace(db=db, notifications=None, manager=SimpleNamespace(submit=submit)), lambda: {"user_id": "operator"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://testserver") as client:
        put = await client.put("/api/calendar/types/meal", json=MEAL)
        assert put.status_code == 200 and put.json()["version"] == 1 and put.json()["owner"] == "user"
        assert "meal" in [item["id"] for item in (await client.get("/api/calendar/types")).json()["types"]]
        assert (await client.put("/api/calendar/types/Bad Id", json=MEAL)).status_code == 400
        group = (await client.post("/api/calendar/groups", json={"name": "Menu", "icon": "\U0001f37d"})).json()
        made = await client.post("/api/calendar/events", json={"title": "Breakfast", "start_at": "2026-10-14T08:00:00+05:00", "end_at": "2026-10-14T08:30:00+05:00", "type": "meal",
                                                                "fields": {"kcal": 518, "meal": "Breakfast"}, "group_id": group["id"], "tags": ["food"],
                                                                "links": [{"kind": "url", "ref": "https://example.invalid/recipe", "label": "Recipe"}]})
        assert made.status_code == 201, made.text
        event = made.json()
        assert event["blocks"] and event["group_id"] == group["id"] and event["links"][0]["label"] == "Recipe"
        bulk = await client.post("/api/calendar/events/bulk", json={"events": [{"title": "Lunch", "start_at": "2026-10-14T13:00:00+05:00", "end_at": "2026-10-14T13:30:00+05:00", "type": "meal", "fields": {"kcal": 1500}, "group_id": group["id"]}]})
        assert bulk.status_code == 201 and len(bulk.json()["events"]) == 1
        listed = (await client.get("/api/calendar/events", params={"start": "2026-10-13T00:00:00Z", "end": "2026-10-16T00:00:00Z", "group_id": group["id"]})).json()
        assert [item["title"] for item in listed] == ["Breakfast", "Lunch"] and "blocks" not in listed[0] and listed[0]["block_summary"] and listed[0]["chip"] == "Breakfast · 518 kcal"
        full = (await client.get("/api/calendar/events", params={"start": "2026-10-13T00:00:00Z", "end": "2026-10-16T00:00:00Z", "tag": "food", "include_blocks": "true"})).json()
        assert len(full) == 1 and full[0]["blocks"]
        checklist = next(block for block in event["blocks"] if block["kind"] == "checklist")
        patched = await client.patch(f"/api/calendar/events/{event['event_id']}/blocks", json={"ops": [{"op": "add_item", "block_id": checklist["id"], "item": {"text": "eggs"}}], "version": event["version"]})
        assert patched.status_code == 200 and patched.json()["progress"] == {"done": 0, "total": 1}
        stale = await client.patch(f"/api/calendar/events/{event['event_id']}/blocks", json={"ops": [{"op": "add_item", "block_id": checklist["id"], "item": {"text": "milk"}}], "version": event["version"]})
        assert stale.status_code == 409
        status = await client.post(f"/api/calendar/events/{event['event_id']}/status", json={"status": "done"})
        assert status.status_code == 200 and status.json()["status"] == "done"
        assert (await client.post(f"/api/calendar/events/{event['event_id']}/status", json={"status": "eaten"})).status_code == 400
        history = (await client.get(f"/api/calendar/events/{event['event_id']}/history")).json()["entries"]
        assert [entry["action"] for entry in history] == ["status", "add_item", "created"] and {entry["actor"] for entry in history} == {"operator"}
        aggregates = (await client.get("/api/calendar/aggregates", params={"from": "2026-10-13T00:00:00Z", "to": "2026-10-16T00:00:00Z", "type": "meal"})).json()
        assert aggregates["days"] == [{"date": "2026-10-14", "items": [{"type": "meal", "field": "kcal", "op": "sum", "label": "kcal a day", "value": 2018, "unit": "kcal", "target": [1900, 2100], "ok": True}]}]
        groups = (await client.get("/api/calendar/groups")).json()["groups"]
        assert groups[0]["event_count"] == 2 and groups[0]["progress"] == {"done": 1, "total": 2} and groups[0]["range"]["from"] == "2026-10-14T03:00:00+00:00"
        assert (await client.patch(f"/api/calendar/groups/{group['id']}", json={"archived": True, "hidden": True})).json()["hidden"] is True
        moved = await client.post(f"/api/calendar/groups/{group['id']}/shift", json={"days": 1})
        assert moved.status_code == 200 and moved.json()["range"]["from"] == "2026-10-15T03:00:00+00:00"
        copy = await client.post(f"/api/calendar/groups/{group['id']}/duplicate", json={"offset_days": 7, "name": "Menu next week"})
        assert copy.status_code == 201 and copy.json()["name"] == "Menu next week" and copy.json()["event_count"] == 2
        action = await client.patch(f"/api/calendar/events/{event['event_id']}/blocks", json={"ops": [{"op": "add_block", "block": {"id": "act", "kind": "action", "action": "agent", "label": "Rebuild", "session_id": "s1", "prompt": "rebuild the basket"}}]})
        assert action.status_code == 200, action.text
        ran = await client.post(f"/api/calendar/events/{event['event_id']}/blocks/act/run")
        assert ran.status_code == 200 and ran.json() == {"ok": True, "session_id": "s1", "run_id": "run-1"}
        assert sent[0][0] == "s1" and "rebuild the basket" in sent[0][1] and event["event_id"] in sent[0][1] and "☐ eggs" in sent[0][1] and sent[0][2]["origin"] == "operator"
        assert (await client.post(f"/api/calendar/events/{event['event_id']}/blocks/{checklist['id']}/run")).status_code == 400
        assert (await client.delete(f"/api/calendar/groups/{group['id']}", params={"events": "delete"})).json() == {"ok": True}
        assert (await client.get(f"/api/calendar/events/{event['event_id']}")).status_code == 404
        assert (await client.delete("/api/calendar/types/meal")).json() == {"ok": True}
        assert (await client.delete("/api/calendar/types/meal")).status_code == 404


# -- export to connected calendars -----------------------------------------------------------


def test_blocks_read_as_plain_text():
    blocks = entries.normalize_blocks([
        {"kind": "markdown", "title": "Notes", "text": "Cook on **Sunday**."},
        {"kind": "checklist", "title": "Products", "items": [{"text": "eggs", "checked": True}, {"text": "milk"}]},
        _basket(),
        {"kind": "steps", "title": "Recipe", "items": [{"text": "boil", "duration_min": 15, "done": True}, {"text": "serve"}]},
        {"kind": "fields", "source": "fields"},
        {"kind": "action", "action": "url", "label": "Open the shop", "url": "https://example.invalid/shop"},
        {"kind": "action", "action": "agent", "label": "Rebuild", "session_id": "s", "prompt": "p"},
        {"kind": "timeline", "lanes": []},
    ])
    assert entries.compose("Plan for Monday", blocks, {"kcal": 518}) == (
        "Plan for Monday\n\n"
        "Notes\nCook on **Sunday**.\n\n"
        "Products\n☑ eggs\n☐ milk\n\n"
        "Basket\n• Buckwheat 1 kg · 1 · 435 ₸ · ☐\n• Milk · 2 · 650 ₸ · ☑\nΣ Price 1085 ₸ · ✓ 1/2\n\n"
        "Recipe\n1. ✓ boil (15 min)\n2. serve\n\n"
        "kcal: 518\n\n"
        "Open the shop: https://example.invalid/shop"
    )
    assert entries.compose("Only text", [], None) == "Only text"


@pytest.mark.asyncio
async def test_blocks_go_to_google_as_text_and_a_second_sync_neither_duplicates_nor_loses_them(db):
    """Acceptance 5."""
    store = CalendarStore(db)
    account = await store.connect("google", "Work", OAUTH)
    made = await store.create({"calendar_id": account["calendar_id"], "title": "Order", "description": "Deliver at 18:00", "start_at": "2026-10-14T08:00:00Z", "end_at": "2026-10-14T09:00:00Z",
                               "blocks": [{"id": "c", "kind": "checklist", "title": "Basket", "items": [{"id": "a", "text": "eggs"}, {"id": "b", "text": "milk", "checked": True}]}]})
    remote = {"description": None, "etag": '"1"'}
    posts = []

    def item():
        return {"id": "ev-1", "etag": remote["etag"], "summary": "Order", "description": remote["description"], "start": {"dateTime": "2026-10-14T08:00:00Z"}, "end": {"dateTime": "2026-10-14T09:00:00Z"}}

    def reply(request):
        if request.method in ("POST", "PATCH"):
            posts.append(json.loads(request.content))
            remote["description"] = posts[-1]["description"]
            return httpx.Response(200, json={"id": "ev-1", "etag": remote["etag"]})
        return httpx.Response(200, json={"nextSyncToken": "t", "items": [item()]})

    async def sync():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            await CalendarSync(store)._json_provider(client, await store.account(account["id"]), "token")
        return await store.get(made["event_id"])

    first = await sync()
    assert posts[0]["description"] == "Deliver at 18:00\n\nBasket\n☐ eggs\n☑ milk"
    assert first["description"] == "Deliver at 18:00" and len(first["blocks"]) == 1 and not first["pending_sync"]
    again = await sync()
    assert again["description"] == "Deliver at 18:00" and again["blocks"] == first["blocks"] and again["version"] == first["version"]
    # A tick here is sent as text; reading it back changes nothing.
    await store.patch_blocks(made["event_id"], [{"op": "set_checked", "block_id": "c", "item_id": "a", "checked": True}])
    ticked = await sync()
    assert posts[-1]["description"].endswith("☑ eggs\n☑ milk") and ticked["description"] == "Deliver at 18:00" and len(ticked["blocks"]) == 1
    # Edited in Google above the blocks: the edit is the new description, the blocks stay.
    remote["description"] = remote["description"].replace("18:00", "19:00")
    remote["etag"] = '"2"'
    edited = await sync()
    assert edited["description"] == "Deliver at 19:00" and edited["blocks"] == ticked["blocks"]
    # Edited inside the blocks: they are kept, and the remote text is kept once as a block of its own.
    remote["description"] = "Deliver at 19:00\n\nBasket\n☑ eggs\n☑ milk\n☐ bread"
    remote["etag"] = '"3"'
    absorbed = await sync()
    assert absorbed["description"] == "Deliver at 19:00" and len(absorbed["blocks"]) == 2
    assert absorbed["blocks"][0] == ticked["blocks"][0] and absorbed["blocks"][1] | {"id": None} == {"id": None, "kind": "markdown", "title": "", "text": remote["description"], "source": "remote"}
    assert len((await sync())["blocks"]) == 2, "the same remote text is not absorbed twice"
    assert (await store.history(made["event_id"]))[0]["actor"] == "sync"
    row = await db.fetchone("SELECT * FROM calendar_events WHERE id=?", (made["event_id"],))
    assert exported(row).startswith("Deliver at 19:00\n\nBasket\n☑ eggs")
