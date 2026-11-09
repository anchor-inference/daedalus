"""The agent's side of canvases: list, read, create and edit the boards of its chat and the chat's project.

A canvas is the operator's board as much as the agent's, and the operator may be dragging a card on it
right now. So every edit names the version the agent last read, and the store accepts it on top of
newer work when the two touched different items; only an edit of an item the operator changed since
comes back as stale, naming those items. The tools speak in cards, groups and edges and leave ids,
sizes and placement to the host: given text and connections, a new card is sized for its words and
put where it does not cover anything.

The comment bar under a board writes into the canvas's owner chat as an ordinary message that names
the canvas, so "make the second card red" from the bar reaches the agent with the id it needs here.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.stores.canvas_doc import CanvasError
from daedalus.stores.canvas_layout import card_size, layout
from daedalus.stores.canvases import CanvasStore, NotFound, Stale, store_for
from daedalus.stores.notes import Scope
from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import error, ok, services_for

GAP = 120
TEXT_EXCERPT = 600
NODE_KEYS = ("type", "text", "file", "subpath", "url", "label", "background", "backgroundStyle", "x", "y", "width", "height", "color")
EDGE_KEYS = {"from": "fromNode", "to": "toNode", "fromSide": "fromSide", "toSide": "toSide", "fromEnd": "fromEnd", "toEnd": "toEnd", "label": "label", "color": "color"}


def _store(context: ToolContext) -> CanvasStore:
    return store_for(services_for(context).extra["manager"])


def link(canvas: dict[str, Any]) -> str:
    return f"[{canvas['title']}](/app/canvas/{canvas['id']})"


async def _scopes(store: CanvasStore, context: ToolContext) -> tuple[Scope, Scope | None]:
    return await store.scopes_of_chat(context.session_id)


async def _visible(store: CanvasStore, context: ToolContext, canvas_id: str) -> dict[str, Any]:
    """A canvas of this chat or of its project; any other is reported as missing, not as forbidden."""
    found = await store.get(canvas_id)
    chat, project = await _scopes(store, context)
    if found is None or Scope(found["scope"], found["scope_id"]) not in (chat, project):
        raise NotFound(canvas_id)
    return found


def _excerpt(text: str) -> str:
    return text if len(text) <= TEXT_EXCERPT else text[: TEXT_EXCERPT - 1] + "…"


def summary(canvas: dict[str, Any]) -> str:
    """The board as the model needs it to plan an edit: every node with its id, kind, place, size,
    colour and words; which group holds which nodes; every edge as ``from.side -> to.side``."""
    doc = canvas["doc"]
    nodes = doc.get("nodes", [])
    lines = [
        f"canvas {canvas['id']} {json.dumps(canvas['title'], ensure_ascii=False)} version {canvas['version']}; "
        f"{canvas['scope']} {json.dumps(canvas['scope_name'], ensure_ascii=False)}; last change by {canvas['updated_by']}; link {link(canvas)}"
    ]

    def inside(group: dict[str, Any], node: dict[str, Any]) -> bool:
        return (node is not group and group["x"] <= node["x"] and group["y"] <= node["y"]
                and node["x"] + node["width"] <= group["x"] + group["width"] and node["y"] + node["height"] <= group["y"] + group["height"])

    if nodes:
        lines.append("nodes (drawing order, last on top):")
    for node in nodes:
        head = f"  {node['id']} {node['type']} at {node['x']},{node['y']} size {node['width']}x{node['height']}"
        if node.get("color"):
            head += f" color {node['color']}"
        shape = (node.get("daedalus") or {}).get("shape")
        if shape:
            head += f" (was {shape})"
        if node["type"] == "text":
            head += f": {json.dumps(_excerpt(str(node.get('text', ''))), ensure_ascii=False)}"
        elif node["type"] == "file":
            head += f": {node.get('file')}{node.get('subpath') or ''}"
        elif node["type"] == "link":
            head += f": {node.get('url')}"
        else:
            members = [other["id"] for other in nodes if other["type"] != "group" and inside(node, other)]
            head += f" label {json.dumps(node.get('label') or '', ensure_ascii=False)}; holds {', '.join(members) or 'nothing'}"
        lines.append(head)
    edges = doc.get("edges", [])
    if edges:
        lines.append("edges:")
    for edge in edges:
        line = f"  {edge['id']} {edge['fromNode']}.{edge.get('fromSide') or 'auto'} -> {edge['toNode']}.{edge.get('toSide') or 'auto'}"
        if edge.get("fromEnd") == "arrow" or edge.get("toEnd") == "none":
            line += f" ends {edge.get('fromEnd') or 'none'}/{edge.get('toEnd') or 'arrow'}"
        if edge.get("color"):
            line += f" color {edge['color']}"
        if edge.get("label"):
            line += f" {json.dumps(edge['label'], ensure_ascii=False)}"
        lines.append(line)
    if canvas.get("files"):
        lines.append("attached files: " + ", ".join(f"files/{file['id']}/{file['name']}" for file in canvas["files"]))
    if not nodes:
        lines.append("empty")
    return "\n".join(lines)


def _free_spot(nodes: list[dict[str, Any]], width: int, height: int) -> tuple[int, int]:
    """Where a new card goes when the agent gave no place: right of everything, level with the top."""
    if not nodes:
        return 0, 0
    right = max(node["x"] + node["width"] for node in nodes)
    top = min(node["y"] for node in nodes)
    return right + GAP, top


def _node_ops(current: dict[str, Any], specs: list[Any]) -> list[dict[str, Any]]:
    """Each entry merged onto the node it names, or made into a new one; a card given new words and no
    size grows to fit them, so the agent never has to compute one."""
    by_id = {node["id"]: node for node in current.get("nodes", [])}
    placed = list(current.get("nodes", []))
    ops = []
    for spec in specs:
        if not isinstance(spec, dict):
            raise CanvasError("every entry of nodes is an object")
        node_id = str(spec.get("id") or uuid.uuid4().hex[:16])
        old = by_id.get(node_id)
        node: dict[str, Any] = dict(old) if old else {"id": node_id, "type": spec.get("type") or "text"}
        for key in NODE_KEYS:
            if key in spec:
                if spec[key] is None:
                    node.pop(key, None)
                else:
                    node[key] = spec[key]
        if node.get("type") == "text":
            node.setdefault("text", "")
        sized = "width" in spec or "height" in spec
        if not sized and (old is None or "text" in spec or "label" in spec):
            words = str(node.get("text") or node.get("url") or node.get("file") or node.get("label") or "")
            width, height = card_size(words)
            if node["type"] == "group":
                width, height = max(width, 400), max(height, 300)
            if old is None:
                node["width"], node["height"] = width, height
            else:
                node["height"] = max(node["height"], height) if node["type"] == "text" else node["height"]
        if old is None and ("x" not in spec or "y" not in spec):
            x, y = _free_spot(placed, node["width"], node["height"])
            node.setdefault("x", x)
            node.setdefault("y", y)
        placed.append(node)
        ops.append({"op": "put", "kind": "node", "item": node, **({"at": "back"} if old is None and node["type"] == "group" else {})})
    return ops


def _edge_ops(current: dict[str, Any], specs: list[Any], taken: set[str]) -> list[dict[str, Any]]:
    by_id = {edge["id"]: edge for edge in current.get("edges", [])}
    ops = []
    for spec in specs:
        if not isinstance(spec, dict):
            raise CanvasError("every entry of edges is an object")
        old = by_id.get(str(spec.get("id") or ""))
        edge: dict[str, Any] = dict(old) if old else {}
        for key, field in EDGE_KEYS.items():
            if key in spec:
                if spec[key] is None:
                    edge.pop(field, None)
                else:
                    edge[field] = spec[key]
        if not edge.get("fromNode") or not edge.get("toNode"):
            raise CanvasError("an edge needs from and to: the ids of two nodes")
        if old is None:
            edge_id = str(spec.get("id") or f"{edge['fromNode']}-{edge['toNode']}")
            suffix = 2
            while edge_id in taken:
                edge_id, suffix = f"{edge['fromNode']}-{edge['toNode']}-{suffix}", suffix + 1
            edge = {"id": edge_id, **edge}
        taken.add(edge["id"])
        ops.append({"op": "put", "kind": "edge", "item": edge})
    return ops


def _failure(context: ToolContext, exc: Exception) -> ToolResult:
    if isinstance(exc, NotFound):
        return error(context, "no such canvas in this chat or its project; CanvasList shows the ids")
    if isinstance(exc, Stale):
        if exc.ids:
            return error(context, f"the operator changed {', '.join(exc.ids)} after your version; CanvasRead the canvas again and redo the edit on what is there now (it is at version {exc.current['version']})")
        return error(context, f"the canvas changed after your version; CanvasRead it again (it is at version {exc.current['version']})")
    return error(context, str(exc))


@tool_group("canvas")
@search_hint("canvas board whiteboard diagram list холст доска схема диаграмма список канвас холсты доски схемы какие есть obsidian")
@tool(name="CanvasList", description="List the canvases of this chat and of its project: id, title, scope, version, size, last change.")
async def canvas_list(context: ToolContext) -> ToolResult:
    store = _store(context)
    try:
        listing = await store.list(session_id=context.session_id)
    except NotFound as exc:
        return _failure(context, exc)
    if not listing["items"]:
        return ok(context, "no canvases in this chat or its project yet; CanvasCreate makes one")
    lines = [
        f"- {item['id']} {json.dumps(item['title'], ensure_ascii=False)} · {item['scope']} · v{item['version']} · {item['nodes']} nodes, {item['edges']} edges"
        f" · {item['updated_at'][:16]} by {item['updated_by']}{' · shared' if item['shared'] else ''}"
        for item in listing["items"]
    ]
    return ok(context, "\n".join(lines))


@tool_group("canvas")
@search_hint("read canvas board cards edges what is on прочитать холст доска карточки связи посмотреть что на холсте изучить")
@tool(name="CanvasRead", description="Read a canvas: its version, every node (id, kind, place, size, colour, words), what each group holds, every edge. Read it before an edit.")
async def canvas_read(context: ToolContext, canvas_id: str) -> ToolResult:
    store = _store(context)
    try:
        found = await _visible(store, context, canvas_id)
    except NotFound as exc:
        return _failure(context, exc)
    return ok(context, summary(found), canvas_id=canvas_id)


@tool_group("canvas")
@search_hint("create canvas board diagram flowchart mind map draw создать холст доску схему нарисовать диаграмму блок-схему карту")
@tool(
    name="CanvasCreate",
    description=(
        "Create a canvas (a JSON Canvas board, Obsidian-compatible) the operator opens and edits in the app, laid out for you. "
        "nodes: [{id, text, color?}] become cards sized for their words (Markdown; color \"1\".. \"6\" = red, orange, yellow, "
        "green, cyan, purple, or #rrggbb); edges: [{from, to, label?, color?}]; direction: right|down. scope: chat (default) or "
        "project, for a board every chat of the project should see. Link the operator as [Title](/app/canvas/<id>)."
    ),
)
async def canvas_create(context: ToolContext, title: str, nodes: list[dict[str, Any]] | None = None, edges: list[dict[str, Any]] | None = None, direction: str = "right", scope: str = "chat") -> ToolResult:
    store = _store(context)
    try:
        chat, project = await _scopes(store, context)
        if scope == "project" and project is None:
            raise CanvasError("this chat is in no project, so its canvases are the chat's own")
        if scope not in ("chat", "project"):
            raise CanvasError("scope is chat or project")
        if edges and not nodes:
            raise CanvasError("edges need nodes to connect")
        cards, lines = layout(nodes or [], edges or [], direction) if nodes else ([], [])
        made = await store.create(title, project if scope == "project" and project else chat, owner_session_id=context.session_id, doc={"nodes": cards, "edges": lines}, source="agent")
    except (CanvasError, NotFound) as exc:
        return _failure(context, exc)
    return ok(context, f"created canvas {made['id']} (version {made['version']}, {made['scope']}); link it as {link(made)}", canvas_id=made["id"])


@tool_group("canvas")
@search_hint("edit canvas add card connect edge move recolor group delete изменить холст добавить карточку связать стрелку переместить цвет группа удалить")
@tool(
    name="CanvasEdit",
    description=(
        "Change a canvas item by item. Pass the version from CanvasRead. nodes: [{id?, type?: text|link|file|group, text?, url?, "
        "file?, label?, x?, y?, width?, height?, color?}] — an existing id changes only the fields given (null removes one), "
        "a new card without a place goes right of the board and is sized for its words. edges: [{id?, from, to, fromSide?, "
        "toSide?: top|right|bottom|left, label?, color?, fromEnd?, toEnd?: none|arrow}]. delete_ids removes nodes (with their "
        "edges) or edges; deleting a group keeps what it holds. layout_graph: {nodes, edges, direction} adds a laid-out graph beside "
        "the board. An edit is refused as stale only when an item it touches changed after your version."
    ),
)
async def canvas_edit(
    context: ToolContext, canvas_id: str, version: int, nodes: list[dict[str, Any]] | None = None, edges: list[dict[str, Any]] | None = None,
    delete_ids: list[str] | None = None, title: str | None = None, layout_graph: dict[str, Any] | None = None,
) -> ToolResult:
    store = _store(context)
    try:
        found = await _visible(store, context, canvas_id)
        doc = found["doc"]
        ops: list[dict[str, Any]] = [{"op": "delete", "id": str(item_id)} for item_id in delete_ids or []]
        ops += _node_ops(doc, nodes or [])
        taken = {item["id"] for item in (*doc.get("nodes", []), *doc.get("edges", []))}
        if layout_graph:
            existing = doc.get("nodes", [])
            right = max((node["x"] + node["width"] for node in existing), default=-GAP)
            top = min((node["y"] for node in existing), default=0)
            cards, lines = layout(layout_graph.get("nodes") or [], layout_graph.get("edges") or [], str(layout_graph.get("direction") or "right"), origin=(right + GAP, top))
            clash = [card["id"] for card in cards if card["id"] in taken]
            if clash:
                raise CanvasError(f"layout node ids already on the canvas: {', '.join(clash)}; pick new ids")
            ops += [{"op": "put", "kind": "node", "item": card} for card in cards]
            for line in lines:
                if line["id"] in taken:
                    line["id"] = f"{line['id']}-{uuid.uuid4().hex[:6]}"
                taken.add(line["id"])
            ops += [{"op": "put", "kind": "edge", "item": line} for line in lines]
            taken.update(card["id"] for card in cards)
        ops += _edge_ops(doc, edges or [], taken)
        if not ops and title is None:
            raise CanvasError("nothing to change: give nodes, edges, delete_ids, layout_graph or title")
        saved, merged = await store.apply(canvas_id, int(version), ops, title=title, source="agent")
    except (CanvasError, NotFound, Stale) as exc:
        return _failure(context, exc)
    note = " (saved on top of the operator's newer changes to other items)" if merged else ""
    return ok(context, f"saved canvas {canvas_id}, now version {saved['version']}{note}; {len(ops)} change(s); link {link(saved)}", canvas_id=canvas_id)


TOOLS = [canvas_list, canvas_read, canvas_create, canvas_edit]

__all__ = ["TOOLS", "link", "summary"]
