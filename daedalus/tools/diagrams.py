"""Build Excalidraw scenes with simple shapes while preserving the editor's version lock."""

from __future__ import annotations

import json
import secrets
import time
import uuid
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.stores.diagrams import DiagramStore, shape_label
from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import error, ok, services_for


def _store(context: ToolContext) -> DiagramStore:
    return DiagramStore(services_for(context).extra["manager"].db)


def _shape(spec: dict[str, Any], old: dict[str, Any] | None = None) -> dict[str, Any]:
    kind = str(spec.get("type") or (old or {}).get("type") or "rectangle")
    if kind not in {"rectangle", "diamond", "ellipse", "arrow", "line", "text"}:
        raise ValueError("agent shapes are rectangle, diamond, ellipse, arrow, line or text")
    x = float(spec.get("x", (old or {}).get("x", 0)))
    y = float(spec.get("y", (old or {}).get("y", 0)))
    width = float(spec.get("width", (old or {}).get("width", 160)))
    height = float(spec.get("height", (old or {}).get("height", 80)))
    if not all(-100000 <= n <= 100000 for n in (x, y, width, height)):
        raise ValueError("shape coordinates are outside the canvas")
    element = {
        "id": str(spec.get("id") or (old or {}).get("id") or uuid.uuid4().hex[:20]), "type": kind,
        "x": x, "y": y, "width": width, "height": height, "angle": 0,
        "strokeColor": spec.get("strokeColor", (old or {}).get("strokeColor", "#1e1e1e")),
        "backgroundColor": spec.get("backgroundColor", (old or {}).get("backgroundColor", "transparent")),
        "fillStyle": "solid", "strokeWidth": 2, "strokeStyle": "solid", "roughness": 1, "opacity": 100,
        "groupIds": [], "frameId": None, "roundness": {"type": 3} if kind in ("rectangle", "diamond") else None,
        "seed": (old or {}).get("seed", secrets.randbelow(2**31)), "version": int((old or {}).get("version", 0)) + 1,
        "versionNonce": secrets.randbelow(2**31), "isDeleted": False, "boundElements": (old or {}).get("boundElements"),
        "updated": int(time.time() * 1000), "link": None, "locked": False,
    }
    if kind == "text":
        value = str(spec.get("text", (old or {}).get("text", "")))
        element.update({"text": value, "originalText": value, "fontSize": 20, "fontFamily": 1, "textAlign": "left", "verticalAlign": "top", "baseline": 18, "containerId": None, "lineHeight": 1.25, "autoResize": True})
    if kind in ("arrow", "line"):
        element.update({"points": spec.get("points", (old or {}).get("points", [[0, 0], [width, height]])), "startBinding": None, "endBinding": None, "startArrowhead": None, "endArrowhead": "arrow" if kind == "arrow" else None, "elbowed": False})
    return element


@tool_group("diagrams")
@search_hint("diagram excalidraw canvas draw chart flowchart scheme схема диаграмма нарисовать список рисунки холст чертежи посмотреть")
@tool(name="DiagramList", description="List diagrams the operator can open in the native Excalidraw editor.")
async def diagram_list(context: ToolContext) -> ToolResult:
    return ok(context, json.dumps(await _store(context).list(), ensure_ascii=False))


@tool_group("diagrams")
@search_hint("create excalidraw diagram draw new scheme flowchart создать нарисовать схему диаграмму холст рисунок чертеж блоки")
@tool(name="DiagramCreate", description="Create an editable Excalidraw diagram. Follow with DiagramEdit to add rectangles, text and arrows. The returned address opens inside the app.")
async def diagram_create(context: ToolContext, title: str) -> ToolResult:
    try:
        result = await _store(context).create(title)
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, f"created {result['id']} at /app/diagrams/{result['id']} (version {result['version']})", diagram_id=result["id"])


@tool_group("diagrams")
@search_hint("read inspect excalidraw diagram elements shapes схема диаграмма посмотреть элементы фигуры прочитать изучить холст")
@tool(name="DiagramRead", description="Read a diagram's version and element summary before editing it.")
async def diagram_read(context: ToolContext, diagram_id: str) -> ToolResult:
    result = await _store(context).get(diagram_id)
    if result is None:
        return error(context, "no such diagram")
    elements = [{key: el.get(key) for key in ("id", "type", "x", "y", "width", "height", "text", "points") if key in el} for el in result["scene"]["elements"]]
    return ok(context, json.dumps({"id": result["id"], "title": result["title"], "version": result["version"], "elements": elements}, ensure_ascii=False))


@tool_group("diagrams")
@search_hint("edit excalidraw diagram add shape arrow text change move delete схема диаграмма фигура стрелка текст добавить изменить удалить блок")
@tool(name="DiagramEdit", description="Add or change shapes in an editable Excalidraw diagram. Each shape has type (rectangle, diamond, ellipse, arrow, line, text), x, y, width, height, optional text, colors, points and id. An existing id changes that shape. delete_ids removes shapes. Pass the current version from DiagramRead; a concurrent edit is refused.")
async def diagram_edit(context: ToolContext, diagram_id: str, version: int, elements: list[dict[str, Any]] | None = None, delete_ids: list[str] | None = None, title: str | None = None) -> ToolResult:
    store = _store(context)
    result = await store.get(diagram_id)
    if result is None:
        return error(context, "no such diagram")
    if result["version"] != version:
        return error(context, "the diagram changed elsewhere; read it again before editing")
    current = result["scene"]["elements"]
    by_id = {el["id"]: el for el in current}
    remove = set(delete_ids or [])
    added: list[str] = []
    try:
        for spec in elements or []:
            shape = _shape(spec, by_id.get(str(spec.get("id") or "")))
            by_id[shape["id"]] = shape
            added.append(shape["id"])
            if shape["type"] in {"rectangle", "ellipse", "diamond"} and "text" in spec:
                label = shape_label(shape, str(spec["text"]))
                by_id[label["id"]] = label
                added.append(label["id"])
        remove |= {f"{shape_id}-label" for shape_id in remove}
        scene = {**result["scene"], "elements": [by_id[el["id"]] for el in current if el["id"] in by_id and el["id"] not in remove] + [by_id[shape_id] for shape_id in added if shape_id not in {el["id"] for el in current} and shape_id not in remove]}
        saved = await store.save(diagram_id, title or result["title"], scene, version)
    except (ValueError, RuntimeError) as exc:
        return error(context, str(exc))
    return ok(context, f"saved diagram {diagram_id}, version {saved['version']}; shapes {', '.join(added)}; open /app/diagrams/{diagram_id}")


TOOLS = [diagram_list, diagram_create, diagram_read, diagram_edit]

__all__ = ["TOOLS"]
