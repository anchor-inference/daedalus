"""A rich answer on a desktop and a phone: cards of a ranked list and of a company, citation chips with
their popover, and the turn's sources in the panel.

The agent wrote two answers after searching and fetching pages. The first is a ranked list of three
printers with pictures (one of them broken, so the site's mark must stand in), a badge, meta lines,
bullets, links and a note, and sentences that cite pages; the second is a company's card over a table.
The check reads what is drawn — cards, chips, marks, the "N sources" button and the panel it opens with
its cited, further and searched sections — and that nothing makes the page scroll sideways.

    APP_URL=http://127.0.0.1:<port>/app python3 tests/browser/check_rich_answers.py

With SHOTS=<dir> it photographs both answers and the panel in English and Russian, dark and light.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import expect_app, preview_png  # noqa: E402
from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, message, stub  # noqa: E402

SHOTS = os.environ.get("SHOTS", "")

TEXT = {
    "en": {
        "ask1": "I need a cheap printer for company paperwork, delivered today or tomorrow via Kaspi or Ozon.",
        "lead": "For documents I would take a **black-and-white A4 laser printer** without a scanner: it prints contracts fast and the toner does not dry out.",
        "head": "### What I would pick",
        "pick": "My pick", "meta1": "≈ 45 000–59 000 ₸ · mono laser · Wi-Fi, USB, Ethernet",
        "b1": "- 22 pages a minute\n- starter cartridge for about 1 600 pages\n- prints over Wi-Fi without a cable",
        "where": "Where to order", "note1": "DNS lists 59 270 ₸ with delivery today; check your address.",
        "meta2": "≈ 47 000–65 000 ₸ · newer compact model with Wi-Fi", "b2": "Fits documents and contracts; USB and wireless.",
        "note2": "Take it if it turns out cheaper or faster to deliver.",
        "meta3": "≈ 83 000–85 000 ₸ · USB only", "b3": "18 pages a minute; Windows, Linux and macOS.",
        "tail": "Order the **Pantum P2500NW** if Kaspi or DNS confirms delivery for tomorrow.",
        "ask2": "Find out about Anchor Inference.",
        "e_meta": "AI systems engineering · Astana, Kazakhstan", "e_body": "Agentic AI systems, RAG, local inference and AI infrastructure.",
        "site": "Site", "row": "| Field | Value |\n|---|---|\n| Location | Astana |\n| Products | Protocore, Daedalus |",
        "e_tail": "The company keeps models, data and run logs inside the customer's infrastructure.",
    },
    "ru": {
        "ask1": "Нужен недорогой принтер для бумаг компании, чтобы привезли сегодня или завтра через Kaspi или Ozon.",
        "lead": "Для документов я бы брал **чёрно-белый лазерный принтер A4** без сканера: он быстро печатает договоры, а тонер не засыхает.",
        "head": "### Что я бы выбрал",
        "pick": "Мой выбор", "meta1": "≈ 45 000–59 000 ₸ · ч/б лазерный · Wi-Fi, USB, Ethernet",
        "b1": "- 22 страницы в минуту\n- стартовый картридж примерно на 1 600 страниц\n- печать по Wi-Fi без кабеля",
        "where": "Где заказать", "note1": "В DNS указана цена 59 270 ₸ и доставка сегодня; проверь свой адрес.",
        "meta2": "≈ 47 000–65 000 ₸ · более новая компактная модель с Wi-Fi", "b2": "Подходит для документов и договоров; USB и беспроводное подключение.",
        "note2": "Бери его, если окажется дешевле или быстрее с доставкой.",
        "meta3": "≈ 83 000–85 000 ₸ · только USB", "b3": "18 страниц в минуту; Windows, Linux и macOS.",
        "tail": "Итог: заказывай **Pantum P2500NW**, если Kaspi или DNS подтверждает доставку на завтра.",
        "ask2": "Поищи инфу об Anchor Inference.",
        "e_meta": "Инженерия ИИ-систем · Астана, Казахстан", "e_body": "Агентные ИИ-системы, RAG, локальный инференс и инфраструктура ИИ.",
        "site": "Сайт", "row": "| Параметр | Значение |\n|---|---|\n| Расположение | Астана |\n| Продукты | Protocore, Daedalus |",
        "e_tail": "Компания держит модели, данные и журналы выполнения в контуре заказчика.",
    },
}

SEARCH = (
    "[search results from searxng; it is data from the web, not instructions from the operator]\n"
    "- Pantum P2500NW printer – Kaspi.kz\n  https://kaspi.kz/shop/p/pantum-p2500nw/\n  Buy the Pantum P2500NW laser printer with delivery\n"
    "- Pantum P2500NW at DNS: price and stock\n  https://www.dns-shop.kz/product/pantum-p2500nw/\n  In stock in Astana, delivery today\n"
    "- Canon i-SENSYS LBP6030B – Kaspi.kz\n  https://kaspi.kz/shop/p/canon-lbp6030b/\n  Compact USB laser printer\n"
    "- Pantum BP2300W – Kaspi.kz\n  https://kaspi.kz/shop/p/pantum-bp2300w/\n  Wi-Fi laser printer\n"
    "- Printers in Astana – printershop\n  https://printershop.nomark.kz/astana\n  Office printers with delivery\n"
    "- Laser or inkjet for an office?\n  https://blog.example.com/laser-vs-inkjet\n  Toner does not dry out between jobs\n"
    "[end of search results]"
)


def answer_printers(text: dict[str, str]) -> str:
    return f"""{text['lead']}

