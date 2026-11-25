"""The Changes page: a list of the agent's change proposals, and one of them opened for review.

Held here: the list and its filter; the review's geometry at a laptop, an ultrawide and a phone (a 52 px
header, a 320 px file column, a 44 px file header over 22 px lines at 13 px, a 3 px edge in the colour
of what happened to a line); the file tree, its filter, the progress that survives a reload, and the
next-file button; unified and side-by-side views; a folded run of unchanged lines opening; a binary,
a moved and a removed file saying so instead of showing nothing; the picker; a decision (approve, and a
reject that carries its reason); a diff the host cut; and the empty list. A two-thousand-line diff is
timed so a render that grows back shows here as a number.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_changes_screen.py
"""
from __future__ import annotations

import json
import os
import sys

from api_stub import DEFAULT_APP, PROPOSALS, expect_app
from playwright.sync_api import Page, expect, sync_playwright
from screenshots import UNHANDLED, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"waiting": "Waiting", "approve": "Approve", "reject": "Reject", "files": "6 files", "viewed": "Viewed", "split": "Split", "unified": "Unified",
           "filter": "Filter files", "progress": "1 of 6 viewed", "binary": "A binary file", "moved": "Moved from config/search.sample.toml", "reason": "Reason (sent to the agent)",
           "cut": "The diff was cut at 400 KB", "none": "No change proposals yet", "merged": "merged PR #57", "viewed_next": "Viewed, next", "list": "Change sets"},
    "ru": {"waiting": "Ждут решения", "approve": "Принять", "reject": "Отклонить", "files": "6 файлов", "viewed": "Просмотрено", "split": "Рядом", "unified": "Единый",
           "filter": "Фильтр по файлам", "progress": "1 из 6 просмотрено", "binary": "Двоичный файл", "moved": "Перенесён из config/search.sample.toml", "reason": "Причина (её увидит агент)",
           "cut": "Различия обрезаны на 400 КБ", "none": "Предложений пока нет", "merged": "merged PR #57", "viewed_next": "Просмотрено, дальше", "list": "Наборы изменений"},
}

# The language the run is in; the pages are opened with it and the words looked for come from it.
CURRENT = {"lang": "en"}
measured: dict[str, object] = {}
problems: list[str] = []


def check(ok: bool, message: str) -> None:
    if not ok:
        problems.append(message)


def box(page: Page, selector: str) -> dict:
    found = page.evaluate("""(sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const css = getComputedStyle(el);
      return { x: Math.round(r.x * 10) / 10, y: Math.round(r.y * 10) / 10, w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10,
               font: parseFloat(css.fontSize), bg: css.backgroundColor, shadow: css.boxShadow };
    }""", selector)
    assert found is not None, f"{selector} is not on the page"
    return found


def sideways(page: Page, where: str) -> None:
    over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    check(over <= 0, f"{where}: the page scrolls sideways by {over}px")


def open_review(context, route: str, ready: str = ".review .dp-line, .review .dp-pair") -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.set_default_timeout(8000)
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/{route}{'&' if '?' in route else '?'}token=t&lang={CURRENT['lang']}")
    page.wait_for_selector(ready)
    return page


