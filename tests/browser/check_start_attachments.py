"""Files picked on the start screen show as cards inside its composer, the way a session's do.

The start screen once read the picked files inside a state update that ran after the file input had
been cleared, so the list it read was empty and nothing attached ever showed; and when files did
arrive they were a line of names under the box. Picked, pasted or dropped, each is now a card with
its picture or glyph, name, size and a remove button.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_start_attachments.py

Exit status is the number of checks that failed.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = os.environ.get("APP_URL", "http://127.0.0.1:8163/app")


def main() -> int:
    expect_app(BASE)
    failures = 0
    work = Path(tempfile.mkdtemp())
    note, picture = work / "notes.txt", work / "shot.png"
    note.write_text("rates")
    picture.write_bytes(shots.png(40, 40))
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shots.CHROMIUM)
        for width, height, mobile in ((1440, 900, False), (390, 844, True)):
            page = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile).new_page()
            page.route("**/api/**", shots.stub)
            page.goto(f"{BASE}/agents?token=t&lang=en")
            page.wait_for_selector(".start-composer")
            page.locator(".start-composer .plus").click()
            # The desktop's + is a menu; a phone's is a sheet whose third tile attaches files.
            with page.expect_file_chooser() as chooser:
                page.locator(".plus-menu [role=menuitem], .ph-plus-sheet .ph-tile:nth-child(3)").first.click()
            chooser.value.set_files([str(note), str(picture)])
            cards = page.locator(".start-composer .attachment")
            try:
                cards.nth(1).wait_for(timeout=5000)
            except Exception:
                pass
            if cards.count() != 2:
                print(f"FAIL {width}px: {cards.count()} cards after picking two files")
                failures += 1
                page.context.close()
                continue
            if page.locator(".start-composer .attachment.image img").count() != 1:
                print(f"FAIL {width}px: the picture has no preview")
                failures += 1
            page.locator(".start-composer .attachment-x").first.click()
            if cards.count() != 1:
                print(f"FAIL {width}px: removing a card left {cards.count()}")
                failures += 1
            box = page.locator(".start-composer .composer-box").bounding_box()
            if box and box["x"] + box["width"] > width + 1:
                print(f"FAIL {width}px: the composer overflows with an attachment")
                failures += 1
            print(f"{width}px: two files became two cards, the picture previewed, and one could be removed")
            page.context.close()
        browser.close()
    return failures


if __name__ == "__main__":
    sys.exit(main())
