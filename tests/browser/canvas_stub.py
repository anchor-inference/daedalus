"""The canvas routes for the browser checks, answered the way the host's contract says they are.

Saves are operations on single items with the version they were made on. A stale batch is applied
when none of the items it touches changed since that version (``merged``), and refused with 409 and
the canvas as it is now when one did, so the checks exercise the same merge and conflict paths the
real host takes. The stub keeps a version per item to decide that, which is the simplest way to know
what changed since a version without keeping every revision.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

NOW = datetime.now(UTC)


def stamp(**ago: float) -> str:
    return (NOW - timedelta(**ago)).isoformat()


def card(node_id: str, x: int, y: int, text: str, color: str | None = None, width: int = 220, height: int = 80) -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "type": "text", "text": text, "x": x, "y": y, "width": width, "height": height}
    if color:
        node["color"] = color
    return node


def edge(edge_id: str, from_node: str, from_side: str, to_node: str, to_side: str, label: str | None = None) -> dict[str, Any]:
    item = {"id": edge_id, "fromNode": from_node, "fromSide": from_side, "toNode": to_node, "toSide": to_side}
    if label:
        item["label"] = label
    return item


def preview_svg(doc: dict[str, Any]) -> str:
    """A plain sketch: a rectangle per node. Enough for a thumbnail to have something to show."""
    nodes = doc.get("nodes") or []
    if not nodes:
        return ""
    x1 = min(n["x"] for n in nodes)
    y1 = min(n["y"] for n in nodes)
    x2 = max(n["x"] + n["width"] for n in nodes)
    y2 = max(n["y"] + n["height"] for n in nodes)
    shapes = "".join(f'<rect x="{n["x"]}" y="{n["y"]}" width="{n["width"]}" height="{n["height"]}" rx="8" fill="#f4f4f5" stroke="#71717a" stroke-width="2"/>' for n in nodes)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x1 - 16} {y1 - 16} {x2 - x1 + 32} {y2 - y1 + 32}">{shapes}</svg>'


class CanvasStub:
    """Every ``/api/canvases*`` and ``/api/public/canvases/*`` route, over canvases held in memory."""

    def __init__(self, session_id: str, project_id: str = "proj-1", project_name: str = "Website") -> None:
        self.scopes = {"chat": {"kind": "chat", "id": session_id, "name": "Release chat"}, "project": {"kind": "project", "id": project_id, "name": project_name}}
        self.canvases: dict[str, dict[str, Any]] = {}
        self.revisions: dict[str, list[dict[str, Any]]] = {}
        self.item_versions: dict[str, dict[str, int]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.counter = 0

    def add(self, title: str, doc: dict[str, Any], *, scope: str = "chat", source: str = "operator", minutes_ago: float = 5, shared: bool = False, history: list[tuple[dict[str, Any], str, float]] | None = None) -> str:
        self.counter += 1
        canvas_id = f"{self.counter:032x}"
        steps = history or [(doc, source, minutes_ago)]
        self.revisions[canvas_id] = []
        for version, (content, who, ago) in enumerate(steps, start=1):
            self.revisions[canvas_id].insert(0, {"version": version, "title": title, "saved_at": stamp(minutes=ago), "started_at": stamp(minutes=ago), "source": who, "kind": "create" if version == 1 else "edit", "restored_from": None, "summary": {"created": True} if version == 1 else {"added": {"card": 1}, "changed": 1}, "doc": copy.deepcopy(content)})
        last = steps[-1]
        where = self.scopes[scope]
        self.canvases[canvas_id] = {
            "id": canvas_id, "title": title, "version": len(steps), "created_at": stamp(minutes=steps[0][2]), "updated_at": stamp(minutes=last[2]),
            "updated_by": last[1], "share_token": f"token{canvas_id[-4:]}" if shared else "", "doc": copy.deepcopy(last[0]),
            "scope": where["kind"], "scope_id": where["id"], "scope_name": where["name"], "owner_session_id": self.scopes["chat"]["id"], "files": [],
        }
        self.item_versions[canvas_id] = {item["id"]: len(steps) for item in [*last[0]["nodes"], *last[0]["edges"]]}
        return canvas_id

    def view(self, found: dict[str, Any]) -> dict[str, Any]:
        return {**self.item(found), "doc": found["doc"], "share_token": found["share_token"], "files": found["files"]}

    def item(self, found: dict[str, Any]) -> dict[str, Any]:
        keys = ("id", "title", "version", "created_at", "updated_at", "updated_by", "scope", "scope_id", "scope_name", "owner_session_id")
        return {key: found[key] for key in keys} | {"shared": bool(found["share_token"]), "nodes": len(found["doc"]["nodes"]), "edges": len(found["doc"]["edges"])}

    def head(self, found: dict[str, Any]) -> dict[str, Any]:
        return {key: found[key] for key in ("id", "title", "version", "updated_at", "updated_by")}

    def apply(self, doc: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
        nodes, edges = list(doc["nodes"]), list(doc["edges"])
        for op in ops:
            if op["op"] == "delete":
                if any(node["id"] == op["id"] for node in nodes):
                    nodes = [node for node in nodes if node["id"] != op["id"]]
                    edges = [e for e in edges if op["id"] not in (e["fromNode"], e["toNode"])]
                else:
                    edges = [e for e in edges if e["id"] != op["id"]]
                continue
            target = nodes if op["kind"] == "node" else edges
            item = op["item"]
            index = next((i for i, existing in enumerate(target) if existing["id"] == item["id"]), None)
            if index is not None and not op.get("at"):
                target[index] = item
            else:
                if index is not None:
                    target.pop(index)
                target.insert(0, item) if op.get("at") == "back" else target.append(item)
        ids = {node["id"] for node in nodes}
        for e in edges:
            if e["fromNode"] not in ids or e["toNode"] not in ids:
                raise ValueError(f"edge {e['id']} names a node that is not on the canvas")
        return {**doc, "nodes": nodes, "edges": edges}

    def agent_edit(self, canvas_id: str, ops: list[dict[str, Any]]) -> int:
        """What the agent's tool does to a canvas the page has open."""
        found = self.canvases[canvas_id]
        return self._save(found, ops, "agent")

    def _save(self, found: dict[str, Any], ops: list[dict[str, Any]], source: str, title: str | None = None, kind: str = "edit") -> int:
        doc = self.apply(found["doc"], ops)
        found.update(doc=doc, version=found["version"] + 1, updated_at=datetime.now(UTC).isoformat(), updated_by=source, title=title or found["title"])
        for op in ops:
            self.item_versions[found["id"]][op["id"] if op["op"] == "delete" else op["item"]["id"]] = found["version"]
        self.revisions[found["id"]].insert(0, {"version": found["version"], "title": found["title"], "saved_at": found["updated_at"], "started_at": found["updated_at"], "source": source, "kind": kind, "restored_from": None, "summary": {"changed": len(ops)}, "doc": copy.deepcopy(doc)})
        return found["version"]

    def answer(self, method: str, url: str, body: dict[str, Any] | None) -> tuple[int, Any] | None:
        parts = urlsplit(url)
        path = parts.path[parts.path.index("/api/"):]
        segments = path.strip("/").split("/")
        body = body or {}
        if segments[:3] == ["api", "public", "canvases"] and len(segments) == 4 and method == "GET":
            found = next((item for item in self.canvases.values() if item["share_token"] and item["share_token"] == segments[3]), None)
            return (200, {"title": found["title"], "version": found["version"], "updated_at": found["updated_at"], "doc": found["doc"], "files": []}) if found else (404, {"detail": "no such shared canvas"})
        if segments[:2] != ["api", "canvases"]:
            return None
        self.calls.append((method, path, body))
        if len(segments) == 2:
            if method == "GET":
                query = parse_qs(parts.query)
                rows = sorted(self.canvases.values(), key=lambda item: item["updated_at"], reverse=True)
                if query.get("project_id"):
                    scopes = [self.scopes["project"]]
                    rows = [row for row in rows if row["scope"] == "project"]
                elif query.get("session_id"):
                    scopes = [self.scopes["chat"], self.scopes["project"]]
                else:
                    scopes = [scope for scope in self.scopes.values() if any(row["scope"] == scope["kind"] for row in rows)]
                return 200, {"scopes": scopes, "items": [self.item(row) for row in rows]}
            if method == "POST":
                canvas_id = self.add(body["title"], body.get("doc") or {"nodes": [], "edges": []}, scope=body["scope"], minutes_ago=0)
                return 201, self.view(self.canvases[canvas_id])
        if segments[2] == "import" and method == "POST":
            try:
                doc = json.loads(body["text"])
                assert isinstance(doc.get("nodes", []), list)
            except (ValueError, AssertionError):
                return 400, {"detail": "this file is not a JSON Canvas"}
            canvas_id = self.add(body["title"], {"nodes": doc.get("nodes", []), "edges": doc.get("edges", [])}, scope=body["scope"], minutes_ago=0)
            return 201, self.view(self.canvases[canvas_id])
        found = self.canvases.get(segments[2])
        if found is None:
            return 404, {"detail": "no such canvas"}
        rest = segments[3:]
        if not rest:
            if method == "GET":
                return 200, self.view(found)
            if method == "PATCH":
                if body.get("version") != found["version"]:
                    return 409, {"detail": "the canvas changed elsewhere"}
                self._save(found, [], "operator", body["title"])
                return 200, self.head(found)
            if method == "DELETE":
                del self.canvases[found["id"]]
                return 200, {"ok": True}
        if rest == ["ops"] and method == "POST":
            base = int(body["base_version"])
            touched = {op["id"] if op["op"] == "delete" else op["item"]["id"] for op in body["ops"]}
            versions = self.item_versions[found["id"]]
            conflicts = sorted(item for item in touched if versions.get(item, 0) > base)
            if base != found["version"] and conflicts:
                return 409, {"detail": {"message": "the canvas changed elsewhere", "conflicts": conflicts, "current": self.view(found)}}
            try:
                self._save(found, body["ops"], "operator", body.get("title"))
            except ValueError as exc:
                return 400, {"detail": str(exc)}
            return 200, {"canvas": self.view(found), "merged": base != found["version"] - 1}
        if rest == ["head"] and method == "GET":
            return 200, self.head(found)
        if rest == ["preview"] and method == "GET":
            return 200, {"version": found["version"], "svg": preview_svg(found["doc"])}
        if rest == ["versions"] and method == "GET":
            return 200, [{key: value for key, value in item.items() if key != "doc"} for item in self.revisions[found["id"]]]
        if len(rest) >= 2 and rest[0] == "versions" and method == "GET":
            revision = next((item for item in self.revisions[found["id"]] if item["version"] == int(rest[1])), None)
            if revision is None:
                return 404, {"detail": "no such version"}
            if rest[2:] == ["preview"]:
                return 200, {"version": revision["version"], "svg": preview_svg(revision["doc"])}
            return 200, revision
        if rest == ["restore"] and method == "POST":
            if body.get("version") != found["version"]:
                return 409, {"detail": "the canvas changed elsewhere"}
            revision = next(item for item in self.revisions[found["id"]] if item["version"] == body["revision"])
            ops = [{"op": "delete", "id": node["id"]} for node in found["doc"]["nodes"]]
            ops += [{"op": "put", "kind": "node", "item": node} for node in revision["doc"]["nodes"]]
            ops += [{"op": "put", "kind": "edge", "item": e} for e in revision["doc"]["edges"]]
            self._save(found, ops, "operator", kind="restore")
            return 200, self.view(found)
        if rest == ["duplicate"] and method == "POST":
            made = self.add(body.get("title") or f"{found['title']} (copy)", found["doc"], scope=found["scope"], minutes_ago=0)
            return 201, self.view(self.canvases[made])
        if rest == ["move"] and method == "POST":
            where = self.scopes[body["scope"]]
            found.update(scope=where["kind"], scope_id=where["id"], scope_name=where["name"])
            return 200, self.view(found)
        if rest == ["export"] and method == "GET":
            return 200, found["doc"]
        if rest == ["share"]:
            if method == "POST":
                found["share_token"] = found["share_token"] or f"token{found['id'][-4:]}"
                return 200, {"url": f"/app/d/{found['share_token']}"}
            if method == "DELETE":
                found["share_token"] = ""
                return 200, {"ok": True}
        return 404, {"detail": "Not Found"}

    def handle(self, route) -> bool:  # type: ignore[no-untyped-def]
        """Answer a Playwright route when it is a canvas route; ``False`` leaves it to the caller."""
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
