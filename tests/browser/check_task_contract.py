"""What a card was asked and how its acceptance went, on a project's board, at 390 and 1280 px, in both languages.

A finished card used to say only that it was done: whether anybody had checked it, and against what,
lived in the orchestrator's chat. Checked here: a card in review or done names its acceptance level
in a chip — handed in, checked, approved by the operator — without a row of its own; its sheet lists
each check with the member's evidence and the orchestrator's mark, and the card's requirements with
their kind, where they came from, the input's file, and for each member whether it confirmed, opened
the file, has not confirmed yet, or (a command-line member, whose tools the host cannot see) only
said so. A replaced requirement stays in the list, struck through. And a question the orchestrator
reworded in place says so, keeps the operator's draft under the same id, and drops only a chosen
option the new words no longer offer. Nothing scrolls sideways.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import CONTRACT_WORDS, DEFAULT_APP, FOCUS_WORDS, FocusStub, expect_app  # noqa: E402
from check_project_focus import PID, fits, serve  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {
        "done": "Done", "accepted": "checked", "handed_in": "handed in", "approved": "approved", "acceptance": "Acceptance", "requirements": "Requirements",
        "accepted.title": "Checked by the orchestrator", "handed_in.title": "Handed in, not checked yet", "met": "met", "noword": "No evidence given",
        "input": "input", "quality": "quality", "from.you": "from you", "from.answer": "from your answer [q4r8tz]", "from.rule": "from your rule #120",
        "from.orchestrator": "from the orchestrator", "replaced": "replaced", "confirmed": "Lev: confirmed", "opened": "Lev: file opened",
        "sent": "Lev: sent, not confirmed", "words": "Ira: confirmed in words", "updated": "updated",
    },
    "ru": {
        "done": "Готово", "accepted": "проверено", "handed_in": "сдано", "approved": "принято", "acceptance": "Приёмка", "requirements": "Требования",
        "accepted.title": "Проверено оркестратором", "handed_in.title": "Сдано, ещё не проверено", "met": "выполнено", "noword": "Подтверждения нет",
        "input": "исходные данные", "quality": "качество", "from.you": "от вас", "from.answer": "из вашего ответа [q4r8tz]", "from.rule": "из вашего правила #120",
        "from.orchestrator": "от оркестратора", "replaced": "заменено", "confirmed": "Lev: подтверждено", "opened": "Lev: файл открыт",
        "sent": "Lev: отправлено, не подтверждено", "words": "Ira: подтверждено на словах", "updated": "изменён",
    },
}


def no_sideways(page: Page, where: str) -> None:
    """The page, and the open sheet's own body, which scrolls on its own and could hide an overflow inside it."""
    fits(page, where)
    inner = page.evaluate("() => { const b = document.querySelector('.sheet-body'); return b ? b.scrollWidth - b.clientWidth : 0 }")
    assert inner <= 0, f"{where}: the sheet scrolls sideways by {inner}px"


def show_done(page: Page, width: int, words: dict) -> None:
    if width >= 1024:
        page.locator(".pboard-col.done .pboard-fold").click()
    else:
        page.locator(".pboard-chips .chip", has_text=words["done"]).tap()


