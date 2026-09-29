"""Settings → Environments, the browser half, driven in a real browser.

At 1440 × 900 and on a 390 px phone, in English and Russian:

- the environment says which Chromium runs and that its sandbox holds;
- the running browser is listed with its owner and its memory, and Close asks first, then closes it;
- the cap is saved on blur, and the load bar follows it as it is typed: terminals and browsers on one
  track, the browsers' part named in words, and a cap the machine cannot carry beside the terminals
  turns the bar bad and warns — without refusing the value;
- a profile is cleared and deleted only after asking, and never while its browser runs;
- the recording's default, watch mode and its sites, and the local network's addresses are saved as
  the host's `[browser]` section;
- the old /settings/browser address lands on Environments;
- nothing on the page scrolls sideways on a phone.

Exit 0 when every step holds.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app, terminal_load  # noqa: E402
from browser_stub import BrowserStub, browser_load, render_scenes  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LANG = os.environ.get("LANG_UI", "")
MIB = 1 << 20

BROWSER = {
    "env": "auto", "running_cap": 2, "idle_close_minutes": 10, "agent_wait_seconds": 60, "control_wait_seconds": 20, "ticket_ttl_seconds": 30,
    "audit_retention_days": 90, "closed_retention_hours": 72, "lan_allow": [], "rules": [], "record_frames": False, "record_takeover": False,
    "record_retention_days": 7, "record_max_mb": 500, "watch_mode": False, "watch_domains": ["mail.google.com"], "injection_monitor": False, "injection_monitor_preset": "",
}


class Host:
    """The settings the page reads and writes, and the workloads it draws; the rest is the stubs'."""

    def __init__(self, bs: BrowserStub) -> None:
        self.bs = bs
        self.settings = {"revision": "s1", "browser": copy.deepcopy(BROWSER), "presets": {"p": {}, "q": {}}, "providers": {}, "model": {"preset": "p", "chain": []}}
        self.puts: list[dict] = []

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]

        def answer(data: object, status: int = 200) -> None:
            route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

        if rel == "/api/settings" and request.method == "GET":
            return answer(self.settings)
        if rel == "/api/settings/validate":
            body = json.loads(request.post_data or "{}")
            return answer({"valid": True, "stale": body.get("base_revision") != self.settings["revision"], "problems": []})
        if rel == "/api/settings" and request.method == "PUT":
            body = json.loads(request.post_data or "{}")
            self.puts.append(body)
            self.settings["browser"] = body.get("browser", self.settings["browser"])
            self.settings["revision"] = f"s{len(self.puts) + 1}"
            return answer(self.settings)
        if rel == "/api/workloads/load":
            return answer({"terminals": terminal_load(), "browsers": browser_load(running=len(self.bs.running)), "together": None})
        if rel.startswith("/api/browsers"):
            return self.bs.route(route)
        return stub(route)


def say_into(problems: list[str], lang: str):  # type: ignore[no-untyped-def]
    return lambda text: problems.append(f"[{lang}] {text}")


def prepared(scenes) -> BrowserStub:  # type: ignore[no-untyped-def]
    bs = BrowserStub(scenes)
    bs.add("s-a1", owner_id=S1, owner_label="Bakery site")
    bs.running = [{"env": "container", "id": "b1a2b3c4", "profile": "project-bakery", "started_at": "2026-09-26T10:00:00Z", "rss_bytes": 312 * MIB, "cpu_percent": 3.2, "tabs": 2,
                   "memory_basis": "cgroup", "groups": [{"id": "s-a1", "owner": {"kind": "session", "id": S1, "label": "Bakery site"}, "url": "https://shop.example.com/", "title": "Shop"}]}]
    bs.profiles = [
        {"id": "project-bakery", "env": "container", "scope": "project", "project_id": "p1", "session_id": None, "staff_id": None, "created_at": "2026-09-20T10:00:00Z", "last_used_at": "2026-09-26T10:00:00Z", "size_bytes": 18 * MIB, "running": True},
        {"id": "session-old", "env": "container", "scope": "session", "project_id": None, "session_id": "old", "staff_id": None, "created_at": "2026-09-01T10:00:00Z", "last_used_at": "2026-09-02T10:00:00Z", "size_bytes": 4 * MIB, "running": False},
    ]
    return bs


