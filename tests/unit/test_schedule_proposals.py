"""An agent suggestion is inert until an exact operator receipt activates it."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig
from daedalus.extensions import api_recurring
from daedalus.extensions.recurring import Recurring
from daedalus.extensions.schedule_proposals import ScheduleProposals
from daedalus.extensions.scheduler import Scheduler
from daedalus.host.session_runner import SessionManager
from daedalus.stores.control import ControlConflict, ControlDenied, Principal
from daedalus.stores.database import Database
from daedalus.stores.files import FileRefused
from daedalus.stores.schedule_file_pin_schema import MIGRATION as FILE_PIN_MIGRATION
from daedalus.stores.schedule_proposal_schema import MIGRATION
from daedalus.tools.files import read_file


def later(minutes: int = 60) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


@pytest.fixture
async def proposals(settings, db: Database):  # type: ignore[no-untyped-def]
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    await manager.create_session("origin", session_id="origin")
    app = SimpleNamespace(settings=settings, db=db, extensions={}, manager=manager)
    service = ScheduleProposals(app)
    app.extensions["schedule_proposals"] = service
    app.extensions["scheduler"] = Scheduler(app)
    yield service
    await manager.close()


async def revision(db: Database) -> int:
    row = await db.fetchone("SELECT revision FROM domain_collection_revisions"
                            " WHERE scope_kind='project' AND scope_id='origin'")
    return int(row["revision"])


async def test_proposal_has_no_executable_schedule_until_atomic_acceptance(proposals: ScheduleProposals,
                                                                             db: Database) -> None:
    due = later()
    source = dict(source_session_id="origin", source_run_id=None, source_command_id="tool:1",
                  name="medicine", prompt="take medicine", cron=None, run_at=due, files=[],
                  model=None, kind="message", run_in="new")
    proposal = await proposals.create(**source)
    assert proposal == await proposals.create(**source)
    assert await db.fetchone("SELECT id FROM schedules") is None
    assert await db.fetchone("SELECT id FROM actor_grants WHERE actor_id LIKE 'schedule:%'") is None
    expiry = later(120)
    accepted = await proposals.accept(
        Principal("operator:1", "operator"), proposal["id"], request_digest=proposal["request_digest"],
        expected_proposal_revision=1, expires_at=expiry,
        expected_collection_revision=await revision(db), client_operation_id="accept:1",
    )
    row = await db.fetchone("SELECT authority_state,grant_id,kind FROM schedules WHERE id = ?", (accepted["id"],))
    assert row["authority_state"] == "current" and row["grant_id"] and row["kind"] == "message"
    assert accepted == await proposals.accept(
        Principal("operator:1", "operator"), proposal["id"], request_digest=proposal["request_digest"],
        expected_proposal_revision=1, expires_at=expiry,
        expected_collection_revision=accepted["entity_revision"] - 1, client_operation_id="accept:1",
    )
    assert len(await db.fetchall("SELECT id FROM schedules")) == 1


async def test_changed_source_command_and_withdrawn_proposal_are_refused(proposals: ScheduleProposals,
                                                                           db: Database) -> None:
    due = later()
    request = dict(source_session_id="origin", source_run_id=None, source_command_id="tool:2",
                   name="review", prompt="read", cron=None, run_at=due, files=[], model=None,
                   kind="agent", run_in="new")
    proposal = await proposals.create(**request)
    with pytest.raises(ControlConflict):
        await proposals.create(**{**request, "prompt": "changed"})
    withdrawn = await proposals.withdraw(
        Principal("operator:1", "operator"), proposal["id"], request_digest=proposal["request_digest"],
        expected_proposal_revision=1, expected_collection_revision=await revision(db),
        client_operation_id="withdraw:1",
    )
    assert withdrawn == await proposals.withdraw(
        Principal("operator:1", "operator"), proposal["id"], request_digest=proposal["request_digest"],
        expected_proposal_revision=1, expected_collection_revision=withdrawn["entity_revision"] - 1,
        client_operation_id="withdraw:1",
    )
    with pytest.raises(ControlConflict):
        await proposals.accept(
            Principal("operator:1", "operator"), proposal["id"], request_digest=proposal["request_digest"],
            expected_proposal_revision=1, expires_at=later(120),
            expected_collection_revision=await revision(db), client_operation_id="accept:withdrawn",
        )
    assert await db.fetchone("SELECT id FROM schedules") is None


async def test_agent_can_replay_withdrawal_only_for_its_own_pending_proposal(proposals: ScheduleProposals,
                                                                             db: Database) -> None:
    proposal = await proposals.create(
        source_session_id="origin", source_run_id=None, source_command_id="tool:create",
        name="review", prompt="read", cron=None, run_at=later(), files=[], model=None,
        kind="agent", run_in="new",
    )
    args = dict(source_session_id="origin", source_run_id=None,
                source_command_id="tool:withdraw", proposal_id=proposal["id"])
    withdrawn = await proposals.withdraw_agent(**args)
    assert withdrawn == await proposals.withdraw_agent(**args)
    assert withdrawn["status"] == "withdrawn" and withdrawn["receipt_id"]
    assert await db.fetchone("SELECT id FROM schedules") is None
    with pytest.raises(ControlConflict):
        await proposals.withdraw_agent(**{**args, "proposal_id": "other"})


async def test_file_change_after_proposal_cannot_gain_schedule_authority(proposals: ScheduleProposals,
                                                                         db: Database, tmp_path: Path) -> None:
    attachment = proposals.app.settings.workspaces_dir / "origin" / "guide.txt"
    attachment.write_text("reviewed text")
    proposal = await proposals.create(
        source_session_id="origin", source_run_id=None, source_command_id="tool:file",
        name="review", prompt="read guide", cron=None, run_at=later(),
        files=[str(attachment)], model=None, kind="agent", run_in="new",
    )
    attachment.write_text("changed text")
    with pytest.raises(ControlConflict, match="attached file changed"):
        await proposals.accept(
            Principal("operator:1", "operator"), proposal["id"], request_digest=proposal["request_digest"],
            expected_proposal_revision=1, expires_at=later(120),
            expected_collection_revision=await revision(db), client_operation_id="accept:changed-file",
        )
    assert await db.fetchone("SELECT id FROM schedules") is None
    assert await db.fetchone("SELECT id FROM actor_grants WHERE actor_id LIKE 'schedule:%'") is None


async def test_approved_file_change_keeps_the_pinned_occurrence_bytes(proposals: ScheduleProposals,
                                                                  db: Database, tmp_path: Path) -> None:
    attachment = proposals.app.settings.workspaces_dir / "origin" / "guide.txt"
    attachment.write_text("approved")
    saved = await proposals.create(
        source_session_id="origin", source_run_id=None, source_command_id="tool:approved-file",
        name="review", prompt="read guide", cron=None, run_at=later(),
        files=[str(attachment)], model=None, kind="agent", run_in="new",
    )
    accepted = await proposals.accept(
        Principal("operator:1", "operator"), saved["id"], request_digest=saved["request_digest"],
        expected_proposal_revision=1, expires_at=later(120),
        expected_collection_revision=await revision(db), client_operation_id="accept:approved-file",
    )
    attachment.write_text("changed after approval")
    row = dict(await db.fetchone("SELECT * FROM schedules WHERE id=?", (accepted["id"],)))
    await proposals.verify_accepted_files(row)
    [handle] = json.loads(row["files"])
    stored = await proposals.app.manager.files.in_scope(handle, row["project_id"])
    assert await proposals.app.manager.files.read(stored) == b"approved"


async def test_legacy_pending_row_is_preserved_as_inert_proposal_until_accepted(proposals: ScheduleProposals,
                                                                                db: Database) -> None:
    due = later()
    await db.execute("DROP TABLE schedule_proposal_receipts")
    await db.execute("DROP TABLE schedule_proposals")
    await db.execute(
        "INSERT INTO schedules(id,name,cron,run_at,prompt,files,model,recurring,enabled,workspace,"
        "next_run_at,created_by_session,created_at,kind,target_session,run_in,schedule_revision,project_id,"
        "authority_state) VALUES ('old-one','Old reminder',NULL,?,'remember','[]',NULL,0,1,'',?,"
        "'origin',?,'message','origin','new',1,'origin','needs_approval')",
        (due, due, later(-5)),
    )
    await db.conn.executescript(MIGRATION)
    [proposal] = await proposals.list()
    assert proposal["legacy_schedule_id"] == "old-one" and proposal["status"] == "pending"
    pending = await db.fetchone("SELECT enabled,grant_id FROM schedules WHERE id='old-one'")
    assert pending["enabled"] == 0 and pending["grant_id"] is None
    accepted = await proposals.accept(
        Principal("operator:1", "operator"), proposal["id"], request_digest=proposal["request_digest"],
        expected_proposal_revision=1, expires_at=later(120),
        expected_collection_revision=await revision(db), client_operation_id="accept:legacy",
    )
    assert accepted["id"] == "old-one"
    assert (await db.fetchone("SELECT enabled,authority_state FROM schedules WHERE id='old-one'"))["authority_state"] == "current"
    assert len(await db.fetchall("SELECT id FROM schedules")) == 1


async def test_legacy_attachments_need_a_new_reviewed_digest_before_activation(proposals: ScheduleProposals,
                                                                                  db: Database,
                                                                                  tmp_path: Path) -> None:
    attachment = proposals.app.settings.workspaces_dir / "origin" / "old-guide.txt"
    attachment.write_text("current reviewed contents")
    due = later()
    await db.execute("DROP TABLE schedule_proposal_receipts")
    await db.execute("DROP TABLE schedule_proposals")
    await db.execute(
        "INSERT INTO schedules(id,name,cron,run_at,prompt,files,model,recurring,enabled,workspace,"
        "next_run_at,created_by_session,created_at,kind,target_session,run_in,schedule_revision,project_id,"
        "authority_state) VALUES ('old-file','Old attachment',NULL,?,'read',?,NULL,0,1,'',?,"
        "'origin',?,'agent','origin','new',1,'origin','needs_approval')",
        (due, json.dumps([str(attachment)]), due, later(-5)),
    )
    await db.conn.executescript(MIGRATION)
    [proposal] = await proposals.list()
    assert proposal["legacy_file_review_required"] and proposal["file_count"] == 1
    with pytest.raises(Exception, match="review the original attachments"):
        await proposals.accept(
            Principal("operator:1", "operator"), proposal["id"], request_digest=proposal["request_digest"],
            expected_proposal_revision=1, expires_at=later(120),
            expected_collection_revision=await revision(db), client_operation_id="accept:unverified",
        )
    unchanged = await db.fetchone("SELECT enabled,grant_id FROM schedules WHERE id='old-file'")
    assert unchanged["enabled"] == 0 and unchanged["grant_id"] is None
    review_args = dict(request_digest=proposal["request_digest"], expected_proposal_revision=1,
                       expected_collection_revision=await revision(db), client_operation_id="review:files")
    reviewed = await proposals.review_legacy_files(Principal("operator:1", "operator"), proposal["id"],
                                                   **review_args)
    assert reviewed == await proposals.review_legacy_files(Principal("operator:1", "operator"), proposal["id"],
                                                            **review_args)
    [fresh] = await proposals.list()
    assert not fresh["legacy_file_review_required"] and fresh["proposal_revision"] == 2
    assert fresh["request_digest"] == reviewed["request_digest"] != proposal["request_digest"]
    assert fresh["files"][0]["name"] == attachment.name and fresh["files"][0]["digest"]
    attachment.write_text("unreviewed replacement")
    with pytest.raises(ControlConflict, match="attached file changed"):
        await proposals.accept(
            Principal("operator:1", "operator"), fresh["id"], request_digest=fresh["request_digest"],
            expected_proposal_revision=2, expires_at=later(120),
            expected_collection_revision=await revision(db), client_operation_id="accept:changed-legacy-file",
        )
    row = await db.fetchone("SELECT enabled,grant_id FROM schedules WHERE id='old-file'")
    assert row["enabled"] == 0 and row["grant_id"] is None


async def test_operator_route_uses_exact_proposal_hash_and_receipt(proposals: ScheduleProposals,
                                                                   db: Database) -> None:
    saved = await proposals.create(
        source_session_id="origin", source_run_id=None, source_command_id="tool:http",
        name="review", prompt="read", cron=None, run_at=later(), files=[], model=None,
        kind="message", run_in="new",
    )
    api = FastAPI()
    api_recurring.register(api, proposals.app, lambda: {"via": "token", "user_id": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        listed = await client.get("/api/recurring/proposals")
        assert listed.status_code == 200
        assert listed.json()["entries"][0]["id"] == saved["id"]
        rev = listed.json()["collection_revisions"]["project:origin"]
        body = {"request_digest": saved["request_digest"], "expected_proposal_revision": 1,
                "expected_collection_revision": rev, "expires_at": later(120),
                "client_operation_id": "accept:http"}
        stale = await client.post(f"/api/recurring/proposals/{saved['id']}/accept",
                                  json={**body, "request_digest": "wrong"})
        assert stale.status_code == 409
        accepted = await client.post(f"/api/recurring/proposals/{saved['id']}/accept", json=body)
        assert accepted.status_code == 200 and accepted.json()["receipt_id"]
        replay = await client.post(f"/api/recurring/proposals/{saved['id']}/accept", json=body)
        assert replay.json() == accepted.json()
        assert len(await db.fetchall("SELECT id FROM schedules")) == 1


async def test_schedule_attachment_source_must_be_a_regular_file_in_project(proposals: ScheduleProposals,
                                                                             tmp_path: Path) -> None:
    root = proposals.app.settings.workspaces_dir / "origin"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    linked = root / "linked.txt"
    linked.symlink_to(outside)
    for index, path in enumerate((outside, linked, root / ".." / "outside.txt")):
        with pytest.raises(ControlDenied):
            await proposals.create(
                source_session_id="origin", source_run_id=None, source_command_id=f"tool:unsafe:{index}",
                name="review", prompt="read", cron=None, run_at=later(), files=[str(path)],
                model=None, kind="agent", run_in="new",
            )


async def test_occurrence_uses_approved_bytes_after_source_replacement(proposals: ScheduleProposals,
                                                                        db: Database, tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    source = proposals.app.settings.workspaces_dir / "origin" / "guide.txt"
    source.write_text("reviewed contents")
    saved = await proposals.create(
        source_session_id="origin", source_run_id=None, source_command_id="tool:pin-delivery",
        name="read guide", prompt="read the approved guide", cron=None, run_at=later(),
        files=[str(source)], model=None, kind="agent", run_in="new",
    )
    accepted = await proposals.accept(
        Principal("operator:1", "operator"), saved["id"], request_digest=saved["request_digest"],
        expected_proposal_revision=1, expires_at=later(120),
        expected_collection_revision=await revision(db), client_operation_id="accept:pin-delivery",
    )
    source.write_text("unreviewed before reserve")
    await proposals.verify_accepted_files(dict(await db.fetchone("SELECT * FROM schedules WHERE id=?", (accepted["id"],))))
    await db.execute("UPDATE schedules SET next_run_at=? WHERE id=?", (later(-1), accepted["id"]))
    proposals.app.extensions["recurring"] = Recurring(proposals.app)
    cycle = await proposals.app.extensions["recurring"].reserve(accepted["id"])
    assert cycle is not None
    effect = await db.fetchone("SELECT payload_json FROM effect_outbox WHERE id=?", (cycle["effect_id"],))
    snapshot = json.loads(effect["payload_json"])["data"]["schedule"]
    replacement = tmp_path / "unreviewed.txt"
    replacement.write_text("unreviewed after reserve")
    source.unlink()
    source.symlink_to(replacement)
    proposals.app.front = None
    proposals.app.config = RuntimeConfig()
    proposals.app.create_session = proposals.app.manager.create_session
    submit = AsyncMock(return_value="host-run")
    monkeypatch.setattr(proposals.app.manager, "submit", submit)
    monkeypatch.setattr(proposals.app.extensions["scheduler"], "_mark_in_flight", AsyncMock())
    await proposals.app.extensions["scheduler"]._fire_agent(snapshot)
    prompt = submit.await_args.args[1]
    [file_handle] = json.loads(snapshot["files"])
    assert file_handle in prompt and str(source) not in prompt
    stored = await proposals.app.manager.files.in_scope(file_handle, snapshot["project_id"])
    assert await proposals.app.manager.files.read(stored) == b"reviewed contents"


async def test_kept_schedule_file_read_is_project_scoped_and_digest_checked(proposals: ScheduleProposals) -> None:
    source = proposals.app.settings.workspaces_dir / "origin" / "facts.txt"
    source.write_text("stable facts")
    saved = await proposals.create(
        source_session_id="origin", source_run_id=None, source_command_id="tool:scoped-file",
        name="facts", prompt="read", cron=None, run_at=later(), files=[str(source)],
        model=None, kind="agent", run_in="new",
    )
    [entry] = (await proposals.list())[0]["files"]
    request = json.loads((await proposals.db.fetchone("SELECT request_json FROM schedule_proposals WHERE id=?",
                                                      (saved["id"],)))["request_json"])
    handle = request["files"][0]["handle"]
    context = ToolContext(tenant_id="daedalus", run_id="r", session_id="origin")
    assert "stable facts" in (await read_file().invoke(context, {"path": handle})).content
    other = await proposals.app.manager.create_session("other", session_id="other")
    foreign = ToolContext(tenant_id="daedalus", run_id="r", session_id=other.session.id)
    assert (await read_file().invoke(foreign, {"path": handle})).is_error
    stored = await proposals.app.manager.files.in_scope(handle, "origin")
    proposals.app.manager.files.path_of(stored).write_bytes(b"replaced blob")
    with pytest.raises(FileRefused):
        await proposals.app.manager.files.read(stored)
    assert (await read_file().invoke(context, {"path": handle})).is_error
    assert entry["digest"] == stored.sha256


async def test_path_approved_schedule_is_paused_and_requires_new_review(proposals: ScheduleProposals,
                                                                         db: Database) -> None:
    source = proposals.app.settings.workspaces_dir / "origin" / "older.txt"
    source.write_text("old approved bytes")
    saved = await proposals.create(
        source_session_id="origin", source_run_id=None, source_command_id="tool:old-pin",
        name="old pin", prompt="read", cron=None, run_at=later(), files=[str(source)],
        model=None, kind="agent", run_in="new",
    )
    accepted = await proposals.accept(
        Principal("operator:1", "operator"), saved["id"], request_digest=saved["request_digest"],
        expected_proposal_revision=1, expires_at=later(120),
        expected_collection_revision=await revision(db), client_operation_id="accept:old-pin",
    )
    await db.execute("UPDATE schedules SET files=? WHERE id=?", (json.dumps([str(source)]), accepted["id"]))
    await db.conn.executescript(FILE_PIN_MIGRATION)
    blocked = await db.fetchone("SELECT enabled,authority_state,grant_id FROM schedules WHERE id=?", (accepted["id"],))
    assert blocked["enabled"] == 0 and blocked["authority_state"] == "needs_approval" and blocked["grant_id"] is None
    [proposal] = [item for item in await proposals.list() if item["id"] == f"repin-{accepted['id']}"]
    assert proposal["legacy_file_review_required"] and proposal["status"] == "pending"
    revised = await proposals.review_legacy_files(
        Principal("operator:1", "operator"), proposal["id"], request_digest=proposal["request_digest"],
        expected_proposal_revision=1, expected_collection_revision=await revision(db),
        client_operation_id="review:old-pin",
    )
    resumed = await proposals.accept(
        Principal("operator:1", "operator"), proposal["id"], request_digest=revised["request_digest"],
        expected_proposal_revision=2, expires_at=later(120),
        expected_collection_revision=await revision(db), client_operation_id="accept:repin",
    )
    assert resumed["id"] == accepted["id"] and resumed["schedule_revision"] > 1
    [handle] = json.loads((await db.fetchone("SELECT files FROM schedules WHERE id=?", (accepted["id"],)))["files"])
    assert handle.startswith("att:")
    await proposals.verify_accepted_files(dict(await db.fetchone("SELECT * FROM schedules WHERE id=?", (accepted["id"],))))


async def test_repin_migration_cancels_only_pending_old_file_delivery(proposals: ScheduleProposals,
                                                                       db: Database) -> None:
    source = proposals.app.settings.workspaces_dir / "origin" / "queued.txt"
    source.write_text("reviewed")
    saved = await proposals.create(
        source_session_id="origin", source_run_id=None, source_command_id="tool:queued-pin",
        name="queued", prompt="read", cron=None, run_at=later(), files=[str(source)],
        model=None, kind="agent", run_in="new",
    )
    accepted = await proposals.accept(
        Principal("operator:1", "operator"), saved["id"], request_digest=saved["request_digest"],
        expected_proposal_revision=1, expires_at=later(120),
        expected_collection_revision=await revision(db), client_operation_id="accept:queued-pin",
    )
    await db.execute("UPDATE schedules SET files=?,next_run_at=? WHERE id=?",
                     (json.dumps([str(source)]), later(-1), accepted["id"]))
    proposals.app.extensions["recurring"] = Recurring(proposals.app)
    cycle = await proposals.app.extensions["recurring"].reserve(accepted["id"])
    assert cycle is not None
    await db.conn.executescript(FILE_PIN_MIGRATION)
    effect = await db.fetchone("SELECT state FROM effect_outbox WHERE id=?", (cycle["effect_id"],))
    historical = await db.fetchone("SELECT action_state FROM recurring_cycles WHERE id=?", (cycle["id"],))
    assert effect["state"] == "cancelled" and historical["action_state"] == "needs_approval"
    assert (await db.fetchone("SELECT count(*) AS n FROM grant_events WHERE reason='attachment bytes require renewed review'"))["n"] == 1
