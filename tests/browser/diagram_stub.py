"""The diagram routes for the browser checks, answered the way the host answers them.

The history behaves like the store's: a save carries the version it was made on and a stale one is
refused with 409, every save writes a revision with its source and a change summary, and a restore is
a new revision. Summaries and thumbnails come from the host's own functions, so the cards and the
history show what the real server would send rather than a picture invented for the test.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from daedalus.stores.diagram_preview import preview_svg  # noqa: E402
from daedalus.stores.diagrams import change_summary  # noqa: E402

NOW = datetime.now(UTC)


def stamp(**ago: float) -> str:
    return (NOW - timedelta(**ago)).isoformat()


class DiagramStub:
    """Every ``/api/diagrams*`` and ``/api/public/diagrams/*`` route, over diagrams held in memory."""

    def __init__(self) -> None:
        self.diagrams: dict[str, dict[str, Any]] = {}
        self.revisions: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.counter = 0

    def add(self, title: str, scene: dict[str, Any], *, source: str = "user", minutes_ago: float = 5, shared: bool = False, history: list[tuple[dict[str, Any], str, float]] | None = None) -> str:
        """A diagram with a history: ``history`` is (scene, source, minutes ago) oldest first, and the
        current scene is the last revision."""
        self.counter += 1
        diagram_id = f"{self.counter:032x}"
        steps = history or [(scene, source, minutes_ago)]
        self.revisions[diagram_id] = []
        previous: dict[str, Any] | None = None
        for version, (content, who, ago) in enumerate(steps, start=1):
            self.revisions[diagram_id].insert(0, self._revision(version, title, content, who, stamp(minutes=ago), "create" if version == 1 else "edit", previous, title))
            previous = content
        last = steps[-1]
        self.diagrams[diagram_id] = {
            "id": diagram_id, "title": title, "version": len(steps), "created_at": stamp(minutes=steps[0][2]), "updated_at": stamp(minutes=last[2]),
            "updated_by": last[1], "share_token": f"token-{diagram_id[-4:]}" if shared else "", "scene": last[0],
        }
        return diagram_id

    def _revision(self, version: int, title: str, scene: dict[str, Any], source: str, at: str, kind: str, before: dict[str, Any] | None, before_title: str | None, restored_from: int | None = None) -> dict[str, Any]:
        summary = change_summary(before, before_title, scene, title)
        if restored_from:
            summary["restored_from"] = restored_from
        return {"version": version, "title": title, "saved_at": at, "started_at": at, "source": source, "kind": kind, "restored_from": restored_from, "summary": summary, "scene": scene}

    def agent_edit(self, diagram_id: str, scene: dict[str, Any], title: str | None = None) -> int:
        """What the agent's tool does to a diagram the page has open."""
        found = self.diagrams[diagram_id]
        return self._save(found, title or found["title"], scene, "agent")

    def _save(self, found: dict[str, Any], title: str, scene: dict[str, Any], source: str, kind: str = "edit", restored_from: int | None = None) -> int:
        before = self.revisions[found["id"]][0]
        found.update(title=title, scene=scene, version=found["version"] + 1, updated_at=datetime.now(UTC).isoformat(), updated_by=source)
        self.revisions[found["id"]].insert(0, self._revision(found["version"], title, scene, source, found["updated_at"], kind, before["scene"], before["title"], restored_from))
        return found["version"]

    def listing(self, item: dict[str, Any]) -> dict[str, Any]:
        return {key: item[key] for key in ("id", "title", "version", "created_at", "updated_at", "updated_by")} | {"shared": bool(item["share_token"])}

    def answer(self, method: str, url: str, body: dict[str, Any] | None) -> tuple[int, Any] | None:
        parts = urlsplit(url)
        path = parts.path[parts.path.index("/api/"):]
        segments = path.strip("/").split("/")
        body = body or {}
        if segments[:2] == ["api", "public"] and len(segments) == 4 and segments[2] == "diagrams" and method == "GET":
            found = next((item for item in self.diagrams.values() if item["share_token"] and item["share_token"] == segments[3]), None)
            return (200, {"title": found["title"], "version": found["version"], "updated_at": found["updated_at"], "scene": found["scene"]}) if found else (404, {"detail": "no such shared diagram"})
        if segments[:2] != ["api", "diagrams"]:
            return None
        self.calls.append((method, path, body))
        if len(segments) == 2:
            if method == "GET":
                rows = sorted(self.diagrams.values(), key=lambda item: item["updated_at"], reverse=True)
                session = parse_qs(parts.query).get("session_id", [None])[0]
                if session:
                    rows = rows[:1]
                return 200, [self.listing(item) for item in rows]
            if method == "POST":
                diagram_id = self.add(body["title"], body.get("scene") or {"elements": [], "appState": {}, "files": {}}, minutes_ago=0)
                return 201, self.diagrams[diagram_id]
        found = self.diagrams.get(segments[2])
        if found is None:
            return 404, {"detail": "no such diagram"}
        rest = segments[3:]
        if not rest:
            if method == "GET":
                return 200, found
            if method == "PUT":
                if body.get("version") != found["version"]:
                    return 409, {"detail": "the diagram changed elsewhere; reload before saving"}
                self._save(found, body["title"], body["scene"], "user")
                return 200, found
            if method == "PATCH":
                if body.get("version") != found["version"]:
                    return 409, {"detail": "the diagram changed elsewhere; reload before saving"}
                self._save(found, body["title"], found["scene"], "user")
                return 200, {key: found[key] for key in ("id", "title", "version", "updated_at", "updated_by")}
            if method == "DELETE":
                del self.diagrams[found["id"]]
                return 200, {"ok": True}
        if rest == ["head"] and method == "GET":
            return 200, {key: found[key] for key in ("id", "title", "version", "updated_at", "updated_by")}
        if rest == ["preview"] and method == "GET":
            return 200, {"version": found["version"], "svg": preview_svg(found["scene"])}
        if rest == ["versions"] and method == "GET":
            return 200, [{key: value for key, value in item.items() if key != "scene"} for item in self.revisions[found["id"]]]
        if len(rest) >= 2 and rest[0] == "versions" and method == "GET":
            revision = next((item for item in self.revisions[found["id"]] if item["version"] == int(rest[1])), None)
            if revision is None:
                return 404, {"detail": "no such diagram version"}
            if rest[2:] == ["preview"]:
                return 200, {"version": revision["version"], "svg": preview_svg(revision["scene"])}
            return 200, revision
        if rest == ["restore"] and method == "POST":
            if body.get("version") != found["version"]:
                return 409, {"detail": "the diagram changed elsewhere; reload before saving"}
            revision = next(item for item in self.revisions[found["id"]] if item["version"] == body["revision"])
            self._save(found, revision["title"], revision["scene"], "user", "restore", revision["version"])
            return 200, found
        if rest == ["duplicate"] and method == "POST":
            copy = self.add(body.get("title") or f"{found['title']} (copy)", found["scene"], minutes_ago=0)
            return 201, self.diagrams[copy]
        if rest == ["share"]:
            if method == "POST":
                found["share_token"] = found["share_token"] or f"token-{found['id'][-4:]}"
                return 200, {"url": f"/app/d/{found['share_token']}"}
            if method == "DELETE":
                found["share_token"] = ""
                return 200, {"ok": True}
        return 404, {"detail": "Not Found"}

    def handle(self, route) -> bool:  # type: ignore[no-untyped-def]
        """Answer a Playwright route when it is a diagram route; ``False`` leaves it to the caller."""
        request = route.request
        body = None
        if request.method in ("POST", "PUT", "PATCH") and request.post_data:
            body = json.loads(request.post_data)
        answered = self.answer(request.method, request.url, body)
        if answered is None:
            return False
        status, value = answered
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))
        return True
