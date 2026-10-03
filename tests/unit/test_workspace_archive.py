"""Private workspace archives preserve authored history without restoring live authority."""

from __future__ import annotations

import hashlib
import io
import sqlite3
import zipfile
from copy import deepcopy

import pytest
from pydantic import ValidationError

from daedalus.extensions.api_workspace_archive import ImportInput
from daedalus.extensions.orchestrator_domain import OriginalReports
from daedalus.extensions.workspace_archive import ArchiveRefused, WorkspaceArchive, _archive_bytes, check_archive
from daedalus.host.engine_factory import TENANT
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.files import FileStore


def test_import_revision_requires_an_integer() -> None:
    with pytest.raises(ValidationError):
        ImportInput(archive_artifact_id="a" * 64, expected_collection_revision="2", client_operation_id="restore")


@pytest.fixture
async def archive_store(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        files = FileStore(db, FileBlobStore(tmp_path / "blobs"))
        run_detail = await files.blobs.put("main", b"saved run detail")
        picture = await files.blobs.put("main", b"saved image")
        await db.execute(
            "INSERT INTO projects(id,name,created_at,settings) VALUES (?,?,?,?)",
            ("source", "Research", "2026-01-01", '{"private_token":"must-never-export"}'),
        )
        await db.execute(
            "INSERT INTO project_folders(id,project_id,path,label,env,created_at) VALUES (?,?,?,?,?,?)",
            ("folder", "source", str(tmp_path / "secret-location"), "Sources", "host", "2026-01-01"),
        )
        await db.execute(
            "INSERT INTO sessions(id,tenant_id,title,created_at,last_message_at,metadata,project_id) VALUES (?,?,?,?,?,?,?)",
            ("session", "main", "Discussion", "2026-01-01", "2026-01-01", '{"share_secret":"must-never-export"}', "source"),
        )
        await db.execute(
            "INSERT INTO session_messages(session_id,tenant_id,message,gen,key) VALUES (?,?,?,?,?)",
            ("session", "main", '{"role":"user","text":"Keep this conversation","content_blocks":[{"kind":"image_ref","blob_ref":"' + picture.ref + '"}]}', 1, "first"),
        )
        await db.execute(
            "INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at,detail_blob_ref) VALUES (?,?,?,?,?,?,?)",
            ("run", "main", "session", "running", "2026-01-01", "2026-01-01", run_detail.ref),
        )
        await db.execute(
            "INSERT INTO events(id,run_id,tenant_id,name,payload,created_at) VALUES (?,?,?,?,?,?)",
            ("event", "run", "main", "turn", '{"note":"historical event"}', "2026-01-01"),
        )
        await db.execute(
            "INSERT INTO staff(id,project_id,name,harness,created_by,created_at) VALUES (?,?,?,?,?,?)",
            ("member", "source", "Writer", "claude", "operator", "2026-01-01"),
        )
        await db.execute(
            "INSERT INTO staff_role_versions(staff_id,role_revision,purpose,authority_json,output_contract_json,created_at) VALUES (?,?,?,?,?,?)",
            ("member", 1, "Write", '{"operations":["board.read"]}', "{}", "2026-01-01"),
        )
        await db.execute(
            "INSERT INTO board_tasks(id,title,status,created_at,updated_at,project_id) VALUES (?,?,?,?,?,?)",
            ("task", "Build the report", "doing", "2026-01-01", "2026-01-01", "source"),
        )
        await db.execute(
            "INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,snapshot_json,created_at) VALUES (?,?,?,?,?)",
            ("task", 1, "operator", '{"requirements":[],"depends_on":[]}', "2026-01-01"),
        )
        await db.execute(
            "INSERT INTO staff_sessions(id,staff_id,kind,task_id,status,status_at,started_at,team_token_hash) VALUES (?,?,?,?,?,?,?,?)",
            ("member-session", "member", "cli", "task", "working", "2026-01-01", "2026-01-01", "must-never-export"),
        )
        await db.execute(
            "INSERT INTO staff_messages(id,staff_id,staff_session_id,origin,text,mode,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("member-message", "member", "member-session", "operator", "Please write the report", "after_turn", "queued", "2026-01-01", "2026-01-01"),
        )
        await db.execute(
            "INSERT INTO task_requirements(id,task_id,project_id,number,text,kind,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("requirement", "task", "source", 1, "Keep evidence", "quality", "operator", "2026-01-01", "2026-01-01"),
        )
        await db.execute(
            "INSERT INTO requirement_deliveries(requirement_id,staff_session_id,staff_id,via,message_id,path,delivered_at) VALUES (?,?,?,?,?,?,?)",
            ("requirement", "member-session", "member", "message", "member-message", "local-path-not-portable", "2026-01-01"),
        )
        await db.execute(
            "INSERT INTO dispatches(id,project_id,seq,text,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            ("dispatch", "source", 1, "Dispatch history", "open", "2026-01-01", "2026-01-01"),
        )
        await db.execute(
            "INSERT INTO dispatch_messages(dispatch_id,at,author,kind,text) VALUES (?,?,?,?,?)",
            ("dispatch", "2026-01-01", "operator", "note", "A reply"),
        )
        await db.execute(
            "INSERT INTO asks(id,short_id,project_id,origin,kind,staff_id,staff_session_id,task_id,text,routed_to,created_at,routed_at,dispatch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ask", "A1", "source", "staff", "question", "member", "member-session", "task", "What next?", "operator", "2026-01-01", "2026-01-01", "dispatch"),
        )
        stored = await files.add(b"attachment bytes", name="notes.txt", mime="text/plain", origin="operator", scope="source", actor="operator")
        await db.execute(
            "INSERT INTO task_files(task_id,file_id,added_at) VALUES (?,?,?)",
            ("task", stored.id, "2026-01-01"),
        )
        await db.execute(
            "INSERT INTO artifact_manifests(id,project_id,task_id,artifact_kind,artifact_key,artifact_revision,digest,size_bytes,file_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("manifest", "source", "task", "document", "notes", 1, stored.sha256, stored.size, stored.id, "2026-01-01"),
        )
        original = "A verified historical report"
        await db.execute(
            "INSERT INTO result_receipts(id,task_id,contract_revision,outcome,original_text,original_digest,original_size_bytes,artifact_manifest_id,actor_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("result", "task", 1, "complete", original, hashlib.sha256(original.encode()).hexdigest(), len(original), "manifest", "operator", "2026-01-01"),
        )
        large_original = ("A report with durable source bytes. " * 2500).encode()
        _, report_ref, report_digest, report_size = OriginalReports(db.path.parent / "result-originals").stage(large_original)
        await db.execute(
            "INSERT INTO result_receipts(id,task_id,contract_revision,outcome,original_blob_ref,original_digest,original_size_bytes,actor_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("large-result", "task", 1, "partial", report_ref, report_digest, report_size, "operator", "2026-01-01"),
        )
        turn = await db.fetchone("SELECT seq FROM session_messages WHERE session_id = 'session'")
        await db.execute(
            "INSERT INTO result_turn_anchors(result_id,session_id,turn_seq,source_digest) VALUES (?,?,?,?)",
            ("result", "session", turn["seq"], hashlib.sha256(b"source turn").hexdigest()),
        )
        await db.execute(
            "INSERT INTO watches(id,project_id,pattern_json,action_json,created_by,created_at) VALUES (?,?,?,?,?,?)",
            ("watch", "source", '{"type":"ci"}', '{"type":"notify"}', "operator", "2026-01-01"),
        )
        await db.execute(
            "INSERT INTO watch_deliveries(id,watch_id,condition_revision,source_cursor,dedup_key,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("delivery", "watch", 1, "event:1", "old-dedup", "pending", "2026-01-01", "2026-01-01"),
        )
        await db.execute(
            "INSERT INTO app_events(at,type,project_id,payload_json) VALUES (?,?,?,?)",
            ("2026-01-01", "task.moved", "source", '{"task_id":"task"}'),
        )
        await db.execute(
            "INSERT INTO replan_fingerprints(project_id,contract_revision,digest,count) VALUES (?,?,?,?)",
            ("source", 1, hashlib.sha256(b"plan").hexdigest(), 1),
        )
        await db.execute(
            "INSERT INTO planning_budgets(project_id,max_depth,max_tasks,max_tokens) VALUES (?,?,?,?)",
            ("source", 2, 10, 2000),
        )
        yield db, files, WorkspaceArchive(db, files)
    finally:
        await db.close()


