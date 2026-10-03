"""Is "Add a model" centred in the window, at every width a window is opened at?

The reported symptom was a desktop window 2000 px wide with the whole flow pushed right and an empty
column the width of the rail beside it. The cause is structural rather than cosmetic: past 1024 px
the shell is a grid whose first column belongs to the rail, and the first-run flow was drawn inside
that grid without a rail, so it sat in the second column with the first one empty. Nothing about the
flow's own stylesheet said so, which is why it survived a look at the code.

So the check measures rather than looks: the gutter on the left of the content and the gutter on its
right, at four widths, and the page's own horizontal scroll.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app OUT=/tmp/shots python3 tests/browser/check_add_model_layout.py

Exit 0 when the flow is centred and nothing spills sideways. One PNG per width lands in OUT.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import stub  # noqa: E402  the same invented installation the pictures are taken of

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
OUT = Path(os.environ.get("OUT", "/tmp/add-model-widths"))
WIDTHS = (320, 390, 400, 1440, 2000, 2560)
SLACK = 2.0
"""How far the two gutters may differ and still be one centred block: sub-pixel rounding, no more."""


def run() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    stub.fresh = True  # type: ignore[attr-defined]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for width in WIDTHS:
            context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme="dark")
            page = context.new_page()
            page.route("**/api/**", stub)
            page.goto(f"{BASE}/agents?token=t&scheme=dark")
            page.wait_for_selector(".addmodel", timeout=15000)
            page.wait_for_timeout(500)
            page.screenshot(path=str(OUT / f"add-model-{width}.png"))
            box = page.locator(".onboard").bounding_box()
            if not box:
                failures.append(f"{width}: the flow has no box at all")
                context.close()
                continue
            left = box["x"]
            right = width - (box["x"] + box["width"])
            # The flow scrolls inside `.gate.tall`, so an overflowing child grows *that* element's
            # scrollWidth and never the document's: asking the document would always answer no.
            scrolls = page.evaluate(
                """() => {
                    const doc = document.documentElement;
                    const gate = document.querySelector('.gate.tall') ?? doc;
                    return gate.scrollWidth > gate.clientWidth + 1 || doc.scrollWidth > doc.clientWidth + 1;
                }"""
            )
            print(f"{width:>5}px  content {box['width']:>7.1f}  gutters {left:>6.1f} / {right:>6.1f}  h-scroll {scrolls}")
            if abs(left - right) > SLACK:
                failures.append(f"{width}: gutters differ by {abs(left - right):.1f}px — the flow is not centred")
            if scrolls:
                failures.append(f"{width}: the page scrolls sideways")
            if box["width"] < min(width, 380) * 0.75:
                failures.append(f"{width}: the flow uses only {box['width']:.0f}px of it")
            # Every step card is drawn and none of them is a sliver: a flow that renders but collapses
            # at one width is the same defect wearing different clothes.
            steps = page.locator(".addmodel .step").count()
            if steps != 3:
                failures.append(f"{width}: {steps} step cards, not 3")
            if width <= 400:
                page.locator(".pickgrid .pick", has_text="OpenRouter").first.click()
                page.wait_for_selector(".modelgrid .pick", timeout=15000)
                page.locator(".modelgrid .pick").first.click()
                page.locator(".addmodel-pricing summary").click()
                expanded_scroll = page.evaluate(
                    "() => { const gate = document.querySelector('.gate.tall'); return gate.scrollWidth > gate.clientWidth + 1; }"
                )
                if expanded_scroll:
                    failures.append(f"{width}: the expanded price fields scroll sideways")
            context.close()
        browser.close()
    stub.fresh = False  # type: ignore[attr-defined]
    for line in failures:
        print("FAIL:", line)
    print("ok" if not failures else f"{len(failures)} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
