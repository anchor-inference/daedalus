"""The model picker opens next to the control that opened it.

The panel strip's model chip once opened the composer's picker where the composer keeps it, at the
bottom left of the chat column, half a window away from the chip. Here the chip, the strip's own
menu and the composer's pill each open the list, and the list must hang from the one that was
pressed and stay inside the window. A phone opens the same list as a sheet from the strip in its
drawer, a running turn included, when the composer has no pill of its own. The two Anthropic routes
must read as two different names.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_composer as composer  # noqa: E402
from api_stub import expect_app, reveal_composer  # noqa: E402

SHOTS = os.environ.get("SHOTS", "")

NAMES = {
    "en": ("Anthropic · subscription", "Anthropic · API key", "Change the model"),
    "ru": ("Anthropic · подписка", "Anthropic · API-ключ", "Сменить модель"),
}


def box(page: Page, selector: str) -> dict:
    return page.locator(selector).first.evaluate("el => { const r = el.getBoundingClientRect(); return {left: r.left, right: r.right, top: r.top, bottom: r.bottom}; }")


def inside(page: Page, rect: dict, where: str) -> None:
    width, height = page.evaluate("[innerWidth, innerHeight]")
    assert rect["left"] >= 0 and rect["top"] >= 0 and rect["right"] <= width + 0.5 and rect["bottom"] <= height + 0.5, f"{where}: the picker leaves the window: {rect} in {width}x{height}"


def below(page: Page, chip_selector: str, where: str) -> None:
    """The list hangs under the chip, from its left edge or clamped to the window's right edge."""
    chip = box(page, chip_selector)
    menu = box(page, ".model-menu")
    inside(page, menu, where)
    gap = menu["top"] - chip["bottom"]
    assert 0 <= gap <= 12, f"{where}: the picker is not under the chip (chip {chip}, picker {menu})"
    width = page.evaluate("innerWidth")
    assert abs(menu["left"] - chip["left"]) <= 1 or menu["right"] >= width - 9, f"{where}: the picker is not aligned to the chip (chip {chip}, picker {menu})"


def names(page: Page, language: str, where: str) -> None:
    subscription, key, _ = NAMES[language]
    expect(page.locator('.provider-row[data-provider="claude"]'), where).to_contain_text(subscription)
    expect(page.locator('.provider-row[data-provider="anthropic"]'), where).to_contain_text(key)


def desktop(page: Page, language: str, where: str) -> None:
    chip = ".panel [data-panel-strip] .panel-strip-model"
    page.wait_for_selector(chip)
    page.locator(chip).click()
    expect(page.locator(".model-menu"), where).to_be_visible()
    page.wait_for_selector(".model-menu .provider-row")
    below(page, chip, f"{where} chip")
    names(page, language, where)
    # The composer's pill is not the one that is open.
    assert page.locator(".composer .model-select").get_attribute("aria-expanded") == "false", f"{where}: the composer's pill claims the list"
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/picker-strip-{where.replace(' ', '-')}.png")
    page.keyboard.press("Escape")
    expect(page.locator(".model-menu"), where).to_have_count(0)
    expect(page.locator(chip), f"{where}: Escape gives the keyboard back to the chip").to_be_focused()
    # A second press on the chip closes what the first opened.
    page.locator(chip).click()
    expect(page.locator(".model-menu"), where).to_be_visible()
    page.locator(chip).click()
    expect(page.locator(".model-menu"), f"{where}: the chip toggles").to_have_count(0)
    # "Change the model" in the strip's own menu hangs the list from the strip's chip too.
    page.locator(".panel [data-panel-strip] button[aria-haspopup='menu']").last.click()
    page.get_by_role("menuitem", name=NAMES[language][2]).click()
    expect(page.locator(".model-menu"), where).to_be_visible()
    below(page, chip, f"{where} strip menu")
    page.keyboard.press("Escape")
    expect(page.locator(".model-menu"), where).to_have_count(0)
    # The composer's pill keeps its list where it always was: above the pill, its right edges together.
    pill = ".composer .model-select"
    page.locator(pill).click()
    expect(page.locator(".model-menu"), where).to_be_visible()
    trigger = box(page, pill)
    menu = box(page, ".model-menu")
    inside(page, menu, f"{where} composer")
    assert menu["bottom"] <= trigger["top"] and trigger["top"] - menu["bottom"] <= 12, f"{where}: the composer's picker left its pill (pill {trigger}, picker {menu})"
    assert abs(menu["right"] - trigger["right"]) <= 1 or menu["left"] <= 8.5, f"{where}: the composer's picker is not aligned to its pill (pill {trigger}, picker {menu})"
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/picker-composer-{where.replace(' ', '-')}.png")
    page.keyboard.press("Escape")
    expect(page.locator(pill), f"{where}: Escape gives the keyboard back to the pill").to_be_focused()


def phone(page: Page, language: str, where: str) -> None:
    chip = ".panel-sheet [data-panel-strip] .panel-strip-model"
    page.wait_for_selector(chip)
    page.locator(chip).click()
    sheet = page.locator(".model-sheet")
    expect(sheet, where).to_be_visible()
    page.wait_for_selector(".model-sheet .provider-row")
    # The sheet slides up into place; measured mid-slide its foot is still below the window.
    page.wait_for_timeout(600)
    inside(page, box(page, ".model-sheet"), where)
    names(page, language, where)
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/picker-{where.replace(' ', '-')}.png")
    page.keyboard.press("Escape")
    expect(sheet, where).to_have_count(0)


def main() -> int:
    expect_app(composer.BASE)
    composer.PRESETS["anthropic.sonnet"] = {**composer.PRESETS["opus"], "provider": "anthropic", "label": "Claude Sonnet (key)", "model": "claude-sonnet"}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=composer.CHROMIUM)
        for language in ("en", "ru"):
            for width, height, mobile, status in ((1440, 900, False, "idle"), (1128, 800, False, "idle"), (390, 844, True, "idle"), (390, 844, True, "running")):
                for theme in ("dark", "light"):
                    where = f"{language} {width} {theme} {status}"
                    composer.HOST = composer.Host()
                    composer.HOST.status = status
                    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=theme, is_mobile=mobile, has_touch=mobile)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.route("**/api/**", composer.stub)
                    page.goto(f"{composer.BASE}/agents/{composer.SESSION}?panel=details&token=t&scheme={theme}&lang={language}")
                    page.wait_for_selector(".composer .composer-field")
                    page.wait_for_timeout(300)
                    if mobile:
                        phone(page, language, where)
                    else:
                        reveal_composer(page)
                        desktop(page, language, where)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{where}: the page scrolls sideways"
                    context.close()
                    print(f"{where}: ok")
        browser.close()
    return composer.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
