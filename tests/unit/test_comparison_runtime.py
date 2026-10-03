"""Two comparison members reach distinct real host sessions after paired reservation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from protocore.contracts.llm import LLMObservabilityContext, LLMProviderError, LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.extensions import api_comparisons, api_orchestrator_domain
from daedalus.extensions.board import Board
from daedalus.extensions.board_commands import BoardCommands
from daedalus.extensions.comparison_commands import comparison_state, queue_comparison
from daedalus.extensions.comparison_launch import ComparisonLaunchEffect
from daedalus.extensions.comparison_stop import ComparisonStopEffect
from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.lifecycle import Lifecycle
from daedalus.extensions.merge_effect import MergeEffect
from daedalus.extensions.review import Review
from daedalus.extensions.staff import Team
from daedalus.extensions.staff_results import StaffReportService
from daedalus.host.inference_admission import HostInferenceAdmission
from daedalus.host.session_runner import SessionManager
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.providers.pricing import ModelPricing
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.runtime_release import physical_exit_in
from daedalus.stores.sqlite import SqliteUsageSink
from tests.support.waiting import until_await
from tests.unit.test_session_runner import ScriptedProvider, _manager
from tests.unit.test_staff_runtime import close_team, git, project_with, repository, team_for


class InspectingProvider(ScriptedProvider):
    def __init__(self, db: Database) -> None:
        super().__init__([{"text": "Alternative one"}, {"text": "Alternative two"}])
        self.db = db
        self.reserved_at_send: list[int] = []
        self.endpoint = SimpleNamespace(id="scripted", kind="llamacpp", pricing={})

    async def stream_with_tools(self, request):
        count = await self.db.fetchone("SELECT count(*) AS n FROM comparison_funding_slots WHERE state = 'held'")
        self.reserved_at_send.append(int(count["n"]))
        async for delta in super().stream_with_tools(request):
            yield delta


class HeldProvider(InspectingProvider):
    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.release = asyncio.Event()

    async def stream_with_tools(self, request):
        async for delta in super().stream_with_tools(request):
            yield delta
            await self.release.wait()


@pytest.mark.parametrize("priced_transport,child_call,reviewed,stopped,cancel_group", [
    (False, False, False, False, False),
    (True, False, False, False, False),
    (True, True, False, False, False),
    (True, False, True, False, False),
    (False, False, False, True, False),
    (False, False, False, False, True),
])
async def test_two_native_sessions_start_only_after_both_slots_exist(settings, db: Database,
                                                                     tmp_path: Path,
                                                                     priced_transport: bool,
                                                                     child_call: bool,
                                                                     reviewed: bool,
                                                                     stopped: bool,
                                                                     cancel_group: bool) -> None:
    repo = repository(tmp_path)
    provider = HeldProvider(db) if stopped or cancel_group else InspectingProvider(db)
    manager: SessionManager = await _manager(settings, db, provider)
    adapter = None
    transported: list[tuple[int, list[tuple[str, str, str]]]] = []
    child_sent = False
    if priced_transport:
        endpoint = ProviderEndpoint(id="scripted", kind="openai_compat", base_url="http://127.0.0.1",
                                    pricing={"scripted-model": ModelPricing(input=1.0, output=1.0,
                                                                              input_limit=1000,
                                                                              limit_source="provider")})

        async def respond(sent):
            nonlocal child_sent
            count = await db.fetchone("SELECT count(*) AS n FROM comparison_funding_slots WHERE state = 'held'")
            rows = await db.fetchall("SELECT comparison_slot_id,execution_attempt_id,state"
                                     " FROM inference_reservations WHERE state = 'inflight'")
            transported.append((int(count["n"]), [tuple(row) for row in rows]))
            if child_call and not child_sent:
                child_sent = True
                parent = await db.fetchone("SELECT s.session_id,a.task_id FROM execution_attempts a"
                                           " JOIN staff_sessions s ON s.id = a.staff_session_id"
                                           " WHERE a.id = ?", (rows[-1]["execution_attempt_id"],))
                assert parent is not None
                source = await db.fetchone("SELECT tenant_id,project_id FROM sessions WHERE id = ?",
                                           (parent["session_id"],))
                await db.execute("INSERT INTO sessions(id,tenant_id,project_id,title,created_at,last_message_at,"
                                 "metadata) VALUES ('comparison-child',?,?, 'Child','2026-01-01','2026-01-01',?)",
                                 (source["tenant_id"], source["project_id"],
                                  json.dumps({"subagent_of": parent["session_id"]})))
                await db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                                 " VALUES ('comparison-child-run',?,'comparison-child','running',"
                                 "'2026-01-01','2026-01-01')", (source["tenant_id"],))
                child_observation = LLMObservabilityContext(tenant_id=source["tenant_id"],
                                                            session_id="comparison-child",
                                                            run_id="comparison-child-run")
                await adapter.complete_text(LLMRequest(
                    model="scripted-model", max_tokens=20,
                    messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="child context")])],
                    observability=child_observation,
                ))
                calls_before_refusals = len(transported)
                for model, maximum, reason in (("fallback-model", 20, "inference budget"),
                                               ("scripted-model", 1_000_000, "output ceiling"),
                                               ("scripted-model", 20, "prepaid balance")):
                    with pytest.raises(LLMProviderError, match=reason):
                        await adapter.complete_text(LLMRequest(
                            model=model, max_tokens=maximum,
                            messages=[Message(role=MessageRole.user,
                                              content_blocks=[TextBlock(text="out of scope")])],
                            observability=child_observation,
                        ))
                assert len(transported) == calls_before_refusals
            if not json.loads(sent.content).get("stream"):
                return httpx.Response(200, json={"choices": [{"message": {"content": "Child context"},
                                                               "finish_reason": "stop"}],
                                                "usage": {"prompt_tokens": 2, "completion_tokens": 3}})
            chunks = [{"choices": [{"delta": {"content": "An alternative"}, "finish_reason": "stop"}]},
                      {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}]
            wire = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, text=wire, headers={"content-type": "text/event-stream"})

        adapter = OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
                                           usage_sink=SqliteUsageSink(db), admission=HostInferenceAdmission(manager))
        manager.providers.rungs_for = lambda _config: [(adapter, "scripted-model")]
    team: Team | None = None
    try:
        team = await team_for(settings, manager, stagger=0)
        dispatcher: EffectDispatcher = team.app.extensions["effects"]
        comparison = ComparisonLaunchEffect(team.app)
        dispatcher.register("comparison.launch.first", comparison)
        dispatcher.register("comparison.launch.second", comparison)
        if stopped:
            dispatcher.register("comparison.stop", ComparisonStopEffect(team.app))
        if cancel_group:
            dispatcher.register("lifecycle.stop", Lifecycle(team.app))
        if reviewed:
            team.app.extensions["board"] = Board(team.app)
            team.review = Review(team.app, team)
            dispatcher.register("review.merge", MergeEffect(team.app))
        project = await project_with(manager, repo, concurrency=2)
        folder = await db.fetchone("SELECT id FROM project_folders WHERE project_id = ?", (project.id,))
        assert folder is not None
        first = await manager.staff.hire(project.id, name="Ada", role="First alternative", isolation="worktree")
        second = await manager.staff.hire(project.id, name="Bora", role="Second alternative", isolation="worktree")
        scope = Scope("project", project.id)
        collection = await ControlStore(db).revision(scope, Entity("collection", project.id))
        created = await BoardCommands(db).create(
            Principal.operator({"via": "token", "user_id": 1}), scope,
            client_operation_id="comparison-task", expected_collection_revision=collection,
            title="Compare menu variants", brief={"objective": "Compare menu variants",
                                                  "deliverable": "Two separate suggestions",
                                                  "boundaries": "Only the temporary repository",
                                                  "done_when": "Each suggestion is recorded"},
            folder_id=folder["id"],
        )
        task_id = created["task_id"]
        revision = await ControlStore(db).revision(scope, Entity("task", task_id))
        launched = await queue_comparison(
            team.app, task_id=task_id, principal=Principal.operator({"via": "token", "user_id": 1}),
            client_operation_id="comparison-pair", expected_entity_revision=revision,
            contract_revision=1, budget_cap_microusd=100_000,
            alternatives=({"staff_id": first.id, "allowance_microusd": 34_020},
                          {"staff_id": second.id, "allowance_microusd": 40_000}),
        )

        async def both_started() -> bool:
            rows = await db.fetchall("SELECT id,state FROM effect_outbox WHERE kind LIKE 'comparison.launch.%'")
            return len(rows) == 2 and all(row["state"] == "completed" for row in rows)

        await until_await(both_started, "both alternatives reached the host runtime")
        attempts = await db.fetchall("SELECT a.id,a.staff_session_id,s.worktree_path,s.branch,s.staff_id"
                                     " FROM execution_attempts a JOIN staff_sessions s ON s.id = a.staff_session_id"
                                     " WHERE a.task_id = ? ORDER BY s.staff_id", (task_id,))
        assert len(attempts) == 2
        assert {item["staff_id"] for item in attempts} == {first.id, second.id}
        assert len({item["worktree_path"] for item in attempts}) == 2
        assert len({item["branch"] for item in attempts}) == 2
        assert all(Path(item["worktree_path"]).is_dir() for item in attempts)
        assert (await db.fetchone("SELECT current_attempt_id FROM board_tasks WHERE id = ?", (task_id,)))[0] is None
        assert {item["attempt_id"] for item in await db.fetchall(
            "SELECT attempt_id FROM comparison_group_attempts WHERE group_id = ?", (launched["group_id"],)
        )} == {item["id"] for item in attempts}
        async with asyncio.timeout(30):
            while len(transported if priced_transport else provider.reserved_at_send) < (3 if child_call else 2):
                await asyncio.sleep(0.05)
        if priced_transport:
            assert [count for count, _ in transported] == [2] * (3 if child_call else 2)
            assert all(len(charges) >= 1 for _, charges in transported)
            async def both_settled() -> bool:
                rows = await db.fetchall("SELECT state FROM inference_reservations ORDER BY created_at,id")
                return len(rows) == (3 if child_call else 2) and all(row["state"] == "settled" for row in rows)

            await until_await(both_settled, "both model sends settled against their own prepaid slot")
            charges = await db.fetchall("SELECT comparison_slot_id,execution_attempt_id,state"
                                         " FROM inference_reservations ORDER BY created_at,id")
            assert len(charges) == (3 if child_call else 2)
            assert {item["comparison_slot_id"] for item in charges} == {
                slot["slot_id"] for slot in launched["slots"]
            }
            assert {item["execution_attempt_id"] for item in charges} == {item["id"] for item in attempts}
            if child_call:
                first_slot = launched["slots"][0]
                assert sum(item["comparison_slot_id"] == first_slot["slot_id"] for item in charges) == 2
                assert sum(item["execution_attempt_id"] == first_slot["attempt_id"] for item in charges) == 2
        else:
            assert provider.reserved_at_send == [2, 2]
        if stopped:
            async def auth():
                return {"via": "token", "user_id": 1}

            api = FastAPI()
            api_comparisons.install_routes(api, team.app, auth)
            revision = await ControlStore(db).revision(scope, Entity("task", task_id))
            body = {"client_operation_id": "stop-first", "expected_entity_revision": revision,
                    "reason": "Stop only first contender"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                         base_url="http://127.0.0.1") as client:
                stopped_response = await client.post(
                    f"/api/board/{task_id}/comparisons/{launched['group_id']}/slots/1/stop", json=body,
                )
                assert stopped_response.status_code == 200, stopped_response.text
                assert stopped_response.json()["state"] == "queued"
                assert (await client.post(
                    f"/api/board/{task_id}/comparisons/{launched['group_id']}/slots/1/stop", json=body,
                )).json() == stopped_response.json()

            async def first_exited() -> bool:
                async with db.transaction() as conn:
                    return await physical_exit_in(conn, launched["slots"][0]["attempt_id"])

            await until_await(first_exited, "the exact first contender stopped physically")
            await dispatcher.reconcile()
            assert (await db.fetchone("SELECT state FROM effect_outbox WHERE id = ?",
                                     (stopped_response.json()["effect_id"],)))["state"] == "completed"
            async with db.transaction() as conn:
                assert not await physical_exit_in(conn, launched["slots"][1]["attempt_id"])
        if cancel_group:
            lifecycle = Lifecycle(team.app)
            preview = await lifecycle.preview("task", task_id)
            assert {item["child_id"] for item in preview["owned_descendants"]
                    if item["child_kind"] == "execution_attempt"} == {
                slot["attempt_id"] for slot in launched["slots"]
            }
            cancelled = await lifecycle.cancel_command(
                Principal.operator({"via": "token", "user_id": 1}), "task", task_id,
                "Stop both alternatives", expected_entity_revision=preview["entity_revision"],
                expected_source_revision=preview["source_revision"], client_operation_id="cancel-pair",
                preview_fingerprint=preview["preview_fingerprint"],
            )
            assert cancelled["cancel_state"] == "requested"
        reviewed_results = []
        if reviewed:
            reports = StaffReportService(team.app)
            for number, reserved in enumerate(launched["slots"], 1):
                attempt = next(item for item in attempts if item["id"] == reserved["attempt_id"])
                worktree = Path(attempt["worktree_path"])
                (worktree / f"proposal-{number}.md").write_text(f"Alternative {number}\n")
                git(worktree, "add", f"proposal-{number}.md")
                git(worktree, "commit", "-qm", f"alternative {number}")
                live = await team.live(attempt["staff_session_id"])
                assert live is not None
                report, _event = await reports.submit(live, "done", f"Alternative {number} complete",
                                                       artifacts=[f"proposal-{number}.md"],
                                                       call_id=f"member-report-{number}")
                reviewed_results.append((attempt, report))
            assert (await db.fetchone("SELECT current_attempt_id,status FROM board_tasks WHERE id = ?",
                                     (task_id,)))["current_attempt_id"] is None
        if not stopped and not cancel_group:
            assert await team.release(first, keep_worktree=True)
        if not cancel_group:
            assert await team.release(second, keep_worktree=True)

        async def both_exited() -> bool:
            async with db.transaction() as conn:
                return all([await physical_exit_in(conn, item["id"]) for item in attempts])

        await until_await(both_exited, "both actual runtimes have exact exit observations")
        if cancel_group:
            await lifecycle.drain_verified()
            await dispatcher.reconcile()
            assert (await db.fetchone("SELECT cancel_state FROM lifecycle_parents"
                                     " WHERE parent_kind = 'task' AND parent_id = ?", (task_id,)))["cancel_state"] == "drained"
        readiness = await comparison_state(team.app, task_id=task_id, group_id=launched["group_id"])
        assert all(item["physical_exit_verified"] for item in readiness["alternatives"])
        assert all(item["observed_cost_microusd"] is not None for item in readiness["alternatives"])
        assert sum(reason.startswith("result_missing:") for reason in readiness["blockers"]) == (0 if reviewed else 2)
        if reviewed:
            async def auth():
                return {"via": "token", "user_id": 1}

            api = FastAPI()
            api_orchestrator_domain.install_routes(api, team.app, auth)
            api_comparisons.install_routes(api, team.app, auth)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                         base_url="http://127.0.0.1") as client:
                missing_slot = await client.get(
                    f"/api/board/{task_id}/comparisons/{launched['group_id']}/slots/3/review",
                )
                assert missing_slot.status_code == 404
                verdicts = []
                verdict_commands = []
                for number, (attempt, report) in enumerate(reviewed_results, 1):
                    inspected_response = await client.get(
                        f"/api/board/{task_id}/comparisons/{launched['group_id']}/slots/{number}/review",
                    )
                    assert inspected_response.status_code == 200, inspected_response.text
                    inspected = inspected_response.json()
                    assert inspected["attempt_id"] == attempt["id"]
                    assert inspected["result_id"] == report["result_id"]
                    assert inspected["source_current"] and inspected["physical_exit_verified"]
                    assert not inspected["self_review_waiver_required"]
                    assert inspected["head_sha"] != inspected["base_sha"]
                    manifest = next(item for item in inspected["artifacts"] if item["file_id"] is not None)
                    revision = await ControlStore(db).revision(scope, Entity("task", task_id))
                    attested = await client.post(
                        f"/api/board/{task_id}/results/{report['result_id']}/evidence",
                        json={"client_operation_id": f"evidence-{number}",
                              "expected_entity_revision": revision, "manifest_id": manifest["manifest_id"],
                              "criterion_id": "manual-review", "observation": "Read the attached proposal"},
                    )
                    assert attested.status_code == 200, attested.text
                    revision = await ControlStore(db).revision(scope, Entity("task", task_id))
                    body = {"client_operation_id": f"verdict-{number}", "expected_entity_revision": revision,
                            "result_id": report["result_id"], "verification": "verified", "accepted": True,
                            "evidence_ids": [attested.json()["evidence_id"]]}
                    invented_head = await client.post(
                        f"/api/board/{task_id}/comparisons/{launched['group_id']}/slots/{number}/verdicts",
                        json={**body, "head": inspected["head_sha"]},
                    )
                    assert invented_head.status_code == 422
                    response = await client.post(
                        f"/api/board/{task_id}/comparisons/{launched['group_id']}/slots/{number}/verdicts",
                        json=body,
                    )
                    assert response.status_code == 200, response.text
                    assert response.json()["head_sha"] == inspected["head_sha"]
                    assert response.json()["base_sha"] == inspected["base_sha"]
                    verdicts.append(response.json()["verdict_id"])
                    verdict_commands.append(body)
                winner_attempt, winner_report = reviewed_results[0]
                winner_review = await client.get(
                    f"/api/board/{task_id}/comparisons/{launched['group_id']}/slots/1/review",
                )
                assert winner_review.status_code == 200 and winner_review.json()["can_choose"]
                winner_worktree = Path(winner_attempt["worktree_path"])
                (winner_worktree / "follow-up.md").write_text("Updated contender\n")
                git(winner_worktree, "add", "follow-up.md")
                git(winner_worktree, "commit", "-qm", "follow-up")
                revision = await ControlStore(db).revision(scope, Entity("task", task_id))
                stale_choice = await client.post(
                    f"/api/board/{task_id}/comparisons/{launched['group_id']}/choose",
                    json={"client_operation_id": "choose-stale", "expected_entity_revision": revision,
                          "result_id": winner_report["result_id"], "verdict_id": verdicts[0]},
                )
                assert stale_choice.status_code == 409, stale_choice.text
                still_unselected = await db.fetchone(
                    "SELECT current_attempt_id,status,entity_revision FROM board_tasks WHERE id = ?", (task_id,),
                )
                assert tuple(still_unselected) == (None, "todo", revision)
                assert [row["state"] for row in await db.fetchall(
                    "SELECT state FROM comparison_funding_slots WHERE group_id = ? ORDER BY slot",
                    (launched["group_id"],),
                )] == ["held", "held"]
                stale_review = await client.get(
                    f"/api/board/{task_id}/comparisons/{launched['group_id']}/slots/1/review",
                )
                assert stale_review.status_code == 200
                assert stale_review.json()["source_current"] and not stale_review.json()["can_choose"]
                assert any(item["code"] == "verdict_stale" for item in stale_review.json()["blockers"])
                replacement_verdict = {**verdict_commands[0], "client_operation_id": "verdict-1-new-head",
                                       "expected_entity_revision": revision}
                replaced = await client.post(
                    f"/api/board/{task_id}/comparisons/{launched['group_id']}/slots/1/verdicts",
                    json=replacement_verdict,
                )
                assert replaced.status_code == 200, replaced.text
                assert replaced.json()["head_sha"] != winner_review.json()["head_sha"]
                verdicts[0] = replaced.json()["verdict_id"]
                verdict_commands[0] = replacement_verdict
                renewed_review = await client.get(
                    f"/api/board/{task_id}/comparisons/{launched['group_id']}/slots/1/review",
                )
                assert renewed_review.status_code == 200 and renewed_review.json()["can_choose"]
                revision = await ControlStore(db).revision(scope, Entity("task", task_id))
                choice = await client.post(
                    f"/api/board/{task_id}/comparisons/{launched['group_id']}/choose",
                    json={"client_operation_id": "choose-winner", "expected_entity_revision": revision,
                          "result_id": winner_report["result_id"], "verdict_id": verdicts[0]},
                )
                assert choice.status_code == 200, choice.text
                assert choice.json()["state"] == "chosen"
                replayed_verdict = await client.post(
                    f"/api/board/{task_id}/comparisons/{launched['group_id']}/slots/1/verdicts",
                    json=verdict_commands[0],
                )
                assert replayed_verdict.status_code == 200
                assert replayed_verdict.json()["verdict_id"] == verdicts[0]
                selected = await db.fetchone("SELECT current_attempt_id,status,branch,acceptance_state,"
                                             " accepted_result_id FROM board_tasks WHERE id = ?",
                                             (task_id,))
                assert selected["current_attempt_id"] == winner_attempt["id"]
                assert selected["status"] == "review"
                assert selected["acceptance_state"] == "accepted"
                assert selected["accepted_result_id"] is None
                assert selected["branch"] == (await db.fetchone(
                    "SELECT branch FROM staff_sessions WHERE id = ?",
                    (winner_attempt["staff_session_id"],)))["branch"]
                assert (await team.review.review(task_id))["can_merge"]
                revision = await ControlStore(db).revision(scope, Entity("task", task_id))
                merge = await client.post(
                    f"/api/board/{task_id}/results/{winner_report['result_id']}/merge",
                    json={"client_operation_id": "merge-winner", "expected_entity_revision": revision,
                          "verdict_id": verdicts[0]},
                )
                assert merge.status_code == 200, merge.text
                action_id = merge.json()["action_id"]

                async def merged() -> bool:
                    row = await db.fetchone("SELECT state FROM task_merge_receipts WHERE id = ?", (action_id,))
                    return row is not None and row["state"] == "merged"

                await until_await(merged, "the selected branch was merged by the fenced effect")
                merged_receipt = await db.fetchone("SELECT state,head_sha,base_sha,merge_sha FROM task_merge_receipts"
                                                   " WHERE id = ?", (action_id,))
                assert merged_receipt["state"] == "merged"
                assert merged_receipt["merge_sha"] not in (merged_receipt["head_sha"], merged_receipt["base_sha"])
                revision = await ControlStore(db).revision(scope, Entity("task", task_id))
                accepted = await client.post(
                    f"/api/board/{task_id}/results/{winner_report['result_id']}/accept",
                    json={"client_operation_id": "accept-winner", "expected_entity_revision": revision,
                          "contract_revision": 1, "verdict_id": verdicts[0]},
                )
                assert accepted.status_code == 200, accepted.text
                task = await db.fetchone("SELECT status,accepted_result_id FROM board_tasks WHERE id = ?",
                                         (task_id,))
                assert tuple(task) == ("done", winner_report["result_id"])
                assert [row["state"] for row in await db.fetchall(
                    "SELECT state FROM comparison_funding_slots WHERE group_id = ? ORDER BY slot",
                    (launched["group_id"],)
                )] == ["released", "released"]
                loser_attempt, loser_report = reviewed_results[1]
                assert (await db.fetchone("SELECT id FROM result_receipts WHERE id = ? AND attempt_id = ?",
                                         (loser_report["result_id"], loser_attempt["id"]))) is not None
                assert (await db.fetchone("SELECT id FROM review_verdicts WHERE id = ? AND result_id = ?",
                                         (verdicts[1], loser_report["result_id"]))) is not None
                assert Path(loser_attempt["worktree_path"]).is_dir()
                assert (repo / "proposal-1.md").read_text() == "Alternative 1\n"
                assert not (repo / "proposal-2.md").exists()
    finally:
        if team is not None:
            await close_team(manager)
        await manager.close()
        if adapter is not None:
            await adapter.aclose()
