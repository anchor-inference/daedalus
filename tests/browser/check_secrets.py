"""Hand the agent a secret from the composer in a real browser, on a desktop and on a phone.

The form's logic has a unit test (miniapp/src/secrets.test.tsx); this is what only exists once it is drawn
against a host: the "+" offers Secret (a menu item on a desktop, a row of the sheet on a phone), the form
masks the value, the value goes to ``POST /api/secrets`` on its own and nowhere else, the draft carries a
chip with the name, the message names the secret and never holds the value, and the sent message shows
the chip. ``SHOTS=dir`` keeps a picture of each step.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_secrets.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

UNHANDLED = Unhandled()
BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
LANG = os.environ.get("LANG_UI", "en")
SESSION = "sess-1"
VALUE = "Tr0ub4dor-and-3\nsecond line"


def message(seq: int, role: str, text: str, **over: object) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "headline": "", "text": text, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": f"2026-10-08T12:00:{seq % 60:02d}+00:00", "model": "", "provider": "", "fallback": None, **over}


class Host:
    """The host's half: the session, the secrets it keeps, and every write the app made."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, str, object]] = []
        self.secrets: list[dict] = []
        self.messages = [message(1, "user", "Check the router's firmware."), message(2, "assistant", "I need the admin password to log in.")]

    def detail(self) -> dict:
        return {
            "id": SESSION, "title": "Router", "status": "idle", "error": "", "run_id": None, "workspace": "/workspace", "workspace_name": "ws", "workspace_own": True,
            "workspace_sessions": [], "pending": None, "model": "Claude Opus 5", "provider": "claude",
            "project": {"id": "p", "name": "Home network", "folders": folders("/workspace"), "settings": {"snapshots": True}},
            "configured_model": "claude-opus-5", "effective_model": "claude-opus-5", "fallback": None, "thinking": False, "reasoning_effort": "", "mode": "", "yagni": False, "brief": "",
            "tools_off": [], "loop": None, "services": [], "subagents": [], "usage": {}, "context": None, "messages": self.messages,
        }


HOST = Host()


def stub(route) -> None:  # type: ignore[no-untyped-def]
    req = route.request
    rel = req.url.split("?", 1)[0]
    rel = rel[rel.index("/api/"):]

    def answer(body: object, status: int = 200) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    if rel.endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if req.method != "GET":
        data = json.loads(req.post_data) if req.post_data else None
        HOST.posted.append((req.method, rel, data))
        if rel == "/api/secrets" and isinstance(data, dict):
            name = str(data["name"])
            kept = {"id": f"s{len(HOST.secrets) + 1}", "name": name, "scope": data["scope"], "scope_id": SESSION, "note": data.get("note", ""), "placeholder": f"«secret:{name}»",
                    "env": "DAEDALUS_SECRET_" + name.upper(), "created_at": "2026-10-08T12:00:00Z", "updated_at": "2026-10-08T12:00:00Z", "last_used_at": None, "last_used_by": "", "uses": 0, "readable": True}
            HOST.secrets.append(kept)
            return answer(kept)
        if rel.startswith("/api/secrets/") and req.method == "DELETE":
            HOST.secrets = [s for s in HOST.secrets if s["id"] != rel.rsplit("/", 1)[1]]
            return answer({"ok": True})
        if rel == f"/api/sessions/{SESSION}/messages" and isinstance(data, dict):
            seq = len(HOST.messages) + 1
            HOST.messages = HOST.messages + [message(seq, "user", str(data.get("text", "")), client_message_id=data.get("client_message_id"), secrets=[{"name": n, "scope": "session"} for n in data.get("secrets") or []] or None)]
            return answer({"run_id": "r2", "receipt": {"status": "consumed"}})
        return answer({})
    if rel == "/api/auth/me":
        return answer({"user_id": 1, "via": "token"})
    if rel == "/api/secrets":
        return answer({"secrets": HOST.secrets, "project_id": "p", "project_name": "Home network"})
    if rel == f"/api/sessions/{SESSION}":
        return answer(HOST.detail())
    if rel.startswith(f"/api/sessions/{SESSION}/"):
        return answer([])
    if rel == "/api/settings":
        return answer({"model": {"preset": "opus"}, "presets": {"opus": {"provider": "claude", "model": "claude-opus-5", "label": "Claude Opus 5", "thinking": False, "reasoning_effort": "", "images": True, "context_window": 200000, "max_output_tokens": 32000}}})
    if rel == "/api/sessions":
        return answer({"sessions": [], "projects": []})
    if rel.startswith("/api/usage/provider/"):
        return answer({"provider": "claude", "today": {"calls": 0}, "subscription": None, "balance": None})
    if fulfil_shared(route):
        return
    UNHANDLED.record(rel)
    answer([])


def writes(path: str) -> list[object]:
    return [data for _method, rel, data in HOST.posted if rel == path]


def wait_for(page: Page, check, what: str, problems: list[str]) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + 10
    while not check():
        if time.monotonic() > deadline:
            problems.append(f"{what}: never happened")
            return False
        page.wait_for_timeout(50)
    return True


def shot(page: Page, name: str) -> None:
    if SHOTS:
        page.wait_for_timeout(450)  # past a sheet's slide, so the picture is of the sheet and not of its way in
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=f"{SHOTS}/secret-{name}-{LANG}.png")