def desktop(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = say_into(problems, lang)
    bs = prepared(scenes)
    host = Host(bs)
    page = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark").new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/settings/environments?token=t&lang={lang}")
    page.wait_for_selector(".bs-envs .bs-env", timeout=15000)

    env = page.inner_text(".bs-envs")
    print(f"[{lang}] env: {env!r}")
    if "151.0.7922.34" not in env or "src-4f1c2a9e0b7d" not in env:
        say(f"the environment does not name its daemon and Chromium: {env!r}")

    row = page.locator(".bs-running .bs-row[data-browser='b1a2b3c4']")
    row.wait_for(timeout=5000)
    text = row.inner_text()
    if "Bakery site" not in text or "312" not in text:
        say(f"the running browser reads {text!r}")
    row.locator(".btn.danger").click()
    page.wait_for_selector(".dialog", timeout=5000)
    page.locator(".dialog .btn.danger, .dialog .btn.primary").last.click()
    page.wait_for_selector(".bs-running .bs-row", state="detached", timeout=5000)
    if not any(p.endswith("/running/container/b1a2b3c4/close") for m, p, _ in bs.requests if m == "POST"):
        say("Close did not ask the host to close that browser")

    # The cap: the one bar at the top of the page follows the draft, both kinds on one track.
    bar = page.locator(".env-load .loadbar.workloads")
    bar.wait_for(timeout=5000)
    kinds = page.locator(".env-load .loadbar-kind").all_inner_texts()
    print(f"[{lang}] kinds: {kinds}; level {bar.get_attribute('data-level')}")
    if len(kinds) != 2:
        say(f"the bar does not name both kinds: {kinds}")
    if bar.get_attribute("data-level") != "ok":
        say(f"at a cap of 2 the bar is {bar.get_attribute('data-level')}")
    cap = page.locator("#browser-cap")
    cap.fill("32")
    page.wait_for_timeout(200)
    level = bar.get_attribute("data-level")
    browsers_width = page.evaluate("() => parseFloat(document.querySelector('.env-load .loadbar-browsers-extra').style.width)")
    print(f"[{lang}] at 32: {level}, browsers' extra {browsers_width}%")
    if level not in ("warn", "bad") or browsers_width < 10:
        say(f"at a cap of 32 the bar is {level} with the browsers' part at {browsers_width}%")
    cap.press("Enter")
    page.wait_for_timeout(800)
    if not host.puts or host.puts[-1].get("browser", {}).get("running_cap") != 32:
        say(f"the cap was saved as {host.puts[-1:]}")
    cap.fill("0")
    page.wait_for_timeout(100)
    if not page.query_selector("#browser-cap[aria-invalid=true]"):
        say("a cap of 0 is not marked invalid")
    puts = len(host.puts)
    cap.press("Enter")
    page.wait_for_timeout(800)
    if len(host.puts) != puts:
        say("a cap of 0 was sent")

    # Profiles: the running one cannot be touched; the other is cleared and deleted after asking.
    busy = page.locator(".bs-profiles .bs-row[data-profile='project-bakery'] button")
    if not all(busy.nth(i).is_disabled() for i in range(busy.count())):
        say("a running profile's buttons are enabled")
    old = page.locator(".bs-profiles .bs-row[data-profile='session-old']")
    old.locator("button").first.click()
    page.wait_for_selector(".dialog", timeout=5000)
    page.locator(".dialog .btn").last.click()
    page.wait_for_timeout(300)
    old.locator("button.danger").click()
    page.wait_for_selector(".dialog", timeout=5000)
    page.locator(".dialog .btn").last.click()
    page.wait_for_selector(".bs-profiles .bs-row[data-profile='session-old']", state="detached", timeout=5000)
    calls = [(m, p) for m, p, _ in bs.requests if "/profiles/" in p]
    print(f"[{lang}] profile calls: {calls}")
    if ("POST", "/api/browsers/profiles/container/session-old/clear") not in calls or ("DELETE", "/api/browsers/profiles/container/session-old") not in calls:
        say(f"the profile was not cleared and deleted: {calls}")

    # The rest of the section is the host's [browser].
    page.locator(".bs-recording [role=switch]").first.click()
    page.locator(".bs-watch [role=switch]").first.click()
    page.locator("#browser-watch").fill("mail.google.com\n*.bank.example\n\nmail.google.com")
    page.locator("#browser-lan").fill("10.0.5.20, 10.0.5.0/24")
    page.locator("#browser-cap").focus()
    page.wait_for_timeout(900)
    saved = host.settings["browser"]
    print(f"[{lang}] saved: record {saved['record_frames']}, watch {saved['watch_mode']} {saved['watch_domains']}, lan {saved['lan_allow']}")
    if not saved["record_frames"] or not saved["watch_mode"]:
        say("the recording's default or watch mode was not saved")
    if saved["watch_domains"] != ["mail.google.com", "*.bank.example"] or saved["lan_allow"] != ["10.0.5.20", "10.0.5.0/24"]:
        say(f"the lists were saved as {saved['watch_domains']} and {saved['lan_allow']}")
    page.context.close()


def phone(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = say_into(problems, lang)
    bs = prepared(scenes)
    host = Host(bs)
    page = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark").new_page()
    page.route("**/api/**", host.route)
    # The address Browser had as a page of its own: kept working, it lands on the merged page.
    page.goto(f"{BASE}/settings/browser?token=t&lang={lang}")
    page.wait_for_selector(".bs-running .bs-row", timeout=15000)
    if "/settings/environments" not in page.url:
        say(f"the old address stayed at {page.url}")
    width = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth, Math.max(...[...document.querySelectorAll('.bs *')].map(e => e.getBoundingClientRect().right))]")
    print(f"[{lang}] phone widths: {width}")
    if width[0] > width[1] or width[2] > width[1] + 0.5:
        say(f"Settings → Browser scrolls sideways on a phone: {width}")
    page.context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        for lang in ([LANG] if LANG else ["en", "ru"]):
            desktop(browser, scenes, lang, problems)
            phone(browser, scenes, lang, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
