"""Does a select survive being opened and closed fifty times on a starved CPU?

Base UI's Select is pinned at 1.9.0 because of its issue #5358: on a busy machine a rapid open and
close could leave the list re-opening itself in a loop, and the page stopped answering. This host
runs many agents at once, so that is the condition the app lives in. The check opens and closes the
select on the primitives page fifty times under a four-times CPU throttle — half of them waiting for
the list to show, half of them closing it again at once — and then asks three things: the list is
closed and the trigger says so, the page still answers a frame within two seconds, and no single
task blocked the main thread for longer than a second.

Build the app, serve it with tests/browser/serve_app.py, then run this with APP_URL pointing at it.
Exit 0 when the select settles every time.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ROUNDS = 50
THROTTLE = 4
LONGEST_TASK_MS = 1000


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/primitives?token=t&lang=en")
        trigger = page.locator("#primitives-select")
        expect(trigger).to_be_visible(timeout=15000)
        page.evaluate("""() => {
          window.__longest = 0; window.__long = 0;
          new PerformanceObserver((list) => { for (const entry of list.getEntries()) { window.__long += 1; window.__longest = Math.max(window.__longest, entry.duration); } })
            .observe({ type: 'longtask', buffered: false });
        }""")
        cdp = page.context.new_cdp_session(page)
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": THROTTLE})
        listbox = page.locator(".dropdown-list")
        started = time.monotonic()
        for i in range(ROUNDS):
            trigger.click()
            if i % 2 == 0:
                expect(listbox).to_be_visible(timeout=5000)
                page.keyboard.press("Escape")
            else:
                # The rapid case of the issue: the close lands before the list has finished opening.
                trigger.click()
            expect(listbox).to_have_count(0, timeout=5000)
        spent = time.monotonic() - started
        expect(trigger).to_have_attribute("aria-expanded", "false")
        answered = page.evaluate("""() => new Promise((resolve) => {
          const at = performance.now();
          requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now() - at)));
          setTimeout(() => resolve(-1), 2000);
        })""")
        longest, count = page.evaluate("() => [window.__longest, window.__long]")
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 1})
        print(f"{ROUNDS} opens and closes at {THROTTLE}x throttle in {spent:.1f}s; long tasks {count}, longest {longest:.0f} ms; a frame after them in {answered:.0f} ms")
        if answered < 0:
            problems.append("the page did not answer a frame within two seconds after the last close")
        if longest > LONGEST_TASK_MS:
            problems.append(f"a task held the main thread for {longest:.0f} ms (limit {LONGEST_TASK_MS})")
        # And it still works: one more open picks a value.
        trigger.click()
        page.locator(".dropdown-list [role=option][data-value='en']").click()
        expect(trigger).to_have_attribute("data-value", "en")
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
