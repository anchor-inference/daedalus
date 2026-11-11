"""A workspace's disk use in the session's Details tab and in a project's settings, its limit and the clean-up."""
from __future__ import annotations

import json
import os
import sys

from api_stub import DEFAULT_APP, expect_app, fulfil_shared, reset_disk_limit
from playwright.sync_api import Page, expect, sync_playwright
from screenshots import S1, SETTINGS, UNHANDLED, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {
        "settings": "Settings for Bakery site", "cleanup": "Clean up…", "total": "23 GB", "close": "Close", "freed": "Freed 18 GB",
        "tracked": "Include files tracked by git", "change": "Change limit", "over": "Over the size limit.", "over2": "More than twice the size limit.",
        "of": "of {n} GB", "none": "no limit", "choices": ["+10 GB", "×2", "50 GB", "100 GB", "No limit", "Use default (20 GB)"],
        "plus": "+10 GB", "double": "×2", "no_limit": "No limit", "default": "Use default (20 GB)", "unit": "GB", "save": "Save",
    },
    "ru": {
        "settings": "Настройки: Bakery site", "cleanup": "Очистить…", "total": "23 ГБ", "close": "Закрыть", "freed": "Освобождено 18 ГБ",
        "tracked": "Включая файлы, отслеживаемые git", "change": "Изменить лимит", "over": "Больше лимита размера.", "over2": "Больше лимита размера более чем вдвое.",
        "of": "из {n} ГБ", "none": "без лимита", "choices": ["+10 ГБ", "×2", "50 ГБ", "100 ГБ", "Без лимита", "По умолчанию (20 ГБ)"],
        "plus": "+10 ГБ", "double": "×2", "no_limit": "Без лимита", "default": "По умолчанию (20 ГБ)", "unit": "ГБ", "save": "Сохранить",
    },
}


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def limits(page: Page, panel, words: dict, requests: list[str]) -> None:  # type: ignore[no-untyped-def]
    """Every way of changing the limit redraws the bar, the size against it and the over line at once."""
    shown = panel.locator("[data-disk-limit]")
    over = panel.locator("[data-disk-over]")
    edit = panel.locator("[data-disk-limit-edit]")
    choices = edit.locator("[data-disk-limit-choice]")
    expect(shown).to_have_text(words["of"].format(n=20))
    expect(over).to_have_text(words["over"])
    expect(panel.locator(".bar.attn")).to_have_count(1)
    panel.get_by_role("button", name=words["change"]).click()
    expect(edit).to_be_visible()
    assert [t.strip() for t in choices.all_inner_texts()] == words["choices"], choices.all_inner_texts()
    for box in (edit, choices.first, edit.locator("[data-disk-limit-input]"), edit.locator("[data-disk-limit-set]")):
        inside = box.bounding_box()
        assert inside and inside["x"] >= 0 and inside["x"] + inside["width"] <= page.viewport_size["width"], f"{inside} leaves the screen"
    fits(page, "the limit editor")
    # Ten more: 30 GB holds the 23 GB, so the warning and the tint go.
    choices.get_by_text(words["plus"], exact=True).click()
    expect(shown).to_have_text(words["of"].format(n=30))
    expect(over).to_have_count(0)
    expect(panel.locator(".bar.attn")).to_have_count(0)
    expect(edit).to_have_count(0)
    # Twice that, then the number typed by hand, which is under the size again.
    panel.get_by_role("button", name=words["change"]).click()
    edit.get_by_text(words["double"], exact=True).click()
    expect(shown).to_have_text(words["of"].format(n=60))
    panel.get_by_role("button", name=words["change"]).click()
    field = edit.locator("[data-disk-limit-input]")
    expect(field).to_have_value("60")
    field.fill("0")
    expect(edit.locator("[data-disk-limit-set]")).to_be_disabled()
    field.fill("11")
    edit.locator("[data-disk-limit-set]").click()
    expect(shown).to_have_text(words["of"].format(n=11))
    expect(over).to_have_text(words["over2"])
    expect(panel.locator(".bar.bad")).to_have_count(1)
    fits(page, "a limit under half the size")
    # No limit: no bar, nothing to warn about, and the editor says it is the current choice.
    panel.get_by_role("button", name=words["change"]).click()
    edit.get_by_text(words["no_limit"], exact=True).click()
    expect(shown).to_have_text(words["none"])
    expect(panel.locator(".bar")).to_have_count(0)
    expect(over).to_have_count(0)
    panel.get_by_role("button", name=words["change"]).click()
    expect(edit.locator("[data-disk-limit-choice='none']")).to_have_attribute("aria-pressed", "true")
    fits(page, "no limit")
    # And back to following the default.
    edit.locator("[data-disk-limit-choice='default']").click()
    expect(shown).to_have_text(words["of"].format(n=20))
    expect(over).to_have_text(words["over"])
    expect(panel.locator(".bar.attn")).to_have_count(1)
    puts = [r for r in requests if r.startswith("PUT /api/disk/limit/ws-bakery")]
    assert len(puts) == 5, requests


