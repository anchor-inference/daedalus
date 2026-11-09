"""Manage the operator's secrets in a real browser: the composer's Secret sheet as a small manager, on a
desktop and on a phone, and the same manager in Settings → Security.

The manager's logic has unit tests (miniapp/src/secrets.test.tsx); this is what only exists once it is
drawn against a host: every row masked and the list never carrying a value, the eye fetching one value on
its own request and hiding it again (on a second press and by itself after half a minute), Copy going
through the same request, an edit in place, an add, a removal warned about and taken back with Undo and
then sent when the undo runs out, and attaching still the first thing a row offers. The values are the
stub's invented one (``api_stub.FAKE_SECRET_VALUE``). ``SHOTS=dir`` keeps a picture of the manager, never
with a value shown.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_secrets_manage.py

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
import api_stub  # noqa: E402
from api_stub import DEFAULT_APP, FAKE_SECRET_VALUE, expect_app, folders  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402  the record the installation below writes its misses to
from screenshots import stub as installation  # noqa: E402  the same invented installation the pictures are taken of

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
LANG = os.environ.get("LANG_UI", "en")
SESSION = "sess-1"
NEW_VALUE = "another-fake-value"


def message(seq: int, role: str, text: str, **over: object) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "headline": "", "text": text, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": f"2026-11-05T12:00:{seq % 60:02d}+00:00", "model": "", "provider": "", "fallback": None, **over}


MESSAGES = [message(1, "user", "Check the router's firmware."), message(2, "assistant", "I need the admin password to log in.")]
SENT: list[dict] = []


def seed() -> None:
    """A chat in a project with two secrets of its own, one of the project's, and one of another chat."""
    store = api_stub.FakeSecrets()
    store.chat = SESSION
    store.project = {"id": "p", "name": "Home network"}
    store.projects = [{"id": "p", "name": "Home network"}, {"id": "p2", "name": "Release"}]
    router = store.seed("router_admin", scope_title="Router", note="ISP router web admin, user admin", used="2026-11-04T20:10:00Z", uses=3, by="BrowserAct in Router")
    store.seed("wifi", scope_title="Router", note="home Wi-Fi")
    store.seed("nas", scope="project", scope_id="p", scope_title="Home network", note="NAS admin over SSH")
    store.seed("github_token", scope_id="sess-9", scope_title="Release notes", note="repo scope only")
    store.usage[router["id"]] = [{"id": SESSION, "title": "Router"}, {"id": "sess-7", "title": "Firmware update"}]
    api_stub.SECRETS = store


def detail() -> dict:
    return {
        "id": SESSION, "title": "Router", "status": "idle", "error": "", "run_id": None, "workspace": "/workspace", "workspace_name": "ws", "workspace_own": True,
        "workspace_sessions": [], "pending": None, "model": "Claude Opus 5", "provider": "claude",
        "project": {"id": "p", "name": "Home network", "folders": folders("/workspace"), "settings": {"snapshots": True}},
        "configured_model": "claude-opus-5", "effective_model": "claude-opus-5", "fallback": None, "thinking": False, "reasoning_effort": "", "yagni": False, "brief": "",
        "tools_off": [], "loop": None, "services": [], "subagents": [], "usage": {}, "context": None, "messages": MESSAGES,
    }


def stub(route) -> None:  # type: ignore[no-untyped-def]
    req = route.request
    rel = req.url.split("?", 1)[0]
    rel = rel[rel.index("/api/"):]

    def answer(body: object, status: int = 200) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    if rel == "/api/secrets" or rel.startswith("/api/secrets/"):
        # The installation below answers every write it does not know with an empty success; the
        # secrets go to the stub's store first, as the host's routes would take them.
        query = req.url.split("?", 1)[1] if "?" in req.url else ""
        session_id = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair).get("session_id", "")
        answered = api_stub.SECRETS.answer(req.method, rel, json.loads(req.post_data) if req.post_data else None, session_id)
        if answered is not None:
            return answer(answered[1], answered[0])
    if rel.endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if rel == f"/api/sessions/{SESSION}/messages" and req.method == "POST":
        data = json.loads(req.post_data or "{}")
        SENT.append(data)
        return answer({"run_id": "r2", "receipt": {"status": "queued"}})
    if req.method == "GET" and rel == f"/api/sessions/{SESSION}":
        return answer(detail())
    if req.method == "GET" and rel.startswith(f"/api/sessions/{SESSION}/"):
        return answer([])
    return installation(route)


