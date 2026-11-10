"""Laying out a graph as canvas cards: the agent says what is connected, this says where it goes.

A model asked for coordinates places boxes on top of each other and draws edges through them, so the
agent's tools take nodes and edges and nothing else. Nodes go in layers along the flow (a node one
step after another goes one layer further), cycles are broken at the edge that closes them, and within
a layer nodes are ordered by where their neighbours sit, which keeps most edges from crossing. Every
edge leaves the side facing the next layer and enters the opposite one.

Card sizes are an estimate from the words, because the server has no fonts: generous on purpose,
since a card a little too big wastes a few pixels and one too small hides the end of its text.
"""

from __future__ import annotations

import math
from typing import Any

from daedalus.stores.canvas_doc import CanvasError

MAX_NODES = 300
MAX_EDGES = 600
CHAR_WIDTH = 8.4
"""An average glyph of the board's body text, Latin and Cyrillic alike, rounded up."""
LINE_HEIGHT = 24
MIN_WIDTH, MAX_WIDTH = 180, 360
MIN_HEIGHT = 60
PADDING_X, PADDING_Y = 32, 28
LAYER_GAP = 120
SIBLING_GAP = 56


def card_size(text: str) -> tuple[int, int]:
    """Width and height for a card holding ``text``: as narrow as its longest line allows, wrapped
    at :data:`MAX_WIDTH`, and tall enough for the lines that makes."""
    lines = text.split("\n") or [""]
    longest = max((len(line) for line in lines), default=0)
    width = min(MAX_WIDTH, max(MIN_WIDTH, math.ceil(longest * CHAR_WIDTH + PADDING_X)))
    per_line = max(1, int((width - PADDING_X) / CHAR_WIDTH))
    rows = sum(max(1, math.ceil(len(line) / per_line)) for line in lines)
    height = max(MIN_HEIGHT, rows * LINE_HEIGHT + PADDING_Y)
    return width, height


