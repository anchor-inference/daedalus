"""The project's three visible destinations stay usable on a short phone."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, expect_app  # noqa: E402
from check_project_phone import serve  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PROJECT = "b4k3ry20f0c5"


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width in (320, 390):
                context = browser.new_context(viewport={"width": width, "height": 560}, is_mobile=True, has_touch=True)
                page = context.new_page()
                focus = FocusStub.bakery(lang)
                focus.ask_from_ira(lang)
                focus.next_actions[PROJECT] = [{"action_id": "next-for-operator", "task_id": "task-next", "contract_revision": 1,
                                                "kind": "provide_input", "owner_kind": "operator", "owner_id": None,
                                                "due_at": None, "context_ref": None, "enabled": False, "blockers": ["dependency_not_ready"]}]
                serve(page, focus)
                page.goto(f"{BASE}/orchestration/project/{PROJECT}?token=t&lang={lang}")
                bar = page.locator("nav.project-tabs")
                expect(bar.locator(":scope > a, :scope > button")).to_have_count(5)
                # Waiting questions take the goal line's place: on a phone as the Needs-you card above the
                # chat; with none, the goal line is there.
                expect(page.locator(".goal-headline, .needs-card").first).to_be_visible()
                # The composer's white circle is on screen: a voice conversation while the field is
                # empty, Send once it holds words.
                send = page.locator(".composer [data-action='send'], .composer [data-action='voice']").first
                expect(send).to_be_visible()
                assert send.bounding_box()["y"] + send.bounding_box()["height"] <= 560
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (lang, width, "home overflow")

                draft = page.locator(".composer textarea")
                draft.fill("Keep this draft until I send it")
                page.locator(".composer input[type='file']").first.set_input_files({"name": "evidence.txt", "mimeType": "text/plain", "buffer": b"checked artifact"})
                expect(page.locator(".composer .attachments")).to_contain_text("evidence.txt")
                page.wait_for_function("""() => new Promise(resolve => {
                    const request = indexedDB.open('daedalus-composer-drafts');
                    request.onerror = () => resolve(false);
                    request.onsuccess = () => {
                      const db = request.result;
                      const get = db.transaction('attachments').objectStore('attachments').get('orch-bakery');
                      get.onsuccess = () => { resolve(get.result?.files?.length === 1); db.close(); };
                      get.onerror = () => { resolve(false); db.close(); };
                    };
                  })""")
                page.reload()
                expect(page.locator(".composer textarea")).to_have_value("Keep this draft until I send it")
                expect(page.locator(".composer .attachments")).to_contain_text("evidence.txt")

                bar.locator("button[data-tab='more']").click()
                page.locator(".sheet.more-sheet .ph-mrow[data-more='attention']").click()
                expect(page.locator(".focus-attention-item")).not_to_have_count(0)
                # A request is a row that opens the one decision sheet, where it is answered.
                page.locator("[data-kind='ask'] .focus-attention-item").first.click()
                expect(page.locator(".ph-decisions .q-card").first).to_be_visible()
                page.keyboard.press("Escape")
                expect(page.locator(".focus-attention-item")).to_have_count(3)
                blocked = page.locator(".focus-attention-item").last
                expect(blocked).to_contain_text("dependent work is unfinished" if lang == "en" else "зависимая работа не закончена")
                expect(blocked.get_by_role("button", name="Open task" if lang == "en" else "Открыть задачу")).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (lang, width, "attention overflow")

                bar.locator("button[data-tab='more']").click()
                page.locator(".sheet.more-sheet .ph-mrow[data-more='journal']").click()
                expect(page.locator(".journal-entry")).not_to_have_count(0)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (lang, width, "history overflow")
                context.close()
        browser.close()
    print("project clarity: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