def wait_for(page: Page, check, what: str, problems: list[str], seconds: float = 10) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            problems.append(f"{what}: never happened")
            return False
        page.wait_for_timeout(50)
    return True


def shot(page: Page, name: str) -> None:
    if not SHOTS:
        return
    page.wait_for_timeout(450)  # past a sheet's slide, so the picture is of the sheet and not of its way in
    if FAKE_SECRET_VALUE in page.content() or NEW_VALUE in page.content():
        raise AssertionError(f"{name}: a value is on screen; the pictures never show one")
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    page.screenshot(path=f"{SHOTS}/secrets-manage-{name}-{LANG}.png")


def open_chat(context, scheme: str) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme={scheme}&lang={LANG}")
    page.wait_for_selector(".composer .roundbtn.primary", timeout=15000)
    page.evaluate("() => localStorage.removeItem('daedalus.draft.sess-1')")
    page.wait_for_timeout(300)
    return page


def open_sheet(page: Page, phone: bool) -> None:
    if phone:
        page.locator(".composer .iconbtn.plus").click()
        page.wait_for_selector(".ph-plus-sheet", timeout=5000)
        page.locator(".ph-plus-sheet .ph-mrow").filter(has_text="Secret" if LANG == "en" else "Секрет").click()
    else:
        page.locator(".composer .plus").click()
        page.wait_for_selector(".plus-menu", timeout=5000)
        page.locator(".plus-menu [role='menuitem']").last.click()
    page.locator(".secret-sheet .secret-row").first.wait_for(timeout=5000)


def row(page: Page, name: str):  # type: ignore[no-untyped-def]
    return page.locator(f".secret-sheet .secret-row[data-secret='{name}'], .secrets-card .secret-row[data-secret='{name}']")


def masked(page: Page, where: str, problems: list[str]) -> None:
    dots = page.locator(".secret-row-dots").all_inner_texts()
    if not dots or any(set(text.strip()) != {"•"} for text in dots):
        problems.append(f"{where}: a row is not masked ({dots})")
    if FAKE_SECRET_VALUE in page.content():
        problems.append(f"{where}: a value is in the page before anything was revealed")


