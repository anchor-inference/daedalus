"""Ending and opening terminals, in a real browser: refuse what does not behave.

Ending. A tab's ✕ and its middle click end the terminal — at once for a shell the host says sits at
its prompt, after a question naming the command for one that runs something, and Cancel keeps it.
Keeping a terminal without its tab is the tab's right-click Detach, which sends nothing. Ctrl+Shift+W
ends the terminal in front. The dock is hidden by a chevron, not a cross. On the Overview a card has
End beside Open; "End idle" lists what it ends; a selection ends what was ticked after one question
that names the staff member's session it would stop; a group ends all of its own; the Finished
filter removes every finished card. On a phone the card and the terminal's header have End.

Opening. The dock's "+" and the Overview's "New terminal" ask the same question: the container,
the host, another folder; 1/2/3 choose, the arrows and Enter too, Escape closes; an environment that
cannot be used is disabled with the reason; the place chosen last is marked and takes the keyboard;
Ctrl+Shift+` asks it too.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_ending.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import copy
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import P1, S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import (  # noqa: E402
    DEBUG,
    ENVS,
    TerminalStub,
    dock_state,
    open_session,
    run,
    stub_requests,
    wait_live,
)

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
# The pictures in the other scheme too: SCHEME=light.
SCHEME = os.environ.get("SCHEME", "dark")
LENS = f"try {{ localStorage.setItem('daedalus.project', '{P1}'); }} catch (e) {{}}"


def ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def kills(term: TerminalStub) -> list[str]:
    return [p.split("/")[3] for m, p, _ in term.requests if m == "POST" and p.endswith("/kill")]


def the_dock(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    envs = copy.deepcopy(ENVS)
    envs[1]["available"] = False
    envs[1]["reason"] = "not_configured"
    term = TerminalStub(S1, envs=envs)
    term.add("e1idle000000", title="bash · idle")
    term.add("e2busy000000", title="bash · tests", busy=True, running_text="npm test -- checkout")
    term.add("e3idle000000", title="bash · spare")
    for i in term.terms:
        term.emit(i, f"{i} $ \r\n")
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=SCHEME)
    context.add_init_script(DEBUG)
    context.add_init_script(dock_state(S1, ["e1idle000000", "e2busy000000", "e3idle000000"], active="e2busy000000"))
    context.add_init_script("try { localStorage.removeItem('daedalus.term.lastPlace'); } catch (e) {}")
    page = open_session(context, term, stub, BASE, S1)
    wait_live(page, "e2busy000000")

    hide = page.locator(".term-tools .term-hide")
    if hide.get_attribute("aria-label") != "Hide the terminal panel":
        problems.append(f"the dock's hide button is {hide.get_attribute('aria-label')!r}")

    # ✕ on a busy terminal asks and names the command; Cancel keeps it running and tabbed.
    page.locator(".term-tab[data-tab='e2busy000000'] .term-tab-x").click()
    page.wait_for_selector(".dialog", timeout=5000)
    question = page.locator(".dialog").inner_text().replace("\n", " | ")
    print("✕ asks:", question)
    if "Running: npm test -- checkout" not in question:
        problems.append(f"✕ on a busy terminal does not name what runs: {question!r}")
    page.locator(".dialog .dialog-actions .btn").first.click()
    page.wait_for_timeout(300)
    if kills(term) or not page.locator(".term-tab[data-tab='e2busy000000']").count():
        problems.append("Cancel did not keep the busy terminal")

    # The middle button is the same ✕: confirmed, the terminal ends and its tab goes.
    page.locator(".term-tab[data-tab='e2busy000000'] .term-tab-title").click(button="middle")
    page.wait_for_selector(".dialog", timeout=5000)
    page.locator(".dialog .btn.danger").click()
    page.wait_for_timeout(500)
    if kills(term) != ["e2busy000000"] or page.locator(".term-tab[data-tab='e2busy000000']").count():
        problems.append(f"the middle click did not end the terminal and take its tab: {kills(term)}")

    # The tab's right click: Rename, Detach, End, End the others. Detach sends nothing and the
    # terminal comes back from the ▾ menu.
    page.locator(".term-tab[data-tab='e1idle000000']").click(button="right")
    page.wait_for_selector(".context-menu, .menu", timeout=5000)
    items = [i.strip() for i in page.locator(".context-menu button, .menu button").all_inner_texts()]
    print("tab menu:", items)
    for wanted in ("Rename", "Detach, keep running", "End", "End the others"):
        if not any(i.startswith(wanted) for i in items):
            problems.append(f"the tab's menu has no {wanted!r}: {items}")
    before = len([r for r in term.requests if r[0] != "GET"])
    page.locator(".context-menu button, .menu button", has_text="Detach, keep running").first.click()
    page.wait_for_timeout(300)
    if page.locator(".term-tab[data-tab='e1idle000000']").count() or len([r for r in term.requests if r[0] != "GET"]) != before:
        problems.append("Detach did not just take the tab away")
    page.locator(".term-tabs button[aria-label='More terminals']").click()
    if not page.locator(".menu button", has_text="bash · idle").count():
        problems.append("a detached terminal is not offered back in the ▾ menu")
    page.keyboard.press("Escape")

    # Ctrl+Shift+W ends the terminal in front: a shell at its prompt, so without a question.
    page.locator(".term-tab[data-tab='e3idle000000']").click()
    page.locator(".term-slot.left .xterm").click()
    page.keyboard.press("Control+Shift+KeyW")
    page.wait_for_timeout(500)
    if page.locator(".dialog").count() or kills(term)[-1:] != ["e3idle000000"]:
        problems.append(f"Ctrl+Shift+W did not end the idle terminal in front: {kills(term)}")

    # "+" asks where. The host is down: disabled, with the reason. 1/2/3 are shown; Escape closes.
    page.locator(".term-tabs button[aria-label='New terminal']").click()
    page.wait_for_selector(".term-pick-pop", timeout=5000)
    rows = page.locator(".term-pick-pop .term-pick-row[data-choice]:not([data-choice='sandbox'])")
    choices = [rows.nth(i).get_attribute("data-choice") for i in range(rows.count())]
    if choices != ["container", "host", "folder"]:
        problems.append(f"the question offers {choices}")
    host = page.locator(".term-pick-pop [data-choice='host']")
    print("host:", host.is_disabled(), host.inner_text().replace("\n", " | "))
    if not host.is_disabled() or "Host terminal is not installed" not in host.inner_text():
        problems.append("an unavailable host is not disabled with its reason")
    if page.locator(".term-pick-pop [data-choice='sandbox']").count() != 1:
        problems.append("the sandbox switch is not in the question")
    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / "terminal-picker-dock.png"))
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    if page.locator(".term-pick-pop").count():
        problems.append("Escape did not close the question")

    # The keyboard: Ctrl+Shift+` asks, the arrows move, Enter chooses.
    page.locator(".chat-scroll").click(position={"x": 20, "y": 20})
    page.keyboard.press("Control+Shift+Backquote")
    page.wait_for_selector(".term-pick-pop", timeout=5000)
    focused = page.evaluate("() => document.activeElement?.dataset?.choice")
    if focused != "container":
        problems.append(f"the question did not give the keyboard to the first place: {focused}")
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    made = stub_requests(term, "POST", "/api/terminals")
    print("created:", made)
    if made != [{"env": "container", "owner_kind": "session", "owner_id": S1}]:
        problems.append(f"Enter did not open a container terminal of the session: {made}")

    # The place chosen last is marked, and takes the keyboard next time.
    page.locator(".term-tabs button[aria-label='New terminal']").click()
    page.wait_for_selector(".term-pick-pop", timeout=5000)
    marked = page.locator(".term-pick-pop [aria-current='true']")
    if marked.get_attribute("data-choice") != "container" or "last time" not in marked.inner_text():
        problems.append("the place chosen last is not marked")
    page.keyboard.press("3")
    page.wait_for_selector(".term-pick-folder", timeout=5000)
    page.locator(".term-pick-folder .term-new-path .field").fill("/srv/workspaces/scratch")
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    made = stub_requests(term, "POST", "/api/terminals")
    if made[-1:] != [{"env": "container", "owner_kind": "session", "owner_id": S1, "cwd": "/srv/workspaces/scratch"}]:
        problems.append(f"another folder did not open there: {made[-1:]}")
    context.close()


def fleet() -> TerminalStub:
    term = TerminalStub(S1)
    term.add("o1idle000000", title="bash · forgotten", owner_kind="free", owner_id="", cwd="/srv/workspaces/main", clients=0, created_at=ago(days=7), project_id=P1,
             preview=[[run("main $ ")]])
    term.add("o2devsrv0000", title="npm run dev", owner_kind="session", owner_label="Checkout page", project_id=P1, busy=True, running_text="npm run dev",
             preview=[[run("[vite]", 6), run(" ready")]])
    term.add("o3staff00000", title="Ira · Claude Code", owner_kind="staff", owner_id="st-ira", owner_label="Ira", project_id=P1, running_text="claude --resume",
             preview=[[run("● Working")]])
    term.add("o4ended00000", title="psql · orders", owner_kind="free", owner_id="", status="exited", exit_code=0, exited_at=ago(minutes=5))
    term.add("o5ended00000", title="make", owner_kind="free", owner_id="", status="exited", exit_code=2, exited_at=ago(minutes=9))
    term.add("o6loose00000", title="bash · loose", owner_kind="free", owner_id="", cwd="/tmp", preview=[[run("$ ")]])
    return term


def open_page(context, term: TerminalStub, path: str, wait: str, lang: str = "en"):  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    term.install(page)
    page.goto(f"{BASE}/{path}{'&' if '?' in path else '?'}token=t&scheme={SCHEME}&lang={lang}")
    page.wait_for_selector(wait, timeout=20000)
    return page


def the_overview(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    term = fleet()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=SCHEME)
    context.add_init_script(LENS)
    page = open_page(context, term, "terminals?view=overview", ".term-card")
    page.wait_for_timeout(400)

    # A terminal nobody has looked at for a week is marked as one to clean up.
    if page.locator("[data-terminal='o1idle000000'][data-stale='1']").count() != 1:
        problems.append("the forgotten terminal is not marked stale")
    if page.locator("[data-terminal='o2devsrv0000'][data-stale='1']").count():
        problems.append("a watched terminal is marked stale")
    # Every running card has End beside Open; a finished one has neither.
    for i in ("o1idle000000", "o2devsrv0000", "o3staff00000"):
        if page.locator(f"[data-terminal='{i}'] .term-card-foot [data-action='end']").count() != 1:
            problems.append(f"the card {i} has no End")
    if page.locator("[data-terminal='o4ended00000'] [data-action='end']").count():
        problems.append("a finished card offers End")
    page.screenshot(path=str(SHOTS / "terminals-overview-ending.png"))

    # End idle: only the idle, unwatched shell, after a question that lists it.
    page.locator("[data-action='end-idle']").click()
    page.wait_for_selector(".dialog", timeout=5000)
    question = page.locator(".dialog").inner_text().replace("\n", " | ")
    print("end idle asks:", question)
    if "bash · forgotten" not in question or "npm run dev" in question or "Ira" in question:
        problems.append(f"End idle does not list exactly the idle terminal: {question!r}")
    page.locator(".dialog .btn.danger").click()
    page.wait_for_timeout(600)
    if kills(term) != ["o1idle000000"]:
        problems.append(f"End idle ended {kills(term)}")

    # A selection: the dev server and the staff member's session, named in one question.
    page.locator("[data-action='select']").click()
    page.locator("[data-terminal='o2devsrv0000'] .term-card-check input").check()
    page.locator("[data-terminal='o3staff00000'] .term-card-open").click()
    if page.url.split("?")[0].endswith("o3staff00000"):
        problems.append("a press on a card while selecting opened it")
    page.screenshot(path=str(SHOTS / "terminals-overview-selecting.png"))
    page.locator("[data-action='end-selected']").click()
    page.wait_for_selector(".dialog", timeout=5000)
    question = page.locator(".dialog").inner_text().replace("\n", " | ")
    print("end selected asks:", question)
    if "End 2 terminals?" not in question or "npm run dev" not in question or "Ira's session" not in question:
        problems.append(f"End selected does not name what stops: {question!r}")
    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / "terminals-overview-end-selected.png"))
    page.locator(".dialog .btn.danger").click()
    page.wait_for_timeout(600)
    if sorted(kills(term)) != sorted(["o1idle000000", "o2devsrv0000", "o3staff00000"]):
        problems.append(f"End selected ended {kills(term)}")

    # One card's End: an idle shell goes at once.
    page.locator("[data-terminal='o6loose00000'] [data-action='end']").click()
    page.wait_for_timeout(500)
    if page.locator(".dialog").count() or kills(term)[-1:] != ["o6loose00000"]:
        problems.append("a card's End on an idle shell did not end it at once")

    # Finished: one button forgets them all.
    page.locator(".chip[data-filter='finished']").click()
    page.locator("[data-action='remove-finished']").click()
    page.wait_for_timeout(600)
    deletes = sorted(p.split("/")[3] for m, p, _ in term.requests if m == "DELETE")
    print("removed:", deletes)
    if not {"o4ended00000", "o5ended00000"} <= set(deletes):
        problems.append(f"Remove all finished removed {deletes}")

    # The Overview's "New terminal" asks the same question as the dock's "+".
    page.locator(".chip[data-filter='all']").click()
    page.locator(".term-new-button").click()
    page.wait_for_selector(".term-pick-pop", timeout=5000)
    rows = page.locator(".term-pick-pop .term-pick-row[data-choice]:not([data-choice='sandbox'])")
    if [rows.nth(i).get_attribute("data-choice") for i in range(rows.count())] != ["container", "host", "folder"]:
        problems.append("the Overview's question is not the dock's")
    page.keyboard.press("2")
    page.wait_for_url("**/app/terminals/new*", timeout=10000)
    made = stub_requests(term, "POST", "/api/terminals")
    if made[-1:] != [{"env": "host", "owner_kind": "free", "cwd": "/home/operator"}]:
        problems.append(f"2 did not open a host terminal at home: {made}")
    context.close()

    # The group's End all, in Russian too.
    term = fleet()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=SCHEME)
    context.add_init_script(LENS)
    page = open_page(context, term, "terminals?view=overview", ".term-card", lang="ru")
    page.wait_for_timeout(400)
    page.locator(f".term-group[data-group='{P1}'] [data-action='end-group']").click()
    page.wait_for_selector(".dialog", timeout=5000)
    question = page.locator(".dialog").inner_text().replace("\n", " | ")
    print("end group asks:", question)
    if "Завершить 3 терминала" not in question or "сессия сотрудника Ira" not in question:
        problems.append(f"the group's End all does not say what it ends: {question!r}")
    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / "terminals-overview-end-group-ru.png"))
    page.locator(".dialog .btn.danger").click()
    page.wait_for_timeout(600)
    if sorted(kills(term)) != sorted(["o1idle000000", "o2devsrv0000", "o3staff00000"]):
        problems.append(f"the group's End all ended {kills(term)}")
    context.close()


def the_phone(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    term = fleet()
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=SCHEME, is_mobile=True, has_touch=True)
    context.add_init_script(LENS)
    context.add_init_script(DEBUG)
    page = open_page(context, term, "terminals", ".ph-tcard")
    page.wait_for_timeout(400)
    if page.locator("[data-terminal='o2devsrv0000'] [data-action='end']").count() != 1:
        problems.append("a phone card has no End")
    page.screenshot(path=str(SHOTS / "terminals-phone-cards.png"))
    page.locator("[data-terminal='o2devsrv0000'] [data-action='end']").click()
    page.wait_for_selector(".dialog", timeout=5000)
    if "Running: npm run dev" not in page.locator(".dialog").inner_text():
        problems.append("the phone's End does not name what runs")
    page.locator(".dialog .btn.danger").click()
    page.wait_for_timeout(500)
    if kills(term) != ["o2devsrv0000"]:
        problems.append(f"the phone's End ended {kills(term)}")

    # The phone's "+" asks the same three places, as a sheet.
    page.locator(".ph-terms .ph-topbar button[aria-label='New terminal'], .ph-terms button[aria-label='New terminal']").first.click()
    page.wait_for_selector(".term-pick-sheet", timeout=5000)
    if page.locator(".term-pick-sheet .term-pick-row[data-choice]:not([data-choice='sandbox'])").count() != 3:
        problems.append("the phone's question does not offer three places")
    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / "terminals-phone-picker.png"))
    page.keyboard.press("Escape")

    # A terminal's own header carries End.
    page.locator("[data-terminal='o6loose00000'] .ph-tcard-open").click()
    page.wait_for_selector(".term-phone-head", timeout=10000)
    if page.locator(".term-phone-head .term-phone-end").count() != 1:
        problems.append("the phone terminal's header has no End")
    page.screenshot(path=str(SHOTS / "terminal-phone-header.png"))
    context.close()


SHOTS = Path(os.environ.get("SHOTS", "/tmp")) / "terminal-ending"


def main() -> int:
    expect_app(BASE)
    SHOTS.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        the_dock(browser, problems)
        the_overview(browser, problems)
        the_phone(browser, problems)
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    missed = UNHANDLED.report()
    return 1 if problems or missed else 0


if __name__ == "__main__":
    sys.exit(main())
