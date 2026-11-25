"""The composer's toolbar holds every control it carries, side by side, on a phone and in a narrow pane.

With the agent's language translated, a long model name, an effort and the dictation configured, the
toolbar carries "+", the access chip, the model chip with its effort, the context, the microphone and
the circle. When a language chip of its own joined the row it stopped fitting a phone: the controls slid
over one another, the effort lay on the microphone and the model was cut to "clau…".

The language is a row of the access chip's menu at every size; on a phone the chip is its shield alone and
the effort sits under the model's name inside its pill. This walks the open toolbar at 360, 390 and 430 px
in English and in Russian and refuses:

- two controls whose boxes overlap, or a control whose contents spill out of it;
- a control outside the composer's pill;
- a circle that is not whole, not the phone's size, or not at the row's end;
- a model name cut below a recognisable prefix, or an effort that is missing or cut;
- the language's pair on the phone's chip, or an access menu without the language row that opens the choice.

A wide desktop window, a desktop composer narrowed by the side panel and the two panes of the dual view
are walked with the same claims, plus the desktop's own: the access chip says the pair ("EN ⇄ RU") where
the composer is wide, and gives up its words for its shield in a narrow pane.

    APP_URL=http://127.0.0.1:<port>/app python tests/browser/check_composer_fit.py

Each state is photographed into ``SHOTS`` when it is set. Exit 0 when every claim holds.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_composer as composer  # noqa: E402
from api_stub import DEFAULT_APP, GATES, expect_app, reveal_composer, session_translation  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = Path(os.environ["SHOTS"]) if os.environ.get("SHOTS") else None

# A name the pill must shorten: the operator's own reads "claude-…" from a provider's catalogue.
LONG_MODEL = "claude-sonnet-4-5-20250929-extended"
# The fewest characters of the name that still tell one model from another ("claude-son").
RECOGNISABLE = 10
LANGUAGE_ROW = {"en": "Agent language", "ru": "Язык агента"}

FACTS = """(composer) => {
  const box = composer.querySelector('.composer-box');
  const row = composer.querySelector('.composer-row');
  const controls = [...row.querySelectorAll(':scope > *, .composer-tools > *')]
    .filter((e) => e.tagName !== 'INPUT' && !e.classList.contains('composer-tools') && e.getClientRects().length && getComputedStyle(e).display !== 'contents');
  const rect = (e) => { const r = e.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height }; };
  const name = (e) => (e.className && typeof e.className === 'string' ? e.className.trim().split(/\\s+/).join('.') : e.tagName);
  // A part drawn past its control's edge: the effort once lay on the microphone while its pill's
  // own box stayed in its lane, so the boxes alone did not show it.
  const spill = [];
  for (const c of controls) {
    const r = c.getBoundingClientRect();
    for (const part of c.querySelectorAll('*')) {
      if (!part.getClientRects().length) continue;
      const p = part.getBoundingClientRect();
      if (p.left < r.left - 0.5 || p.right > r.right + 0.5) spill.push(`${name(part)} in ${name(c)}`);
    }
  }
  const label = composer.querySelector('.model-select .model-label');
  const effort = composer.querySelector('.model-select .model-effort');
  const chip = composer.querySelector('.composer-access .access-lang');
  const send = composer.querySelector('.roundbtn.primary');
  // How much of the name is in view: the share of its width the pill shows, in characters.
  const shown = label ? Math.floor(label.textContent.replace('…', '').length * Math.min(1, label.clientWidth / Math.max(1, label.scrollWidth))) : 0;
  return {
    box: rect(box),
    controls: controls.map((e) => ({ name: name(e), ...rect(e) })),
    spill,
    label: label ? label.textContent : null, shown,
    effort: effort && effort.getClientRects().length ? effort.textContent : null,
    effortCut: effort ? effort.scrollWidth > effort.clientWidth + 1 : null,
    chip: chip && chip.getClientRects().length ? chip.innerText.trim() : null,
    send: send ? rect(send) : null,
    last: controls.length ? name(controls[controls.length - 1]) : null,
    round: parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ctl-round')) || 0,
    viewport: { width: window.innerWidth, height: window.innerHeight },
    doc: document.documentElement.scrollWidth - window.innerWidth,
  };
}"""


def stub(route) -> None:  # type: ignore[no-untyped-def]
    """The composer's invented host, with the session translating between English and Russian."""
    path = route.request.url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    if rel == f"/api/sessions/{composer.SESSION}" and route.request.method == "GET":
        detail = {**composer.HOST.detail(), "model": LONG_MODEL, "translation": session_translation("en", explicit=True)}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))
    return composer.stub(route)


