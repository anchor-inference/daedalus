"""Excalidraw elements built the way the editor itself would build them, without any I/O.

Everything here is deterministic: seeds and nonces come from the element id through ``uuid5`` and the
timestamp is a parameter, so the same request produces byte-identical JSON. That keeps a layout
reproducible in tests and keeps a re-run of the same agent call from churning the revision history.

The functions that take existing elements mutate them in place (a label binding has to be written on
both sides), and a caller that needs the original for comparison copies the scene first;
``stamp`` then bumps the version of exactly the elements that really changed.
"""

from __future__ import annotations

import math
import re
import uuid
from typing import Any

PALETTE: dict[str, tuple[str, str]] = {
    "default": ("#1e1e1e", "transparent"),
    "black": ("#1e1e1e", "transparent"),
    "blue": ("#1971c2", "#a5d8ff"),
    "green": ("#2f9e44", "#b2f2bb"),
    "yellow": ("#f08c00", "#ffec99"),
    "red": ("#e03131", "#ffc9c9"),
    "violet": ("#6741d9", "#d0bfff"),
    "grey": ("#495057", "#e9ecef"),
    "teal": ("#0c8599", "#99e9f2"),
    "orange": ("#e8590c", "#ffd8a8"),
}

CONTAINERS = frozenset({"rectangle", "ellipse", "diamond"})
LINEAR = frozenset({"arrow", "line"})
KINDS = CONTAINERS | LINEAR | {"text"}

LABEL_COLOR = "#1e1e1e"
FONT_FAMILY = 5  # Excalifont, the 0.18 default; unlike Virgil it ships Cyrillic subsets.
LINE_HEIGHT = 1.25
NODE_FONT_SIZE = 20
ARROW_FONT_SIZE = 16
# Excalidraw's BOUND_TEXT_PADDING: the editor keeps this much air between a container and its text.
PADDING = 5
# Our own breathing room inside that padding, so a fitted label does not touch the outline.
MARGIN_X = 12
MARGIN_Y = 8
# Excalidraw measures text with the real font; we cannot, so we estimate a glyph's advance. Excalifont
# averages a little over half its size for Latin and Cyrillic. We round the estimate up on purpose:
# the editor does not re-wrap a bound label when it loads a scene, so an under-estimate draws text
# across the border while an over-estimate only costs a few pixels of air.
GLYPH_WIDTH = 0.6
# Excalidraw's ARROW_LABEL_WIDTH_FRACTION and ARROW_LABEL_FONT_SIZE_TO_MIN_WIDTH_RATIO.
ARROW_LABEL_FRACTION = 0.7
ARROW_LABEL_MIN_RATIO = 11
ARROW_GAP = 8
MAX_NODES = 200
MAX_EDGES = 400
COORDINATE_LIMIT = 100_000
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _seed(key: str) -> int:
    return uuid.uuid5(uuid.NAMESPACE_OID, key).int & 0x7FFFFFFF


def _round(value: float) -> float:
    # Two decimals keep the JSON readable and stop float noise from making equal layouts differ.
    rounded = round(float(value), 2)
    return rounded + 0.0


def resolve_color(color: str | None) -> tuple[str, str]:
    """``(stroke, light fill)`` for a palette name or a raw ``#rrggbb``; a raw colour has no fill."""
    if color is None or color == "":
        return PALETTE["default"]
    name = str(color).strip()
    if name.lower() in PALETTE:
        return PALETTE[name.lower()]
    if _HEX.match(name):
        return (name.lower(), "transparent")
    raise ValueError(f"unknown colour {name!r}; use one of {', '.join(PALETTE)} or #rrggbb")


def resolve_fill(fill: bool | str | None, color: str | None, kind: str) -> str:
    """The background for a shape: ``None`` fills a closed shape with its colour's light tint,
    ``False`` leaves it transparent, a name or ``#rrggbb`` picks the tint explicitly."""
    if kind not in CONTAINERS or fill is False:
        return "transparent"
    if fill is None or fill is True:
        return resolve_color(color)[1]
    name = str(fill).strip()
    if _HEX.match(name):
        return name.lower()
    return resolve_color(name)[1]


