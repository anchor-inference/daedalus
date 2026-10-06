"""Archive a project and restore it from the switcher's fold; clean up worker worktrees safely.

What the operator relies on: an archived project leaves the project list and appears under a small
"Archived (n)" fold, Restore brings it back, and the write carries the project's revision. In the
settings sheet, "Worker worktrees" lists each worktree with its member, branch, size, uncommitted
changes, live worker and last commit; Remove is offered only for one nobody works in and with
nothing uncommitted, and a merged branch is deleted only when the operator ticks that.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def project(pid: str, name: str) -> dict:
    return {"id": pid, "name": name, "entity_revision": 1, "folders": folders(f"/work/{pid}"), "created_at": "2026-09-19T00:00:00Z",
            "settings": {"snapshots": False, "system": "", "archived": False}, "system": "", "sessions": []}


def worktree(name: str, member: str, *, live: bool = False, changes: int = 0, merged: bool = False) -> dict:
    at = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    return {"folder_id": "f-p1", "path": f"/work/p1/.agents/worktrees/{name}", "name": name,
            "member": {"id": f"st-{name}", "name": member, "dismissed": False}, "branch": f"agent/{name}/1-task", "base": "main",
            "size_bytes": 3 * 1024 * 1024 * 1024 if name == "cy" else 42 * 1024 * 1024, "changes": changes, "last_commit_at": at,
            "merged": merged, "prunable": False,
            "live": {"staff_id": f"st-{name}", "name": member, "status": "working"} if live else None,
            "blocked": "live" if live else "changes" if changes else ""}


def run() -> int:
    projects = [project("p1", "Bakery"), project("p2", "Old shop")]
    trees = [worktree("ada", "Ada", live=True), worktree("bo", "Bo", changes=2), worktree("cy", "Cy", merged=True)]
    patches: list[dict] = []
    removals: list[dict] = []
    unhandled = Unhandled()

    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        if path == "/api/projects" and request.method == "GET":
            return answer(route, projects)
        if path == "/api/sessions":
            return answer(route, {"sessions": [], "projects": [{**p, "total": 0, "active": 0, "loops": 0, "last_message_at": ""} for p in projects]})
        if path == "/api/settings":
            return answer(route, {"presets": {}, "model": {}})
        if path.startswith("/api/projects/") and path.count("/") == 3 and request.method == "PATCH":
            payload = request.post_data_json
            patches.append(payload)
            found = next(p for p in projects if p["id"] == path.split("/")[3])
            if payload["expected_entity_revision"] != found["entity_revision"]:
                return answer(route, {"detail": "project changed", "current_revision": found["entity_revision"]}, status=409)
            if "archived" in payload:
                found["settings"]["archived"] = payload["archived"]
            found["entity_revision"] += 1
            return answer(route, {"receipt_id": f"r-{len(patches)}", "entity_revision": found["entity_revision"], "project_id": found["id"]})
        if path == "/api/projects/p1/worktrees" and request.method == "GET":
            return answer(route, {"worktrees": trees, "problems": []})
        if path == "/api/projects/p1/worktrees/remove" and request.method == "POST":
            payload = request.post_data_json
            removals.append(payload)
            trees[:] = [t for t in trees if t["path"] != payload["path"]]
            return answer(route, {"removed": True, "branch_deleted": bool(payload["delete_branch"])})
        if path.endswith("/orchestrator/authority") and request.method == "GET":
            return answer(route, {"project_id": path.split("/")[3], "entity_revision": 1, "current_coordinator_session_id": "",
                                  "available_bundles": [], "grants": [], "readiness_blockers": []})
        if path.startswith("/api/lifecycle/project_goal/") and request.method == "GET":
            pid = path.split("/")[4]
            return answer(route, {"parent_kind": "project_goal", "parent_id": pid, "project_id": pid, "entity_revision": 1,
                                  "source_revision": 1, "generation": 1, "cancel_state": "active", "children": [],
                                  "owned_descendants": [], "preview_fingerprint": "a" * 64})
        if path == "/api/plugins/catalog":
            return answer(route, [])
        if path == "/api/plugins" and request.method == "GET":
            return answer(route, {"items": [], "collection_revision": 1})
        if path == "/api/plugins/safe-mode":
            return answer(route, {"enabled": False})
        if path == "/api/runtime/hosts" and request.method == "GET":
            return answer(route, {"items": [], "collection_revision": 1})
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        answer(route, [])

    def confirm(page) -> None:  # type: ignore[no-untyped-def]
        page.locator(".sheet-backdrop.confirm .dialog button").last.click()

    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/agents?token=t&lang=en")

        # Archive "Old shop" from its settings: it leaves the list for the fold.
        page.locator(".project-chip").click()
        expect(page.locator(".project-row:not(.archived)")).to_have_count(2)
        expect(page.locator(".project-archived")).to_have_count(0)
        page.locator(".project-row", has_text="Old shop").get_by_role("button", name="Settings for Old shop").click()
        page.get_by_role("button", name="Archive", exact=True).click()
        expect(page.locator(".sheet-backdrop.confirm .dialog")).to_contain_text("Nothing is deleted")
        confirm(page)
        fold = page.locator("details.project-archived")
        expect(fold.locator("summary")).to_have_text("Archived (1)")
        assert patches[-1]["archived"] is True and patches[-1]["expected_entity_revision"] == 1, patches[-1]
        expect(page.locator(".project-row:not(.archived)")).to_have_count(1)
        expect(page.locator(".project-row:not(.archived)")).to_contain_text("Bakery")

        # Restore it from the fold: back in the list, the fold gone.
        fold.locator("summary").click()
        fold.locator(".project-row", has_text="Old shop").get_by_role("button", name="Restore").click()
        expect(page.locator(".project-row:not(.archived)")).to_have_count(2)
        expect(page.locator("details.project-archived")).to_have_count(0)
        assert patches[-1]["archived"] is False and patches[-1]["expected_entity_revision"] == 2, patches[-1]

        # Worker worktrees, folded until opened.
        page.locator(".project-row", has_text="Bakery").get_by_role("button", name="Settings for Bakery").click()
        section = page.locator("details.project-worktrees")
        expect(section.locator(".worktree-row")).to_have_count(0)
        section.locator("summary").click()
        expect(section.locator(".worktree-row")).to_have_count(3)
        ada, bo, cy = (section.locator(f".worktree-row[data-worktree='{name}']") for name in ("ada", "bo", "cy"))
        expect(ada).to_contain_text("Ada")
        expect(ada).to_contain_text("working")
        expect(ada).to_contain_text("agent/ada/1-task")
        expect(ada).to_contain_text("Ada is working in it")
        expect(ada.get_by_role("button", name="Remove")).to_be_disabled()
        expect(bo).to_contain_text("2 uncommitted changes")
        expect(bo).to_contain_text("uncommitted changes; commit or discard them first")
        expect(bo.get_by_role("button", name="Remove")).to_be_disabled()
        expect(cy).to_contain_text("3.0 GB")
        expect(cy).to_contain_text("last commit")
        expect(cy).to_contain_text("merged")
        for width in (320, 390):
            page.set_viewport_size({"width": width, "height": 800})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"worktrees overflow at {width}px"
        page.set_viewport_size({"width": 1440, "height": 900})
        cy.get_by_role("checkbox").check()
        cy.get_by_role("button", name="Remove").click()
        expect(page.locator(".sheet-backdrop.confirm .dialog")).to_contain_text("all of it is already in main")
        confirm(page)
        expect(section.locator(".worktree-row")).to_have_count(2)
        assert removals == [{"folder_id": "f-p1", "path": "/work/p1/.agents/worktrees/cy", "delete_branch": True}], removals
        browser.close()
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
