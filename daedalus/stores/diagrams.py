"""Versioned Excalidraw scenes shared by the editor and agent tools.

Every save bumps the diagram's version, which is the lock both writers hold: the editor and the agent
each send the version they last saw, and the one that lost is refused rather than merged. The history
behind it is coarser than the versions. The editor autosaves a second after every stroke, and a row
per autosave grew the history by the full scene each time; consecutive saves by the operator within
``COALESCE_SECONDS`` fold into one revision, while an agent's edit, a restore and a creation always
keep a row of their own, and the oldest revisions beyond ``REVISION_CAP`` are dropped.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime
from typing import Any

import aiosqlite

from daedalus.stores.calendar import now
from daedalus.stores.database import Database
from daedalus.stores.diagram_preview import preview_svg

MAX_SCENE_BYTES = 4_000_000
MAX_ELEMENTS = 2000
ALLOWED_ELEMENTS = {"rectangle", "diamond", "ellipse", "arrow", "line", "text", "freedraw", "image", "frame", "magicframe", "iframe", "embeddable"}
SOURCES = ("user", "agent")
COALESCE_SECONDS = 600
"""How long a run of the operator's autosaves stays one revision, counted from its first save, so an
hour of drawing still leaves a revision every ten minutes to go back to."""
REVISION_CAP = 100
"""Revisions kept per diagram; the oldest go first."""

SHAPES = {"rectangle", "diamond", "ellipse", "image", "frame", "magicframe", "iframe", "embeddable", "freedraw"}
CONNECTORS = {"arrow", "line"}
VISIBLE = ("type", "x", "y", "width", "height", "angle", "strokeColor", "backgroundColor", "fillStyle", "strokeStyle", "strokeWidth", "roughness", "opacity", "roundness", "text", "originalText", "fontSize", "fontFamily", "textAlign", "points", "startArrowhead", "endArrowhead", "link", "locked", "groupIds", "fileId")
"""What a history entry compares. Excalidraw fills in and rewrites the rest whenever it loads a scene
(fractional indices, re-measured text, nonces, binding focus), and comparing everything reported ten
shapes "changed" on the first save after the operator merely opened an agent's diagram."""
DERIVED_TEXT = ("width", "height", "text")
"""A text box's size and its wrapped lines are measured from its words and font, not chosen."""


def _visible(item: dict[str, Any]) -> dict[str, Any]:
    picked: dict[str, Any] = {}
    for key in VISIBLE:
        if key not in item or (item.get("type") == "text" and key in DERIVED_TEXT):
            continue
        value = item[key]
        if isinstance(value, float):
            # Whole pixels: a position Excalidraw rounds on load is the same position.
            value = round(value)
        elif key == "points" and isinstance(value, list):
            value = [[round(float(n)) for n in point[:2]] for point in value if isinstance(point, list | tuple) and len(point) >= 2]
        picked[key] = value
    for end in ("startBinding", "endBinding"):
        binding = item.get(end)
        picked[end] = binding.get("elementId") if isinstance(binding, dict) else None
    return picked


