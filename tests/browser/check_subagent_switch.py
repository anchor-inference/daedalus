"""An open subagent has the list of its leader's subagents down its left side, to switch between them in place.

With the room (a panel dragged wide) the list stands beside the conversation, the current helper marked; a click
shows another helper's conversation without going back to the cards. Without it (a narrow panel, a
phone) the list folds into a button in the head and opens over the conversation, closing again once a
helper is picked. Checked in both languages.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_subagent_switch.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_subagents_tab  # noqa: E402
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, S12, S13, S14, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS")
NAMES = {S12: "link-check", S13: "photo-alt", S14: "price-audit"}
SHOW = {"en": "Show the list of subagents", "ru": "Показать список субагентов"}


def shown(scope, id_: str) -> None:  # type: ignore[no-untyped-def]
    expect(scope.locator(".subagent-head-copy b")).to_have_text(NAMES[id_])
    expect(scope.locator(f'.subagent-chat[data-subagent="{id_}"]')).to_be_visible(timeout=10000)


def wide(page: Page, lang: str, problems: list[str]) -> None:
    # The list docks only in a panel 560 px wide or more, and the panel's own width stops at 520: the
    # room is a panel the operator dragged wider, which the stored width stands for.
    page.add_init_script("try { localStorage.setItem('daedalus.width.panel', '760'); } catch (e) {}")
    page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}&panel=subagents&sub={S12}", wait_until="networkidle")
    scope = page.locator(".panel")
    rail = scope.locator(".subagent-rail.docked")
    expect(rail).to_be_visible(timeout=20000)
    expect(rail.locator(".subagent-rail-row")).to_have_count(6)
    expect(rail.locator('.subagent-rail-row[aria-current="true"]')).to_have_attribute("data-subagent", S12)
    shown(scope, S12)
    beside = page.evaluate("() => { const r = document.querySelector('.subagent-rail').getBoundingClientRect(), m = document.querySelector('.subagent-main').getBoundingClientRect(); return r.right <= m.left + 1 && m.width >= 300; }")
    if not beside:
        problems.append(f"{lang} wide: the list does not stand beside a conversation with room to read")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/subagent-rail-wide-{lang}.png")
    rail.locator(f'.subagent-rail-row[data-subagent="{S13}"]').click()
    shown(scope, S13)
    expect(rail.locator('.subagent-rail-row[aria-current="true"]')).to_have_attribute("data-subagent", S13)
    if f"sub={S13}" not in page.url:
        problems.append(f"{lang} wide: the address does not name the helper shown ({page.url})")
    expect(rail).to_be_visible()
    # The head's button folds it away, and brings it back.
    scope.locator(".subagent-rail-toggle").click()
    expect(scope.locator(".subagent-rail")).to_have_count(0)
    expect(scope.locator(".subagent-rail-toggle")).to_have_attribute("aria-label", SHOW[lang])
    scope.locator(".subagent-rail-toggle").click()
    expect(rail).to_be_visible()


def narrow(page: Page, lang: str, phone: bool, problems: list[str]) -> None:
    tag = f"{lang} {'phone' if phone else 'narrow'}"
    page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}&panel=subagents&sub={S12}", wait_until="networkidle")
    scope = page.locator(".panel-sheet" if phone else ".panel")
    toggle = scope.locator(".subagent-rail-toggle")
    expect(toggle).to_be_visible(timeout=20000)
    shown(scope, S12)
    if scope.locator(".subagent-rail").count():
        problems.append(f"{tag}: the list is open over the conversation before it was asked for")
    expect(toggle).to_have_attribute("aria-label", SHOW[lang])
    toggle.click()
    rail = scope.locator(".subagent-rail.over")
    expect(rail).to_be_visible()
    expect(rail.locator('.subagent-rail-row[aria-current="true"]')).to_have_attribute("data-subagent", S12)
    fits = page.evaluate("() => { const r = document.querySelector('.subagent-rail').getBoundingClientRect(), o = document.querySelector('.subagent-open').getBoundingClientRect(); return r.left >= o.left - 0.5 && r.right <= o.right + 0.5; }")
    if not fits:
        problems.append(f"{tag}: the list runs out of its panel")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/subagent-rail-{'phone' if phone else 'narrow'}-{lang}.png")
    rail.locator(f'.subagent-rail-row[data-subagent="{S14}"]').click()
    shown(scope, S14)
    expect(scope.locator(".subagent-rail")).to_have_count(0)
    if phone:
        sideways = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        if sideways > 0:
            problems.append(f"{tag}: the page scrolls sideways by {sideways}px")
    # The shade over the conversation closes the list without picking anyone.
    toggle.click()
    expect(rail).to_be_visible()
    scrim = scope.locator(".subagent-rail-scrim")
    box = scrim.bounding_box() or {"width": 300, "height": 400}
    scrim.click(position={"x": box["width"] - 12, "y": box["height"] / 2})  # right of the list, over the conversation
    expect(scope.locator(".subagent-rail")).to_have_count(0)
    shown(scope, S14)


def run() -> int:
    check_subagents_tab.scene()
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        scenes = [
            ("wide", {"width": 1920, "height": 1000}, False),
            ("narrow", {"width": 1280, "height": 800}, False),
            ("phone", {"width": 390, "height": 844}, True),
        ]
        for lang in ("en", "ru"):
            for name, viewport, phone in scenes:
                page = browser.new_page(viewport=viewport, is_mobile=phone, has_touch=phone)
                errors: list[str] = []
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                page.route("**/api/**", stub)
                try:
                    wide(page, lang, problems) if name == "wide" else narrow(page, lang, phone, problems)
                except Exception as exc:  # noqa: BLE001 — every scene is looked at; the failures are listed together
                    problems.append(f"{lang} {name}: {' '.join(str(exc).split())[:400]}")
                problems.extend(f"{lang} {name}: page error: {e}" for e in errors)
                page.close()
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("the list of subagents switches the open one in place, docked when wide, over it when narrow and on a phone: passed")
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
