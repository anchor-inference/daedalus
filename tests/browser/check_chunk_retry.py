"""A screen whose file failed to download still opens, and the stale-app notice means what it says.

What went wrong: every screen past the first is a chunk of its own, and its loader was retried twice
before the page gave up and said "This screen belongs to an older version of the app". The retries
never reached the network. Chromium keeps a module whose download failed as failed for the rest of
the page's life, so `import()` of the same address again rejects at once with the same error. One
dropped request was therefore a dead page: on a phone that changes networks, and in the browser
checks on this machine, where Chromium cancels whatever is in flight with `ERR_NETWORK_CHANGED`
whenever a container starts or stops and an address appears on the host.

Four cases, each walking from orchestration's main chat to a project, its board, a staff member's
session and back:

- the screens' own files are refused once: each is asked for again under a new address, the screens
  arrive, and the page is never reloaded;
- every file is refused once, the shared ones too: a shared file stays failed for the page's life, so
  the page reloads itself, once, and then everything arrives, with no notice drawn;
- the files are gone (404): the stale-app notice, and no reload;
- the files are there but never arrive: one reload, then the notice that the download dropped, not a
  loop of reloads.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from check_orchestration_mode import go, serve, stubs  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
CHUNK = re.compile(r"/app/assets/[^/?]+\.js(\?|$)")
# The files the screens on this walk are loaded from and nothing imports by name. The board is not
# one: the conversation's panel imports it, so it is a shared file and belongs to the second case.
SCREENS = re.compile(r"(MainScreen|Session|ProjectScreen|ProjectSidebar|StaffView)-")
STALE = "This screen belongs to an older version of the app"
DROPPED = "Part of the app did not download"


class Refuser:
    """Refuses requests for chunks: the first for each (`once`), every one (`always`), or answers 404 (`gone`).

    Only the names `which` matches are touched, and the entry never is: refusing it tests the browser."""

    def __init__(self, entry: str, mode: str, which: re.Pattern[str] = re.compile(".")) -> None:
        self.entry, self.mode, self.which = entry, mode, which
        self.refused: set[str] = set()
        self.served: list[str] = []

    def __call__(self, route) -> None:  # type: ignore[no-untyped-def]
        url = route.request.url
        name = url.split("/app/assets/", 1)[1].split("?", 1)[0]
        if name == self.entry or not self.which.match(name):
            return route.continue_()
        if self.mode == "gone":
            return route.fulfill(status=404, body="")
        if route.request.method == "GET" and (self.mode == "always" or name not in self.refused):
            self.refused.add(name)
            return route.abort("internetdisconnected")
        self.served.append(url.split("/app/assets/", 1)[1])
        return route.continue_()


def entry_of(page: Page) -> str:
    html = page.request.get(f"{BASE}/").text()
    found = re.search(r'src="/app/assets/([^"]+\.js)"', html)
    assert found, "the page names no entry script"
    return found.group(1)


def opened(page: Page, mode: str, which: re.Pattern[str] = re.compile(".")) -> tuple[Refuser, list[str]]:
    refuser = Refuser(entry_of(page), mode, which)
    loads: list[str] = []
    page.on("load", lambda: loads.append(page.url))
    page.route(CHUNK, refuser)
    focus, main = stubs("en")
    serve(page, focus, main, "en")
    go(page, "/orchestration", "en")
    return refuser, loads


def no_notice(page: Page, where: str) -> None:
    text = page.locator(".app").inner_text()
    for notice in (STALE, DROPPED):
        assert notice not in text, f"{where}: {notice!r} was drawn after a download that recovers"


def walk(page: Page) -> None:
    expect(page.locator(".main-flow")).to_be_visible(timeout=20000)
    no_notice(page, "main chat")
    page.locator("nav.orch-sidebar .orch-row").first.click()
    expect(page.locator(".chat.in-project.orchestrator")).to_be_visible(timeout=15000)
    no_notice(page, "project")
    side = page.locator("nav.project-sidebar")
    side.locator(".focus-staff", has_text="Lev").click()
    expect(page.locator(".staff-head")).to_contain_text("Lev", timeout=15000)
    no_notice(page, "staff session")
    side.locator(".focus-row", has_text="Board").click()
    expect(page.locator(".pboard")).to_be_visible(timeout=15000)
    no_notice(page, "board")
    side.locator(".focus-back").click()
    expect(page.locator(".main-flow")).to_be_visible(timeout=15000)
    no_notice(page, "back to the main chat")


def screens_refused(page: Page) -> None:
    refuser, loads = opened(page, "once", SCREENS)
    walk(page)
    assert refuser.refused, "no screen's file was requested: nothing was tested"
    # Not every refused request was an import: a file preloaded ahead of the screen that needs it and
    # refused then is fetched afresh by the import itself. Those the imports asked for are asked again.
    again = sorted(n for n in refuser.refused if any(s.startswith(n + "?retry=") for s in refuser.served))
    assert again, f"refused {sorted(refuser.refused)}, and none was asked for again under a new address"
    assert len(loads) == 1, f"the page reloaded to recover a screen's own file: {loads}"
    print(f"  refused once and asked for again: {again}")


def everything_refused(page: Page) -> None:
    refuser, loads = opened(page, "once")
    walk(page)
    assert len(loads) <= 2, f"the page reloaded {len(loads) - 1} times: {loads}"
    print(f"  {len(refuser.refused)} files refused once; page loads: {len(loads)}")


def gone(page: Page) -> None:
    _, loads = opened(page, "gone")
    expect(page.get_by_text(STALE)).to_be_visible(timeout=20000)
    page.wait_for_timeout(1500)
    assert len(loads) == 1, f"a build that is gone reloaded the page: {loads}"


def never_arrives(page: Page) -> None:
    _, loads = opened(page, "always")
    expect(page.get_by_text(DROPPED)).to_be_visible(timeout=30000)
    page.wait_for_timeout(3000)
    assert len(loads) == 2, f"a download that never arrives should reload the page exactly once: {loads}"
    expect(page.get_by_text(STALE)).to_have_count(0)


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        cases = (
            ("a screen's own file refused once is asked for again", screens_refused),
            ("every file refused once: one reload and nothing lost", everything_refused),
            ("a build that is gone says so", gone),
            ("a download that never arrives reloads once and says so", never_arrives),
        )
        for name, run in cases:
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            run(context.new_page())
            context.close()
            print(f"{name}: ok")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
