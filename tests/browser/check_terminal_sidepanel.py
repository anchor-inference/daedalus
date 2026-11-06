"""Drive the Terminals workspace's side panel and its named layouts, and refuse what does not behave.

The panel's promises: it opens from the bar's Panel button beside the grid and is gone on a phone;
Files lists the active pane's changed files with git's letters and line counts, and a file opens its
diff in the app's diff viewer with added and removed lines coloured; Actions counts its kinds in
pills, a failed command is red and "failed only" keeps only those, a kind pill keeps only its kind,
a command opens its output, and a staff member's pane lists its CLI's tool calls instead; Panes
lists every terminal and puts one into a chosen place; a layout saved under a name comes back after
the grid was changed, survives a reload (it is kept on the host), renames and deletes; restoring a
layout whose terminal has gone leaves that place empty with a note and a toast, and "Open again"
puts a fresh shell there; the panel's width follows its edge, comes back after a reload and goes
back to the default on a double click; its tab and its closed state are remembered. The Russian page
has Russian words, and no page throws.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_sidepanel.py

``SHOTS=<dir>`` also saves the panel's three tabs there (with ``THEME=<id>`` for an app theme).

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import P1, S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
THEME = os.environ.get("THEME", "")
IDS = ["s1shell00000", "s2devsrv0000", "s3staff00000"]
SPARE = "s5spare00000"
STAFF = "st-ira"

TURNS = [
    {"index": 0, "role": "user", "text": "Add the gluten-free section", "tools": [], "started_at": "2026-11-01T09:58:00Z", "ended_at": "2026-11-01T09:58:00Z", "usage": None},
    {"index": 1, "role": "assistant", "text": "Reading the menu first.", "started_at": "2026-11-01T10:00:00Z", "ended_at": "2026-11-01T10:01:00Z", "usage": None, "tools": [
        {"name": "Read", "summary": "src/menu.py", "ok": True},
        {"name": "Grep", "summary": "gluten in src", "ok": True},
        {"name": "Bash", "summary": "npm test", "ok": False},
        {"name": "Edit", "summary": "src/menu.py", "ok": True},
    ]},
]


class Check:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def that(self, ok: bool, problem: str) -> None:
        if not ok:
            self.problems.append(problem)
            print("PROBLEM:", problem)


def fleet() -> TerminalStub:
    term = TerminalStub(S1)
    term.add(IDS[0], title="bash · bakery", project_id=P1, cwd="/srv/workspaces/bakery")
    term.add(IDS[1], title="npm run dev", project_id=P1, busy=True, cwd="/srv/workspaces/bakery/web")
    term.add(IDS[2], title="Ira · Claude Code", owner_kind="staff", owner_id=STAFF, owner_label="Ira", project_id=P1, cwd="/srv/worktrees/ira-menu")
    term.add(SPARE, title="psql", project_id=P1, cwd="/srv/workspaces/bakery")
    for i in [*IDS, SPARE]:
        term.emit(i, "".join(f"{i} line {n}\r\n" for n in range(12)))
    term.shell_prompt(IDS[0])
    term.shell_command(IDS[0], "git status", ["On branch main", "Changes not staged for commit:"], 0)
    term.shell_prompt(IDS[0])
    term.shell_command(IDS[0], "cat README.md", ["# Bakery"], 0)
    term.shell_prompt(IDS[0])
    term.shell_command(IDS[0], "npm test", ["FAIL src/menu.test.ts", "  expected 3 items, got 2"], 1)
    term.shell_prompt(IDS[0])
    term.shell_command(IDS[0], "rg gluten src", ["src/menu.py:12: gluten"], 0)
    term.shell_prompt(IDS[0])
    term.files[IDS[0]] = {"root": "/srv/workspaces/bakery", "branch": "menu-gluten", "files": [
        {"path": "README.md", "status": "M", "added": 1, "removed": 0},
        {"path": "notes/plan.md", "status": "?", "added": None, "removed": None},
        {"path": "old/menu.txt", "status": "D", "added": 0, "removed": 14},
        {"path": "src/menu.py", "status": "M", "added": 12, "removed": 3},
    ]}
    term.diffs[(IDS[0], "src/menu.py")] = (
        "diff --git a/src/menu.py b/src/menu.py\n--- a/src/menu.py\n+++ b/src/menu.py\n@@ -10,4 +10,5 @@ MENU = [\n"
        "     \"bread\",\n-    \"cake\",\n+    \"cake (gluten-free)\",\n+    \"rye\",\n     \"buns\",\n"
    )
    term.files[IDS[2]] = {"root": "/srv/worktrees/ira-menu", "branch": "ira/menu", "base": "a1b2c3d", "files": [{"path": "src/menu.py", "status": "M", "added": 4, "removed": 1}]}
    return term


def staff_route(route) -> None:  # type: ignore[no-untyped-def]
    if "/transcript" in route.request.url:
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"turns": TURNS, "more": False}))
    else:
        route.fallback()


def open_page(context, term: TerminalStub, path: str, lang: str = "en") -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.route(f"**/api/staff/{STAFF}/**", staff_route)
    term.install(page)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors  # type: ignore[attr-defined]
    page.goto(f"{BASE}/{path}{'&' if '?' in path else '?'}token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".tp-ws, .term-card, .term-phone", timeout=20000)
    return page


def context_for(browser, **extra):  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport=extra.pop("viewport", {"width": 1600, "height": 960}), color_scheme="dark", **extra)
    context.add_init_script(DEBUG)
    if THEME:
        context.add_init_script(f"try {{ localStorage.setItem('daedalus.appearance', JSON.stringify({{follow: false, theme: {json.dumps(THEME)}}})); }} catch (e) {{}}")
    return context


def panes(page: Page) -> list[str]:
    return page.evaluate("""() => [0, 1, 2, 3].map((i) => { const el = document.querySelector(`.tp-pane[data-slot='${i}']`); return el ? el.dataset.pane : '-'; })""")


def width(page: Page) -> float:
    return page.eval_on_selector(".tside", "(e) => e.getBoundingClientRect().width")


def colour(page: Page, selector: str) -> str:
    return page.eval_on_selector(selector, "(e) => getComputedStyle(e).color")


def token(page: Page, name: str) -> str:
    """A theme token as a computed colour, for comparing with an element's."""
    return page.evaluate("(n) => { const s = document.createElement('span'); s.style.color = `var(${n})`; document.body.append(s); const c = getComputedStyle(s).color; s.remove(); return c; }", name)


