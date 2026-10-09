"""The folder browser's API over an invented tree, for the browser checks and the screenshots.

``/api/folders``, ``/api/folders/check``, ``/api/folders/mkdir`` and ``/api/folders/favourites`` in
the shapes ``daedalus/extensions/api_folder_browser.py`` answers, over two small trees: the
container's and the machine's. A check sets ``host`` to ``"down"``, ``"missing"`` or ``"outdated"``
to draw the host side failing, and reads ``made`` and ``favourites`` back.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import parse_qs

NOW = time.time()
DAY = 86400.0

WORKSPACES = "/data/workspaces"
CONTAINER_HOME = "/home/you"
HOST_HOME = "/home/operator"


def _folder(age_days: float, *, git: bool = False, writable: bool = True, project: tuple[str, str] | None = None,
            kind: str = "folder", chat: str = "") -> dict[str, Any]:
    entry: dict[str, Any] = {"mtime": NOW - age_days * DAY, "writable": writable, "readable": True, "is_git": git, "link": False,
                             "kind": kind, "project": {"id": project[0], "name": project[1]} if project else None}
    if chat:
        entry["chat_title"] = chat
    return entry


def container_tree() -> dict[str, dict[str, dict[str, Any]]]:
    return {
        "/srv": {"projects": _folder(1), "media": _folder(9)},
        "/srv/projects": {
            "esp32-door": _folder(2, git=True),
            "home-assistant": _folder(1, git=True, project=("p-home", "Smart home")),
            "landing-cafe": _folder(0.2, project=("p-cafe", "Cafe landing")),
            "notes-archive": _folder(21, writable=False),
            "scripts": _folder(6),
            "zigbee2mqtt-backup": _folder(60, git=True),
        },
        "/srv/projects/esp32-door": {}, "/srv/projects/scripts": {}, "/srv/projects/zigbee2mqtt-backup": {},
        "/srv/projects/notes-archive": {}, "/srv/media": {},
        WORKSPACES: {
            "smart-home": _folder(0.003, project=("p-home2", "Smart home")),
            "photo-archive": _folder(14),
            "102f8d2d82c2": _folder(0.012, project=("c1", "chat"), kind="chat", chat="Reading the September statement"),
            "62d62b5f668d": _folder(0.05, project=("c2", "chat"), kind="chat", chat="Why the docker build fails"),
            "228b7df7caa5": _folder(0.12, project=("c3", "chat"), kind="chat", chat="Translating the KV cache article"),
            "voice": _folder(0.12, project=("p-voice", "Voice"), kind="service"),
            "main": _folder(0.5, project=("p-main", "Main orchestrator"), kind="service"),
        },
        "/data/workspaces/photo-archive": {},
        CONTAINER_HOME: {},
    }


def host_tree() -> dict[str, dict[str, dict[str, Any]]]:
    return {
        HOST_HOME: {"projects": _folder(0.5), "Documents": _folder(3)},
        f"{HOST_HOME}/projects": {
            "esp32-door": _folder(2, git=True),
            "smart-home": _folder(0.003, git=True, project=("p-home", "Smart home")),
            "stream-cuts": _folder(1),
            "thesis-latex": _folder(90, git=True),
        },
        f"{HOST_HOME}/projects/esp32-door": {}, f"{HOST_HOME}/projects/stream-cuts": {},
        f"{HOST_HOME}/projects/thesis-latex": {}, f"{HOST_HOME}/Documents": {},
        "/mnt/nas/photos": {},
    }


def crumbs(path: str, home: str, anchor: str = "") -> list[dict[str, str]]:
    start = anchor or (home if path == home or path.startswith(home + "/") else "/")
    out = [{"name": "~" if start == home and not anchor else (start.rsplit("/", 1)[-1] or "/"), "path": start}]
    rest = path[len(start):].strip("/")
    current = start
    for part in [p for p in rest.split("/") if p]:
        current = f"{current.rstrip('/')}/{part}"
        out.append({"name": part, "path": current})
    return out


class FolderStub:
    """The folder browser's routes. ``host``: ``"up"``, ``"down"``, ``"missing"`` (no bridge
    installed) or ``"outdated"`` (a daemon without ``fs.browse``)."""

    def __init__(self, *, host: str = "down", native: bool = False) -> None:
        self.host = host
        self.native = native
        self.trees = {"container": container_tree(), "host": host_tree()}
        self.favourites: list[dict[str, str]] = [{"env": "container", "path": "/srv/projects"}, {"env": "container", "path": "/srv/media"},
                                                 {"env": "host", "path": f"{HOST_HOME}/projects"}, {"env": "host", "path": f"{HOST_HOME}/Documents"}]
        self.made: list[dict[str, str]] = []
        self.calls: list[tuple[str, str]] = []

    def environments(self) -> dict[str, Any]:
        if self.native:
            return {"local": "host", "available": ["host"], "host_bridge": False, "docker": False, "host_configured": False,
                    "workspaces_root": f"{HOST_HOME}/.daedalus/workspaces", "home": HOST_HOME}
        return {"local": "container", "available": ["container", "host"] if self.host == "up" else ["container"],
                "host_bridge": self.host == "up", "docker": True, "host_configured": self.host in ("up", "down", "outdated"),
                "workspaces_root": WORKSPACES, "home": CONTAINER_HOME}

    def _favourites(self, env: str = "") -> list[dict[str, str]]:
        return [{**row, "name": row["path"].rsplit("/", 1)[-1]} for row in self.favourites if not env or row["env"] == env]

    def _host_failure(self) -> tuple[int, dict[str, Any]] | None:
        if self.host in ("down", "missing"):
            return 503, {"detail": {"code": "host_down", "configured": self.host == "down", "message": "the host terminal bridge is not available",
                                    "start": "systemctl --user start daedalus-ptyd", "install": "bash deploy/host-terminal.sh install"}}
        if self.host == "outdated":
            return 501, {"detail": {"code": "host_outdated", "message": "the host terminal daemon is older than this version and cannot browse folders",
                                    "install": "bash deploy/host-terminal.sh install"}}
        return None

    def listing(self, env: str, path: str) -> tuple[int, dict[str, Any]]:
        if self.native:
            env = "host"
        if env == "host" and not self.native:
            failure = self._host_failure()
            if failure:
                return failure
        tree = self.trees[env]
        home = HOST_HOME if env == "host" else CONTAINER_HOME
        path = (path or home).rstrip("/") or "/"
        if path.startswith("~"):
            path = home + path[1:]
        if path not in tree:
            on_host = path in self.trees["host"] if env == "container" else None
            return 404, {"detail": {"code": "missing", "message": f"{path} does not exist here", "mount": env == "container", "on_host": on_host}}
        entries = [{"name": name, "path": f"{path.rstrip('/')}/{name}", **meta} for name, meta in sorted(tree[path].items(), key=lambda kv: kv[0].lower())]
        anchor = "" if env == "host" else next((root for root in ("/srv", WORKSPACES, CONTAINER_HOME) if path == root or path.startswith(root + "/")), "")
        parent = None if path in (anchor, "/") else path.rsplit("/", 1)[0] or "/"
        roots = ([{"name": "operator", "path": HOST_HOME, "kind": "home"}, {"name": "D:", "path": "/mnt/data", "kind": "volume"}] if env == "host"
                 else [{"name": "you", "path": CONTAINER_HOME, "kind": "home"}, {"name": "workspaces", "path": WORKSPACES, "kind": "workspaces"},
                       {"name": "srv", "path": "/srv", "kind": "volume"}])
        recent = ([{"name": "smart-home", "path": f"{HOST_HOME}/projects/smart-home"}] if env == "host"
                  else [{"name": "esp32-door", "path": "/srv/projects/esp32-door"}])
        here = {"kind": "folder", "project": None}
        return 200, {"env": env, "path": path, "parent": parent, "home": home, "writable": True, "is_git": path.endswith("esp32-door"),
                     "crumbs": crumbs(path, home, anchor), "entries": entries, "truncated": False, "roots": roots,
                     "recent": recent, "favourites": self._favourites(), "here": here}

    def check(self, env: str, path: str) -> tuple[int, dict[str, Any]]:
        if env == "host" and not self.native:
            failure = self._host_failure()
            if failure:
                return failure
        tree = self.trees["host" if self.native else env]
        parent, _, name = path.rpartition("/")
        meta = tree.get(parent, {}).get(name)
        exists = path in tree or meta is not None
        return 200, {"env": env, "path": path, "exists": exists, "is_dir": exists, "writable": True if exists else None,
                     "is_git": bool(meta and meta.get("is_git")), "mount": env == "container" and not exists and not self.native,
                     "on_host": path in self.trees["host"] if env == "container" else None,
                     "kind": (meta or {}).get("kind", "folder"), "project": (meta or {}).get("project")}

    def answer(self, method: str, path: str, query: str, body: Any) -> tuple[int, dict[str, Any]] | None:
        params = {key: values[0] for key, values in parse_qs(query).items()}
        env = params.get("env") or ("host" if self.native else "container")
        if method == "GET" and path == "/api/folders":
            self.calls.append((env, params.get("path", "")))
            return self.listing(env, params.get("path", ""))
        if method == "GET" and path == "/api/folders/check":
            return self.check(env, params.get("path", ""))
        if method == "POST" and path == "/api/folders/mkdir":
            target = f"{body['path'].rstrip('/')}/{body['name']}"
            tree = self.trees[body["env"]]
            if body["name"] in tree.get(body["path"], {}):
                return 409, {"detail": {"code": "exists", "message": f"{target} already exists"}}
            tree.setdefault(body["path"], {})[body["name"]] = _folder(0)
            tree[target] = {}
            self.made.append({"env": body["env"], "path": target})
            return 200, {"env": body["env"], "path": target, "name": body["name"]}
        if method == "PUT" and path == "/api/folders/favourites":
            self.favourites = [row for row in self.favourites if not (row["env"] == body["env"] and row["path"] == body["path"])]
            if body["on"]:
                self.favourites.insert(0, {"env": body["env"], "path": body["path"]})
            return 200, {"favourites": self._favourites()}
        if method == "GET" and path == "/api/project-environments":
            return 200, self.environments()
        return None

    def fulfil(self, route) -> bool:  # type: ignore[no-untyped-def]
        """Answer a Playwright route when it is one of these; ``True`` when it was."""
        request = route.request
        url = request.url
        query = url.split("?", 1)[1] if "?" in url else ""
        path = url.split("?", 1)[0]
        path = path[path.index("/api/"):] if "/api/" in path else path
        body = request.post_data_json if request.post_data and request.method in ("POST", "PUT") else None
        answered = self.answer(request.method, path, query, body)
        if answered is None:
            return False
        status, payload = answered
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        return True


DEFAULT = FolderStub()
"""What every harness answers when it does not install its own: a Docker installation whose host
bridge is installed but stopped, over the invented trees."""
