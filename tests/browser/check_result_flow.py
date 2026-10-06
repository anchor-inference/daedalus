"""A result moves through evidence, coordinator verdict, merge receipt and exact operator acceptance.

On the way the operator counts the worker's passing check on the reviewed commit as evidence with
one click.
"""

from __future__ import annotations

import os

from api_stub import BoardStub, Unhandled, expect_app
from check_project_board import BASE, CHROMIUM, PID, project, serve
from playwright.sync_api import expect, sync_playwright


class ResultStub(BoardStub):
    def __init__(self) -> None:
        task = self.task("review-one", "Verify export", status="review", branch="agent/export", project_id=PID, acceptance_state="handed_in")
        super().__init__(project(), tasks=[task])
        self.evidence: list[dict] = []
        self.verdict: dict | None = None
        self.merge_receipt: dict | None = None
        self.operations: list[tuple[str, dict]] = []

    def answer(self, method: str, path: str, query: str, body: dict | None):  # type: ignore[no-untyped-def]
        base = "/api/board/review-one"
        result = f"{base}/results/result-one"
        task = self.tasks[0]
        if path == "/api/artifact-manifests/artifact-one/transfer-readiness" and method == "GET":
            return 200, {"manifest_id": "artifact-one", "project_id": PID, "available": False, "reason": "file_unavailable", "transfers": []}
        if path == f"{base}/contract" and method == "GET":
            return 200, {"task_id": task["id"], "contract_revision": 1, "entity_revision": task["entity_revision"], "checklist": [{"id": "C1", "text": "Export opens"}]}
        if path == f"{base}/results" and method == "GET":
            current = {"result_id": "result-one", "current_result_id": "result-one", "task_id": task["id"], "contract_revision": 1, "outcome": "complete", "original_preview": "An export was generated", "original_digest": "a" * 64, "original_size_bytes": 23, "artifacts": [{"id": "artifact-one", "artifact_kind": "file", "artifact_key": "export.json", "artifact_revision": 1, "digest": "b" * 64, "size_bytes": 28}], "checks": [], "limitations": [], "verification": "verified" if self.verdict else "unverified", "verdict_id": "verdict-one" if self.verdict else None, "verdict_accepted": bool(self.verdict), "verdict_head": "head" if self.verdict else None, "verdict_base": "base" if self.verdict else None, "self_review_waiver_required": False, "acceptance_state": task["acceptance_state"], "accepted": task["acceptance_state"] == "operator_approved", "created_at": "2026-10-03T00:00:00Z"}
            previous = {**current, "result_id": "result-previous", "outcome": "partial", "original_preview": "Export was missing a field", "original_digest": "c" * 64, "checks": ["Required field absent"], "limitations": ["Schema incomplete"], "verification": "stale", "verdict_id": None, "verdict_accepted": False, "created_at": "2026-10-03T00:01:00Z"}
            return 200, [previous, current]
        if path == f"{result}/original" and method == "GET":
            return 200, {"original_text": "An export was generated\nFull source report"}
        if path == f"{result}/turns" and method == "GET":
            return 200, [{"session_id": "session-source", "turn_seq": 7, "source_ref": "turn:7", "source_digest": "d" * 64, "source_current": True}]
        if path == f"{result}/comments" and method == "GET":
            return 200, []
        if path == f"{result}/evidence/check" and method == "POST":
            payload = dict(body or {})
            self.operations.append(("check", payload))
            if payload.get("verification_id") != 7:
                return 409, {"detail": "the check ran against another commit than the reviewed branch head"}
            self.evidence.append({"evidence_id": "evidence-check", "criterion_id": payload["criterion_id"], "observation": "verify: export tests pass", "verification": "verified", "exit_code": 0, "manifest_digest_before": "tree:head", "manifest_digest_after": "tree:head", "observed_at": "2026-10-03T00:02:00Z"})
            task["entity_revision"] += 1
            return 200, {"evidence_id": "evidence-check", "verification": "verified", "entity_revision": task["entity_revision"], "receipt_id": "check-receipt"}
        if path == f"{result}/evidence" and method == "GET":
            return 200, self.evidence
        if path == f"{result}/evidence" and method == "POST":
            payload = dict(body or {})
            self.operations.append(("evidence", payload))
            self.evidence.append({"evidence_id": "evidence-one", "criterion_id": payload["criterion_id"], "observation": payload["observation"], "verification": "verified", "exit_code": 0, "manifest_digest_before": "b" * 64, "manifest_digest_after": "b" * 64, "observed_at": "2026-10-03T00:01:00Z"})
            task["entity_revision"] += 1
            return 200, {"evidence_id": "evidence-one", "verification": "verified", "entity_revision": task["entity_revision"], "receipt_id": "evidence-receipt"}
        if path == f"{result}/verdicts" and method == "POST":
            payload = dict(body or {})
            self.operations.append(("verdict", payload))
            if sorted(payload["evidence_ids"]) != ["evidence-check", "evidence-one"]:
                return 409, {"detail": "missing evidence"}
            self.verdict = payload
            task["acceptance_state"] = "accepted"
            task["entity_revision"] += 1
            return 200, {"verdict_id": "verdict-one", "accepted": True, "entity_revision": task["entity_revision"], "receipt_id": "verdict-receipt"}
        if path == f"{base}/review" and method == "GET":
            # Receipt 7 ran on the reviewed head; receipt 8 on uncommitted edits, so it is never offered.
            review = self.review(task, receipts=[
                {"id": 7, "criterion": "export tests pass", "command": "pytest tests/test_export.py", "exit_code": 0, "passed": True, "at": "2026-10-03T00:00:00Z", "tree": "head"},
                {"id": 8, "criterion": "lint is clean", "command": "ruff check", "exit_code": 0, "passed": True, "at": "2026-10-03T00:00:00Z", "tree": "head+worktree"}])
            return 200, {**review, "head_sha": "head", "base_sha": "base", "current_sha": "merged-head" if self.merge_receipt else "base", "merge_receipt": self.merge_receipt, "merged": bool(self.merge_receipt), "can_merge": not self.merge_receipt, "blockers": []}
        if path == f"{result}/merge" and method == "POST":
            payload = dict(body or {})
            self.operations.append(("merge", payload))
            self.merge_receipt = {"id": "merge-one", "result_id": "result-one", "verdict_id": "verdict-one", "head_sha": "head", "base_sha": "base", "merge_sha": "merged-head", "state": "merged", "error": None}
            task["merge_state"] = "merged"
            task["entity_revision"] += 1
            return 200, {"effect_id": "merge-one", "state": "queued", "entity_revision": task["entity_revision"], "receipt_id": "merge-receipt"}
        if path == f"{result}/accept" and method == "POST":
            payload = dict(body or {})
            self.operations.append(("accept", payload))
            task["acceptance_state"] = "operator_approved"
            task["status"] = "done"
            task["entity_revision"] += 1
            return 200, {"accepted": True, "entity_revision": task["entity_revision"], "receipt_id": "accept-receipt"}
        return super().answer(method, path, query, body)


