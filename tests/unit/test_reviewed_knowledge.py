"""Unreviewed extraction and stale evidence must never enter a task context packet."""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.stores.control import ControlConflict, Principal
from daedalus.stores.database import Database
from daedalus.stores.knowledge import KnowledgeConflict, KnowledgeStore


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    opened = Database(tmp_path / "state.sqlite")
    await opened.open()
    await opened.execute("INSERT INTO projects(id, name, created_at) VALUES ('project', 'Project', 'now')")
    await opened.execute(
        "INSERT INTO files(id, name, mime, size, sha256, origin, created_at) "
        "VALUES ('123456789abc', 'proof.txt', 'text/plain', 1, 'digest-one', 'operator', 'now')"
    )
    await opened.execute(
        "INSERT INTO file_access(file_id, scope, added_at) VALUES ('123456789abc', 'project', 'now')"
    )
    yield opened
    await opened.close()


async def test_candidate_needs_review_and_fresh_project_source(db: Database) -> None:
    store = KnowledgeStore(db)
    item = await store.candidate(
        "project", "A checked claim about this project", source_kind="file",
        source_id="123456789abc", actor="extractor",
    )
    assert await store.context_facts("project") == []
    reviewed = await store.review(item["fact_id"], "project", expected_version=1, verdict="promote", actor="operator", reason="verified")
    assert reviewed["version"] == 2
    assert [fact["claim"] for fact in await store.context_facts("project")] == ["A checked claim about this project"]
    with pytest.raises(KnowledgeConflict, match="version changed"):
        await store.review(item["fact_id"], "project", expected_version=1, verdict="forget", actor="operator", reason="old")
    await db.execute("DELETE FROM file_access WHERE file_id = '123456789abc'")
    assert await store.context_facts("project") == []
    with pytest.raises(KnowledgeConflict, match="source file"):
        await store.review(item["fact_id"], "project", expected_version=2, verdict="promote", actor="operator", reason="old proof")
    assert (await store.latest(item["fact_id"], "project"))["version"] == 2


async def test_review_and_forget_leave_an_auditable_history(db: Database) -> None:
    store = KnowledgeStore(db)
    item = await store.candidate(
        "project", "A second claim with a source", source_kind="file",
        source_id="123456789abc", actor="extractor",
    )
    await store.review(item["fact_id"], "project", expected_version=1, verdict="promote", actor="operator", reason="verified")
    await store.review(item["fact_id"], "project", expected_version=2, verdict="forget", actor="operator", reason="obsolete")
    assert await store.context_facts("project") == []
    history = await db.fetchall(
        "SELECT version, status FROM knowledge_fact_versions WHERE fact_id = ? ORDER BY version", (item["fact_id"],)
    )
    assert [(row["version"], row["status"]) for row in history] == [
        (1, "candidate"), (2, "promoted"), (3, "forgotten")
    ]


async def test_command_replay_returns_one_receipt_and_review_has_separate_cas(db: Database) -> None:
    store = KnowledgeStore(db)
    principal = Principal.operator({"via": "token", "user_id": 1})
    request = {
        "expected_revision": 1, "client_operation_id": "candidate-one",
        "claim": "A claim which needs explicit review", "kind": "fact",
        "source_kind": "file", "source_id": "123456789abc",
    }
    first = await store.candidate_command(principal, "project", **request)
    assert first == await store.candidate_command(principal, "project", **request)
    assert len(await store.list("project")) == 1
    with pytest.raises(ControlConflict, match="different request"):
        await store.candidate_command(principal, "project", **{**request, "claim": "A different claim of sufficient length"})
    promoted = await store.review_command(
        principal, "project", first["fact_id"], expected_revision=1,
        expected_version=1, verdict="promote", reason="source verified",
        client_operation_id="review-one",
    )
    assert promoted["version"] == 2
    assert promoted == await store.review_command(
        principal, "project", first["fact_id"], expected_revision=1,
        expected_version=1, verdict="promote", reason="source verified",
        client_operation_id="review-one",
    )