def validate_scene(scene: Any) -> dict[str, Any]:
    if not isinstance(scene, dict) or not isinstance(scene.get("elements"), list):
        raise ValueError("a diagram needs an Excalidraw elements array")
    if len(scene["elements"]) > MAX_ELEMENTS:
        raise ValueError("a diagram has too many elements")
    ids: set[str] = set()
    for element in scene["elements"]:
        if not isinstance(element, dict) or element.get("type") not in ALLOWED_ELEMENTS or not isinstance(element.get("id"), str):
            raise ValueError("an Excalidraw element has an invalid type or id")
        if element["id"] in ids:
            raise ValueError("an Excalidraw element id appears twice")
        ids.add(element["id"])
    if not isinstance(scene.get("appState", {}), dict) or not isinstance(scene.get("files", {}), dict):
        raise ValueError("invalid Excalidraw app state or files")
    encoded = json.dumps(scene, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > MAX_SCENE_BYTES:
        raise ValueError("the diagram exceeds 4 MB")
    return scene


def _title(title: str) -> str:
    title = title.strip()
    if not 1 <= len(title) <= 160:
        raise ValueError("a diagram title needs 1 to 160 characters")
    return title


def _source(source: str) -> str:
    if source not in SOURCES:
        raise ValueError("a diagram is saved by the user or by an agent")
    return source


def _items(elements: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """What the operator would call the scene's items: id to (category, content).

    A label bound to a shape is part of that shape — renaming a box is one change to the box, not a
    change to some text element the operator never sees as separate."""
    live = [item for item in elements if isinstance(item, dict) and not item.get("isDeleted")]
    ids = {item.get("id") for item in live}
    labels: dict[str, list[str]] = {}
    for item in live:
        if item.get("type") == "text" and item.get("containerId") in ids:
            labels.setdefault(str(item["containerId"]), []).append(str(item.get("originalText") or item.get("text") or ""))
    result: dict[str, tuple[str, str]] = {}
    for item in live:
        if item.get("type") == "text" and item.get("containerId") in ids:
            continue
        kind = item.get("type")
        category = "connector" if kind in CONNECTORS else "text" if kind == "text" else "shape"
        result[str(item.get("id"))] = (category, json.dumps([_visible(item), labels.get(str(item.get("id")), [])], sort_keys=True, default=str))
    return result


def change_summary(before: dict[str, Any] | None, before_title: str | None, after: dict[str, Any], after_title: str) -> dict[str, Any]:
    """How one revision differs from the one before it, in counts a person can read.

    ``{"added": {"shape": 3}, "removed": {}, "changed": 2, "renamed": true}``; a revision with no
    predecessor is ``{"created": true}``. Element ids never appear: they mean nothing to the reader."""
    if before is None:
        return {"created": True}
    old, new = _items(before.get("elements") or []), _items(after.get("elements") or [])
    added: dict[str, int] = {}
    removed: dict[str, int] = {}
    for key, (category, _) in new.items():
        if key not in old:
            added[category] = added.get(category, 0) + 1
    for key, (category, _) in old.items():
        if key not in new:
            removed[category] = removed.get(category, 0) + 1
    changed = sum(1 for key, (_, content) in new.items() if key in old and old[key][1] != content)
    return {"added": added, "removed": removed, "changed": changed, "renamed": before_title is not None and before_title != after_title}


def _age(earlier: str, later: str) -> float:
    try:
        return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds()
    except ValueError:
        return float("inf")


async def record_revision(conn: aiosqlite.Connection, diagram_id: str, version: int, title: str, scene: dict[str, Any], at: str, source: str, kind: str, restored_from: int | None = None) -> None:
    """Write the revision a save produced, folding it into the previous one when it continues the
    operator's run of autosaves. Runs inside the caller's transaction."""
    cursor = await conn.execute(
        "SELECT version,source,kind,started_at FROM diagram_revisions WHERE diagram_id=? ORDER BY version DESC LIMIT 2",
        (diagram_id,),
    )
    recent = await cursor.fetchall()
    latest = recent[0] if recent else None
    coalesce = (
        kind == "edit" and source == "user" and latest is not None and latest["source"] == "user" and latest["kind"] == "edit"
        and _age(latest["started_at"] or at, at) < COALESCE_SECONDS
    )
    # The summary is against the revision the reader will see listed before this one: after folding,
    # that is the row before the folded one, not the save a second ago.
    base_version = (recent[1]["version"] if len(recent) > 1 else None) if coalesce else (latest["version"] if latest else None)
    base = None
    if base_version is not None:
        row = await (await conn.execute("SELECT title,scene_json FROM diagram_revisions WHERE diagram_id=? AND version=?", (diagram_id, base_version))).fetchone()
        base = row
    summary = change_summary(json.loads(base["scene_json"]) if base else None, base["title"] if base else None, scene, title)
    if kind == "restore":
        summary = {**summary, "restored_from": restored_from}
    encoded, preview = json.dumps(scene), preview_svg(scene)
    if coalesce and latest is not None:
        await conn.execute(
            "UPDATE diagram_revisions SET version=?,title=?,scene_json=?,saved_at=?,summary_json=?,preview_svg=? WHERE diagram_id=? AND version=?",
            (version, title, encoded, at, json.dumps(summary), preview, diagram_id, latest["version"]),
        )
    else:
        await conn.execute(
            "INSERT INTO diagram_revisions(diagram_id,version,title,scene_json,saved_at,source,kind,restored_from,started_at,summary_json,preview_svg)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (diagram_id, version, title, encoded, at, source, kind, restored_from, at, json.dumps(summary), preview),
        )
    await conn.execute(
        "DELETE FROM diagram_revisions WHERE diagram_id=? AND version NOT IN"
        " (SELECT version FROM diagram_revisions WHERE diagram_id=? ORDER BY version DESC LIMIT ?)",
        (diagram_id, diagram_id, REVISION_CAP),
    )


LIST_COLUMNS = "d.id,d.title,d.version,d.created_at,d.updated_at,d.updated_by,d.share_token!='' AS shared"


class DiagramStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def list(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id:
            # Tool results record the exact created diagram id, including diagrams made before
            # the editor had a session view. Match that id rather than guessing by title or time.
            rows = await self.db.fetchall(
                f"SELECT {LIST_COLUMNS} FROM session_events e"
                " JOIN diagrams d ON d.id=json_extract(e.payload,'$.metadata.diagram_id')"
                " WHERE e.session_id=? AND e.kind='tool_result' GROUP BY d.id ORDER BY d.updated_at DESC LIMIT 200",
                (session_id,),
            )
        else:
            rows = await self.db.fetchall(f"SELECT {LIST_COLUMNS} FROM diagrams d ORDER BY d.updated_at DESC LIMIT 200")
        return [{**dict(row), "shared": bool(row["shared"])} for row in rows]

    async def get(self, diagram_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT id,title,version,created_at,updated_at,updated_by,share_token,scene_json FROM diagrams WHERE id=?",
            (diagram_id,),
        )
        if row is None:
            return None
        found = dict(row)
        return {**{key: value for key, value in found.items() if key != "scene_json"}, "scene": json.loads(found["scene_json"])}

    async def head(self, diagram_id: str) -> dict[str, Any] | None:
        """What changed last, without the scene: the editor polls this and fetches the scene only when
        the version moved, instead of pulling up to 4 MB every few seconds."""
        row = await self.db.fetchone("SELECT id,title,version,updated_at,updated_by FROM diagrams WHERE id=?", (diagram_id,))
        return dict(row) if row else None

    async def create(self, title: str, scene: dict[str, Any] | None = None, *, source: str = "user") -> dict[str, Any]:
        title, source = _title(title), _source(source)
        checked = validate_scene(scene or {"elements": [], "appState": {}, "files": {}})
        diagram_id, at = uuid.uuid4().hex, now()
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO diagrams(id,title,scene_json,created_at,updated_at,updated_by,preview_svg) VALUES(?,?,?,?,?,?,?)",
                (diagram_id, title, json.dumps(checked), at, at, source, preview_svg(checked)),
            )
            await record_revision(conn, diagram_id, 1, title, checked, at, source, "create")
        result = await self.get(diagram_id)
        assert result is not None
        return result

    async def save(self, diagram_id: str, title: str, scene: dict[str, Any], version: int, *, source: str = "user") -> dict[str, Any]:
        title, source = _title(title), _source(source)
        checked = validate_scene(scene)
        async with self.db.transaction() as conn:
            at = now()
            await self._bump(conn, diagram_id, version, title, checked, at, source)
            await record_revision(conn, diagram_id, version + 1, title, checked, at, source, "edit")
        result = await self.get(diagram_id)
        assert result is not None
        return result

    async def rename(self, diagram_id: str, title: str, version: int) -> dict[str, Any]:
        """A new title on the current scene, from the list page, which holds the version but not the scene."""
        title = _title(title)
        async with self.db.transaction() as conn:
            row = await (await conn.execute("SELECT scene_json FROM diagrams WHERE id=?", (diagram_id,))).fetchone()
            if row is None:
                raise KeyError(diagram_id)
            scene, at = json.loads(row["scene_json"]), now()
            await self._bump(conn, diagram_id, version, title, scene, at, "user")
            await record_revision(conn, diagram_id, version + 1, title, scene, at, "user", "edit")
        return await self.head(diagram_id) or {}

    async def restore(self, diagram_id: str, revision: int, version: int) -> dict[str, Any]:
        """Make an earlier revision current again, as a new version on top of ``version``.

        Nothing is rewound: the restore is itself a revision, so restoring the wrong one is undone by
        restoring the one before it. A diagram that moved on since the reader opened the history is
        refused like any stale save, rather than replaced under someone's newer work."""
        async with self.db.transaction() as conn:
            row = await (await conn.execute("SELECT title,scene_json FROM diagram_revisions WHERE diagram_id=? AND version=?", (diagram_id, revision))).fetchone()
            if row is None:
                if await (await conn.execute("SELECT 1 FROM diagrams WHERE id=?", (diagram_id,))).fetchone() is None:
                    raise KeyError(diagram_id)
                raise ValueError("that version is no longer in the history")
            scene, at = json.loads(row["scene_json"]), now()
            await self._bump(conn, diagram_id, version, row["title"], scene, at, "user")
            await record_revision(conn, diagram_id, version + 1, row["title"], scene, at, "user", "restore", restored_from=revision)
        result = await self.get(diagram_id)
        assert result is not None
        return result

    async def duplicate(self, diagram_id: str, title: str | None = None) -> dict[str, Any]:
        found = await self.get(diagram_id)
        if found is None:
            raise KeyError(diagram_id)
        name = (title or "").strip() or f"{found['title']} (copy)"[:160]
        return await self.create(name, found["scene"])

    async def _bump(self, conn: aiosqlite.Connection, diagram_id: str, version: int, title: str, scene: dict[str, Any], at: str, source: str) -> None:
        cursor = await conn.execute(
            "UPDATE diagrams SET title=?,scene_json=?,version=version+1,updated_at=?,updated_by=?,preview_svg=? WHERE id=? AND version=?",
            (title, json.dumps(scene), at, source, preview_svg(scene), diagram_id, version),
        )
        if cursor.rowcount == 0:
            exists = await conn.execute("SELECT 1 FROM diagrams WHERE id=?", (diagram_id,))
            if await exists.fetchone() is None:
                raise KeyError(diagram_id)
            raise RuntimeError("the diagram changed elsewhere; reload before saving")

    async def delete(self, diagram_id: str) -> None:
        row = await self.db.fetchone("SELECT 1 FROM diagrams WHERE id=?", (diagram_id,))
        if row is None:
            raise KeyError(diagram_id)
        await self.db.execute("DELETE FROM diagrams WHERE id=?", (diagram_id,))

    async def versions(self, diagram_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT version,title,saved_at,started_at,source,kind,restored_from,summary_json FROM diagram_revisions WHERE diagram_id=? ORDER BY version DESC LIMIT ?",
            (diagram_id, REVISION_CAP),
        )
        return [{**{key: row[key] for key in row.keys() if key != "summary_json"}, "summary": json.loads(row["summary_json"] or "{}")} for row in rows]

    async def version(self, diagram_id: str, version: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT version,title,scene_json,saved_at,source,kind FROM diagram_revisions WHERE diagram_id=? AND version=?",
            (diagram_id, version),
        )
        if row is None:
            return None
        return {**{key: row[key] for key in row.keys() if key != "scene_json"}, "scene": json.loads(row["scene_json"])}

    async def preview(self, diagram_id: str, version: int | None = None) -> dict[str, Any] | None:
        if version is None:
            row = await self.db.fetchone("SELECT version,preview_svg FROM diagrams WHERE id=?", (diagram_id,))
        else:
            row = await self.db.fetchone("SELECT version,preview_svg FROM diagram_revisions WHERE diagram_id=? AND version=?", (diagram_id, version))
        return {"version": row["version"], "svg": row["preview_svg"]} if row else None

    async def share(self, diagram_id: str) -> str:
        async with self.db.transaction() as conn:
            cursor = await conn.execute("SELECT share_token FROM diagrams WHERE id=?", (diagram_id,))
            row = await cursor.fetchone()
            if row is None:
                raise KeyError(diagram_id)
            if row["share_token"]:
                return str(row["share_token"])
            token = secrets.token_urlsafe(24)
            await conn.execute("UPDATE diagrams SET share_token=? WHERE id=?", (token, diagram_id))
            return token

    async def revoke_share(self, diagram_id: str) -> None:
        if await self.db.fetchone("SELECT 1 FROM diagrams WHERE id=?", (diagram_id,)) is None:
            raise KeyError(diagram_id)
        await self.db.execute("UPDATE diagrams SET share_token='' WHERE id=?", (diagram_id,))

    async def shared(self, token: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT id FROM diagrams WHERE share_token=? AND share_token!=''", (token,))
        if row is None:
            return None
        found = await self.get(row["id"])
        return {"title": found["title"], "version": found["version"], "updated_at": found["updated_at"], "scene": found["scene"]} if found else None


async def repair_history(conn: aiosqlite.Connection) -> None:
    """The one-off pass that brings the diagrams written before revision sources existed up to date.

    Earlier ``DiagramEdit`` calls accepted text on a shape and dropped it when writing the scene; the
    store then searched the session log for that text on every read, forever. This puts the lost
    labels into the stored scenes once, from the recorded tool inputs, and the read path no longer
    looks. The same pass tells the agent's revisions from the operator's (an edit the agent made from
    version N produced version N + 1), and writes each revision's summary and thumbnail.

    Running it twice changes nothing: a shape that has a label is not given another.
    """
    from daedalus.stores.diagram_scene import bound_label  # Lazy: only this one-off repair needs it.

    diagrams = await (await conn.execute("SELECT id,version,scene_json FROM diagrams")).fetchall()
    for diagram in diagrams:
        diagram_id = diagram["id"]
        labels: dict[str, str] = {}
        agent_versions: set[int] = set()
        events = await (await conn.execute(
            "SELECT kind,payload FROM session_events WHERE kind IN ('tool_use_stop','tool_result') AND payload LIKE ? ORDER BY created_at",
            (f"%{diagram_id}%",),
        )).fetchall()
        for event in events:
            try:
                payload = json.loads(event["payload"])
            except ValueError:
                continue
            if event["kind"] == "tool_result":
                if (payload.get("metadata") or {}).get("diagram_id") == diagram_id:
                    agent_versions.add(1)
                continue
            edit = payload.get("final_input") or {}
            if edit.get("diagram_id") != diagram_id or not isinstance(edit.get("version"), int):
                continue
            if edit.get("elements") or edit.get("delete_ids") or edit.get("title"):
                agent_versions.add(edit["version"] + 1)
            for spec in edit.get("elements") or []:
                if isinstance(spec, dict) and spec.get("id") and spec.get("text"):
                    labels[str(spec["id"])] = str(spec["text"])
        revisions = await (await conn.execute(
            "SELECT version,title,scene_json FROM diagram_revisions WHERE diagram_id=? ORDER BY version", (diagram_id,),
        )).fetchall()
        previous: tuple[dict[str, Any], str] | None = None
        latest_source = "user"
        for index, revision in enumerate(revisions):
            scene = _with_labels(json.loads(revision["scene_json"]), labels, bound_label)
            source = "agent" if revision["version"] in agent_versions else "user"
            summary = change_summary(previous[0] if previous else None, previous[1] if previous else None, scene, revision["title"])
            if previous is None and revision["version"] != 1:
                # The revisions before it were never kept, so this is where the history begins, not
                # where the diagram was created.
                summary = {"oldest": True}
            await conn.execute(
                "UPDATE diagram_revisions SET scene_json=?,source=?,kind=?,started_at=saved_at,summary_json=?,preview_svg=? WHERE diagram_id=? AND version=?",
                (json.dumps(scene), source, "create" if index == 0 and revision["version"] == 1 else "edit", json.dumps(summary), preview_svg(scene), diagram_id, revision["version"]),
            )
            previous = (scene, revision["title"])
            if revision["version"] == diagram["version"]:
                latest_source = source
        current = _with_labels(json.loads(diagram["scene_json"]), labels, bound_label)
        await conn.execute(
            "UPDATE diagrams SET scene_json=?,updated_by=?,preview_svg=? WHERE id=?",
            (json.dumps(current), latest_source, preview_svg(current), diagram_id),
        )


def _with_labels(scene: dict[str, Any], labels: dict[str, str], bound_label: Any) -> dict[str, Any]:
    elements = scene.get("elements") or []
    labelled = {item.get("containerId") for item in elements if item.get("type") == "text" and item.get("containerId")}
    ids = {item.get("id") for item in elements}
    for shape in list(elements):
        shape_id = shape.get("id")
        if shape.get("type") not in {"rectangle", "ellipse", "diamond"} or shape_id in labelled or f"{shape_id}-label" in ids or shape_id not in labels:
            continue
        elements.append(bound_label(shape, labels[shape_id]))
    return scene


__all__ = ["DiagramStore", "change_summary", "record_revision", "repair_history", "validate_scene"]
