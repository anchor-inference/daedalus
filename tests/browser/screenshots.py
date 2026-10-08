"""Take the README screenshots of the built app over an invented installation.

Everything the app asks the API for is answered here with made-up data (a small studio's
agents, a bakery site, a weekly digest), so the pictures show the product and nothing of the
operator's own sessions. Build the app, serve it with tests/browser/serve_app.py, then:

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app OUT=docs/screenshots python3 tests/browser/screenshots.py

One PNG per screen lands in OUT (default: the directory this file is in). The Russian set is the
same run in the other language:

    LANG_UI=ru OUT=docs/screenshots/ru python3 tests/browser/screenshots.py

CHROMIUM points at the browser to drive and defaults to /usr/local/bin/chromium, which is where a
container puts one. On a machine that installed its browser through Playwright it has to be set, or
the launch fails on a path that is not there.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import (  # noqa: E402
    DEFAULT_APP,
    FILE_TEXT,
    FOCUS_WORDS,
    GATES,
    BoardStub,
    FocusStub,
    HarnessesStub,
    MainStub,
    TeamStub,
    Unhandled,
    expect_app,
    file_entries,
    file_search,
    folders,
    fulfil_shared,
    notification_preferences,
    session_tool_groups,
)

BASE = os.environ.get("APP_URL", DEFAULT_APP)
# The app is bilingual, and so is this set: LANG_UI=ru opens every page with ?lang=ru and the words
# the helpers click on come from the same column the app reads. The pictures go wherever OUT says.
LANG = os.environ.get("LANG_UI", "en")
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
OUT = Path(os.environ.get("OUT", str(Path(__file__).parent)))
NOW = datetime.now(UTC)


def ago(**kw: float) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def ahead(**kw: float) -> str:
    return (NOW + timedelta(**kw)).isoformat()


# ---- the invented installation ------------------------------------------------------------

S1, S2, S3, S4, S5, S6, S7, S8 = "a1b2c3d4e5f6", "b2c3d4e5f6a1", "c3d4e5f6a1b2", "d4e5f6a1b2c3", "e5f6a1b2c3d4", "f6a1b2c3d4e5", "0a1b2c3d4e5f", "1b2c3d4e5f6a"
S9, S10, S11 = "2c3d4e5f6a1b", "3d4e5f6a1b2c", "4e5f6a1b2c3d"

LOOP = {"mode": "interval", "interval_seconds": 5400, "status": "active", "run_count": 14, "max_runs": None, "next_run_at": ahead(minutes=38), "last_run_at": ago(minutes=52), "last_reason": None, "stop_reason": None, "pause_note": None, "instruction": "Read the support inbox, answer what you can, and put the rest on the board."}


P1, P2, P3, P4, PV = "9f3c2a1b7d40", "2e7b5c9a1f88", "4c6d8e0f2a13", "6e8f0a2b4c35", "7c1e4d9f2a06"

PROJECTS = [
    {"id": P1, "name": "Bakery site", "folders": folders("/home/operator/work/bakery"), "created_at": ago(days=9), "settings": {"snapshots": True}, "sessions": [{"id": "a1b2c3d4e5f6", "title": "Bakery site", "running": True}, {"id": "b2c3d4e5f6a1", "title": "Bakery site: photos", "running": False}, {"id": "e5f6a1b2c3d4", "title": "Bakery site (fork @412)", "running": False}]},
    {"id": P2, "name": "Expenses", "folders": folders("/home/operator/work/expenses"), "created_at": ago(days=4), "settings": {"snapshots": False}, "sessions": [{"id": "f6a1b2c3d4e5", "title": "Expense tracker", "running": False}]},
    {"id": P3, "name": "Support", "folders": folders("/home/operator/work/support"), "created_at": ago(days=6), "settings": {"snapshots": True}, "sessions": [{"id": S3, "title": "Support inbox", "running": False}]},
    {"id": P4, "name": "Weekly digest", "folders": folders("/home/operator/work/digest"), "created_at": ago(days=5), "settings": {"snapshots": True}, "sessions": [{"id": S4, "title": "Weekly digest", "running": True}]},
    # A folder added but not mounted yet: in Docker that is a restart away, and the app says so.
    {"id": "5a8d1c0b6e22", "name": "Courier rates", "folders": folders("/home/operator/documents/courier", reachable=False), "created_at": ago(hours=2), "settings": {"snapshots": False}, "sessions": []},
    # The installation's own: the concierge and what it started by being spoken to.
    {"id": PV, "name": "Voice", "folders": folders("/home/operator/.daedalus/workspaces/voice"), "created_at": ago(days=12), "settings": {"snapshots": False, "system": "voice"}, "system": "voice", "sessions": []},
]


def _team() -> TeamStub:
    """The Bakery site's team: a Daedalus writer at work, a Claude Code reviewer waiting on permission,
    a Codex tester with nothing to do, and a one-off helper that finished its turn."""
    bakery = next(p for p in PROJECTS if p["id"] == P1)
    listed = [dict(f, is_git=True) for f in bakery["folders"]]
    staff = [
        TeamStub.member("st-ada", "Ada", status="working", sessions=4, color="teal", role="Builds the seasonal menu page", agent="builder", model="strong", created_at=ago(days=3)),
        TeamStub.member("st-cleo", "Cleo", harness="claude", status="permission", sessions=9, color="violet", role="Reviews every change before it reaches review", agent="code-reviewer", model="opus", permission_mode="acceptEdits", created_at=ago(days=3)),
        TeamStub.member("st-kai", "Kai", harness="codex", sessions=2, color="amber", role="Keeps the checkout tests green", model="gpt-5.2-codex", isolation="shared", created_at=ago(days=2)),
        TeamStub.member("st-photo", "Photo sorter", status="turn_done_unseen", sessions=1, color="rose", role="Renames and crops the product photos", one_off=True, isolation="shared", created_at=ago(hours=5)),
    ]
    for member in staff:
        member["project_id"] = P1
    return TeamStub({**bakery, "folders": listed}, staff=staff, presets=[{"id": "strong", "label": "Claude Opus 5"}, {"id": "fast", "label": "DeepSeek Flash"}], personas=["builder", "reviewer", "tester"])


def session(id_: str, title: str, model: str, *, status: str = "idle", last: str, own: bool = False, meta: dict | None = None, project: str) -> dict:
    row = next(p for p in PROJECTS if p["id"] == project)
    root = row["folders"][0]["path"]
    path = f"{root}/.agents/{id_}" if own else root
    return {"id": id_, "title": title, "status": status, "created_at": ago(days=3), "last_message_at": last, "run_id": "run1" if status == "running" else None, "model": model, "workspace": path.rsplit("/", 1)[-1], "workspace_path": path, "workspace_own": own, "metadata": meta or {}, "project_id": project, "project": row["name"]}


SESSIONS = [
    session(S1, "Bakery site", "Claude Opus 5", status="running", last=ago(seconds=40), project=P1),
    session(S2, "Bakery site: photos", "Local Qwen3.8", last=ago(minutes=12), project=P1),
    session(S3, "Support inbox", "GPT-5.6 Luna", last=ago(minutes=52), meta={"loop": LOOP}, project=P3),
    session(S7, "[sub] triage", "GPT-5.6 Luna", last=ago(minutes=53), meta={"subagent_of": S3, "subagent_name": "triage"}, project=P3),
    session(S8, "[sub] reply-drafts", "GPT-5.6 Luna", last=ago(minutes=51), meta={"subagent_of": S3, "subagent_name": "reply-drafts"}, project=P3),
    session(S4, "Weekly digest", "DeepSeek Flash", status="waiting", last=ago(minutes=4), project=P4),
    session(S5, "Bakery site (fork @412)", "DeepSeek Flash", last=ago(hours=1), meta={"forked_from": {"session_id": S1, "seq": 412}}, project=P1),
    session(S6, "Expense tracker", "Local Qwen3.8", last=ago(days=1), project=P2),
    # Started by talking: the concierge itself, one agent in the folder they share and one with its own.
    session(S9, "Voice", "Qwen 3.7 Flash", last=ago(minutes=3), project=PV, own=False),
    session(S10, "Invoice run", "Claude Opus 5", status="running", last=ago(seconds=20), project=PV, own=False, meta={"voice_parent": S9}),
    session(S11, "Courier quotes", "DeepSeek Flash", last=ago(minutes=26), project=PV, own=True, meta={"voice_parent": S9}),
]

# Counted over the whole table by the API: the folder header says how many agents are in it whatever
# the page of rows beside it reached.
FOLDER_COUNTS = {
    P1: {"total": 3, "active": 1, "loops": 0, "last_message_at": ago(seconds=40)},
    P2: {"total": 1, "active": 0, "loops": 0, "last_message_at": ago(days=1)},
    P3: {"total": 1, "active": 0, "loops": 1, "last_message_at": ago(minutes=52)},
    P4: {"total": 1, "active": 1, "loops": 0, "last_message_at": ago(minutes=4)},
    "5a8d1c0b6e22": {"total": 0, "active": 0, "loops": 0, "last_message_at": ""},
    PV: {"total": 3, "active": 1, "loops": 0, "last_message_at": ago(seconds=20)},
}
def listing() -> dict:
    """What GET /api/sessions answers: the page of rows and their project folders."""
    folders = [{k: v for k, v in p.items() if k != "sessions"} | FOLDER_COUNTS[p["id"]] for p in PROJECTS]
    return {"sessions": SESSIONS, "projects": folders}

ANSWER = """The menu page is live and checked on a phone.

**What changed**

- `menu.html` — the seasonal section reads from `data/menu.json`, so the owner edits one file
- prices are formatted from the same JSON; no more hand-typed `€` in the markup
- the hero image is 46 KB now (was 1.9 MB), same crop

**Checks**

| check | result |
|---|---|
| Lighthouse, mobile | 98 / 100 / 100 / 100 |
| links | 41 ok, 0 broken |
| menu items | 27, matches the sheet |

The preview runs at the address in the panel. Next I would wire the order form to the sheet; say the word."""

IMAGE_CALL, SEND_CALL = "toolu_img1", "toolu_send1"

MESSAGES = [
    {"role": "user", "seq": 401, "text": "Rebuild the menu page from the owner's sheet: seasonal items on top, prices from the sheet, and make the hero image smaller. Check it on a phone.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=9)},
    {"role": "assistant", "seq": 402, "text": "", "thinking": "The sheet has three sections; seasonal is the third. I will move it first and generate the page from JSON so the owner never edits markup.", "tool_calls": [{"id": "toolu_1", "name": "Read", "arguments": {"path": "data/menu.csv"}}, {"id": "toolu_2", "name": "Read", "arguments": {"path": "menu.html"}}], "tool_results": [], "created_at": ago(minutes=9)},
    {"role": "tool", "seq": 403, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": "toolu_1", "content": "section,name,price,note\nseasonal,Pumpkin loaf,4.80,October\nseasonal,Fig tart,5.20,\nbread,Sourdough,6.50,\n… 24 more rows", "is_error": False, "length": 1412}, {"id": "toolu_2", "content": "<!doctype html>\n<html lang=\"en\">…\n<section id=\"menu\">…</section>", "is_error": False, "length": 6120}], "created_at": ago(minutes=8, seconds=57)},
    {"role": "assistant", "seq": 404, "text": "", "thinking": "", "tool_calls": [{"id": "toolu_3", "name": "Write", "arguments": {"path": "data/menu.json", "content": "[{\"section\": \"seasonal\", \"items\": […]}]"}}, {"id": "toolu_4", "name": "Edit", "arguments": {"path": "menu.html", "old": "<section id=\"menu\">", "new": "<section id=\"menu\" data-source=\"data/menu.json\">"}}], "tool_results": [], "created_at": ago(minutes=8)},
    {"role": "tool", "seq": 405, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": "toolu_3", "content": "wrote data/menu.json (27 items)", "is_error": False}, {"id": "toolu_4", "content": "menu.html: 1 replacement", "is_error": False}], "created_at": ago(minutes=7, seconds=58)},
    {"role": "assistant", "seq": 406, "text": "", "thinking": "", "tool_calls": [{"id": "toolu_5", "name": "Exec", "arguments": {"command": "convert images/hero.jpg -resize 1600x -quality 78 images/hero.jpg && ls -la images/hero.jpg"}}], "tool_results": [], "created_at": ago(minutes=7)},
    {"role": "tool", "seq": 407, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": "toolu_5", "content": "exit_code=0 elapsed=0.8s cwd=/srv/workspaces/a1b2c3d4e5f6 sandbox=workspace\n-rw-r--r-- 1 agent agent 46812 images/hero.jpg", "is_error": False}], "created_at": ago(minutes=6, seconds=52)},
    {"role": "assistant", "seq": 408, "text": "", "thinking": "", "tool_calls": [{"id": IMAGE_CALL, "name": "ImageView", "arguments": {"path": "shots/menu-phone.png", "question": "Is the seasonal section first and readable at 390 px?"}}], "tool_results": [], "created_at": ago(minutes=6)},
    {"role": "tool", "seq": 409, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": IMAGE_CALL, "content": "Yes: 'Seasonal' is the first heading under the hero, three items with prices aligned right, nothing overflows the viewport.", "is_error": False}], "created_at": ago(minutes=5, seconds=54)},
    {"role": "assistant", "seq": 410, "text": "", "thinking": "", "tool_calls": [{"id": "toolu_7", "name": "Verify", "arguments": {"command": "npx lighthouse http://127.0.0.1:8100/menu.html --preset=mobile --quiet", "criterion": "performance ≥ 90"}}, {"id": SEND_CALL, "name": "SendFile", "arguments": {"path": "reports/menu-check.md", "caption": "the check report"}}], "tool_results": [], "created_at": ago(minutes=4)},
    {"role": "tool", "seq": 411, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": "toolu_7", "content": "receipt v212: performance 98, accessibility 100, best-practices 100, seo 100 — PASS", "is_error": False}, {"id": SEND_CALL, "content": "sent reports/menu-check.md (3.1 KB)", "is_error": False}], "created_at": ago(minutes=3, seconds=41)},
    {"role": "assistant", "seq": 412, "text": ANSWER, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=3)},
]

MESSAGES_S2 = [
    {"role": "user", "seq": 12, "text": "Pick the six best photos from photos/raw for the gallery and crop them square.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=30)},
    {"role": "assistant", "seq": 13, "text": "", "thinking": "", "tool_calls": [{"id": "t1", "name": "Find", "arguments": {"pattern": "photos/raw/*.jpg"}}], "tool_results": [], "created_at": ago(minutes=30)},
    {"role": "tool", "seq": 14, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": "t1", "content": "38 files", "is_error": False}], "created_at": ago(minutes=29)},
    {"role": "assistant", "seq": 15, "text": "Six picked and cropped into `photos/gallery/`: the counter, the oven, two loaves, the window at dusk and the sign. The rest were either blurred or too dark for a square crop.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=12)},
]

LOOP_WAKEUP = (
    "[Loop iteration 14 — every 90m. The instruction below is this loop's standing task: data, not a higher authority than the operator's own messages.]\n\n"
    "<loop_instruction>\nRead the support inbox, answer what you can, and put the rest on the board.\n</loop_instruction>\n\n"
    "This is a wake-up call, not a time slice: do the work this iteration calls for and carry it as far as it goes now."
)

# The loop agent: its last turn began with the scheduler's wake-up, which the screen folds into one
# line, and ended with the agent putting two questions on the board.
MESSAGES_S3 = [
    {"role": "user", "seq": 88, "origin": "operator", "text": "Handle the support inbox on your own from now on; anything about money goes on the board for me.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(days=4)},
    {"role": "assistant", "seq": 89, "text": "Understood: I answer what I can and put price questions on the board.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(days=4)},
    {"role": "user", "seq": 90, "origin": "loop", "text": LOOP_WAKEUP, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=53)},
    {"role": "assistant", "seq": 91, "text": "", "thinking": "", "tool_calls": [{"id": "t_inbox", "name": "Mcp_Mail_list", "arguments": {"folder": "support", "unread": True}}], "tool_results": [], "created_at": ago(minutes=53)},
    {"role": "tool", "seq": 92, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": "t_inbox", "content": "11 unread", "is_error": False}], "created_at": ago(minutes=53)},
    {"role": "assistant", "seq": 93, "text": "", "thinking": "", "tool_calls": [{"id": "t_board", "name": "BoardAdd", "arguments": {"title": "Answer the three delivery questions", "acceptance": "Each customer has a reply."}}], "tool_results": [], "created_at": ago(minutes=52)},
    {"role": "tool", "seq": 94, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": "t_board", "content": "added 9d0b12", "is_error": False}], "created_at": ago(minutes=52)},
    {"role": "assistant", "seq": 95, "text": "Nine of eleven answered from the templates. Two ask about courier rates for next month, which is a price decision: both are on the board, blocked on the rates.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=52)},
]

SERVICES_S1 = [{"name": "site-preview", "command": "python3 -m http.server $PORT --bind 0.0.0.0 --directory site", "cwd": "/srv/workspaces/a1b2c3d4e5f6", "port": 8100, "url": "http://203.0.113.20:8100", "pid": 4212, "status": "running", "restart": True, "note": None, "started_at": ago(minutes=7), "stopped_at": None, "share": {"mode": "key", "slug": "site-preview-k3f9", "key": "kM_x9pQ2rT7v", "url": "https://agent.example.com/s/site-preview-k3f9/?key=kM_x9pQ2rT7v", "public_base": "https://agent.example.com"}}]
SERVICES_ALL = [
    {**SERVICES_S1[0], "session_id": S1, "session_title": "Bakery site"},
    {"name": "digest-api", "command": "uvicorn app:api --host 0.0.0.0 --port $PORT", "cwd": "/srv/workspaces/d4e5f6a1b2c3", "port": 8101, "url": "http://203.0.113.20:8101", "pid": 4380, "status": "running", "restart": True, "note": None, "started_at": ago(hours=3), "stopped_at": None, "share": {"mode": "local", "slug": None, "key": None, "url": None, "public_base": "https://agent.example.com"}, "session_id": S4, "session_title": "Weekly digest"},
    {"name": "expense-ui", "command": "npm run dev -- --port $PORT", "cwd": "/srv/workspaces/f6a1b2c3d4e5", "port": 8102, "url": None, "pid": None, "status": "stopped", "restart": False, "note": "stopped by the operator", "started_at": ago(days=1), "stopped_at": ago(hours=20), "share": {"mode": "local", "slug": None, "key": None, "url": None, "public_base": "https://agent.example.com"}, "session_id": S6, "session_title": "Expense tracker"},
]

SUBAGENTS_S1 = [{"session_id": "sub1", "name": "link-check", "running": False, "status": "done", "model": "Local Qwen3.8", "kept": False}]

QUESTION = {"questions": [{"question": "The digest has 14 items this week; keep it to the usual 8?", "header": "Length", "options": [{"label": "Top 8", "description": "the usual"}, {"label": "All 14"}, {"label": "Split into two mails"}], "allow_custom": True}]}


def detail(id_: str) -> dict:
    s = next(x for x in SESSIONS if x["id"] == id_)
    messages = MESSAGES if id_ == S1 else MESSAGES_S2 if id_ == S2 else MESSAGES_S3 if id_ == S3 else [{"role": "user", "seq": 1, "text": "Start.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(hours=1)}, {"role": "assistant", "seq": 2, "text": "Started. Waiting for the sheet.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(hours=1)}]
    project = next(p for p in PROJECTS if p["id"] == s["project_id"])
    # A project session works in the project root, and the server answers exactly that: the folder is
    # not a directory of the session's own, and its name is the folder's.
    workspace = s["workspace_path"]
    # The composer's mode pictures (``ONLY=modes``): YAGNI on, the opening message marked as
    # the one that told the agent, and a phone mid-run, where the mode chip has to stay.
    yagni = id_ == S1 and getattr(stub, "yagni", False)
    if yagni:
        messages = [{**m, "yagni": "on"} if m["seq"] == 401 else m for m in messages]
    running = id_ == S1 and getattr(stub, "running", False)
    return {
        "id": id_, "title": s["title"], "status": "running" if running else "idle" if id_ != S4 else "waiting", "run_id": "r-modes" if running else None, "compacting": None,
        "yagni": yagni,
        "workspace": workspace, "workspace_name": workspace.rsplit("/", 1)[-1], "workspace_own": s["workspace_own"],
        "project": project,
        "workspace_sessions": [{"id": S2, "title": "Bakery site: photos"}] if id_ == S1 else [{"id": S1, "title": "Bakery site"}] if id_ == S2 else [],
        "pending": QUESTION if id_ == S4 else None, "model": (id_ == S1 and getattr(stub, "model", None)) or s["model"], "provider": "claude" if id_ == S1 else "opencode",
        "thinking": True, "reasoning_effort": "high",
        "messages": messages, "mode": "", "usd_cap": 4.0, "brief": "Site of a small bakery. Static HTML, no frameworks; the owner edits data files, never markup.", "spawned_by": None, "tools_off": [],
        "loop": LOOP if id_ == S3 else None, "services": SERVICES_S1 if id_ == S1 else [], "subagents": SUBAGENTS_S1 if id_ == S1 else [],
        "context": {"tokens": 41200, "window": 200000, "messages": len(messages), "summaries": 1, "operator_turns": 6},
        "usage": {"c": 38, "i": 612000, "o": 24100, "ch": 540000, "usd": 1.84},
    }


PROVIDER_USAGE = {"provider": "claude", "today": {"calls": 38, "input_tokens": 612000, "output_tokens": 24100, "cache_read_tokens": 540000, "cost_usd": 0}, "subscription": {"logged_in": True, "plan": "max", "windows": [{"name": "5h", "used_percent": 23, "resets_at": ahead(hours=3, minutes=12)}, {"name": "weekly", "used_percent": 41, "resets_at": ahead(days=4)}]}, "balance": None}

FILES = {
    "": [{"name": "data", "dir": True, "size": 0, "mtime": (NOW - timedelta(minutes=8)).timestamp()}, {"name": "images", "dir": True, "size": 0, "mtime": (NOW - timedelta(minutes=7)).timestamp()}, {"name": "photos", "dir": True, "size": 0, "mtime": (NOW - timedelta(minutes=12)).timestamp()}, {"name": "reports", "dir": True, "size": 0, "mtime": (NOW - timedelta(minutes=4)).timestamp()}, {"name": "shots", "dir": True, "size": 0, "mtime": (NOW - timedelta(minutes=6)).timestamp()}, {"name": "site", "dir": True, "size": 0, "mtime": (NOW - timedelta(minutes=8)).timestamp()}, {"name": "index.html", "dir": False, "size": 8120, "mtime": (NOW - timedelta(days=2)).timestamp()}, {"name": "menu.html", "dir": False, "size": 6340, "mtime": (NOW - timedelta(minutes=8)).timestamp()}, {"name": "NOTES.md", "dir": False, "size": 2210, "mtime": (NOW - timedelta(hours=5)).timestamp()}, {"name": "PLAN.md", "dir": False, "size": 1480, "mtime": (NOW - timedelta(minutes=4)).timestamp()}, {"name": "style.css", "dir": False, "size": 11020, "mtime": (NOW - timedelta(days=1)).timestamp()}],
}

NOTES_MD = """# Bakery site — notes

