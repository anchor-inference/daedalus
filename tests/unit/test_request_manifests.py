from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from protocore.conformance.request_manifest import RequestManifestSinkConformance
from protocore.contracts.llm import LLMObservabilityContext, LLMRequest
from protocore.contracts.observability import build_request_manifest
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolDefinition, ToolParameterSchema

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.request_manifests import KIND, RequestManifestStore
from daedalus.host.session_runner import SessionManager
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.database import Database


async def test_request_manifest_is_durable_and_large_values_are_by_reference(tmp_path) -> None:
    db = Database(tmp_path / "state.db")
    await db.open()
    try:
        await db.execute(
            "INSERT INTO projects(id, name, created_at, settings, system) VALUES ('project', 'p', 'now', '{}', '')"
        )
        await db.execute(
            "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES ('session', 'tenant', '', 'now', 'now', '{}', 'project')"
        )
        blobs = FileBlobStore(tmp_path / "blobs")
        store = RequestManifestStore(db, blobs, tenant_id="tenant")
        request = LLMRequest(
            model="model",
            messages=[Message(role=MessageRole.system, content_blocks=[TextBlock(text="rules")]), Message(role=MessageRole.user, content_blocks=[TextBlock(text="x" * 500)])],
            tools=[ToolDefinition(name="Read", description="read", parameters=ToolParameterSchema())],
            observability=LLMObservabilityContext(run_id="run", session_id="session", call_category="stream", call_purpose="test"),
        )
        manifest, bodies = build_request_manifest(
            request=request,
            attempt_scope="run/1",
            constants_sha256="a" * 64,
            inline_value_max_bytes=64,
        )

        await store.record_request_manifest(manifest=manifest, manifest_id=manifest.manifest_id, bodies=bodies)
        await store.record_request_manifest(manifest=manifest, manifest_id=manifest.manifest_id, bodies=bodies)
        await store.settle()

        rows = await db.fetchall("SELECT manifest FROM request_manifests")
        assert len(rows) == 1
        saved = json.loads(rows[0]["manifest"])
        assert saved["messages"]["inline"] is None
        assert await blobs.exists("tenant", saved["messages"]["blob_ref"])
        named = {row["ref"] for row in await db.fetchall("SELECT ref FROM request_manifest_blobs")}
        assert saved["messages"]["blob_ref"] in named and saved["tools"]["blob_ref"] in named
        assert (await store.latest_for_session("session"))["request_sha256"] == manifest.request_sha256
    finally:
        await db.close()


async def test_request_manifest_cascades_with_session(tmp_path) -> None:
    db = Database(tmp_path / "state.db")
    await db.open()
    try:
        await db.execute(
            "INSERT INTO projects(id, name, created_at, settings, system) VALUES ('p', 'p', 'now', '{}', '')"
        )
        await db.execute(
            "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES ('s', 't', '', 'now', 'now', '{}', 'p')"
        )
        await db.execute(
            "INSERT INTO request_manifests(manifest_id, session_id, attempt_id, request_sha256, model, manifest, created_at) VALUES ('m', 's', 'a', 'd', 'model', '{}', 'now')"
        )
        await db.execute("DELETE FROM sessions WHERE id = 's'")
        assert await db.fetchone("SELECT 1 FROM request_manifests") is None
        plan = await db.fetchone("EXPLAIN QUERY PLAN SELECT * FROM request_manifests WHERE session_id = 's' ORDER BY seq DESC LIMIT 1")
        assert plan is not None and "request_manifests_by_session" in str(plan["detail"])
    finally:
        await db.close()


class TestTheStoreKeepsTheCoresSinkContract(RequestManifestSinkConformance):
    """The core's own conformance suite, run against this store.

    The store once took the manifest and bodies positionally while the core passed them by keyword
    together with `manifest_id`, so every call raised inside the core and no manifest was kept.
    """

    @pytest.fixture
    async def subject(self, tmp_path) -> AsyncIterator[RequestManifestStore]:
        db = Database(tmp_path / "state.db")
        await db.open()
        try:
            yield RequestManifestStore(db, FileBlobStore(tmp_path / "blobs"), tenant_id="tenant")
        finally:
            await db.close()


# Retention: a session keeps its newest two manifests, and a blob goes only when nothing names it.


