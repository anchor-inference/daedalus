"""The step editor: a finished recording made into a skill the agent replays, in a real browser, at 1440 × 900,
in English and Russian.

- the finished recording's card offers "Edit steps…" beside the procedure draft; it opens the editor in
  the card's place with every recorded step but the tab switch;
- a typed value becomes a parameter with one press, and the parameters the steps name are listed;
- a step can be deleted and restored, and a deleted one is left out of what is saved;
- a sign-in can be bound to the operator's secrets, one per field; a value that is not a placeholder
  holds Save back and says why;
- Save posts the kept steps by number with only what changed, and the card goes once the recording
  is a skill; nothing reaches past the window or the card.

Exit 0 when every claim holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_stub import BrowserStub, open_page, render_scenes, wait_frames  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SKILL_SHOTS", "")
ROOT = ".panel .bp"
WORDS = {
    "en": {"open": "Edit steps…", "params": "Parameters:", "need_secret": "«secret:name»"},
    "ru": {"open": "Править шаги…", "params": "Параметры:", "need_secret": "«secret:name»"},
}


def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def record(page, bs: BrowserStub) -> None:  # type: ignore[no-untyped-def]
    """Take the browser, record five steps (one a tab switch, one a sign-in with two fields), stop."""
    page.locator(f"{ROOT} .bp-control.take").click()
    page.wait_for_selector(f"{ROOT} .bp-rec-btn", timeout=5000)
    page.locator(f"{ROOT} .bp-rec-btn").click()
    page.locator(".bp-rec-start input[type=checkbox]").check()
    page.locator(".bp-rec-start .btn.primary").click()
    page.wait_for_selector(f"{ROOT} .bp-recbar", timeout=5000)
    bs.record_step("g1", action="type", element={"role": "textbox", "name": "Period", "selector": "#period"}, slot="period", value="2026-10", submit=True)
    bs.record_step("g1", action="tab", to="https://shop.example/other", title="Other")
    bs.record_step("g1", action="handoff", reason="login", element={"role": "textbox", "name": "Email"},
                   fields=[{"role": "textbox", "name": "Email", "selector": "#email", "secret": "name"}, {"role": "textbox", "name": "Password", "selector": "#pw", "secret": "password"}])
    bs.record_step("g1", action="scroll", direction="down", count=2)
    bs.record_step("g1", action="click", element={"role": "button", "name": "Export", "selector": "#export", "text": "Export"})
    page.locator(f"{ROOT} .bp-recbar-stop").click()
    page.locator(f"{ROOT} .bp-rec[data-state='stopped']").wait_for(timeout=5000)


def check(browser, scenes, lang: str, width: int, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{lang} {width}] {text}")  # noqa: E731
    words = WORDS[lang]
    bs = BrowserStub(scenes)
    bs.add("g1", scene="shop", owner_id=S1)
    context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme="dark")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}&panel=browser")
    page.wait_for_selector(f"{ROOT} .bv[data-state='live']", timeout=10000)
    wait_frames(page, ROOT, 1)
    record(page, bs)

    card = page.locator(f"{ROOT} .bp-rec[data-state='stopped']")
    opener = card.locator(".bp-rec-skill")
    if opener.inner_text().strip() != words["open"]:
        say(f"the card's button reads {opener.inner_text()!r}")
    opener.click()
    editor = page.locator(f"{ROOT} .bp-rec[data-state='skill'] .bp-skill")
    editor.wait_for(timeout=5000)
    rows = editor.locator(".bp-skill-step")
    actions = [rows.nth(i).get_attribute("data-action") for i in range(rows.count())]
    if actions != ["type", "handoff", "scroll", "click"]:
        say(f"the editor lists {actions}, not the recorded steps without the tab switch")

    # The typed value becomes a parameter, and the parameters are listed.
    typed = editor.locator(".bp-skill-step[data-action='type']")
    if typed.locator(".bp-skill-input").input_value() != "2026-10":
        say(f"the typing starts as {typed.locator('.bp-skill-input').input_value()!r}")
    typed.locator(".bp-skill-param-btn").click()
    if typed.locator(".bp-skill-input").input_value() != "{period}":
        say(f"Make a parameter wrote {typed.locator('.bp-skill-input').input_value()!r}")
    params = editor.locator(".bp-skill-params").inner_text()
    if words["params"] not in params or "{period}" not in params:
        say(f"the parameters line reads {params!r}")

    # Delete the scroll, then restore and delete it again: the deleted one is struck through.
    scroll = editor.locator(".bp-skill-step[data-action='scroll']")
    scroll.locator(".bp-skill-drop").click()
    scroll.locator(".bp-skill-drop").click()
    scroll.locator(".bp-skill-drop").click()
    if "gone" not in (scroll.get_attribute("class") or ""):
        say("a deleted step is not marked as gone")

    editor.locator(".bp-skill-name").fill("monthly-report")
    editor.locator(".bp-skill-desc").fill("Export the monthly report from the shop")

    # A sign-in bound to secrets: a bare value holds Save back.
    signin = editor.locator(".bp-skill-step[data-action='handoff']")
    signin.locator(".bp-skill-mode").select_option("secrets")
    secrets = signin.locator(".bp-skill-secret input")
    if secrets.count() != 2:
        say(f"the sign-in offers {secrets.count()} secret fields, not one per recorded field")
    secrets.nth(0).fill("someone")
    secrets.nth(1).fill("«secret:shop_password»")
    save = editor.locator(".bp-skill-save")
    if not save.is_disabled():
        say("Save is offered with a value in a sign-in field")
    if words["need_secret"] not in editor.locator(".bp-skill-why").inner_text():
        say(f"the reason Save is held back reads {editor.locator('.bp-skill-why').inner_text()!r}")
    secrets.nth(0).fill("«secret:shop_user»")
    if save.is_disabled():
        say(f"Save is held back: {editor.locator('.bp-skill-why').inner_text()!r}")
    page.wait_for_timeout(300)
    shot(page, f"skill-editor-{lang}-{width}")
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    if overflow > 0:
        say(f"the page scrolls sideways by {overflow}px")
    wide = page.evaluate(f"() => {{ const e = document.querySelector(\"{ROOT} .bp-skill\"); return e ? e.scrollWidth - e.clientWidth : 0; }}")
    if wide > 1:
        say(f"the editor is wider than its card by {wide}px")
    save.click()
    page.wait_for_selector(f"{ROOT} .bp-rec", state="detached", timeout=5000)
    posted = bs.posted("/skill")
    want = {"name": "monthly-report", "description": "Export the monthly report from the shop", "steps": [
        {"n": 1, "text": "{period}"}, {"n": 3, "signin": "secrets", "secrets": ["«secret:shop_user»", "«secret:shop_password»"]}, {"n": 5},
    ]}
    if posted[-1:] != [want]:
        say(f"Save posted {posted}")
    context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        for lang in ("en", "ru"):
            check(browser, scenes, lang, 1440, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