def _layers(order: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """Longest-path layers over the graph with the edges that close a cycle left out."""
    outgoing: dict[str, list[str]] = {node: [] for node in order}
    for source, target in edges:
        outgoing[source].append(target)
    state: dict[str, int] = {}
    forward: list[tuple[str, str]] = []

    def visit(node: str) -> None:
        # Iterative depth-first search: a long chain must not reach Python's recursion limit.
        stack = [(node, iter(outgoing[node]))]
        state[node] = 1
        while stack:
            current, children = stack[-1]
            child = next(children, None)
            if child is None:
                state[current] = 2
                stack.pop()
                continue
            if state.get(child) == 1:
                continue  # closes a cycle: drawn, but it does not push its target a layer further
            forward.append((current, child))
            if child not in state:
                state[child] = 1
                stack.append((child, iter(outgoing[child])))

    for node in order:
        if node not in state:
            visit(node)
    layer = dict.fromkeys(order, 0)
    changed = True
    while changed:
        changed = False
        for source, target in forward:
            if layer[target] < layer[source] + 1:
                layer[target] = layer[source] + 1
                changed = True
    return layer


def _ordered(order: list[str], layer: dict[str, int], edges: list[tuple[str, str]]) -> list[list[str]]:
    rows: list[list[str]] = [[] for _ in range(max(layer.values(), default=0) + 1)]
    for node in order:
        rows[layer[node]].append(node)
    neighbours: dict[str, list[str]] = {node: [] for node in order}
    for source, target in edges:
        neighbours[source].append(target)
        neighbours[target].append(source)
    for _ in range(2):
        for index in range(1, len(rows)):
            place = {node: position for position, node in enumerate(rows[index - 1])}
            rows[index].sort(key=lambda node: (sum(place[n] for n in neighbours[node] if n in place) / max(1, sum(1 for n in neighbours[node] if n in place))) if any(n in place for n in neighbours[node]) else math.inf)
    return rows


def layout(nodes: list[Any], edges: list[Any], direction: str = "right", *, origin: tuple[int, int] = (0, 0)) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cards and edges for ``nodes`` (``{id, text, color?}``) and ``edges`` (``{from, to, label?,
    color?}``), laid out from ``origin`` along ``direction`` (``right`` or ``down``)."""
    if direction not in ("right", "down"):
        raise CanvasError("direction is right or down")
    if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
        raise CanvasError(f"a layout takes at most {MAX_NODES} nodes and {MAX_EDGES} edges")
    specs: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]:
            raise CanvasError("every node is an object with an id")
        if node["id"] in specs:
            raise CanvasError(f"node {node['id']} appears twice")
        specs[node["id"]] = node
    pairs: list[tuple[str, str]] = []
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("from") not in specs or edge.get("to") not in specs:
            raise CanvasError(f"edge {edge.get('from') if isinstance(edge, dict) else edge!r} -> {edge.get('to') if isinstance(edge, dict) else ''} needs two nodes of this layout")
        pairs.append((edge["from"], edge["to"]))
    order = list(specs)
    rows = _ordered(order, _layers(order, pairs), pairs)
    sizes = {node_id: card_size(str(specs[node_id].get("text") or specs[node_id].get("label") or "")) for node_id in order}
    placed: dict[str, dict[str, Any]] = {}
    along = origin[0] if direction == "right" else origin[1]
    breadths = [sum((sizes[n][1] if direction == "right" else sizes[n][0]) for n in row) + SIBLING_GAP * (len(row) - 1) for row in rows]
    widest = max(breadths, default=0)
    for row, breadth in zip(rows, breadths, strict=True):
        across = (origin[1] if direction == "right" else origin[0]) + (widest - breadth) // 2
        depth = max(((sizes[n][0] if direction == "right" else sizes[n][1]) for n in row), default=0)
        for node_id in row:
            width, height = sizes[node_id]
            spec = specs[node_id]
            x, y = (along + (depth - width) // 2, across) if direction == "right" else (across, along + (depth - height) // 2)
            card: dict[str, Any] = {"id": node_id, "type": "text", "text": str(spec.get("text") or spec.get("label") or ""), "x": x, "y": y, "width": width, "height": height}
            if spec.get("color"):
                card["color"] = spec["color"]
            placed[node_id] = card
            across += (height if direction == "right" else width) + SIBLING_GAP
        along += depth + LAYER_GAP
    used: set[str] = set(order)
    drawn_edges = []
    forward_side, back_side = ("right", "left") if direction == "right" else ("bottom", "top")
    layer_of = {node_id: index for index, row in enumerate(rows) for node_id in row}
    for edge, (source, target) in zip(edges, pairs, strict=True):
        edge_id = str(edge.get("id") or f"{source}-{target}")
        suffix = 2
        while edge_id in used:
            edge_id, suffix = f"{source}-{target}-{suffix}", suffix + 1
        used.add(edge_id)
        if layer_of[target] == layer_of[source]:
            # Two cards of one layer sit side by side across the flow: the edge runs across too.
            sides = ("bottom", "top") if direction == "right" else ("right", "left")
            if (placed[target]["y"] if direction == "right" else placed[target]["x"]) < (placed[source]["y"] if direction == "right" else placed[source]["x"]):
                sides = (sides[1], sides[0])
        else:
            sides = (back_side, forward_side) if layer_of[target] < layer_of[source] else (forward_side, back_side)
        drawn: dict[str, Any] = {"id": edge_id, "fromNode": source, "fromSide": sides[0], "toNode": target, "toSide": sides[1]}
        if edge.get("label"):
            drawn["label"] = str(edge["label"])
        if edge.get("color"):
            drawn["color"] = edge["color"]
        drawn_edges.append(drawn)
    return [placed[node_id] for node_id in order], drawn_edges


__all__ = ["card_size", "layout"]
