"""Two things on a phone that must fit the screen they are on, in both languages.

The Questions sheet: a question whose options are long sentences, and one whose option is a link with
no spaces in it. The options once kept to one line each, so the card grew wider than the phone and
the whole sheet scrolled sideways, a card border crossing the chips and the Send bar shifted with
it. Every option now wraps inside its card, and nothing in the sheet scrolls sideways; the same card
is looked at beside a project's orchestrator on a desktop and in the Main orchestrator's list.

The composer: the mode chip, the one pill for the model and its effort, the microphone and the
circle, at 360, 375, 390 and 430 px, with a short and a long model name, idle and running. The mode
chip once hugged its label and was cut to "Age…" beside a long model name; now the mode keeps its
whole name, each pill has room inside it, the model name is what gives way (never the effort), every
control stays inside the pill and at least 40 px tall.

    APP_URL=http://127.0.0.1:<port>/app python tests/browser/check_phone_fit.py

Exit 0 when every claim holds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_composer as composer  # noqa: E402
from api_stub import DEFAULT_APP, FocusStub, MainStub, expect_app, reveal_composer  # noqa: E402
from check_main import serve as serve_main  # noqa: E402
from check_project_focus import PID, fits, serve  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

MODELS = ("GLM 4", "DeepSeek Flash long-context preview")

OVERFLOW = """(root) => {
  // Every element in the sheet that could scroll sideways, and by how much it would.
  const out = [];
  for (const el of [root, ...root.querySelectorAll('*')]) {
    const style = getComputedStyle(el);
    if (!/(auto|scroll)/.test(style.overflowX) && el !== root) continue;
    // The strip of tabs is a scroller by design (six tabs beside an orchestrator do not fit a
    // phone, and the chosen one is kept in view); what must not scroll is the content under it.
    if (el.tagName === 'TEXTAREA' || el.tagName === 'PRE' || el.getAttribute('role') === 'tablist') continue;
    if (el.scrollWidth > el.clientWidth + 1) out.push(`${el.className || el.tagName} ${el.scrollWidth}>${el.clientWidth}`);
  }
  return out;
}"""


def cards_fit(page: Page, scope: str, where: str) -> list[str]:
    problems: list[str] = []
    card = page.locator(f"{scope} .q-card[data-ask='ql4nch']")
    expect(card).to_be_visible()
    box = card.bounding_box()
    width = page.evaluate("window.innerWidth")
    if not box or box["x"] < 0 or box["x"] + box["width"] > width + 0.5:
        problems.append(f"{where}: the card is {box}, outside a {width}px window")
    chips = card.locator(".q-chip")
    expect(chips).to_have_count(4)
    lines = 0
    for chip in chips.all():
        b = chip.bounding_box()
        if not b or not box or b["x"] + b["width"] > box["x"] + box["width"] + 0.5:
            problems.append(f"{where}: a chip {b} leaves its card {box}")
        clipped = chip.locator(".q-chip-label").evaluate("(e) => e.scrollWidth > e.clientWidth + 1")
        if clipped:
            problems.append(f"{where}: a chip's text is cut: {chip.inner_text()[:40]}")
        if b and b["height"] > 44:
            lines += 1
    if width < 1024 and not lines:
        problems.append(f"{where}: no option wrapped, so the long ones were not really long here")
    scrolls = page.locator(scope).first.evaluate(OVERFLOW)
    if scrolls:
        problems.append(f"{where}: something scrolls sideways: {scrolls}")
    fits(page, where)
    return problems


def project_questions(browser, lang: str, width: int) -> list[str]:  # type: ignore[no-untyped-def]
    phone = width < 1024
    context = browser.new_context(viewport={"width": width, "height": 844 if phone else 900}, is_mobile=phone, has_touch=phone)
    page = context.new_page()
    focus = FocusStub.bakery(lang)
    focus.questions_of_bakery(lang)
    focus.question_of_long_options(lang)
    serve(page, focus)
    page.goto(f"{BASE}/orchestration/project/{PID}?token=t&lang={lang}")
    if phone:
        page.locator(".chat-head .questions-headbtn").tap()
        scope = ".panel-sheet"
    else:
        scope = ".panel"
    expect(page.locator(f"{scope} .panel-tab[data-tab='questions']")).to_have_attribute("aria-selected", "true")
    problems = cards_fit(page, scope, f"{lang} {width} project questions")
    if phone:
        # The Send bar spans the sheet and no more: it was shifted with the rest when the sheet scrolled.
        bar = page.locator(f"{scope} .questions-send").bounding_box()
        if not bar or bar["x"] < -0.5 or bar["x"] + bar["width"] > width + 0.5:
            problems.append(f"{lang} {width}: the send bar is {bar}")
        page.locator(f"{scope} .q-card[data-ask='ql4nch'] .q-chip").nth(3).tap()
        problems += cards_fit(page, scope, f"{lang} {width} project questions, a link chosen")
    context.close()
    return problems


def main_questions(browser, lang: str, width: int) -> list[str]:  # type: ignore[no-untyped-def]
    phone = width < 1024
    context = browser.new_context(viewport={"width": width, "height": 844 if phone else 900}, is_mobile=phone, has_touch=phone)
    page = context.new_page()
    main = MainStub(lang)
    # The Main orchestrator lists every project's questions; the long one is put under Bakery.
    long = FocusStub.bakery(lang).question_of_long_options(lang)
    main.asks.append({**long, "project_id": "p-bakery", "project_name": "Bakery", "asker": "orchestrator", "host": False, "dispatch_id": None})
    serve_main(page, main)
    if phone:
        # On a phone the main chat is the first row of orchestration's list, and its questions a sheet away.
        page.goto(f"{BASE}/orchestration/projects?token=t&lang={lang}")
        page.locator(".ph-orch [data-main-entry] .ph-row").click()
        page.locator(".chat-head .questions-headbtn").tap()
        scope = ".panel-sheet"
    else:
        page.goto(f"{BASE}/orchestration?token=t&lang={lang}")
        scope = ".panel"
    problems = cards_fit(page, scope, f"{lang} {width} main questions")
    context.close()
    return problems


def composer_fits(browser, lang: str, width: int, model: str, running: bool) -> list[str]:  # type: ignore[no-untyped-def]
    where = f"{lang} {width} {'running' if running else 'idle'} '{model}'"
    problems: list[str] = []
    composer.HOST.status = "running" if running else "idle"
    composer.HOST.yagni = width == 360
    original = composer.Host.detail
    composer.HOST.detail = lambda: {**original(composer.HOST), "model": model}  # type: ignore[method-assign]
    context = browser.new_context(viewport={"width": width, "height": 780}, is_mobile=True, has_touch=True, color_scheme="dark")
    context.add_init_script("try { localStorage.setItem('daedalus.session.panel', '0'); } catch (e) {}")
    page = context.new_page()
    page.route("**/api/**", composer.stub)
    page.goto(f"{BASE}/agents/{composer.SESSION}?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".composer .roundbtn.primary", timeout=15000)
    page.wait_for_timeout(300)
    # The phone's composer rests as one row; its toolbar (mode, model, effort) is the open shape's.
    reveal_composer(page)
    try:
        facts = page.evaluate("""() => {
          const box = document.querySelector('.composer-box').getBoundingClientRect();
          const shown = [...document.querySelectorAll('.composer-row > *, .composer-tools > *')]
            .filter((e) => e.getClientRects().length && !e.classList.contains('composer-tools') && e.tagName !== 'INPUT');
          const pill = (sel) => {
            const e = document.querySelector(sel);
            if (!e) return null;
            const s = getComputedStyle(e), r = e.getBoundingClientRect();
            return { w: r.width, h: r.height, pad: parseFloat(s.paddingLeft), fs: parseFloat(s.fontSize) };
          };
          const cut = (sel) => { const e = document.querySelector(sel); return e ? e.scrollWidth > e.clientWidth + 1 : null; };
          return {
            box: { left: box.left, right: box.right },
            outside: shown.filter((e) => { const r = e.getBoundingClientRect(); return r.left < box.left - 0.5 || r.right > box.right + 0.5; }).map((e) => e.className),
            // A 36 px circle or pill reaches 44 px through its ::after; what counts is the target.
            short: shown.filter((e) => {
              const r = e.getBoundingClientRect(), a = getComputedStyle(e, '::after');
              const grow = a.content !== 'none' && a.position === 'absolute' ? -(parseFloat(a.top) || 0) - (parseFloat(a.bottom) || 0) : 0;
              return r.height + grow < 43.5;
            }).map((e) => e.className),
            mode: pill('.composer .composer-mode'), model: pill('.composer .model-select'),
            modeCut: cut('.composer .composer-mode .truncate'), effortCut: cut('.composer .model-select .model-effort'),
            effort: document.querySelector('.composer .model-select .model-effort')?.textContent ?? null,
            doc: document.documentElement.scrollWidth - window.innerWidth,
          };
        }""")
    finally:
        composer.HOST.detail = lambda: original(composer.HOST)  # type: ignore[method-assign]
    if facts["doc"] > 0:
        problems.append(f"{where}: the page scrolls sideways by {facts['doc']}px")
    if facts["outside"]:
        problems.append(f"{where}: {facts['outside']} leave the pill")
    if facts["short"]:
        problems.append(f"{where}: {facts['short']} are under the 44 px target")
    mode, pill = facts["mode"], facts["model"]
    if not mode:
        problems.append(f"{where}: no mode chip")
    else:
        if facts["modeCut"]:
            problems.append(f"{where}: the mode chip's name is cut")
        # The phone's pills are the design's: 14 px type with room inside (15 until the phone's scale
        # came down a step, a size too large beside the chat apps on the same phone).
        if mode["pad"] < 8 or mode["fs"] != 14:
            problems.append(f"{where}: the mode chip has {mode['pad']}px inside and {mode['fs']}px type")
    if running:
        if pill:
            problems.append(f"{where}: the model pill did not give way to steering")
    elif not pill:
        problems.append(f"{where}: no model pill")
    else:
        if pill["pad"] < 8 or pill["fs"] != 14:
            problems.append(f"{where}: the model pill has {pill['pad']}px inside and {pill['fs']}px type")
        if not facts["effort"] or facts["effortCut"]:
            problems.append(f"{where}: the effort is missing or cut ({facts['effort']!r})")
    print(where, "mode", mode, "model", pill, "effort", facts["effort"])
    context.close()
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width in (360, 390, 1440):
                problems += project_questions(browser, lang, width)
                problems += main_questions(browser, lang, width)
            for width in (360, 375, 390, 430):
                for model in MODELS:
                    for running in (False, True):
                        problems += composer_fits(browser, lang, width, model, running)
        browser.close()
    composer.HOST.status, composer.HOST.yagni = "idle", False
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run() or composer.UNHANDLED.report())
