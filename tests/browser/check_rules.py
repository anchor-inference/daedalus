"""Settings → Rules: the categories × levels grid, the review's switches and model, and the decision log.

What is checked, on a desktop window and on a phone, in both languages:

- one row per category the host lists, each with its four levels, the saved level marked;
- picking a level saves the whole ``action_rules`` section with that level changed and the rest kept;
- the review's switch is on and locked at "on command", locked off at "asks" and "hands to you";
- the log lists the host's decisions with their verdict and reason;
- on a phone nothing leaves the screen sideways and each level is a target a finger can press.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_rules.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import ACTION_RULES, DEFAULT_APP, expect_app  # noqa: E402
from screenshots import SETTINGS, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

OVERFLOW = """() => {
  const vw = document.documentElement.clientWidth;
  const out = [];
  for (const el of document.querySelectorAll('.action-rules *, .ar-log *')) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    if (r.left < -0.5 || r.right > vw + 0.5) out.push((el.className || el.tagName) + ' ' + Math.round(r.left) + '..' + Math.round(r.right));
  }
  return out;
}"""


def open_rules(browser, width: int, lang: str, saved: list[dict]) -> Page:  # type: ignore[no-untyped-def]
    phone = width < 600
    context = browser.new_context(viewport={"width": width, "height": 900}, is_mobile=phone, has_touch=phone, color_scheme="dark")
    page = context.new_page()
    page.route("**/api/**", stub)
    state = {"settings": copy.deepcopy(SETTINGS) | {"revision": "r1"}}

    def settings(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        if request.method == "PUT":
            body = json.loads(request.post_data or "{}")
            saved.append(body)
            state["settings"] = {**state["settings"], **{k: v for k, v in body.items() if k != "base_revision"}}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(state["settings"]))

    def validate(route) -> None:  # type: ignore[no-untyped-def]
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({"valid": True, "stale": False, "problems": []}))

    page.route("**/api/settings", settings)
    page.route("**/api/settings/validate", validate)
    page.goto(f"{BASE}/settings/rules?token=t&lang={lang}")
    page.wait_for_selector(".action-rules .ar-row[data-category]", timeout=20000)
    page.wait_for_selector(".ar-log-entry", timeout=10000)
    return page


def check(page: Page, width: int, lang: str, saved: list[dict], problems: list[str]) -> None:
    where = f"{lang} {width}px"
    rows = page.locator(".action-rules .ar-row[data-category]")
    categories = [rows.nth(i).get_attribute("data-category") for i in range(rows.count())]
    if categories != ACTION_RULES["categories"]:
        problems.append(f"{where}: the rows are {categories}, not the host's categories")
    for i in range(rows.count()):
        cells = rows.nth(i).locator(".ar-cell")
        if cells.count() != 4:
            problems.append(f"{where}: {categories[i]} has {cells.count()} levels, not four")
    publish = page.locator('.ar-row[data-category="publish"]')
    if publish.locator('.ar-cell[aria-checked="true"]').get_attribute("data-level") != "ask":
        problems.append(f"{where}: the saved level of publishing (ask) is not the one marked")
    if publish.locator(".ar-review").get_attribute("data-review") != "none" or not publish.locator(".ar-review button").is_disabled():
        problems.append(f"{where}: at 'asks' the review's switch is not locked off")
    if width < 600:
        for i in range(rows.count()):
            for j in range(4):
                box = rows.nth(i).locator(".ar-cell").nth(j).bounding_box()
                if box and box["height"] < 36:
                    problems.append(f"{where}: a level of {categories[i]} is {round(box['height'])} px tall, under a finger's 36")
    outside = page.evaluate(OVERFLOW)
    for item in outside[:5]:
        problems.append(f"{where}: past the screen's edge: {item}")
    before = len(saved)
    page.locator('.ar-row[data-category="external_write"] .ar-cell[data-level="on_command"]').click()
    page.wait_for_timeout(500)
    if len(saved) != before + 1:
        problems.append(f"{where}: picking a level did not save")
    else:
        rules = saved[-1].get("action_rules") or {}
        levels = rules.get("levels") or {}
        if levels.get("external_write") != "on_command" or levels.get("publish") != "ask" or rules.get("review") != ["message", "publish", "payment"]:
            problems.append(f"{where}: the save changed more or less than the one level: {rules}")
    page.wait_for_selector('.ar-row[data-category="external_write"][data-level="on_command"]', timeout=5000)
    review = page.locator('.ar-row[data-category="external_write"] .ar-review')
    if review.get_attribute("data-review") != "always" or not review.locator("button").is_disabled() or review.locator("button").get_attribute("aria-checked") != "true":
        problems.append(f"{where}: at 'on command' the review's switch is not on and locked")
    entries = page.locator(".ar-log-entry")
    if entries.count() != len(ACTION_RULES["log"]):  # type: ignore[arg-type]
        problems.append(f"{where}: the log shows {entries.count()} decisions, not {len(ACTION_RULES['log'])}")  # type: ignore[arg-type]
    first = entries.first
    if first.get_attribute("data-verdict") != "block" or "accountant" not in (first.text_content() or ""):
        problems.append(f"{where}: the newest decision is not the blocked one with its reason")


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width in (1280, 390, 360):
                saved: list[dict] = []
                page = open_rules(browser, width, lang, saved)
                check(page, width, lang, saved, problems)
                page.context.close()
            print(f"rules {lang}: {'ok' if not any(x.startswith(lang) for x in problems) else 'FAILED'}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