def run() -> int:
    expect_app(BASE)
    stub = ResultStub()
    unhandled = Unhandled()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("CHROMIUM", CHROMIUM))
        page = browser.new_page(viewport={"width": int(os.environ.get("RESULT_WIDTH", "390")), "height": 760}, is_mobile=True, has_touch=True)
        serve(page, stub, unhandled)
        page.goto(f"{BASE}/project/{PID}/board?token=t&lang=en")
        page.locator(".pboard-list .pcard", has_text="Verify export").click()
        flow = page.locator(".result-flow")
        expect(flow).to_be_visible()
        expect(flow.locator(".result-summary")).to_contain_text("An export was generated")
        flow.get_by_text("Earlier results (1)").click()
        expect(flow.locator(".result-compare")).to_contain_text("Export was missing a field")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "result comparison overflow"
        expect(flow.get_by_role("button", name="Accept this result")).to_be_disabled()
        flow.get_by_text("Evidence and original report").click()
        expect(flow.get_by_text("Send artifact to a host")).to_be_visible()
        flow.get_by_role("button", name="Show original report").click()
        expect(flow.locator(".result-original")).to_contain_text("Full source report")
        flow.get_by_role("button", name="Source messages").click()
        expect(flow.get_by_role("button", name="Open source message")).to_be_visible()
        review = flow.locator("details.result-details", has_text="Review checks and evidence")
        expect(review).to_have_attribute("open", "")
        checks = flow.locator(".result-checks", has_text="Checks the worker ran on this commit")
        expect(checks).to_contain_text("export tests pass")
        expect(checks).not_to_contain_text("lint is clean")
        expect(checks).to_contain_text("another commit")
        checks.get_by_role("button", name="Use this check").click()
        expect(page.locator(".toast")).to_contain_text("Check recorded as evidence")
        flow.get_by_role("textbox", name="What did you observe?").fill("Opened the export and checked its schema")
        flow.get_by_role("button", name="Record observation").click()
        expect(flow).to_contain_text("Evidence recorded")
        flow.get_by_role("textbox", name="Review conclusion").fill("Export matches the requested schema")
        flow.get_by_role("button", name="Approve reviewed result").click()
        expect(flow.get_by_role("button", name="Merge reviewed branch")).to_be_enabled()
        flow.get_by_role("button", name="Merge reviewed branch").click()
        expect(flow.get_by_role("button", name="Accept this result")).to_be_enabled()
        flow.get_by_role("button", name="Accept this result").click()
        expect(page.locator(".toast")).to_contain_text("Result accepted")
        assert [kind for kind, _ in stub.operations] == ["check", "evidence", "verdict", "merge", "accept"], stub.operations
        check = dict(stub.operations)["check"]
        assert check["verification_id"] == 7 and check["criterion_id"] == "C1", check
        for _, payload in stub.operations:
            assert payload.get("client_operation_id") and isinstance(payload.get("expected_entity_revision"), int), payload
        assert stub.tasks[0]["acceptance_state"] == "operator_approved"
        browser.close()
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
