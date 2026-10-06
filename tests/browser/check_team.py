"""Hire, edit and dismiss a staff member on a project's team page, on a desktop and a phone, in both languages.

What is checked is what the operator relies on: a Daedalus member can be hired and shows up with its
badge; a command-line agent that cannot run here is offered but disabled, with the reason beside it;
the branch a worktree will get is previewed from the name; an edit is sent; a dismissal asks first
and takes the member off the list; a member who works cannot be dismissed, and the page says why.
And the page fits: nothing scrolls sideways at 390 px.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, BoardStub, TeamStub, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PID = "9f3c2a1b7d40"

WORDS = {
    "en": {"title": "Team · Bakery", "hire": "Hire", "save": "Save", "dismiss": "Dismiss", "signedout": "not signed in", "missing": "not installed", "working": "working", "edit": "Edit {name}", "empty": "No staff yet", "shared": "Shared folder", "worktree": "Own worktree", "blocked": "This environment cannot safely write in a shared folder"},
    "ru": {"title": "Команда · Bakery", "hire": "Нанять", "save": "Сохранить", "dismiss": "Уволить", "signedout": "нет входа", "missing": "не установлен", "working": "работает", "edit": "Изменить: {name}", "empty": "Сотрудников пока нет", "shared": "Общая папка", "worktree": "Свой worktree", "blocked": "Эта среда не может безопасно писать в общую папку"},
}


def project() -> dict:
    listed = folders("/home/operator/work/bakery")
    listed[0]["is_git"] = True
    return {"id": PID, "name": "Bakery", "folders": listed, "created_at": "2026-09-20T00:00:00Z", "settings": {"snapshots": True, "system": "", "ephemeral": False}, "system": "", "sessions": []}


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def run_one(page: Page, lang: str, width: int, unhandled: Unhandled) -> None:
    words = WORDS[lang]
    team = TeamStub(project(), staff=[TeamStub.member("st-cleo", "Cleo", harness="claude", status="working", sessions=9, role="Reviews every change", model="opus", permission_mode="acceptEdits", color="violet")])
    team.staff[0]["project_id"] = PID
    board = BoardStub(project())

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        # The team page is a page of the project's focus mode, whose column reads the project's board too.
        answered = team.answer(request.method, path, url.query, request.post_data_json if request.method in ("POST", "PATCH") else None) or board.answer(request.method, path, url.query, None)
        if answered is not None:
            status, body = answered
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        if path == "/api/projects":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps([project()]))
        if path == "/api/sessions":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"sessions": [], "projects": []}))
        if path == "/api/settings":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"presets": {}, "model": {}}))
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        route.fulfill(status=200, content_type="application/json", body="[]")

    page.route("**/api/**", stub)
    page.goto(f"{BASE}/project/{PID}/team?token=t&lang={lang}")
    # On a phone the team is a tab of the project (project/phone.tsx): the header names the project,
    # a row opens the member's work and the button beside it edits the member, under the same name.
    phone = width < 1024
    row = ".phone-staff-item" if phone else ".staff-row"
    expect(page.get_by_role("heading", name="Bakery" if phone else words["title"], exact=True)).to_be_visible()
    cleo = page.locator(row, has_text="Cleo")
    expect(cleo).to_contain_text(words["working"])
    expect(cleo.locator(".harness-badge")).to_have_text("CC")
    if not phone:
        expect(cleo).to_contain_text("Claude Code · opus · acceptEdits")
    fits(page, f"{lang} {width} list")

    # Hire a Daedalus member. The executors that cannot run here are there, disabled, saying why.
    page.get_by_role("button", name=words["hire"], exact=True).first.click()
    sheet = page.locator(".sheet.staff-sheet")
    expect(sheet).to_be_visible()
    codex = sheet.locator(".executor", has_text="Codex")
    expect(codex).to_be_disabled()
    expect(codex).to_contain_text(words["signedout"])
    grok = sheet.locator(".executor", has_text="Grok Build")
    expect(grok).to_be_disabled()
    expect(grok).to_contain_text(words["missing"])
    expect(sheet.locator(".executor", has_text="Claude Code")).to_be_enabled()
    expect(sheet.locator(".executor", has_text="Daedalus")).to_have_attribute("aria-pressed", "true")
    sheet.locator("#staff-name").fill("Ada Lovelace")
    sheet.locator("#staff-role").fill("Writes the menu page")
    sheet.locator("#staff-agent").select_option("reviewer")
    expect(sheet.locator(".branch-preview")).to_contain_text("agent/ada-lovelace/")
    fits(page, f"{lang} {width} hire sheet")
    sheet.locator(".sheet-foot").get_by_role("button", name=words["hire"], exact=True).click()
    expect(sheet).to_have_count(0)
    hired = team.hired[-1]
    assert (hired["name"], hired["harness"], hired["agent"], hired["isolation"], hired["role"]) == ("Ada Lovelace", "daedalus", "reviewer", "worktree", "Writes the menu page"), hired
    ada = page.locator(row, has_text="Ada Lovelace")
    expect(ada).to_be_visible()
    expect(ada.locator(".harness-badge")).to_have_text("D")

    # A command-line member: its own agents and models come from the catalog, and it takes a permission mode.
    def unavailable_shared(route) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"envs": [
            {"env": "container", "available": True, "sandbox": "ok", "containment": {"kind": "cgroup_v2", "available": False}},
            {"env": "host", "available": True, "sandbox": "ok", "containment": {"kind": "cgroup_v2", "available": False}},
        ]}))

    page.route("**/api/terminals/envs", unavailable_shared)
    page.get_by_role("button", name=words["hire"], exact=True).first.click()
    expect(sheet).to_be_visible()
    sheet.locator(".executor", has_text="Claude Code").click()
    expect(sheet.locator("#staff-permissions")).to_be_visible()
    isolation = sheet.get_by_role("group", name="Isolation" if lang == "en" else "Изоляция")
    # A machine that cannot contain a shared writer still offers it: the worker then runs uncontained.
    isolation.get_by_role("button", name=words["shared"]).click()
    expect(sheet.get_by_text(words["blocked"], exact=False)).to_have_count(0)
    fits(page, f"{lang} {width} shared without containment")
    isolation.get_by_role("button", name=words["worktree"]).click()
    sheet.locator("#staff-name").fill("Rex")
    sheet.locator("#staff-agent").select_option("code-reviewer")
    sheet.locator("#staff-model").select_option("opus")
    sheet.locator("#staff-permissions").fill("acceptEdits")
    sheet.locator(".sheet-foot").get_by_role("button", name=words["hire"], exact=True).click()
    expect(sheet).to_have_count(0)
    rex = team.hired[-1]
    assert (rex["harness"], rex["agent"], rex["model"], rex["permission_mode"], rex["env"]) == ("claude", "code-reviewer", "opus", "acceptEdits", ""), rex
    expect(page.locator(row, has_text="Rex").locator(".harness-badge")).to_have_text("CC")

    # Edit it.
    page.get_by_role("button", name=words["edit"].format(name="Ada Lovelace")).click()
    expect(sheet).to_be_visible()
    expect(sheet.locator("#staff-name")).to_have_count(0)
    sheet.locator("#staff-role").fill("Tests the menu page")
    sheet.get_by_role("button", name=words["save"], exact=True).click()
    expect(sheet).to_have_count(0)
    assert team.patched[-1] == {"role": "Tests the menu page"}, team.patched[-1]
    expect(ada).to_contain_text("Tests the menu page")

    # Dismiss it: asked first, then gone from the current team.
    page.get_by_role("button", name=words["edit"].format(name="Ada Lovelace")).click()
    sheet.get_by_role("button", name=words["dismiss"]).click()
    page.locator(".dialog").get_by_role("button", name=words["dismiss"]).click()
    expect(page.locator(row, has_text="Ada Lovelace")).to_have_count(0)

    # A member at work cannot be dismissed; the refusal is shown as the host words it.
    page.get_by_role("button", name=words["edit"].format(name="Cleo")).click()
    sheet.get_by_role("button", name=words["dismiss"]).click()
    page.locator(".dialog").get_by_role("button", name=words["dismiss"]).click()
    expect(page.locator(".toast")).to_contain_text("release the session first")
    expect(cleo).to_be_visible()
    fits(page, f"{lang} {width} after")


def run() -> int:
    unhandled = Unhandled()
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width, height, mobile in ((1440, 900, False), (390, 844, True)):
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                run_one(page, lang, width, unhandled)
                context.close()
        browser.close()
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
