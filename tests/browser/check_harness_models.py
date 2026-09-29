"""Which models a CLI offers: chosen on the Harnesses screen, listed first on the hiring form.

On a desktop at 1440 px: Claude Code's row unfolds to "Models to offer", a tick per model it listed and
none ticked while every model is offered; two are ticked and saved, the host is sent exactly those two,
and the row says "2 of 11". The hiring form then lists the two under "Offered" and the other nine
under "Other models", so every model is still there to pick. "Offer all" clears the choice and the
form lists the eleven again with no groups.

On a touch tablet and on a phone each tick is a thumb's row (44 px), the choice saves, and nothing
scrolls sideways. The Russian page is checked on the same points with its own words.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_harness_models.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import CLAUDE_ALIASES, CLAUDE_VERSIONS, DEFAULT_APP, HarnessesStub, TeamStub, expect_app  # noqa: E402
from screenshots import P1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
DESK = {"width": 1440, "height": 900}
TABLET = {"width": 1180, "height": 820}
PHONE = {"width": 390, "height": 844}
ALL = [*CLAUDE_ALIASES, *CLAUDE_VERSIONS]
PICKED = ["claude-opus-5-5", "claude-sonnet-5-5"]

WORDS = {
    "en": {"of": "2 of 11", "offered": "Offered", "others": "Other models"},
    "ru": {"of": "2 из 11", "offered": "Предлагаемые", "others": "Другие модели"},
}


class Check:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def that(self, ok: bool, problem: str) -> None:
        if not ok:
            self.problems.append(problem)


def sideways(page: Page) -> int:
    return int(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"))


def route(page: Page, harnesses: HarnessesStub, team: TeamStub) -> None:
    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path[parts.path.index("/api/"):]
        body = request.post_data_json if request.method in ("POST", "PUT") and request.post_data else None
        # The hiring form reads the same rows the screen changes, as the host's catalog does.
        if path == "/api/harnesses/catalog":
            env = dict(p.split("=", 1) for p in parts.query.split("&") if "=" in p).get("env", "container")
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(harnesses.catalog_view(env)))
        answered = harnesses.answer(request.method, path, parts.query, body) or team.answer(request.method, path, parts.query, body)
        if answered is not None:
            return route.fulfill(status=answered[0], content_type="application/json", body=json.dumps(answered[1]))
        return stub(route)

    page.route("**/api/**", handle)


def ticked(page: Page) -> list[str]:
    return page.eval_on_selector_all(".harness-offer-model", "els => els.filter(e => e.querySelector('input').checked).map(e => e.dataset.model)")


def model_select(page: Page) -> dict[str, list[str]]:
    """The hiring form's model options by group; ungrouped options under the empty name."""
    return page.eval_on_selector("#staff-model", """select => {
        const out = {};
        for (const option of select.options) {
            if (!option.value) continue;
            const group = option.parentElement.tagName === 'OPTGROUP' ? option.parentElement.label : '';
            (out[group] = out[group] || []).push(option.value);
        }
        return out;
    }""")


def hiring_models(page: Page, lang: str) -> dict[str, list[str]]:
    page.goto(f"{BASE}/project/{P1}/team?token=t&lang={lang}")
    page.wait_for_selector(".pagehead-actions .iconbtn.primary", timeout=15000)
    page.locator(".pagehead-actions .iconbtn.primary").click()
    page.wait_for_selector(".staff-sheet .executor", timeout=5000)
    page.locator(".staff-sheet .executor", has_text="Claude Code").click()
    page.wait_for_selector("select#staff-model", timeout=5000)
    return model_select(page)


