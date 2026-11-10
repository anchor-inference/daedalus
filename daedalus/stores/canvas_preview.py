"""A small schematic SVG of a canvas, drawn on the server whenever the canvas is saved.

It is made here rather than by the board because half the saves have no browser behind them: the
agent writes through its tools, and a thumbnail the board uploaded would be missing exactly for the
canvases the operator did not draw. It is a sketch, not a rendering: cards keep their box and colour,
groups their dashed frame, edges their curve, and words become faint bars, because at card size real
glyphs are an unreadable smudge that costs more bytes than the rest of the picture.

Colours are fixed shades rather than the app's tokens: the picture is an image, which cannot read the
page's stylesheet, and it has to stay legible on both themes, so it carries its own light background.
"""

from __future__ import annotations

from typing import Any

from daedalus.stores.canvas_doc import HEX_COLOR, PRESET_COLORS

MAX_ITEMS = 600
"""Items drawn at most, so the thumbnail of the largest canvas stays a few dozen kilobytes."""
PADDING = 24
WIDTH = 480
"""The thumbnail's nominal width; the view box keeps the canvas's own proportions."""

PRESETS = dict(zip(PRESET_COLORS, ("#e03131", "#f08c00", "#e8b400", "#2f9e44", "#0c8599", "#7048e8"), strict=True))
INK = "#5c6370"
PAPER = "#fbfbfc"
CARD = "#ffffff"


def _colour(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value in PRESETS:
        return PRESETS[value]
    return value if HEX_COLOR.match(value) else None


def _box(node: dict[str, Any]) -> tuple[float, float, float, float]:
    return float(node["x"]), float(node["y"]), float(node["width"]), float(node["height"])


def _anchor(node: dict[str, Any], side: str | None, toward: tuple[float, float]) -> tuple[float, float, str]:
    x, y, w, h = _box(node)
    cx, cy = x + w / 2, y + h / 2
    if side not in ("top", "right", "bottom", "left"):
        dx, dy = toward[0] - cx, toward[1] - cy
        side = ("right" if dx > 0 else "left") if abs(dx) * h >= abs(dy) * w else ("bottom" if dy > 0 else "top")
    return {"top": (cx, y), "bottom": (cx, y + h), "left": (x, cy), "right": (x + w, cy)}[side] + (side,)


def _pull(point: tuple[float, float], side: str, distance: float) -> tuple[float, float]:
    dx, dy = {"top": (0, -1), "bottom": (0, 1), "left": (-1, 0), "right": (1, 0)}[side]
    return point[0] + dx * distance, point[1] + dy * distance


def _bars(x: float, y: float, w: float, h: float, words: str) -> list[str]:
    """Faint lines standing for a card's text, as many as the text would roughly fill."""
    text = " ".join(words.split())
    if not text or w < 24 or h < 18:
        return []
    line_height = 18.0
    chars_per_line = max(1, int((w - 24) / 8))
    lines = min(max(1, -(-len(text) // chars_per_line)), int((h - 16) // line_height) or 1)
    out = []
    for index in range(lines):
        remaining = len(text) - index * chars_per_line
        width = (w - 24) * min(1.0, remaining / chars_per_line) if index == lines - 1 else (w - 24)
        out.append(f'<rect x="{x + 12:.0f}" y="{y + 12 + index * line_height:.0f}" width="{max(8.0, width):.0f}" height="8" rx="4" fill="{INK}" opacity="0.28"/>')
    return out


def preview_svg(doc: dict[str, Any]) -> str:
    nodes = [node for node in doc.get("nodes", []) if isinstance(node, dict)][:MAX_ITEMS]
    if not nodes:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} 300"><rect width="{WIDTH}" height="300" fill="{PAPER}"/></svg>'
    left = min(float(node["x"]) for node in nodes) - PADDING
    top = min(float(node["y"]) for node in nodes) - PADDING
    right = max(float(node["x"]) + float(node["width"]) for node in nodes) + PADDING
    bottom = max(float(node["y"]) + float(node["height"]) for node in nodes) + PADDING
    width, height = max(right - left, 1.0), max(bottom - top, 1.0)
    stroke = max(1.5, width / WIDTH * 1.5)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{left:.0f} {top:.0f} {width:.0f} {height:.0f}" width="{WIDTH}" height="{round(WIDTH * height / width)}">',
        f'<rect x="{left:.0f}" y="{top:.0f}" width="{width:.0f}" height="{height:.0f}" fill="{PAPER}"/>',
    ]
    by_id = {node["id"]: node for node in nodes}
    for node in nodes:
        if node.get("type") != "group":
            continue
        x, y, w, h = _box(node)
        colour = _colour(node.get("color")) or INK
        parts.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="12" fill="{colour}" fill-opacity="0.06" stroke="{colour}" stroke-opacity="0.6" stroke-width="{stroke:.1f}" stroke-dasharray="{stroke * 4:.0f} {stroke * 3:.0f}"/>')
    for edge in doc.get("edges", [])[:MAX_ITEMS]:
        source, target = by_id.get(edge.get("fromNode")), by_id.get(edge.get("toNode"))
        if source is None or target is None:
            continue
        sx, sy, sw, sh = _box(source)
        tx, ty, tw, th = _box(target)
        start = _anchor(source, edge.get("fromSide"), (tx + tw / 2, ty + th / 2))
        end = _anchor(target, edge.get("toSide"), (sx + sw / 2, sy + sh / 2))
        reach = max(40.0, min(160.0, ((start[0] - end[0]) ** 2 + (start[1] - end[1]) ** 2) ** 0.5 / 2.5))
        c1, c2 = _pull(start[:2], start[2], reach), _pull(end[:2], end[2], reach)
        colour = _colour(edge.get("color")) or INK
        parts.append(f'<path d="M{start[0]:.0f} {start[1]:.0f} C{c1[0]:.0f} {c1[1]:.0f} {c2[0]:.0f} {c2[1]:.0f} {end[0]:.0f} {end[1]:.0f}" fill="none" stroke="{colour}" stroke-width="{stroke:.1f}" opacity="0.8"/>')
    for node in nodes:
        if node.get("type") == "group":
            continue
        x, y, w, h = _box(node)
        colour = _colour(node.get("color"))
        border = colour or "#c9ced6"
        parts.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="8" fill="{CARD}" stroke="{border}" stroke-width="{stroke:.1f}"/>')
        if colour:
            parts.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="8" fill="{colour}" fill-opacity="0.08"/>')
        words = str(node.get("text") or node.get("url") or node.get("file") or "")
        parts.extend(_bars(x, y, w, h, words))
    parts.append("</svg>")
    # Every value written above is a number or one of our own colour strings; nothing from the
    # document reaches the markup as text, so the picture cannot carry a script or a link.
    return "".join(parts)


__all__ = ["preview_svg"]
