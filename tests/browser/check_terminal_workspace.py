"""Drive the Terminals workspace through its edge cases, and refuse what does not behave.

The grid's own promises, one by one: four panes make a 2 × 2 grid; the layout switch hides panes
without dropping them (4 → 1 → 4 keeps every screen and every xterm element); a pane dragged by its
grip onto another swaps the two while its program keeps printing, with the same element and the same
socket; a drop on itself or outside the grid changes nothing; the splitters stop at 20 and 80 % and a
double click puts them back at half; a splitter drag ends in at most a couple of RESIZE frames per
pane; Alt+1…4 focuses a place and does not reach the shell; the layout, the places, the fractions and
the active pane survive a reload and the address names the active pane; opening a terminal already in
a pane focuses it instead of showing it twice; closing a pane leaves the others alone; a staff member
waiting for permission lights the bar's indicator and the pane's own; a title double-clicked is
renamed through the host; `/app/terminals` opens on the Workspace once panes are set up and on the
Overview once the operator chose it; at 1280 × 720 nothing scrolls sideways and no pane is narrower
than 200 px. The Russian page has Russian words, and no page throws.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_workspace.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import P1, S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, stub_requests, text, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
IDS = ["w1shell00000", "w2devsrv0000", "w3staff00000", "w4htop000000"]
SPARE = "w5spare00000"


class Check:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def that(self, ok: bool, problem: str) -> None:
        if not ok:
            self.problems.append(problem)
            print("PROBLEM:", problem)


def fleet() -> TerminalStub:
    """Four terminals for the grid (one a staff member waiting for permission) and a fifth to open."""
    term = TerminalStub(S1)
    term.add(IDS[0], title="bash · bakery", project_id=P1)
    term.add(IDS[1], title="npm run dev", project_id=P1, busy=True)
    term.add(IDS[2], title="Ira · Claude Code", owner_kind="staff", owner_id="st-ira", owner_label="Ira", project_id=P1,
             activity={"status": "permission", "label": "bash: npm install grammy", "level": "warn", "action": {"kind": "answer", "label": "Answer", "path": "/app/inbox"}})
    term.add(IDS[3], title="htop", env="host", owner_kind="free", owner_id="", cwd="/home/operator")
    term.add(SPARE, title="psql", project_id=P1)
    for i in [*IDS, SPARE]:
        term.emit(i, "".join(f"{i} line {n}\r\n" for n in range(30)))
    return term


def open_page(context, term: TerminalStub, path: str, lang: str = "en") -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    term.install(page)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors  # type: ignore[attr-defined]
    page.goto(f"{BASE}/{path}{'&' if '?' in path else '?'}token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".tp-ws, .term-card, .term-phone", timeout=20000)
    return page


def panes(page: Page) -> list[str]:
    """The terminal in each of the four places, in place order ('' for an empty one, '-' for none drawn)."""
    return page.evaluate("""() => [0, 1, 2, 3].map((i) => { const el = document.querySelector(`.tp-pane[data-slot='${i}']`); return el ? el.dataset.pane : '-'; })""")


def visible(page: Page) -> list[str]:
    return page.eval_on_selector_all(".tp-pane:not(.hidden)", "(p) => p.map((e) => e.dataset.pane)")


def box(page: Page, selector: str) -> dict:
    return page.eval_on_selector(selector, "(e) => { const b = e.getBoundingClientRect(); return { x: b.x, y: b.y, w: b.width, h: b.height }; }")


def mark_elements(page: Page) -> None:
    """Remembers each pane's xterm element, so a later step can tell a moved pane from a remounted one."""
    page.evaluate("() => { window.__xterms = {}; for (const p of document.querySelectorAll('.tp-pane')) { const x = p.querySelector('.xterm'); if (x) window.__xterms[p.dataset.pane] = x; } }")


def same_elements(page: Page, ids: list[str]) -> list[str]:
    """The terminals whose xterm element is no longer the one remembered."""
    return page.evaluate("(ids) => ids.filter((id) => { const p = document.querySelector(`.tp-pane[data-pane='${id}']`); return !p || p.querySelector('.xterm') !== window.__xterms[id]; })", ids)


def split_handle(page: Page, axis: str) -> dict:
    """A point on a splitter away from the middle, where the two splitters of a 2 × 2 grid cross and
    the other one is on top."""
    b = box(page, f".tp-split.{axis}")
    if axis == "col":
        return {"x": b["x"], "y": b["y"] + b["h"] * 0.2, "w": b["w"], "h": 4}
    return {"x": b["x"] + b["w"] * 0.2, "y": b["y"], "w": 4, "h": b["h"]}