## What the owner asked for

- one page per section: **bread**, **pastry**, **seasonal**
- prices come from the sheet, never typed by hand
- the site must load fast on the shop's old phone

## Decisions

| topic | decision | why |
|---|---|---|
| framework | none, static HTML | the owner edits files over SFTP |
| images | ≤ 60 KB each, 1600 px wide | the phone in the shop is on 3G |
| menu | `data/menu.json`, rendered at build | one file to edit |

## Open

- [x] seasonal section first
- [x] hero image under 50 KB
- [ ] order form → the sheet
- [ ] opening hours from the calendar
"""

REPORT_MD = """# Menu page check

Lighthouse (mobile preset), 3 runs, median:

| category | score |
|---|---|
| performance | 98 |
| accessibility | 100 |
| best practices | 100 |
| seo | 100 |

Links: 41 checked, 0 broken. Menu items: 27, matching `data/menu.csv`.
"""

MEMORY = {
    "records": [
        {"id": "m1", "scope": "global", "scope_key": "", "kind": "preference", "text": "The operator writes in English and wants answers under 200 words unless asked for a report.", "salience": 0.9, "version": 1, "created_at": ago(days=6), "last_accessed_at": ago(hours=2)},
        {"id": "m2", "scope": "global", "scope_key": "", "kind": "fact", "text": "Deploys go through the `deploy` script in each project; never rsync by hand.", "salience": 0.8, "version": 2, "created_at": ago(days=5), "last_accessed_at": ago(days=1)},
        {"id": "m3", "scope": "session", "scope_key": S1, "kind": "fact", "text": "The bakery owner edits data files over SFTP; markup changes must not be required of them.", "salience": 0.85, "version": 1, "created_at": ago(days=3), "last_accessed_at": ago(minutes=9)},
        {"id": "m4", "scope": "session", "scope_key": S1, "kind": "fact", "text": "Images are capped at 60 KB and 1600 px; the shop's phone is on 3G.", "salience": 0.7, "version": 1, "created_at": ago(days=2), "last_accessed_at": ago(minutes=7)},
        {"id": "m5", "scope": "session", "scope_key": S3, "kind": "howto", "text": "Refund questions are answered with the template in templates/refund.md and put on the board for the operator.", "salience": 0.75, "version": 1, "created_at": ago(days=4), "last_accessed_at": ago(minutes=52)},
        {"id": "m6", "scope": "session", "scope_key": S4, "kind": "preference", "text": "The digest goes out on Friday at 09:00; eight items, newest first.", "salience": 0.8, "version": 1, "created_at": ago(days=7), "last_accessed_at": ago(minutes=4)},
    ],
    "buckets": [{"scope": "global", "scope_key": "", "title": None, "count": 2}, {"scope": "session", "scope_key": S1, "title": "Bakery site", "count": 2}, {"scope": "session", "scope_key": S3, "title": "Support inbox", "count": 1}, {"scope": "session", "scope_key": S4, "title": "Weekly digest", "count": 1}],
    "sessions": {S1: "Bakery site", S3: "Support inbox", S4: "Weekly digest"},
}

BOARD = [
    {"id": "7f2a1c", "title": "Order form posts to the owner's sheet", "status": "doing", "priority": 2, "acceptance": "A test order appears as a row in the sheet within a minute; the form says so.", "checklist": [{"text": "sheet API token in the key proxy", "done": True}, {"text": "form → POST /order", "done": True}, {"text": "confirmation page", "done": False}], "depends_on": [], "session_id": S1, "origin_session_id": S1, "notes": "", "created_at": ago(hours=2), "updated_at": ago(minutes=20)},
    {"id": "3b9e44", "title": "Opening hours from the shop calendar", "status": "todo", "priority": 3, "acceptance": "The footer shows this week's hours, read from the calendar at build time.", "checklist": [], "depends_on": ["7f2a1c"], "session_id": None, "origin_session_id": S1, "notes": "", "created_at": ago(hours=2), "updated_at": ago(hours=2)},
    {"id": "c41d07", "title": "Refund policy page", "status": "review", "priority": 2, "acceptance": "The page answers the five questions from the support inbox.", "checklist": [{"text": "draft", "done": True}, {"text": "owner's wording", "done": True}], "depends_on": [], "session_id": S3, "origin_session_id": S3, "notes": "", "created_at": ago(days=1), "updated_at": ago(minutes=50)},
    {"id": "9d0b12", "title": "Answer the three delivery questions", "status": "blocked", "priority": 1, "acceptance": "Each customer has a reply; the operator confirmed the courier's rates.", "checklist": [], "depends_on": ["e8a5f3"], "session_id": None, "origin_session_id": S3, "notes": "", "created_at": ago(hours=5), "updated_at": ago(hours=1)},
    {"id": "e8a5f3", "title": "Courier rates for next month", "status": "todo", "priority": 1, "acceptance": "The operator posted the rates.", "checklist": [], "depends_on": [], "session_id": None, "origin_session_id": None, "notes": "", "created_at": ago(hours=5), "updated_at": ago(hours=5)},
    {"id": "51c8aa", "title": "Gallery: six square photos", "status": "done", "priority": 3, "acceptance": "Six photos in photos/gallery, 800×800.", "checklist": [{"text": "pick", "done": True}, {"text": "crop", "done": True}], "depends_on": [], "session_id": S2, "origin_session_id": S2, "notes": "", "created_at": ago(hours=1), "updated_at": ago(minutes=12)},
]


def notification(id: int, at: str, kind: str, category: str, level: str, tone: str, title: str, body: str, session_id: str | None, seen: bool, count: int = 1, *, project: str | None = None, actions: list | None = None, ref: str | None = None) -> dict:
    return {
        "id": id, "at": at, "updated_at": at, "category": category, "kind": kind, "level": level, "tone": tone, "title": title, "body": body,
        "link": f"/app/agents/{session_id}" if session_id else "", "session_id": session_id, "run_id": None, "project_id": project, "staff_id": None,
        "terminal_id": None, "source": "system", "dedupe_key": ref, "request_ref": ref, "count": count, "actions": actions or [], "seen": seen,
        "resolved": None, "needs_you": ref is not None, "delivered": {},
    }


def buttons(*pairs: tuple[str, str, str]) -> list[dict]:
    """A request's buttons as the host words them (``id``, label, style); everything but Open is quick."""
    return [{"id": i, "label": label, "style": style, "quick": i != "open"} for i, label, style in pairs]


# The host writes these in the operator's language; the Russian run gets the Russian words it would.
NOTICE_WORDS = {
    "en": {"allow": "Allow", "deny": "Deny", "open": "Open", "permission": "Bakery site: photos is waiting for permission", "ask": "Weekly digest asks: keep it to the usual 8?", "eight": "Keep 8", "all": "All 14"},
    "ru": {"allow": "Разрешить", "deny": "Отклонить", "open": "Открыть", "permission": "Bakery site: photos ждёт разрешения", "ask": "Weekly digest спрашивает: оставить обычные 8?", "eight": "Оставить 8", "all": "Все 14"},
}[LANG]

PERMISSION = notification(
    33, ago(minutes=1), "policy", "permission", "urgent", "warning", NOTICE_WORDS["permission"], "Exec: npm install sharp · Bakery site", S2, False,
    project=P1, ref=f"policy:{S2}:exec.npm", actions=buttons(("allow", NOTICE_WORDS["allow"], "primary"), ("deny", NOTICE_WORDS["deny"], "default"), ("open", NOTICE_WORDS["open"], "ghost")),
)
QUESTION = notification(
    32, ago(minutes=4), "ask", "question", "normal", "warning", NOTICE_WORDS["ask"], "The digest has 14 items this week.", S4, False,
    project=P4, ref=f"ask:{S4}:toolu_ask", actions=buttons(("answer:0", NOTICE_WORDS["eight"], "primary"), ("answer:1", NOTICE_WORDS["all"], "default"), ("open", NOTICE_WORDS["open"], "ghost")),
)
INBOX = [
    PERMISSION,
    QUESTION,
    notification(31, ago(minutes=6), "run", "run_finished", "normal", "ok", "Bakery site: photos finished", "Six photos cropped to 800×800 and renamed.", S2, False, project=P1),
    notification(30, ago(minutes=20), "change_proposal", "system", "normal", "info", "Pull request #57 is waiting for your decision", "WebSearch: retry a backend that timed out once before falling back", None, False),
    notification(29, ago(minutes=52), "loop", "agent_notify", "quiet", "info", "Support inbox: 6 answered, 2 on the board", "Two delivery questions wait for the courier's rates.", S3, True, project=P3),
    notification(28, ago(hours=3), "service", "system", "normal", "warning", "digest-api restarted after the rebuild", "It was running before the rebuild and is running again on :8101.", S4, True, project=P4),
    notification(27, ago(hours=20), "balance", "spend", "normal", "warning", "DeepSeek balance below $5", "$4.62 left; the next threshold is $2.", None, True, count=2),
]
INBOX_SUMMARY = {"unseen": 4, "needs_you": 2}


def notifications_view(view: str) -> list[dict]:
    if view == "needs_you":
        return [e for e in INBOX if e["needs_you"]]
    if view == "unseen":
        return [e for e in INBOX if not e["seen"] and e["level"] != "quiet"]
    if view == "problems":
        return [e for e in INBOX if e["tone"] in ("warning", "error")]
    return INBOX


def notify_frames(*entries: dict) -> str:
    """The host's stream saying these notifications arrived just now, each marked for a toast."""
    frames = [f'event: hello\ndata: {json.dumps({"head": 900, "oldest": 1, "server_time": datetime.now(UTC).isoformat(), "client": ""})}\n\n']
    for n, e in enumerate(entries, start=901):
        event = {"seq": n, "at": datetime.now(UTC).isoformat(), "type": "notify", "project_id": e["project_id"], "session_id": e["session_id"], "staff_id": None, "terminal_id": None,
                 "payload": {"notification": e, "toast": True, "deliver": {"push": False, "desktop": False, "telegram": "handled"}, "merged": False, "summary": INBOX_SUMMARY}}
        frames.append(f"id: {n}\nevent: notify\ndata: {json.dumps(event)}\n\n")
    return "".join(frames)

PROPOSALS = [{"id": "p57", "repo": "daedalus", "branch": "bot/websearch-retry", "pr_number": 57, "pr_url": "https://github.com/example/daedalus/pull/57", "title": "WebSearch: retry a backend that timed out once before falling back", "summary": "A backend that answers 504 once is tried again after a second; only a second failure falls through to the next backend. Unit test added.", "status": "pending", "reason": None, "created_at": ago(minutes=20)}]

