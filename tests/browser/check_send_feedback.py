"""A slow accepted send stays visible in the chat before the host answers."""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = os.environ.get("APP_URL", shots.BASE)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
WORDS = "A message whose acknowledgement takes several seconds"


def run() -> None:
    expect_app(BASE)
    sent: list[str] = []

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = urlsplit(request.url).path
        if path == "/api/diagrams" and "session_id=" in urlsplit(request.url).query:
            return route.fulfill(status=200, content_type="application/json", body='[{"id":"0123456789abcdef0123456789abcdef","title":"Flow"}]')
        if path == f"/api/sessions/{shots.S1}/messages" and request.method == "POST":
            sent.append(request.post_data_json["text"])
            return route.fulfill(status=200, content_type="application/json", body='{"run_id":"slow-run"}')
        if path == f"/api/sessions/{shots.S1}" and request.method == "GET":
            page = deepcopy(shots.detail(shots.S1))
            if sent:
                page["messages"].append({"role": "user", "seq": 999999, "text": sent[0], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": datetime.now(UTC).isoformat()})
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(page))
        return shots.stub(route)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.add_init_script("""(() => {
          const original = window.fetch.bind(window);
          window.fetch = (input, init) => {
            if (String(input).includes('/messages') && init?.method === 'POST')
              return new Promise(resolve => setTimeout(() => resolve(original(input, init)), 4000));
            return original(input, init);
          };
        })()""")
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/agents/{shots.S1}?token=t&lang=en")
        expect(page.locator(".chat-title")).to_be_visible()
        expect(page.get_by_role("link", name="Session diagrams (1)")).to_be_visible()
        page.locator(".composer textarea").fill(WORDS)
        page.locator(".composer [data-action=send]").click()
        expect(page.locator(".timeline .msg.user").last).to_contain_text(WORDS, timeout=1000)
        expect(page.locator(".timeline .msg.user").last).to_be_in_viewport(timeout=1000)
        assert not sent, "the temporary bubble should precede the delayed request"
        page.wait_for_timeout(4600)
        assert sent == [WORDS]
        expect(page.locator(".timeline .msg.user").filter(has_text=WORDS)).to_have_count(1, timeout=8000)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.get_by_role("link", name="Session diagrams (1)")).to_be_in_viewport()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if page.get_by_role("dialog", name="Project files panel").is_visible():
            page.keyboard.press("Escape")
        page.get_by_role("link", name="Session diagrams (1)").click()
        expect(page.locator(".diagrams-screen")).to_contain_text("Flow")
        browser.close()
    assert not shots.UNHANDLED.report()


if __name__ == "__main__":
    run()