def shoot(page: Page, name: str) -> None:
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(Path(SHOTS) / name))


def tab(page: Page, which: str) -> None:
    page.locator(f".tside-tab[data-tab='{which}']").click()
    page.wait_for_timeout(250)


def toast_text(page: Page) -> str:
    page.wait_for_selector(".toast", timeout=5000)
    return page.locator(".toast").inner_text()


def panel_steps(browser, c: Check) -> None:  # type: ignore[no-untyped-def]
    term = fleet()
    context = context_for(browser)
    page = open_page(context, term, f"terminals/{IDS[0]}?with={','.join(IDS[1:])}")
    for i in IDS:
        wait_live(page, i)
    page.wait_for_timeout(400)

    # Closed until asked for; the bar's Panel button opens it beside the grid.
    c.that(page.locator(".tside").count() == 0, "the panel is open before anyone asked for it")
    grid_before = page.eval_on_selector(".tp-grid", "(e) => e.getBoundingClientRect().width")
    page.locator(".tp-panel-toggle").click()
    page.wait_for_selector(".tp-side .tside", timeout=5000)
    page.wait_for_timeout(300)
    c.that(abs(width(page) - 360) <= 1, f"the panel opens {width(page)} px wide, not 360")
    grid_after = page.eval_on_selector(".tp-grid", "(e) => e.getBoundingClientRect().width")
    c.that(grid_after < grid_before - 300, f"the grid did not give the panel its room ({grid_before} → {grid_after})")
    c.that(page.locator(".tp-panel-toggle").get_attribute("aria-pressed") == "true", "the Panel button does not say the panel is open")

    # Files: git's letters, the counts, the diff in the app's viewer.
    page.wait_for_selector(".tside-file", timeout=5000)
    files = page.eval_on_selector_all(".tside-file", "(rows) => rows.map((r) => [r.dataset.path, r.querySelector('.tside-status').textContent])")
    c.that(files == [["README.md", "M"], ["notes/plan.md", "U"], ["old/menu.txt", "D"], ["src/menu.py", "M"]], f"the Files tab lists {files}")
    c.that(page.locator(".tside-tab[data-tab='files'] .tside-count").inner_text() == "4", "the Files tab does not count its files")
    c.that("menu-gluten" in page.locator(".tside-branch").inner_text(), "the branch is not shown")
    c.that(colour(page, ".tside-file[data-path='old/menu.txt'] .tside-status") == token(page, "--data-bad"), "a deleted file's letter is not the error colour")
    shoot(page, "terminals-panel-files.png")
    page.locator(".tside-file[data-path='src/menu.py']").click()
    page.wait_for_selector(".tside-diff .diff-line", timeout=5000)
    c.that(page.locator(".tside-diff .diff-add").count() == 2 and page.locator(".tside-diff .diff-del").count() == 1, "the diff does not show two added lines and one removed")
    add_bg = page.eval_on_selector(".tside-diff .diff-add", "(e) => getComputedStyle(e).backgroundColor")
    c.that(add_bg not in ("rgba(0, 0, 0, 0)", "transparent"), "an added line is not coloured")
    shoot(page, "terminals-panel-diff.png")
    page.locator(".tside-back").click()
    page.wait_for_selector(".tside-file", timeout=5000)

    # Actions: pills, a red failure, filters, a command's output.
    tab(page, "actions")
    page.wait_for_selector(".tside-act", timeout=5000)
    titles = page.eval_on_selector_all(".tside-act-title", "(r) => r.map((e) => e.textContent)")
    c.that(titles == ["rg gluten src", "npm test", "cat README.md", "git status"], f"the feed is not newest first: {titles}")
    pills = page.eval_on_selector_all(".tside-pill", "(r) => r.map((e) => e.dataset.kind + ':' + e.querySelector('.num').textContent)")
    c.that(pills == ["all:4", "shell:1", "reads:1", "search:1", "git:1", "failed:1"], f"the pills count {pills}")
    failed = ".tside-act[data-state='failed'] .tside-act-title"
    c.that(page.locator(failed).count() == 1 and colour(page, failed) == token(page, "--data-bad"), "the failed command is not red")
    c.that("✗ 1" in page.locator(".tside-act[data-state='failed'] .tside-act-end").inner_text(), "the failed command does not show its exit code")
    c.that(page.locator(".tside-tab[data-tab='actions'] .tside-count.bad").count() == 1, "the Actions tab's count is not marked when something failed")
    page.locator(".tside-pill[data-kind='failed']").click()
    c.that(page.locator(".tside-act").count() == 1, "failed only does not keep only the failure")
    page.locator(".tside-pill[data-kind='failed']").click()
    page.locator(".tside-pill[data-kind='git']").click()
    c.that(page.eval_on_selector_all(".tside-act-title", "(r) => r.map((e) => e.textContent)") == ["git status"], "the git pill does not keep only git")
    page.locator(".tside-pill[data-kind='all']").click()
    page.locator(".tside-act[data-state='failed'] .tside-act-row").click()
    page.wait_for_selector(".tside-out", timeout=5000)
    c.that("expected 3 items" in page.locator(".tside-out").inner_text(), "a command does not open its output")
    shoot(page, "terminals-panel-actions.png")

    # A staff member's pane: its CLI's tool calls.
    page.locator(".tp-tab[data-slot='2']").click()
    page.wait_for_timeout(500)
    page.wait_for_selector(".tside-act[data-kind='edits']", timeout=5000)
    staff_titles = page.eval_on_selector_all(".tside-act", "(r) => r.map((e) => e.dataset.kind + ':' + e.dataset.state)")
    c.that(staff_titles == ["edits:ok", "shell:failed", "search:ok", "reads:ok"], f"the staff member's feed is {staff_titles}")
    c.that("agent" in page.locator(".tside-source").inner_text().lower(), "the staff feed does not say it is the agent's tools")
    tab(page, "files")
    page.wait_for_selector(".tside-file", timeout=5000)
    c.that("a1b2c3d" in page.locator(".tside-files .tside-sub").first.inner_text(), "a staff member's files do not say they are its work since its base")

    # Panes: every terminal, and one put into a chosen place.
    tab(page, "panes")
    page.wait_for_selector(".tside-term", timeout=5000)
    c.that(page.locator(".tside-term").count() == 4, f"the Panes tab lists {page.locator('.tside-term').count()} terminals, not 4")
    page.locator(f".tside-term[data-terminal='{SPARE}'] .tside-slot[data-slot='3']").click()
    page.wait_for_timeout(400)
    c.that(panes(page)[3] == SPARE, f"putting a terminal into pane 4 gave {panes(page)}")
    c.that(page.locator(".tp-tab[data-slot='3']").get_attribute("aria-selected") == "true", "the place a terminal was put into is not the active one")

    # A layout: saved, the grid changed, restored, renamed.
    page.locator(".tside-save .tside-input").fill("Bakery review")
    page.locator(".tside-save button[type='submit']").click()
    page.wait_for_selector(".tside-layout", timeout=5000)
    saved = term.layouts[0]["workspace"] if term.layouts else {}
    c.that(saved.get("panes") == [*IDS, SPARE] and saved.get("layout") == 4, f"the host was sent {saved}")
    c.that(page.locator(".tside-layout .tside-chip:not(.shape)").count() == 4, "the saved layout does not list its panes")
    shoot(page, "terminals-panel-panes.png")
    page.locator(".tp-layout[data-layout='1']").click()
    page.locator(".tp-pane[data-slot='0'] .tp-pane-close").click()
    page.wait_for_timeout(300)
    page.locator(".tside-restore").click()
    page.wait_for_timeout(500)
    c.that(panes(page) == [*IDS, SPARE] and page.locator(".tp-pane:not(.hidden)").count() == 4, f"restoring gave {panes(page)}")
    page.locator(".tside-layout button[aria-haspopup='menu']").click()
    page.locator(".menu [role='menuitem']").filter(has_text="Rename").first.click()
    page.locator(".tside-layout .tside-input").fill("Night shift")
    page.locator(".tside-layout .tside-input").press("Enter")
    page.wait_for_timeout(400)
    c.that(page.locator(".tside-layout-name").inner_text() == "Night shift", "the layout was not renamed")

    # The panel's edge: dragged wider, remembered, back to the default on a double click.
    grip = page.eval_on_selector(".tside-grip", "(e) => { const b = e.getBoundingClientRect(); return { x: b.x + b.width / 2, y: b.y + 200 }; }")
    page.mouse.move(grip["x"], grip["y"])
    page.mouse.down()
    page.mouse.move(grip["x"] - 100, grip["y"], steps=8)
    page.mouse.up()
    page.wait_for_timeout(300)
    c.that(abs(width(page) - 460) <= 2, f"dragging the edge 100 px left made the panel {width(page)} px")
    sideways = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    c.that(sideways <= 0, f"the page scrolls sideways by {sideways} px with the panel open")
    c.that(not page.errors, f"the page threw: {page.errors}")  # type: ignore[attr-defined]
    page.close()

    # A reload: the panel, its tab, its width and the layout kept on the host all come back.
    page = open_page(context, term, f"terminals/{IDS[0]}")
    page.wait_for_selector(".tside", timeout=10000)
    page.wait_for_timeout(300)
    c.that(page.locator(".tside-tab.on").get_attribute("data-tab") == "panes", "the panel's tab is not remembered")
    c.that(abs(width(page) - 460) <= 2, f"the panel's width is not remembered ({width(page)} px)")
    page.wait_for_selector(".tside-layout", timeout=5000)
    c.that(page.locator(".tside-layout-name").inner_text() == "Night shift", "the layout did not survive a reload")
    page.locator(".tside-grip").dblclick()
    page.wait_for_timeout(200)
    c.that(abs(width(page) - 360) <= 1, f"a double click on the edge left the panel {width(page)} px")

    # A terminal that has gone: its place is empty with a note, and a shell can be opened there again.
    del term.terms[IDS[1]]
    page.evaluate("() => window.dispatchEvent(new Event('focus'))")
    page.wait_for_timeout(5600)
    page.locator(".tside-restore").click()
    page.wait_for_timeout(500)
    c.that(panes(page)[1] == "", f"the gone terminal's place is not empty: {panes(page)}")
    c.that("ended" in toast_text(page).lower(), "restoring with a gone terminal does not say so")
    c.that(page.locator(".tside-missing-row").count() == 1, "the note does not name the gone terminal")
    page.locator(".tside-missing-row .btn").click()
    page.wait_for_timeout(800)
    placed = panes(page)[1]
    c.that(placed not in ("", "-", IDS[1]) and placed in term.terms, f"Open again did not put a fresh shell in the empty place: {panes(page)}")
    fresh = term.terms.get(placed)
    c.that(fresh is not None and fresh.cwd == "/srv/workspaces/bakery/web", "the fresh shell is not in the gone terminal's folder")

    # Deleting the layout, then closing the panel: it stays closed after a reload.
    page.locator(".tside-layout button[aria-haspopup='menu']").click()
    page.locator(".menu [role='menuitem']").filter(has_text="Delete").first.click()
    page.wait_for_timeout(400)
    c.that(page.locator(".tside-layout").count() == 0 and not term.layouts, "the layout was not deleted")
    page.locator(".tside-close").click()
    page.wait_for_timeout(200)
    c.that(page.locator(".tside").count() == 0, "the panel's close button did not close it")
    c.that(not page.errors, f"the page threw: {page.errors}")  # type: ignore[attr-defined]
    page.close()
    page = open_page(context, term, f"terminals/{IDS[0]}")
    page.wait_for_timeout(800)
    c.that(page.locator(".tside").count() == 0, "a closed panel opened again after a reload")
    context.close()


