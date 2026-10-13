"""Structured calendar entries: blocks, typed fields, type definitions and their plain-text export.

Everything here is pure: it takes JSON-shaped values and returns new ones, so the store can run it
inside a transaction and the tests can run it without a database.

A block is ``{"id", "kind", "title"?, "collapsed"?, ...payload}``. Its ``id`` and the ids of its
items and rows are stable, because the operator ticks one item while the agent edits another and
each change has to name what it touches. A block of a kind this build does not know is kept exactly
as it came, apart from an ``id`` when it had none: the app of a newer build, or an agent, may write
kinds this host has never heard of, and a save that dropped them would lose the operator's data.

Field values are checked against the type's ``fields_schema`` but never removed for failing it. A
value that no longer fits (the type changed, an agent wrote a string where a number belongs) stays
and is reported in ``field_issues``, so a schema change cannot destroy what was recorded under the
old one.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import date
from typing import Any

STATUSES = ("planned", "done", "skipped", "cancelled")
KNOWN_KINDS = ("markdown", "checklist", "table", "fields", "steps", "link", "attachment", "action", "ref")
FIELD_TYPES = ("string", "number", "integer", "boolean", "enum", "date", "money")
COLUMN_TYPES = ("text", "number", "money", "checkbox", "link", "date")
AGGREGATE_OPS = ("sum", "avg", "count", "min", "max")
LINK_KINDS = ("event", "task", "url", "file")

MAX_BLOCKS = 100
MAX_ITEMS = 1000
"""Items or rows in one block. A shopping list of a few hundred lines is real; ten thousand is a bug."""
MAX_BLOCKS_BYTES = 512 * 1024
"""The serialised blocks of one event. A row is read whole on every list, so this bounds a page."""
MAX_FIELDS_BYTES = 32 * 1024
MAX_TYPES = 100
"""Types per installation: enough for every kind of plan, few enough that an agent looping on
CalendarTypeUpsert with fresh ids is stopped long before the registry becomes noise."""
MAX_SCHEMA_BYTES = 32 * 1024
MAX_TAGS = 20
MAX_LINKS = 50
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
OPS = ("set_checked", "add_item", "update_item", "remove_item", "add_row", "update_cell", "remove_row", "reorder", "add_block", "update_block", "remove_block", "reorder_blocks")
VERSIONLESS_OPS = ("set_checked",)
"""Ops that never wait for the event's version. A tick is one bit of one item; refusing it because
someone else ticked another item meanwhile would make two people shopping from one list fight."""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _text(value: Any, what: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        raise ValueError(f"{what} must be text")
    text = str(value)
    if required and not text.strip():
        raise ValueError(f"{what} is required")
    if len(text) > limit:
        raise ValueError(f"{what} is longer than {limit} characters")
    return text


def _list(value: Any, what: str, limit: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list")
    if len(value) > limit:
        raise ValueError(f"{what} holds at most {limit} entries")
    return value


# -- blocks ------------------------------------------------------------------------------------


def _children_key(block: dict[str, Any]) -> str | None:
    """The list inside a block that ops address by id: items of a checklist or steps, rows of a table."""
    kind = block.get("kind")
    if kind in ("checklist", "steps"):
        return "items"
    if kind == "table":
        return "rows"
    return None


def _normalize_child(kind: str, child: Any, seen: set[str]) -> dict[str, Any]:
    if isinstance(child, str) and kind in ("checklist", "steps"):
        # An agent writing a checklist as a list of strings means one item per string.
        child = {"text": child}
    if not isinstance(child, dict):
        raise ValueError(f"a {kind} entry must be an object")
    child = dict(child)
    child_id = child.get("id")
    if not isinstance(child_id, str) or not child_id or child_id in seen or len(child_id) > 64:
        child["id"] = new_id("row" if kind == "table" else "itm")
    seen.add(child["id"])
    if kind == "checklist":
        child["text"] = _text(child.get("text"), "a checklist item", 2000)
        child["checked"] = bool(child.get("checked", False))
    elif kind == "steps":
        child["text"] = _text(child.get("text"), "a step", 4000)
        child["done"] = bool(child.get("done", False))
        if child.get("duration_min") is not None:
            minutes = child["duration_min"]
            if isinstance(minutes, bool) or not isinstance(minutes, int | float) or not 0 < minutes <= 24 * 60:
                raise ValueError("a step's duration_min is 1 to 1440 minutes")
    elif kind == "table":
        cells = child.get("cells", {})
        if not isinstance(cells, dict):
            raise ValueError("a table row's cells must be an object")
        child["cells"] = dict(cells)
    return child


def normalize_block(block: Any, seen: set[str]) -> dict[str, Any]:
    """One block made safe to store: an id, ids for its items, the known kinds' shapes checked."""
    if not isinstance(block, dict):
        raise ValueError("a block must be an object")
    kind = block.get("kind")
    if not isinstance(kind, str) or not kind or len(kind) > 40:
        raise ValueError("a block needs a kind")
    block = dict(block)
    block_id = block.get("id")
    if not isinstance(block_id, str) or not block_id or block_id in seen or len(block_id) > 64:
        block["id"] = new_id("blk")
    seen.add(block["id"])
    if kind not in KNOWN_KINDS:
        return block
    if "title" in block:
        block["title"] = _text(block["title"], "a block title", 200)
    if kind == "markdown":
        block["text"] = _text(block.get("text"), "a markdown block", 100_000)
    elif kind in ("checklist", "steps", "table"):
        key = _children_key(block)
        assert key is not None
        children: set[str] = set()
        block[key] = [_normalize_child(kind, child, children) for child in _list(block.get(key), f"a {kind}'s {key}", MAX_ITEMS)]
        if kind == "table":
            columns = []
            keys: set[str] = set()
            for column in _list(block.get("columns"), "a table's columns", 50):
                if not isinstance(column, dict) or not isinstance(column.get("key"), str) or not column["key"] or column["key"] in keys:
                    raise ValueError("every table column needs a distinct key")
                keys.add(column["key"])
                column = dict(column)
                if column.get("type", "text") not in COLUMN_TYPES:
                    raise ValueError(f"a table column's type is one of {', '.join(COLUMN_TYPES)}")
                if column.get("aggregate") is not None and column["aggregate"] not in ("sum", "avg", "count"):
                    raise ValueError("a table column's aggregate is sum, avg or count")
                columns.append(column)
            block["columns"] = columns
    elif kind == "fields":
        if "items" in block:
            items = []
            for item in _list(block.get("items"), "a fields block's items", 200):
                if not isinstance(item, dict):
                    raise ValueError("a fields item must be an object")
                items.append({**item, "label": _text(item.get("label"), "a fields label", 200)})
            block["items"] = items
    elif kind in ("link", "attachment"):
        if block.get("url") is not None:
            _url(block["url"])
    elif kind == "action":
        # The spec's example wrote the action's sort as ``kind_action``; the contract names it
        # ``action``. The old spelling is read so an agent following the spec is not refused.
        action = block.get("action", block.pop("kind_action", None))
        if action not in ("url", "agent"):
            raise ValueError("an action block's action is url or agent")
        block["action"] = action
        block["label"] = _text(block.get("label"), "an action's label", 200, required=True)
        if action == "url":
            _url(block.get("url"))
        else:
            _text(block.get("session_id"), "an agent action's session_id", 200, required=True)
            _text(block.get("prompt"), "an agent action's prompt", 4000, required=True)
    elif kind == "ref":
        _text(block.get("ref_id"), "a ref block's ref_id", 200, required=True)
    return block


