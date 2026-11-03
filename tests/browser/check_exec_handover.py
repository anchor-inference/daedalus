"""The step of a command that moved to the background says so, and opens the Jobs list.

A foreground Exec still running at its foreground time is kept by the host as a job instead of being
killed; its result says "moved to the background as job-…". The turn's step for it carries a link,
"moved to background · job-…", in the operator's language, that opens the Jobs tab, and a command
started with background=true carries "in background · job-…" the same way. A command that ended
carries neither. Checked at a desktop's width and a phone's, where the id gives its room to the
command: the link says only "in background", and the rest is in its title.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_exec_handover.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

UNHANDLED = Unhandled()
BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SESSION = "sess-build"
KEPT = "job-3-482913"
STARTED = "job-4-120007"


def message(seq: int, role: str, text: str, **over: object) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "headline": "", "text": text, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": f"2026-11-03T09:00:{seq % 60:02d}+00:00", "model": "", "provider": "", "fallback": None, **over}


def command(seq: int, call_id: str, line: str, output: str, *, error: bool = False) -> list[dict]:
    return [
        message(seq, "assistant", "", tool_calls=[{"id": call_id, "name": "Exec", "arguments": {"command": line}}]),
        message(seq + 1, "tool", "", tool_results=[{"id": call_id, "content": output, "is_error": error}]),
    ]


MESSAGES = [
    message(1, "user", "Build the release and run the tests."),
    *command(2, "c_ls", "ls", "exit_code=0 elapsed=0.0s cwd=/workspace\nMakefile src tests"),
    *command(4, "c_build", "make release", (
        f"still running after 120.0s cwd=/workspace — moved to the background as {KEPT} (pid 4242), the same process carrying on; "
        f"its output so far is below and all of it, with what follows, is in /workspace/.jobs/{KEPT}.log.\n"
        "You are woken with its outcome when it ends: end your turn or get on with other work, and do not poll it or wait for it.\n"
        "[ 12%] Building CXX object src/core.o"
    )),
    *command(6, "c_serve", "npm run dev", f"{STARTED}: running (pid 4300); output in /workspace/.jobs/{STARTED}.log (you are woken with its outcome when it ends)"),
    message(8, "assistant", "The build carries on in the background; I will look at it when it ends."),
]

TASKS = [
    {"id": KEPT, "owner_session_id": SESSION, "parent_run_id": None, "kind": "job", "state": "running", "outcome": None, "exit_code": None,
     "title": "make release", "command": "make release", "where": "local", "started_at": "2026-11-03T08:58:00Z", "last_activity_at": "2026-11-03T09:00:00Z",
     "ended_at": None, "flag": None, "reported": False, "progress": None, "child_session_id": None, "result_ref": f".jobs/{KEPT}.log", "stop_supported": True},
]


def detail() -> dict:
    return {
        "id": SESSION, "title": "Release build", "status": "idle", "error": "", "run_id": None, "workspace": "/workspace",
        "workspace_name": "release", "workspace_own": True, "workspace_sessions": [], "pending": None, "model": "Claude Opus 5", "provider": "claude",
        "project": {"id": "p", "name": "Release", "folders": folders("/workspace"), "settings": {"snapshots": True}},
        "thinking": True, "reasoning_effort": "high", "yagni": False, "brief": "",
        "tools_off": [], "loop": None, "services": [], "subagents": [], "usage": {}, "context": {"tokens": 21000, "window": 200000, "messages": 9, "summaries": 0, "operator_turns": 1},
        "messages": MESSAGES,
    }


def stub(route) -> None:  # type: ignore[no-untyped-def]
    req = route.request
    path = req.url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    if rel.endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if req.method != "GET":
        return route.fulfill(status=200, content_type="application/json", body="{}")
    body: object
    if rel == "/api/auth/me":
        body = {"user_id": 1, "via": "token"}
    elif rel == f"/api/sessions/{SESSION}":
        body = detail()
    elif rel == f"/api/sessions/{SESSION}/tasks":
        body = TASKS
    elif rel.startswith(f"/api/sessions/{SESSION}/"):
        body = []
    elif rel == "/api/settings":
        body = {"model": {"preset": "opus"}, "presets": {"opus": {"provider": "claude", "model": "claude-opus-5", "label": "Claude Opus 5", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 200000, "max_output_tokens": 32000}}}
    elif rel == "/api/sessions":
        body = {"sessions": [], "projects": []}
    elif rel.startswith("/api/usage/provider/"):
        body = {"provider": "claude", "today": {"calls": 4}, "subscription": None, "balance": None}
    elif fulfil_shared(route):
        return
    else:
        UNHANDLED.record(rel)
        body = []
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def open_page(context, lang: str) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".thinking-head, .act", timeout=15000)
    page.wait_for_timeout(300)
    heads = page.locator(".thinking-head[aria-expanded='false']")
    while heads.count():
        heads.first.click()
        page.wait_for_timeout(50)
    # Three commands in a row fold into one "Ran 3 commands" step; the links are on the steps inside.
    groups = page.locator(".act.head")
    for i in range(groups.count()):
        groups.nth(i).click()
        page.wait_for_timeout(50)
    return page


def check(browser, width: int, height: int, lang: str, moved: str, started: str) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    where = f"{width}px {lang}"
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark")
    page = open_page(context, lang)
    links = page.locator(".act .exec-job")
    texts = [links.nth(i).inner_text().strip() for i in range(links.count())]
    print(where, "links:", texts)
    expected = [f"{moved} · {KEPT}", f"{started} · {STARTED}"] if width > 600 else [started, started]
    if texts != expected:
        problems.append(f"{where}: the kept and the started command should each name their job, the finished one nothing ({texts})")
    titles = [links.nth(i).get_attribute("title") or "" for i in range(links.count())]
    if len(titles) == 2 and (KEPT not in titles[0] or STARTED not in titles[1]):
        problems.append(f"{where}: the links' titles do not name their jobs ({titles})")
    details = page.locator(".act .detail")
    shown = [details.nth(i).bounding_box() for i in range(details.count())]
    if any(box is None or box["width"] < 40 for box in shown):
        problems.append(f"{where}: a step's command was squeezed out by the link ({shown})")
    if links.count():
        box = links.first.bounding_box()
        if box is None or box["x"] + box["width"] > width:
            problems.append(f"{where}: the link runs off the screen ({box})")
        links.first.click()
        page.wait_for_timeout(400)
        if KEPT not in page.content() or not page.locator(".jobs").count():
            problems.append(f"{where}: the link did not open the Jobs list with the job in it")
    overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    if overflow > 0:
        problems.append(f"{where}: the page scrolls sideways by {overflow}px")
    context.close()
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        problems += check(browser, 1440, 900, "en", "moved to background", "in background")
        problems += check(browser, 1440, 900, "ru", "перенесено в фон", "в фоне")
        problems += check(browser, 390, 844, "en", "moved to background", "in background")
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