async def test_private_archive_roundtrips_transcript_contract_report_and_blob(archive_store) -> None:
    db, files, service = archive_store
    data = await service.export("source")
    assert b"must-never-export" not in data
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        assert b"secret-location" not in zipped.read("workspace.json")
    checked = check_archive(data)
    assert checked.counts["session_messages"] == 1
    assert checked.counts["task_contract_versions"] == 1
    assert checked.counts["files"] == 1
    assert checked.counts["events"] == 1
    assert checked.counts["dispatch_messages"] == 1
    assert checked.counts["requirement_deliveries"] == 1
    assert checked.counts["app_events"] == 1
    assert len(checked.report_blobs) == 1
    assert len(checked.session_blobs) == 1
    assert checked.folder_handles[0]["label"] == "Sources"
    principal = Principal.operator({"via": "token", "user_id": 1})
    revision = await ControlStore(db).revision(Scope("global", "global"), Entity("collection", "global"))
    result = await service.import_command(principal, checked, expected_collection_revision=revision, client_operation_id="restore-one")
    restored = result["project_id"]
    assert restored != "source"
    assert result["runtime_state"] == "inactive"
    assert result["archived_only"]["app_events"] == 1
    assert await db.fetchall("PRAGMA foreign_key_check") == []
    task = await db.fetchone("SELECT id,status,current_attempt_id,accepted_result_id FROM board_tasks WHERE project_id = ?", (restored,))
    assert task["status"] == "blocked" and task["current_attempt_id"] is None
    message = await db.fetchone("SELECT m.message,m.tenant_id FROM session_messages m JOIN sessions s ON s.id = m.session_id WHERE s.project_id = ?", (restored,))
    assert "Keep this conversation" in message["message"]
    assert message["tenant_id"] == TENANT
    assert await files.blobs.get(TENANT, next(iter(checked.session_blobs))) == b"saved image"
    file = await db.fetchone("SELECT f.* FROM files f JOIN file_access a ON a.file_id = f.id WHERE a.scope = ?", (restored,))
    assert len(file["id"]) == 12
    assert await files.blobs.get("files", file["sha256"]) == b"attachment bytes"
    assert await db.fetchone("SELECT r.id FROM result_receipts r JOIN board_tasks t ON t.id = r.task_id WHERE t.project_id = ?", (restored,))
    large = await db.fetchone("SELECT r.original_blob_ref,r.original_digest FROM result_receipts r JOIN board_tasks t ON t.id = r.task_id WHERE t.project_id = ? AND r.original_blob_ref IS NOT NULL", (restored,))
    assert (db.path.parent / "result-originals" / large["original_blob_ref"]).read_bytes() == checked.report_blobs[large["original_digest"]]
    run = await db.fetchone("SELECT r.status,r.detail_blob_ref FROM runs r JOIN sessions s ON s.id = r.session_id WHERE s.project_id = ?", (restored,))
    assert run["status"] == "incomplete"
    assert await files.blobs.get(TENANT, run["detail_blob_ref"]) == b"saved run detail"
    member_session = await db.fetchone("SELECT ss.status,ss.team_token_hash,ss.ended_at FROM staff_sessions ss JOIN staff s ON s.id = ss.staff_id WHERE s.project_id = ?", (restored,))
    assert member_session["status"] == "exited" and not member_session["team_token_hash"] and member_session["ended_at"]
    assert await db.fetchone("SELECT 1 FROM asks WHERE project_id = ? AND resolved_at IS NOT NULL", (restored,))
    anchor = await db.fetchone("SELECT a.* FROM result_turn_anchors a JOIN result_receipts r ON r.id = a.result_id JOIN board_tasks t ON t.id = r.task_id WHERE t.project_id = ?", (restored,))
    assert await db.fetchone("SELECT 1 FROM session_messages WHERE session_id = ? AND seq = ?", (anchor["session_id"], anchor["turn_seq"]))
    delivery = await db.fetchone("SELECT d.path,d.message_id FROM requirement_deliveries d JOIN task_requirements r ON r.id = d.requirement_id WHERE r.project_id = ?", (restored,))
    assert delivery["path"] == "" and delivery["message_id"] != "member-message"
    watch = await db.fetchone("SELECT enabled FROM watches WHERE project_id = ?", (restored,))
    assert watch["enabled"] == 0
    watch_delivery = await db.fetchone("SELECT status FROM watch_deliveries WHERE watch_id = (SELECT id FROM watches WHERE project_id = ?)", (restored,))
    assert watch_delivery["status"] == "delivered"
    assert await db.fetchone("SELECT 1 FROM planning_budgets WHERE project_id = ?", (restored,))
    assert await db.fetchone("SELECT 1 FROM file_transfers WHERE scope = ?", (restored,))
    assert await db.fetchone("SELECT 1 FROM app_events WHERE project_id = ?", (restored,)) is None
    assert (await service.preview(checked))["id_map"]["board_tasks"]["task"] == task["id"]
    assert await service.import_command(principal, checked, expected_collection_revision=revision, client_operation_id="restore-one") == result
    with pytest.raises(ControlConflict):
        await service.import_command(principal, checked, expected_collection_revision=revision, client_operation_id="restore-two")
    assert (await service.preview(checked))["collision"] is True


