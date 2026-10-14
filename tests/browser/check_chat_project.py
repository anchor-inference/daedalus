"""A chat made a project and turned back into its chat, and a chat dragged onto a project.

On a desktop: "Make a project" in a chat's ⋯ menu asks for a name, offers the chat's title, and sends
one ``as-project`` command with the name typed; the sidebar then lists a project with the chat inside.
"Turn back into a chat" on that project's menu sends ``as-chat`` and the chat is back among the chats;
on a project with several chats the item is there but disabled. A chat dragged onto a project row is
moved into it working in its folder, with an Undo that moves it back; a drop on the installation's own
project does nothing. A chat's menu lists no subagents. On a phone the same two commands live in the
row's sheet and the project section's sheet. In English and Russian, and nothing runs off the side.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 9047 /tmp/app-root &
    APP_URL=http://127.0.0.1:9047/app python3 tests/browser/check_chat_project.py

Pictures go to SHOTS_DIR when it is set. Exit status is the number of checks that failed.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = shots.BASE
SHOTS = os.environ.get("SHOTS_DIR", "")
WORDS = {
    "en": {"make": "Make a project", "back": "Turn back into a chat", "undo": "Undo"},
    "ru": {"make": "Сделать проектом", "back": "Снова сделать чатом", "undo": "Вернуть"},
}
NAME = {"en": "Household budget", "ru": "Домашний бюджет"}

ORIGINAL = (copy.deepcopy(shots.PROJECTS), copy.deepcopy(shots.SESSIONS), copy.deepcopy(shots.FOLDER_COUNTS))
POSTS: list[tuple[str, dict]] = []


def reset() -> None:
    """The invented installation as the pictures have it, with the revision each project command needs."""
    shots.PROJECTS[:] = copy.deepcopy(ORIGINAL[0])
    shots.SESSIONS[:] = copy.deepcopy(ORIGINAL[1])
    shots.FOLDER_COUNTS.clear()
    shots.FOLDER_COUNTS.update(copy.deepcopy(ORIGINAL[2]))
    for project in shots.PROJECTS:
        project["entity_revision"] = 1
    POSTS.clear()


def project_of(pid: str) -> dict:
    return next(p for p in shots.PROJECTS if p["id"] == pid)


def recount() -> None:
    """What the host counts per project: its top-level chats, which decide the way back."""
    for project in shots.PROJECTS:
        top = [s for s in shots.SESSIONS if s["project_id"] == project["id"] and not s["metadata"].get("subagent_of") and not s["metadata"].get("forked_from")]
        shots.FOLDER_COUNTS.setdefault(project["id"], {"total": 0, "active": 0, "loops": 0, "last_message_at": ""})["total"] = len(top)
        project["sessions"] = [{"id": s["id"], "title": s["title"], "running": s["status"] == "running"} for s in top]


def stub(route) -> None:  # type: ignore[no-untyped-def]
    request = route.request
    rel = request.url.split("?", 1)[0]
    rel = rel[rel.index("/api/"):]
    if request.method == "POST" and rel.startswith("/api/projects/") and rel.endswith(("/as-project", "/as-chat")):
        body = json.loads(request.post_data or "{}")
        POSTS.append((rel, body))
        project = project_of(rel.split("/")[3])
        if body.get("expected_entity_revision") != project["entity_revision"]:
            return shots.respond(route, {"detail": {"reason": "the entity has changed", "current_revision": project["entity_revision"]}}, status=409)
        if rel.endswith("/as-project"):
            project["name"] = body["name"]
            project["settings"] = {**project["settings"], "ephemeral": False}
        else:
            if shots.FOLDER_COUNTS[project["id"]]["total"] != 1:
                return shots.respond(route, {"detail": "only a project with exactly one chat can become a chat"}, status=409)
            project["settings"] = {**project["settings"], "ephemeral": True}
        for session in shots.SESSIONS:
            if session["project_id"] == project["id"]:
                session["project"] = project["name"]
        project["entity_revision"] += 1
        return shots.respond(route, {"project_id": project["id"], "change": "kept", "receipt_id": "r", "entity_revision": project["entity_revision"]})
    if request.method == "POST" and rel.startswith("/api/sessions/") and rel.endswith("/project"):
        body = json.loads(request.post_data or "{}")
        POSTS.append((rel, body))
        session = next(s for s in shots.SESSIONS if s["id"] == rel.split("/")[3])
        session["project_id"] = body["project_id"]
        session["project"] = project_of(body["project_id"])["name"]
        recount()
        return shots.respond(route, {"id": session["id"], "project_id": body["project_id"], "project": session["project"], "workspace": "/w"})
    return shots.stub(route)


def main() -> int:
    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures += 1

    def fits(page, where: str) -> None:  # type: ignore[no-untyped-def]
        wide = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        check(wide <= 1, f"{where}: nothing runs off the side ({wide}px)")
        sheet = page.locator(".sheet").last
        if sheet.count():
            box = sheet.bounding_box()
            width = page.viewport_size["width"]
            check(bool(box and box["x"] >= -1 and box["x"] + box["width"] <= width + 1), f"{where}: the sheet sits inside the window ({box})")

    def shoot(page, name: str) -> None:  # type: ignore[no-untyped-def]
        if SHOTS:
            page.wait_for_timeout(700)  # past the sheet's rise, so the picture is the sheet at rest
            page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shots.CHROMIUM)
        for lang in ("en", "ru"):
            words = WORDS[lang]

            # -- the desktop ----------------------------------------------------------------
            reset()
            context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            page = context.new_page()
            page.route("**/api/**", stub)
            page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
            side = page.locator("nav.sidebar")
            expect(side.locator(".sb-row").first).to_be_visible(timeout=15000)

            def item(name: str, page=page):  # type: ignore[no-untyped-def]
                return page.get_by_role("menuitem", name=name, exact=True)

            def menu_of(row, side=side):  # type: ignore[no-untyped-def]
                row.hover()
                row.locator(".sb-acts button[aria-haspopup='menu']").first.click()

            # A leader's menu lists no subagents: they fold out from its plate.
            leader = side.locator(f"[data-session='{shots.S1}']")
            if not leader.is_visible():
                side.locator(f"[data-project='{shots.P1}'] .sb-prow").click()
            menu_of(leader)
            labels = page.locator(".menu [role='menuitem']").all_inner_texts()
            check(not any(name in " ".join(labels) for name in ("link-check", "photo-alt", "price-audit")), f"{lang}: a chat's menu lists no subagents ({labels})")
            page.keyboard.press("Escape")

            # Make a project from the chat's menu, under a new name.
            expense = side.locator(f".sb-day [data-session='{shots.S6}']")
            menu_of(expense)
            item(words["make"]).click()
            field = page.locator("#make-project-name")
            expect(field).to_be_visible(timeout=5000)
            check(field.input_value() == "Expense tracker", f"{lang}: the name offered is the chat's title ({field.input_value()})")
            fits(page, f"{lang} desktop sheet")
            shoot(page, f"chatproject-make-desktop-{lang}")
            field.fill(NAME[lang])
            page.locator(".sheet button[type='submit']").click()
            group = side.locator(f"[data-project='{shots.P2}']")
            expect(group.locator(".sb-prow")).to_be_visible(timeout=5000)
            sent = [b for path, b in POSTS if path.endswith(f"{shots.P2}/as-project")]
            check(len(sent) == 1 and sent[0].get("name") == NAME[lang] and sent[0].get("expected_entity_revision") == 1, f"{lang}: one as-project with the name typed ({sent})")
            check(group.locator(".sb-prow .sb-t").inner_text() == NAME[lang], f"{lang}: the project row carries the new name")
            expect(group.locator(f"[data-session='{shots.S6}']")).to_be_visible(timeout=5000)
            check(side.locator(f".sb-day [data-session='{shots.S6}']").count() == 0, f"{lang}: the chat left the chats section")
            check(f"/agents/{shots.S6}" in page.url, f"{lang}: the chat is opened, selected inside its project ({page.url})")

            # A project of three chats offers the way back disabled.
            menu_of(side.locator(f"[data-project='{shots.P1}'] .sb-prow"))
            check(item(words["back"]).is_disabled(), f"{lang}: a project with three chats offers the way back disabled")
            page.keyboard.press("Escape")

            # And back.
            menu_of(group.locator(".sb-prow"))
            item(words["back"]).click()
            expect(side.locator(f".sb-day [data-session='{shots.S6}']")).to_be_visible(timeout=5000)
            check(any(path.endswith(f"{shots.P2}/as-chat") for path, _ in POSTS), f"{lang}: as-chat was sent")
            check(side.locator(f"[data-project='{shots.P2}']").count() == 0, f"{lang}: the project row is gone, the chat is a chat again")

            # Drag a chat onto a project; Undo moves it back.
            digest = side.locator(f".sb-day [data-session='{shots.S4}']")
            support = side.locator(f"[data-project='{shots.P3}'] .sb-prow")
            digest.drag_to(support)
            moves = [b for path, b in POSTS if path == f"/api/sessions/{shots.S4}/project"]
            check(moves == [{"project_id": shots.P3, "own_directory": False}], f"{lang}: the drop posts the move into the project's folder ({moves})")
            expect(side.locator(f"[data-project='{shots.P3}'] [data-session='{shots.S4}']")).to_be_visible(timeout=5000)
            page.locator(".toast button", has_text=words["undo"]).click()
            expect(side.locator(f".sb-day [data-session='{shots.S4}']")).to_be_visible(timeout=5000)
            moves = [b for path, b in POSTS if path == f"/api/sessions/{shots.S4}/project"]
            check(len(moves) == 2 and moves[1]["project_id"] == shots.P4, f"{lang}: Undo moves it back where it was ({moves})")

            # The installation's own project takes no drop.
            before = len(POSTS)
            side.locator(f".sb-day [data-session='{shots.S6}']").drag_to(side.locator(f"[data-project='{shots.PV}'] .sb-prow"))
            page.wait_for_timeout(400)
            check(len(POSTS) == before, f"{lang}: a drop on the installation's own project moves nothing")
            fits(page, f"{lang} desktop")
            context.close()

            # -- the phone ------------------------------------------------------------------
            for scheme in ("dark", "light"):
                reset()
                context = browser.new_context(viewport=shots.PHONE, device_scale_factor=2, color_scheme=scheme, is_mobile=True, has_touch=True)
                page = context.new_page()
                page.route("**/api/**", stub)
                page.goto(f"{BASE}/agents?view=chats&token=t&scheme={scheme}&lang={lang}")
                expect(page.locator(".ph-row").first).to_be_visible(timeout=15000)

                def sheet_pick(name: str, page=page):  # type: ignore[no-untyped-def]
                    return page.locator(".ph-actions .ph-mrow", has_text=name).first

                page.locator(f"[data-session='{shots.S6}'] .ph-row-more").click()
                sheet_pick(words["make"]).click()
                field = page.locator("#make-project-name")
                expect(field).to_be_visible(timeout=5000)
                fits(page, f"{lang} phone {scheme} sheet")
                shoot(page, f"chatproject-make-phone-{lang}-{scheme}")
                field.fill(NAME[lang])
                page.locator(".sheet button[type='submit']").click()
                section = page.locator(f"section[data-project='{shots.P2}']")
                expect(section.locator(f"[data-session='{shots.S6}']")).to_be_visible(timeout=5000)
                check(NAME[lang] in section.locator(".ph-sec").first.inner_text(), f"{lang} phone {scheme}: the section is headed by the new name")

                # The project with three chats: the item is there, disabled, and says why.
                page.locator(f"section[data-project='{shots.P1}'] .ph-sec-act button[aria-haspopup='menu']").click()
                busy = sheet_pick(words["back"])
                check(busy.is_disabled(), f"{lang} phone {scheme}: three chats, the way back is disabled")
                fits(page, f"{lang} phone {scheme} project sheet")
                shoot(page, f"chatproject-back-phone-{lang}-{scheme}")
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)

                section.locator(".ph-sec-act button[aria-haspopup='menu']").click()
                sheet_pick(words["back"]).click()
                expect(page.locator(f"section[data-project='{shots.P2}']")).to_have_count(0, timeout=5000)
                check(page.locator(f"[data-session='{shots.S6}']").count() == 1, f"{lang} phone {scheme}: the chat is listed among the chats again")
                fits(page, f"{lang} phone {scheme}")
                context.close()
        browser.close()
    reset()
    return failures + shots.UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(main())
