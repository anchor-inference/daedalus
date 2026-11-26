"""Drive the Terminals screen, its full-screen grid and the ways to reach it, and refuse what does not behave.

The list: the header counts what runs and says who waits; the memory meter is there, coloured by the
projection, with what the numbers are one press away; the filters, the search and the project groups
hold the right rows; a row says its command, its owner and where it runs; the three new-terminal flows
POST the right body (and go to the new terminal); past the cap the operator is asked and the request
goes again with ``confirm``; Remove all finished sends DELETE.

The workspace: four panes each send exactly one size when shown, each pane's own header follows its
terminal, and a workspace left behind sends no size however the window changes. Navigation: ``g t``, the menu, the palette, and the More sheet on a 390 px phone.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminals_screen.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app, open_drawer  # noqa: E402
from screenshots import P1, S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, load_answer, run, stub_requests, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LENS = f"try {{ localStorage.setItem('daedalus.project', '{P1}'); }} catch (e) {{}}"


def ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fleet() -> TerminalStub:
    """Six terminals: tests failing in a session, a dev server, a staff member waiting, htop on the host,
    a host staff member, and a psql that ended with an error."""
    term = TerminalStub(S1)
    term.add("k1tests00000", title="bash · bakery-site", owner_kind="session", owner_label="Checkout page", project_id=P1, created_at=ago(minutes=38),
             preview=[[run("✗", 1), run(" promo code SPRING10")], [run("   Expected: "), run("1080", 2)], [run("   Received: "), run("1200", 1)],
                      [run(" Tests: "), run("1 failed", 1), run(", "), run("2 passed", 2)], [run("bakery-site", 2), run(" $ ")]])
    term.add("k2devsrv0000", title="npm run dev", owner_kind="session", owner_label="Checkout page", project_id=P1, created_at=ago(minutes=38),
             preview=[[run("14:02:11 ", d=True), run("[vite]", 6), run(" hmr update")]])
    term.add("k3staff00000", title="Ira · Claude Code", owner_kind="staff", owner_id="st-ira", owner_label="Ira", project_id=P1,
             activity={"label": "waiting for permission · 1 min", "level": "warn", "action": {"label": "Answer", "path": "/app/inbox"}},
             preview=[[run("△ Permission required", 3)], [run("  bash: npm install grammy")]])
    term.add("k4htop000000", title="htop", env="host", owner_kind="free", owner_id="", cwd="/home/operator", clients=0, last_input_at=ago(hours=1),
             preview=[[run("  1", 6), run("["), run("|||||||", 2), run("   34%]")]])
    term.add("k5hoststaff0", title="Naya · OpenCode", env="host", owner_kind="staff", owner_id="st-naya", owner_label="Naya", project_id=P1)
    term.add("k6psql000000", title="psql · orders", owner_kind="project", owner_id=P1, owner_label="Bakery site", project_id=P1,
             status="exited", exit_code=1, exited_at=ago(minutes=20), preview=[[run("orders=# \\q")]])
    return term


def open_page(context, term: TerminalStub, path: str, wait: str):  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    term.install(page)
    page.goto(f"{BASE}/{path}{'&' if '?' in path else '?'}token=t&scheme=dark&lang=en")
    page.wait_for_selector(wait, timeout=20000)
    return page


def ids_of(page) -> list[str]:  # type: ignore[no-untyped-def]
    return page.eval_on_selector_all(".tp-list .tp-row", "(rows) => rows.map((r) => r.dataset.terminal)")


def the_list(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    term = fleet()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script(LENS)
    page = open_page(context, term, "terminals", ".tp-list .tp-row")
    page.wait_for_timeout(400)

    subtitle = page.locator(".pagehead .pagehead-title .sub").inner_text().strip()
    print("header:", subtitle)
    if subtitle != "5 open · 2 on host · 2 with staff":
        problems.append(f"the header counts {subtitle!r}, not '5 open · 2 on host · 2 with staff'")
    waiting = page.locator(".pagehead .tp-waiting").inner_text().strip()
    if waiting != "Waiting for you: 1":
        problems.append(f"the header says {waiting!r} of who waits, not 'Waiting for you: 1'")

    # The memory meter at the list's foot: the terminals' share of the machine, in words and as a bar;
    # the projection for the cap is one press away, coloured by its level, with the key to its fills.
    meter = page.locator(".tp-mem")
    if not meter.count():
        problems.append("the list has no memory meter")
    else:
        line = meter.inner_text().replace("\n", " | ")
        print("memory meter:", line, "| level", meter.get_attribute("data-level"))
        if "1.4 GB of 62 GB" not in line:
            problems.append(f"the memory meter does not say the terminals' share of the machine: {line!r}")
        if meter.get_attribute("data-level") != "ok":
            problems.append(f"the memory meter is {meter.get_attribute('data-level')!r} on a machine at 44 %")
        if page.locator(".tp-mem [role=meter]").get_attribute("aria-valuenow") != "1":
            problems.append("the meter's value is not the terminals' gigabytes")
        page.locator(".tp-mem-button").click()
        page.wait_for_selector(".tp-mem-pop .loadbar", timeout=5000)
        bar = page.locator(".tp-mem-pop .loadbar")
        # 20 GB of 62 in use, 5 running at 1.4 GB, 15 more at 500 MB each would make 27.3 GB = 44 % — ok.
        words = bar.inner_text().replace("\n", " | ")
        print("load bar:", words, "| level", bar.get_attribute("data-level"))
        if bar.get_attribute("data-level") != "ok":
            problems.append(f"the load bar is {bar.get_attribute('data-level')!r} on a machine at 44 %")
        if "5 terminals now 1.4 GB" not in words or "20 sessions ≈ 8.7 GB of 62 GB" not in words or "44 % of memory" not in words:
            problems.append(f"the load bar does not say what runs now and what the cap would cost: {words!r}")
        # Each fill of the track has its key; the daemon's own memory is in the terminals' one.
        if "Other programs" not in words or "Kept for the limit of 20" not in words or "Free now" not in words:
            problems.append(f"the load bar has no key to its fills: {words!r}")
        if "60 MB of the terminal daemon itself" not in (bar.locator(".loadbar-key[title]").get_attribute("title") or ""):
            problems.append("the load bar does not count the terminal service's own memory")
        colour = page.eval_on_selector(".loadbar-now", "(e) => getComputedStyle(e).backgroundColor")
        ok = page.evaluate("() => { const d = document.createElement('i'); d.style.color = 'var(--ok)'; document.body.append(d); const c = getComputedStyle(d).color; d.remove(); return c; }")
        if colour != ok:
            problems.append(f"the bar's 'now' part is {colour}, not the ok token {ok}")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

    # Groups: the project, then the terminals of none.
    groups = page.eval_on_selector_all(".tp-list-group .tp-label-name", "(s) => s.map((e) => e.textContent)")
    print("groups:", groups)
    if groups != ["Bakery site", "No project"]:
        problems.append(f"the groups are {groups}")

    # Filters: the counts on the chips say what each one shows, and the rows are those.
    expected = {
        "all": {"k1tests00000", "k2devsrv0000", "k3staff00000", "k4htop000000", "k5hoststaff0", "k6psql000000"},
        "container": {"k1tests00000", "k2devsrv0000", "k3staff00000"},
        "host": {"k4htop000000", "k5hoststaff0"},
        "staff": {"k3staff00000", "k5hoststaff0"},
        "finished": {"k6psql000000"},
    }
    for name, want in expected.items():
        chip = page.locator(f".tp-filter[data-filter='{name}']")
        chip.click()
        page.wait_for_timeout(150)
        got = set(ids_of(page))
        if got != want:
            problems.append(f"the {name} filter shows {sorted(got)}, not {sorted(want)}")
        if chip.locator(".tp-filter-n").inner_text().strip() != str(len(want)):
            problems.append(f"the {name} chip counts {chip.locator('.tp-filter-n').inner_text()!r}, not {len(want)}")
        if chip.get_attribute("aria-pressed") != "true":
            problems.append(f"the {name} chip is not pressed after it was chosen")
    page.locator(".tp-filter[data-filter='all']").click()

    # The search narrows by title, owner or command.
    page.locator(".tp-list-search").fill("grammy")
    page.wait_for_timeout(150)
    if ids_of(page) != ["k3staff00000"] and ids_of(page) != []:
        problems.append(f"searching the folder or the command of a terminal shows {ids_of(page)}")
    page.locator(".tp-list-search").fill("Ira")
    page.wait_for_timeout(150)
    if ids_of(page) != ["k3staff00000"]:
        problems.append(f"searching an owner shows {ids_of(page)}")
    page.locator(".tp-list-search").fill("")

    # A row says what it is in three lines: the title and its tag, the command, the owner and where it runs.
    row = lambda i: page.locator(f".tp-row[data-terminal='{i}']")  # noqa: E731
    lines = {i: [row(i).locator(sel).inner_text().strip() for sel in (".tp-row-title", ".tp-row-cmd", ".tp-row-owner")] for i in ("k1tests00000", "k3staff00000", "k4htop000000", "k6psql000000")}
    print("rows:", lines)
    if lines["k1tests00000"][2] != "chat «Checkout page»":
        problems.append(f"a chat's terminal names its owner {lines['k1tests00000'][2]!r}")
    if lines["k3staff00000"][2] != "staff Ira":
        problems.append(f"a staff member's terminal names its owner {lines['k3staff00000'][2]!r}")
    if lines["k4htop000000"][2] != "you · host" or row("k4htop000000").get_attribute("data-env") != "host":
        problems.append(f"a host terminal does not say it runs on the host: {lines['k4htop000000']}")
    if not lines["k6psql000000"][1].startswith("code 1"):
        problems.append(f"a finished terminal does not say how it ended: {lines['k6psql000000']}")
    tags = {i: row(i).locator(".tp-row-tag").inner_text().strip() for i in ("k1tests00000", "k3staff00000", "k6psql000000")}
    print("tags:", tags)
    if tags["k1tests00000"] != "38m" or tags["k3staff00000"] != "waiting" or tags["k6psql000000"] != "20m":
        problems.append(f"the row tags are {tags}")
    if row("k3staff00000").locator(".tp-row-tag").get_attribute("data-level") != "wait":
        problems.append("the waiting tag is not drawn as one")

    # Forgetting what ended is one command of the list's menu, and sends DELETE.
    page.locator(".tp-list-actions").click()
    page.locator("[role=menuitem]", has_text="Remove all finished").click()
    page.wait_for_timeout(500)
    deletes = [p for m, p, _ in term.requests if m == "DELETE"]
    if deletes != ["/api/terminals/k6psql000000"]:
        problems.append(f"Remove all finished sent {deletes}")
    if page.locator("[data-terminal='k6psql000000']").count():
        problems.append("the removed terminal is still listed")

    # A row opens its terminal in the panes, and the pane's owner is a link to where it lives.
    page.locator("[data-terminal='k1tests00000'] .tp-row-open").click()
    page.wait_for_selector(".tp-ws .term-view[data-terminal-view='k1tests00000']", timeout=10000)
    if not page.url.split("?")[0].endswith("/app/terminals/k1tests00000"):
        problems.append(f"Open went to {page.url}")
    owner = page.locator(".tp-pane[data-pane='k1tests00000'] .tp-pane-owner")
    if not owner.count() or owner.get_attribute("href") != f"/app/agents/{S1}":
        problems.append("the pane's owner is not a link to the session")
    if "tp-row" in (page.locator("[data-terminal='k1tests00000']").first.get_attribute("class") or "") and "on" not in (page.locator(".tp-list [data-terminal='k1tests00000']").get_attribute("class") or "").split():
        problems.append("the opened terminal's row is not the chosen one")

    # The three ways to open a free terminal.
    def create(choice: str, then=None) -> dict | None:  # type: ignore[no-untyped-def]
        before = len(stub_requests(term, "POST", "/api/terminals"))
        page.locator(".term-new-button").click()
        page.wait_for_selector(".term-pick-pop", timeout=5000)
        page.locator(f".term-pick-pop [data-choice='{choice}']").click()
        if then:
            then()
        page.wait_for_url("**/app/terminals/new*", timeout=10000)
        bodies = stub_requests(term, "POST", "/api/terminals")[before:]
        page.wait_for_selector(".tp-ws .term-view", timeout=10000)
        return bodies[-1] if bodies else None

    body = create("container")
    print("container:", body)
    if body != {"env": "container", "owner_kind": "free", "cwd": "/home/operator/work/bakery", "project_id": P1}:
        problems.append(f"a container terminal was asked for with {body}")
    body = create("host")
    print("host:", body)
    if body != {"env": "host", "owner_kind": "free", "cwd": "/home/operator"}:
        problems.append(f"a host terminal was asked for with {body}")

    def choose() -> None:
        page.locator(".term-pick-folder .segmented button", has_text="Host").click()
        page.locator(".term-pick-folder .term-new-path .field").fill("/srv/scratch")
        page.locator(".term-pick-folder .term-new-actions .btn.primary").click()

    body = create("folder", choose)
    print("chosen folder:", body)
    if body != {"env": "host", "owner_kind": "free", "cwd": "/srv/scratch"}:
        problems.append(f"a terminal in a chosen folder was asked for with {body}")

    # At the cap: asked, then the same request with confirm.
    term.cap = term.running()
    before = len(stub_requests(term, "POST", "/api/terminals"))
    page.locator(".term-new-button").click()
    page.locator(".term-pick-pop [data-choice='container']").click()
    page.wait_for_selector(".dialog", timeout=5000)
    question = page.locator(".dialog").inner_text().replace("\n", " | ")
    print("over the cap:", question)
    if f"{term.cap} terminals are running" not in question:
        problems.append(f"the cap question does not say how many run: {question!r}")
    page.locator(".dialog .btn", has_text="Open anyway").click()
    page.wait_for_url("**/app/terminals/new*", timeout=10000)
    bodies = stub_requests(term, "POST", "/api/terminals")[before:]
    print("cap requests:", bodies)
    if len(bodies) != 2 or bodies[0].get("confirm") or bodies[1].get("confirm") is not True:
        problems.append(f"past the cap the requests were {bodies}, not one refused and one confirmed")

    # A machine that would not carry the cap: the meter turns to the attention colour and the
    # projection says so, without refusing.
    term.cap = 20
    term.load = load_answer(used_gb=54)
    page.goto(f"{BASE}/terminals?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".tp-mem[data-level='bad']", timeout=15000)
    colour = page.eval_on_selector(".tp-mem-fill", "(e) => getComputedStyle(e).backgroundColor")
    attention = page.evaluate("() => { const d = document.createElement('i'); d.style.color = 'var(--attention)'; document.body.append(d); const c = getComputedStyle(d).color; d.remove(); return c; }")
    page.locator(".tp-mem-button").click()
    page.wait_for_selector(".tp-mem-pop .loadbar", timeout=5000)
    warning = page.locator(".loadbar-warning")
    print("bad meter:", colour, "|", warning.inner_text() if warning.count() else None)
    if colour != attention:
        problems.append(f"a meter past 90 % is {colour}, not the attention token {attention}")
    if not warning.count():
        problems.append("a cap the machine cannot carry is not called out")
    page.keyboard.press("Escape")
    if page.locator(".term-new-button").is_disabled():
        problems.append("a heavy machine disabled New terminal: a warning must not become a refusal")
    context.close()


def the_grid(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    term = fleet()
    ids = ["k1tests00000", "k2devsrv0000", "k3staff00000", "k4htop000000"]
    for i in ids:
        term.emit(i, f"terminal {i}\r\n")
    # Each pane's size goes out before its snapshot arrives, and the host's confirmation comes after
    # that snapshot, which is drawn at the old size: the order that once left a pane at 80 × 24 after
    # it had sent 80 × 23. Without the delay only a loaded machine produced it.
    term.snapshot_delay = 0.3
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script(DEBUG)
    page = open_page(context, term, f"terminals/{ids[0]}?with={','.join(ids[1:])}", ".tp-grid")
    for i in ids:
        wait_live(page, i)
    page.wait_for_timeout(800)
    sizes = {i: [(c, r) for _, _, c, r in term.resizes(i)] for i in ids}
    print("sizes per pane:", sizes)
    for i in ids:
        if len(sizes[i]) != 1:
            problems.append(f"pane {i} sent {len(sizes[i])} sizes when shown, not one")
        elif sizes[i][0][0] < 20 or sizes[i][0][1] < 4:
            problems.append(f"pane {i} sent a size too small to be a real pane: {sizes[i][0]}")
    # What each pane sent is what its terminal was fitted to in that pane, not a size from elsewhere.
    # A pane may pass through the attach's size for a moment while the snapshot drawn at it is still
    # being parsed, so the settled grid is what is compared; one that settles at a size other than the
    # one it sent is the defect (the host's confirmation once overtook that snapshot and was undone).
    grids: dict = {}
    for _ in range(25):
        grids = {i: page.evaluate("(id) => window.__terminals.size(id)", i) for i in ids}
        if all(not sizes[i] or (grids[i] and (grids[i]["cols"], grids[i]["rows"]) == sizes[i][-1]) for i in ids):
            break
        page.wait_for_timeout(200)
    for i in ids:
        if sizes[i] and grids[i] and (grids[i]["cols"], grids[i]["rows"]) != sizes[i][-1]:
            problems.append(f"pane {i} holds {grids[i]} but sent {sizes[i][-1]}")
    boxes = page.eval_on_selector_all(".tp-pane:not(.hidden)", "(p) => p.map((e) => { const b = e.getBoundingClientRect(); return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]; })")
    print("panes:", boxes)
    if len({(b[0], b[1]) for b in boxes}) != 4 or len({b[0] for b in boxes}) != 2 or len({b[1] for b in boxes}) != 2:
        problems.append(f"four panes are not a 2 × 2 grid: {boxes}")

    # Each pane has its own header; the one touched last is the active one.
    page.locator(".tp-pane[data-pane='k4htop000000'] .term-screen").click()
    page.wait_for_timeout(200)
    head = page.locator(".tp-pane[data-pane='k4htop000000'] .tp-pane-title").inner_text().strip()
    if head != "htop" or not page.locator(".tp-pane[data-pane='k4htop000000'] .tp-pane-head .term-env.host").count():
        problems.append(f"the htop pane's header does not name it as a host terminal: {head!r}")
    if not page.locator(".tp-pane.on[data-pane='k4htop000000']").count():
        problems.append("the touched pane is not the active one")

    # Narrow the grid to one pane: the three it hides may not size anything any more, whatever the
    # window does.
    count = len(term.resizes())
    page.locator(".tp-layouts [role=radio]").nth(0).click()
    page.wait_for_timeout(300)
    for w in (1200, 1000, 1300):
        page.set_viewport_size({"width": w, "height": 800})
        page.wait_for_timeout(200)
    page.wait_for_timeout(400)
    later = [r for r in term.resizes()[count:] if r[0] != ids[0]]
    if later:
        problems.append(f"panes hidden by the layout sent sizes: {later}")
    context.close()


def navigation(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    term = fleet()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, term, "inbox", ".pagehead")
    page.locator("body").click()
    page.keyboard.press("g")
    page.keyboard.press("t")
    try:
        page.wait_for_url("**/app/terminals", timeout=5000)
    except Exception:  # noqa: BLE001
        problems.append(f"g t went to {page.url}")
    page.goto(f"{BASE}/inbox?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".sb-pinned-nav [data-nav='menu']", timeout=10000)
    # A daily destination has one place: pinned in the sidebar; the secondary menu must not duplicate it.
    page.locator(".sb-pinned-nav [data-nav='terminals']").click()
    page.wait_for_url("**/app/terminals", timeout=5000)
    # The palette offers the running terminals and a new one; they are read from the listing already made.
    page.wait_for_selector(".tp-list .tp-row", timeout=10000)
    page.keyboard.press("Control+k")
    page.locator(".palette-sheet .field").fill("terminal")
    labels = page.locator(".palette-row").all_inner_texts()
    print("palette:", labels)
    if not any("Open terminal: htop" in x for x in labels) or not any("New container terminal" in x for x in labels) or not any("New host terminal" in x for x in labels):
        problems.append(f"the palette does not offer the terminals: {labels}")
    page.keyboard.press("Escape")
    context.close()

    phone = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_page(phone, term, "agents", ".ph-top .ph-menu")
    # Terminals is a destination of the drawer on a phone, two taps from anywhere.
    open_drawer(page)
    more = page.locator(".ph-drawer-root.open [data-nav='terminals']")
    if not more.count():
        problems.append("the drawer has no Terminals")
    else:
        more.first.click()
        page.wait_for_selector(".ph-tcard", timeout=10000)
        width = page.evaluate("() => document.documentElement.scrollWidth")
        if width > 390:
            problems.append(f"the Terminals screen scrolls sideways on a phone: {width} px")
        cards = page.eval_on_selector_all(".ph-tcard", "(c) => c.map((e) => Math.round(e.getBoundingClientRect().width))")
        if cards and max(cards) > 390 - 2 * 16:
            problems.append(f"a card is wider than the phone allows: {cards}")
        # The capacity is one line under the bar, and the whole picture a tap away in its sheet.
        if not page.locator(".ph-cap .ph-cbar").count():
            problems.append("the phone's Terminals has no capacity line")
        else:
            page.locator(".ph-cap").tap()
            try:
                page.wait_for_selector(".ph-capacity .ph-legend", timeout=5000)
            except Exception:  # noqa: BLE001
                problems.append("the capacity line does not open the capacity sheet")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        page.locator("[data-terminal='k1tests00000'] .ph-tcard-open").tap()
        # On a phone the full-screen address shows the phone's terminal, with its keys (mobile.tsx).
        page.wait_for_selector(".term-phone .term-view[data-terminal-view='k1tests00000']", timeout=10000)
        if page.locator("nav.tabbar").count():
            problems.append("a bottom bar stays under a terminal shown full screen on a phone")
    phone.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        the_list(browser, problems)
        the_grid(browser, problems)
        navigation(browser, problems)
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    missed = UNHANDLED.report()
    return 1 if problems or missed else 0


if __name__ == "__main__":
    sys.exit(main())
