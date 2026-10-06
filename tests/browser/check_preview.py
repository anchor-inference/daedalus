"""Render each file kind, follow sandboxed HTML links, and open cited source lines."""
from __future__ import annotations

import base64
import io
import os
import sys
import zipfile
from urllib.parse import parse_qs, quote, urlsplit

from api_stub import DEFAULT_APP, FILE_TEXT, expect_app
from playwright.sync_api import expect, sync_playwright
from screenshots import S1, SILENCE, UNHANDLED, detail, respond, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def document_zip(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return out.getvalue()


def tiny_pdf() -> bytes:
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>"]
    data = b"%PDF-1.4\n"
    offsets = [0]
    for i, body in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(data)
    data += b"xref\n0 4\n0000000000 65535 f \n" + b"".join(f"{n:010d} 00000 n \n".encode() for n in offsets[1:])
    return data + f"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode()


DOCX = document_zip({
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
    "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
    "word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Document example</w:t></w:r></w:p></w:body></w:document>',
})

MEDIA = {
    "sample.webm": (base64.b64decode("GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAKeEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHYTbuMU6uEElTDZ1OsggEeTbuMU6uEHFO7a1OsggKI7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsirXsYMPQkBNgI1MYXZmNjAuMTYuMTAwV0GNTGF2ZjYwLjE2LjEwMESJiEB0AAAAAAAAFlSua8GuAQAAAAAAADjXgQFzxYhkdXNGH1R1tZyBACK1nIN1bmSIgQCGhVZfVlA4g4EBI+ODhAJiWgDgibCBILqBIJqBAhJUw2f8c3OgY8CAZ8iaRaOHRU5DT0RFUkSHjUxhdmY2MC4xNi4xMDBzc9ZjwItjxYhkdXNGH1R1tWfIoUWjh0VOQ09ERVJEh5RMYXZjNjAuMzEuMTAyIGxpYnZweGfIoUWjiERVUkFUSU9ORIeTMDA6MDA6MDAuMzIwMDAwMDAwAB9DtnVA4+eBAKO9gQAAgNACAJ0BKiAAIAAARwiFhYiFhIgCAgJ1qgP4AgghCD0A/v9NEv/8WFfxYV/FhX/xYV/8/M7txfzmAKOVgQAoALEBAAUQrAAYABhYL/QACHAAo5WBAFAAsQEABRCsABgAGFgv9AAIcACjlYEAeACxAQAFEKwAGAAYWC/0AAhwAKOVgQCgALEBAAUQrAAYABhYL/QACHAAo5WBAMgAsQEABRCsABgAGFgv9AAIcACjlYEA8ACxAQAFEKwAGAAYWC/0AAhwAKOVgQEYALEBAAUQEBRgAGFgv9AAIcAAHFO7a5G7j7OBALeK94EB8YIBn/CBAw=="), "video/webm"),
    "sample.svg": ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40"><rect width="80" height="40" fill="#529"/></svg>', "image/svg+xml"),
    "sample.pdf": (tiny_pdf(), "application/pdf"),
    "sample.wav": (SILENCE, "audio/wav"),
    "sample.bin": (bytes(range(256)), "application/octet-stream"),
    "sample.docx": (DOCX, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    # The sheet reader accepts this spreadsheet XML without needing a binary fixture in git.
    "sample.xls": ('<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="Prices"><Table><Row><Cell><Data ss:Type="String">Item</Data></Cell></Row><Row><Cell><Data ss:Type="String">Bread</Data></Cell></Row></Table></Worksheet></Workbook>', "application/vnd.ms-excel"),
}


def run() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_init_script("""(() => {
          const original = window.fetch;
          window.fetch = (url, opts) => {
            if (String(url).includes('download?path=slow.py')) {
              const data = new TextEncoder().encode('print(42)\\n');
              return Promise.resolve(new Response(new ReadableStream({ start(controller) {
                controller.enqueue(data.slice(0, 4));
                setTimeout(() => { controller.enqueue(data.slice(4)); controller.close(); }, 1200);
              } }), { headers: { 'Content-Length': String(data.length), 'Content-Type': 'text/plain' } }));
            }
            return original(url, opts);
          };
        })()""")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        downloads = []

        def route(r):
            url = urlsplit(r.request.url)
            if url.path == f"/api/sessions/{S1}":
                import copy
                data = copy.deepcopy(detail(S1))
                data["messages"][-1]["text"] += '\n\n<file path="src/main.py" lines="4-5">source</file>'
                return respond(r, data)
            if url.path.endswith("/verifications"):
                return respond(r, [{"id": 1, "criterion": "Check the changes", "command": "git diff", "exit_code": 0, "passed": 1, "output_head": FILE_TEXT["change.diff"], "duration_ms": 12, "at": "2026-01-01T00:00:00Z", "sandboxed": 1, "dependencies": "", "tests_run": None}])
            if url.path.endswith("/download"):
                name = parse_qs(url.query).get("path", [""])[0]
                downloads.append(name)
                if name in MEDIA:
                    body, mime = MEDIA[name]
                    return respond(r, body, content_type=mime)
            return stub(r)

        page.route("**/api/**", route)

        def open_file(path, selector, lines=""):
            page.goto(f"{BASE}/agents/{S1}?token=t&lang=en&panel=preview&path={quote(path)}" + (f"&lines={lines}" if lines else ""))
            expect(page.locator(selector)).to_be_visible(timeout=20000)
            expect(page.locator('.panel-toolbar a[aria-label="Download"]')).to_be_visible()
            expect(page.locator('.panel-toolbar a[aria-label="Open in a new tab"]')).to_be_visible()

        page.goto(f"{BASE}/agents/{S1}?token=t&lang=en&panel=preview&path=slow.py")
        expect(page.locator(".file-skeleton")).to_be_visible()
        progress = page.locator(".panel-body > .preview-progress")
        expect(progress).to_be_visible()
        assert 0 < int(progress.get_attribute("aria-valuenow")) < 100
        expect(page.locator(".source-view")).to_be_visible()
        expect(progress).to_have_count(0)
        open_file("src/main.py", ".source-view")
        page.locator('.chat-scroll .evidence[data-path="src/main.py"]').click()
        expect(page.locator('.line.cited[data-line="4"]')).to_be_visible()
        open_file("src/main.py", ".source-view")
        assert page.locator(".source-view .line").count() == 8
        assert "mono" in page.locator(".source-view").evaluate("e => getComputedStyle(e).fontFamily").lower()
        assert page.locator(".source-view .line").first.evaluate("e => getComputedStyle(e).whiteSpace") == "pre"
        page.get_by_role("button", name="Wrap lines", exact=True).click()
        expect(page.locator(".source-view")).to_have_class("filetext lined source-view wrap")
        assert page.locator(".source-view .line").first.evaluate("e => getComputedStyle(e).whiteSpace") == "pre-wrap"
        page.get_by_role("spinbutton", name="Go to line").fill("5")
        page.get_by_role("button", name="Go to line", exact=True).click()
        expect(page.locator('.line.cited[data-line="5"]')).to_be_visible()
        open_file("src/main.py", '.line.cited[data-line="4"]', "4-5")
        assert page.locator(".line.cited").count() == 2
        open_file("NOTES.md", ".preview-doc")
        page.get_by_role("button", name="Source", exact=True).click()
        expect(page.locator(".source-view")).to_be_visible()
        open_file("data/sample.csv", ".preview-grid")
        expect(page.locator(".preview-grid")).to_contain_text("fresh, daily")
        assert page.locator(".preview-grid th").first.evaluate("e => getComputedStyle(e).position") == "sticky"
        open_file("data/sample.tsv", ".preview-grid")
        expect(page.locator(".preview-grid")).to_contain_text("Bread")
        open_file("data/sample.json", ".json-tree")
        expect(page.locator(".json-tree")).to_contain_text('"ready": true')
        before = page.locator(".json-row").count()
        page.locator(".json-row button").first.click()
        assert page.locator(".json-row").count() < before
        page.get_by_role("button", name="Source", exact=True).click()
        expect(page.locator(".source-view")).to_be_visible()
        for name in ["change.diff", "change.patch"]:
            open_file(name, ".diff-view")
            expect(page.locator(".diff-add")).to_contain_text("new = 2")
            expect(page.locator(".diff-del")).to_contain_text("old = 1")
        open_file("sample.svg", ".preview-image")
        expect(page.locator(".source-tools")).to_contain_text("80 × 40 px")
        page.get_by_role("button", name="1:1", exact=True).click()
        expect(page.locator(".image-pan.actual")).to_be_visible()
        open_file("sample.pdf", ".preview-frame")
        # A data URL, not the blob URL: the PDF document gets an opaque origin, apart from the app's.
        assert page.locator(".preview-frame").get_attribute("src").startswith("data:application/pdf")
        open_file("sample.wav", "audio")
        page.wait_for_function("document.querySelector('audio').readyState >= 1")
        open_file("sample.webm", "video")
        page.wait_for_function("document.querySelector('video').readyState >= 1")
        open_file("sample.bin", ".viewer.other")
        expect(page.locator(".viewer.other")).to_contain_text("256")
        open_file("sample.docx", ".preview-doc.docx")
        expect(page.locator(".preview-doc.docx")).to_contain_text("Document example")
        open_file("sample.xls", ".preview-grid")
        expect(page.locator(".preview-grid")).to_contain_text("Bread")
        open_file("site/index.html", ".html-frame")
        def html_ready(heading):
            # Navigation replaces the srcdoc iframe. Wait for that document and its load
            # indicator before resolving a frame locator or clicking its links.
            page.wait_for_function("""heading => {
                const frame = document.querySelector('.html-frame');
                return frame?.srcdoc.includes(heading) && !document.querySelector('.panel-body > .preview-progress');
            }""", arg=heading)
            frame = page.frame_locator(".html-frame")
            expect(frame.get_by_role("heading", name=heading)).to_be_visible()
            return frame

        frame = html_ready("Bakery report")
        assert page.locator(".html-frame").get_attribute("sandbox") == "allow-scripts"
        isolated = page.locator(".html-frame").evaluate("e => { try { return !!e.contentWindow.document.body; } catch { return false; } }")
        assert not isolated, "the document can reach the app origin"
        frame.get_by_role("link", name="Next page").click()
        frame = html_ready("Second page")
        page.get_by_role("button", name="Back", exact=True).click()
        frame = html_ready("Bakery report")
        page.get_by_role("button", name="Forward", exact=True).click()
        frame = html_ready("Second page")
        before = downloads.count("site/next.html")
        previous_frame = page.locator(".html-frame").element_handle()
        page.get_by_role("button", name="Reload", exact=True).click()
        page.wait_for_function("frame => !frame.isConnected", arg=previous_frame)
        frame = html_ready("Second page")
        page.wait_for_function("document.querySelector('.panel-toolbar a[aria-label=\"Open in a new tab\"]').href.includes('next.html')")
        assert downloads.count("site/next.html") > before
        page.locator('.panel-toolbar button[aria-label="Phone width"]').click()
        assert page.locator(".html-frame").bounding_box()["width"] <= 390
        page.set_viewport_size({"width": 2560, "height": 1400})
        expect(page.locator(".panel-body.split")).to_be_visible()
        assert page.locator(".html-frame").bounding_box()["width"] <= 390
        page.emulate_media(reduced_motion="reduce")
        assert page.locator(".panel").evaluate("e => getComputedStyle(e).transitionDuration") == "0s"
        page.locator('[data-tab="jobs"]').click()
        page.locator(".receipt summary").click()
        expect(page.locator(".receipt .diff-add")).to_contain_text("new = 2")
        print("source, markdown, HTML navigation, image, PDF, CSV, TSV, JSON, media, diffs, office and binary: passed")
        print("numbered lines, cited range, wrap, sticky headers, image dimensions and reduced motion: passed")
        assert not errors, errors
        context.close()
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
