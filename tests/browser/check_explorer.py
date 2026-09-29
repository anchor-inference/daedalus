"""Exercise the lazy explorer, both search routes and its fallback on older servers."""
from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit

from api_stub import DEFAULT_APP, expect_app
from playwright.sync_api import expect, sync_playwright
from screenshots import S1, UNHANDLED, detail, respond, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


DOCS = {"id": "f-docs", "path": "/home/operator/work/bakery-docs", "label": "Docs", "env": "container", "readonly": True, "writable": False}
HOME = {"id": "f-bakery", "path": "/home/operator/work/bakery", "label": "", "env": "container", "readonly": False, "writable": True}


def folders(browser, width: int) -> None:  # type: ignore[no-untyped-def]
    """A session whose project has two folders: the pane switches to the read-only one and back."""
    phone = width < 600
    context = browser.new_context(viewport={"width": width, "height": 844 if phone else 900}, is_mobile=phone, has_touch=phone)
    page = context.new_page()
    requests: list[str] = []

    def route(r):  # type: ignore[no-untyped-def]
        url = urlsplit(r.request.url)
        requests.append(url.path + "?" + url.query)
        if url.path == f"/api/sessions/{S1}":
            return respond(r, detail(S1) | {"folder_id": HOME["id"], "folders": [HOME, DOCS]})
        if url.path == f"/api/sessions/{S1}/folders/f-docs/files":
            return respond(r, {"path": "", "kind": "dir", "entries": [{"name": "guide.md", "dir": False, "size": 42, "mtime": 0}]})
        if url.path == f"/api/sessions/{S1}/folders/f-docs/download":
            return respond(r, "# The guide\n\nHow the bakery site is put together.\n", content_type="text/markdown")
        return stub(r)

    page.route("**/api/**", route)
    page.goto(f"{BASE}/agents/{S1}?token=t&lang=en&panel=files")
    # On a phone the route opens the same pane as a full-height sheet.
    switcher = page.get_by_role("combobox", name="Folder", exact=True)
    expect(switcher).to_be_visible()
    tree = page.locator(".explorer-tree")
    expect(tree.locator('[data-path="src"]')).to_be_visible()
    expect(page.get_by_role("button", name="upload", exact=True)).to_be_visible()
    switcher.select_option("f-docs")
    expect(tree.locator('[data-path="guide.md"]')).to_be_visible()
    expect(tree.locator('[data-path="src"]')).to_have_count(0)
    assert any(r.startswith(f"/api/sessions/{S1}/folders/f-docs/files") for r in requests), requests
    lock = page.locator(".explorer-folders .chip", has_text="read-only")
    expect(lock).to_be_visible()
    expect(lock.locator("svg")).to_have_count(1)
    expect(page.locator(".explorer-folders .chip", has_text="container")).to_be_visible()
    # A folder the session may not write offers no upload.
    expect(page.get_by_role("button", name="upload", exact=True)).to_have_count(0)
    expect(page.locator(".explorer-crumbs .crumb").first).to_have_text("Docs")
    tree.locator('[data-path="guide.md"]').click()
    expect(page.locator(".preview-doc")).to_contain_text("How the bakery site is put together")
    assert any(r.startswith(f"/api/sessions/{S1}/folders/f-docs/download") for r in requests), requests
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"the page scrolls sideways by {overflow}px at {width}px"
    page.locator('[data-tab="files"]').click()
    switcher = page.get_by_role("combobox", name="Folder", exact=True)
    switcher.select_option("f-bakery")
    expect(tree.locator('[data-path="src"]')).to_be_visible()
    expect(page.locator(".explorer-folders .chip", has_text="read-only")).to_have_count(0)
    print(f"{width}px: the pane switched to the read-only folder, read and previewed it, offered no upload, and switched back")
    context.close()