def desktop(browser, width: int, height: int) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[CURRENT['lang']]
    where = f"{CURRENT['lang']} {width}"
    PROPOSALS.reset()
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark")

    # The list: four change sets, the waiting ones narrowed by the chip, a row opens its review.
    page = context.new_page()
    page.set_default_timeout(8000)
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/changes?token=t&lang={CURRENT['lang']}")
    page.wait_for_selector(".proposal.erow")
    check(page.locator(".proposal.erow").count() == 4, f"{where}: the list holds {page.locator('.proposal.erow').count()} rows, not 4")
    page.locator(".pagehead .chip", has_text=words["waiting"]).click()
    check(page.locator(".proposal.erow").count() == 2, f"{where}: the waiting filter leaves {page.locator('.proposal.erow').count()} rows, not 2")
    page.locator(".proposal.erow").first.click()
    page.wait_for_selector(".review .dp-line")
    check(page.url.split("?")[0].endswith("/changes/p57"), f"{where}: the first waiting row opened {page.url}")
    page.close()

    # The review's geometry.
    page = open_review(context, "changes/p57")
    main = box(page, ".main")
    head = box(page, ".pagehead")
    files = box(page, ".review-files")
    pane = box(page, ".review-diff")
    filehead = box(page, ".dp-head")
    line = box(page, ".dp-line")
    hunk = box(page, ".dp-hunk")
    added = box(page, ".dp-line.add")
    removed = box(page, ".dp-line.del")
    measured[where] = {"header": head["h"], "files_column": files["w"], "diff_pane": pane["w"], "main": main["w"], "file_header": filehead["h"], "line": line["h"],
                       "line_font": line["font"], "hunk": hunk["h"], "added_shadow": added["shadow"], "removed_shadow": removed["shadow"]}
    check(head["h"] == 52, f"{where}: the header is {head['h']} px high, not 52")
    check(files["w"] == 320, f"{where}: the file column is {files['w']} px wide, not 320")
    check(abs(files["w"] + pane["w"] - main["w"]) <= 1, f"{where}: the columns ({files['w']} + {pane['w']}) do not fill the main area ({main['w']})")
    check(filehead["h"] == 44, f"{where}: the file header is {filehead['h']} px high, not 44")
    check(line["h"] == 22 and line["font"] == 13, f"{where}: a line is {line['h']} px high at {line['font']} px, not 22 at 13")
    check(hunk["h"] == 28, f"{where}: a hunk mark is {hunk['h']} px high, not 28")
    check("3px" in added["shadow"] and "3px" in removed["shadow"] and added["shadow"] != removed["shadow"], f"{where}: the line edges are {added['shadow']!r} and {removed['shadow']!r}")
    check(added["bg"] != "rgba(0, 0, 0, 0)" and removed["bg"] != "rgba(0, 0, 0, 0)" and added["bg"] != removed["bg"], f"{where}: added and removed lines are not tinted differently ({added['bg']}, {removed['bg']})")
    expect(page.locator(".review-files-count")).to_have_text(words["files"])
    sideways(page, where)

    # A folded run opens on a click; the gap between two hunks is only a count.
    folds = page.locator("button.dp-fold")
    check(folds.count() >= 1, f"{where}: no run of unchanged lines was folded")
    before = page.locator(".dp-line").count()
    folds.first.click()
    check(page.locator(".dp-line").count() > before, f"{where}: opening a fold added no lines")
    check(page.locator(".dp-fold.gap").count() >= 1, f"{where}: the lines a patch leaves out between hunks are not counted")

    # Side by side: both columns, still 22 px a row.
    page.get_by_role("radio", name=words["split"]).click()
    page.wait_for_selector(".dp-body.split .dp-pair")
    shortest = page.evaluate("Math.min(...[...document.querySelectorAll('.dp-half')].map((el) => el.getBoundingClientRect().height))")
    check(shortest == 22, f"{where}: the shortest side-by-side row is {shortest} px high, not 22")
    blank = page.evaluate("Math.min(...[...document.querySelectorAll('.dp-half.blank')].map((el) => el.getBoundingClientRect().height))")
    check(blank == 22, f"{where}: the empty side of an added line is {blank} px high, not 22 (the page's own .empty rule must not reach it)")
    check(page.locator(".dp-half").count() % 2 == 0, f"{where}: the side-by-side halves do not pair")
    page.get_by_role("radio", name=words["unified"]).click()
    page.wait_for_selector(".dp-body.unified")

    # The viewed state: a box in the tree, the progress, kept across a reload, the next-file button.
    page.get_by_role("checkbox", name=words["viewed"]).first.click()
    expect(page.locator(".review-progress")).to_contain_text(words["progress"])
    page.reload()
    page.wait_for_selector(".review .dp-line")
    expect(page.locator(".review-progress")).to_contain_text(words["progress"])
    page.get_by_role("button", name=words["viewed_next"]).click()
    expect(page.locator(".dp-name b")).to_have_text("test_search_retry.py")
    expect(page.locator(".review-progress")).to_contain_text(words["progress"])
    page.get_by_role("button", name=words["viewed_next"]).click()
    expect(page.locator(".review-progress")).to_contain_text(words["progress"].replace("1", "2", 1))

    # The files that have no lines say what they are.
    page.locator(".review-pick", has_text="fallback.png").click()
    expect(page.locator(".dp-none")).to_contain_text(words["binary"])
    page.locator(".review-pick", has_text="search.example.toml").click()
    expect(page.locator(".dp-none")).to_contain_text(words["moved"])
    page.locator(".review-pick", has_text="old_probe.sh").click()
    expect(page.locator(".dp-line.del")).to_have_count(2)

    # The filter narrows the tree to the files that match.
    page.get_by_label(words["filter"]).fill("search")
    check(page.locator(".review-file").count() == 4, f"{where}: filtering by 'search' leaves {page.locator('.review-file').count()} files, not 4")
    page.get_by_label(words["filter"]).fill("")

    # The picker: four change sets, one a click away.
    page.locator(".changes-chip").click()
    page.wait_for_selector(".changes-pick-row")
    check(page.locator(".changes-pick-row").count() == 4, f"{where}: the picker lists {page.locator('.changes-pick-row').count()} change sets, not 4")
    check(page.locator(".changes-pick-row[aria-selected='true']").count() == 1, f"{where}: the picker does not mark the open change set")
    page.locator(".changes-pick-row", has_text="Memory").click()
    page.wait_for_selector(".review .dp-line")
    check(page.url.split("?")[0].endswith("/changes/p56"), f"{where}: the picker opened {page.url}")
    check(page.locator(".pagehead-actions .pill").count() == 1 and page.get_by_role("button", name=words["approve"]).count() == 0, f"{where}: a merged change set still offers a decision")
    page.close()

    # A decision: approve answers in the host's words and the page shows it merged.
    page = open_review(context, "changes/p57")
    page.get_by_role("button", name=words["approve"]).click()
    expect(page.locator(".toast")).to_contain_text(words["merged"])
    expect(page.locator(".pagehead-actions .pill")).to_be_visible()
    check(PROPOSALS.decided[-1][1].get("decision") == "approve", f"{where}: the approval sent {PROPOSALS.decided[-1]}")
    page.close()

    # A rejection carries the operator's reason to the agent.
    page = open_review(context, "changes/p58")
    page.get_by_role("button", name=words["reject"]).first.click()
    page.get_by_label(words["reason"]).fill("The table is out of date")
    page.locator(".sheet .btn.danger").click()
    page.wait_for_selector(".pagehead-actions .pill")
    sent = PROPOSALS.decided[-1]
    check(sent[0] == "p58" and sent[1] == {"decision": "reject", "reason": "The table is out of date"}, f"{where}: the rejection sent {sent}")
    page.close()

    # A diff the host cut says so under the last file.
    PROPOSALS.cut = True
    page = open_review(context, "changes/p57")
    page.locator(".review-pick", has_text="old_probe.sh").click()
    expect(page.locator(".dp-cut")).to_contain_text(words["cut"])
    page.close()
    PROPOSALS.cut = False

    # The empty list.
    stub.proposals_empty = True  # type: ignore[attr-defined]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/changes?token=t&lang={CURRENT['lang']}")
    expect(page.locator(".empty")).to_contain_text(words["none"])
    page.close()
    stub.proposals_empty = False  # type: ignore[attr-defined]
    context.close()


