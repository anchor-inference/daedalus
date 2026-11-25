"""Does one click on a model in a list choose it, and does the quick agent list stay inside its row?

Every model list opens with its search field taking the keyboard. The first click on a model then used
to be lost: pressing the row moved the focus into the list, the list answered its focus by marking the
current model and scrolling to it, the row slid away under the pointer, and the click that followed
landed on whatever had slid there. Only the second click chose. Here, with the search field holding the
focus, one press on a model that is not the current one must choose it without the list moving: a
Settings single list (the review model, whose current model sits far down the list), the Settings
multi-select (the quick agent models, scrolled part way down first), and the composer's list on a
desktop and a phone.

The quick agent models were also drawn as a narrow column beside the row's words and spilled past the
card and the window; their list must sit under the words across the row, inside its card, at 1440 and
390 px.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_model_picker_click.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, SETTINGS, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")

# Thirty-six models across four providers, as on the installation the fault was seen on: enough that a
# list is taller than its box and the current model is out of sight when it opens.
PROVIDERS = ["deepseek", "claude", "opencode", "vllm"]
EXTRA = {
    f"bulk-{i:02d}": {"provider": PROVIDERS[i % 4], "model": f"bulk-model-{i:02d}", "label": f"Bulk Model {i:02d}", "thinking": i % 2 == 0, "reasoning_effort": "medium" if i % 2 == 0 else "",
                      "images": i % 3 == 0, "context_window": 128000 + i * 1000, "context_window_source": "table", "max_output_tokens": 8192}
    for i in range(32)
}
LAST = "bulk-31"


def settings_with_models() -> dict:
    settings = copy.deepcopy(SETTINGS)
    settings["presets"] = {**settings["presets"], **EXTRA}
    settings["on_demand_defaults"] = {**settings.get("on_demand_defaults", {}), **{k: True for k in EXTRA}}
    # The review model is the last of the list, so opening the list shows its top and the current row is
    # far below it: the case where scrolling to the current row moved the list under the pointer.
    settings["action_rules"] = {**settings["action_rules"], "review_preset": LAST}
    settings.setdefault("subagents", {})["quick_models"] = []
    # Settings saves nothing without the revision it read, and the shared pictures' settings carry none.
    settings["revision"] = "r1"
    return settings


def open_page(browser, width: int, path: str, settings: dict, sent: list) -> tuple:  # type: ignore[no-untyped-def]
    mobile = width < 600
    context = browser.new_context(viewport={"width": width, "height": 900}, is_mobile=mobile, has_touch=mobile)
    page = context.new_page()
    page.set_default_timeout(10000)
    page.route("**/api/**", stub)

    def settings_route(route):  # type: ignore[no-untyped-def]
        request = route.request
        if request.method != "GET":
            body = request.post_data_json
            sent.append(("settings", body))
            for key, value in (body or {}).items():
                if key != "base_revision":
                    settings[key] = value
        route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))

    page.route("**/api/settings", settings_route)
    page.route("**/api/settings/validate", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({"valid": True, "stale": False, "problems": []})))

    def model_route(route):  # type: ignore[no-untyped-def]
        if route.request.method != "GET":
            sent.append(("model", route.request.post_data_json))
        route.fallback()

    page.route(f"**/api/sessions/{S1}/model", model_route)
    page.goto(f"{BASE}/{path}{'&' if '?' in path else '?'}token=t&lang=en")
    return context, page


def scroll_tops(page: Page, list_selector: str) -> dict:
    """The list's own scroll and every scrolled ancestor's, so a jump of the page counts as well."""
    return page.locator(list_selector).first.evaluate("""el => {
        const out = {list: el.scrollTop};
        let n = el.parentElement, i = 0;
        while (n) { if (n.scrollHeight > n.clientHeight + 1) out['up' + (i++)] = n.scrollTop; n = n.parentElement; }
        out.window = window.scrollY;
        return out;
    }""")


def press_once(page: Page, row, list_selector: str, where: str, failures: list[str]) -> None:
    """One press and release at the row's centre, the way a hand does it, checking between the two that
    nothing scrolled: the jump happens on the press, and the release then lands on another row."""
    row.scroll_into_view_if_needed()
    box = row.bounding_box()
    assert box, f"{where}: the row has no box"
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    before = scroll_tops(page, list_selector)
    page.mouse.move(x, y)
    page.mouse.down()
    page.wait_for_timeout(120)
    after = scroll_tops(page, list_selector) if page.locator(list_selector).count() else before
    if after != before:
        failures.append(f"{where}: pressing a row scrolled the list: {before} -> {after}")
    page.mouse.up()
    page.wait_for_timeout(300)


def settings_single(browser, width: int, failures: list[str]) -> None:  # type: ignore[no-untyped-def]
    where = f"{width} review model"
    sent: list = []
    context, page = open_page(browser, width, "settings/tasks", settings_with_models(), sent)
    row = page.locator("[data-setting='action_rules.review_preset']")
    row.wait_for()
    row.scroll_into_view_if_needed()
    # The row holds the model's picker and, under it, its effort's; the model's is the one under test.
    row = row.locator("#models-review-preset")
    row.locator(".sel-current button").click()
    search = row.locator(".sel-search-field")
    search.wait_for()
    page.wait_for_timeout(150)
    if not search.evaluate("el => el === document.activeElement"):
        failures.append(f"{where}: opening the list did not give the search field the keyboard")
    target = row.locator(".sel-list [data-id='claude-opus-5']")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/single-{width}-open.png")
    press_once(page, target, "[data-setting='action_rules.review_preset'] .sel-list", where, failures)
    picked = [body for kind, body in sent if kind == "settings" and isinstance(body, dict) and "action_rules" in body]
    if not picked or picked[-1]["action_rules"].get("review_preset") != "claude-opus-5":
        failures.append(f"{where}: one click on Claude Opus 5 did not choose it: {picked}")
    if row.locator(".sel-list").count():
        failures.append(f"{where}: the list is still open after a pick")
    # The keyboard still works: open, ArrowDown into the list, ArrowDown, Enter chooses the second row.
    row.locator(".sel-current button").click()
    search.wait_for()
    page.wait_for_timeout(150)
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    second = row.locator(".sel-list [data-index='1']").get_attribute("data-id")
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    picked = [body for kind, body in sent if kind == "settings" and isinstance(body, dict) and "action_rules" in body]
    if not picked or picked[-1]["action_rules"].get("review_preset") != second:
        failures.append(f"{where}: ArrowDown, ArrowDown, Enter did not choose the second row ({second}): {picked[-1:]}")
    # Escape from the search field closes the list.
    row.locator(".sel-current button").click()
    search.wait_for()
    page.wait_for_timeout(150)
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    if row.locator(".sel-list").count():
        failures.append(f"{where}: Escape left the list open")
    context.close()


def inside(page: Page, container: str, inner: str, where: str, failures: list[str]) -> None:
    """No element under `inner` ends right of `container` or starts left of it."""
    over = page.evaluate("""([container, inner]) => {
        const box = document.querySelector(container);
        if (!box) return ['no ' + container];
        const outer = box.getBoundingClientRect();
        const out = [];
        for (const el of document.querySelectorAll(inner)) {
            const r = el.getBoundingClientRect();
            if (!r.width && !r.height) continue;
            if (r.right > outer.right + 0.5 || r.left < outer.left - 0.5)
                out.push(`${el.className || el.tagName} [${Math.round(r.left)}, ${Math.round(r.right)}] in [${Math.round(outer.left)}, ${Math.round(outer.right)}]`);
        }
        return out.slice(0, 5);
    }""", [container, inner])
    if over:
        failures.append(f"{where}: past the edge of {container}: {over}")


def settings_multi(browser, width: int, failures: list[str]) -> None:  # type: ignore[no-untyped-def]
    where = f"{width} quick agent models"
    sent: list = []
    context, page = open_page(browser, width, "settings/tasks", settings_with_models(), sent)
    setting = "[data-setting='subagents.quick_models']"
    row = page.locator(setting)
    row.wait_for()
    row.scroll_into_view_if_needed()
    page.wait_for_timeout(200)
    if SHOTS:
        row.screenshot(path=f"{SHOTS}/quick-{width}.png")
        page.screenshot(path=f"{SHOTS}/quick-{width}-page.png")
    # The list sits under the words across the row: its box starts where the words start.
    text = row.locator(".settings-row-text").bounding_box()
    # The row holds the model list and, under it, its effort's; the list is the one measured.
    box = row.locator("#quick-models").bounding_box()
    if not text or not box or abs(box["x"] - text["x"]) > 1 or box["y"] < text["y"] + text["height"] - 1:
        failures.append(f"{where}: the list is not under the row's words: words {text}, list {box}")
    inside(page, f"{setting}", f"{setting} *", where, failures)
    card = row.evaluate("el => { const c = el.closest('.card'); c.setAttribute('data-probe-card', ''); return true; }")
    if card:
        inside(page, "[data-probe-card]", f"{setting} *", where, failures)
    inside(page, ".settings-col, .main", f"{setting} *", where, failures)
    if page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"):
        failures.append(f"{where}: the page scrolls sideways")
    # Scrolled part way down, the search field holding the keyboard, one press on a visible row.
    lst = f"{setting} .sel-list"
    page.locator(lst).evaluate("el => { el.scrollTop = 260; }")
    row.locator(".sel-search-field").focus()
    page.wait_for_timeout(100)
    target = page.locator(lst).locator("[role=option]").filter(has_not=page.locator(".on")).nth(8)
    target_id = target.get_attribute("data-id")
    press_once(page, target, lst, where, failures)
    picked = [body for kind, body in sent if kind == "settings" and isinstance(body, dict) and "subagents" in body]
    if not picked or target_id not in (picked[-1]["subagents"].get("quick_models") or []):
        failures.append(f"{where}: one click on {target_id} did not choose it: {picked[-1:]}")
    if page.locator(f"{lst} [data-id='{target_id}']").get_attribute("aria-selected") != "true":
        failures.append(f"{where}: {target_id} is not checked after one click")
    # The keyboard: ArrowDown from the search field into the list, Space takes the first row.
    row.locator(".sel-search-field").focus()
    page.keyboard.press("ArrowDown")
    first = page.locator(f"{lst} [data-index='0']").get_attribute("data-id")
    page.keyboard.press(" ")
    page.wait_for_timeout(300)
    picked = [body for kind, body in sent if kind == "settings" and isinstance(body, dict) and "subagents" in body]
    if not picked or first not in (picked[-1]["subagents"].get("quick_models") or []):
        failures.append(f"{where}: ArrowDown and Space did not take the first row ({first}): {picked[-1:]}")
    if SHOTS:
        row.screenshot(path=f"{SHOTS}/quick-{width}-chosen.png")
    context.close()


# Every section of Settings that embeds a list of models (or another in-place list) in a row.
SECTIONS = ["models", "tasks", "translation", "rules", "approvals", "context", "memory", "memory-upkeep", "browser", "browser-safety", "search", "dependencies", "chat", "voice", "speech-models", "schedules", "tools", "limits"]


def sweep(browser, width: int, failures: list[str]) -> None:  # type: ignore[no-untyped-def]
    """Each row holding a list has it under its words and nothing of it past the row's edges, with the
    subagent models' own list open as well."""
    settings = settings_with_models()
    settings["subagents"]["models"] = {"mode": "chosen", "presets": ["deepseek-flash"]}
    context, page = open_page(browser, width, "settings/models", settings, [])
    page.wait_for_timeout(500)
    seen = 0
    for section in SECTIONS:
        page.goto(f"{BASE}/settings/{section}?token=t&lang=en")
        page.wait_for_timeout(700)
        found = page.evaluate("""() => {
            const out = [];
            let n = 0;
            for (const row of document.querySelectorAll('.settings-row')) {
                const control = row.querySelector(':scope > .settings-row-ctl');
                if (!control || !control.querySelector('.sel, .subagent-models')) continue;
                n++;
                const name = row.dataset.setting || row.querySelector('.settings-row-title')?.textContent || '?';
                const outer = row.getBoundingClientRect();
                const words = row.querySelector(':scope > .settings-row-text')?.getBoundingClientRect();
                const box = control.getBoundingClientRect();
                if (words && (box.top < words.bottom - 1 || Math.abs(box.left - words.left) > 1)) out.push(`${name}: the list is beside its words, not under them`);
                for (const el of row.querySelectorAll('*')) {
                    const r = el.getBoundingClientRect();
                    if (!r.width && !r.height) continue;
                    if (r.right > outer.right + 0.5 || r.left < outer.left - 0.5) { out.push(`${name}: ${String(el.className)} [${Math.round(r.left)}, ${Math.round(r.right)}] past [${Math.round(outer.left)}, ${Math.round(outer.right)}]`); break; }
                }
            }
            return [n, out];
        }""")
        seen += found[0]
        failures.extend(f"{width} settings/{section}: {line}" for line in found[1])
    if seen < 8:
        failures.append(f"{width}: the sweep found only {seen} rows with a list; the sections moved")
    context.close()


