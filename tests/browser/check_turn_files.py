"""The files under an answer when a turn wrote many of them, in a real browser.

A turn that writes two dozen files used to draw two dozen full-width cards under its answer, each
with the file's absolute path. This drives a turn that wrote 23 files, edited a few of them and sent
five to the operator, and refuses anything but the convention the chat apps share: the sent files are
cards, three of them and a "+2 more"; everything the turn merely changed is one compact line that
opens into a list of paths relative to the workspace; a file already shown as a card is not listed
again. On a phone the line opens a sheet of rows, and a long press on a row offers its actions.

With the installation native and the page on the same machine, a row also offers to reveal the file
in the system's file manager, which asks the host (``POST /api/reveal``) with the workspace-relative
path; anywhere else the action is not drawn at all.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8701 /tmp/app-root &
    APP_URL=http://127.0.0.1:8701/app python3 tests/browser/check_turn_files.py

SHOTS=<directory> also saves the pictures this looks at. Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import CAPABILITIES, S1, UNHANDLED, ago, detail, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
LANG = os.environ.get("LANG_UI", "en")

WORKSPACE = detail(S1)["workspace"]
WRITTEN = [
    "site/index.html", "site/menu.html", "site/about.html", "site/css/base.css", "site/css/menu.css", "site/css/print.css",
    "site/js/menu.js", "site/js/prices.js", "site/js/cart.js", "data/menu.json", "data/prices.json", "data/hours.json",
    "scripts/build.py", "scripts/check_links.py", "tests/test_prices.py", "tests/test_menu.py", "README.md", ".gitignore",
    "Makefile", "docs/owner-guide.md", "docs/changelog.md", "netlify.toml", "reports/menu-check.md",
]
EDITED_ONLY = "site/robots.txt"
SENT = ["reports/menu-check.md", "reports/lighthouse.html", "exports/menu.pdf", "exports/prices.csv", "exports/hours.txt"]
# Written and then sent: a card, and therefore not a row of the changed list as well.
CHANGED = sorted({*WRITTEN, EDITED_ONLY} - set(SENT))


def turn_messages() -> list[dict]:
    """One turn: the writes with absolute paths, as the tool reports them, edits with relative ones."""
    calls = [{"id": f"w{i}", "name": "Write", "arguments": {"path": f"{WORKSPACE}/{p}", "content": "…"}} for i, p in enumerate(WRITTEN)]
    calls += [{"id": "e1", "name": "Edit", "arguments": {"path": "site/menu.html", "old_string": "a", "new_string": "b"}}, {"id": "e2", "name": "MultiEdit", "arguments": {"path": EDITED_ONLY, "edits": []}}]
    results = [{"id": c["id"], "content": f"wrote 120 characters to {c['arguments']['path']}", "is_error": False} for c in calls]
    sends = [{"id": f"s{i}", "name": "SendFile", "arguments": {"path": p}} for i, p in enumerate(SENT)]
    sent = [{"id": c["id"], "content": f"sent {c['arguments']['path'].rsplit('/', 1)[-1]} (2048 bytes): delivered", "is_error": False} for c in sends]
    return [
        {"role": "user", "seq": 501, "text": "Rebuild the whole site from the sheet and send me the reports.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=9)},
        {"role": "assistant", "seq": 502, "text": "", "thinking": "", "tool_calls": calls, "tool_results": [], "created_at": ago(minutes=8)},
        {"role": "tool", "seq": 503, "text": "", "thinking": "", "tool_calls": [], "tool_results": results, "created_at": ago(minutes=7)},
        {"role": "assistant", "seq": 504, "text": "", "thinking": "", "tool_calls": sends, "tool_results": [], "created_at": ago(minutes=6)},
        {"role": "tool", "seq": 505, "text": "", "thinking": "", "tool_calls": [], "tool_results": sent, "created_at": ago(minutes=5)},
        {"role": "assistant", "seq": 506, "text": "The site is rebuilt from the sheet: every page reads its prices from `data/prices.json`. The reports are attached.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=4)},
    ]


def make_route(native: bool, reveals: list[dict]):  # type: ignore[no-untyped-def]
    def route(r) -> None:  # type: ignore[no-untyped-def]
        url = r.request.url.split("?", 1)[0]
        rel = url[url.index("/api/"):]
        if rel == f"/api/sessions/{S1}" and r.request.method == "GET":
            return respond(r, {**detail(S1), "messages": turn_messages()})
        if rel == "/api/capabilities":
            return respond(r, {**CAPABILITIES, "reveal": {"available": native, "platform": "linux"}})
        if rel == "/api/reveal" and r.request.method == "POST":
            body = json.loads(r.request.post_data or "{}")
            reveals.append(body)
            return respond(r, {"path": f"{WORKSPACE}/{body.get('path', '')}", "directory": not body.get("path"), "opened": True, "platform": "linux"})
        return stub(r)
    return route


def open_page(context, native: bool, reveals: list[dict]) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", make_route(native, reveals))
    page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={LANG}")
    page.wait_for_selector(".chat-scroll .timeline .answer", timeout=15000)
    page.wait_for_timeout(500)
    return page


def shot(page: Page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def desktop(browser, native: bool) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    reveals: list[dict] = []
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, native, reveals)
    turn = page.locator(".turn").last

    cards = turn.locator(".artifacts .artifact")
    names = cards.locator(".artifact-name").all_inner_texts()
    print("cards:", names)
    if names != ["menu-check.md", "lighthouse.html", "menu.pdf"]:
        problems.append(f"expected the first three sent files as cards, got {names}")
    more = turn.locator(".artifacts-more")
    if not more.count() or "2" not in more.inner_text():
        problems.append("there is no '+2 more' after the third card")

    summary = turn.locator(".changed-files-head")
    if summary.count() != 1:
        problems.append(f"expected one changed-files line, found {summary.count()}")
        context.close()
        return problems
    text = summary.inner_text()
    print("summary:", repr(text))
    if str(len(CHANGED)) not in text:
        problems.append(f"the line does not count the {len(CHANGED)} changed files ({text!r})")
    height = summary.evaluate("(el) => Math.round(el.getBoundingClientRect().height)")
    if height > 32:
        problems.append(f"the changed-files line is {height} px tall, more than one compact row")
    if turn.locator(".changed-file").count():
        problems.append("the changed files are listed before the line is opened")
    page.evaluate("() => document.querySelector('.turn:last-child .changed-files-head')?.scrollIntoView({ block: 'center' })")
    shot(page, f"turn-files-desktop-{'native' if native else 'server'}")

    summary.click()
    rows = turn.locator(".changed-file")
    rows.first.wait_for(timeout=5000)
    paths = rows.locator(".changed-file-path").all_inner_texts()
    print("rows:", len(paths), paths[:4])
    if sorted(paths) != CHANGED:
        problems.append(f"the list is not the changed files relative to the workspace: {sorted(paths)[:5]}…")
    if any(p.startswith("/") or WORKSPACE in p for p in paths):
        problems.append("a row shows an absolute path")
    if "reports/menu-check.md" in paths:
        problems.append("a file shown as a card is listed again")
    row_heights = set(rows.evaluate_all("(els) => els.map((el) => Math.round(el.getBoundingClientRect().height))"))
    if max(row_heights) > 28:
        problems.append(f"the rows are not compact one-liners ({sorted(row_heights)})")
    # The actions appear on hover, not before.
    first = rows.first
    hidden = first.locator(".changed-file-actions").evaluate("(el) => getComputedStyle(el).opacity")
    first.hover()
    page.wait_for_timeout(250)
    shown = first.locator(".changed-file-actions").evaluate("(el) => getComputedStyle(el).opacity")
    if not (float(hidden) < 0.5 <= float(shown)):
        problems.append(f"the row's actions are not revealed on hover ({hidden} → {shown})")
    reveal = first.locator(".changed-file-reveal")
    if native and not reveal.count():
        problems.append("native: the row has no reveal action")
    if not native and reveal.count():
        problems.append("server: the row offers to reveal a file on a machine the reader is not at")
    shot(page, f"turn-files-desktop-open-{'native' if native else 'server'}")
    if native and reveal.count():
        reveal.click()
        page.wait_for_timeout(400)
        print("reveal requests:", reveals)
        if not reveals or reveals[-1].get("session_id") != S1 or reveals[-1].get("path") != paths[0]:
            problems.append(f"reveal did not ask the host for the workspace-relative path ({reveals})")

    # Opening a row puts the file in the panel's Preview.
    rows.filter(has_text="docs/owner-guide.md").locator(".changed-file-main").click()
    page.wait_for_timeout(600)
    crumbs = page.locator(".panel-crumbs").inner_text() if page.locator(".panel-crumbs").count() else ""
    if "owner-guide.md" not in crumbs:
        problems.append(f"a row did not open its file in Preview ({crumbs!r})")

    # The session's workspace: a command of the header's menu, there only on the operator's machine.
    page.locator(".chat-head button[aria-label='Session actions']").click()
    item = page.locator(".menu button", has_text="Open in file manager")
    if native != bool(item.count()):
        problems.append(f"{'native' if native else 'server'}: the session menu {'lacks' if native else 'offers'} the workspace in the file manager")
    if native and item.count():
        item.click()
        page.wait_for_timeout(400)
        if not reveals or reveals[-1] != {"session_id": S1, "run": True}:
            problems.append(f"the session menu did not ask the host for the workspace ({reveals[-1:]})")
    else:
        page.keyboard.press("Escape")

    more.click()
    page.wait_for_timeout(200)
    after = turn.locator(".artifacts .artifact").count()
    if after != len(SENT):
        problems.append(f"'+2 more' did not show every sent file ({after})")
    context.close()
    return problems


def phone(browser, native: bool) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    reveals: list[dict] = []
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_page(context, native, reveals)
    turn = page.locator(".turn").last
    summary = turn.locator(".changed-files-head")
    if summary.count() != 1:
        problems.append("phone: there is no changed-files line")
        context.close()
        return problems
    for box in turn.locator(".artifacts .artifact, .changed-files-head").evaluate_all("(els) => els.map((el) => { const r = el.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.right)]; })"):
        if box[0] < -1 or box[1] > 391:
            problems.append(f"phone: a file line is outside the viewport ({box})")
    page.evaluate("() => document.querySelector('.turn:last-child .changed-files-head')?.scrollIntoView({ block: 'center' })")
    shot(page, "turn-files-phone")
    summary.click()
    sheet = page.locator(".changed-files-sheet")
    sheet.wait_for(timeout=5000)
    rows = sheet.locator(".ph-row")
    if rows.count() != len(CHANGED):
        problems.append(f"phone: the sheet lists {rows.count()} rows, not {len(CHANGED)}")
    first_title = rows.first.locator(".ph-row-t").inner_text()
    if first_title.startswith("/"):
        problems.append(f"phone: a row shows an absolute path ({first_title!r})")
    page.wait_for_timeout(500)
    shot(page, "turn-files-phone-sheet")
    rows.first.dispatch_event("contextmenu")
    actions = page.locator(".ph-actions [role=menu] .ph-mrow")
    actions.first.wait_for(timeout=3000)
    labels = actions.all_inner_texts()
    print("phone actions:", labels)
    if len(labels) < 2:
        problems.append(f"phone: a long press offers no actions ({labels})")
    page.wait_for_timeout(500)
    shot(page, "turn-files-phone-actions")
    context.close()
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        problems += desktop(browser, native=True)
        problems += desktop(browser, native=False)
        problems += phone(browser, native=False)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
