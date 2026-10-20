"""The orchestrator's Files tab: the project's folders and each staff member's worktree, read-only.

The orchestrator writes no file, yet the operator steering it must see the project's files and what
the staff made in their worktrees before it is merged. Its panel has a Files tab; the folder switch
lists the project's folders and then one entry per worktree, named by the member and the branch; a
worktree opens under the pane id the host gave it, its document opens in Preview, and nothing in the
tab offers an upload. At 1440 px and on a 390 px phone, in English and Russian.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_orchestrator_files.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import (  # noqa: E402
    DEFAULT_APP,
    WORKTREE_BRANCH,
    WORKTREE_DOCX_TEXT,
    WORKTREE_MEMBER,
    WORKTREE_PANE,
    FocusStub,
    expect_app,
)
from screenshots import UNHANDLED, focus_stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
DOCX = "docs/drafts/Акт о приёме на работу.docx"
FOLDER_NAME = {"en": "Folder", "ru": "Папка"}


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def visit(browser, lang: str, phone: bool) -> None:  # type: ignore[no-untyped-def]
    where = f"{lang} {'phone' if phone else 'desktop'}"
    focus = FocusStub.bakery(lang)
    pid = focus.projects[0]["id"]
    viewport = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=viewport, color_scheme="dark", is_mobile=phone, has_touch=phone)
    page = context.new_page()
    requests: list[str] = []
    page.on("request", lambda request: requests.append(unquote(urlsplit(request.url).path)))
    page.route("**/api/**", focus_stub(focus))
    page.goto(f"{BASE}/project/{pid}?panel=files&token=t&lang={lang}")

    scope = ".panel-sheet" if phone else ".panel"
    if not phone:
        page.wait_for_selector(".chat.in-project .panel", timeout=15000)
        tabs = page.eval_on_selector_all(".panel .panel-tab", "els => els.map(e => e.dataset.tab)")
        assert tabs[:3] == ["questions", "details", "files"], f"{where}: the orchestrator's tabs are {tabs}"
    switcher = page.locator(f"{scope} .explorer-folders select")
    expect(switcher).to_be_visible(timeout=15000)
    assert switcher.get_attribute("aria-label") == FOLDER_NAME[lang], f"{where}: the switch is named {switcher.get_attribute('aria-label')!r}"
    options = switcher.locator("option").all_inner_texts()
    label = f"{WORKTREE_MEMBER} · {WORKTREE_BRANCH}"
    assert options[:3] == ["bakery-site", "bakery-api", "bakery-bot"] and options[-1] == label, f"{where}: the switch lists {options}"
    assert any(p.endswith(f"/api/projects/{pid}/worktrees") for p in requests), f"{where}: the worktrees were never asked for"
    # The project's own folder is read-only for the orchestrator: no upload, and the lock says so.
    explorer = page.locator(f"{scope} .explorer")
    expect(explorer.locator(".explorer-folders .chip")).to_be_visible()
    expect(explorer.locator("input[type=file]")).to_have_count(0)

    switcher.select_option(WORKTREE_PANE)
    tree = explorer.locator(".explorer-tree")
    expect(tree.locator('[data-path="docs"]')).to_be_visible()
    expect(explorer.locator(".explorer-folders .chip")).to_be_visible()
    expect(explorer.locator("input[type=file]")).to_have_count(0)
    assert any(p.endswith(f"/folders/{WORKTREE_PANE}/files") for p in requests), f"{where}: the worktree was listed through {[p for p in requests if '/files' in p]}"
    # A long label stays inside the switch rather than pushing the chips out of the pane.
    inside = page.evaluate("""(scope) => { const row = document.querySelector(scope + ' .explorer-folders'); const pane = document.querySelector(scope + ' .explorer');
      const a = row.getBoundingClientRect(), b = pane.getBoundingClientRect(); return a.right <= b.right + 1 && row.scrollWidth <= row.clientWidth + 1; }""", scope)
    assert inside, f"{where}: the folder switch overflows its pane"

    tree.locator('[data-path="docs"]').click()
    tree.locator('[data-path="docs/drafts"]').click()
    tree.locator(f'[data-path="{DOCX}"]').click()
    expect(page.locator(".preview-doc.docx")).to_contain_text(WORKTREE_DOCX_TEXT, timeout=10000)
    assert any(p.endswith(f"/folders/{WORKTREE_PANE}/download") for p in requests), f"{where}: the document was read from {[p for p in requests if 'download' in p]}"
    fits(page, where)
    context.close()


def run() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for phone in (False, True):
                try:
                    visit(browser, lang, phone)
                except AssertionError as error:
                    problems.append(str(error))
                except Exception as error:  # noqa: BLE001 - a selector that never appears is a failure to report, not a crash
                    problems.append(f"{lang} {'phone' if phone else 'desktop'}: {error}")
        browser.close()
    unhandled = UNHANDLED.report()
    for problem in problems:
        print("FAIL", problem)
    if not problems and not unhandled:
        print("the orchestrator's Files tab shows the project and the staff worktrees, read-only")
    return 1 if problems or unhandled else 0


if __name__ == "__main__":
    sys.exit(run())