def composer(browser, width: int, failures: list[str]) -> None:  # type: ignore[no-untyped-def]
    where = f"{width} composer"
    mobile = width < 600
    sent: list = []
    context, page = open_page(browser, width, f"agents/{S1}", settings_with_models(), sent)
    page.wait_for_selector(".composer", timeout=15000)
    page.locator(".composer .model-select").click()
    page.wait_for_selector(".model-picker [role=option]")
    page.wait_for_timeout(200)
    search = page.locator(".model-picker .model-search")
    if not mobile and not search.evaluate("el => el === document.activeElement"):
        failures.append(f"{where}: opening the list did not give the search field the keyboard")
    # Onto a provider's tab, then back to the search field, as the operator does before clicking.
    page.locator(".model-picker [role=tab][data-provider='opencode']").click()
    page.wait_for_timeout(200)
    search.focus()
    page.wait_for_timeout(100)
    rows = page.locator(".model-picker [role=option][data-preset]:not(.on)").filter(has_text="Bulk Model")
    target = rows.nth(min(5, rows.count() - 1))
    label = target.locator(".model-row-name").inner_text().strip()
    target_id = next((k for k, v in settings_with_models()["presets"].items() if v["label"] == label), "")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/composer-{width}-open.png")
    press_once(page, target, ".model-picker-list", where, failures)
    picked = [body for kind, body in sent if kind == "model"]
    if not picked or picked[-1] != {"preset": target_id}:
        failures.append(f"{where}: one click on {label} ({target_id}) did not choose it: {picked}")
    context.close()


def main() -> int:
    expect_app(BASE)
    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        for width in (1440, 390):
            for scene in (settings_single, settings_multi, sweep, composer):
                try:
                    scene(browser, width, failures)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{width} {scene.__name__}: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        browser.close()
    if failures:
        print("FAILED")
        for failure in failures:
            print(" -", failure)
        return 1
    print("ok: one click chooses in every model list, and the quick agent list stays inside its row")
    return 0


if __name__ == "__main__":
    sys.exit(main())
