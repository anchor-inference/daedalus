"""Talking to the agent in another language, in a real browser.

The composer's language chip names the pair it translates between and changes the session's language
at the host; the operator's message shows their own words with a switch to what the agent got, the code
block intact on both sides; the agent's answer shows the translation with a switch to the original, a
"translating" mark while the translator works and a plain mark when it failed; a question the agent asks
is shown in the operator's language while the answer names the agent's own label; and Settings → Translation
has the translation card. Each state is photographed in English and in Russian, into ``SHOTS`` when set.

    APP_URL=http://127.0.0.1:8371/app python3 tests/browser/check_agent_language.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from api_stub import TRANSLATED_EXCHANGE, session_translation, translated_messages
from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, message
from check_composer import stub as composer_stub
from playwright.sync_api import expect, sync_playwright
from screenshots import stub as app_stub

SHOTS = Path(os.environ["SHOTS"]) if os.environ.get("SHOTS") else None
LANGUAGE = {"agent": "en", "explicit": True}
CHOSEN: list[dict] = []


def stub(route) -> None:  # type: ignore[no-untyped-def]
    path = route.request.url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    if rel == f"/api/sessions/{SESSION}/language" and route.request.method == "POST":
        data = json.loads(route.request.post_data or "{}")
        CHOSEN.append(data)
        LANGUAGE["agent"] = "en" if data.get("agent") is None else str(data["agent"])
        LANGUAGE["explicit"] = data.get("agent") is not None
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(session_translation(str(LANGUAGE["agent"]), explicit=bool(LANGUAGE["explicit"]))))
    if rel == f"/api/sessions/{SESSION}" and route.request.method == "GET":
        detail = {**HOST.detail(), "translation": session_translation(str(LANGUAGE["agent"]), explicit=bool(LANGUAGE["explicit"]))}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))
    return composer_stub(route)


def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / name))


WORDS = {
    "en": {"agent": "Show what the agent got (EN)", "mine": "Show my words", "original": "Show original", "translation": "Show translation", "done": "Translated EN → RU",
           "pending": "Translating into RU…", "failed": "Translation failed — showing the original", "off": "Off", "card": "Talking in another language"},
    "ru": {"agent": "Показать, что получил агент (EN)", "mine": "Показать мои слова", "original": "Показать оригинал", "translation": "Показать перевод", "done": "Переведено EN → RU",
           "pending": "Перевожу на RU…", "failed": "Перевести не удалось — показан оригинал", "off": "Выключено", "card": "Разговор на другом языке"},
}


def walk(browser, lang: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    x = TRANSLATED_EXCHANGE
    HOST.status = "idle"
    HOST.pending = None
    HOST.messages = translated_messages(message)
    LANGUAGE.update(agent="en", explicit=True)
    page = browser.new_page(viewport={"width": 1280, "height": 860})
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&lang={lang}&scheme=dark")

    # The chip: the pair, in the accent.
    chip = page.locator(".composer .agentlang-chip")
    expect(chip).to_have_text("EN ⇄ RU")
    assert "translating" in (chip.get_attribute("class") or ""), chip.get_attribute("class")

    # The operator's own words by default; one click shows what the agent got, code block and all.
    bubble = page.locator(".msg.user").last
    expect(bubble).to_contain_text("Почему эта функция")
    flip = bubble.locator("[data-translation='out']")
    expect(flip).to_have_text(words["agent"])
    flip.click()
    expect(bubble).to_contain_text("Why does this function sometimes return False")
    expect(bubble.locator("pre")).to_contain_text("def retry(call, attempts=3):")
    expect(flip).to_have_text(words["mine"])
    flip.click()
    expect(bubble).to_contain_text("Почему эта функция")
    expect(bubble.locator("pre")).to_contain_text("def retry(call, attempts=3):")

    # The answer: the translation, then the original on request, then back.
    answer = page.locator(".answer").last
    expect(answer).to_contain_text("Цикл возвращает")
    line = page.locator(".answer-translation[data-translation='done']")
    expect(line).to_contain_text(words["done"])
    line.get_by_role("button", name=words["original"]).click()
    expect(answer).to_contain_text("The loop returns")
    expect(line.get_by_role("button", name=words["translation"])).to_be_visible()
    page.locator(".answer-translation").scroll_into_view_if_needed()
    shot(page, f"answer-original-{lang}.png")
    line.get_by_role("button", name=words["translation"]).click()
    expect(answer).to_contain_text("Цикл возвращает")
    shot(page, f"answer-translated-{lang}.png")

    # The menu: the default, off and the languages, English checked; off reaches the host and the chip mutes.
    chip.click()
    menu = page.locator(".mode-menu")
    expect(menu.locator(".agentlang-row[data-lang='en']")).to_have_attribute("aria-checked", "true")
    expect(menu.locator(".agentlang-row[data-lang='ru']")).to_have_count(0)
    page.wait_for_timeout(400)  # the popover fades in
    shot(page, f"composer-chip-menu-{lang}.png")
    before = len(CHOSEN)
    menu.locator(".agentlang-row[data-lang='off']").click()
    expect(chip).to_have_text("RU")
    assert CHOSEN[before:] == [{"agent": ""}], CHOSEN
    page.wait_for_timeout(300)
    expect(chip).to_have_text("RU")
    assert "off" in (chip.get_attribute("class") or ""), chip.get_attribute("class")
    chip.click()
    page.locator(".mode-menu .agentlang-row[data-lang='default']").click()
    expect(chip).to_have_text("EN ⇄ RU")
    assert CHOSEN[-1] == {"agent": None}, CHOSEN

    # While the translator works the original stays, marked; a failed translation says so.
    HOST.messages = [translated_messages(message)[0], message(202, "assistant", x["answer_en"], translation={"direction": "in", "state": "pending", "from": "en", "to": "ru"})]
    page.reload()
    expect(page.locator(".answer-translation[data-translation='pending']")).to_have_text(words["pending"])
    expect(page.locator(".answer").last).to_contain_text("The loop returns")
    shot(page, f"answer-pending-{lang}.png")
    HOST.messages = [message(201, "user", x["asked"], translation={"direction": "out", "state": "failed", "from": "ru", "to": "en", "error": "timed out"}),
                     message(202, "assistant", x["answer_en"], translation={"direction": "in", "state": "failed", "from": "en", "to": "ru", "error": "timed out"})]
    page.reload()
    expect(page.locator(".answer-translation[data-translation='failed']")).to_contain_text(words["failed"])
    expect(page.locator(".msg.user [data-translation='out-failed']")).to_be_visible()

    # A question in the operator's language; the answer names the agent's own label.
    HOST.messages = translated_messages(message)
    HOST.status = "waiting"
    HOST.pending = {"questions": [{"question": "Retry with a backoff?", "options": [{"label": "Yes, exponential", "description": "up to 30 s"}, {"label": "No"}], "allow_custom": True,
                                   "translation": {"question": "Повторять с паузой?", "options": [{"label": "Да, с ростом паузы", "description": "до 30 с"}, {"label": "Нет", "description": ""}]}}]}
    page.reload()
    page.wait_for_selector(".composer .ask", timeout=15000)
    expect(page.locator(".composer .ask .ask-text")).to_have_text("Повторять с паузой?")
    page.locator(".composer .ask .ask-opt", has_text="Да, с ростом паузы").click()
    expect(page.locator(".composer .ask .ask-opt-desc").first).to_have_text("до 30 с")
    shot(page, f"question-{lang}.png")
    before = len(HOST.posted)
    page.locator(".composer .roundbtn.primary").click()
    page.wait_for_timeout(600)
    answered = [body for method, rel, body in HOST.posted[before:] if rel.endswith("/answer")]
    assert answered and answered[-1]["answers"][0]["selected"] == ["Yes, exponential"], answered
    HOST.pending = None
    HOST.status = "idle"
    page.close()

    # Settings → Translation: the card, the model left automatic with the host's choice named.
    settings = browser.new_page(viewport={"width": 1280, "height": 900})
    settings.route("**/api/**", app_stub)
    settings.goto(f"{BASE}/settings/translation?token=t&lang={lang}&scheme=dark")
    card = settings.locator("[data-card='agent-language']")
    expect(card.locator(".section-title")).to_have_text(words["card"])
    expect(card.locator("[data-setting='translation.preset']")).to_contain_text("DeepSeek Flash")
    expect(card.locator("[data-setting='translation.timeout_seconds'] input")).to_have_value("120")
    card.scroll_into_view_if_needed()
    shot(settings, f"settings-{lang}.png")
    settings.close()


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            walk(browser, lang)
        browser.close()
    assert not UNHANDLED.report()
    print("agent language: the chip, both sides of the exchange, the marks, the question and the settings hold")


if __name__ == "__main__":
    main()