def run() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, permissions=["clipboard-read", "clipboard-write"])
        context.add_init_script("""(() => {
          const original = window.fetch;
          window.fetch = (url, opts) => {
            if (String(url).endsWith('/stream')) {
              return Promise.resolve(new Response(new ReadableStream({ start(controller) {
                window.settleRun = () => controller.enqueue(new TextEncoder().encode('event: run_settled\\ndata: {"status":"idle"}\\n\\n'));
                opts?.signal?.addEventListener('abort', () => controller.close());
              } }), { headers: { 'Content-Type': 'text/event-stream' } }));
            }
            return original(url, opts);
          };
        })()""")
        page = context.new_page()
        requests = []
        status = {"search": 200, "grep": 200}

        def route(r):
            url = urlsplit(r.request.url)
            requests.append(url.path + "?" + url.query)
            mode = url.path.rsplit("/", 1)[-1]
            if mode in status and status[mode] != 200:
                return respond(r, {"detail": "Search unavailable"}, status=status[mode])
            return stub(r)

        page.route("**/api/**", route)
        page.goto(f"{BASE}/agents/{S1}?token=t&lang=en&panel=files")
        tree = page.locator(".explorer-tree")
        src = tree.locator('[data-path="src"]')
        expect(src).to_be_visible()
        assert not any("path=src" in r for r in requests), "children were fetched before expansion"
        src.click()
        child = tree.locator('[data-path="src/main.py"]')
        expect(child).to_be_visible()
        assert sum("files?path=src" in r for r in requests) == 1
        src.click()
        expect(child).to_have_count(0)
        src.click()
        expect(child).to_be_visible()
        assert sum("files?path=src" in r for r in requests) == 1, "cached folder was downloaded again"
        before_refresh = sum("files?path=src" in r for r in requests)
        page.evaluate("window.settleRun()")
        page.wait_for_timeout(500)
        assert sum("files?path=src" in r for r in requests) > before_refresh, "settling did not refresh an open folder"
        tree.locator('[data-path="data"]').click()
        expect(tree.locator('[data-path="data/menu.json"] .written-badge')).to_be_visible()
        height = src.evaluate("e => e.getBoundingClientRect().height")
        assert height == 32, height
        assert src.evaluate("e => getComputedStyle(e).borderTopWidth") == "0px"
        src.focus()
        page.keyboard.press("ArrowRight")
        expect(tree.locator('[data-path="src/helper.ts"]')).to_be_focused()
        page.keyboard.press("ArrowDown")
        expect(child).to_be_focused()
        page.keyboard.press("Backspace")
        expect(src).to_be_focused()
        page.keyboard.press("ArrowLeft")
        expect(child).to_have_count(0)
        page.keyboard.press("n")
        expect(tree.locator('[data-path="NOTES.md"]')).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator(".preview-doc")).to_be_visible()
        page.locator('[data-tab="files"]').click()
        page.get_by_role("switch", name="Show hidden and ignored", exact=True).click()
        assert "dim" in tree.locator('[data-path=".env"]').get_attribute("class")
        expect(tree.locator('[data-path="node_modules"]')).to_be_visible()
        page.get_by_role("switch", name="Show hidden and ignored", exact=True).click()
        expect(tree.locator('[data-path=".env"]')).to_have_count(0)
        search = page.get_by_role("searchbox", name="Search files…")
        search.fill("helper")
        expect(tree.locator('[data-path="src/helper.ts"]')).to_be_visible()
        assert any("files/search?q=helper" in r for r in requests)
        page.get_by_label("Search mode", exact=True).select_option("content")
        search.fill("return")
        hit = tree.locator('[data-path="src/main.py"]')
        expect(hit.locator(".grep-snippet")).to_contain_text("return")
        hit.click()
        expect(page.locator('.source-view .cited[data-line="5"]')).to_be_visible()
        assert "lines=5" in page.url
        page.locator('[data-tab="files"]').click()
        status["grep"] = 501
        search.fill("unavailable")
        expect(page.locator(".explorer-note")).to_contain_text("ripgrep")
        status["grep"] = 404
        search.fill("main")
        expect(page.locator(".explorer-note")).to_contain_text("matching names")
        expect(tree.locator('[data-path="src/main.py"]')).to_be_visible()
        status["search"] = 404
        page.get_by_label("Search mode", exact=True).select_option("name")
        search.fill("helper")
        expect(page.locator(".explorer-note")).to_contain_text("loaded folders only")
        expect(tree.locator('[data-path="src/helper.ts"]')).to_be_visible()
        search.fill("")
        note = tree.locator('[data-path="NOTES.md"]')
        note.click(button="right")
        page.get_by_role("menuitem", name="Copy path").click()
        assert page.evaluate("navigator.clipboard.readText()") == "NOTES.md"
        note.click(button="right")
        with page.expect_download() as downloaded:
            page.get_by_role("menuitem", name="Download", exact=True).click()
        assert downloaded.value.suggested_filename
        note.click(button="right")
        with page.expect_popup() as opened:
            page.get_by_role("menuitem", name="Open in a new tab").click()
        opened.value.close()
        note.click(button="right")
        page.get_by_role("menuitem", name="preview", exact=True).click()
        expect(page.locator(".preview-doc")).to_be_visible()
        page.locator('[data-tab="details"]').click()
        assert page.locator(".details > .dt-section").evaluate_all("els => els.every(e => e.tagName === 'DETAILS')")
        provider = page.locator("[id$='-info-provider']")
        provider.locator("summary").click()
        expect(provider).not_to_have_attribute("open", "")
        session = page.locator("[id$='-info-session']")
        session.locator("summary").click()
        expect(session).not_to_have_attribute("open", "")
        page.reload()
        expect(page.locator("[id$='-info-session']")).not_to_have_attribute("open", "")
        expect(page.locator("[id$='-info-provider']")).not_to_have_attribute("open", "")
        page.locator("[id$='-info-cron']").scroll_into_view_if_needed()
        expect(page.locator("[id$='-info-cron'] summary")).to_be_in_viewport()
        print(f"tree row: {height}px; lazy reads, keyboard, search, fallbacks, context actions and disclosures: passed")
        context.close()
        context = browser.new_context(viewport={"width": 2560, "height": 1400})
        page = context.new_page()
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/agents/{S1}?token=t&lang=en&panel=preview&path=NOTES.md")
        expect(page.locator(".panel-body.split")).to_be_visible()
        handle = page.locator(".panel-files .pane-handle")
        box = handle.bounding_box()
        assert box
        before = page.locator(".panel-files").evaluate("e => e.getBoundingClientRect().width")
        page.mouse.move(box["x"] + 2, box["y"] + 100)
        page.mouse.down()
        page.mouse.move(box["x"] + 82, box["y"] + 100, steps=6)
        page.mouse.up()
        after = page.locator(".panel-files").evaluate("e => e.getBoundingClientRect().width")
        assert after > before + 60, (before, after)
        page.reload()
        expect(page.locator(".panel-body.split")).to_be_visible()
        assert abs(page.locator(".panel-files").evaluate("e => e.getBoundingClientRect().width") - after) < 2
        print(f"tree width: {before} → {after}, restored after reload")
        context.close()
        for width in (1440, 390):
            folders(browser, width)
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
