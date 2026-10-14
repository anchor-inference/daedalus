"""Every value of the key proxy is set from Settings, at a desktop width and on a phone.

The reported fault: an operator with a Keenable key had no field to put it in. The search backends
were keyed only by editing keyproxy.env by hand and restarting the stack. What is checked:

- Settings → Tools shows a key field for each search backend, and a key typed there is sent to
  ``PUT /api/keyproxy/KEYPROXY_KEY_KEENABLE``; the field is empty again afterwards and the row says
  the key is stored, without the key ever coming back;
- Settings → Models → Providers has the key proxy card with the model, search and own-upstream
  groups, an upstream of the operator's own can be added (address and key written), and the daily
  cap is written as a number;
- nothing on either page reaches past the screen on a phone.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_keyproxy.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, KEYPROXY_VIEW, expect_app, keyproxy_upstream, keyproxy_var  # noqa: E402
from screenshots import UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


class Proxy:
    """A key proxy that keeps what is written to it, and hands a key back only as "set"."""

    def __init__(self) -> None:
        self.view = copy.deepcopy(KEYPROXY_VIEW)
        self.writes: list[tuple[str, str]] = []

    def write(self, name: str, value: str) -> None:
        self.writes.append((name, value))
        if name == "KEYPROXY_USD_PER_DAY":
            self.view["budget"] = keyproxy_var(name, value, "0", written=True)  # type: ignore[index]
            return
        rows = self.view["upstreams"]  # type: ignore[index]
        if name.startswith("KEYPROXY_UPSTREAM_") and value:
            upstream = name[len("KEYPROXY_UPSTREAM_"):].lower()
            if not any(row["upstream"] == upstream for row in rows):
                rows.append(keyproxy_upstream(upstream, "extra", "", f"KEYPROXY_KEY_{upstream.upper()}", key_set=any(var == f"KEYPROXY_KEY_{upstream.upper()}" and written for var, written in self.writes)))
                rows[-1]["address"] = keyproxy_var(name, value, written=True)
            return
        for row in rows:
            if row["key"]["name"] == name:
                row["key"] = keyproxy_var(name, "set" if value else "", secret=True, written=True)

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("?", 1)[0]
        if request.method == "PUT":
            name = path.rsplit("/", 1)[1]
            self.write(name, json.loads(request.post_data or "{}").get("value", ""))
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(self.view))


OUTSIDE = """() => {
  const vw = document.documentElement.clientWidth;
  const out = [];
  for (const el of document.querySelectorAll('.settings-col *')) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    let clipped = false;
    for (let p = el.parentElement; p; p = p.parentElement) { if (getComputedStyle(p).overflowX !== 'visible') { clipped = true; break; } }
    if (!clipped && (r.left < -0.5 || r.right > vw + 0.5)) out.push((el.className || el.tagName) + ' ' + Math.round(r.left) + '..' + Math.round(r.right));
  }
  return out.slice(0, 5);
}"""


def run(width: int) -> list[str]:
    problems: list[str] = []
    phone = width < 600
    proxy = Proxy()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": width, "height": 900}, is_mobile=phone, has_touch=phone)
        page = context.new_page()
        page.set_default_timeout(8000)
        page.route("**/api/**", stub)
        page.route("**/api/keyproxy**", proxy.route)

        page.goto(f"{BASE}/settings/tools?token=t&lang=en")
        keys = page.locator('[data-setting="tools.web.search.keys"]')
        keys.scroll_into_view_if_needed()
        expect(keys).to_be_visible()
        field = keys.get_by_label("Keenable: Key")
        expect(field).to_have_attribute("placeholder", "No key — paste one")
        field.fill("kn-secret")
        field.press("Enter")
        expect(field).to_have_value("")
        expect(field).to_have_attribute("placeholder", "Key stored — type a new one to replace it")
        if ("KEYPROXY_KEY_KEENABLE", "kn-secret") not in proxy.writes:
            problems.append(f"{width}: the Keenable key was not written to the key proxy: {proxy.writes}")
        if "kn-secret" in page.content():
            problems.append(f"{width}: the key is still on the page after it was saved")
        if phone and (outside := page.evaluate(OUTSIDE)):
            problems.append(f"{width}: Tools reaches past the screen: {outside}")

        page.goto(f"{BASE}/settings/models?tab=providers&token=t&lang=en")
        card = page.locator('[data-setting="keyproxy.upstreams"]')
        card.scroll_into_view_if_needed()
        for title in ("Model providers", "Web search", "Your own upstreams"):
            expect(card.locator(".section-title", has_text=title)).to_be_visible()
        expect(card.locator('[data-upstream="keenable"] .pill')).to_have_text("key stored")
        card.locator('[data-upstream="tavily"] .mmain').click()
        expect(card.get_by_label("Tavily: Address")).to_have_attribute("placeholder", "https://api.tavily.com")

        add = card.locator('[data-setting="keyproxy.add"]')
        add.get_by_label("Name").fill("myllm")
        add.get_by_label("Address").fill("https://llm.example/v1")
        add.get_by_label("Key").fill("my-key")
        add.get_by_role("button", name="Add upstream").click()
        expect(card.locator('[data-upstream="myllm"]')).to_be_visible()
        if proxy.writes[-2:] != [("KEYPROXY_KEY_MYLLM", "my-key"), ("KEYPROXY_UPSTREAM_MYLLM", "https://llm.example/v1")]:
            problems.append(f"{width}: the new upstream was not written as key then address: {proxy.writes[-2:]}")

        cap = card.locator('[data-setting="keyproxy.usd_per_day"] input')
        cap.fill("7")
        cap.press("Enter")
        page.wait_for_timeout(200)
        if ("KEYPROXY_USD_PER_DAY", "7") not in proxy.writes:
            problems.append(f"{width}: the daily cap was not written: {proxy.writes}")
        if phone and (outside := page.evaluate(OUTSIDE)):
            problems.append(f"{width}: Models reaches past the screen: {outside}")
        context.close()
        browser.close()
    return problems


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    for width in (1440, 390):
        found = run(width)
        problems += found
        print(f"{width}: key proxy settings {'ok' if not found else 'FAILED'}")
    for problem in problems:
        print("FAIL", problem)
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
