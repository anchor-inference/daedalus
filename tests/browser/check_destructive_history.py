"""The rendered conversation loses a deleted tail immediately and after reload."""
from __future__ import annotations

from check_composer import CHROMIUM, HOST, UNHANDLED, message, open_page
from playwright.sync_api import expect, sync_playwright


def run() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for width in (390, 1440):
            context = browser.new_context(viewport={"width": width, "height": 900})
            HOST.status = "idle"
            HOST.thinking = True
            HOST.messages = [message(101, "user", "Original request"), message(102, "assistant", "Discarded answer"), message(103, "user", "Discarded followup"), message(104, "assistant", "Discarded final")]
            page = open_page(context, phone=width < 600)
            page.set_default_timeout(5000)
            turn = page.locator('.turn').filter(has=page.locator('.answer', has_text="Discarded answer"))
            if width < 600:
                turn.locator(".msg-actions").last.get_by_role("button", name="More actions").click()
                page.get_by_role("menuitem", name="Regenerate", exact=True).click()
            else:
                turn.get_by_role("button", name="Regenerate", exact=True).click()
            page.get_by_role("alertdialog").get_by_role("button", name="Regenerate", exact=True).click()
            expect(page.locator(".answer", has_text="Replacement answer")).to_be_visible(timeout=3000)
            assert "Discarded" not in page.locator(".chat-scroll").inner_text()
            assert page.locator(".msg.user").count() == 1
            page.reload()
            expect(page.locator(".answer", has_text="Replacement answer")).to_be_visible()
            assert "Discarded" not in page.locator(".chat-scroll").inner_text()
            if width < 600:
                page.locator(".msg-wrap").get_by_role("button", name="More actions").click()
                page.get_by_role("menuitem", name="Revert to here…").click()
            else:
                page.locator(".msg-wrap").get_by_role("button", name="Revert to here…").click()
            page.get_by_role("alertdialog").get_by_role("button", name="Revert", exact=True).click()
            expect(page.locator(".msg.user")).to_have_count(0, timeout=3000)
            expect(page.locator(".answer")).to_have_count(0)
            page.reload()
            page.wait_for_selector(".composer")
            expect(page.locator(".answer")).to_have_count(0)
            # Effort is a branch of the model menu at every width.
            page.locator(".composer .model-select").click()
            page.locator(".effort-entry").click()
            page.locator('.effort-option input[value="off"]').click()
            page.wait_for_timeout(200)
            assert HOST.thinking is False
            page.reload()
            page.wait_for_selector(".composer")
            page.locator(".composer .model-select").click()
            page.locator(".effort-entry").click()
            expect(page.locator('.effort-option input[value="off"]')).to_be_checked()
            print(f"{width}: destructive retry/revert, reload, reasoning off passed")
            context.close()
        browser.close()
    assert not UNHANDLED.report()


if __name__ == "__main__":
    run()
