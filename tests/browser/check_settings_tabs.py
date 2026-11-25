"""Settings as many short sections, in a real browser.

The sections were split so that each holds one subject (the models' page alone had held presets,
providers, accounts, the key proxy, free models and every task's model). What is checked, against
the settings index the page and the agent's documentation search both read:

- every row the index lists is drawn on its own section's page: opened at
  ``/app/settings/<section>``, the page carries ``data-setting="<anchor>"`` — so a link the agent
  hands out never opens a section without its row. A handful of rows show only in a state the page
  is in by choice (an engine picked, the companion switched on); the check puts the page in it;
- an address written before the split still lands: each old section id and the models page's old
  tabs open the section that holds their content now, and an old address naming a row that moved
  (``/app/settings/voice#asr.transcriber``) opens the row's new section with the row in view and
  blinking;
- the list of sections, at 1440 × 900 and on a 390 px phone, in English and Russian: every group's
  header in its order, every section under it, nothing sideways, and on a desktop the open
  section's entry in view inside the list however far down it is.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_settings_tabs.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app, terminal_load  # noqa: E402
from browser_stub import browser_load, render_scenes  # noqa: E402
from check_settings_browser import BROWSER, prepared  # noqa: E402
from screenshots import SETTINGS, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
INDEX = json.loads((Path(__file__).resolve().parents[2] / "miniapp/src/screens/settings-index.json").read_text(encoding="utf-8"))
GROUPS = ["general", "voice", "models", "autonomy", "tools", "integrations", "system"]
GROUP_WORDS = {
    "en": ["General", "Voice", "Models", "Autonomy", "Tools", "Connections", "Security and system"],
    "ru": ["Основное", "Голос", "Модели", "Автономия", "Инструменты", "Подключения", "Безопасность и система"],
}
# Drawn only in a state no stub puts the page in: a voice of several speakers chosen and installed.
CONDITIONAL = {"companion.tts.local_speaker"}
# Drawn only inside the desktop application, or by an installation that says it is native: a browser
# looking at the stub is neither, and these sections leave the list there on purpose.
ELSEWHERE = {"desktop", "address"}

# Addresses from before the split, each with where it must land: the old ids and tabs by themselves,
# and old addresses of rows that moved, one from every section that lost rows.
OLD = [
    ("heartbeat", None, "schedules"),
    ("terminals", None, "environments"),
    ("models?tab=providers", None, "models"),
    ("providers", None, "models"),
    ("providers", "providers.list", "models"),
    ("models?tab=free", None, "free-models"),
    ("voice", "asr.transcriber", "dictation"),
    ("voice", "stt.models", "speech-models"),
    ("models", "vision.preset", "tasks"),
    ("models?tab=providers", "keyproxy.upstreams", "keyproxy"),
    ("limits", "compaction.preset", "context"),
    ("limits", "limits.usd_total", "budget"),
    ("limits", "subagents.models", "tasks"),
    ("tools", "tools.web.search.backend", "search"),
    ("tools", "mcp.servers", "mcp"),
    ("chat", "telegram.mode", "telegram"),
    ("chat", "scheduler.topic_mode", "schedules"),
    ("heartbeat", "heartbeat.preset", "schedules"),
    ("environments", "browser.profiles", "browser"),
    ("environments", "browser.watch_mode", "browser-safety"),
    ("memory", "memory.reset", "memory-upkeep"),
    ("rules", "self_change.approval", "approvals"),
    ("appearance", "appearance.font", "text"),
]


def everything_shown() -> dict:
    """The settings with every engine among the search fallbacks and the cloud transcriber chosen, so
    the rows that belong to one engine or to the cloud endpoint are on their pages."""
    settings = copy.deepcopy(SETTINGS)
    settings["tools"]["web"]["search"]["fallback"] = ["duckduckgo", "serper", "keenable", "tavily", "exa"]
    settings["tools"]["web"]["search"]["backend"] = "searxng"
    settings["asr"] = {**settings.get("asr", {}), "transcriber": "", "fallback": ""}
    settings["browser"] = copy.deepcopy(BROWSER)
    return settings


SCENES: list = []


def open_page(browser, width: int, lang: str) -> Page:  # type: ignore[no-untyped-def]
    phone = width < 1024
    context = browser.new_context(viewport={"width": width, "height": 900 if not phone else 844}, color_scheme="dark", is_mobile=phone, has_touch=phone)
    # The companion is this browser's choice; on, its model's row is drawn.
    context.add_init_script("try { localStorage.setItem('daedalus.pet', 'on'); } catch {}")
    page = context.new_page()
    # A configured browser, so the three sections of the agent's browser draw their cards rather than
    # the one that says there is none.
    bs = prepared(SCENES[0])

    def api(route) -> None:  # type: ignore[no-untyped-def]
        path = route.request.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]
        if rel.startswith("/api/browsers"):
            return bs.route(route)
        if rel == "/api/workloads/load":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"terminals": terminal_load(), "browsers": browser_load(running=len(bs.running)), "together": None}))
        return stub(route)

    page.route("**/api/**", api)
    # Each row that blinks is written down as it starts: a page that draws at once can blink and stop
    # before a busy machine hands the check its turn to look.
    context.add_init_script("""window.__flashed = new Set();
      new MutationObserver((changes) => { for (const c of changes) if (c.target.classList?.contains('setting-flash')) window.__flashed.add(c.target.dataset.setting); })
        .observe(document, { subtree: true, attributes: true, attributeFilter: ['class'] });""")
    body = json.dumps(everything_shown())

    def settings(route) -> None:  # type: ignore[no-untyped-def]
        if route.request.method != "GET":
            return route.fallback()
        return route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/api/settings", settings)
    return page


# How long a condition may take to come true before it is called a failure. A wait that is met returns
# at once, so this costs nothing on a quiet machine; it is long because a loaded one starts the app,
# redirects and draws a section at a tenth of the speed, and a limit that a slow start can reach says
# nothing about the page.
BOUND_MS = 30_000


def drawn(page: Page, section: str, lang: str, want: set[str]) -> set[str]:
    page.goto(f"{BASE}/settings/{section}?token=t&lang={lang}")
    page.wait_for_selector(".settings-col .card, .settings-col .set-card, .settings-col .settings-row, .settings-col .empty", timeout=BOUND_MS)
    # Cards that read their own listing draw their rows after it answers: waited for, up to a limit.
    try:
        page.wait_for_function("(want) => want.every((key) => document.querySelector(`.settings-col [data-setting=\"${key}\"], .settings-head [data-setting=\"${key}\"]`))", arg=sorted(want), timeout=BOUND_MS)
    except Exception:  # noqa: BLE001
        pass
    # The page's header belongs to the section too: Models and providers keeps Add a model there.
    return set(page.evaluate("() => [...document.querySelectorAll('.settings-col [data-setting], .settings-head [data-setting]')].map((el) => el.dataset.setting)"))


def rows(browser, problems: list[str]) -> None:
    page = open_page(browser, 1440, "en")
    for section in INDEX["sections"]:
        sid = section["id"]
        if sid in ELSEWHERE:
            continue
        want = {field["anchor"] for field in INDEX["fields"] if field["section"] == sid} - CONDITIONAL
        found = drawn(page, sid, "en", want)
        missing = sorted(want - found)
        print(f"rows {sid}: {len(want)} listed, {len(want) - len(missing)} drawn")
        for anchor in missing:
            problems.append(f"{sid}: the row {anchor} is not on its section's page")
    page.context.close()


def old_addresses(browser, lang: str, width: int, problems: list[str]) -> None:
    page = open_page(browser, width, lang)
    for old, row, home in OLD:
        where = f"[{lang} {width}] /settings/{old}{'#' + row if row else ''}"
        page.goto(f"{BASE}/settings/{old}{'&' if '?' in old else '?'}token=t&lang={lang}{'#' + row if row else ''}")
        try:
            page.wait_for_url(f"**/settings/{home}**", timeout=BOUND_MS, wait_until="commit")
        except Exception:  # noqa: BLE001
            problems.append(f"{where}: stayed at {page.url}, not /settings/{home}")
            continue
        page.wait_for_selector(".settings-col .card, .settings-col .set-card, .settings-col .settings-row", timeout=BOUND_MS)
        if "tab=" in page.url:
            problems.append(f"{where}: the old tab is still in the address: {page.url}")
        if row:
            try:
                page.wait_for_function("(key) => window.__flashed.has(key)", arg=row, timeout=BOUND_MS)
            except Exception:  # noqa: BLE001
                problems.append(f"{where}: the row did not blink on /settings/{home}")
                continue
            # The row is scrolled to once it has blinked; whether it got there is read when it has, not
            # after a pause that a busy machine may not have been long enough for.
            try:
                page.wait_for_function("""(key) => { const r = document.querySelector(`[data-setting="${key}"]`).getBoundingClientRect();
                    return r.bottom > 0 && r.top < window.innerHeight; }""", arg=row, timeout=BOUND_MS)
            except Exception:  # noqa: BLE001
                problems.append(f"{where}: the row is not in view")
        print(f"{where} → {page.url.split('/app', 1)[1]}")
    page.context.close()


def listing(browser, lang: str, width: int, problems: list[str]) -> None:
    page = open_page(browser, width, lang)
    where = f"[{lang} {width}]"
    phone = width < 1024
    # On a phone the list is the first level; on a desktop it stands beside the open section, here the
    # last one of all, so its entry is far below the first screenful of the list.
    page.goto(f"{BASE}/settings{'' if phone else '/about'}?token=t&lang={lang}")
    page.wait_for_selector(".settings-index .settings-link", timeout=BOUND_MS)
    # Measured once the fonts the entries are set in are in, and on a desktop once the list has brought
    # the open section's entry into view, which it does after the first paint.
    page.evaluate("document.fonts.ready.then(() => true)")
    if not phone:
        page.wait_for_function("""() => { const a = document.querySelector('.settings-link.active'), l = document.querySelector('.settings-index');
            if (!a || !l) return false; const b = l.getBoundingClientRect(), r = a.getBoundingClientRect(); return r.top >= b.top - 0.5 && r.bottom <= b.bottom + 0.5; }""", timeout=BOUND_MS)
    state = page.evaluate("""() => {
        const blocks = [...document.querySelectorAll('.settings-index .settings-group-block')];
        const vw = document.documentElement.clientWidth;
        const wide = [...document.querySelectorAll('.settings-index *')].filter((el) => { const r = el.getBoundingClientRect(); return r.width && (r.left < -0.5 || r.right > vw + 0.5); }).length;
        const list = document.querySelector('.settings-index');
        const active = document.querySelector('.settings-link.active');
        const box = list.getBoundingClientRect(), a = active ? active.getBoundingClientRect() : null;
        return {
            groups: blocks.map((b) => ({ head: b.querySelector('.settings-group').textContent.trim(), sections: [...b.querySelectorAll('.settings-link')].map((l) => l.dataset.section) })),
            outside: wide, scrolls: list.scrollHeight > list.clientHeight + 1, overflow: getComputedStyle(list).overflowY,
            active: active ? active.dataset.section : null, activeIn: a ? a.top >= box.top - 0.5 && a.bottom <= box.bottom + 0.5 : null,
            heights: [...document.querySelectorAll('.settings-index .settings-link')].map((l) => Math.round(l.getBoundingClientRect().height)),
        };
    }""")
    heads = [group["head"] for group in state["groups"]]
    if heads != GROUP_WORDS[lang]:
        problems.append(f"{where}: the group headers read {heads}")
    # A missing or extra group is already said above; the rest are compared as far as both go.
    for group, block in zip(GROUPS, state["groups"], strict=False):
        want = [s["id"] for s in INDEX["sections"] if s["group"] == group and s["id"] not in ELSEWHERE]
        if block["sections"] != want:
            problems.append(f"{where}: {group} lists {block['sections']}, not {want}")
    if state["outside"]:
        problems.append(f"{where}: {state['outside']} elements of the list leave the screen sideways")
    if len(set(state["heights"])) != 1:
        problems.append(f"{where}: the entries are not one height: {sorted(set(state['heights']))}")
    if not phone:
        if not state["scrolls"] or state["overflow"] not in ("auto", "scroll"):
            problems.append(f"{where}: the list does not scroll inside its column: {state['scrolls']} {state['overflow']}")
        if state["active"] != "about" or not state["activeIn"]:
            problems.append(f"{where}: the open section's entry is not in view in the list: {state['active']} {state['activeIn']}")
    print(f"{where} groups {heads}, {sum(len(g['sections']) for g in state['groups'])} sections, entry height {sorted(set(state['heights']))}")
    page.context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        SCENES.append(render_scenes(browser))
        rows(browser, problems)
        for lang in ("en", "ru"):
            for width in (1440, 390):
                listing(browser, lang, width, problems)
                old_addresses(browser, lang, width, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
