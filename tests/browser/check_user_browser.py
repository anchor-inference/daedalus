"""Settings → Browser → "Your Chrome" and the browser panel's switch, driven in a real browser.

At 1440 × 900 and on a 390 px phone, in English and Russian:

- on this computer: a connected Chrome is named with its extension and Chrome versions and how it
  connects; "Check connection" asks the host and shows the round trip; a connector that is not
  registered offers Register, which asks the launcher through the host; the extension's folder and
  the copyable chrome://extensions are there;
- with no Chrome connected the card says why, in the daemon's words;
- on a server: "Pair a browser" shows a one-time code and the server's address, a paired browser is
  listed and revoked only after asking;
- the defaults (bodies off) and the always-allowed sites are saved as the host's `[browser.user]`;
- the extension popup's check (a `browser.user` event) is a toast;
- in a conversation the panel's header switches between the agent's browser and "Your Chrome ·
  Work", and the first switch asks with the consent summary (in the reader's language) before it
  is sent;
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
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_stub import BrowserStub, render_scenes  # noqa: E402
from check_settings_browser import BROWSER  # noqa: E402
from event_feed import EventFeed  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
CONSENT = "The agent will work in your own Chrome, signed in as you."
RELAY = {"connected": True, "profile_id": "p1", "profile_name": "Work", "ext_version": "0.1.0", "chrome_version": "141.0.7390.54", "platform": "linux",
         "transport": "native", "since": "2026-10-20T09:00:00Z", "last_seen": "2026-10-20T09:59:58Z"}
WORDS = {
    "en": {"connected": "Connected: Work", "check": "Check connection", "rtt": "14 ms", "register": "Register", "waiting": "Not connected", "pair": "Pair a browser",
           "agent": "Agent's browser", "chrome": "Your Chrome · Work", "toast": "Chrome «Work» checked the connection", "consent": "Every new site asks you first"},
    "ru": {"connected": "Подключён: Work", "check": "Проверить связь", "rtt": "14 мс", "register": "Зарегистрировать", "waiting": "Не подключён", "pair": "Подключить браузер",
           "agent": "Браузер агента", "chrome": "Ваш Chrome · Work", "toast": "Chrome «Work» проверил связь", "consent": "Каждый новый сайт сначала спрашивает вас"},
}


def status(**over: object) -> dict[str, object]:
    return {
        "configured": True, "enabled": True, "daemon": True, "reason": "", "detail": "", "relay": dict(RELAY), "native": True,
        "launcher": {"connector": {"registered": True}, "extension": {"path": "/data/browser-extension", "ready": True, "version": "0.1.0"}},
        "launcher_error": "", "devices": [], "server_url": "", "consent": CONSENT, **over,
    }


class Host:
    """The settings and the card's routes; the rest is the stubs'."""

    def __init__(self, bs: BrowserStub, card: dict[str, object]) -> None:
        self.bs = bs
        self.card = card
        self.settings = {"revision": "s1", "browser": {**copy.deepcopy(BROWSER), "user": {"enabled": True, "granted_sites": [], "monitor": True, "watch": True, "bodies": False}},
                         "presets": {"p": {}}, "providers": {}, "model": {"preset": "p", "chain": []}}
        self.calls: list[tuple[str, str, str]] = []

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]

        def answer(data: object, status_code: int = 200) -> None:
            route.fulfill(status=status_code, content_type="application/json", body=json.dumps(data))

        if rel == "/api/settings" and request.method == "GET":
            return answer(self.settings)
        if rel == "/api/settings/validate":
            return answer({"valid": True, "stale": False, "problems": []})
        if rel == "/api/settings" and request.method == "PUT":
            body = json.loads(request.post_data or "{}")
            self.settings["browser"] = body.get("browser", self.settings["browser"])
            return answer(self.settings)
        if rel.startswith("/api/browser/"):
            self.calls.append((request.method, rel, request.post_data or ""))
            if rel == "/api/browser/user":
                return answer(self.card)
            if rel == "/api/browser/user/check":
                return answer({"ok": True, "rtt_ms": 14, "host_ms": 15})
            if rel == "/api/browser/user/register":
                self.card["launcher"] = {**self.card["launcher"], "connector": {"registered": True, "restart_chrome": True}}  # type: ignore[dict-item]
                return answer({"launcher": self.card["launcher"], "error": ""})
            if rel == "/api/browser/user/pairing":
                return answer({"code": "ABCD-EFGH", "expires_at": "2026-10-20T10:10:00Z", "server_url": "https://daedalus.example"})
            if rel.startswith("/api/browser/user/devices/") and request.method == "DELETE":
                self.card["devices"] = []
                return answer({"ok": True})
            if rel.startswith("/api/browser/user/sessions/") and request.method == "PUT":
                body = json.loads(request.post_data or "{}")
                if body.get("env") == "user" and not (self.bs.user or {}).get("consented") and not body.get("consent"):
                    return answer({"detail": "consent", "code": "consent_needed", "consent": CONSENT}, 409)
                self.bs.user = {**(self.bs.user or {}), "env": body["env"], "consented": True}
                return answer({**self.bs.user, "sites": [], "offered": True, "consent": CONSENT})
        if rel.startswith("/api/browsers"):
            return self.bs.route(route)
        return stub(route)


def card_page(browser, host: Host, lang: str, width: int = 1440, height: int = 900, feed: EventFeed | None = None):  # type: ignore[no-untyped-def]
    page = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark").new_page()
    page.route("**/api/**", host.route)
    if feed is not None:
        page.route("**/api/events**", feed.route)
    page.goto(f"{BASE}/settings/environments?token=t&lang={lang}")
    page.wait_for_selector(".bs-chrome", timeout=15000)
    return page


def check(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{lang}] {text}")  # noqa: E731
    w = WORDS[lang]

    # This computer, a connected Chrome.
    bs = BrowserStub(scenes)
    host = Host(bs, status())
    feed = EventFeed()
    page = card_page(browser, host, lang, feed=feed)
    card = page.locator(".bs-chrome")
    text = card.inner_text()
    print(f"[{lang}] connected card: {text[:160]!r}")
    for word in (w["connected"], "141.0.7390.54", "0.1.0", "chrome://extensions", "/data/browser-extension", CONSENT):
        if word not in text:
            say(f"the connected card does not say {word!r}")
    card.get_by_role("button", name=w["check"]).click()
    page.wait_for_selector(".bs-chrome-rtt", timeout=5000)
    if w["rtt"] not in page.inner_text(".bs-chrome-rtt") or not any(p == "/api/browser/user/check" for _, p, _ in host.calls):
        say("Check connection did not show the round trip from the host")
    # The defaults are the host's [browser.user].
    page.locator(".bs-chrome [data-setting='browser.user.bodies'] [role=switch]").click()
    page.locator("#browser-user-sites").fill("example.com\nMail.Example.org\nexample.com")
    page.locator(".bs-chrome [data-setting='browser.user.bodies'] [role=switch]").focus()
    page.wait_for_timeout(900)
    saved = host.settings["browser"]["user"]
    print(f"[{lang}] saved user: {saved}")
    if not saved.get("bodies") or saved.get("granted_sites") != ["example.com", "mail.example.org"]:
        say(f"the defaults were saved as {saved}")
    # The extension popup's check is a toast here at the same moment.
    for _ in range(50):
        if feed.connected():
            break
        page.wait_for_timeout(100)
    feed.send("browser.user", {"kind": "check", "profile_name": "Work"})
    try:
        page.get_by_text(w["toast"]).wait_for(timeout=5000)
    except Exception:  # noqa: BLE001 — said as a problem, with the rest of the run going on
        say("the popup's check was not a toast")
    page.context.close()
    feed.close()

    # Not registered, and no Chrome: the reason, and Register goes to the launcher.
    host = Host(bs, status(relay={"connected": False, "last_error": "Chrome closed the connection"}, launcher={"connector": {"registered": False}, "extension": {"path": "/data/browser-extension", "ready": True}}))
    page = card_page(browser, host, lang)
    text = page.inner_text(".bs-chrome")
    if w["waiting"] not in text or "Chrome closed the connection" not in text:
        say(f"the card does not say why no Chrome is connected: {text[:200]!r}")
    page.get_by_role("button", name=w["register"]).click()
    page.wait_for_timeout(500)
    if not any(m == "POST" and p == "/api/browser/user/register" for m, p, _ in host.calls):
        say("Register did not ask the host")
    page.context.close()

    # A server: pairing and the paired browsers.
    device = {"id": "d1", "name": "Laptop", "profile_id": "p2", "ext_version": "0.1.0", "chrome_version": "141", "paired_at": "2026-10-01T00:00:00Z", "last_seen": "2026-10-19T00:00:00Z", "connected": False}
    host = Host(bs, status(native=False, launcher=None, relay={"connected": False}, devices=[device], server_url="https://daedalus.example"))
    page = card_page(browser, host, lang)
    if page.locator(".bs-chrome [data-step=connector]").count():
        say("a server shows the connector")
    page.get_by_role("button", name=w["pair"]).click()
    page.wait_for_selector(".bs-chrome-code", timeout=5000)
    code = page.inner_text(".bs-chrome-code")
    if "ABCD-EFGH" not in code or "https://daedalus.example" not in code:
        say(f"the pairing reads {code!r}")
    page.locator(".bs-chrome [data-device=d1] .btn.danger").click()
    page.wait_for_selector(".dialog", timeout=5000)
    if any(m == "DELETE" for m, _, _ in host.calls):
        say("Revoke did not ask first")
    page.locator(".dialog .btn.danger, .dialog .btn.primary").last.click()
    page.wait_for_timeout(500)
    if not any(m == "DELETE" and p == "/api/browser/user/devices/d1" for m, p, _ in host.calls):
        say("Revoke did not reach the host")
    page.context.close()

    # A phone: nothing scrolls sideways.
    host = Host(bs, status(native=False, launcher=None, devices=[device], server_url="https://daedalus.example"))
    page = card_page(browser, host, lang, 390, 844)
    page.locator(".bs-chrome").scroll_into_view_if_needed()
    wide = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    if wide > 1:
        say(f"the phone page scrolls sideways by {wide}px")
    page.context.close()

    # The conversation's switch, and the popup's check as a toast.
    bs = BrowserStub(scenes)
    bs.user = {"env": "agent", "consented": False, "connected": True, "name": "Work"}
    host = Host(bs, status())
    page = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark").new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}")
    tab = page.locator(".panel-tab[data-tab='browser']")
    tab.wait_for(timeout=15000)
    tab.click()
    page.wait_for_selector(".panel .bp-switch", timeout=10000)
    options = page.locator(".panel .bp-switch button").all_inner_texts()
    print(f"[{lang}] switch: {options}")
    if options != [w["agent"], w["chrome"]]:
        say(f"the switch reads {options}")
    page.locator(".panel .bp-switch button").nth(1).click()
    page.wait_for_selector(".dialog", timeout=5000)
    if w["consent"] not in page.inner_text(".dialog"):
        say("the first switch does not show the consent summary")
    if any(m == "PUT" for m, _, _ in host.calls):
        say("the switch was sent before the operator agreed")
    page.locator(".dialog .btn.primary, .dialog .btn.danger").last.click()
    page.wait_for_function("() => document.querySelector('.panel .bp-switch')?.dataset.env === 'user'", timeout=8000)
    puts = [json.loads(b) for m, p, b in host.calls if m == "PUT" and p == f"/api/browser/user/sessions/{S1}"]
    if puts != [{"env": "user", "consent": True}]:
        say(f"the switch sent {puts}")
    page.context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        scenes = render_scenes(browser)
        for lang in ("en", "ru"):
            check(browser, scenes, lang, problems)
        browser.close()
    for problem in problems:
        print("PROBLEM", problem)
    unhandled = UNHANDLED.report()
    if problems or unhandled:
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
