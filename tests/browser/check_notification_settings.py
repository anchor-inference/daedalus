"""Settings → Notifications and the cap on running terminals, driven in a browser.

The host behind the page keeps its preferences and a revision the way the real one does: a save
carries the revision it was read at, and one read before another window saved is answered 409. On a
desktop:

- a cell of the matrix steps to "urgent only", and the PUT changes that one cell and nothing else;
- a save after another window's is refused, and the page reloads and shows the other window's work;
- muting a project for an hour sends an end about an hour away;
- the devices are listed, and removing one deletes it;
- the test button lists an outcome for every channel;
- the terminal cap shows the load bar with the numbers in words, turns bad and warns (without
  refusing) when typed past what the machine carries, and saves the new value.

On a phone the matrix is rows that open into one line per channel, a choice there sends its PUT, and
neither section scrolls sideways.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_notification_settings.py
"""

from __future__ import annotations

import copy
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import expect_app, notification_preferences, terminal_load  # noqa: E402
from screenshots import BASE, CHROMIUM, P2, PROJECTS, SETTINGS, UNHANDLED, stub  # noqa: E402

LANG = os.environ.get("LANG_UI", "en")


class Host:
    """The routes this section writes to, with state and a revision."""

    def __init__(self) -> None:
        self.view = notification_preferences()
        self.revision = 1
        self.puts: list[dict] = []
        self.settings = copy.deepcopy(SETTINGS) | {"revision": "s1"}
        self.settings_puts: list[dict] = []
        self.devices = [
            {"id": 1, "endpoint": "https://push.example.com/send/a", "device": "Chrome · Linux", "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36", "created_at": "2026-09-20T10:00:00Z", "last_ok_at": "2026-09-24T10:00:00Z", "failures": 0, "last_error": None, "apple": False},
            {"id": 2, "endpoint": "https://push.example.com/send/b", "device": "Firefox · Android", "user_agent": "Mozilla/5.0 (Android 15; Mobile; rv:140.0) Gecko/140.0 Firefox/140.0", "created_at": "2026-09-10T10:00:00Z", "last_ok_at": None, "failures": 4, "last_error": "503", "apple": False},
        ]
        self.deleted: list[int] = []

    def other_window_saves(self, **changes: object) -> None:
        self.view["preferences"].update(changes)  # type: ignore[union-attr]
        self.revision += 1

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]
        body = json.loads(request.post_data) if request.post_data else None

        def answer(data: object, status: int = 200) -> None:
            route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

        if rel == "/api/notifications/preferences":
            if request.method == "PUT":
                self.puts.append(body)
                if body.get("base_revision") != f"r{self.revision}":
                    return answer({"detail": {"message": "settings changed in another window", "current_revision": f"r{self.revision}"}}, 409)
                self.view["preferences"] = body["preferences"]
                self.revision += 1
            return answer({**self.view, "revision": f"r{self.revision}"})
        if rel == "/api/notifications/test":
            return answer({"delivered": {"in_app": "toast", "push": {"devices": [{"id": d["id"], "device": d["device"], "outcome": "sent"} for d in self.devices]}, "desktop": "skipped: no launcher", "telegram": "general"}})
        if rel == "/api/push/subscriptions" and request.method == "GET":
            return answer({"subscriptions": self.devices})
        if rel.startswith("/api/push/subscriptions/") and request.method == "DELETE":
            gone = int(rel.rsplit("/", 1)[-1])
            self.deleted.append(gone)
            self.devices = [d for d in self.devices if d["id"] != gone]
            return answer({"deleted": gone})
        if rel == "/api/terminals/load":
            return answer(terminal_load())
        if rel == "/api/projects":
            return answer(PROJECTS)
        if rel == "/api/settings" and request.method == "GET":
            return answer(self.settings)
        if rel == "/api/settings/validate":
            return answer({"valid": True, "stale": body["base_revision"] != self.settings["revision"], "problems": []})
        if rel == "/api/settings" and request.method == "PUT":
            self.settings_puts.append(body)
            self.settings["terminals"] = body.get("terminals", self.settings["terminals"])
            self.settings["revision"] = "s" + str(len(self.settings_puts) + 1)
            return answer(self.settings)
        return stub(route)


def changed(before: dict, after: dict, prefix: str = "") -> list[str]:
    """The dotted paths whose values differ, as the app's own diff reads them."""
    out: list[str] = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        path = f"{prefix}{key}"
        if isinstance(a, dict) and isinstance(b, dict):
            out += changed(a, b, path + ".")
        elif a != b:
            out.append(path)
    return out


