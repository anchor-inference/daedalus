"""Drive the Terminals status line, and refuse what does not behave.

Under each workspace pane: a staff member's CLI shows its model and effort, a context meter on the
window the CLI reported, the cache share, the output and the cost, each in its theme colour
(`--data-*`), and the pane's header the folder and the branch with a dirty mark; a CLI that reported no window shows the count and no
meter; one waiting for permission says so in the wait colour, and the line follows the listing when a
member starts waiting; a plain shell shows its last command and its exit; a terminal the host knows
little about shows what it knows and never "undefined". A narrow pane drops the cache and the output
first and nothing spills out of its line; every value is in the tooltip. The four panes' headers at
1440 px keep their titles readable. The Russian page
has Russian words, and no page throws. The same colour check runs in a second theme (THEME, default
`paper`, the light one), since the line must follow whichever theme the reader picked.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_statusline.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, agent_usage, expect_app, terminal_agent  # noqa: E402
from screenshots import P1, S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import TerminalStub, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SECOND_THEME = os.environ.get("THEME", "paper")
CLAUDE, CODEX, SHELL, BARE = "s1claude0000", "s2codex00000", "s3shell00000", "s4bare000000"
IDS = [CLAUDE, CODEX, SHELL, BARE]


class Check:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def that(self, ok: bool, problem: str) -> None:
        if not ok:
            self.problems.append(problem)
            print("PROBLEM:", problem)


def fleet() -> TerminalStub:
    """Two staff members (one with every number, one waiting whose CLI gave no window), a shell whose
    last command failed, and a shell the host knows nothing more about."""
    term = TerminalStub(S1)
    term.add(CLAUDE, title="Ira · Claude Code", owner_kind="staff", owner_id="st-ira", owner_label="Ira", project_id=P1,
             cwd="/home/operator/work/bakery-site/.agents/worktrees/ira",
             activity={"status": "working", "label": "", "level": "ok"},
             agent=terminal_agent("claude", model="opus", effort="high", state="working", usage=agent_usage(context=61_000, window=200_000, input_tokens=412_000, cached=371_000, output=18_400, cost=2.84)),
             git={"branch": "staff/ira", "detached": False, "dirty": True})
    term.add(CODEX, title="Naya · Codex", owner_kind="staff", owner_id="st-naya", owner_label="Naya", project_id=P1, cwd="/home/operator/work/bakery-bot",
             activity={"status": "working", "label": "", "level": "ok"},
             agent=terminal_agent("codex", model="gpt-5.1-codex", effort=None, state="working", usage=agent_usage(context=9_000, window=None, input_tokens=0, cached=0, output=0, cost=None)),
             git={"branch": "bot-commands", "detached": False, "dirty": False})
    term.add(SHELL, title="bash · bakery", project_id=P1, cwd="/home/operator/work/bakery-site",
             git={"branch": "main", "detached": False, "dirty": True}, last_command={"command": "npm test -- checkout", "exit_code": 1, "at": "2026-09-24T09:00:00Z"})
    term.add(BARE, title="htop", env="host", owner_kind="free", owner_id="", cwd="/srv")
    for i in IDS:
        term.emit(i, "".join(f"{i} line {n}\r\n" for n in range(20)))
    return term


def open_page(context, term: TerminalStub, path: str, lang: str = "en", theme: str = "") -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    term.install(page)
    if theme:
        page.add_init_script(f"try {{ localStorage.setItem('daedalus.appearance', JSON.stringify({{follow: false, theme: {json.dumps(theme)}}})); }} catch (e) {{}}")
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors  # type: ignore[attr-defined]
    page.goto(f"{BASE}/{path}{'&' if '?' in path else '?'}token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".tp-ws", timeout=20000)
    return page


def foot(page: Page, id_: str) -> str:
    return f".tp-pane[data-pane='{id_}'] .tp-pane-foot"


def seg_text(page: Page, id_: str, seg: str) -> str:
    """What a segment shows; "" when it is not drawn or a narrow line dropped it."""
    loc = page.locator(f"{foot(page, id_)} [data-seg='{seg}']")
    return loc.inner_text() if loc.count() and loc.is_visible() else ""


def colour_of(page: Page, selector: str) -> str:
    return page.eval_on_selector(selector, "(e) => getComputedStyle(e).color")


def token(page: Page, name: str) -> str:
    """A theme token resolved to the colour a text of it is drawn in."""
    return page.evaluate("(name) => { const p = document.createElement('span'); p.style.color = `var(${name})`; document.body.appendChild(p); const c = getComputedStyle(p).color; p.remove(); return c; }", name)


def colours(page: Page, c: Check, where: str) -> None:
    for selector, name in ((f"{foot(page, CLAUDE)} .tsl-ctx", "--data-context"), (f"{foot(page, CLAUDE)} .tsl-cache", "--data-cache"),
                           (f"{foot(page, CLAUDE)} .tsl-out", "--data-output"), (f".tp-pane[data-pane='{CLAUDE}'] .tp-pane-cwd", "--fg-3")):
        got, want = colour_of(page, selector), token(page, name)
        c.that(got == want, f"{where}: {selector} is drawn in {got}, not {name} ({want})")


def workspace(browser, c: Check) -> None:  # type: ignore[no-untyped-def]
    """A 1920 px window, where a pane of a 2 × 2 grid holds the whole line."""
    term = fleet()
    context = browser.new_context(viewport={"width": 1920, "height": 1080}, color_scheme="dark")
    # The operator's own terminal is the active pane: it has no hold line to take room from the others' lines.
    page = open_page(context, term, f"terminals/{BARE}?layout=4&panes={','.join(IDS)}")
    for i in IDS:
        wait_live(page, i)
    page.wait_for_timeout(500)

    # A staff member with every number.
    line = page.locator(foot(page, CLAUDE)).inner_text().replace("\n", " ")
    print("claude:", line)
    c.that(seg_text(page, CLAUDE, "agent").split() == ["opus", "high"], f"the model and effort are not shown: {seg_text(page, CLAUDE, 'agent')!r}")
    where = page.locator(f".tp-pane[data-pane='{CLAUDE}'] .tp-pane-cwd").inner_text()
    c.that("staff/ira*" in where, f"the header's branch or its dirty mark is missing: {where!r}")
    c.that("…/worktrees/ira" in where, f"the header's folder is not shortened to its end: {where!r}")
    c.that(not page.locator(f"{foot(page, CLAUDE)} [data-seg='where']").count(), "the foot repeats the folder the header says")
    c.that(seg_text(page, CLAUDE, "context") == "61k/200k", f"the context is {seg_text(page, CLAUDE, 'context')!r}, not 61k/200k")
    meter = page.locator(f"{foot(page, CLAUDE)} .tsl-meter")
    c.that(meter.count() == 1 and meter.get_attribute("aria-valuenow") == "31", "the context meter is missing or does not say 31 %")
    c.that(seg_text(page, CLAUDE, "cache") == "cache 90%", f"the cache share is {seg_text(page, CLAUDE, 'cache')!r}")
    c.that(seg_text(page, CLAUDE, "output") == "out 18k", f"the output is {seg_text(page, CLAUDE, 'output')!r}")
    c.that(seg_text(page, CLAUDE, "cost") == "≈$2.84", f"the cost is {seg_text(page, CLAUDE, 'cost')!r}")
    tip = page.locator(f"{foot(page, CLAUDE)} .tsl").get_attribute("title") or ""
    c.that("Context: 61,000 of 200,000 tokens (31%)" in tip and "Folder: /home/operator/work/bakery-site/.agents/worktrees/ira" in tip, f"the tooltip does not carry the full values: {tip!r}")
    colours(page, c, "the default theme")
    size = page.eval_on_selector(f"{foot(page, CLAUDE)} .tsl", "(e) => ({ h: e.getBoundingClientRect().height, f: parseFloat(getComputedStyle(e).fontSize) })")
    foot_h = page.eval_on_selector(foot(page, CLAUDE), "(e) => e.getBoundingClientRect().height")
    # 12 px since nothing is drawn under the 12 px floor; the foot around it is the page's 36 px.
    c.that(size["h"] <= 25 and size["f"] <= 12, f"the status line is {size}, more than a 24 px line of 12 px text")
    c.that(abs(foot_h - 36) <= 1, f"the pane's foot is {foot_h} px, not 36")

    # A CLI that reported no window: a count and no meter; nothing else invented.
    print("codex:", page.locator(foot(page, CODEX)).inner_text().replace("\n", " "))
    c.that(page.locator(f"{foot(page, CODEX)} .tsl-meter").count() == 0, "a meter is drawn without the CLI's window")
    c.that(seg_text(page, CODEX, "context") == "9k", f"the context count is {seg_text(page, CODEX, 'context')!r}")
    for seg in ("cache", "output", "cost"):
        c.that(not seg_text(page, CODEX, seg), f"a {seg} segment is drawn from no number")
    c.that(seg_text(page, CODEX, "agent") == "gpt-5.1-codex", f"the codex model is {seg_text(page, CODEX, 'agent')!r}")

    # A plain shell: the last command and how it ended; no agent.
    print("shell:", page.locator(foot(page, SHELL)).inner_text().replace("\n", " "))
    c.that("npm test -- checkout" in seg_text(page, SHELL, "command") and "exit 1" in seg_text(page, SHELL, "command"), f"the shell's last command is {seg_text(page, SHELL, 'command')!r}")
    c.that(page.locator(f"{foot(page, SHELL)} [data-seg='command']").get_attribute("data-result") == "failed", "a failed command is not marked failed")
    c.that(not seg_text(page, SHELL, "agent"), "a plain shell is given an agent")

    # Little known: the folder in the header, an empty foot, and no word for what is missing.
    bare = page.locator(foot(page, BARE)).inner_text()
    print("bare:", bare.replace("\n", " "))
    c.that(page.locator(f"{foot(page, BARE)} .tsl").count() == 0, "the bare terminal has a status line though nothing is known of it")
    c.that(bare.strip() == "This is your terminal · type right in it", f"the bare terminal's foot says {bare!r}, not who types")
    c.that(page.locator(f".tp-pane[data-pane='{BARE}'] .tp-pane-cwd").inner_text().strip() == "/srv", "the bare terminal's header does not say its folder")
    every = page.locator(".tp-pane-foot").all_inner_texts()
    c.that(not any(w in " ".join(every) for w in ("undefined", "null", "NaN", "[term.")), f"a line says what is missing: {every}")

    # The listing changes: the member now waits for permission, and the line says so in the wait colour.
    term.terms[CODEX].agent = terminal_agent("codex", model="gpt-5.1-codex", effort=None, state="waiting", status="permission", waiting_for="bash: npm install grammy", usage=None)
    term.terms[CODEX].activity = {"status": "permission", "label": "bash: npm install grammy", "level": "warn", "action": {"kind": "answer", "label": "Answer", "path": "/app/inbox"}}
    page.wait_for_selector(f"{foot(page, CODEX)} [data-seg='waiting']", timeout=12000)
    waiting = seg_text(page, CODEX, "waiting")
    c.that("waiting" in waiting and "npm install grammy" in waiting, f"the waiting segment says {waiting!r}")
    c.that(colour_of(page, f"{foot(page, CODEX)} .tsl-wait") == token(page, "--data-wait"), "the waiting segment is not in the wait colour")
    c.that(page.locator(".pagehead .tp-waiting").count() == 1, "the header does not say something is waiting")
    c.that(not page.errors, f"the workspace threw: {page.errors}")  # type: ignore[attr-defined]
    context.close()


def headers(browser, c: Check) -> None:  # type: ignore[no-untyped-def]
    """The four panes at 1440 px with the list shown keep their titles whole, and the list's rows say the
    same state their panes do."""
    term = fleet()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, term, f"terminals/{CLAUDE}?layout=4&panes={','.join(IDS)}")
    wait_live(page, CLAUDE)
    page.wait_for_timeout(400)
    shown = page.eval_on_selector_all(".tp-pane-head .tp-pane-title .truncate", "(ts) => ts.map((t) => ({ text: t.textContent, whole: t.scrollWidth <= t.clientWidth + 1 }))")
    print("titles at 1440:", shown)
    c.that(len(shown) == 4 and all(t["whole"] for t in shown), f"a pane's title is cut at 1440: {shown}")
    pane = page.locator(f".tp-pane[data-pane='{CLAUDE}']").get_attribute("data-state")
    row = page.locator(f".tp-row[data-terminal='{CLAUDE}']").get_attribute("data-state")
    c.that(pane == row == "working", f"the pane says {pane!r} and its row {row!r}, not working in both")
    context.close()


def narrow(browser, c: Check) -> None:  # type: ignore[no-untyped-def]
    """Four panes in a 1024 px window: the line drops cache and output and spills out of nothing."""
    term = fleet()
    context = browser.new_context(viewport={"width": 1024, "height": 720}, color_scheme="dark")
    page = open_page(context, term, f"terminals/{CLAUDE}?layout=4&panes={','.join(IDS)}")
    wait_live(page, CLAUDE)
    page.wait_for_timeout(400)
    width = page.eval_on_selector(f"{foot(page, CLAUDE)} .tsl", "(e) => e.clientWidth")
    hidden = page.eval_on_selector_all(f"{foot(page, CLAUDE)} .tsl-cache, {foot(page, CLAUDE)} .tsl-out", "(es) => es.map((e) => getComputedStyle(e).display)")
    print("narrow line:", width, "px; cache and output:", hidden, "|", page.locator(foot(page, CLAUDE)).inner_text().replace("\n", " "))
    c.that(width <= 560 and hidden == ["none", "none"], f"a {width} px line keeps its cache and output: {hidden}")
    spill = page.evaluate("""() => [...document.querySelectorAll('.tsl')].flatMap((line) => { const box = line.getBoundingClientRect();
        return [...line.querySelectorAll('.tsl-seg')].filter((s) => getComputedStyle(s).display !== 'none' && s.getBoundingClientRect().right > box.right + 1).map((s) => s.dataset.seg); })""")
    c.that(not spill, f"segments spill out of their line: {spill}")
    c.that(page.locator(f"{foot(page, CLAUDE)} .tsl-meter").count() == 1, "the meter is dropped from a narrow pane")
    sideways = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    c.that(sideways <= 0, f"the page scrolls sideways by {sideways} px")
    context.close()


def russian(browser, c: Check) -> None:  # type: ignore[no-untyped-def]
    term = fleet()
    context = browser.new_context(viewport={"width": 1920, "height": 1080}, color_scheme="dark")
    page = open_page(context, term, f"terminals/{BARE}?layout=4&panes={','.join(IDS)}", lang="ru", theme=SECOND_THEME)
    wait_live(page, CLAUDE)
    page.wait_for_timeout(400)
    cache = seg_text(page, CLAUDE, "cache")
    tip = page.locator(f"{foot(page, CLAUDE)} .tsl").get_attribute("title") or ""
    print("Russian:", page.locator(foot(page, CLAUDE)).inner_text().replace("\n", " "), "| theme", SECOND_THEME)
    c.that(cache == "кэш 90%", f"the Russian cache share is {cache!r}")
    c.that("Контекст: " in tip and "Ветка: staff/ira, есть незакоммиченные изменения" in tip, f"the Russian tooltip is {tip!r}")
    c.that("код 1" in seg_text(page, SHELL, "command"), f"the Russian exit is {seg_text(page, SHELL, 'command')!r}")
    colours(page, c, f"the {SECOND_THEME} theme")
    c.that(not page.errors, f"the Russian page threw: {page.errors}")  # type: ignore[attr-defined]
    context.close()


def main() -> int:
    expect_app(BASE)
    c = Check()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        workspace(browser, c)
        headers(browser, c)
        narrow(browser, c)
        russian(browser, c)
        browser.close()
    missed = UNHANDLED.report()
    print("problems:", len(c.problems))
    return 1 if c.problems or missed else 0


if __name__ == "__main__":
    sys.exit(main())