SCHEDULES = [
    {"id": "sch1", "name": "Weekly digest", "run_in": "self", "cron": "0 9 * * 5", "run_at": None, "prompt": "Collect the week's notes and send the digest.", "enabled": 1, "next_run_at": ahead(days=2, hours=8), "last_run_at": ago(days=5), "last_summary": "Eight items sent; two links fixed.", "kind": "agent", "target_session": S4, "created_by_session": S4, "active_session_id": None, "failure_count": 0, "last_error": None},
    {"id": "sch2", "name": "Backup check", "run_in": "new", "cron": "30 3 * * *", "run_at": None, "prompt": "Verify last night's backup restored into a scratch directory; report sizes.", "enabled": 1, "next_run_at": ahead(hours=5), "last_run_at": ago(hours=19), "last_summary": "Restore ok: 2.1 GB, 12 340 files.", "kind": "agent", "target_session": None, "created_by_session": None, "active_session_id": None, "failure_count": 0, "last_error": None},
    {"id": "sch3", "name": "Remind: renew the domain", "run_in": "new", "cron": None, "run_at": ahead(days=12), "prompt": "The bakery domain renews in 3 days.", "enabled": 1, "next_run_at": ahead(days=12), "last_run_at": None, "last_summary": None, "kind": "message", "target_session": None, "created_by_session": S1, "active_session_id": None, "failure_count": 0, "last_error": None},
]

