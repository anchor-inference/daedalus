"""Push on this device, as Settings and the phone's notification list show it.

Four browsers open the app against the screenshot stub, each shaped like a real one:

- desktop Chromium with notifications allowed: Settings says push is off, "Turn on push" subscribes
  with the host's key and posts the subscription, the card then says it is on, the test button
  lists the device, and "Turn off push" deletes the device and unsubscribes;
- Safari on an iPhone, not added to the Home Screen: the card gives the three steps instead of a
  button, and the list shows no nudge;
- the Telegram Mini App: the card says the bot is the push, and nothing asks the host about push;
- Chrome on an Android phone: the list opens with the one-line nudge, and the nudge leads to the card.

A headless browser has no push service to subscribe with, so the page's ``pushManager`` is a fake
installed before the app loads; everything above it — the permission, the key, the requests — is
the app's own.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_push.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import expect_app  # noqa: E402
from screenshots import BASE, CHROMIUM, UNHANDLED, stub  # noqa: E402

# RFC 8291's example keys: any valid P-256 point will do for a key nobody decrypts with.
PUBLIC_KEY = "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
ENDPOINT = "https://push.example.com/send/check-push"

FAKE_PUSH = """
(() => {
  let current = null;
  const subscription = {
    endpoint: ENDPOINT_JSON,
    options: { applicationServerKey: null },
    toJSON() { return { endpoint: this.endpoint, expirationTime: null, keys: { p256dh: KEY_JSON, auth: "BTBZMqHH6r4Tts7J_aSIgg" } }; },
    unsubscribe: async () => { current = null; window.__unsubscribed = true; return true; },
  };
  const registration = { pushManager: {
    getSubscription: async () => current,
    subscribe: async (options) => { window.__subscribedWith = new Uint8Array(options.applicationServerKey).length; current = subscription; return subscription; },
  } };
  if (navigator.serviceWorker) {
    Object.defineProperty(navigator.serviceWorker, "getRegistration", { value: async () => registration });
    Object.defineProperty(navigator.serviceWorker, "ready", { get: () => Promise.resolve(registration) });
  }
})();
""".replace("ENDPOINT_JSON", json.dumps(ENDPOINT)).replace("KEY_JSON", json.dumps(PUBLIC_KEY))

TELEGRAM = """
window.Telegram = { WebApp: {
  initData: "query_id=check&user=%7B%22id%22%3A1%7D&hash=x", initDataUnsafe: { user: { language_code: "en" } },
  themeParams: {}, colorScheme: "dark", platform: "android", isExpanded: true,
  ready() {}, expand() {}, onEvent() {}, offEvent() {}, setHeaderColor() {}, setBackgroundColor() {},
  disableVerticalSwipes() {}, enableVerticalSwipes() {}, enableClosingConfirmation() {}, disableClosingConfirmation() {},
  isVersionAtLeast() { return true; },
  BackButton: { show() {}, hide() {}, onClick() {}, offClick() {} },
  HapticFeedback: { impactOccurred() {}, notificationOccurred() {} },
} };
"""

IPHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
ANDROID_UA = "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"


class PushHost:
    """The host's side of push, as far as the app can ask about it."""

    def __init__(self) -> None:
        self.devices: list[dict] = []
        self.requests: list[tuple[str, str, dict | None]] = []

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]
        if rel.startswith("/api/push/") or rel == "/api/notifications/test":
            body = json.loads(request.post_data) if request.post_data else None
            self.requests.append((request.method, rel, body))
            answer: object
            if rel == "/api/push/config":
                answer = {"available": True, "reason": "", "public_key": PUBLIC_KEY}
            elif rel == "/api/push/subscriptions" and request.method == "GET":
                answer = {"subscriptions": self.devices}
            elif rel == "/api/push/subscriptions" and request.method == "POST":
                device = {"id": len(self.requests), "endpoint": body["endpoint"], "device": body.get("device", ""), "user_agent": "", "created_at": "", "last_ok_at": None, "failures": 0, "last_error": None, "apple": False}
                self.devices = [d for d in self.devices if d["endpoint"] != body["endpoint"]] + [device]
                answer = {"subscription": device}
            elif rel.startswith("/api/push/subscriptions/") and request.method == "DELETE":
                gone = int(rel.rsplit("/", 1)[-1])
                self.devices = [d for d in self.devices if d["id"] != gone]
                answer = {"deleted": gone}
            elif rel == "/api/notifications/test":
                answer = {"delivered": {"in_app": "toast", "push": {"devices": [{"id": d["id"], "device": d["device"], "outcome": "sent"} for d in self.devices]}}}
            else:
                return route.fulfill(status=404, content_type="application/json", body='{"detail": "Not Found"}')
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(answer))
        return stub(route)


SHOTS = os.environ.get("PUSH_SHOTS", "")
"""A directory for pictures of each state; none are taken without it, so a run never dirties the tree."""