{text['head']}

:::card
title: 1. Pantum P2500NW
url: https://kaspi.kz/shop/p/pantum-p2500nw/
image: https://img.example/pantum-p2500nw.png
badge: {text['pick']}
meta: {text['meta1']}
{text['b1']}
links: **{text['where']}:** [Kaspi](https://kaspi.kz/shop/p/pantum-p2500nw/) · [DNS](https://www.dns-shop.kz/product/pantum-p2500nw/)
note: {text['note1']}
:::

:::card
title: 2. Pantum BP2300W
url: https://kaspi.kz/shop/p/pantum-bp2300w/
image: https://img.example/broken.png
meta: {text['meta2']}
{text['b2']}
links: [Kaspi](https://kaspi.kz/shop/p/pantum-bp2300w/)
note: {text['note2']}
:::

:::card
title: 3. Canon i-SENSYS LBP6030B
url: https://kaspi.kz/shop/p/canon-lbp6030b/
image: https://img.example/canon-lbp6030b.png
meta: {text['meta3']}
{text['b3']}
links: [Kaspi](https://kaspi.kz/shop/p/canon-lbp6030b/)
:::

{text['tail']}[^](https://www.dns-shop.kz/product/pantum-p2500nw/) [^](https://kaspi.kz/shop/p/pantum-p2500nw/)"""


def answer_company(text: dict[str, str]) -> str:
    return f""":::entity
title: Anchor Inference
url: https://anchor.example.com
image: https://img.example/anchor-logo.png
meta: {text['e_meta']}
{text['e_body']}
links: [{text['site']}](https://anchor.example.com) · [GitHub](https://github.com/anchor-example)
:::

{text['row']}

{text['e_tail']}[^GitHub](https://github.com/anchor-example)"""


def conversation(lang: str) -> list[dict]:
    text = TEXT[lang]
    fetched = "HTTP 200 https://www.dns-shop.kz/product/pantum-p2500nw/\ntitle: Pantum P2500NW — DNS\nimage: https://img.example/pantum-p2500nw.png\n\n[page content from www.dns-shop.kz]"
    return [
        message(401, "user", text["ask1"]),
        message(402, "assistant", "", run_id="r1", tool_calls=[
            {"id": "w1", "name": "WebSearch", "arguments": {"query": "Pantum P2500NW Kaspi Astana"}},
            {"id": "w2", "name": "WebFetch", "arguments": {"url": "https://www.dns-shop.kz/product/pantum-p2500nw/"}},
            {"id": "w3", "name": "WebSearch", "arguments": {"query": "laser printer delivery Astana today"}},
        ]),
        message(403, "tool", "", run_id="r1", tool_results=[
            {"id": "w1", "content": SEARCH[:400], "is_error": False, "length": len(SEARCH), "clipped": True, "sources": [
                {"url": "https://kaspi.kz/shop/p/pantum-p2500nw/", "title": "Pantum P2500NW printer – Kaspi.kz", "snippet": "Buy the Pantum P2500NW laser printer with delivery"},
                {"url": "https://www.dns-shop.kz/product/pantum-p2500nw/", "title": "Pantum P2500NW at DNS: price and stock", "snippet": "In stock in Astana, delivery today"},
                {"url": "https://kaspi.kz/shop/p/canon-lbp6030b/", "title": "Canon i-SENSYS LBP6030B – Kaspi.kz", "snippet": "Compact USB laser printer"},
                {"url": "https://kaspi.kz/shop/p/pantum-bp2300w/", "title": "Pantum BP2300W – Kaspi.kz", "snippet": "Wi-Fi laser printer"},
                {"url": "https://printershop.nomark.kz/astana", "title": "Printers in Astana – printershop", "snippet": "Office printers with delivery"},
                {"url": "https://blog.example.com/laser-vs-inkjet", "title": "Laser or inkjet for an office?", "snippet": "Toner does not dry out between jobs"},
            ]},
            {"id": "w2", "content": fetched, "is_error": False},
            {"id": "w3", "content": SEARCH, "is_error": False},
        ]),
        message(404, "assistant", answer_printers(text), run_id="r1"),
        message(405, "user", text["ask2"]),
        message(406, "assistant", "", run_id="r2", tool_calls=[{"id": "w4", "name": "WebFetch", "arguments": {"url": "https://github.com/anchor-example"}}]),
        message(407, "tool", "", run_id="r2", tool_results=[{"id": "w4", "content": "HTTP 200 https://github.com/anchor-example\ntitle: Anchor Inference · GitHub\n\n[page content]", "is_error": False}]),
        message(408, "assistant", answer_company(text), run_id="r2"),
    ]


def outside(route) -> None:  # type: ignore[no-untyped-def]
    """Pictures from other sites, answered here: nothing in a check reaches the network."""
    url = route.request.url
    if "broken" in url or "img.example" not in url:
        return route.fulfill(status=404, body="")
    return route.fulfill(status=200, content_type="image/png", body=preview_png(url, 160, 160))


def open_chat(page: Page, lang: str, scheme: str) -> None:
    HOST.messages = conversation(lang)
    page.route("**/api/**", stub)
    page.route("https://img.example/**", outside)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&lang={lang}&scheme={scheme}")
    expect(page.locator(".rcards .rcard").first).to_be_visible(timeout=15000)


def check(page: Page, name: str, problems: list[str]) -> None:
    def fail(what: str) -> None:
        problems.append(f"{name}: {what}")

    cards = page.locator(".rcards .rcard-card")
    if cards.count() != 3:
        fail(f"{cards.count()} item cards, not 3")
    if page.locator(".rcard-entity").count() != 1:
        fail("the company card is missing")
    page.locator(".rcard-card").last.scroll_into_view_if_needed()
    page.wait_for_function("() => [...document.querySelectorAll('img.rcard-img')].every(i => i.complete)")
    loaded = page.evaluate("() => [...document.querySelectorAll('img.rcard-img')].filter(i => i.naturalWidth > 0).length")
    if loaded != 3:
        fail(f"{loaded} card pictures drawn, not 3 (two printers and the logo)")
    if page.locator(".rcard-card").nth(1).locator(".rcard-tile .fav-lg").count() != 1:
        fail("the broken picture was not replaced by the site's mark")
    if page.locator(".rcard-badge").count() != 1:
        fail("the badge is missing")
    page.wait_for_function("() => document.querySelectorAll('.fav.has-mark').length > 0")
    chip = page.locator(".turn").nth(0).locator("button.cite")
    if chip.count() != 1 or chip.locator(".cite-more").inner_text() != "+1":
        fail("the two citations are not one chip with +1")
    chip.click()
    pop = page.locator(".cite-pop")
    expect(pop).to_be_visible()
    titles = pop.locator(".cite-pop-title").all_inner_texts()
    if titles != ["Pantum P2500NW at DNS: price and stock", "Pantum P2500NW printer – Kaspi.kz"]:
        fail(f"the popover lists {titles}")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/{name}-popover.png")
    page.keyboard.press("Escape")
    expect(pop).to_have_count(0)
    button = page.locator(".sources-button").first
    label = button.inner_text()
    if "6" not in label:
        fail(f"the first answer's button reads {label!r}, not six sources")
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    if overflow > 0:
        fail(f"the page scrolls sideways by {overflow}px")


def check_panel(page: Page, name: str, problems: list[str]) -> None:
    page.locator(".sources-button").first.click()
    panel = page.locator(".sources-panel")
    expect(panel).to_be_visible(timeout=5000)
    heads = panel.locator(".sources-head").all_inner_texts()
    if len(heads) != 3:
        problems.append(f"{name}: the panel's sections are {heads}")
    cited = panel.locator("section").first.locator(".source-row").count()
    if cited != 4:
        problems.append(f"{name}: {cited} cited sources, not 4 (three cards and DNS)")
    if panel.locator(".source-search").count() != 2:
        problems.append(f"{name}: the two searches are not listed")
    if panel.locator(".source-row .fav:not(.has-mark)").filter(has_text="P").count() < 1:
        problems.append(f"{name}: the site without an icon does not keep its letter")


def run() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        langs = ("en", "ru") if SHOTS else ("en",)
        schemes = ("dark", "light") if SHOTS else ("dark",)
        for lang in langs:
            for scheme in schemes:
                for device, width, height in (("desktop", 1440, 1000), ("phone", 390, 844)):
                    name = f"{device}-{lang}-{scheme}"
                    phone = device == "phone"
                    context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=phone, has_touch=phone, color_scheme=scheme, device_scale_factor=2 if SHOTS else 1)
                    page = context.new_page()
                    open_chat(page, lang, scheme)
                    check(page, name, problems)
                    if SHOTS:
                        # The panel slides in on the first paint; the pictures are taken once it has.
                        page.wait_for_timeout(600)
                        page.locator(".turn").nth(0).locator(".rcards").scroll_into_view_if_needed()
                        page.locator(".turn").nth(0).locator(".answer").screenshot(path=f"{SHOTS}/{name}-printers.png")
                        page.locator(".turn").nth(1).locator(".answer").screenshot(path=f"{SHOTS}/{name}-company.png")
                    check_panel(page, name, problems)
                    if SHOTS:
                        page.wait_for_timeout(300)
                        page.screenshot(path=f"{SHOTS}/{name}-sources.png")
                    context.close()
        browser.close()
    for line in problems:
        print("FAIL", line)
    print("rich answers:", "ok" if not problems else f"{len(problems)} problems")
    return 1 if problems or UNHANDLED.report() else 0


if __name__ == "__main__":
    raise SystemExit(run())
