"""The API answers every browser harness in this directory has to agree on.

Each script here invents an installation of its own — a session of three thousand synthetic
messages, a small studio's agents — but none of them reaches a screen at all until a handful of
routes answer sensibly. The app asks whether a model is configured before it draws anything, and
what this installation is allowed to do before it draws the navigation; a stub that answers ``{}``
to those sits on the "Add a model" screen until its selector times out, which is exactly what
happened to ``perf_session.py`` when the onboarding gate landed in the app and only
``screenshots.py`` was taught about it.

So the gates live here, once. A script answers what it has invented and falls back to this table
for the rest, and records every ``/api/`` path it did not recognise so the run can fail with the
list: a gate added to the app cannot be answered by one harness and silently missed by another.

``expect_app`` is here for the same reason one level down: before any of this matters, the address
a harness was pointed at has to be *this* build and not something else that happens to hold the
port. It is checked once, in a sentence, rather than discovered as a selector that never appears.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, unquote_plus

# Outside services_port_range (8100-8119), the range this product hands to an agent's own preview
# servers: a harness that serves its build into that range competes with the installation running
# beside it, and loses silently — the browser is pointed at the address either way.
DEFAULT_PORT = 8163
DEFAULT_APP = f"http://127.0.0.1:{DEFAULT_PORT}/app"


def folder(path: str, *, position: int = 0, label: str = "", env: str = "container", reach: str = "agents", reachable: bool = True, writable: bool | None = None, readonly: bool = False, is_git: bool = False) -> dict[str, object]:
    """One folder of a project as ``/api/projects`` reports it."""
    return {
        "id": "f-" + path.rstrip("/").rsplit("/", 1)[-1],
        "path": path, "label": label, "env": env, "is_git": is_git, "readonly": readonly, "position": position, "managed": False,
        "reachable": reachable, "writable": (reachable and not readonly) if writable is None else writable, "reach": reach,
    }


def folders(path: str, *, reachable: bool = True, writable: bool | None = None) -> list[dict[str, object]]:
    """A project's folders as the host reports them, for a project whose one folder is ``path``."""
    return [folder(path, reachable=reachable, writable=writable)]


ENVIRONMENTS = {"local": "container", "available": ["container"], "host_bridge": False, "docker": True}
"""Where a folder may live: a Docker installation without the host terminal bridge."""

NOTIFICATION_CATEGORIES = ["run_finished", "question", "permission", "run_failed", "staff_turn", "staff_review", "orchestrator_report", "agent_notify", "reminder", "spend", "system"]


def notification_preferences(**over: object) -> dict[str, object]:
    """``GET /api/notifications/preferences`` with the host's defaults, and whatever a harness changes."""
    def cells(push: str = "on", desktop: str = "on", telegram: str = "off") -> dict[str, str]:
        return {"in_app": "on", "push": push, "desktop": desktop, "telegram": telegram}
    matrix = {c: cells() for c in NOTIFICATION_CATEGORIES}
    for c in ("question", "permission", "run_failed", "orchestrator_report", "reminder", "spend"):
        matrix[c] = cells(telegram="on")
    matrix["staff_turn"] = cells(push="off", desktop="off")
    matrix["agent_notify"] = cells(telegram="urgent")
    preferences: dict[str, object] = {
        "matrix": matrix, "finished_min_seconds": 30, "quiet_hours": "", "muted_projects": {}, "quick_actions": True, "telegram_covers_push": True,
        "keep_days": 30, "push_per_session": 6, "push_window_minutes": 10, "push_per_hour": 60, "notify_tool_per_session": 5,
        "notify_tool_window_minutes": 10, "notify_tool_urgent_per_hour": 2, "orchestrator_hold_seconds": 60,
    }
    preferences.update(over)
    return {"preferences": preferences, "revision": "r1", "categories": NOTIFICATION_CATEGORIES, "zone": "Europe/Amsterdam"}


GIB = 1 << 30
MIB = 1 << 20


def terminal_load(*, running: int = 3, cap: int = 20, total: int = 62 * GIB, available: int = 38 * GIB, each: int = 700 * MIB, cpu: float = 14.0) -> dict[str, object]:
    """``GET /api/terminals/load`` for a 62 GB, 16-CPU machine: three sessions running, each about 700 MB."""
    daemon = 45 * MIB
    used = running * each + daemon
    extra = max(0, cap - running)
    machine_used = total - available + extra * each
    return {
        "cap": cap, "running": running, "queued": [],
        "used": {"rss_bytes": used, "daemon_rss_bytes": daemon, "cpu_percent": 9.0, "cpus": 16, "mem_total_bytes": total, "mem_available_bytes": available, "machine_cpu_percent": cpu},
        "profiles": {"harness:claude": {"rss_bytes": each, "cpu_percent": 3.0, "samples": 60}},
        "likely": {"rss_bytes": each, "cpu_percent": 3.0, "samples": 60, "basis": "running"},
        "projection": {"cap": cap, "sessions": max(cap, running), "terminals_rss_bytes": used + extra * each, "machine_used_bytes": machine_used, "mem_total_bytes": total,
                       "mem_percent": round(100 * machine_used / total, 1), "cpu_percent": round(cpu + extra * 3.0 / 16, 1), "level": "ok", "cpu_level": "ok"},
        "envs": [{"env": "container", "supported": True, "terminals": running, "rss_bytes": running * each, "cpu_percent": 9.0, "daemon_rss_bytes": daemon, "mem_total_bytes": total, "mem_available_bytes": available, "cpus": 16}],
        "thresholds": {"warn": 70.0, "bad": 90.0},
    }

def browser_load(*, running: int = 1, cap: int = 2, total: int = 62 * GIB, available: int = 38 * GIB, each: int = 260 * MIB) -> dict[str, object]:
    """``GET /api/browsers/load`` for the same machine: browsers of about 260 MB (browser_stub's twin)."""
    daemon = 12 * MIB
    used = running * each + daemon
    return {
        "cap": cap, "running": running, "queued": [], "memory_basis": "cgroup",
        "used": {"rss_bytes": used, "daemon_rss_bytes": daemon, "cpu_percent": 4.0, "cpus": 16, "mem_total_bytes": total, "mem_available_bytes": available, "machine_cpu_percent": 14.0},
        "likely": {"rss_bytes": each, "cpu_percent": 12.0, "samples": 30, "basis": "measured"},
        "projection": {"cap": cap, "sessions": max(cap, running), "terminals_rss_bytes": used, "machine_used_bytes": total - available, "mem_total_bytes": total, "mem_percent": 40.0, "cpu_percent": 15.0, "level": "ok", "cpu_level": "ok"},
        "envs": [{"env": "container", "supported": True, "browsers": running, "rss_bytes": running * each, "cpu_percent": 4.0, "mem_total_bytes": total, "mem_available_bytes": available}],
        "thresholds": {"warn": 70.0, "bad": 90.0},
    }


def _group(name: str, description: str, tools: list[str], tokens: int, default: str, sessions: int, runs: int, calls: int, load: str | None = None) -> dict[str, object]:
    return {"name": name, "description": description, "tools": tools, "tokens": tokens, "default": default, "load": load or default, "usage": {"sessions": sessions, "runs": runs, "calls": calls}}


TOOL_GROUP_CATALOGUE: dict[str, object] = {
    "days": 30,
    "sessions": 151,
    "runs": 894,
    "groups": [
        _group("agents", "Hand work to other agents", ["AskPeer", "PeerList", "SpawnAgent", "SubAgent", "SubAgentList", "SubAgentSend"], 1480, "auto", 8, 31, 243),
        _group("board", "The session's task board", ["BoardAdd", "BoardGet", "BoardList", "BoardUpdate"], 910, "auto", 20, 64, 394),
        _group("browser", "Drive a real browser", [f"Browser{n}" for n in ("Act", "Close", "Dialog", "Download", "Handoff", "Look", "Navigate", "Open", "Snapshot", "Tabs", "Text", "Wait")], 1690, "lazy", 2, 3, 9),
        _group("docs", "The documentation of this version", ["DocsRead", "DocsSearch"], 160, "lazy", 1, 1, 2),
        _group("learning", "Look back on your own work", ["LearningReport", "SkillDraft"], 330, "lazy", 2, 2, 3),
        _group("loop", "The standing task of a loop agent", ["LoopNext", "LoopPause", "LoopResume", "LoopStatus", "LoopStop"], 520, "lazy", 2, 2, 2),
        _group("mcp", "MCP servers for this session", ["McpDisable", "McpEnable", "McpList"], 420, "auto", 16, 18, 30),
        _group("mcp_oauth", "Sign in to an MCP server", ["McpOAuthBegin", "McpOAuthDisconnect", "McpOAuthFinish", "McpOAuthStatus"], 390, "lazy", 0, 0, 0),
        _group("scheduling", "Work that runs later or on an event", ["IntentCreate", "IntentDelete", "IntentList", "ScheduleCreate", "ScheduleDelete", "ScheduleList"], 1260, "lazy", 3, 10, 42),
        _group("self_development", "Change your own code", ["SelfApply", "SelfPropose", "SelfRebuild", "SelfRollback", "SelfWorkspace"], 900, "lazy", 3, 27, 53),
        _group("services", "Processes that outlive the turn", ["ServiceList", "ServiceLogs", "ServiceStart", "ServiceStop"], 700, "auto", 17, 40, 136, load="eager"),
    ],
}
"""``GET /api/tool-groups``: every host group, shaped like one month of a real installation's use."""


def session_tool_groups() -> dict[str, object]:
    """``GET /api/sessions/<id>/tool-groups`` for a session that loaded the browser, cannot sign in to MCP
    servers and chose its own mode for the board. Every other group is where its mode puts it, as the
    host derives it: on demand held back, always and "when it fits" (as the last run placed it) on the
    surface — so the panel shows the defaults and the settings, not a state nothing would produce."""
    fixed = {"browser": "loaded", "mcp_oauth": "off"}
    by_load = {"eager": "advertised", "auto": "advertised", "lazy": "deferred"}
    groups = TOOL_GROUP_CATALOGUE["groups"]
    assert isinstance(groups, list)
    return {"groups": [
        {"name": g["name"], "description": g["description"], "tools": len(g["tools"]), "load": g["load"],
         "source": "session" if g["name"] == "board" else "settings" if g["load"] != g["default"] else "default",
         "state": fixed.get(g["name"], by_load[g["load"]]), "pending": False}
        for g in groups
    ]}


LAUNCHER_COMMAND = "daedalus-launcher upgrade --stub-command-never-shown"
"""The command the launcher reports beside an update. The app must never draw it: a harness looks for
this exact text anywhere on the page."""


