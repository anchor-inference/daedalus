"""Provider navigation, scoped/global search and model selection use the real composer commands."""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_composer as composer  # noqa: E402
from api_stub import expect_app  # noqa: E402


def main() -> int:
    expect_app(composer.BASE)
    composer.PRESETS["sonnet"] = {**composer.PRESETS["opus"], "label": "Claude Sonnet", "model": "claude-sonnet"}
    composer.PRESETS["free"] = {**composer.PRESETS["opus"], "label": "Free test model", "model": "free-test", "free_only": True}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=composer.CHROMIUM)
        for width, theme in ((1440, "dark"), (1128, "light"), (390, "dark")):
            composer.HOST = composer.Host()
            context = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
            page = composer.open_page(context)
            trigger = page.locator(".composer .model-select")
            trigger.click()
            providers = page.locator(".provider-row")
            expect(providers).to_have_count(4)
            expect(page.locator(".provider-mark svg")).to_have_count(3)
            expect(page.locator('.provider-row[data-provider="claude"] .provider-count')).to_have_text("2")
            page.locator(".provider-row", has_text="Free").click()
            expect(providers).to_have_count(1)
            page.locator('.provider-row[data-provider="claude"]').click()
            expect(page.locator(".model-row", has_text="Free test model")).to_have_count(1)
            page.locator(".provider-back").click()
            page.locator(".provider-back").click()
            expect(providers).to_have_count(4)
            expect(page.locator(".model-list .model-row.on")).to_have_count(0)
            before = len(composer.posts("/model"))
            page.locator('.provider-row[data-provider="claude"]').click()
            expect(page.locator(".model-list .model-row.on")).to_contain_text("Claude Opus 5")
            expect(page.locator(".model-row", has_text="Claude Sonnet")).to_have_count(1)
            expect(page.locator(".model-row", has_text="DeepSeek Flash")).to_have_count(0)
            assert len(composer.posts("/model")) == before, "Opening a provider must not change the selected model"
            search = page.locator(".model-search")
            search.fill("Flash")
            expect(page.locator(".model-list [role=status]")).to_be_visible()
            expect(page.locator(".model-row", has_text="DeepSeek Flash")).to_have_count(0)
            page.locator(".provider-back").click()
            expect(providers).to_have_count(4)
            search.fill("Flash")
            expect(providers).to_have_count(0)
            expect(page.locator(".model-row", has_text="DeepSeek Flash")).to_have_count(1)
            page.locator(".model-row", has_text="DeepSeek Flash").click()
            expect(page.locator(".model-list")).to_have_count(0)
            problems: list[str] = []
            assert composer.reached(page, "/model", before, "search selection", problems)
            assert composer.posts("/model")[-1][2] == {"preset": "flash"}
            trigger.click()
            expect(providers).to_have_count(4)
            page.locator('.provider-row[data-provider="local"]').click()
            page.keyboard.press("ArrowLeft")
            expect(providers).to_have_count(4)
            expect(page.locator('.provider-row[data-provider="local"]')).to_be_focused()
            page.keyboard.press("Escape")
            expect(page.locator(".model-list")).to_have_count(0)
            if width >= 1024:
                expect(trigger).to_be_focused()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            context.close()
            print(f"provider picker {width} {theme}: ok")
        browser.close()
    return composer.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