def overlaps(controls: list[dict]) -> list[str]:
    found = []
    for i, a in enumerate(controls):
        for b in controls[i + 1:]:
            across = min(a["right"], b["right"]) - max(a["left"], b["left"])
            down = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
            if across > 0.5 and down > 0.5:
                found.append(f"{a['name']} and {b['name']} overlap by {across:.1f}px")
    return found


def judge(facts: dict, where: str, phone: bool, narrow: bool) -> list[str]:
    problems: list[str] = []
    box, controls, send = facts["box"], facts["controls"], facts["send"]
    names = [c["name"].split(".") for c in controls]
    for wanted in ("plus", "composer-access", "model-select", "ctx-ring", "mic", "primary"):
        if not any(wanted in n for n in names):
            problems.append(f"{where}: no {wanted} in the row ({[c['name'] for c in controls]})")
    problems += [f"{where}: {line}" for line in overlaps(controls)]
    problems += [f"{where}: {part} is drawn outside it" for part in facts["spill"]]
    for c in controls:
        if c["left"] < box["left"] - 0.5 or c["right"] > box["right"] + 0.5 or c["top"] < box["top"] - 0.5 or c["bottom"] > box["bottom"] + 0.5:
            problems.append(f"{where}: {c['name']} at {c['left']:.0f}–{c['right']:.0f} leaves the pill at {box['left']:.0f}–{box['right']:.0f}")
    if facts["doc"] > 0:
        problems.append(f"{where}: the page scrolls sideways by {facts['doc']}px")
    if not send:
        problems.append(f"{where}: no circle")
    else:
        view = facts["viewport"]
        if send["left"] < 0 or send["right"] > view["width"] or send["top"] < 0 or send["bottom"] > view["height"]:
            problems.append(f"{where}: the circle is not whole in the window: {send}")
        if phone and (abs(send["width"] - facts["round"]) > 0.5 or abs(send["height"] - facts["round"]) > 0.5):
            problems.append(f"{where}: the circle is {send['width']}×{send['height']}, not the phone's {facts['round']}px")
        if "primary" not in (facts["last"] or "").split("."):
            problems.append(f"{where}: the row ends with {facts['last']}, not the circle")
    if facts["label"] is None or facts["shown"] < RECOGNISABLE:
        problems.append(f"{where}: the model reads {facts['shown']} characters of {facts['label']!r}")
    if not facts["effort"] or facts["effortCut"]:
        problems.append(f"{where}: the effort is missing or cut ({facts['effort']!r})")
    if phone and facts["chip"] is not None:
        problems.append(f"{where}: the language's pair is on the phone's chip ({facts['chip']!r})")
    # A narrow pane keeps the access chip's shield alone; its title and menu still name the pair.
    if not phone and facts["chip"] != (None if narrow else "EN ⇄ RU"):
        problems.append(f"{where}: the desktop's access chip reads {facts['chip']!r} for the language")
    return problems


