"""Versioned Excalidraw scenes shared by the editor and agent tools."""

from __future__ import annotations

import json
import secrets
import uuid
from typing import Any

from daedalus.stores.calendar import now
from daedalus.stores.database import Database

MAX_SCENE_BYTES = 4_000_000
MAX_ELEMENTS = 2000
ALLOWED_ELEMENTS = {"rectangle", "diamond", "ellipse", "arrow", "line", "text", "freedraw", "image", "frame", "magicframe", "iframe", "embeddable"}


def shape_label(shape: dict[str, Any], text: str) -> dict[str, Any]:
    """Bind a text element to a shape; a bare shape text field does not render or move with it."""
    label_id = f"{shape['id']}-label"
    seed = uuid.uuid5(uuid.NAMESPACE_OID, label_id).int & 0x7FFFFFFF
    nonce = uuid.uuid5(uuid.NAMESPACE_OID, f"{label_id}:{shape.get('version', 1)}:{text}").int & 0x7FFFFFFF
    shape["boundElements"] = [item for item in shape.get("boundElements") or [] if item["id"] != label_id] + [{"id": label_id, "type": "text"}]
    return {
        "id": label_id, "type": "text", "x": shape["x"] + 12, "y": shape["y"] + 12,
        "width": max(20, shape["width"] - 24), "height": max(20, shape["height"] - 24), "angle": 0,
        "strokeColor": shape.get("strokeColor", "#1e1e1e"), "backgroundColor": "transparent",
        "fillStyle": "solid", "strokeWidth": 1, "strokeStyle": "solid", "roughness": 0, "opacity": 100,
        "groupIds": [], "frameId": None, "roundness": None, "seed": seed,
        "version": 1, "versionNonce": nonce, "isDeleted": False,
        "boundElements": None, "updated": shape.get("updated", 0), "link": None, "locked": False,
        "text": text, "originalText": text, "fontSize": 16, "fontFamily": 1, "textAlign": "left",
        "verticalAlign": "top", "baseline": 14, "containerId": shape["id"], "lineHeight": 1.25,
        "autoResize": False,
    }


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


class DiagramStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def list(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id:
            # Tool results record the exact created diagram id, including diagrams made before
            # the editor had a session view. Match that id rather than guessing by title or time.
            rows = await self.db.fetchall(
                "SELECT d.id,d.title,d.version,d.created_at,d.updated_at FROM session_events e"
                " JOIN diagrams d ON d.id=json_extract(e.payload,'$.metadata.diagram_id')"
                " WHERE e.session_id=? AND e.kind='tool_result' GROUP BY d.id ORDER BY d.updated_at DESC LIMIT 200",
                (session_id,),
            )
        else:
            rows = await self.db.fetchall("SELECT id,title,version,created_at,updated_at FROM diagrams ORDER BY updated_at DESC LIMIT 200")
        return [dict(row) for row in rows]

    async def get(self, diagram_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM diagrams WHERE id=?", (diagram_id,))
        if row is None:
            return None
        scene = json.loads(row["scene_json"])
        await self._recover_labels(diagram_id, scene, row["updated_at"])
        return {**dict(row), "scene": scene}

    async def _recover_labels(self, diagram_id: str, scene: dict[str, Any], saved_at: str) -> None:
        # Earlier DiagramEdit calls accepted text on a rectangle but dropped it when writing
        # Excalidraw elements. Recover those labels from the recorded tool input on read so an
        # existing diagram is visible without asking the agent to redraw it.
        missing = {item["id"]: item for item in scene["elements"] if item["type"] in {"rectangle", "ellipse", "diamond"} and not any(other["id"] == f"{item['id']}-label" for other in scene["elements"])}
        if missing:
            events = await self.db.fetchall(
                "SELECT payload FROM session_events WHERE kind='tool_use_stop' AND created_at<=? AND payload LIKE ? ORDER BY created_at DESC LIMIT 100",
                (saved_at, f"%{diagram_id}%"),
            )
            for event in events:
                edit = json.loads(event["payload"]).get("final_input") or {}
                if edit.get("diagram_id") != diagram_id:
                    continue
                for spec in edit.get("elements") or []:
                    shape = missing.pop(spec.get("id"), None) if spec.get("text") else None
                    if shape is not None:
                        scene["elements"].append(shape_label(shape, str(spec["text"])))
                if not missing:
                    break

    async def create(self, title: str, scene: dict[str, Any] | None = None) -> dict[str, Any]:
        title = title.strip()
        if not 1 <= len(title) <= 160:
            raise ValueError("a diagram title needs 1 to 160 characters")
        checked = validate_scene(scene or {"elements": [], "appState": {}, "files": {}})
        diagram_id, at = uuid.uuid4().hex, now()
        async with self.db.transaction() as conn:
            await conn.execute("INSERT INTO diagrams(id,title,scene_json,created_at,updated_at) VALUES(?,?,?,?,?)", (diagram_id, title, json.dumps(checked), at, at))
            await conn.execute("INSERT INTO diagram_revisions(diagram_id,version,title,scene_json,saved_at) VALUES(?,1,?,?,?)", (diagram_id, title, json.dumps(checked), at))
        result = await self.get(diagram_id)
        assert result is not None
        return result

    async def save(self, diagram_id: str, title: str, scene: dict[str, Any], version: int) -> dict[str, Any]:
        if not 1 <= len(title.strip()) <= 160:
            raise ValueError("a diagram title needs 1 to 160 characters")
        checked = validate_scene(scene)
        async with self.db.transaction() as conn:
            at = now()
            cursor = await conn.execute(
                "UPDATE diagrams SET title=?,scene_json=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (title.strip(), json.dumps(checked), at, diagram_id, version),
            )
            if cursor.rowcount == 0:
                exists = await conn.execute("SELECT 1 FROM diagrams WHERE id=?", (diagram_id,))
                if await exists.fetchone() is None:
                    raise KeyError(diagram_id)
                raise RuntimeError("the diagram changed elsewhere; reload before saving")
            await conn.execute(
                "INSERT INTO diagram_revisions(diagram_id,version,title,scene_json,saved_at) VALUES(?,?,?,?,?)",
                (diagram_id, version + 1, title.strip(), json.dumps(checked), at),
            )
        result = await self.get(diagram_id)
        assert result is not None
        return result

    async def delete(self, diagram_id: str) -> None:
        row = await self.db.fetchone("SELECT 1 FROM diagrams WHERE id=?", (diagram_id,))
        if row is None:
            raise KeyError(diagram_id)
        await self.db.execute("DELETE FROM diagrams WHERE id=?", (diagram_id,))

    async def versions(self, diagram_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT version,title,saved_at FROM diagram_revisions WHERE diagram_id=? ORDER BY version DESC LIMIT 100",
            (diagram_id,),
        )
        return [dict(row) for row in rows]

    async def version(self, diagram_id: str, version: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT version,title,scene_json,saved_at FROM diagram_revisions WHERE diagram_id=? AND version=?",
            (diagram_id, version),
        )
        if row is None:
            return None
        scene = json.loads(row["scene_json"])
        await self._recover_labels(diagram_id, scene, row["saved_at"])
        return {**dict(row), "scene": scene}

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
        return {"title": found["title"], "version": found["version"], "scene": found["scene"]} if found else None


__all__ = ["DiagramStore", "validate_scene"]