async def _session(db: Database, session_id: str) -> None:
    await db.execute("INSERT OR IGNORE INTO projects(id, name, created_at, settings, system) VALUES ('p', 'p', 'now', '{}', '')")
    await db.execute(
        "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES (?, 't', '', 'now', 'now', '{}', 'p')",
        (session_id,),
    )


def _request(session_id: str | None, text: str, *, tools: str = "read") -> tuple[Any, dict[str, bytes]]:
    request = LLMRequest(
        model="model",
        messages=[Message(role=MessageRole.system, content_blocks=[TextBlock(text="rules " * 20)]), Message(role=MessageRole.user, content_blocks=[TextBlock(text=text * 50)])],
        tools=[ToolDefinition(name="Read", description=tools * 30, parameters=ToolParameterSchema())],
        observability=LLMObservabilityContext(run_id="run", session_id=session_id, call_category="stream", call_purpose="test"),
    )
    return build_request_manifest(request=request, attempt_scope=f"run/{text}", constants_sha256="a" * 64, inline_value_max_bytes=64)


async def _record(store: RequestManifestStore, session_id: str | None, text: str, **kwargs: Any) -> dict[str, Any]:
    manifest, bodies = _request(session_id, text, **kwargs)
    await store.record_request_manifest(manifest=manifest, manifest_id=manifest.manifest_id, bodies=bodies)
    return json.loads(manifest.model_dump_json())


def _ref(body: dict[str, Any]) -> str:
    return str(body["sha256"])


@pytest.fixture
async def store(tmp_path) -> AsyncIterator[RequestManifestStore]:
    db = Database(tmp_path / "state.db")
    await db.open()
    try:
        made = RequestManifestStore(db, FileBlobStore(tmp_path / "blobs"), tenant_id="tenant")
        yield made
        await made.close()
    finally:
        await db.close()


async def _count(store: RequestManifestStore, session_id: str | None) -> int:
    row = await store.db.fetchone("SELECT count(*) AS n FROM request_manifests WHERE session_id IS ?", (session_id,))
    assert row is not None
    return int(row["n"])


async def test_a_session_keeps_exactly_its_newest_two_manifests(store: RequestManifestStore) -> None:
    await _session(store.db, "a")
    await _session(store.db, "b")
    written = [await _record(store, "a", f"turn-{index}") for index in range(5)]
    await _record(store, "b", "other")
    await store.settle()

    assert await _count(store, "a") == 2 and await _count(store, "b") == 1
    kept = await store.recent_for_session("a")
    assert [m["request_sha256"] for m in kept] == [written[4]["request_sha256"], written[3]["request_sha256"]]
    # The bodies only the pruned manifests named are gone; the newest two still have theirs.
    for old in written[:3]:
        assert not await store.blobs.exists("tenant", _ref(old["messages"]))
    for new in written[3:]:
        assert await store.blobs.exists("tenant", _ref(new["messages"]))
    refs = await store.db.fetchone("SELECT count(DISTINCT manifest_seq) AS n FROM request_manifest_blobs")
    assert refs is not None and refs["n"] == 3


async def test_manifests_without_a_session_keep_their_newest_two_as_one_group(store: RequestManifestStore) -> None:
    for index in range(4):
        await _record(store, None, f"loose-{index}")
    await store.settle()
    assert await _count(store, None) == 2


async def test_a_shared_blob_survives_while_any_manifest_still_names_it(store: RequestManifestStore) -> None:
    await _session(store.db, "a")
    await _session(store.db, "b")
    first = await _record(store, "a", "one", tools="shared")
    await _record(store, "b", "elsewhere", tools="shared")
    for index in range(3):
        await _record(store, "a", f"later-{index}", tools=f"changed-{index}")
    await store.settle()

    tools = _ref(first["tools"])
    assert await store.blobs.exists("tenant", tools), "pruning session a freed a body session b still names"
    await store.forget_session("b")
    assert not await store.blobs.exists("tenant", tools)


async def test_a_blob_of_another_kind_is_never_deleted(store: RequestManifestStore, tmp_path) -> None:
    await _session(store.db, "a")
    foreign = await store.blobs.put("tenant", b"an attachment", content_type="image/png")
    tagged = await store.blobs.put("tenant", b"a page", metadata={"kind": "page"})
    # The same bytes a manifest is about to store, written first by somebody else: the manifest
    # names the blob but does not claim it.
    manifest, bodies = _request("a", "collide")
    claimed = await store.blobs.put("tenant", bodies["messages"], metadata={"kind": "attachment"})
    await store.record_request_manifest(manifest=manifest, manifest_id=manifest.manifest_id, bodies=bodies)
    for index in range(3):
        await _record(store, "a", f"after-{index}")
    await store.settle()
    await store.sweep()
    await store.forget_session("a")

    for meta in (foreign, tagged, claimed):
        assert await store.blobs.exists("tenant", meta.ref)
    assert (await store.blobs.head("tenant", claimed.ref)).metadata["kind"] == "attachment"


