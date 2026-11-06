"""The agent link: the chat's sheet for handing someone a link, and a guest's message in the chat.

A person opens "Agent link…" from the chat's menu, makes a link, and sees it once. The check drives that
the way a hand does:

- a stand-in link is made from a typed briefing, the address shows once with one line of intent in front
  of it, and the clipboard holds that line and the address; the address is nowhere on the page after Done;
- a briefing drafted from the chat has to be read and ticked before Create answers;
- a direct link says in one line that the guest reaches everything, and opens on read-only tools;
- a first message that waits is shown as plain text, and Admit and Revoke change the list;
- a conversation is read as plain text, who spoke on each line, and nothing a guest wrote becomes a link,
  a picture or markup;
- a door that is off, or has no address, says so in one line and draws no form;
- a guest's message in the chat is a card marked with the guest, never the operator's bubble.

    cd miniapp && npm run build && cd ..
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8293 /tmp/app-root
    APP_URL=http://127.0.0.1:8293/app python3 tests/browser/check_guest_links.py

``SHOTS=<directory>`` also leaves a picture of the sheet in each language and width.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_stub  # noqa: E402
from api_stub import DEFAULT_APP, FakeGuests, expect_app  # noqa: E402
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402
from screenshots import S1, UNHANDLED, ago, detail, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {
        "actions": "Session actions", "menu": "Agent link…", "title": "Agent link", "make": "Make a link", "create": "Create link", "done": "Done",
        "direct": "Direct", "standin": "Stand-in", "admit": "Admit", "refuse": "Refuse", "revoke": "Revoke", "read": "Read", "back": "Back", "copy": "Copy",
        "intro": "Talk to this agent and ask what you need", "once": "Shown only now. If you lose it, make a new link.",
        "warning": "The guest's agent can reach everything in this chat.", "off": "The agent link is off in this installation.",
        "noaddress": "The agent link has no public address yet.", "draft": "Draft from this chat", "tick": "I have read this briefing",
        "from": "From a guest · Mira's assistant", "state_open": "In conversation", "state_pending": "Waits for you", "state_revoked": "Revoked",
        "state_waiting": "Not opened yet", "readonly": "Read-only", "save": "Save", "daily": "Daily limit for all guests", "cancel": "Cancel",
        "who": "Who it is for", "topic": "What to talk about", "briefing": "Briefing", "stand_in_role": "Stand-in", "guest_role": "Guest", "door_role": "System",
        "newlink": "New link", "all": "All tools",
        "all_note": "This chat's folder only; commands run sandboxed, with no network or secrets.",
        "why_no_sandbox": "There is no sandbox here, so the guest cannot have all tools.",
        "why_host_folder": "This chat works in a folder on the host, so the guest cannot have all tools.",
        "retention": "Keep the record for", "retention_direct": "The link's own record is kept for", "more": "More settings",
        "nomessages_direct": "Nothing here. What a guest said is kept in this chat.",
    },
    "ru": {
        "actions": "Действия с сессией", "menu": "Ссылка для агента…", "title": "Ссылка для агента", "make": "Сделать ссылку", "create": "Создать ссылку", "done": "Готово",
        "direct": "Напрямую", "standin": "Дублёр", "admit": "Допустить", "refuse": "Отказать", "revoke": "Отозвать", "read": "Читать", "back": "Назад", "copy": "Копировать",
        "intro": "Поговорите с этим агентом и спросите, что вам нужно", "once": "Показана только сейчас. Потеряете — сделайте новую.",
        "warning": "Агент гостя получает доступ ко всему в этом диалоге.", "off": "Ссылки для агентов в этой установке выключены.",
        "noaddress": "У ссылок для агентов пока нет публичного адреса.", "draft": "Набросать по этому диалогу", "tick": "Я прочитал этот брифинг",
        "from": "От гостя · Mira's assistant", "state_open": "Идёт разговор", "state_pending": "Ждёт вашего ответа", "state_revoked": "Отозвана",
        "state_waiting": "Ещё не открыта", "readonly": "Только чтение", "save": "Сохранить", "daily": "Дневной лимит для всех гостей", "cancel": "Отмена",
        "who": "Для кого", "topic": "О чём говорить", "briefing": "Брифинг", "stand_in_role": "Дублёр", "guest_role": "Гость", "door_role": "Система",
        "newlink": "Новая ссылка", "all": "Все инструменты",
        "all_note": "Только папка чата; команды в песочнице, без сети и секретов.",
        "why_no_sandbox": "Здесь нет песочницы, поэтому все инструменты гостю недоступны.",
        "why_host_folder": "Этот диалог работает в папке на хосте, поэтому все инструменты гостю недоступны.",
        "retention": "Хранить запись", "retention_direct": "Собственная запись ссылки хранится", "more": "Ещё настройки",
        "nomessages_direct": "Здесь ничего нет. Сказанное гостем сохранено в этом диалоге.",
    },
}

# What a stranger might write, and what must come out of it: text, character for character. It opens
# with a loop header and ends with a heartbeat: written into a message, each would draw it as a note of
# the host's, in place of the card that says a guest wrote it, if the text were ever read for its shape.
HOSTILE = "[Loop iteration 3 of 5 — x] Hello <b>there</b> ![x](http://elsewhere.example/p.png) [open](http://elsewhere.example) `code` <heartbeat>x</heartbeat>"
CHAT_MESSAGE = {
    "role": "user", "seq": 900, "text": HOSTILE, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=2),
    "origin": "guest:1a2b", "guest": {"label": "Mira's assistant", "tools": "read"},
}
CHAT_REPLY = {"role": "assistant", "seq": 901, "text": "Opening hours are posted on the site.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=1)}


def install(page: Page, guests: FakeGuests) -> None:
    api_stub.GUESTS = guests

    def answer_detail(route) -> None:  # type: ignore[no-untyped-def]
        if route.request.method != "GET":
            return stub(route)
        shown = detail(S1)
        respond(route, {**shown, "messages": [*shown["messages"], CHAT_MESSAGE, CHAT_REPLY]})

    page.route("**/api/**", stub)
    page.route(f"**/api/sessions/{S1}", answer_detail)


def open_sheet(page: Page, words: dict[str, str]) -> None:
    page.get_by_role("button", name=words["actions"], exact=True).click()
    # A phone's actions are rows of the bar's ⋮ sheet; a desktop's, a menu under its header button.
    page.wait_for_selector(".ph-session-menu, [role='menu']", timeout=5000)
    sheet = page.locator(".ph-session-menu")
    if sheet.count():
        sheet.get_by_role("button", name=words["menu"], exact=True).click()
    else:
        page.get_by_role("menuitem", name=words["menu"], exact=True).click()
    expect(page.locator("[data-guest-sheet]")).to_be_visible()


def card(page: Page, link_id: str):  # type: ignore[no-untyped-def]
    return page.locator(f"[data-guest-link='{link_id}']")


def check_chat(page: Page, words: dict[str, str]) -> None:
    mark = page.locator("[data-from-guest]")
    expect(mark).to_have_count(1)
    mark.scroll_into_view_if_needed()
    expect(mark.locator(".guest-card-head")).to_contain_text(words["from"])
    expect(mark.locator(".guest-card-head")).to_contain_text(words["readonly"])
    # Plain text, character for character: no tag, link, picture or code came out of it.
    expect(mark.locator(".guest-card-text")).to_have_text(HOSTILE)
    assert mark.locator(".guest-card-text").locator("b, a, img, code").count() == 0
    # Not the operator's bubble, and not folded away as a note.
    assert page.locator(".msg.user [data-from-guest], .msg.user:has-text('Hello there')").count() == 0
    assert page.locator(".sysnote:has-text('Hello'), .loop-turn:has-text('Hello')").count() == 0
    if os.environ.get("SHOTS"):
        tag = "ru" if words["title"].startswith("С") else "en"
        page.screenshot(path=f"{os.environ['SHOTS']}/guest-chat-{tag}-{page.viewport_size['width']}.png")


def check_list(page: Page, words: dict[str, str], guests: FakeGuests) -> None:
    expect(page.locator("[data-guest-link]")).to_have_count(3)
    pending = card(page, "3c4d")
    expect(pending.locator("[data-guest-state]")).to_have_text(words["state_pending"])
    quote = pending.locator("[data-guest-quote] .guest-quote-text")
    expect(quote).to_contain_text("read the draft of the new menu")
    assert pending.locator("[data-guest-quote] img, [data-guest-quote] a").count() == 0
    expect(pending.locator("[data-guest-redeems='2']")).to_be_visible()
    expect(pending.get_by_role("button", name=words["newlink"], exact=True)).to_be_visible()
    expect(card(page, "5e6f").locator("[data-guest-state]")).to_have_text(words["state_revoked"])
    assert card(page, "5e6f").get_by_role("button", name=words["revoke"], exact=True).count() == 0
    expect(page.locator("[data-guest-daily] input")).to_have_value("2")
    expect(page.locator("[data-guest-daily]")).to_contain_text("$0.50")

    # Admit: the conversation opens, the quote and the two buttons go.
    pending.get_by_role("button", name=words["admit"], exact=True).click()
    expect(card(page, "3c4d").locator("[data-guest-state]")).to_have_text(words["state_open"])
    expect(card(page, "3c4d").locator("[data-guest-quote]")).to_have_count(0)
    assert ("POST", "/api/guest/conversations/c3c/admit", None) in guests.calls

    # A direct conversation is kept in the chat itself, and the link's own copy may hold nothing: it says so.
    card(page, "3c4d").get_by_role("button", name=words["read"], exact=True).click()
    expect(page.locator("[data-guest-reader]")).to_contain_text(words["nomessages_direct"])
    page.locator("[data-guest-reader]").get_by_role("button", name=words["back"], exact=True).click()
    expect(page.locator("[data-guest-link]")).to_have_count(3)

    # Read: who spoke on each line, and what the guest wrote as text.
    card(page, "1a2b").get_by_role("button", name=words["read"], exact=True).click()
    reader = page.locator("[data-guest-reader]")
    expect(reader).to_be_visible()
    expect(reader.locator(".guest-msg")).to_have_count(4)
    roles = reader.locator(".guest-msg").evaluate_all("rows => rows.map(r => r.dataset.role)")
    assert roles == ["guest", "agent", "guest", "door"], roles
    heads = reader.locator(".guest-msg-head b").all_inner_texts()
    assert heads == [words["guest_role"], words["stand_in_role"], words["guest_role"], words["door_role"]], heads
    expect(reader.locator(".guest-msg-text").nth(2)).to_have_text("Can I order <b>a cake</b> for Sunday? See http://elsewhere.example/menu")
    assert reader.locator(".guest-msg-text b, .guest-msg-text a").count() == 0
    reader.get_by_role("button", name=words["briefing"], exact=True).click()
    expect(reader.locator("[data-guest-briefing]")).to_contain_text("seven to six")
    reader.get_by_role("button", name=words["back"], exact=True).click()
    expect(page.locator("[data-guest-link]")).to_have_count(3)

    # Revoke: the link is over and says so; the door was told.
    card(page, "1a2b").get_by_role("button", name=words["revoke"], exact=True).click()
    expect(card(page, "1a2b").locator("[data-guest-state]")).to_have_text(words["state_revoked"])
    assert ("POST", "/api/guest/links/1a2b/revoke", None) in guests.calls

    # The daily limit across guests is saved.
    field = page.locator("[data-guest-daily] input")
    field.fill("3.5")
    page.locator("[data-guest-daily]").get_by_role("button", name=words["save"], exact=True).click()
    expect(page.locator("[data-guest-daily] input")).to_have_value("3.5")
    assert any(call[1] == "/api/guest/settings" and call[2] == {"usd_per_day": 3.5} for call in guests.calls), guests.calls


def made_text(page: Page) -> str:
    return page.locator("#guest-url").input_value()


def check_stand_in(page: Page, words: dict[str, str], guests: FakeGuests, width: int) -> None:
    page.locator("[data-guest-new]").click()
    create = page.get_by_role("button", name=words["create"], exact=True)
    expect(create).to_be_disabled()
    expect(page.locator("[data-guest-warning]")).to_have_count(0)
    # A hand-typed briefing needs no tick: pressing Create approves it.
    expect(page.locator("[data-guest-read]")).to_have_count(0)
    check_segments(page)
    # A stand-in's conversation lives only on the link, so its record is the record.
    expect(page.locator("label[for='guest-retention_days']")).to_have_text(words["retention"])
    page.get_by_label(words["who"], exact=True).fill("Anna's agent")
    page.get_by_label(words["topic"], exact=True).fill("Opening hours for the autumn")
    page.locator("#guest-briefing").fill("The bakery opens at seven.")
    expect(create).to_be_enabled()
    create.click()
    url_box = page.locator("#guest-url")
    expect(url_box).to_be_visible()
    shown = made_text(page)
    assert shown.startswith("Opening hours for the autumn: https://door.example/a/"), shown
    expect(page.locator("[data-guest-once]")).to_have_text(words["once"])
    snap(page, words, width, "made")
    sent = [call for call in guests.calls if call[1].endswith("/guest/links")][-1]
    body = sent[2]
    assert body["mode"] == "stand_in" and body["model_id"] == "cheap" and body["briefing"] == "The bakery opens at seven." and body["briefing_approved"] is True, body
    assert body["label"] == "Anna's agent" and body["topic"] == "Opening hours for the autumn" and body["approval"] is True, body
    # The copy is the line and the address, together.
    page.locator("[data-guest-copy]").click()
    clip = page.evaluate("navigator.clipboard.readText()")
    assert clip == shown and "Opening hours for the autumn" in clip and "https://door.example/a/" in clip, clip
    url = shown.split(": ", 1)[1]
    page.get_by_role("button", name=words["done"], exact=True).click()
    expect(page.locator("[data-guest-link]")).to_have_count(4)
    # The address is not kept: not in the list, not anywhere on the page.
    assert url not in page.content(), "the address is still on the page"
    assert url not in json.dumps(page.evaluate("Object.assign({}, window.localStorage)"))


def check_draft(page: Page, words: dict[str, str]) -> None:
    page.locator("[data-guest-new]").click()
    create = page.get_by_role("button", name=words["create"], exact=True)
    page.locator("[data-guest-draft]").click()
    expect(page.locator("#guest-briefing")).to_contain_text("bakery site")
    # A draft is the one briefing the operator did not write: it is read and ticked first.
    tick = page.locator("[data-guest-read] input")
    expect(tick).to_be_visible()
    expect(create).to_be_disabled()
    tick.check()
    expect(create).to_be_enabled()
    page.locator("#guest-briefing").fill("Edited after reading.")
    expect(create).to_be_disabled()
    expect(tick).not_to_be_checked()
    page.get_by_role("button", name=words["cancel"], exact=True).click()
    expect(page.locator("[data-guest-link]")).to_have_count(4)


def check_segments(page: Page) -> None:
    """Every segment of a choice shows all its words, and the choice stays inside the sheet.

    Three long words in a third of a phone's width each were once clipped at the button's edge ("All
    tools" lost its end): a button's text is what scrollWidth measures against its own width."""
    problems = page.evaluate(
        """() => {
            const sheet = document.querySelector('.sheet').getBoundingClientRect();
            const out = [];
            for (const group of document.querySelectorAll('.guest-form .segmented')) {
                const box = group.getBoundingClientRect();
                if (box.left < sheet.left - 1 || box.right > sheet.right + 1) out.push('a choice runs outside the sheet');
                for (const button of group.querySelectorAll('button')) {
                    const at = button.getBoundingClientRect();
                    if (button.scrollWidth > button.clientWidth) out.push(button.textContent + ' is clipped by its button');
                    if (at.left < box.left - 1 || at.right > box.right + 1) out.push(button.textContent + ' runs outside its choice');
                    if (at.width < 20 || at.height < 20) out.push(button.textContent + ' has no room');
                }
            }
            return out;
        }"""
    )
    assert not problems, problems


def check_direct(page: Page, words: dict[str, str], guests: FakeGuests, width: int) -> None:
    page.locator("[data-guest-new]").click()
    page.get_by_role("radio", name=words["direct"], exact=True).click()
    # One line, the one that says what the guest can reach, and the tools open on read-only.
    expect(page.locator("[data-guest-warning]")).to_have_text(words["warning"])
    assert page.locator(".guest-form .sub").count() == 1
    expect(page.locator("[data-guest-tools-all], [data-guest-tools-why]")).to_have_count(0)
    snap(page, words, width, "direct")
    check_segments(page)
    expect(page.get_by_role("radio", name=words["all"], exact=True)).to_be_visible()
    expect(page.get_by_role("radio", name=words["readonly"], exact=True)).to_have_attribute("aria-checked", "true")
    expect(page.locator("#guest-briefing")).to_have_count(0)
    # A direct conversation is also kept in the chat, so the link's own record is named as the link's.
    expect(page.locator("label[for='guest-retention_days']")).to_have_text(words["retention_direct"])
    page.get_by_label(words["who"], exact=True).fill("Tomas")
    page.get_by_role("button", name=words["create"], exact=True).click()
    expect(page.locator("#guest-url")).to_be_visible()
    # Without a topic the line of intent is the default sentence of the language.
    assert made_text(page).startswith(f"{words['intro']}: https://door.example/a/"), made_text(page)
    body = [call for call in guests.calls if call[1].endswith("/guest/links")][-1][2]
    assert body["mode"] == "direct" and body["tools"] == "read" and body["briefing"] == "" and body["model_id"] == "", body
    page.get_by_role("button", name=words["done"], exact=True).click()

    # All tools is an allow-list, and the form says so in one more line, only while it is chosen.
    page.locator("[data-guest-new]").click()
    page.get_by_role("radio", name=words["direct"], exact=True).click()
    page.get_by_role("radio", name=words["all"], exact=True).click()
    expect(page.locator("[data-guest-tools-all]")).to_have_text(words["all_note"])
    assert page.locator(".guest-form .sub").count() == 2
    expect(page.locator("[data-guest-warning]")).to_have_text(words["warning"])
    snap(page, words, width, "direct-all")
    check_segments(page)
    page.get_by_role("radio", name=words["readonly"], exact=True).click()
    expect(page.locator("[data-guest-tools-all]")).to_have_count(0)
    page.get_by_role("radio", name=words["all"], exact=True).click()
    page.get_by_role("button", name=words["create"], exact=True).click()
    expect(page.locator("#guest-url")).to_be_visible()
    body = [call for call in guests.calls if call[1].endswith("/guest/links")][-1][2]
    assert body["mode"] == "direct" and body["tools"] == "all", body
    page.get_by_role("button", name=words["done"], exact=True).click()


def check_no_full_tools(browser, words: dict[str, str], lang: str, why: str, width: int) -> None:  # type: ignore[no-untyped-def]
    """A chat that cannot be given the sandboxed tools is not offered them: one line says why, and the
    tools stay on read-only, which is what the link is made with."""
    guests = FakeGuests(full_tools_why=why)
    context = browser.new_context(viewport={"width": width, "height": 844})
    page = context.new_page()
    install(page, guests)
    page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme=dark", wait_until="domcontentloaded")
    expect(page.locator(".chat-title")).to_be_visible()
    open_sheet(page, words)
    expect(page.locator("[data-guest-link]")).to_have_count(3)
    # The door was asked about this chat.
    assert guests.door_asked and all(asked == S1 for asked in guests.door_asked), guests.door_asked
    page.locator("[data-guest-new]").click()
    page.get_by_role("radio", name=words["direct"], exact=True).click()
    expect(page.get_by_role("radio", name=words["all"], exact=True)).to_have_count(0)
    expect(page.get_by_role("radio", name=words["readonly"], exact=True)).to_have_attribute("aria-checked", "true")
    expect(page.locator(f"[data-guest-tools-why='{why}']")).to_have_text(words[f"why_{why}"])
    # The warning of every direct link, and this one reason: two lines, and no more.
    assert page.locator(".guest-form .sub").count() == 2
    expect(page.locator("[data-guest-warning]")).to_have_text(words["warning"])
    expect(page.locator("[data-guest-tools-all]")).to_have_count(0)
    snap(page, words, width, f"direct-{why}")
    check_segments(page)
    page.get_by_role("button", name=words["create"], exact=True).click()
    expect(page.locator("#guest-url")).to_be_visible()
    body = [call for call in guests.calls if call[1].endswith("/guest/links")][-1][2]
    assert body["mode"] == "direct" and body["tools"] == "read", body
    context.close()


def check_fit(page: Page, width: int) -> None:
    box = page.locator(".sheet").last.bounding_box()
    assert box is not None
    assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1, box
    assert box["y"] >= 0
    wide = page.evaluate("(() => { const b = document.querySelector('.sheet-body'); return b ? b.scrollWidth - b.clientWidth : 0; })()")
    assert wide <= 1, f"the sheet scrolls sideways by {wide}px"


def snap(page: Page, words: dict[str, str], width: int, name: str) -> None:
    if os.environ.get("SHOTS"):
        # The sheet slides in; a picture taken mid-way shows the page through it.
        page.wait_for_timeout(600)
        tag = "ru" if words["title"].startswith("С") else "en"
        page.screenshot(path=f"{os.environ['SHOTS']}/guest-{name}-{tag}-{width}.png")
        # And the foot of it, where the settings and the buttons are.
        page.evaluate("document.querySelector('.sheet-body').scrollTop = 1e6")
        page.wait_for_timeout(200)
        page.screenshot(path=f"{os.environ['SHOTS']}/guest-{name}-end-{tag}-{width}.png")
        page.evaluate("document.querySelector('.sheet-body').scrollTop = 0")


def check_off(browser, words: dict[str, str], guests: FakeGuests, line: str, kind: str) -> None:  # type: ignore[no-untyped-def]
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    install(page, guests)
    page.goto(f"{BASE}/agents/{S1}?lang={'ru' if words['title'].startswith('С') else 'en'}&scheme=dark", wait_until="domcontentloaded")
    expect(page.locator(".chat-title")).to_be_visible()
    open_sheet(page, words)
    expect(page.locator(f"[data-guest-off='{kind}']")).to_have_text(line)
    expect(page.locator("[data-guest-new]")).to_have_count(0)
    expect(page.locator("form")).to_have_count(0)
    page.close()


def main() -> None:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        for lang, words in WORDS.items():
            for width, height in ((1280, 800), (390, 844)):
                guests = FakeGuests()
                context = browser.new_context(viewport={"width": width, "height": height}, permissions=["clipboard-read", "clipboard-write"])
                page = context.new_page()
                install(page, guests)
                page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme=dark", wait_until="domcontentloaded")
                expect(page.locator(".chat-title")).to_be_visible()
                check_chat(page, words)
                open_sheet(page, words)
                expect(page.locator(".sheet h3")).to_have_text(words["title"])
                expect(page.locator("[data-guest-link]")).to_have_count(3)
                snap(page, words, width, "list")
                check_fit(page, width)
                check_list(page, words, guests)
                check_stand_in(page, words, guests, width)
                check_draft(page, words)
                check_direct(page, words, guests, width)
                page.get_by_role("button", name=words["make"], exact=True).click()
                snap(page, words, width, "form")
                check_fit(page, width)
                page.keyboard.press("Escape")
                context.close()
            for why in ("no_sandbox", "host_folder"):
                for width in (1280, 390):
                    check_no_full_tools(browser, words, lang, why, width)
            check_off(browser, words, FakeGuests(available=False), words["off"], "off")
            check_off(browser, words, FakeGuests(public_base=""), words["noaddress"], "address")
        browser.close()
    if UNHANDLED.report():
        raise SystemExit(1)
    print("guest links: ok")


if __name__ == "__main__":
    main()
