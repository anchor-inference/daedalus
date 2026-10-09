"""The session import's API over invented sessions of other programs, for the browser checks and the screenshots.

``/api/imports/harnesses``, ``/api/imports/scan``, ``/api/imports/preview``, ``POST /api/imports``,
``/api/imports/{job_id}``, ``/api/sessions/{id}/import/refresh`` and ``/api/sessions/{id}/import/original``
in the shapes ``daedalus/extensions/api_session_import.py`` answers (and ``miniapp/src/imports/model.ts``
reads), over a small machine: Claude Code with sessions in several folders, Codex in two, Gemini CLI
with one, opencode installed with none, Cursor not installed.

``api_stub.answer_shared`` answers through ``DEFAULT``, so every harness that opens the start screen
gets the card's sessions without inventing them. A check that drives the import installs an
``ImportsStub`` of its own to stop the machine (``host="down"``), hold a job at a stage, and read the
writes back. ``imported_session`` is the chat an import makes, with the markers the conversation draws.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

HOME = "/home/operator"
PROJECTS = f"{HOME}/projects"
IMPORTED_ID = "imp0c9e2a1f3"
"""The Daedalus chat the import makes, and the one an earlier import already made."""
EARLIER_ID = "imp7be05d41a"


def _ago(**kw: float) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def _session(harness: str, id_: str, cwd: str, title: str, *, minutes: float, messages: int, size: int, branch: str = "",
             compacted: int = 0, sidechains: int = 0, live: bool = False, imported_as: str | None = None, model: str = "",
             project: dict | None = None) -> dict[str, Any]:
    return {
        "harness": harness, "id": id_, "cwd": cwd, "title": title, "started_at": _ago(minutes=minutes + 90), "updated_at": _ago(minutes=minutes),
        "messages": messages, "bytes": size, "branch": branch, "model": model,
        "flags": {"compacted": compacted, "sidechains": sidechains, "live": live, "imported_as": imported_as},
        "imported_as": {"session_id": imported_as, "title": title} if imported_as else None, "project": project,
    }


HOME_PROJECT = {"id": "p-home", "name": "Smart home", "kind": "project"}
MB = 1024 * 1024


def sessions() -> list[dict[str, Any]]:
    """Every session on the invented machine, freshest first within each program."""
    home = f"{PROJECTS}/smart-home"
    return [
        _session("claude", "a1f3c9e2-5b7d-4c11-9e0a-2f6d8c3b4d17", home, "Rebind the Aqara lock after the battery change", minutes=120, messages=214, size=int(1.8 * MB),
                 branch="main", sidechains=2, model="claude-opus-5", project=HOME_PROJECT),
        _session("claude", "7be05d41-0c2e-4a8f-b3d9-61e4f0a2c5b8", home, "Move automations.yaml to blueprints", minutes=60 * 24, messages=3412, size=38 * MB,
                 branch="blueprints", compacted=3, sidechains=7, model="claude-opus-5", project=HOME_PROJECT),
        _session("claude", "c0d4e8aa-91f2-4d6b-8a3c-5e7b9d1f2a64", home, "Zigbee2MQTT: OTA firmware for the plugs", minutes=60 * 48, messages=88, size=640 * 1024,
                 branch="main", model="claude-sonnet-5", project=HOME_PROJECT),
        _session("claude", "5e91b2f7-3a6c-4e8d-9b1f-0c2d4e6f8a13", home, "Nursery night light on the motion sensor", minutes=60 * 24 * 5, messages=41, size=210 * 1024,
                 model="claude-opus-5", project=HOME_PROJECT),
        _session("claude", "e2aa0c13-7d4b-4f9e-a1c6-3b5d7e9f1a28", home, "Sunset lighting scenes", minutes=60 * 24 * 21, messages=63, size=320 * 1024,
                 imported_as=EARLIER_ID, model="claude-opus-5", project=HOME_PROJECT),
        _session("claude", "09f1c2d8-4b6a-4c8e-9d2f-1a3b5c7d9e0f", home, "/init", minutes=60 * 24 * 30, messages=6, size=18 * 1024, project=HOME_PROJECT),
        _session("claude", "3c8e71b0-6f2a-4d9c-8b1e-7a5c3d1f9e24", f"{PROJECTS}/esp32-door", "Reed switch and door lock on one board", minutes=12, messages=97, size=int(1.1 * MB),
                 model="claude-opus-5"),
        _session("claude", "b71d09e5-2c4f-4a6e-8d0b-9f1e3a5c7b42", f"{PROJECTS}/daedalus", "Why the event stream drops after sleep", minutes=0.3, messages=152, size=int(2.2 * MB),
                 branch="main", live=True, model="claude-opus-5"),
        _session("claude", "41aa2f60-8e1c-4b3d-a5f7-2c9e0d4b6a81", f"{HOME}/Documents/rent", "Comparing smart locks under 200 euros", minutes=60 * 24 * 30, messages=30, size=95 * 1024),
        _session("claude", "aa52d0c4-1b3e-4f6a-9c8d-7e0f2a4b6c19", f"{PROJECTS}/smart-home/homeassistant", "Template sensor for the boiler", minutes=60 * 30, messages=24, size=120 * 1024,
                 project=HOME_PROJECT),
        _session("codex", "019d4c2e-8a1f-7f30-9b2d-3e5c7a9b1d40", f"{PROJECTS}/esp32-door", "Door sensor: deep sleep between triggers", minutes=40, messages=156, size=int(2.4 * MB),
                 branch="deep-sleep", compacted=1, model="gpt-5.5-codex"),
        _session("codex", "019c7b1d-2e4a-7c19-8f3e-5d7a9c1e3a1e4", f"{PROJECTS}/esp32-door", "ESP32-C3 build in GitHub Actions", minutes=60 * 72, messages=72, size=910 * 1024,
                 branch="main", model="gpt-5.5-codex"),
        _session("codex", "019b33d0-6c8e-7a2b-9d4f-1e3a5c7e9b02", f"{PROJECTS}/smart-home", "Why the reed switch does not wake it", minutes=60 * 24 * 7, messages=38, size=300 * 1024,
                 model="gpt-5.5-codex", project=HOME_PROJECT),
        _session("gemini", "6f2a1c9e-4b7d-4e2f-8a1c-0d3e5f7a9b24", f"{PROJECTS}/thesis-latex", "Bibliography in biblatex", minutes=60 * 24 * 9, messages=19, size=88 * 1024),
    ]


HARNESSES = [
    {"id": "claude", "name": "Claude Code", "found": True, "root": "~/.claude/projects", "version": "2.4.1"},
    {"id": "codex", "name": "Codex", "found": True, "root": "~/.codex/sessions", "version": "0.160.0"},
    {"id": "gemini", "name": "Gemini CLI", "found": True, "root": "~/.gemini/tmp"},
    {"id": "opencode", "name": "opencode", "found": True, "root": "~/.local/share/opencode"},
    {"id": "cursor", "name": "Cursor", "found": False},
]


DOWN = {"code": "host_down", "message": "the host terminal daemon is not answering", "configured": True,
        "start": "systemctl --user start daedalus-ptyd", "install": "daedalus host install"}
OUTDATED = {"code": "host_outdated", "message": "the host terminal daemon is older than this version", "install": "daedalus host install"}

STAGES = ["read", "parse", "mask", "write", "index", "open"]
"""The stages a whole import goes through; a summary and its tail adds "summarise" before "open"."""
STAGES_ALL = ["read", "parse", "mask", "write", "index", "summarise", "open"]


def _norm(path: str) -> str:
    return path.rstrip("/") if len(path) > 1 else path


class ImportsStub:
    """The machine's other programs; ``host`` is ``"up"``, ``"down"`` or ``"outdated"``."""

    def __init__(self, host: str = "up") -> None:
        self.host = host
        self.posted: list[dict[str, Any]] = []
        self.refreshed: list[str] = []
        self.polls = 0
        self.hold: int | None = None
        """Keep a job at this stage (an index into ``STAGES``) however often it is asked: a picture of the progress."""
        self.fail = False
        self.all = sessions()

    def _mine(self, harness: str) -> list[dict[str, Any]]:
        return [s for s in self.all if s["harness"] == harness]

    def harnesses(self) -> dict[str, Any]:
        rows = []
        for h in HARNESSES:
            mine = self._mine(h["id"])
            rows.append({**h, "sessions": len(mine), "folders": len({s["cwd"] for s in mine})})
        return {"harnesses": rows}

    def scan(self, harness: str, path: str, query: str, deep: bool) -> dict[str, Any]:
        mine = self._mine(harness)
        folders: dict[str, dict[str, Any]] = {}
        for s in mine:
            row = folders.setdefault(s["cwd"], {"path": s["cwd"], "name": s["cwd"].rsplit("/", 1)[-1], "sessions": 0, "latest": s["updated_at"], "project": s["project"], "empty": False})
            row["sessions"] += 1
            row["latest"] = max(row["latest"], s["updated_at"])
        places = sorted(folders.values(), key=lambda f: f["latest"], reverse=True)
        base = {"harness": harness, "home": "", "parent": None, "crumbs": [], "browse": None, "truncated": False, "cursor": ""}
        if query:
            q = query.lower()
            hits = []
            for s in mine:
                if q in s["title"].lower():
                    hits.append(s)
                elif deep:
                    hits.append({**s, "snippet": f"…and then the [[{query}]] came back after the restart…"})
            return {**base, "path": "", "here": hits, "children": [], "folders": places}
        if not path:
            return {**base, "path": "", "here": [], "children": [], "folders": places}
        path = _norm(path)
        here = sorted((s for s in mine if s["cwd"] == path), key=lambda s: s["updated_at"], reverse=True)
        children: dict[str, dict[str, Any]] = {}
        for s in mine:
            if s["cwd"].startswith(path + "/"):
                name = s["cwd"][len(path) + 1:].split("/", 1)[0]
                child = children.setdefault(name, {"name": name, "path": f"{path}/{name}", "sessions": 0, "latest": s["updated_at"], "project": None, "empty": False})
                child["sessions"] += 1
        if path == f"{PROJECTS}/smart-home":
            # Folders beside the sessions that hold none, as the host's folder listing adds them.
            children.setdefault("backups", {"name": "backups", "path": f"{path}/backups", "sessions": 0, "latest": None, "project": None, "empty": True})
        parent = path.rsplit("/", 1)[0] or "/"
        crumbs = [{"name": "~", "path": HOME}] + [{"name": part, "path": f"{HOME}/{'/'.join(path[len(HOME) + 1:].split('/')[:i + 1])}"} for i, part in enumerate(path[len(HOME) + 1:].split("/"))] if path.startswith(HOME + "/") else []
        return {**base, "path": path, "parent": parent, "home": HOME, "crumbs": crumbs, "browse": "ok", "here": here,
                "children": sorted(children.values(), key=lambda c: (bool(c["empty"]), c["name"].lower())), "folders": places}

    def preview(self, harness: str, id_: str) -> tuple[int, Any]:
        s = next((x for x in self.all if x["harness"] == harness and x["id"] == id_), None)
        if s is None:
            return 404, {"detail": {"code": "missing", "message": "no such session"}}
        window = 200_000
        messages = s["messages"]
        tokens = messages * 450 if messages < 3000 else 1_900_000
        project = s["project"]
        if project:
            destination = {"kind": "project", "cwd": s["cwd"], "project": {"id": project["id"], "name": project["name"], "ephemeral": False},
                           "folder_id": "f-home", "worktree_cwd": None, "code": None, "problem": None, "other_project": None, "exists": True, "becomes_project": False}
        elif s["cwd"].startswith(f"{HOME}/Documents"):
            destination = {"kind": "refused", "cwd": s["cwd"], "project": None, "folder_id": None, "worktree_cwd": None, "code": "missing",
                           "problem": f"{s['cwd']} is not on the machine any more; choose the folder to continue in", "other_project": None, "exists": False, "becomes_project": False}
        else:
            destination = {"kind": "new_chat", "cwd": s["cwd"], "project": None, "folder_id": None, "worktree_cwd": None, "code": None, "problem": None,
                           "other_project": None, "exists": True, "becomes_project": False}
        name = {"claude": "Claude Code", "codex": "Codex"}.get(harness, harness)
        opus = s["model"].startswith("claude-opus")
        return 200, {
            "header": s, "harness_name": name,
            "first": [{"role": "user", "text": "The Aqara U200 lock stopped answering in Home Assistant after I changed the batteries…", "at": s["started_at"]},
                      {"role": "assistant", "text": "Let me look at the Zigbee2MQTT logs first.", "at": s["started_at"]}],
            "last": [{"role": "user", "text": "Then pair it straight to the coordinator.", "at": s["updated_at"]},
                     {"role": "assistant", "text": "The lock is back on the coordinator and the 23:00 automation is checked.", "at": s["updated_at"]}],
            "complete": messages < 3000,
            "counts": {"turns": messages * 2, "user": messages // 3, "assistant": messages - messages // 3, "tool_calls": int(messages * 0.8), "native_calls": int(messages * 0.7),
                       "text_calls": int(messages * 0.1), "compactions": s["flags"]["compacted"], "sidechains": s["flags"]["sidechains"], "images": 0, "thinking": 0, "meta": 0, "orphan_results": 0},
            "messages": messages, "tokens": tokens, "window": window, "suggested_mode": "tail" if (tokens > window * 0.6 or messages > 2000) else "full",
            "model": {"source": s["model"], "preset": "opus" if opus else "flash", "label": "Claude Opus 5" if opus else "DeepSeek Flash", "same": opus, "window": window},
            "models": [{"id": "opus", "label": "Claude Opus 5", "window": 200_000}, {"id": "flash", "label": "DeepSeek Flash", "window": 128_000}],
            "destination": destination, "imported_as": s["imported_as"], "live": s["flags"]["live"], "masked": 3,
        }

    def job(self, job_id: str) -> dict[str, Any]:
        """One poll of a job: each poll moves it one stage on, unless ``hold`` keeps it at one."""
        self.polls += 1
        at = min(self.polls if self.hold is None else self.hold, len(STAGES))
        stage = STAGES[min(at, len(STAGES) - 1)]
        counts: dict[str, int] = {"turns_read": 428, "turns_total": 428}
        if at >= 2:
            counts.update(masked=3, messages=214)
        if at >= 3:
            counts.update(written=214 if at > 3 else 132)
        if at >= 5:
            counts.update(history=214)
        view: dict[str, Any] = {"job_id": job_id, "harness": "claude", "id": "a1f3c9e2-5b7d-4c11-9e0a-2f6d8c3b4d17", "stage": stage, "stages": STAGES_ALL,
                                "counts": counts, "session_id": None, "error": None, "started_at": _ago(minutes=0.1), "finished_at": None}
        if self.fail and at >= 3:
            return {**view, "stage": "write", "state": "failed", "error": {"code": "failed", "message": "the transcript could not be written: the disk is full"}}
        if at >= len(STAGES):
            return {**view, "stage": "open", "state": "done", "session_id": IMPORTED_ID, "finished_at": _ago(minutes=0)}
        return {**view, "state": "running"}

    def answer(self, method: str, path: str, query: str, body: Any) -> tuple[int, str, str | bytes] | None:
        params = {key: values[0] for key, values in parse_qs(query, keep_blank_values=True).items()}
        reply = lambda status, payload: (status, "application/json", json.dumps(payload))  # noqa: E731
        if path.startswith("/api/imports") and self.host != "up":
            return reply(503 if self.host == "down" else 501, {"detail": DOWN if self.host == "down" else OUTDATED})
        if method == "GET" and path == "/api/imports/harnesses":
            return reply(200, self.harnesses())
        if method == "GET" and path == "/api/imports/scan":
            return reply(200, self.scan(params.get("harness", ""), params.get("path", ""), params.get("q", ""), params.get("deep") == "1"))
        if method == "GET" and path == "/api/imports/preview":
            return reply(*self.preview(params.get("harness", ""), params.get("id", "")))
        if method == "POST" and path == "/api/imports":
            sent = body or {}
            self.posted.append(sent)
            done = next((s for s in self.all if s["id"] == sent.get("id") and s["imported_as"]), None)
            if done and not sent.get("again"):
                return reply(409, {"detail": {"code": "already_imported", "message": "this session has been imported already; pull in what is new there, or import it again as a new chat",
                                              "session_id": done["imported_as"]["session_id"]}})
            self.polls = 0
            job_id = f"job-{len(self.posted)}"
            return reply(200, {"job_id": job_id, "job": {"job_id": job_id, "harness": sent.get("harness"), "id": sent.get("id"), "state": "running", "stage": "read",
                                                          "stages": STAGES_ALL, "counts": {}, "session_id": None, "error": None}})
        if method == "GET" and path.startswith("/api/imports/job-"):
            return reply(200, self.job(path.rsplit("/", 1)[-1]))
        parts = path.split("/")
        if len(parts) == 6 and parts[2] == "sessions" and parts[4] == "import":
            if method == "POST" and parts[5] == "refresh":
                self.refreshed.append(parts[3])
                return reply(200, {"session_id": parts[3], "added": 2, "turns": 5, "masked": 0, "live": True})
            if method == "GET" and parts[5] == "original":
                line = json.dumps({"type": "user", "message": {"role": "user", "content": "The lock stopped answering"}, "cwd": f"{PROJECTS}/smart-home"})
                return 200, "application/x-ndjson", (line + "\n").encode()
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
        status, content_type, payload = answered
        headers = {"Content-Disposition": 'attachment; filename="claude-a1f3c9e2.jsonl"'} if content_type == "application/x-ndjson" else None
        route.fulfill(status=status, content_type=content_type, body=payload, headers=headers)
        return True