def usage_data() -> dict:
    daily = []
    for d in range(14):
        day = (NOW - timedelta(days=13 - d)).date().isoformat()
        daily.append({"day": day, "provider_id": "deepseek", "model": "deepseek-flash", "calls": 40 + (d * 7) % 23, "input_tokens": 900_000 + (d * 131_000) % 700_000, "output_tokens": 38_000 + (d * 9_000) % 30_000, "cache_read_tokens": 600_000, "reasoning_tokens": 12_000, "cost_usd": round(0.42 + ((d * 37) % 90) / 100, 2), "unmetered": 0})
        daily.append({"day": day, "provider_id": "claude", "model": "claude-opus-5", "calls": 12 + (d * 5) % 11, "input_tokens": 500_000, "output_tokens": 21_000, "cache_read_tokens": 450_000, "reasoning_tokens": 0, "cost_usd": 0, "unmetered": 12 + (d * 5) % 11})
        daily.append({"day": day, "provider_id": "opencode", "model": "gpt-5.6-luna", "calls": 20 + (d * 3) % 9, "input_tokens": 300_000, "output_tokens": 15_000, "cache_read_tokens": 200_000, "reasoning_tokens": 4_000, "cost_usd": 0.0, "unmetered": 0})
    recent = [
        {"at": ago(minutes=3), "provider_id": "claude", "model": "claude-opus-5", "purpose": "stream", "session_id": S1, "session_title": "Bakery site", "run_id": "r1", "input_tokens": 41_200, "output_tokens": 1_180, "cache_read_tokens": 39_000, "reasoning_tokens": 0, "cost_usd": 0, "duration_ms": 8_400, "raw": {}},
        {"at": ago(minutes=4), "provider_id": "deepseek", "model": "deepseek-flash", "purpose": "stream", "session_id": S4, "session_title": "Weekly digest", "run_id": "r4", "input_tokens": 18_300, "output_tokens": 640, "cache_read_tokens": 16_000, "reasoning_tokens": 210, "cost_usd": 0.0061, "duration_ms": 3_100, "raw": {}},
        {"at": ago(minutes=12), "provider_id": "opencode", "model": "gpt-5.6-luna", "purpose": "stream", "session_id": S3, "session_title": "Support inbox", "run_id": "r3", "input_tokens": 22_800, "output_tokens": 2_010, "cache_read_tokens": 20_000, "reasoning_tokens": 800, "cost_usd": 0.0, "duration_ms": 12_900, "raw": {}},
        {"at": ago(minutes=40), "provider_id": "deepseek", "model": "deepseek-flash", "purpose": "structured", "session_id": S6, "session_title": "Expense tracker", "run_id": None, "input_tokens": 61_000, "output_tokens": 1_900, "cache_read_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.019, "duration_ms": 22_000, "raw": {}},
    ]
    sessions = [
        {"session_id": S1, "title": "Bakery site", "calls": 38, "input_tokens": 612_000, "output_tokens": 24_100, "cost_usd": 0, "unmetered": 38},
        {"session_id": S3, "title": "Support inbox", "calls": 210, "input_tokens": 3_900_000, "output_tokens": 180_000, "cost_usd": 2.31, "unmetered": 0},
        {"session_id": S4, "title": "Weekly digest", "calls": 64, "input_tokens": 1_100_000, "output_tokens": 41_000, "cost_usd": 0.58, "unmetered": 0},
        {"session_id": S6, "title": "Expense tracker", "calls": 31, "input_tokens": 700_000, "output_tokens": 22_000, "cost_usd": 0.29, "unmetered": 0},
    ]
    subscriptions = {
        "claude": {"provider": "claude", "logged_in": True, "plan": "max", "windows": [{"name": "5h", "used_percent": 23, "resets_at": ahead(hours=3)}, {"name": "weekly", "used_percent": 41, "resets_at": ahead(days=4)}]},
        "opencode": {"provider": "opencode", "logged_in": True, "plan": "go", "windows": [{"name": "5h", "used_percent": 8, "resets_at": ahead(hours=1)}, {"name": "weekly", "used_percent": 12, "resets_at": ahead(days=6)}, {"name": "monthly", "used_percent": 31, "resets_at": ahead(days=17)}]},
    }
    return {"daily": daily, "recent": recent, "sessions": sessions, "subscriptions": subscriptions}


SETTINGS = {
    "model": {"preset": "deepseek-flash", "chain": ["deepseek-flash", "gpt-5.6-luna"]},
    "presets": {
        "deepseek-flash": {"provider": "deepseek", "model": "deepseek-flash", "label": "DeepSeek Flash", "thinking": True, "reasoning_effort": "medium", "images": False, "context_window": 128000, "max_output_tokens": 16384},
        "claude-opus-5": {"provider": "claude", "model": "claude-opus-5", "label": "Claude Opus 5", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 200000, "max_output_tokens": 32000},
        "gpt-5.6-luna": {"provider": "opencode", "model": "gpt-5.6-luna", "label": "GPT-5.6 Luna", "thinking": True, "reasoning_effort": "medium", "images": True, "context_window": 200000, "max_output_tokens": 32000},
        "qwen-local": {"provider": "vllm", "model": "Qwen3.8", "label": "Local Qwen3.8", "thinking": False, "reasoning_effort": "", "images": False, "context_window": 65536, "max_output_tokens": 4096},
    },
    "on_demand_defaults": {"deepseek-flash": True, "claude-opus-5": True, "gpt-5.6-luna": True, "qwen-local": False},
    "providers": {}, "prompt": {"rules": ""}, "vision": {"preset": "gpt-5.6-luna", "max_output_tokens": 800},
    "orchestrator": {"preset": "", "strongest": "claude-opus-5"},
    "asr": {"provider": "", "url": "", "api_key": "", "model": "", "language": "auto", "timeout_seconds": 60, "max_seconds": 120, "autosend": False},
    "tools": {"web": {"fetch_timeout_seconds": 30, "proxy": "", "user_agent": "", "fetch_max_chars": 40000, "search": {"backend": "searxng", "fallback": ["duckduckgo"], "results": 8, "timeout_seconds": 20, "searxng": {"url": "", "engines": "", "categories": "", "safesearch": 0}, "duckduckgo": {"url": "", "region": ""}, "serper": {"base_url": "", "gl": "", "hl": ""}, "keenable": {"base_url": "", "snippet_max_length": 0}, "tavily": {"base_url": "", "depth": ""}, "exa": {"base_url": "", "type": ""}, "perplexity": {"base_url": ""}}}, "exec": {"max_output_chars": 20000}},
    # The backends the host offers: the pictures of WebSearch show a primary, a fallback and one waiting for its key.
    "search_backends": [
        {"id": "searxng", "label": "SearXNG", "needs_key": False},
        {"id": "duckduckgo", "label": "DuckDuckGo", "needs_key": False},
        {"id": "serper", "label": "Serper (Google)", "needs_key": True, "available": True},
        {"id": "tavily", "label": "Tavily", "needs_key": True, "available": False},
    ],
    "self_change": {"approval": "manual", "auto_rebuild": True},
    "limits": {"max_iterations": 200, "tool_timeout_seconds": 900, "usd_per_run": 5, "usd_total": 0, "usd_total_per_provider": {}, "total_since": ""},
    "balance": {"enabled": True, "poll_seconds": 60, "thresholds_usd": [5, 2, 0.5]},
    "scheduler": {"topic_mode": "per_task", "catch_up_missed": True},
    "compaction": {"auto_ratio": 0.7, "keep_recent_messages": 6, "max_words": 900, "chunk_tokens": 30000, "min_messages": 12, "call_timeout_seconds": 90, "core_trigger_ratio": 0.85, "preset": ""},
    "telegram": {"verbosity": 1, "reactions": True, "topic_status_emoji": True, "stale_after_seconds": 600, "max_inbound_file_mb": 200, "forward_unknown_commands": True, "slow_tool_seconds": 30, "photo_caption_wait_seconds": 8},
    "terminals": {"running_cap": 20},
}

# Two phones and a laptop that receive push; the phone that has been off for two days keeps failing.
PUSH_DEVICES = [
    {"id": 1, "endpoint": "https://push.example.com/send/laptop", "device": "Chrome · macOS", "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36", "created_at": ago(days=12), "last_ok_at": ago(minutes=8), "failures": 0, "last_error": None, "apple": False},
    {"id": 2, "endpoint": "https://push.example.com/send/pixel", "device": "Chrome · Android", "user_agent": "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36", "created_at": ago(days=30), "last_ok_at": ago(days=2), "failures": 3, "last_error": "503 Service Unavailable", "apple": False},
    {"id": 3, "endpoint": "https://web.push.apple.com/send/iphone", "device": "Safari · iPhone", "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1", "created_at": ago(days=5), "last_ok_at": ago(hours=1), "failures": 0, "last_error": None, "apple": True},
]


def png(width: int, height: int) -> bytes:
    """A small warm gradient with a lighter block: the 'phone screenshot' the agent looked at."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            base = (28 + y * 40 // height, 22 + y * 25 // height, 20 + y * 18 // height)
            if height * 0.18 < y < height * 0.42 and width * 0.08 < x < width * 0.92:
                base = (236, 224, 205)
            elif height * 0.5 < y < height * 0.9 and width * 0.08 < x < width * 0.92 and int((y - height * 0.5) // (height * 0.1)) % 2 == 0:
                base = (60, 48, 40)
            row.extend(base)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


PHONE_PNG = png(390, 520)


def wav(seconds: float = 30.0, rate: int = 8000) -> bytes:
    """A playable, silent WAV.

    The speaking state is not a flag the page can be put into: it is true while an `<audio>` element
    is playing, so the picture needs something that really plays. Silence for half a minute is the
    smallest thing that is really audio.
    """
    frames = int(seconds * rate)
    body = b"\x00\x00" * frames
    header = b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(body))
    return header + body


SILENCE = wav()


VOICE = {
    "enabled": True,
    "session_id": S2,
    "model": "Local Qwen3.8",
    # The Model row: the same table Settings shows, with the server's judgement of which of them
    # answers quickly enough to hold a conversation, and which one this installation is on.
    "preset": "qwen-local",
    "using": "qwen-local",
    "presets": [
        {
            "id": pid,
            "label": p["label"],
            "provider": p["provider"],
            "model": p["model"],
            "thinking": p["thinking"],
            "max_output_tokens": p["max_output_tokens"],
            "fast": not p["thinking"] and p["max_output_tokens"] <= 8_000,
        }
        for pid, p in SETTINGS["presets"].items()
    ],
    # A voice downloaded onto this machine, which is what the chip on the page is there to say: the
    # stub used to leave `kind` out, so every picture of the page called it "server voice" instead.
    "tts": {"configured": True, "reason": "", "kind": "local", "voice": "Dmitri", "model": "vits-piper-ru_RU-dmitri", "state": "ready",
            "loaded_in_ms": 1480, "error": "",
            # The last answer's wait between the words being written and a sound, as the server
            # measured its half of it. The page draws it as one quiet diagnostic line.
            "last_turn": {"turn": "run-1", "first_audio_ms": 720, "clip_ms": 640, "load_ms": 0}},
    # A model that runs on this machine, in memory and ready: the case the page is designed around.
    "stt": {
        "configured": True,
        "reason": "",
        "kind": "local",
        "state": "ready",
        "loaded_in_ms": 5400,
        "error": "",
        "local": {"model": "nemotron-streaming-multi", "label": "Nemotron 3.5 Streaming 0.6B", "installed": True, "active": True, "streaming": True, "loaded": "nemotron-streaming-multi"},
    },
    # One of each state the panel can be in: a line it said mid-run, which wins over the answer and
    # carries its own timestamp; a finished answer; and the two things it can be waiting for.
    "agents": [
        {
            "session_id": S1,
            "title": "Bakery site",
            "status": "running",
            "last_message_at": ago(minutes=6),
            "answer": "Rewriting the menu page so the seasonal section reads from one JSON file.",
            "progress": "Reading the menu page to see how the seasonal section is put together.",
            "progress_at": ago(seconds=20),
        },
        {"session_id": S3, "title": "Support inbox", "status": "idle", "last_message_at": ago(minutes=52), "answer": "Answered nine of eleven; two went on the board as they need a price decision."},
        {"session_id": S4, "title": "Weekly digest", "status": "waiting", "last_message_at": ago(minutes=4), "answer": "Which week should the digest cover — the one that just ended, or the running one?", "waiting": "operator"},
        {
            "session_id": S2,
            "title": "Invoice run",
            "status": "waiting",
            "last_message_at": ago(minutes=1),
            "answer": "Ready to send the eleven invoices for last month.",
            "progress": "Asking to send the invoices.",
            "progress_at": ago(seconds=50),
            "waiting": "approval",
        },
    ],
    "listening": True,
}


# An installation that has already been set up, and one that has not: "Add a model" is the first
# thing a fresh install shows, so it gets a picture of its own.
# One key, two command-line logins, and the rest with nothing behind them: the mixture a machine
# really has, and the one the cards have to tell apart.
PROVIDERS = [
    {"id": "openrouter", "kind": "openrouter", "base_url": "http://keyproxy:3200/openrouter", "via_proxy": True, "key_held": True, "key_kind": "api_key", "ready": True},
    {"id": "codex", "kind": "openai_compat", "base_url": "http://keyproxy:3200/codex/v1", "via_proxy": True, "key_held": True, "key_kind": "cli_login", "ready": True},
    {"id": "grok", "kind": "openai_compat", "base_url": "http://keyproxy:3200/grok/v1", "via_proxy": True, "key_held": True, "key_kind": "cli_login", "ready": True},
    # OpenCode Go only: Zen, the other way of paying OpenCode, is the plan the page offers to add.
    {"id": "opencode", "kind": "opencode", "billing": "subscription", "base_url": "http://keyproxy:3200/opencode", "via_proxy": True, "key_held": False, "key_kind": "api_key", "ready": False},
    {"id": "deepseek", "kind": "deepseek", "base_url": "http://keyproxy:3200/deepseek", "via_proxy": True, "key_held": False, "key_kind": "api_key", "ready": False},
    {"id": "claude", "kind": "openai_compat", "base_url": "http://keyproxy:3200/claude/v1", "via_proxy": True, "key_held": False, "key_kind": "cli_login", "ready": False},
    {"id": "vllm", "kind": "vllm", "base_url": "", "via_proxy": False, "key_held": False, "key_kind": "endpoint", "ready": False},
]
ONBOARDING = {"has_model": True, "presets": 4, "default_preset": "deepseek-flash", "providers": PROVIDERS, "keyproxy_base": "http://keyproxy:3200", "needs": [], "message": ""}
FRESH = {"has_model": False, "presets": 0, "default_preset": "", "providers": PROVIDERS, "keyproxy_base": "http://keyproxy:3200", "needs": ["model"], "message": "No model is configured yet. Add one in the app: Settings \u2192 Models \u2192 Add a model."}


# The Health screen of this installation: mostly green, with the two things a real one tends to have
# outstanding, so the picture shows what a warning and a failure look like and what each asks for.
# The checks share api_stub's quiet answer, which several harnesses rely on staying quiet.
def _check(name: str, ok: bool, message: str, severity: str, fix: str = "") -> dict:
    return {"name": name, "ok": ok, "message": message, "severity": severity, "fix_hint": fix, "fixable": False, "fixed": False}


DOCTOR = {"checks": [
    _check("default model", True, "deepseek-flash = DeepSeek Flash", "ok"),
    _check("default provider key", True, "held by the key proxy", "ok"),
    _check("pricing for the default model", True, "known", "ok"),
    _check("vision preset", True, "claude-opus-5", "ok"),
    _check("fallback chain", True, "deepseek-flash, gpt-5.6-luna", "ok"),
    _check("exec sandbox", True, "bubblewrap, network filtered", "ok"),
    _check("per-run spend cap", True, "$2.00 per run, $20.00 per day", "ok"),
    _check("local speech", True, "Nemotron 3.5 Streaming 0.6B active (streaming)", "ok"),
    _check("disk free", True, "41.2 GB free", "ok"),
    _check("workspace size", False, "11.8 GB in /srv/workspaces", "warn", "close finished sessions with 'delete the agent + workspace', or /cleanup"),
    _check("secrets dir", True, "private", "ok"),
    _check("boot health", True, "this boot was clean", "ok"),
], "summary": {"ok": 11, "warn": 1, "fail": 0, "fixed": 0}}
INTEGRATIONS = {"rows": [
    {"kind": "github", "name": "GitHub", "state": "authenticated", "severity": "ok", "detail": ""},
    {"kind": "mcp", "name": "calendar", "state": "connected", "severity": "ok", "detail": "4 tools"},
    {"kind": "mcp", "name": "notes", "state": "auth_required", "severity": "fail", "detail": "authorization required: the refresh token was revoked"},
    {"kind": "provider", "name": "deepseek", "state": "ready", "severity": "ok", "detail": "deepseek, key: proxy"},
    {"kind": "provider", "name": "openrouter", "state": "rate_limited", "severity": "warn", "detail": "HTTP 429 on the last call"},
]}


def catalogue_entry(id_: str, name: str, context: int, *, images: bool, reasoning: bool, price_in: float, price_out: float) -> dict:
    return {
        "id": id_,
        "name": name,
        "context_length": context,
        "max_output_tokens": 32000,
        "input_modalities": ["text", "image"] if images else ["text"],
        "images": images,
        "reasoning": reasoning,
        "pricing": {"input": price_in, "output": price_out, "cache_hit": round(price_in / 10, 4)},
    }


CATALOGUE = [
    catalogue_entry("anthropic/claude-opus-5", "Claude Opus 5", 1000000, images=True, reasoning=True, price_in=5.0, price_out=25.0),
    catalogue_entry("deepseek/deepseek-v4-flash", "DeepSeek V4 Flash", 128000, images=True, reasoning=True, price_in=0.28, price_out=0.42),
    catalogue_entry("openai/gpt-5.6-luna", "GPT-5.6 Luna", 400000, images=True, reasoning=True, price_in=0.4, price_out=1.8),
    catalogue_entry("qwen/qwen3.8-max", "Qwen3.8 Max", 256000, images=False, reasoning=True, price_in=2.0, price_out=6.0),
    catalogue_entry("moonshot/kimi-k3", "Kimi K3", 256000, images=False, reasoning=True, price_in=3.0, price_out=15.0),
    catalogue_entry("z-ai/glm-5.3-flash", "GLM-5.3 Flash", 200000, images=False, reasoning=True, price_in=0.15, price_out=0.5),
]


# The installation in the pictures develops itself on a server: it has both checkouts, a token and a
# rebuilder, so Changes is in the nav and carries no qualifying tag.
CAPABILITIES = {"selfdev": {"mode": "server", "configured": "auto", "reasons": ["checkouts: both writable", "token: configured", "remotes: both have an origin", "rebuild: the rebuilder service"], "missing": [], "tools": ["SelfPropose", "SelfRebuild", "SelfRollback", "SelfWorkspace"]}, "restart_required": None, "last_change": None}
CAPABILITIES["components"] = {"mode": "native", "missing": ["browser", "node", "bwrap"], "needed": [], "installing": ""}


def _component(cid, state, detail_key, detail_args=None, **over):  # type: ignore[no-untyped-def]
    """One card on Settings -> Components, as the registry would have measured it.

    The sentence comes as a key and its numbers, the way the registry sends it: the Russian set of
    these pictures is a check that the page is Russian all the way down, and a stub that sent English
    prose would photograph a page the app does not produce.
    """
    return {
        "id": cid, "state": state, "detail": "", "detail_key": detail_key, "detail_args": detail_args or {},
        "installable": False, "how": "none", "fix": "",
        "download_bytes": 0, "disk_bytes": 0, "requires_restart": False,
        "installed_count": 0, "total_count": 0, "skills": [], "enables": [], "progress": None, **over,
    }


# A portable installation part-way through being fitted out: the speech runtime and its models are in,
# the browser is going in while the picture is taken, and Node is waiting to be asked for. That is the
# state the page exists for, so it is the state it is photographed in.
COMPONENTS = {
    "mode": "native",
    "launcher": True,
    "disk_bytes": 214_577_296,
    "missing": ["browser", "node", "bwrap"],
    "busy": "browser",
    "components": [
        _component("speech", "installed", "comp.detail.speech.ok", download_bytes=15_728_640, enables=["stt", "tts", "voicenotes"]),
        _component("stt-models", "installed", "comp.detail.models.some", {"n": "1", "total": "12"}, how="models", disk_bytes=178_000_000, installed_count=1, total_count=12, enables=["stt"]),
        _component("tts-voices", "installed", "comp.detail.models.some", {"n": "2", "total": "16"}, how="models", disk_bytes=36_577_296, installed_count=2, total_count=16, enables=["tts"]),
        _component("browser", "installing", "comp.detail.browser.missing",
                   installable=True, how="launcher", download_bytes=104_857_600, requires_restart=True,
                   enables=["skills.browser", "screenshots"], skills=["accessibility", "canvas-design", "web-design-reviewer", "webapp-testing"],
                   progress={"id": "browser", "state": "running", "step": "installing browser", "step_key": "", "error": "", "restart_required": True}),
        _component("node", "missing", "comp.detail.node.missing",
                   installable=True, how="launcher", download_bytes=60_817_408, requires_restart=True,
                   enables=["skills.node", "npx"], skills=["accessibility", "web-artifacts-builder", "webapp-testing"]),
        _component("git", "installed", "comp.detail.binary.ok", {"binary": "git", "path": "/usr/bin/git"}, enables=["selfdev", "projects"]),
        _component("ripgrep", "installed", "comp.detail.binary.ok", {"binary": "rg", "path": "/usr/bin/rg"}, enables=["search"]),
        _component("opus", "missing", "comp.detail.binary.nopath", {"binary": "opusdec"}, fix="install opus-tools with this machine's package manager", enables=["voicenotes"]),
        _component("bwrap", "unavailable", "comp.detail.bwrap.refused",
                   fix="allow unprivileged user namespaces on this machine", enables=["sandbox"]),
    ],
}


# ---- the stub API -------------------------------------------------------------------------


TEAM = _team()
# The team page is a page of the project's focus mode, whose column also reads the project's board.
TEAM_BOARD = BoardStub(next(p for p in PROJECTS if p["id"] == P1))


def respond(route, body, *, content_type: str = "application/json", status: int = 200) -> None:  # type: ignore[no-untyped-def]
    route.fulfill(status=status, content_type=content_type, body=body if isinstance(body, (bytes, str)) else json.dumps(body))


def stub(route) -> None:  # type: ignore[no-untyped-def]
    request = route.request
    url = request.url
    path = url.split("?", 1)[0]
    rel = path[path.index("/api/") :]
    params = parse_qs(urlsplit(url).query)
    if rel == "/api/pet/react" and request.method == "POST":
        return respond(route, {"line": "I'm here.", "emotion": "joy", "action": "wave", "prop": ""})
    search = file_search(rel, params.get("q", [""])[0])
    if search is not None:
        return respond(route, search)
    if rel == "/api/providers/lookup-models":
        return respond(route, {"base_url": "http://keyproxy:3200/openrouter/v1", "models": [e["id"] for e in CATALOGUE], "entries": CATALOGUE})
    if rel == "/api/voice/tts":
        return respond(route, SILENCE, content_type="audio/wav")
    if rel.startswith("/api/harnesses"):
        posted = json.loads(request.post_data or "{}") if request.method in ("POST", "PUT") and request.post_data else None
        harnesses = HARNESSES.answer(request.method, rel, urlsplit(url).query, posted)
        if harnesses is not None:
            return respond(route, harnesses[1], status=harnesses[0])
    # The installation-wide Board lists BOARD below. The team's board also answers GET /api/board, and
    # asked first it answered with its own empty list, so the Board picture came out with no cards.
    whole_board = rel == "/api/board" and request.method == "GET"
    team = TEAM.answer(request.method, rel, urlsplit(url).query, None) or (None if whole_board else TEAM_BOARD.answer(request.method, rel, urlsplit(url).query, None))
    if team is not None:
        return respond(route, team[1], status=team[0])
    if request.method == "POST" and rel.startswith("/api/notifications/") and rel.endswith("/act"):
        entry = next((e for e in INBOX if e["id"] == int(rel.split("/")[3])), None)
        action = json.loads(request.post_data or "{}").get("action", "")
        resolution = "answered" if action.startswith("answer") else action
        return respond(route, {"resolution": resolution, "notification": entry and {**entry, "resolved": resolution, "needs_you": False, "seen": True}})
    if request.method == "POST" and rel == "/api/notifications/seen":
        return respond(route, {"marked": 0, "summary": INBOX_SUMMARY})
    if request.method != "GET":
        return respond(route, {"ok": True})
    if rel == "/api/events" and getattr(stub, "events", ""):
        # A finite body: the page reads it, reconnects a second later and is handed the same frames,
        # which it recognises by their sequence numbers and does not raise twice.
        return respond(route, stub.events, content_type="text/event-stream")  # type: ignore[attr-defined]
    if rel == "/api/stt/progress":
        # The voice page opens this to hear the engine finish loading; the picker opens it for the
        # download bars. Neither needs anything to happen here — what is true now is on /api/voice.
        over = getattr(stub, "voice_over", None) or {}
        load = {"kind": "engine", "state": over.get("state", VOICE["stt"]["state"]), "model": VOICE["stt"]["local"]["model"], "loaded_in_ms": over.get("loaded_in_ms", VOICE["stt"]["loaded_in_ms"]), "error": ""}
        return respond(route, f"data: {json.dumps(load)}\n\n", content_type="text/event-stream")
    if rel == "/api/tts/progress":
        # The voice page opens this to hear the voice finish being built; the picker opens it for the
        # download bars. The same two-source shape the recognition stream has.
        over = getattr(stub, "voice_tts", None) or {}
        load = {"kind": "engine", "state": over.get("state", VOICE["tts"]["state"]), "voice": "ru-dmitri", "loaded_in_ms": over.get("loaded_in_ms", VOICE["tts"]["loaded_in_ms"]), "error": ""}
        return respond(route, f"data: {json.dumps(load)}\n\n", content_type="text/event-stream")
    if rel == "/api/voice/stream":
        # The concierge's half of the conversation, canned: the page has no other way into its
        # thinking, delegating and speaking states, and those are three of the five worth a picture.
        return respond(route, "event: hello\ndata: {}\n\n" + getattr(stub, "voice_frames", ""), content_type="text/event-stream")
    if rel.endswith("/stream"):
        return respond(route, "event: hello\ndata: {}\n\n", content_type="text/event-stream")
    if rel == "/api/auth/me":
        if getattr(stub, "signedout", False):
            return respond(route, {"detail": "not signed in"}, status=401)
        return respond(route, {"user_id": 1, "via": "token"})
    if rel == "/api/auth/config":
        return respond(route, {"telegram": None, "passkeys": 1, "pairing": True})
    if rel == "/api/sessions":
        return respond(route, listing())
    if rel == "/api/doctor":
        return respond(route, DOCTOR)
    if rel == "/api/integrations/health":
        return respond(route, INTEGRATIONS)
    if rel == "/api/services":
        return respond(route, SERVICES_ALL)
    if rel.startswith("/api/sessions/"):
        parts = rel.split("/")
        sid = parts[3]
        tail = "/".join(parts[4:])
        if tail.startswith("folders/"):
            # Another folder of the session's project answers under /folders/{id} with the same routes.
            tail = "/".join(tail.split("/")[2:])
        if tail == "":
            return respond(route, detail(sid))
        if tail == "files":
            return respond(route, {"path": params.get("path", [""])[0], "kind": "dir", "entries": FILES[""] + [{"name": "src", "dir": True, "size": 0, "mtime": 0}, {"name": ".env", "dir": False, "size": 24, "mtime": 0}, {"name": "node_modules", "dir": True, "size": 0, "mtime": 0}] if not params.get("path", [""])[0] else file_entries(params["path"][0])})
        if tail == "download":
            p = params.get("path", [""])[0]
            if p in FILE_TEXT:
                return respond(route, FILE_TEXT[p], content_type="text/plain")
            if p.endswith(".png"):
                return respond(route, PHONE_PNG, content_type="image/png")
            return respond(route, NOTES_MD if "NOTES" in p else REPORT_MD, content_type="text/markdown")
        if tail.startswith("sent/") and tail.endswith("download"):
            return respond(route, REPORT_MD, content_type="text/markdown")
        if tail == "tools/timing":
            return respond(route, {"items": [{"name": "Exec", "calls": 9, "errors": 0, "total_ms": 21000, "mean_ms": 2333}, {"name": "Read", "calls": 14, "errors": 0, "total_ms": 900, "mean_ms": 64}]})
        if tail == "steer":
            # What the host holds for the running agent: empty unless a scene puts a written-during-
            # the-run message there, which is the only way the card with its Steer button is drawn.
            return respond(route, getattr(stub, "queued", []) if sid == S1 and getattr(stub, "running", False) else [])
        if tail == "checkpoints":
            return respond(route, {"checkpoints": [], "total": 0, "pruned": False, "pruned_before": None, "removed": 0, "note": "", "keep_days": 30, "keep_last": 50})
        if tail == "mcp":
            return respond(route, {"enabled": [], "servers": []})
        if tail == "tools":
            return respond(route, [])
        if tail.startswith("tool-groups"):
            return respond(route, session_tool_groups())
        if tail.startswith("tool-results/") and fulfil_shared(route):
            return None
        return respond(route, {})
    if rel.startswith("/api/usage/provider/"):
        return respond(route, PROVIDER_USAGE)
    if rel == "/api/usage":
        return respond(route, usage_data())
    if rel == "/api/balance":
        return respond(route, {"balances": {"deepseek": 4.62, "openrouter": 11.08}, "thresholds": [5, 2, 0.5]})
    if rel == "/api/memory":
        return respond(route, MEMORY)
    if rel == "/api/board":
        return respond(route, BOARD)
    if rel == "/api/notifications/summary":
        return respond(route, INBOX_SUMMARY)
    if rel == "/api/notifications":
        return respond(route, {"entries": notifications_view(params.get("view", ["all"])[0]), "next_before": None, "summary": INBOX_SUMMARY})
    if rel == "/api/proposals":
        return respond(route, PROPOSALS)
    if rel == "/api/schedules":
        return respond(route, SCHEDULES)
    if rel == "/api/recurring/proposals":
        return respond(route, {"entries": [], "collection_revisions": {"global:global": 1}})
    if rel == "/api/projects":
        return respond(route, PROJECTS)
    if rel == "/api/settings":
        return respond(route, SETTINGS)
    if rel == "/api/limits/spend":
        # Settings → Limits reads it for the spend beside each cap; without it the whole section fails to draw.
        return respond(route, {"since": "", "total": {"spent_usd": 3.42, "unmetered": 2, "cap_usd": 0, "reserved_usd": 1.12, "uncertain_usd": 0.35, "reserved_count": 3, "uncertain_count": 1}, "per_provider": {}})
    if rel == "/api/onboarding":
        return respond(route, FRESH if getattr(stub, "fresh", False) else ONBOARDING)
    if rel == "/api/modes":
        return respond(route, GATES["/api/modes"])
    if rel == "/api/commands":
        return respond(route, [])
    if rel == "/api/skills":
        return respond(route, GATES["/api/skills"])
    if rel == "/api/voice":
        over = getattr(stub, "voice_over", None)
        spoken = getattr(stub, "voice_tts", None)
        body = dict(VOICE)
        if over:
            body["stt"] = {**VOICE["stt"], **over}
        if spoken:
            body["tts"] = {**VOICE["tts"], **spoken}
        return respond(route, body)
    if rel == "/api/tts":
        return respond(route, TTS)
    if rel == "/api/stt":
        return respond(route, STT)
    if rel == "/api/components":
        # The state the owner reported: a portable build with no speech runtime, where the voice
        # pages say the browser is doing the listening and offer the download that changes it.
        if getattr(stub, "nospeech", False):
            missing = dict(COMPONENTS["components"][0], state="missing", installable=True, how="extra",
                           detail_key="comp.detail.speech.missing")
            return respond(route, {**COMPONENTS, "components": [missing, *COMPONENTS["components"][1:]], "busy": ""})
        return respond(route, COMPONENTS)
    if rel == "/api/components/stream":
        # Nothing moves while a picture is taken; the card carries its own frame in the view above.
        return route.fulfill(status=200, content_type="text/event-stream", body=": keepalive\n\n")
    if rel.endswith("/progress"):
        # The two pickers subscribe to a download stream as they mount. Nothing is downloading in a
        # picture, so this is an empty stream rather than an unhandled route the reporter complains
        # about; an empty body would leave the reader in its retry loop for the whole shot.
        return route.fulfill(status=200, content_type="text/event-stream", body=": keepalive\n\n")
    if rel == "/api/asr":
        return respond(route, {"configured": False, "reason": "", "provider": "", "model": "", "max_seconds": 120, "autosend": False})
    if rel == "/api/heartbeat":
        return respond(route, {"enabled": True, "armed": True, "interval_minutes": 60, "active_hours": "08:00-23:00", "preset": "", "max_runs_per_day": 24, "last_run": ago(minutes=35), "runs_today": 9, "session_id": None, "running": False, "file": "/srv/state/HEARTBEAT.md", "text": "Check the services and the board; write to the inbox only when something needs me."})
    if rel == "/api/capabilities":
        return respond(route, CAPABILITIES)
    if rel == "/api/status":
        return respond(route, {"ok": True})
    if rel == "/api/push/subscriptions":
        return respond(route, {"subscriptions": PUSH_DEVICES})
    if rel == "/api/notifications/preferences":
        # Quiet at night, pushes for finished work only when urgent, and one project muted until the morning.
        view = notification_preferences(quiet_hours="23:00-07:30", muted_projects={P2: ahead(hours=10)})
        view["preferences"]["matrix"]["run_finished"]["push"] = "urgent"  # type: ignore[index]
        return respond(route, view)
    if fulfil_shared(route):
        return None
    # A route nobody taught this stub about is answered with nothing and reported at the end: the
    # app grows gates (a model, the capabilities) that decide whether a screen is drawn at all, and
    # one harness knowing about them while another does not is how the pictures and the numbers
    # come to describe different apps.
    UNHANDLED.record(rel)
    return respond(route, [])


def _tts_voice(vid, label, language, gender, size, disk, quality, speed, rtf, note, installed=False, selected=False, speakers=(), recommended=()):  # type: ignore[no-untyped-def]
    return {
        "id": vid, "label": label, "kind": "vits", "language": language, "languages": [], "new": False, "gender": gender,
        "size_bytes": size, "disk_bytes": disk, "memory_mb": 120, "sample_rate": 22050,
        "licence": "CC0 (public domain)", "quality": quality, "speed": speed, "rtf": rtf,
        "keeps_up": rtf < 1.0, "note": note, "speakers": list(speakers),
        "recommended_for": list(recommended), "installed": installed, "installed_bytes": disk if installed else 0,
        "selected": selected,
    }


TTS = {
    "models": [
        dict(_tts_voice("multi-supertonic", "Supertonic 3 (Russian and 30 more, 10 styles)", "ru", "mixed",
                        128_774_318, 145_316_356, 90, 61, 0.175,
                        "The best Russian here, and the fastest: forty-four kilohertz, ten voices in one download.",
                        speakers=tuple(f"Style {n}" for n in range(1, 11)), recommended=("ru",)),
             kind="supertonic", sample_rate=44100, memory_mb=256, new=True,
             languages=["ru", "en", "de", "es", "fr", "it", "pl", "pt", "uk"],
             licence="OpenRAIL-M (use restrictions apply)"),
        _tts_voice("ru-dmitri", "Dmitri (Russian)", "ru", "male", 21_129_441, 36_577_368, 74, 53, 0.219,
                   "A clear male Russian, four times faster than speech.", installed=True, selected=True),
        _tts_voice("ru-irina", "Irina (Russian)", "ru", "female", 21_149_417, 36_577_296, 73, 39, 0.337,
                   "The female Russian voice. Warm and unhurried.", installed=True),
        _tts_voice("en-amy", "Amy (American English)", "en", "female", 21_028_122, 36_679_476, 72, 57, 0.195,
                   "Twenty megabytes, five times faster than speech.", recommended=("en",)),
        dict(_tts_voice("en-kokoro", "Kokoro (English, 11 voices)", "en", "mixed", 319_625_534, 369_315_617, 92, 21, 0.692,
                        "The most natural English here, and the largest download.",
                        speakers=("af", "af_bella", "am_adam", "bf_emma", "bm_george")),
             kind="kokoro", sample_rate=24000, licence="Apache-2.0", memory_mb=620),
        _tts_voice("de-thorsten", "Thorsten (German)", "de", "male", 20_949_833, 36_577_367, 76, 60, 0.183,
                   "The German voice most German projects use.", recommended=("de",)),
        _tts_voice("fr-siwis", "Siwis (French)", "fr", "female", 20_914_888, 36_577_449, 73, 59, 0.189,
                   "A steady French, quick enough that nothing waits for it.", recommended=("fr",)),
    ],
    "languages": ["de", "en", "es", "fr", "it", "pl", "pt", "ru", "uk"],
    "selected": "ru-dmitri",
    "disk_bytes": 73_154_664,
    "root": "/srv/state/models/tts",
    "engine_installed": True,
    "recommended": {"de": "de-thorsten", "en": "en-amy", "fr": "fr-siwis", "ru": "multi-supertonic"},
    "state": {
        "voice": "ru-dmitri", "label": "Dmitri (Russian)", "language": "ru", "speaker": "", "speed": 1.0,
        "threads": 2, "installed": True, "active": True, "engine_installed": True, "state": "ready",
        "error": "", "loaded": "ru-dmitri", "encoder": True,
    },
}

STT = {
    "models": [{
        "id": "gigaam-ru", "label": "GigaAM v3 Russian", "kind": "nemo_transducer", "streaming": False,
        "languages": ["ru"], "language_count": 1, "size_bytes": 170_197_019, "disk_bytes": 178_000_000,
        "memory_mb": 380, "licence": "MIT", "accuracy": 91, "speed": 94,
        "note": "The best Russian here, and it writes the punctuation itself.", "url": "",
        "recommended_for": ["ru"], "verified": True, "detects_language": True,
        "installed": True, "installed_bytes": 178_000_000, "selected": True,
    }],
    "languages": ["en", "ru"], "selected": "gigaam-ru", "disk_bytes": 178_000_000,
    "root": "/srv/state/models/stt", "language": "auto", "threads": 2, "engine_installed": True,
    "decoders": {"opus": True, "any": False}, "recommended": {"ru": "gigaam-ru"},
}


UNHANDLED = Unhandled()
HARNESSES = HarnessesStub()

# ---- the shots ----------------------------------------------------------------------------

DESK = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}


# The handful of words these helpers click on, in the language the run is in. Everything else is
# picked by class or by data, which no translation moves.
WORDS = {
    "en": {"steps": "8 steps", "panel": "Panel", "access": "Access", "actions": "Session actions", "details": "Details", "role": "Writes the delivery page",
           "permission": "waiting for permission · 1 min", "answer": "Answer", "checkout": "Checkout page", "q.note": "like last spring",
           "queued": "Then put the opening hours from the shop calendar in the footer.", "steered": "Keep the old prices on the archive page."},
    "ru": {"steps": "8 шагов", "panel": "Панель", "access": "Доступ", "actions": "Действия с сессией", "details": "Сведения", "role": "Пишет страницу доставки",
           "permission": "ждёт разрешения · 1 мин", "answer": "Ответить", "checkout": "Оформление заказа", "q.note": "как прошлой весной",
           "queued": "Потом добавь в подвал часы работы из календаря магазина.", "steered": "На странице архива оставь старые цены."},
}


def word(key: str) -> str:
    return WORDS[LANG][key]


def shot(page: Page, name: str, route: str, *, wait: str = ".screen, .chat, .ph-page", settle: int = 900, before=None, full: bool = False) -> None:  # type: ignore[no-untyped-def]
    page.goto(f"{BASE}/{route}{'&' if '?' in route else '?'}token=t&scheme=dark&lang={LANG}")
    page.wait_for_selector(wait, timeout=15000)
    if before:
        before(page)
    page.wait_for_timeout(settle)
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=full)
    print("wrote", name)


def expand_steps(page: Page) -> None:
    page.get_by_text(word("steps")).first.click()
    page.wait_for_timeout(600)
    # Scroll like a hand does: the timeline follows the newest content only until the reader scrolls.
    box = page.locator(".chat-scroll").bounding_box()
    assert box
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, -4000)
    page.wait_for_timeout(400)


def open_panel_files(page: Page) -> None:
    """The panel on its Files tab: the workspace browser beside the conversation."""
    page.locator(".panel-tab[data-tab='files']").click()
    page.wait_for_selector(".panel-files .filerow", timeout=5000)


def open_panel_preview(page: Page) -> None:
    """A file opened from Files: the panel switches to Preview, with the breadcrumb and the history buttons."""
    open_panel_files(page)
    page.locator(".panel-files .filerow .title", has_text="NOTES.md").click()
    page.wait_for_selector(".panel-body.tab-preview .preview-doc", timeout=5000)


def phone_tool(page: Page, tile: str) -> None:
    """On a phone the session's tools are tiles in the ⋮ sheet of its bar: open it and take one."""
    page.locator(".ph-chat-top button[aria-haspopup='menu']").first.click()
    page.locator(f".ph-session-menu [data-tile='{tile}']").click()


def open_phone_panel(page: Page) -> None:
    """On a phone the tabs open as a full sheet from the session's action menu."""
    phone_tool(page, "details")
    page.wait_for_selector(".panel-sheet .panel-tab", timeout=5000)
    page.locator(".panel-sheet .panel-tab[data-tab='files']").click()
    page.wait_for_selector(".panel-sheet .filerow", timeout=5000)


def open_tool_groups(page: Page) -> None:
    """The session's Details on its tool groups: which the agent has loaded, which wait on demand."""
    page.locator(".panel-tab[data-tab='details']").first.click()
    page.wait_for_selector(".stgroups .stgroup", timeout=5000)
    page.locator(".stgroups").first.scroll_into_view_if_needed()
    page.wait_for_timeout(300)


def open_phone_tool_groups(page: Page) -> None:
    phone_tool(page, "details")
    page.wait_for_selector(".panel-sheet .stgroups .stgroup", timeout=5000)
    page.locator(".panel-sheet .stgroups").first.scroll_into_view_if_needed()
    page.wait_for_timeout(300)


def open_share(page: Page) -> None:
    page.locator(".service-row").first.locator("button[aria-haspopup='menu']").click()
    page.wait_for_selector(".menu[role='menu']", timeout=5000)
    page.locator(".menu[role='menu'] button", has_text=word("access")).first.click()
    page.wait_for_selector(".access-options", timeout=5000)


def pick_a_model(page: Page) -> None:
    """Walk the flow far enough that the picture shows all three steps with something in them."""
    page.locator(".pickgrid .pick", has_text="OpenRouter").first.click()
    page.wait_for_selector(".modelgrid .pick", timeout=15000)
    page.locator(".modelgrid .pick", has_text="Claude Opus 5").first.click()
    # Picking a model scrolls the page down to how it runs, which left the endpoints out of the
    # picture: the two ways of paying OpenCode side by side are what this page has to show first.
    page.wait_for_timeout(400)
    # A phone's column holds the endpoints one under another, so there it starts at OpenCode Go and
    # Zen is still above the bar at the bottom.
    if (page.viewport_size or {}).get("width", 0) < 600:
        page.locator(".pickgrid .pick", has_text="OpenCode Go").first.evaluate("e => e.scrollIntoView({block: 'start'})")
    else:
        page.evaluate("document.querySelector('.addmodel .step-head').scrollIntoView({block: 'start'})")


def open_drawer(page: Page) -> None:
    """The phone's drawer: the two modes as a switch, the destinations, the projects and the recents."""
    page.locator(".ph-menu:visible").first.click()
    page.wait_for_selector(".ph-drawer-root.open .ph-nrow", timeout=5000)
    page.wait_for_timeout(400)


def open_more(page: Page) -> None:
    """The drawer's More sheet on a phone: every other destination, and the language switch under them."""
    open_drawer(page)
    page.locator(".ph-drawer-root.open [data-nav='more']").click()
    page.wait_for_selector(".ph-more-sheet", timeout=5000)
    page.wait_for_timeout(300)


def scroll_to_voices(page: Page) -> None:
    """The settings body scrolls inside itself, so a full-page shot would still show only the top of it.

    The synthesis picker is the last card in the Voice section; bringing it into view is what makes
    the picture about the voices rather than about the recogniser above them.
    """
    page.locator(".stt-list").last.scroll_into_view_if_needed()
    page.wait_for_timeout(400)


def open_projects(page: Page) -> None:
    """The switcher over a list already grouped by project: the folders on one side, the agents in them on the other."""
    # A picture before this one folds the sidebar, and folded there is only the rail: unfold it first.
    if not page.locator("nav.sidebar").count():
        page.locator(".rail .rail-home.folded").click()
    page.locator(".sidebar .project-chip").click()
    page.wait_for_selector(".project-row", timeout=5000)


def open_hire(page: Page) -> None:
    # The team's header also links to the board; hiring is its primary action.
    page.locator(".pagehead-actions .iconbtn.primary").click()
    page.wait_for_selector(".staff-sheet .executor")
    page.locator("#staff-name").fill("Mira")
    page.locator("#staff-role").fill(word("role"))


def open_menu(page: Page) -> None:
    """The menu from the rail's foot: every destination with its count, the language, Settings — over the conversation."""
    page.locator(".rail [data-rail='menu']").click()
    page.wait_for_selector(".navmenu[role='menu']", timeout=5000)


def fold_sidebar(page: Page) -> None:
    """The sidebar folded away: the rail alone beside the conversation, its Home turned into the way to unfold."""
    page.keyboard.press("Control+\\")
    page.wait_for_timeout(400)


# ---- the voice page in each of its states --------------------------------------------------
#
# Five states, three widths. None of them can be reached by clicking: the page's phase comes from the
# concierge's event stream, from the engine loading a model into memory, and from a microphone. So the
# stream is canned above, the engine state is set on /api/voice, and the microphone is a fake capture
# device the browser is launched with — which also gives the orb a real level to react to.


def sse(*frames: tuple[str, dict]) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(body)}\n\n" for name, body in frames)