def drag(page: Page, start: dict, end: tuple[float, float], steps: int = 12) -> None:
    page.mouse.move(start["x"] + start["w"] / 2, start["y"] + start["h"] / 2)
    page.mouse.down()
    page.mouse.move(end[0], end[1], steps=steps)
    page.mouse.up()
    page.wait_for_timeout(250)


def grid_steps(browser, c: Check) -> None:  # type: ignore[no-untyped-def]
    term = fleet()
    context = browser.new_context(viewport={"width": 1600, "height": 960}, color_scheme="dark")
    context.add_init_script(DEBUG)
    page = open_page(context, term, f"terminals/{IDS[0]}?with={','.join(IDS[1:])}")
    for i in IDS:
        wait_live(page, i)
    page.wait_for_timeout(600)

    # The 2 × 2 grid.
    boxes = [box(page, f".tp-pane[data-slot='{i}']") for i in range(4)]
    print("panes:", [(round(b["x"]), round(b["y"]), round(b["w"]), round(b["h"])) for b in boxes])
    c.that(panes(page) == IDS, f"the places hold {panes(page)}, not the address's order")
    c.that(len({round(b["x"]) for b in boxes}) == 2 and len({round(b["y"]) for b in boxes}) == 2, "four panes are not a 2 × 2 grid")
    c.that(page.locator(".tp-pane-head").count() == 4, "not every pane has its own header")
    c.that(page.locator(".tp-tab").count() == 4, "the bar does not have one tab per pane")

    # The staff member waiting for permission: the bar's indicator, the pane's own, the tab's word.
    c.that(page.locator(".tp-top .tp-waiting").count() == 1, "the bar does not say something is waiting")
    c.that(page.locator(f".tp-pane[data-pane='{IDS[2]}'] .tp-attn").count() == 1, "the waiting pane has no attention marker")
    c.that(page.locator(".tp-tab[data-slot='2']").get_attribute("data-state") == "waiting", "the waiting pane's tab does not say so")
    glow = page.eval_on_selector(f".tp-pane[data-pane='{IDS[2]}'] .tp-pane-head .tp-dot", "(e) => getComputedStyle(e).boxShadow")
    c.that("px" in glow and "inset" not in glow, f"the waiting dot does not glow: {glow}")
    c.that(page.locator(".tp-tab[data-slot='1']").get_attribute("data-state") == "working", "the busy dev server's tab does not say working")

    # 4 → 1 → 4 keeps every pane, its element and its scrollback.
    mark_elements(page)
    before = {i: len(text(page, i)) for i in IDS}
    sockets = {i: len(term.of(i)) for i in IDS}
    page.locator(".tp-layout[data-layout='1']").click()
    page.wait_for_timeout(300)
    c.that(visible(page) == [IDS[0]], f"one pane shows {visible(page)}")
    c.that(page.locator(".tp-pane.hidden").count() == 3, "the hidden panes were dropped, not hidden")
    page.locator(".tp-layout[data-layout='4']").click()
    page.wait_for_timeout(300)
    c.that(sorted(visible(page)) == sorted(IDS), f"back at four the grid shows {visible(page)}")
    c.that(not same_elements(page, IDS), f"4 → 1 → 4 remounted {same_elements(page, IDS)}")
    after = {i: len(text(page, i)) for i in IDS}
    c.that(all(after[i] >= before[i] for i in IDS), f"4 → 1 → 4 lost scrollback: {before} → {after}")
    c.that({i: len(term.of(i)) for i in IDS} == sockets, "4 → 1 → 4 opened new sockets")

    # Drag the first pane onto the last while its program prints: swapped, same element, same socket.
    grip = box(page, ".tp-pane[data-slot='0'] .tp-grip")
    target = boxes[3]
    page.mouse.move(grip["x"] + grip["w"] / 2, grip["y"] + grip["h"] / 2)
    page.mouse.down()
    for n in range(6):
        term.emit(IDS[0], f"streaming while dragged {n}\r\n")
        page.mouse.move(target["x"] + target["w"] * (0.2 + n * 0.1), target["y"] + target["h"] / 2, steps=3)
    c.that(page.locator(".tp-ghost").count() == 1, "no ghost follows the dragged pane")
    c.that(page.locator(".tp-pane.drop[data-slot='3']").count() == 1, "the drop target is not outlined")
    page.mouse.up()
    page.wait_for_timeout(400)
    c.that(panes(page) == [IDS[3], IDS[1], IDS[2], IDS[0]], f"after the swap the places hold {panes(page)}")
    c.that(not same_elements(page, IDS), f"the swap remounted {same_elements(page, IDS)}")
    c.that(len(term.of(IDS[0])) == sockets[IDS[0]], "the dragged pane reconnected")
    c.that(any("streaming while dragged 5" in line for line in text(page, IDS[0])), "output printed during the drag did not arrive")
    c.that(page.locator(".tp-pane.on[data-slot='3']").count() == 1, "the keyboard did not follow the dragged pane")

    # A drop on itself and a drop outside the grid change nothing.
    order = panes(page)
    drag(page, box(page, ".tp-pane[data-slot='1'] .tp-grip"), (boxes[1]["x"] + 80, boxes[1]["y"] + 120))
    c.that(panes(page) == order, f"a drop on itself changed the places: {panes(page)}")
    rail = box(page, ".tp-list")
    drag(page, box(page, ".tp-pane[data-slot='1'] .tp-grip"), (rail["x"] + 40, rail["y"] + 200))
    c.that(panes(page) == order, f"a drop outside the grid changed the places: {panes(page)}")
    c.that(page.locator(".tp-ghost").count() == 0, "the ghost stayed after the drop")

    # The splitters: clamped to 20 and 80 %, a double click back to half, and no RESIZE flood.
    grid = box(page, ".tp-grid")
    resizes = len(term.resizes())
    drag(page, split_handle(page, "col"), (grid["x"] + 5, grid["y"] + grid["h"] / 3), steps=16)
    page.wait_for_timeout(400)
    col = page.locator(".tp-split.col").get_attribute("aria-valuenow")
    left = box(page, ".tp-pane[data-slot='0']")["w"]
    print("column at the left edge:", col, "pane", round(left), "of", round(grid["w"]))
    c.that(col == "20", f"the column splitter went to {col} %, not 20")
    c.that(abs(left / grid["w"] - 0.2) < 0.02, f"the left pane is {left / grid['w']:.2f} of the grid, not 0.20")
    flood = term.resizes()[resizes:]
    per = {i: sum(1 for r in flood if r[0] == i) for i in IDS}
    print("RESIZE frames during the drag:", per)
    c.that(max(per.values()) <= 2, f"a splitter drag flooded RESIZE frames: {per}")
    drag(page, split_handle(page, "col"), (grid["x"] + grid["w"] - 5, grid["y"] + grid["h"] / 3), steps=8)
    c.that(page.locator(".tp-split.col").get_attribute("aria-valuenow") == "80", "the column splitter passed 80 %")
    h = split_handle(page, "col")
    page.mouse.dblclick(h["x"] + h["w"] / 2, h["y"] + 2)
    page.wait_for_timeout(200)
    c.that(page.locator(".tp-split.col").get_attribute("aria-valuenow") == "50", "a double click did not put the column back at half")
    drag(page, split_handle(page, "row"), (grid["x"] + grid["w"] / 4, grid["y"] + 3), steps=8)
    c.that(page.locator(".tp-split.row").get_attribute("aria-valuenow") == "20", "the row splitter passed 20 %")
    bottom = [box(page, f".tp-pane[data-slot='{i}']") for i in (2, 3)]
    top = [box(page, f".tp-pane[data-slot='{i}']") for i in (0, 1)]
    c.that(abs(bottom[0]["w"] - top[0]["w"]) < 2, "the bottom row does not follow the column fraction")
    drag(page, split_handle(page, "col"), (grid["x"] + grid["w"] * 0.3, grid["y"] + grid["h"] / 3), steps=6)
    col = page.locator(".tp-split.col").get_attribute("aria-valuenow")

    # Alt+3 focuses the third place and does not reach its shell as ESC 3.
    page.locator(".tp-pane[data-slot='0'] .term-screen").click()
    page.wait_for_timeout(200)
    page.keyboard.press("Alt+3")
    page.wait_for_timeout(300)
    third = panes(page)[2]
    c.that(page.locator(".tp-pane.on[data-slot='2']").count() == 1, "Alt+3 did not focus the third place")
    c.that(page.url.split("?")[0].endswith(f"/app/terminals/{third}"), f"the address does not follow the active pane: {page.url}")
    sent = term.inputs(panes(page)[0]) + term.inputs(third)
    c.that(b"\x1b3" not in sent, "Alt+3 also reached the shell")

    # Opening a terminal a pane already holds focuses it; one no pane holds lands in the active place.
    page.locator(f".tp-row[data-terminal='{IDS[1]}'] .tp-row-open").click()
    page.wait_for_timeout(300)
    c.that(page.locator(f".tp-pane.on[data-pane='{IDS[1]}']").count() == 1, "opening a shown terminal did not focus its pane")
    c.that(page.locator(f".tp-pane[data-pane='{IDS[1]}']").count() == 1, "opening a shown terminal showed it twice")

    # The reload keeps the places, the layout, the fraction and the active pane.
    state = panes(page)
    active = page.get_attribute(".tp-pane.on", "data-slot")
    page.reload()
    page.wait_for_selector(".tp-grid", timeout=15000)
    page.wait_for_timeout(500)
    c.that(panes(page) == state, f"after a reload the places are {panes(page)}, not {state}")
    c.that(page.get_attribute(".tp-pane.on", "data-slot") == active, "after a reload another pane is active")
    c.that(page.locator(".tp-split.col").get_attribute("aria-valuenow") == col, f"after a reload the column is {page.locator('.tp-split.col').get_attribute('aria-valuenow')}, not {col}")

    # Rename by a double click on the title.
    slot = page.get_attribute(f".tp-pane[data-pane='{IDS[0]}']", "data-slot")
    page.locator(f".tp-pane[data-pane='{IDS[0]}'] .tp-pane-title").dblclick()
    page.locator(f".tp-pane[data-pane='{IDS[0]}'] .tp-rename").fill("deploy")
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    patches = [b for m, p, b in term.requests if m == "PATCH" and p.endswith(IDS[0])]
    c.that(patches == [{"title": "deploy"}], f"the rename sent {patches}")
    c.that(b"deploy" not in term.inputs(IDS[0]), "the rename's keys reached the shell")
    page.wait_for_function(f"() => document.querySelector(\".tp-pane[data-pane='{IDS[0]}'] .tp-pane-title\")?.textContent.includes('deploy')", timeout=8000)

    # Close a pane: its place is empty, the others stay; the empty pane offers the spare terminal.
    others = [p for i, p in enumerate(panes(page)) if str(i) != slot]
    page.locator(f".tp-pane[data-slot='{slot}'] .tp-pane-close").click()
    page.wait_for_timeout(300)
    c.that(panes(page)[int(slot or 0)] == "", f"the closed place holds {panes(page)[int(slot or 0)]!r}")
    c.that([p for i, p in enumerate(panes(page)) if str(i) != slot] == others, "closing a pane moved the others")
    c.that(not stub_requests(term, "DELETE", IDS[0]), "closing a pane removed the terminal")
    pick = page.locator(f".tp-pane[data-slot='{slot}'] .tp-empty-row[data-terminal='{SPARE}']")
    c.that(pick.count() == 1, "the empty pane does not offer the terminal no pane shows")
    if pick.count():
        pick.click()
        page.wait_for_timeout(300)
        c.that(panes(page)[int(slot or 0)] == SPARE, "picking a terminal in the empty pane did not put it there")

    # Maximise and back.
    page.locator(".tp-pane[data-slot='1'] .tp-pane-max").click()
    page.wait_for_timeout(300)
    c.that(len(visible(page)) == 1 and box(page, ".tp-pane[data-slot='1']")["w"] > grid["w"] * 0.9, "maximise did not give the pane the grid")
    page.locator(".tp-pane[data-slot='1'] .tp-pane-max").click()
    page.wait_for_timeout(300)
    c.that(len(visible(page)) == 4, "the maximised pane did not give the grid back")

    # The waiting indicator goes to the waiting terminal.
    page.locator(".tp-waiting").click()
    page.wait_for_timeout(300)
    c.that(page.locator(f".tp-pane.on[data-pane='{IDS[2]}']").count() == 1, "the waiting indicator did not focus the waiting pane")

    # Which view opens: the Workspace once panes are set up; the Overview once it is chosen.
    page.goto(f"{BASE}/terminals?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".tp-ws, .term-card", timeout=15000)
    c.that(page.locator(".tp-ws").count() == 1, "/app/terminals did not open the set-up Workspace")
    page.locator(".tp-top .tp-views [data-view='overview']").click()
    page.wait_for_selector(".term-card", timeout=10000)
    page.goto(f"{BASE}/terminals?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".tp-ws, .term-card", timeout=15000)
    c.that(page.locator(".term-card").count() > 0 and page.locator(".tp-ws").count() == 0, "/app/terminals did not remember the Overview")
    c.that(not page.errors, f"the page threw: {page.errors}")  # type: ignore[attr-defined]
    context.close()


def narrow(browser, c: Check) -> None:  # type: ignore[no-untyped-def]
    """1280 × 720 with the list open: nothing sideways, no pane under 200 px, the bar on one row."""
    term = fleet()
    context = browser.new_context(viewport={"width": 1280, "height": 720}, color_scheme="dark")
    page = open_page(context, term, f"terminals/{IDS[0]}?with={','.join(IDS[1:])}")
    page.wait_for_timeout(800)
    sideways = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    widths = page.eval_on_selector_all(".tp-pane:not(.hidden)", "(p) => p.map((e) => Math.round(e.getBoundingClientRect().width))")
    bar = box(page, ".tp-top")
    head = box(page, ".tp-pane-head")
    print("1280 × 720: panes", widths, "bar", round(bar["h"]), "pane header", round(head["h"]))
    c.that(sideways <= 0, f"the workspace scrolls sideways by {sideways} px")
    c.that(min(widths) >= 200, f"a pane is narrower than 200 px: {widths}")
    c.that(bar["h"] <= 36.5, f"the bar is {bar['h']} px, more than a row")
    c.that(head["h"] <= 32.5, f"a pane header is {head['h']} px, more than a dense row")
    big = page.evaluate("""() => [...document.querySelectorAll('.tp-ws *')].filter((e) => !e.closest('.xterm') && e.childNodes.length && [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()) && parseFloat(getComputedStyle(e).fontSize) > 14).map((e) => e.className + ' ' + getComputedStyle(e).fontSize)""")
    c.that(not big, f"text above 14 px in the workspace: {big[:5]}")
    context.close()


def russian(browser, c: Check) -> None:  # type: ignore[no-untyped-def]
    term = fleet()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, term, f"terminals/{IDS[0]}?with={','.join(IDS[1:])}", lang="ru")
    page.wait_for_timeout(500)
    words = page.locator(".tp-top").inner_text()
    print("Russian bar:", words.replace("\n", " | "))
    c.that("Рабочее место" in words and "Обзор" in words, f"the Russian bar has no Russian view names: {words!r}")
    c.that("Ждут вас" in words, f"the Russian bar does not say who waits: {words!r}")
    c.that(not page.errors, f"the Russian page threw: {page.errors}")  # type: ignore[attr-defined]
    context.close()


def phone(browser, c: Check) -> None:  # type: ignore[no-untyped-def]
    """A phone shows one pane at a time and switches between them."""
    term = fleet()
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_page(context, term, f"terminals/{IDS[0]}?with={','.join(IDS[1:3])}")
    page.wait_for_selector(".term-phone .tp-switch", timeout=15000)
    tabs = page.locator(".tp-switch-tab").count()
    c.that(tabs == 3, f"the phone's switcher has {tabs} tabs, not 3")
    c.that(page.locator(".term-phone .term-view").count() == 1, "a phone shows more than one pane")
    page.locator(f".tp-switch-tab[data-pane='{IDS[1]}']").tap()
    page.wait_for_selector(f".term-phone .term-view[data-terminal-view='{IDS[1]}']", timeout=10000)
    c.that(page.url.split("?")[0].endswith(f"/app/terminals/{IDS[1]}"), f"the phone's address does not follow the pane: {page.url}")
    sideways = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    c.that(sideways <= 0, f"the phone's workspace scrolls sideways by {sideways} px")
    c.that(not page.errors, f"the phone page threw: {page.errors}")  # type: ignore[attr-defined]
    context.close()


def main() -> int:
    expect_app(BASE)
    c = Check()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        grid_steps(browser, c)
        narrow(browser, c)
        russian(browser, c)
        phone(browser, c)
        browser.close()
    missed = UNHANDLED.report()
    print("problems:", len(c.problems))
    return 1 if c.problems or missed else 0


if __name__ == "__main__":
    sys.exit(main())