def handles(path: str) -> bool:
    """Whether a path is one of the import's: ``api_stub`` asks before it hands a request over."""
    parts = path.split("/")
    return path.startswith("/api/imports") or (len(parts) == 6 and parts[2] == "sessions" and parts[4] == "import")


DEFAULT = ImportsStub()
"""What every harness answers when it does not install its own: a machine that answers."""


def _message(role: str, seq: int, minutes: float, text: str = "", *, calls: list | None = None, results: list | None = None,
             imported: dict | None = None, origin: str = "", summary: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = {"role": role, "seq": seq, "text": text, "thinking": "", "tool_calls": calls or [], "tool_results": results or [],
                           "created_at": _ago(minutes=minutes), "origin": origin}
    if imported:
        row["imported"] = imported
    if summary:
        row.update(summary=True, compaction={"reason": "import", "messages": 140})
    return row


def imported_messages() -> list[dict[str, Any]]:
    """The chat an import of the lock session made: a summary of the early part, the last exchange in
    Claude Code with its steps under their own names, and then the operator going on in Daedalus."""
    def cc(seq: int, **more: Any) -> dict[str, Any]:
        return {"harness": "claude", "ext_id": f"u{seq}", "seq": seq, **more}
    tools = cc(3, tools={"t1": "Bash", "t2": "Read", "t4": "Edit", "t6": "Bash"})
    calls = [
        {"id": "t1", "name": "Exec", "arguments": {"command": "docker logs zigbee2mqtt --since 2h | grep -i u200"}},
        {"id": "t2", "name": "Read", "arguments": {"path": f"{PROJECTS}/smart-home/zigbee2mqtt/configuration.yaml"}},
        {"id": "t3", "name": "Task", "arguments": {"description": "Find every automation that uses lock_front", "prompt": "…"}},
        {"id": "t4", "name": "Edit", "arguments": {"path": f"{PROJECTS}/smart-home/homeassistant/automations.yaml", "old_string": "lock.front_door", "new_string": "lock.lock_front"}},
        {"id": "t5", "name": "TodoWrite", "arguments": {"todos": [{"content": "rebind", "status": "completed"}, {"content": "fix the automation", "status": "completed"}, {"content": "check", "status": "completed"}]}},
        {"id": "t6", "name": "Exec", "arguments": {"command": "ha core check"}},
    ]
    results = [{"id": c["id"], "content": "ok", "is_error": False} for c in calls]
    return [
        _message("user", 1, 300, "Claude Code compacted the first 140 messages: the U200 lock stopped answering after the battery change; the Zigbee2MQTT logs showed it reconnecting through the hallway plug, a router with old firmware; the plug's OTA update did not help.",
                 summary=True, imported={"harness": "claude", "ext_id": "", "summary": True}),
        _message("user", 2, 200, "Then pair the lock straight to the coordinator and fix the 23:00 lock automation.", imported=cc(2), origin="operator"),
        _message("assistant", 3, 199, "", calls=calls, imported=tools),
        _message("tool", 4, 198, results=results, imported=cc(4)),
        _message("assistant", 5, 197, "The lock is back on the coordinator at 100 %. The automation pointed at the old `entity_id`; it is `lock.lock_front` now and `ha core check` passes.", imported=cc(5)),
        _message("user", 6, 3, "Run a test lock and then put it back as it was.", origin="operator"),
        _message("assistant", 7, 2, "Locked and unlocked once: both answered within a second. Everything is as it was."),
    ]


def imported_origin() -> dict[str, Any]:
    """``imported`` of the session's detail, as ``session_import.imported_view`` writes it."""
    return {"harness": "claude", "harness_name": "Claude Code", "id": "a1f3c9e2-5b7d-4c11-9e0a-2f6d8c3b4d17", "cwd": f"{PROJECTS}/smart-home",
            "source_model": "claude-opus-5", "branch": "main", "mode": "tail", "live": True, "complete": True,
            "imported_at": _ago(minutes=5), "refreshed_at": None, "masked": 3, "transcript_dropped": 0,
            "counts": {"turns": 428, "user": 60, "assistant": 154, "tool_calls": 171, "native_calls": 150, "text_calls": 21, "compactions": 1, "sidechains": 2,
                       "images": 0, "thinking": 40, "meta": 12, "orphan_results": 0},
            "original": {"stored": True, "bytes": 1_800_000, "name": "claude-a1f3c9e2-5b7d-4c11-9e0a-2f6d8c3b4d17.jsonl", "reason": None}}


def imported_session(base: dict[str, Any]) -> dict[str, Any]:
    """A session detail (``screenshots.detail``'s shape) turned into the chat the import made."""
    return {**base, "id": IMPORTED_ID, "title": "Rebind the Aqara lock after the battery change", "env": "host", "status": "idle", "run_id": None,
            "messages": imported_messages(), "imported": imported_origin(), "pending": None, "loop": None, "services": [], "subagents": [],
            "workspace": f"{PROJECTS}/smart-home", "workspace_name": "smart-home"}


def imported_row(base: dict[str, Any]) -> dict[str, Any]:
    """The chat's row in the session list, with the program it came from for its plate's corner mark."""
    return {**base, "id": IMPORTED_ID, "title": "Rebind the Aqara lock after the battery change", "env": "host", "status": "idle",
            "last_message_at": _ago(minutes=2), "imported_from": "claude"}