def usage(page: Page, scope: str, words: dict[str, str], requests: list[str]) -> None:
    """The size against the limit, the way to change it, the largest folders, then a clean-up of the throwaway ones."""
    panel = page.locator(scope)
    expect(panel.locator("[data-disk-workspace]")).to_be_visible()
    expect(panel.locator("[data-disk-total]")).to_have_text(words["total"])
    expect(panel.locator(".bar.attn")).to_have_count(1)
    reset_disk_limit()
    limits(page, panel, words, requests)
    scratch = panel.locator("[data-disk-entry='_scratch']")
    expect(scratch).to_be_visible()
    expect(panel.locator("[data-disk-entry='src']")).to_be_visible()
    scratch.locator(".disk-name").click()
    expect(panel.locator("[data-disk-child]")).to_have_count(2)
    fits(page, "usage")
    panel.get_by_role("button", name=words["cleanup"]).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.locator("[data-disk-pick='_scratch'] input")).to_be_checked()
    expect(dialog.locator("[data-disk-pick='.uv-cache'] input")).to_be_checked()
    expect(dialog.locator("[data-disk-pick='src'] input")).not_to_be_checked()
    expect(dialog.locator("[data-disk-pick='src'] input")).to_be_disabled()
    expect(dialog.locator("[data-disk-pick='inbox'] input")).to_be_disabled()
    dialog.get_by_label(words["tracked"]).check()
    expect(dialog.locator("[data-disk-pick='src'] input")).to_be_enabled()
    dialog.get_by_label(words["tracked"]).uncheck()
    dialog.locator("[data-disk-confirm]").click()
    expect(dialog.locator("[data-disk-freed]")).to_have_text(words["freed"])
    dialog.get_by_role("button", name=words["close"], exact=True).last.click()
    assert any(r.startswith("POST /api/disk/cleanup") for r in requests), requests


def default_limit(page: Page, language: str, width: int) -> None:
    """Settings → Limits carries the default every workspace without a limit of its own follows."""
    saved: list[dict] = []

    def route(r) -> None:  # type: ignore[no-untyped-def]
        url, method = r.request.url.split("?", 1)[0], r.request.method
        if url.endswith("/api/settings") and method == "GET":
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({**SETTINGS, "revision": "r1"}))
        if url.endswith("/api/settings/validate"):
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"valid": True, "stale": False, "problems": []}))
        if url.endswith("/api/settings") and method == "PUT":
            sent = json.loads(r.request.post_data or "{}")
            saved.append(sent)
            disk = {**SETTINGS["disk"], **sent["disk"]}
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({**SETTINGS, "disk": disk, "revision": "r2"}))
        stub(r)

    page.route("**/api/**", route)
    page.goto(f"{BASE}/settings/limits?token=t&lang={language}")
    row = page.locator("[data-setting='disk.workspace_soft_limit_gb']")
    expect(row).to_be_visible()
    field = row.locator("input")
    expect(field).to_have_value("20")
    expect(row).to_contain_text(WORDS[language]["unit"])
    fits(page, "the default limit")
    field.fill("50")
    with page.expect_response(lambda reply: reply.url.split("?", 1)[0].endswith("/api/settings") and reply.request.method == "PUT"):
        field.press("Enter")
    expect(row.locator("input")).to_have_value("50")
    assert saved and saved[-1]["disk"] == {"workspace_soft_limit_gb": 50.0}, saved
    row.scroll_into_view_if_needed()
    print(f"{language} {width}px: Settings → Limits changed the default workspace limit")


def serve(route) -> None:  # type: ignore[no-untyped-def]
    """The disk routes first: the pictures' stub answers every write with a bare ok, which would hide the clean-up's result."""
    if "/api/disk" in route.request.url and fulfil_shared(route):
        return
    stub(route)


def recorder(requests: list[str]):  # type: ignore[no-untyped-def]
    """A route that notes every call, so the check can tell what the page asked and when."""
    def route(r) -> None:  # type: ignore[no-untyped-def]
        requests.append(f"{r.request.method} {r.request.url.split('?', 1)[0][r.request.url.index('/api/'):]}")
        serve(r)
    return route


def run() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            words = WORDS[language]
            for width, height, mobile in ((1440, 900, False), (390, 844, True)):
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                page.set_default_timeout(8000)
                requests: list[str] = []

                page.route("**/api/**", recorder(requests))
                # The Details tab asks nothing about the disk until its section is opened.
                page.goto(f"{BASE}/agents/{S1}?token=t&lang={language}&panel=details")
                section = page.locator("[id$='-info-disk']")
                expect(section).to_be_visible()
                assert not any("/api/disk" in r for r in requests), requests
                # The inspector's line opens the section's page in its place, under the same id.
                section.click()
                usage(page, "[id$='-info-disk']", words, requests)
                context.close()
                print(f"{language} {width}px: session details showed the size, the folders and the clean-up")
            for width, height, mobile in ((1440, 900, False), (390, 844, True)):
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                page.set_default_timeout(8000)
                default_limit(page, language, width)
                context.close()
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.set_default_timeout(8000)
            requests = []

            page.route("**/api/**", recorder(requests))
            page.goto(f"{BASE}/agents?token=t&lang={language}")
            page.locator(".project-chip").click()
            page.locator(".projects-row", has_text="Bakery site").get_by_role("button", name=words["settings"]).click()
            fold = page.locator("details.project-disk")
            assert not any("/api/disk/project" in r for r in requests), requests
            fold.locator("summary").click()
            usage(page, "details.project-disk", words, requests)
            context.close()
            print(f"{language} project settings showed the size, the folders and the clean-up")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(run())
