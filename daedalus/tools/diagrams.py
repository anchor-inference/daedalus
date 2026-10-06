"""Agent tools over the operator's Excalidraw diagrams.

The tools speak in shapes, labels and connections and leave the Excalidraw bookkeeping (bound
labels, arrow bindings on both ends, versions) to ``daedalus.stores.diagram_scene``, because every
hand-built scene the model produced before got one of those wrong: text floating beside its box,
arrows that stayed put when the operator dragged a shape.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.stores import diagram_scene as scene_kit
from daedalus.stores.diagrams import DiagramStore, validate_scene
from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import error, ok, services_for

STALE = "the diagram changed elsewhere; read it again before editing"


def _store(context: ToolContext) -> DiagramStore:
    return DiagramStore(services_for(context).extra["manager"].db)


def _now() -> int:
    return int(time.time() * 1000)


def _link(diagram_id: str, title: str) -> str:
    return f"open /app/diagrams/{diagram_id} (link it as [{title}](/app/diagrams/{diagram_id}))"


def _number(spec: dict[str, Any], key: str, fallback: float) -> float:
    value = spec.get(key)
    if value is None:
        return float(fallback)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number") from None


def _put(elements: list[dict[str, Any]], element: dict[str, Any], after: str | None = None) -> None:
    """Replace the element with the same id where it stands, or insert it after ``after``, or append.

    Position matters: Excalidraw draws in array order, so a label must follow its shape."""
    for index, existing in enumerate(elements):
        if existing["id"] == element["id"]:
            elements[index] = element
            return
    if after is not None:
        for index, existing in enumerate(elements):
            if existing["id"] == after:
                elements.insert(index + 1, element)
                return
    elements.append(element)


def _labels_of(elements: list[dict[str, Any]], container_id: str) -> list[dict[str, Any]]:
    return [element for element in elements if element["type"] == "text" and element.get("containerId") == container_id]


def _keep_identity(element: dict[str, Any], old: dict[str, Any] | None) -> None:
    """Carry over what the operator may have set in the editor and the tool does not manage."""
    if old is None:
        return
    for key in ("seed", "boundElements", "groupIds", "frameId", "link", "locked", "angle", "opacity", "version", "versionNonce", "updated"):
        if key in old:
            element[key] = copy.deepcopy(old[key])


def _set_label(elements: list[dict[str, Any]], container: dict[str, Any], text: str, *, font_size: int, now: int) -> None:
    labels = _labels_of(elements, container["id"])
    elements[:] = scene_kit.remove_elements(elements, {label["id"] for label in labels})
    container = next(element for element in elements if element["id"] == container["id"])
    if text:
        _put(elements, scene_kit.bound_label(container, text, font_size=font_size, updated=now), after=container["id"])


def _apply_shape(elements: list[dict[str, Any]], spec: dict[str, Any], now: int) -> str:
    """Create or change a shape, free text or unbound line from one ``elements`` entry; returns its id."""
    by_id = {element["id"]: element for element in elements}
    element_id = str(spec.get("id") or uuid.uuid4().hex[:20])
    old = by_id.get(element_id)
    kind = str(spec.get("type") or (old or {}).get("type") or "rectangle")
    if kind not in scene_kit.KINDS:
        raise ValueError(f"element {element_id}: type must be one of {', '.join(sorted(scene_kit.KINDS))}")
    if old is not None and old["type"] == "text" and old.get("containerId") and kind == "text":
        # Editing a label directly: keep it bound and let its container decide where it sits.
        container = by_id.get(old["containerId"])
        if container is not None and "text" in spec:
            old["originalText"] = str(spec["text"])
            scene_kit.place_label(old, container)
        return element_id
    color = spec.get("color")
    base = old or {}
    if kind == "text":
        element = scene_kit.shape("text", element_id, _number(spec, "x", base.get("x", 0)), _number(spec, "y", base.get("y", 0)), text=str(spec.get("text", base.get("originalText", ""))), color=color, updated=now)
        if old is not None and color is None:
            element["strokeColor"] = old.get("strokeColor", element["strokeColor"])
        _keep_identity(element, old)
        _put(elements, element)
        return element_id
    if kind in scene_kit.LINEAR:
        points = spec.get("points", base.get("points"))
        element = scene_kit.shape(kind, element_id, _number(spec, "x", base.get("x", 0)), _number(spec, "y", base.get("y", 0)), _number(spec, "width", base.get("width", 160)), _number(spec, "height", base.get("height", 0)), color=color, dashed=bool(spec.get("dashed", base.get("strokeStyle") == "dashed")), points=points, updated=now)
        if old is not None and color is None:
            element["strokeColor"] = old.get("strokeColor", element["strokeColor"])
        _keep_identity(element, old)
        if old is not None:
            # Explicit points mean the agent drew this connector by hand; drop the bindings it had.
            if spec.get("points") is None:
                element["startBinding"], element["endBinding"] = old.get("startBinding"), old.get("endBinding")
            else:
                for bound_id in {(old.get("startBinding") or {}).get("elementId"), (old.get("endBinding") or {}).get("elementId")} - {None}:
                    if bound_id in by_id:
                        scene_kit.unbind(by_id[bound_id], {element_id})
        _put(elements, element)
        if "label" in spec or "text" in spec:
            _set_label(elements, element, str(spec.get("label", spec.get("text")) or ""), font_size=scene_kit.ARROW_FONT_SIZE, now=now)
        return element_id
    text = spec.get("text")
    if old is None and text and spec.get("width") is None and spec.get("height") is None:
        width, height = scene_kit.fit_shape_to_label(str(text), kind)
    else:
        width, height = _number(spec, "width", base.get("width", 160)), _number(spec, "height", base.get("height", 80))
    if width <= 0 or height <= 0:
        raise ValueError(f"element {element_id}: width and height must be positive")
    fill = spec.get("fill")
    # With no colour given, a fill still means the tint of the colour the shape already has.
    tint = color if color is not None else scene_kit.color_name(base.get("strokeColor"))
    element = scene_kit.shape(kind, element_id, _number(spec, "x", base.get("x", 0)), _number(spec, "y", base.get("y", 0)), width, height, color=tint, fill=fill, dashed=bool(spec.get("dashed", base.get("strokeStyle") == "dashed")), updated=now)
    if old is not None:
        if color is None:
            element["strokeColor"] = old.get("strokeColor", element["strokeColor"])
        if color is None and fill is None:
            element["backgroundColor"] = old.get("backgroundColor", element["backgroundColor"])
    _keep_identity(element, old)
    _put(elements, element)
    if text is not None:
        _set_label(elements, element, str(text), font_size=scene_kit.NODE_FONT_SIZE, now=now)
    return element_id


def _is_connector(elements: list[dict[str, Any]], spec: dict[str, Any]) -> bool:
    """An entry is routed between shapes when it names them, or when it touches an arrow that is
    already bound at both ends without giving points (a label or colour change keeps the binding)."""
    if spec.get("from") or spec.get("to"):
        return True
    old = next((element for element in elements if element["id"] == str(spec.get("id") or "")), None)
    return old is not None and old["type"] == "arrow" and bool(old.get("startBinding")) and bool(old.get("endBinding")) and spec.get("points") is None


def _apply_connector(elements: list[dict[str, Any]], spec: dict[str, Any], now: int, used: set[str]) -> str:
    """Create or re-route an arrow bound between two existing shapes named by ``from`` and ``to``."""
    by_id = {element["id"]: element for element in elements}
    old = by_id.get(str(spec.get("id") or ""))
    source = str(spec.get("from") or ((old or {}).get("startBinding") or {}).get("elementId") or "")
    target = str(spec.get("to") or ((old or {}).get("endBinding") or {}).get("elementId") or "")
    for end in (source, target):
        if end not in by_id or by_id[end]["type"] in scene_kit.LINEAR or by_id[end].get("containerId"):
            raise ValueError(f"an arrow end {end!r} is not a shape in this diagram")
    element_id = str(spec.get("id") or scene_kit.edge_id(source, target, used))
    old_label = next((label for label in _labels_of(elements, element_id)), None)
    label = spec["label"] if "label" in spec else spec.get("text", (old_label or {}).get("originalText"))
    color = spec.get("color")
    dashed = bool(spec.get("dashed", (old or {}).get("strokeStyle") == "dashed"))
    index = next((i for i, element in enumerate(elements) if element["id"] == element_id), None)
    if old is not None:
        elements[:] = scene_kit.remove_elements(elements, {element_id})
        by_id = {element["id"]: element for element in elements}
    made = scene_kit.bound_arrow(element_id, by_id[source], by_id[target], label=str(label) if label else None, color=color, dashed=dashed, updated=now)
    if old is not None:
        if color is None:
            made[0]["strokeColor"] = old.get("strokeColor", made[0]["strokeColor"])
        for key in ("seed", "groupIds", "frameId", "link", "locked", "opacity", "version", "versionNonce", "updated", "startArrowhead", "endArrowhead"):
            if key in old:
                made[0][key] = copy.deepcopy(old[key])
    if index is None:
        elements.extend(made)
    else:
        elements[index:index] = made
    return element_id


def _summary(found: dict[str, Any]) -> str:
    """The scene the way the model needs it to plan an edit: shapes with their label text, arrows as
    connections between shape ids. Bound labels are reported on their container, not on their own."""
    elements = [element for element in found["scene"].get("elements") or [] if not element.get("isDeleted")]
    by_id = {element["id"]: element for element in elements}
    labels: dict[str, str] = {}
    for element in elements:
        if element.get("type") == "text" and element.get("containerId") in by_id:
            labels[element["containerId"]] = str(element.get("originalText") or element.get("text") or "")
    lines = [f"diagram {found['id']} {json.dumps(found['title'], ensure_ascii=False)} version {found['version']}; open /app/diagrams/{found['id']}"]

    def where(element: dict[str, Any]) -> str:
        return f"{round(element.get('x', 0))},{round(element.get('y', 0))} {round(element.get('width', 0))}x{round(element.get('height', 0))}"

    def extras(element: dict[str, Any]) -> str:
        parts = []
        name = scene_kit.color_name(element.get("strokeColor"))
        if name and name != "default":
            parts.append(name)
        if element.get("strokeStyle") == "dashed":
            parts.append("dashed")
        if element["id"] in labels:
            parts.append(json.dumps(labels[element["id"]], ensure_ascii=False))
        return (" " + " ".join(parts)) if parts else ""

    shapes, arrows, texts, other = [], [], [], []
    for element in elements:
        kind = element.get("type")
        if kind == "text" and element.get("containerId") in by_id:
            continue
        if kind in scene_kit.CONTAINERS:
            shapes.append(f"  {element['id']} {kind} {where(element)}{extras(element)}")
        elif kind in scene_kit.LINEAR:
            points = element.get("points") or [[0, 0], [0, 0]]

            def end(binding: Any, point: list[float], element: dict[str, Any] = element) -> str:
                if isinstance(binding, dict) and binding.get("elementId") in by_id:
                    return str(binding["elementId"])
                return f"({round(element.get('x', 0) + point[0])},{round(element.get('y', 0) + point[1])})"

            arrows.append(f"  {element['id']} {kind} {end(element.get('startBinding'), points[0])} -> {end(element.get('endBinding'), points[-1])}{extras(element)}")
        elif kind == "text":
            texts.append(f"  {element['id']} text {where(element)} {json.dumps(str(element.get('originalText') or element.get('text') or ''), ensure_ascii=False)}")
        else:
            other.append(f"  {element['id']} {kind} {where(element)}")
    for heading, group in (("shapes", shapes), ("connectors", arrows), ("text", texts), ("other", other)):
        if group:
            lines.append(f"{heading}:")
            lines.extend(group)
    if len(lines) == 1:
        lines.append("empty")
    return "\n".join(lines)


async def _load(context: ToolContext, store: DiagramStore, diagram_id: str, version: int) -> tuple[dict[str, Any] | None, ToolResult | None]:
    found = await store.get(diagram_id)
    if found is None:
        return None, error(context, f"no diagram {diagram_id}; DiagramList shows the ids")
    if int(found["version"]) != int(version):
        return None, error(context, f"{STALE} (you passed version {version}, it is now {found['version']})")
    return found, None


async def _save(context: ToolContext, store: DiagramStore, found: dict[str, Any], elements: list[dict[str, Any]], title: str | None, version: int, now: int) -> dict[str, Any]:
    scene_kit.stamp(elements, {element["id"]: element for element in found["scene"].get("elements") or []}, now)
    scene = validate_scene({**found["scene"], "elements": elements})
    return await store.save(found["id"], title or found["title"], scene, version, source="agent")


def _store_failure(context: ToolContext, exc: Exception) -> ToolResult:
    if isinstance(exc, KeyError):
        return error(context, "the diagram no longer exists")
    if isinstance(exc, RuntimeError):
        return error(context, STALE)
    return error(context, str(exc))


@tool_group("diagrams")
@search_hint("diagram excalidraw canvas draw chart flowchart scheme схема диаграмма нарисовать список рисунки холст чертежи посмотреть")
@tool(name="DiagramList", description="List diagrams the operator can open in the native Excalidraw editor: id, title, version, last change.")
async def diagram_list(context: ToolContext) -> ToolResult:
    return ok(context, json.dumps(await _store(context).list(), ensure_ascii=False))


@tool_group("diagrams")
@search_hint("create excalidraw diagram draw new scheme flowchart layout создать нарисовать схему диаграмму холст рисунок чертеж блоки граф")
@tool(
    name="DiagramCreate",
    description=(
        "Create an editable Excalidraw diagram, optionally laid out at once. nodes: [{id, label, shape?: rectangle|ellipse|diamond, "
        "color?: blue|green|yellow|red|violet|grey|teal|orange}], edges: [{from, to, label?, color?, dashed?}], direction: right|down. "
        "Positions, sizes, labels and arrow bindings are computed for you. Link the operator as [Title](/app/diagrams/<id>)."
    ),
)
async def diagram_create(context: ToolContext, title: str, nodes: list[dict[str, Any]] | None = None, edges: list[dict[str, Any]] | None = None, direction: str = "right") -> ToolResult:
    scene = None
    try:
        if edges and not nodes:
            raise ValueError("edges need nodes to connect")
        if nodes:
            scene = validate_scene({"elements": scene_kit.layout(nodes, edges or [], direction, updated=_now()), "appState": {}, "files": {}})
        result = await _store(context).create(title, scene, source="agent")
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, f"created {result['id']} at /app/diagrams/{result['id']} (version {result['version']}); {_link(result['id'], result['title'])}", diagram_id=result["id"])


@tool_group("diagrams")
@search_hint("read inspect excalidraw diagram elements shapes схема диаграмма посмотреть элементы фигуры прочитать изучить холст")
@tool(name="DiagramRead", description="Read a diagram's version and a compact summary: shapes with position, size, colour and label; connectors as from -> to. Read before every edit.")
async def diagram_read(context: ToolContext, diagram_id: str) -> ToolResult:
    found = await _store(context).get(diagram_id)
    if found is None:
        return error(context, f"no diagram {diagram_id}; DiagramList shows the ids")
    return ok(context, _summary(found), diagram_id=diagram_id)


@tool_group("diagrams")
@search_hint("edit excalidraw diagram add shape arrow text change move delete схема диаграмма фигура стрелка текст добавить изменить удалить блок")
@tool(
    name="DiagramEdit",
    description=(
        "Change a diagram by element. Each entry: {id?, type: rectangle|diamond|ellipse|text|arrow|line, x, y, width?, height?, text?, "
        "color?, fill?: bool or colour, dashed?}. text on a shape becomes its centred label. For a connector give "
        "{type: arrow, from: <shape id>, to: <shape id>, label?} and it is routed and bound for you; points only for a free line. "
        "An existing id changes that element; moving a shape moves its label and its arrows. delete_ids removes elements, "
        "with their labels and the arrows bound to them. Pass the version from DiagramRead."
    ),
)
async def diagram_edit(context: ToolContext, diagram_id: str, version: int, elements: list[dict[str, Any]] | None = None, delete_ids: list[str] | None = None, title: str | None = None) -> ToolResult:
    store = _store(context)
    found, refusal = await _load(context, store, diagram_id, version)
    if refusal is not None:
        return refusal
    assert found is not None
    now = _now()
    scene = copy.deepcopy(found["scene"].get("elements") or [])
    try:
        missing = [item for item in delete_ids or [] if item not in {element["id"] for element in scene}]
        if missing:
            raise ValueError(f"no element {', '.join(missing)} in this diagram")
        scene = scene_kit.remove_elements(scene, set(delete_ids or []))
        specs = [spec for spec in elements or [] if isinstance(spec, dict)]
        if len(specs) != len(elements or []):
            raise ValueError("every entry in elements is an object")
        connectors = [spec for spec in specs if _is_connector(scene, spec)]
        changed: list[str] = []
        for spec in specs:
            if not any(spec is connector for connector in connectors):
                changed.append(_apply_shape(scene, spec, now))
        scene_kit.reroute(scene, set(changed))
        used = {element["id"] for element in scene}
        for spec in connectors:
            changed.append(_apply_connector(scene, spec, now, used))
        saved = await _save(context, store, found, scene, title, version, now)
    except (KeyError, RuntimeError, ValueError) as exc:
        return _store_failure(context, exc)
    parts = [f"saved diagram {diagram_id}, version {saved['version']}"]
    if changed:
        parts.append(f"changed {', '.join(changed)}")
    if delete_ids:
        parts.append(f"deleted {', '.join(delete_ids)}")
    return ok(context, "; ".join(parts + [_link(diagram_id, saved.get("title") or found["title"])]), diagram_id=diagram_id)


@tool_group("diagrams")
@search_hint("layout arrange flowchart graph nodes edges auto диаграмма схема расставить блоки граф автоматически связи стрелки раскладка")
@tool(
    name="DiagramLayout",
    description=(
        "Lay out a flow or structure into a diagram: nodes [{id, label, shape?: rectangle|ellipse|diamond, color?}] and edges "
        "[{from, to, label?, color?, dashed?}] become sized, labelled shapes in layers with bound arrows. direction: right|down. "
        "replace=true redraws the whole canvas; otherwise the group goes beside the existing content (or at origin_x/origin_y), "
        "a node id that already exists is redrawn in its new place, and an edge may also point at an existing shape id. "
        "Pass the version from DiagramRead."
    ),
)
async def diagram_layout(context: ToolContext, diagram_id: str, version: int, nodes: list[dict[str, Any]], edges: list[dict[str, Any]] | None = None, direction: str = "right", replace: bool = False, origin_x: float | None = None, origin_y: float | None = None) -> ToolResult:
    store = _store(context)
    found, refusal = await _load(context, store, diagram_id, version)
    if refusal is not None:
        return refusal
    assert found is not None
    now = _now()
    scene = [] if replace else copy.deepcopy(found["scene"].get("elements") or [])
    try:
        node_ids = {str(node.get("id")) for node in nodes if isinstance(node, dict)}
        existing = {element["id"]: element for element in scene}
        inner, outer = [], []
        for edge in edges or []:
            if not isinstance(edge, dict):
                raise ValueError("every edge is an object with from and to")
            ends = {str(edge.get("from")), str(edge.get("to"))}
            if ends <= node_ids:
                inner.append(edge)
            elif all(end in node_ids or (end in existing and existing[end]["type"] in scene_kit.CONTAINERS) for end in ends):
                outer.append(edge)
            else:
                raise ValueError(f"edge {edge.get('from')!r} -> {edge.get('to')!r} refers to an unknown node")
        replaced = node_ids & set(existing)
        stale_labels = {element["id"] for element in scene if element.get("containerId") in replaced}
        drawn = scene_kit.layout(nodes, inner, direction, updated=now)
        drawn_ids = {element["id"] for element in drawn}
        # An arrow or label from an earlier run of the same layout is redrawn, not duplicated.
        scene = scene_kit.remove_elements(scene, (drawn_ids - replaced) & set(existing))
        scene = [element for element in scene if element["id"] not in stale_labels]
        left_alone = [element for element in scene if element["id"] not in replaced]
        box = scene_kit.bounds(left_alone)
        if origin_x is None or origin_y is None:
            start_x, start_y = (0.0, 0.0) if box is None else ((box[2] + 120, box[1]) if direction == "right" else (box[0], box[3] + 120))
            origin_x = start_x if origin_x is None else origin_x
            origin_y = start_y if origin_y is None else origin_y
        drawn = scene_kit.layout(nodes, inner, direction, origin_x=float(origin_x), origin_y=float(origin_y), updated=now)
        current = {element["id"]: element for element in scene}
        for element in drawn:
            old = current.get(element["id"])
            if old is not None:
                # Arrows the operator drew to this node stay attached: keep their entries.
                kept = [item for item in old.get("boundElements") or [] if item.get("id") in current and item.get("type") != "text"]
                element["boundElements"] = [*(element.get("boundElements") or []), *(item for item in kept if item not in (element.get("boundElements") or []))] or None
                element["seed"] = old.get("seed", element["seed"])
            _put(scene, element, after=element.get("containerId"))
        used = {element["id"] for element in scene}
        by_id = {element["id"]: element for element in scene}
        for edge in outer:
            source, target = str(edge["from"]), str(edge["to"])
            scene.extend(scene_kit.bound_arrow(scene_kit.edge_id(source, target, used), by_id[source], by_id[target], label=edge.get("label") or None, color=edge.get("color"), dashed=bool(edge.get("dashed")), updated=now))
        scene_kit.reroute(scene, replaced)
        saved = await _save(context, store, found, scene, None, version, now)
    except (KeyError, RuntimeError, ValueError) as exc:
        return _store_failure(context, exc)
    return ok(context, f"laid out {len(nodes)} nodes and {len(edges or [])} edges in diagram {diagram_id}, version {saved['version']}; {_link(diagram_id, found['title'])}", diagram_id=diagram_id)


TOOLS = [diagram_list, diagram_create, diagram_read, diagram_edit, diagram_layout]

__all__ = ["TOOLS"]