def language_row(page: Page, lang: str, where: str) -> list[str]:
    """The phone's language: a row of the access chip's sheet naming the pair, which opens the languages."""
    page.locator(".composer .composer-access").tap()
    row = page.locator(".sheet .access-menu [data-action='language']")
    expect(row).to_be_visible()
    problems: list[str] = []
    if LANGUAGE_ROW[lang] not in row.inner_text() or "EN ⇄ RU" not in row.inner_text():
        problems.append(f"{where}: the access sheet's language row reads {row.inner_text()!r}")
    if SHOTS is not None:
        page.wait_for_timeout(300)  # the sheet slides up
        page.screenshot(path=str(SHOTS / f"access-sheet-{lang}.png"))
    row.tap()
    sheet = page.locator(".sheet .agentlang-menu")
    expect(sheet.locator(".agentlang-row[data-lang='en']")).to_have_attribute("aria-checked", "true")
    if SHOTS is not None:
        page.wait_for_timeout(300)
        page.screenshot(path=str(SHOTS / f"language-sheet-{lang}.png"))
    return problems


def walk(browser, lang: str, width: int, phone: bool, panel: bool = False, dual: bool = False) -> list[str]:  # type: ignore[no-untyped-def]
    where = f"{lang} {width}{' panel' if panel else ''}{' dual' if dual else ''}"
    context = browser.new_context(viewport={"width": width, "height": 844 if phone else 900}, is_mobile=phone, has_touch=phone, color_scheme="dark")
    context.add_init_script(f"try {{ localStorage.setItem('daedalus.session.panel', '{1 if panel else 0}'); }} catch (e) {{}}")
    page = context.new_page()
    page.route("**/api/**", stub)
    # The dual view puts the same session in both panes: what is measured is the narrow pane's row.
    page.goto(f"{BASE}/agents/{composer.SESSION}?token=t&scheme=dark&lang={lang}{f'&with={composer.SESSION}' if dual else ''}")
    # A desktop's empty field shows the mic alone in the circle's place; Send is there, but hidden.
    page.wait_for_selector(".composer .roundbtn.primary", state="attached", timeout=15000)
    page.wait_for_selector(".composer .model-select", state="attached", timeout=15000)
    page.wait_for_timeout(300)
    reveal_composer(page)
    problems: list[str] = []
    composers = page.locator(".composer:has(.composer-row)")
    for n in range(composers.count()):
        # A word in the field: the phone keeps its toolbar open, and Send joins the mic on both.
        composers.nth(n).locator(".composer-field").first.click()
        page.keyboard.type("Check the log")
        page.wait_for_timeout(250)
        facts = composers.nth(n).evaluate(FACTS)
        here = f"{where}{f' pane {n + 1}' if dual else ''}"
        print(here, json.dumps({k: facts[k] for k in ("label", "shown", "effort", "chip")}, ensure_ascii=False))
        print("   ", " ".join(f"{c['name'].split('.')[-1]}[{c['left']:.0f}–{c['right']:.0f}]" for c in facts["controls"]))
        problems += judge(facts, here, phone, narrow=dual)
        if SHOTS is not None:
            SHOTS.mkdir(parents=True, exist_ok=True)
            composers.nth(n).screenshot(path=str(SHOTS / f"composer-{width}{'-panel' if panel else ''}{f'-dual{n + 1}' if dual else ''}-{lang}.png"))
    if dual and composers.count() != 2:
        problems.append(f"{where}: {composers.count()} composers, not one per pane")
    if phone and width == 390:
        try:
            problems += language_row(page, lang, where)
        except AssertionError as missing:
            problems.append(f"{where}: the language is not reachable from the access sheet: {str(missing).splitlines()[0]}")
    context.close()
    return problems


def run() -> int:
    problems: list[str] = []
    original_asr = GATES["/api/asr"]
    GATES["/api/asr"] = {**original_asr, "configured": True}
    composer.HOST.status, composer.HOST.effort, composer.HOST.thinking = "idle", "medium", True
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM)
            for lang in ("en", "ru"):
                for width in (360, 390, 430):
                    problems += walk(browser, lang, width, phone=True)
                problems += walk(browser, lang, 1440, phone=False)
                problems += walk(browser, lang, 1024, phone=False, panel=True)
                problems += walk(browser, lang, 1280, phone=False, dual=True)
            browser.close()
    finally:
        GATES["/api/asr"] = original_asr
        composer.HOST.effort = "high"
    print("problems:", "\n  ".join(problems) if problems else "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run() or composer.UNHANDLED.report())
