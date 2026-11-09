"""A JSON Canvas 1.0 document: checking it, writing it as Obsidian does, and changing it item by item.

The format is the one Obsidian opens (jsoncanvas.org): ``nodes`` and ``edges``, each item an object
with an ``id``. Array order is drawing order. Every key this module does not know is kept, on the
document, a node and an edge alike, because a file that went through Obsidian and back must not lose
what a plugin wrote there; ours sit under a ``daedalus`` key on a node, which Obsidian keeps too.

The writer reproduces Obsidian's own layout (tabs, one plain item per line, an item that holds an
object spread over lines, no spaces after colons, integers without a decimal point, no newline at the
end). That is not cosmetics: an exported file the operator keeps in a vault under git then changes
only on the lines that really changed when either program saves it.

Changes arrive as operations on items (``put`` and ``delete`` by id), from the board and from the
agent's tool alike. Each item remembers the version that last changed it (the clock), which is what
lets a writer holding an older version still save when it touched nothing the other writer touched:
the operator dragging a card while the agent relabels another is two saves, not a refusal.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from typing import Any

NODE_TYPES = ("text", "file", "link", "group")
SIDES = ("top", "right", "bottom", "left")
ENDS = ("none", "arrow")
BACKGROUND_STYLES = ("cover", "ratio", "repeat")
PRESET_COLORS = ("1", "2", "3", "4", "5", "6")
"""Obsidian's presets: red, orange, yellow, green, cyan, purple. Each app picks its own shade."""
HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

MAX_BYTES = 4_000_000
"""A document past this is refused. Our own canvases are kilobytes; the cap is for an import."""
MAX_ITEMS = 5000
MAX_TEXT = 100_000
"""Characters in one card. A card that long is a document, and belongs in a file node."""
MAX_ID = 128
MAX_COORDINATE = 10_000_000
"""Coordinates beyond this are a broken file, not a big board."""
MAX_TOMBSTONES = 5000
"""Deleted ids the clock remembers, newest kept. A writer more than this many deletions behind could
re-create an item the other writer removed, which is the whole cost of forgetting."""
TITLE_KEY = "#title"
"""The clock's entry for the canvas's title, which is not an item but can conflict like one."""

FILE_PREFIX = "files/"
"""A file node whose path starts here shows an attachment of the canvas (``files/<id>/<name>``);
any other path is a file of the workspace."""


class CanvasError(ValueError):
    """A document or an operation that cannot be accepted, in words the writer can act on."""


class Conflict(Exception):
    """Items an operation touches changed after the version the writer last saw."""

    def __init__(self, ids: list[str]) -> None:
        super().__init__(f"changed since your version: {', '.join(ids)}")
        self.ids = ids


def empty() -> dict[str, Any]:
    return {"nodes": [], "edges": []}


