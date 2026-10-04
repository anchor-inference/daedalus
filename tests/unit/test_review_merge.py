"""A reviewed staff branch merges only through an exact, durable result receipt."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Header

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.api_orchestrator_domain import install_routes
from daedalus.extensions.board import Board
from daedalus.extensions.ci_observations import observation_from_webhook, record_observation
from daedalus.extensions.merge_effect import MergeEffect
from daedalus.extensions.review import Review, ReviewRefused
from daedalus.extensions.staff import Team
from daedalus.host.session_runner import SessionManager
from daedalus.staff_runtime import StartRequest
from daedalus.stores.control import Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.outbox import Claim
from daedalus.stores.projects import Project
from daedalus.stores.staff import Staff
from tests.support.authorized_launch import operator_assignment
from tests.support.waiting import until_await
from tests.unit.test_session_runner import ScriptedProvider, _manager
from tests.unit.test_staff_runtime import (
    ObservedFakeStaffRuntime,
    board_task,
    close_team,
    git,
    project_with,
    repository,
    task_row,
    team_for,
)


class Rig:
    def __init__(self, manager: SessionManager, team: Team, runtime: ObservedFakeStaffRuntime,
                 project: Project, review: Review) -> None:
        self.manager = manager
        self.team = team
        self.runtime = runtime
        self.project = project
        self.review = review

    @property
    def folder(self) -> Path:
        assert self.project.primary is not None
        return self.project.primary.path

    def client(self) -> httpx.AsyncClient:
        api = FastAPI()

        async def authenticated(x_user: int = Header(1)) -> dict[str, Any]:
            return {"via": "token", "user_id": x_user}

        install_routes(api, self.team.app, authenticated)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")

    async def hire(self) -> Staff:
        return await self.manager.staff.hire(self.project.id, name="Ada", isolation="worktree")

    async def reviewed(self, member: Staff, title: str, files: dict[str, str], *, ci: bool = True) -> tuple[str, StartRequest]:
        task_id = await board_task(self.manager, self.project, title)
        if ci:
            await self.manager.db.execute("INSERT INTO ci_required_checks(task_id,contract_revision,provider,"
                                          "repository_id,check_name,created_at) VALUES (?,?, 'github','7','unit','2026-01-01')",
                                          (task_id, 1))
        await operator_assignment(self.team, member, task_id)
        request = next(item for item in reversed(self.runtime.started) if item.task.id == task_id)
        assert request.worktree is not None
        for name, content in files.items():
            (request.worktree.cwd / name).write_text(content)
        git(request.worktree.path, "add", "-A")
        git(request.worktree.path, "commit", "-qm", f"{title} by {member.name}")
        if ci:
            await self.ci_result(task_id, "success")
        live = await self.team.live_of(member)
        assert live is not None
        _folder, cwd = await self.team.cwd_of(live)
        assert Path(cwd) == request.worktree.cwd
        await self.team.ingress.report(live, "done", "finished", artifacts=list(files),
                                      call_id=f"review:{uuid.uuid4().hex}")
        assert (await task_row(self.manager, task_id))["status"] == "review"
        result = await self.manager.db.fetchone("SELECT id,attempt_id,outcome FROM result_receipts WHERE task_id = ?",
                                                (task_id,))
        assert result is not None and result["outcome"] == "complete"
        assert result["attempt_id"] == (await task_row(self.manager, task_id))["current_attempt_id"]
        return task_id, request

    async def ci_result(self, task_id: str, conclusion: str, *, run_id: int = 1,
                        head: str | None = None) -> None:
        branch = (await task_row(self.manager, task_id))["branch"]
        sha = head or git(self.folder, "rev-parse", f"refs/heads/{branch}").strip()
        observation = observation_from_webhook("github", "check_run", uuid.uuid4().hex,
                                                {"repository": {"id": 7}, "check_run": {
                                                    "id": run_id, "name": "unit", "head_sha": sha,
                                                    "status": "completed", "conclusion": conclusion}},
                                                payload_digest=hashlib.sha256(uuid.uuid4().bytes).hexdigest())
        assert observation is not None
        async with self.manager.db.transaction() as conn:
            await record_observation(conn, observation)

    async def approve(self, client: httpx.AsyncClient, task_id: str) -> tuple[str, str]:
        result = await self.manager.db.fetchone("SELECT id FROM result_receipts WHERE task_id = ?", (task_id,))
        assert result is not None
        result_id = result["id"]
        artifact = await self.manager.db.fetchone(
            "SELECT m.id FROM result_artifacts a JOIN artifact_manifests m ON m.id=a.manifest_id"
            " WHERE a.result_id = ? AND m.file_id IS NOT NULL ORDER BY m.id LIMIT 1", (result_id,))
        assert artifact is not None, [dict(row) for row in await self.manager.db.fetchall(
            "SELECT m.id,m.file_id,m.artifact_key FROM result_artifacts a"
            " JOIN artifact_manifests m ON m.id=a.manifest_id WHERE a.result_id = ?", (result_id,))]
        review = await self.review.review(task_id)
        assert [blocker["code"] for blocker in review["blockers"]] == ["verdict_missing"]
        base = f"/api/board/{task_id}/results/{result_id}"
        revision = (await task_row(self.manager, task_id))["entity_revision"]
        evidence = await client.post(base + "/evidence", json={
            "client_operation_id": f"evidence:{uuid.uuid4().hex}",
            "expected_entity_revision": revision,
            "criterion_id": "result",
            "manifest_id": artifact["id"],
            "observation": "Reviewed the committed report artifact",
        })
        assert evidence.status_code == 200, evidence.text
        verdict = await client.post(base + "/verdicts", json={
            "client_operation_id": f"verdict:{uuid.uuid4().hex}",
            "expected_entity_revision": evidence.json()["entity_revision"],
            "verification": "verified",
            "accepted": True,
            "head": review["head_sha"],
            "base": review["base_sha"],
            "evidence_ids": [evidence.json()["evidence_id"]],
            "reason": "The submitted branch and artifact were checked",
        })
        assert verdict.status_code == 200, verdict.text
        assert (await self.review.review(task_id))["can_merge"]
        return result_id, verdict.json()["verdict_id"]

    async def close(self) -> None:
        await close_team(self.manager)
        await self.manager.close()


async def rig(settings: Settings, db: Database, tmp_path: Path) -> Rig:
    manager = await _manager(settings, db, ScriptedProvider([]))
    team = await team_for(settings, manager)
    runtime = ObservedFakeStaffRuntime(kind="daedalus")
    runtime.manager = manager
    team.runtimes["daedalus"] = runtime
    project = await project_with(manager, repository(tmp_path))
    app = team.app
    app.config = manager.config  # type: ignore[attr-defined]
    board = Board(app)  # type: ignore[arg-type]
    app.extensions["board"] = board  # type: ignore[attr-defined]
    review = Review(app, team)  # type: ignore[arg-type]
    team.review = review
    app.extensions["effects"].register("review.merge", MergeEffect(app))
    return Rig(manager, team, runtime, project, review)


async def test_exact_review_merges_a_commit_then_accepts_the_same_result(settings: Settings, db: Database,
                                                                          tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        member = await r.hire()
        task_id, request = await r.reviewed(member, "Menu page", {"menu.md": "bread\ncake\n"})
        assert request.worktree is not None
        before = git(r.folder, "rev-parse", "HEAD").strip()
        async with r.client() as client:
            result_id, verdict_id = await r.approve(client, task_id)
            merge_body = {"client_operation_id": "merge:menu", "expected_entity_revision":
                          (await task_row(r.manager, task_id))["entity_revision"], "verdict_id": verdict_id}
            queued = await client.post(f"/api/board/{task_id}/results/{result_id}/merge", json=merge_body)
            assert queued.status_code == 200, queued.text
            assert queued.json()["state"] == "queued"
            action_id = queued.json()["action_id"]

            async def merged() -> bool:
                row = await r.manager.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (action_id,))
                return row is not None and row["state"] == "completed"

            await until_await(merged, "review merge completed")
            receipt = await r.manager.db.fetchone("SELECT state,merge_sha,head_sha,base_sha FROM task_merge_receipts"
                                                  " WHERE id = ?", (action_id,))
            assert receipt is not None and receipt["state"] == "merged"
            assert git(r.folder, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:] == [
                before, receipt["head_sha"]]
            assert git(r.folder, "status", "--porcelain") == ""
            assert (r.folder / "menu.md").read_text() == "bread\ncake\n"
            await r.ci_result(task_id, "failure", run_id=2)
            accept_body = {"client_operation_id": "accept:menu", "expected_entity_revision":
                          (await task_row(r.manager, task_id))["entity_revision"],
                          "verdict_id": verdict_id, "contract_revision": 1}
            refused = await client.post(f"/api/board/{task_id}/results/{result_id}/accept", json=accept_body)
            assert refused.status_code == 409 and "required CI" in refused.text
            await r.ci_result(task_id, "success", run_id=3)
            accepted = await client.post(f"/api/board/{task_id}/results/{result_id}/accept", json=accept_body)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["acceptance_state"] == "operator_approved"
            replay = await client.post(f"/api/board/{task_id}/results/{result_id}/accept", json=accept_body)
            assert replay.status_code == 200 and replay.json() == accepted.json()
            row = await task_row(r.manager, task_id)
            assert row["status"] == "done" and row["accepted_result_id"] == result_id
    finally:
        await r.close()


async def test_branch_without_ci_policy_cannot_gain_an_accepted_verdict(settings: Settings, db: Database,
                                                                         tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        member = await r.hire()
        task_id, _request = await r.reviewed(member, "Menu", {"menu.md": "bread\n"}, ci=False)
        review = await r.review.review(task_id)
        assert review["ci_status"] == "blocked" and review["ci_checks"] == []
        assert "ci" in [blocker["code"] for blocker in review["blockers"]]
        assert not review["can_merge"]
        async with r.client() as client:
            refused = await client.post(f"/api/board/{task_id}/results/{review['result_id']}/verdicts", json={
                "client_operation_id": "verdict:without-ci",
                "expected_entity_revision": (await task_row(r.manager, task_id))["entity_revision"],
                "verification": "verified", "accepted": True,
                "head": review["head_sha"], "base": review["base_sha"],
                "evidence_ids": [], "reason": "Reviewed",
            })
            assert refused.status_code == 409 and "required CI" in refused.text
    finally:
        await r.close()


async def test_new_failure_and_new_head_each_block_merge(settings: Settings, db: Database,
                                                          tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        member = await r.hire()
        task_id, request = await r.reviewed(member, "Menu", {"menu.md": "bread\n"})
        assert request.worktree is not None
        async with r.client() as client:
            result_id, verdict_id = await r.approve(client, task_id)
            await r.ci_result(task_id, "failure", run_id=2)
            failed = await r.review.review(task_id)
            assert failed["ci_status"] == "blocked" and failed["ci_checks"][0]["state"] == "failed"
            assert "ci" in [blocker["code"] for blocker in failed["blockers"]]
            prior = (await client.get(f"/api/board/{task_id}/results")).json()
            assert next(item for item in prior if item["result_id"] == result_id)["verification"] == "verified"
            body = {"client_operation_id": "merge:failed-ci", "expected_entity_revision":
                    (await task_row(r.manager, task_id))["entity_revision"], "verdict_id": verdict_id}
            refused = await client.post(f"/api/board/{task_id}/results/{result_id}/merge", json=body)
            assert refused.status_code == 409
            await r.ci_result(task_id, "success", run_id=3)
            (request.worktree.cwd / "later.txt").write_text("later\n")
            git(request.worktree.path, "add", "-A")
            git(request.worktree.path, "commit", "-qm", "Later work")
            projected = await client.get(f"/api/board/{task_id}/results")
            assert projected.status_code == 200
            current = next(item for item in projected.json() if item["result_id"] == result_id)
            assert current["verification"] == "stale"
            assert current["verdict_accepted"] is False
            stale = await r.review.review(task_id)
            assert stale["ci_status"] == "blocked" and stale["ci_checks"][0]["state"] == "unknown"
            assert {item["code"] for item in stale["blockers"]} == {"ci", "verdict_stale"}
            assert (await client.post(f"/api/board/{task_id}/results/{result_id}/merge", json={
                **body, "client_operation_id": "merge:stale-ci",
            })).status_code == 409
            assert await r.manager.db.fetchall("SELECT id FROM task_merge_receipts WHERE task_id = ?",
                                                (task_id,)) == []
    finally:
        await r.close()


async def test_ci_failure_arriving_during_merge_lease_check_stops_git(settings: Settings, db: Database,
                                                                       tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        member = await r.hire()
        task_id, _request = await r.reviewed(member, "Menu", {"menu.md": "bread\n"})
        async with r.client() as client:
            result_id, verdict_id = await r.approve(client, task_id)
            r.team.app.extensions["effects"] = None
            review = await r.review.review(task_id)
            before = git(r.folder, "rev-parse", "HEAD").strip()
            queued = await client.post(f"/api/board/{task_id}/results/{result_id}/merge", json={
                "client_operation_id": "merge:ci-race",
                "expected_entity_revision": (await task_row(r.manager, task_id))["entity_revision"],
                "verdict_id": verdict_id,
            })
            assert queued.status_code == 200, queued.text
            action_id = queued.json()["action_id"]
            claim = Claim(action_id, queued.json().get("receipt_id", ""), "review.merge", 1,
                          Principal.operator({"via": "token", "user_id": 1}), Scope("project", r.project.id),
                          task_id, None, ("git.merge",), "review.merge",
                          {"task_id": task_id, "result_id": result_id, "verdict_id": verdict_id,
                           "head_sha": review["head_sha"], "base_sha": review["base_sha"]},
                          False)

            async def newer_failure(_claim: Claim) -> None:
                await r.ci_result(task_id, "failure", run_id=2)

            outcome = await MergeEffect(r.team.app).run(claim, newer_failure)
            assert outcome.state == "failed" and "required CI" in outcome.error
            assert git(r.folder, "rev-parse", "HEAD").strip() == before
            assert (await r.manager.db.fetchone("SELECT state FROM task_merge_receipts WHERE id = ?",
                                                 (action_id,)))["state"] == "queued"
    finally:
        await r.close()


@pytest.mark.parametrize("change,code", [("dirty", "dirty"), ("head", "verdict_stale"),
                                           ("conflict", "conflicts")])
async def test_merge_preflight_preserves_work_and_requires_fresh_review(
    settings: Settings, db: Database, tmp_path: Path, change: str, code: str,
) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        member = await r.hire()
        task_id, request = await r.reviewed(member, "Readme", {"README.md": "bakery by Ada\n"})
        assert request.worktree is not None
        async with r.client() as client:
            result_id, verdict_id = await r.approve(client, task_id)
            before = git(r.folder, "rev-parse", "HEAD").strip()
            if change == "dirty":
                (r.folder / "README.md").write_text("uncommitted operator work\n")
            elif change == "head":
                (request.worktree.cwd / "other.txt").write_text("new worker work\n")
                git(request.worktree.path, "add", "-A")
                git(request.worktree.path, "commit", "-qm", "Later worker change")
            else:
                (r.folder / "README.md").write_text("operator edit\n")
                git(r.folder, "commit", "-qam", "Operator edit")
                before = git(r.folder, "rev-parse", "HEAD").strip()
            inspected = await r.review.review(task_id)
            assert code in [blocker["code"] for blocker in inspected["blockers"]]
            refused = await client.post(f"/api/board/{task_id}/results/{result_id}/merge", json={
                "client_operation_id": f"merge:{change}",
                "expected_entity_revision": (await task_row(r.manager, task_id))["entity_revision"],
                "verdict_id": verdict_id,
            })
            assert refused.status_code == 409
            assert git(r.folder, "rev-parse", "HEAD").strip() == before
            assert await r.manager.db.fetchall("SELECT id FROM task_merge_receipts WHERE task_id = ?",
                                                (task_id,)) == []
            assert request.worktree.path.exists()
    finally:
        await r.close()


async def test_legacy_one_tap_routes_cannot_bypass_exact_review(settings: Settings, db: Database,
                                                                 tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        member = await r.hire()
        task_id, _request = await r.reviewed(member, "Menu", {"menu.md": "bread\n"})
        r.team.app.front = None
        r.team.app.guard = None
        api = build_app(r.team.app, "tok")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                     base_url="http://test") as client:
            headers = {"X-Daedalus-Token": "tok"}
            assert (await client.post(f"/api/board/{task_id}/accept", headers=headers)).status_code == 404
            assert (await client.post(f"/api/board/{task_id}/merge", headers=headers)).status_code == 404
            review = await r.review.review(task_id)
            assert review["result_id"] and review["verdict_id"] is None
            assert [blocker["code"] for blocker in review["blockers"]] == ["verdict_missing"]
            assert (await task_row(r.manager, task_id))["status"] == "review"
    finally:
        await r.close()


async def test_comparison_review_uses_member_worktree_without_projecting_branch(settings: Settings, db: Database,
                                                                                   tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        member = await r.hire()
        task_id = await board_task(r.manager, r.project, "Compare reports")
        assert r.project.primary is not None
        worktree = await r.team.worktrees.prepare(r.project.primary, member.name, task_id, "Compare reports")
        (worktree.cwd / "candidate.txt").write_text("candidate\n")
        git(worktree.path, "add", "-A")
        git(worktree.path, "commit", "-qm", "Candidate work")
        await r.manager.db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status,status_at,"
                                   "started_at,folder_id,worktree_path,branch,base_ref)"
                                   " VALUES ('candidate-session',?,'daedalus',?,'working','2026-01-01',"
                                   " '2026-01-01',?,?,?,?)",
                                   (member.id, task_id, r.project.primary.id, str(worktree.path),
                                    worktree.branch, worktree.base_ref))
        await r.manager.db.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                                   "fence_token_hash,state,staff_session_id,runtime_kind,created_at,updated_at)"
                                   " VALUES ('candidate-attempt',?,1,1,'digest','completed','candidate-session',"
                                   " 'daedalus','2026-01-01','2026-01-01')", (task_id,))
        await r.manager.db.execute("INSERT INTO comparison_groups(id,task_id,contract_revision,max_attempts,"
                                   "created_at,state,budget_cap_microusd)"
                                   " VALUES ('candidate-group',?,1,2,'2026-01-01','active',1000)", (task_id,))
        await r.manager.db.execute("INSERT INTO comparison_group_attempts(group_id,attempt_id,slot,"
                                   "reserved_microusd,worktree_identity)"
                                   " VALUES ('candidate-group','candidate-attempt',1,500,?)",
                                   (hashlib.sha256(str(worktree.path).encode()).hexdigest(),))
        content = "complete alternative"
        await r.manager.db.execute("INSERT INTO result_receipts(id,task_id,contract_revision,attempt_id,outcome,"
                                   "original_text,original_digest,original_size_bytes,actor_id,created_at)"
                                   " VALUES ('candidate-result',?,1,'candidate-attempt','complete',?,?,?,?,?)",
                                   (task_id, content, hashlib.sha256(content.encode()).hexdigest(),
                                    len(content), "staff", "2026-01-01"))
        projected = await r.review.review(task_id, comparison_attempt_id="candidate-attempt")
        assert projected["branch"] == worktree.branch
        assert projected["comparison_group_id"] == "candidate-group"
        assert projected["result_id"] == "candidate-result"
        assert projected["can_merge"] is False
        assert [item["code"] for item in projected["blockers"]] == ["ci", "verdict_missing"]
        task = await task_row(r.manager, task_id)
        assert task["branch"] is None and task["current_attempt_id"] is None and task["status"] == "todo"
        with pytest.raises(ReviewRefused, match="comparison attempt"):
            await r.review.review(task_id, comparison_attempt_id="missing")
    finally:
        await r.close()


async def test_return_names_the_current_result_and_keeps_the_branch(settings: Settings, db: Database,
                                                                     tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        member = await r.hire()
        task_id, request = await r.reviewed(member, "Menu", {"menu.md": "bread\n"})
        assert request.worktree is not None
        async with r.client() as client:
            result_id, verdict_id = await r.approve(client, task_id)
            base = f"/api/board/{task_id}/results/{result_id}/return"
            revision = (await task_row(r.manager, task_id))["entity_revision"]
            empty = await client.post(base, json={"client_operation_id": "return:empty",
                                                  "expected_entity_revision": revision,
                                                  "verdict_id": verdict_id, "contract_revision": 1,
                                                  "reason": " "})
            assert empty.status_code == 422
            returned = await client.post(base, json={"client_operation_id": "return:menu",
                                                     "expected_entity_revision": revision,
                                                     "verdict_id": verdict_id, "contract_revision": 1,
                                                     "reason": "Add prices before acceptance"})
            assert returned.status_code == 200, returned.text
            assert returned.json()["result_id"] == result_id
            task = await task_row(r.manager, task_id)
            assert task["status"] == "todo" and task["current_attempt_id"] is None
            assert request.worktree.path.exists()
            assert (await r.manager.db.fetchone("SELECT accepted_result_id FROM board_tasks WHERE id = ?",
                                                 (task_id,)))["accepted_result_id"] is None
    finally:
        await r.close()


async def test_review_patch_is_bounded_without_changing_the_git_evidence(settings: Settings, db: Database,
                                                                          tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        member = await r.hire()
        files = {f"f{index:02d}.txt": "".join(f"line {line}\n" for line in range(40))
                 for index in range(6)}
        task_id, _request = await r.reviewed(member, "Many", files)
        assert r.project.primary is not None
        branch = (await task_row(r.manager, task_id))["branch"]
        few = await r.team.worktrees.compare(r.project.primary, branch, max_files=2)
        assert len(few.files) == 6 and few.added == 240 and not few.patch_complete
        assert few.patch.count("diff --git") == 2
        clipped = await r.team.worktrees.compare(r.project.primary, branch, max_chars=100)
        assert len(clipped.patch) == 100 and not clipped.patch_complete
        full = await r.team.worktrees.compare(r.project.primary, branch)
        assert full.patch.count("diff --git") == 6 and full.patch_complete
        assert (await r.review.review(task_id))["result_id"]
    finally:
        await r.close()
