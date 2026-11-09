"""A model suggestion stays a reviewable draft until its diff is explicitly applied."""
from __future__ import annotations

import copy
import os
import time
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from screenshots import BASE, CHROMIUM, SETTINGS, UNHANDLED, respond, stub


def run() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for width, language in ((390, "ru"), (1440, "en")):
            settings = copy.deepcopy(SETTINGS)
            original = "Working rules:\n- Verify results.\n- Keep changes focused."
            revised = "Working rules:\n- Verify results.\n- Ask before deleting files.\n- Keep changes focused."
            settings["prompt"] = {"rules": "", "default_rules": original}
            state = {"proposal": None, "models": [{"id": "deepseek-flash", "label": "DeepSeek Flash"}]}
            posts = []

            def route_api(route, *, settings=settings, state=state, posts=posts, revised=revised):
                path = urlsplit(route.request.url).path
                if path == "/api/settings":
                    return respond(route, settings)
                if path == "/api/prompt-change":
                    return respond(route, state)
                if path == "/api/prompt-change/request":
                    data = route.request.post_data_json
                    posts.append(("request", data))
                    state["proposal"] = {"id": f"p{len(posts)}", "state": "planning", "stage": "model", "instruction": data["instruction"], "preset": data["preset"], "started_at": time.time(), "updated_at": time.time()}
                    return respond(route, state["proposal"])
                if path.endswith("/cancel"):
                    posts.append(("cancel", path))
                    state["proposal"]["state"] = "cancelled"
                    return respond(route, {"cancelled": True})
                if path.endswith("/approve"):
                    posts.append(("approve", path))
                    state["proposal"]["state"] = "applied"
                    settings["prompt"]["rules"] = revised
                    return respond(route, {"applied": True, "rules": revised})
                return stub(route)

            context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme="dark")
            page = context.new_page()
            page.route("**/api/**", route_api)
            page.goto(f"{BASE}/settings/rules?lang={language}")
            request = page.locator("#prompt-change-request")
            expect(request).to_be_visible()
            expect(page.locator(".prompt-change")).to_contain_text("DeepSeek Flash")
            if directory := os.environ.get("PROMPT_SCREENSHOTS"):
                page.screenshot(path=f"{directory}/prompt-rules-{width}-{language}.png")
            request.fill("Ask before deleting files")
            prepare = "Подготовить предложение" if language == "ru" else "Prepare suggestion"
            page.get_by_role("button", name=prepare).click()
            expect(page.locator(".prompt-change-status")).to_be_visible()
            if directory := os.environ.get("PROMPT_SCREENSHOTS"):
                page.screenshot(path=f"{directory}/prompt-working-{width}-{language}.png")
            assert posts[0] == ("request", {"instruction": "Ask before deleting files", "preset": "deepseek-flash"})

            proposal = state["proposal"]
            proposal.update(state="ready", stage="ready", summary="Добавлено подтверждение удаления." if language == "ru" else "Added confirmation before deletion.", rules=revised, diff="--- Current working rules\n+++ Proposed working rules\n@@ -1,3 +1,4 @@\n Working rules:\n - Verify results.\n+- Ask before deleting files.\n - Keep changes focused.\n")
            # The review sheet: Settings is itself a dialog now, so "the dialog" has to name which.
            dialog = page.locator(".sheet[role='dialog']")
            expect(dialog).to_be_visible(timeout=8000)
            expect(dialog.locator(".diff-add")).to_contain_text("Ask before deleting files")
            assert original in page.locator(".rules-editor-field").input_value()
            discard = "Отклонить предложение" if language == "ru" else "Discard suggestion"
            dialog.get_by_role("button", name=discard).click()
            expect(dialog).to_have_count(0)
            assert posts[-1][0] == "cancel"

            request.fill("Ask before deleting files")
            # The new proposal has to exist before it is made ready: updating straight after the click
            # raced the request under load, readied the cancelled proposal, and the new one then
            # replaced it still planning, so the review never opened.
            with page.expect_response("**/api/prompt-change/request"):
                page.get_by_role("button", name=prepare).click()
            state["proposal"].update(state="ready", stage="ready", summary="Added confirmation before deletion.", rules=revised, diff=proposal["diff"])
            expect(dialog).to_be_visible(timeout=8000)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            if directory := os.environ.get("PROMPT_SCREENSHOTS"):
                page.wait_for_timeout(300)
                page.screenshot(path=f"{directory}/prompt-review-{width}-{language}.png")
            apply = "Применить изменение" if language == "ru" else "Apply change"
            dialog.get_by_role("button", name=apply).click()
            expect(dialog).to_have_count(0)
            expect(page.locator(".rules-editor-field")).to_have_value(revised)
            assert posts[-1][0] == "approve"
            context.close()
            print(f"{width} {language}: request, progress, diff, discard, reload-safe review and apply passed")
        browser.close()
    assert not UNHANDLED.report()


if __name__ == "__main__":
    run()