VOICE_STATES: dict[str, dict] = {
    "ready": {"over": {}, "frames": "", "mic": False, "ask": False},
    "loading": {"over": {"state": "loading", "loaded_in_ms": 0}, "frames": "", "mic": False, "ask": False},
    # The other engine loading: the recogniser is ready and listening, and the voice the answer will
    # be read in is still being built, which the page says rather than leaving the operator to find
    # out by not hearing anything.
    "warming": {"over": {}, "tts": {"state": "loading", "loaded_in_ms": 0}, "frames": "", "mic": False, "ask": False},
    "listening": {"over": {}, "frames": "", "mic": True, "ask": False},
    "thinking": {
        "over": {},
        "frames": sse(("status", {"state": "thinking"}), ("partial", {"text": "Looking at the board and the two invoices that came back"})),
        "mic": True,
        "ask": True,
    },
    "delegating": {
        "over": {},
        "frames": sse(("status", {"state": "delegating", "title": "Invoice run"}), ("partial", {"text": "Handing the eleven invoices to an agent of its own"})),
        "mic": True,
        "ask": True,
    },
    "speaking": {
        "over": {},
        "frames": sse(
            ("status", {"state": "idle"}),
            ("say", {"text": "All eleven invoices went out this morning."}),
            ("say", {"text": "Two came back with the wrong VAT line, and I have put both on the board."}),
            ("say", {"text": "Shall I have someone redo them now?"}),
        ),
        "mic": True,
        "ask": True,
    },
}

VOICE_ASKED = "How did the invoice run go?"

# The microphone in the middle of the voice page is the mascot unless the operator switched it to the
# orb, and the pictures are of the page as it opens, so the tap goes to whichever of the two is there.
VOICE_MIC = ".voice-mascot, .voice-orb"


def listening(page: Page) -> None:
    """Open the microphone and say nothing: the state the page spends most of its time in."""
    page.locator(VOICE_MIC).click()
    page.wait_for_timeout(900)


def talking(page: Page) -> None:
    """Open the microphone and ask something, the way a hand does.

    The wait in the middle is not padding. The canned event stream is a finite body, so the page
    reads it, reaches the end and reconnects a second later — and asking something clears the answer
    on the screen. Asking after the first delivery and shooting before the third is what leaves
    exactly one answer under exactly one question.
    """
    page.locator(VOICE_MIC).click()
    page.wait_for_timeout(1200)
    page.fill(".voice-compose .field", VOICE_ASKED)
    page.locator(".voice-compose button[type=submit]").click()


def reading(page: Page) -> None:
    """Ask something, then open the transcript of this very conversation while it is being answered.

    The button is found by its place rather than by its word: the page is photographed in two
    languages and the first action in the header is the transcript in both of them.
    """
    talking(page)
    page.wait_for_timeout(900)
    page.locator(".pagehead-actions .btn.ghost").first.click()
    page.wait_for_selector(".voice-read .chat-scroll", timeout=15000)


def reading_agent(page: Page) -> None:
    """The same, for one of the agents the concierge started: its transcript, the conversation kept."""
    talking(page)
    page.wait_for_timeout(900)
    page.locator(".voice-agent").first.click()
    page.wait_for_selector(".voice-read .chat-scroll", timeout=15000)


# The two views that are not a state of the orb but a change of what is in the middle of the page.
# Both are taken mid-answer, because the whole point of them is that the answer is still being read.
VOICE_VIEWS = {"transcript": reading, "agent": reading_agent}


def voice_shots(page: Page, prefix: str) -> None:
    for name, plan in VOICE_STATES.items():
        stub.voice_over = plan["over"]  # type: ignore[attr-defined]
        stub.voice_tts = plan.get("tts")  # type: ignore[attr-defined]
        stub.voice_frames = plan["frames"]  # type: ignore[attr-defined]
        before = talking if plan["ask"] else listening if plan["mic"] else None
        shot(page, f"{prefix}{name}", "voice", settle=1000, before=before)
    stub.voice_over = {}  # type: ignore[attr-defined]
    stub.voice_tts = None  # type: ignore[attr-defined]
    stub.voice_frames = VOICE_STATES["speaking"]["frames"]  # type: ignore[attr-defined]
    for name, before in VOICE_VIEWS.items():
        shot(page, f"{prefix}{name}", "voice", settle=1200, before=before)
    stub.voice_over = None  # type: ignore[attr-defined]
    stub.voice_tts = None  # type: ignore[attr-defined]
    stub.voice_frames = ""  # type: ignore[attr-defined]


