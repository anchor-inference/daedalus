"""Settings → Limits & budget, the context compaction's summary model, driven in a real browser.

At 1440 × 900 and on a 390 px phone, in English and Russian:

- the picker (a compact dropdown in the row's control lane) offers the session's own model first, then
  every preset under the model picker's label;
- a choice is saved through the settings save with its revision, and after a reload the picker shows
  the value the host holds;
- a preset deleted after it was chosen is shown as missing, not as whichever option comes first, and
  saving another field of the section carries it along unchanged;
- the summariser's call timeout sits beside it and is saved on blur;
- nothing on the page scrolls sideways on a phone, and the picker's list stays on its screen.

Exit 0 when every step holds.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import SETTINGS, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LANG = os.environ.get("LANG_UI", "")
OWN = {"en": "The session's own model", "ru": "Модель самой сессии"}
MISSING = {"en": "missing", "ru": "нет такой"}


class Host:
    """The settings the page reads and writes; an unknown summary preset is refused as the host does."""

    def __init__(self, preset: str = "") -> None:
        self.settings = copy.deepcopy(SETTINGS)
        self.settings["revision"] = "s1"
        self.settings["compaction"]["preset"] = preset
        self.puts: list[dict] = []
        self.spend_failed = False
        self.spend = {"since": "", "total": {"spent_usd": 3.42, "unmetered": 2, "cap_usd": 10, "reserved_usd": 1.12, "uncertain_usd": 0.35, "reserved_count": 3, "uncertain_count": 1},
                      "per_provider": {"deepseek": {"spent_usd": 3.42, "unmetered": 2, "cap_usd": 5, "reserved_usd": 1.12, "uncertain_usd": 0.35, "reserved_count": 3, "uncertain_count": 1}}}

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]

        def answer(data: object, status: int = 200) -> None:
            route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

        if rel == "/api/settings" and request.method == "GET":
            return answer(self.settings)
        if rel == "/api/limits/spend" and request.method == "GET":
            return answer({"detail": "balance unavailable"}, 503) if self.spend_failed else answer(self.spend)
        if rel == "/api/limits/reset-total" and request.method == "POST":
            self.spend["total"]["spent_usd"] = 0
            self.spend["per_provider"]["deepseek"]["spent_usd"] = 0
            return answer({"since": "2026-10-03T00:00:00Z"})
        if rel == "/api/settings/validate":
            body = json.loads(request.post_data or "{}")
            return answer({"valid": True, "stale": body.get("base_revision") != self.settings["revision"], "problems": []})
        if rel == "/api/settings" and request.method == "PUT":
            body = json.loads(request.post_data or "{}")
            self.puts.append(body)
            if body.get("base_revision") != self.settings["revision"]:
                return answer({"detail": "settings changed in another window"}, 409)
            section = {**self.settings["compaction"], **body.get("compaction", {})}
            if section["preset"] not in ("", self.settings["compaction"]["preset"], *self.settings["presets"]):
                return answer({"detail": f"no such model preset {section['preset']!r} for the summary model"}, 422)
            self.settings["compaction"] = section
            self.settings["revision"] = f"s{len(self.puts) + 1}"
            return answer(self.settings)
        return stub(route)


def say_into(problems: list[str], lang: str):  # type: ignore[no-untyped-def]
    return lambda text: problems.append(f"[{lang}] {text}")


def opened(browser, host: Host, lang: str, viewport: dict, **context):  # type: ignore[no-untyped-def]
    page = browser.new_context(viewport=viewport, color_scheme="dark", **context).new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/settings/limits?token=t&lang={lang}")
    page.wait_for_selector("#compaction-preset", timeout=15000)
    return page


def desktop(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = say_into(problems, lang)
    host = Host()
    page = opened(browser, host, lang, {"width": 1440, "height": 900})
    picker = page.locator("#compaction-preset")
    picker.click()
    labels = page.locator(".dropdown-list [role=option] .dropdown-item-text > span:first-child").all_inner_texts()
    print(f"[{lang}] options: {labels}")
    if labels[:1] != [OWN[lang]] or labels[1:] != [p["label"] for p in SETTINGS["presets"].values()]:
        say(f"the options read {labels}")
    if picker.get_attribute("data-value") != "":
        say(f"with nothing chosen the picker holds {picker.get_attribute('data-value')!r}")
    # The hint is the row's description, beside the picker rather than under a full-width select.
    hint = page.locator(".settings-row", has=picker).locator(".settings-row-desc").inner_text()
    if "flash" not in hint.lower():
        say(f"the hint reads {hint!r}")

    page.locator(".dropdown-list [role=option][data-value='deepseek-flash']").click()
    page.wait_for_timeout(800)
    last = host.puts[-1] if host.puts else {}
    print(f"[{lang}] saved: {last.get('compaction')} at {last.get('base_revision')}")
    if last.get("compaction", {}).get("preset") != "deepseek-flash" or last.get("base_revision") != "s1":
        say(f"the choice was saved as {last}")

    page.reload()
    page.wait_for_selector("#compaction-preset", timeout=15000)
    if page.locator("#compaction-preset").get_attribute("data-value") != "deepseek-flash":
        say(f"after a reload the picker holds {page.locator('#compaction-preset').get_attribute('data-value')!r}")

    timeout = page.locator("#compaction-timeout")
    timeout.fill("120")
    timeout.blur()
    page.wait_for_timeout(800)
    if host.settings["compaction"]["call_timeout_seconds"] != 120 or host.settings["compaction"]["preset"] != "deepseek-flash":
        say(f"the timeout was saved as {host.settings['compaction']}")
    page.context.close()

    # A preset deleted since it was chosen: shown as missing, carried along by another save.
    host = Host("retired-model")
    page = opened(browser, host, lang, {"width": 1440, "height": 900})
    picker = page.locator("#compaction-preset")
    chosen = picker.inner_text()
    print(f"[{lang}] missing: {chosen!r}, invalid {picker.get_attribute('aria-invalid')}")
    if picker.get_attribute("data-value") != "retired-model" or MISSING[lang] not in chosen or picker.get_attribute("aria-invalid") != "true":
        say(f"a deleted preset reads {chosen!r} ({picker.get_attribute('data-value')!r})")
    words = page.locator("#compaction-words")
    words.fill("1000")
    words.blur()
    page.wait_for_timeout(800)
    if host.settings["compaction"]["max_words"] != 1000 or host.settings["compaction"]["preset"] != "retired-model":
        say(f"another save around a missing preset left {host.settings['compaction']}")
    page.context.close()


def phone(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = say_into(problems, lang)
    page = opened(browser, Host("retired-model"), lang, {"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    width = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth, document.querySelector('#compaction-preset').getBoundingClientRect().right]")
    print(f"[{lang}] phone widths: {width}")
    if width[0] > width[1] or width[2] > width[1] + 0.5:
        say(f"Settings → Limits scrolls sideways on a phone: {width}")
    # The picker's list floats over the page; on a phone it must stay inside the screen.
    page.locator("#compaction-preset").scroll_into_view_if_needed()
    page.locator("#compaction-preset").tap()
    box = page.locator(".dropdown-list").bounding_box() or {"x": -1, "width": 0}
    print(f"[{lang}] phone list: {box}")
    if box["x"] < 0 or box["x"] + box["width"] > 390.5:
        say(f"the picker's list leaves the phone's screen: {box}")
    page.context.close()


def spending(browser, lang: str) -> None:  # type: ignore[no-untyped-def]
    host = Host()
    host.settings["limits"]["usd_total"] = 4
    host.settings["limits"]["usd_total_per_provider"] = {"deepseek": 5}
    host.settings["providers"] = {"deepseek": {"kind": "deepseek", "pricing": {}}}
    page = opened(browser, host, lang, {"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    meter = page.locator(".spend-meter")
    expect(meter).to_contain_text("$3.42")
    expect(meter).to_contain_text("$1.12")
    expect(meter).to_have_attribute("data-level", "bad")
    page.get_by_text("How this cap is counted" if lang == "en" else "Как считается лимит").click()
    expect(page.locator("details.sheet-section", has_text="$0.35")).to_contain_text("already included" if lang == "en" else "уже учтена")
    expect(page.locator("details.sheet-section", has_text="$1.12")).to_contain_text("not the provider's final bill" if lang == "en" else "не окончательный счёт провайдера")
    page.get_by_role("button", name="Reset counters" if lang == "en" else "Обнулить счётчики").click()
    expect(meter).to_contain_text("$0.00")
    expect(meter).to_contain_text("$1.12")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "budget details overflow the phone"
    host.spend_failed = True
    page.reload()
    expect(page.get_by_text("Charges and holds are unknown" if lang == "en" else "Списания и удержания неизвестны").first).to_be_visible()
    expect(page.locator(".spend-meter")).to_have_count(0)
    host.spend_failed = False
    page.locator(".result-warning").get_by_role("button", name="Try again" if lang == "en" else "Ещё раз").click()
    expect(page.locator(".spend-meter")).to_contain_text("$1.12")
    page.context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ([LANG] if LANG else ["en", "ru"]):
            desktop(browser, lang, problems)
            phone(browser, lang, problems)
            spending(browser, lang)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