def russian(browser, c: Check) -> None:  # type: ignore[no-untyped-def]
    term = fleet()
    context = context_for(browser)
    context.add_init_script("try { localStorage.setItem('daedalus.term.side', JSON.stringify({open: true, width: 380, tab: 'actions'})); } catch (e) {}")
    page = open_page(context, term, f"terminals/{IDS[0]}?with={IDS[1]}", lang="ru")
    page.wait_for_selector(".tside-act", timeout=10000)
    tabs = page.eval_on_selector_all(".tside-tab span:first-child", "(r) => r.map((e) => e.textContent)")
    c.that(tabs == ["Файлы", "Действия", "Панели"], f"the Russian tabs read {tabs}")
    c.that("действ" in page.locator(".tside-summary").inner_text(), "the Russian feed's summary is not Russian")
    if SHOTS:
        shoot(page, "ru-terminals-panel-actions.png")
    c.that(not page.errors, f"the Russian page threw: {page.errors}")  # type: ignore[attr-defined]
    context.close()


def phone(browser, c: Check) -> None:  # type: ignore[no-untyped-def]
    term = fleet()
    context = context_for(browser, viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=2)
    context.add_init_script("try { localStorage.setItem('daedalus.term.side', JSON.stringify({open: true, width: 380, tab: 'files'})); } catch (e) {}")
    page = open_page(context, term, f"terminals/{IDS[0]}")
    page.wait_for_selector(".term-phone", timeout=15000)
    page.wait_for_timeout(500)
    c.that(page.locator(".tside, .tp-side").count() == 0, "the side panel is drawn on a phone")
    c.that(not page.errors, f"the phone page threw: {page.errors}")  # type: ignore[attr-defined]
    context.close()


def main() -> int:
    expect_app(BASE)
    c = Check()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        panel_steps(browser, c)
        russian(browser, c)
        phone(browser, c)
        browser.close()
    missed = UNHANDLED.report()
    print("problems:", len(c.problems))
    return 1 if c.problems or missed else 0


if __name__ == "__main__":
    sys.exit(main())