async def test_corrupt_archive_and_failed_restore_leave_no_project(archive_store) -> None:
    db, _, service = archive_store
    original = await service.export("source")
    with pytest.raises(ArchiveRefused):
        check_archive(original[:-15] + b"broken")
    checked = check_archive(original)
    damaged_blobs = dict(checked.blobs)
    damaged_blobs[next(iter(damaged_blobs))] = b"wrong bytes"
    with pytest.raises(ArchiveRefused):
        check_archive(_archive_bytes(checked.rows, damaged_blobs, checked.report_blobs,
                                     checked.config_handles, checked.run_blobs, checked.folder_handles,
                                     checked.session_blobs))
    rows = deepcopy(checked.rows)
    rows["board_tasks"][0]["status"] = "todo"
    rows["board_tasks"][0]["depends_on"] = '["outside-project"]'
    bad = check_archive(_archive_bytes(rows, checked.blobs, checked.report_blobs, checked.config_handles, checked.run_blobs, checked.folder_handles, checked.session_blobs))
    with pytest.raises(ArchiveRefused):
        await service.preview(bad)
    assert await db.fetchone("SELECT 1 FROM workspace_archive_imports") is None
    rows = deepcopy(checked.rows)
    rows["result_receipts"][0]["outcome"] = "invalid"
    invalid = check_archive(_archive_bytes(rows, checked.blobs, checked.report_blobs, checked.config_handles, checked.run_blobs, checked.folder_handles, checked.session_blobs))
    revision = await ControlStore(db).revision(Scope("global", "global"), Entity("collection", "global"))
    with pytest.raises(sqlite3.IntegrityError):
        await service.import_command(Principal.operator({"via": "token", "user_id": 1}), invalid,
                                     expected_collection_revision=revision, client_operation_id="restore-invalid")
    assert await db.fetchone("SELECT 1 FROM workspace_archive_imports") is None
    assert len(await db.fetchall("SELECT id FROM projects")) == 1
    assert await db.fetchall("PRAGMA foreign_key_check") == []


