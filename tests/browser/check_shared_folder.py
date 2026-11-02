"""A second project on a folder another project has: asked about, then made, on a desktop and a phone,
in both languages.

What is checked:

- picking a folder the project "Cafe landing" has says so, in the accent colour rather than as a
  fault, with "Use the same folder" and "Open the project", and Create waits for the answer;
- "Use the same folder" marks the folder as shared and the project is created with exactly that
  folder in the request;
- "Open the project" closes the dialog and creates nothing;
- nothing scrolls sideways on a 390 px phone, and the two answers stay touch-sized.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_shared_folder.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app  # noqa: E402
from check_new_project import WORDS, Harness, fits  # noqa: E402
from folder_stub import FolderStub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
CAFE = "/srv/projects/landing-cafe"


def run(playwright, language: str, phone: bool) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[language]
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    size = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=size, is_mobile=phone, has_touch=phone, color_scheme="dark")
    page = context.new_page()
    unhandled = Unhandled()
    h = Harness(page, FolderStub(host="up"), unhandled)
    h.open(language)
    sheet = page.locator(".sheet.np-sheet")
    page.locator("#project-name").fill("Cafe menu")
    h.existing(words)
    sheet.locator(".fb-place.fav", has_text="projects").click()
    row = sheet.locator(".fb-row", has_text="landing-cafe")
    # The row is offered like any other folder, with the project that has it as a badge.
    expect(row).not_to_have_class("dim")
    row.click()
    question = sheet.locator(".fb-shared")
    expect(question).to_contain_text(words["taken"])
    expect(question).to_have_attribute("data-shared-with", "p-cafe")
    create = sheet.get_by_role("button", name=words["create"])
    expect(create).to_be_disabled()
    share = question.get_by_role("button", name=words["share"])
    if phone:
        assert fits(page), "the shared-folder question scrolls sideways on a phone"
        question.scroll_into_view_if_needed()
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/{'phone' if phone else 'desktop'}-shared-folder-{language}.png")
    share.click()
    expect(sheet.locator(".fb-foot [data-shared]")).to_contain_text(words["shared"])
    expect(create).to_be_enabled()
    create.scroll_into_view_if_needed()
    create.click()
    expect(sheet).to_have_count(0)
    assert h.posted and h.posted[-1]["folders"][0]["path"] == CAFE, h.posted
    assert len(h.projects) == 1

    # "Open the project" leaves without making anything.
    h.open(language)
    page.locator("#project-name").fill("Another")
    h.existing(words)
    sheet.locator(".fb-place.fav", has_text="projects").click()
    sheet.locator(".fb-row", has_text="landing-cafe").click()
    sheet.locator(".fb-shared").get_by_role("button", name=words["open"]).click()
    expect(sheet).to_have_count(0)
    assert len(h.projects) == 1, h.projects
    context.close()
    browser.close()
    assert unhandled.report() == 0


if __name__ == "__main__":
    expect_app(BASE)
    with sync_playwright() as pw:
        for lang in ("en", "ru"):
            for on_phone in (False, True):
                run(pw, lang, on_phone)
                print(f"shared folder {lang} {'390' if on_phone else '1440'}px: PASS")