FAKE_MEDIA = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"]


def run_voice() -> int:
    """Only the voice page, in every state, at the three widths that have to hold it."""
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=FAKE_MEDIA)
        for prefix, viewport, mobile in (
            ("voice-", {"width": 1440, "height": 900}, False),
            ("voice-wide-", {"width": 2560, "height": 1300}, False),
            ("voice-phone-", {"width": 390, "height": 844}, True),
        ):
            context = browser.new_context(viewport=viewport, color_scheme="dark", is_mobile=mobile, has_touch=mobile, permissions=["microphone"])
            page = context.new_page()
            page.route("**/api/**", stub)
            voice_shots(page, prefix)
            context.close()
        browser.close()
    return UNHANDLED.report()


def agents_shots() -> int:
    """The Agents screen at the three widths, in both of the two states a folder has.

    Collapsed is how the screen opens: the folders of the projects, the concierge's first with its
    mic, and the free agents under them. Expanded is every folder open, which is the case the reader
    asks for one folder at a time and the picture shows all at once.
    """
    open_keys = [p["id"] for p in PROJECTS] + ["\u0000free"]
    out = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for prefix, viewport, mobile in (
            ("agents-phone-", PHONE, True),
            ("agents-", DESK, False),
            ("agents-wide-", {"width": 2560, "height": 1300}, False),
        ):
            for state in ("collapsed", "expanded"):
                context = browser.new_context(viewport=viewport, device_scale_factor=2 if not mobile else 3, color_scheme="dark", is_mobile=mobile, has_touch=mobile)
                script = "" if state == "collapsed" else " ".join(f"localStorage.setItem('daedalus.folder.{k}', '1');" for k in open_keys)
                context.add_init_script("try { " + script + " } catch (e) {}")
                page = context.new_page()
                page.route("**/api/**", stub)
                # A phone's folders are the Chats page's project sections (open unless closed by hand).
                if mobile:
                    shot(page, f"{prefix}{state}", "agents?view=chats", wait=".ph-row")
                else:
                    shot(page, f"{prefix}{state}", "agents", wait=".folder")
                context.close()
        browser.close()
    return out or UNHANDLED.report()


def workspace_shots(page: Page) -> None:
    """The tree and rich documents, all from the small file fixtures shared with the checks."""
    from urllib.parse import quote
    for name, path, selector in [
        ("session-preview-code", "src/main.py", ".source-view"),
        ("session-preview-html", "site/index.html", ".html-frame"),
        ("session-preview-json", "data/sample.json", ".json-tree"),
        ("session-preview-diff", "change.diff", ".diff-view"),
    ]:
        shot(page, name, f"agents/{S1}?panel=preview&path={quote(path)}", wait=selector, settle=500)

    def open_code_tree(p: Page) -> None:
        p.locator('.explorer-tree [data-path="src"]').click()
        p.locator('.explorer-tree [data-path="data"]').click()
        p.locator('.explorer-tree [data-path="src/main.py"] .title').click()
        p.wait_for_selector(".panel-body.split .source-view")

    page.set_viewport_size({"width": 2560, "height": 1400})
    shot(page, "session-explorer-wide", f"agents/{S1}?panel=files", wait=".explorer-tree [data-path='src']", before=open_code_tree)
    page.set_viewport_size(DESK)


def run_workspace() -> int:
    """Retake the conversation and file surfaces without changing unrelated screenshots."""
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
        desk.add_init_script("try { localStorage.setItem('daedalus.session.panel', 'details'); localStorage.setItem('agents.groupBy', 'workspace'); } catch (e) {}")
        page = desk.new_page()
        page.route("**/api/**", stub)
        shot(page, "session", f"agents/{S1}", wait=".chat-scroll .timeline", before=expand_steps, settle=300)
        shot(page, "session-panel-files", f"agents/{S1}", before=open_panel_files)
        shot(page, "session-panel-preview", f"agents/{S1}", before=open_panel_preview)
        workspace_shots(page)
        shot(page, "session-menu", f"agents/{S1}", before=open_menu)
        shot(page, "session-folded", f"agents/{S1}", before=fold_sidebar)
        shot(page, "dual", f"agents/{S1}?with={S2}", settle=1500)
        shot(page, "session-share", f"agents/{S1}", before=open_share)
        desk.close()
        phone = browser.new_context(viewport=PHONE, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
        page = phone.new_page()
        page.route("**/api/**", stub)
        shot(page, "phone-session", f"agents/{S1}", wait=".chat-scroll .timeline", before=expand_steps)
        shot(page, "phone-session-panel", f"agents/{S1}", before=open_phone_panel)
        phone.close()
        browser.close()
    return UNHANDLED.report()


def run_composer() -> int:
    """Retake conversation cards and the voice page that embeds them, in the selected language."""
    result = run_workspace()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=FAKE_MEDIA)
        for prefix, viewport, mobile in (
            ("voice", DESK, False),
            ("voice-phone", PHONE, True),
            ("voice-wide", {"width": 2560, "height": 1300}, False),
        ):
            context = browser.new_context(viewport=viewport, device_scale_factor=3 if mobile else 2, color_scheme="dark", is_mobile=mobile, has_touch=mobile, permissions=["microphone"])
            page = context.new_page()
            page.route("**/api/**", stub)
            if prefix != "voice-wide":
                shot(page, "phone-voice" if mobile else "voice", "voice")
            stub.voice_frames = VOICE_STATES["speaking"]["frames"]  # type: ignore[attr-defined]
            for name, before in VOICE_VIEWS.items():
                shot(page, f"{prefix}-{name}", "voice", settle=1200, before=before)
            stub.voice_frames = ""  # type: ignore[attr-defined]
            context.close()
        browser.close()
    return result or UNHANDLED.report()


def open_first_kind(page: Page) -> None:
    """A phone's matrix row, opened into its four channels."""
    page.locator(".nrows .nrow-head").first.click()
    page.wait_for_selector(".nrow.open .nrow-line", timeout=5000)


# The session's terminal dock, as the mock-up drew it: the tests failing in one pane, the dev server in
# the other. The terminal's own text is what the programs print, the same in both languages.
ESC = "\x1b["
def dock_prompt(ok: bool) -> str:
    """The bakery shell's prompt: its dot says how the last command went, as a themed prompt does."""
    return f"{ESC}{32 if ok else 31}m●{ESC}0m {ESC}36mbakery-site{ESC}0m {ESC}34m(main){ESC}0m $ "


def dock_tests(term, id_: str) -> None:  # type: ignore[no-untyped-def]
    """The tests terminal as a shell with integration prints it: each prompt and command marked, so the
    dock shows a dot beside every command and the tab takes the last one's colour."""
    term.shell_prompt(id_, dock_prompt(True))
    term.shell_command(id_, "git status -s", [f" {ESC}33mM{ESC}0m src/pages/checkout.tsx", f" {ESC}33mM{ESC}0m src/lib/cart.ts", f"{ESC}31m??{ESC}0m src/pages/checkout.test.tsx"], 0)
    term.shell_prompt(id_, dock_prompt(True))
    term.shell_command(id_, "npm test -- checkout", [
        "",
        f" {ESC}32m✓{ESC}0m the cart adds up the total {ESC}2m(4 ms){ESC}0m",
        f" {ESC}32m✓{ESC}0m an empty cart leads back to the menu {ESC}2m(2 ms){ESC}0m",
        f" {ESC}31m✗ promo code SPRING10{ESC}0m",
        f"   Expected: {ESC}32m1080{ESC}0m",
        f"   Received: {ESC}31m1200{ESC}0m",
        "",
        f" Tests: {ESC}31m1 failed{ESC}0m, {ESC}32m2 passed{ESC}0m, 3 total",
    ], 1)
    term.shell_prompt(id_, dock_prompt(False))


DOCK_DEV = (
    f"\r\n  {ESC}1;32mVITE{ESC}0m {ESC}32mv7.1.4{ESC}0m  ready in {ESC}1m412{ESC}0m ms\r\n\r\n"
    f"  {ESC}32m➜{ESC}0m  {ESC}1mLocal{ESC}0m:   {ESC}36mhttp://127.0.0.1:8124/{ESC}0m\r\n"
    f"  {ESC}2m➜  Network: use --host to expose{ESC}0m\r\n"
    f"{ESC}2m14:02:11{ESC}0m {ESC}36m[vite]{ESC}0m hmr update /src/pages/checkout.tsx\r\n"
    f"{ESC}2m14:02:19{ESC}0m {ESC}36m[vite]{ESC}0m hmr update /src/lib/cart.ts\r\n"
)


def dock_shots(context, prefix: str = "") -> None:  # type: ignore[no-untyped-def]
    """The dock under the conversation (desktop), or one terminal full screen (phone)."""
    from terminal_stub import TerminalStub, dock_state

    term = TerminalStub(S1)
    term.add("d1tests00000", title="bash · bakery-site")
    term.add("d2devsrv0000", title="npm run dev", busy=True)
    dock_tests(term, "d1tests00000")
    term.emit("d2devsrv0000", DOCK_DEV)
    page = context.new_page()
    page.route("**/api/**", stub)
    term.install(page)
    # The DOM renderer: under an emulated device scale factor Chromium reports the canvas's device
    # pixels at scale 1, and the WebGL renderer, which sizes itself from them, draws doubled glyphs.
    page.add_init_script("try { localStorage.setItem('daedalus.term.renderer', 'dom'); } catch (e) {}")
    if prefix:
        # A phone has no dock: the header's list opens a terminal over the whole screen.
        page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={LANG}")
        page.wait_for_selector(".chat-scroll .timeline", timeout=15000)
        phone_tool(page, "terminal")
        page.locator(".term-sheet .term-sheet-row").first.click()
        page.wait_for_selector(".term-full .term-view[data-state='live']", timeout=15000)
        page.wait_for_timeout(900)
        page.screenshot(path=str(OUT / "phone-session-terminal.png"))
        print("wrote phone-session-terminal")
    else:
        page.add_init_script(dock_state(S1, ["d1tests00000", "d2devsrv0000"], split="d2devsrv0000", height=330))
        page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={LANG}")
        page.wait_for_selector(".term-dock.open .term-view[data-state='live']", timeout=15000)
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / "session-dock.png"))
        print("wrote session-dock")
    page.close()


def terminals_fleet():  # type: ignore[no-untyped-def]
    """The Terminals screen as the mock-up drew it: failing tests and a dev server in a conversation, two
    staff members (one on the host, waiting for permission), htop nobody watches, and a psql that ended.
    The rows are what the programs print, the same in both languages; the owners and the staff member's
    status are the app's words, in the run's language."""
    from terminal_stub import TerminalStub, run

    term = TerminalStub(S1)
    term.add("m1tests00000", title="bash · bakery-site", owner_kind="session", owner_label=word("checkout"), project_id=P1, created_at=ago(minutes=2),
             preview=[[run(" ✗", 1), run(" promo code SPRING10")], [run("   Expected: "), run("1080", 2)], [run("   Received: "), run("1200", 1)],
                      [run(" Tests: "), run("1 failed", 1), run(", "), run("2 passed", 2)], [run("bakery-site", 2), run(" $ ")]])
    term.add("m2devsrv0000", title="npm run dev", owner_kind="session", owner_label=word("checkout"), project_id=P1, created_at=ago(minutes=38),
             preview=[[run("14:02:11 ", 8), run("[vite]", 6), run(" hmr update /src/pages/checkout.tsx")], [run("14:02:19 ", 8), run("[vite]", 6), run(" hmr update /src/lib/cart.ts")],
                      [run("14:03:40 ", 8), run("[vite]", 6), run(" page reload")], [run("14:05:02 ", 8), run("[vite]", 6), run(" hmr update /src/lib/cart.ts")]])
    term.add("m3claude0000", title="Ira · Claude Code", owner_kind="staff", owner_id="st-ira", owner_label="Ira", project_id=P1, created_at=ago(minutes=64),
             preview=[[run("⏺", 7), run(" Update(src/lib/cart.ts)")], [run("  ⎿ ", 8), run(" Updated with "), run("6 additions", 2)], [run("⏺", 7), run(" Bash(npm test -- checkout)")],
                      [run("  ⎿ ", 8), run(" ✓ 3 passed", 2)], [run("✻ Thinking…", 3), run(" (esc to interrupt)", 8)]])
    term.add("m4opencode00", title="Naya · OpenCode", env="host", owner_kind="staff", owner_id="st-naya", owner_label="Naya", project_id=P1, created_at=ago(minutes=12),
             activity={"label": word("permission"), "level": "warn", "action": {"label": word("answer"), "path": "/app/inbox"}},
             preview=[[run("△ Permission required", 3)], [run("  bash: "), run("npm install grammy", 7, b=True)], [run("  cwd ~/work/bakery-bot", 8)], [], [run(" Allow once ", inv=True), run("  Always  Reject")]])
    term.add("m5htop000000", title="htop", env="host", owner_kind="free", owner_id="", cwd="~", clients=0, last_input_at=ago(hours=1), created_at=ago(hours=3),
             preview=[[run("  1", 6), run("["), run("|||||||", 2), run("        34%]", 8)], [run("  2", 6), run("["), run("||||", 2), run("           18%]", 8)],
                      [run("  Mem", 6), run("["), run("|||||||||", 2), run("||", 4), run("  9.1G/31G]", 8)], [run("  Load average: ", 8), run("3.12 2.80 2.41")]])
    term.add("m6psql000000", title="psql · orders", owner_kind="project", owner_id=P1, owner_label="Bakery site", project_id=P1,
             status="exited", exit_code=0, exited_at=ago(minutes=20), preview=[[run("orders=# \\q")], [run("[process exited · code 0]", 8)]])
    return term


def terminals_shots(context, prefix: str = "") -> None:  # type: ignore[no-untyped-def]
    """The Terminals screen: cards by project with the load bar in the header (desktop), or its list (phone)."""
    from terminal_stub import load_answer

    term = terminals_fleet()
    term.load = load_answer(total_gb=62, used_gb=21, terminals_gb=2.1, likely_mb=640)
    page = context.new_page()
    page.route("**/api/**", stub)
    term.install(page)
    page.goto(f"{BASE}/terminals?token=t&scheme=dark&lang={LANG}")
    page.wait_for_selector(".term-card .term-card-line", timeout=15000)
    page.wait_for_timeout(700)
    page.screenshot(path=str(OUT / f"{prefix}terminals.png"))
    print(f"wrote {prefix}terminals")
    page.close()


