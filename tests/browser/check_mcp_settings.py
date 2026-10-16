"""Settings → Tools → MCP servers, in a real browser.

At 1440 × 900 and on a 390 px phone: the card lists each server with its state and what it runs, a
server waiting for its sign-in offers Sign in, Add server opens a form whose fields follow the
connection kind and whose save sends the server to the host, Remove asks first and sends a delete,
and a sign-in that came back says so once and leaves the address clean. Nothing scrolls sideways.

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LANG = os.environ.get("LANG_UI", "en")


def check(page: Page, problems: list[str], phone: bool) -> None:
    sent: list[tuple[str, str, dict]] = []

    def route(r) -> None:  # type: ignore[no-untyped-def]
        url = r.request.url.split("?", 1)[0]
        if "/api/mcp/" in url and r.request.method in ("PUT", "DELETE"):
            sent.append((r.request.method, url[url.index("/api/"):], json.loads(r.request.post_data or "{}")))
        return stub(r)

    page.route("**/api/**", route)
    page.goto(f"{BASE}/settings/tools?token=t&lang={LANG}&mcp=tracker&mcp_linked=1")
    card = page.locator("[data-setting='mcp.servers']")
    card.wait_for(timeout=15000)
    card.scroll_into_view_if_needed()
    if "mcp=" in page.url:
        problems.append(f"the sign-in outcome stayed in the address: {page.url}")
    rows = card.locator("[data-mcp-server]")
    names = rows.evaluate_all("(els) => els.map((el) => el.dataset.mcpServer)")
    if names != ["files", "tracker"]:
        problems.append(f"expected the two servers listed, got {names}")
    files = card.locator("[data-mcp-server='files']")
    if "@modelcontextprotocol/server-filesystem" not in files.inner_text():
        problems.append("the stdio server does not show what it runs")
    tracker = card.locator("[data-mcp-server='tracker']")
    sign_in = "Sign in" if LANG == "en" else "Войти"
    if tracker.get_by_role("button", name=sign_in).count() != 1 or files.get_by_role("button", name=sign_in).count():
        problems.append("only the server waiting for its sign-in should offer Sign in")
    card.get_by_role("button", name="Add server" if LANG == "en" else "Добавить сервер").click()
    form = card.locator("[data-mcp-form]")
    form.locator("input.field").first.fill("linear")
    form.locator("input.field.mono").nth(1).fill("https://mcp.linear.example/mcp")
    form.locator("input[type=checkbox]").check()
    if form.locator("input[type=password]").count() != 1:
        problems.append("turning sign-in on does not offer the client fields")
    form.get_by_role("button", name="Add and connect" if LANG == "en" else "Добавить и подключить").click()
    page.wait_for_timeout(400)
    put = next((item for item in sent if item[0] == "PUT"), None)
    if put is None or put[1] != "/api/mcp/servers/linear" or put[2].get("url") != "https://mcp.linear.example/mcp" or put[2].get("oauth") is None:
        problems.append(f"the new server was not sent as asked: {put}")
    files.get_by_role("button", name="Remove" if LANG == "en" else "Убрать").click()
    page.locator(".sheet-backdrop.confirm .dialog button").last.click()
    page.wait_for_timeout(300)
    if not any(item[0] == "DELETE" and item[1] == "/api/mcp/servers/files" for item in sent):
        problems.append("Remove did not delete the server")
    if phone and page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth + 1"):
        problems.append("the page scrolls sideways on a phone")


def run() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for viewport, phone in (({"width": 1440, "height": 900}, False), ({"width": 390, "height": 844}, True)):
            context = browser.new_context(viewport=viewport, color_scheme="dark")
            check(context.new_page(), problems, phone)
            context.close()
        browser.close()
    for problem in problems:
        print("FAIL:", problem)
    print("check_mcp_settings:", "ok" if not problems else f"{len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(run())
