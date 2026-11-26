"""Does the model picker stay quick and one size with a long catalogue on a starved CPU?

An installation used for a while holds fourteen providers of six models each and more. The picker is
opened and closed thirty times under a four-times CPU throttle, half of them closed before the list has
drawn; then, opened once more, a search is typed a letter at a time, the left arrow crosses into the
providers' column and the down arrow walks every provider in it, then the right arrow comes back and the
down arrow walks the rows. It asks four things: no task held the main thread for a second, the page still
answers a frame within two seconds, the box never changed its size through any of it, and a pick still
reaches the host.

Build the app, serve it with tests/browser/serve_app.py, then run this with APP_URL pointing at it.
Exit 0 when every claim holds; the longest task and the count are printed for the record.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, SETTINGS, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ROUNDS = 30
THROTTLE = 4
LONGEST_TASK_MS = 1000
PROVIDERS = ["deepseek", "claude", "anthropic", "openai", "codex", "google", "antigravity", "openrouter", "opencode", "zai", "grok", "vllm", "ollama", "lmstudio"]


def catalogue() -> dict:
    settings = copy.deepcopy(SETTINGS)
    for prov in PROVIDERS:
        for i in range(6):
            settings["presets"][f"{prov}-m{i:02d}"] = {"provider": prov, "model": f"{prov}-model-{i:02d}", "label": f"{prov.title()} Model {i:02d}", "thinking": i % 2 == 0,
                                                     "reasoning_effort": "medium", "images": False, "favourite": i == 0, "context_window": 128000 + i * 1000, "max_output_tokens": 8192}
    return settings


def run() -> int:
    problems: list[str] = []
    chosen: list[dict] = []
    settings = catalogue()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.route("**/api/**", stub)
        page.route("**/api/settings", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(settings)) if route.request.method == "GET" else stub(route))

        def model_route(route):  # type: ignore[no-untyped-def]
            if route.request.method == "POST":
                chosen.append(route.request.post_data_json)
            stub(route)
        page.route(f"**/api/sessions/{S1}/model", model_route)
        page.goto(f"{BASE}/agents/{S1}?token=t&lang=en&panel=0")
        trigger = page.locator(".composer .model-select")
        expect(trigger).to_be_visible(timeout=15000)
        page.evaluate("""() => {
          window.__longest = 0; window.__long = 0;
          new PerformanceObserver((list) => { for (const entry of list.getEntries()) { window.__long += 1; window.__longest = Math.max(window.__longest, entry.duration); } })
            .observe({ type: 'longtask', buffered: false });
        }""")
        cdp = page.context.new_cdp_session(page)
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": THROTTLE})
        picker = page.locator(".menu.model-picker")
        sizes: set[tuple[int, int]] = set()

        def size() -> None:
            box = picker.bounding_box()
            if box:
                sizes.add((round(box["width"]), round(box["height"])))
        started = time.monotonic()
        for i in range(ROUNDS):
            trigger.click()
            if i % 2 == 0:
                page.wait_for_selector(".model-picker [role=option]", timeout=8000)
                size()
                page.keyboard.press("Escape")
            else:
                # The close lands before the list has finished drawing.
                trigger.click()
            expect(picker).to_have_count(0, timeout=8000)
        trigger.click()
        page.wait_for_selector(".model-picker [role=option]", timeout=8000)
        size()
        for letter in "model 0":
            page.keyboard.type(letter)
            size()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.press("ArrowLeft")
        for _ in range(len(PROVIDERS) + 2):
            page.keyboard.press("ArrowDown")
            size()
        page.keyboard.press("ArrowRight")
        for _ in range(8):
            page.keyboard.press("ArrowDown")
        size()
        spent = time.monotonic() - started
        answered = page.evaluate("""() => new Promise((resolve) => {
          const at = performance.now();
          requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now() - at)));
          setTimeout(() => resolve(-1), 2000);
        })""")
        longest, count = page.evaluate("() => [window.__longest, window.__long]")
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 1})
        before = len(chosen)
        page.keyboard.press("Enter")
        expect(picker).to_have_count(0, timeout=8000)
        deadline = time.monotonic() + 5
        while len(chosen) == before and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        print(f"{ROUNDS} opens and closes, a search typed, {len(PROVIDERS) + 2} providers walked, at {THROTTLE}x throttle in {spent:.1f}s; "
              f"long tasks {count}, longest {longest:.0f} ms; a frame after them in {answered:.0f} ms; box sizes {sorted(sizes)}")
        if answered < 0:
            problems.append("the page did not answer a frame within two seconds")
        if longest > LONGEST_TASK_MS:
            problems.append(f"a task held the main thread for {longest:.0f} ms (limit {LONGEST_TASK_MS})")
        if len(sizes) != 1:
            problems.append(f"the picker changed its size: {sorted(sizes)}")
        if len(chosen) == before or "preset" not in (chosen[-1] or {}):
            problems.append(f"Enter on a row did not pick a model ({chosen[-1:]})")
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