def _url(value: Any) -> str:
    text = _text(value, "a URL", 4000, required=True)
    if not re.match(r"^https?://", text):
        raise ValueError("a URL starts with http:// or https://")
    return text


def normalize_blocks(blocks: Any) -> list[dict[str, Any]]:
    seen: set[str] = set()
    found = [normalize_block(block, seen) for block in _list(blocks, "blocks", MAX_BLOCKS)]
    if len(json.dumps(found, ensure_ascii=False).encode()) > MAX_BLOCKS_BYTES:
        raise ValueError(f"an event's blocks are limited to {MAX_BLOCKS_BYTES // 1024} KB")
    return found


def _without_ids(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bare = []
    for block in copy.deepcopy(blocks):
        block.pop("id", None)
        key = _children_key(block)
        children = block.get(key) if key else None
        for child in children or []:
            if isinstance(child, dict):
                child.pop("id", None)
        bare.append(block)
    return bare


def instantiate(template: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A type's default blocks for one new event: a copy with fresh ids, so two events never share one."""
    return normalize_blocks(_without_ids(template))


def reset_marks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The blocks with every tick cleared, for a plan duplicated to run again."""
    fresh = copy.deepcopy(blocks)
    for block in fresh:
        kind = block.get("kind")
        if kind == "checklist":
            for item in block.get("items") or []:
                item["checked"] = False
        elif kind == "steps":
            for item in block.get("items") or []:
                item["done"] = False
        elif kind == "table":
            boxes = [column["key"] for column in block.get("columns") or [] if column.get("type") == "checkbox"]
            for row in block.get("rows") or []:
                for key in boxes:
                    if key in row.get("cells", {}):
                        row["cells"][key] = False
    return fresh


def progress(block: dict[str, Any]) -> tuple[int, int] | None:
    """(done, total) of a block that can be ticked off; None for one that cannot."""
    kind = block.get("kind")
    if kind == "checklist":
        items = block.get("items") or []
        return sum(1 for item in items if item.get("checked")), len(items)
    if kind == "steps":
        items = block.get("items") or []
        return sum(1 for item in items if item.get("done")), len(items)
    if kind == "table":
        boxes = [column["key"] for column in block.get("columns") or [] if isinstance(column, dict) and column.get("type") == "checkbox"]
        if not boxes:
            return None
        rows = block.get("rows") or []
        # The first checkbox column is the one that says "bought"; a second (say, "favourite") is
        # not progress.
        return sum(1 for row in rows if (row.get("cells") or {}).get(boxes[0]) is True), len(rows)
    return None


def complete(block: dict[str, Any]) -> bool:
    found = progress(block)
    return found is not None and found[1] > 0 and found[0] == found[1]


def block_summary(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for block in blocks:
        found = progress(block)
        summary.append({"id": block.get("id"), "kind": block.get("kind"), "title": block.get("title") or "", "done": found[0] if found else None, "total": found[1] if found else None})
    return summary


def event_progress(blocks: list[dict[str, Any]], badge: str = "") -> dict[str, int] | None:
    """The progress an event shows in its chip.

    The type's ``display.badge`` chooses: ``progress:<kind>`` the first block of that kind,
    ``progress:<block id>`` that block, ``progress:all`` every checkable block summed. Without a
    badge, the first checklist, or else the first table with a checkbox column.
    """
    target = badge.removeprefix("progress:") if badge.startswith("progress:") else ""
    if target == "all":
        found = [progress(block) for block in blocks]
        counted = [pair for pair in found if pair is not None]
        return {"done": sum(pair[0] for pair in counted), "total": sum(pair[1] for pair in counted)} if counted else None
    candidates: list[dict[str, Any]]
    if target:
        candidates = [block for block in blocks if block.get("id") == target] or [block for block in blocks if block.get("kind") == target]
    else:
        candidates = [block for block in blocks if block.get("kind") == "checklist"] + [block for block in blocks if block.get("kind") == "table"]
    for block in candidates:
        found = progress(block)
        if found is not None:
            return {"done": found[0], "total": found[1]}
    return None


def _find_block(blocks: list[dict[str, Any]], block_id: Any) -> dict[str, Any]:
    for block in blocks:
        if block.get("id") == block_id:
            return block
    raise ValueError(f"no block {block_id}")


def _find_child(block: dict[str, Any], child_id: Any) -> dict[str, Any]:
    key = _children_key(block)
    if key is None:
        raise ValueError(f"block {block.get('id')} ({block.get('kind')}) has no items")
    for child in block.get(key) or []:
        if child.get("id") == child_id:
            return child
    raise ValueError(f"block {block.get('id')} has no item {child_id}")


def _label(child: dict[str, Any], block: dict[str, Any]) -> str:
    if block.get("kind") == "table":
        cells = child.get("cells") or {}
        columns = block.get("columns") or []
        first = cells.get(columns[0]["key"]) if columns else next(iter(cells.values()), "")
        return str(first if first is not None else "")[:80]
    return str(child.get("text") or "")[:80]


def _index(value: Any, length: int) -> int:
    if value is None:
        return length
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("index must be an integer")
    return max(0, min(value, length))


def _reordered(entries: list[dict[str, Any]], ids: Any) -> list[dict[str, Any]]:
    if not isinstance(ids, list):
        raise ValueError("reorder needs ids, a list")
    by_id = {entry.get("id"): entry for entry in entries}
    unknown = [entry_id for entry_id in ids if entry_id not in by_id]
    if unknown:
        raise ValueError(f"reorder names unknown ids: {', '.join(map(str, unknown[:5]))}")
    # Ids left out keep their relative order after the named ones, so a client that only knows
    # the visible part of a long list can still move one item to the top.
    named = list(dict.fromkeys(ids))
    return [by_id[entry_id] for entry_id in named] + [entry for entry in entries if entry.get("id") not in set(named)]


def apply_ops(blocks: list[dict[str, Any]], ops: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply block ops in order to a copy of ``blocks``; returns (new blocks, history entries).

    All or nothing: an op that fails raises and nothing of the patch is kept, so a client never has
    to work out which half of its request landed.
    """
    if not isinstance(ops, list) or not ops:
        raise ValueError("ops must be a non-empty list")
    if len(ops) > 500:
        raise ValueError("one patch holds at most 500 ops")
    work = copy.deepcopy(blocks)
    journal: list[dict[str, Any]] = []
    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise ValueError(f"every op is an object whose op is one of {', '.join(OPS)}")
        name = op["op"]
        if name == "add_block":
            seen = {block.get("id") for block in work}
            block = normalize_block(op.get("block"), {entry for entry in seen if isinstance(entry, str)})
            work.insert(_index(op.get("index"), len(work)), block)
            journal.append({"action": name, "block_id": block["id"], "summary": f"added {block['kind']} block {block.get('title') or ''}".strip()})
            continue
        if name == "reorder_blocks":
            work = _reordered(work, op.get("ids"))
            journal.append({"action": name, "summary": "reordered blocks"})
            continue
        block = _find_block(work, op.get("block_id"))
        block_id = block["id"]
        if name == "remove_block":
            work = [entry for entry in work if entry is not block]
            journal.append({"action": name, "block_id": block_id, "summary": f"removed {block.get('kind')} block {block.get('title') or ''}".strip()})
            continue
        if name == "update_block":
            patch = op.get("patch")
            if not isinstance(patch, dict):
                raise ValueError("update_block needs a patch object")
            changed = {key: value for key, value in patch.items() if key not in ("id", "kind")}
            replaced = normalize_block({**block, **changed}, {entry["id"] for entry in work if entry is not block})
            work[work.index(block)] = replaced
            journal.append({"action": name, "block_id": block_id, "summary": "changed " + ", ".join(sorted(changed)) if changed else "no change"})
            continue
        if block.get("kind") not in KNOWN_KINDS:
            raise ValueError(f"block {block_id} is of a kind this calendar does not edit ({block.get('kind')})")
        if name == "set_checked":
            checked = op.get("checked")
            if not isinstance(checked, bool):
                raise ValueError("set_checked needs checked, true or false")
            if op.get("column") is not None:
                if block.get("kind") != "table":
                    raise ValueError("a column is only given for a table")
                column = next((entry for entry in block.get("columns") or [] if entry.get("key") == op["column"]), None)
                if column is None or column.get("type") != "checkbox":
                    raise ValueError(f"column {op['column']} is not a checkbox column")
                row = _find_child(block, op.get("row_id", op.get("item_id")))
                row.setdefault("cells", {})[op["column"]] = checked
                journal.append({"action": name, "block_id": block_id, "item_id": row["id"], "summary": f"{'checked' if checked else 'unchecked'} {_label(row, block)}"})
                continue
            if block.get("kind") not in ("checklist", "steps"):
                raise ValueError("set_checked on a table names its row_id and checkbox column")
            item = _find_child(block, op.get("item_id"))
            item["checked" if block["kind"] == "checklist" else "done"] = checked
            journal.append({"action": name, "block_id": block_id, "item_id": item["id"], "summary": f"{'checked' if checked else 'unchecked'} {_label(item, block)}"})
            continue
        key = _children_key(block)
        if key is None:
            raise ValueError(f"block {block_id} ({block.get('kind')}) has no items or rows")
        children = block.setdefault(key, [])
        if name in ("add_item", "add_row"):
            entry = op.get("item" if name == "add_item" else "row", op.get("row" if name == "add_item" else "item"))
            if len(children) >= MAX_ITEMS:
                raise ValueError(f"a block holds at most {MAX_ITEMS} entries")
            child = _normalize_child(block["kind"], entry, {child["id"] for child in children})
            children.insert(_index(op.get("index"), len(children)), child)
            journal.append({"action": name, "block_id": block_id, "item_id": child["id"], "summary": f"added {_label(child, block)}"})
        elif name in ("remove_item", "remove_row"):
            child = _find_child(block, op.get("item_id", op.get("row_id")))
            children.remove(child)
            journal.append({"action": name, "block_id": block_id, "item_id": child["id"], "summary": f"removed {_label(child, block)}"})
        elif name == "update_item":
            child = _find_child(block, op.get("item_id", op.get("row_id")))
            patch = op.get("patch")
            if not isinstance(patch, dict):
                raise ValueError("update_item needs a patch object")
            merged = {**child, **{name_: value for name_, value in patch.items() if name_ != "id"}}
            if block["kind"] == "table" and isinstance(patch.get("cells"), dict):
                merged["cells"] = {**(child.get("cells") or {}), **patch["cells"]}
            replaced = _normalize_child(block["kind"], merged, {entry["id"] for entry in children if entry is not child})
            children[children.index(child)] = replaced
            journal.append({"action": name, "block_id": block_id, "item_id": child["id"], "summary": f"changed {_label(replaced, block)}"})
        elif name == "update_cell":
            if block["kind"] != "table":
                raise ValueError("update_cell is for a table")
            row = _find_child(block, op.get("row_id", op.get("item_id")))
            column = op.get("column")
            if not isinstance(column, str) or not any(entry.get("key") == column for entry in block.get("columns") or []):
                raise ValueError(f"table {block_id} has no column {column}")
            row.setdefault("cells", {})[column] = op.get("value")
            journal.append({"action": name, "block_id": block_id, "item_id": row["id"], "summary": f"set {column} of {_label(row, block)}"})
        elif name == "reorder":
            block[key] = _reordered(children, op.get("ids"))
            journal.append({"action": name, "block_id": block_id, "summary": f"reordered {key}"})
    return normalize_blocks(work), journal


def completed_blocks(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Blocks that were there unfinished before and are finished now: the transitions a hook fires on.

    A block that arrives already finished (a new event, a pasted list) is not a transition: nobody
    finished anything, and a hook that fired then would start the follow-up plan on creation.
    """
    unfinished = {block.get("id") for block in before if progress(block) is not None and not complete(block)}
    return [block for block in after if block.get("id") in unfinished and complete(block)]


# -- fields and types --------------------------------------------------------------------------


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        try:
            number = float(value.replace(",", ".").replace(" ", ""))
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    return None


def amount(value: Any) -> float | int | None:
    """The number in a field value: a number, a numeric string, or a money object's ``amount``."""
    if isinstance(value, dict):
        value = value.get("amount")
    return _number(value)


def check_fields(fields: Any, schema: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, str]]:
    """Fields checked against a type's schema: (fields to store, problems by key).

    Values are coerced only where the intent is unambiguous (``"518"`` for a number); anything
    else is stored as given and named in the problems.
    """
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        raise ValueError("fields must be an object")
    if len(json.dumps(fields, ensure_ascii=False, default=str).encode()) > MAX_FIELDS_BYTES:
        raise ValueError(f"an event's fields are limited to {MAX_FIELDS_BYTES // 1024} KB")
    stored: dict[str, Any] = {}
    issues: dict[str, str] = {}
    schema = schema or {}
    for key, value in fields.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            raise ValueError("a field name is 1 to 64 characters")
        spec = schema.get(key)
        stored[key] = value
        if spec is None:
            if schema:
                issues[key] = "not in the type's schema"
            continue
        if value is None:
            continue
        kind = spec.get("type")
        if kind in ("number", "integer"):
            number = _number(value)
            if number is None or (kind == "integer" and not float(number).is_integer()):
                issues[key] = f"expected {'an integer' if kind == 'integer' else 'a number'}"
            else:
                stored[key] = int(number) if kind == "integer" else number
        elif kind == "money":
            if isinstance(value, dict):
                if _number(value.get("amount")) is None:
                    issues[key] = "expected an amount"
            elif _number(value) is None:
                issues[key] = "expected an amount"
            else:
                stored[key] = _number(value)
        elif kind == "boolean":
            if not isinstance(value, bool):
                issues[key] = "expected true or false"
        elif kind == "enum":
            if value not in (spec.get("values") or []):
                issues[key] = "not one of the allowed values"
        elif kind == "date":
            try:
                date.fromisoformat(str(value)[:10])
                if not isinstance(value, str):
                    raise ValueError
            except ValueError:
                issues[key] = "expected a date (YYYY-MM-DD)"
        elif kind == "string" and not isinstance(value, str):
            issues[key] = "expected text"
    for key, spec in schema.items():
        if spec.get("required") and stored.get(key) in (None, ""):
            issues[key] = "required"
    return stored, issues


def check_type(body: Any, type_id: str) -> dict[str, Any]:
    """A type definition validated and reduced to the keys this calendar understands."""
    if not isinstance(body, dict):
        raise ValueError("a type definition must be an object")
    if not SLUG.match(type_id or ""):
        raise ValueError("a type id is a slug: lower-case letters, digits, - and _, up to 48 characters")
    name = _text(body.get("name"), "a type's name", 80, required=True).strip()
    icon = _text(body.get("icon"), "a type's icon", 40)
    color = _text(body.get("color"), "a type's colour", 7)
    if color and not COLOR.match(color):
        raise ValueError("a type's colour is #rrggbb")
    schema_in = body.get("fields_schema") or {}
    if not isinstance(schema_in, dict) or len(schema_in) > 64:
        raise ValueError("fields_schema is an object of at most 64 fields")
    schema: dict[str, Any] = {}
    for key, spec in schema_in.items():
        if not isinstance(key, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", key):
            raise ValueError(f"field name {key!r} must be a letter or _ followed by letters, digits or _")
        if not isinstance(spec, dict) or spec.get("type") not in FIELD_TYPES:
            raise ValueError(f"field {key}'s type is one of {', '.join(FIELD_TYPES)}")
        entry = {name_: spec[name_] for name_ in ("type", "unit", "label", "values", "required", "currency", "min", "max") if name_ in spec}
        if spec["type"] == "enum":
            values = spec.get("values")
            if not isinstance(values, list) or not values or len(values) > 100 or not all(isinstance(value, str | int | float) and not isinstance(value, bool) for value in values):
                raise ValueError(f"enum field {key} needs 1 to 100 values")
        for text_key in ("unit", "label", "currency"):
            if text_key in entry:
                entry[text_key] = _text(entry[text_key], f"field {key}'s {text_key}", 40)
        schema[key] = entry
    # Checked as blocks, kept as a template without ids: every event made from it gets its own, and
    # ids minted here would make the same definition sent twice look like a change of the type.
    default_blocks = _without_ids(normalize_blocks(body.get("default_blocks") or []))
    display_in = body.get("display") or {}
    if not isinstance(display_in, dict):
        raise ValueError("display must be an object")
    display = {key: _text(display_in[key], f"display.{key}", 200) for key in ("chip", "subtitle", "badge") if display_in.get(key) is not None}
    aggregates = []
    for entry in _list(body.get("aggregates"), "aggregates", 20):
        if not isinstance(entry, dict) or entry.get("op", "sum") not in AGGREGATE_OPS:
            raise ValueError(f"an aggregate's op is one of {', '.join(AGGREGATE_OPS)}")
        op = entry.get("op", "sum")
        field = entry.get("field")
        if op != "count":
            spec = schema.get(field) if isinstance(field, str) else None
            if spec is None or spec["type"] not in ("number", "integer", "money"):
                raise ValueError(f"aggregate {op} needs a number, integer or money field of the schema (got {field!r})")
        per = entry.get("per", "day")
        if per not in ("day", "week"):
            raise ValueError("an aggregate is per day or per week")
        clean = {"field": field, "op": op, "per": per, "label": _text(entry.get("label"), "an aggregate's label", 80)}
        target = entry.get("target")
        if target is not None:
            if not isinstance(target, list) or len(target) != 2 or any(_number(bound) is None and bound is not None for bound in target):
                raise ValueError("an aggregate's target is [min, max]")
            clean["target"] = [_number(bound) if bound is not None else None for bound in target]
        aggregates.append(clean)
    flow = _list(body.get("status_flow"), "status_flow", len(STATUSES)) or ["planned", "done", "skipped"]
    if any(status not in STATUSES for status in flow):
        raise ValueError(f"status_flow names statuses from {', '.join(STATUSES)}")
    quick = [_text(action, "a quick action", 40) for action in _list(body.get("quick_actions"), "quick_actions", 10)]
    definition = {
        "name": name, "icon": icon, "color": color.lower(), "fields_schema": schema, "default_blocks": default_blocks,
        "display": display, "aggregates": aggregates, "status_flow": flow, "quick_actions": quick,
    }
    if len(json.dumps(definition, ensure_ascii=False).encode()) > MAX_SCHEMA_BYTES:
        raise ValueError(f"a type definition is limited to {MAX_SCHEMA_BYTES // 1024} KB")
    return definition


def check_status(value: Any) -> str:
    if value not in STATUSES:
        raise ValueError(f"status is one of {', '.join(STATUSES)}")
    return str(value)


def check_tags(value: Any) -> list[str]:
    tags: list[str] = []
    for tag in _list(value, "tags", MAX_TAGS):
        text = _text(tag, "a tag", 40).strip()
        if text and text not in tags:
            tags.append(text)
    return tags


def check_links(value: Any) -> list[dict[str, Any]]:
    links = []
    for link in _list(value, "links", MAX_LINKS):
        if not isinstance(link, dict) or link.get("kind") not in LINK_KINDS:
            raise ValueError(f"a link is {{kind, ref, label}} with kind one of {', '.join(LINK_KINDS)}")
        ref = _text(link.get("ref"), "a link's ref", 4000, required=True)
        if link["kind"] == "url":
            _url(ref)
        links.append({"kind": link["kind"], "ref": ref, "label": _text(link.get("label"), "a link's label", 200)})
    return links


def chip(template: str, title: str, fields: dict[str, Any], status: str, found: dict[str, int] | None) -> str | None:
    """A type's ``display.chip`` filled in: ``{name}`` is a field, ``{title}``, ``{status}`` and
    ``{progress}`` are the event's. A placeholder with no value leaves nothing, and the separators
    it would have stranded at either end are trimmed."""
    if not template:
        return None
    values = {**{key: value for key, value in fields.items() if isinstance(value, str | int | float) and not isinstance(value, bool)}, "title": title, "status": status}
    values["progress"] = f"{found['done']}/{found['total']}" if found else ""
    for key, value in fields.items():
        if isinstance(value, dict) and "amount" in value:
            values[key] = value["amount"]

    def fill(match: re.Match[str]) -> str:
        value = values.get(match.group(1))
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return "" if value is None else str(value)

    text = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", fill, template)
    text = re.sub(r"\s{2,}", " ", text).strip(" ·-|,")
    return text[:200] or None


STARTER_TYPES: dict[str, dict[str, Any]] = {
    "todo-list": {
        "name": "To-do list", "icon": "list", "color": "#4f7cff",
        "default_blocks": [{"kind": "checklist", "title": "", "show_progress": True}],
        "display": {"badge": "progress:checklist"}, "quick_actions": ["mark_done"],
    },
    "shopping": {
        "name": "Shopping", "icon": "cart", "color": "#2bb673",
        "fields_schema": {"store": {"type": "string"}, "total": {"type": "money"}},
        "default_blocks": [{"kind": "table", "title": "Basket", "columns": [
            {"key": "name", "label": "Item", "type": "text"}, {"key": "qty", "label": "Qty", "type": "text"},
            {"key": "price", "label": "Price", "type": "money", "aggregate": "sum"}, {"key": "got", "label": "\u2713", "type": "checkbox", "aggregate": "count"},
        ], "rows": []}],
        "display": {"chip": "{store} · {total}", "badge": "progress:table"},
        "aggregates": [{"field": "total", "op": "sum", "per": "day", "label": "Spent"}],
        "quick_actions": ["mark_done"],
    },
    "appointment": {
        "name": "Appointment", "icon": "calendar", "color": "#8e6cff",
        "fields_schema": {"place": {"type": "string"}, "with": {"type": "string"}},
        "default_blocks": [{"kind": "markdown", "title": "Notes", "text": ""}],
        "display": {"chip": "{title}"}, "status_flow": ["planned", "done", "cancelled"],
    },
    "routine": {
        "name": "Routine", "icon": "loop", "color": "#f5a623",
        "default_blocks": [{"kind": "steps", "title": "", "items": []}],
        "display": {"badge": "progress:steps"}, "quick_actions": ["mark_done", "skip"],
    },
}
"""Starter types seeded once as ordinary rows: the operator may change or delete them like any other."""


# -- plain-text export -------------------------------------------------------------------------


def _cell(value: Any, column: dict[str, Any]) -> str:
    kind = column.get("type", "text")
    if kind == "checkbox":
        return "☑" if value is True else "☐"
    if value is None or value == "":
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    unit = column.get("unit") or ""
    return f"{value} {unit}".strip() if kind in ("number", "money") else str(value)


def _total(block: dict[str, Any]) -> str:
    parts = []
    for column in block.get("columns") or []:
        op = column.get("aggregate")
        if not op:
            continue
        values = [(row.get("cells") or {}).get(column["key"]) for row in block.get("rows") or []]
        if column.get("type") == "checkbox":
            parts.append(f"{column.get('label') or column['key']} {sum(1 for value in values if value is True)}/{len(values)}")
            continue
        numbers = [number for number in (amount(value) for value in values) if number is not None]
        if op == "count":
            result: Any = len([value for value in values if value not in (None, "")])
        elif not numbers:
            continue
        elif op == "sum":
            result = sum(numbers)
        else:
            result = round(sum(numbers) / len(numbers), 2)
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        parts.append(f"{column.get('label') or column['key']} {result} {column.get('unit') or ''}".strip())
    return ("Σ " + " · ".join(parts)) if parts else ""


def blocks_text(blocks: list[dict[str, Any]], fields: dict[str, Any] | None = None) -> str:
    """The blocks as plain text for a calendar that only holds a description.

    Plain text rather than Markdown: Google and Outlook show a description as typed, and a reader
    there sees ``☑ Milk`` cleanly where ``- [x] Milk`` would be noise. Blocks with nothing to read
    outside the app (an agent action, an embedded event, a kind this build does not know) are left
    out rather than shown as JSON.
    """
    parts: list[str] = []
    for block in blocks:
        kind = block.get("kind")
        title = str(block.get("title") or "").strip()
        lines: list[str] = []
        if kind == "markdown":
            text = str(block.get("text") or "").strip()
            if text:
                lines.append(text)
        elif kind == "checklist":
            lines.extend(("☑ " if item.get("checked") else "☐ ") + str(item.get("text") or "") for item in block.get("items") or [])
        elif kind == "steps":
            for number, item in enumerate(block.get("items") or [], start=1):
                minutes = f" ({item['duration_min']} min)" if item.get("duration_min") else ""
                lines.append(f"{number}. {'✓ ' if item.get('done') else ''}{item.get('text') or ''}{minutes}")
        elif kind == "table":
            columns = [column for column in block.get("columns") or [] if isinstance(column, dict)]
            for row in block.get("rows") or []:
                cells = row.get("cells") or {}
                lines.append("• " + " · ".join(text for text in (_cell(cells.get(column["key"]), column) for column in columns) if text))
            total = _total(block)
            if total:
                lines.append(total)
        elif kind == "fields":
            if block.get("source") == "fields":
                lines.extend(f"{key}: {value}" for key, value in (fields or {}).items() if value not in (None, "") and not isinstance(value, dict | list))
            else:
                lines.extend(f"{item.get('label')}: {item.get('value')} {item.get('unit') or ''}".rstrip() for item in block.get("items") or [])
        elif kind in ("link", "attachment") and block.get("url"):
            lines.append(f"{block.get('label') or title or 'Link'}: {block['url']}")
        elif kind == "action" and block.get("action") == "url":
            lines.append(f"{block.get('label')}: {block.get('url')}")
        if not lines:
            continue
        parts.append("\n".join(([title] if title and kind not in ("link", "attachment") else []) + lines))
    return "\n\n".join(parts)


def compose(description: str, blocks: list[dict[str, Any]], fields: dict[str, Any] | None = None) -> str:
    """The description a remote calendar receives: the operator's text, then the blocks."""
    text = blocks_text(blocks, fields)
    head = (description or "").rstrip()
    if not text:
        return head
    return f"{head}\n\n{text}" if head else text


__all__ = [
    "AGGREGATE_OPS", "FIELD_TYPES", "KNOWN_KINDS", "MAX_TYPES", "STARTER_TYPES", "STATUSES", "VERSIONLESS_OPS",
    "amount", "apply_ops", "block_summary", "blocks_text", "check_fields", "check_links", "check_status", "check_tags",
    "check_type", "chip", "complete", "completed_blocks", "compose", "event_progress", "instantiate", "new_id",
    "normalize_blocks", "progress", "reset_marks",
]