def desktop(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    harnesses = HarnessesStub()
    team = TeamStub({"id": P1, "name": "Bakery", "folders": []})
    context = browser.new_context(viewport=DESK, color_scheme="dark")
    page = context.new_page()
    route(page, harnesses, team)
    page.goto(f"{BASE}/harnesses?token=t&lang={lang}")
    page.wait_for_selector(".harness-table .harness-row", timeout=15000)
    page.locator(".harness-row[data-harness='claude'] .harness-name").click()
    offer = page.locator(".harness-row-details .harness-offer")
    expect(offer.locator(".harness-offer-model")).to_have_count(len(ALL))
    got = page.eval_on_selector_all(".harness-row-details .harness-offer-model", "els => els.map(e => e.dataset.model)")
    check.that(got == ALL, f"{lang}: the checklist lists {got}")
    check.that(ticked(page) == [], f"{lang}: with no choice the checklist starts ticked at {ticked(page)}")
    expect(offer.locator("[data-offer='save']")).to_be_disabled()
    expect(offer.locator("[data-offer='all']")).to_be_disabled()

    for model in PICKED:
        offer.locator(f".harness-offer-model[data-model='{model}']").click()
    offer.locator("[data-offer='save']").click()
    expect(page.locator(".harness-row[data-harness='claude'] .harness-models")).to_have_attribute("data-chosen", "true", timeout=5000)
    expect(page.locator(".harness-row[data-harness='claude'] .harness-models-of")).to_contain_text(words["of"])
    check.that(harnesses.put == [("/api/harnesses/claude/models", {"env": "container", "models": PICKED})], f"{lang}: the screen sent {harnesses.put}")
    check.that(ticked(page) == PICKED, f"{lang}: after saving the ticks are {ticked(page)}")

    groups = hiring_models(page, lang)
    check.that(list(groups) == [words["offered"], words["others"]], f"{lang}: the hiring form's model groups are {list(groups)}")
    check.that(groups.get(words["offered"]) == PICKED, f"{lang}: the hiring form offers {groups.get(words['offered'])}")
    others = groups.get(words["others"]) or []
    check.that(sorted([*PICKED, *others]) == sorted(ALL) and len(others) == len(ALL) - 2, f"{lang}: the other models are {others}")

    # Offer all: the choice goes, and the form lists every model again, ungrouped.
    page.goto(f"{BASE}/harnesses?token=t&lang={lang}")
    page.wait_for_selector(".harness-table .harness-row", timeout=15000)
    page.locator(".harness-row[data-harness='claude'] .harness-name").click()
    page.locator(".harness-row-details [data-offer='all']").click()
    expect(page.locator(".harness-row[data-harness='claude'] .harness-models")).to_have_attribute("data-chosen", "false", timeout=5000)
    check.that(harnesses.put[-1] == ("/api/harnesses/claude/models", {"env": "container", "models": None}), f"{lang}: Offer all sent {harnesses.put[-1]}")
    check.that(ticked(page) == [], f"{lang}: after Offer all the ticks are {ticked(page)}")
    groups = hiring_models(page, lang)
    check.that(groups == {"": ALL}, f"{lang}: with no choice the hiring form lists {groups}")
    context.close()


def touch(browser, lang: str, check: Check, viewport: dict[str, int], name: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    harnesses = HarnessesStub()
    context = browser.new_context(viewport=viewport, color_scheme="dark", is_mobile=True, has_touch=True)
    page = context.new_page()
    route(page, harnesses, TeamStub({"id": P1, "name": "Bakery", "folders": []}))
    page.goto(f"{BASE}/harnesses?token=t&lang={lang}")
    page.wait_for_selector(".harness-row, .harness-card", timeout=15000)
    check.that(bool(page.evaluate("matchMedia('(pointer: coarse)').matches")), f"{lang} {name}: the page is not given a coarse pointer")
    wide = page.locator(".harness-table").count() > 0
    page.locator(".harness-row[data-harness='claude'] .harness-name" if wide else ".harness-card[data-harness='claude'] .harness-card-more").tap()
    rows = page.locator(".harness-offer-model")
    expect(rows).to_have_count(len(ALL), timeout=5000)
    heights = page.eval_on_selector_all(".harness-offer-model", "els => els.map(e => e.getBoundingClientRect().height)")
    check.that(min(heights) >= 44, f"{lang} {name}: the ticks are {min(heights)} px high")
    for model in PICKED:
        page.locator(f".harness-offer-model[data-model='{model}']").tap()
    save = page.locator("[data-offer='save']")
    box = save.bounding_box()
    check.that(bool(box and box["height"] >= 32), f"{lang} {name}: Save is {box} high")
    save.tap()
    models = page.locator(f"{'.harness-row' if wide else '.harness-card'}[data-harness='claude'] .harness-models")
    expect(models).to_have_attribute("data-chosen", "true", timeout=5000)
    expect(models).to_contain_text(words["of"])
    check.that([b.get("models") for _, b in harnesses.put] == [PICKED], f"{lang} {name}: the screen sent {harnesses.put}")
    check.that(sideways(page) <= 0, f"{lang} {name}: the screen scrolls sideways by {sideways(page)} px")
    context.close()


def run() -> int:
    expect_app(BASE)
    check = Check()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            desktop(browser, lang, check)
            touch(browser, lang, check, TABLET, "tablet")
            touch(browser, lang, check, PHONE, "phone")
        browser.close()
    unhandled = UNHANDLED.report()
    for problem in check.problems:
        print("FAIL", problem)
    if not check.problems and not unhandled:
        print("the models a CLI offers hold")
    return 1 if check.problems or unhandled else 0


if __name__ == "__main__":
    sys.exit(run())