def _integer(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise CanvasError(f"{what} must be a number")
    number = round(value)
    if abs(number) > MAX_COORDINATE:
        raise CanvasError(f"{what} is out of range")
    return number


def _text(value: Any, what: str, *, limit: int = MAX_TEXT, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise CanvasError(f"{what} must be text")
    if len(value) > limit:
        raise CanvasError(f"{what} is longer than {limit} characters")
    return value


def _color(value: Any, what: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not (value in PRESET_COLORS or HEX_COLOR.match(value)):
        raise CanvasError(f"{what}: a colour is a preset \"1\" to \"6\" or #rrggbb")


def _identifier(item: dict[str, Any], what: str) -> str:
    value = item.get("id")
    if not isinstance(value, str) or not value or len(value) > MAX_ID or value != value.strip() or value == TITLE_KEY:
        raise CanvasError(f"{what} needs an id of 1 to {MAX_ID} characters")
    return value


def check_node(node: Any) -> dict[str, Any]:
    """The node with its numbers made whole, or :class:`CanvasError`. Unknown keys pass untouched."""
    if not isinstance(node, dict):
        raise CanvasError("a node is an object")
    node_id = _identifier(node, "a node")
    kind = node.get("type")
    if kind not in NODE_TYPES:
        raise CanvasError(f"node {node_id}: type is one of {', '.join(NODE_TYPES)}")
    checked = dict(node)
    for key in ("x", "y", "width", "height"):
        checked[key] = _integer(node.get(key), f"node {node_id}: {key}")
    if checked["width"] < 1 or checked["height"] < 1:
        raise CanvasError(f"node {node_id}: width and height must be at least 1")
    _color(node.get("color"), f"node {node_id}")
    if kind == "text":
        _text(node.get("text", ""), f"node {node_id}: text")
        checked.setdefault("text", "")
    elif kind == "file":
        path = _text(node.get("file"), f"node {node_id}: file", limit=1024)
        if not path:
            raise CanvasError(f"node {node_id}: a file node names a file")
        _text(node.get("subpath"), f"node {node_id}: subpath", limit=1024, required=False)
    elif kind == "link":
        _text(node.get("url"), f"node {node_id}: url", limit=4096)
    else:
        _text(node.get("label"), f"node {node_id}: label", limit=1000, required=False)
        _text(node.get("background"), f"node {node_id}: background", limit=1024, required=False)
        style = node.get("backgroundStyle")
        if style is not None and style not in BACKGROUND_STYLES:
            raise CanvasError(f"node {node_id}: backgroundStyle is one of {', '.join(BACKGROUND_STYLES)}")
    extra = node.get("daedalus")
    if extra is not None and not isinstance(extra, dict):
        raise CanvasError(f"node {node_id}: daedalus is an object")
    return checked


def check_edge(edge: Any) -> dict[str, Any]:
    if not isinstance(edge, dict):
        raise CanvasError("an edge is an object")
    edge_id = _identifier(edge, "an edge")
    for key in ("fromNode", "toNode"):
        if not isinstance(edge.get(key), str) or not edge[key]:
            raise CanvasError(f"edge {edge_id}: {key} names a node")
    for key in ("fromSide", "toSide"):
        if edge.get(key) is not None and edge[key] not in SIDES:
            raise CanvasError(f"edge {edge_id}: {key} is one of {', '.join(SIDES)}")
    for key in ("fromEnd", "toEnd"):
        if edge.get(key) is not None and edge[key] not in ENDS:
            raise CanvasError(f"edge {edge_id}: {key} is none or arrow")
    _color(edge.get("color"), f"edge {edge_id}")
    _text(edge.get("label"), f"edge {edge_id}: label", limit=1000, required=False)
    return dict(edge)


def check(doc: Any) -> dict[str, Any]:
    """A whole document, checked: every item valid, ids unique across nodes and edges, every edge
    between two nodes that exist, and the encoded size within :data:`MAX_BYTES`."""
    if not isinstance(doc, dict):
        raise CanvasError("a canvas is a JSON object with nodes and edges")
    nodes, edges = doc.get("nodes", []), doc.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise CanvasError("nodes and edges are arrays")
    if len(nodes) + len(edges) > MAX_ITEMS:
        raise CanvasError(f"a canvas holds at most {MAX_ITEMS} nodes and edges")
    checked_nodes = [check_node(node) for node in nodes]
    checked_edges = [check_edge(edge) for edge in edges]
    seen: set[str] = set()
    for item in (*checked_nodes, *checked_edges):
        if item["id"] in seen:
            raise CanvasError(f"the id {item['id']} is used twice")
        seen.add(item["id"])
    node_ids = {node["id"] for node in checked_nodes}
    for edge in checked_edges:
        missing = [end for end in (edge["fromNode"], edge["toNode"]) if end not in node_ids]
        if missing:
            raise CanvasError(f"edge {edge['id']} points at {', '.join(missing)}, which is not a node of this canvas")
    result = {**doc, "nodes": checked_nodes, "edges": checked_edges}
    # The two arrays lead, as Obsidian writes them; other top-level keys follow in their own order.
    result = {"nodes": checked_nodes, "edges": checked_edges, **{key: value for key, value in result.items() if key not in ("nodes", "edges")}}
    if len(write(result).encode()) > MAX_BYTES:
        raise CanvasError(f"the canvas exceeds {MAX_BYTES // 1_000_000} MB")
    return result


def parse(text: str) -> dict[str, Any]:
    """A ``.canvas`` file's text, checked. Obsidian writes an empty file for a new canvas."""
    if len(text.encode()) > MAX_BYTES:
        raise CanvasError(f"the file exceeds {MAX_BYTES // 1_000_000} MB")
    if not text.strip():
        return empty()
    try:
        loaded = json.loads(text)
    except ValueError as exc:
        raise CanvasError(f"not JSON: {exc.msg} at line {exc.lineno}") from None
    return check(loaded)


# -- writing ------------------------------------------------------------------------------------------

_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _plain(value: Any) -> Any:
    """Numbers as JavaScript prints them: a whole float without its ``.0``, and nothing a JSON
    reader would choke on."""
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value) if value.is_integer() else value
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def compact(value: Any) -> str:
    """``JSON.stringify(value)``: no spaces, characters as they are, a lone surrogate escaped (Python
    would otherwise write a string no encoder can save)."""
    text = json.dumps(_plain(value), ensure_ascii=False, separators=(",", ":"))
    return _LONE_SURROGATE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def _item(item: dict[str, Any]) -> str:
    if not any(isinstance(value, dict | list) for value in item.values()):
        return "\t\t" + compact(item)
    inner = ",\n".join(f"\t\t\t{compact(key)}:{compact(value)}" for key, value in item.items())
    return "\t\t{\n" + inner + "\n\t\t}"


def write(doc: dict[str, Any]) -> str:
    """The document in Obsidian's layout. No newline at the end: Obsidian writes none, and a file
    that gained one on every export would show a change nobody made."""
    parts = []
    for key, value in doc.items():
        if key in ("nodes", "edges") and isinstance(value, list):
            if not value:
                parts.append(f"\t{compact(key)}:[]")
            else:
                parts.append(f"\t{compact(key)}:[\n" + ",\n".join(_item(item) for item in value) + "\n\t]")
        else:
            parts.append(f"\t{compact(key)}:{compact(value)}")
    return "{\n" + ",\n".join(parts) + "\n}"


# -- reading for people ---------------------------------------------------------------------------------

def category(item: dict[str, Any]) -> str:
    """What a reader calls an item: a card, a group, a link, a file, or an edge."""
    if "fromNode" in item:
        return "edge"
    return {"text": "card", "group": "group", "link": "link", "file": "file"}.get(str(item.get("type")), "card")


def _items(doc: dict[str, Any]) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for item in (*doc.get("nodes", []), *doc.get("edges", [])):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            found[item["id"]] = (category(item), compact(item))
    return found


def changed_ids(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    """Ids added, removed or different between two documents; a change of drawing order counts for
    the items that moved in it."""
    old, new = _items(before), _items(after)
    ids = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    for kind in ("nodes", "edges"):
        old_order = [item.get("id") for item in before.get(kind, []) if item.get("id") in new]
        new_order = [item.get("id") for item in after.get(kind, []) if item.get("id") in old]
        if old_order != new_order:
            ids.update(str(a) for a, b in zip(old_order, new_order, strict=False) if a != b)
    return ids


def change_summary(before: dict[str, Any] | None, before_title: str | None, after: dict[str, Any], after_title: str) -> dict[str, Any]:
    """How a revision differs from the one listed before it, in counts a person reads:
    ``{"added": {"card": 2}, "removed": {"edge": 1}, "changed": 3, "renamed": false}``."""
    if before is None:
        return {"created": True}
    old, new = _items(before), _items(after)
    added: dict[str, int] = {}
    removed: dict[str, int] = {}
    for key, (kind, _) in new.items():
        if key not in old:
            added[kind] = added.get(kind, 0) + 1
    for key, (kind, _) in old.items():
        if key not in new:
            removed[kind] = removed.get(kind, 0) + 1
    changed = sum(1 for key, (_, content) in new.items() if key in old and old[key][1] != content)
    return {"added": added, "removed": removed, "changed": changed, "renamed": before_title is not None and before_title != after_title}


def counts(doc: dict[str, Any]) -> tuple[int, int]:
    return len(doc.get("nodes", [])), len(doc.get("edges", []))


# -- operations -----------------------------------------------------------------------------------------

def _place(items: list[dict[str, Any]], item: dict[str, Any], at: str | None) -> None:
    index = next((i for i, existing in enumerate(items) if existing.get("id") == item["id"]), None)
    if index is not None and at is None:
        items[index] = item
        return
    if index is not None:
        items.pop(index)
    if at == "back":
        items.insert(0, item)
    else:
        items.append(item)


def apply_ops(doc: dict[str, Any], ops: Iterable[Any]) -> tuple[dict[str, Any], set[str]]:
    """Apply ``put`` and ``delete`` operations to a copy of ``doc``; returns it checked, with the ids
    the operations touched (a deleted node's edges among them).

    ``{"op": "put", "kind": "node" | "edge", "item": {...}, "at": "front" | "back"}`` inserts or
    replaces the item with that id; ``at`` moves it to the top or the bottom of the drawing order.
    ``{"op": "delete", "id": ...}`` removes a node with every edge on it, or an edge. Deleting a group
    leaves what lies inside it: a group is a frame drawn around cards, not their owner.
    """
    nodes = [dict(node) for node in doc.get("nodes", [])]
    edges = [dict(edge) for edge in doc.get("edges", [])]
    touched: set[str] = set()
    for op in ops:
        if not isinstance(op, dict):
            raise CanvasError("every operation is an object")
        name = op.get("op")
        if name == "put":
            kind, item, at = op.get("kind"), op.get("item"), op.get("at")
            if kind not in ("node", "edge"):
                raise CanvasError("put needs kind node or edge")
            if at is not None and at not in ("front", "back"):
                raise CanvasError("at is front or back")
            checked = check_node(item) if kind == "node" else check_edge(item)
            other = edges if kind == "node" else nodes
            if any(existing.get("id") == checked["id"] for existing in other):
                raise CanvasError(f"the id {checked['id']} already names {'an edge' if kind == 'node' else 'a node'}")
            _place(nodes if kind == "node" else edges, checked, at)
            touched.add(checked["id"])
        elif name == "delete":
            target = op.get("id")
            if not isinstance(target, str):
                raise CanvasError("delete needs the id of a node or an edge")
            if any(node.get("id") == target for node in nodes):
                nodes = [node for node in nodes if node.get("id") != target]
                attached = {edge["id"] for edge in edges if target in (edge.get("fromNode"), edge.get("toNode"))}
                edges = [edge for edge in edges if edge.get("id") not in attached]
                touched.update(attached)
            elif any(edge.get("id") == target for edge in edges):
                edges = [edge for edge in edges if edge.get("id") != target]
            else:
                raise CanvasError(f"no node or edge {target} on this canvas")
            touched.add(target)
        else:
            raise CanvasError("an operation is put or delete")
    extra = {key: value for key, value in doc.items() if key not in ("nodes", "edges")}
    return check({"nodes": nodes, "edges": edges, **extra}), touched


def referenced(ops: Iterable[Any]) -> set[str]:
    """The node ids the edges an operation puts depend on: an edge put onto a node the other writer
    deleted is a conflict about that node, not a broken document."""
    ends: set[str] = set()
    for op in ops:
        if isinstance(op, dict) and op.get("op") == "put" and op.get("kind") == "edge" and isinstance(op.get("item"), dict):
            ends.update(str(op["item"].get(key)) for key in ("fromNode", "toNode") if op["item"].get(key))
    return ends


def tick(clock: dict[str, int], ids: Iterable[str], version: int, doc: dict[str, Any]) -> dict[str, int]:
    """The clock after ``ids`` changed at ``version``, with the oldest tombstones beyond
    :data:`MAX_TOMBSTONES` forgotten."""
    updated = dict(clock)
    for item_id in ids:
        updated[item_id] = version
    live = {item.get("id") for item in (*doc.get("nodes", []), *doc.get("edges", []))} | {TITLE_KEY}
    dead = sorted((key for key in updated if key not in live), key=lambda key: updated[key])
    for key in dead[: max(0, len(dead) - MAX_TOMBSTONES)]:
        del updated[key]
    return updated


def conflicts(clock: dict[str, int], base_version: int, ids: Iterable[str]) -> list[str]:
    """Which of ``ids`` changed after ``base_version``."""
    return sorted(item_id for item_id in set(ids) if clock.get(item_id, 0) > base_version)


__all__ = [
    "ENDS",
    "FILE_PREFIX",
    "MAX_BYTES",
    "NODE_TYPES",
    "PRESET_COLORS",
    "SIDES",
    "TITLE_KEY",
    "CanvasError",
    "Conflict",
    "apply_ops",
    "category",
    "change_summary",
    "changed_ids",
    "check",
    "check_edge",
    "check_node",
    "compact",
    "conflicts",
    "counts",
    "empty",
    "parse",
    "referenced",
    "tick",
    "write",
]