def cards(page: Page, lang: str, width: int) -> None:
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    where = f"{lang} {width}"
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}")
    expect(page.locator(".pcard", has_text=invented["task.photos"])).to_be_visible()
    # Work in progress carries no acceptance, even with requirements on it.
    expect(page.locator(".pcard-accept")).to_have_count(0)
    show_done(page, width, words)
    for task, state, text in (("task.menu", "accepted", words["accepted"]), ("task.prices", "handed_in", words["handed_in"]), ("task.hero", "operator_approved", words["approved"])):
        chip = page.locator(".pcard", has_text=invented[task]).locator(".pcard-accept")
        expect(chip).to_have_attribute("data-acceptance", state)
        expect(chip).to_have_text(text)
    expect(page.locator(".pcard", has_text=invented["task.hero"]).locator(".pcard-accept svg")).to_have_count(1)
    expect(page.locator(".pcard", has_text=invented["task.prices"]).locator(".pcard-accept")).not_to_have_class("ok")
    # The chip sits in the card's last row: a finished card grows no row for it.
    for task in ("task.menu", "task.prices"):
        card = page.locator(".pcard", has_text=invented[task])
        rows = card.evaluate("(c) => [...c.children].filter((e) => e.querySelector('.pcard-accept') || e.classList.contains('pcard-accept')).map((e) => e.className)")
        assert rows == ["pcard-meta"], f"{where}: the acceptance chip of {task} is in {rows}"
    no_sideways(page, f"{where} board")


def menu_sheet(page: Page, lang: str, width: int) -> None:
    words, contract = WORDS[lang], CONTRACT_WORDS[lang]
    where = f"{lang} {width} checked"
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}&task=t-menu")
    sheet = page.locator(".sheet.pboard-sheet")
    acceptance = sheet.locator(".pboard-acceptance")
    expect(acceptance.locator(".pboard-contract-title")).to_have_text(words["acceptance"])
    expect(acceptance.locator(".pboard-contract-head .chip")).to_have_text(words["accepted.title"])
    checks = acceptance.locator(".pboard-item")
    expect(checks).to_have_count(2)
    expect(checks.nth(0).locator(".pboard-label")).to_have_text("C1")
    expect(checks.nth(0).locator(".pboard-evidence")).to_have_text(f"{contract['e.items.how']} → {contract['e.items.result']}")
    expect(checks.locator(".pboard-mark[data-mark='met']")).to_have_count(2)
    expect(checks.nth(0).locator(".pboard-mark")).to_have_text(words["met"])
    expect(checks.nth(1).locator(".pboard-mark-note")).to_have_text(contract["m.phone"])
    requirement = sheet.locator(".pboard-requirements .pboard-item[data-requirement='R1']")
    expect(requirement.locator(".pboard-req-meta")).to_have_text(f"{words['input']} · {words['from.you']}")
    expect(requirement.locator(".pboard-req-file")).to_contain_text("spring-menu.csv")
    expect(requirement.locator(".pboard-delivery")).to_have_attribute("data-delivery", "words")
    expect(requirement.locator(".pboard-delivery")).to_have_text(words["words"])
    no_sideways(page, where)


def prices_sheet(page: Page, lang: str, width: int) -> None:
    words = WORDS[lang]
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}&task=t-prices")
    acceptance = page.locator(".sheet.pboard-sheet .pboard-acceptance")
    expect(acceptance.locator(".pboard-contract-head .chip")).to_have_text(words["handed_in.title"])
    expect(acceptance.locator(".pboard-mark")).to_have_count(0)
    expect(acceptance.locator(".pboard-item").nth(1)).to_contain_text(words["noword"])
    expect(page.locator(".sheet.pboard-sheet .pboard-requirements")).to_have_count(0)
    no_sideways(page, f"{lang} {width} handed in")


