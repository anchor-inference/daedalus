"""The model picker's tabs, its search across providers and its keys, through the real composer commands.

The picker is one box of one size: Favourites first, a tab per provider, the free models last; a search
spans every provider; the side arrows walk the tabs from an empty search, up and down walk the rows,
Enter picks and Escape gives the keyboard back to the chip. A phone has the same box as a sheet.
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_composer as composer  # noqa: E402
from api_stub import expect_app, reveal_composer  # noqa: E402


def box(page):  # type: ignore[no-untyped-def]
    return page.locator(".menu.model-picker").evaluate("el => { const r = el.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; }")


def main() -> int:
    expect_app(composer.BASE)
    composer.PRESETS["sonnet"] = {**composer.PRESETS["opus"], "label": "Claude Sonnet", "model": "claude-sonnet", "favourite": True}
    composer.PRESETS["free"] = {**composer.PRESETS["opus"], "label": "Free test model", "model": "free-test", "free_only": True}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=composer.CHROMIUM)
        for width, theme in ((1440, "dark"), (1128, "light"), (390, "dark")):
            composer.HOST = composer.Host()
            context = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
            page = composer.open_page(context)
            trigger = page.locator(".composer .model-select")
            reveal_composer(page)
            trigger.click()
            phone = width < 1024
            tabs = page.locator(".model-picker [role=tab]")
            page.wait_for_selector(".model-picker [role=option]")
            # Favourites, then each provider with its mark, then the free models.
            expect(tabs).to_have_count(5)
            assert tabs.first.get_attribute("data-tab") == "favourites"
            assert tabs.last.get_attribute("data-tab") == "free"
            expect(page.locator(".model-picker [role=tab][data-provider] .provider-mark")).to_have_count(3)
            expect(tabs.first).to_have_attribute("aria-selected", "true")
            rows = page.locator(".model-picker [role=option]")
            expect(rows).to_have_count(1)
            expect(rows.first).to_contain_text("Claude Sonnet")
            size = box(page) if not phone else None
            before = len(composer.posts("/model"))
            page.locator(".model-picker [role=tab][data-provider='claude']").click()
            expect(page.locator(".model-picker [role=option][aria-selected='true']")).to_contain_text("Claude Opus 5")
            expect(page.locator(".model-picker [role=option]", has_text="Claude Sonnet")).to_have_count(1)
            expect(page.locator(".model-picker [role=option]", has_text="DeepSeek Flash")).to_have_count(0)
            if size:
                assert box(page) == size, f"{width}: a tab changed the box from {size} to {box(page)}"
            assert len(composer.posts("/model")) == before, "Opening a provider must not change the selected model"
            page.locator(".model-picker [role=tab][data-tab='free']").click()
            expect(page.locator(".model-picker [role=option]", has_text="Free test model")).to_have_count(1)
            search = page.locator(".model-picker .model-search")
            search.fill("zzz")
            expect(page.locator(".model-picker-empty, .model-picker-title")).to_contain_text(["Nothing matches."])
            if size:
                assert box(page) == size, f"{width}: an empty search changed the box from {size} to {box(page)}"
            search.fill("Flash")
            expect(rows).to_have_count(1)
            expect(page.locator(".model-picker [role=tab][aria-selected='true']")).to_have_count(0)
            if size:
                assert box(page) == size, f"{width}: a search changed the box from {size} to {box(page)}"
            rows.first.click()
            expect(page.locator(".model-picker")).to_have_count(0)
            problems: list[str] = []
            assert composer.reached(page, "/model", before, "search selection", problems)
            assert composer.posts("/model")[-1][2] == {"preset": "flash"}
            if not phone:
                # The keyboard alone: the arrows walk the tabs and the rows, Enter picks, Escape leaves.
                trigger.click()
                page.wait_for_selector(".model-picker [role=option]")
                expect(search).to_be_focused()
                page.keyboard.press("ArrowRight")
                expect(page.locator(".model-picker [role=tab][data-provider='local']")).to_have_attribute("aria-selected", "true")
                page.keyboard.press("ArrowRight")
                expect(page.locator(".model-picker [role=tab][data-provider='claude']")).to_have_attribute("aria-selected", "true")
                page.keyboard.press("ArrowDown")
                active = page.locator(".model-picker [role=option].active")
                expect(active).to_have_count(1)
                assert search.get_attribute("aria-activedescendant") == active.get_attribute("id")
                chosen = active.get_attribute("data-preset")
                before = len(composer.posts("/model"))
                page.keyboard.press("Enter")
                expect(page.locator(".model-picker")).to_have_count(0)
                assert composer.reached(page, "/model", before, "keyboard selection", problems)
                assert composer.posts("/model")[-1][2] == {"preset": chosen}, composer.posts("/model")[-1]
                trigger.click()
                page.wait_for_selector(".model-picker [role=option]")
                # Tab goes round the box (the reasoning, the settings) and back, never out of it.
                page.keyboard.press("Tab")
                expect(page.locator(".model-picker .model-effort-seg [aria-checked='true']")).to_be_focused()
                page.keyboard.press("Tab")
                expect(page.locator(".model-picker .model-picker-foot")).to_be_focused()
                page.keyboard.press("Tab")
                expect(search).to_be_focused()
                page.keyboard.press("Escape")
                expect(page.locator(".model-picker")).to_have_count(0)
                expect(trigger).to_be_focused()
            assert not problems, problems
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            context.close()
            print(f"provider picker {width} {theme}: ok")
        browser.close()
    return composer.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
