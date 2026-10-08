"""Does a session say which model really answered?

A run that begins on one model and ends on another used to look exactly like a run that never
moved: the header went on naming the configured model and the answer carried no mark at all. This
check loads a session whose last turn was written by a stand-in and asserts that both places say
so — the header chip names the model in use and the one it replaced, and the turn above the answer
carries a line that opens into the reason.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_model_fallback.py

Exit 0 when both say it. ``SHOTS=1`` also writes the pictures next to this file (``OUT=`` to
put them elsewhere).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared, reveal_composer  # noqa: E402

UNHANDLED = Unhandled()

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
OUT = Path(os.environ.get("OUT", str(Path(__file__).parent)))
SESSION = "sess-1"
CONFIGURED = "claude-opus-5"
STANDBY = "deepseek-flash"


def message(seq: int, role: str, text: str, **over: object) -> dict:
    return {
        "role": role,
        "summary": False,
        "internal": False,
        "origin": "operator" if role == "user" else "",
        "seq": seq,
        "compaction": None,
        "archived": None,
        "headline": "",
        "text": text,
        "thinking": "",
        "tool_calls": [],
        "tool_results": [],
        "created_at": f"2026-09-18T12:00:{seq:02d}+00:00",
        "model": "",
        "provider": "",
        "fallback": None,
        **over,
    }


DETAIL = {
    "id": SESSION,
    "title": "Weekly digest",
    "status": "idle",
    "run_id": None,
    "workspace": "/workspace",
    "workspace_name": "ws",
    "workspace_own": True,
    "workspace_sessions": [],
    "project": {"id": "p", "name": "Project", "folders": folders("/workspace"), "settings": {"snapshots": True}},
    "pending": None,
    "model": CONFIGURED,
    "provider": "claude",
    "configured_model": CONFIGURED,
    "effective_model": STANDBY,
    "effective_provider": "deepseek",
    "fallback": {"from": CONFIGURED, "to": STANDBY, "reason": "rate_limit"},
    "mode": "",
    "brief": "",
    "tools_off": [],
    "loop": None,
    "services": [],
    "subagents": [],
    "usage": {},
    "context": {"tokens": 10, "window": 100000, "messages": 4, "summaries": 0, "operator_turns": 2},
    "messages": [
        message(101, "user", "Summarise what the bakery site did this week."),
        message(102, "assistant", "Three posts went out and the contact form was fixed.", model=CONFIGURED, provider="claude"),
        message(103, "user", "And the week before?"),
        message(
            104,
            "assistant",
            "Two posts, and the price list was rewritten.",
            model=STANDBY,
            provider="deepseek",
            fallback={"from": CONFIGURED, "to": STANDBY, "reason": "rate_limit"},
        ),
    ],
}


def stub(route) -> None:  # type: ignore[no-untyped-def]
    url = route.request.url
    if url.split("?", 1)[0].endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if "auth/me" in url:
        body = json.dumps({"user_id": 1, "via": "token"})
    elif f"/api/sessions/{SESSION}/steer" in url:
        body = "[]"
    elif f"/api/sessions/{SESSION}" in url and "/events" not in url and "/stream" not in url:
        body = json.dumps(DETAIL)
    elif url.split("?", 1)[0].endswith("/api/settings"):
        body = json.dumps({"model": {"preset": "opus"}, "presets": {"opus": {"provider": "claude", "model": CONFIGURED, "label": "Claude Opus 5", "thinking": True}, "flash": {"provider": "deepseek", "model": STANDBY, "label": "DeepSeek Flash", "thinking": False}}})
    elif "unread" in url:
        body = json.dumps({"unread": 0})
    elif "/api/usage/provider/" in url:
        # The usage card beside the chat asks what this session's provider has spent.
        body = json.dumps({"provider": "claude", "spent_usd": 1.2, "calls": 4})
    elif url.rstrip("/").endswith("/api/sessions"):
        body = json.dumps([{"id": SESSION, "title": "Weekly digest", "status": "idle", "created_at": "2026-09-18T12:00:00+00:00", "last_message_at": "2026-09-18T12:00:04+00:00", "run_id": None}])
    else:
        rel = url.split("?", 1)[0]
        rel = rel[rel.index("/api/"):] if "/api/" in rel else ""
        if fulfil_shared(route):
            return
        if rel:
            UNHANDLED.record(rel)
        body = "[]"
    route.fulfill(status=200, content_type="application/json", body=body)


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for name, width, height in [("phone", 390, 844), ("desktop", 1440, 900)]:
            context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=name == "phone", has_touch=name == "phone")
            page = context.new_page()
            page.route("**/api/**", stub)
            page.goto(f"{BASE}/agents/{SESSION}?token=t")
            page.wait_for_selector(".msg.user", timeout=15000)
            page.wait_for_timeout(400)

            # The model is the selector inside the composer, on a phone and on a desk alike; while a
            # stand-in answers, its label names both models and the button is marked.
            selector = page.locator(".composer .model-select").first
            header = selector.evaluate("(el) => el.textContent")
            print(f"{name}: composer selector = {header!r}")
            if STANDBY.split("-")[-1] not in header:
                problems.append(f"{name}: the selector does not name the model that is answering ({header!r})")
            if "opus" not in header:
                problems.append(f"{name}: the selector does not say what it stands in for ({header!r})")
            if not selector.evaluate("(el) => el.classList.contains('attn')"):
                problems.append(f"{name}: the selector is not marked as standing in")
            reveal_composer(page)
            selector.click()
            page.wait_for_selector(".model-list", timeout=5000)
            restore = page.locator(".model-list .model-row.restore")
            if not restore.count():
                problems.append(f"{name}: the open list does not offer the way back to the configured model")
            elif CONFIGURED.split("-")[-1] not in restore.first.inner_text():
                problems.append(f"{name}: the way back does not name the configured model ({restore.first.inner_text()!r})")
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)

            notes = page.locator(".fallback-note")
            print(f"{name}: {notes.count()} turn(s) marked")
            if notes.count() != 1:
                problems.append(f"{name}: expected exactly one marked turn, found {notes.count()}")
            else:
                line = notes.first.inner_text()
                print(f"{name}: turn chip = {line!r}")
                if STANDBY not in line or CONFIGURED not in line:
                    problems.append(f"{name}: the turn's line does not name both models ({line!r})")
                box = notes.first.bounding_box()
                if box is None or box["x"] < -1 or box["x"] + box["width"] > width + 1:
                    problems.append(f"{name}: the line is outside the viewport ({box})")
                notes.first.locator("button").click()
                page.wait_for_timeout(200)
                detail = notes.first.locator(".sub").inner_text()
                print(f"{name}: reason = {detail!r}")
                if "quota" not in detail:
                    problems.append(f"{name}: opening the line does not give the reason ({detail!r})")
            if os.environ.get("SHOTS"):
                OUT.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(OUT / f"model-fallback-{name}.png"))
            context.close()
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