def color_name(stroke: str | None) -> str | None:
    """The palette name for a stroke colour, or ``None`` when the colour is not one of ours."""
    for name, (value, _) in PALETTE.items():
        if name != "black" and stroke and value == stroke.lower():
            return name
    return None


def _base(element_id: str, kind: str, x: float, y: float, width: float, height: float, *, stroke: str, background: str, dashed: bool, version: int, updated: int) -> dict[str, Any]:
    for value in (x, y, width, height):
        if not math.isfinite(value) or abs(value) > COORDINATE_LIMIT:
            raise ValueError("shape coordinates are outside the canvas")
    return {
        "id": element_id, "type": kind, "x": _round(x), "y": _round(y), "width": _round(width), "height": _round(height), "angle": 0,
        "strokeColor": stroke, "backgroundColor": background, "fillStyle": "solid", "strokeWidth": 2,
        "strokeStyle": "dashed" if dashed else "solid", "roughness": 1, "opacity": 100, "groupIds": [], "frameId": None,
        "roundness": {"type": 3} if kind in ("rectangle", "diamond") else ({"type": 2} if kind in LINEAR else None),
        "seed": _seed(element_id), "version": version, "versionNonce": _seed(f"{element_id}:{version}"), "isDeleted": False,
        "boundElements": None, "updated": updated, "link": None, "locked": False,
    }


def shape(kind: str, element_id: str, x: float, y: float, width: float = 160, height: float = 80, *, color: str | None = None, fill: bool | str | None = None, dashed: bool = False, text: str | None = None, font_size: int = NODE_FONT_SIZE, points: list[list[float]] | None = None, version: int = 1, updated: int = 0) -> dict[str, Any]:
    """One Excalidraw 0.18 element. Text sizes itself from its content; ``width``/``height`` are ignored for it."""
    if kind not in KINDS:
        raise ValueError(f"shape type must be one of {', '.join(sorted(KINDS))}")
    stroke = resolve_color(color)[0]
    if kind == "text":
        value = str(text or "")
        lines = value.split("\n")
        width = max(1.0, max(_text_width(line, font_size) for line in lines))
        height = len(lines) * font_size * LINE_HEIGHT
        element = _base(element_id, kind, x, y, width, height, stroke=stroke, background="transparent", dashed=False, version=version, updated=updated)
        element.update(_text_fields(value, value, font_size, None, align="left", vertical="top"))
        return element
    element = _base(element_id, kind, x, y, width, height, stroke=stroke, background=resolve_fill(fill, color, kind), dashed=dashed, version=version, updated=updated)
    if kind in LINEAR:
        path = [[_round(px), _round(py)] for px, py in (points or [[0, 0], [width, height]])]
        if len(path) < 2:
            raise ValueError("a line or arrow needs at least two points")
        xs, ys = [p[0] for p in path], [p[1] for p in path]
        element.update({"width": _round(max(xs) - min(xs)), "height": _round(max(ys) - min(ys))})
        element.update({
            "points": path, "lastCommittedPoint": None, "startBinding": None, "endBinding": None,
            "startArrowhead": None, "endArrowhead": "arrow" if kind == "arrow" else None, "elbowed": False,
        })
    return element


def _text_fields(text: str, original: str, font_size: int, container_id: str | None, *, align: str, vertical: str) -> dict[str, Any]:
    return {
        "text": text, "originalText": original, "fontSize": font_size, "fontFamily": FONT_FAMILY,
        "textAlign": align, "verticalAlign": vertical, "containerId": container_id, "lineHeight": LINE_HEIGHT, "autoResize": True,
    }


def _text_width(line: str, font_size: int) -> float:
    return len(line) * font_size * GLYPH_WIDTH


