"""Manage the operator's secrets in a real browser: the composer's Secret sheet as a wide list-and-pane
manager, on a desktop and on a phone, and the same manager in Settings → Security.

The manager's logic has unit tests (miniapp/src/secrets.test.tsx); this is what only exists once it is
drawn against a host: the three views of a chat's secrets and the "all" view grouped by project, search,
sort, the keyboard (arrows, Enter, `/`, N), every row masked and the page never carrying a value until an
eye is pressed, the eye fetching one value on its own request and hiding it again (on a second press, on
another row, when the sheet closes and by itself after half a minute), Copy through the same request, an
edit, a new value under the old name, a removal warned about and taken back with Undo and then sent when
the undo runs out, attaching and taking off with one press on a row, the question before unsaved edits
are dropped, and a new secret saved and attached in one press. In Settings: every secret as rows of a
table, the open one beside the list once the column has room (900 px) and a sheet over it when not, and
"Add secret" as the section's header button. The values are the stub's invented ones
(``api_stub.FAKE_SECRET_VALUE``). ``SHOTS=dir`` keeps a picture of the manager, never with a value shown.

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
SHEET = ".secret-sheet"


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
    router = store.seed("router_admin", scope_title="Router", note="ISP router web admin, user admin", used="2026-11-04T20:10:00Z", uses=3, by=f"Exec in {SESSION}")
    store.seed("wifi", scope_title="Router", note="home Wi-Fi")
    store.seed("nas", scope="project", scope_id="p", scope_title="Home network", note="NAS admin over SSH", used="2026-11-02T20:10:00Z", uses=1, by="BrowserAct (agent) on https://nas.example")
    store.seed("github_token", scope_id="sess-9", scope_title="Release notes", note="repo scope only")
    store.usage[router["id"]] = [{"id": SESSION, "title": "Router"}, {"id": "sess-7", "title": "Firmware update"}]
    store.audit[router["id"]] = [{"at": "2026-11-04T20:10:00Z", "name": "router_admin", "action": "copy", "via": "token"}]
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
        page.wait_for_selector(".sheet .menu.attach-menu", timeout=5000)
        page.locator(".sheet .attach-menu [data-action='secret']").click()
    else:
        page.locator(".composer .plus").click()
        page.wait_for_selector(".menu.attach-menu", timeout=5000)
        page.locator(".attach-menu [data-action='secret']").click()
    page.locator(f"{SHEET} .secret-row").first.wait_for(timeout=5000)


def row(page: Page, name: str):  # type: ignore[no-untyped-def]
    return page.locator(f"{SHEET} .secret-row[data-secret='{name}'], .secrets-card .secret-row[data-secret='{name}']")


def names(page: Page, scope: str = SHEET) -> list[str]:
    return page.locator(f"{scope} .secret-row").evaluate_all("rows => rows.map(r => r.dataset.secret)")


def pane(page: Page):  # type: ignore[no-untyped-def]
    return page.locator(".secret-pane[data-secret-pane]")


def tab(page: Page, index: int) -> None:
    page.locator(f"{SHEET} .secrets-tabs [role='radio']").nth(index).click()
    page.wait_for_timeout(100)


def masked(page: Page, where: str, problems: list[str]) -> None:
    dots = page.locator(".secret-dots").all_inner_texts()
    if any(set(text.strip()) != {"•"} for text in dots):
        problems.append(f"{where}: a value is not masked ({dots})")
    if FAKE_SECRET_VALUE in page.content():
        problems.append(f"{where}: a value is in the page before anything was revealed")
    # Not in a name a tooltip or a screen reader reads, nor anywhere the page keeps for later.
    kept = page.evaluate("""(v) => [...document.querySelectorAll('*')].some(e => [...e.attributes].some(a => a.value.includes(v)))
        || Object.keys(localStorage).some(k => (localStorage.getItem(k) || '').includes(v))
        || Object.keys(sessionStorage).some(k => (sessionStorage.getItem(k) || '').includes(v))
        || location.href.includes(v)""", FAKE_SECRET_VALUE)
    if kept:
        problems.append(f"{where}: a value is in an attribute, the address or the browser's storage")


def desktop(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    seed()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    context.clock.install()
    page = open_chat(context, "dark")
    open_sheet(page, phone=False)
    store = api_stub.SECRETS

    box = page.locator(f"{SHEET}").bounding_box()
    if not box or box["width"] < 960 or box["height"] < 600:
        problems.append(f"desktop: the sheet is not a wide, tall one ({box})")
    # The chat's own first, the most recently used on top; the project's and all one press away.
    if names(page) != ["router_admin", "wifi"]:
        problems.append(f"desktop: the chat's own secrets are {names(page)}")
    heights = page.locator(f"{SHEET} .secret-row-open").evaluate_all("rows => rows.map(r => r.getBoundingClientRect().height)")
    if any(round(h) < 44 or round(h) > 48 for h in heights):
        problems.append(f"desktop: a row is not 44-48 px ({heights})")
    if pane(page).get_attribute("data-secret-pane") != "router_admin":
        problems.append("desktop: the first row is not open beside the list")
    tab(page, 1)
    if names(page) != ["nas"]:
        problems.append(f"desktop: the project's view lists {names(page)}")
    tab(page, 2)
    # Grouped by project: this chat's and its project's under the project, the other chat (in no
    # project the list knows) with the chats outside one.
    groups = page.locator(f"{SHEET} .secrets-group").evaluate_all("gs => gs.map(g => g.dataset.group)")
    if names(page) != ["router_admin", "nas", "wifi", "github_token"] or groups != ["p", "none"]:
        problems.append(f"desktop: the all view is {names(page)} in groups {groups}")
    if row(page, "github_token").locator(".secret-row-attach").count():
        problems.append("desktop: another chat's secret offers to be attached")
    masked(page, "desktop", problems)
    shot(page, "desktop-all")

    # Search and sort.
    page.locator(f"{SHEET} .secrets-search input").fill("ssh")
    wait_for(page, lambda: names(page) == ["nas"], "desktop: the search narrowing to the note", problems)
    page.locator(f"{SHEET} .secrets-search input").fill("nothing at all")
    wait_for(page, lambda: page.locator(f"{SHEET} .secrets-empty[data-empty='search']").count() == 1, "desktop: the empty search saying so", problems)
    page.locator(f"{SHEET} .secrets-search input").fill("")
    tab(page, 0)
    page.locator(f"{SHEET} .secrets-sort .dropdown-btn").click()
    page.locator("[role='option'][data-value='name']").click()
    wait_for(page, lambda: names(page) == ["router_admin", "wifi"], "desktop: sorting by name", problems)

    # The keyboard: the arrows walk the rows, Enter opens one, / searches, N starts a new secret.
    row(page, "router_admin").locator(".secret-row-open").focus()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    wait_for(page, lambda: pane(page).get_attribute("data-secret-pane") == "wifi", "desktop: ↓ and Enter opening the next row", problems)
    page.keyboard.press("/")
    if not page.evaluate("() => document.activeElement?.closest('.secrets-search') !== null"):
        problems.append("desktop: / does not focus the search")
    row(page, "wifi").locator(".secret-row-open").focus()
    page.keyboard.press("n")
    wait_for(page, lambda: page.locator(f"{SHEET} .secret-add #secret-name").count() == 1, "desktop: N opening the new-secret form", problems)
    page.locator(f"{SHEET} .secret-add .secret-cancel").click()

    # Reveal: one value on its own request; hidden on a second press, on another row, and after 30 s.
    row(page, "router_admin").locator(".secret-row-open").click()
    pane(page).locator(".secret-reveal").click()
    wait_for(page, lambda: FAKE_SECRET_VALUE in pane(page).inner_text(), "desktop: the eye showing the value", problems)
    if store.revealed[-1:] != [("s1", "reveal")]:
        problems.append(f"desktop: the reveal did not ask the host for one value ({store.revealed})")
    pane(page).locator(".secret-reveal").click()
    wait_for(page, lambda: FAKE_SECRET_VALUE not in page.content(), "desktop: a second press hiding the value", problems)
    pane(page).locator(".secret-reveal").click()
    wait_for(page, lambda: FAKE_SECRET_VALUE in page.content(), "desktop: the eye showing it again", problems)
    row(page, "wifi").locator(".secret-row-open").click()
    wait_for(page, lambda: FAKE_SECRET_VALUE not in page.content(), "desktop: another row hiding the value", problems)
    row(page, "router_admin").locator(".secret-row-open").click()
    pane(page).locator(".secret-reveal").click()
    wait_for(page, lambda: FAKE_SECRET_VALUE in page.content(), "desktop: the eye showing it a third time", problems)
    context.clock.fast_forward(29_000)
    page.wait_for_timeout(100)
    if FAKE_SECRET_VALUE not in page.content():
        problems.append("desktop: the value hid before half a minute")
    context.clock.fast_forward(2_000)
    wait_for(page, lambda: FAKE_SECRET_VALUE not in page.content(), "desktop: the value hiding by itself after 30 s", problems)

    # Usage: the tool and the chat that used it, the conversations that name it, its trail.
    usage = pane(page).inner_text()
    if "Exec" not in usage or "Firmware update" not in usage:
        problems.append(f"desktop: the pane does not say where the secret is used ({usage!r})")

    # Copy: the same request; the clipboard holds the value, a line says so, and the page never draws it.
    row(page, "wifi").locator(".secret-row-open").click()
    pane(page).locator(".secret-copy").click()
    wait_for(page, lambda: store.revealed[-1:] == [("s2", "copy")], "desktop: Copy asking the host for the value", problems)
    page.wait_for_timeout(200)
    clipboard = page.evaluate("() => navigator.clipboard.readText()")
    if clipboard != FAKE_SECRET_VALUE:
        problems.append(f"desktop: the clipboard holds {clipboard!r}")
    if FAKE_SECRET_VALUE in page.content():
        problems.append("desktop: Copy drew the value")
    if page.locator(".toast").count() == 0:
        problems.append("desktop: Copy said nothing")

    # Unsaved edits: another row asks first; keeping them stays, dropping them moves on.
    page.locator("#secret-edit-s2-note").fill("the 5 GHz network")
    row(page, "router_admin").locator(".secret-row-open").click()
    page.locator(".dialog").wait_for(timeout=5000)
    page.locator(".dialog .dialog-actions .btn").first.click()
    if pane(page).get_attribute("data-secret-pane") != "wifi":
        problems.append("desktop: keeping the edits did not keep the open secret")

    # Edit and a new value under the same name, in one save.
    page.locator("#secret-edit-s2-name").fill("home_wifi")
    pane(page).locator(".secret-rotate-open").click()
    page.locator("#secret-edit-s2-value").fill(NEW_VALUE)
    if FAKE_SECRET_VALUE in page.content() or NEW_VALUE in page.content():
        problems.append("desktop: the edit drew the current or the new value into the page")
    pane(page).locator(".secret-save").click()
    wait_for(page, lambda: row(page, "home_wifi").count() == 1, "desktop: the renamed row", problems)
    if store.patched[-1:] != [("s2", {"name": "home_wifi", "note": "the 5 GHz network", "value": NEW_VALUE})]:
        problems.append(f"desktop: the edit sent {store.patched}")
    if NEW_VALUE in page.content():
        problems.append("desktop: the new value stayed on the page after saving")

    # Remove: the warning says where and which conversations, Undo brings it back, and left alone it goes.
    row(page, "router_admin").locator(".secret-row-open").click()
    pane(page).locator(".secret-remove").click()
    page.locator(".dialog .secret-warn").wait_for(timeout=5000)
    warning = page.locator(".dialog").inner_text()
    if "Firmware update" not in warning:
        problems.append(f"desktop: the removal does not say which conversations name it ({warning!r})")
    shot(page, "desktop-remove")
    page.locator(".dialog .dialog-actions .btn.danger").click()
    wait_for(page, lambda: row(page, "router_admin").count() == 0, "desktop: the removed row leaving", problems)
    page.locator(".toast button").first.click()
    wait_for(page, lambda: row(page, "router_admin").count() == 1, "desktop: Undo bringing it back", problems)
    context.clock.fast_forward(7_000)
    page.wait_for_timeout(150)
    if store.deleted:
        problems.append(f"desktop: an undone removal still reached the host ({store.deleted})")
    row(page, "router_admin").locator(".secret-row-open").click()
    pane(page).locator(".secret-remove").click()
    page.locator(".dialog .dialog-actions .btn.danger").click()
    wait_for(page, lambda: row(page, "router_admin").count() == 0, "desktop: removed again", problems)
    context.clock.fast_forward(7_000)
    wait_for(page, lambda: store.deleted == ["s1"], "desktop: the removal going out when the undo ran out", problems)

    # Attach with one press on a row: the sheet stays, the draft gains the chip; a second press takes it off.
    tab(page, 1)
    row(page, "nas").locator(".secret-row-attach").click()
    wait_for(page, lambda: [c.strip() for c in page.locator(".composer .attachments .secret-chip").all_inner_texts()] == ["nas"], "desktop: a row's press attaching", problems)
    if page.locator(SHEET).count() != 1:
        problems.append("desktop: attaching from a row closed the sheet")
    if row(page, "nas").locator(".secret-row-attach").get_attribute("aria-pressed") != "true":
        problems.append("desktop: the attached row is not marked")

    # A revealed value goes with the sheet.
    row(page, "nas").locator(".secret-row-open").click()
    pane(page).locator(".secret-reveal").click()
    wait_for(page, lambda: FAKE_SECRET_VALUE in page.content(), "desktop: revealing before the close", problems)
    page.keyboard.press("Escape")
    page.locator(SHEET).wait_for(state="detached", timeout=5000)
    if FAKE_SECRET_VALUE in page.content():
        problems.append("desktop: the value outlived the sheet")

    # A new secret, saved and attached in one press, closes the sheet.
    open_sheet(page, phone=False)
    page.locator(f"{SHEET} .secrets-add-open").click()
    page.locator("#secret-name").fill("Printer admin")
    page.locator("#secret-value").fill(NEW_VALUE)
    page.locator("#secret-note").fill("office printer")
    if NEW_VALUE in page.content():
        problems.append("desktop: the typed value is in the page's markup")
    shot(page, "desktop-add")
    page.locator(f"{SHEET} .secret-add .secret-save").click()
    page.locator(SHEET).wait_for(state="detached", timeout=5000)
    chips = [c.strip() for c in page.locator(".composer .attachments .secret-chip").all_inner_texts()]
    if chips != ["nas", "printer_admin"]:
        problems.append(f"desktop: the added secret is not on the draft ({chips})")

    # Unsaved typing asks before the sheet closes.
    open_sheet(page, phone=False)
    page.locator(f"{SHEET} .secrets-add-open").click()
    page.locator("#secret-name").fill("half_typed")
    page.keyboard.press("Escape")
    page.locator(".dialog").wait_for(timeout=5000)
    page.locator(".dialog .dialog-actions .btn.danger").click()
    page.locator(SHEET).wait_for(state="detached", timeout=5000)

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
    tab(page, 2)
    row(page, "github_token").wait_for(timeout=5000)
    if row(page, "github_token").locator(".secret-row-attach").count():
        problems.append("light: another chat's secret offers Attach")
    masked(page, "light", problems)
    shot(page, "desktop-light")
    context.close()
    return problems


def phone(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    seed()
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_chat(context, "dark")
    open_sheet(page, phone=True)
    if page.locator(f"{SHEET}.ph-sheet").count() != 1:
        problems.append("phone: the manager is not a bottom sheet")
    masked(page, "phone", problems)
    heights = page.locator(f"{SHEET} .secret-row-open").evaluate_all("rows => rows.map(r => r.getBoundingClientRect().height)")
    if any(round(h) < 56 for h in heights):
        problems.append(f"phone: a row is under 56 px ({heights})")
    for button in page.locator(f"{SHEET} .secret-row-attach").all()[:2]:
        box = button.bounding_box()
        if not box or round(box["height"]) < 44 or round(box["width"]) < 44:
            problems.append(f"phone: a row's button is under 44 px ({box})")
            break
    # Everything inside the 16 px gutters, and nothing scrolls sideways.
    edges = page.evaluate("""() => [...document.querySelectorAll('.secret-sheet .secrets-search, .secret-sheet .secrets-tabs, .secret-sheet .secret-row-name')]
        .map(e => e.getBoundingClientRect()).map(r => [Math.round(r.left), Math.round(r.right)])""")
    if any(left < 16 or right > 390 - 16 for left, right in edges):
        problems.append(f"phone: something leaves the 16 px gutters ({edges})")
    body = page.evaluate("() => { const b = document.querySelector('.secret-sheet .sheet-body'); return [b.scrollWidth, b.clientWidth]; }")
    if body[0] > body[1] + 1 or page.evaluate("() => document.documentElement.scrollWidth") > 391:
        problems.append(f"phone: the sheet scrolls sideways ({body})")
    shot(page, "phone")
    row(page, "wifi").locator(".secret-row-open").click()
    pane(page).wait_for(timeout=5000)
    if page.locator(f"{SHEET} .secret-row").count():
        problems.append("phone: the open secret is not a view of its own over the list")
    pane(page).locator(".secret-reveal").click()
    wait_for(page, lambda: FAKE_SECRET_VALUE in page.content(), "phone: the eye showing the value", problems)
    pane(page).locator(".secret-pane-back").click()
    wait_for(page, lambda: FAKE_SECRET_VALUE not in page.content(), "phone: going back hiding the value", problems)
    row(page, "wifi").locator(".secret-row-attach").click()
    wait_for(page, lambda: [c.strip() for c in page.locator(".composer .attachments .secret-chip").all_inner_texts()] == ["wifi"], "phone: a row's press attaching", problems)
    context.close()
    return problems


def settings(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    seed()
    # A window whose settings column is under 900 px: the list is a table, a secret opens as a sheet.
    context = browser.new_context(viewport={"width": 1180, "height": 900}, color_scheme="dark")
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
    # "Add secret" is the section's own header button, not a button inside the list.
    if page.locator(".settings-head .secrets-add-open").count() != 1:
        problems.append("settings: Add secret is not the section's header button")
    width = page.evaluate("() => document.querySelector('.secrets-card .secrets-manager').clientWidth")
    layout = page.locator(".secrets-card .secrets-manager").get_attribute("data-layout")
    if width >= 900 or layout != "sheet":
        problems.append(f"settings: a {width} px column drew the {layout} layout, not the table with a sheet")
    if not page.locator(".secrets-card .secrets-cols").is_visible():
        problems.append("settings: the table has no column heads")
    row(page, "wifi").locator(".secret-row-open").click()
    page.locator(".secret-pane-sheet .secret-pane").wait_for(timeout=5000)
    page.keyboard.press("Escape")
    page.locator(".secret-pane-sheet").wait_for(state="detached", timeout=5000)
    context.close()

    # A wide window: the open secret beside the list, the first one open from the start.
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/settings/security?token=t&scheme=dark&lang={LANG}")
    page.locator(".secrets-card .secret-row").first.wait_for(timeout=15000)
    width = page.evaluate("() => document.querySelector('.secrets-card .secrets-manager').clientWidth")
    wait_for(page, lambda: page.locator(".secrets-card .secrets-manager[data-layout='split'] .secret-pane[data-secret-pane]").count() == 1, f"settings: a {width} px column putting the open secret beside the list", problems)
    masked(page, "settings wide", problems)
    if SHOTS:
        page.wait_for_timeout(300)
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=f"{SHOTS}/secrets-manage-settings-{LANG}.png")
    page.locator(".settings-head .secrets-add-open").click()
    page.locator(".secrets-card #secret-name").wait_for(timeout=5000)
    page.locator("#secret-name").fill("Backup key")
    page.locator("#secret-value").fill(NEW_VALUE)
    page.locator(".secrets-card .secret-add .secret-save").click()
    wait_for(page, lambda: page.locator(".secrets-card .secret-row[data-secret='backup_key']").count() == 1, "settings: the added secret in the list", problems)
    added = next((r for r in api_stub.SECRETS.items if r["name"] == "backup_key"), None)
    if not added or added["scope"] != "project" or added["scope_id"] != "p":
        problems.append(f"settings: the secret was not filed under the first project ({added})")
    if NEW_VALUE in page.content():
        problems.append("settings: the added value stayed in the page")
    context.close()
    return problems


def settings_phone(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    seed()
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/settings/security?token=t&scheme=dark&lang={LANG}")
    page.locator(".secrets-card .secret-row").first.wait_for(timeout=15000)
    # The section's button and the card sit inside the same 16 px gutters.
    add = page.locator(".settings-phone-actions .secrets-add-open").bounding_box()
    card = page.locator(".secrets-card .secrets-manager").bounding_box()
    if not add or not card or add["x"] < 16 or add["x"] + add["width"] > 390 - 16 + 0.5 or card["x"] < 16 or card["x"] + card["width"] > 390 - 16 + 0.5:
        problems.append(f"settings phone: Add secret {add} or the list {card} leaves the 16 px gutters")
    if page.evaluate("() => document.documentElement.scrollWidth") > 391:
        problems.append("settings phone: the page scrolls sideways")
    page.locator(".settings-phone-actions .secrets-add-open").click()
    page.locator(".secret-pane-sheet #secret-name").wait_for(timeout=5000)
    context.close()
    return problems


def run() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        try:
            problems = desktop(browser) + light(browser) + phone(browser) + settings(browser) + settings_phone(browser)
        finally:
            browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("ok: the secrets manager lists, searches, sorts, reveals and hides, copies, edits, replaces, removes with undo, attaches and guards unsaved edits, on a desktop, a phone and in Settings")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