async def test_failed_import_removes_newly_staged_blobs(archive_store, tmp_path) -> None:
    _, _, source = archive_store
    archive = check_archive(await source.export("source"))
    rows = deepcopy(archive.rows)
    rows["result_receipts"][0]["outcome"] = "invalid"
    invalid = check_archive(_archive_bytes(rows, archive.blobs, archive.report_blobs,
                                           archive.config_handles, archive.run_blobs,
                                           archive.folder_handles, archive.session_blobs))
    destination = Database(tmp_path / "destination" / "state.sqlite")
    await destination.open()
    try:
        files = FileStore(destination, FileBlobStore(tmp_path / "destination" / "blobs"))
        service = WorkspaceArchive(destination, files)
        with pytest.raises(sqlite3.IntegrityError):
            await service.import_command(Principal.operator({"via": "token", "user_id": 1}), invalid,
                                         expected_collection_revision=1, client_operation_id="restore-bad")
        assert await destination.fetchone("SELECT 1 FROM projects") is None
        assert await destination.fetchall("PRAGMA foreign_key_check") == []
        for digest in invalid.blobs:
            assert not files.blobs.path_of("files", digest).exists()
        for digest in invalid.session_blobs:
            assert not files.blobs.path_of(TENANT, digest).exists()
    finally:
        await destination.close()
