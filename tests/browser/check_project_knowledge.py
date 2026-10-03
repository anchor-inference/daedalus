"""Project memory preserves intent, checks source freshness and stays usable on a phone."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, fulfil_shared  # noqa: E402
from check_workspace_archive import PROJECT  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def check(lang: str, width: int) -> None:
    unhandled = Unhandled()
    project = copy.deepcopy(PROJECT)
    facts: list[dict] = []
    receipts: dict[str, tuple[dict, dict]] = {}
    sent: list[dict] = []
    history: list[dict] = []
    stale_queue: list[dict] = []
    stale_commands: list[dict] = []
    lose_stale_reply = True
    identity_collision = False
    unknown = True
    revision = 1
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": width, "height": 560 if width == 320 else 900})

        def answer(route, body, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

        def stub(route):
            nonlocal unknown, revision, lose_stale_reply
            request = route.request
            path = urlsplit(request.url).path
            method = request.method
            base = "/api/projects/p1/knowledge"
            if path == base and method == "GET":
                return answer(route, {"project_id": "p1", "entity_revision": revision, "collection_revision": revision, "facts": facts, "next_before": None})
            if path == base + "/sources":
                return answer(route, [{"source_kind": "file", "source_id": "proof", "label": "proof.txt"}])
            if path == base + "/stale" and method == "GET":
                return answer(route, stale_queue)
            if path == base + "/fact/revalidate" and method == "POST":
                body = request.post_data_json
                stale_commands.append(body)
                if body["queue_id"] == "queued-conflict":
                    return answer(route, {"detail": "the command identity was reused with a different request" if identity_collision else "fact version changed"}, 409)
                assert body["queue_id"] == "queued-change" and body["expected_version"] == 3
                operation = body["client_operation_id"]
                if operation not in receipts:
                    assert body["expected_entity_revision"] == revision
                    facts[0] = {**facts[0], "version": 4, "status": "invalidated", "source_status": "stale"}
                    stale_queue.clear()
                    revision += 1
                    receipts[operation] = body, {"fact_id": "fact", "version": 4, "status": "invalidated", "receipt_id": operation}
                payload, result = receipts[operation]
                assert payload == body
                if lose_stale_reply:
                    lose_stale_reply = False
                    return answer(route, {"detail": "unconfirmed response"}, 503)
                return answer(route, result)
            if path == base + "/fact/history":
                return answer(route, history)
            if path in (base + "/candidates", base + "/fact/review") and method == "POST":
                body = request.post_data_json
                sent.append(body)
                operation = body["client_operation_id"]
                if operation in receipts:
                    payload, result = receipts[operation]
                    return answer(route, result if body == payload else {"detail": "intent changed"}, 200 if body == payload else 409)
                if path.endswith("/candidates"):
                    assert body["source_id"] == "proof" and body["source_kind"] == "file"
                    facts.append({"fact_id": "fact", "version": 1, "claim": body["claim"], "status": "candidate", "kind": "fact",
                                  "source_kind": "file", "source_id": "proof", "source_revision": "digest", "source_digest": "digest",
                                  "source_status": "current", "actor": "operator", "reason": "", "created_at": "now"})
                else:
                    assert body["expected_version"] == facts[0]["version"]
                    facts[0] = {**facts[0], "version": facts[0]["version"] + 1,
                                "status": {"review": "reviewed", "promote": "promoted", "forget": "forgotten"}[body["verdict"]], "reason": body["reason"]}
                history.append(copy.deepcopy(facts[0]))
                revision += 1
                result = {"fact_id": "fact", "version": facts[0]["version"], "receipt_id": operation}
                receipts[operation] = body, result
                if unknown:
                    unknown = False
                    return answer(route, {"detail": "unconfirmed response"}, 503)
                return answer(route, result)
            if path == "/api/projects":
                return answer(route, [project])
            if path == "/api/sessions":
                return answer(route, {"sessions": [], "projects": [{**project, "total": 0, "active": 0, "loops": 0, "last_message_at": ""}]})
            if path == "/api/project-environments":
                return answer(route, {"local": "container", "available": ["container"], "host_bridge": False, "docker": True})
            if path == "/api/projects/p1/workspace-archive":
                return answer(route, {"latest": None, "available": False})
            if path == "/api/control/revisions":
                return answer(route, {"scope": {"kind": "global", "id": "global"}, "collection_revision": 1, "entity_revision": None})
            if path == "/api/fs/roots":
                return answer(route, {"roots": [], "docker": True})
            if path == "/api/settings" and method == "GET":
                return answer(route, {"presets": {}, "model": {}})
            if fulfil_shared(route):
                return None
            unhandled.record(path)
            answer(route, [])

        page.route("**/api/**", stub)

        def open_memory():
            projects = "Projects" if lang == "en" else "Проекты"
            settings = "Settings for Bakery" if lang == "en" else "Настройки: Bakery"
            page.locator(f".project-chip:visible, .start-list-head .iconbtn[aria-label='{projects}']:visible").first.click()
            page.locator(f".project-row .iconbtn[aria-label='{settings}']").click(force=True)
            section = page.locator("details.sheet-section", has=page.get_by_text("Project memory" if lang == "en" else "Память проекта", exact=True))
            section.locator("summary").first.click()
            return section

        page.goto(f"{BASE}/agents?token=t&lang={lang}")
        section = open_memory()
        section.get_by_text("Propose a fact" if lang == "en" else "Предложить факт", exact=True).click()
        claim = "The current project source requires explicit checking"
        section.get_by_label("Fact to check" if lang == "en" else "Факт для проверки").fill(claim)
        section.get_by_label("Evidence source" if lang == "en" else "Источник подтверждения").select_option("file:proof")
        page.reload()
        section = open_memory()
        section.get_by_text("Propose a fact" if lang == "en" else "Предложить факт", exact=True).click()
        expect(section.get_by_label("Fact to check" if lang == "en" else "Факт для проверки")).to_have_value(claim)
        section.get_by_role("button", name="Save for checking" if lang == "en" else "Сохранить для проверки", exact=True).click()
        expect(section).to_contain_text("unconfirmed" if lang == "en" else "не подтверждён")
        page.reload()
        section = open_memory()
        section.get_by_role("button", name="Retry saved command" if lang == "en" else "Повторить сохранённую команду", exact=True).click()
        expect(section.get_by_role("button", name=claim, exact=True)).to_be_visible()
        assert sent[0] == sent[1] and len(facts) == 1
        section.get_by_role("button", name=claim, exact=True).click()
        reason = section.get_by_label("Reason for this decision" if lang == "en" else "Причина решения")
        reason.fill("Verified against proof.txt")
        section.get_by_role("button", name="Mark checked" if lang == "en" else "Отметить проверенным", exact=True).click()
        reason.fill("Allow the checked fact")
        section.get_by_role("button", name="Allow in context" if lang == "en" else "Разрешить в контексте", exact=True).click()
        expect(section).to_contain_text("Allowed in context" if lang == "en" else "Разрешено в контексте")
        stale_queue.append({"id": "queued-change", "fact_id": "fact", "fact_version": 3, "claim": claim,
                            "source_id": "proof", "replacement_digest": "new-digest", "observed_revision": 2,
                            "created_at": "2026-10-03T10:00:00Z"})
        section.get_by_text("Sources needing review" if lang == "en" else "Источники для проверки", exact=True).click()
        expect(section).to_contain_text("approved source has a new version" if lang == "en" else "У разрешённого источника новая версия")
        section.get_by_role("button", name="Check source again" if lang == "en" else "Проверить источник снова").click()
        expect(section).to_contain_text("unconfirmed" if lang == "en" else "не подтверждён")
        page.reload()
        section = open_memory()
        section.get_by_role("button", name="Retry saved command" if lang == "en" else "Повторить сохранённую команду").click()
        expect(section).to_contain_text("Invalidated" if lang == "en" else "Признано неактуальным")
        expect(section.get_by_role("button", name="Retry saved command" if lang == "en" else "Повторить сохранённую команду")).to_have_count(0)
        assert len(stale_commands) == 2 and stale_commands[0] == stale_commands[1]
        expect(section.get_by_role("button", name="Allow in context" if lang == "en" else "Разрешить в контексте", exact=True)).to_have_count(0)
        facts[0] = {**facts[0], "version": 5, "status": "promoted", "source_status": "stale"}
        stale_queue.append({"id": "queued-conflict", "fact_id": "fact", "fact_version": 5, "claim": claim,
                            "source_id": "proof", "replacement_digest": "later-digest", "observed_revision": 3,
                            "created_at": "2026-10-03T11:00:00Z"})
        page.reload()
        section = open_memory()
        section.get_by_text("Sources needing review" if lang == "en" else "Источники для проверки", exact=True).click()
        section.get_by_role("button", name="Check source again" if lang == "en" else "Проверить источник снова").click()
        expect(section).to_contain_text("rejected command was cleared" if lang == "en" else "Отклонённая команда удалена")
        assert page.evaluate("sessionStorage.getItem('daedalus.knowledge.pending.p1')") is None
        page.reload()
        section = open_memory()
        expect(section).to_contain_text("rejected command was cleared" if lang == "en" else "Отклонённая команда удалена")
        section.get_by_role("button", name="Review updated evidence" if lang == "en" else "Проверить обновлённые сведения").click()
        section.get_by_text("Sources needing review" if lang == "en" else "Источники для проверки", exact=True).click()
        expect(section.get_by_role("button", name="Check source again" if lang == "en" else "Проверить источник снова")).to_be_enabled()
        section.get_by_role("button", name="Check source again" if lang == "en" else "Проверить источник снова").click()
        expect(section).to_contain_text("rejected command was cleared" if lang == "en" else "Отклонённая команда удалена")
        assert stale_commands[-1]["client_operation_id"] != stale_commands[-2]["client_operation_id"]
        identity_collision = True
        section.get_by_role("button", name="Review updated evidence" if lang == "en" else "Проверить обновлённые сведения").click()
        section.get_by_role("button", name="Check source again" if lang == "en" else "Проверить источник снова").click()
        expect(section).to_contain_text("command ID belongs to a different request" if lang == "en" else "ID команды относится к другому запросу")
        page.reload()
        section = open_memory()
        expect(section).to_contain_text("command ID belongs to a different request" if lang == "en" else "ID команды относится к другому запросу")
        section.get_by_role("button", name="Set aside this command" if lang == "en" else "Отложить эту команду").click()
        assert page.evaluate("sessionStorage.getItem('daedalus.knowledge.pending.p1')") is None
        expect(section.get_by_role("button", name="Retry saved command" if lang == "en" else "Повторить сохранённую команду")).to_have_count(0)
        assert len(sent) == 4
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
        assert unhandled.report() == 0
        browser.close()


if __name__ == "__main__":
    expect_app(BASE)
    for language in ("en", "ru"):
        for viewport in (320, 390, 1440):
            check(language, viewport)
            print(f"knowledge {language} {viewport}: PASS")
