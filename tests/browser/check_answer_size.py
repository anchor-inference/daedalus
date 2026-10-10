"""Answer text takes any whole size from 12 to 22 px, on a phone and on a desktop.

Settings, Appearance, Size used to offer Usual, 17 and 19, so answer text could not be made smaller
than usual. This sets the extremes the stepper allows, both by driving the stepper itself and by
storing the choice, and reads the size the answer is actually drawn at. It also proves the old
stored choices ("17", "19", "auto") are read as sizes, and that the interface step does not move a
pinned size.

    APP_URL=http://127.0.0.1:<port>/app python3 tests/browser/check_answer_size.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
KEY = "daedalus.appearance"
ANSWER = "() => parseFloat(getComputedStyle(document.querySelector('.answer')).fontSize)"


def answer_size(page, stored: dict) -> float:  # type: ignore[no-untyped-def]
    page.evaluate("([k, v]) => localStorage.setItem(k, v)", [KEY, json.dumps(stored)])
    page.goto(f"{BASE}/agents/{S1}?token=t&lang=en")
    page.wait_for_selector(".answer")
    page.wait_for_timeout(200)
    return page.evaluate(ANSWER)


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        for name, width, usual in (("phone", 390, 16), ("desktop", 1440, 15)):
            context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme="dark", is_mobile=width < 500, has_touch=width < 500)
            page = context.new_page()
            page.route("**/api/**", stub)
            page.goto(f"{BASE}/agents/{S1}?token=t&lang=en")
            page.wait_for_selector(".answer")
            if (got := page.evaluate(ANSWER)) != usual:
                problems.append(f"{name}: usual is {got}px, not {usual}")
            for size in (12, 22):
                got = answer_size(page, {"prose": size})
                if got != size:
                    problems.append(f"{name}: stored {size} draws {got}px")
                got = answer_size(page, {"prose": size, "scale": "xl"})
                if got != size:
                    problems.append(f"{name}: {size} moved to {got}px by the interface step")
            for old, want in (("17", 17), ("19", 19), ("auto", usual)):
                got = answer_size(page, {"prose": old})
                if got != want:
                    problems.append(f"{name}: old choice {old!r} draws {got}px, not {want}")
            # The stepper: down to the floor, up to the ceiling, then back to usual.
            page.evaluate("([k]) => localStorage.removeItem(k)", [KEY])
            page.goto(f"{BASE}/settings/appearance?token=t&lang=en")
            stepper = page.locator(".prose-stepper")
            stepper.wait_for()
            smaller = stepper.get_by_label("Smaller answer text")
            larger = stepper.get_by_label("Larger answer text")
            for _ in range(12):
                if smaller.is_enabled():
                    smaller.click()
            if "12 px" not in stepper.locator("output").inner_text() or not smaller.is_disabled():
                problems.append(f"{name}: stepper floor reads {stepper.locator('output').inner_text()!r}")
            if page.evaluate("() => parseFloat(getComputedStyle(document.querySelector('.prose-stepper-preview')).fontSize)") != 12:
                problems.append(f"{name}: preview is not drawn at 12px")
            for _ in range(12):
                if larger.is_enabled():
                    larger.click()
            if "22 px" not in stepper.locator("output").inner_text() or not larger.is_disabled():
                problems.append(f"{name}: stepper ceiling reads {stepper.locator('output').inner_text()!r}")
            if page.evaluate("() => parseFloat(document.documentElement.style.getPropertyValue('--fs-prose'))") != 22:
                problems.append(f"{name}: 22 was not applied to the page")
            page.get_by_role("button", name="Reset to usual").click()
            if page.evaluate("() => document.documentElement.style.getPropertyValue('--fs-prose')") != "":
                problems.append(f"{name}: reset left a pinned size")
            if name == "phone":
                page.get_by_role("button", name="Smaller answer text").click()
                page.screenshot(path=os.environ.get("SHOT", "/tmp/answer-size-phone.png"))
            context.close()
        browser.close()
    for line in problems:
        print("FAIL", line)
    print("answer size:", "ok" if not problems else f"{len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
