"""Local HTML, SVG and PDF previews cannot open app-origin blob documents."""

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, expect_app  # noqa: E402
from check_kept_files import serve  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SCRIPT = "window.activeArtifactRan = true; fetch('/api/private', {credentials:'include'})"
SAMPLES = (
    ("sample.html", "text/html", f"<!doctype html><script>{SCRIPT}</script><p>HTML preview</p>"),
    ("sample.svg", "image/svg+xml", f'<svg xmlns="http://www.w3.org/2000/svg"><script>{SCRIPT}</script><text x="10" y="20">SVG preview</text></svg>'),
    ("sample.pdf", "application/pdf", (Path(__file__).parents[2] / "skills/theme-factory/theme-showcase.pdf").read_bytes()),
    ("active.pdf", "application/pdf", f"<!doctype html><script>{SCRIPT}</script>"),
)


def main() -> int:
    expect_app(BASE)
    stub = FocusStub.bakery("en")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        page = context.new_page()
        hits = []
        serve(page, stub.answer)

        def private(route):
            hits.append(route.request.url)
            return route.fulfill(status=200, body="private")

        context.route("**/api/private", private)
        page.goto(f"{BASE}/agents/sess-lev?token=t&lang=en")
        expect(page.locator(".composer textarea")).to_be_visible()
        page.wait_for_timeout(800)
        for name, mime, body in SAMPLES:
            page.locator(".composer input[type=file]").first.set_input_files({"name": name, "mimeType": mime, "buffer": body if isinstance(body, bytes) else body.encode()})
            attachment = page.locator(".composer .attachment", has_text=name)
            expect(attachment).to_be_visible()
            attachment.locator(".attachment-open").click()
            sheet = page.locator(".sheet.preview")
            expect(sheet).to_be_visible()
            expect(sheet.locator("a[aria-label='Download']")).to_be_visible()
            assert sheet.locator("a[aria-label='Open in a new tab']").count() == 0, name
            if name.endswith(".pdf"):
                frame = sheet.locator("iframe.preview-frame")
                expect(frame).to_be_visible()
                assert (frame.get_attribute("src") or "").startswith("data:application/pdf;base64,")
                if name == "sample.pdf":
                    page.wait_for_timeout(800)
                    assert any(child.url.startswith("chrome-extension://") for child in page.frames), "PDF viewer did not load"
            assert not page.evaluate("window.activeArtifactRan === true"), name
            assert not hits, f"{name} contacted a private API"
            before = page.url
            with page.expect_download() as download:
                sheet.locator("a[aria-label='Download']").click()
            assert download.value.suggested_filename == name and page.url == before, name
            sheet.get_by_role("button", name="Close").click()
        context.close()
        browser.close()
    print("active HTML, SVG and PDF previews: no blob new-tab action or private API hit")
    return UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