def fill_form(page: Page, where: str, problems: list[str]) -> None:
    sheet = page.locator(".secret-sheet")
    sheet.wait_for(timeout=5000)
    # The project choice appears once the chat's secrets and project are read.
    page.locator(".secret-sheet [role='radio']").first.wait_for(timeout=5000)
    if page.locator(".secret-sheet [role='radio']").count() != 2:
        problems.append(f"{where}: the form does not offer this chat and the project")
    page.locator("#secret-name").fill("Router admin")
    if "«secret:router_admin»" not in sheet.inner_text():
        problems.append(f"{where}: the form does not say what the agent will see")
    page.locator("#secret-value").fill(VALUE)
    masked = page.evaluate("() => getComputedStyle(document.querySelector('#secret-value')).webkitTextSecurity")
    if masked != "disc":
        problems.append(f"{where}: the value is not masked ({masked})")
    page.locator("#secret-note").fill("ISP router web admin, user admin")
    shot(page, f"form-{where}")
    page.locator(".secret-sheet .btn.primary").click()
    page.locator(".secret-sheet").wait_for(state="detached", timeout=5000)


def send_and_read(page: Page, where: str, problems: list[str]) -> None:
    chip = page.locator(".composer .attachments .secret-chip")
    if chip.count() != 1 or chip.inner_text().strip() != "router_admin":
        problems.append(f"{where}: the draft does not carry the chip ({chip.count()})")
    if VALUE.split("\n")[0] in page.content():
        problems.append(f"{where}: the value is still in the page after it was attached")
    kept = writes("/api/secrets")
    if not kept or not isinstance(kept[-1], dict) or kept[-1].get("value") != VALUE or kept[-1].get("name") != "router_admin" or kept[-1].get("session_id") != SESSION:
        problems.append(f"{where}: the value did not go to the host on its own request ({kept})")
    shot(page, f"draft-{where}")
    page.locator(".composer textarea").fill("Log in and read the firmware version")
    before = len(writes(f"/api/sessions/{SESSION}/messages"))
    page.locator(".composer .roundbtn.primary").click()
    if not wait_for(page, lambda: len(writes(f"/api/sessions/{SESSION}/messages")) > before, f"{where}: the message", problems):
        return
    sent = writes(f"/api/sessions/{SESSION}/messages")[-1]
    if not isinstance(sent, dict) or sent.get("secrets") != ["router_admin"] or VALUE.split("\n")[0] in json.dumps(sent):
        problems.append(f"{where}: the message does not name the secret, or holds its value ({sent})")
    page.wait_for_selector(".msg.user .msg-secrets .secret-chip", timeout=8000)
    # The draft lets go of its chip once the host has confirmed the message, which can be a moment after
    # the transcript shows it.
    wait_for(page, lambda: page.locator(".composer .attachments .secret-chip").count() == 0, f"{where}: the chip leaving the draft after the message went", problems)
    shot(page, f"sent-{where}")


def open_page(context) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme=dark&lang={LANG}")
    page.wait_for_selector(".composer .roundbtn.primary", timeout=15000)
    page.evaluate("() => localStorage.removeItem('daedalus.draft.sess-1')")
    page.wait_for_timeout(300)
    return page


def desktop(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context)
    page.locator(".composer .plus").click()
    page.wait_for_selector(".plus-menu", timeout=5000)
    item = page.locator(".plus-menu [role='menuitem']").last
    if "Secret" not in item.inner_text() and LANG == "en":
        problems.append(f"desktop: the + menu ends with {item.inner_text()!r}, not Secret")
    shot(page, "menu-desktop")
    item.click()
    fill_form(page, "desktop", problems)
    send_and_read(page, "desktop", problems)
    # A secret attached by mistake goes again with its chip, and the host forgets it.
    page.locator(".composer .plus").click()
    page.locator(".plus-menu [role='menuitem']").last.click()
    page.locator(".secret-sheet").wait_for(timeout=5000)
    page.locator("#secret-name").fill("wifi")
    page.locator("#secret-value").fill("wifi-password-9")
    page.locator(".secret-sheet .btn.primary").click()
    page.locator(".composer .attachments .secret-chip").wait_for(timeout=5000)
    before = len([1 for method, rel, _ in HOST.posted if method == "DELETE" and rel.startswith("/api/secrets/")])
    page.locator(".composer .attachments .secret-chip .secret-chip-x").click()
    wait_for(page, lambda: len([1 for method, rel, _ in HOST.posted if method == "DELETE" and rel.startswith("/api/secrets/")]) > before, "desktop: removing a fresh chip takes the secret back", problems)
    context.close()
    return problems


def phone(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    HOST.secrets = []
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_page(context)
    page.locator(".composer .iconbtn.plus").click()
    page.wait_for_selector(".ph-plus-sheet", timeout=5000)
    tiles = page.locator(".ph-plus-sheet .ph-tile").count()
    if tiles != 3:
        problems.append(f"phone: the + sheet has {tiles} tiles; Secret is a row, not a tile")
    row = page.locator(".ph-plus-sheet .ph-mrow", has=page.locator("svg")).filter(has_text="Secret" if LANG == "en" else "Секрет")
    if row.count() != 1:
        problems.append("phone: the + sheet has no Secret row")
        context.close()
        return problems
    box = row.bounding_box()
    if not box or box["height"] < 44:
        problems.append(f"phone: the Secret row is under 44 px ({box})")
    shot(page, "sheet-phone")
    row.click()
    fill_form(page, "phone", problems)
    send_and_read(page, "phone", problems)
    width = page.evaluate("() => document.documentElement.scrollWidth")
    if width > 391:
        problems.append(f"phone: the page scrolls sideways ({width} px)")
    context.close()
    return problems


def run() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        try:
            problems = desktop(browser) + phone(browser)
        finally:
            browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("ok: the secret form, its chip and the message that names it, on a desktop and a phone")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
