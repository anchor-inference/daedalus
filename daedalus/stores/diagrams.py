"""Versioned Excalidraw scenes shared by the editor and agent tools."""

from __future__ import annotations

import json
import uuid
from typing import Any

from daedalus.stores.calendar import now
from daedalus.stores.database import Database

MAX_SCENE_BYTES = 4_000_000
MAX_ELEMENTS = 2000
ALLOWED_ELEMENTS = {"rectangle", "diamond", "ellipse", "arrow", "line", "text", "freedraw", "image", "frame", "magicframe", "iframe", "embeddable"}


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

    async def list(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT id,title,version,created_at,updated_at FROM diagrams ORDER BY updated_at DESC LIMIT 200")
        return [dict(row) for row in rows]

    async def get(self, diagram_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM diagrams WHERE id=?", (diagram_id,))
        return {**dict(row), "scene": json.loads(row["scene_json"])} if row else None

    async def create(self, title: str, scene: dict[str, Any] | None = None) -> dict[str, Any]:
        title = title.strip()
        if not 1 <= len(title) <= 160:
            raise ValueError("a diagram title needs 1 to 160 characters")
        checked = validate_scene(scene or {"elements": [], "appState": {}, "files": {}})
        diagram_id, at = uuid.uuid4().hex, now()
        await self.db.execute("INSERT INTO diagrams(id,title,scene_json,created_at,updated_at) VALUES(?,?,?,?,?)", (diagram_id, title, json.dumps(checked), at, at))
        result = await self.get(diagram_id)
        assert result is not None
        return result

    async def save(self, diagram_id: str, title: str, scene: dict[str, Any], version: int) -> dict[str, Any]:
        if not 1 <= len(title.strip()) <= 160:
            raise ValueError("a diagram title needs 1 to 160 characters")
        checked = validate_scene(scene)
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE diagrams SET title=?,scene_json=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (title.strip(), json.dumps(checked), now(), diagram_id, version),
            )
            if cursor.rowcount == 0:
                exists = await conn.execute("SELECT 1 FROM diagrams WHERE id=?", (diagram_id,))
                if await exists.fetchone() is None:
                    raise KeyError(diagram_id)
                raise RuntimeError("the diagram changed elsewhere; reload before saving")
        result = await self.get(diagram_id)
        assert result is not None
        return result

    async def delete(self, diagram_id: str) -> None:
        row = await self.db.fetchone("SELECT 1 FROM diagrams WHERE id=?", (diagram_id,))
        if row is None:
            raise KeyError(diagram_id)
        await self.db.execute("DELETE FROM diagrams WHERE id=?", (diagram_id,))


__all__ = ["DiagramStore", "validate_scene"]
