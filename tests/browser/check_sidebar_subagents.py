"""The desktop sidebar stays flat, its menus escape the sidebar, and subagents live in the header."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def centres(page, button: str) -> float:  # type: ignore[no-untyped-def]
    return page.locator(button).first.evaluate(
        """button => {
          const box = button.getBoundingClientRect();
          const icon = button.querySelector('svg').getBoundingClientRect();
          return Math.max(
            Math.abs((box.left + box.width / 2) - (icon.left + icon.width / 2)),
            Math.abs((box.top + box.height / 2) - (icon.top + icon.height / 2)),
          );
        }"""
    )


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        posted: list[dict] = []

        def watch(route):  # type: ignore[no-untyped-def]
            request = route.request
            path = request.url.split("?", 1)[0]
            if request.method == "POST" and path.rstrip("/").endswith("/api/sessions"):
                posted.append(json.loads(request.post_data or "{}"))
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": S1, "title": "draft", "model": "Local model"}))
                return
            if request.method == "POST" and path.endswith("/messages"):
                posted.append(json.loads(request.post_data or "{}"))
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"run_id": "r"}))
                return
            return stub(route)

        page.route("**/api/**", watch)

        page.goto(f"{BASE}/agents?token=t&lang=en&scheme=dark", wait_until="networkidle")
        expect(page.locator(".start-greeting")).to_be_visible()
        expect(page.locator(".start .composer-box")).to_be_visible()
        expect(page.locator(".sidebar .folder").first).to_be_visible()
        main_plus_error = centres(page, ".start .iconbtn.plus")
        if main_plus_error > 1:
            problems.append(f"the start composer plus is {main_plus_error:.1f}px off centre")
        page.locator(".start textarea").fill("The login form rejects a correct password")
        page.locator(".start .roundbtn.primary").click()
        page.wait_for_url(f"**/agents/{S1}")
        if not posted or posted[0].get("autotitle") is not True or "login form" not in str(posted[0].get("title")):
            problems.append(f"the first message did not open a chat: {posted[:1]}")
        if len(posted) < 2 or posted[1].get("text") != "The login form rejects a correct password":
            problems.append(f"the first message was not sent: {posted[1:]}")

        page.goto(f"{BASE}/agents/{S1}?token=t&lang=en&scheme=dark&panel=details", wait_until="networkidle")
        expect(page.locator(".sidebar-new-agent")).to_be_visible()
        project = page.locator('.sidebar [data-project="9f3c2a1b7d40"]')
        expect(project.locator(".erow")).to_have_count(3)
        folder_height = project.locator(".folder-top").evaluate("node => node.getBoundingClientRect().height")
        row_height = project.locator(".erow").first.evaluate("node => node.getBoundingClientRect().height")
        if folder_height > 44 or row_height > 44:
            problems.append(f"the sidebar is still card-dense: folder={folder_height:.0f}px row={row_height:.0f}px")

        project.locator(".folder-top").hover()
        expect(project.locator(".folder-add")).to_be_visible()
        sidebar_plus_error = centres(page, '.sidebar [data-project="9f3c2a1b7d40"] .folder-add')
        if sidebar_plus_error > 1:
            problems.append(f"the project plus is {sidebar_plus_error:.1f}px off centre")

        row_menu = project.locator(".erow").first.locator(".session-row-menu button")
        row_menu.click()
        menu = page.locator(".menu[role='menu']")
        expect(menu).to_be_visible()
        geometry = menu.evaluate(
            """menu => {
              const box = menu.getBoundingClientRect();
              const sidebar = document.querySelector('.sidebar').getBoundingClientRect();
              return { width: box.width, left: box.left, right: box.right, sidebarRight: sidebar.right,
                       menuZ: Number(getComputedStyle(menu).zIndex), sidebarZ: Number(getComputedStyle(document.querySelector('.sidebar')).zIndex) };
            }"""
        )
        if geometry["width"] > 300:
            problems.append(f"the session menu is {geometry['width']:.0f}px wide")
        if geometry["menuZ"] <= geometry["sidebarZ"]:
            problems.append(f"the session menu is behind the sidebar ({geometry['menuZ']} <= {geometry['sidebarZ']})")
        page.get_by_role("menuitem", name="Rename").click()
        expect(page.get_by_role("dialog", name="Rename")).to_be_visible()
        page.get_by_role("dialog").get_by_role("button", name="Close").click()

        trigger = page.locator(".subagents-trigger")
        expect(trigger).to_be_visible()
        trigger.click()
        popover = page.locator(".subagents-popover")
        expect(popover).to_be_visible()
        expect(popover).to_contain_text("link-check")
        popover_box = popover.bounding_box()
        assert popover_box
        if popover_box["width"] > 360 or popover_box["x"] + popover_box["width"] > 1432:
            problems.append(f"the subagent window is misplaced: {popover_box}")
        expect(page.locator('.details [id$="-info-subagents"]')).to_have_count(0)

        if os.environ.get("SHOTS"):
            page.screenshot(path="/tmp/daedalus-sidebar-subagents.png", full_page=True)
        # A phone's rows carry no "+" of their own: a new chat in a project is the first command of
        # the project's sheet (its section's ⋮) and of each of its rows' sheets.
        phone = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        phone.route("**/api/**", stub)
        for page_, width in ((phone, 390), (browser.new_page(viewport={"width": 688, "height": 900}, is_mobile=True, has_touch=True), 688)):
            if width != 390:
                page_.route("**/api/**", stub)
            page_.goto(f"{BASE}/agents?view=chats&token=t&lang=en&scheme=dark", wait_until="networkidle")
            expect(page_.locator(".folder-add")).to_have_count(0)
            section = page_.locator(".ph-chats section[data-project]").first
            name = section.locator(".ph-sec-t").inner_text()
            section.locator(".ph-sec-act .ph-ib").first.click()
            expect(page_.locator(".ph-actions .ph-mrow").first).to_have_text(f"New chat in {name}")
            page_.keyboard.press("Escape")
            section.locator(".ph-row-more").first.click()
            expect(page_.locator(".ph-actions .ph-mrow").first).to_have_text(f"New chat in {name}")
            page_.keyboard.press("Escape")
            if width != 390:
                page_.close()
        phone.goto(f"{BASE}/agents/{S1}?token=t&lang=en&scheme=dark", wait_until="networkidle")
        # A phone reaches the sub-agents from the bar's ⋮ sheet, as a sheet of rows of its own.
        phone.locator(".ph-chat-top button[aria-haspopup='menu']").first.click()
        phone.locator(".ph-session-menu .ph-mrow", has_text="Subagents").click()
        phone_sheet = phone.locator(".sheet", has_text="Subagents")
        expect(phone_sheet).to_be_visible()
        phone_box = phone_sheet.bounding_box()
        assert phone_box
        if phone_box["x"] < 0 or phone_box["x"] + phone_box["width"] > 390:
            problems.append(f"the phone subagent sheet runs off screen: {phone_box}")
        phone.close()
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    raise SystemExit(run())
