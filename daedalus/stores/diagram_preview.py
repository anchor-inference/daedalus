"""A small schematic SVG of an Excalidraw scene, drawn on the server when the scene is saved.

The thumbnail is made here rather than by the editor because half the saves have no browser behind
them: the agent writes diagrams through its tools, and a preview the editor uploaded would be missing
(or stale) for exactly the diagrams the operator did not draw. It is a sketch, not a rendering: shapes
keep their outline and fill, connectors their path, and text becomes faint bars, because at card
size real glyphs are an unreadable smudge that costs more bytes than the rest of the picture.
"""

from __future__ import annotations

import math
from html import escape
from typing import Any

MAX_ELEMENTS = 400
"""Elements drawn at most. A scene near the 2000-element cap still gets a recognisable picture, and
the preview of the worst case stays a few dozen kilobytes."""
MAX_POINTS = 48
"""Points kept of a freehand stroke or a long polyline; the rest are evenly thinned out."""
PADDING = 16


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _colour(value: Any, fallback: str) -> str:
    text = str(value or "")
    if text == "transparent" or not text:
        return fallback
    # Only plain colour values reach the markup: the scene comes from a client, and an attribute is
    # escaped anyway, but a url(...) or an expression has no business in a thumbnail.
    if text.startswith("#") and len(text) in (4, 7, 9) and all(c in "0123456789abcdefABCDEF" for c in text[1:]):
        return text
    return fallback


def _thin(points: list[list[float]]) -> list[list[float]]:
    if len(points) <= MAX_POINTS:
        return points
    step = (len(points) - 1) / (MAX_POINTS - 1)
    return [points[round(index * step)] for index in range(MAX_POINTS)]


def _bounds(element: dict[str, Any]) -> tuple[float, float, float, float]:
    x, y = _number(element.get("x")), _number(element.get("y"))
    points = element.get("points")
    if isinstance(points, list) and points:
        xs = [x + _number(point[0]) for point in points if isinstance(point, list) and len(point) >= 2]
        ys = [y + _number(point[1]) for point in points if isinstance(point, list) and len(point) >= 2]
        if xs and ys:
            return min(xs), min(ys), max(xs), max(ys)
    width, height = _number(element.get("width")), _number(element.get("height"))
    return min(x, x + width), min(y, y + height), max(x, x + width), max(y, y + height)


def preview_svg(scene: dict[str, Any]) -> str:
    """The scene's elements as a compact SVG string; an empty scene gives an empty string."""
    elements = [item for item in scene.get("elements") or [] if isinstance(item, dict) and not item.get("isDeleted")][:MAX_ELEMENTS]
    if not elements:
        return ""
    boxes = [_bounds(item) for item in elements]
    left = min(box[0] for box in boxes) - PADDING
    top = min(box[1] for box in boxes) - PADDING
    width = max(1.0, max(box[2] for box in boxes) + PADDING - left)
    height = max(1.0, max(box[3] for box in boxes) + PADDING - top)
    # Strokes are scaled with the picture, so a thin line in a wide scene would vanish; one stroke
    # width in the picture's own units keeps every outline visible at card size.
    stroke = max(1.5, max(width, height) / 220)
    parts: list[str] = []
    for item in elements:
        parts.append(_element(item, left, top, stroke))
    body = "".join(part for part in parts if part)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {round(width)} {round(height)}" '
        f'preserveAspectRatio="xMidYMid meet" fill="none" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )


