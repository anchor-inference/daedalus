"""The model picker with an installation's real number of providers: every one named and within reach.

Fourteen providers, eight of them endpoints with no brand mark (local servers, test endpoints, custom
addresses), one whose key is missing, one with seven models and a 40-character model id. The picker
once showed providers as a strip of icon tabs that scrolled sideways with no scrollbar: seven fitted,
the rest were off the edge with nothing to say they existed, and four of the eight visible were the same
generic glyph, so a provider with seven models could not be found. This asks:

- every provider's name is in the open picker, in a column of rows that scrolls on its own and says so
  (a fade at its foot while rows wait below), and each row can be scrolled into the column's view;
- no two provider rows look the same: the mark and the name differ, and so do the letter tiles of the
  endpoints without a brand;
- the thirteenth provider can be chosen with the keyboard (left from the search, down the column) and
  with the pointer (wheel, click), and its seven models are then listed;
- a search finds a model of a provider that was out of sight, names that provider on the row, and the
  column counts each provider's matches, dimming those with none;
- the provider whose key is missing says so on its row and over its models;
- "Global default" and the model it names fit side by side, and the 40-character id is cut in its
  middle with its end kept and the whole id in the tooltip;
- the box keeps one size through provider switches and typing, and stays inside a 1280x700 window
  from the composer's chip and from the panel's Details row at the right edge;
- on a phone the providers are a row of named chips, faded at the edge with more behind it.

With SHOTS=<dir> it also saves the pictures of the states above at 1920x1080 and 1366x768 (dark,
Russian), 1920x1080 English and light, and the phone sheet at 390x844.

Build the app, serve it with tests/browser/serve_app.py, then run this with APP_URL pointing at it.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, SETTINGS, open_tool, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
LONG_ID = "gemini-3.5-pro-preview-thinking-exp-0925"
assert len(LONG_ID) == 40

# id, kind, name (None: the app's own name for the brand), models, branded
PROVIDERS: list[tuple[str, str, str | None, int, bool]] = [
    ("deepseek", "deepseek", "DeepSeek", 3, True),
    ("vllm", "vllm", "vLLM", 2, False),
    ("work-test-vllm", "vllm", "Work-test-vllm", 2, False),
    ("test-endpoint", "openai_compat", "Test endpoint", 2, False),
    ("test-2", "openai_compat", "test-2", 2, False),
    ("claude", "claude", None, 4, True),
    ("grok", "openai_compat", "xAI Grok", 3, True),
    ("abliteration", "openai_compat", "abliteration", 2, False),
    ("opencode", "opencode", "OpenCode Go", 3, True),
    ("openrouter", "openrouter", "OpenRouter", 4, True),
    ("custom", "openai_compat", "Custom", 2, False),
    ("lab-llamacpp", "llamacpp", "Lab llama.cpp", 2, False),
    ("antigravity", "openai_compat", "Antigravity", 7, True),
    ("ollama-box", "ollama", "Ollama box", 2, False),
]
THIRTEENTH = "antigravity"
MISSING_KEY = "opencode"


def catalogue() -> dict:
    settings = copy.deepcopy(SETTINGS)
    settings["presets"] = {}
    settings["providers"] = {}
    settings["provider_keys"] = {}
    for prov, kind, name, models, _ in PROVIDERS:
        conf = {"kind": kind, "base_url": f"http://127.0.0.1:9000/{prov}", "timeout_seconds": 600, "api_key_set": prov != MISSING_KEY}
        if name:
            conf["name"] = name
        if prov == "claude":
            conf["billing"] = "subscription"
        settings["providers"][prov] = conf
        settings["provider_keys"][prov] = {"via_proxy": True, "key_held": prov != MISSING_KEY, "key_kind": "endpoint" if kind in ("vllm", "llamacpp", "ollama") else "api_key", "ready": prov != MISSING_KEY}
        for i in range(models):
            model = LONG_ID if prov == THIRTEENTH and i == 6 else f"{prov}-model-{i}"
            preset = {"provider": prov, "model": model, "label": "" if model == LONG_ID else f"{(name or prov).split()[0].title()} {['Flash', 'Pro', 'Mini', 'Max', 'Lite', 'Ultra', 'Nano'][i]}",
                      "thinking": i % 2 == 0, "reasoning_effort": "medium", "images": False, "favourite": i == 0 and prov in ("claude", "deepseek", "antigravity"),
                      "context_window": 128000 + i * 64000, "max_output_tokens": 8192}
            settings["presets"][f"{prov}-{i}"] = preset
    # The configured default the operator saw cut: "Global default  deepseek-v4.1-fla…".
    settings["presets"]["deepseek-0"]["label"] = "deepseek-v4.1-flash"
    settings["model"] = {**settings["model"], "preset": "deepseek-0", "chain": ["deepseek-0"]}
    settings["presets"]["free-1"] = {"provider": "openrouter", "model": "free-model", "label": "Free test model", "free_only": True, "thinking": False, "reasoning_effort": "", "images": False, "context_window": 32000, "max_output_tokens": 4096}
    return settings


SETTINGS_BODY = json.dumps(catalogue())
NAMES = {prov: name for prov, _, name, _, _ in PROVIDERS}


def open_page(browser, width: int, height: int, theme: str = "dark", lang: str = "en", panel: bool = False) -> Page:  # type: ignore[no-untyped-def]
    page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=theme)
    page.route("**/api/**", stub)
    page.route("**/api/settings", lambda route: route.fulfill(status=200, content_type="application/json", body=SETTINGS_BODY) if route.request.method == "GET" else stub(route))
    page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}&panel={1 if panel else 0}")
    expect(page.locator(".composer .model-select")).to_be_visible(timeout=15000)
    return page


def open_picker(page: Page) -> None:
    page.locator(".composer .model-select").click()
    page.wait_for_selector(".model-picker [role=option]", timeout=8000)


def size(page: Page) -> tuple[int, int]:
    r = page.locator(".menu.model-picker").bounding_box()
    assert r, "the picker is not drawn"
    return round(r["width"]), round(r["height"])


def rect(page: Page, selector: str) -> dict:
    return page.locator(selector).first.evaluate("el => { const r = el.getBoundingClientRect(); return {left: r.left, top: r.top, right: r.right, bottom: r.bottom}; }")


def inside_window(page: Page, where: str, problems: list[str]) -> None:
    r = rect(page, ".menu.model-picker")
    w, h = page.evaluate("[innerWidth, innerHeight]")
    if r["left"] < 0 or r["top"] < 0 or r["right"] > w + 0.5 or r["bottom"] > h + 0.5:
        problems.append(f"{where}: the picker leaves the {w}x{h} window: {r}")


def provider_rows(page: Page) -> list[dict]:
    return page.locator(".model-picker [role=tab]").evaluate_all("""els => els.map((el) => {
      const mark = el.querySelector('.model-prov-mark');
      const letters = mark.querySelector('.provider-letter');
      return { id: el.dataset.id, name: el.querySelector('.model-prov-name').textContent, letters: letters ? letters.textContent : '',
               mark: letters ? 'letters:' + letters.textContent : mark.innerHTML, selected: el.getAttribute('aria-selected') === 'true',
               state: el.querySelector('.model-prov-state')?.textContent ?? '', count: el.querySelector('.model-prov-count')?.textContent ?? '',
               none: el.classList.contains('none') };
    })""")


def shot(page: Page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        # The box fades in as it opens; taken at once, the page behind it showed through.
        page.wait_for_timeout(400)
        page.screenshot(path=f"{SHOTS}/{name}.png")


def desktop(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    page = open_page(browser, 1280, 700)
    open_picker(page)
    first = size(page)
    inside_window(page, "1280x700 composer", problems)
    rows = provider_rows(page)
    names = [row["name"] for row in rows]
    for prov, name in NAMES.items():
        expected = name or {"claude": "Anthropic · subscription"}.get(prov, prov)
        if expected not in names:
            problems.append(f"provider {prov} ({expected}) is not named in the picker: {names}")
    if names[0] != "Favourites" or names[-1] != "Free":
        problems.append(f"the column does not run Favourites … Free: {names}")
    if [row["id"] for row in rows[1:-1]] != [prov for prov, *_ in PROVIDERS]:
        problems.append(f"the providers are not in the order Settings lists them: {[row['id'] for row in rows]}")
    pairs = [(row["mark"], row["name"]) for row in rows]
    if len(set(pairs)) != len(pairs):
        problems.append(f"two provider rows look the same: {pairs}")
    letters = [row["letters"] for row in rows if row["letters"]]
    unbranded = sum(1 for *_, branded in PROVIDERS if not branded)
    if len(letters) != unbranded or len(set(letters)) != len(letters):
        problems.append(f"the {unbranded} endpoints without a brand do not each have letters of their own: {letters}")
    expected_letters = {"work-test-vllm": "WT", "abliteration": "AB", "test-2": "T2"}
    for row in rows:
        if row["id"] in expected_letters and row["letters"] != expected_letters[row["id"]]:
            problems.append(f"{row['id']} wears {row['letters']!r}, not {expected_letters[row['id']]!r}")
    missing = next(row for row in rows if row["id"] == MISSING_KEY)
    if missing["state"] not in ("no key",):
        problems.append(f"the provider without a key does not say so on its row: {missing}")
    # The column is a list that scrolls on its own, says that more waits below, and every row can be
    # brought into its view by its own scroll.
    column = page.locator(".model-picker-provs")
    geometry = column.evaluate("el => ({ scroll: el.scrollHeight, client: el.clientHeight, overflow: getComputedStyle(el).overflowY, more: el.dataset.more || '' })")
    if not (geometry["scroll"] > geometry["client"] and geometry["overflow"] == "auto" and geometry["more"] == "below"):
        problems.append(f"the providers' column is not a scrolling list that says more waits below: {geometry}")
    hidden = column.evaluate("""el => [...el.querySelectorAll('[role=tab]')].filter((row) => {
      el.scrollTop = row.offsetTop - el.clientHeight / 2;
      const a = el.getBoundingClientRect(), b = row.getBoundingClientRect();
      return b.top < a.top - 0.5 || b.bottom > a.bottom + 0.5;
    }).map((row) => row.dataset.id)""")
    column.evaluate("el => { el.scrollTop = 0; }")
    if hidden:
        problems.append(f"rows the column's own scroll cannot bring into view: {hidden}")
    if column.evaluate("el => el.scrollWidth > el.clientWidth + 1"):
        problems.append("the providers' column scrolls sideways")
    # "Global default" and the model it names fit side by side (the default's provider, DeepSeek).
    page.locator(".model-picker [role=tab][data-provider='deepseek']").click()
    global_row = page.locator(".model-picker .model-row.global")
    expect(global_row).to_have_count(1)
    cut = global_row.evaluate("""el => { const name = el.querySelector('.model-row-name'); const head = el.querySelector('.model-row-meta .cut-head');
      return { name: name.scrollWidth > name.clientWidth + 0.5, meta: head.scrollWidth > head.clientWidth + 0.5, text: el.textContent }; }""")
    if cut["name"] or cut["meta"]:
        problems.append(f"the Global default row is cut at 1280: {cut}")
    shot(page, "check-1280-global-default")
    # The keyboard: left from the search into the column, down it to the thirteenth provider.
    search = page.locator(".model-picker .model-search")
    search.focus()
    page.keyboard.press("ArrowLeft")
    focused = page.evaluate("document.activeElement?.dataset?.id")
    if focused != "deepseek":
        problems.append(f"the left arrow from the search did not land on the provider on show: {focused}")
    page.keyboard.press("Home")
    for _ in range(13):
        page.keyboard.press("ArrowDown")
    tab = page.locator(f".model-picker [role=tab][data-provider='{THIRTEENTH}']")
    expect(tab).to_have_attribute("aria-selected", "true")
    expect(tab).to_be_focused()
    options = page.locator(".model-picker [role=option]")
    expect(options).to_have_count(7)
    if size(page) != first:
        problems.append(f"walking the providers changed the box from {first} to {size(page)}")
    page.keyboard.press("ArrowRight")
    expect(search).to_be_focused()
    page.keyboard.press("ArrowDown")
    active = page.locator(".model-picker [role=option].active")
    if search.get_attribute("aria-activedescendant") != active.get_attribute("id"):
        problems.append("the search does not point at the highlighted model after the column")
    # Tab crosses from the list to the column and back round.
    page.keyboard.press("Tab")
    expect(tab).to_be_focused()
    # Typing in the column goes to the search.
    page.keyboard.type("09")
    expect(search).to_be_focused()
    expect(search).to_have_value("09")
    page.keyboard.press("Escape")
    expect(page.locator(".model-picker")).to_have_count(0)
    # The 40-character id: cut in its middle, its end kept whole, its whole name in the tooltip.
    open_picker(page)
    page.locator(".model-picker-provs").hover()
    page.mouse.wheel(0, 600)
    tab = page.locator(f".model-picker [role=tab][data-provider='{THIRTEENTH}']")
    tab.click()
    expect(page.locator(".model-picker [role=option]")).to_have_count(7)
    long_row = page.locator(f".model-picker [role=option][data-preset='{THIRTEENTH}-6']")
    long = long_row.evaluate("""el => { const head = el.querySelector('.model-row-name .cut-head'), tail = el.querySelector('.model-row-name .cut-tail');
      return { title: el.title, tail: tail ? tail.textContent : '', tailCut: tail ? tail.scrollWidth > tail.clientWidth + 0.5 : true, text: el.querySelector('.model-row-name').textContent }; }""")
    if LONG_ID not in long["title"] or long["text"] != LONG_ID or not LONG_ID.endswith(long["tail"]) or len(long["tail"]) < 6 or long["tailCut"]:
        problems.append(f"the 40-character id is not cut in its middle with its end and tooltip whole: {long}")
    shot(page, "check-1280-thirteenth")
    if size(page) != first:
        problems.append(f"choosing with the pointer changed the box from {first} to {size(page)}")
    # A search for a model of a provider out of sight.
    page.locator(".model-picker-provs").evaluate("el => { el.scrollTop = 0; }")
    search = page.locator(".model-picker .model-search")
    search.click()
    sizes = set()
    for letter in "0925":
        page.keyboard.type(letter)
        sizes.add(size(page))
    expect(page.locator(".model-picker [role=option]")).to_have_count(1)
    meta = page.locator(".model-picker [role=option] .model-row-meta").first.text_content()
    if meta != "Antigravity":
        problems.append(f"the match does not name its provider: {meta!r}")
    rows = provider_rows(page)
    counted = {row["id"]: (row["count"], row["none"]) for row in rows}
    if counted.get(THIRTEENTH) != ("1", False) or counted.get("deepseek") != ("0", True):
        problems.append(f"the column does not count each provider's matches: {counted}")
    if sizes != {first}:
        problems.append(f"typing changed the box: {sorted(sizes)} from {first}")
    page.keyboard.press("Escape")
    page.close()
    # Near the right edge: the panel's Details row, at 1280x700.
    page = open_page(browser, 1280, 700, panel=True)
    open_tool(page, "details")
    page.locator(".panel .details button.dt-kv[data-prop='model']").click()
    page.wait_for_selector(".menu.model-picker [role=option]", timeout=8000)
    inside_window(page, "1280x700 panel Details", problems)
    if size(page) != first:
        problems.append(f"the panel's picker is {size(page)}, the composer's {first}")
    page.close()
    print(f"desktop 1280x700: box {first[0]}x{first[1]}, {len(names)} rows in the providers' column, letters {letters}")


def phone(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    page = open_page(browser, 390, 844, lang="ru")
    page.locator(".composer .model-select").click()
    page.wait_for_selector(".sheet .model-picker [role=option]", timeout=8000)
    page.wait_for_timeout(500)
    chips = page.locator(".sheet .model-picker [role=tab]")
    expect(chips).to_have_count(len(PROVIDERS) + 2)
    empty = chips.evaluate_all("els => els.filter((el) => !el.querySelector('.model-prov-name').textContent.trim() || el.querySelector('.model-prov-name').getBoundingClientRect().width < 8).map((el) => el.dataset.id)")
    if empty:
        problems.append(f"chips without a visible name on the phone: {empty}")
    column = page.locator(".sheet .model-picker-provs")
    fade = column.evaluate("el => el.dataset.fade || ''")
    if fade != "end":
        problems.append(f"the phone's chips do not fade at the edge with more behind it: {fade!r}")
    shot(page, "phone-390-ru-favourites")
    page.locator(f".sheet .model-picker [role=tab][data-provider='{THIRTEENTH}']").scroll_into_view_if_needed()
    page.locator(f".sheet .model-picker [role=tab][data-provider='{THIRTEENTH}']").click()
    expect(page.locator(".sheet .model-picker [role=option]")).to_have_count(7)
    # Back to the first chips: they are still within reach of the row's own scroll.
    column.evaluate("el => { el.scrollLeft = 0; }")
    if column.evaluate("el => el.scrollLeft") != 0:
        problems.append("the first chips cannot be scrolled back to")
    column.evaluate(f"el => {{ const chip = el.querySelector('[data-id=\"{THIRTEENTH}\"]'); el.scrollLeft = chip.offsetLeft - 40; }}")
    page.wait_for_timeout(100)
    shot(page, "phone-390-ru-thirteenth")
    if page.evaluate("document.documentElement.scrollWidth > innerWidth"):
        problems.append("the phone page scrolls sideways with the sheet open")
    page.close()
    print("phone 390x844: named chips, faded edge")


def pictures(browser) -> None:  # type: ignore[no-untyped-def]
    for width, height, theme, lang in ((1920, 1080, "dark", "ru"), (1366, 768, "dark", "ru"), (1920, 1080, "dark", "en"), (1920, 1080, "light", "ru")):
        tag = f"{width}-{theme}-{lang}"
        page = open_page(browser, width, height, theme, lang)
        open_picker(page)
        shot(page, f"{tag}-favourites")
        page.locator(f".model-picker [role=tab][data-provider='{THIRTEENTH}']").click()
        page.mouse.move(5, 5)
        shot(page, f"{tag}-mid-list-provider")
        if theme == "light" or lang == "en":
            page.close()
            continue
        page.locator(f".model-picker [role=tab][data-provider='{MISSING_KEY}']").click()
        page.mouse.move(5, 5)
        shot(page, f"{tag}-missing-key")
        page.locator(".model-picker-provs").evaluate("el => { el.scrollTop = 0; }")
        page.locator(".model-picker .model-search").fill("pro")
        page.mouse.move(5, 5)
        shot(page, f"{tag}-searching")
        page.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desktop(browser, problems)
        phone(browser, problems)
        if SHOTS:
            pictures(browser)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
