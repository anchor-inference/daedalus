"""The session importer's API over invented transcripts, for the browser checks and the screenshots.

``/api/imports/*`` and the two ``/api/sessions/{id}/import/...`` routes in the shapes
``daedalus/extensions`` answers when a conversation from Claude Code, Codex or another agent program
is brought in. A check sets ``host`` to ``"down"`` or ``"outdated"`` to draw the host side failing,
and reads ``started`` (the bodies of the imports it began) and ``refreshed`` (the sessions it
refreshed) back.

The transcripts are chosen so every branch of the screen has one: a session that is live, one so
large the importer suggests keeping only the tail, one imported already, one whose folder is gone,
and destinations that join a project, make one, open a chat, or are refused.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

NOW = time.time()
HOUR = 3600.0
DAY = 86400.0

HOME = "/home/operator"
PROJECTS = f"{HOME}/projects"
SMART_HOME = {"id": "p-home", "name": "Smart home", "kind": "project"}

STAGES = ["read", "parse", "mask", "write", "index", "summarise", "open"]

HARNESSES = [
    {"id": "claude", "name": "Claude Code", "found": True, "root": "~/.claude/projects", "sessions": 106, "folders": 27, "version": "2.4.1"},
    {"id": "codex", "name": "Codex", "found": True, "root": "~/.codex/sessions", "sessions": 4307, "folders": 41, "version": "0.160.0"},
    {"id": "gemini", "name": "Gemini CLI", "found": False, "root": "~/.gemini/tmp", "sessions": 0, "folders": 0, "version": ""},
    {"id": "opencode", "name": "OpenCode", "found": False, "root": "~/.local/share/opencode", "sessions": 0, "folders": 0, "version": ""},
    {"id": "grok", "name": "Grok CLI", "found": False, "root": "~/.grok/sessions", "sessions": 0, "folders": 0, "version": ""},
    {"id": "pi", "name": "Pi", "found": False, "root": "~/.pi/agent/sessions", "sessions": 0, "folders": 0, "version": ""},
    {"id": "cursor", "name": "Cursor", "found": False, "root": "~/.cursor/chats", "sessions": 0, "folders": 0, "version": ""},
]
NAMES = {row["id"]: row["name"] for row in HARNESSES}

# Folders on the machine that hold no session at all, so the browser draws them greyed out.
EMPTY_FOLDERS = [f"{PROJECTS}/stream-cuts", f"{HOME}/Documents"]
# A folder a session remembers but that has since been deleted: it is listed by what the sessions
# say, and the destination check is what finds that it is gone.
GONE = f"{PROJECTS}/greenhouse-proto"

OPUS = "claude-opus-4-1-20250805"
CODEX = "gpt-5-codex"


def _session(harness: str, sid: str, cwd: str, title: str, age_hours: float, *, messages: int, size: int, model: str, branch: str = "main",
             compacted: int = 0, sidechains: int = 0, live: bool = False, imported: tuple[str, str] | None = None,
             destination: str = "project", code: str | None = None) -> dict[str, Any]:
    return {"harness": harness, "id": sid, "cwd": cwd, "title": title, "age": age_hours, "messages": messages, "bytes": size,
            "model": model, "branch": branch, "compacted": compacted, "sidechains": sidechains, "live": live, "imported": imported,
            "destination": destination, "code": code}


SESSIONS = [
    _session("claude", "7d1c2e0a-4b5f-4c7e-9a1d-2f3e4a5b6c7d", f"{PROJECTS}/smart-home", "Fix the flaky door sensor test", 3,
             messages=148, size=942113, model=OPUS, compacted=1, sidechains=2),
    _session("claude", "3b8e51f6-92c4-4d0a-b7e3-6a1f0c9d2e48", f"{PROJECTS}/smart-home", "Add presence detection to the hallway automation", 0.2,
             messages=64, size=388120, model=OPUS, live=True),
    _session("claude", "a94c07d2-18be-4f63-8c5a-e07b3d916f25", f"{PROJECTS}/esp32-door", "Port the firmware to C3", 30,
             messages=4120, size=61_400_000, model=OPUS, branch="c3-port", compacted=6, sidechains=11, destination="new_project"),
    _session("claude", "e5f2b1c8-6d37-4a90-93be-4c8a7d1f0b36", f"{PROJECTS}/thesis-latex", "Rewrite the related work chapter", 200,
             messages=96, size=512004, model=OPUS, imported=("s-imp1", "Rewrite the related work chapter"), destination="new_chat"),
    _session("claude", "0c6d93ae-5f21-48b7-a2e4-b91d7f3c8a50", GONE, "Prototype the greenhouse controller", 700,
             messages=37, size=204880, model=OPUS, destination="refused", code="missing"),
    _session("codex", "0199f3a2-7b41-7c3e-8d52-1a9e4c6b0f27", f"{PROJECTS}/smart-home", "Refactor the MQTT topic naming", 26,
             messages=212, size=1_780_300, model=CODEX),
    _session("codex", "0199f5c8-02d9-7a14-b36e-9d4f1e8a7c05", f"{PROJECTS}/esp32-door", "Debounce the reed switch in the interrupt handler", 52,
             messages=88, size=640221, model=CODEX, destination="new_project"),
    _session("codex", "0199e8b4-6a3f-7d02-9c71-5e2b0a4d8f19", HOME, "Explain the rsync flags for a nightly backup", 120,
             messages=22, size=96410, model=CODEX, branch="", destination="new_chat"),
    _session("codex", "0199d471-c8e5-7b36-a4f0-3d9e6b1c2a87", f"{PROJECTS}/thesis-latex", "Fix the bibliography style", 400,
             messages=51, size=310977, model=CODEX, destination="new_chat"),
]


def _iso(age_hours: float) -> str:
    return datetime.fromtimestamp(NOW - age_hours * HOUR, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _project_for(cwd: str) -> dict[str, str] | None:
    return dict(SMART_HOME) if cwd == f"{PROJECTS}/smart-home" else None


def _source_path(row: dict[str, Any]) -> str:
    if row["harness"] == "claude":
        return f"~/.claude/projects/{row['cwd'].replace('/', '-')}/{row['id']}.jsonl"
    stamp = datetime.fromtimestamp(NOW - row["age"] * HOUR, UTC)
    return f"~/.codex/sessions/{stamp:%Y/%m/%d}/rollout-{stamp:%Y-%m-%dT%H-%M-%S}-{row['id']}.jsonl"


def header(row: dict[str, Any]) -> dict[str, Any]:
    imported = {"session_id": row["imported"][0], "title": row["imported"][1]} if row["imported"] else None
    return {"harness": row["harness"], "id": row["id"], "cwd": row["cwd"], "title": row["title"],
            "started_at": _iso(row["age"] + max(1.0, row["messages"] / 40)), "updated_at": _iso(row["age"]),
            "messages": row["messages"], "bytes": row["bytes"], "branch": row["branch"], "model": row["model"],
            "flags": {"compacted": row["compacted"], "sidechains": row["sidechains"], "live": row["live"],
                      "imported_as": imported["session_id"] if imported else None},
            "source": {"path": _source_path(row)}, "imported_as": imported, "project": _project_for(row["cwd"])}


def _dirs(harness: str) -> set[str]:
    """Every folder the browser can step into: those holding sessions, their ancestors and the empty ones."""
    found: set[str] = set(EMPTY_FOLDERS)
    for row in SESSIONS:
        if row["harness"] != harness:
            continue
        current = row["cwd"]
        while current.startswith(HOME):
            found.add(current)
            current = current.rsplit("/", 1)[0]
    found.discard(HOME)
    found.add(PROJECTS)
    found.add(f"{HOME}/Documents")
    return found


def _crumbs(path: str) -> list[dict[str, str]]:
    out = [{"name": "~", "path": HOME}]
    current = HOME
    for part in [p for p in path[len(HOME):].split("/") if p]:
        current = f"{current}/{part}"
        out.append({"name": part, "path": current})
    return out


def _destination(row: dict[str, Any]) -> dict[str, Any]:
    kind = row["destination"]
    project = {"id": SMART_HOME["id"], "name": SMART_HOME["name"], "ephemeral": False} if kind == "project" else None
    problem = f"{row['cwd']} no longer exists on this machine" if row["code"] == "missing" else None
    return {"kind": kind, "cwd": row["cwd"], "project": project, "folder_id": "f-smart-home" if kind == "project" else None,
            "worktree_cwd": None, "code": row["code"], "problem": problem, "other_project": None,
            "exists": row["code"] != "missing", "becomes_project": kind == "new_project"}


def _excerpt(row: dict[str, Any]) -> list[dict[str, str]]:
    title = row["title"][0].lower() + row["title"][1:]
    return [
        {"role": "user", "text": f"I would like to {title}. Start by reading how it is set up today.", "at": _iso(row["age"] + 2)},
        {"role": "assistant", "text": "I will look at the current layout first, then propose a change before editing anything.", "at": _iso(row["age"] + 1.9)},
    ]


def _tail(row: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "user", "text": "Run the tests once more and tell me what is left.", "at": _iso(row["age"] + 0.2)},
        {"role": "assistant", "text": "Everything passes. The one open item is the retry budget, which I left as it was.", "at": _iso(row["age"] + 0.1)},
        {"role": "user", "text": "Good, that is enough for now.", "at": _iso(row["age"])},
    ]


def _job(state: dict[str, Any]) -> dict[str, Any]:
    done = state["state"] == "done"
    messages = state["messages"]
    progress = (state["step"] + 1) / len(STAGES)
    return {"job_id": state["job_id"], "harness": state["harness"], "id": state["id"], "state": state["state"],
            "stage": STAGES[state["step"]], "stages": list(STAGES),
            "counts": {"turns_read": int(messages * 1.07), "messages": messages if state["step"] >= 1 else 0,
                       "written": messages if done else int(messages * progress * (state["step"] >= 3)), "masked": 3 if state["step"] >= 2 else 0},
            "session_id": "s-imported" if done else None, "error": state.get("error"), "started_at": state["started_at"],
            "finished_at": state["finished_at"]}


class ImportStub:
    """The importer's routes. ``host``: ``"up"``, ``"down"`` (the bridge is stopped) or ``"outdated"``
    (a daemon that cannot read other programs' transcripts yet)."""

    def __init__(self, *, host: str = "up") -> None:
        self.host = host
        self.started: list[dict[str, Any]] = []
        self.refreshed: list[str] = []
        self.jobs: dict[str, dict[str, Any]] = {}

    def _host_failure(self) -> tuple[int, dict[str, Any]] | None:
        if self.host == "down":
            return 503, {"detail": {"code": "host_down", "configured": True, "message": "the host terminal bridge is not available",
                                    "start": "systemctl --user start daedalus-ptyd", "install": "bash deploy/host-terminal.sh install"}}
        if self.host == "outdated":
            return 501, {"detail": {"code": "host_outdated", "message": "the host terminal daemon is older than this version and cannot read other programs' sessions",
                                    "install": "bash deploy/host-terminal.sh install"}}
        return None

    def _find(self, harness: str, sid: str) -> dict[str, Any] | None:
        return next((row for row in SESSIONS if row["harness"] == harness and row["id"] == sid), None)

    def scan(self, params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        harness, path = params.get("harness", ""), params.get("path", "").rstrip("/")
        needle, deep = params.get("q", "").lower(), params.get("deep", "") in ("1", "true")
        known = next((row for row in HARNESSES if row["id"] == harness and row["found"]), None)
        rows = [row for row in SESSIONS if known and row["harness"] == harness]
        rows = [row for row in rows if needle in row["title"].lower()]
        empty = {"harness": harness, "path": path, "here": [], "children": [], "folders": [], "truncated": False, "cursor": "",
                 "parent": None, "home": "", "crumbs": [], "browse": None}
        if not known:
            return 200, empty
        if not path:
            by_folder: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                by_folder.setdefault(row["cwd"], []).append(row)
            folders = [{"path": cwd, "sessions": len(group), "latest": _iso(min(r["age"] for r in group)), "project": _project_for(cwd)}
                       for cwd, group in sorted(by_folder.items(), key=lambda kv: min(r["age"] for r in kv[1]))]
            return 200, {**empty, "folders": folders}
        dirs = _dirs(harness)
        if path not in dirs and path != HOME:
            return 200, {**empty, "parent": path.rsplit("/", 1)[0] or "/", "home": HOME, "crumbs": _crumbs(path) if path.startswith(HOME) else []}
        here = [row for row in rows if row["cwd"] == path or (deep and row["cwd"].startswith(path + "/"))]
        children = []
        for child in sorted(d for d in dirs if d.rsplit("/", 1)[0] == path):
            inside = [row for row in SESSIONS if row["harness"] == harness and (row["cwd"] == child or row["cwd"].startswith(child + "/"))]
            children.append({"name": child.rsplit("/", 1)[1], "path": child, "sessions": len(inside),
                             "latest": _iso(min(r["age"] for r in inside)) if inside else None, "empty": not inside, "project": _project_for(child)})
        return 200, {**empty, "here": [header(row) for row in sorted(here, key=lambda r: r["age"])], "children": children,
                     "parent": path.rsplit("/", 1)[0] if path != HOME else None, "home": HOME, "crumbs": _crumbs(path), "browse": "ok"}

    def preview(self, params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        harness = params.get("harness", "")
        row = self._find(harness, params.get("id", ""))
        if row is None:
            return 404, {"detail": {"code": "missing", "message": "no such session"}}
        messages = row["messages"]
        large = messages > 2000
        tokens = messages * 230
        claude = harness == "claude"
        window = 200000 if claude else 400000
        header_row = header(row)
        if claude:
            model = {"source": row["model"], "preset": "opus", "label": "Claude Opus 4.1", "same": True, "window": window}
            models = [{"id": "opus", "label": "Claude Opus 4.1", "window": 200000}, {"id": "deepseek", "label": "DeepSeek V3.2", "window": 128000}]
        else:
            model = {"source": row["model"], "preset": "default", "label": "Default model", "same": False, "window": 128000}
            models = [{"id": "default", "label": "Default model", "window": 128000}, {"id": "opus", "label": "Claude Opus 4.1", "window": 200000}]
        tool_calls = messages // 2
        counts = {"turns": int(messages * 1.07), "user": messages // 7, "assistant": messages // 2 - messages // 7 + messages // 3,
                  "tool_calls": tool_calls, "native_calls": tool_calls * 4 // 5, "text_calls": tool_calls - tool_calls * 4 // 5,
                  "compactions": row["compacted"], "sidechains": row["sidechains"], "images": 1 if claude and not large else 0,
                  "thinking": messages // 5, "meta": messages // 25, "orphan_results": 0}
        return 200, {"header": header_row, "harness_name": NAMES[harness], "first": _excerpt(row), "last": [] if large else _tail(row),
                     "complete": not large, "counts": counts, "messages": messages, "tokens": tokens, "window": window,
                     "suggested_mode": "tail" if tokens > window else "full", "model": model, "models": models,
                     "destination": _destination(row), "imported_as": header_row["imported_as"], "live": row["live"], "masked": 3}

    def start(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        row = self._find(body.get("harness", ""), body.get("id", ""))
        if row is None:
            return 404, {"detail": {"code": "missing", "message": "no such session"}}
        if row["imported"] and not body.get("again"):
            return 409, {"detail": {"code": "already_imported", "message": "this session was imported before", "session_id": row["imported"][0]}}
        self.started.append(body)
        job_id = f"job-{len(self.started)}"
        self.jobs[job_id] = {"job_id": job_id, "harness": row["harness"], "id": row["id"], "state": "running", "step": 0,
                             "messages": 800 if body.get("mode") == "tail" or row["messages"] > 2000 else row["messages"],
                             "started_at": _iso(0), "finished_at": None, "error": None}
        if row["code"] == "missing" and not body.get("cwd"):
            # As the server does: the job is accepted and fails when it learns where the session would land.
            self.jobs[job_id]["fails"] = {"code": "missing", "message": f"{row['cwd']} is not on the machine any more; choose the folder to continue in"}
        return 200, {"job_id": job_id, "job": _job(self.jobs[job_id])}

    def poll(self, job_id: str) -> tuple[int, dict[str, Any]]:
        state = self.jobs.get(job_id)
        if state is None:
            return 404, {"detail": {"code": "missing", "message": "no such import"}}
        if state["state"] == "running":
            # One stage per poll, so a check can watch the bar move and then follow the job to its end.
            state["step"] += 1
            if state.get("fails") and state["step"] == 2:
                state["state"], state["finished_at"], state["error"] = "failed", _iso(0), state["fails"]
            elif state["step"] == len(STAGES) - 1:
                state["state"], state["finished_at"] = "done", _iso(0)
        return 200, _job(state)

    def answer(self, method: str, path: str, query: str, body: Any) -> tuple[int, Any] | None:
        """``(status, payload)``; a string payload is the original transcript, served as ndjson."""
        params = {key: values[0] for key, values in parse_qs(query).items()}
        body = body or {}
        parts = path.split("/")
        if len(parts) == 6 and parts[:3] == ["", "api", "sessions"] and parts[4] == "import":
            session_id = parts[3]
            if method == "POST" and parts[5] == "refresh":
                failure = self._host_failure()
                if failure:
                    return failure
                self.refreshed.append(session_id)
                return 200, {"session_id": session_id, "added": 6, "turns": 3, "masked": 0, "live": True}
            if method == "GET" and parts[5] == "original":
                if session_id not in {row["imported"][0] for row in SESSIONS if row["imported"]} | {"s-imported"}:
                    return 404, {"detail": {"code": "missing", "message": "no original was kept for this session"}}
                lines = [{"type": "user", "message": {"role": "user", "content": "Fix the flaky door sensor test"}},
                         {"type": "assistant", "message": {"role": "assistant", "content": "I will start with the test's timing."}},
                         {"type": "summary", "summary": "Door sensor test fixed"}]
                return 200, "".join(json.dumps(line) + "\n" for line in lines)
            return None
        if not path.startswith("/api/imports"):
            return None
        failure = self._host_failure()
        if failure:
            return failure
        if method == "GET" and path == "/api/imports/harnesses":
            return 200, {"harnesses": HARNESSES}
        if method == "GET" and path == "/api/imports/scan":
            return self.scan(params)
        if method == "GET" and path == "/api/imports/preview":
            return self.preview(params)
        if method == "POST" and path == "/api/imports":
            return self.start(body)
        if method == "GET" and len(parts) == 4 and parts[3]:
            return self.poll(parts[3])
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
        if isinstance(payload, str):
            route.fulfill(status=status, content_type="application/x-ndjson", body=payload)
        else:
            route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        return True


DEFAULT = ImportStub()
"""What every harness answers when it does not install its own: the host bridge up, over the
invented transcripts."""