def shot(page: Page, name: str) -> None:
    if SHOTS:
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def card_state(page: Page, wanted: str, problems: list[str], what: str) -> bool:
    try:
        page.wait_for_selector(f'.push-card[data-push-state="{wanted}"]', timeout=10000)
        return True
    except Exception:  # noqa: BLE001 — the harness reports, it does not stop at the first thing wrong
        found = page.eval_on_selector(".push-card", "e => e.dataset.pushState") if page.query_selector(".push-card") else "no card"
        problems.append(f"{what}: the card says {found!r}, not {wanted!r}")
        return False


def desktop(playwright, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    host = PushHost()
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.grant_permissions(["notifications"], origin=BASE.split("/app", 1)[0])
    context.add_init_script(FAKE_PUSH)
    page = context.new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/settings/notifications?token=t&lang=en")
    if card_state(page, "off", problems, "desktop before"):
        page.get_by_role("button", name="Turn on push").click()
        if card_state(page, "on", problems, "desktop after turning on"):
            posted = [b for m, r, b in host.requests if m == "POST" and r == "/api/push/subscriptions"]
            if len(posted) != 1 or posted[0]["endpoint"] != ENDPOINT or posted[0]["keys"]["auth"] != "BTBZMqHH6r4Tts7J_aSIgg" or not posted[0]["device"]:
                problems.append(f"the subscription posted was {posted}")
            if page.evaluate("window.__subscribedWith") != 65:
                problems.append("the browser was not given the host's 65-byte key")
            print("subscribed:", posted[0] if posted else None)
            page.get_by_role("button", name="Send a test notification").click()
            page.wait_for_selector(".push-test-result li", timeout=5000)
            lines = page.eval_on_selector_all(".push-test-result li", "els => els.map(e => e.textContent)")
            print("test:", lines)
            # One line per channel; the push line names the device that was just subscribed.
            if lines != ["In app: shown here", f"Push: {posted[0]['device']}, sent"]:
                problems.append(f"the test result reads {lines}")
            page.get_by_role("button", name="Turn off push").click()
            card_state(page, "off", problems, "desktop after turning off")
            if host.devices or not page.evaluate("window.__unsubscribed === true"):
                problems.append(f"turning off left devices {host.devices} or the browser subscribed")
    shot(page, "push-desktop")
    browser.close()


def iphone(playwright, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    host = PushHost()
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    context = browser.new_context(viewport={"width": 390, "height": 844}, user_agent=IPHONE_UA, is_mobile=True, has_touch=True, color_scheme="dark")
    page = context.new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/settings/notifications?token=t&lang=en")
    if card_state(page, "needs-home-screen", problems, "iPhone in Safari"):
        steps = page.eval_on_selector_all(".push-steps li", "els => els.length")
        if steps != 3 or page.query_selector(".push-card button"):
            problems.append(f"the iPhone card shows {steps} steps and a button={bool(page.query_selector('.push-card button'))}")
    shot(page, "push-iphone")
    page.goto(f"{BASE}/inbox?token=t&lang=en")
    page.wait_for_selector(".ph-inbox .ph-page-body", timeout=10000)
    page.wait_for_timeout(500)
    if page.query_selector(".push-nudge"):
        problems.append("the iPhone list nudges to turn on push, which it cannot do there")
    browser.close()


def telegram(playwright, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    host = PushHost()
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    context = browser.new_context(viewport={"width": 390, "height": 844}, user_agent=ANDROID_UA, is_mobile=True, has_touch=True, color_scheme="dark")
    context.add_init_script(TELEGRAM)
    context.route("https://telegram.org/**", lambda route: route.abort())
    page = context.new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/settings/notifications?lang=en")
    card_state(page, "telegram", problems, "the Telegram Mini App")
    asked = [r for _, r, _ in host.requests if r.startswith("/api/push/")]
    if asked:
        problems.append(f"inside Telegram the app still asked the host about push: {asked}")
    shot(page, "push-telegram")
    browser.close()


def android(playwright, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    host = PushHost()
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    context = browser.new_context(viewport={"width": 390, "height": 844}, user_agent=ANDROID_UA, is_mobile=True, has_touch=True, color_scheme="dark")
    context.add_init_script(FAKE_PUSH)
    page = context.new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/inbox?token=t&lang=en")
    try:
        page.wait_for_selector(".push-nudge", timeout=10000)
    except Exception:  # noqa: BLE001
        problems.append("the phone's list shows no nudge while push is off")
        browser.close()
        return
    box = page.eval_on_selector(".push-nudge", "e => { const r = e.getBoundingClientRect(); return [r.left, r.right, document.documentElement.scrollWidth]; }")
    if box[1] > 390 or box[2] > 390:
        problems.append(f"the nudge overflows the phone: {box}")
    shot(page, "push-android-inbox")
    page.click(".push-nudge")
    card_state(page, "off", problems, "the card the nudge leads to")
    browser.close()


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as playwright:
        desktop(playwright, problems)
        iphone(playwright, problems)
        telegram(playwright, problems)
        android(playwright, problems)
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
