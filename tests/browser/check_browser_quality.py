"""The live picture's quality is the operator's to choose, in English and Russian, at 1440 × 900:

- the toolbar has a quality menu naming the current preset, Auto by default;
- choosing Sharp sends the open view a VIEW with JPEG 92, four frames a second and no adaptation, at
  once, without reopening the view;
- the choice is the device's: a window opened later attaches with it;
- going back to Auto names the daemon's defaults again (quality 60, its own frame cap, adaptation on),
  since a VIEW leaves out what it does not change.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_browser_quality.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_stub import BrowserStub, open_page, render_scenes, wait_frames  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ROOT = ".panel .bp"
WORDS = {
    "en": {"auto": "Auto", "sharp": "Sharp", "label": "Picture quality"},
    "ru": {"auto": "Авто", "sharp": "Чётко", "label": "Качество картинки"},
}


def wait_for(page, predicate, seconds: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    """Wait on the page's own clock: the stub's socket is served by the same loop, which a sleep stops."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        page.wait_for_timeout(100)
    return predicate()


def check(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{lang}] {text}")  # noqa: E731
    words = WORDS[lang]
    bs = BrowserStub(scenes)
    bs.add("g1", scene="shop", owner_id=S1)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    url = f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}&panel=browser"
    page = open_page(context, bs, stub, url)
    page.wait_for_selector(f"{ROOT} .bv[data-state='live']", timeout=10000)
    wait_frames(page, ROOT, 1)
    menu = page.locator(f"{ROOT} button.bp-quality").first
    label = menu.get_attribute("aria-label") or ""
    if label != f"{words['label']}: {words['auto']}":
        say(f"the quality menu reads {label!r}")
    live = next(c for c in bs.clients if c.tier == "live" and not c.closed)
    first = live.of("attach")[0]
    if first.get("quality") or first.get("adaptive") is False:
        say(f"Auto attached with a preset's settings: {first}")
    menu.click()
    items = page.locator("[role='menuitemcheckbox']")
    names = [items.nth(i).inner_text().strip() for i in range(items.count())]
    print(f"[{lang}] presets: {names}")
    if len(names) != 4 or names[0] != words["auto"] or names[3] != words["sharp"]:
        say(f"the presets are {names}")
    if items.nth(0).get_attribute("aria-checked") != "true":
        say("Auto is not the checked preset by default")
    seen = len(live.of("view"))
    items.nth(3).click()
    if not wait_for(page, lambda: any(v.get("quality") == 92 for v in live.of("view")[seen:])):
        say(f"choosing Sharp sent no VIEW with quality 92: {live.of('view')[seen:]}")
    else:
        sharp = next(v for v in live.of("view")[seen:] if v.get("quality") == 92)
        print(f"[{lang}] sharp view: {sharp}")
        if sharp.get("fps") != 4 or sharp.get("adaptive") is not False or sharp.get("max_w", 0) < 64:
            say(f"Sharp is not four frames a second, unadapted: {sharp}")
    if live.closed or len([c for c in bs.clients if c.tier == "live"]) != 1:
        say("choosing a preset reopened the view")
    page.keyboard.press("Escape")
    if (menu.get_attribute("aria-label") or "") != f"{words['label']}: {words['sharp']}":
        say(f"the menu does not name the chosen preset: {menu.get_attribute('aria-label')!r}")
    # The device keeps it: another window attaches sharp at once.
    other = open_page(context, bs, stub, url)
    other.wait_for_selector(f"{ROOT} .bv[data-state='live']", timeout=10000)
    second = [c for c in bs.clients if c.tier == "live" and c is not live][-1]
    attach = second.of("attach")[0]
    print(f"[{lang}] another window attached with {attach}")
    if attach.get("quality") != 92 or attach.get("adaptive") is not False:
        say(f"another window did not attach with the chosen preset: {attach}")
    other.close()
    # Back to Auto: the daemon's defaults named again.
    menu.click()
    seen = len(live.of("view"))
    page.locator("[role='menuitemcheckbox']").nth(0).click()
    if not wait_for(page, lambda: any(v.get("quality") == 60 and v.get("adaptive") is True for v in live.of("view")[seen:])):
        say(f"Auto did not restore the defaults: {live.of('view')[seen:]}")
    page.close()
    context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        for lang in ("en", "ru"):
            check(browser, scenes, lang, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
