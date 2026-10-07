"""Does "Add a model" record the model the operator ended on, and nothing of the one before it?

Step 2 offers a list and a text field. Picking from the list fills step 3 in from the catalogue —
the price, the label, the window, whether the model sees pictures — and typing an id afterwards is
how an operator reaches a model the endpoint did not list. Nothing on the screen says which of the
two the form now describes, so the check is that the requests say it: pick an expensive model with
a very large window, type a cheap small one, save, and read what went to the API.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_add_model.py

Then OpenCode, which is sold two ways: the Go card says it is a subscription and asks for no price,
and Zen, which this installation has no endpoint for yet, is offered as a card that adds one on its
key proxy route with ``billing = "metered"``.

Exit 0 when the picked model's description does not reach the typed model and both OpenCode plans
are offered with what they cost.
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
from screenshots import FRESH, SETTINGS, stub  # noqa: E402  the same invented installation the pictures are taken of

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

GO_HINT = "OpenCode Go — subscription, not charged per token"
ZEN_HINT = "OpenCode Zen — pay per token through OpenCode's gateway"
PICKED = "Claude Opus 5"  # $5.00/$25.00 in, a 1M window, sees pictures
TYPED = "z-ai/glm-5.3-flash"  # $0.15/$0.50, 200k, text only


def settings_tab(browser, failures: list[str]) -> None:  # type: ignore[no-untyped-def]
    """Settings -> Models -> Providers: the Go row says how it is paid for, and Zen is one click away."""
    settings = copy.deepcopy(SETTINGS)
    settings["keyproxy_base"] = "http://keyproxy:3200"
    settings["providers"] = {"opencode": {"kind": "opencode", "name": "OpenCode Go", "base_url": "http://keyproxy:3200/opencode", "billing": "subscription", "timeout_seconds": 900}}
    added: list[tuple[str, dict]] = []

    def host(route):  # type: ignore[no-untyped-def]
        request = route.request
        rel = "/api/" + request.url.split("?", 1)[0].split("/api/", 1)[-1]
        if rel == "/api/settings" and request.method == "GET":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))
        if rel.startswith("/api/providers/") and request.method == "PUT":
            added.append((rel, request.post_data_json))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))
        return stub(route)

    for width in (1440, 390):
        context = browser.new_context(viewport={"width": width, "height": 900})
        page = context.new_page()
        page.route("**/api/**", host)
        page.goto(f"{BASE}/settings/models?token=t")
        page.get_by_role("tab", name="Providers").click()
        row = page.locator(".mrow", has_text="OpenCode Go").first
        if GO_HINT not in row.inner_text():
            failures.append(f"{width}: the OpenCode Go row does not say it is a subscription")
        add = page.locator(".opencode-add button", has_text="OpenCode Zen")
        if add.count() != 1 or ZEN_HINT not in add.first.inner_text():
            failures.append(f"{width}: Settings does not offer OpenCode Zen with its line about paying per token")
        else:
            add.first.click()
            page.wait_for_timeout(300)
        if not page.evaluate("document.documentElement.scrollWidth <= innerWidth"):
            failures.append(f"{width}: the providers tab scrolls sideways")
        context.close()
    expected = ("/api/providers/opencode_zen", {"kind": "opencode", "name": "OpenCode Zen", "base_url": "http://keyproxy:3200/opencode_zen", "billing": "metered"})
    if added != [expected, expected]:
        failures.append(f"Settings did not add Zen on its route with its billing: {added}")


def run() -> int:
    sent: list[tuple[str, dict]] = []
    declared: list[tuple[str, dict]] = []
    opencode: list[tuple[str, dict]] = []
    failures: list[str] = []
    stub.fresh = True  # type: ignore[attr-defined]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.route("**/api/**", stub)
        page.on("request", lambda r: sent.append((r.url.split("/api/", 1)[1], json.loads(r.post_data or "{}"))) if r.method == "PUT" else None)
        page.goto(f"{BASE}/agents?token=t")
        page.wait_for_selector(".addmodel", timeout=15000)
        page.locator(".pickgrid .pick", has_text="OpenRouter").first.click()
        page.wait_for_selector(".modelgrid .pick", timeout=15000)
        page.locator(".modelgrid .pick", has_text=PICKED).first.click()
        page.wait_for_timeout(200)
        page.locator(".addmodel-pricing summary").click()
        page.get_by_label("Provider-enforced input ceiling (tokens)").fill("1048576")
        page.get_by_label("Source for that ceiling").fill("Provider contract")
        page.locator("input.field.mono").fill(TYPED)
        page.wait_for_timeout(200)
        page.locator(".addmodel-foot .btn.primary").click()
        page.wait_for_timeout(600)
        second = context.new_page()
        second.route("**/api/**", stub)
        second.on("request", lambda r: declared.append((r.url.split("/api/", 1)[1], json.loads(r.post_data or "{}"))) if r.method == "PUT" else None)
        second.goto(f"{BASE}/agents?token=t")
        second.wait_for_selector(".addmodel", timeout=15000)
        second.locator(".pickgrid .pick", has_text="OpenRouter").first.click()
        second.wait_for_selector(".modelgrid .pick", timeout=15000)
        second.locator(".modelgrid .pick", has_text=PICKED).first.click()
        second.locator(".addmodel-pricing summary").click()
        second.get_by_label("Provider-enforced input ceiling (tokens)").fill("1048576")
        second.get_by_label("Source for that ceiling").fill("Provider contract")
        second.locator(".addmodel-foot .btn.primary").click()
        second.wait_for_timeout(600)
        third = context.new_page()
        third.route("**/api/**", stub)
        # Go with its key in the proxy, so its card can be picked; the rest is the installation above.
        ready_go = {**FRESH, "providers": [{**p, "key_held": True, "ready": True} if p["id"] == "opencode" else p for p in FRESH["providers"]]}
        third.route("**/api/onboarding", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(ready_go)))
        third.on("request", lambda r: opencode.append((r.url.split("/api/", 1)[1], json.loads(r.post_data or "{}"))) if r.method == "PUT" else None)
        third.goto(f"{BASE}/agents?token=t")
        third.wait_for_selector(".addmodel", timeout=15000)
        go = third.locator(".pickgrid .pick", has_text="OpenCode Go")
        if go.count() != 1 or GO_HINT not in go.first.inner_text():
            failures.append(f"the OpenCode Go card does not say it is a subscription: {go.all_inner_texts()}")
        zen = third.locator(".pickgrid .pick.dashed", has_text="OpenCode Zen")
        if zen.count() != 1 or ZEN_HINT not in zen.first.inner_text():
            failures.append(f"OpenCode Zen is not offered to add with its line about paying per token: {zen.all_inner_texts()}")
        else:
            zen.first.click()
            third.wait_for_timeout(200)
            if "http://keyproxy:3200/opencode_zen" not in third.locator(".addmodel .step").first.inner_text():
                failures.append("adding Zen does not show its key proxy route")
            if third.locator('a[href="https://opencode.ai/auth"]').count() < 1:
                failures.append("adding Zen does not link to where an OpenCode key is made")
            third.get_by_role("button", name="Add OpenCode Zen").click()
            third.wait_for_timeout(400)
        third.locator(".pickgrid .pick", has_text="OpenCode Go").first.click()
        third.wait_for_selector(".modelgrid .pick", timeout=15000)
        third.locator(".modelgrid .pick").first.click()
        third.wait_for_timeout(300)
        if third.locator(".addmodel-prepaid").count() != 1:
            failures.append("a model on OpenCode Go does not say it is not charged per token")
        if third.locator(".addmodel-pricing").count():
            failures.append("a model on OpenCode Go is offered a per-token price, which would be counted as dollars spent")
        context.close()
        stub.fresh = False  # type: ignore[attr-defined]
        settings_tab(browser, failures)
        browser.close()

    if not sent:
        print("nothing was sent: the flow did not reach the save")
        return 1
    for path, body in sent:
        print("PUT", path, json.dumps(body, sort_keys=True))
    prices = {k: v for path, body in sent for k, v in (body.get("pricing") or {}).items()}
    presets = [body for path, body in sent if path.startswith("presets/")]
    if prices:
        failures.append(f"a price was recorded for a model that was typed, not picked: {prices}")
    if not presets:
        failures.append("no preset was written")
    for preset in presets:
        if preset.get("model") != TYPED:
            failures.append(f"the preset is for {preset.get('model')!r}, not the model that was typed")
        if preset.get("label"):
            failures.append(f"the preset carries a label from the picked model: {preset['label']!r}")
        if preset.get("images"):
            failures.append("the preset says the typed model sees pictures, which is the picked model's answer")
        if preset.get("context_window") != 128000:
            failures.append(f"the preset carries a window from the picked model: {preset.get('context_window')}")
    declared_prices = {k: v for path, body in declared for k, v in (body.get("pricing") or {}).items()}
    declared_price = declared_prices.get("anthropic/claude-opus-5")
    if not declared_price or declared_price.get("input_limit") != 1048576 or declared_price.get("limit_source") != "Provider contract":
        failures.append(f"an exact picked model lost its operator-sourced input ceiling: {declared_price}")
    zen_puts = [body for path, body in opencode if path == "providers/opencode_zen"]
    if zen_puts != [{"kind": "opencode", "name": "OpenCode Zen", "base_url": "http://keyproxy:3200/opencode_zen", "billing": "metered"}]:
        failures.append(f"adding Zen did not send its route and billing: {opencode}")
    for line in failures:
        print("FAIL:", line)
    print("ok" if not failures else f"{len(failures)} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