def large(browser) -> None:  # type: ignore[no-untyped-def]
    """The two-thousand-line diff: the longest task the render held the page for, and what it left in the DOM."""
    PROPOSALS.reset()
    context = browser.new_context(viewport={"width": 1920, "height": 1080}, color_scheme="dark")
    page = context.new_page()
    page.route("**/api/**", stub)
    page.add_init_script("""
      window.__long = [];
      try { new PerformanceObserver((list) => { for (const e of list.getEntries()) window.__long.push(e.duration); }).observe({ entryTypes: ['longtask'] }); } catch (e) {}
    """)
    page.goto(f"{BASE}/changes/p58?token=t&lang={CURRENT['lang']}")
    page.wait_for_selector(".review .dp-line")
    page.wait_for_timeout(500)
    stats = page.evaluate("""() => ({ nodes: document.querySelectorAll('*').length, lines: document.querySelectorAll('.dp-line').length, longest: Math.max(0, ...window.__long) })""")
    measured["large"] = stats
    # The file header stays at the top of its column while the long file scrolls under it.
    page.locator(".review-scroll").evaluate("el => { el.scrollTop = 3000; }")
    stuck = box(page, ".dp-head")
    scroller = box(page, ".review-scroll")
    check(abs(stuck["y"] - scroller["y"]) <= 1, f"the file header left the top of its column ({stuck['y']} against {scroller['y']})")
    check(stats["longest"] < 400, f"a 2,000-line diff held the page for {stats['longest']:.0f} ms in one task")
    check(stats["nodes"] < 20000, f"a 2,000-line diff leaves {stats['nodes']} nodes in the page")
    context.close()


def phone(browser) -> None:  # type: ignore[no-untyped-def]
    PROPOSALS.reset()
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark")
    page = open_review(context, "changes/p57")
    sideways(page, f"{CURRENT['lang']} phone review")
    check(page.locator(".review-decide .btn").count() == 2, f"{CURRENT['lang']} phone: the decision is not in the bottom bar")
    check(not page.locator(".review-files-body").is_visible(), f"{CURRENT['lang']} phone: the file list is open before it is asked for")
    page.locator(".review-files-toggle").click()
    check(page.locator(".review-files-body").is_visible(), f"{CURRENT['lang']} phone: the file list did not open")
    page.locator(".review-pick", has_text="WEBSEARCH.md").click()
    expect(page.locator(".dp-name b")).to_have_text("WEBSEARCH.md")
    check(not page.locator(".review-files-body").is_visible(), f"{CURRENT['lang']} phone: picking a file left the list open")
    sideways(page, f"{CURRENT['lang']} phone after picking a file")
    page.close()
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/changes?token=t&lang={CURRENT['lang']}")
    page.wait_for_selector(".proposal.erow")
    sideways(page, f"{CURRENT['lang']} phone list")
    context.close()


def run() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            CURRENT['lang'] = language
            desktop(browser, 1920, 1080)
            desktop(browser, 1366, 768)
            phone(browser)
        CURRENT['lang'] = "en"
        large(browser)
        browser.close()
    print(json.dumps(measured, indent=1, ensure_ascii=False))
    for problem in problems:
        print("FAIL", problem)
    return 1 if problems else UNHANDLED.report()



if __name__ == "__main__":
    sys.exit(run())
