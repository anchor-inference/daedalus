"""The host's notes routes, kept in memory for the browser harnesses.

It answers the way ``daedalus/extensions/api_notes.py`` does: a chat's listing with both scopes and
their files, writes that carry the version they were typed on (a stale one is refused 409 with the
note as it is), metadata that never bumps the version, a deleted note that comes back, files uploaded
as multipart and served back, and a file a note still shows refused until it is forced. A harness
seeds a chat's project with ``project_of`` and notes with ``seed``; ``changed_elsewhere`` plays the
agent or another window saving in between.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default as default_policy
from typing import Any

REFERENCE_RE = re.compile(r"\]\(\s*<?files/([a-z0-9]{12})(?=[/)>\s])")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _id() -> str:
    return "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(12))


def png_size(data: bytes) -> tuple[int | None, int | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None, None


class NotesStub:
    def __init__(self) -> None:
        self.notes: dict[str, dict[str, Any]] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.blobs: dict[str, bytes] = {}
        self.project_of: dict[str, dict[str, str] | None] = {}
        """A chat's real project, by session id; a chat not named here is a plain one, with no Project tab."""
        self.writes: list[tuple[str, str, Any]] = []
        """Every write as (method, path, body), for a check to read back."""

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    # -- seeding --------------------------------------------------------------------------------------

    def seed(self, scope: str, scope_id: str, title: str, body: str = "", *, color: str = "", pinned: bool = False, by: str = "operator", updated_at: str | None = None, note_id: str | None = None) -> dict[str, Any]:
        stamp = updated_at or _now()
        lowest = min((n["position"] for n in self._live(scope, scope_id)), default=1.0)
        note = {
            "id": note_id or _id(), "scope": scope, "scope_id": scope_id, "title": title, "body": body, "color": color, "pinned": pinned,
            "position": lowest - 1.0, "version": 1, "created_at": stamp, "updated_at": stamp, "updated_by": by, "deleted_at": None,
        }
        self.notes[note["id"]] = note
        return copy.deepcopy(note)

    def add_file(self, scope: str, scope_id: str, name: str, data: bytes, mime: str, *, file_id: str | None = None) -> dict[str, Any]:
        width, height = png_size(data)
        file = {"id": file_id or _id(), "scope": scope, "scope_id": scope_id, "name": name, "mime": mime, "size": len(data), "width": width, "height": height, "created_at": _now(), "created_by": "operator"}
        self.files[file["id"]] = file
        self.blobs[file["id"]] = data
        return copy.deepcopy(file)

    def changed_elsewhere(self, note_id: str, *, body: str | None = None, title: str | None = None, by: str = "agent") -> dict[str, Any]:
        note = self.notes[note_id]
        if body is not None:
            note["body"] = body
        if title is not None:
            note["title"] = title
        note["version"] += 1
        note["updated_at"] = _now()
        note["updated_by"] = by
        return copy.deepcopy(note)

    # -- reading --------------------------------------------------------------------------------------

    def _live(self, scope: str, scope_id: str) -> list[dict[str, Any]]:
        return [n for n in self.notes.values() if n["scope"] == scope and n["scope_id"] == scope_id and not n["deleted_at"]]

    def _scope(self, scope: str, scope_id: str) -> dict[str, Any]:
        notes = sorted(self._live(scope, scope_id), key=lambda n: (not n["pinned"], n["position"]))
        files = sorted((f for f in self.files.values() if f["scope"] == scope and f["scope_id"] == scope_id), key=lambda f: f["created_at"], reverse=True)
        return {"notes": copy.deepcopy(notes), "files": copy.deepcopy(files)}

    def listing(self, session_id: str) -> dict[str, Any]:
        project = self.project_of.get(session_id)
        return {
            "session_id": session_id, "project": project, "chat": self._scope("chat", session_id),
            "project_scope": self._scope("project", project["id"]) if project else None,
        }

    def _target(self, session_id: str, scope: str) -> tuple[str, str] | None:
        if scope == "chat":
            return "chat", session_id
        project = self.project_of.get(session_id)
        return ("project", project["id"]) if project else None

    # -- the routes -----------------------------------------------------------------------------------

    def answer(self, method: str, path: str, query: str, body: Any) -> tuple[int, str, str | bytes] | None:
        parts = path.split("/")
        method = method.upper()
        js = lambda status, payload: (status, "application/json", json.dumps(payload))  # noqa: E731
        if len(parts) >= 5 and parts[2] == "sessions" and parts[4] == "notes":
            session_id = parts[3]
            if len(parts) == 5 and method == "GET":
                return js(200, self.listing(session_id))
            if len(parts) == 5 and method == "POST":
                self.writes.append((method, path, body))
                target = self._target(session_id, (body or {}).get("scope", "chat"))
                if target is None:
                    return js(400, {"detail": "this chat is in no project"})
                note = self.seed(*target, (body or {}).get("title", ""), (body or {}).get("body", ""), color=(body or {}).get("color", ""), pinned=bool((body or {}).get("pinned")))
                return js(201, note)
            if len(parts) == 6 and parts[5] == "order" and method == "POST":
                self.writes.append((method, path, body))
                target = self._target(session_id, body["scope"])
                if target is None:
                    return js(400, {"detail": "this chat is in no project"})
                for index, note_id in enumerate(body["ids"]):
                    if note_id in self.notes:
                        self.notes[note_id]["position"] = float(index)
                return js(200, {"notes": self._scope(*target)["notes"]})
            if len(parts) == 6 and parts[5] == "files" and method == "POST":
                return self._upload(session_id, body)
        if len(parts) >= 4 and parts[2] == "notes" and parts[3] == "files":
            file_id = parts[4] if len(parts) > 4 else ""
            found = self.files.get(file_id)
            if found is None:
                return js(404, {"detail": "no such file"})
            if len(parts) == 6 and parts[5] == "download" and method == "GET":
                return 200, found["mime"], self.blobs[file_id]
            if len(parts) == 6 and parts[5] == "references" and method == "GET":
                return js(200, {"notes": self._referencing(file_id)})
            if len(parts) == 5 and method == "DELETE":
                self.writes.append((method, path, query))
                users = self._referencing(file_id)
                if users and "force=true" not in query:
                    return js(409, {"detail": {"message": "notes still show this file", "notes": users}})
                del self.files[file_id]
                return js(200, found)
        if len(parts) >= 4 and parts[2] == "notes":
            note = self.notes.get(parts[3])
            if note is None:
                return js(404, {"detail": "no such note"})
            if len(parts) == 4 and method == "GET":
                return js(404, {"detail": "no such note"}) if note["deleted_at"] else js(200, note)
            if len(parts) == 4 and method == "PATCH":
                self.writes.append((method, path, body))
                return self._patch(note, body or {})
            if len(parts) == 4 and method == "DELETE":
                self.writes.append((method, path, None))
                note["deleted_at"] = _now()
                return js(200, note)
            if len(parts) == 5 and parts[4] == "restore" and method == "POST":
                self.writes.append((method, path, None))
                note["deleted_at"] = None
                return js(200, note)
            if len(parts) == 5 and parts[4] == "duplicate" and method == "POST":
                self.writes.append((method, path, body))
                return js(201, self.seed(note["scope"], note["scope_id"], (body or {}).get("title", ""), note["body"], color=note["color"]))
            if len(parts) == 5 and parts[4] == "move" and method == "POST":
                self.writes.append((method, path, body))
                target = self._target(body["session_id"], body["scope"])
                if target is None:
                    return js(400, {"detail": "this chat is in no project"})
                note["scope"], note["scope_id"] = target
                return js(200, note)
        return None

    def _patch(self, note: dict[str, Any], body: dict[str, Any]) -> tuple[int, str, str]:
        js = lambda status, payload: (status, "application/json", json.dumps(payload))  # noqa: E731
        if "title" in body or "body" in body:
            if body.get("version") != note["version"] and not body.get("force"):
                return js(409, {"detail": {"message": "the note changed elsewhere since it was read", "note": note}})
            changed = False
            for key in ("title", "body"):
                if key in body and body[key] is not None and body[key] != note[key]:
                    note[key] = body[key]
                    changed = True
            if changed:
                note["version"] += 1
                note["updated_at"] = _now()
                note["updated_by"] = "operator"
        if body.get("pinned") is not None:
            note["pinned"] = bool(body["pinned"])
        if body.get("color") is not None:
            note["color"] = body["color"]
        return js(200, note)

    def _referencing(self, file_id: str) -> list[dict[str, Any]]:
        return [{"id": n["id"], "title": n["title"], "scope": n["scope"]} for n in self.notes.values() if not n["deleted_at"] and file_id in REFERENCE_RE.findall(n["body"])]

    def _upload(self, session_id: str, body: Any) -> tuple[int, str, str]:
        if not isinstance(body, dict) or "multipart" not in body:
            return 400, "application/json", json.dumps({"detail": "a multipart form is expected"})
        message = BytesParser(policy=default_policy).parsebytes(b"Content-Type: " + body["content_type"].encode() + b"\r\n\r\n" + body["multipart"])
        scope = "chat"
        made: list[dict[str, Any]] = []
        uploads: list[tuple[str, bytes, str]] = []
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "scope":
                scope = part.get_content().strip()
            elif name == "files":
                uploads.append((part.get_filename() or "file", part.get_payload(decode=True) or b"", part.get_content_type()))
        target = self._target(session_id, scope)
        if target is None:
            return 400, "application/json", json.dumps({"detail": "this chat is in no project"})
        for filename, data, mime in uploads:
            made.append(self.add_file(*target, filename, data, mime))
        self.writes.append(("POST", f"/api/sessions/{session_id}/notes/files", [f["name"] for f in made]))
        return 201, "application/json", json.dumps({"files": made})
