"""Turning an Excalidraw scene into a JSON Canvas document, for the migration that retired diagrams.

JSON Canvas has cards, groups and edges between cards, and nothing else, so the conversion keeps
what a reader of a diagram reads (every box with its words, every connection with its label, the
colours and the places) and loses what the format cannot say:

- an ellipse or a diamond becomes a rectangular card; ``daedalus.shape`` remembers which it was;
- a line, and an arrow that ends nowhere near a shape, has no node to run between and is dropped;
- an arrow's bend points go, because an edge is drawn as one curve between two sides;
- freehand strokes and pictures have no card to become (none of the diagrams this ran on had any).

It does better than a straight mapping in three places. An arrow the operator drew by hand ends a
few pixels short of the boxes it joins and was never bound to them; when both of its ends lie close
to a shape's border it becomes a real edge between those shapes. A free text sitting inside a shape
that has no label of its own becomes that shape's words, as the reader saw it. And a text's font size
survives as a Markdown heading, so a title is still a title.

The counts of what was lost are returned with the document, so the migration can say so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

SHAPES = ("rectangle", "ellipse", "diamond")
CONNECTORS = ("arrow", "line")
SNAP = 48.0
"""How far from a shape's border an unbound arrow's end may stop and still count as touching it."""
DEFAULT_INK = {"#1e1e1e", "#000000", "#000", "transparent", ""}
"""Excalidraw's default stroke: no colour of its own, so the card takes the board's."""


@dataclass
class Converted:
    doc: dict[str, Any]
    shapes: dict[str, int] = field(default_factory=dict)
    """Former ellipses and diamonds, now cards, by kind."""
    snapped: int = 0
    """Unbound arrows that became edges because both ends touched a shape."""
    dropped: dict[str, int] = field(default_factory=dict)
    """Elements with nothing to become, by kind."""
    bends: int = 0
    """Edges whose arrow had bend points."""


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _box(element: dict[str, Any]) -> tuple[float, float, float, float]:
    x, y, w, h = (_number(element.get(key)) for key in ("x", "y", "width", "height"))
    return min(x, x + w), min(y, y + h), abs(w), abs(h)


def _colour(element: dict[str, Any]) -> str | None:
    value = str(element.get("strokeColor") or "").lower()
    if value in DEFAULT_INK or not value.startswith("#") or len(value) not in (4, 7):
        return None
    return value


def _words(element: dict[str, Any]) -> str:
    return str(element.get("originalText") or element.get("text") or "")


def _heading(text: str, font_size: float) -> str:
    """A big text keeps its weight as a Markdown heading on its first line."""
    if not text.strip():
        return text
    level = "# " if font_size >= 32 else "## " if font_size >= 24 else ""
    if not level:
        return text
    first, _, rest = text.partition("\n")
    return f"{level}{first}" + (f"\n{rest}" if rest else "")


def _distance_to_border(point: tuple[float, float], box: tuple[float, float, float, float]) -> float:
    x, y, w, h = box
    px, py = point
    outside_x = max(x - px, 0.0, px - (x + w))
    outside_y = max(y - py, 0.0, py - (y + h))
    if outside_x or outside_y:
        return math.hypot(outside_x, outside_y)
    return min(px - x, x + w - px, py - y, y + h - py)


def _side(point: tuple[float, float], box: tuple[float, float, float, float]) -> str:
    """The side of ``box`` nearest to ``point``: where the arrow met the shape."""
    x, y, w, h = box
    px, py = point
    distances = {"left": abs(px - x), "right": abs(px - (x + w)), "top": abs(py - y), "bottom": abs(py - (y + h))}
    return min(distances, key=lambda side: distances[side])


def _ends(element: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    points = [point for point in element.get("points") or [] if isinstance(point, list | tuple) and len(point) >= 2]
    if len(points) < 2:
        return None
    x, y = _number(element.get("x")), _number(element.get("y"))
    return (x + _number(points[0][0]), y + _number(points[0][1])), (x + _number(points[-1][0]), y + _number(points[-1][1]))


def _contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[0] + inner[2] <= outer[0] + outer[2] and inner[1] + inner[3] <= outer[1] + outer[3]


def convert(scene: dict[str, Any]) -> Converted:
    elements = [item for item in scene.get("elements") or [] if isinstance(item, dict) and not item.get("isDeleted") and isinstance(item.get("id"), str)]
    by_id = {item["id"]: item for item in elements}
    labels: dict[str, list[dict[str, Any]]] = {}
    for item in elements:
        if item.get("type") == "text" and item.get("containerId") in by_id:
            labels.setdefault(str(item["containerId"]), []).append(item)
    result = Converted(doc={"nodes": [], "edges": []})
    nodes: list[dict[str, Any]] = []
    node_boxes: dict[str, tuple[float, float, float, float]] = {}
    shape_text: dict[str, str] = {}
    free_texts: list[dict[str, Any]] = []

    for item in elements:
        kind = item.get("type")
        if kind in SHAPES:
            words = "\n\n".join(_words(label) for label in labels.get(item["id"], []) if _words(label).strip())
            shape_text[item["id"]] = words
            if kind != "rectangle":
                result.shapes[kind] = result.shapes.get(kind, 0) + 1
        elif kind == "text" and item.get("containerId") not in by_id:
            free_texts.append(item)
        elif kind == "frame" or kind == "magicframe":
            pass
        elif kind not in CONNECTORS and kind != "text":
            result.dropped[str(kind)] = result.dropped.get(str(kind), 0) + 1

    # A free text inside an unlabelled shape is that shape's label, drawn by hand instead of bound.
    absorbed: set[str] = set()
    shapes = [item for item in elements if item.get("type") in SHAPES]
    for text in free_texts:
        box = _box(text)
        holders = [shape for shape in shapes if not shape_text[shape["id"]] and _contains(_box(shape), box)]
        if holders:
            holder = min(holders, key=lambda shape: _box(shape)[2] * _box(shape)[3])
            shape_text[holder["id"]] = _words(text)
            absorbed.add(text["id"])

    for item in elements:
        kind = item.get("type")
        if kind in ("frame", "magicframe"):
            x, y, w, h = _box(item)
            node = {"id": item["id"], "type": "group", "x": round(x), "y": round(y), "width": max(1, round(w)), "height": max(1, round(h))}
            if item.get("name"):
                node["label"] = str(item["name"])
        elif kind in SHAPES:
            x, y, w, h = _box(item)
            node = {"id": item["id"], "type": "text", "text": shape_text[item["id"]], "x": round(x), "y": round(y), "width": max(1, round(w)), "height": max(1, round(h))}
            if kind != "rectangle":
                node["daedalus"] = {"shape": kind}
        elif kind == "text" and item.get("containerId") not in by_id and item["id"] not in absorbed:
            x, y, w, h = _box(item)
            # A label floating free reads as words on the board, not as a box: the card is drawn plain.
            node = {
                "id": item["id"], "type": "text", "text": _heading(_words(item), _number(item.get("fontSize")) or 20),
                "x": round(x) - 8, "y": round(y) - 8, "width": max(40, round(w) + 16), "height": max(32, round(h) + 16),
                "daedalus": {"shape": "text"},
            }
        else:
            continue
        colour = _colour(item)
        if colour:
            node["color"] = colour
        nodes.append(node)
        node_boxes[item["id"]] = _box(item)

    # Groups first: they are drawn under everything, as Obsidian draws them.
    nodes.sort(key=lambda node: node["type"] != "group")
    edges: list[dict[str, Any]] = []
    for item in elements:
        kind = item.get("type")
        if kind not in CONNECTORS:
            continue
        ends = _ends(item)
        start = (item.get("startBinding") or {}).get("elementId")
        finish = (item.get("endBinding") or {}).get("elementId")
        bound = kind == "arrow" and start in node_boxes and finish in node_boxes
        if not bound and kind == "arrow" and ends is not None:
            near = [min(node_boxes, key=lambda key, point=point: _distance_to_border(point, node_boxes[key])) if node_boxes else None for point in ends]
            if all(key is not None and _distance_to_border(point, node_boxes[key]) <= SNAP for key, point in zip(near, ends, strict=True)) and near[0] != near[1]:
                start, finish, bound = near[0], near[1], True
                result.snapped += 1
        if not bound or ends is None or start == finish:
            result.dropped[str(kind)] = result.dropped.get(str(kind), 0) + 1
            continue
        edge: dict[str, Any] = {
            "id": item["id"],
            "fromNode": str(start), "fromSide": _side(ends[0], node_boxes[str(start)]),
            "toNode": str(finish), "toSide": _side(ends[1], node_boxes[str(finish)]),
        }
        if item.get("startArrowhead"):
            edge["fromEnd"] = "arrow"
        if not item.get("endArrowhead"):
            edge["toEnd"] = "none"
        colour = _colour(item)
        if colour:
            edge["color"] = colour
        label = " ".join(_words(text) for text in labels.get(item["id"], []) if _words(text).strip())
        if label:
            edge["label"] = label
        if len(item.get("points") or []) > 2:
            result.bends += 1
        edges.append(edge)
    result.doc = {"nodes": nodes, "edges": edges}
    return result


def bounds(doc: dict[str, Any]) -> tuple[int, int, int, int]:
    """Left, top, right, bottom of everything on a document; a blank one is a point at the origin."""
    nodes = doc.get("nodes") or []
    if not nodes:
        return 0, 0, 0, 0
    return (
        min(node["x"] for node in nodes), min(node["y"] for node in nodes),
        max(node["x"] + node["width"] for node in nodes), max(node["y"] + node["height"] for node in nodes),
    )


__all__ = ["Converted", "bounds", "convert"]
