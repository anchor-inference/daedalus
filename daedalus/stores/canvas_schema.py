"""The canvas tables, and the one-off conversion of the Excalidraw diagrams that came before them.

Diagrams were global: a list with no owner, which chat had drawn one known only from tool results
in the session log. Canvases belong to a chat or a project. The conversion reads that log once to
place each diagram: with the project of the chat that created it when the chat is in a real project
(every chat of the project then sees it, and the chat stays its owner, so the comment bar writes
there), with the chat itself when the chat has no project of its own, and with the operator's main
project when no chat is recorded at all.

Each converted canvas keeps what the conversion could not carry (``canvas_convert`` lists it): the
diagram's last picture, as the thumbnail the server had drawn, and the Excalidraw scene itself as a
file the operator can download and open at excalidraw.com, both in a group beside the cards. The
diagram revisions are not carried over: they are Excalidraw scenes, and nothing here draws those any
more. The canvas history starts at the conversion, with a revision that says what was lost.

"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import aiosqlite

SQL = """
CREATE TABLE IF NOT EXISTS canvases (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    owner_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    doc_json TEXT NOT NULL,
    clock_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT 'operator' CHECK (updated_by IN ('operator', 'agent')),
    share_token TEXT NOT NULL DEFAULT '',
    preview_svg TEXT NOT NULL DEFAULT '',
    node_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    CHECK ((session_id IS NULL) <> (project_id IS NULL))
);
CREATE INDEX IF NOT EXISTS canvases_by_session ON canvases(session_id, updated_at) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS canvases_by_project ON canvases(project_id, updated_at) WHERE project_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS canvases_share_token ON canvases(share_token) WHERE share_token != '';
CREATE TABLE IF NOT EXISTS canvas_revisions (
    canvas_id TEXT NOT NULL REFERENCES canvases(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    doc_json TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    started_at TEXT,
    source TEXT NOT NULL CHECK (source IN ('operator', 'agent')),
    kind TEXT NOT NULL CHECK (kind IN ('create', 'edit', 'restore', 'import')),
    restored_from INTEGER,
    summary_json TEXT NOT NULL DEFAULT '{}',
    preview_svg TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (canvas_id, version)
);
CREATE TABLE IF NOT EXISTS canvas_files (
    id TEXT NOT NULL,
    canvas_id TEXT NOT NULL REFERENCES canvases(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    data BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (canvas_id, id)
);
"""

GROUP_LABEL = "Original Excalidraw drawing"
GAP = 160
"""Space between the converted cards and the group that holds the original drawing."""
INSET = 32
VIEWBOX = re.compile(r'viewBox="\s*(-?[\d.]+)[\s,]+(-?[\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)\s*"')


def _file_name(title: str) -> str:
    cleaned = " ".join(re.sub(r"[^\w\- .]+", " ", title).split()).strip(" .")[:60].strip()
    return (cleaned or "diagram") + ".excalidraw"


def _picture_size(svg: str, fallback: tuple[int, int]) -> tuple[int, int]:
    """The thumbnail at the size of what it shows: its view box is the diagram's own extent."""
    match = VIEWBOX.search(svg)
    if not match:
        return fallback
    width, height = float(match.group(3)), float(match.group(4))
    if width < 1 or height < 1:
        return fallback
    return round(width), round(height)


async def _owner(conn: aiosqlite.Connection, diagram_id: str) -> str | None:
    """The chat whose agent created the diagram: its first tool result names the id. The ``LIKE``
    narrows the log to the few events that mention the id before any JSON is read."""
    row = await (await conn.execute(
        "SELECT e.session_id FROM session_events e JOIN sessions s ON s.id=e.session_id"
        " WHERE e.kind='tool_result' AND e.payload LIKE ? AND json_valid(e.payload)"
        " AND json_extract(e.payload,'$.metadata.diagram_id')=? ORDER BY e.created_at LIMIT 1",
        (f"%{diagram_id}%", diagram_id),
    )).fetchone()
    return str(row["session_id"]) if row else None


async def _scope(conn: aiosqlite.Connection, session_id: str | None) -> tuple[str, str, str | None] | None:
    """``(kind, id, owner chat)`` for a diagram, or None when the database has nowhere to put it."""
    if session_id:
        row = await (await conn.execute(
            "SELECT s.project_id, p.system, json_extract(p.settings,'$.system') AS marked, json_extract(p.settings,'$.ephemeral') AS ephemeral"
            " FROM sessions s JOIN projects p ON p.id=s.project_id WHERE s.id=?",
            (session_id,),
        )).fetchone()
        if row is not None:
            # The notes' rule for a real project: a scratch project made for one chat is that chat.
            if not (row["system"] or row["marked"]) and row["ephemeral"]:
                return "chat", session_id, session_id
            return "project", str(row["project_id"]), session_id
    main = await (await conn.execute("SELECT id FROM projects WHERE system='dispatcher'")).fetchone()
    if main is None:
        main = await (await conn.execute("SELECT id FROM projects ORDER BY created_at LIMIT 1")).fetchone()
    return ("project", str(main["id"]), None) if main is not None else None


async def convert_diagrams(conn: aiosqlite.Connection) -> None:
    # Lazy, all four: the canvas and note stores import the database module, which imports this one.
    from daedalus.stores import canvas_doc  # Lazy: imports the database module through its callers.
    from daedalus.stores.canvas_convert import bounds, convert  # Lazy: one-off conversion, needed only here.
    from daedalus.stores.canvases import insert_canvas  # Lazy: the store imports the database module.
    from daedalus.stores.notes import Scope  # Lazy: the notes store imports the database module.

    tables = {row[0] for row in await (await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    if "diagrams" not in tables:
        return
    diagrams = await (await conn.execute("SELECT id,title,scene_json,created_at,updated_at,updated_by,preview_svg FROM diagrams ORDER BY created_at")).fetchall()
    for diagram in diagrams:
        placed = await _scope(conn, await _owner(conn, diagram["id"]))
        if placed is None:
            continue  # no project exists at all, so there is nothing a canvas could belong to
        kind, scope_id, owner = placed
        scene = json.loads(diagram["scene_json"])
        result = convert(scene)
        doc = result.doc
        left, top, right, bottom = bounds(doc)
        content_w, content_h = right - left, bottom - top
        picture_w, picture_h = _picture_size(diagram["preview_svg"] or "", (max(content_w, 600), max(content_h, 400)))
        # Beside the cards along the shorter side: a tall plan gets its picture to the right, a wide
        # board gets it underneath, so the two read side by side.
        group_x, group_y = (right + GAP, top) if content_h > content_w else (left, bottom + GAP)
        preview_id, scene_id = uuid.uuid4().hex[:12], uuid.uuid4().hex[:12]
        scene_name = _file_name(diagram["title"])
        legacy = []
        if diagram["preview_svg"]:
            legacy.append({"id": f"legacy-picture-{preview_id}", "type": "file", "file": f"files/{preview_id}/preview.svg",
                           "x": group_x + INSET, "y": group_y + INSET * 2, "width": picture_w, "height": picture_h})
        legacy.append({"id": f"legacy-scene-{scene_id}", "type": "file", "file": f"files/{scene_id}/{scene_name}",
                       "x": group_x + INSET, "y": group_y + INSET * 2 + (picture_h + INSET if diagram["preview_svg"] else 0), "width": 400, "height": 80})
        group_w = max(node["x"] + node["width"] for node in legacy) - group_x + INSET
        group_h = max(node["y"] + node["height"] for node in legacy) - group_y + INSET
        group = {"id": f"legacy-{diagram['id'][:12]}", "type": "group", "label": GROUP_LABEL, "x": group_x, "y": group_y, "width": group_w, "height": group_h}
        doc = canvas_doc.check({"nodes": [group, *doc["nodes"], *legacy], "edges": doc["edges"]})
        canvas_id = diagram["id"]
        source = "agent" if diagram["updated_by"] == "agent" else "operator"
        await insert_canvas(conn, canvas_id=canvas_id, title=diagram["title"], scope=Scope(kind, scope_id), owner_session_id=owner, doc=doc,
                            at=diagram["updated_at"], source=source, kind="import")
        await conn.execute("UPDATE canvases SET created_at=? WHERE id=?", (diagram["created_at"], canvas_id))
        summary: dict[str, Any] = {"created": True, "converted": {"shapes": result.shapes, "dropped": result.dropped, "snapped": result.snapped, "bends": result.bends}}
        await conn.execute("UPDATE canvas_revisions SET summary_json=? WHERE canvas_id=? AND version=1", (json.dumps(summary), canvas_id))
        if diagram["preview_svg"]:
            svg = diagram["preview_svg"].encode()
            await conn.execute("INSERT INTO canvas_files(id,canvas_id,name,mime,size,data,created_at) VALUES(?,?,?,?,?,?,?)",
                               (preview_id, canvas_id, "preview.svg", "image/svg+xml", len(svg), svg, diagram["updated_at"]))
        original = diagram["scene_json"].encode()
        await conn.execute("INSERT INTO canvas_files(id,canvas_id,name,mime,size,data,created_at) VALUES(?,?,?,?,?,?,?)",
                           (scene_id, canvas_id, scene_name, "application/json", len(original), original, diagram["updated_at"]))
    await conn.execute("DROP TABLE IF EXISTS diagram_revisions")
    await conn.execute("DROP TABLE IF EXISTS diagrams")


__all__ = ["SQL", "convert_diagrams"]