def _element(item: dict[str, Any], left: float, top: float, stroke: float) -> str:
    kind = item.get("type")
    x, y = _number(item.get("x")) - left, _number(item.get("y")) - top
    width, height = _number(item.get("width")), _number(item.get("height"))
    line = _colour(item.get("strokeColor"), "#1e1e1e")
    fill = _colour(item.get("backgroundColor"), "none")
    opacity = max(0.0, min(1.0, _number(item.get("opacity"), 100) / 100))
    attrs = f'stroke="{escape(line)}" stroke-width="{stroke:.1f}" fill="{escape(fill)}"'
    if opacity < 1:
        attrs += f' opacity="{opacity:.2f}"'
    angle = _number(item.get("angle"))
    turn = f' transform="rotate({math.degrees(angle):.1f} {x + width / 2:.0f} {y + height / 2:.0f})"' if angle else ""
    if kind in ("rectangle", "frame", "magicframe", "embeddable", "iframe"):
        radius = min(width, height) * 0.12 if item.get("roundness") else 0
        dashed = ' stroke-dasharray="6 6"' if kind in ("frame", "magicframe") else ""
        return f'<rect x="{x:.0f}" y="{y:.0f}" width="{width:.0f}" height="{height:.0f}" rx="{radius:.0f}" {attrs}{dashed}{turn}/>'
    if kind == "ellipse":
        return f'<ellipse cx="{x + width / 2:.0f}" cy="{y + height / 2:.0f}" rx="{width / 2:.0f}" ry="{height / 2:.0f}" {attrs}{turn}/>'
    if kind == "diamond":
        points = f"{x + width / 2:.0f},{y:.0f} {x + width:.0f},{y + height / 2:.0f} {x + width / 2:.0f},{y + height:.0f} {x:.0f},{y + height / 2:.0f}"
        return f'<polygon points="{points}" {attrs}{turn}/>'
    if kind == "image":
        return f'<rect x="{x:.0f}" y="{y:.0f}" width="{width:.0f}" height="{height:.0f}" fill="#ced4da" stroke="none"{turn}/>'
    if kind in ("arrow", "line", "freedraw"):
        raw = item.get("points") if isinstance(item.get("points"), list) else []
        points = _thin([[_number(point[0]), _number(point[1])] for point in raw if isinstance(point, list) and len(point) >= 2])
        if len(points) < 2:
            return ""
        path = " ".join(f"{x + px:.0f},{y + py:.0f}" for px, py in points)
        fill_attr = f'stroke="{escape(line)}" stroke-width="{stroke:.1f}" fill="none"'
        head = ""
        if kind == "arrow" and item.get("endArrowhead"):
            head = _arrowhead(x + points[-2][0], y + points[-2][1], x + points[-1][0], y + points[-1][1], stroke * 4, line, stroke)
        return f'<polyline points="{path}" {fill_attr}/>{head}'
    if kind == "text":
        return _text_bars(item, x, y, width, line)
    return ""


def _arrowhead(x1: float, y1: float, x2: float, y2: float, size: float, colour: str, stroke: float) -> str:
    angle = math.atan2(y2 - y1, x2 - x1)
    wings = [(x2 - size * math.cos(angle - turn), y2 - size * math.sin(angle - turn)) for turn in (0.5, -0.5)]
    return f'<polyline points="{wings[0][0]:.0f},{wings[0][1]:.0f} {x2:.0f},{y2:.0f} {wings[1][0]:.0f},{wings[1][1]:.0f}" stroke="{escape(colour)}" stroke-width="{stroke:.1f}" fill="none"/>'


def _text_bars(item: dict[str, Any], x: float, y: float, width: float, colour: str) -> str:
    size = max(4.0, _number(item.get("fontSize"), 20))
    step = size * _number(item.get("lineHeight"), 1.25)
    lines = str(item.get("text") or "").split("\n")[:12]
    align = item.get("textAlign")
    bars = []
    for index, words in enumerate(lines):
        if not words.strip():
            continue
        # The same glyph-width estimate the label builder uses; a bar the length of its line is all a
        # thumbnail needs to say "a label sits here".
        length = min(width or len(words) * size * 0.55, len(words) * size * 0.55)
        start = x + (width - length) / 2 if align == "center" else x + width - length if align == "right" else x
        bars.append(f'<rect x="{start:.0f}" y="{y + index * step + size * 0.3:.0f}" width="{length:.0f}" height="{size * 0.45:.0f}" rx="{size * 0.2:.0f}" fill="{escape(colour)}" opacity="0.45"/>')
    return "".join(bars)


__all__ = ["preview_svg"]
