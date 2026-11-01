"""The Terminals workspace's named layouts: a grid saved under a name, kept on the host.

A layout is the workspace as the app holds it — one, two or four places shown, the terminal in each
of the four places, the two splitter positions, the place with the keyboard — plus, for every
terminal it names, what reopening that terminal would take once it has ended: its environment,
folder, owner, title and program kind. Those are read from ``terminals`` when the layout is saved,
not taken from the app, so a layout never carries a folder the host did not itself record.

The shape is checked and clamped here rather than stored as sent. A layout is restored on other
devices, by other builds of the app, months later; a malformed one must come back as a usable grid
(the same defaults the app's own ``normalise`` applies), never as a page that fails to open.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from daedalus.stores.database import Database

SLOTS = 4
LAYOUTS = (1, 2, 4)
NAME_MAX = 80
LAYOUTS_MAX = 50
"""How many layouts are kept. A list longer than this is no longer something anyone chooses from."""
TERMINAL_ID_MAX = 64
FRACTION_MIN = 0.2
FRACTION_MAX = 0.8
"""The splitter clamp the app applies (``workspacestate.ts``): a pane never under a fifth of the grid."""


class LayoutError(ValueError):
    """A layout refused, with the HTTP status the route answers and a sentence for the operator."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_name(name: object) -> str:
    text = " ".join(str(name or "").split())
    if not text:
        raise LayoutError("a layout needs a name")
    if len(text) > NAME_MAX:
        raise LayoutError(f"a layout's name is at most {NAME_MAX} characters")
    return text


def _fraction(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    if number != number:  # NaN, which compares unequal to itself and would pass the clamp unchanged
        return 0.5
    return min(FRACTION_MAX, max(FRACTION_MIN, number))


def clean_workspace(raw: object) -> dict[str, Any]:
    """The workspace part of a layout, in the app's shape, with anything malformed set to its default.

    Three panes are saved as four (the 2 × 2 grid with one place empty), as the app reads them; a
    terminal named twice keeps its first place only, because a terminal is in at most one place.
    """
    source = raw if isinstance(raw, dict) else {}
    try:
        layout = int(source.get("layout") or 1)
    except (TypeError, ValueError):
        layout = 1
    layout = 4 if layout == 3 else layout if layout in LAYOUTS else 1
    panes: list[str | None] = [None] * SLOTS
    seen: set[str] = set()
    given = source.get("panes")
    if isinstance(given, list):
        for i, value in enumerate(given[:SLOTS]):
            text = value.strip() if isinstance(value, str) else ""
            if text and len(text) <= TERMINAL_ID_MAX and text not in seen:
                seen.add(text)
                panes[i] = text
    active = source.get("active")
    active = active if isinstance(active, int) and not isinstance(active, bool) and 0 <= active < layout else 0
    return {"layout": layout, "panes": panes, "col": _fraction(source.get("col", 0.5)), "row": _fraction(source.get("row", 0.5)), "active": active}


class TerminalLayouts:
    """The ``terminal_layouts`` table: list, save (a name saved again is replaced), rename, delete."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _view(row: Any) -> dict[str, Any]:
        try:
            data = json.loads(row["data_json"] or "{}")
        except ValueError:
            data = {}
        workspace = clean_workspace(data)
        terminals = data.get("terminals") if isinstance(data.get("terminals"), dict) else {}
        named = {tid: terminals[tid] for tid in workspace["panes"] if tid and isinstance(terminals.get(tid), dict)}
        return {"id": row["id"], "name": row["name"], "workspace": workspace, "terminals": named, "created_at": row["created_at"], "updated_at": row["updated_at"]}

    async def list(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM terminal_layouts ORDER BY updated_at DESC, name")
        return [self._view(row) for row in rows]

    async def _terminals(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = await self.db.fetchall(f"SELECT id, env, cwd, title, project_id, owner_kind, owner_id, profile, sandbox FROM terminals WHERE id IN ({marks})", ids)
        return {
            row["id"]: {
                "env": row["env"], "cwd": row["cwd"], "title": row["title"], "project_id": row["project_id"],
                "owner_kind": row["owner_kind"], "owner_id": row["owner_id"], "profile": row["profile"], "sandbox": bool(row["sandbox"]),
            }
            for row in rows
        }

    async def save(self, name: object, workspace: object) -> dict[str, Any]:
        """Keeps ``workspace`` under ``name``; a layout of that name already kept is replaced, so
        "save" over a name is how a layout is updated."""
        label = clean_name(name)
        clean = clean_workspace(workspace)
        terminals = await self._terminals([tid for tid in clean["panes"] if tid])
        data = json.dumps({**clean, "terminals": terminals}, separators=(",", ":"))
        at = _now()
        async with self.db.transaction() as conn:
            found = await (await conn.execute("SELECT id FROM terminal_layouts WHERE name = ?", (label,))).fetchone()
            if found is not None:
                layout_id = str(found["id"])
                await conn.execute("UPDATE terminal_layouts SET data_json = ?, updated_at = ? WHERE id = ?", (data, at, layout_id))
            else:
                count = await (await conn.execute("SELECT count(*) AS n FROM terminal_layouts")).fetchone()
                if count is not None and int(count["n"]) >= LAYOUTS_MAX:
                    raise LayoutError(f"at most {LAYOUTS_MAX} layouts are kept; delete one first", status=409)
                layout_id = secrets.token_hex(6)
                await conn.execute("INSERT INTO terminal_layouts(id, name, data_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", (layout_id, label, data, at, at))
        row = await self.db.fetchone("SELECT * FROM terminal_layouts WHERE id = ?", (layout_id,))
        return self._view(row)

    async def rename(self, layout_id: str, name: object) -> dict[str, Any]:
        label = clean_name(name)
        if await self.db.fetchone("SELECT 1 FROM terminal_layouts WHERE id = ?", (layout_id,)) is None:
            raise LayoutError("no such layout", status=404)
        try:
            await self.db.execute("UPDATE terminal_layouts SET name = ?, updated_at = ? WHERE id = ?", (label, _now(), layout_id))
        except sqlite3.IntegrityError:
            raise LayoutError(f"a layout named {label!r} already exists", status=409) from None
        row = await self.db.fetchone("SELECT * FROM terminal_layouts WHERE id = ?", (layout_id,))
        return self._view(row)

    async def delete(self, layout_id: str) -> None:
        if await self.db.fetchone("SELECT 1 FROM terminal_layouts WHERE id = ?", (layout_id,)) is None:
            raise LayoutError("no such layout", status=404)
        await self.db.execute("DELETE FROM terminal_layouts WHERE id = ?", (layout_id,))


__all__ = ["LayoutError", "TerminalLayouts", "clean_name", "clean_workspace"]