async def test_the_sweep_trims_the_backlog_removes_orphans_and_then_finds_nothing(store: RequestManifestStore) -> None:
    db = store.db
    await _session(db, "a")
    await _session(db, "c")
    written = [await _record(store, "a", f"turn-{index}") for index in range(3)]
    await store.settle()
    # A backlog from before the retention: a session's rows nobody pruned and no reference was ever
    # written for, a row of a session that is gone, and a manifest blob no row ever named.
    for index in range(6):
        await db.execute(
            "INSERT INTO request_manifests(manifest_id, session_id, attempt_id, request_sha256, model, manifest, created_at) VALUES (?, 'c', 'x', 'd', 'm', '{}', 'now')",
            (f"old-{index}",),
        )
    await db.conn.execute("PRAGMA foreign_keys=OFF")
    await db.execute(
        "INSERT INTO request_manifests(manifest_id, session_id, attempt_id, request_sha256, model, manifest, created_at) VALUES ('ghost', 'gone', 'x', 'd', 'm', '{}', 'now')"
    )
    await db.conn.execute("PRAGMA foreign_keys=ON")
    orphan = await store.blobs.put("tenant", b"never named", metadata={"kind": KIND, "slot": "messages"})
    other = await store.blobs.put("tenant", b"not a manifest")

    report = await store.sweep()

    assert (report.rows, report.blobs) == (4 + 1, 1) and report.bytes > len(b"never named")
    assert await _count(store, "a") == 2 and await _count(store, "c") == 2 and await _count(store, "gone") == 0
    assert not await store.blobs.exists("tenant", orphan.ref)
    assert await store.blobs.exists("tenant", other.ref)
    for kept in written[1:]:
        for slot in ("system_prompt", "messages", "tools"):
            assert await store.blobs.exists("tenant", _ref(kept[slot]))

    again = await store.sweep()
    assert (again.rows, again.blobs, again.bytes) == (0, 0, 0)


async def test_deleting_a_session_frees_its_manifests_and_their_blobs(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        keep = await manager.create_session("kept")
        gone = await manager.create_session("gone")
        store = manager.request_manifests
        mine = await _record(store, gone.session.id, "mine", tools="shared")
        theirs = await _record(store, keep.session.id, "theirs", tools="shared")
        await store.settle()

        assert await manager.delete_session(gone.session.id)

        assert await _count(store, gone.session.id) == 0
        assert not await store.blobs.exists(store.tenant_id, _ref(mine["messages"]))
        assert await store.blobs.exists(store.tenant_id, _ref(theirs["messages"]))
        assert await store.blobs.exists(store.tenant_id, _ref(mine["tools"])), "a body the other session names went with this one"
        orphaned = await db.fetchone(
            "SELECT count(*) AS n FROM request_manifest_blobs WHERE manifest_seq NOT IN (SELECT seq FROM request_manifests)"
        )
        assert orphaned is not None and orphaned["n"] == 0
    finally:
        await manager.close()


async def test_a_session_deleted_by_cascade_takes_its_references_along(store: RequestManifestStore) -> None:
    await _session(store.db, "a")
    await _record(store, "a", "only")
    await store.settle()
    await store.db.execute("DELETE FROM sessions WHERE id = 'a'")
    assert await store.db.fetchone("SELECT 1 FROM request_manifest_blobs") is None


async def test_a_failing_prune_is_logged_and_never_fails_the_record(store: RequestManifestStore, monkeypatch, caplog) -> None:
    await _session(store.db, "a")

    async def broken(session_id: str | None) -> int:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "prune_session", broken)
    with caplog.at_level(logging.ERROR, logger="daedalus.host.request_manifests"):
        for index in range(3):
            await _record(store, "a", f"turn-{index}")
        await store.settle()
    assert await _count(store, "a") == 3
    assert "pruning the request manifests of a failed" in caplog.text