def launcher_state(*, upgrade: bool = False, package: str = "", installable: bool = True, download: dict | None = None, **over: object) -> dict[str, object]:
    """``GET /api/system/launcher``: by default a desktop application beside the server, up to date,
    so every picture and check that does not ask about updates draws the rail as it always has."""
    offer = {
        "from": "desktop-v0.15.1", "to": "desktop-v0.15.2", "url": "https://example.invalid/releases/tag/desktop-v0.15.2",
        "command": LAUNCHER_COMMAND, "package": package,
    }
    state: dict[str, object] = {
        "connected": True, "version": "0.15.1", "upgrade": offer if upgrade else None, "installable": installable,
        "checked_at": (datetime.now(UTC) - timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M:%SZ"), "error": "",
        "download": {"state": "idle", "done": 0, "total": 0, "error": "", "ready": False, **(download or {})},
    }
    state.update(over)
    return state


class LauncherStub:
    """The launcher's update flow for a harness that drives it: an offer, a download that moves a step
    on every read, and an install that is recorded rather than run.

    ``stage`` starts as ``"offer"``; ``"downloading"``, ``"ready"`` and ``"failed"`` put the flow
    straight into that state for a picture. ``posts`` lists the writes in the order they came."""

    TOTAL = 222 * (1 << 20)

    def __init__(self, stage: str = "offer", *, steps: int = 3) -> None:
        self.stage = stage
        self.steps = steps
        self.reads = 0
        self.posts: list[str] = []

    def view(self) -> dict[str, object]:
        if self.stage == "downloading":
            done = min(self.TOTAL, (self.reads + 1) * self.TOTAL // (self.steps + 1))
            return launcher_state(upgrade=True, download={"state": "running", "done": done, "total": self.TOTAL})
        if self.stage == "ready":
            return launcher_state(upgrade=True, download={"state": "done", "done": self.TOTAL, "total": self.TOTAL, "ready": True})
        if self.stage == "failed":
            return launcher_state(upgrade=True, download={"state": "failed", "error": "the release's checksum did not match"})
        return launcher_state(upgrade=True)

    def answer(self, method: str, path: str) -> tuple[int, object] | None:
        if not path.startswith("/api/system/launcher"):
            return None
        if method == "GET" and path == "/api/system/launcher":
            if self.stage == "downloading":
                self.reads += 1
                if self.reads > self.steps:
                    self.stage = "ready"
            return 200, self.view()
        if method != "POST":
            return None
        action = path.rsplit("/", 1)[-1]
        self.posts.append(action)
        if action == "check":
            return 200, self.view()
        if action == "download":
            self.stage, self.reads = "downloading", 0
            return 200, self.view()
        if action == "install":
            return 200, {"started": True}
        return None


GATES: dict[str, object] = {
    "/api/admission/capacity": {"host_id": "local", "cap": 4, "active": 1, "reserved": 0, "available": 3},
    "/api/admission/queue": {"entries": []},
    # Settings → Tools opens on the tool groups.
    "/api/tool-groups": TOOL_GROUP_CATALOGUE,
    "/api/maintenance": {"notice": None},
    "/api/conversation-search/settings": {"mode": "off", "paused": False, "reason": "off", "busy": False, "indexed": 0, "pending": 0, "label": "Multilingual E5 Small", "size_bytes": 135429554, "licence": "MIT", "installed": False},
    # Drawn before any screen: no model means the whole app is the "Add a model" flow.
    "/api/onboarding": {"has_model": True, "presets": 1, "default_preset": "p", "providers": [], "needs": [], "message": ""},
    # Decides which screens the navigation has at all.
    "/api/capabilities": {
        "selfdev": {"mode": "off", "configured": "off", "reasons": [], "missing": [], "tools": []},
        # The shell marks the Components entry from this; a stub without it marks nothing, which is
        # the right answer for a harness that has invented no missing component.
        "components": {"mode": "docker", "missing": [], "needed": [], "installing": ""},
    },
    # Settings asks what the installation is missing before it draws its index.
    "/api/components": {"mode": "docker", "launcher": False, "components": [], "disk_bytes": 0, "missing": [], "busy": ""},
    "/api/dependencies": {"capability": {"mode": "docker", "python": True, "system": True, "manager": "apt", "reason": ""}, "tools": [{"name": "python", "available": True, "version": "Python 3.12"}, {"name": "gcc", "available": False, "version": ""}], "packages": [], "recipe": {"python": [], "system": []}, "models": [{"id": "default", "label": "Test model"}], "proposal": None, "job": None},
    "/api/prompt-change": {"models": [{"id": "default", "label": "Test model"}], "proposal": None},
    "/api/auth/me": {"user": "operator"},
    "/api/auth/config": {"passkeys": 1},
    # The rail's account item opens Settings' security section, which lists the passkeys.
    "/api/auth/passkeys": [{"id": 1, "name": "Laptop", "created_at": "2026-09-01T10:00:00Z", "last_used_at": "2026-09-25T08:00:00Z", "transports": ["internal"]}],
    "/api/status": {"ok": True},
    # The desktop application beside the server, up to date: the rail has no update button.
    "/api/system/launcher": launcher_state(),
    # The badge on the Inbox entry of the navigation and on the bell.
    "/api/notifications/summary": {"unseen": 0, "needs_you": 0},
    # The centre itself, whichever view the bell's popover or the Inbox asks for: nothing yet.
    "/api/notifications": {"entries": [], "next_before": None, "summary": {"unseen": 0, "needs_you": 0}},
    # Web Push on this device: a harness serves plain http, and its host has no public https address.
    "/api/push/config": {"available": False, "reason": "no_https_url", "public_key": ""},
    "/api/push/subscriptions": {"subscriptions": []},
    # Settings → Notifications: the host's defaults, and a machine to draw the terminal load bar for.
    "/api/notifications/preferences": notification_preferences(),
    "/api/terminals/load": terminal_load(),
    # The built-in modes as a fresh installation configures them; the composer's mode chip lists them.
    "/api/modes": {
        "plan": {"description": "read and plan only; nothing is changed until the operator switches the mode"},
        "quick": {"description": "short answers, few tool calls, cheap"},
        "deep": {"description": "long autonomous work with a high budget"},
        "careful": {"description": "ask before anything irreversible"},
    },
    "/api/commands": [],
    "/api/asr": {"configured": False, "reason": "", "provider": "", "model": "", "max_seconds": 120, "autosend": False},
    "/api/proposals": [],
    "/api/schedules": [],
    "/api/calendar/events": [],
    "/api/calendar/accounts": [],
    "/api/diagrams": [],
    # A project's requests: none waits, so a phone's project draws no banner.
    "/api/asks": {"asks": []},
    "/api/sessions": {"sessions": [], "projects": []},
    # The main orchestrator's entry is pinned in every sidebar and reads its chat's summary: no session
    # yet, nothing handed out, no questions.
    "/api/main": {"session_id": "", "dispatches": [], "asks": [], "questions": 0, "setup": []},
    # The Questions tab of a project or of the main chat: nothing waits.
    "/api/questions": {"questions": []},
    # The shell asks which projects there are before it draws the rail.
    "/api/projects": [],
    # The folder form asks where a folder may live before it offers the environment choice.
    "/api/project-environments": ENVIRONMENTS,
    # The composer offers the voice page only where the installation has one; a harness has none.
    "/api/voice": {"enabled": False},
    # The browsers of whichever session or staff member a screen shows: none, on an installation that
    # has a browser. A harness that opens one installs `browser_stub.BrowserStub`, which answers the rest
    # of `/api/browsers*` and the live view's WebSocket.
    "/api/browsers": {"available": True, "reason": "", "groups": []},
    # Settings → Browser and the load bars read these; a harness with a browser installs the stub's.
    "/api/browsers/load": browser_load(running=0),
    "/api/browsers/running": {"browsers": []},
    "/api/browsers/profiles": {"profiles": []},
    "/api/browsers/recordings": {"envs": []},
    "/api/browsers/notes": {"notes": []},
    # Remote identity is optional and empty until a real host is paired.
    "/api/runtime/hosts": {"items": [], "collection_revision": 1},
    # Without a browser installed, the workloads are the terminals alone: their own bar.
    "/api/workloads/load": {"terminals": terminal_load(), "browsers": None, "together": None},
    # Terminal environments and the terminals in them: a container environment that works, a host
    # one that is not installed, and no terminals yet. Every session screen lists its terminals (with
    # `?owner_kind=session&owner_id=…`, answered here by path). A harness that opens terminals installs
    # `terminal_stub.TerminalStub`, which answers the rest of `/api/terminals*` and the WebSocket.
    "/api/terminals": {
        "envs": [
            {"env": "container", "available": True, "reason": "", "version": "0.1.0", "sandbox": "ok", "shell": "/bin/bash", "home": "/root", "port_range": "8120-8139", "public_host": "", "preview_poll_ms": 3000},
            {"env": "host", "available": False, "reason": "not_installed", "version": "", "sandbox": "", "shell": "", "home": "", "port_range": "", "public_host": "", "preview_poll_ms": 3000},
        ],
        "terminals": [],
        "capacity": {"running": 0, "cap": 20, "queued": 0},
    },
}


SHARED_WRITES: dict[tuple[str, str], tuple[int, str, str]] = {
    # Every signed-in window reports what it shows, whatever screen a harness drives; the host
    # answers with no content.
    ("POST", "/api/presence"): (204, "application/json", ""),
    # "Mark all read" in the bell's popover and on the Inbox.
    ("POST", "/api/notifications/seen"): (200, "application/json", json.dumps({"marked": 0, "summary": {"unseen": 0, "needs_you": 0}})),
    ("POST", "/api/pet/react"): (200, "application/json", json.dumps({"line": "I'm here.", "emotion": "joy", "action": "wave", "prop": ""})),
}


EVENTS = "/api/events"

LONG_RESULT_LAST_LINE = "the last line of the long result"
LONG_RESULT = "".join(f"{n:05d} build step output, long enough to wrap on a phone and to need scrolling\n" for n in range(660)) + LONG_RESULT_LAST_LINE
"""A tool result of about fifty thousand characters, ending in a line a harness looks for.

The listing carries only a preview of a result (``TOOL_RESULT_PREVIEW_CHARS``), and "show all"
asks ``/api/sessions/<id>/tool-results/<call>`` for the rest; every such call is answered with this,
so a harness that opens a result sees a long one end to end."""


def event_stream_hello() -> str:
    """The host's event stream as a stub gives it: one comment line, and then the end.

    No ``hello`` on purpose. The app counts a stream as up only once the host has greeted it, and
    while it is up the lists stop polling every few seconds and wait for events instead. A stub
    cannot send those events, so a harness that changes its own answers and waits for the list to
    follow (``check_conversation_search.py`` does) would wait a minute. Without the greeting the app
    stays on its polls, exactly as it behaves when the host is unreachable, and reconnects on its
    backoff (one, two, four seconds and up to fifteen), which costs a harness a request now and then.
    Finite rather than held open, so it never occupies one of the browser's six connections. A
    harness that tests the stream itself (``check_event_stream.py``) serves its own.
    """
    return ": no events from a stub\n\n"


VOICE_NOTE_PREFIX = "🎙 Voice note, transcribed automatically (wording may be imperfect):"
"""What the host puts before a transcript (daedalus/transport/telegram/voice.py)."""


SESSION_TAIL = 600
"""How many messages the host answers ``GET /api/sessions/<id>`` with when no ``tail`` is asked for."""


def session_page(detail: dict, params: dict[str, str]) -> dict:
    """One page of a session, as the host reads it: the newest ``tail`` messages, older than
    ``before=<seq>`` when that is given.

    A history no longer than the host's first page is answered whole to a read of its end, as it
    always was here: the checks that add a message mid-run and read the chat again depend on that,
    and the host would answer them the same messages anyway, only fewer of them.
    """
    messages = detail["messages"]
    before = int(params.get("before") or 0)
    if not before and len(messages) <= SESSION_TAIL:
        return detail
    tail = int(params.get("tail") or SESSION_TAIL)
    rows = [m for m in messages if not before or (m.get("seq") or 0) < before][-tail:]
    oldest = messages[0]["seq"] if messages else 0
    return {**detail, "messages": rows, "first_seq": rows[0]["seq"] if rows else 0, "has_older": bool(rows) and rows[0]["seq"] > oldest}


def search_messages(messages: list[dict], words: str, limit: int = 30) -> dict:
    """``GET /api/sessions/<id>/search``: every message whose text holds the words, newest first, with
    a passage around the match and the match in [brackets], as the host marks it."""
    needle = words.strip().casefold()
    hits = []
    for m in messages:
        text = m.get("text") or ""
        at = text.casefold().find(needle) if needle else -1
        if at < 0:
            continue
        start, end = max(0, at - 40), min(len(text), at + len(needle) + 60)
        snippet = ("…" if start else "") + text[start:at] + "[" + text[at:at + len(needle)] + "]" + text[at + len(needle):end] + ("…" if end < len(text) else "")
        hits.append({"seq": m["seq"], "snippet": " ".join(snippet.split()), "role": m["role"], "at": m.get("created_at", ""), "origin": m.get("origin")})
    hits.sort(key=lambda h: -h["seq"])
    return {"hits": hits[:limit], "partial": False, "semantic": False, "reason": "off"}


def long_history(detail: dict, pairs: int = 700, planted: dict[int, str] | None = None) -> dict[int, int]:
    """Put a long conversation in front of a session's own: ``pairs`` questions and answers, several
    of the host's pages of them, so that an early message is only reached by reading older pages.

    The session's own messages are moved after the new ones, their seqs shifted up. ``planted`` puts
    words into the answer of the pair with that index (for a search to find); the answer is the
    seq of each pair's question, by index.
    """
    shift = 2 * pairs + 10
    for m in detail["messages"]:
        if isinstance(m.get("seq"), int):
            m["seq"] += shift
    earlier: list[dict] = []
    questions: dict[int, int] = {}
    for i in range(pairs):
        seq = 1 + 2 * i
        questions[i] = seq
        at = (datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=30 * i)).isoformat().replace("+00:00", "Z")
        extra = f" {planted[i]}" if planted and i in planted else ""
        earlier.append({"role": "user", "seq": seq, "origin": "operator", "text": f"Question {i}: how is the next part going?", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": at})
        earlier.append({"role": "assistant", "seq": seq + 1, "text": f"Answer {i}.{extra}\n\nA paragraph of the answer that takes a line or two on any screen, about the work of the day.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": at})
    detail["messages"] = earlier + detail["messages"]
    detail["first_seq"] = 1
    return questions


def answer_shared(method: str, path: str) -> tuple[int, str, str] | None:
    """The answer every harness gives the same way: ``(status, content type, body)``, or ``None``.

    ``path`` may be a whole URL; the query string and everything before ``/api/`` are ignored. A
    harness asks this where it used to look ``GATES`` up, after its own routes, so what it invented
    still wins.
    """
    path = path.split("?", 1)[0]
    path = path[path.index("/api/"):] if "/api/" in path else path
    if method.upper() == "GET" and path == EVENTS:
        return 200, "text/event-stream", event_stream_hello()
    write = SHARED_WRITES.get((method.upper(), path))
    if write is not None:
        return write
    if method.upper() == "POST" and path.startswith("/api/notifications/") and path.endswith("/act"):
        # The shared centre is empty, so every entry a harness might answer is one the host no
        # longer has; a harness that invents entries answers this route itself.
        return 404, "application/json", json.dumps({"detail": "no such notification"})
    if method.upper() == "POST" and path in ("/api/system/launcher/check", "/api/system/launcher/download"):
        return 200, "application/json", json.dumps(launcher_state())
    if method.upper() == "POST" and path == "/api/system/launcher/install":
        return 200, "application/json", json.dumps({"started": True})
    if path in GATES:
        return 200, "application/json", json.dumps(GATES[path])
    parts = path.split("/")
    if method.upper() == "GET" and len(parts) == 5 and parts[2] == "providers" and parts[4] == "limits":
        return 200, "application/json", json.dumps({"provider_id": parts[3], "observations": []})
    if method.upper() == "GET" and len(parts) == 6 and parts[2:4] == ["runtime", "browsers"] and parts[5] == "ownership":
        return 200, "application/json", json.dumps({"group_id": parts[4], "alive": "unknown", "reason": "unconfirmed", "observed_at": None,
                                              "last_safe_url": None, "reconnectable": False, "instance_generation": "", "authorization_state": "authenticated_operator"})
    if method.upper() == "GET" and len(parts) == 5 and parts[2] == "artifact-manifests" and parts[4] == "transfer-readiness":
        return 200, "application/json", json.dumps({"manifest_id": parts[3], "project_id": "", "available": False, "reason": "file_unavailable", "transfers": []})
    if method.upper() == "POST" and len(parts) == 5 and parts[2] == "sessions" and parts[4] in ("mode", "yagni"):
        # The composer's mode chip: a harness that reads the switch back answers these itself.
        return 200, "application/json", "{}"
    if method.upper() == "GET" and len(parts) == 6 and parts[2] == "sessions" and parts[4] == "tool-results":
        return 200, "application/json", json.dumps({"id": parts[5], "content": LONG_RESULT, "is_error": False, "length": len(LONG_RESULT), "complete": True})
    if len(parts) >= 5 and parts[2] == "sessions" and parts[4] == "tool-groups":
        # Every session's Details lists its tool groups; a change or a "Load now" answers the same list.
        return 200, "application/json", json.dumps(session_tool_groups())
    if method.upper() == "PUT" and len(parts) == 4 and parts[2] == "tool-groups":
        return 200, "application/json", json.dumps(TOOL_GROUP_CATALOGUE)
    if method.upper() == "POST" and ((len(parts) == 5 and parts[2] == "sessions" and parts[4] == "transcribe") or path == "/api/transcribe"):
        # A composer voice note, a session's or the start page's before it has one: the words the
        # stub heard, marked as the host marks a transcript. check_voice_note.py answers the
        # session's itself to fail it, keep it and retry it.
        heard = "hello from the stub"
        return 200, "application/json", json.dumps({"transcript": heard, "text": f"{VOICE_NOTE_PREFIX}\n{heard}", "autosend": False})
    if method.upper() == "DELETE" and ((len(parts) == 6 and parts[2] == "sessions" and parts[4] == "transcribe") or (len(parts) == 4 and parts[2] == "transcribe")):
        return 200, "application/json", json.dumps({"deleted": True})
    if method.upper() == "GET" and len(parts) == 6 and parts[2] == "sessions" and parts[4:] == ["tools", "timing"]:
        # A session's Details reads the time its tools took; nobody here timed any.
        return 200, "application/json", json.dumps({"items": []})
    if method.upper() == "GET" and len(parts) == 5 and parts[2] == "sessions" and parts[3] != "search" and parts[4] == "search":
        # The chat's own search, in a chat a harness did not fill: nothing found, the words only.
        return 200, "application/json", json.dumps({"hits": [], "partial": False, "semantic": False, "reason": "off"})
    if method.upper() == "GET" and len(parts) == 5 and parts[2] == "sessions" and parts[4] == "mcp":
        return 200, "application/json", json.dumps({"enabled": [], "servers": []})
    if method.upper() == "GET" and len(parts) == 5 and parts[3] == "provider" and parts[2] == "usage":
        # The provider card in Details: a day with nothing spent, no subscription read.
        return 200, "application/json", json.dumps({"provider": parts[4], "today": {}, "subscription": None, "balance": None})
    if method.upper() == "GET" and path == "/api/files":
        # Handles a harness did not invent name no file: the chat draws no card for them.
        return 200, "application/json", json.dumps({"files": []})
    parts = path.split("/")
    if method.upper() == "GET" and path == "/api/control/revisions":
        return 200, "application/json", json.dumps({"scope": {"kind": "global", "id": "global"},
                                                      "collection_revision": 1, "entity_revision": None})
    if method.upper() == "GET" and path == "/api/admission/resources":
        unavailable = {"available": False, "reason": "kernel delegation is unavailable"}
        return 200, "application/json", json.dumps({"container": unavailable, "host": unavailable,
                                                      "native": {"available": False, "reason": "in-process runtime"},
                                                      "disk_quota_supported": False})
    if method.upper() == "GET" and path == "/api/plugins/read-models":
        return 200, "application/json", json.dumps([])
    if method.upper() == "GET" and len(parts) == 5 and parts[2] == "projects" and parts[4] == "workspace-archive":
        return 200, "application/json", json.dumps({"latest": None, "available": False})
    if method.upper() == "GET" and len(parts) == 6 and parts[2] == "projects" and parts[4:] == ["workspace-archive", "budget-history"]:
        return 200, "application/json", json.dumps({"state": "not_in_source", "restored": False,
                                                      "current_available_microusd": None})
    if method.upper() == "GET" and len(parts) == 5 and parts[2] == "projects" and parts[4] == "files":
        return 200, "application/json", json.dumps({"files": []})
    if method.upper() == "GET" and len(parts) == 5 and parts[2] == "projects" and parts[4] == "focus-state":
        # An orchestrator's chat nobody invented a project for has nothing in hand, and no goal line.
        return 200, "application/json", json.dumps(empty_focus_state(parts[3]))
    if method.upper() == "GET" and len(parts) == 5 and parts[2] == "projects" and parts[4] == "next-actions":
        return 200, "application/json", json.dumps({"project_id": parts[3], "actions": []})
    if method.upper() == "GET" and len(parts) == 5 and parts[2] == "projects" and parts[4] == "unknown-stops":
        return 200, "application/json", json.dumps({"items": [], "next_after": None})
    if method.upper() == "GET" and len(parts) == 6 and parts[2] == "projects" and parts[4:] == ["scope-revisions", "current"]:
        return 200, "application/json", json.dumps({"project_id": parts[3], "goal_revision": 1,
                                                      "entity_revision": 1, "body": "Existing project goal",
                                                      "checks": None})
    if method.upper() == "GET" and len(parts) == 5 and parts[2] == "projects" and parts[4] == "budget":
        return 200, "application/json", json.dumps({"configured": False, "project_id": parts[3],
                                                      "entity_revision": 1, "goal_revision": 1})
    if method.upper() == "GET" and len(parts) == 5 and parts[2] == "projects" and parts[4] == "resource-profile":
        return 200, "application/json", json.dumps({"configured": False, "project_id": parts[3],
                                                      "entity_revision": 1, "profile_revision": None,
                                                      "state": "disabled", "limits": None,
                                                      "min_free_disk_bytes": 0,
                                                      "disk_quota_supported": False})
    if method.upper() == "GET" and len(parts) == 5 and parts[2] == "projects" and parts[4] == "usage":
        # A project nobody invented spend for spent nothing; a harness with a team answers it itself.
        nothing = {w: {"usd": 0.0, "tokens": 0, "unpriced": 0} for w in ("today", "week", "all")}
        line = {**nothing, "subscription": None}
        return 200, "application/json", json.dumps({"project_id": parts[3], "since": {}, "staff": [], "orchestrator": line, "other": line, "total": line})
    return None


def fulfil_shared(route) -> bool:  # type: ignore[no-untyped-def]
    """:func:`answer_shared` for a Playwright route: ``True`` when it answered."""
    shared = answer_shared(route.request.method, route.request.url)
    if shared is None:
        return False
    status, content_type, body = shared
    route.fulfill(status=status, content_type=content_type, body=body)
    return True


def serve_shared_post(handler, unhandled: Unhandled) -> None:  # type: ignore[no-untyped-def]
    """``do_POST`` for a harness built on ``http.server``, which otherwise answers every write 501.

    The body is read whatever it is, so the connection stays usable; a write nobody here knows is
    recorded like an unknown read and answered 404.
    """
    length = int(handler.headers.get("Content-Length") or 0)
    if length:
        handler.rfile.read(length)
    shared = answer_shared("POST", handler.path)
    if shared is None:
        unhandled.record(handler.path.split("?", 1)[0])
        shared = (404, "application/json", '{"detail": "Not Found"}')
    status, content_type, body = shared
    data = body.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    if data:
        handler.wfile.write(data)


class Unhandled:
    """Every ``/api/`` path a stub had no answer for, so a run can end by saying so."""

    def __init__(self) -> None:
        self.paths: set[str] = set()

    def record(self, path: str) -> None:
        self.paths.add(path)

    def report(self) -> int:
        """Print what was missed. Non-zero when something was, which is the run's exit code."""
        if not self.paths:
            return 0
        print("\nthe stub had no answer for these API paths (add them here or to tests/browser/api_stub.py):")
        for path in sorted(self.paths):
            print(f"  {path}")
        return 1


def expect_app(base: str) -> None:
    """Refuse to drive a browser at an address that is not the built Mini App.

    ``serve_app.py`` is normally started in the background, and a port it could not bind leaves
    something else answering there — an agent's own preview server, a stale run. The browser loads
    that page quite happily, and the failure arrives much later as a selector timeout on ``.screen``
    that reads like a broken component. One request up front says what actually happened.
    """
    try:
        with urllib.request.urlopen(f"{base}/index.html", timeout=5) as answer:  # noqa: S310
            body = answer.read(4096).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        print(f"nothing is serving the app at {base} ({exc}); build it and start tests/browser/serve_app.py", file=sys.stderr)
        raise SystemExit(1) from None
    if "/app/assets/" not in body:
        print(f"{base} answers, but not with the built Mini App — something else holds that port", file=sys.stderr)
        raise SystemExit(1)



__all__ = ["VOICE_NOTE_PREFIX", "CAPABILITIES", "CATALOG", "CLAUDE_ALIASES", "CLAUDE_VERSIONS", "HarnessesStub", "answer_batch", "question_view", "DEFAULT_APP", "DEFAULT_PORT", "ENVIRONMENTS", "EVENTS", "FOCUS_WORDS", "GATES", "NOTIFICATION_CATEGORIES", "TOOL_GROUP_CATALOGUE", "BoardStub", "FocusStub", "TeamStub", "Unhandled", "answer_shared", "event_stream_hello", "expect_app", "folder", "folders", "fulfil_shared", "notification_preferences", "serve_shared_post", "session_tool_groups", "terminal_load"]

# What the harness manager reports for the container: Claude Code installed and signed in, Codex
# installed but signed out, the rest absent. Enough for the hiring form to show one command-line agent
# it can offer and one it cannot, with the reason.
CATALOG: dict[str, object] = {
    "claude": {"installed": True, "version": "2.1.40", "latest": "2.1.40", "logged_in": True, "agents": [{"name": "code-reviewer", "source": "project"}], "models": ["opus", "sonnet"], "all_models": ["opus", "sonnet"], "models_chosen": False, "error": "", "checked_at": "2026-09-24T09:00:00Z"},
    "codex": {"installed": True, "version": "0.40.0", "latest": "0.41.0", "logged_in": False, "agents": [], "models": ["gpt-5.2-codex"], "all_models": ["gpt-5.2-codex"], "models_chosen": False, "error": "", "checked_at": "2026-09-24T09:00:00Z"},
}


class TeamStub:
    """A project's team, answered the way the host answers it, and kept between requests.

    The page hires, edits and dismisses; a stub that forgot each change would show a list that never
    moves, and a check of those three would pass against nothing. The rules the host enforces that
    the page shows back — a name already taken, dismissing a member who works — are kept here too.
    """

    def __init__(self, project: dict, *, staff: list[dict] | None = None, catalog: dict | None = None, presets: list[dict] | None = None, personas: list[str] | None = None) -> None:
        folders = project.get("folders") or []
        self.project = {
            "id": project["id"], "name": project["name"], "ephemeral": False, "system": "", "default_env": "container", "local_env": "container",
            "concurrency": 6, "concurrency_cap": 10, "orchestrator": False,
            "folders": [{k: f[k] for k in ("id", "path", "label", "env", "is_git", "readonly")} for f in folders],
        }
        self.staff: list[dict] = [dict(m) for m in staff or []]
        self.catalog = CATALOG if catalog is None else catalog
        self.presets = presets if presets is not None else [{"id": "strong", "label": "Claude Opus 5"}, {"id": "fast", "label": "DeepSeek Flash"}]
        self.personas = personas if personas is not None else ["reviewer", "tester"]
        self.hired: list[dict] = []
        self.patched: list[dict] = []
        self.spend: dict[str, dict] = {}
        """What each member spent, ``staff_id -> {"today": {...}, "subscription": ...}``; the rest spent nothing."""
        self.orchestrator_spend: dict | None = None

    @staticmethod
    def spent(usd: float = 0.0, tokens: int = 0, *, unpriced: int = 0, week: float | None = None, total: float | None = None, subscription: float | None = None) -> dict:
        """One usage line: today's spend, the week and all-time totals (at least today's), a subscription window."""
        today = {"usd": usd, "tokens": tokens, "unpriced": unpriced}
        return {
            "today": today, "week": {**today, "usd": week if week is not None else usd}, "all": {**today, "usd": total if total is not None else (week if week is not None else usd)},
            "subscription": {"window_used_pct": subscription, "source": "subscription"} if subscription is not None else None,
        }

    def usage(self) -> dict:
        """The answer of ``GET /api/projects/{id}/usage``: every member, the orchestrator, and the sums."""
        nothing = self.spent()
        rows = [{"staff_id": m["id"], "name": m["name"], "harness": m["harness"], "archived": bool(m["archived_at"]), **self.spend.get(m["id"], nothing)} for m in self.staff]
        orchestrator = self.orchestrator_spend or nothing
        total = {w: {k: round(sum(line[w][k] for line in [*rows, orchestrator]), 4) for k in ("usd", "tokens", "unpriced")} for w in ("today", "week", "all")}
        return {"project_id": self.project["id"], "since": {}, "staff": rows, "orchestrator": orchestrator, "other": nothing, "total": {**total, "subscription": None}}

    @staticmethod
    def member(id_: str, name: str, *, harness: str = "daedalus", status: str = "off", sessions: int = 0, **fields: object) -> dict:
        row = {
            "id": id_, "project_id": "", "name": name, "color": "blue", "role": "", "harness": harness, "agent": "", "model": "", "effort": "",
            "permission_mode": "", "env": "", "default_folder_id": None, "isolation": "worktree", "instructions": "", "notes": "", "one_off": False,
            "created_by": "operator", "created_at": "2026-09-24T09:00:00Z", "archived_at": None, "sessions": sessions, "status": status,
            "live": None if status == "off" else {"id": f"ss-{id_}", "status": status, "waiting_for": "", "task_id": None, "started_at": "2026-09-24T09:00:00Z", "ended_at": None, "branch": None, "worktree_path": None},
        }
        row.update(fields)
        return row

    def listing(self, archived: bool) -> dict:
        rows = [m for m in self.staff if archived or not m["archived_at"]]
        working = sum(1 for m in self.staff if m["status"] in ("starting", "working", "question", "permission", "no_signal"))
        return {
            "project": self.project,
            "staff": rows,
            "counts": {"staff": sum(1 for m in self.staff if not m["archived_at"]), "working": working},
            "choices": {"harnesses": ["daedalus", "claude", "codex", "grok", "opencode", "pi", "cursor"], "personas": self.personas, "presets": self.presets, "default_preset": self.presets[0]["id"] if self.presets else "", "coordinator_default_preset": self.presets[0]["id"] if self.presets else ""},
        }

    def answer(self, method: str, path: str, query: str, body: dict | None) -> tuple[int, object] | None:
        """``(status, body)`` for a route of the team, or None for anything else."""
        if path == "/api/harnesses/catalog":
            return (200, self.catalog) if self.catalog is not None else (404, {"detail": "Not Found"})
        if path == f"/api/projects/{self.project['id']}/usage" and method == "GET":
            return 200, self.usage()
        # The project's column counts its wake-ups and watches; this project has none.
        if path == f"/api/projects/{self.project['id']}/wakeups" and method == "GET":
            return 200, {"wakeups": [], "max": 20}
        if path == f"/api/projects/{self.project['id']}/watches" and method == "GET":
            return 200, {"watches": [], "max": 50, "min_cooldown_minutes": 1, "providers": []}
        if path == f"/api/projects/{self.project['id']}/staff":
            if method == "GET":
                return 200, self.listing("archived=1" in query)
            if method == "POST":
                payload = body or {}
                if any(m["name"].lower() == str(payload.get("name", "")).strip().lower() and not m["archived_at"] for m in self.staff):
                    return 400, {"detail": f"the team already has someone called {payload['name']}"}
                self.hired.append(payload)
                row = self.member(f"st-{len(self.staff) + 1}", str(payload["name"]).strip(), harness=payload.get("harness", "daedalus"))
                row.update({k: payload[k] for k in ("role", "agent", "model", "effort", "permission_mode", "env", "isolation", "instructions", "one_off") if k in payload})
                row["default_folder_id"] = payload.get("folder_id") or None
                row["project_id"] = self.project["id"]
                self.staff.append(row)
                return 201, row
        if path.startswith("/api/staff/"):
            sid = path.split("/")[3]
            row = next((m for m in self.staff if m["id"] == sid), None)
            if row is None:
                return 404, {"detail": "no such staff member"}
            if method == "PATCH":
                payload = dict(body or {})
                self.patched.append(payload)
                if "folder_id" in payload:
                    payload["default_folder_id"] = payload.pop("folder_id") or None
                row.update(payload)
                return 200, row
            if method == "DELETE":
                if row["live"] is not None:
                    return 409, {"detail": f"{row['name']} is working; release the session first"}
                row["archived_at"] = "2026-09-24T10:00:00Z"
                return 200, {"ok": True, "staff": row}
            if path.endswith("/sessions"):
                return 200, [row["live"]] if row["live"] else []
            if path.endswith("/resume-sessions"):
                return 200, []
            return 200, row
        return None

class BoardStub:
    """A project's board, answered the way the host answers it, and kept between requests.

    The page creates, edits, assigns and accepts; each is remembered, so a card that moved is drawn
    where it moved to. The host's own refusal to accept what is not in review is kept as well.
    """

    ORDER = {"doing": 0, "review": 1, "todo": 2, "blocked": 3, "done": 4, "dropped": 5}

    def __init__(self, project: dict, *, staff: list[dict] | None = None, tasks: list[dict] | None = None, needs_you: list[dict] | None = None) -> None:
        self.project = {"id": project["id"], "name": project["name"], "ephemeral": False, "system": ""}
        self.staff = [dict(m) for m in staff or []]
        self.tasks = [dict(t) for t in tasks or []]
        self.needs_you = [dict(n) for n in needs_you or []]
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []
        self.launched: list[tuple[str, dict]] = []
        self.launch_failures = 0
        self.launch_effects: dict[str, dict] = {}
        self.launch_unknowns = 0
        self.unknown_launches: list[tuple[str, dict]] = []
        self.cancellations: list[tuple[str, dict]] = []
        self.cancel_states: dict[str, str] = {}
        self.accepted: list[str] = []
        self.reviews: dict[str, dict] = {}
        self.ci_requirement_requests: list[dict] = []
        self.ci_requirement_conflicts = 0
        self.ci_requirement_failures = 0
        self.ci_requirement_receipts: dict[str, dict] = {}
        """A task's review as the host would read it from git; a branch task without one gets :meth:`review`."""
        self.merged: list[str] = []
        self.result_rows: dict[str, list[dict]] = {}
        self.merge_requests: list[tuple[str, dict]] = []
        self.merge_receipts: dict[str, dict] = {}
        self.rejected: list[tuple[str, str]] = []
        self.stops: list[tuple[str, dict]] = []
        self.workflow_runs: dict[str, dict] = {}
        self.workflow_commands: dict[str, dict] = {}
        self.workflow_unknowns = 0
        self.unknown_stops: list[dict] = []
        self.unknown_stops_failed = False
        self.stop_reconciliations: list[str] = []

    @staticmethod
    def task(id_: str, title: str, *, status: str = "todo", priority: int = 3, assignee: dict | None = None, **fields: object) -> dict:
        row = {
            "id": id_, "title": title, "status": status, "priority": priority, "acceptance": "", "checklist": [], "depends_on": [], "session_id": None, "notes": "",
            "created_at": "2026-09-24T09:00:00Z", "updated_at": "2026-09-24T09:30:00Z", "project_id": "", "assignee_staff_id": assignee["id"] if assignee else None,
            "brief": {"objective": "", "deliverable": "", "boundaries": "", "done_when": ""}, "branch": None, "merge_state": "", "assignee": assignee,
            "acceptance_state": "", "requirements": [], "entity_revision": 1,
        }
        row.update(fields)
        return row

    @staticmethod
    def assignee(id_: str, name: str, *, harness: str = "daedalus", color: str = "blue", status: str = "off", on_task: bool = False, **fields: object) -> dict:
        row = {"id": id_, "name": name, "color": color, "harness": harness, "archived_at": None, "status": status, "on_task": on_task, "waiting_for": "", "status_at": "2026-09-24T09:40:00Z" if on_task else None, "session_id": f"sess-{id_}" if status != "off" else None}
        row.update(fields)
        return row

    @staticmethod
    def review(row: dict, *, blockers: list[dict] | None = None, conflicts: list[str] | None = None, receipts: list[dict] | None = None) -> dict:
        """A two-file change on the task's branch, mergeable unless ``blockers`` say otherwise."""
        patch = (
            "diff --git a/api/notify.py b/api/notify.py\n--- a/api/notify.py\n+++ b/api/notify.py\n@@ -1,3 +1,5 @@\n def notify(order):\n-    send(order)\n+    if order.paid:\n+        send(order)\n+    log(order)\n     return True\n"
            "diff --git a/tests/test_notify.py b/tests/test_notify.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/test_notify.py\n@@ -0,0 +1,2 @@\n+def test_unpaid_orders_are_not_sent():\n+    assert not notify(unpaid)\n"
        )
        blockers = list(blockers or [])
        return {
            "task_id": row["id"], "title": row["title"], "status": row["status"], "merge_state": row.get("merge_state") or "proposed", "branch": row["branch"], "base": "main", "current": "main",
            "folder": {"id": "f1", "path": "/home/operator/work/bakery", "label": "", "env": "container"}, "exists": True, "on_base": True, "folder_clean": True, "merged": False,
            "commits": [
                {"sha": "4f2a9c1d0b7e6a5f4c3b2a1d0e9f8a7b6c5d4e3f", "author": "daedalus", "at": "2026-09-24T09:20:00Z", "subject": "Send notifications for paid orders only"},
                {"sha": "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d", "author": "daedalus", "at": "2026-09-24T09:25:00Z", "subject": "Test that unpaid orders are not sent"},
            ],
            "more_commits": False, "files": [{"path": "api/notify.py", "added": 3, "removed": 1}, {"path": "tests/test_notify.py", "added": 2, "removed": 0}],
            "added": 5, "removed": 1, "patch": patch, "patch_complete": True, "conflicts": conflicts or [],
            "receipts": receipts if receipts is not None else [{"criterion": "tests pass", "command": "pytest -q", "exit_code": 0, "passed": True, "at": "2026-09-24T09:26:00Z"}],
            "head_sha": "4f2a9c1d0b7e6a5f4c3b2a1d0e9f8a7b6c5d4e3f",
            "ci_status": "passed",
            "ci_checks": [{"provider": "github", "repository_id": "7", "check_name": "unit",
                           "head_sha": "4f2a9c1d0b7e6a5f4c3b2a1d0e9f8a7b6c5d4e3f",
                           "state": "passed", "delivery_id": "check-one"}],
            "can_merge": not blockers, "blockers": blockers,
        }

    def _review_of(self, row: dict) -> dict:
        return self.reviews.get(row["id"]) or self.review(row)

    def listing(self, include_done: bool) -> dict:
        rows = [t for t in self.tasks if include_done or t["status"] not in ("done", "dropped")]
        rows.sort(key=lambda t: (self.ORDER.get(t["status"], 9), t["priority"], t["created_at"]))
        counts = {s: sum(1 for t in self.tasks if t["status"] == s) for s in self.ORDER}
        counts["needs_you"] = len(self.needs_you)
        team = [{**{k: m[k] for k in ("id", "name", "color", "harness")}, "isolation": m.get("isolation", "worktree")} for m in self.staff]
        return {"project": self.project, "tasks": rows, "needs_you": self.needs_you, "counts": counts, "staff": team}

    def _assign(self, row: dict, staff_id: str | None) -> None:
        member = next((m for m in self.staff if m["id"] == staff_id), None)
        row["assignee_staff_id"] = member["id"] if member else None
        row["assignee"] = self.assignee(member["id"], member["name"], harness=member["harness"], color=member["color"]) if member else None

    def answer(self, method: str, path: str, query: str, body: dict | None) -> tuple[int, object] | None:
        """``(status, body)`` for a route of the board, or None for anything else."""
        base = f"/api/projects/{self.project['id']}/board"
        stops = f"/api/projects/{self.project['id']}/unknown-stops"
        if path == stops and method == "GET":
            if self.unknown_stops_failed:
                return 503, {"detail": "inspection unavailable"}
            from urllib.parse import parse_qs
            params = parse_qs(query)
            after = params.get("after", [""])[0]
            limit = int(params.get("limit", ["50"])[0])
            rows = [row for row in sorted(self.unknown_stops, key=lambda row: row["id"]) if row["id"] > after]
            return 200, {"items": rows[:limit], "next_after": rows[limit - 1]["id"] if len(rows) > limit else None}
        if path.startswith(stops + "/") and path.endswith("/reconcile") and method == "POST":
            attempt_id = path[len(stops) + 1:-len("/reconcile")]
            self.stop_reconciliations.append(attempt_id)
            row = next((item for item in self.unknown_stops if item["id"] == attempt_id), None)
            if row is None:
                return 404, {"detail": "no such timed-out attempt"}
            self.unknown_stops.remove(row)
            return 200, {"cancel_state": "completed"}
        if method == "GET" and path.startswith("/api/board/") and path.endswith("/context-history"):
            task_id = path.split("/")[3]
            if not any(task["id"] == task_id for task in self.tasks):
                return 404, {"detail": "no such task"}
            return 200, {"task_id": task_id, "entries": []}
        if method == "GET" and path.startswith("/api/board/") and path.endswith("/context"):
            task_id = path.split("/")[3]
            task = next((item for item in self.tasks if item["id"] == task_id), None)
            if task is None:
                return 404, {"detail": "no such task"}
            return 200, {"task_id": task_id, "title": task["title"], "role": "worker",
                         "contract_revision": 1, "contract": {"brief": {}, "requirements": [], "checklist": []},
                         "dependencies": [], "facts": [], "artifacts": [], "source_refs": [], "packet_hash": "a" * 64}
        if path == "/api/board-workflows/validate" and method == "POST":
            return 200, {"valid": True, "topology": [node["id"] for node in (body or {}).get("nodes", [])], "required_capabilities": [], "definition_digest": "a" * 64}
        if path == "/api/board-workflows/runs" and method == "POST":
            payload = dict(body or {})
            if payload.get("project_id") != self.project["id"]:
                return None
            key = payload.get("client_operation_id", "")
            if key in self.workflow_commands:
                return 200, self.workflow_commands[key]
            row = next((task for task in self.tasks if task["id"] == payload.get("task_id")), None)
            if row is None or row["entity_revision"] != payload.get("expected_entity_revision"):
                return 409, {"detail": "task revision changed"}
            if any(run["task_id"] == row["id"] and run["status"] in {"pending", "running", "blocked"} for run in self.workflow_runs.values()):
                return 409, {"detail": "task already has an active workflow"}
            run_id = f"workflow-{len(self.workflow_runs) + 1}"
            nodes = payload["nodes"]
            self.workflow_runs[run_id] = {"run_id": run_id, "task_id": row["id"], "status": "running", "created_at": "2026-09-24T10:00:00Z",
                                          "definition_value": {"nodes": nodes, "edges": payload["edges"], "budget": payload["budget"]},
                                          "projection_current": True,
                                          "steps": [{"node_id": node["id"], "task_id": node["task_id"], "kind": node["kind"], "status": "ready" if node["kind"] == "approval" else "pending",
                                                     "step_revision": 1, "source_contract_revision": 1, "current_input_digest": "b" * 64, "input_digest": "b" * 64,
                                                     "source_current": True, "projected_state": "ready", "can_approve": node["kind"] == "approval" and len(nodes) == 1,
                                                     "approval_blocker": "predecessor_unready" if node["kind"] == "approval" and len(nodes) > 1 else None,
                                                     "receipt_id": None} for node in nodes]}
            row["entity_revision"] += 1
            receipt = {"run_id": run_id, "status": "pending", "receipt_id": f"receipt-{run_id}", "entity_revision": row["entity_revision"]}
            self.workflow_commands[key] = receipt
            if self.workflow_unknowns:
                self.workflow_unknowns -= 1
                return 503, {"detail": "connection interrupted after submission"}
            return 200, receipt
        if path.startswith("/api/board-workflows/runs/"):
            parts = path.split("/")
            run = self.workflow_runs.get(parts[4]) if len(parts) >= 5 else None
            if run is None:
                return 404, {"detail": "no such workflow"}
            if len(parts) == 5 and method == "GET":
                task_runs = [item for item in self.workflow_runs.values() if item["task_id"] == run["task_id"]]
                newer = task_runs[-1] if task_runs and task_runs[-1]["run_id"] != run["run_id"] else None
                steps = [{**step, "can_approve": False, "approval_blocker": "newer_run"} if newer and step["kind"] == "approval" else step for step in run["steps"]]
                return 200, {**run, "superseded_by": newer["run_id"] if newer else None, "steps": steps}
            if method == "POST" and len(parts) == 6 and parts[5] == "reconcile":
                step = next((step for step in run["steps"] if step["node_id"] == (body or {}).get("node_id")), None)
                if step is None:
                    return 404, {"detail": "no such workflow step"}
                current = step["status"] == "completed" and (body or {}).get("expected_input_hash") == step["input_digest"]
                return 200, {"run_id": run["run_id"], "node_id": step["node_id"], "decision": "reuse" if current else "needs_review",
                             "reason": "matching_verified_receipt" if current else "source_changed", "receipt_id": step["receipt_id"],
                             "current_input_hash": step["current_input_digest"]}
            if method == "POST" and len(parts) == 6 and parts[5] == "cancel":
                run["status"] = "cancelled"
                return 200, {"run_id": run["run_id"], "status": "cancelled", "receipt_id": "receipt-cancel"}
            if method == "POST" and len(parts) == 8 and parts[5] == "steps" and parts[7] == "approve":
                step = next((step for step in run["steps"] if step["node_id"] == parts[6]), None)
                if step is None or not step["can_approve"] or (body or {}).get("expected_input_digest") != step["current_input_digest"]:
                    return 409, {"detail": "approval source changed"}
                step.update(status="completed", can_approve=False, receipt_id="receipt-approval")
                run["status"] = "completed"
                return 200, {"run_id": run["run_id"], "status": "completed", "receipt_id": "receipt-approval"}
        if path.startswith("/api/board/") and path.endswith("/board-workflows") and method == "GET":
            task_id = path.split("/")[3]
            row = next((task for task in self.tasks if task["id"] == task_id), None)
            if row is None:
                return 404, {"detail": "no such project task"}
            items = [run for run in reversed(list(self.workflow_runs.values())) if run["task_id"] == task_id]
            return 200, {"task_id": task_id, "project_id": self.project["id"], "task_entity_revision": row["entity_revision"],
                         "items": [{key: run[key] for key in ("run_id", "status", "created_at")} for run in items], "next_before": None}
        if path == "/api/control/effects/stop-1" and method == "GET":
            return 200, {"effect_id": "stop-1", "state": "pending"}
        if path.startswith("/api/control/effects/launch-") and method == "GET":
            identity = path.rsplit("/", 1)[-1]
            return 200, {"effect_id": identity, **self.launch_effects.get(identity, {"state": "pending"})}
        if path == "/api/control/revisions" and method == "GET":
            return 200, {"scope": {"kind": "project", "id": self.project["id"]}, "entity_revision": 1, "collection_revision": len(self.tasks) + 1}
        if path.startswith("/api/lifecycle/"):
            parts = path.split("/")
            if len(parts) < 5:
                return 404, {"detail": "no such parent"}
            kind, parent_id = parts[3], parts[4]
            name = f"{kind}:{parent_id}"
            if kind == "task":
                row = next((task for task in self.tasks if task["id"] == parent_id), None)
                if row is None:
                    return 404, {"detail": "no such task"}
                children = ([{"parent_kind": "task", "parent_id": parent_id, "child_kind": "execution_attempt", "child_id": f"a-{parent_id}", "generation": 1, "source_revision": 1, "cancel_state": "active"}]
                            if row["status"] == "doing" else [])
                entity_revision = row["entity_revision"]
            elif kind == "project_goal" and parent_id == self.project["id"]:
                children = [{"parent_kind": "project_goal", "parent_id": parent_id, "child_kind": "task", "child_id": "t-checkout", "generation": 1, "source_revision": 1, "cancel_state": "active"}]
                entity_revision = 1
            else:
                return 404, {"detail": "no such parent"}
            descendants = children + ([{"parent_kind": "task", "parent_id": "t-checkout", "child_kind": "execution_attempt", "child_id": "a-t-checkout", "generation": 1, "source_revision": 1, "cancel_state": "active"}]
                                      if kind == "project_goal" else [])
            if method == "GET" and len(parts) == 5:
                return 200, {"parent_kind": kind, "parent_id": parent_id, "project_id": self.project["id"], "entity_revision": entity_revision,
                             "source_revision": 1, "generation": 1, "cancel_state": self.cancel_states.get(name, "active"),
                             "children": children, "owned_descendants": descendants, "preview_fingerprint": "a" * 64}
            if method == "POST" and path.endswith("/cancel"):
                payload = dict(body or {})
                if payload.get("preview_fingerprint") != "a" * 64 or payload.get("expected_source_revision") != 1 or payload.get("expected_entity_revision") != entity_revision:
                    return 409, {"detail": "ownership changed"}
                self.cancellations.append((name, payload))
                self.cancel_states[name] = "requested"
                return 200, {"parent_kind": kind, "parent_id": parent_id, "cancel_state": "requested", "generation": 1,
                             "children": [{"kind": child["child_kind"], "id": child["child_id"], "status": "draining"} for child in descendants],
                             "receipt_id": "cancel-receipt", "entity_revision": entity_revision + 1}
        if path == base and method == "GET":
            return 200, self.listing("include_done=1" in query)
        if path == base and method == "POST":
            payload = dict(body or {})
            self.created.append(payload)
            brief = {"objective": "", "deliverable": "", "boundaries": "", "done_when": ""}
            brief.update(payload.get("brief") or {})
            row = self.task(f"n{len(self.tasks) + 1}", payload["title"], priority=int(payload.get("priority", 3)), brief=brief, depends_on=payload.get("depends_on") or [], project_id=self.project["id"])
            self._assign(row, payload.get("assignee_staff_id"))
            self.tasks.append(row)
            return 201, {"command": {"task_id": row["id"], "entity_revision": row["entity_revision"]}, "task": row}
        if path == "/api/board" and method == "POST":
            payload = dict(body or {})
            self.created.append(payload)
            row = self.task(f"n{len(self.tasks) + 1}", payload["title"], priority=int(payload.get("priority", 3)), project_id=None,
                            acceptance=payload.get("acceptance") or "", checklist=[{"text": text, "done": False} for text in payload.get("checklist") or []])
            self.tasks.append(row)
            return 201, {"command": {"task_id": row["id"], "entity_revision": row["entity_revision"]}, "task": row}
        if path == "/api/board" and method == "GET":
            # The host names a project's task's project on this listing, as the Board screen's card reads it.
            listed = [t for t in self.tasks if "include_done=1" in query or t["status"] not in ("done", "dropped")]
            return 200, [{**t, "project_name": self.project["name"] if t.get("project_id") else None} for t in listed]
        if path.startswith("/api/board/"):
            parts = path.split("/")
            row = next((t for t in self.tasks if t["id"] == parts[3]), None)
            if row is None:
                return 404, {"detail": "no such task"}
            if path.endswith("/ci/requirements") and method == "POST":
                payload = dict(body or {})
                self.ci_requirement_requests.append(payload)
                operation = payload.get("client_operation_id")
                if operation in self.ci_requirement_receipts:
                    return 200, self.ci_requirement_receipts[operation]
                if self.ci_requirement_conflicts:
                    self.ci_requirement_conflicts -= 1
                    row["entity_revision"] += 1
                    return 409, {"detail": "task changed"}
                if self.ci_requirement_failures:
                    self.ci_requirement_failures -= 1
                    return 503, {"detail": "receipt unavailable"}
                if payload.get("expected_entity_revision") != row["entity_revision"]:
                    return 409, {"detail": "task changed"}
                if payload.get("provider") != "github" or not str(payload.get("repository_id", "")).isdigit() or not payload.get("check_names"):
                    return 422, {"detail": "invalid checks"}
                row["entity_revision"] += 1
                row["contract_revision"] = row.get("contract_revision", 1) + 1
                row["status"] = "todo"
                row["acceptance_state"] = "returned"
                receipt = {"receipt_id": "ci-receipt", "entity_revision": row["entity_revision"],
                           "contract_revision": row["contract_revision"], "returned_from_review": True}
                self.ci_requirement_receipts[operation] = receipt
                return 200, receipt
            if path.endswith("/handoff-options") and method == "GET":
                source_harness = row.get("source_harness") or (row.get("assignee") or {}).get("harness")
                source_id = row.get("current_attempt_id")
                return 200, {"task_id": row["id"], "source_attempt_id": source_id,
                             "source_harness": source_harness, "source_released": bool(row.get("source_released")),
                             "entity_revision": row["entity_revision"], "contract_revision": row.get("contract_revision", 1),
                             "targets": [{"id": member["id"], "name": member["name"], "harness": member["harness"],
                                          "permission_mode": member.get("permission_mode", "default")}
                                         for member in self.staff if member["harness"] != source_harness and not member.get("archived_at")] if source_id else []}
            if path.endswith("/handoff-preview") and method == "GET":
                from urllib.parse import parse_qs

                args = parse_qs(query)
                source_id = args.get("source_attempt_id", [""])[0]
                target_id = args.get("target_staff_id", [""])[0]
                target = next((member for member in self.staff if member["id"] == target_id), None)
                if not target or source_id != row.get("current_attempt_id"):
                    return 409, {"detail": "stale handoff source or target"}
                released = bool(row.get("source_released"))
                return 200, {"ready": released, "preview_digest": "a" * 64,
                             "blockers": [] if released else ["source_runtime_not_released"],
                             "budget": None, "resource": {"state": "none"},
                             "capability": {"available": True, "reason": "", "version": "verified",
                                            "login": "yes", "cost": "unknown subscription price"},
                             "packet": {"source_result": row.get("source_result"), "artifacts": [],
                                        "criteria": row.get("checklist", []), "requirements": row.get("requirements", []),
                                        "open_obligations": [],
                                        "workspace": {"branch": row.get("branch"), "base_ref": "main"},
                                        "target_permission_mode": target.get("permission_mode", "default"),
                                        "source_permission_mode": (row.get("assignee") or {}).get("permission_mode", "default"),
                                        "history_portability": "none", "workspace_transfer": "none",
                                        "cost_state": "unpriced_or_external"}}
            if path.endswith("/continue-elsewhere") and method == "POST":
                payload = dict(body or {})
                if payload.get("source_attempt_id") != row.get("current_attempt_id") or not row.get("source_released"):
                    return 409, {"detail": "source runtime is not released"}
                if payload.get("expected_entity_revision") != row["entity_revision"]:
                    return 409, {"detail": "task changed before continuation"}
                if payload.get("preview_digest") != "a" * 64:
                    return 409, {"detail": "preview changed"}
                self.launched.append((row["id"], payload))
                row["entity_revision"] += 1
                row["status"] = "blocked"
                return 200, {"task_id": row["id"], "effect_id": f"handoff-{row['id']}",
                             "handoff_id": f"handoff-{row['id']}", "state": "queued",
                             "receipt_id": "receipt", "entity_revision": row["entity_revision"]}
            if path.endswith("/contract") and method == "GET":
                return 200, {"task_id": row["id"], "contract_revision": 1, "entity_revision": row["entity_revision"],
                             "checklist": [{"id": f"C{index}", "text": item["text"]} for index, item in enumerate(row["checklist"], 1)]}
            if path.endswith("/results") and method == "GET":
                if row["id"] in self.result_rows:
                    return 200, self.result_rows[row["id"]]
                if row["id"] == "t-hero":
                    return 200, [{"result_id": "res-hero", "task_id": "t-hero", "attempt_id": "attempt-hero", "contract_revision": 1,
                                  "outcome": "complete", "origin_kind": "worker", "author": "Olga", "original_preview": "Original worker report: image delivered",
                                  "original_digest": "a" * 64, "artifacts": [{"id": "artifact-hero", "artifact_kind": "file", "artifact_key": "hero.png", "digest": "b" * 64}],
                                  "checks": ["Image opens on a phone"], "limitations": [], "verification": "verified", "verdict_id": "verdict-hero",
                                  "current_result_id": "res-hero", "acceptance_state": "operator_approved", "accepted": True,
                                  "created_at": "2026-09-23T17:55:00Z"}]
                return 200, []
            if path.endswith("/results/res-hero/original") and method == "GET":
                return 200, {"original_text": "Original worker report: image delivered\nChecked on desktop and phone.\nNo outstanding changes."}
            if len(parts) == 7 and parts[4] == "results" and parts[6] == "comments" and method == "GET":
                return 200, []
            if len(parts) == 7 and parts[4] == "results" and parts[6] == "evidence" and method == "GET":
                return 200, [{"evidence_id": "evidence-endpoint", "criterion_id": "completion", "observation": "tests pass",
                              "verification": "verified", "manifest_digest_before": "b" * 64,
                              "manifest_digest_after": "b" * 64, "observed_at": "2026-09-24T09:26:00Z"}]
            if len(parts) == 7 and parts[4] == "results" and parts[6] == "merge" and method == "POST":
                review = self._review_of(row)
                if row["status"] != "review" or not row.get("branch") or not review["can_merge"] or row["id"] in self.merge_receipts:
                    return 409, {"detail": "the reviewed branch is not ready"}
                self.merge_requests.append((row["id"], dict(body or {})))
                self.merge_receipts[row["id"]] = {"id": "merge-endpoint", "result_id": parts[5],
                    "verdict_id": (body or {}).get("verdict_id"), "head_sha": "head", "base_sha": "base",
                    "merge_sha": "merged-head", "state": "queued", "error": None}
                return 200, {"state": "queued", "effect_id": "merge-endpoint"}
            if len(parts) == 7 and parts[4] == "results" and parts[6] == "return" and method == "POST":
                note = str((body or {}).get("reason") or "")
                if not note or row["status"] != "review":
                    return 409, {"detail": "the reviewed result cannot be returned"}
                self.rejected.append((row["id"], note))
                row.update(status="doing", merge_state="rejected", acceptance_state="returned")
                return 200, {"result_id": parts[5], "state": "returned"}
            if method == "POST" and path.endswith("/accept"):
                if row["status"] != "review":
                    return 409, {"detail": f"only a task in review can be accepted; this one is {row['status']}"}
                if row.get("branch") and row.get("merge_state") != "merged" and not self._review_of(row)["can_merge"]:
                    return 409, {"detail": "; ".join(b["text"] for b in self._review_of(row)["blockers"])}
                self.accepted.append(row["id"])
                row["status"] = "done"
                if row.get("branch"):
                    row["merge_state"] = "merged"
                return 200, row
            if path.endswith("/review") and method == "GET":
                if not row.get("branch"):
                    return 409, {"detail": f"task {row['id']} has no staff branch to review"}
                receipt = self.merge_receipts.get(row["id"])
                return 200, {**self._review_of(row), "status": row["status"], "head_sha": "head", "base_sha": "base",
                             "current_sha": "merged-head" if receipt and receipt["state"] == "merged" else "base", "merge_receipt": receipt}
            if path.endswith("/stop") and method == "POST":
                self.stops.append((row["id"], dict(body or {})))
                return 200, {"effect_id": "stop-1", "task_id": row["id"], "state": "queued", "receipt_id": "receipt", "entity_revision": row["entity_revision"] + 1}
            if path.endswith("/launch") and method == "POST":
                payload = dict(body or {})
                if self.launch_unknowns:
                    self.launch_unknowns -= 1
                    self.unknown_launches.append((row["id"], payload))
                    return 503, {"detail": "connection interrupted after submission"}
                if self.launch_failures:
                    self.launch_failures -= 1
                    return 409, {"detail": "launch policy changed"}
                if payload.get("expected_entity_revision") != row["entity_revision"]:
                    return 409, {"detail": "the task changed before launch"}
                self.launched.append((row["id"], payload))
                self._assign(row, payload.get("staff_id"))
                row["entity_revision"] += 1
                return 200, {"task_id": row["id"], "effect_id": f"launch-{row['id']}", "state": "queued", "receipt_id": "receipt", "entity_revision": row["entity_revision"]}
            if path.endswith("/merge") and method == "POST":
                review = self._review_of(row)
                if row["status"] != "review" or not review["can_merge"]:
                    return 409, {"detail": "; ".join(b["text"] for b in review["blockers"]) or "not in review"}
                self.merged.append(row["id"])
                row.update(status="done", merge_state="merged")
                return 200, {**row, "merge": {"commit": "c0ffee00" * 5, "into": "main", "worktree_removed": True, "branch_deleted": True}}
            if path.endswith("/reject") and method == "POST":
                note = str((body or {}).get("note") or "")
                if not note:
                    return 422, {"detail": "a note is required"}
                self.rejected.append((row["id"], note))
                row.update(status="doing", merge_state="rejected")
                return 200, {**row, "told": True}
            if method == "PUT":
                payload = dict(body or {})
                self.updated.append((row["id"], payload))
                if "assignee_staff_id" in payload:
                    self._assign(row, payload["assignee_staff_id"] or None)
                if "brief" in payload:
                    row["brief"] = {**row["brief"], **payload["brief"]}
                for key in ("title", "status", "priority", "depends_on"):
                    if key in payload:
                        row[key] = payload[key]
                for key, done in (("check_ids", True), ("uncheck_ids", False)):
                    for criterion_id in payload.get(key) or []:
                        index = int(criterion_id[1:]) - 1
                        row["checklist"][index]["done"] = done
                if payload.get("note"):
                    row["notes"] = (row["notes"] + "\n" if row["notes"] else "") + payload["note"]
                row["entity_revision"] += 1
                return 200, {"command": {"task_id": row["id"], "entity_revision": row["entity_revision"]}, "task": row}
            if method == "DELETE":
                row["status"] = "dropped"
                row["entity_revision"] += 1
                return 200, {"archived": True, "command": {"task_id": row["id"], "entity_revision": row["entity_revision"]}, "task": row}
        return None


# Small documents with deliberately different structures make the explorer and preview checks
# exercise parsing, navigation and media decoding without reading anybody's real workspace.
FILE_TEXT = {
    "src/main.py": "# A small example\nfrom pathlib import Path\n\ndef greeting(name):\n    return f'Hello {name}'\n\nprint(greeting('reader'))\n",
    "src/helper.ts": "export const count: number = 42;\n",
    "data/menu.json": '{"items":[{"name":"Bread","price":3}]}',
    "data/sample.json": '{"title":"Example","items":[{"name":"Bread","price":3}],"ready":true}',
    "data/sample.csv": 'name,price,note\nBread,3,"fresh, daily"\nTea,2,hot\n',
    "data/sample.tsv": "name\tprice\nBread\t3\nTea\t2\n",
    "site/index.html": """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>
body{margin:0;padding:32px;color:#e8eaf3;background:#111421;font:14px/1.6 system-ui}h1{font-size:28px;margin:0 0 8px}p{color:#aab2cb}.status{display:inline-block;color:#65d8a3;border:1px solid #315a50;border-radius:24px;padding:6px 14px}.facts{display:flex;gap:12px;margin:28px 0}.facts article{flex:1;padding:16px;background:#1a1f31;border:1px solid #2b3044;border-radius:12px}.facts b{display:block;font-size:24px}.facts span{color:#aab2cb;font-size:12px}section{padding:20px;background:#1a1f31;border:1px solid #2b3044;border-radius:12px;margin:20px 0}h2{font-size:18px;margin:0 0 14px}table{width:100%;border-collapse:collapse}td{border-bottom:1px solid #2b3044;padding:10px 0}a{color:#b0beff;margin-right:16px}@media(max-width:420px){body{padding:20px}.facts{flex-wrap:wrap}}
</style></head><body><h1>Bakery report</h1><p>The seasonal menu, checked on a phone.</p><div class="status">All checks passed</div><div class="facts"><article><b>27</b><span>Menu items</span></article><article><b>41</b><span>Links checked</span></article><article><b>46 KB</b><span>Hero image</span></article></div><section><h2>What changed</h2><p>Seasonal items now come first. Prices read from the same sheet as the shop menu. The hero image loads quickly on a small screen.</p></section><section id="details"><h2>Details section</h2><table><tr><td>Mobile performance</td><td>98 / 100</td></tr><tr><td>Broken links</td><td>0</td></tr><tr><td>Price mismatches</td><td>0</td></tr></table></section><a href="next.html">Next page</a><a href="#details">Details</a></body></html>""",
    "site/next.html": '<!doctype html><html><body><h1>Second page</h1><a href="index.html">First page</a></body></html>',
    "change.diff": "diff --git a/menu.py b/menu.py\n--- a/menu.py\n+++ b/menu.py\n@@ -1,2 +1,2 @@\n-old = 1\n+new = 2\n keep = True\n",
    "change.patch": "--- a/menu.py\n+++ b/menu.py\n@@ -1 +1 @@\n-old = 1\n+new = 2\n",
}


def file_entries(path: str) -> list[dict]:
    """One level only, the same contract as the directory browse route."""
    prefix = f"{path}/" if path else ""
    entries = {}
    for name, body in FILE_TEXT.items():
        if not name.startswith(prefix):
            continue
        child = name[len(prefix):].split("/", 1)[0]
        entries[child] = {"name": child, "dir": "/" in name[len(prefix):], "size": len(body), "mtime": 0}
    return list(entries.values())


def file_search(path: str, query: str, limit: int = 200) -> dict | None:
    """Both bounded search routes, shared by screenshots and behaviour checks."""
    import fnmatch
    if path.endswith("/files/search"):
        names = [name for name in FILE_TEXT if query.lower() in name.rsplit("/", 1)[-1].lower() or fnmatch.fnmatch(name.lower(), query.lower()) or fnmatch.fnmatch(name.rsplit("/", 1)[-1].lower(), query.lower())]
        return {"query": query, "results": [{"path": name, "kind": "file", "size": len(FILE_TEXT[name]), "mtime": 0} for name in names[:limit]], "truncated": len(names) > limit, "engine": "walk"}
    if path.endswith("/files/grep"):
        hits = [{"path": name, "line": i, "text": line} for name, body in FILE_TEXT.items() for i, line in enumerate(body.splitlines(), 1) if query.lower() in line.lower()]
        return {"query": query, "hits": hits[:limit], "truncated": len(hits) > limit}
    return None


def question_view(ask: dict, project_name: str, *, asker: str = "", always: bool = False, rules: bool = False) -> dict:
    """One entry of GET /api/questions, as ``daedalus/extensions/questions.py`` draws a request.
    ``rules``: the member's CLI keeps standing rules (Claude Code), so an MCP tool's "Always" may
    cover its whole server (``always_server``)."""
    detail = ask.get("detail") or {}
    tool = str(detail.get("tool") or "")
    server = tool.split("__")[1] if rules and ask["kind"] == "permission" and tool.startswith("mcp__") and tool.count("__") >= 2 else ""
    options = [o for o in detail.get("options") or [] if isinstance(o, str)]
    title = str(ask.get("title") or "")
    heading = title or next((line.strip() for line in ask["text"].splitlines() if line.strip()), "")
    origin = ask["origin"]
    return {
        **ask, "title": title, "heading": heading, "project_name": project_name,
        "asker": asker or ("main" if origin == "dispatcher" else "orchestrator" if origin == "orchestrator" else "staff"),
        "section": "requests" if origin == "staff" else "questions",
        "options": options, "multi": bool(detail.get("multi")) and len(options) > 1,
        "host": bool(ask.get("host")), "always": always, "always_server": server if always else "", "urgent": bool(detail.get("urgent")),
    }


def answer_batch(asks: list[dict], items: list[dict], *, via: str, project_id: str | None = None, refusals: dict[str, str] | None = None, late: dict[str, dict] | None = None) -> dict:
    """POST …/asks/answer as the host answers it: each item its own outcome, the first answer winning.

    ``late`` holds requests that were answered elsewhere a moment before the send (the phone,
    Telegram), with that answer's resolution; ``refusals`` approvals the host takes and cannot carry out.
    """
    results: list[dict] = []
    for item in items:
        ask = next((a for a in asks if a["id"] == item.get("ask_id")), None)
        if ask is None:
            results.append({"ask_id": item.get("ask_id"), "state": "missing", "error": "no such request"})
            continue
        base = {"ask_id": ask["id"], "short_id": ask["short_id"]}
        if project_id is not None and ask["project_id"] != project_id:
            results.append({**base, "state": "refused", "error": "that request is not this project's"})
            continue
        if ask["id"] in (late or {}) and not ask["resolved_at"]:
            ask.update(resolved_at="2026-09-25T09:59:00Z", resolved_by="operator", resolution=late[ask["id"]])  # type: ignore[index]
        if ask["resolved_at"]:
            results.append({**base, "state": "conflict", "answered_by": ask["resolved_by"], "withdrawn": ask["resolved_by"] == "system", "ask": ask})
            continue
        resolution = {"allow": item.get("allow"), "text": item.get("text") or "", "selected": item.get("selected") or [], "via": via, "batch": "b-stub"}
        if item.get("note"):
            resolution["note"] = item["note"]
        refused = (refusals or {}).get(ask["id"], "")
        if refused:
            resolution.update(outcome=f"approved, but the folder could not be added: {refused}", error=refused)
        ask.update(resolved_at="2026-09-25T10:00:00Z", resolved_by="operator", resolution=resolution)
        results.append({**base, "state": "answered", "delivered": not refused, "error": refused, "ask": ask})
    return {"batch_id": "b-stub", "results": results}


LONG_LINK = "https://example.org/docs/readme-and-badges/a-very-long-path-with-no-spaces-at-all/that-runs-on-past-the-edge-of-any-phone"
LONG_OPTIONS = {
    "en": {
        "title": "Where to start the launch",
        "text": "Pick what goes first; the others follow in the order you choose.",
        "options": [
            "Links and badges: the project's README, the maintainer's profile, .github",
            "Repository showcase: topics, descriptions, social preview, the core's README",
            "Channels: Telegram, X, LinkedIn and drafts of the first posts",
            LONG_LINK,
        ],
    },
    "ru": {
        "title": "С чего начать запуск",
        "text": "Выберите, что идёт первым; остальное — в том порядке, который вы укажете.",
        "options": [
            "Ссылки и бейджи: README проекта, профиль сопровождающего, .github",
            "Витрина репозитория: топики, описания, social preview, README ядра",
            "Каналы: Telegram, X, LinkedIn и черновики первых постов",
            LONG_LINK,
        ],
    },
}
"""The long-options question in both languages (``FocusStub.question_of_long_options``)."""


def empty_focus_state(pid: str) -> dict:
    """``GET /api/projects/<id>/focus-state`` of a project with nothing in hand: the chat draws no line."""
    return {"project_id": pid, "counts": {"in_work": 0, "decisions": 0, "waiting_for_you": 0, "unconfirmed": 0, "commitments": 0}, "goals": [], "open_results": [], "commitments": [], "receipts": {}}


def steps_markdown(steps: dict, member: str, task: str) -> str:
    """The steps as the host writes them into the chat and the file (``OperatorSteps.markdown``), in English as the host writes."""
    lines = [f"# {steps['goal']}", "", f"From {member}" + (f", task {task}" if task else "") + "."]
    if steps["verified"] != "on-running-version":
        lines += ["", "**Not checked on the running version.**" + (f" {steps['verified_how']}" if steps["verified_how"] else "")]
    if steps["roles"]:
        lines += ["", "| account | what it is for |", "|---|---|", *(f"| {r['account']} | {r['purpose']} |" for r in steps["roles"])]
    lines += ["", *(f"{i}. {step}" for i, step in enumerate(steps["steps"], start=1))]
    for label, key in (("Expected", "expected"), ("How to check", "check"), ("Limits", "limits")):
        if steps[key]:
            lines += ["", f"**{label}:** {steps[key]}"]
    return "\n".join(lines) + "\n"


class FocusStub:
    """A project with its orchestrator switched on, as focus mode reads it, kept between requests.

    Everything the focus column, the orchestrator's chat and the project's pages ask for is answered
    here: the project list and the agents listing (with the counts orchestration mode lists the
    project by), the sessions of the orchestrator and of a Daedalus staff member, the project's
    requests (answered once, the second answer refused as the host refuses it), the brief, the
    journal in pages, the wake-ups, the terminals, what was sent to a member and the member's
    controls. The team and the board are the two stubs above. What the page sent is recorded, so a
    check can assert the exact request.

    ``bakery(lang)`` invents the one project the check and the pictures share; the operator's words and
    the orchestrator's answers are in the page's language, and the lines the host writes for the
    orchestrator stay in English, as the host writes them.
    """

    def __init__(self, *, projects: list[dict], listing: dict, details: dict[str, dict], team: TeamStub, board: BoardStub, others: list[TeamStub | BoardStub] | None = None, asks: list[dict], brief: list[dict], journal: list[dict], schedules: list[dict], rules: list[dict] | None = None, wakeups: list[dict] | None = None, watches: list[dict] | None = None, terminals: list[dict], messages: dict[str, list[dict]]) -> None:
        self.projects = projects
        self.listing = listing
        self.details = details
        self.team = team
        self.board = board
        self.others = others or []
        self.asks = asks
        self.brief = brief
        self.journal = journal
        self.rules: list[dict] = list(rules or [])
        """The operator's rules in force, pinned over the journal; ``ruled`` and ``lifted`` are what the page
        made and lifted."""
        self.ruled: list[str] = []
        self.lifted: list[int] = []
        self.schedules = schedules
        self.wakeups = wakeups or []
        self.wake_collection_revision = 1
        self.woken: list[dict] = []
        self.watches = watches or []
        self.coordinator_handoffs: dict[str, dict] = {}
        self.watched: list[tuple[str, dict]] = []
        self.watch_collection_revision = 1
        self.watch_project_revision = 1
        self.watch_receipts: dict[str, tuple[dict, dict]] = {}
        self.watch_lost_reply = False
        self.terminals = terminals
        self.messages = messages
        self.answers: list[tuple[str, dict]] = []
        self.batches: list[tuple[str, list[dict]]] = []
        """Every batch the Questions tab sent: the path, and the items exactly as posted."""
        self.late: dict[str, dict] = {}
        self.answered_first: dict[str, str] = {}
        """Requests someone else answers the moment the operator's answer arrives, and who: the request
        stays open on the page until then, so no read of the page's in between can take it away."""
        """Requests answered elsewhere a moment before the next send, with that answer."""
        self.refusals: dict[str, str] = {}
        """Requests whose approval the host takes and then cannot carry out, by id, with the reason
        it answers: a folder the host refuses to add."""
        self.enabled: list[tuple[str, dict]] = []
        self.notes: list[str] = []
        self.briefed: list[dict] = []
        self.controls: list[tuple[str, str]] = []
        self.staff_views: dict[str, dict] = {}
        """What the staff view reads of a command-line member: ``session``, ``turns``, ``events``, ``changes``."""
        self.sent: list[tuple[str, dict]] = []
        """Every message the operator posted to a member, from the staff view or a terminal card."""
        self.seen: list[str] = []
        self.revoked: list[tuple[str, str]] = []
        """Every standing grant the operator took back: ``(staff id, rule)``."""
        self.focus_states: dict[str, dict] = {}
        """What ``GET /api/projects/<id>/focus-state`` answers, by project; a project not here has an empty one."""
        self.next_actions: dict[str, list[dict]] = {}
        """Operator-owned next steps returned by the project-wide readiness projection."""
        self.budget_views: dict[str, dict] = {}
        self.budget_read_error: set[str] = set()
        self.posted: list[tuple[str, dict]] = []
        """Every message the operator sent to a chat of the project: ``(session id, body)``, as posted."""
        self.search_busy = False
        """Whether a chat's search answers 429, as the host does while its search runs for someone else."""
        self.searched: list[tuple[str, str]] = []
        """Every search inside a chat: ``(session id, words)``."""

    def project(self, pid: str) -> dict | None:
        return next((p for p in self.projects if p["id"] == pid), None)

    def answer(self, method: str, path: str, query: str, body: dict | None) -> tuple[int, object] | None:
        """``(status, body)`` for a route of focus mode, or None for anything else."""
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        if path == "/api/projects" and method == "GET":
            return 200, self.projects
        if path == "/api/sessions" and method == "GET":
            return 200, self.listing
        if path.startswith("/api/sessions/") and method == "GET" and path.count("/") == 3:
            sid = path.split("/")[3]
            if sid in self.details:
                return 200, session_page(self.details[sid], params)
        if path.startswith("/api/sessions/") and path.endswith("/search") and method == "GET" and path.count("/") == 4:
            sid = path.split("/")[3]
            if sid in self.details:
                words = unquote_plus(params.get("q", ""))
                self.searched.append((sid, words))
                if self.search_busy:
                    return 429, {"detail": "search is busy"}
                return 200, search_messages(self.details[sid]["messages"], words, int(params.get("limit") or 30))
        if path == "/api/asks" and method == "GET":
            rows = [a for a in self.asks if a["project_id"] == params.get("project")]
            if params.get("open") != "0":
                rows = [a for a in rows if not a["resolved_at"]]
            if params.get("routed_to"):
                rows = [a for a in rows if a["routed_to"] == params["routed_to"]]
            return 200, {"asks": rows}
        if path == "/api/questions" and method == "GET":
            names = {m["id"]: m for m in self.team.staff}
            orchestrated = {p["id"] for p in self.projects if (p.get("settings") or {}).get("orchestrator", {}).get("enabled")}
            wanted = params.get("project")
            rows = [a for a in self.asks if not a["resolved_at"] and a["routed_to"] == "operator" and (a["project_id"] == wanted if wanted else a["project_id"] in orchestrated)]
            rows.sort(key=lambda a: a["created_at"])

            def view(a: dict) -> dict:
                member = names.get(a.get("staff_id") or "")
                project = self.project(a["project_id"]) or {}
                return question_view(a, project.get("name", ""), asker=member["name"] if member else "", always=bool(member) and a["kind"] == "permission" and member["harness"] != "daedalus", rules=bool(member) and member["harness"] == "claude")

            return 200, {"questions": [view(a) for a in rows]}
        if (path == "/api/asks/answer" or (path.startswith("/api/projects/") and path.endswith("/asks/answer"))) and method == "POST":
            items = list(body or []) if isinstance(body, list) else []
            self.batches.append((path, items))
            pid = path.split("/")[3] if path.startswith("/api/projects/") else None
            return 200, answer_batch(self.asks, items, via="project" if pid else "main", project_id=pid, refusals=self.refusals, late=self.late)
        if path.startswith("/api/asks/") and path.endswith("/answer") and method == "POST":
            ref = path.split("/")[3]
            ask = next((a for a in self.asks if ref in (a["id"], a["short_id"])), None)
            if ask is None:
                return 404, {"detail": "no such request"}
            if ask["id"] in self.answered_first and not ask["resolved_at"]:
                ask.update(resolved_at="2026-09-25T10:00:00Z", resolved_by=self.answered_first[ask["id"]])
            if ask["resolved_at"]:
                return 409, {"detail": f"request {ask['short_id']} was already answered by the {ask['resolved_by']}"}
            payload = dict(body or {})
            self.answers.append((ask["id"], payload))
            self.board.needs_you = [n for n in self.board.needs_you if n["id"] != ask["id"]]
            ask.update(resolved_at="2026-09-24T10:00:00Z", resolved_by="operator", resolution={"allow": payload.get("allow"), "text": payload.get("text") or "", "selected": payload.get("selected") or [], "via": "app"})
            tool = str((ask.get("detail") or {}).get("tool") or "")
            view = self.staff_views.get(ask.get("staff_id") or "")
            if payload.get("allow") and payload.get("always") and tool.startswith("mcp__") and view is not None:
                # The host keeps an MCP tool's "Always" as the member's rule: the tool, or with
                # ``server`` every tool of its server.
                rule = "mcp__" + tool.split("__")[1] if payload.get("server") else tool
                rules = view["session"].setdefault("rules", [])
                if all(r["rule"] != rule for r in rules):
                    rules.append({"rule": rule, "created_at": "2026-09-24T10:00:00Z", "created_by": "operator"})
                ask["resolution"]["rule"] = rule
            refused = self.refusals.get(ask["id"], "")
            if refused:
                ask["resolution"].update(outcome=f"approved, but the folder could not be added: {refused}", error=refused)
            return 200, {"state": "answered", "delivered": not refused, "error": refused, "ask": ask}
        if path.startswith("/api/projects/") and path.endswith("/focus-state") and method == "GET":
            pid = path.split("/")[3]
            return 200, self.focus_states.get(pid) or empty_focus_state(pid)
        if path.startswith("/api/projects/") and path.endswith("/next-actions") and method == "GET":
            pid = path.split("/")[3]
            return 200, {"project_id": pid, "actions": self.next_actions.get(pid, [])}
        if path.startswith("/api/projects/") and path.endswith("/budget") and method == "GET":
            pid = path.split("/")[3]
            if pid in self.budget_read_error:
                return 503, {"detail": "budget unavailable"}
            if pid in self.budget_views:
                return 200, self.budget_views[pid]
        if path.startswith("/api/sessions/") and path.endswith("/messages") and method == "POST" and path.count("/") == 4:
            sid = path.split("/")[3]
            if sid in self.details:
                # The message lands in the transcript as the host writes it: what it answers kept
                # beside the words rather than in them (``transcript_view.message_view``).
                payload = dict(body or {})
                self.posted.append((sid, payload))
                messages = self.details[sid]["messages"]
                seq = max((m.get("seq") or 0 for m in messages), default=0) + 1
                reply = payload.get("reply_to")
                if reply is not None and not any(m.get("seq") == reply.get("seq") for m in messages):
                    return 404, {"detail": "that message is not in this conversation"}
                now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                messages.append({"role": "user", "seq": seq, "origin": "operator", "text": payload.get("text", ""), "thinking": "", "tool_calls": [], "tool_results": [], "created_at": now,
                                 "delivery": "steer" if payload.get("steer") else None, "reply_to": reply})
                # A missing receipt is an unconfirmed send: the composer must retain its draft and
                # reply quote. Mirror the host's consumed receipt once the idle session starts a run.
                run_id = f"run-{seq}"
                return 200, {"run_id": run_id, "receipt": {"status": "consumed", "run_id": run_id,
                                                          "client_message_id": payload.get("client_message_id")}}
        if path.startswith("/api/projects/") and path.endswith("/orchestrator/replace"):
            pid = path.split("/")[3]
            if method == "GET":
                return 200, {"handoff": self.coordinator_handoffs.get(pid)}
            if method == "POST":
                project = self.project(pid)
                if project is None:
                    return 404, {"detail": "no such project"}
                command = dict(body or {})
                office = project.get("settings", {}).get("orchestrator", {})
                if not office.get("enabled") or office.get("session_id") != command.get("expected_coordinator_session_id"):
                    return 409, {"detail": "coordinator changed"}
                handoff = {"handoff_id": f"handoff-{pid}", "state": "completed", "blocker": None,
                           "old_active": False, "new_active": True, "receipt_id": f"receipt-{pid}"}
                self.coordinator_handoffs[pid] = handoff
                office["session_id"] = f"orch-next-{pid}"
                project["entity_revision"] = int(project.get("entity_revision") or 1) + 1
                return 200, {"handoff_id": handoff["handoff_id"], "state": "retirement_pending",
                             "old_active": False, "receipt_id": handoff["receipt_id"], "session_id": office["session_id"],
                             "entity_revision": project["entity_revision"]}
        if path.startswith("/api/projects/") and path.endswith("/orchestrator") and method == "POST":
            pid = path.split("/")[3]
            project = self.project(pid)
            if project is None:
                return 404, {"detail": "no such project"}
            payload = dict(body or {})
            self.enabled.append((pid, payload))
            sid = f"orch-{pid}"
            project["settings"]["orchestrator"] = {"enabled": True, "session_id": sid, "model": payload.get("model", ""), "autonomy": payload.get("autonomy", "normal"), "concurrency": 6, "concurrency_cap": payload.get("concurrency_cap", 10), "telegram_topic_id": 0}
            self.details[sid] = FocusStub.session_detail(sid, f"Orchestrator · {project['name']}", project, [], orchestrator_of=pid)
            return 200, {**project["settings"]["orchestrator"], "effective_model": "strong", "project_id": pid}
        if path.startswith("/api/projects/") and path.endswith("/brief"):
            if method == "PUT":
                payload = dict(body or {})
                self.briefed.append(payload)
                section = next((s for s in self.brief if s["section"] == payload.get("section")), None)
                if section is not None:
                    section.update(body=payload.get("body", ""), updated_by="operator", updated_at="2026-09-24T10:00:00Z")
                return 200, section or {}
            return 200, {"sections": self.brief}
        if path.startswith("/api/projects/") and "/journal/" in path and path.endswith("/lift") and method == "POST":
            rule_id = int(path.split("/")[-2])
            rule = next((r for r in self.rules if r["id"] == rule_id), None)
            if rule is None:
                return 400, {"detail": f"entry #{rule_id} is not a rule of this project"}
            self.rules.remove(rule)
            self.lifted.append(rule_id)
            entry = {"id": max((e["id"] for e in self.journal), default=0) + 1, "at": "2026-09-24T10:00:00Z", "author": "operator", "kind": "rule_lifted", "text": f"Lifted rule #{rule_id}", "refs": {"rule_id": rule_id}}
            self.journal.insert(0, entry)
            return 200, entry
        if path.startswith("/api/projects/") and path.endswith("/journal"):
            if method == "POST":
                text = str((body or {}).get("text", ""))
                kind = str((body or {}).get("kind") or "note")
                if kind == "rule":
                    self.ruled.append(text)
                else:
                    self.notes.append(text)
                entry = {"id": max((e["id"] for e in [*self.journal, *self.rules]), default=0) + 1, "at": "2026-09-24T10:00:00Z", "author": "operator", "kind": kind, "text": text, "refs": {}}
                self.journal.insert(0, entry)
                if kind == "rule":
                    self.rules.append(entry)
                return 200, entry
            limit = int(params.get("limit", "50"))
            before = int(params["before"]) if params.get("before") else None
            rows = [e for e in self.journal if before is None or e["id"] < before][:limit]
            return 200, {"entries": rows, "next_before": rows[-1]["id"] if len(rows) == limit else None, "rules": self.rules}
        if path == "/api/schedules" and method == "GET":
            return 200, self.schedules
        if path == "/api/recurring/overview" and method == "GET":
            return 200, {"global_collection_revision": 1}
        if path == "/api/recurring/proposals" and method == "GET":
            return 200, {"entries": [], "collection_revisions": {"global:global": 1}}
        if path.startswith("/api/recurring/proposals/") and method == "POST":
            return 404, {"detail": "no such proposal"}
        if path == "/api/recurring/preview" and method == "POST":
            payload = body or {}
            return 200, {"next_run_at": payload.get("run_at") or "2026-09-25T09:00:00Z", "timezone": "UTC",
                         "output_contract": {"kind": payload.get("kind", "message")}, "authority": "operator approval with expiry required"}
        if path == "/api/recurring" and method == "POST":
            payload = body or {}
            if not payload.get("client_operation_id") or not payload.get("expected_collection_revision"):
                return 400, {"detail": "a command identity and revision are required"}
            created = {"id": "scheduled-new", "name": payload["name"], "prompt": payload["prompt"],
                       "kind": payload["kind"], "run_in": "new", "cron": payload.get("cron"),
                       "run_at": payload.get("run_at"), "enabled": 1, "next_run_at": payload.get("run_at") or "2026-09-25T09:00:00Z",
                       "last_run_at": None, "last_summary": None, "target_session": None, "failure_count": 0,
                       "last_error": None, "schedule_revision": 1, "authority_state": "current", "project_id": None}
            self.schedules.append(created)
            return 200, {"id": created["id"], "receipt_id": payload["client_operation_id"], "schedule_revision": 1,
                         "authority_state": "current", "entity_revision": 2}
        if path.startswith("/api/recurring/"):
            parts = path.split("/")
            schedule_id = parts[3]
            schedule = next((item for item in self.schedules if item["id"] == schedule_id), None)
            if schedule is None:
                return 404, {"detail": "no such schedule"}
            if len(parts) == 5 and parts[4] == "cycles" and method == "GET":
                return 200, {"schedule_id": schedule_id, "schedule_revision": schedule.get("schedule_revision", 1),
                             "authority_state": schedule.get("authority_state", "needs_approval"),
                             "collection_revision": 1, "scope": {"kind": "global", "id": "global"},
                             "cycles": []}
            if len(parts) == 5 and parts[4] == "approve" and method == "POST":
                schedule["authority_state"] = "current"
                schedule["schedule_revision"] = schedule.get("schedule_revision", 1) + 1
                return 200, {"id": schedule_id, "schedule_revision": schedule["schedule_revision"],
                             "authority_state": "current", "receipt_id": (body or {}).get("client_operation_id")}
            if len(parts) == 4 and method == "PATCH":
                schedule.update({key: value for key, value in (body or {}).items() if key in {"name", "prompt", "cron", "run_at", "enabled"}})
                schedule["schedule_revision"] = schedule.get("schedule_revision", 1) + 1
                schedule["authority_state"] = "needs_approval"
                return 200, {"id": schedule_id, "schedule_revision": schedule["schedule_revision"],
                             "authority_state": "needs_approval", "entity_revision": 2,
                             "receipt_id": (body or {}).get("client_operation_id")}
            if len(parts) == 5 and parts[4] == "remove" and method == "POST":
                self.schedules.remove(schedule)
                return 200, {"id": schedule_id, "deleted": True, "receipt_id": (body or {}).get("client_operation_id")}
            if len(parts) == 5 and parts[4] == "run" and method == "POST":
                return 200, {"cycle_id": "manual-cycle", "effect_id": "manual-effect", "state": "pending",
                             "receipt_id": (body or {}).get("client_operation_id")}
            if len(parts) == 7 and parts[4] == "cycles" and parts[6] == "reconcile" and method == "POST":
                return 200, {"cycle_id": parts[5], "outcome": (body or {}).get("outcome"),
                             "evidence_kind": "operator_attestation", "receipt_id": (body or {}).get("client_operation_id")}
            return 404, {"detail": "no such recurring action"}
        if path.startswith("/api/projects/") and "/watches" in path:
            parts = path.split("/")
            pid = parts[3]
            if len(parts) == 5 and method == "GET":
                return 200, {"watches": [w for w in self.watches if w["project_id"] == pid], "max": 50, "min_cooldown_minutes": 1, "providers": ["github"], "collection_revision": self.watch_collection_revision, "project_entity_revision": self.watch_project_revision}
            if len(parts) == 5 and method == "POST":
                payload = dict(body or {})
                self.watched.append(("create", payload))
                previous = self.watch_receipts.get(payload.get("client_operation_id", ""))
                if previous:
                    return (200, previous[1]) if previous[0] == payload else (409, {"detail": "intent changed"})
                assert payload["expected_collection_revision"] == self.watch_collection_revision and payload["client_operation_id"]
                row = {"id": f"w{len(self.watches) + 1}", "project_id": pid, "when": payload.get("when", {}), "then": payload.get("then", {}), "cooldown_minutes": payload.get("cooldown_minutes", 10), "once": bool(payload.get("once")), "note": payload.get("note", ""), "deadline_at": payload.get("deadline_at"), "latest_delivery": None,
                       "created_by": "operator", "created_at": "2026-09-24T10:00:00Z", "last_fired_at": None, "fire_count": 0, "enabled": True, "stopped": "", "last_error": "", "describe": "", "condition_revision": 1, "authority_state": "current"}
                self.watches.append(row)
                self.watch_collection_revision += 1
                self.watch_project_revision += 1
                receipt = {**row, "receipt_id": payload["client_operation_id"], "entity_revision": self.watch_project_revision}
                self.watch_receipts[payload["client_operation_id"]] = payload, receipt
                if self.watch_lost_reply:
                    self.watch_lost_reply = False
                    return 503, {"detail": "response lost"}
                return 200, receipt
            found = next((w for w in self.watches if w["project_id"] == pid and len(parts) == 6 and w["id"] == parts[5]), None)
            if found is None:
                return 404, {"detail": "no such watch"}
            if method == "PATCH":
                payload = dict(body or {})
                self.watched.append(("update", {"id": found["id"], **payload}))
                assert payload["expected_entity_revision"] == self.watch_project_revision and payload["expected_condition_revision"] == found["condition_revision"] and payload["client_operation_id"]
                found.update({k: v for k, v in payload.items() if v is not None})
                if payload.get("enabled"):
                    found["stopped"] = ""
                    found["authority_state"] = "current"
                found["condition_revision"] += 1
                self.watch_project_revision += 1
                return 200, {**found, "receipt_id": payload["client_operation_id"], "entity_revision": self.watch_project_revision}
            if method == "DELETE":
                payload = dict(body or {})
                self.watched.append(("delete", {"id": found["id"], **payload}))
                assert payload["expected_entity_revision"] == self.watch_project_revision and payload["expected_condition_revision"] == found["condition_revision"] and payload["client_operation_id"]
                self.watches.remove(found)
                self.watch_collection_revision += 1
                self.watch_project_revision += 1
                return 200, {"deleted": True, "receipt_id": payload["client_operation_id"], "entity_revision": self.watch_project_revision}
        if path.startswith("/api/projects/") and "/wakeups" in path:
            parts = path.split("/")
            pid = parts[3]
            if len(parts) == 5 and method == "GET":
                return 200, {"wakeups": [w for w in self.wakeups if w["project_id"] == pid],
                             "max": 20, "collection_revision": self.wake_collection_revision}
            if len(parts) == 5 and method == "POST":
                payload = dict(body or {})
                self.woken.append(payload)
                if not payload.get("client_operation_id") or payload.get("expected_collection_revision") != self.wake_collection_revision or not payload.get("expires_at"):
                    return 409, {"detail": "wake command changed"}
                if not str(payload.get("note", "")).strip():
                    return 400, {"detail": "a wake-up needs a note: what to look at when it fires"}
                at = payload.get("at") or ("2026-09-24T12:30:00Z" if payload.get("in_minutes") else None)
                row = {"id": f"wk{len(self.wakeups) + 1}", "project_id": pid, "note": payload["note"], "cron": payload.get("cron"), "at": at, "next_run_at": at or "2026-09-25T07:00:00Z", "last_run_at": None, "enabled": True, "set_by": "operator", "created_at": "2026-09-24T10:00:00Z", "schedule_revision": 1}
                self.wakeups.append(row)
                self.wake_collection_revision += 1
                return 200, row
            if len(parts) == 6 and method == "DELETE":
                payload = dict(body or {})
                found = next((w for w in self.wakeups if w["project_id"] == pid and w["id"] == parts[5]), None)
                if found and (payload.get("expected_collection_revision") != self.wake_collection_revision or payload.get("expected_schedule_revision") != found["schedule_revision"] or not payload.get("client_operation_id")):
                    return 409, {"detail": "wake command changed"}
                before = len(self.wakeups)
                self.wakeups = [w for w in self.wakeups if not (w["project_id"] == pid and w["id"] == parts[5])]
                if len(self.wakeups) < before:
                    self.wake_collection_revision += 1
                    return 200, {"deleted": True, "receipt_id": payload["client_operation_id"], "entity_revision": self.wake_collection_revision}
                return 404, {"detail": "no such wake-up"}
        if path == "/api/terminals" and method == "GET" and params.get("project_id"):
            rows = [term for term in self.terminals if term["project_id"] == params["project_id"]]
            return 200, {**GATES["/api/terminals"], "terminals": rows}  # type: ignore[dict-item]
        if path.startswith("/api/staff/"):
            parts = path.split("/")
            if len(parts) == 6 and parts[4] == "rules" and method == "DELETE":
                # Revoking a standing grant (``api_staff.py``): gone from the list the next launch reads.
                view = self.staff_views.get(parts[3])
                rules = view["session"].get("rules", []) if view is not None else []
                rule = unquote(parts[5])
                if all(r["rule"] != rule for r in rules):
                    return 404, {"detail": f"no standing grant {rule!r}"}
                view["session"]["rules"] = [r for r in rules if r["rule"] != rule]  # type: ignore[index]
                self.revoked.append((parts[3], rule))
                return 200, {"rules": view["session"]["rules"]}  # type: ignore[index]
            staff_view = self.staff_view_answer(method, parts, params, body)
            if staff_view is not None:
                return staff_view
            if len(parts) == 5 and parts[4] == "messages" and method == "GET":
                return 200, self.messages.get(parts[3], [])
            if len(parts) == 5 and parts[4] == "messages" and method == "POST":
                # A member without a staff view of its own here (a terminal card's compose line).
                self.sent.append((parts[3], dict(body or {})))
                return 200, {"state": "queued", "message_id": f"m{len(self.sent) + 10}", "degraded_to": None}
            if len(parts) == 5 and parts[4] in ("interrupt", "pause", "release") and method == "POST":
                self.controls.append((parts[3], parts[4]))
                member = next((m for m in self.team.staff if m["id"] == parts[3]), None)
                if member is not None and member["live"] and parts[4] == "pause":
                    member["live"]["pause_requested"] = True
                return 200, {"ok": True} if parts[4] != "pause" else {"state": "pausing"}
        for part in (self.team, self.board, *self.others):
            answered = part.answer(method, path, query, body)
            if answered is not None:
                return answered
        return None

    def staff_view_answer(self, method: str, parts: list[str], params: dict[str, str], body: dict | None) -> tuple[int, object] | None:
        """The staff view's own routes (``api_staff.py``): the session with its capabilities and health,
        the transcript from a turn on, the events, the changes, the operator's message and "seen"."""
        if len(parts) != 5:
            return None
        sid, what = parts[3], parts[4]
        member = next((m for m in self.team.staff if m["id"] == sid), None)
        if member is None:
            return None
        view = self.staff_views.get(sid)
        if what == "session" and method == "GET":
            if view is None:
                return 200, {"staff": {k: member[k] for k in ("id", "name", "harness", "project_id")}, "session": None}
            return 200, view["session"]
        if view is None:
            return None
        if what == "transcript" and method == "GET":
            since = int(params.get("since", "0") or 0)
            return 200, {"turns": [turn for turn in view["turns"] if turn["index"] >= since], "more": False}
        if what == "events" and method == "GET":
            return 200, {"events": view["events"]}
        if what == "changes" and method == "GET":
            return 200, view["changes"]
        if what == "seen" and method == "POST":
            self.seen.append(sid)
            return 200, {"ok": True}

        if what == "messages" and method == "POST":
            payload = dict(body or {})
            self.sent.append((sid, payload))
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            row = {"id": f"m{sid}-{len(self.sent)}", "staff_id": sid, "staff_session_id": view["session"]["session"]["id"], "origin": "operator", "text": payload.get("text", ""), "mode": payload.get("when", "now"),
                   "state": "queued", "attempts": 0, "created_at": now, "updated_at": now, "error": ""}
            self.messages.setdefault(sid, []).insert(0, row)
            return 200, {"state": "queued", "message_id": row["id"], "mode": row["mode"], "degraded_to": ""}
        return None

    def staff_view_of_ira(self, lang: str = "en") -> dict:
        """Ira as the staff view shows a command-line member (M5): Claude Code in her own worktree at
        its fourth turn, three messages in three states, a permission waiting on the operator, a Feed,
        events and changes. Naya (OpenCode, whose CLI cannot take a message into a running turn) gets
        a session too, so the composer's "now" can be checked against a second capability row."""
        words = FOCUS_WORDS[lang]
        pid = self.projects[0]["id"]
        now = datetime.now(UTC)

        def at(**back: float) -> str:
            return (now - timedelta(**back)).strftime("%Y-%m-%dT%H:%M:%SZ")

        ira = next(m for m in self.team.staff if m["id"] == "st-ira")
        ira["live"].update(terminal_id="tm-ira", status_at=at(minutes=18), worktree_path="/home/operator/work/bakery-site/.agents/worktrees/ira")
        ira["health"] = health(at, tools="connected")
        naya = next(m for m in self.team.staff if m["id"] == "st-naya")
        naya["live"].update(terminal_id="tm-naya", status_at=at(minutes=2))
        naya["health"] = health(at, tools="missing", silent=True)
        self.messages["st-ira"] = [
            {"id": "mi3", "staff_id": "st-ira", "staff_session_id": "ss-ira", "origin": "orchestrator", "text": words["ira.queued"], "mode": "after_turn", "state": "queued", "attempts": 0, "created_at": at(minutes=1), "updated_at": at(minutes=1), "error": ""},
            {"id": "mi2", "staff_id": "st-ira", "staff_session_id": "ss-ira", "origin": "operator", "text": words["ira.sent"], "mode": "now", "state": "submitted", "attempts": 1, "created_at": at(minutes=4), "updated_at": at(minutes=4), "error": ""},
            {"id": "mi1", "staff_id": "st-ira", "staff_session_id": "ss-ira", "origin": "orchestrator", "text": words["ira.accepted"], "mode": "now", "state": "acknowledged", "attempts": 1, "created_at": at(minutes=25), "updated_at": at(minutes=25), "error": "",
             "delivery": {"via": "paste", "degraded_to": "", "enters": 1, "written_at": at(minutes=25), "submitted_at": at(minutes=25), "acknowledged_at": at(minutes=25)}},
        ]
        permission = {
            "id": "ask-stripe", "short_id": "k7m2qd", "project_id": pid, "origin": "staff", "kind": "permission", "staff_id": "st-ira", "staff_session_id": "ss-ira", "task_id": "t-checkout",
            "request_ref": "toolu_1", "text": "Bash: npm install @stripe/stripe-js", "detail": {"tool": "Bash"}, "routed_to": "operator", "suggestion": "",
            "created_at": at(minutes=2), "routed_at": at(minutes=2), "resolved_at": None, "resolved_by": None, "resolution": {},
        }
        self.asks.insert(0, permission)

        def tool(name: str, summary: str, ok: bool | None = True) -> dict:
            return {"name": name, "summary": summary, "ok": ok}

        turns = [
            {"index": 0, "role": "orchestrator", "text": words["ira.task"], "tools": [], "started_at": at(minutes=58), "ended_at": at(minutes=58), "usage": None},
            {"index": 1, "role": "assistant", "text": words["ira.reply1"], "tools": [tool("Read", "src/lib/cart.ts"), tool("Update", "src/lib/cart.ts: +3 −1"), tool("Bash", "npm test -- checkout")], "started_at": at(minutes=57), "ended_at": at(minutes=40), "usage": {"input_tokens": 180000, "output_tokens": 12000, "cache_read_tokens": 90000, "cost_usd": None}},
            {"index": 2, "role": "orchestrator", "text": words["ira.accepted"], "tools": [], "started_at": at(minutes=25), "ended_at": at(minutes=25), "usage": None},
            {"index": 3, "role": "assistant", "text": words["ira.reply2"], "tools": [tool("Update", "src/lib/cart.ts: +2 −2")], "started_at": at(minutes=25), "ended_at": at(minutes=20), "usage": None},
            {"index": 4, "role": "user", "text": words["ira.sent"], "tools": [], "started_at": at(minutes=19), "ended_at": at(minutes=19), "usage": None},
            {"index": 5, "role": "assistant", "text": words["ira.reply3"], "tools": [tool("Bash", "npm install @stripe/stripe-js", None)], "started_at": at(minutes=18), "ended_at": "", "usage": None},
        ]
        events = [
            {"seq": 60, "at": at(minutes=2), "type": "permission.pending", "payload": {"summary": "Bash: npm install @stripe/stripe-js"}},
            {"seq": 59, "at": at(minutes=4), "type": "staff.message", "payload": {"message_id": "mi2", "state": "submitted"}},
            {"seq": 58, "at": at(minutes=20), "type": "staff.status", "payload": {"status": "turn_done_unseen", "waiting_for": ""}},
            {"seq": 57, "at": at(minutes=25), "type": "staff.message", "payload": {"message_id": "mi1", "state": "acknowledged"}},
            {"seq": 56, "at": at(minutes=40), "type": "staff.report", "payload": {"kind": "checkpoint", "text": "cart discount fixed\n[full report in report receipt]", "task_id": "t-checkout", "report_id": "report-checkout"}},
            {"seq": 55, "at": at(minutes=58), "type": "staff.channel", "payload": {"team_tools": "connected"}},
            {"seq": 54, "at": at(minutes=58), "type": "staff.status", "payload": {"status": "working", "waiting_for": ""}},
        ]
        changes = {"files": [{"path": "src/lib/cart.ts", "added": 120, "removed": 18}, {"path": "src/pages/checkout.tsx", "added": 60, "removed": 9}, {"path": "src/pages/checkout.test.tsx", "added": 32, "removed": 4}], "added": 212, "removed": 31, "untracked": ["src/lib/promo.ts"], "base": "main"}
        launch = {"harness": "claude", "model": "opus", "effort": "", "agent": "", "permission_mode": "acceptEdits", "env": "container", "version": "2.1.281", "launch_id": "l-ira", "companion_terminal_id": None,
                  "worktree": ira["live"]["worktree_path"], "branch": "agent/ira/checkout", "task_id": "t-checkout"}
        self.staff_views["st-ira"] = {
            "session": {"staff": {"id": "st-ira", "name": "Ira", "harness": "claude", "project_id": pid}, "session": {**ira["live"], "cli_session_id": "7c1e2f4a-claude-session"},
                        "capabilities": CAPABILITIES["claude"], "launch": launch, "channel": {"team_tools": "connected"}, "health": ira["health"], "requests": [permission],
                        "usage": {"input_tokens": 412000, "output_tokens": 31000, "cost_usd": None, "window_used_pct": None, "source": "subscription",
                                  "context_tokens": 386000, "context_window": None, "windows": [], "replies": 3, "at": at(minutes=20)}},
            "turns": turns, "events": events, "changes": changes,
        }
        naya_launch = {**launch, "harness": "opencode", "model": "", "permission_mode": "", "version": "1.18.23", "launch_id": "l-naya", "worktree": None, "branch": None, "task_id": "t-bot"}
        self.staff_views["st-naya"] = {
            "session": {"staff": {"id": "st-naya", "name": "Naya", "harness": "opencode", "project_id": pid}, "session": {**naya["live"], "cli_session_id": "ses_naya"},
                        "capabilities": CAPABILITIES["opencode"], "launch": naya_launch, "channel": {"team_tools": "missing"}, "health": naya["health"], "requests": [], "usage": None},
            "turns": turns[:2], "events": [], "changes": {"files": [], "added": 0, "removed": 0, "untracked": [], "detail": "no worktree of its own"},
        }
        return permission

    def browser_permission_of_ira(self) -> dict:
        """After ``staff_view_of_ira``: Ira's CLI asks to navigate her browser — a tool of an MCP server,
        so "Always" may cover the one tool or every tool of the server — and she already holds one
        standing grant from an earlier "Always", which the Session tab lists with its Revoke."""
        pid = self.projects[0]["id"]
        at = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tool = "mcp__daedalus_browser__BrowserNavigate"
        browse = {
            "id": "ask-browse", "short_id": "b9w4rx", "project_id": pid, "origin": "staff", "kind": "permission", "staff_id": "st-ira", "staff_session_id": "ss-ira", "task_id": "t-checkout",
            "request_ref": "toolu_2", "text": f"{tool}: https://example.com/photos/bread", "detail": {"tool": tool}, "routed_to": "operator", "suggestion": "",
            "created_at": at, "routed_at": at, "resolved_at": None, "resolved_by": None, "resolution": {},
        }
        self.asks.insert(0, browse)
        session = self.staff_views["st-ira"]["session"]
        session["requests"] = [*session["requests"], browse]
        session["rules"] = [{"rule": "mcp__daedalus_browser__BrowserSnapshot", "created_at": at, "created_by": "operator"}]
        return browse

    def question_of_long_options(self, lang: str = "en") -> dict:
        """A question whose options are sentences, and one a link with no spaces in it: the options once
        kept to one line each and pushed a phone's Questions sheet sideways."""
        words = LONG_OPTIONS[lang]
        row = {
            "id": "ask-launch", "short_id": "ql4nch", "project_id": self.projects[0]["id"], "origin": "orchestrator", "kind": "question", "staff_id": None, "staff_session_id": None,
            "task_id": None, "request_ref": "", "title": words["title"], "text": words["text"], "detail": {"options": words["options"], "multi": True},
            "routed_to": "operator", "suggestion": "", "created_at": "2026-09-24T09:40:00Z", "routed_at": "2026-09-24T09:40:00Z", "resolved_at": None, "resolved_by": None, "resolution": {},
        }
        self.asks.append(row)
        return row

    def questions_of_bakery(self, lang: str = "en") -> list[dict]:
        """What the Questions tab has to show beside the discount question: a batch the orchestrator
        asked (several options at once, one long enough to fold, one that blocks the work), a folder it
        wants, and what staff wait on — Ira's permission, which her CLI can grant always, and Lev's
        question the orchestrator escalated with a suggestion. The payment question is stored as an
        orchestrator could once ask it, "options only", which the tab no longer honours."""
        words = FOCUS_WORDS[lang]
        pid = self.projects[0]["id"]

        def ask(id_: str, short: str, at: str, **over: object) -> dict:
            row = {"id": id_, "short_id": short, "project_id": pid, "origin": "orchestrator", "kind": "question", "staff_id": None, "staff_session_id": None, "task_id": None,
                   "request_ref": "", "title": "", "text": "", "detail": {}, "routed_to": "operator", "suggestion": "", "created_at": at, "routed_at": at,
                   "resolved_at": None, "resolved_by": None, "resolution": {}}
            row.update(over)
            return row

        added = [
            ask("ask-pay", "qp4y01", "2026-09-24T09:56:00Z", title=words["title.pay"], text=words["ask.pay"], detail={"options": ["Stripe", "PayPal", words["cash"]], "multi": True, "allow_free": False}),
            ask("ask-open", "qo9d02", "2026-09-24T09:56:30Z", title=words["title.open"], text=words["ask.open"], detail={"urgent": True}),
            ask("ask-labs", "qf0ld3", "2026-09-24T09:57:00Z", kind="folder", title="", text="Add the host folder /home/operator/work/labs to Bakery 2.0? It is also in the project Labs.", detail={"options": ["Add", "Don't add"], "path": "/home/operator/work/labs", "env": "host"}),
            ask("ask-push", "qg1t04", "2026-09-24T09:52:00Z", origin="staff", kind="permission", staff_id="st-ira", staff_session_id="ss-ira", task_id="t-checkout", text="Exec: git push origin agent/ira/checkout"),
            ask("ask-font", "qf9n05", "2026-09-24T09:50:00Z", origin="staff", staff_id="st-lev", staff_session_id="ss-lev", text=words["ask.font"], detail={"options": ["Inter", "Georgia"]}, suggestion="Georgia", routed_at="2026-09-24T10:00:00Z"),
        ]
        self.asks.extend(added)
        return added

    def ask_from_ira(self, lang: str = "en") -> dict:
        """Ira's own question, escalated to the operator and older than the orchestrator's: the one a
        phone's banner shows first (M8). She works in a terminal of her own, which the phone opens."""
        words = FOCUS_WORDS[lang]
        pid = self.projects[0]["id"]
        ask = {
            "id": "ask-ira", "short_id": "q9w2e1", "project_id": pid, "origin": "staff", "kind": "question", "staff_id": "st-ira", "staff_session_id": "ss-ira",
            "task_id": "t-checkout", "request_ref": "", "text": words["ask.spring"], "detail": {"options": [words["ask.before"], words["ask.after"]]},
            "routed_to": "operator", "suggestion": "", "created_at": "2026-09-24T09:54:00Z", "routed_at": "2026-09-24T09:54:00Z", "resolved_at": None, "resolved_by": None, "resolution": {},
        }
        self.asks.insert(0, ask)
        ira = next(m for m in self.team.staff if m["id"] == "st-ira")
        # The host derives the board's "Needs you" from the same open requests, so hers is there too.
        self.board.needs_you.insert(0, {
            "id": ask["id"], "short_id": ask["short_id"], "origin": "staff", "kind": "question", "text": ask["text"], "suggestion": "",
            "created_at": ask["created_at"], "task_id": "t-checkout", "task_title": words["task.checkout"],
            "staff": {k: ira[k] for k in ("id", "name", "color", "harness")}, "session_id": None,
        })
        ira["live"]["terminal_id"] = "tm-ira"
        self.terminals.insert(0, {
            "id": "tm-ira", "env": "container", "title": "claude · Ira", "owner": {"kind": "staff", "id": "st-ira", "label": "Ira"}, "project_id": pid, "profile": "harness:claude",
            "sandbox": False, "cwd": "/home/operator/work/bakery-site", "status": "running", "exit_code": None, "exit_signal": None, "created_at": "2026-09-24T09:00:00Z",
            "exited_at": None, "last_output_at": "2026-09-24T09:54:00Z", "last_input_at": None, "cols": 80, "rows": 24,
        })
        return ask

    @staticmethod
    def session_detail(sid: str, title: str, project: dict, messages: list[dict], *, orchestrator_of: str | None = None, staff: dict | None = None, status: str = "idle") -> dict:
        root = project["folders"][0]["path"]
        return {
            "id": sid, "title": title, "status": status, "run_id": None, "compacting": None, "housekeeping": False, "error": "",
            "workspace": root, "workspace_name": root.rsplit("/", 1)[-1], "workspace_own": False, "workspace_sessions": [],
            "project": {k: v for k, v in project.items() if k != "sessions"}, "folder_id": project["folders"][0]["id"], "folders": [],
            "pending": None, "model": "Claude Opus 5", "provider": "claude", "thinking": True, "reasoning_effort": "high",
            "messages": messages, "mode": "", "usd_cap": None, "brief": "", "spawned_by": None, "tools_off": [], "loop": None, "services": [], "subagents": [],
            "subagent_of": None, "subagent_name": None, "leader_title": None, "orchestrator_of": orchestrator_of, "staff": staff, "telegram_linked": False,
            "verifications": {}, "first_seq": messages[0]["seq"] if messages else 0, "has_older": False,
            "context": {"tokens": 18400, "window": 200000, "messages": len(messages), "summaries": 1, "operator_turns": 1, "last_compaction": {"at": "2026-09-24T08:10:00Z", "reason": "auto"}},
            "usage": {"c": 12, "i": 184000, "o": 9100, "ch": 150000, "usd": 0.84, "c_today": 5, "i_today": 61000, "o_today": 2900, "usd_today": 0.31},
        }

    @classmethod
    def bakery(cls, lang: str = "en", *, others: list[dict] | None = None, other_sessions: list[dict] | None = None) -> FocusStub:
        pid, garden = "b4k3ry20f0c5", "9a4d3e2f1c0b"
        words = FOCUS_WORDS[lang]
        project_folders = [
            folder("/home/operator/work/bakery-site", is_git=True),
            folder("/home/operator/work/bakery-api", position=1, is_git=True),
            folder("/home/operator/work/bakery-bot", position=2, env="host", reach="terminals", reachable=False),
        ]
        orchestration = {"enabled": True, "session_id": "orch-bakery", "model": "", "autonomy": "normal", "concurrency": 6, "concurrency_cap": 10, "telegram_topic_id": 0}
        bakery = {"id": pid, "name": "Bakery 2.0", "folders": project_folders, "created_at": "2026-09-20T00:00:00Z", "settings": {"snapshots": True, "system": "", "ephemeral": False, "default_env": "container", "orchestrator": orchestration}, "system": "", "sessions": [{"id": "orch-bakery", "title": "Orchestrator · Bakery 2.0", "running": False}]}
        garden_project = {"id": garden, "name": "Garden", "folders": [folder("/home/operator/work/garden")], "created_at": "2026-09-22T00:00:00Z", "settings": {"snapshots": True, "system": "", "ephemeral": False}, "system": "", "sessions": []}
        projects = [bakery, garden_project, *(others or [])]

        def live(sid: str, status: str, task: str | None, session_id: str | None = None, **extra: object) -> dict:
            return {"id": sid, "session_id": session_id, "terminal_id": None, "status": status, "waiting_for": "", "task_id": task, "started_at": "2026-09-24T09:00:00Z", "ended_at": None, "branch": None, "worktree_path": None, "pause_requested": False, **extra}

        machine = [{"staff_id": "st-olga", "task_id": "t-hours", "priority": 2, "position": 1, "reason": "machine", "detail": "20 of the machine's 20 terminal sessions are running", "since": 1_727_164_800, "by": "orchestrator"}]
        staff = [
            TeamStub.member("st-ira", "Ira", harness="claude", status="working", sessions=9, color="orange", role=words["ira.role"], model="opus", permission_mode="acceptEdits"),
            TeamStub.member("st-max", "Max", harness="codex", status="turn_done_unseen", sessions=5, color="blue", role="API"),
            TeamStub.member("st-naya", "Naya", harness="opencode", status="permission", sessions=3, color="green", role=words["naya.role"], env="host", isolation="shared"),
            TeamStub.member("st-lev", "Lev", status="working", sessions=4, color="teal", role=words["lev.role"], agent="reviewer", model="strong"),
            TeamStub.member("st-olga", "Olga", harness="claude", sessions=1, color="violet", role=words["olga.role"], queued=machine),
            TeamStub.member("st-link", words["link.name"], status="working", sessions=1, color="rose", one_off=True, isolation="shared"),
        ]
        staff[0]["live"] = live("ss-ira", "working", "t-checkout", branch="agent/ira/checkout")
        staff[1]["live"] = live("ss-max", "turn_done_unseen", "t-endpoint", branch="agent/max/endpoint")
        staff[2]["live"] = live("ss-naya", "permission", "t-bot")
        staff[3]["live"] = live("ss-lev", "working", "t-photos", session_id="sess-lev", branch="agent/lev/photos", worktree_path="/home/operator/work/bakery-site/.agents/worktrees/lev")
        staff[5]["live"] = live("ss-link", "working", None)
        for member in staff:
            member["project_id"] = pid
        team = TeamStub({**bakery}, staff=staff)
        team.project["orchestrator"] = True
        team.spend = {"st-lev": TeamStub.spent(1.2, 412_000, week=6.8, total=21.5), "st-ira": TeamStub.spent(0, 380_000, subscription=23)}
        team.orchestrator_spend = TeamStub.spent(0.85, 96_000, week=4.1, total=12.3)

        ira = BoardStub.assignee("st-ira", "Ira", harness="claude", color="orange", status="working", on_task=True)
        tasks = [
            BoardStub.task("t-checkout", words["task.checkout"], status="doing", priority=1, assignee=ira, checklist=[{"text": "cart", "done": True}, {"text": "promo", "done": True}, {"text": "payment", "done": False}]),
            BoardStub.task("t-bot", words["task.bot"], status="doing", assignee=BoardStub.assignee("st-naya", "Naya", harness="opencode", color="green", status="permission", on_task=True)),
            BoardStub.task("t-endpoint", words["task.endpoint"], status="review", assignee=BoardStub.assignee("st-max", "Max", harness="codex"), branch="agent/max/endpoint"),
            BoardStub.task("t-photos", words["task.photos"], status="doing", assignee=BoardStub.assignee("st-lev", "Lev", color="teal", status="working", on_task=True, session_id="sess-lev"), requirements=photo_requirements(lang)),
            BoardStub.task("t-hours", words["task.hours"], status="todo", priority=2, assignee=BoardStub.assignee("st-olga", "Olga", harness="claude", color="violet")),
            *finished_contracts(lang),
        ]
        needs = [{
            "id": "ask-spring", "short_id": "q4r8tz", "origin": "orchestrator", "kind": "question", "text": words["ask.spring"], "suggestion": "",
            "created_at": "2026-09-24T09:55:00Z", "task_id": "t-checkout", "task_title": words["task.checkout"], "staff": None, "session_id": "orch-bakery",
        }]
        board = BoardStub({**bakery}, staff=[{k: m[k] for k in ("id", "name", "color", "harness")} for m in staff], tasks=tasks, needs_you=needs)

        asks = [
            # Reworded once by the orchestrator in place: the same id, and the card says it changed.
            {"id": "ask-spring", "short_id": "q4r8tz", "project_id": pid, "origin": "orchestrator", "kind": "question", "staff_id": None, "staff_session_id": None, "task_id": "t-checkout", "request_ref": "", "title": words["title.spring"], "text": words["ask.spring"], "detail": {"options": [words["ask.before"], words["ask.after"]], "revision": 1, "updated_at": "2026-09-24T09:58:00Z"}, "routed_to": "operator", "suggestion": "", "created_at": "2026-09-24T09:55:00Z", "routed_at": "2026-09-24T09:55:00Z", "resolved_at": None, "resolved_by": None, "resolution": {}},
            {"id": "ask-grammy", "short_id": "qk7m2x", "project_id": pid, "origin": "staff", "kind": "permission", "staff_id": "st-naya", "staff_session_id": "ss-naya", "task_id": "t-bot", "request_ref": "", "text": "Exec: npm install grammy", "detail": {}, "routed_to": "orchestrator", "suggestion": "", "created_at": "2026-09-24T09:53:00Z", "routed_at": "2026-09-24T09:53:00Z", "resolved_at": "2026-09-24T09:54:00Z", "resolved_by": "orchestrator", "resolution": {"allow": True, "text": "", "selected": [], "via": "orchestrator"}},
        ]

        def call(cid: str, name: str, **arguments: object) -> dict:
            return {"id": cid, "name": name, "arguments": arguments}

        def result(cid: str, content: str, error: bool = False) -> dict:
            return {"id": cid, "content": content, "is_error": error}

        steps = {
            "goal": words["steps.goal"], "steps": [words["steps.1"], words["steps.2"], words["steps.3"]],
            "roles": [{"account": words["steps.bakery"], "purpose": words["steps.bakery.for"]}, {"account": words["steps.baker"], "purpose": words["steps.baker.for"]}],
            "expected": words["steps.expected"], "check": words["steps.check"], "limits": words["steps.limits"], "verified": "unverified", "verified_how": words["steps.how"],
        }
        events = (
            "[events · Bakery 2.0 · 3 since 09:51]\n"
            '- 09:51 Max (Codex) finished a turn on "Notify: endpoint" (t-endpoint): "3 files, tests green" — ReadStaff("Max") for the whole reply\n'
            "- 09:53 Naya needs permission [qk7m2x]: Exec: npm install grammy (in bakery-bot) (autonomy normal) — yours to answer or escalate\n"
            "- 09:54 Ira asks [q9w2e1]: SPRING10: before delivery or after? — options: before / after — yours to answer or escalate"
        )
        transcript = [
            {"role": "user", "seq": 10, "origin": "operator", "text": words["op.ask"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:40:00Z"},
            {"role": "assistant", "seq": 11, "text": "", "thinking": "", "tool_calls": [
                call("c1", "Folders", op="add", path="/home/operator/work/bakery-bot", env="host"),
                call("c2", "Tasks", op="create", title=words["task.endpoint"], objective="POST /orders/notify", deliverable="endpoint + tests", boundaries="api/ only", done_when="tests green", assignee="Max"),
                call("c3", "Tasks", op="create", title=words["task.bot"], objective="grammy bot", deliverable="bot", boundaries="bakery-bot/", done_when="a test order reaches the chat", depends_on=["t-endpoint"], assignee="Naya"),
                call("c4", "Assign", staff="Max", task_id="t-endpoint"),
                call("c5", "Watch", when={"event": "staff_finished", "staff": "Max"}, then={"action": "wake"}, note=words["watch.note"]),
            ], "tool_results": [], "created_at": "2026-09-24T09:40:10Z"},
            {"role": "tool", "seq": 12, "text": "", "thinking": "", "tool_calls": [], "tool_results": [
                result("c1", "added /home/operator/work/bakery-bot (host)"), result("c2", "created t-endpoint"), result("c3", "created t-bot"), result("c4", "started Max on t-endpoint"), result("c5", "watch w1 set"),
            ], "created_at": "2026-09-24T09:40:12Z"},
            {"role": "assistant", "seq": 13, "text": words["orch.plan"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:40:20Z"},
            {"role": "user", "seq": 14, "origin": "events", "text": events, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:54:10Z"},
            {"role": "assistant", "seq": 15, "text": "", "thinking": "", "tool_calls": [
                call("c6", "ReadStaff", staff="Max", what="last"),
                call("c7", "Answer", request_id="qk7m2x", allow=True, basis="installing dependencies in the bot folder"),
                call("c8", "AskOperator", title=words["title.spring"], text=words["ask.spring"], options=[words["ask.before"], words["ask.after"]], task_id="t-checkout"),
            ], "tool_results": [], "created_at": "2026-09-24T09:54:20Z"},
            {"role": "tool", "seq": 16, "text": "", "thinking": "", "tool_calls": [], "tool_results": [
                result("c6", "3 files changed, tests green"), result("c7", "answered qk7m2x: allowed"), result("c8", "asked the operator as [q4r8tz]; do not wait — the answer arrives as an event in a later wake-up"),
            ], "created_at": "2026-09-24T09:54:22Z"},
            {"role": "assistant", "seq": 17, "text": words["orch.after"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:55:00Z"},
            # A correction that became a requirement Lev confirmed, and a promise to the operator.
            {"role": "user", "seq": 18, "origin": "operator", "text": words["op.photos"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:56:00Z", "delivery": None, "reply_to": None},
            {"role": "assistant", "seq": 19, "text": words["orch.photos"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:56:30Z"},
            # Naya's steps for the operator, carried by the host as a card; she could not walk them on the running version.
            {"role": "user", "seq": 20, "origin": "events", "text": steps_markdown(steps, "Naya", "t-bot"), "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:57:00Z",
             "operator_steps": {**steps, "file": {"id": "5e0b7a11c0de", "name": "operator-steps-t-bot-1.md", "mime": "text/markdown", "size": 1180}, "member": "Naya", "task_id": "t-bot", "event_seq": 412}},
            # The operator's question about those steps, written while a turn ran and placed into it.
            {"role": "user", "seq": 21, "origin": "operator", "text": words["op.steps"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:57:40Z",
             "delivery": "steer", "reply_to": {"seq": 20, "excerpt": f"Naya: {words['steps.goal']}"}},
            {"role": "assistant", "seq": 22, "text": words["orch.steps"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:58:00Z"},
            # Sent in the last seconds of that turn: read at the start of the next, sent on to Olga, not confirmed yet.
            {"role": "user", "seq": 23, "origin": "operator", "text": words["op.hours"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:58:02Z", "delivery": "drained", "reply_to": None},
            {"role": "assistant", "seq": 24, "text": words["orch.hours"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:58:30Z"},
        ]
        lev = [
            {"role": "user", "seq": 3, "origin": "orchestrator", "text": words["lev.task"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:30:00Z"},
            {"role": "assistant", "seq": 4, "text": words["lev.reply"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:36:00Z"},
        ]
        details = {
            "orch-bakery": cls.session_detail("orch-bakery", "Orchestrator · Bakery 2.0", bakery, transcript, orchestrator_of=pid),
            # Between turns, so its composer offers to write to it rather than to steer a run.
            "sess-lev": cls.session_detail("sess-lev", f"Lev · {words['task.photos']}", bakery, lev, staff={"id": "st-lev", "session_id": "ss-lev"}),
        }
        brief = [
            {"section": "goals", "body": words["brief.goals"], "updated_at": "2026-09-22T10:00:00Z", "updated_by": "operator"},
            {"section": "constraints", "body": words["brief.constraints"], "updated_at": "2026-09-22T10:00:00Z", "updated_by": "operator"},
            {"section": "preferences", "body": "", "updated_at": None, "updated_by": None},
            {"section": "done_when", "body": words["brief.done"], "updated_at": "2026-09-22T10:00:00Z", "updated_by": "operator"},
            {"section": "allowed_without_operator", "body": "installing dependencies in the bot folder\nrunning the test suites", "updated_at": "2026-09-22T10:00:00Z", "updated_by": "operator"},
            {"section": "notes", "body": words["brief.notes"], "updated_at": "2026-09-24T09:41:00Z", "updated_by": "orchestrator"},
        ]
        kinds = [("orchestrator", "decision", words["journal.decision"], {"task_id": "t-bot"}), ("system", "grant", "The orchestrator allowed Naya: Exec: npm install grammy (basis: installing dependencies in the bot folder)", {"staff_id": "st-naya", "ask_id": "qk7m2x"}), ("staff", "report", "Max: done — 3 files, tests green", {"staff_id": "st-max", "task_id": "t-endpoint"}), ("operator", "note", words["journal.note"], {})]
        journal = [
            {"id": 100 - i, "at": f"2026-09-24T{9 - i // 12:02d}:{59 - (i % 12) * 5:02d}:00Z", "author": kinds[i % 4][0], "kind": kinds[i % 4][1], "text": f"{kinds[i % 4][2]} ({i + 1})" if i else kinds[0][2], "refs": kinds[i % 4][3]}
            for i in range(35)
        ]
        schedules = [{"id": "wk1", "name": words["wake.name"], "run_in": "self", "cron": None, "run_at": "2026-09-24T12:00:00Z", "prompt": words["wake.note"], "enabled": 1, "next_run_at": "2026-09-24T12:00:00Z", "last_run_at": None, "last_summary": None, "kind": "lazy", "target_session": "orch-bakery", "failure_count": 0, "last_error": None, "authority_state": "needs_approval", "schedule_revision": 1, "project_id": None}]
        wakeups = [
            {"id": "wk1", "project_id": pid, "note": words["wake.note"], "cron": None, "at": "2026-09-24T12:00:00Z", "next_run_at": "2026-09-24T12:00:00Z", "last_run_at": None, "enabled": True, "set_by": "orchestrator", "created_at": "2026-09-24T09:40:00Z", "schedule_revision": 1},
        ]
        watches = [
            {"id": "w1", "project_id": pid, "when": {"event": "staff_finished", "staff": "Max", "staff_id": "st-max"}, "then": {"action": "wake"}, "cooldown_minutes": 10, "once": False, "note": words["watch.note"],
             "created_by": "orchestrator", "created_at": "2026-09-24T09:40:12Z", "last_fired_at": "2026-09-24T09:51:00Z", "fire_count": 1, "enabled": True, "stopped": "", "last_error": "", "describe": "when Max finishes a turn → wake the orchestrator", "condition_revision": 1, "authority_state": "needs_approval", "latest_delivery": {"id": "d1", "status": "pending", "receipt_id": None, "last_error": None, "created_at": "2026-09-24T09:51:00Z"}},
            {"id": "w2", "project_id": pid, "when": {"event": "ci", "provider": "github", "repo": "bakery/api", "conclusion": "failure"}, "then": {"action": "notify", "title": "CI", "level": "urgent"}, "cooldown_minutes": 30, "once": False, "note": "",
             "created_by": "operator", "created_at": "2026-09-23T18:00:00Z", "last_fired_at": None, "fire_count": 0, "enabled": False, "stopped": "expired", "last_error": "", "describe": "", "condition_revision": 1, "authority_state": "current", "latest_delivery": {"id": "d2", "status": "failed", "receipt_id": None, "last_error": "delivery declined", "created_at": "2026-09-23T18:00:00Z"}},
            {"id": "w3", "project_id": pid, "when": {"event": "staff_finished", "staff": "Lev"}, "then": {"action": "wake"}, "cooldown_minutes": 15, "once": False, "note": "",
             "created_by": "operator", "created_at": "2026-09-23T18:01:00Z", "last_fired_at": None, "fire_count": 0, "enabled": False, "stopped": "needs_approval", "last_error": "", "describe": "", "condition_revision": 2, "authority_state": "needs_approval", "latest_delivery": None},
            {"id": "w4", "project_id": pid, "when": {"event": "task_moved", "task": "t1", "to": "review"}, "then": {"action": "wake"}, "cooldown_minutes": 15, "once": False, "note": "",
             "created_by": "operator", "created_at": "2026-09-23T18:02:00Z", "last_fired_at": None, "fire_count": 0, "enabled": False, "stopped": "", "last_error": "", "describe": "", "condition_revision": 1, "authority_state": "capability_unavailable", "latest_delivery": None},
        ]
        terminals = [
            {"id": "tm-api", "env": "container", "title": "bash · bakery-api", "owner": {"kind": "project", "id": pid}, "project_id": pid, "profile": "shell", "sandbox": False, "cwd": "/home/operator/work/bakery-api", "status": "running", "exit_code": None, "exit_signal": None, "created_at": "2026-09-24T09:00:00Z", "exited_at": None, "last_output_at": "2026-09-24T09:50:00Z", "last_input_at": None, "cols": 120, "rows": 30},
            {"id": "tm-psql", "env": "container", "title": "psql · orders", "owner": {"kind": "project", "id": pid}, "project_id": pid, "profile": "shell", "sandbox": False, "cwd": "/home/operator/work/bakery-api", "status": "exited", "exit_code": 0, "exit_signal": None, "created_at": "2026-09-24T08:00:00Z", "exited_at": "2026-09-24T08:30:00Z", "last_output_at": None, "last_input_at": None, "cols": 120, "rows": 30},
        ]
        messages = {"st-lev": [
            {"id": "m2", "staff_id": "st-lev", "staff_session_id": "ss-lev", "origin": "orchestrator", "text": words["lev.message"], "mode": "now", "state": "acknowledged", "attempts": 1, "created_at": "2026-09-24T09:45:00Z", "updated_at": "2026-09-24T09:45:05Z", "error": ""},
            {"id": "m1", "staff_id": "st-lev", "staff_session_id": "ss-lev", "origin": "operator", "text": words["lev.first"], "mode": "after_turn", "state": "acknowledged", "attempts": 1, "created_at": "2026-09-24T09:30:00Z", "updated_at": "2026-09-24T09:30:02Z", "error": ""},
        ]}
        sessions = [
            {"id": "orch-bakery", "title": "Orchestrator · Bakery 2.0", "status": "idle", "created_at": "2026-09-20T00:00:00Z", "last_message_at": "2026-09-24T09:55:00Z", "run_id": None, "model": "Claude Opus 5", "metadata": {"orchestrator_of": pid}, "project_id": pid, "project": "Bakery 2.0"},
            {"id": "sess-lev", "title": f"Lev · {words['task.photos']}", "status": "idle", "created_at": "2026-09-24T09:30:00Z", "last_message_at": "2026-09-24T09:36:00Z", "run_id": None, "model": "Claude Opus 5", "metadata": {"staff_id": "st-lev"}, "project_id": pid, "project": "Bakery 2.0"},
            *(other_sessions or []),
        ]
        counts = {"total": 2, "active": 1, "loops": 0, "last_message_at": "2026-09-24T09:55:00Z"}
        folders_listed = [
            {**{k: v for k, v in bakery.items() if k != "sessions"}, **counts, "orchestrator": {"enabled": True, "session_id": "orch-bakery", "staff": 6, "working": 4, "needs_you": 1}},
            {**{k: v for k, v in garden_project.items() if k != "sessions"}, "total": 0, "active": 0, "loops": 0, "last_message_at": "", "orchestrator": None},
        ]
        listing = {"sessions": sessions, "projects": folders_listed}
        # The project without an orchestrator has a team and a board of its own, both empty.
        others: list[TeamStub | BoardStub] = [TeamStub(garden_project), BoardStub(garden_project)]
        rules = [{"id": 120, "at": "2026-09-24T08:30:00Z", "author": "operator", "kind": "rule", "text": words["journal.rule"], "refs": {}}]
        stub = cls(projects=projects, listing=listing, details=details, team=team, board=board, others=others, asks=asks, brief=brief, journal=journal, schedules=schedules, rules=rules, wakeups=wakeups, watches=watches, terminals=terminals, messages=messages)
        stub.focus_states[pid] = cls.focus_state_of_bakery(lang, pid)
        return stub

    @staticmethod
    def focus_state_of_bakery(lang: str, pid: str) -> dict:
        """The bakery as its orchestrator's goal line reads it: five cards in hand, the checkout blocked on
        the operator's discount answer, Max's done report and Lev's call for input waiting for the
        orchestrator's decision, two promises, and what became of two of the operator's messages —
        a requirement Lev confirmed, and one sent to Olga she has not confirmed yet."""
        words, w = FOCUS_WORDS[lang], CONTRACT_WORDS[lang]

        def goal(task_id: str, title: str, status: str, owner: tuple[str, str] | None, *, acceptance: str = "", nxt: str = "", previous: str | None = None) -> dict:
            return {"task_id": task_id, "title": words[title], "status": status, "acceptance": acceptance, "owner": {"id": owner[0], "name": owner[1]} if owner else None,
                    "previous_owner": previous, "next": nxt, "updated_at": "2026-09-24T09:50:00Z"}

        decision = "waiting for the orchestrator's decision"
        goals = [
            goal("t-checkout", "task.checkout", "doing", ("st-ira", "Ira"), nxt=words["next.spring"]),
            goal("t-endpoint", "task.endpoint", "review", ("st-max", "Max"), acceptance="handed_in", nxt=decision),
            goal("t-photos", "task.photos", "doing", ("st-lev", "Lev"), nxt=decision),
            goal("t-bot", "task.bot", "doing", ("st-naya", "Naya")),
            goal("t-hours", "task.hours", "todo", ("st-olga", "Olga")),
        ]
        results = [
            {"id": 7, "cause": "report_done", "task_id": "t-endpoint", "title": words["task.endpoint"], "staff_name": "Max", "summary": "3 files, tests green", "opened_at": "2026-09-24T09:51:00Z", "reminded": False},
            {"id": 5, "cause": "report_needs_input", "task_id": "t-photos", "title": words["task.photos"], "staff_name": "Lev", "summary": words["result.lev"], "opened_at": "2026-09-24T09:36:00Z", "reminded": True},
        ]
        commitments = [
            {"id": 131, "text": words["commit.gallery"], "task_id": "t-photos", "message_seq": 18, "at": "2026-09-24T09:56:30Z"},
            {"id": 134, "text": words["commit.holidays"], "task_id": "t-hours", "message_seq": 23, "at": "2026-09-24T09:58:30Z"},
        ]
        receipts = {
            "18": [
                {"kind": "requirement", "label": "R1", "text": w["r.short"], "state": "active", "task_id": "t-photos", "task_title": words["task.photos"],
                 "deliveries": [{"staff_name": "Lev", "acknowledged": True, "opened": False, "via": "message", "cli": False}]},
                {"kind": "commitment", "id": 131, "text": words["commit.gallery"], "task_id": "t-photos", "kept": False},
            ],
            "23": [
                {"kind": "requirement", "label": "R2", "text": words["r.hours"], "state": "active", "task_id": "t-hours", "task_title": words["task.hours"],
                 "deliveries": [{"staff_name": "Olga", "acknowledged": False, "opened": False, "via": "message", "cli": False}]},
                {"kind": "commitment", "id": 134, "text": words["commit.holidays"], "task_id": "t-hours", "kept": False},
            ],
        }
        counts = {"in_work": len(goals), "decisions": len(results), "waiting_for_you": 1, "unconfirmed": 2, "commitments": len(commitments)}
        accepted_results = [{"task_id": "t-hero", "title": words["task.hero"], "result_id": "res-hero", "contract_revision": 1,
                             "current_contract_revision": 1, "attempt_id": "attempt-hero", "original_digest": "a" * 64,
                             "author": "Olga", "created_at": "2026-09-23T17:55:00Z"}]
        return {"project_id": pid, "counts": counts, "goals": goals, "open_results": results, "accepted_results": accepted_results,
                "commitments": commitments, "receipts": receipts}


def health(at, *, tools: str, silent: bool = False) -> dict:  # type: ignore[no-untyped-def]
    """``daedalus/harness/health.py``'s verdict for an invented member."""
    problems = (["team_tools_missing"] if tools == "missing" else []) + (["silent"] if silent else [])
    return {
        "team_tools": tools, "last_hook_at": at(seconds=40), "last_team_call_at": at(minutes=3) if tools == "connected" else None, "last_signal_at": at(minutes=9 if silent else 0, seconds=40),
        "silent_s": 540 if silent else 40, "silence_after_s": 300, "silent": silent, "last_message": {"id": "mi2", "state": "submitted"}, "last_acknowledged_at": at(minutes=25),
        "problems": problems, "level": "warn" if problems else "ok",
    }


def _caps(harness: str, label: str, channel: str, channel_label: str, steer: str, permissions: str, team_tools: str, tested: tuple[str, str], major: int) -> dict:
    return {"harness": harness, "label": label, "status_channel": channel, "status_channel_label": channel_label, "steer": steer, "permissions": permissions, "questions": "structured",
            "team_tools": team_tools, "first_prompt": "argv", "interrupt": "keys", "companion": harness == "codex", "tested_versions": list(tested), "supported_major": major}


# The capability table as ``daedalus/harness/capabilities.py`` has it, in the fields the app reads.
CAPABILITIES: dict[str, dict] = {
    "claude": {**_caps("claude", "Claude Code", "hooks", "hooks per launch", "tui_queue", "hook_then_keys", "mcp", ("2.1.281", "2.2.0"), 2), "standing_rules": True},
    "codex": _caps("codex", "Codex", "app_server", "app-server notifications", "native", "structured", "mcp", ("0.155.1", "0.157.0"), 0),
    "opencode": _caps("opencode", "OpenCode", "sse", "server events", "degrade_to_queue", "structured", "mcp", ("1.18.23", "1.19.0"), 1),
    "pi": _caps("pi", "pi", "extension", "bridge extension", "native", "none", "extension", ("0.84.2", "0.88.0"), 0),
    "grok": _caps("grok", "Grok Build", "files", "session files", "cancel_and_send", "keys", "none", ("1.0.40", "1.1.0"), 1),
}


# Claude Code's models as the host reads them from the installed program: the aliases, then each
# version the /model picker offers, newest first within each family.
CLAUDE_ALIASES = ["fable", "opus", "sonnet", "haiku"]
CLAUDE_VERSIONS = ["claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-fable-5-1", "claude-sonnet-5-5", "claude-sonnet-5", "claude-haiku-4-5"]


class HarnessesStub:
    """The harness manager's routes (``api_harnesses.py``) over the five CLIs of M7, kept between
    requests: a check moves ``checked_at``, an update or install leaves the row busy with its
    operation, an update of a CLI staff work on is refused with 409 and who they are, as the host does.

    The container has Claude Code current and signed in with its agents, Codex, Grok Build and OpenCode
    each one version behind, and pi not installed (npm is there: Node is); Grok's last self-check
    failed at ``ready``. The host environment has Claude Code only. ``unverified(harness)`` moves a CLI
    past its tested range without a self-check on the new version, for the version guard.
    """

    def __init__(self, *, working: dict[str, list[dict]] | None = None, host: bool = True) -> None:
        now = datetime.now(UTC)
        self.checked = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.working = working if working is not None else {"codex": [{"harness": "codex", "staff_id": "st-max", "name": "Max", "project": "Bakery 2.0", "project_id": "b4k3ry20f0c5", "env": "container", "staff_session_id": "ss-max", "status": "turn_done_unseen"}]}
        self.host = host
        self.posted: list[tuple[str, dict]] = []
        self.put: list[tuple[str, dict]] = []
        passed = {"ok": True, "version": "", "duration_ms": 8200, "at": self.checked, "steps": [{"name": n, "ok": True, "skipped": False, "detail": d, "duration_ms": 900} for n, d in (("version", ""), ("supported", "within the tested versions"), ("signin", "signed in"), ("launch", ""), ("ready", "SessionStart"), ("team", "team tools said hello"), ("deliver", "one line acknowledged"), ("reply", "Report came through the team channel"), ("exit", "exit code 0"))]}
        failed = {"ok": False, "version": "1.0.40", "duration_ms": 30100, "at": self.checked, "steps": [{"name": "version", "ok": True, "skipped": False, "detail": "1.0.40", "duration_ms": 300}, {"name": "launch", "ok": True, "skipped": False, "detail": "", "duration_ms": 600}, {"name": "ready", "ok": False, "skipped": False, "detail": "no SessionStart within 30 s", "duration_ms": 30000}]}

        def entry(harness: str, version: str, latest: str, login: str, detail: str, agents: list[dict], models: list[str], check: dict | None = None, **over: object) -> dict:
            caps = CAPABILITIES[harness]
            installed = bool(version)
            row = {
                "env": "container", "harness": harness, "label": caps["label"], "installed": installed, "installed_version": version, "latest_version": latest, "install_method": "npm" if harness in ("codex", "opencode", "pi") else "native",
                "binary_path": "", "logged_in": login, "login_detail": detail, "agents": agents, "models": models, "all_models": models, "models_chosen": False, "modes": [], "efforts": [], "profiles": [],
                "self_check": ({**check, "version": check.get("version") or version} if check else {}), "checked_at": self.checked, "latest_checked_at": self.checked, "error": "",
                "status_channel": caps["status_channel"], "status_channel_label": caps["status_channel_label"], "steer": caps["steer"], "tested_versions": caps["tested_versions"],
                "tested": installed, "supported": installed, "adapter": True, "version_guard": "", "update_available": installed and latest != version, "self_check_ok": check["ok"] if check else None,
                "operation": None, "installable": not installed, "install_problem": "", "can_sign_in": True, "unavailable": "" if installed else f"{caps['label']} is not installed in the container environment",
            }
            row.update(over)
            return row

        claude_agents = [{"name": "default", "source": "builtin"}, {"name": "frontend-developer", "source": "user"}, {"name": "code-reviewer", "source": "project"}, {"name": "copywriter", "source": "project"}]
        self.rows: dict[str, list[dict]] = {
            "container": [
                entry("claude", "2.1.281", "2.1.281", "yes", "subscription · max", claude_agents, [*CLAUDE_ALIASES, *CLAUDE_VERSIONS], passed),
                entry("codex", "0.155.1", "0.156.1", "yes", "ChatGPT", [{"name": "reviewer", "source": "user"}, {"name": "api", "source": "project"}, {"name": "tester", "source": "project"}], ["gpt-5.2-codex", "gpt-5.2-codex-mini", "o5"], passed),
                entry("opencode", "1.18.23", "1.18.32", "yes", "keys: 2", [{"name": n, "source": "builtin"} for n in ("build", "plan", "general", "review", "docs")], [f"provider/model-{i}" for i in range(42)], passed),
                entry("pi", "", "0.87.1", "unknown", "", [], [], None),
                entry("grok", "1.0.40", "1.0.41", "yes", "signed in", [{"name": "coder", "source": "user"}, {"name": "writer", "source": "user"}], ["grok-4-fast", "grok-4"], failed, unavailable="Grok Build's last self-check failed at ready: no SessionStart within 30 s"),
            ],
            "host": [
                {**entry("claude", "2.1.270", "2.1.281", "yes", "subscription · max", claude_agents[:2], ["opus", "sonnet", "haiku"], passed), "env": "host"},
                *[{**entry(h, "", "", "unknown", "", [], [], None), "env": "host", "unavailable": ""} for h in ("codex", "opencode", "pi", "grok")],
            ],
        }
        self.node = {"container": {"installed": True, "version": "24.21.0", "path": "/root/.local/bin/node", "pinned": "24.21.0", "current": True, "checked_at": self.checked}, "host": {"installed": True, "version": "22.20.0", "path": "/usr/bin/node", "pinned": "24.21.0", "current": False, "checked_at": self.checked}}

    def unverified(self, harness: str, version: str) -> None:
        row = next(r for r in self.rows["container"] if r["harness"] == harness)
        row.update(installed_version=version, latest_version=version, tested=False, version_guard="unverified", update_available=False)

    def screen(self, env: str) -> dict:
        rows = self.rows[env]
        return {"env": env, "environments": ["container", "host"] if self.host else ["container"], "rows": rows, "node": self.node[env], "checked_at": self.checked, "updates": sum(1 for r in rows if r["update_available"])}

    def catalog_view(self, env: str) -> dict:
        """``GET /api/harnesses/catalog`` from these rows, as the host's ``catalog_view`` builds it."""
        return {
            r["harness"]: {
                "installed": r["installed"], "version": r["installed_version"], "latest": r["latest_version"], "logged_in": {"yes": True, "no": False}.get(r["logged_in"]),
                "agents": r["agents"], "models": r["models"], "all_models": r["all_models"], "models_chosen": r["models_chosen"], "error": r["error"], "checked_at": r["checked_at"],
                "version_guard": r["version_guard"], "tested_versions": r["tested_versions"], "unavailable": r["unavailable"],
            }
            for r in self.rows[env]
        }

    def offer(self, env: str, harness: str, models: list[str] | None) -> tuple[int, object]:
        """``PUT /api/harnesses/{harness}/models``: only a listed model can be chosen; none offers all."""
        row = next((r for r in self.rows[env] if r["harness"] == harness), None)
        if row is None:
            return 404, {"detail": "no such harness"}
        unknown = [m for m in models or [] if m not in row["all_models"]]
        if unknown:
            return 400, {"detail": f"{row['label']} in the {env} environment lists no model {unknown[0]!r}"}
        chosen = [m for m in row["all_models"] if m in (models or [])]
        row.update(models=chosen or list(row["all_models"]), models_chosen=bool(chosen))
        return 200, {"row": row}

    def answer(self, method: str, path: str, query: str, body: dict | None) -> tuple[int, object] | None:
        if not path.startswith("/api/harnesses") or path == "/api/harnesses/catalog" or path.endswith("/catalog"):
            return None
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        env = (body or {}).get("env") or params.get("env") or "container"
        if env not in self.rows:
            return 422, {"detail": "env is container or host"}
        if method == "GET" and path == "/api/harnesses":
            return 200, self.screen(env)
        if method == "PUT" and path.endswith("/models") and path.count("/") == 4:
            self.put.append((path, dict(body or {})))
            return self.offer(env, path.split("/")[3], (body or {}).get("models"))
        if method != "POST":
            return None
        self.posted.append((path, dict(body or {})))
        if path == "/api/harnesses/check":
            self.checked = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            for row in self.rows[env]:
                row["checked_at"] = self.checked
            return 200, {"env": env, "rows": self.rows[env], "node": self.node[env]}
        if path == "/api/harnesses/update-all":
            return 202, {"state": "started"}
        if path == "/api/harnesses/node/install":
            return (400, {"detail": "Node is installed only in the container"}) if env == "host" else (202, {"state": "started"})
        parts = path.split("/")
        row = next((r for r in self.rows[env] if len(parts) == 5 and r["harness"] == parts[3]), None)
        if row is None:
            return 404, {"detail": "no such harness"}
        if parts[4] == "update":
            working = [s for s in self.working.get(row["harness"], []) if s["env"] == env]
            if working:
                names = ", ".join(f"{s['name']} ({s['project']})" for s in working)
                return 409, {"detail": f"{row['label']} is running staff in the {env} environment: {names}. Release them or wait for them to finish, then update.", "staff": working}
            row["operation"] = {"kind": "update", "started_at": self.checked, "terminal_id": None, "target": row["latest_version"]}
            return 202, {"state": "started", "operation": row["operation"]}
        if parts[4] == "install":
            row["operation"] = {"kind": "install", "started_at": self.checked, "terminal_id": f"inst-{row['harness']}"[:12], "target": row["latest_version"]}
            return 202, {"state": "started", "operation": row["operation"]}
        if parts[4] == "login-terminal":
            return 200, {"terminal_id": f"login-{row['harness']}"[:12], "argv": [row["harness"], "login"]}
        return None


FOCUS_WORDS: dict[str, dict[str, str]] = {
    "en": {
        "op.ask": "Add the folder ~/work/bakery-bot. The baker should get a Telegram message for every new order: the endpoint is Max's, the bot is Naya's.",
        "orch.plan": "Split into two tasks. The bot depends on the endpoint, so Naya starts after Max. Ira is busy with the checkout; I am not pulling her off it.",
        "orch.after": "Naya may install the bot's dependencies: the brief allows it. The discount question is yours — Ira's checkout waits on it.",
        "ask.spring": "SPRING10: is the discount taken before delivery or after?",
        "ask.before": "Before delivery", "ask.after": "After delivery",
        "title.spring": "SPRING10 discount",
        "title.pay": "Payment providers", "cash": "Cash on pickup",
        "ask.pay": "Ira's checkout needs to know which providers to wire up at launch. Each one costs a day of work:\n\n- **Stripe** — cards, Apple Pay and Google Pay; the fees are 1.5 % + 20 p\n- **PayPal** — some regulars asked for it; a separate sandbox account is needed\n- **Cash on pickup** — no fees, but the order is only confirmed at the counter\n\nChoose any. The ones you leave out can be added later without touching the checkout's layout.\n\nThe brief says nothing about payments, so this is yours.",
        "title.open": "Opening day",
        "ask.open": "When does the new site go live? A date or a weekday is enough — the bot and the menu are planned backwards from it.",
        "ask.font": "Which typeface for the menu headings?",
        "watch.note": "Max's turn finished → wake me",
        "task.checkout": "Checkout", "task.bot": "Notify: bot", "task.endpoint": "Notify: endpoint", "task.photos": "Menu photo captions", "task.hours": "Opening hours",
        "task.menu": "Seasonal menu page", "task.prices": "Price sheet check", "task.hero": "Hero image",
        "ira.role": "front end", "naya.role": "the baker's bot", "lev.role": "review", "olga.role": "copy", "link.name": "Menu link check",
        "lev.task": "Task: captions for the menu photos. Done when every photo in the gallery has a caption from the sheet.",
        "lev.reply": "Twelve of eighteen captions are written from the sheet; the rest have no row yet, so I am asking the orchestrator.",
        "lev.message": "Take the captions from the owner's sheet, column C", "lev.first": "Start with the seasonal photos",
        "brief.goals": "Orders arrive in the baker's Telegram within a minute.\nThe checkout takes SPRING10.",
        "brief.constraints": "No new services; the bot runs on the host.", "brief.done": "A test order reaches the baker's chat.",
        "brief.notes": "Max's endpoint is merged before Naya starts.",
        "journal.decision": "The bot waits for the endpoint: one contract, not two.", "journal.note": "Remember the Friday price change.",
        "journal.rule": "When a member reports and another is free, hand out the next step at once; never leave free hands idle.",
        "wake.name": "Check the checkout", "wake.note": "Look at Ira's checkout after lunch",
        "ira.task": "Task: the checkout page. Cart → promo code → payment. Done when checkout.test.tsx is green. Do not touch api/.",
        "ira.reply1": "The discount was taken from the total with delivery. Fixed in `cart.ts`; the checkout tests pass.",
        "ira.accepted": "SPRING10 comes off the sum without delivery, as the brief says.", "ira.reply2": "Done: the discount now leaves delivery out.",
        "ira.sent": "Add Stripe for the payment step", "ira.reply3": "Installing Stripe's browser library for the payment step.",
        "ira.queued": "After Stripe, check the page on a phone",
        "op.photos": "Lev's captions: under 80 characters, and no emoji.",
        "orch.photos": "On Lev's card as a requirement; he has confirmed it.",
        "next.spring": "the operator — SPRING10 before or after delivery [q4r8tz]",
        "op.steps": "Which of the two accounts does the baker sign in with?",
        "orch.steps": "The baker's own account. The bakery account only owns the bot; Naya's steps say so in the table.",
        "op.hours": "The hours page must list the holiday hours too.",
        "orch.hours": "Added to Olga's card and sent to her. I will tell you when the page shows them.",
        "steps.goal": "Connect the bot to the baker's chat",
        "steps.how": "the test chat answered; the baker's own chat could not be reached from here",
        "steps.bakery": "the bakery account (@bakery_orders_bot's owner)", "steps.bakery.for": "owns the bot; never sign in to the chat with it",
        "steps.baker": "the baker's own Telegram account", "steps.baker.for": "receives the orders",
        "steps.1": "Open Telegram on the baker's phone, signed in as the baker.",
        "steps.2": "Find @bakery_orders_bot and press Start.",
        "steps.3": "Send /link and the six-digit code the site shows under Settings → Notifications.",
        "steps.expected": "The bot answers \"Linked: orders will arrive here\".",
        "steps.check": "Place a test order on the site with the code TEST; it arrives within a minute.",
        "steps.limits": "Only one chat can be linked; linking another replaces it.",
        "r.hours": "List the holiday hours on the hours page",
        "commit.gallery": "Tell you when the menu gallery is live",
        "commit.holidays": "Tell you when the page shows the holiday hours",
        "result.lev": "12 of 18 captions written; 6 photos have no row in the sheet",
    },
    "ru": {
        "op.ask": "Добавь в проект папку ~/work/bakery-bot. Нужно, чтобы пекарь получал в Telegram сообщение о каждом новом заказе: эндпоинт — Максу, бота — Нае.",
        "orch.plan": "Разложено на две задачи. Бот зависит от эндпоинта, поэтому Ная начнёт после Макса. Ира занята оформлением заказа, её не отвлекаю.",
        "orch.after": "Нае можно ставить зависимости бота: это есть в брифе. Вопрос о скидке — ваш, от него зависит оформление заказа у Иры.",
        "ask.spring": "SPRING10: скидка до доставки или после?",
        "ask.before": "До доставки", "ask.after": "После доставки",
        "title.spring": "Скидка SPRING10",
        "title.pay": "Способы оплаты", "cash": "Наличными при получении",
        "ask.pay": "Ире для оформления заказа нужно знать, какие способы оплаты подключить к запуску. Каждый — это день работы:\n\n- **Stripe** — карты, Apple Pay и Google Pay; комиссия 1,5 % + 20 ₽\n- **PayPal** — о нём просили постоянные клиенты; нужен отдельный тестовый аккаунт\n- **Наличными при получении** — без комиссии, но заказ подтверждается только на кассе\n\nМожно выбрать несколько. Остальные можно добавить позже, не трогая вёрстку оформления.\n\nВ брифе про оплату ничего нет, поэтому решать вам.",
        "title.open": "День открытия",
        "ask.open": "Когда запускаем новый сайт? Достаточно даты или дня недели — бот и меню планируются от этой даты.",
        "ask.font": "Какой шрифт для заголовков меню?",
        "watch.note": "ход Макса завершён → разбудить меня",
        "task.checkout": "Оформление заказа", "task.bot": "Уведомление: бот", "task.endpoint": "Уведомление: эндпоинт", "task.photos": "Подписи к фото в меню", "task.hours": "Часы работы",
        "task.menu": "Страница сезонного меню", "task.prices": "Сверка прайса", "task.hero": "Главное изображение",
        "ira.role": "фронтенд", "naya.role": "бот пекаря", "lev.role": "ревью", "olga.role": "тексты", "link.name": "Проверка ссылок меню",
        "lev.task": "Задача: подписи к фото в меню. Готово, когда у каждого фото в галерее есть подпись из таблицы.",
        "lev.reply": "Двенадцать из восемнадцати подписей взяты из таблицы; для остальных строк нет, спрашиваю оркестратора.",
        "lev.message": "Подписи бери из таблицы владельца, колонка C", "lev.first": "Начни с сезонных фото",
        "brief.goals": "Заказы приходят пекарю в Telegram за минуту.\nОформление заказа принимает SPRING10.",
        "brief.constraints": "Без новых сервисов; бот работает на хосте.", "brief.done": "Тестовый заказ доходит до чата пекаря.",
        "brief.notes": "Эндпоинт Макса сливается до того, как начнёт Ная.",
        "journal.decision": "Бот ждёт эндпоинт: один контракт, а не два.", "journal.note": "Не забыть про смену цен в пятницу.",
        "journal.rule": "Когда сотрудник отчитался, а другой свободен, сразу поручай следующий шаг; свободные руки не должны простаивать.",
        "wake.name": "Проверить оформление заказа", "wake.note": "После обеда посмотреть оформление заказа у Иры",
        "ira.task": "Задача: страница оформления заказа. Корзина → промокод → оплата. Готово, когда checkout.test.tsx зелёный. Не трогать api/.",
        "ira.reply1": "Скидка считалась от суммы с доставкой. Исправлено в `cart.ts`; тесты оформления проходят.",
        "ira.accepted": "Скидка SPRING10 — от суммы без доставки, так в брифе.", "ira.reply2": "Готово: скидка теперь без доставки.",
        "ira.sent": "Подключи Stripe для шага оплаты", "ira.reply3": "Ставлю браузерную библиотеку Stripe для шага оплаты.",
        "ira.queued": "После Stripe проверь страницу на телефоне",
        "op.photos": "Подписи у Льва — короче 80 символов и без эмодзи.",
        "orch.photos": "Это требование в карточке Льва; он его подтвердил.",
        "next.spring": "оператора — SPRING10 до или после доставки [q4r8tz]",
        "op.steps": "С каким из двух аккаунтов входит пекарь?",
        "orch.steps": "Со своим. Аккаунт пекарни только владеет ботом — в таблице у Наи это есть.",
        "op.hours": "На странице часов работы нужны и праздничные часы.",
        "orch.hours": "Добавил в карточку Ольги и отправил ей. Скажу, когда страница их покажет.",
        "steps.goal": "Подключить бота к чату пекаря",
        "steps.how": "тестовый чат ответил; до чата самого пекаря отсюда не дотянуться",
        "steps.bakery": "аккаунт пекарни (владелец @bakery_orders_bot)", "steps.bakery.for": "владеет ботом; в чат с ним не входить",
        "steps.baker": "личный Telegram пекаря", "steps.baker.for": "получает заказы",
        "steps.1": "Откройте Telegram на телефоне пекаря, под его аккаунтом.",
        "steps.2": "Найдите @bakery_orders_bot и нажмите «Старт».",
        "steps.3": "Отправьте /link и шестизначный код из раздела сайта «Настройки → Уведомления».",
        "steps.expected": "Бот отвечает «Привязано: заказы будут приходить сюда».",
        "steps.check": "Оформите на сайте тестовый заказ с кодом TEST — он придёт в течение минуты.",
        "steps.limits": "Привязать можно один чат; новая привязка заменяет старую.",
        "r.hours": "Указать праздничные часы на странице часов работы",
        "commit.gallery": "Сказать вам, когда галерея меню будет на сайте",
        "commit.holidays": "Сказать вам, когда страница покажет праздничные часы",
        "result.lev": "12 из 18 подписей готовы; для 6 фото в таблице нет строки",
    },
}


# What the demo board's cards were asked and what their members said about it, in both languages:
# the English texts are what `check_task_contract.py` asserts against, the Russian ones are for the pictures.
CONTRACT_WORDS: dict[str, dict[str, str]] = {
    "en": {
        "r.short": "Captions under 80 characters, no emoji", "r.shots": "Start from the photographer's shot list", "r.all": "Caption every photo on the site",
        "r.menu": "Caption the menu photos only", "r.checkout": "Do not touch the checkout pages", "r.sheet": "Take the items from the spring menu sheet",
        "c.items": "The page lists every seasonal item", "c.phone": "It reads well on a 390 px phone",
        "e.items.how": "compared the page with spring-menu.csv", "e.items.result": "27 of 27 items shown",
        "e.phone.how": "screenshot at 390 px", "e.phone.result": "no sideways scroll", "m.phone": "checked the screenshot myself",
        "c.prices": "Every price matches the shop sheet", "c.vat": "VAT is shown on every line",
        "e.prices.how": "diffed the page against prices.xlsx", "e.prices.result": "no mismatches",
        "c.hero": "The image is under 100 KB", "e.hero.how": "ls -l hero.webp", "e.hero.result": "46 KB",
    },
    "ru": {
        "r.short": "Подписи короче 80 символов, без эмодзи", "r.shots": "Начать со списка снимков фотографа", "r.all": "Подписать все фото на сайте",
        "r.menu": "Подписать только фото в меню", "r.checkout": "Не трогать страницы оформления заказа", "r.sheet": "Позиции брать из таблицы весеннего меню",
        "c.items": "На странице есть все сезонные позиции", "c.phone": "Страница читается на телефоне шириной 390 px",
        "e.items.how": "сверил страницу с spring-menu.csv", "e.items.result": "показаны 27 из 27",
        "e.phone.how": "снимок экрана на 390 px", "e.phone.result": "без горизонтальной прокрутки", "m.phone": "снимок проверил сам",
        "c.prices": "Все цены совпадают с таблицей магазина", "c.vat": "НДС указан в каждой строке",
        "e.prices.how": "сравнил страницу с prices.xlsx", "e.prices.result": "расхождений нет",
        "c.hero": "Изображение меньше 100 КБ", "e.hero.how": "ls -l hero.webp", "e.hero.result": "46 КБ",
    },
}


def requirement(label: int, text: str, kind: str, source: str, *, state: str = "active", replaces: str | None = None, file_name: str = "", deliveries: list[dict] | None = None, mark: dict | None = None) -> dict:
    """One requirement of a card as ``daedalus/extensions/board.py`` draws it."""
    return {
        "id": f"rq-{label}-{kind}", "label": f"R{label}", "number": label, "text": text, "kind": kind, "source": source,
        "from_operator": source == "operator" or source.startswith(("operator:", "answer:", "rule:")), "state": state, "replaces": replaces,
        "file_id": f"file-{label}" if file_name else None, "file_name": file_name, "evidence": None, "mark": mark, "created_at": "2026-09-24T09:10:00Z",
        "deliveries": deliveries or [],
    }


def delivery(staff_id: str, name: str, *, via: str = "brief", acknowledged: bool = False, opened: bool = False, cli: bool = False) -> dict:
    """One member given one requirement: when, how, and whether it confirmed it or opened its file."""
    return {
        "staff_id": staff_id, "staff_name": name, "via": via, "message_id": "m-req" if via == "message" else None, "delivered_at": "2026-09-24T09:31:00Z",
        "acknowledged_at": "2026-09-24T09:32:00Z" if acknowledged else None, "opened_at": "2026-09-24T09:33:00Z" if opened else None, "cli": cli,
    }


def photo_requirements(lang: str) -> list[dict]:
    """Lev's captions: a requirement of the operator's he confirmed, an input he opened, a scope the
    orchestrator narrowed (the old one kept, struck through) and sent him by a message he has not
    confirmed yet, and one of the operator's standing rules."""
    w = CONTRACT_WORDS[lang]
    return [
        requirement(1, w["r.short"], "quality", "operator", deliveries=[delivery("st-lev", "Lev", acknowledged=True)]),
        requirement(2, w["r.shots"], "input", "answer:q4r8tz", file_name="shot-list.csv", deliveries=[delivery("st-lev", "Lev", acknowledged=True, opened=True)]),
        requirement(3, w["r.all"], "scope", "orchestrator", state="superseded", deliveries=[delivery("st-lev", "Lev", acknowledged=True)]),
        requirement(4, w["r.menu"], "scope", "orchestrator", replaces="rq-3-scope", deliveries=[delivery("st-lev", "Lev", via="message")]),
        requirement(5, w["r.checkout"], "constraint", "rule:120", deliveries=[delivery("st-lev", "Lev", acknowledged=True)]),
    ]


def finished_contracts(lang: str) -> list[dict]:
    """Finished cards at each level of acceptance: Ira's menu checked item by item, with an input her
    command-line agent could only confirm in words; Max's price check handed in with one item left
    without a word; Olga's image approved by the operator."""
    words, w = FOCUS_WORDS[lang], CONTRACT_WORDS[lang]
    ira = BoardStub.assignee("st-ira", "Ira", harness="claude", color="orange")
    max_ = BoardStub.assignee("st-max", "Max", harness="codex")
    olga = BoardStub.assignee("st-olga", "Olga", harness="claude", color="violet")
    ok = {"ok": True, "note": "", "by": "orchestrator"}
    menu = [
        {"text": w["c.items"], "done": True, "evidence": {"how": w["e.items.how"], "result": w["e.items.result"], "by": "Ira", "at": "2026-09-24T08:40:00Z"}, "mark": ok},
        {"text": w["c.phone"], "done": True, "evidence": {"how": w["e.phone.how"], "result": w["e.phone.result"], "by": "Ira", "at": "2026-09-24T08:40:00Z"}, "mark": {**ok, "note": w["m.phone"]}},
    ]
    sheet = requirement(1, w["r.sheet"], "input", "operator", file_name="spring-menu.csv", deliveries=[delivery("st-ira", "Ira", acknowledged=True, cli=True)], mark=ok)
    prices = [
        {"text": w["c.prices"], "done": True, "evidence": {"how": w["e.prices.how"], "result": w["e.prices.result"], "by": "Max", "at": "2026-09-24T09:10:00Z"}},
        {"text": w["c.vat"], "done": False},
    ]
    hero = [{"text": w["c.hero"], "done": True, "evidence": {"how": w["e.hero.how"], "result": w["e.hero.result"], "by": "Olga", "at": "2026-09-23T17:00:00Z"}, "mark": ok}]
    return [
        BoardStub.task("t-menu", words["task.menu"], status="done", assignee=ira, checklist=menu, acceptance_state="accepted", requirements=[sheet], updated_at="2026-09-24T08:50:00Z"),
        BoardStub.task("t-prices", words["task.prices"], status="done", assignee=max_, checklist=prices, acceptance_state="handed_in", updated_at="2026-09-24T09:12:00Z"),
        BoardStub.task("t-hero", words["task.hero"], status="done", assignee=olga, checklist=hero, acceptance_state="operator_approved", contract_revision=1, updated_at="2026-09-23T18:00:00Z"),
    ]


MAIN_SID = "main0sess001"

# The main chat's invented day. The English texts are what `check_main.py` asserts against; the
# Russian ones are for the pictures. Postgres and SQLite, project names and dispatch ids stay as
# they are in both.
MAIN_WORDS: dict[str, dict[str, str]] = {
    "en": {
        "op.ask": "In Bakery, add a gluten-free section to the menu", "handed": "Handed to Bakery as d7k2m9.",
        "watering": "Watering plan", "plan.done": "the plan is in the brief", "finished": "Garden finished the watering plan.",
        "ask.db": "Postgres or SQLite for the orders?", "ask.sunday": "Deliver on Sundays?", "yes": "Yes", "no": "No",
        "ask.create": "Create the project Shop?\n· make the host folder /home/someone/shop", "create": "Create", "dont": "Don't create",
        "ask.dawn": "Water at dawn?", "gluten": "Gluten-free menu", "recipes": "Recipes collected", "survey": "Survey the folders and write the brief",
    },
    "ru": {
        "op.ask": "В Bakery добавь в меню раздел без глютена", "handed": "Передал в Bakery как d7k2m9.",
        "watering": "План полива", "plan.done": "план в брифе", "finished": "Garden закончил план полива.",
        "ask.db": "Postgres или SQLite для заказов?", "ask.sunday": "Доставлять по воскресеньям?", "yes": "Да", "no": "Нет",
        "ask.create": "Создать проект Shop?\n· создать на хосте папку /home/someone/shop", "create": "Создать", "dont": "Не создавать",
        "ask.dawn": "Поливать на рассвете?", "gluten": "Меню без глютена", "recipes": "Рецепты собраны", "survey": "Осмотреть папки и написать бриф",
    },
}


class MainStub:
    """The main chat's routes, stateful: answers close cards, a cancel closes a dispatch."""

    def __init__(self, lang: str = "en") -> None:
        w = self.words = MAIN_WORDS[lang]
        self.posts: list[tuple[str, dict]] = []
        project = {"id": "p-main", "name": "Main", "folders": [folder("/srv/workspaces/main")], "created_at": "2026-09-20T00:00:00Z", "settings": {"snapshots": False, "system": "dispatcher"}, "system": "dispatcher"}
        messages = [
            {"role": "user", "seq": 1, "origin": "operator", "text": w["op.ask"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-25T09:00:00Z"},
            {"role": "assistant", "seq": 2, "text": w["handed"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-25T09:00:05Z"},
            {"role": "user", "seq": 3, "origin": "events", "text": "[reports · 1 since 09:40]\n- 09:40 Garden closed dispatch dg4h1x \"" + w["watering"] + "\" as done: " + w["plan.done"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-25T09:40:00Z"},
            {"role": "assistant", "seq": 4, "text": w["finished"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-25T09:40:05Z"},
        ]
        self.detail = FocusStub.session_detail(MAIN_SID, "Main", project, messages)
        self.opened = False

        def a(id_: str, short: str, pid: str | None, name: str, text: str, **over: object) -> dict:
            row = {"id": id_, "short_id": short, "project_id": pid, "origin": "orchestrator", "kind": "question", "staff_id": None, "task_id": None, "text": text,
                   "detail": {"options": ["Postgres", "SQLite"]}, "routed_to": "operator", "suggestion": "", "created_at": "2026-09-25T09:10:00Z", "resolved_at": None,
                   "resolved_by": None, "resolution": {}, "dispatch_id": "d7k2m9", "project_name": name, "asker": "orchestrator", "host": False}
            row.update(over)
            return row

        self.asks = [
            a("ask-db", "q1db00", "p-bakery", "Bakery", w["ask.db"]),
            a("ask-late", "q2lt00", "p-bakery", "Bakery", w["ask.sunday"], detail={"options": [w["yes"], w["no"]]}, created_at="2026-09-25T09:12:00Z"),
            a("ask-new", "q3nw00", None, "", w["ask.create"], origin="dispatcher", kind="project",
              detail={"options": [w["create"], w["dont"]]}, dispatch_id=None, asker="main", host=True, created_at="2026-09-25T09:15:00Z"),
            a("ask-old", "q4ol00", "p-garden", "Garden", w["ask.dawn"], resolved_at="2026-09-25T09:30:00Z", resolved_by="operator", resolution={"selected": [w["yes"]], "via": "telegram"}),
        ]
        self.dispatches = [
            {"id": "d7k2m9", "project_id": "p-bakery", "project_name": "Bakery", "seq": 3, "kind": "work", "title": w["gluten"], "text": w["op.ask"],
             "status": "open", "result": "", "created_at": "2026-09-25T09:00:00Z", "updated_at": "2026-09-25T09:20:00Z", "closed_at": None, "stalled_at": None,
             "last": {"id": 1, "dispatch_id": "d7k2m9", "at": "2026-09-25T09:20:00Z", "author": "orchestrator", "kind": "progress", "text": w["recipes"]}},
            {"id": "dq8s1p", "project_id": "p-garden", "project_name": "Garden", "seq": 1, "kind": "setup", "title": w["survey"], "text": w["survey"],
             "status": "open", "result": "", "created_at": "2026-09-25T08:00:00Z", "updated_at": "2026-09-25T08:10:00Z", "closed_at": None, "stalled_at": "2026-09-25T08:40:00Z", "last": None},
            {"id": "dg4h1x", "project_id": "p-garden", "project_name": "Garden", "seq": 2, "kind": "work", "title": w["watering"], "text": w["watering"],
             "status": "done", "result": w["plan.done"], "created_at": "2026-09-24T08:00:00Z", "updated_at": "2026-09-25T09:40:00Z", "closed_at": "2026-09-25T09:40:00Z", "stalled_at": None, "last": None},
        ]
        self.setup = [{"project_id": "p-garden", "name": "Garden"}]

    def view(self) -> dict:
        return {"session_id": MAIN_SID if self.opened else "", "dispatches": self.dispatches, "asks": self.asks, "questions": sum(1 for x in self.asks if not x["resolved_at"]), "setup": self.setup}

    def answer(self, method: str, path: str, body: dict | None) -> tuple[int, object] | None:
        if path == "/api/main" and method == "GET":
            return 200, self.view()
        if path == "/api/questions" and method == "GET":
            rows = sorted((x for x in self.asks if not x["resolved_at"]), key=lambda x: x["created_at"])
            return 200, {"questions": [question_view(x, x["project_name"], asker=x["asker"]) for x in rows]}
        if path == "/api/asks/answer" and method == "POST":
            items = list(body or []) if isinstance(body, list) else []
            self.posts.append((path, {"items": items}))
            # Answered on the phone a moment earlier: the first answer wins.
            late = {"ask-late": {"selected": [self.words["yes"]], "via": "telegram"}}
            return 200, answer_batch(self.asks, items, via="main", late=late)
        if path == "/api/main" and method == "POST":
            self.opened = True
            self.posts.append((path, {}))
            return 200, {"session_id": MAIN_SID}
        if path == f"/api/sessions/{MAIN_SID}" and method == "GET":
            return 200, self.detail
        if path.startswith("/api/asks/") and path.endswith("/answer") and method == "POST":
            ref = path.split("/")[3]
            found = next((x for x in self.asks if x["id"] == ref), None)
            if found is None:
                return 404, {"detail": "no such request"}
            self.posts.append((path, dict(body or {})))
            if ref == "ask-late" or found["resolved_at"]:
                # Answered on the phone a moment earlier: the first answer wins.
                found.update(resolved_at="2026-09-25T09:59:00Z", resolved_by="operator", resolution={"selected": [self.words["yes"]], "via": "telegram"})
                return 409, {"detail": "request q2lt00 was already answered by the operator"}
            payload = dict(body or {})
            found.update(resolved_at="2026-09-25T10:00:00Z", resolved_by="operator", resolution={"selected": payload.get("selected") or [], "text": payload.get("text") or "", "via": payload.get("window") or "app"})
            return 200, {"state": "answered", "delivered": True, "error": "", "ask": found}
        if path.startswith("/api/dispatches/") and path.endswith("/cancel") and method == "POST":
            ref = path.split("/")[3]
            self.posts.append((path, dict(body or {})))
            found = next(d for d in self.dispatches if d["id"] == ref)
            found.update(status="cancelled", closed_at="2026-09-25T10:01:00Z", result="cancelled")
            return 200, found
        if path.startswith("/api/dispatches/") and method == "GET":
            ref = path.split("/")[3]
            found = next((d for d in self.dispatches if d["id"] == ref), None)
            return (200, {**found, "messages": [found["last"]] if found and found["last"] else [], "asks": []}) if found else (404, {"detail": "no such dispatch"})
        if path.endswith("/setup/finish") and method == "POST":
            self.posts.append((path, {}))
            self.setup = []
            return 200, {"finished": True}
        return None
