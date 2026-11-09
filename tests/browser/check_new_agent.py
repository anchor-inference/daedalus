"""New agent is the new-chat composer in Agent mode: no window of its own. English and Russian,
1440 px and a 390 px phone.

What is checked:

- every way in leads there: the sidebar's + of a project, the New chat button's menu, a phone's
  project sheet, the command palette's "New agent" and the mode menu of the composer itself; a project's entries arrive
  with that project picked, and no dialog opens anywhere;
- a typical agent takes two actions: New agent, then the task and Enter; the name it is not given
  comes from the task;
- a named agent with no task is made idle (no message is sent);
- the rare choices sit under +: a private chat (fixed at creation), an own directory inside the
  project, a loop with its instruction, and the tools it must not have; each reaches the request.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_new_agent.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import DEFAULT_APP, expect_app  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS_DIR", "")
WORDS = {
    "en": {"agent": "Agent", "palette": "New agent", "greeting": "What should the agent do?", "loop": "Loop agent", "own": "Give this agent its own directory"},
    "ru": {"agent": "Агент", "palette": "Новый агент", "greeting": "Что поручим агенту?", "loop": "Агент с циклом", "own": "Выделить агенту свою папку"},
}
TOOLS = [{"name": "Exec", "group": "shell", "description": "Run a command"}, {"name": "Read", "group": "files", "description": "Read a file"}]


class Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def made(self) -> list[dict]:
        return [body for path, body in self.calls if path == "/api/sessions"]

    def messages(self) -> list[dict]:
        return [body for path, body in self.calls if path.endswith("/messages")]


def serve(page: Page, host: Host) -> None:
    def route(r) -> None:  # type: ignore[no-untyped-def]
        request = r.request
        path = urlsplit(request.url).path
        path = path[path.index("/api/"):]
        if request.method == "GET" and path == "/api/tools":
            return shots.respond(r, TOOLS)
        if request.method == "POST" and path == "/api/sessions":
            host.calls.append((path, request.post_data_json))
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"id": shots.S1}))
        if request.method == "POST" and path.startswith(f"/api/sessions/{shots.S1}/"):
            host.calls.append((path, request.post_data_json or {}))
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "run_id": "r1"}))
        return shots.stub(r)

    page.route("**/api/**", route)


def in_agent_mode(page: Page, project: str | None) -> bool:
    page.wait_for_selector(".start-composer[data-agent]", timeout=10000)
    query = page.evaluate("() => location.search")
    picked = page.locator(f".start-where-chip[data-project='{project}']") if project else None
    return "agent=1" in query and (project is None or f"in={project}" in query) and (picked is None or picked.count() == 0 or picked.get_attribute("aria-pressed") == "true")


def desktop(browser, lang: str, check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"{lang} 1440"
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    host = Host()
    serve(page, host)
    home = f"{BASE}/agents?token=t&lang={lang}&scheme=dark"

    # The sidebar's + on a project.
    page.goto(home)
    row = page.locator(f".sidebar [data-project='{shots.P1}'] .sb-prow").first
    row.wait_for(timeout=15000)
    row.hover()
    page.locator(f".sidebar [data-project='{shots.P1}'] .sb-padd").click()
    check(in_agent_mode(page, shots.P1), f"{where}: the sidebar's + opens Agent mode in the project")
    check(page.locator(".sheet").count() == 0, f"{where}: and no dialog")
    check(page.locator(".start-greeting").inner_text() == words["greeting"], f"{where}: the page says what it is for")
    check(page.evaluate("() => document.activeElement?.classList.contains('start-agent-name')"), f"{where}: the name has the caret")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/new-agent-{lang}-1440.png")

    # The split New chat button's menu.
    page.goto(home)
    page.locator(".sb-newsplit .sb-new-more").click()
    page.get_by_role("menuitem", name=words["palette"]).click()
    check(in_agent_mode(page, None), f"{where}: the New chat button's menu offers New agent")

    # The command palette.
    page.goto(home)
    page.wait_for_selector(".start-composer", timeout=15000)
    page.keyboard.press("Control+k")
    page.wait_for_selector("[role=option]", timeout=5000)
    page.locator(".palette-sheet input").click()
    page.keyboard.type(words["palette"])
    page.wait_for_function("w => document.querySelector('[role=option]')?.textContent === w", arg=words["palette"], timeout=5000)
    page.keyboard.press("Enter")
    check(in_agent_mode(page, None), f"{where}: the palette's New agent opens Agent mode")

    # The composer's own mode menu, there and back.
    page.goto(home)
    page.locator(".start-composer .kind-select").click()
    page.locator(".kind-menu [data-kind='agent']").click()
    check(in_agent_mode(page, None), f"{where}: the mode menu offers Agent")
    check(page.locator(".start-composer .kind-select").get_attribute("data-kind") == "agent", f"{where}: and the mode says Agent")
    page.locator(".start-composer .kind-select").click()
    page.locator(".kind-menu [data-kind='chat']").click()
    page.wait_for_function("() => !location.search.includes('agent=1')", timeout=5000)
    check(page.locator(".start-agent-name").count() == 0, f"{where}: Chat leaves Agent mode")

    # Two actions: New agent, then the task and Enter. The name comes from the task.
    page.goto(f"{BASE}/agents?agent=1&token=t&lang={lang}&scheme=dark")
    page.wait_for_selector(".start-agent-name", timeout=15000)
    page.keyboard.press("Enter")  # from the name to the task, nothing typed
    page.keyboard.type("Check the logs for errors")
    page.keyboard.press("Enter")
    page.wait_for_function("() => location.pathname.includes('/agents/')", timeout=10000)
    made = host.made()
    check(len(made) == 1 and made[0].get("autotitle") is True and made[0].get("title") == "Check the logs for errors", f"{where}: a task alone makes the agent, named from it ({made})")
    check(host.messages() == [{"text": "Check the logs for errors"}], f"{where}: and is its first message ({host.messages()})")

    # A named agent with no task is made idle; the rare choices reach the request.
    host.calls.clear()
    page.goto(f"{BASE}/agents?agent=1&in={shots.P1}&token=t&lang={lang}&scheme=dark")
    page.wait_for_selector(".start-agent-name", timeout=15000)
    page.locator(".start-agent-name").fill("Night watch")
    composer = page.locator(".start-composer")
    composer.locator(".plus").click()
    menu = page.locator(".plus-menu")
    check(menu.locator("[data-private]").count() == 1 and menu.locator("[data-option='own-directory']").inner_text().strip() == words["own"], f"{where}: + holds the private chat and the own directory")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/new-agent-plus-{lang}-1440.png")
    menu.locator("[data-option='own-directory']").click()
    menu.locator("[data-private]").click()
    composer.locator(".plus").click()
    page.locator(".plus-menu [data-option='loop']").click()
    loop = composer.locator(".start-agent-loop")
    check(loop.count() == 1, f"{where}: the loop asks for its instruction")
    check(composer.locator(".roundbtn.primary").is_disabled(), f"{where}: and creating waits for it")
    loop.locator("textarea").fill("Look at the overnight orders")
    composer.locator(".plus").click()
    page.locator(".plus-menu [data-option='tools']").click()
    sheet = page.locator(".sheet")
    sheet.locator(".toolpicker-head").click()
    sheet.locator(".toolrow:not(.head)", has_text="Exec").locator("input").uncheck()
    page.keyboard.press("Escape")
    check("1" in composer.locator("[data-agent-chosen]").inner_text(), f"{where}: the tools taken away are said under the field")
    composer.locator(".roundbtn.primary").click()
    page.wait_for_function("() => location.pathname.includes('/agents/')", timeout=10000)
    made = host.made()
    body = made[0] if made else {}
    check(body.get("title") == "Night watch" and body.get("autotitle") is False, f"{where}: the name is kept as given ({body})")
    check(body.get("project_id") == shots.P1 and body.get("own_directory") is True and body.get("private") is True, f"{where}: the project, the own directory and private reach the request ({body})")
    check((body.get("loop") or {}).get("instruction") == "Look at the overnight orders" and body.get("tools_off") == ["Exec"], f"{where}: the loop and the tools reach the request ({body})")
    check(host.messages() == [], f"{where}: no task, no message: the agent waits ({host.messages()})")
    context.close()


def phone(browser, lang: str, check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"{lang} 390"
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark")
    page = context.new_page()
    host = Host()
    serve(page, host)
    page.goto(f"{BASE}/agents?view=chats&token=t&lang={lang}&scheme=dark")
    section = page.locator(".ph-chats section[data-project]").first
    section.wait_for(timeout=15000)
    project = section.get_attribute("data-project")
    section.locator(".ph-sec-act .ph-ib").first.click()
    page.locator(".ph-actions .ph-mrow").first.click()
    check(in_agent_mode(page, project), f"{where}: the project sheet's first row opens Agent mode in it")
    check(page.locator(".start-composer .start-agent-name").is_visible(), f"{where}: the name heads the composer")
    check(page.locator(".ph-kind").get_attribute("data-kind") == "agent", f"{where}: the mode says {words['agent']}")
    wide = page.evaluate("() => document.documentElement.scrollWidth - innerWidth")
    check(wide <= 0, f"{where}: nothing scrolls sideways ({wide})")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/new-agent-{lang}-390.png")
    page.locator(".start-composer .plus").click()
    rows = page.locator(".ph-plus-sheet")
    check(rows.locator("[data-option='loop']").count() == 1 and rows.locator("[data-option='tools']").count() == 1, f"{where}: + offers the loop and the tools")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/new-agent-plus-{lang}-390.png")
    page.keyboard.press("Escape")
    expect(rows).to_have_count(0)
    page.locator(".start-composer .composer-field").click()
    page.keyboard.type("Water the plants")
    page.locator(".start-composer .roundbtn.primary").click()
    page.wait_for_function("() => location.pathname.includes('/agents/')", timeout=10000)
    made = host.made()
    check(bool(made) and made[0].get("project_id") == project, f"{where}: the agent is made in the project ({made})")
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            problems.append(what)
            print("FAIL", what)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            desktop(browser, lang, check)
            phone(browser, lang, check)
        browser.close()
    print("new agent:", "ok" if not problems else f"{len(problems)} FAILED")
    return (1 if problems else 0) + shots.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