def run_terminals() -> int:
    """Only the Terminals screen and the More sheet that leads to it on a phone (``ONLY=terminals``)."""
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
        terminals_shots(desk)
        desk.close()
        phone = browser.new_context(viewport=PHONE, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
        terminals_shots(phone, "phone-")
        page = phone.new_page()
        page.route("**/api/**", stub)
        shot(page, "phone-more", "agents", before=open_more)
        phone.close()
        browser.close()
    return UNHANDLED.report()


def open_bell(page: Page) -> None:
    """The bell's popover over the agents: what needs you, with its buttons, and the day's feed."""
    page.locator(".sidebar .bell").click()
    page.wait_for_selector(".bell-pop .needs-you .notice-row", timeout=5000)


def raise_toasts(page: Page) -> None:
    """Three notifications arriving while the operator reads another conversation."""
    page.wait_for_selector(".notice-toasts .notice-toast", timeout=10000)
    page.wait_for_timeout(400)
    page.mouse.move(DESK["width"] - 60, DESK["height"] - 60)  # a hand on the stack keeps it still


def run_notifications() -> int:
    """The bell, the toasts and the Inbox as the centre, on a desktop and on a phone."""
    OUT.mkdir(parents=True, exist_ok=True)
    finished = next(e for e in INBOX if e["id"] == 31)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
        page = desk.new_page()
        page.route("**/api/**", stub)
        shot(page, "bell", "agents", before=open_bell, settle=500)
        shot(page, "inbox", "inbox")
        stub.events = notify_frames(finished, QUESTION, PERMISSION)  # type: ignore[attr-defined]
        shot(page, "toasts", f"agents/{S1}", wait=".chat-scroll .timeline", before=raise_toasts, settle=300)
        stub.events = ""  # type: ignore[attr-defined]
        desk.close()
        phone = browser.new_context(viewport=PHONE, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
        page = phone.new_page()
        page.route("**/api/**", stub)
        shot(page, "phone-inbox", "inbox", wait=".needs-you .notice-row")
        # Two at once: the banner holds the first, and the second is counted beside Close all.
        stub.events = notify_frames(PERMISSION, finished)  # type: ignore[attr-defined]
        shot(page, "phone-toast", "agents", wait=".notice-toasts.banner .notice-toast", settle=500)
        stub.events = ""  # type: ignore[attr-defined]
        phone.close()
        browser.close()
    return UNHANDLED.report()


def focus_stub(focus: FocusStub):  # type: ignore[no-untyped-def]
    """The invented installation with an orchestrated project in it: focus mode's routes first."""

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        rel = url.path[url.path.index("/api/"):]
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        answered = focus.answer(request.method, rel, url.query, body)
        if answered is not None:
            return respond(route, answered[1], status=answered[0])
        return stub(route)

    return handle


def open_focus_brief(page: Page) -> None:
    """The orchestrator with the board open beside it, as the mock-up draws it."""
    page.wait_for_selector(".chat.in-project .event-card", timeout=10000)
    tab = page.locator(".panel .panel-tab[data-tab='board']")
    if tab.count() == 0:
        page.locator(".chat-head .head-actions button[aria-pressed]").last.click()
    page.locator(".panel .panel-tab[data-tab='board']").click()
    page.wait_for_selector(".panel .pboard.embedded .pcard", timeout=5000)


def draft_questions(page: Page) -> None:
    """The Questions tab half answered: an option chosen with a note, two providers of three, and a
    refusal whose reason is still to be written — two cards ready, one a draft."""
    words = FOCUS_WORDS[LANG]
    page.locator(f".q-card[data-ask='q4r8tz'] .q-chip[data-option=\"{words['ask.before']}\"]").click()
    page.locator(".q-card[data-ask='q4r8tz'] .q-field").fill(word("q.note"))
    page.locator(".q-card[data-ask='qp4y01'] .q-chip[data-option='Stripe']").click()
    page.locator(f".q-card[data-ask='qp4y01'] .q-chip[data-option=\"{words['cash']}\"]").click()
    page.locator(".q-card[data-ask='qg1t04'] .q-chip[data-option='because']").click()
    page.locator(".questions-list").evaluate("(el) => el.closest('.panel-body').scrollTo(0, 0)")


def open_phone_questions(page: Page) -> None:
    """On a phone the header's button opens the tab as a sheet."""
    page.locator(".chat-head .questions-headbtn").tap()
    page.wait_for_selector(".panel-sheet .q-card", timeout=5000)
    page.locator(".panel-sheet .q-card[data-ask='qg1t04'] .q-chip[data-option='allow']").tap()


def open_phone_long_question(page: Page) -> None:
    """The sheet with the long question alone in it, one of its options chosen."""
    page.locator(".chat-head .questions-headbtn").tap()
    page.wait_for_selector(".panel-sheet .q-card[data-ask='ql4nch']", timeout=5000)
    page.locator(".panel-sheet .q-card[data-ask='ql4nch'] .q-chip").nth(1).tap()


def run_focus() -> int:
    """A project's focus mode: the orchestrator's chat with the board beside it, and the journal; its
    Questions tab half answered, and the same tab as a phone's sheet."""
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
        page = desk.new_page()
        focus = FocusStub.bakery(LANG)
        page.route("**/api/**", focus_stub(focus))
        pid = focus.projects[0]["id"]
        shot(page, "project-focus", f"project/{pid}?panel=board", wait=".chat.in-project .event-card", before=open_focus_brief, settle=700)
        shot(page, "project-journal", f"project/{pid}/journal", wait=".journal-entry", settle=500)
        shot(page, "project-staff", f"project/{pid}/s/sess-lev?panel=brief", wait=".staff-head", settle=700)
        asking = FocusStub.bakery(LANG)
        asking.questions_of_bakery(LANG)
        page = desk.new_page()
        page.route("**/api/**", focus_stub(asking))
        shot(page, "project-questions", f"project/{pid}?panel=questions", wait=".questions-panel .q-card", before=draft_questions, settle=700)
        desk.close()
        phone = browser.new_context(viewport=PHONE, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
        asking = FocusStub.bakery(LANG)
        asking.questions_of_bakery(LANG)
        page = phone.new_page()
        page.route("**/api/**", focus_stub(asking))
        shot(page, "phone-questions", f"project/{pid}", wait=".chat-head .questions-headbtn", before=open_phone_questions, settle=900)
        # A question whose options are sentences and a link: each wraps inside its card.
        asking = FocusStub.bakery(LANG)
        asking.question_of_long_options(LANG)
        page = phone.new_page()
        page.route("**/api/**", focus_stub(asking))
        shot(page, "phone-questions-long", f"project/{pid}", wait=".chat-head .questions-headbtn", before=open_phone_long_question, settle=900)
        phone.close()
        browser.close()
    return UNHANDLED.report()


def run_main() -> int:
    """The main orchestrator's chat, orchestration mode's home on a desktop: the reports it was woken
    with, then in the flow the projects' questions grouped by project and the work under way; and on a
    phone the list the Orchestration tab opens, then the chat as a detail of it (``ONLY=main``)."""
    OUT.mkdir(parents=True, exist_ok=True)
    main = MainStub(LANG)
    # The projects the main chat hands work to have orchestrators of their own. Without them in the
    # listing the column and the phone's Orchestration page said no project had one, under a chat
    # dispatching to two.
    orchestrated = [
        {"id": "p-bakery", "name": "Bakery", "folders": folders("/srv/workspaces/bakery"), "created_at": ago(days=6), "settings": {"snapshots": True},
         "total": 5, "active": 3, "loops": 0, "last_message_at": ago(minutes=3), "orchestrator": {"enabled": True, "session_id": "orch-bakery", "staff": 4, "working": 3, "needs_you": 2}},
        {"id": "p-garden", "name": "Garden", "folders": folders("/srv/workspaces/garden"), "created_at": ago(days=1), "settings": {"snapshots": False}, "setup_by": "dispatcher",
         "total": 1, "active": 0, "loops": 0, "last_message_at": ago(minutes=40), "orchestrator": {"enabled": True, "session_id": "orch-garden", "staff": 0, "working": 0, "needs_you": 0}},
    ]

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        if url.path.endswith("/api/sessions") and request.method == "GET":
            rows = listing()
            return respond(route, {**rows, "projects": orchestrated + rows["projects"]})
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        answered = main.answer(request.method, url.path[url.path.index("/api/"):], body)
        if answered is not None:
            return respond(route, answered[1], status=answered[0])
        return stub(route)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
        # The panel beside the chat opens on every project's questions, grouped by project.
        page = desk.new_page()
        page.route("**/api/**", handle)
        shot(page, "main", "orchestration", wait=".questions-panel .q-card", settle=900)
        desk.close()
        # On a phone the Orchestration tab lands on the list, Main first; the chat is a detail of it.
        phone = browser.new_context(viewport=PHONE, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
        page = phone.new_page()
        page.route("**/api/**", handle)
        shot(page, "phone-orchestration", "orchestration/projects", wait=".ph-orch [data-main-entry]", settle=900)
        shot(page, "phone-main", "orchestration", wait=".timeline > .questions-line", settle=900)
        phone.close()
        browser.close()
    return UNHANDLED.report()


def run_details() -> int:
    """The Details of a project's orchestrator and of a command-line member, on a desktop and on a
    phone (``ONLY=details``): the spend in total and today, the context with its last compaction, and
    the member's launch, state and what its CLI did and did not report."""
    OUT.mkdir(parents=True, exist_ok=True)

    def member_details(scope: str):  # type: ignore[no-untyped-def]
        def before(page: Page) -> None:
            if scope == ".sheet":
                page.locator(".staff-cli .chat-head .head-actions .iconbtn").last.tap()
            page.locator(f"{scope} .panel-tab[data-tab='details']").click()
            page.wait_for_selector(f"{scope} [data-staff-details]", timeout=5000)
        return before

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
        focus = FocusStub.bakery(LANG)
        page = desk.new_page()
        page.route("**/api/**", focus_stub(focus))
        pid = focus.projects[0]["id"]
        shot(page, "orchestrator-details", f"project/{pid}?panel=details", wait=".panel .details [data-usage-today]", settle=700)
        page, pid = staff_stand(desk)
        shot(page, "staff-details", f"project/{pid}/staff/st-ira", wait=".staff-aside .panel-tab[data-tab='details']", before=member_details(".staff-aside"), settle=900)
        desk.close()
        phone = browser.new_context(viewport=PHONE, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
        focus = FocusStub.bakery(LANG)
        page = phone.new_page()
        page.route("**/api/**", focus_stub(focus))
        shot(page, "phone-orchestrator-details", f"project/{pid}?panel=details", wait=".panel-sheet .details [data-usage-today]", settle=900)
        page, pid = staff_stand(phone)
        shot(page, "phone-staff-details", f"project/{pid}/staff/st-ira", wait=".staff-cli .chat-head", before=member_details(".sheet"), settle=900)
        phone.close()
        browser.close()
    return UNHANDLED.report()


IRA_SCREEN = (
    f"{ESC}1m⏺{ESC}0m Update(src/lib/cart.ts)\r\n"
    f"  {ESC}2m⎿{ESC}0m  Updated with {ESC}32m6 additions{ESC}0m\r\n\r\n"
    f"{ESC}1m⏺{ESC}0m Bash(npm install @stripe/stripe-js)\r\n"
    f"{ESC}2m╭──────────────────────────────────────╮{ESC}0m\r\n"
    f"{ESC}2m│{ESC}0m {ESC}1mBash command{ESC}0m                         {ESC}2m│{ESC}0m\r\n"
    f"{ESC}2m│{ESC}0m   npm install @stripe/stripe-js      {ESC}2m│{ESC}0m\r\n"
    f"{ESC}2m│{ESC}0m Do you want to proceed?              {ESC}2m│{ESC}0m\r\n"
    f"{ESC}2m│{ESC}0m {ESC}36m❯ 1. Yes{ESC}0m                             {ESC}2m│{ESC}0m\r\n"
    f"{ESC}2m│{ESC}0m   2. No, and tell Claude what to do  {ESC}2m│{ESC}0m\r\n"
    f"{ESC}2m╰──────────────────────────────────────╯{ESC}0m\r\n"
)


def phone_terminal_shots(context) -> None:  # type: ignore[no-untyped-def]
    """The phone's terminal (M3, M3б): a shell with the soft keyboard up and Ctrl armed, and a staff
    member's terminal whose request is answered above the keys."""
    from terminal_stub import TerminalStub

    term = TerminalStub(S1)
    term.add("d1tests00000", title="bash · bakery-api", cwd="/home/operator/work/bakery-api")
    term.emit("d1tests00000", "".join(
        f"{line}\r\n" for line in [
            f"{ESC}32mbakery-api{ESC}0m $ docker compose ps", "NAME        STATUS          PORTS", f"api         {ESC}32mUp 2 hours{ESC}0m      8000/tcp",
            f"worker      {ESC}32mUp 2 hours{ESC}0m", f"db          {ESC}32mUp 2 hours{ESC}0m      5432/tcp", f"{ESC}32mbakery-api{ESC}0m $ tail -f logs/orders.log",
            f"{ESC}2m14:23:58{ESC}0m order {ESC}1m#1042{ESC}0m created", f"{ESC}2m14:23:58{ESC}0m notify → baker {ESC}32mok{ESC}0m",
            f"{ESC}2m14:24:05{ESC}0m order {ESC}1m#1043{ESC}0m created", f"{ESC}2m14:24:05{ESC}0m notify → baker {ESC}32mok{ESC}0m",
        ]))
    page = context.new_page()
    page.route("**/api/**", stub)
    term.install(page)
    page.add_init_script("try { localStorage.setItem('daedalus.term.renderer', 'dom'); } catch (e) {}")
    page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={LANG}")
    page.wait_for_selector(".chat-scroll .timeline", timeout=15000)
    phone_tool(page, "terminal")
    page.locator(".term-sheet .term-sheet-row").first.click()
    page.wait_for_selector(".term-phone .term-view[data-state='live']", timeout=15000)
    # The soft keyboard takes the lower 336 px: the picture is what stays visible above it.
    page.locator(".term-phone .term-screen").tap()
    page.set_viewport_size({"width": PHONE["width"], "height": PHONE["height"] - 336})
    page.locator(".term-keys .term-key[data-key='ctrl']").tap()
    page.wait_for_timeout(1000)
    page.screenshot(path=str(OUT / "phone-terminal-keyboard.png"))
    print("wrote phone-terminal-keyboard")
    page.set_viewport_size(PHONE)
    page.close()

    focus = FocusStub.bakery(LANG)
    focus.ask_from_ira(LANG)
    staff_term = TerminalStub(S1)
    staff_term.add("tm-ira", title="claude · Ira", owner_kind="staff", owner_id="st-ira", project_id=focus.projects[0]["id"], owner_label="Ira", cwd="/home/operator/work/bakery-site")
    staff_term.emit("tm-ira", IRA_SCREEN)
    page = context.new_page()
    page.route("**/api/**", focus_stub(focus))
    staff_term.install(page)
    page.add_init_script("try { localStorage.setItem('daedalus.term.renderer', 'dom'); } catch (e) {}")
    page.goto(f"{BASE}/terminals/tm-ira?token=t&scheme=dark&lang={LANG}")
    page.wait_for_selector(".term-phone-actions .ask-answers-row .btn", timeout=15000)
    page.wait_for_selector(".term-phone .term-view[data-state='live']", timeout=15000)
    page.wait_for_timeout(1000)
    page.screenshot(path=str(OUT / "phone-staff-terminal.png"))
    print("wrote phone-staff-terminal")
    page.close()


def phone_project_shots(context) -> None:  # type: ignore[no-untyped-def]
    """A project on the phone (M8, M8б): the team under the request that waits longest, and the board."""
    focus = FocusStub.bakery(LANG)
    focus.ask_from_ira(LANG)
    page = context.new_page()
    page.route("**/api/**", focus_stub(focus))
    pid = focus.projects[0]["id"]
    shot(page, "phone-project", f"project/{pid}/team", wait=".needs-card .needs-row .ph-btn", settle=700)
    shot(page, "phone-board", f"project/{pid}/board", wait=".ph-board .ph-row", settle=700)
    shot(page, "phone-orchestrator", f"project/{pid}", wait=".chat.in-project .event-card", settle=900)
    page.close()


def staff_stand(context, *, browse: bool = False, aside: int | None = None):  # type: ignore[no-untyped-def]
    """Ira, a Claude Code member, in her own view: her terminal with a permission dialog on it. With
    ``browse`` her browser's permission waits beside it, and with ``aside`` the column beside the
    terminal was dragged to that width."""
    from terminal_stub import TerminalStub

    focus = FocusStub.bakery(LANG)
    focus.staff_view_of_ira(LANG)
    if browse:
        focus.browser_permission_of_ira()
    pid = focus.projects[0]["id"]
    term = TerminalStub(S1)
    term.add("tm-ira", title="claude · Ira", owner_kind="staff", owner_id="st-ira", project_id=pid, owner_label="Ira", cwd="/home/operator/work/bakery-site")
    term.emit("tm-ira", IRA_SCREEN)
    page = context.new_page()
    page.route("**/api/**", focus_stub(focus))
    term.install(page)
    page.add_init_script("try { localStorage.setItem('daedalus.term.renderer', 'dom'); } catch (e) {}")
    if aside is not None:
        page.add_init_script(f"try {{ localStorage.setItem('daedalus.staff.asideWidth', '{aside}'); }} catch (e) {{}}")
    return page, pid


def open_harness_rows(page: Page) -> None:
    """Claude Code's row unfolded to its agents, as M7 draws it."""
    page.locator(".harness-row[data-harness='claude'] .harness-name").click()
    page.wait_for_selector(".harness-row-details .harness-agent", timeout=5000)


def run_harnesses() -> int:
    """The Harnesses screen (M7) on a desktop and on a phone (``ONLY=harnesses``)."""
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
        page = desk.new_page()
        page.route("**/api/**", stub)
        shot(page, "harnesses", "harnesses", wait=".harness-table .harness-row", before=open_harness_rows, settle=500)
        desk.close()
        phone = browser.new_context(viewport=PHONE, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
        page = phone.new_page()
        page.route("**/api/**", stub)
        shot(page, "phone-harnesses", "harnesses", wait=".harness-cards .harness-card", settle=500)
        phone.close()
        browser.close()
    return UNHANDLED.report()


def run_staff() -> int:
    """A command-line member's own view (M5): the terminal with the column beside it on a desktop,
    the Feed on a desktop, and the Feed with the request above the composer on a phone (``ONLY=staff``)."""
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
        page, pid = staff_stand(desk)
        shot(page, "staff-view", f"project/{pid}/staff/st-ira", wait=".staff-term .term-view[data-state='live']", settle=1200)
        page.locator(".staff-mode button[data-mode='feed']").click()
        page.locator(".feed-turn[data-turn='1'] .feed-tools-head").click()
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / "staff-feed.png"))
        print("wrote staff-feed")
        desk.close()
        # The browser's permission, which "Always" may grant for its whole server, and the standing
        # grants listed; then the column beside the terminal dragged wider, the terminal refitted to
        # what is left. Each in a browser of its own: a terminal already shown in one holds its size.
        for name, over, wait in (("staff-permissions", {"browse": True}, ".staff-request [data-answer='always-server']"), ("staff-aside-wide", {"aside": 560}, ".staff-term .term-view[data-state='live']")):
            desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
            page, pid = staff_stand(desk, **over)
            shot(page, name, f"project/{pid}/staff/st-ira", wait=wait, settle=1400)
            desk.close()
        phone = browser.new_context(viewport=PHONE, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
        page, pid = staff_stand(phone)
        shot(page, "phone-staff-feed", f"project/{pid}/staff/st-ira", wait=".staff-request .ask-answers-row .btn", settle=900)
        phone.close()
        browser.close()
    return UNHANDLED.report()


def run_phone() -> int:
    """The phone's terminal and a project on the phone (``ONLY=phone``)."""
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        phone = browser.new_context(viewport=PHONE, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
        phone_terminal_shots(phone)
        phone_project_shots(phone)
        phone.close()
        browser.close()
    return UNHANDLED.report()


def run() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
        desk.add_init_script("try { localStorage.setItem('daedalus.session.panel', 'details'); localStorage.setItem('agents.groupBy', 'workspace'); } catch (e) {}")
        # The agents picture has every project folder open, so the fork under its origin, the
        # subagent under its leader and the loop's cadence are in it; collapsed, as the list opens,
        # it showed folder names and nothing the caption speaks of. Its own context, so the open
        # folders do not crowd every later picture's sidebar.
        opened = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
        opened.add_init_script("try { localStorage.setItem('agents.groupBy', 'workspace'); " + " ".join(f"localStorage.setItem('daedalus.folder.{p_['id']}', '1');" for p_ in PROJECTS) + " } catch (e) {}")
        page = opened.new_page()
        page.route("**/api/**", stub)
        shot(page, "bots", "agents")
        opened.close()
        page = desk.new_page()
        page.route("**/api/**", stub)
        shot(page, "session", f"agents/{S1}", wait=".chat-scroll .timeline", before=expand_steps, settle=300)
        shot(page, "session-panel-files", f"agents/{S1}", wait=".chat-scroll .timeline", before=open_panel_files, settle=800)
        shot(page, "session-panel-preview", f"agents/{S1}", wait=".chat-scroll .timeline", before=open_panel_preview, settle=1200)
        workspace_shots(page)
        shot(page, "session-menu", f"agents/{S1}", wait=".chat-scroll .timeline", before=open_menu, settle=500)
        # The fold is remembered: every desktop picture from here on has the sidebar as the strip.
        shot(page, "session-folded", f"agents/{S1}", wait=".chat-scroll .timeline", before=fold_sidebar, settle=500)
        shot(page, "dual", f"agents/{S1}?with={S2}", wait=".chat-scroll .timeline", settle=1500)
        shot(page, "session-share", f"agents/{S1}", wait=".chat-scroll .timeline", before=open_share, settle=800)
        dock_shots(desk)
        terminals_shots(desk)
        desk.add_init_script("try { localStorage.setItem('agents.groupBy', 'project'); } catch (e) {}")
        shot(page, "team", f"project/{P1}/team", wait=".staff-row")
        shot(page, "team-hire", f"project/{P1}/team", wait=".staff-row", before=open_hire, settle=700)
        # The start canvas under a folded sidebar has neither a list nor a chat, so waiting for
        # either timed out here and stopped every picture after this one; the rail is there either
        # way, and the switcher opens from the sidebar it unfolds.
        shot(page, "projects", "agents", wait=".rail", before=open_projects)
        shot(page, "voice", "voice")
        shot(page, "voice-settings", "settings/voice", wait=".stt-list .stt-card", before=scroll_to_voices, settle=700)
        shot(page, "board", "board")
        shot(page, "inbox", "inbox")
        shot(page, "cron", "schedules")
        shot(page, "services", "services")
        shot(page, "usage", "usage", settle=1500)
        shot(page, "health", "health", settle=900)
        shot(page, "memory", "memory")
        # The settings index, because the language switch is its first row.
        # Settings is a centred stage of its own now, with no `.screen` to wait for.
        shot(page, "settings", "settings", wait=".settings-stage")
        shot(page, "components", "settings/components", wait=".comp-grid .comp-card", settle=500)
        shot(page, "settings-notifications", "settings/notifications", wait=".nmatrix", settle=600)
        # Terminals and the browser are one Environments page; the picture keeps its name for the docs.
        shot(page, "settings-terminals", "settings/environments", wait=".loadbar-track", settle=600)
        shot(page, "settings-tools", "settings/tools", wait=".tgroups .tgroup", settle=500)
        shot(page, "session-tool-groups", f"agents/{S1}", wait=".chat-scroll .timeline", before=open_tool_groups, settle=300)
        # And the same install where the owner met its absence: under the two lines on the voice card
        # that say the browser is doing the listening and the speaking.
        stub.nospeech = True  # type: ignore[attr-defined]
        shot(page, "voice-needs-speech", "settings/voice", wait=".voice-needs .btn", settle=600)
        stub.nospeech = False  # type: ignore[attr-defined]
        stub.fresh = True  # type: ignore[attr-defined]
        shot(page, "add-model", "agents", wait=".addmodel", before=pick_a_model, settle=600)
        stub.fresh = False  # type: ignore[attr-defined]
        stub.signedout = True  # type: ignore[attr-defined]
        shot(page, "login", "agents", wait=".login", settle=500)
        stub.signedout = False  # type: ignore[attr-defined]
        desk.close()

        phone = browser.new_context(viewport=PHONE, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
        phone.add_init_script("try { localStorage.setItem('agents.groupBy', 'status'); } catch (e) {}")
        page = phone.new_page()
        page.route("**/api/**", stub)
        shot(page, "phone-bots", "agents")
        shot(page, "phone-session", f"agents/{S1}", wait=".chat-scroll .timeline", settle=300)
        shot(page, "phone-session-panel", f"agents/{S1}", wait=".chat-scroll .timeline", before=open_phone_panel, settle=600)
        dock_shots(phone, "phone-")
        terminals_shots(phone, "phone-")
        shot(page, "phone-voice", "voice")
        shot(page, "phone-memory", "memory")
        shot(page, "phone-health", "health", settle=900)
        shot(page, "phone-more", "agents", before=open_more)
        shot(page, "phone-drawer", "agents", wait=".ph-home", before=open_drawer)
        shot(page, "phone-chats", "agents?view=chats", wait=".ph-row")
        shot(page, "phone-team", f"project/{P1}/team", wait=".phone-staff-row")
        shot(page, "phone-settings-notifications", "settings/notifications", wait=".nrows .nrow", before=open_first_kind, settle=500)
        shot(page, "phone-settings-tools", "settings/tools", wait=".tgroups .tgroup", settle=500)
        shot(page, "phone-session-tool-groups", f"agents/{S1}", wait=".chat-scroll .timeline", before=open_phone_tool_groups, settle=300)
        stub.fresh = True  # type: ignore[attr-defined]
        shot(page, "phone-add-model", "agents", wait=".addmodel", before=pick_a_model, settle=600)
        stub.fresh = False  # type: ignore[attr-defined]
        phone.close()
        browser.close()
    # The notification centre needs a stream of its own for the toasts, and focus mode an installation
    # with an orchestrated project; each reports what went unanswered.
    notifications = run_notifications()
    phone = run_phone()
    staff = run_staff()
    harnesses = run_harnesses()
    main = run_main()
    browser = run_browser()
    return run_focus() or notifications or phone or staff or harnesses or main or browser


def run_browser() -> int:
    """The agent's browser (``ONLY=browser``): the corner preview beside the chat, the Browser tab live
    with the agent's cursor, the operator driving, and the phone's sheet and takeover."""
    from api_stub import terminal_load
    from browser_stub import BrowserStub, browser_load, render_scenes, wait_frames

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        # Also at the shape a phone's takeover asks the page to take (its screen under the bar and
        # the keys), so that picture shows the page at the phone's width as the real one does.
        scenes = render_scenes(browser, sizes=((PHONE["width"], PHONE["height"] - 220),))
        bs = BrowserStub(scenes)
        bs.add("g1", scene="shop", owner_id=S1, acting=True)
        bs.act("g1", "click", "size", name="5 kg", element="the 5 kg size option")

        def page_for(context):  # type: ignore[no-untyped-def]
            page = context.new_page()
            page.route("**/api/**", stub)
            bs.install(page)
            return page

        desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
        page = page_for(desk)
        page.goto(f"{BASE}/agents/{S1}?panel=details&token=t&scheme=dark&lang={LANG}")
        page.wait_for_selector(".bp-pip", timeout=15000)
        wait_frames(page, ".bp-pip", 1)
        page.wait_for_timeout(600)
        bs.act("g1", "click", "add", name="Add to cart", element="the Add to cart button")
        page.wait_for_timeout(260)
        page.screenshot(path=str(OUT / "browser-pip.png"))
        print("wrote browser-pip")

        page.goto(f"{BASE}/agents/{S1}?panel=browser&token=t&scheme=dark&lang={LANG}")
        page.wait_for_selector(".panel .bp .bv[data-state='live']", timeout=15000)
        wait_frames(page, ".panel .bp", 1)
        page.locator(".panel .bp-log-head").click()
        page.wait_for_timeout(500)
        bs.act("g1", "type", "search", name="Search", element="the shop's search field", text_len=15, text="whole rye flour")
        page.wait_for_timeout(280)
        page.screenshot(path=str(OUT / "session-browser.png"))
        print("wrote session-browser")

        page.locator(".panel .bp-control.take").click()
        page.wait_for_selector(".panel .bv.driving", timeout=5000)
        # Out of the way of every tooltip: the empty foot of the panel's stage.
        page.mouse.move(1300, 800)
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "session-browser-takeover.png"))
        print("wrote session-browser-takeover")
        page.locator(".panel .bp-control.give").click()
        page.locator(".bp-give textarea").fill("Signed in; carry on from the cart." if LANG == "en" else "Я вошёл, продолжай с корзины.")
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUT / "session-browser-giveback.png"))
        print("wrote session-browser-giveback")
        page.locator(".bp-give button[type='submit']").click()
        page.wait_for_timeout(300)

        # The recording: a keyframe per action, one of them replayed with its element framed.
        page.locator(".panel .bp-toolbar button[aria-haspopup='menu']").last.click()
        page.locator(".menu [role='menuitemcheckbox']").first.click()
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        added = bs.act("g1", "click", "add", name="Add to cart", element="the Add to cart button")
        bs.act("g1", "click", "size", name="5 kg", element="the 5 kg size option")
        page.wait_for_selector(f".panel .bp-log-row[data-action='{added['id']}'] .bp-log-frame", timeout=10000)
        page.locator(f".panel .bp-log-row[data-action='{added['id']}']").click()
        page.wait_for_selector(".panel .bp-replay .bp-replay-img", timeout=5000)
        page.mouse.move(1300, 850)
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "session-browser-replay.png"))
        print("wrote session-browser-replay")
        page.locator(".panel .bp-replay-live").click()

        # Settings → Environments, the browser half: what runs, the profiles, the limits on the shared load bar.
        bs.running = [{"env": "container", "id": "b1a2b3c4", "profile": f"project-{P1}", "started_at": ago(minutes=12), "rss_bytes": 318 << 20, "cpu_percent": 3.1, "tabs": 2,
                       "memory_basis": "cgroup", "groups": [{"id": "g1", "owner": {"kind": "session", "id": S1, "label": "Bakery site"}, "url": "https://shop.example.com/", "title": "Rye flour"}]}]
        bs.profiles = [
            {"id": f"project-{P1}", "env": "container", "scope": "project", "project_id": P1, "session_id": None, "staff_id": None, "created_at": ago(days=6), "last_used_at": ago(minutes=2), "size_bytes": 21 << 20, "running": True},
            {"id": f"project-{P2}", "env": "container", "scope": "project", "project_id": P2, "session_id": None, "staff_id": None, "created_at": ago(days=4), "last_used_at": ago(days=1), "size_bytes": 9 << 20, "running": False},
        ]
        page.route("**/api/workloads/load", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({"terminals": terminal_load(), "browsers": browser_load(running=1), "together": None})))
        page.goto(f"{BASE}/settings/environments?token=t&scheme=dark&lang={LANG}")
        page.wait_for_selector(".bs-running .bs-row", timeout=15000)
        page.wait_for_selector(".env-load .loadbar.workloads", timeout=15000)
        page.locator("#browser-sessions").evaluate("el => el.scrollIntoView({block: 'start'})")
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "settings-browser.png"))
        print("wrote settings-browser")
        desk.close()

        # The phone: a login the agent cannot do, so the button and the sheet are amber.
        bs.navigate("g1", "signin")
        bs.needs_you("g1", "login", "Sign in to accounts.example.com")
        phone = browser.new_context(viewport=PHONE, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
        phone.add_init_script("try { localStorage.setItem('daedalus.browser.announced', JSON.stringify(['g1'])); } catch (e) {}")
        page = page_for(phone)
        page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={LANG}")
        page.wait_for_selector(".browser-headbtn", timeout=15000)
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / "phone-browser-head.png"))
        print("wrote phone-browser-head")
        page.locator(".browser-headbtn").tap()
        page.wait_for_selector(".panel-sheet .bp .bv[data-state='live']", timeout=15000)
        wait_frames(page, ".panel-sheet .bp", 1)
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / "phone-browser.png"))
        print("wrote phone-browser")
        page.locator(".panel-sheet .bp-control.take").tap()
        page.wait_for_selector(".bp-drive .bv.driving", timeout=5000)
        wait_frames(page, ".bp-drive", 1)
        # The page takes the phone's shape a moment after the screen settles.
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / "phone-browser-takeover.png"))
        print("wrote phone-browser-takeover")
        phone.close()
        browser.close()
    return UNHANDLED.report()


