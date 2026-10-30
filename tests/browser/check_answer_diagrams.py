"""Diagrams in an answer on a desktop and a phone: a flow panel and mermaid fences.

The agent answered twice. The first answer is a `:::flow` panel with a header, icons, a highlighted
node, a row of two nodes joined by an arrow, a labelled arrow and a caption; the second holds a
mermaid flowchart that must be drawn as a real diagram in the app's colours, and a broken fence that
must stay code with a note. The check reads what is drawn — the panel's parts, a phone stacking the
row, the drawing, the code toggle, the large view, the error note, the drawing's colours following the
scheme — and that nothing makes the page scroll sideways.

    APP_URL=http://127.0.0.1:<port>/app python3 tests/browser/check_answer_diagrams.py

With SHOTS=<dir> it photographs both answers in English and Russian, dark and light.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import expect_app  # noqa: E402
from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, message, stub  # noqa: E402

SHOTS = os.environ.get("SHOTS", "")

TEXT = {
    "en": {
        "ask1": "Sketch a small pilot for a support bot.",
        "lead": "Start with one channel and measure how many questions the assistant closes on its own:",
        "title": "Concept of a small pilot project",
        "n1": "💬 Customer — writes in Telegram or on the site",
        "n2": "🤖 ==AI assistant== — answers from the knowledge base",
        "step": "↓ cannot answer",
        "n3": "✅ Solved — about 80% of questions → 🎧 Operator — takes the rest",
        "note": "Two weeks on one channel, then measure the share answered.",
        "tail": "If the share stays above 70%, add the second channel.",
        "ask2": "And how does the hand-over to an operator work?",
        "lead2": "Like this:",
        "chart": 'flowchart TD\n  Q["Question"] --> B{"Bot knows?"}\n  B -->|yes| A["Answer"]\n  B -->|no| O["Operator"]\n  O --> K["Add to knowledge base"]\n  K --> B',
        "tail2": "The second sketch below has a typo, so it stays code.",
    },
    "ru": {
        "ask1": "Набросай небольшой пилот бота поддержки.",
        "lead": "Начни с одного канала и измерь, сколько вопросов ассистент закрывает сам:",
        "title": "Концепция небольшого пилотного проекта",
        "n1": "💬 Клиент — пишет в Telegram или на сайте",
        "n2": "🤖 ==ИИ-ассистент== — отвечает по базе знаний",
        "step": "↓ не знает ответа",
        "n3": "✅ Решено — около 80% вопросов → 🎧 Оператор — берёт остальное",
        "note": "Две недели на одном канале, затем измерить долю отвеченных.",
        "tail": "Если доля держится выше 70%, подключай второй канал.",
        "ask2": "А как устроена передача оператору?",
        "lead2": "Вот так:",
        "chart": 'flowchart TD\n  Q["Вопрос"] --> B{"Бот знает?"}\n  B -->|да| A["Ответ"]\n  B -->|нет| O["Оператор"]\n  O --> K["Дополнить базу знаний"]\n  K --> B',
        "tail2": "Во втором наброске опечатка, поэтому он остаётся кодом.",
    },
}

BROKEN = "flowchart TD\n  A --> B[unclosed"


def answer_flow(text: dict[str, str]) -> str:
    return f"""{text['lead']}

:::flow 💡 {text['title']}
{text['n1']}
↓
{text['n2']}
{text['step']}
{text['n3']}
note: {text['note']}
:::

{text['tail']}"""


def answer_mermaid(text: dict[str, str]) -> str:
    return f"""{text['lead2']}

```mermaid
{text['chart']}
```

{text['tail2']}

