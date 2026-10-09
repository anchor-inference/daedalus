"""Importing a session from another program, in both languages, on a 1440 px desktop and a 390 px phone.

What is checked:

- the entry points: the New project menu, the start screen's card (which opens on its session), and
  the command palette;
- the explorer: the strip of programs with their counts and the missing one dimmed, the places with
  sessions, the folders inside with their counts, the sessions of a folder with the first one chosen,
  and the preview with its numbers, model and destination;
- a large session proposes a summary and the tail; a running one warns and offers a snapshot; a
  folder that is gone refuses the import; an imported one says so and opens its chat;
- the search marks its matches and groups them by folder;
- the import's stages advance with their numbers, and the chat it made opens;
- the imported chat: the banner with the source and the hidden secrets, "Pull in what's new" and the
  original's download, the steps under their own program's names, the line where Daedalus takes
  over, the Origin block in Details and the corner mark on the sidebar row;
- the machine not answering says how to start the bridge;
- the phone's path: program, folder, session, import, chat;
- no page scrolls sideways, and no API path goes unanswered.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_session_import.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from import_stub import IMPORTED_ID, ImportStub, imported_row, imported_session  # noqa: E402
from screenshots import S1, UNHANDLED, detail, listing, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

LOCK = "a1f3c9e2-5b7d-4c11-9e0a-2f6d8c3b4d17"
BIG = "7be05d41-0c2e-4a8f-b3d9-61e4f0a2c5b8"
EARLIER = "e2aa0c13-7d4b-4f9e-a1c6-3b5d7e9f1a28"
LIVE = "b71d09e5-2c4f-4a6e-8d0b-9f1e3a5c7b42"
GONE = "41aa2f60-8e1c-4b3d-a5f7-2c9e0d4b6a81"

WORDS = {
    "en": {"title": "Continue a session from another program", "entry": "Session from another program…", "palette": "Import a session",
           "notfound": "not found", "here": "Claude Code sessions in this folder · 6", "join": "Into the project «Smart home»",
           "import": "Import", "snapshot": "Import a snapshot", "again": "Import again as a new chat", "tail": "A summary and the latest messages",
           "large": "A large session", "live": "still running in Claude Code", "refused": "It cannot land here", "already": "Already imported",
           "found": "Found in 3 folders · 3 sessions", "read": "Read on the host", "parsed": "Parsed: 214 messages", "masked": "Secrets hidden: 3",
           "banner": "Brought over from", "secrets": "secrets hidden: 3", "pull": "Pull in what's new", "original": "Original",
           "handoff": "Continued in Daedalus", "worked": "Work in Claude Code", "origin": "Origin", "down": "The machine is not answering",
           "card": "Continue from other programs", "fresh": "Fresh on the machine", "phone_title": "Continue a session", "pulled": "Pulled in 2 new messages", "nosessions": "no sessions"},
    "ru": {"title": "Продолжить сессию из другой программы", "entry": "Сессия из другой программы…", "palette": "Импорт сессии",
           "notfound": "не найден", "here": "Сессии Claude Code в этой папке · 6", "join": "В проект «Smart home»",
           "import": "Импортировать", "snapshot": "Импортировать снимок", "again": "Импортировать заново как новый чат", "tail": "Сводка и последние сообщения",
           "large": "Большая сессия", "live": "ещё идёт в Claude Code", "refused": "Сюда перенести нельзя", "already": "Уже импортирована",
           "found": "Найдено в 3 папках · 3 сессии", "read": "Прочитано на хосте", "parsed": "Разобрано: 214 сообщений", "masked": "Скрыто секретов: 3",
           "banner": "Перенесено из", "secrets": "скрыто секретов: 3", "pull": "Подтянуть новое", "original": "Оригинал",
           "handoff": "Продолжено в Daedalus", "worked": "Работа в Claude Code", "origin": "Откуда", "down": "Машина не отвечает",
           "card": "Продолжить из других программ", "fresh": "Свежие на машине", "phone_title": "Продолжить сессию", "pulled": "Подтянуто 2 новых сообщения", "nosessions": "нет сессий"},
}


def serve(page: Page, imports: ImportStub) -> None:
    def route(r) -> None:  # type: ignore[no-untyped-def]
        request = r.request
        path = urlsplit(request.url).path
        path = path[path.index("/api/"):]
        if imports.fulfil(r):
            return
        if path == f"/api/sessions/{IMPORTED_ID}" and request.method == "GET":
            return r.fulfill(status=200, content_type="application/json", body=json.dumps(imported_session(detail(S1))))
        if path == "/api/sessions" and request.method == "GET":
            answer = listing()
            answer["sessions"] = [imported_row(answer["sessions"][0]), *answer["sessions"]]
            return r.fulfill(status=200, content_type="application/json", body=json.dumps(answer))
        return stub(r)

    page.route("**/api/**", route)


def start(browser, lang: str, phone: bool, imports: ImportStub):  # type: ignore[no-untyped-def]
    size = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=size, is_mobile=phone, has_touch=phone, color_scheme="dark", accept_downloads=True)
    context.add_init_script("try { localStorage.setItem('daedalus.session.panel', 'details'); } catch (e) {}")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    serve(page, imports)
    return context, page, errors


def open_explorer(page: Page) -> None:
    page.evaluate("window.dispatchEvent(new CustomEvent('daedalus:import-session', { detail: {} }))")


def sideways(page: Page) -> bool:
    return bool(page.evaluate("document.documentElement.scrollWidth - innerWidth > 0"))


def desktop(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    say = lambda text: problems.append(f"{lang} 1440px: {text}")  # noqa: E731
    imports = ImportStub()
    context, page, errors = start(browser, lang, False, imports)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    expect(page.locator(".rail")).to_be_visible(timeout=15000)

    # The start screen's card: fresh sessions of the machine, each opening the explorer on itself.
    card = page.locator(".imp-card")
    expect(card).to_contain_text(words["card"], timeout=10000)
    expect(card.locator("[data-fresh]")).to_have_count(3)
    card.locator("[data-fresh]").first.locator(".btn").click()
    sheet = page.locator(".sheet.imp-sheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator(".imp-pv[data-preview]")).to_be_visible(timeout=10000)
    page.keyboard.press("Escape")
    expect(sheet).to_have_count(0)

    # The New project menu.
    page.locator(".sb-newproject").click()
    page.get_by_role("menuitem", name=words["entry"]).click()
    expect(sheet).to_be_visible()
    expect(sheet.locator(".sheet-head h3")).to_have_text(words["title"])

    # The strip: counts, and the missing program dimmed and disabled.
    expect(sheet.locator(".imp-htile[data-harness='claude'] .imp-n")).to_have_text("10")
    cursor = sheet.locator(".imp-htile[data-harness='cursor']")
    expect(cursor).to_be_disabled()
    expect(cursor).to_contain_text(words["notfound"])

    # A folder: the places, the folders inside, the sessions with the first chosen and previewed.
    sheet.locator(".fb-place[data-place$='/smart-home']").click()
    expect(sheet.locator(".imp-lsec", has_text=words["here"])).to_be_visible()
    expect(sheet.locator(".imp-frow[data-folder$='/homeassistant'] .imp-cnt")).to_have_text("1")
    expect(sheet.locator(".imp-frow[data-folder$='/backups']")).to_contain_text(WORDS[lang]["nosessions"])
    expect(sheet.locator(f".imp-srow[data-session='{LOCK}']")).to_have_attribute("aria-selected", "true")
    preview = sheet.locator(f".imp-pv[data-preview='{LOCK}']")
    expect(preview.locator(".imp-stat").first).to_contain_text("214")
    expect(preview.locator(".imp-dest[data-dest='project']")).to_contain_text(words["join"])
    expect(preview.locator(".imp-mopt[data-mode='full']")).to_have_attribute("aria-checked", "true")
    expect(sheet.locator(".imp-go")).to_contain_text(words["import"])
    if sheet.locator(".imp-srow[data-session='09f1c2d8-4b6a-4c8e-9d2f-1a3b5c7d9e0f']").is_visible():
        say("the /init session is not folded away")

    # A large session: a summary and the tail by default, with the warning.
    sheet.locator(f".imp-srow[data-session='{BIG}']").click()
    big = sheet.locator(f".imp-pv[data-preview='{BIG}']")
    expect(big.locator("[data-large]")).to_contain_text(words["large"])
    expect(big.locator(".imp-mopt[data-mode='tail']")).to_have_attribute("aria-checked", "true")
    expect(big.locator(".imp-mopt[data-mode='tail']")).to_contain_text(words["tail"])

    # Imported already: said, with its chat; a second import is a new chat, and the host's refusal opens it.
    sheet.locator(f".imp-srow[data-session='{EARLIER}']").click()
    earlier = sheet.locator(f".imp-pv[data-preview='{EARLIER}']")
    expect(earlier.locator("[data-imported]")).to_contain_text(words["already"])
    expect(sheet.locator(".imp-go")).to_contain_text(words["again"])

    # The search: matches in every folder, grouped, marked.
    sheet.locator(".imp-search input").fill("lock")
    expect(sheet.locator(".imp-lsec", has_text=words["found"])).to_be_visible(timeout=5000)
    expect(sheet.locator(".imp-group")).to_have_count(3)
    expect(sheet.locator(".imp-srow mark").first).to_have_text("lock")
    # A folder that is gone: the import is refused, with why.
    sheet.locator(f".imp-srow[data-session='{GONE}']").click()
    expect(sheet.locator(f".imp-pv[data-preview='{GONE}'] .imp-dest[data-dest='refused']")).to_contain_text(words["refused"])
    expect(sheet.locator(".imp-go")).to_be_disabled()
    sheet.locator(".imp-search input").fill("")

    # A running session: the warning and a snapshot.
    sheet.locator(".fb-place[data-place$='/daedalus']").click()
    sheet.locator(f".imp-srow[data-session='{LIVE}']").click()
    expect(sheet.locator(f".imp-pv[data-preview='{LIVE}'] [data-live]")).to_contain_text(words["live"])
    expect(sheet.locator(".imp-go")).to_contain_text(words["snapshot"])

    # The import: stages with their numbers, then the chat it made.
    sheet.locator(".fb-place[data-place$='/smart-home']").click()
    sheet.locator(f".imp-srow[data-session='{LOCK}']").click()
    expect(sheet.locator(f".imp-pv[data-preview='{LOCK}']")).to_be_visible()
    imports.hold = 3
    page.keyboard.press("Control+Enter")
    job = sheet.locator(".imp-pv[data-job]")
    expect(job.locator("[data-stage='read']")).to_contain_text(words["read"], timeout=5000)
    expect(job.locator("[data-stage='parse']")).to_contain_text(words["parsed"])
    expect(job.locator("[data-stage='mask']")).to_contain_text(words["masked"])
    expect(job.locator(".imp-pstep.now[data-stage='write'] .imp-r")).to_have_text("132 / 214")
    if imports.posted[-1] != {"harness": "claude", "id": LOCK, "mode": "full", "model": "opus", "project_id": "p-home"}:
        say(f"the import was sent as {imports.posted[-1]}")
    imports.hold = None
    page.wait_for_url(f"**/agents/{IMPORTED_ID}*", timeout=10000)
    expect(sheet).to_have_count(0)

    # The imported chat.
    banner = page.locator(".imp-banner")
    expect(banner).to_contain_text(words["banner"], timeout=10000)
    expect(banner).to_contain_text(words["secrets"])
    banner.locator("[data-pull]").click()
    expect(page.locator(".toast", has_text=words["pulled"]).first).to_be_visible(timeout=5000)
    if imports.refreshed != [IMPORTED_ID]:
        say(f"Pull in what's new reached {imports.refreshed}")
    with page.expect_download() as download:
        banner.locator("[data-original]").click()
    if not download.value.suggested_filename.endswith(".jsonl"):
        say(f"the original downloads as {download.value.suggested_filename}")
    expect(page.locator(".imp-handoff")).to_contain_text(words["handoff"])
    page.locator(".thinking-head", has_text=words["worked"]).click()
    labels = page.locator(".act-orig").all_inner_texts()
    if labels[:3] != ["Bash", "Read", "Edit"] and labels != ["Bash", "Read", "Edit", "Bash"]:
        say(f"the steps' own names are {labels}")
    expect(page.locator(".panel .imp-origin")).to_be_visible(timeout=10000)
    expect(page.locator(".panel .dt-section", has_text=words["origin"]).first).to_be_visible()
    expect(page.locator(f".sidebar [data-session='{IMPORTED_ID}'] .imp-hmini")).to_have_text("C")
    if sideways(page):
        say("the chat scrolls sideways")

    # The palette.
    page.keyboard.press("Control+k")
    page.locator(".palette-sheet input").fill(words["palette"])
    page.locator(".palette-row").first.click()
    expect(page.locator(".sheet.imp-sheet")).to_be_visible()
    page.keyboard.press("Escape")

    # The machine not answering.
    imports.host = "down"
    open_explorer(page)
    down = page.locator(".sheet.imp-sheet .imp-down")
    expect(down).to_contain_text(words["down"], timeout=5000)
    expect(down.locator("code")).to_have_text("systemctl --user start daedalus-ptyd")
    imports.host = "up"
    down.get_by_role("button").click()
    expect(page.locator(".sheet.imp-sheet .imp-htile").first).to_be_visible(timeout=5000)
    if errors:
        say(f"page errors: {errors}")
    context.close()


def phone(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    say = lambda text: problems.append(f"{lang} 390px: {text}")  # noqa: E731
    imports = ImportStub()
    context, page, errors = start(browser, lang, True, imports)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    expect(page.locator(".ph-page")).to_be_visible(timeout=15000)
    expect(page.locator(".imp-card [data-fresh]").first).to_be_visible(timeout=10000)
    open_explorer(page)
    sheet = page.locator(".sheet.imp-phone")
    expect(sheet.locator(".imp-ph-head h3")).to_have_text(words["phone_title"])
    expect(sheet.locator("[data-screen='harness']")).to_contain_text(words["fresh"])
    sheet.locator(".imp-ph-row[data-harness='claude']").click()
    expect(sheet.locator("[data-screen='folder']")).to_be_visible()
    sheet.locator(".imp-ph-chips button[data-place$='/smart-home']").click()
    sheet.locator(f".imp-srow[data-session='{LOCK}']").click()
    expect(sheet.locator(f"[data-screen='session'] .imp-pv[data-preview='{LOCK}']")).to_be_visible(timeout=5000)
    if sideways(page):
        say("the session screen scrolls sideways")
    imports.hold = 3
    sheet.locator(".imp-go").click()
    expect(sheet.locator(".imp-pstep.now[data-stage='write']")).to_be_visible(timeout=5000)
    imports.hold = None
    page.wait_for_url(f"**/agents/{IMPORTED_ID}*", timeout=10000)
    expect(page.locator(".imp-banner")).to_contain_text(words["banner"], timeout=10000)
    expect(page.locator(".imp-handoff")).to_be_visible()
    if sideways(page):
        say("the imported chat scrolls sideways")
    if errors:
        say(f"page errors: {errors}")
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for run in (desktop, phone):
                try:
                    run(browser, lang, problems)
                except Exception as exc:  # noqa: BLE001 — one failure must not hide the rest
                    problems.append(f"{lang} {run.__name__}: {exc}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("ok: importing a session from another program")
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
