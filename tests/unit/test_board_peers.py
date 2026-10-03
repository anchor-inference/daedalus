"""The task board (dependencies, WIP limit, checklist gate, stale hand-back) and named peers."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.board import Board
from daedalus.extensions.peers import Peers
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.files import MAIN
from tests.support.authorized_results import (
    accept_branchless_result,
    operator_domain_client,
    reopen_accepted_result,
    submit_manual_result,
)
from tests.support.notifications import RecordingNotifications


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={})
    app.notifications = RecordingNotifications()
    yield app
    await manager.close()


async def test_board_dependencies_wip_and_checklist(app: Any) -> None:
    board = Board(app)
    app.config.board.wip_limit = 1
    a = await board.add(title="design", checklist=["sketch", "review"])
    b = await board.add(title="build", depends_on=[a["id"]])
    assert a["status"] == "todo" and b["status"] == "blocked"
    with pytest.raises(ValueError):
        await board.add(title="x", depends_on=["nope"])
    await board.update(a["id"], status="doing", session_id="s1")
    with pytest.raises(ValueError):  # WIP limit
        await board.update(b["id"], status="doing")
    with pytest.raises(ValueError):  # dependency still open
        await board.update(b["id"], status="doing")
    with pytest.raises(ValueError):  # checklist incomplete
        await board.update(a["id"], status="done")
    await board.update(a["id"], check=[0, 1], note="all sketched")
    await board.update(a["id"], status="review")
    await submit_manual_result(app, a["id"], note="The design sketch and review are complete")
    await accept_branchless_result(SimpleNamespace(app=app), a["id"])
    done = await board.get(a["id"])
    assert done["status"] == "done" and "all sketched" in done["notes"]
    assert (await board.get(b["id"]))["status"] == "todo"  # promoted
    text = board.render(await board.list(None, include_done=True))
    assert "✅" in text and "build" in text


async def test_board_done_applies_checklist_edits_before_the_gate(app: Any) -> None:
    """A direct completion request changes no checks; review accepts the exact submitted work."""
    board = Board(app)
    t = await board.add(title="ship it", checklist=["write", "test", "announce"])
    with pytest.raises(ValueError) as exc:
        await board.update(t["id"], status="done", check=[0, 1])
    assert "exact-result acceptance" in str(exc.value)
    refused = await board.get(t["id"])
    assert refused["status"] == "todo"  # a refused call changes nothing …
    assert [c["done"] for c in refused["checklist"]] == [False, False, False]  # … not even the checks it sent
    await board.update(t["id"], check=[0, 1, 2], note="published")
    await board.update(t["id"], status="review")
    await submit_manual_result(app, t["id"], note="Written, tested, and announced")
    await accept_branchless_result(SimpleNamespace(app=app), t["id"])
    done = await board.get(t["id"])
    assert done["status"] == "done"
    assert all(c["done"] for c in done["checklist"])
    assert "published" in done["notes"]
    # Uncheck still wins over check for the same index, as it did before the ordering changed.
    t2 = await board.add(title="reopen", checklist=["a", "b"])
    await board.update(t2["id"], check=[0, 1])
    again = await board.update(t2["id"], check=[1], uncheck=[1])
    assert [c["done"] for c in again["checklist"]] == [True, False]


async def test_board_done_gate_reads_the_resulting_checklist(app: Any) -> None:
    """Checklist edits stay precise while completion requires an exact operator decision."""
    board = Board(app)

    async def finish(task_id: str) -> dict[str, Any]:
        await board.update(task_id, status="review")
        await submit_manual_result(app, task_id, note="I completed and checked this task")
        await accept_branchless_result(SimpleNamespace(app=app), task_id)
        return await board.get(task_id)

    # A task with no checklist still has a human completion statement.
    assert (await finish((await board.add(title="bare"))["id"]))["status"] == "done"
    empty = await board.add(title="empty", checklist=[])
    assert (await finish(empty["id"]))["status"] == "done"

    # Out-of-range indexes are ignored; the real decision covers the current criterion.
    ranges = await board.add(title="ranges", checklist=["a"])
    await board.update(ranges["id"], check=[0, 5, -3])
    assert [c["done"] for c in (await board.update(ranges["id"], check=[7], uncheck=[7]))["checklist"]] == [True]
    done = await finish(ranges["id"])
    assert [c["done"] for c in done["checklist"]] == [True]

    # A direct terminal update cannot race an uncheck into an accepted result.
    both = await board.add(title="both", checklist=["a", "b", "c"])
    await board.update(both["id"], check=[0, 1, 2])
    with pytest.raises(ValueError, match="exact-result acceptance"):
        await board.update(both["id"], status="done", check=[1], uncheck=[1])
    assert (await board.get(both["id"]))["status"] != "done"  # the refused call changed nothing
    assert [c["done"] for c in (await board.get(both["id"]))["checklist"]] == [True, True, True]
    assert (await finish(both["id"]))["status"] == "done"

    # A status other than 'done' is not gated at all.
    other = await board.add(title="review", checklist=["a", "b"])
    assert (await board.update(other["id"], status="review", note="half"))["status"] == "review"

    # An accepted result cannot be edited or reopened by a board status shortcut.
    finished = await board.add(title="finished", checklist=["a", "b"])
    await board.update(finished["id"], check=[0, 1])
    await finish(finished["id"])
    with pytest.raises(ValueError, match="exact-result reopen"):
        await board.update(finished["id"], uncheck=[0])
    after = await board.get(finished["id"])
    assert after["status"] == "done" and [c["done"] for c in after["checklist"]] == [True, True]
    with pytest.raises(ValueError, match="exact-result reopen"):
        await board.update(finished["id"], status="doing", uncheck=[0])

    # The operator can verify a larger checklist without a worker or synthetic file.
    long_task = await board.add(title="long", checklist=[f"item {i}" for i in range(12)])
    await board.update(long_task["id"], check=list(range(5)))
    assert [item["done"] for item in (await board.get(long_task["id"]))["checklist"]] == [True] * 5 + [False] * 7
    await board.update(long_task["id"], check=list(range(5, 12)))
    final = await finish(long_task["id"])
    assert final["status"] == "done" and all(c["done"] for c in final["checklist"])


async def test_board_hands_back_quiet_tasks(app: Any) -> None:
    board = Board(app)
    app.config.board.stale_hours = 1
    t = await board.add(title="lingering")
    await board.update(t["id"], status="doing", session_id="ghost")
    await app.db.execute("UPDATE board_tasks SET heartbeat_at = '2020-01-01T00:00:00+00:00', updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (t["id"],))
    assert await board.recover_stale() == [t["id"]]
    assert (await board.get(t["id"]))["status"] == "todo"
    [draft] = app.notifications.drafts
    assert (draft.kind, draft.level) == ("board_stale", "quiet")


async def test_peers_register_ask_and_depth(app: Any) -> None:
    peers = Peers(app)
    manager: SessionManager = app.manager
    asker = await manager.create_session("asker")
    target = await manager.create_session("reviewer")
    with pytest.raises(ValueError):
        await peers.register("Bad Name!", target.session.id)
    await peers.register("reviewer", target.session.id)
    assert await peers.registry() == {"reviewer": target.session.id}
    submitted: list[tuple[str, str, str]] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer=False, as_answer=True, origin="operator") -> str:  # type: ignore[no-untyped-def]
        submitted.append((session_id, text, origin))
        return "run-p"

    manager.submit = fake_submit  # type: ignore[method-assign]
    result = await peers.ask(from_session=asker.session.id, name="reviewer", prompt="is this ok?", wait=False, timeout_minutes=None)
    assert result["run_id"] == "run-p" and submitted[0][0] == target.session.id and submitted[0][2].startswith("peer:")
    assert target.metadata["peer_depth"] == 1
    with pytest.raises(ValueError):
        await peers.ask(from_session=target.session.id, name="reviewer", prompt="self", wait=False, timeout_minutes=None)
    app.config.peers.max_depth = 1
    with pytest.raises(ValueError):  # target is at depth 1 already; asking onward would be depth 2
        await peers.ask(from_session=target.session.id, name="reviewer2", prompt="x", wait=False, timeout_minutes=None)
    assert await peers.forget("reviewer") and await peers.registry() == {}


async def test_board_releases_the_claim_and_reblocks_on_reopen(app: Any) -> None:
    board = Board(app)
    a = await board.add(title="first")
    b = await board.add(title="second", depends_on=[a["id"]])
    await board.update(a["id"], status="doing", session_id="s1", run_id="r1")
    moved = await board.update(a["id"], status="review")
    assert moved["session_id"] is None and moved["run_id"] is None
    await submit_manual_result(app, a["id"], note="The first task is complete")
    await accept_branchless_result(SimpleNamespace(app=app), a["id"])
    assert (await board.get(b["id"]))["status"] == "todo"
    reopened = await reopen_accepted_result(app, a["id"], reason="The first task needs another review")
    assert reopened["status"] == "todo"
    assert (await board.get(b["id"]))["status"] == "blocked"
    await board.update(a["id"], status="review")
    async with operator_domain_client(app) as client:
        old = await client.post(f"/api/board/{a['id']}/results/{reopened['result_id']}/accept", json={
            "client_operation_id": "reaccept-old-work",
            "expected_entity_revision": (await board.get(a["id"]))["entity_revision"],
            "verdict_id": reopened["verdict_id"], "contract_revision": reopened["contract_revision"],
        })
        assert old.status_code == 409
    with pytest.raises(ValueError, match="audited results"):
        await board.delete(a["id"])
    assert (await board.get(b["id"]))["depends_on"] == [a["id"]]
    c = await board.add(title="unused predecessor")
    d = await board.add(title="follows unused", depends_on=[c["id"]])
    assert await board.delete(c["id"])
    assert (await board.get(d["id"]))["depends_on"] == []


async def test_manual_result_refuses_attempt_spoof_pair_and_stale_contract(app: Any) -> None:
    board = Board(app)
    task = await board.add(title="Manual decision", checklist=["Check the statement"])
    await board.update(task["id"], status="review")
    current = await board.get(task["id"])
    base = f"/api/board/{task['id']}"
    request = {"client_operation_id": "manual-first", "expected_entity_revision": current["entity_revision"],
               "contract_revision": current["contract_revision"], "outcome": "complete",
               "original_text": "I checked the statement", "manifest_ids": []}
    async with operator_domain_client(app) as client:
        spoofed = await client.post(base + "/results", json={**request, "attempt_id": "worker-owned"})
        assert spoofed.status_code == 422
        stale = await client.post(base + "/results", json={**request, "contract_revision": 99})
        assert stale.status_code == 409
        await app.db.execute("INSERT INTO comparison_groups(id,task_id,contract_revision,max_attempts,"
                             "state,created_at) VALUES (?,?,?,?,?,datetime('now'))",
                             (uuid.uuid4().hex, task["id"], current["contract_revision"], 2, "planned"))
        pair = await client.post(base + "/results", json=request)
        assert pair.status_code == 409
        assert await app.db.fetchall("SELECT id FROM result_receipts WHERE task_id = ?", (task["id"],)) == []
        await app.db.execute("DELETE FROM comparison_groups WHERE task_id = ?", (task["id"],))
        report = await client.post(base + "/results", json=request)
        assert report.status_code == 200, report.text
        result_id = report.json()["result_id"]
        listing = await client.get(base + "/results")
        assert listing.json()[0]["origin_kind"] == "operator_manual"
        assert listing.json()[0]["self_review_waiver_required"] is False
        pair_id = uuid.uuid4().hex
        await app.db.execute("INSERT INTO comparison_groups(id,task_id,contract_revision,max_attempts,"
                             "state,created_at) VALUES (?,?,?,?,?,datetime('now'))",
                             (pair_id, task["id"], current["contract_revision"], 2, "planned"))
        blocked_attestation = await client.post(base + f"/results/{result_id}/attest", json={
            "client_operation_id": "pair-blocks-attestation", "expected_entity_revision": report.json()["entity_revision"],
            "criterion_id": "C1", "observation": "I checked the statement",
        })
        assert blocked_attestation.status_code == 409
        unavailable = await client.get(base + "/manual-review")
        assert not unavailable.json()["eligible"]
        assert "execution_owned" in unavailable.json()["blockers"]
        await app.db.execute("DELETE FROM comparison_groups WHERE id = ?", (pair_id,))
        attestation = await client.post(base + f"/results/{result_id}/attest", json={
            "client_operation_id": "valid-attestation", "expected_entity_revision": report.json()["entity_revision"],
            "criterion_id": "C1", "observation": "I checked the statement",
        })
        assert attestation.status_code == 200, attestation.text
        await app.db.execute("INSERT INTO comparison_groups(id,task_id,contract_revision,max_attempts,"
                             "state,created_at) VALUES (?,?,?,?,?,datetime('now'))",
                             (pair_id, task["id"], current["contract_revision"], 2, "planned"))
        blocked_verdict = await client.post(base + f"/results/{result_id}/verdicts", json={
            "client_operation_id": "pair-blocks-verdict", "expected_entity_revision": attestation.json()["entity_revision"],
            "verification": "verified", "accepted": True, "head": None, "base": None,
            "evidence_ids": [attestation.json()["evidence_id"]], "reason": "Checked",
        })
        assert blocked_verdict.status_code == 409
        await app.db.execute("DELETE FROM comparison_groups WHERE id = ?", (pair_id,))
        verdict = await client.post(base + f"/results/{result_id}/verdicts", json={
            "client_operation_id": "valid-verdict", "expected_entity_revision": attestation.json()["entity_revision"],
            "verification": "verified", "accepted": True, "head": None, "base": None,
            "evidence_ids": [attestation.json()["evidence_id"]], "reason": "Checked",
        })
        assert verdict.status_code == 200, verdict.text
        await app.db.execute("INSERT INTO comparison_groups(id,task_id,contract_revision,max_attempts,"
                             "state,created_at) VALUES (?,?,?,?,?,datetime('now'))",
                             (pair_id, task["id"], current["contract_revision"], 2, "planned"))
        blocked_accept = await client.post(base + f"/results/{result_id}/accept", json={
            "client_operation_id": "pair-blocks-accept", "expected_entity_revision": verdict.json()["entity_revision"],
            "verdict_id": verdict.json()["verdict_id"], "contract_revision": current["contract_revision"],
        })
        assert blocked_accept.status_code == 409
        await app.db.execute("DELETE FROM comparison_groups WHERE id = ?", (pair_id,))
        replaced = await client.put(base + "/contract", json={
            "client_operation_id": "new-criterion", "expected_entity_revision": verdict.json()["entity_revision"],
            "requirements": [], "checks": [{"text": "Different statement"}], "acceptance": "",
            "brief": {}, "change_kind": "semantic",
        })
        assert replaced.status_code == 200, replaced.text
        old_criterion = await client.post(base + f"/results/{result_id}/attest", json={
            "client_operation_id": "stale-attestation", "expected_entity_revision": replaced.json()["entity_revision"],
            "criterion_id": "C1", "observation": "I checked the old statement",
        })
        assert old_criterion.status_code == 409


async def test_human_report_enters_review_atomically_and_keeps_attestation_provenance(app: Any) -> None:
    board = Board(app)
    task = await board.add(title="Read the statement", checklist=["Statement read"])
    base = f"/api/board/{task['id']}"
    async with operator_domain_client(app) as client:
        readiness = (await client.get(base + "/manual-review")).json()
        assert readiness["eligible"] and readiness["attached_artifacts"] == []
        payload = {"client_operation_id": "human-report", "expected_entity_revision": readiness["entity_revision"],
                   "contract_revision": readiness["contract_revision"], "outcome": "complete",
                   "original_text": "I read the statement and checked its contents", "manifest_ids": []}
        response = await client.post(base + "/results", json=payload)
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["status"] == "review" and report["entity_revision"] == readiness["entity_revision"] + 1
        assert (await board.get(task["id"]))["status"] == "review"
        assert (await client.post(base + "/results", json=payload)).json() == report
        assert (await app.db.fetchone("SELECT COUNT(*) FROM result_receipts WHERE task_id = ?", (task["id"],)))[0] == 1
        result_id = report["result_id"]
        proof = await client.post(base + f"/results/{result_id}/attest", json={
            "client_operation_id": "human-proof", "expected_entity_revision": report["entity_revision"],
            "criterion_id": "C1", "observation": "I checked the statement myself",
        })
        assert proof.status_code == 200, proof.text
        # A matching text prefix and null digests are insufficient without the operator receipt.
        await app.db.execute("INSERT INTO review_evidence(id,result_id,contract_revision,criterion_id,command,"
                             "observed_at) VALUES ('unreceipted',? ,?,'C1','operator attestation: invented','now')",
                             (result_id, readiness["contract_revision"]))
        projected = (await client.get(base + f"/results/{result_id}/evidence")).json()
        by_id = {item["evidence_id"]: item["verification"] for item in projected}
        assert by_id[proof.json()["evidence_id"]] == "operator_attested"
        assert by_id["unreceipted"] == "stale"
        verdict_payload = {"client_operation_id": "fake-human-verdict", "expected_entity_revision": proof.json()["entity_revision"],
                           "verification": "verified", "accepted": True, "head": None, "base": None,
                           "evidence_ids": ["unreceipted"], "reason": "Read"}
        assert (await client.post(base + f"/results/{result_id}/verdicts", json=verdict_payload)).status_code == 409
        verdict_payload.update(client_operation_id="real-human-verdict", evidence_ids=[proof.json()["evidence_id"]])
        verdict = await client.post(base + f"/results/{result_id}/verdicts", json=verdict_payload)
        assert verdict.status_code == 200, verdict.text
        accepted = await client.post(base + f"/results/{result_id}/accept", json={
            "client_operation_id": "accept-human-report", "expected_entity_revision": verdict.json()["entity_revision"],
            "contract_revision": readiness["contract_revision"], "verdict_id": verdict.json()["verdict_id"],
        })
        assert accepted.status_code == 200, accepted.text
        assert (await board.get(task["id"]))["status"] == "done"


async def test_manual_file_requirement_needs_the_exact_attached_source(app: Any) -> None:
    board = Board(app)
    task = await board.add(title="Use the supplied document")
    source = await app.manager.files.add(b"Approved document", name="approved.txt", origin="operator",
                                         scope=MAIN, actor="operator")
    other = await app.manager.files.add(b"Another document", name="other.txt", origin="operator",
                                        scope=MAIN, actor="operator")
    await app.manager.files.attach_to_task(task["id"], [source, other], actor="operator")
    base = f"/api/board/{task['id']}"
    async with operator_domain_client(app) as client:
        revision = (await board.get(task["id"]))["entity_revision"]
        changed = await client.put(base + "/contract", json={
            "client_operation_id": "pin-source", "expected_entity_revision": revision,
            "requirements": [{"kind": "input", "text": "Use the approved document", "file_id": source.id}],
            "checks": [], "acceptance": "", "brief": {}, "change_kind": "semantic",
        })
        assert changed.status_code == 200, changed.text
        contract = (await client.get(base + "/contract")).json()
        criterion = contract["requirements"][0]["id"]
        await board.update(task["id"], status="review")
        revision = (await board.get(task["id"]))["entity_revision"]
        manifests = []
        for index, stored in enumerate((other, source), 1):
            made = await client.post(base + "/artifacts", json={
                "client_operation_id": f"source-artifact-{index}", "expected_entity_revision": revision,
                "artifact_kind": "document", "artifact_key": stored.name, "artifact_revision": 1,
                "digest": stored.sha256, "size_bytes": stored.size, "file_id": stored.id,
            })
            assert made.status_code == 200, made.text
            manifests.append(made.json()["id"])
            revision = made.json()["entity_revision"]
        readiness = (await client.get(base + "/manual-review")).json()
        assert {item["file_id"] for item in readiness["attached_artifacts"]} == {source.id, other.id}
        assert {item["manifest_id"] for item in readiness["attached_artifacts"]} == set(manifests)
        for index, manifest_id in enumerate(manifests, 1):
            report = await client.post(base + "/results", json={
                "client_operation_id": f"source-result-{index}", "expected_entity_revision": revision,
                "contract_revision": contract["contract_revision"], "outcome": "complete",
                "original_text": "I used the supplied document", "manifest_ids": [manifest_id],
            })
            assert report.status_code == 200, report.text
            result_id = report.json()["result_id"]
            listing = (await client.get(base + "/results")).json()
            assert listing[0]["artifacts"][0]["file_id"] == (other.id if index == 1 else source.id)
            revision = report.json()["entity_revision"]
            manual = await client.post(base + f"/results/{result_id}/attest", json={
                "client_operation_id": f"source-manual-{index}", "expected_entity_revision": revision,
                "criterion_id": criterion, "observation": "I read the file",
            })
            assert manual.status_code == 409
            evidence = await client.post(base + f"/results/{result_id}/evidence", json={
                "client_operation_id": f"source-evidence-{index}", "expected_entity_revision": revision,
                "criterion_id": criterion, "manifest_id": manifest_id,
                "observation": "I checked the attached document bytes",
            })
            assert evidence.status_code == 200, evidence.text
            revision = evidence.json()["entity_revision"]
            verdict = await client.post(base + f"/results/{result_id}/verdicts", json={
                "client_operation_id": f"source-verdict-{index}", "expected_entity_revision": revision,
                "verification": "verified", "accepted": True, "head": None, "base": None,
                "evidence_ids": [evidence.json()["evidence_id"]], "reason": "Document reviewed",
            })
            if index == 1:
                assert verdict.status_code == 409
            else:
                assert verdict.status_code == 200, verdict.text
                accepted = await client.post(base + f"/results/{result_id}/accept", json={
                    "client_operation_id": "source-accept", "expected_entity_revision": verdict.json()["entity_revision"],
                    "verdict_id": verdict.json()["verdict_id"], "contract_revision": contract["contract_revision"],
                })
                assert accepted.status_code == 200, accepted.text
    assert (await board.get(task["id"]))["status"] == "done"


async def test_peer_answer_only_counts_what_came_after_the_question(app: Any) -> None:
    from protocore.contracts.types import Message, MessageRole, TextBlock

    peers = Peers(app)
    manager: SessionManager = app.manager
    target = await manager.create_session("reviewer")
    old = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="old reply")])
    await manager.sessions.append_transcript(target.session.id, [old])
    watermark = max(int(m.metadata["daedalus.seq"]) for m in await manager.sessions.list_transcript(target.session.id))
    assert await peers.answer_after(target.session.id, watermark) is None
    await manager.sessions.append_transcript(target.session.id, [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="fresh reply\n\n⟦ h | status: completed; next: none | anchors: x ⟧")])])
    assert await peers.answer_after(target.session.id, watermark) == "fresh reply"
    target.metadata["peer_depth"] = 2
    await peers.on_run_finished(target.session.id, "r", "completed")
    assert "peer_depth" not in target.metadata


async def test_each_agent_sees_its_own_board_and_the_operator_pool(app: Any) -> None:
    """A task belongs to the board of the session that created it (shared with its subagents); another
    agent neither lists, reads nor claims it. A task the operator posts without an addressee is on
    every board. The work-in-progress limit counts what one family holds, not the whole installation."""
    board = Board(app)
    await app.manager.create_session("lead", session_id="lead")
    await app.manager.create_session("[sub] h", session_id="helper", metadata={"subagent_of": "lead"})
    mine = await board.add(title="fix the duplicate message", session_id="lead")
    theirs = await board.add(title="post on the forum", session_id="other")
    pool = await board.add(title="anyone: rotate the logs")

    assert [t["id"] for t in await board.list(None, actor="other")] == [theirs["id"], pool["id"]]
    assert {t["id"] for t in await board.list(None, actor="helper")} == {mine["id"], pool["id"]}  # the subagent works on its leader's board
    assert {t["id"] for t in await board.list(None)} == {mine["id"], theirs["id"], pool["id"]}  # the operator sees everything
    with pytest.raises(KeyError):
        await board.get(mine["id"], actor="other")
    with pytest.raises(KeyError):
        await board.update(mine["id"], status="doing", actor="other")
    assert (await board.get(mine["id"]))["status"] == "todo"  # the refused claim changed nothing

    claimed = await board.update(pool["id"], status="doing", actor="other")
    assert claimed["session_id"] == "other"
    app.config.board.wip_limit = 1
    with pytest.raises(ValueError):
        await board.update(theirs["id"], status="doing", actor="other")  # other's own limit
    assert (await board.update(mine["id"], status="doing", actor="helper"))["session_id"] == "helper"  # lead's family is not rationed by other's claims
    with pytest.raises(ValueError, match="unknown dependency"):
        await board.add(title="after theirs", depends_on=[theirs["id"]], session_id="lead")  # a dependency must be on the same board