def wrap(text: str, max_width: float, font_size: int) -> list[str]:
    """Greedy word wrap by estimated width; a word longer than a line is cut, as the editor does."""
    per_line = max(1, int(max_width // (font_size * GLYPH_WIDTH)))
    lines: list[str] = []
    for paragraph in str(text).split("\n"):
        current = ""
        for word in paragraph.split():
            while len(word) > per_line:
                if current:
                    lines.append(current)
                    current = ""
                lines.append(word[:per_line])
                word = word[per_line:]
            if not word:
                continue
            candidate = f"{current} {word}" if current else word
            if len(candidate) <= per_line:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def inner_box(kind: str, width: float, height: float) -> tuple[float, float]:
    """The box text may use inside a container, after Excalidraw's getBoundTextMaxWidth/MaxHeight:
    an ellipse's inscribed rectangle is its size over √2, a diamond's is half its size."""
    if kind == "ellipse":
        return width / math.sqrt(2) - 2 * PADDING, height / math.sqrt(2) - 2 * PADDING
    if kind == "diamond":
        return width / 2 - 2 * PADDING, height / 2 - 2 * PADDING
    return width - 2 * PADDING, height - 2 * PADDING


def outer_box(kind: str, inner_width: float, inner_height: float) -> tuple[float, float]:
    """The container size whose ``inner_box`` is the given size."""
    width, height = inner_width + 2 * PADDING, inner_height + 2 * PADDING
    if kind == "ellipse":
        return width * math.sqrt(2), height * math.sqrt(2)
    if kind == "diamond":
        return width * 2, height * 2
    return width, height


def fit_shape_to_label(text: str, kind: str = "rectangle", *, font_size: int = NODE_FONT_SIZE, min_width: float = 160, min_height: float = 72, max_width: float = 280) -> tuple[float, float]:
    """The smallest pleasant size for a node holding ``text``: it widens up to ``max_width`` (a bit
    more for an ellipse or a diamond, whose usable box is smaller), then wraps and grows taller."""
    stretch = {"ellipse": 1.2, "diamond": 1.4}.get(kind, 1.0)
    cap = inner_box(kind, max_width * stretch, 0)[0] - 2 * MARGIN_X
    lines = wrap(text, cap, font_size)
    text_width = max(_text_width(line, font_size) for line in lines)
    text_height = len(lines) * font_size * LINE_HEIGHT
    width, height = outer_box(kind, text_width + 2 * MARGIN_X, text_height + 2 * MARGIN_Y)
    return float(math.ceil(max(min_width, width))), float(math.ceil(max(min_height, height)))


def _bind(element: dict[str, Any], bound_id: str, kind: str) -> None:
    listed = [item for item in element.get("boundElements") or [] if item.get("id") != bound_id]
    element["boundElements"] = listed + [{"id": bound_id, "type": kind}]


def unbind(element: dict[str, Any], bound_ids: set[str]) -> None:
    if element.get("boundElements"):
        kept = [item for item in element["boundElements"] if item.get("id") not in bound_ids]
        element["boundElements"] = kept or None


def place_label(label: dict[str, Any], container: dict[str, Any]) -> None:
    """Re-wrap a bound label to its container and centre it there. A shape that is too short for the
    text grows downwards, as the editor grows a container while you type into it."""
    font_size = int(label.get("fontSize") or NODE_FONT_SIZE)
    original = str(label.get("originalText") if label.get("originalText") is not None else label.get("text") or "")
    if container["type"] in LINEAR:
        points = container.get("points") or [[0, 0], [0, 0]]
        start, end = points[0], points[-1]
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        max_width = max(ARROW_LABEL_FRACTION * length, font_size * ARROW_LABEL_MIN_RATIO)
        lines = wrap(original, max_width, font_size)
        width = max(1.0, max(_text_width(line, font_size) for line in lines))
        height = len(lines) * font_size * LINE_HEIGHT
        centre_x = container["x"] + (start[0] + end[0]) / 2
        centre_y = container["y"] + (start[1] + end[1]) / 2
    else:
        max_width, max_height = inner_box(container["type"], container["width"], container["height"])
        lines = wrap(original, max(1.0, max_width), font_size)
        width = max(1.0, max_width)
        height = len(lines) * font_size * LINE_HEIGHT
        if height > max_height:
            container["height"] = _round(math.ceil(outer_box(container["type"], max_width, height)[1]))
        centre_x = container["x"] + container["width"] / 2
        centre_y = container["y"] + container["height"] / 2
    label.update({
        "x": _round(centre_x - width / 2), "y": _round(centre_y - height / 2), "width": _round(width), "height": _round(height),
        "text": "\n".join(lines), "originalText": original, "containerId": container["id"],
        "textAlign": "center", "verticalAlign": "middle",
    })


def bound_label(container: dict[str, Any], text: str, *, font_size: int = NODE_FONT_SIZE, updated: int = 0) -> dict[str, Any]:
    """A text element bound to ``container`` (a shape or an arrow) and listed in its ``boundElements``.

    A bare ``text`` field on a rectangle is not drawn by Excalidraw, and a free text element placed on
    top of a shape stays behind when the shape moves; only a bound label behaves like part of the box."""
    label_id = f"{container['id']}-label"
    label = _base(label_id, "text", container["x"], container["y"], 1, 1, stroke=LABEL_COLOR, background="transparent", dashed=False, version=1, updated=updated)
    label.update(_text_fields(str(text), str(text), font_size, container["id"], align="center", vertical="middle"))
    place_label(label, container)
    _bind(container, label_id, "text")
    return label


def _centre(element: dict[str, Any]) -> tuple[float, float]:
    return element["x"] + element["width"] / 2, element["y"] + element["height"] / 2


def _boundary_distance(element: dict[str, Any], ux: float, uy: float) -> float:
    """How far from the centre the outline lies in the unit direction ``(ux, uy)``."""
    half_width, half_height = max(element["width"] / 2, 0.5), max(element["height"] / 2, 0.5)
    if element["type"] == "ellipse":
        return 1 / math.sqrt((ux / half_width) ** 2 + (uy / half_height) ** 2)
    if element["type"] == "diamond":
        return 1 / (abs(ux) / half_width + abs(uy) / half_height)
    return min(half_width / abs(ux) if ux else math.inf, half_height / abs(uy) if uy else math.inf)


def route_arrow(arrow: dict[str, Any], start: dict[str, Any] | None, end: dict[str, Any] | None, *, gap: float = ARROW_GAP) -> None:
    """Point a straight arrow from the outline of ``start`` to the outline of ``end``.

    Either end may be ``None``, and that end keeps its current absolute position. The line runs
    centre to centre with focus 0, which is what Excalidraw itself computes for these bindings, so
    the editor keeps the same route when the operator later drags one of the shapes."""
    points = arrow.get("points") or [[0, 0], [0, 0]]
    if start is not None:
        from_x, from_y = _centre(start)
    else:
        from_x, from_y = arrow["x"] + points[0][0], arrow["y"] + points[0][1]
    if end is not None:
        to_x, to_y = _centre(end)
    else:
        to_x, to_y = arrow["x"] + points[-1][0], arrow["y"] + points[-1][1]
    distance = math.hypot(to_x - from_x, to_y - from_y)
    ux, uy = ((to_x - from_x) / distance, (to_y - from_y) / distance) if distance else (1.0, 0.0)
    trim_start = _boundary_distance(start, ux, uy) + gap if start is not None else 0.0
    trim_end = _boundary_distance(end, ux, uy) + gap if end is not None else 0.0
    if trim_start + trim_end >= distance:
        # Overlapping shapes: there is no outside path, so draw a short stub instead of a reversed arrow.
        trim_start = trim_end = max(0.0, (distance - 1) / 2)
    sx, sy = from_x + ux * trim_start, from_y + uy * trim_start
    ex, ey = to_x - ux * trim_end, to_y - uy * trim_end
    arrow.update({
        "x": _round(sx), "y": _round(sy), "points": [[0.0, 0.0], [_round(ex - sx), _round(ey - sy)]],
        "width": _round(abs(ex - sx)), "height": _round(abs(ey - sy)),
        "startBinding": {"elementId": start["id"], "focus": 0, "gap": gap, "fixedPoint": None} if start is not None else None,
        "endBinding": {"elementId": end["id"], "focus": 0, "gap": gap, "fixedPoint": None} if end is not None else None,
    })


def bound_arrow(arrow_id: str, start_shape: dict[str, Any], end_shape: dict[str, Any], *, label: str | None = None, color: str | None = None, dashed: bool = False, updated: int = 0) -> list[dict[str, Any]]:
    """An arrow bound at both ends, and its label when given. Both shapes gain the arrow in their
    ``boundElements``; without that back-reference the editor leaves the arrow behind on a drag."""
    if start_shape["id"] == end_shape["id"]:
        raise ValueError("an arrow cannot connect a shape to itself")
    arrow = shape("arrow", arrow_id, 0, 0, 0, 0, color=color, dashed=dashed, updated=updated)
    route_arrow(arrow, start_shape, end_shape)
    _bind(start_shape, arrow_id, "arrow")
    _bind(end_shape, arrow_id, "arrow")
    result = [arrow]
    if label:
        result.append(bound_label(arrow, label, font_size=ARROW_FONT_SIZE, updated=updated))
    return result


def remove_elements(elements: list[dict[str, Any]], ids: set[str]) -> list[dict[str, Any]]:
    """The scene without ``ids``, without whatever only made sense attached to them (their labels,
    arrows bound to a removed shape, those arrows' labels) and without references to any of it."""
    removed = set(ids)
    while True:
        more = {
            element["id"] for element in elements if element["id"] not in removed and (
                element.get("containerId") in removed
                or (element.get("startBinding") or {}).get("elementId") in removed
                or (element.get("endBinding") or {}).get("elementId") in removed
            )
        }
        if not more:
            break
        removed |= more
    kept = [element for element in elements if element["id"] not in removed]
    for element in kept:
        unbind(element, removed)
    return kept


def reroute(elements: list[dict[str, Any]], shape_ids: set[str]) -> None:
    """Re-place the labels of ``shape_ids`` and re-route every connector bound to them, so arrows
    follow a shape that moved or changed size."""
    by_id = {element["id"]: element for element in elements}
    for element in elements:
        if element["type"] == "text" and element.get("containerId") in shape_ids and element["containerId"] in by_id:
            place_label(element, by_id[element["containerId"]])
    for element in elements:
        if element["type"] not in LINEAR:
            continue
        start_id = (element.get("startBinding") or {}).get("elementId")
        end_id = (element.get("endBinding") or {}).get("elementId")
        if start_id not in shape_ids and end_id not in shape_ids:
            continue
        route_arrow(element, by_id.get(start_id), by_id.get(end_id), gap=ARROW_GAP)
        for bound in by_id.get(start_id), by_id.get(end_id):
            if bound is not None:
                _bind(bound, element["id"], element["type"])
        for item in element.get("boundElements") or []:
            if item.get("type") == "text" and item["id"] in by_id:
                place_label(by_id[item["id"]], element)


_BOOKKEEPING = ("version", "versionNonce", "updated")


def stamp(elements: list[dict[str, Any]], previous: dict[str, dict[str, Any]], updated: int) -> None:
    """Bump ``version`` on every element that differs from ``previous`` (keyed by id), and only those.

    The editor reconciles concurrent copies by version, so an element changed here without a bump
    would lose to the stale copy an open tab still holds; bumping unchanged elements would instead
    overwrite edits the operator is making to them at that moment."""
    for element in elements:
        before = previous.get(element["id"])
        if before is None:
            continue
        if {k: v for k, v in element.items() if k not in _BOOKKEEPING} == {k: v for k, v in before.items() if k not in _BOOKKEEPING}:
            element.update({key: before[key] for key in _BOOKKEEPING if key in before})
            continue
        version = int(before.get("version") or 0) + 1
        element.update({"version": version, "versionNonce": _seed(f"{element['id']}:{version}"), "updated": updated})


def bounds(elements: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    """``(min_x, min_y, max_x, max_y)`` over the visible elements, or ``None`` for an empty scene."""
    boxes = []
    for element in elements:
        if element.get("isDeleted"):
            continue
        if element.get("type") in LINEAR and element.get("points"):
            xs = [element["x"] + point[0] for point in element["points"]]
            ys = [element["y"] + point[1] for point in element["points"]]
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
        elif all(isinstance(element.get(key), (int, float)) for key in ("x", "y", "width", "height")):
            boxes.append((element["x"], element["y"], element["x"] + element["width"], element["y"] + element["height"]))
    if not boxes:
        return None
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def _validate_graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    if not nodes:
        raise ValueError("a layout needs at least one node")
    if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
        raise ValueError(f"a layout takes at most {MAX_NODES} nodes and {MAX_EDGES} edges; split the diagram")
    seen: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or not str(node.get("id") or "").strip():
            raise ValueError("every node needs an id")
        node_id = str(node["id"])
        if node_id in seen:
            raise ValueError(f"node id {node_id!r} appears twice")
        seen.add(node_id)
        kind = node.get("shape") or "rectangle"
        if kind not in CONTAINERS:
            raise ValueError(f"node {node_id!r}: shape must be rectangle, ellipse or diamond")
        resolve_color(node.get("color"))
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("every edge is an object with from and to")
        source, target = str(edge.get("from") or ""), str(edge.get("to") or "")
        if source not in seen or target not in seen:
            raise ValueError(f"edge {source!r} -> {target!r} refers to an unknown node")
        if source == target:
            raise ValueError(f"edge {source!r} -> {target!r} connects a node to itself")
        resolve_color(edge.get("color"))


def _layers(order: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """Longest-path layers over the graph with its back edges ignored.

    A depth-first search in input order marks an edge into a node still on the stack as a back edge;
    leaving those out turns any graph into a DAG, and the choice depends only on the input order."""
    outgoing: dict[str, list[str]] = {node: [] for node in order}
    for source, target in edges:
        outgoing[source].append(target)
    state: dict[str, int] = {}
    back: set[tuple[str, str]] = set()
    for root in order:
        if root in state:
            continue
        state[root] = 1
        stack = [(root, iter(outgoing[root]))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                state[node] = 2
                stack.pop()
            elif state.get(child) == 1:
                back.add((node, child))
            elif child not in state:
                state[child] = 1
                stack.append((child, iter(outgoing[child])))
    forward = [edge for edge in edges if edge not in back]
    incoming = {node: 0 for node in order}
    for _, target in forward:
        incoming[target] += 1
    layer = {node: 0 for node in order}
    ready = [node for node in order if incoming[node] == 0]
    position = {node: index for index, node in enumerate(order)}
    while ready:
        ready.sort(key=position.__getitem__)
        node = ready.pop(0)
        for source, target in forward:
            if source == node:
                layer[target] = max(layer[target], layer[node] + 1)
                incoming[target] -= 1
                if incoming[target] == 0:
                    ready.append(target)
    return layer


def _order(order: list[str], layer: dict[str, int], edges: list[tuple[str, str]], sweeps: int = 2) -> list[list[str]]:
    """Nodes per layer, ordered by a few barycentre sweeps to cut crossings; ties keep input order."""
    depth = max(layer.values()) + 1
    rows: list[list[str]] = [[] for _ in range(depth)]
    for node in order:
        rows[layer[node]].append(node)
    neighbours: dict[str, list[str]] = {node: [] for node in order}
    for source, target in edges:
        neighbours[source].append(target)
        neighbours[target].append(source)

    def place() -> dict[str, float]:
        # Centred indices, so layers of different sizes line up around one axis.
        return {node: index - (len(row) - 1) / 2 for row in rows for index, node in enumerate(row)}

    for _ in range(sweeps):
        for direction in (range(1, depth), range(depth - 2, -1, -1)):
            for index in direction:
                positions = place()
                compare = (lambda other, i=index: layer[other] < i) if direction.step == 1 else (lambda other, i=index: layer[other] > i)
                row = rows[index]
                current = {node: position for position, node in enumerate(row)}

                def key(node: str, compare: Any = compare, current: dict[str, int] = current, positions: dict[str, float] = positions) -> tuple[float, int]:
                    related = [positions[other] for other in neighbours[node] if compare(other)]
                    return (sum(related) / len(related) if related else positions[node], current[node])

                rows[index] = sorted(row, key=key)
    return rows


def layout(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], direction: str = "right", *, origin_x: float = 0, origin_y: float = 0, layer_gap: float = 100, sibling_gap: float = 48, updated: int = 0) -> list[dict[str, Any]]:
    """A layered drawing of ``nodes`` and ``edges``: shapes with bound labels, then bound arrows.

    ``nodes`` are ``{id, label, shape?, color?}`` and ``edges`` ``{from, to, label?, color?, dashed?}``.
    Layers run along ``direction`` ("right" or "down") from sources to sinks; the top-left of the
    drawing is ``(origin_x, origin_y)``. The same input always yields the same elements."""
    if direction not in ("right", "down"):
        raise ValueError("direction must be right or down")
    _validate_graph(nodes, edges)
    order = [str(node["id"]) for node in nodes]
    pairs = [(str(edge["from"]), str(edge["to"])) for edge in edges]
    layer = _layers(order, pairs)
    rows = _order(order, layer, pairs)
    by_id = {str(node["id"]): node for node in nodes}
    sizes = {node_id: fit_shape_to_label(str(by_id[node_id].get("label") or node_id), by_id[node_id].get("shape") or "rectangle") for node_id in order}
    horizontal = direction == "right"
    label_room = 0.0
    for edge in edges:
        if edge.get("label"):
            lines = wrap(str(edge["label"]), ARROW_FONT_SIZE * ARROW_LABEL_MIN_RATIO, ARROW_FONT_SIZE)
            needed = max(_text_width(line, ARROW_FONT_SIZE) for line in lines) if horizontal else len(lines) * ARROW_FONT_SIZE * LINE_HEIGHT
            label_room = max(label_room, needed + 48)
    gap = max(layer_gap, label_room)
    # Along the flow each layer is as deep as its deepest node; across it, nodes stack with a sibling gap.
    along = [max(sizes[node][0 if horizontal else 1] for node in row) for row in rows]
    across = [sum(sizes[node][1 if horizontal else 0] for node in row) + sibling_gap * (len(row) - 1) for row in rows]
    widest = max(across)
    positions: dict[str, tuple[float, float]] = {}
    offset = 0.0
    for index, row in enumerate(rows):
        cursor = (widest - across[index]) / 2
        for node in row:
            width, height = sizes[node]
            depth, breadth = (width, height) if horizontal else (height, width)
            primary = offset + (along[index] - depth) / 2
            positions[node] = (origin_x + primary, origin_y + cursor) if horizontal else (origin_x + cursor, origin_y + primary)
            cursor += breadth + sibling_gap
        offset += along[index] + gap
    elements: list[dict[str, Any]] = []
    shapes: dict[str, dict[str, Any]] = {}
    for node_id in order:
        node = by_id[node_id]
        x, y = positions[node_id]
        width, height = sizes[node_id]
        element = shape(node.get("shape") or "rectangle", node_id, x, y, width, height, color=node.get("color"), fill=node.get("fill"), updated=updated)
        shapes[node_id] = element
        elements.append(element)
        elements.append(bound_label(element, str(node.get("label") or node_id), updated=updated))
    used: set[str] = set(order)
    for edge in edges:
        source, target = str(edge["from"]), str(edge["to"])
        arrow_id = edge_id(source, target, used)
        elements.extend(bound_arrow(arrow_id, shapes[source], shapes[target], label=edge.get("label") or None, color=edge.get("color"), dashed=bool(edge.get("dashed")), updated=updated))
    return elements


def edge_id(source: str, target: str, used: set[str]) -> str:
    """A readable arrow id that is stable for the same pair, numbered when a pair repeats."""
    base = f"{source}-to-{target}"
    candidate, number = base, 2
    while candidate in used or f"{candidate}-label" in used:
        candidate, number = f"{base}-{number}", number + 1
    used.update({candidate, f"{candidate}-label"})
    return candidate


__all__ = [
    "CONTAINERS", "KINDS", "LINEAR", "PALETTE", "bound_arrow", "bound_label", "bounds", "color_name", "edge_id",
    "fit_shape_to_label", "inner_box", "layout", "outer_box", "place_label", "remove_elements", "reroute",
    "resolve_color", "resolve_fill", "route_arrow", "shape", "stamp", "unbind", "wrap",
]
