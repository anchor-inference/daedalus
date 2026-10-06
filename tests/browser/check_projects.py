"""Exercise name-only project creation and the optional directory browser."""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def run() -> int:
    projects: list[dict] = []
    created: list[dict] = []
    installed: list[dict] = []
    staged: list[dict] = []
    lifecycle_commands: list[dict] = []
    lifecycle_attempts: list[dict] = []
    lifecycle_fingerprint = ["b" * 64]
    authority_revision = [1]
    authority_grants: list[dict] = []
    authority_requests: list[dict] = []
    authority_receipts: dict[str, tuple[dict, dict]] = {}
    authority_unknown = [1]
    current_coordinator = ["coordinator-current"]
    handoff_status: list[dict | None] = [None]
    handoff_requests: list[dict] = []
    handoff_receipts: dict[str, tuple[dict, dict]] = {}
    handoff_unknown = [1]
    resource_profile = {"configured": False, "project_id": "p1", "entity_revision": 1,
                        "profile_revision": None, "state": "disabled", "limits": None,
                        "min_free_disk_bytes": 0,
                        "disk_quota_supported": False}
    resource_attempts: list[dict] = []
    resource_receipts: dict[str, tuple[dict, dict]] = {}
    host_list_failure = [False]
    host_list_item = [False]
    unhandled = Unhandled()
    extension = {"id": "project_status", "version": "1.0.0", "display_name": "Project status", "description": "Read task counts by status for one selected project.", "capabilities": ["board.read"], "tools": [{"name": "inspect_project", "input_schema": {"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"]}}], "ui_extensions": [{"id": "project_status", "slot": "project.settings", "schema_version": 1, "component": "status", "tool": "inspect_project"}]}

    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        if path == "/api/projects" and request.method == "POST":
            payload = request.post_data_json
            created.append(payload)
            project = {"id": f"p{len(projects) + 1}", "name": payload["name"], "entity_revision": 1, "folders": folders((payload.get("folders") or [{"path": f"/managed/p{len(projects) + 1}"}])[0]["path"]), "created_at": "2026-09-19T00:00:00Z", "settings": {"snapshots": True, "system": ""}, "system": "", "sessions": []}
            projects.append(project)
            return answer(route, project)
        if path == "/api/projects":
            return answer(route, projects)
        if path == "/api/sessions":
            listed = [{**project, "total": 0, "active": 0, "loops": 0, "last_message_at": ""} for project in projects]
            return answer(route, {"sessions": [], "projects": listed})
        if path == "/api/settings":
            # The start canvas reads the default model before a chat exists.
            return answer(route, {"presets": {}, "model": {}})
        if path == "/api/plugins/catalog":
            return answer(route, [{"manifest": extension, "valid": True, "digest": "a" * 64, "required_capabilities": ["board.read"], "ui_extensions": extension["ui_extensions"]}])
        if path == "/api/plugins/read-models":
            return answer(route, [
                {"id": "task_counts", "input_schema": {"type": "object", "additionalProperties": False,
                    "properties": {"project_id": {"type": "string", "minLength": 1, "maxLength": 64}}, "required": ["project_id"]}},
                {"id": "accepted_results", "input_schema": {"type": "object", "additionalProperties": False,
                    "properties": {"project_id": {"type": "string", "minLength": 1, "maxLength": 64},
                                   "limit": {"type": "integer", "minimum": 1, "maximum": 20, "title": "Maximum results"}},
                    "required": ["project_id"]}},
            ])
        if path == "/api/runtime/hosts" and request.method == "GET":
            if host_list_failure[0]:
                return answer(route, {"detail": "host list unavailable"}, status=503)
            item = {"host_id": "host-1", "label": "Test host", "entity_revision": 1,
                                             "identity_state": "verified", "fingerprint": "a" * 64,
                                             "pending_fingerprint": None, "public_key": "a" * 44,
                                             "host_generation": 1, "reachability": "unknown",
                                             "capabilities_state": "unknown", "artifact_transfer_state": "unknown",
                                             "ssh_configured": True, "probe_error": None, "observed_at": None,
                                             "effects_allowed": False, "blockers": []}
            return answer(route, {"items": [item] if host_list_item[0] else [], "collection_revision": 1})
        if path == "/api/plugins" and request.method == "GET":
            return answer(route, {"items": installed, "collection_revision": len(installed) + 1})
        if path == "/api/plugins/safe-mode" and request.method == "GET":
            return answer(route, {"enabled": False})
        if path == "/api/plugins/install" and request.method == "POST":
            payload = request.post_data_json
            staged.append(payload)
            installed.append({"id": "project_status", "version": "1.0.0", "digest": "a" * 64, "status": "staged", "health": "inactive", "created_at": "2026-10-03T00:00:00Z"})
            return answer(route, {"id": "project_status", "status": "staged", "receipt_id": "receipt", "entity_revision": 1})
        if path == "/api/plugins/project_status/health":
            return answer(route, {"id": "project_status", "state": "inactive"})
        if path == "/api/projects/p1/board" and request.method == "GET":
            return answer(route, {"tasks": [{"id": "t-owned", "title": "Owned task"}]})
        if path == "/api/control/revisions" and request.method == "GET":
            return answer(route, {"scope": {"kind": "global", "id": "global"}, "collection_revision": 1, "entity_revision": None})
        if path == "/api/admission/resources" and request.method == "GET":
            unavailable = {"available": False, "reason": "cpu controller is not delegated"}
            return answer(route, {"container": unavailable, "host": unavailable,
                                  "native": {"available": False, "reason": "in-process runtime"},
                                  "disk_quota_supported": False})
        if path == "/api/projects/p1/resource-profile" and request.method == "GET":
            return answer(route, resource_profile)
        if path == "/api/projects/p1/resource-profile" and request.method == "PUT":
            payload = request.post_data_json
            resource_attempts.append(payload)
            key = payload["client_operation_id"]
            if key in resource_receipts:
                original, receipt = resource_receipts[key]
                return answer(route, receipt if original == payload else {"detail": "intent changed"},
                              status=200 if original == payload else 409)
            if payload["expected_entity_revision"] != projects[0]["entity_revision"]:
                return answer(route, {"detail": "project changed", "current_revision": projects[0]["entity_revision"]}, status=409)
            if payload["expected_profile_revision"] != resource_profile["profile_revision"]:
                return answer(route, {"detail": "profile changed"}, status=409)
            projects[0]["entity_revision"] += 1
            resource_profile.update({"configured": True, "entity_revision": projects[0]["entity_revision"],
                                     "profile_revision": (resource_profile["profile_revision"] or 0) + 1,
                                     "state": payload["state"], "min_free_disk_bytes": payload["min_free_disk_bytes"],
                                     "limits": {key: payload[key] for key in ("memory_bytes", "cpu_millis", "process_count", "disk_bytes")}})
            receipt = {"receipt_id": f"resource-{key}", "entity_revision": projects[0]["entity_revision"],
                       "profile_revision": resource_profile["profile_revision"], "state": payload["state"], "limits": resource_profile["limits"]}
            resource_receipts[key] = payload, receipt
            return answer(route, {"detail": "response lost"}, status=503)
        if path == "/api/projects/p1/workspace-archive" and request.method == "GET":
            return answer(route, {"latest": None, "available": False})
        if path == "/api/projects/p1/orchestrator/authority" and request.method == "GET":
            bundles = [
                {"id": name, "scope_kind": scope, "operations": operations, "effects": effects,
                 "max_expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat(), "blockers": []}
                for name, scope, operations, effects in (
                    ("planning", "project", ["board.task.create", "board.task.update", "contract.require", "contract.apply", "contract.withdraw"], []),
                    ("execution", "task", ["task.launch", "task.stop", "staff.release"], ["execution.start", "execution.stop"]),
                    ("execution_project", "project", ["task.launch", "task.stop", "staff.release"], ["execution.start", "execution.stop"]),
                    ("review", "project", ["review.verdict", "review.return"], []),
                    ("watch", "project", ["watch.create", "watch.change", "watch.remove", "watch.deliver"], ["watch.wake", "watch.tell", "watch.notify"]),
                )
            ]
            return answer(route, {"project_id": "p1", "entity_revision": authority_revision[0], "current_coordinator_session_id": current_coordinator[0],
                                  "available_bundles": bundles, "grants": authority_grants, "readiness_blockers": []})
        if path == "/api/projects/p1/orchestrator/replace" and request.method == "GET":
            return answer(route, {"handoff": handoff_status[0]})
        if path == "/api/projects/p1/orchestrator/replace" and request.method == "POST":
            payload = request.post_data_json
            handoff_requests.append(payload)
            key = payload["client_operation_id"]
            if key in handoff_receipts:
                original, receipt = handoff_receipts[key]
                return answer(route, receipt if original == payload else {"detail": "intent changed"}, status=200 if original == payload else 409)
            if (payload["expected_entity_revision"] != authority_revision[0]
                    or payload["expected_coordinator_session_id"] != current_coordinator[0]):
                return answer(route, {"detail": "coordinator changed"}, status=409)
            authority_revision[0] += 1
            current_coordinator[0] = "coordinator-next"
            handoff_status[0] = {"handoff_id": "handoff-1", "state": "completed", "blocker": None,
                                 "old_active": False, "new_active": True, "receipt_id": "handoff-receipt"}
            receipt = {"handoff_id": "handoff-1", "state": "retirement_pending", "old_active": False,
                       "receipt_id": "handoff-receipt", "session_id": current_coordinator[0],
                       "entity_revision": authority_revision[0]}
            handoff_receipts[key] = payload, receipt
            if handoff_unknown[0]:
                handoff_unknown[0] -= 1
                return answer(route, {"detail": "unconfirmed response"}, status=503)
            return answer(route, receipt)
        if path == "/api/projects/p1/orchestrator/authority" and request.method == "POST":
            payload = request.post_data_json
            authority_requests.append(payload)
            key = payload["client_operation_id"]
            if key in authority_receipts:
                original, receipt = authority_receipts[key]
                return answer(route, receipt if original == payload else {"detail": "intent changed"}, status=200 if original == payload else 409)
            if payload["expected_entity_revision"] != authority_revision[0]:
                return answer(route, {"detail": "project changed"}, status=409)
            bundle = payload["bundle_id"]
            task_id = payload.get("task_id")
            operations = ["board.task.create", "board.task.update", "contract.require", "contract.apply", "contract.withdraw"] if bundle == "planning" else (["review.verdict", "review.return"] if bundle == "review" else (["watch.create", "watch.change", "watch.remove", "watch.deliver"] if bundle == "watch" else ["task.launch", "task.stop", "staff.release"]))
            effects = ["execution.start", "execution.stop"] if bundle.startswith("execution") else (["watch.wake", "watch.tell", "watch.notify"] if bundle == "watch" else [])
            grant = {"grant_id": f"grant-{len(authority_grants) + 1}", "generation": 1, "session_id": "coordinator-current",
                     "scope": {"kind": "task" if task_id else "project", "id": task_id or "p1"},
                     "operations": operations, "effects": effects, "expires_at": payload["expires_at"],
                     "revoked_at": None, "created_at": datetime.now(UTC).isoformat(), "state": "active", "receipt_id": f"receipt-{key}",
                     "parent_grant_id": None, "parent_grant_generation": None}
            authority_grants.insert(0, grant)
            authority_revision[0] += 1
            receipt = {"grant_id": grant["grant_id"], "receipt_id": grant["receipt_id"], "entity_revision": authority_revision[0]}
            authority_receipts[key] = payload, receipt
            if authority_unknown[0]:
                authority_unknown[0] -= 1
                return answer(route, {"detail": "unconfirmed response"}, status=503)
            return answer(route, receipt)
        if path == "/api/projects/p1/orchestrator/authority/grant-1/revoke" and request.method == "POST":
            payload = request.post_data_json
            authority_requests.append(payload)
            grant = next(grant for grant in authority_grants if grant["grant_id"] == "grant-1")
            if payload["expected_entity_revision"] != authority_revision[0] or payload["expected_grant_generation"] != grant["generation"]:
                return answer(route, {"detail": "approval changed"}, status=409)
            grant["generation"] += 1
            grant["revoked_at"] = datetime.now(UTC).isoformat()
            grant["state"] = "revoked"
            authority_revision[0] += 1
            return answer(route, {"grant_id": "grant-1", "receipt_id": "withdrawal", "entity_revision": authority_revision[0]})
        if path == "/api/lifecycle/project_goal/p1" and request.method == "GET":
            task = {"parent_kind": "project_goal", "parent_id": "p1", "child_kind": "task", "child_id": "t-owned", "generation": 1, "source_revision": 1, "cancel_state": "active"}
            attempt = {"parent_kind": "task", "parent_id": "t-owned", "child_kind": "execution_attempt", "child_id": "a-owned", "generation": 1, "source_revision": 1, "cancel_state": "active"}
            return answer(route, {"parent_kind": "project_goal", "parent_id": "p1", "project_id": "p1", "entity_revision": 1,
                                  "source_revision": 1, "generation": 1, "cancel_state": "requested" if lifecycle_commands else "active",
                                  "children": [task], "owned_descendants": [task, attempt], "preview_fingerprint": lifecycle_fingerprint[0]})
        if path == "/api/lifecycle/project_goal/p1/cancel" and request.method == "POST":
            payload = request.post_data_json
            lifecycle_attempts.append(payload)
            if len(lifecycle_attempts) == 1:
                lifecycle_fingerprint[0] = "c" * 64
                return answer(route, {"detail": "owned work changed since preview"}, status=409)
            if payload["preview_fingerprint"] != lifecycle_fingerprint[0]:
                return answer(route, {"detail": "owned work changed since preview"}, status=409)
            lifecycle_commands.append(payload)
            return answer(route, {"parent_kind": "project_goal", "parent_id": "p1", "cancel_state": "requested", "generation": 1, "children": [], "receipt_id": "lifecycle-receipt", "entity_revision": 2})
        if path == "/api/project-directories":
            query = parse_qs(url.query)
            if not query:
                return answer(route, {"roots": [{"name": "work", "path": "/work", "readable": True, "writable": True, "project_id": None}], "docker": True})
            selected = query.get("path", ["/work"])[0]
            entries = [{"name": "existing", "path": "/work/existing", "readable": True, "writable": True, "project_id": None}] if selected == "/work" else []
            parents = [{"name": "work", "path": "/work"}] + ([{"name": "existing", "path": "/work/existing"}] if selected != "/work" else [])
            return answer(route, {"root": "/work", "path": selected, "parents": parents, "entries": entries, "truncated": False})
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        answer(route, [])

    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/agents?token=t&lang=en")
        page.locator(".project-chip").click()
        name = page.locator("#project-name")
        expect(name).to_be_visible()
        name.fill("Plain")
        page.get_by_role("button", name="Start with a clear first task").click()
        page.get_by_role("button", name="Add", exact=True).click()
        expect(page.locator(".sheet")).to_have_count(0)
        assert created[0] == {"name": "Plain"}, created[0]

        page.reload()
        page.locator(".project-chip").click()
        expect(page.locator(".project-row")).to_have_count(1)
        page.get_by_role("button", name="Add a project", exact=True).click()
        page.locator("#project-name").fill("Existing")
        page.get_by_role("button", name="Use an existing folder").click()
        expect(page.get_by_text("the bot sees a folder only once it is mounted", exact=False)).to_be_visible()
        page.get_by_role("button", name="work", exact=True).click()
        page.get_by_role("button", name="existing", exact=True).click()
        expect(page.locator("#project-root")).to_have_value("/work/existing")
        page.get_by_role("button", name="Add", exact=True).click()
        # The sheet closes once the request has been answered; read what was posted only then, as
        # for the first project. Read at once, it raced the request on a loaded machine.
        expect(page.locator(".sheet")).to_have_count(0)
        assert created[1] == {"name": "Existing", "folders": [{"path": "/work/existing"}]}, created[1]
        page.locator(".project-chip").click()
        page.locator(".project-row", has_text="Plain").get_by_role("button", name="Settings for Plain").click()
        resources = page.locator("details.project-resources")
        expect(resources).to_be_visible()
        resources.locator("summary").click()
        expect(resources).to_contain_text("Enable strict limits")
        resources.get_by_role("checkbox").check()
        expect(resources).to_contain_text("cpu controller is not delegated")
        expect(resources).to_contain_text("hard disk-space quota is unavailable")
        resources.locator("#resource-memory-p1").fill("512")
        resources.locator("#resource-cpu-p1").fill("1000")
        resources.locator("#resource-processes-p1").fill("64")
        resources.locator("#resource-min-free-p1").fill("1024")
        for width in (320, 390, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"resource settings overflow {width}px"
        resources.get_by_role("button", name="Save resource limits").click()
        expect(page.locator(".result-warning")).to_contain_text("Try again")
        assert resource_attempts[0]["expected_entity_revision"] == 1
        assert resource_attempts[0]["memory_bytes"] == 512 * 1048576
        assert resource_attempts[0]["min_free_disk_bytes"] == 1024 * 1048576
        assert resource_attempts[0]["expected_profile_revision"] is None
        assert resource_attempts[0]["cpu_millis"] == 1000 and resource_attempts[0]["process_count"] == 64
        page.get_by_role("button", name="Try again").click()
        expect(resources.locator("summary")).to_contain_text("512 MiB", timeout=5000)
        assert resource_attempts[1] == resource_attempts[0], "uncertain resource write did not replay the exact request"
        resources.locator("#resource-cpu-p1").fill("1500")
        resource_profile["profile_revision"] = 2
        resource_profile["limits"] = {**resource_profile["limits"], "cpu_millis": 1100}
        resources.get_by_role("button", name="Check current settings").click()
        expect(resources).to_contain_text("Resource settings changed while you were editing")
        expect(resources.locator("#resource-cpu-p1")).to_have_value("1500")
        expect(resources.get_by_role("button", name="Save resource limits")).to_be_disabled()
        assert len(resource_attempts) == 2, "a stale draft was silently rebased and sent"
        resources.get_by_role("button", name="Discard draft and load current").click()
        expect(resources.locator("#resource-cpu-p1")).to_have_value("1100")
        extensions = page.locator(".project-extensions")
        expect(extensions).to_be_visible()
        hosts = page.locator(".project-runtime-hosts")
        expect(hosts).to_be_visible()
        expect(hosts).not_to_contain_text("No remote host identities recorded")
        hosts.locator("summary").first.click()
        expect(hosts).to_contain_text("No remote host identities recorded")
        page.set_viewport_size({"width": 320, "height": 560})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "host settings overflow the phone"
        lifecycle = page.locator("details.sheet-section", has_text="Stop work owned by this goal")
        lifecycle.locator("summary").first.click()
        expect(lifecycle).to_contain_text("Owned task")
        expect(lifecycle).to_contain_text("Owned run")
        lifecycle.get_by_text("Details").first.click()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "ownership identity overflows the phone"
        lifecycle.locator("textarea").fill("Stop the owned work")
        lifecycle.get_by_role("button", name="Request stop").click()
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/lifecycle/project_goal/p1" and response.request.method == "GET"):
            page.locator(".sheet-backdrop.confirm .dialog button").last.click()
        expect(lifecycle.locator("textarea")).to_have_value("Stop the owned work")
        expect(lifecycle).to_contain_text("Active")
        lifecycle.get_by_role("button", name="Request stop").click()
        page.locator(".sheet-backdrop.confirm .dialog button").last.click()
        assert lifecycle_attempts[0]["preview_fingerprint"] == "b" * 64
        assert lifecycle_commands and lifecycle_commands[-1]["preview_fingerprint"] == "c" * 64
        assert lifecycle_commands[-1]["expected_entity_revision"] == 1 and lifecycle_commands[-1]["expected_source_revision"] == 1
        expect(lifecycle).to_contain_text("Stopping…")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "lifecycle settings overflow the phone"
        page.set_viewport_size({"width": 1440, "height": 900})
        expect(extensions.locator("article.project-extension")).to_have_count(0)
        extensions.locator("summary").first.click()
        expect(extensions.locator("article.project-extension")).to_have_count(1)
        expect(extensions.locator("details.project-extension")).to_have_count(1)
        expect(extensions).to_contain_text("ordinary project work needs no extension")
        extensions.get_by_role("button", name="Install").click()
        expect(extensions).to_contain_text("Activating")
        expect(extensions.get_by_role("button", name="Test on this project")).to_be_disabled()
        assert staged and staged[0]["expected_digest"] == "a" * 64 and staged[0]["client_operation_id"], staged
        page.set_viewport_size({"width": 320, "height": 560})
        authority = page.locator("details.sheet-section", has_text="Coordinator permissions")
        authority.locator("summary").first.click()
        expect(authority).to_contain_text("Active approvals: 0")
        authority.get_by_text("Approve an action", exact=True).click()
        # The form opens on task assignment, which this project's coordinator is not offered.
        authority.locator("select").first.select_option("planning")
        authority.get_by_role("button", name="Approve", exact=True).click()
        page.locator(".sheet-backdrop.confirm .dialog button").last.click()
        expect(authority).to_contain_text("Couldn't confirm", timeout=5000)
        assert authority_requests[0]["bundle_id"] == "planning" and authority_requests[0]["task_id"] is None
        authority.get_by_role("button", name="Try again").click()
        expect(authority).to_contain_text("Active approvals: 1", timeout=5000)
        assert authority_requests[1] == authority_requests[0], "uncertain approval did not replay the exact request"
        authority.locator("select").first.select_option("execution")
        expect(authority.get_by_role("button", name="Approve", exact=True)).to_be_disabled()
        authority.locator("select").nth(1).select_option("t-owned")
        authority.get_by_role("button", name="Approve", exact=True).click()
        expect(page.locator(".sheet-backdrop.confirm .dialog")).to_contain_text("release its staff member or stop its bound execution")
        page.locator(".sheet-backdrop.confirm .dialog button").last.click()
        expect(authority).to_contain_text("Active approvals: 2", timeout=5000)
        assert authority_requests[2]["bundle_id"] == "execution" and authority_requests[2]["task_id"] == "t-owned"
        planning = authority.locator(".project-extension", has_text="Plan and edit tasks")
        planning.get_by_role("button", name="Withdraw approval").click()
        planning.locator("textarea").fill("Reduce coordinator scope")
        planning.get_by_role("button", name="Withdraw approval").click()
        page.locator(".sheet-backdrop.confirm .dialog button").last.click()
        expect(authority).to_contain_text("Active approvals: 1", timeout=5000)
        assert authority_requests[-1]["expected_grant_generation"] == 1 and authority_requests[-1]["reason"] == "Reduce coordinator scope"
        authority.locator("select").first.select_option("watch")
        expect(authority).to_contain_text("matching events may wake the coordinator")
        authority.get_by_role("button", name="Approve", exact=True).click()
        expect(page.locator(".sheet-backdrop.confirm .dialog")).to_contain_text("without a new approval each time")
        page.locator(".sheet-backdrop.confirm .dialog button").last.click()
        expect(authority).to_contain_text("Active approvals: 2", timeout=5000)
        assert authority_requests[-1]["bundle_id"] == "watch" and authority_requests[-1]["task_id"] is None
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "authority controls overflow the phone"
        handoff = authority.locator("details.sheet-section", has_text="Replace coordinator")
        handoff.locator("summary").first.click()
        handoff.locator("textarea").fill("Refresh the coordinator context")
        handoff.get_by_role("button", name="Check and replace").click()
        expect(page.locator(".sheet-backdrop.confirm .dialog")).to_contain_text("model is available")
        page.locator(".sheet-backdrop.confirm .dialog button").last.click()
        expect(handoff).to_contain_text("Couldn't confirm the replacement was requested", timeout=5000)
        assert handoff_requests[0]["expected_coordinator_session_id"] == "coordinator-current"
        authority.locator("summary").first.click()
        authority.locator("summary").first.click()
        handoff = authority.locator("details.sheet-section", has_text="Replace coordinator")
        handoff.locator("summary").first.click()
        expect(handoff.locator("textarea")).to_have_value("Refresh the coordinator context")
        handoff.get_by_role("button", name="Try again").click()
        expect(handoff).to_contain_text("Coordinator replaced; the old session was asked to stop", timeout=5000)
        assert handoff_requests[1] == handoff_requests[0], "uncertain replacement did not replay the exact command"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "handoff controls overflow the phone"
        host_list_item[0] = True
        hosts = page.locator("details.project-runtime-hosts")
        hosts.locator("summary").first.click()
        page.wait_for_timeout(3200)
        hosts.locator("summary").first.click()
        expect(hosts.get_by_role("button", name="Check SSH transport")).to_be_enabled()
        host_list_failure[0] = True
        hosts.locator("summary").first.click()
        page.wait_for_timeout(3200)
        hosts.locator("summary").first.click()
        expect(hosts).to_contain_text("Host state could not be confirmed", timeout=5000)
        expect(hosts.get_by_role("button", name="Check SSH transport")).to_be_disabled()
        browser.close()
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