def desktop(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    seed()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    context.clock.install()
    page = open_chat(context, "dark")
    open_sheet(page, phone=False)
    store = api_stub.SECRETS

    # The list: the chat's and its project's first, each with Attach; another chat's folded below.
    names = page.locator(".secret-sheet .secrets-group:not(.secrets-others) .secret-row").evaluate_all("rows => rows.map(r => r.dataset.secret)")
    if names != ["router_admin", "wifi", "nas"]:
        problems.append(f"desktop: the chat's secrets are {names}")
    if row(page, "github_token").count() != 0:
        problems.append("desktop: another chat's secret is not folded away")
    if page.locator(".secret-sheet .secret-row-attach").count() != 3:
        problems.append("desktop: not every secret of this chat offers Attach")
    masked(page, "desktop", problems)
    shot(page, "desktop-dark")

    # Reveal: one value on its own request, hidden on a second press and by itself after half a minute.
    row(page, "router_admin").locator(".secret-reveal").click()
    wait_for(page, lambda: FAKE_SECRET_VALUE in row(page, "router_admin").inner_text(), "desktop: the eye showing the value", problems)
    if store.revealed[-1:] != [("s1", "reveal")]:
        problems.append(f"desktop: the reveal did not ask the host for one value ({store.revealed})")
    row(page, "router_admin").locator(".secret-reveal").click()
    wait_for(page, lambda: FAKE_SECRET_VALUE not in page.content(), "desktop: a second press hiding the value", problems)
    row(page, "router_admin").locator(".secret-reveal").click()
    wait_for(page, lambda: FAKE_SECRET_VALUE in page.content(), "desktop: the eye showing it again", problems)
    context.clock.fast_forward(29_000)
    page.wait_for_timeout(100)
    if FAKE_SECRET_VALUE not in page.content():
        problems.append("desktop: the value hid before half a minute")
    context.clock.fast_forward(2_000)
    wait_for(page, lambda: FAKE_SECRET_VALUE not in page.content(), "desktop: the value hiding by itself after 30 s", problems)

    # Copy: the same request, the clipboard holds the value, the page never draws it.
    row(page, "wifi").locator(".secret-copy").click()
    wait_for(page, lambda: store.revealed[-1:] == [("s2", "copy")], "desktop: Copy asking the host for the value", problems)
    page.wait_for_timeout(200)
    clipboard = page.evaluate("() => navigator.clipboard.readText()")
    if clipboard != FAKE_SECRET_VALUE:
        problems.append(f"desktop: the clipboard holds {clipboard!r}")
    if FAKE_SECRET_VALUE in page.content():
        problems.append("desktop: Copy drew the value")

    # Edit in place: a rename and a new note, and a new value without ever showing the old one.
    row(page, "wifi").locator(".secret-edit-open").click()
    page.locator("#secret-edit-s2-name").fill("home_wifi")
    page.locator("#secret-edit-s2-value").fill(NEW_VALUE)
    page.locator("#secret-edit-s2-note").fill("the 5 GHz network")
    if FAKE_SECRET_VALUE in page.content():
        problems.append("desktop: the edit form fetched the current value")
    row(page, "wifi").locator(".btn.primary").click()
    wait_for(page, lambda: row(page, "home_wifi").count() == 1, "desktop: the renamed row", problems)
    if store.patched[-1:] != [("s2", {"name": "home_wifi", "value": NEW_VALUE, "note": "the 5 GHz network"})]:
        problems.append(f"desktop: the edit sent {store.patched}")
    if NEW_VALUE in page.content():
        problems.append("desktop: the new value stayed on the page after saving")

    # Remove: the warning names the conversations, Undo brings it back, and left alone it goes.
    row(page, "router_admin").locator(".secret-remove").click()
    page.locator(".secret-confirm .warn").wait_for(timeout=5000)
    warning = page.locator(".secret-confirm").inner_text()
    if "Firmware update" not in warning:
        problems.append(f"desktop: the removal does not say which conversations name it ({warning!r})")
    shot(page, "desktop-remove")
    page.locator(".secret-remove-yes").click()
    wait_for(page, lambda: row(page, "router_admin").count() == 0, "desktop: the removed row leaving", problems)
    page.locator(".toast button").first.click()
    wait_for(page, lambda: row(page, "router_admin").count() == 1, "desktop: Undo bringing it back", problems)
    context.clock.fast_forward(7_000)
    page.wait_for_timeout(150)
    if store.deleted:
        problems.append(f"desktop: an undone removal still reached the host ({store.deleted})")
    row(page, "router_admin").locator(".secret-remove").click()
    page.locator(".secret-remove-yes").click()
    wait_for(page, lambda: row(page, "router_admin").count() == 0, "desktop: removed again", problems)
    context.clock.fast_forward(7_000)
    wait_for(page, lambda: store.deleted == ["s1"], "desktop: the removal going out when the undo ran out", problems)

    # Attach is still the first thing: a press closes the sheet with a chip on the draft.
    row(page, "nas").locator(".secret-row-attach").click()
    page.locator(".secret-sheet").wait_for(state="detached", timeout=5000)
    chips = page.locator(".composer .attachments .secret-chip").all_inner_texts()
    if [c.strip() for c in chips] != ["nas"]:
        problems.append(f"desktop: attaching left the chips {chips}")

    # Add from the manager, saved and attached in one press.
    open_sheet(page, phone=False)
    if row(page, "nas").locator(".secret-row-attached").count() != 1:
        problems.append("desktop: a secret already on the draft is offered again")
    page.locator(".secret-sheet .secrets-add-open").click()
    page.locator("#secret-name").fill("Printer admin")
    page.locator("#secret-value").fill(NEW_VALUE)
    page.locator("#secret-note").fill("office printer")
    page.locator(".secret-add .btn.primary").click()
    page.locator(".secret-sheet").wait_for(state="detached", timeout=5000)
    chips = [c.strip() for c in page.locator(".composer .attachments .secret-chip").all_inner_texts()]
    if chips != ["nas", "printer_admin"]:
        problems.append(f"desktop: the added secret is not on the draft ({chips})")
    if NEW_VALUE in page.content():
        problems.append("desktop: the added value stayed in the page")

    # The message names the secrets and carries no value.
    page.locator(".composer .composer-field").fill("Print the firmware notes")
    page.locator(".composer .roundbtn.primary").click()
    if wait_for(page, lambda: bool(SENT), "desktop: the message going out", problems):
        if SENT[-1].get("secrets") != ["nas", "printer_admin"] or NEW_VALUE in json.dumps(SENT[-1]) or FAKE_SECRET_VALUE in json.dumps(SENT[-1]):
            problems.append(f"desktop: the message is {SENT[-1]}")
    context.close()
    return problems


def light(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    seed()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="light")
    page = open_chat(context, "light")
    open_sheet(page, phone=False)
    page.locator(".secret-sheet .secrets-others-toggle").click()
    row(page, "github_token").wait_for(timeout=5000)
    if row(page, "github_token").locator(".secret-row-attach").count():
        problems.append("light: another chat's secret offers Attach")
    masked(page, "light", problems)
    shot(page, "desktop-light")
    page.locator(".secret-sheet .secrets-add-open").click()
    page.locator("#secret-name").fill("Router root")
    page.locator("#secret-note").fill("the root account")
    shot(page, "desktop-add")
    context.close()
    return problems


def phone(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    seed()
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_chat(context, "dark")
    open_sheet(page, phone=True)
    if page.locator(".secret-sheet.ph-sheet").count() != 1:
        problems.append("phone: the manager is not a bottom sheet")
    masked(page, "phone", problems)
    for button in page.locator(".secret-sheet .secret-row-tools .iconbtn").all()[:4]:
        box = button.bounding_box()
        if not box or box["height"] < 44 or box["width"] < 44:
            problems.append(f"phone: a row's button is under 44 px ({box})")
            break
    width = page.evaluate("() => Math.max(document.documentElement.scrollWidth, document.querySelector('.secret-sheet .sheet-body').scrollWidth)")
    sheet_width = page.evaluate("() => document.querySelector('.secret-sheet .sheet-body').clientWidth")
    if width > 391 or page.evaluate("() => { const b = document.querySelector('.secret-sheet .sheet-body'); return b.scrollWidth > b.clientWidth + 1; }"):
        problems.append(f"phone: the sheet scrolls sideways ({width} px in {sheet_width})")
    shot(page, "phone")
    row(page, "wifi").locator(".secret-reveal").click()
    wait_for(page, lambda: FAKE_SECRET_VALUE in page.content(), "phone: the eye showing the value", problems)
    row(page, "wifi").locator(".secret-reveal").click()
    wait_for(page, lambda: FAKE_SECRET_VALUE not in page.content(), "phone: hiding it again", problems)
    row(page, "wifi").locator(".secret-row-attach").click()
    page.locator(".secret-sheet").wait_for(state="detached", timeout=5000)
    chips = [c.strip() for c in page.locator(".composer .attachments .secret-chip").all_inner_texts()]
    if chips != ["wifi"]:
        problems.append(f"phone: attaching left the chips {chips}")
    context.close()
    return problems


def settings(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    seed()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/settings/security?token=t&scheme=dark&lang={LANG}")
    page.locator(".secrets-card .secret-row").first.wait_for(timeout=15000)
    count = page.locator(".secrets-card .secret-row").count()
    if count != 4:
        problems.append(f"settings: the manager lists {count} secrets, not every one")
    if page.locator(".secrets-card .secret-row-attach").count():
        problems.append("settings: a row offers Attach where there is no message")
    masked(page, "settings", problems)
    page.locator(".secrets-card").scroll_into_view_if_needed()
    if SHOTS:
        page.wait_for_timeout(300)
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.locator(".secrets-card").screenshot(path=f"{SHOTS}/secrets-manage-settings-{LANG}.png")
    page.locator(".secrets-card .secrets-add-open").click()
    page.locator("#secret-name").fill("Backup key")
    page.locator("#secret-value").fill(NEW_VALUE)
    page.locator(".secrets-card .secret-add .btn.primary").click()
    wait_for(page, lambda: page.locator(".secrets-card .secret-row[data-secret='backup_key']").count() == 1, "settings: the added secret in the list", problems)
    added = next((r for r in api_stub.SECRETS.items if r["name"] == "backup_key"), None)
    if not added or added["scope"] != "project" or added["scope_id"] != "p":
        problems.append(f"settings: the secret was not filed under the first project ({added})")
    context.close()
    return problems


def run() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        try:
            problems = desktop(browser) + light(browser) + phone(browser) + settings(browser)
        finally:
            browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("ok: the secrets manager lists masked, reveals and hides, copies, edits, adds, removes with undo and still attaches, on a desktop, a phone and in Settings")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