```mermaid
{BROKEN}
```"""


def conversation(lang: str) -> list[dict]:
    text = TEXT[lang]
    return [
        message(501, "user", text["ask1"]),
        message(502, "assistant", answer_flow(text), run_id="r1"),
        message(503, "user", text["ask2"]),
        message(504, "assistant", answer_mermaid(text), run_id="r2"),
    ]


def open_chat(page: Page, lang: str, scheme: str) -> None:
    HOST.messages = conversation(lang)
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&lang={lang}&scheme={scheme}")
    expect(page.locator(".flow").first).to_be_visible(timeout=15000)


def check_flow(page: Page, name: str, phone: bool, problems: list[str]) -> None:
    def fail(what: str) -> None:
        problems.append(f"{name}: {what}")

    flow = page.locator(".flow")
    if flow.count() != 1:
        fail(f"{flow.count()} flow panels, not 1")
    if flow.locator(".flow-node").count() != 4:
        fail(f"{flow.locator('.flow-node').count()} nodes, not 4")
    if flow.locator(".flow-node.is-mark").count() != 1:
        fail("the highlighted node is missing")
    if flow.locator(".flow-head .ic-bulb").count() != 1:
        fail("the header has no icon")
    if flow.locator(".flow-icon .ic-chat, .flow-icon .ic-bots, .flow-icon.tone-ok .ic-checkcircle, .flow-icon .ic-headset").count() != 4:
        fail("the nodes' emoji are not drawn as the app's line icons")
    if flow.locator(".flow-step-label").count() != 1 or flow.locator(".flow-arrow").count() != 2:
        fail("the arrows between rows are not two, one of them labelled")
    if flow.locator(".flow-note").count() != 1:
        fail("the caption is missing")
    if ":::" in flow.inner_text() or "==" in flow.inner_text():
        fail("the panel shows its markup")
    direction = flow.locator(".flow-row[data-n='2']").evaluate("row => getComputedStyle(row).flexDirection")
    if direction != ("column" if phone else "row"):
        fail(f"the row of two nodes runs as a {direction} on a {'phone' if phone else 'desktop'}")
    widths = flow.locator(".flow-node").evaluate_all("nodes => nodes.map(n => n.scrollWidth - n.clientWidth)")
    if any(width > 1 for width in widths):
        fail(f"a node's text overflows it: {widths}")
    # A node is as tall as its icon and two lines; a phone once stretched every lone node to 320 px.
    heights = flow.locator(".flow-node").evaluate_all("nodes => nodes.map(n => Math.round(n.getBoundingClientRect().height))")
    if any(height > 130 for height in heights):
        fail(f"a node is taller than its content: {heights}")


def check_mermaid(page: Page, name: str, scheme: str, problems: list[str]) -> None:
    def fail(what: str) -> None:
        problems.append(f"{name}: {what}")

    cards = page.locator(".mermaid-card")
    cards.first.scroll_into_view_if_needed()
    drawn = cards.nth(0)
    try:
        expect(drawn).to_have_attribute("data-mermaid", "done", timeout=20000)
    except AssertionError:
        fail(f"the diagram was not drawn (state {drawn.get_attribute('data-mermaid')})")
        return
    svg = drawn.locator(".mermaid-view svg")
    expect(svg).to_be_visible()
    if svg.locator("g.node").count() < 5:
        fail(f"the drawing has {svg.locator('g.node').count()} nodes, not 5")
    if drawn.locator("pre").is_visible():
        fail("the code shows under the drawing")
    fill = svg.locator("g.node rect, g.node polygon").first.evaluate("shape => getComputedStyle(shape).fill")
    channels = [int(part) for part in fill.replace("rgb(", "").replace("rgba(", "").replace(")", "").split(",")[:3]]
    light = sum(channels) / 3 > 128
    if light != (scheme == "light"):
        fail(f"a node is filled {fill} in the {scheme} scheme")
    broken = cards.nth(1)
    expect(broken).to_have_attribute("data-mermaid", "error", timeout=20000)
    if not broken.locator(".mermaid-note").is_visible() or not broken.locator("pre").is_visible():
        fail("the broken fence does not show its code with a note")
    if page.locator("body > [id^='danswer-diagram'], body > [id^='answer-diagram']").count():
        fail("a render left its scratch element in the page")
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    if overflow > 0:
        fail(f"the page scrolls sideways by {overflow}px")


def check_controls(page: Page, name: str, problems: list[str]) -> None:
    drawn = page.locator(".mermaid-card").nth(0)
    drawn.locator("button[data-mermaid-toggle]").click()
    if not drawn.locator("pre").is_visible() or drawn.locator(".mermaid-view").is_visible():
        problems.append(f"{name}: the toggle does not show the code")
    drawn.locator("button[data-mermaid-toggle]").click()
    drawn.locator("button[data-mermaid-open]").click()
    zoom = page.locator(".diagram-zoom")
    expect(zoom.locator(".diagram-zoom-body svg")).to_be_visible(timeout=5000)
    box = zoom.locator(".diagram-zoom-body svg").bounding_box()
    viewport = page.viewport_size or {"width": 0, "height": 0}
    if not box or box["width"] < viewport["width"] * 0.6:
        problems.append(f"{name}: the large view is {box and round(box['width'])}px wide")
    page.keyboard.press("Escape")
    expect(zoom).to_have_count(0)


def run() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        langs = ("en", "ru") if SHOTS else ("en",)
        for lang in langs:
            for scheme in ("dark", "light"):
                # A tall phone for the pictures: the answer then fits between the fixed bar and the
                # composer, which would otherwise be photographed over it.
                for device, width, height in (("desktop", 1440, 1000), ("phone", 390, 1700 if SHOTS else 844)):
                    name = f"{device}-{lang}-{scheme}"
                    phone = device == "phone"
                    context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=phone, has_touch=phone, color_scheme=scheme, device_scale_factor=2 if SHOTS else 1)
                    page = context.new_page()
                    open_chat(page, lang, scheme)
                    check_flow(page, name, phone, problems)
                    if SHOTS:
                        page.locator(".turn").nth(0).locator(".answer").screenshot(path=f"{SHOTS}/{name}-flow.png")
                    check_mermaid(page, name, scheme, problems)
                    if SHOTS:
                        page.wait_for_timeout(200)
                        page.locator(".turn").nth(1).locator(".answer").screenshot(path=f"{SHOTS}/{name}-mermaid.png")
                    check_controls(page, name, problems)
                    context.close()
        browser.close()
    for line in problems:
        print("FAIL", line)
    print("answer diagrams:", "ok" if not problems else f"{len(problems)} problems")
    return 1 if problems or UNHANDLED.report() else 0


if __name__ == "__main__":
    raise SystemExit(run())