def open_modes(page: Page) -> None:
    """The composer's mode chip, opened: the modes, and the YAGNI switch under them."""
    page.locator(".composer .composer-mode").click()
    page.wait_for_selector(".mode-menu .mode-row, .mode-sheet .mode-row", timeout=5000)


def queued_messages() -> list[dict]:
    return [
        {"id": "q-steered", "kind": "steer", "text": word("steered"), "queued_at": ago(seconds=50)},
        {"id": "q-follow", "kind": "follow_up", "text": word("queued"), "queued_at": ago(seconds=20)},
    ]


def run_modes() -> int:
    """The mode chip and its menu, with YAGNI on, on a desktop and on a phone, and the card a message
    written during a run waits in (``ONLY=modes``)."""
    OUT.mkdir(parents=True, exist_ok=True)
    stub.yagni = True  # type: ignore[attr-defined]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM)
            desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
            desk.add_init_script("try { localStorage.setItem('daedalus.session.panel', '0'); } catch (e) {}")
            page = desk.new_page()
            page.route("**/api/**", stub)
            shot(page, "composer-modes", f"agents/{S1}", wait=".composer .composer-mode", before=open_modes)
            desk.close()
            phone = browser.new_context(viewport=PHONE, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
            page = phone.new_page()
            page.route("**/api/**", stub)
            shot(page, "phone-composer-modes", f"agents/{S1}", wait=".composer .composer-mode", before=open_modes)
            stub.running = True  # type: ignore[attr-defined]
            shot(page, "phone-composer-yagni", f"agents/{S1}", wait=".composer .composer-mode.yagni")
            # A message written during the run waits for the end of the turn as a card above the
            # composer, with Steer to hand it to the agent now; one already steered sits beside it.
            stub.queued = queued_messages()  # type: ignore[attr-defined]
            shot(page, "phone-composer-queued", f"agents/{S1}", wait=".steers .steer .steer-now")
            phone.close()
            desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
            desk.add_init_script("try { localStorage.setItem('daedalus.session.panel', '0'); } catch (e) {}")
            page = desk.new_page()
            page.route("**/api/**", stub)
            shot(page, "composer-queued", f"agents/{S1}", wait=".steers .steer .steer-now")
            desk.close()
            # The narrowest phone, idle, with a model whose name does not fit: the name is cut inside
            # its pill, the effort beside it and the mode chip keep their words.
            stub.running = False  # type: ignore[attr-defined]
            stub.model = "DeepSeek Flash long-context preview"  # type: ignore[attr-defined]
            narrow = browser.new_context(viewport={"width": 360, "height": 780}, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
            page = narrow.new_page()
            page.route("**/api/**", stub)
            shot(page, "phone-composer-long-model", f"agents/{S1}", wait=".composer .model-select .model-effort")
            narrow.close()
            browser.close()
    finally:
        stub.yagni = False  # type: ignore[attr-defined]
        stub.running = False  # type: ignore[attr-defined]
        stub.queued = []  # type: ignore[attr-defined]
        stub.model = None  # type: ignore[attr-defined]
    return UNHANDLED.report()


if __name__ == "__main__":
    # Before anything is driven: is the address the built app, or whatever else holds the port?
    expect_app(BASE)
    only = os.environ.get("ONLY")
    sys.exit(run_modes() if only == "modes" else run_browser() if only == "browser" else run_harnesses() if only == "harnesses" else run_staff() if only == "staff" else run_details() if only == "details" else run_terminals() if only == "terminals" else run_focus() if only == "focus" else run_phone() if only == "phone" else run_main() if only == "main" else run_notifications() if only == "notifications" else run_composer() if only == "composer" else run_voice() if only == "voice" else agents_shots() if only == "agents" else run_workspace() if only == "workspace" else run())
