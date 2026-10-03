"""Issue import, echo and remote conflict guards use an offline GitHub transport."""

from __future__ import annotations

import json

import aiosqlite
import httpx
import pytest

from daedalus.extensions.issue_sync import GitHubIssues, IssueSync
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore


@pytest.fixture
async def sync_store(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Research','2026-01-01')")
    await db.execute("INSERT INTO planning_budgets(project_id,max_depth,max_tasks,max_tokens)"
                     " VALUES ('project',8,50,100000)")
    remote = {"title": "Investigate failure", "body": "Confirm the cause", "state": "open",
              "updated_at": "2026-01-01T00:00:00Z", "number": 7}
    writes = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/repo/issues/7"
        if request.method == "PATCH":
            writes.append(json.loads(request.content))
            remote.update(writes[-1])
            remote["updated_at"] = "2026-01-02T00:00:00Z"
            if remote.pop("simulate_lost_response", False):
                return httpx.Response(503)
        return httpx.Response(200, json=dict(remote))

    sync = IssueSync(db, GitHubIssues("test-token", transport=httpx.MockTransport(handler)))
    try:
        yield db, sync, remote, writes
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_import_is_one_task_contract_and_replayed_receipt(sync_store) -> None:
    db, sync, _, _ = sync_store
    operator = Principal("operator:1", "operator")
    preview = await sync.preview("project", "owner/repo", 7)
    revision = await ControlStore(db).revision(Scope("project", "project"), Entity("collection", "project"))
    created = await sync.apply_import(operator, "project", "owner/repo", 7,
                                      preview_digest=preview["preview_digest"],
                                      expected_collection_revision=revision,
                                      client_operation_id="import-7")
    again = await sync.apply_import(operator, "project", "owner/repo", 7,
                                    preview_digest=preview["preview_digest"],
                                    expected_collection_revision=revision,
                                    client_operation_id="import-7")
    assert again == created
    contract = await db.fetchone("SELECT contract_revision FROM task_contract_versions WHERE task_id = ?", (created["task_id"],))
    assert contract["contract_revision"] == 1
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM issue_links"))["n"] == 1


@pytest.mark.asyncio
async def test_remote_change_refuses_push_and_delayed_echo_does_not_overwrite(sync_store) -> None:
    db, sync, remote, writes = sync_store
    operator = Principal("operator:1", "operator")
    preview = await sync.preview("project", "owner/repo", 7)
    await sync.apply_import(operator, "project", "owner/repo", 7,
                            preview_digest=preview["preview_digest"],
                            expected_collection_revision=1, client_operation_id="import-7")
    current = await sync.preview("project", "owner/repo", 7)
    remote["title"] = "Changed elsewhere"
    remote["updated_at"] = "2026-01-03T00:00:00Z"
    with pytest.raises(ControlConflict):
        await sync.queue_push(operator, "project", "owner/repo", 7,
                              preview_digest=current["preview_digest"],
                              expected_entity_revision=1, client_operation_id="push-7")
    assert writes == []
    old = {"repository": {"full_name": "owner/repo"}, "issue": {
        "number": 7, "title": "Investigate failure", "body": "Confirm the cause", "state": "open",
        "updated_at": "2026-01-01T00:00:00Z"}}
    assert await sync.record_webhook("issues", old) == "unchanged"
    link = await db.fetchone("SELECT state FROM issue_links WHERE remote_id = 'owner/repo#7'")
    assert link["state"] == "linked"


@pytest.mark.asyncio
async def test_push_has_one_receipt_and_matching_origin_readback(sync_store) -> None:
    db, sync, remote, writes = sync_store
    operator = Principal("operator:1", "operator")
    first = await sync.preview("project", "owner/repo", 7)
    created = await sync.apply_import(operator, "project", "owner/repo", 7,
                                      preview_digest=first["preview_digest"],
                                      expected_collection_revision=1, client_operation_id="import-7")
    await db.execute("UPDATE board_tasks SET title = 'Local title',entity_revision = 2 WHERE id = ?",
                     (created["task_id"],))
    preview = await sync.preview("project", "owner/repo", 7)
    queued = await sync.queue_push(operator, "project", "owner/repo", 7,
                                   preview_digest=preview["preview_digest"],
                                   expected_entity_revision=2, client_operation_id="push-7")
    again = await sync.queue_push(operator, "project", "owner/repo", 7,
                                  preview_digest=preview["preview_digest"],
                                  expected_entity_revision=2, client_operation_id="push-7")
    assert again == queued
    outbox = OutboxStore(db)
    claim = await outbox.claim(("issue.github.push",))
    assert claim is not None and claim.id == queued["effect_id"]
    outcome = await sync.run(claim, outbox.check)
    assert outcome.state == "completed"
    assert len(writes) == 1 and remote["title"] == "Local title"
    link = await db.fetchone("SELECT remote_version,origin_token,local_revision,state FROM issue_links WHERE task_id = ?",
                             (created["task_id"],))
    assert link["origin_token"] and link["local_revision"] == 3 and link["state"] == "linked"
    payload = {"repository": {"full_name": "owner/repo"}, "issue": dict(remote)}
    assert await sync.record_webhook("issues", payload) == "echo"


@pytest.mark.asyncio
async def test_lost_patch_response_reconciles_without_second_write(sync_store) -> None:
    db, sync, remote, writes = sync_store
    operator = Principal("operator:1", "operator")
    first = await sync.preview("project", "owner/repo", 7)
    created = await sync.apply_import(operator, "project", "owner/repo", 7,
                                      preview_digest=first["preview_digest"],
                                      expected_collection_revision=1, client_operation_id="import-7")
    await db.execute("UPDATE board_tasks SET title = 'Changed locally',entity_revision = 2 WHERE id = ?",
                     (created["task_id"],))
    preview = await sync.preview("project", "owner/repo", 7)
    await sync.queue_push(operator, "project", "owner/repo", 7,
                          preview_digest=preview["preview_digest"],
                          expected_entity_revision=2, client_operation_id="push-7")
    remote["simulate_lost_response"] = True
    outbox = OutboxStore(db)
    claim = await outbox.claim(("issue.github.push",))
    assert (await sync.run(claim, outbox.check)).state == "unknown"
    resolution = await sync.reconcile(claim)
    assert resolution is not None and resolution.state == "completed"
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_field_choices_preserve_local_body_and_never_close_task(sync_store) -> None:
    db, sync, remote, _ = sync_store
    operator = Principal("operator:1", "operator")
    first = await sync.preview("project", "owner/repo", 7)
    created = await sync.apply_import(operator, "project", "owner/repo", 7,
                                      preview_digest=first["preview_digest"],
                                      expected_collection_revision=1, client_operation_id="import-7")
    remote.update(title="Remote title", body="Remote body", state="closed",
                  updated_at="2026-01-03T00:00:00Z")
    preview = await sync.preview("project", "owner/repo", 7)
    resolved = await sync.resolve_remote(operator, "project", "owner/repo", 7,
                                         preview_digest=preview["preview_digest"],
                                         expected_entity_revision=1, client_operation_id="resolve-7",
                                         fields={"title": "remote", "body": "local"})
    task = await db.fetchone("SELECT title,acceptance,status,contract_revision FROM board_tasks WHERE id = ?",
                             (created["task_id"],))
    assert resolved["status_mapping"] == "closed_remote_unmapped"
    assert (task["title"], task["acceptance"], task["status"], task["contract_revision"]) == (
        "Remote title", "Confirm the cause", "todo", 1)


async def test_observations_remain_immutable_and_malformed_signed_facts_are_ignored(sync_store) -> None:
    db, sync, _, _ = sync_store
    preview = await sync.preview("project", "owner/repo", 7)
    await sync.apply_import(Principal("operator:1", "operator"), "project", "owner/repo", 7,
                            preview_digest=preview["preview_digest"], expected_collection_revision=1,
                            client_operation_id="import-immutable")
    original = dict(await db.fetchone("SELECT * FROM issue_sync_observations"))
    with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
        await db.execute("UPDATE issue_sync_observations SET remote_json = '{}' WHERE id = ?", (original["id"],))
    with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
        await db.execute("DELETE FROM issue_sync_observations WHERE id = ?", (original["id"],))
    assert dict(await db.fetchone("SELECT * FROM issue_sync_observations")) == original
    assert await sync.record_webhook("issues", {"repository": ["bad"], "issue": {"number": 7}}) == "ignored"
