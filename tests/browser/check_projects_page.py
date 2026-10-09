"""The desktop's projects page, which replaced the "All projects" sheet. English and Russian, 1440 px.

What is checked:

- the sidebar's "All projects" opens the page in the conversation's place, at ``/app/agents?view=projects``,
  and no sheet opens over it;
- the table lists the projects Agents mode keeps: not a chat's scratch project, not an orchestrated
  project, not an archived one — the archived are folded under "Archived" with Restore;
- the columns hold the path, the number of chats, what is live now (a waiting chat as an amber pill),
  where the chats run and when anything last happened;
- the search and the Waiting filter narrow the rows;
- a row opens a new chat in its project, with the project picked under the composer;
- the orchestrated projects are named in the note at the foot, whose link goes to orchestration mode.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_projects_page.py
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app, folders  # noqa: E402

BASE = shots.BASE
ORCHESTRATED = {"id": "8d2f6a0c4e11", "name": "DevHub", "folders": folders("/home/operator/work/devhub"), "created_at": shots.ago(days=3),
                "settings": {"snapshots": True, "orchestrator": {"enabled": True, "session_id": "orch-devhub", "model": ""}}, "sessions": []}
SCRATCH = {"id": "c0ffee000001", "name": "Translate the KV-cache article", "folders": folders("/home/operator/.daedalus/workspaces/c0ffee000001"), "created_at": shots.ago(hours=3),
           "settings": {"snapshots": True, "ephemeral": True}, "sessions": [{"id": "c0ffee000001", "title": "Translate the KV-cache article", "running": False}]}
ARCHIVED = {"id": "a7c4e2b0d999", "name": "Old landing page", "entity_revision": 3, "folders": folders("/home/operator/work/landing"), "created_at": shots.ago(days=40),
            "settings": {"snapshots": False, "archived": True}, "sessions": []}
WORDS = {"en": {"waiting": "1 waiting for you", "filter": "Waiting", "note": "DevHub", "chats": "3 chats"},
         "ru": {"waiting": "1 ждёт вас", "filter": "Ждут", "note": "DevHub", "chats": "3 чата"}}


def kept_projects() -> list[dict]:
    """The shared fixture's projects as this page sees them. The fixture leaves Expenses and Weekly
    digest as single chats' scratch projects; here Weekly digest has been kept, so the page has a
    project with a waiting chat to show, while Expenses stays a chat and must not be listed."""
    projects = copy.deepcopy(shots.PROJECTS)
    for project in projects:
        if project["id"] == shots.P4:
            project["settings"]["ephemeral"] = False
    return projects


def is_kept(project: dict) -> bool:
    return bool(project.get("system") or project["settings"].get("system")) or not project["settings"].get("ephemeral")


def serve(page: Page) -> None:
    def route(r) -> None:  # type: ignore[no-untyped-def]
        path = urlsplit(r.request.url).path
        if path.endswith("/api/projects") and r.request.method == "GET":
            return shots.respond(r, [*kept_projects(), ORCHESTRATED, SCRATCH, ARCHIVED])
        return shots.stub(r)

    page.route("**/api/**", route)


def run(browser, lang: str, check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"{lang} 1440"
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    serve(page)
    page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".start-composer", timeout=15000)

    # The sidebar's opener: the projects chip today; whatever button replaces it calls the same opener.
    page.locator(".sidebar .project-chip, .sidebar [data-action='all-projects']").first.click()
    page.wait_for_selector(".projects-page", timeout=10000)
    check(page.evaluate("location.pathname + location.search").endswith("/agents?view=projects"), f"{where}: All projects opens the page at /agents?view=projects")
    check(page.locator(".sheet").count() == 0, f"{where}: and no sheet over it")

    rows = page.locator(".projects-row:not(.head)")
    ids = [rows.nth(i).get_attribute("data-project") for i in range(rows.count())]
    listed = [p["id"] for p in kept_projects() if is_kept(p)]
    check(sorted(ids) == sorted(listed), f"{where}: the table lists the {len(listed)} kept projects ({len(ids)})")
    check(shots.P2 not in ids, f"{where}: a chat's scratch project is listed as the chat, not here")
    check(SCRATCH["id"] not in ids, f"{where}: a chat's scratch project is not a project")
    check(ORCHESTRATED["id"] not in ids, f"{where}: an orchestrated project lives in orchestration mode")
    check(ARCHIVED["id"] not in ids, f"{where}: an archived project is not in the table")

    bakery = page.locator(f".projects-row[data-project='{shots.P1}']")
    text = bakery.inner_text()
    check("/home/operator/work/bakery" in text and words["chats"] in text, f"{where}: a row holds the path and the chats ({text!r})")
    check(bakery.locator(".term-env").count() == 1, f"{where}: and where its chats run")
    digest = page.locator(f".projects-row[data-project='{shots.P4}'] .projects-pill.warn")
    check(digest.count() == 1 and digest.inner_text().strip() == words["waiting"], f"{where}: a waiting chat is an amber pill ({digest.all_inner_texts()})")
    acts = bakery.locator(".projects-acts")
    page.mouse.move(0, 0)
    page.wait_for_timeout(200)
    check(acts.evaluate("el => getComputedStyle(el).opacity") == "0", f"{where}: the row's actions wait for hover")
    bakery.hover()
    page.wait_for_timeout(300)
    check(acts.evaluate("el => getComputedStyle(el).opacity") == "1", f"{where}: and show on it")

    # Search and the Waiting filter narrow it.
    page.locator(".projects-search input").fill("bak")
    check(page.locator(".projects-row:not(.head)").count() == 1, f"{where}: the search finds Bakery site alone")
    page.locator(".projects-search input").fill("")
    page.locator(".projects-chip", has_text=words["filter"]).click()
    shown = page.locator(".projects-row:not(.head)")
    check(shown.count() == 1 and shown.first.get_attribute("data-project") == shots.P4, f"{where}: Waiting keeps the project with a waiting chat")
    page.locator(".projects-chip").first.click()

    # Archived, folded, with Restore; orchestrated, in the note.
    fold = page.locator(".projects-archived")
    check(fold.count() == 1 and "1" in fold.locator("summary").inner_text(), f"{where}: the archived project is folded under Archived")
    fold.locator("summary").click()
    check(fold.locator(f"[data-project='{ARCHIVED['id']}'] .btn").count() == 1, f"{where}: with Restore")
    note = page.locator(".projects-note")
    check(note.count() == 1 and words["note"] in note.inner_text(), f"{where}: the note names the orchestrated project ({note.all_inner_texts()})")

    # A row starts a chat in its project.
    bakery.locator(".projects-name").click()
    page.wait_for_selector(".start-where", timeout=10000)
    check(page.evaluate("location.search").startswith(f"?in={shots.P1}"), f"{where}: a row opens a new chat in its project ({page.evaluate('location.search')})")
    check(page.locator(f".start-where-chip[data-project='{shots.P1}']").get_attribute("aria-pressed") == "true", f"{where}: with the project picked under the composer")

    page.goto(f"{BASE}/agents?view=projects&token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".projects-note a", timeout=10000)
    page.locator(".projects-note a").click()
    page.wait_for_function("() => location.pathname.includes('/orchestration')", timeout=10000)
    check("/orchestration" in page.evaluate("location.pathname"), f"{where}: the note's link goes to orchestration mode")
    context.close()


def main() -> int:
    expect_app(BASE)
    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures += 1

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shots.CHROMIUM, args=shots.FAKE_MEDIA)
        for lang in ("en", "ru"):
            run(browser, lang, check)
        browser.close()
    return failures + shots.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