def wait_for(page: Page, what, problems: list[str], name: str, timeout_ms: int = 5000) -> bool:  # type: ignore[no-untyped-def]
    for _ in range(timeout_ms // 100):
        if what():
            return True
        page.wait_for_timeout(100)
    problems.append(name)
    return False


def no_sideways_scroll(page: Page, problems: list[str], where: str) -> None:
    width = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth, Math.max(...[...document.querySelectorAll('.screen *')].map(e => e.getBoundingClientRect().right))]")
    if width[0] > width[1] or width[2] > width[1] + 0.5:
        problems.append(f"{where} scrolls sideways on a phone: {width}")


def desktop(playwright, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    host = Host()
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    page = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark").new_page()
    page.route("**/api/**", host.route)
    # The terminals' own bar: this check is about their cap, and the picture stub also reports a browser.
    page.route("**/api/workloads/load", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({"terminals": terminal_load(), "browsers": None, "together": None})))
    page.goto(f"{BASE}/settings/notifications?token=t&lang={LANG}")
    page.wait_for_selector(".nmatrix tbody tr", timeout=15000)

    rows = page.eval_on_selector_all(".nmatrix tbody tr", "rows => rows.map(r => r.dataset.category)")
    if len(rows) != 11:
        problems.append(f"the matrix has {len(rows)} rows, not the eleven kinds: {rows}")

    before = copy.deepcopy(host.view["preferences"])
    cell = page.locator('.nmatrix tr[data-category="run_finished"] td[data-channel="push"] .ncell')
    cell.click()
    if wait_for(page, lambda: len(host.puts) == 1, problems, "stepping a cell sent no PUT"):
        put = host.puts[0]
        diff = changed(before, put["preferences"])
        print("cell PUT:", diff, put["base_revision"])
        if diff != ["matrix.run_finished.push"] or put["preferences"]["matrix"]["run_finished"]["push"] != "urgent" or put["base_revision"] != "r1":
            problems.append(f"the cell's PUT changed {diff} with revision {put['base_revision']}")
    if cell.get_attribute("data-cell") != "urgent":
        problems.append(f"the cell reads {cell.get_attribute('data-cell')!r} after one click")

    # Another window saves quiet hours; this one, not knowing, steps a cell and is refused.
    host.other_window_saves(quiet_hours="22:00-06:00")
    page.locator('.nmatrix tr[data-category="question"] td[data-channel="telegram"] .ncell').click()
    if wait_for(page, lambda: len(host.puts) == 2, problems, "the stale save was not sent"):
        try:
            page.wait_for_selector('.quiet-hours input[type="time"]', timeout=5000)
            shown = page.eval_on_selector_all('.quiet-hours input[type="time"]', "els => els.map(e => e.value)")
            if shown != ["22:00", "06:00"]:
                problems.append(f"after the refused save the page shows quiet hours {shown}")
        except Exception:  # noqa: BLE001 — a report, not a stop
            problems.append("after a 409 the page did not reload the other window's quiet hours")
        telegram = page.get_attribute('.nmatrix tr[data-category="question"] td[data-channel="telegram"] .ncell', "data-cell")
        if telegram != "on":
            problems.append(f"the refused change is still shown as saved ({telegram!r})")
        toast = page.locator(".toast").all_inner_texts() if page.query_selector(".toast") else []
        print("after 409:", toast)
        if not toast:
            problems.append("a refused save said nothing")

    # Mute the second project for an hour.
    count = len(host.puts)
    page.locator(f'.nmute[data-project="{P2}"] .dropdown-btn').click()
    page.locator('.dropdown-item[data-value="hour"]').click()
    if wait_for(page, lambda: len(host.puts) == count + 1, problems, "muting sent no PUT"):
        until = host.puts[-1]["preferences"]["muted_projects"].get(P2)
        minutes = (datetime.fromisoformat(until.replace("Z", "+00:00")) - datetime.now(UTC)).total_seconds() / 60 if until else 0
        print("mute until:", until, round(minutes))
        if not 55 <= minutes <= 61:
            problems.append(f"an hour's mute ends {until!r}")
        page.wait_for_selector(f'.nmute[data-project="{P2}"][data-muted="true"]', timeout=5000)

    # The devices, and removing one.
    names = page.eval_on_selector_all(".ndevice .mtitle", "els => els.map(e => e.textContent)")
    if names != ["Chrome · Linux", "Firefox · Android"]:
        problems.append(f"the devices read {names}")
    page.locator('.ndevice[data-device="2"] .btn').click()
    page.locator(".dialog-actions .btn.danger").click()
    wait_for(page, lambda: host.deleted == [2], problems, "removing a device sent no DELETE")
    try:
        page.wait_for_function("document.querySelectorAll('.ndevice').length === 1", timeout=5000)
    except Exception:  # noqa: BLE001
        problems.append("the removed device stayed in the list")

    page.locator(".btn", has_text="Send a test notification" if LANG == "en" else "Отправить проверочное уведомление").click()
    page.wait_for_selector(".push-test-result li", timeout=5000)
    lines = page.eval_on_selector_all(".push-test-result li", "els => els.map(e => e.textContent)")
    print("test:", lines)
    if LANG == "en" and lines != ["In app: shown here", "Push: Chrome · Linux, sent", "Desktop: no desktop launcher is listening", "Telegram: sent"]:
        problems.append(f"the test lists {lines}")

    # The terminal cap and its load bar.
    page.goto(f"{BASE}/settings/terminals?token=t&lang={LANG}")
    page.wait_for_selector(".loadbar-track", timeout=15000)
    head = page.inner_text(".loadbar-head b")
    level = page.get_attribute(".loadbar", "data-level")
    print("bar:", head, level)
    if LANG == "en" and head != "20 sessions ≈ 14 GB of 62 GB":
        problems.append(f"the bar says {head!r}")
    if level != "ok" or page.query_selector(".loadbar-warning"):
        problems.append(f"at 20 the bar is {level!r} with a warning={bool(page.query_selector('.loadbar-warning'))}")
    text = page.inner_text(".loadbar")
    if LANG == "en" and ("45 MB of the terminal daemon" not in text or "Processors:" not in text):
        problems.append(f"the bar's words leave out the daemon or the processors: {text!r}")
    field = page.locator("#terminal-cap")
    field.fill("80")
    page.wait_for_timeout(200)
    level = page.get_attribute(".loadbar", "data-level")
    warning = page.inner_text(".loadbar-warning") if page.query_selector(".loadbar-warning") else ""
    print("at 80:", level, warning)
    if level != "bad" or not warning:
        problems.append(f"at 80 the bar is {level!r} and warns {warning!r}")
    field.press("Enter")
    if wait_for(page, lambda: len(host.settings_puts) == 1, problems, "the cap was not saved"):
        if host.settings_puts[0].get("terminals", {}).get("running_cap") != 80:
            problems.append(f"the cap was saved as {host.settings_puts[0]}")
    field.fill("0")
    page.wait_for_timeout(100)
    if not page.query_selector("#terminal-cap[aria-invalid=true]"):
        problems.append("a cap of 0 is not marked invalid")
    field.press("Enter")
    page.wait_for_timeout(300)
    if len(host.settings_puts) != 1:
        problems.append("a cap of 0 was sent")
    browser.close()