def photos_sheet(page: Page, lang: str, width: int) -> None:
    words, contract = WORDS[lang], CONTRACT_WORDS[lang]
    where = f"{lang} {width} requirements"
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}&task=t-photos")
    sheet = page.locator(".sheet.pboard-sheet")
    # Nothing handed in and no checks: no acceptance section at all.
    expect(sheet.locator(".pboard-acceptance")).to_have_count(0)
    section = sheet.locator(".pboard-requirements")
    expect(section.locator(".pboard-contract-title")).to_have_text(words["requirements"])
    items = section.locator(".pboard-item")
    expect(items).to_have_count(5)
    expect(items.locator(".pboard-label")).to_have_text(["R1", "R2", "R3", "R4", "R5"])

    r = lambda label: section.locator(f".pboard-item[data-requirement='{label}']")  # noqa: E731
    expect(r("R1").locator(".pboard-item-text")).to_have_text(contract["r.short"])
    expect(r("R1").locator(".pboard-req-meta")).to_have_text(f"{words['quality']} · {words['from.you']}")
    expect(r("R1").locator(".pboard-delivery")).to_have_text(words["confirmed"])
    expect(r("R2").locator(".pboard-req-meta")).to_contain_text(words["from.answer"])
    expect(r("R2").locator(".pboard-req-file")).to_contain_text("shot-list.csv")
    expect(r("R2").locator(".pboard-delivery")).to_have_attribute("data-delivery", "opened")
    expect(r("R2").locator(".pboard-delivery")).to_have_text(words["opened"])
    # Replaced, and kept in the list struck through, with no deliveries of its own to chase.
    expect(r("R3")).to_have_class("pboard-item req superseded")
    expect(r("R3").locator(".pboard-req-state")).to_have_text(words["replaced"])
    assert "line-through" in r("R3").locator(".pboard-item-text").evaluate("(e) => getComputedStyle(e).textDecorationLine"), f"{where}: the replaced requirement is not struck through"
    expect(r("R3").locator(".pboard-delivery")).to_have_count(0)
    expect(r("R4").locator(".pboard-req-meta")).to_contain_text(words["from.orchestrator"])
    expect(r("R4").locator(".pboard-delivery")).to_have_attribute("data-delivery", "sent")
    expect(r("R4").locator(".pboard-delivery")).to_have_text(words["sent"])
    expect(r("R4").locator(".pboard-delivery")).to_have_class("chip tiny pboard-delivery attn")
    expect(r("R5").locator(".pboard-req-meta")).to_contain_text(words["from.rule"])
    no_sideways(page, where)


def reworded(page: Page, lang: str, width: int) -> None:
    """The orchestrator's discount question, reworded once: marked as updated. Then reworded again
    while a draft waits on it: the draft stays, and only the option the new words dropped goes."""
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    where = f"{lang} {width} questions"
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.goto(f"{BASE}/orchestration/project/{PID}?token=t&lang={lang}")
    if width < 1024:
        page.locator(".chat-head .questions-headbtn").tap()
    panel = page.locator(".questions-panel")
    card = panel.locator(".q-card[data-ask='q4r8tz']")
    flag = card.locator(".q-flag.updated")
    expect(flag).to_have_attribute("data-revision", "1")
    expect(flag).to_contain_text(words["updated"])
    expect(panel.locator(".q-flag.updated")).to_have_count(1)
    no_sideways(page, where)

    card.locator(f".q-chip[data-option=\"{invented['ask.after']}\"]").click()
    card.locator(".q-field").fill("like last spring")
    expect(card).to_have_attribute("data-state", "ready")
    ask = next(a for a in focus.asks if a["id"] == "ask-spring")
    ask["detail"] = {**ask["detail"], "options": [invented["ask.before"], "50 / 50"], "revision": 2, "updated_at": "2026-09-24T10:02:00Z"}
    # The open panel is in the address, so the reload comes back to the same list.
    page.reload()
    card = page.locator(".questions-panel .q-card[data-ask='q4r8tz']")
    expect(card.locator(".q-flag.updated")).to_have_attribute("data-revision", "2")
    expect(card.locator(".q-field")).to_have_value("like last spring")
    expect(card.locator(".q-chip[aria-checked='true']")).to_have_count(0)
    expect(card.locator(".q-chip[data-option='50 / 50']")).to_have_count(1)


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width, height in ((390, 844), (1280, 860)):
                phone = width < 1024
                for step in (cards, menu_sheet, prices_sheet, photos_sheet, reworded):
                    context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=phone, has_touch=phone)
                    step(context.new_page(), lang, width)
                    context.close()
                print(f"task contract {lang} {width}: ok")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