def phone(playwright, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    host = Host()
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    page = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark").new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/settings/notifications?token=t&lang={LANG}")
    page.wait_for_selector(".nrows .nrow", timeout=15000)
    if page.query_selector(".nmatrix"):
        problems.append("the phone draws the desktop table")
    no_sideways_scroll(page, problems, "Notifications")
    page.locator('.nrow[data-category="run_finished"] .nrow-head').click()
    lines = page.eval_on_selector_all('.nrow.open .nrow-line', "els => els.map(e => e.dataset.channel)")
    if lines != ["in_app", "push", "desktop", "telegram"]:
        problems.append(f"an opened row shows {lines}")
    heights = page.eval_on_selector_all('.nrow.open .nrow-line .segmented button', "els => els.map(e => e.getBoundingClientRect().height)")
    if not heights or min(heights) < 32:
        problems.append(f"the phone's choices are {heights} px tall, under the touch size")
    no_sideways_scroll(page, problems, "an opened row")
    page.locator('.nrow.open .nrow-line[data-channel="push"] .segmented button').nth(2).click()
    if wait_for(page, lambda: len(host.puts) == 1, problems, "a choice on the phone sent no PUT"):
        diff = changed(notification_preferences()["preferences"], host.puts[0]["preferences"])  # type: ignore[arg-type]
        if diff != ["matrix.run_finished.push"] or host.puts[0]["preferences"]["matrix"]["run_finished"]["push"] != "off":
            problems.append(f"the phone's PUT changed {diff}")
    page.goto(f"{BASE}/settings/terminals?token=t&lang={LANG}")
    page.wait_for_selector(".loadbar-track", timeout=15000)
    no_sideways_scroll(page, problems, "Terminals")
    browser.close()


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as playwright:
        desktop(playwright, problems)
        phone(playwright, problems)
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
