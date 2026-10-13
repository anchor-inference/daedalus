"""Memory by category and topic: what Remember infers and refuses, what Recall narrows to, the
tidying that merges duplicates and drops what a newer record contradicts, its undo, the migration
that categorised the records already kept, and the Memory screen's routes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.memory import MemoryScope
from protocore.contracts.tools import ToolContext

from daedalus.config import MemoryConfig, Settings
from daedalus.memory import taxonomy
from daedalus.memory.curator import MemoryCurator, find
from daedalus.memory.tools import build_memory_tools
from daedalus.stores.database import MIGRATIONS, Database, DataMigration
from daedalus.stores.persistent import PersistentMemory

T = "t"


def ctx(session: str = "s1") -> ToolContext:
    return ToolContext(tenant_id=T, run_id="r", session_id=session, metadata={"tool_call_id": "c"})


async def tools_for(memory: PersistentMemory, *, allowed: list[str] | None = None, ask: Any = None) -> dict[str, Any]:
    return {tool.name: tool for tool in build_memory_tools(memory, tenant_allowed=lambda: allowed or [], classifier=lambda: ask)}


@pytest.fixture
async def memory(db: Database) -> PersistentMemory:
    store = PersistentMemory(db)
    await store.load()
    return store


# -- the word lists ---------------------------------------------------------------------------


def test_the_category_comes_from_the_old_kind_or_the_words() -> None:
    assert taxonomy.guess_category("anything", "skill") == "procedure"
    assert taxonomy.guess_category("anything", "decision") == "project"
    assert taxonomy.guess_category("The operator prefers answers in Russian.") == "preference"
    assert taxonomy.guess_category("Her birthday is in May; she is a colleague from the bakery.") == "person"
    assert taxonomy.guess_category("The release branch of the repo is cut on Fridays.") == "project"
    assert taxonomy.guess_category("The API listens on port 48765.") == "fact"


def test_sensitive_subjects_are_found_and_engineering_words_are_not() -> None:
    assert taxonomy.sensitive_subjects("The operator was diagnosed with diabetes last year.") == {"health"}
    assert taxonomy.sensitive_subjects("Его зарплата — 300 тысяч, ипотека до 2031.") == {"finances"}
    assert taxonomy.sensitive_subjects("Card 4111 1111 1111 1111 is the one to use.") == {"finances"}
    assert taxonomy.sensitive_subjects("Passport number is in the drawer.") == {"identity_documents"}
    assert taxonomy.sensitive_subjects("Живёт по адресу ул. Ленина, д. 5, кв. 12") == {"precise_location"}
    assert taxonomy.sensitive_subjects("Coordinates 55.751244, 37.618423 are the door.") == {"precise_location"}
    # The words an engineering memory is full of.
    for text in ("The /health check answers 200.", "Tech debt: the parser.", "Token savings of 30 % on the cache.",
                 "The symptom of the bug was a hang.", "Build 1234567890123 failed.", "Port 48765."):
        assert taxonomy.sensitive_subjects(text) == set(), text


def test_topics_reuse_the_ones_in_use_and_are_normalised() -> None:
    assert taxonomy.infer_topics("Deploy the bakery site with the deploy script.", {"bakery": 3, "billing": 2})[0] == "bakery"
    assert taxonomy.infer_topics("The Grafana board shows the queue.", {}) == ["grafana"]
    assert taxonomy.normalise_topics(["#Billing API", "billing-api", " ", "x" * 50, 7]) == ["billing-api", "x" * 32]
    assert taxonomy.normalise_topics("a, b; c\nd, e, f") == ["a", "b", "c", "d", "e"]


async def test_the_classifier_model_answers_and_a_broken_one_leaves_the_word_lists() -> None:
    async def good(prompt: str) -> str:
        assert "bakery" in prompt  # the topics in use are offered for reuse
        return 'Sure: {"category": "person", "topics": ["Bakery", "owner"], "sensitive": ["intimate", "bogus"]}'

    found = await taxonomy.classify("The owner of the bakery is called Anna.", vocabulary={"bakery": 2}, ask=good)
    assert (found.category, found.topics, found.sensitive, found.source) == ("person", ["bakery", "owner"], {"intimate"}, "model")

    async def broken(prompt: str) -> str:
        raise TimeoutError

    found = await taxonomy.classify("The operator prefers tea; diagnosed with asthma.", ask=broken)
    assert (found.category, found.source) == ("preference", "heuristic") and found.sensitive == {"health"}

    async def says_clean(prompt: str) -> str:
        return '{"category": "fact", "topics": [], "sensitive": []}'

    # The word lists' findings stay even when the model calls the note clean.
    assert (await taxonomy.classify("Her passport expires in June.", ask=says_clean)).sensitive == {"identity_documents"}


# -- Remember and Recall ----------------------------------------------------------------------


async def test_remember_infers_category_and_topics_and_keeps_given_ones(memory: PersistentMemory) -> None:
    tools = await tools_for(memory)
    result = await tools["Remember"].invoke(ctx(), {"text": "The operator prefers short answers in Russian."})
    assert not result.is_error and "category=preference" in result.content
    record = memory.get_record(T, result.metadata["memory_id"])
    assert record is not None and record.kind == "preference" and record.metadata["topics"]
    result = await tools["Remember"].invoke(ctx(), {"text": "Deploys run through scripts/deploy.sh.", "category": "procedure", "topics": ["Deploy", "#scripts"]})
    record = memory.get_record(T, result.metadata["memory_id"])
    assert record is not None and record.kind == "procedure" and record.metadata["topics"] == ["deploy", "scripts"]
    with pytest.raises(ValueError):
        await tools["Remember"].invoke(ctx(), {"text": "x", "category": "gossip"})


async def test_remember_refuses_a_sensitive_subject_and_says_why(memory: PersistentMemory) -> None:
    tools = await tools_for(memory)
    result = await tools["Remember"].invoke(ctx(), {"text": "The operator takes insulin twice a day.", "category": "person", "topics": ["operator"]})
    assert result.is_error and "health" in result.content and "Settings → Memory" in result.content and "other words" in result.content
    assert memory.records(T) == []
    # Allowed in Settings → Memory, the same note is kept.
    tools = await tools_for(memory, allowed=["health"])
    assert not (await tools["Remember"].invoke(ctx(), {"text": "The operator takes insulin twice a day."})).is_error

    async def model(prompt: str) -> str:
        return '{"category": "person", "topics": ["anna"], "sensitive": ["intimate"]}'

    tools = await tools_for(memory, ask=model)
    refused = await tools["Remember"].invoke(ctx(), {"text": "Anna and Boris are seeing each other."})
    assert refused.is_error and refused.metadata["refused"] == ["intimate"]


async def test_recall_narrows_by_category_and_topic(memory: PersistentMemory) -> None:
    tools = await tools_for(memory)
    await tools["Remember"].invoke(ctx(), {"text": "The bakery site deploys with the deploy script.", "category": "procedure", "topics": ["bakery", "deploy"]})
    await tools["Remember"].invoke(ctx(), {"text": "The bakery owner is called Anna.", "category": "person", "topics": ["bakery"]})
    await tools["Remember"].invoke(ctx(), {"text": "The shop deploys at night.", "category": "fact", "topics": ["shop"]})
    by_topic = await tools["Recall"].invoke(ctx(), {"topic": "Bakery"})
    assert by_topic.metadata["count"] == 2 and "topics" not in by_topic.content and "bakery" in by_topic.content
    both = await tools["Recall"].invoke(ctx(), {"query": "deploys", "topic": "bakery", "category": "procedure"})
    assert both.metadata["count"] == 1 and "deploy script" in both.content and "/procedure; bakery, deploy)" in both.content
    assert (await tools["Recall"].invoke(ctx(), {"category": "preference"})).metadata["count"] == 0
    assert (await tools["Recall"].invoke(ctx("another"), {"topic": "bakery"})).metadata["count"] == 0


async def test_a_new_id_never_reuses_one_still_in_use(db: Database, memory: PersistentMemory) -> None:
    first = (await memory.write(T, MemoryScope.global_, "", "one fact about tea")).record
    second = (await memory.write(T, MemoryScope.global_, "", "another fact about coffee")).record
    await memory.delete(T, first.id)
    reloaded = PersistentMemory(db)
    await reloaded.load()
    third = (await reloaded.write(T, MemoryScope.global_, "", "a third fact about water")).record
    assert third.id not in (second.id,) and reloaded.get_record(T, second.id) is not None


async def test_a_merged_write_keeps_the_topics_it_had(memory: PersistentMemory) -> None:
    first = await memory.write(T, MemoryScope.global_, "", "The API listens on port 48765 today", kind="fact", metadata={"topics": ["api"]})
    again = await memory.write(T, MemoryScope.global_, "", "The API listens on port 48765 today, behind the proxy", kind="fact", metadata={"topics": ["proxy"]}, similarity_threshold=0.5)
    assert again.record.id == first.record.id and again.record.metadata["topics"] == ["api", "proxy"]


# -- tidying ----------------------------------------------------------------------------------


def curator_for(db: Database, memory: PersistentMemory, **config: Any) -> MemoryCurator:
    settings = MemoryConfig(**config)
    return MemoryCurator(db, memory, T, lambda: settings)


async def _write(memory: PersistentMemory, text: str, *, scope: MemoryScope = MemoryScope.global_, key: str = "", kind: str = "fact", topics: list[str] | None = None, age_days: int = 0) -> str:
    record = (await memory.write(T, scope, key, text, kind=kind, metadata={"topics": topics or []}, similarity_threshold=1.01)).record
    if age_days:
        stamp = datetime.now(UTC) - timedelta(days=age_days)
        await memory.restore(record.model_copy(update={"updated_at": stamp, "created_at": stamp}))
    return record.id


async def test_a_sure_duplicate_is_merged_and_the_merge_can_be_undone(db: Database, memory: PersistentMemory) -> None:
    a = await _write(memory, "Deploys go through the deploy script in each project, never rsync by hand", topics=["deploy"], age_days=2)
    b = await _write(memory, "Deploys go through the deploy script in each project; never rsync by hand.", topics=["scripts"])
    curator = curator_for(db, memory)
    assert await curator.consolidate() == {"merged": 1, "suggested": 0}
    left = memory.records(T)
    assert len(left) == 1 and left[0].id == b and left[0].metadata["topics"] == ["scripts", "deploy"]
    # Nothing changed since: the next pass does no work.
    assert await curator.consolidate() == {"merged": 0, "suggested": 0}
    entry = (await curator.history())[0]
    assert entry["actor"] == "auto" and {r["id"] for r in entry["before"]} == {a, b}
    await curator.undo(entry["id"])
    assert {r.id for r in memory.records(T)} == {a, b}
    assert memory.get_record(T, b).metadata["topics"] == ["scripts"]  # type: ignore[union-attr]
    reloaded = PersistentMemory(db)
    await reloaded.load()
    assert {r.id for r in reloaded.records(T)} == {a, b}


async def test_a_newer_value_replaces_the_older_one(db: Database, memory: PersistentMemory) -> None:
    old = await _write(memory, "The API listens on port 8765.", age_days=3)
    new = await _write(memory, "The API listens on port 48765.")
    await curator_for(db, memory).consolidate()
    assert [(r.id, r.text) for r in memory.records(T)] == [(new, "The API listens on port 48765.")]
    assert memory.get_record(T, old) is None


async def test_what_is_not_sure_waits_as_a_suggestion(db: Database, memory: PersistentMemory) -> None:
    # The same text in two scopes: merging would change who can recall it.
    one = await _write(memory, "The bot commit is 5cedcdd.", scope=MemoryScope.session, key="s1")
    two = await _write(memory, "The bot commit is 5cedcdd.", scope=MemoryScope.session, key="s2")
    # Alike, not the same.
    await _write(memory, "Images are capped at 60 KB and 1600 px for the shop", age_days=1)
    await _write(memory, "Images for the shop are capped at 60 KB, the phone is slow")
    curator = curator_for(db, memory)
    assert await curator.consolidate() == {"merged": 0, "suggested": 2}
    waiting = await curator.suggestions()
    commit = next(s for s in waiting if set(s["memory_ids"]) == {one, two})
    await curator.dismiss(waiting[1 - waiting.index(commit)]["id"])
    assert len(await curator.suggestions()) == 1
    # Dismissed stays dismissed on the next pass.
    assert (await curator.consolidate(force=True))["suggested"] == 0
    merged, _ = await curator.apply(commit["id"])
    assert {r.id for r in memory.records(T)} >= {merged.id} and len(memory.records(T)) == 3


async def test_with_auto_merge_off_everything_is_a_suggestion(db: Database, memory: PersistentMemory) -> None:
    await _write(memory, "The API listens on port 8765.", age_days=3)
    await _write(memory, "The API listens on port 48765.")
    curator = curator_for(db, memory, auto_merge=False)
    assert await curator.consolidate() == {"merged": 0, "suggested": 1}
    assert (await curator.suggestions())[0]["kind"] == "contradiction"


async def test_undo_is_refused_once_the_merged_record_changed(db: Database, memory: PersistentMemory) -> None:
    a = await _write(memory, "tea is green", topics=["tea"])
    b = await _write(memory, "tea is black", topics=["tea"])
    curator = curator_for(db, memory)
    merged, history = await curator.merge([a, b], text="tea is green or black")
    await curator.edit(merged.id, category="preference")
    from daedalus.memory.curator import CuratorConflict
    with pytest.raises(CuratorConflict):
        await curator.undo(history)
    renamed = await curator.rename_topic("tea", "drinks")
    assert renamed == 1 and memory.get_record(T, merged.id).metadata["topics"] == ["drinks"]  # type: ignore[union-attr]
    entry = (await curator.history())[0]
    assert entry["action"] == "topic"
    await curator.undo(entry["id"])
    assert memory.get_record(T, merged.id).metadata["topics"] == ["tea"]  # type: ignore[union-attr]


async def test_embeddings_find_what_the_words_miss(memory: PersistentMemory) -> None:
    a = await _write(memory, "The operator wants short answers")
    b = await _write(memory, "Keep replies brief for the operator")
    records = memory.records(T)
    assert find(records) == []
    found = find(records, {a: [1.0, 0.0], b: [0.97, 0.243]})
    assert len(found) == 1 and found[0].kind == "duplicate" and found[0].sure


# -- the migration ----------------------------------------------------------------------------


async def test_the_migration_categorises_the_records_kept_before(settings: Settings) -> None:
    db = Database(settings.state_dir / "legacy.sqlite")
    await db.open()
    try:
        stamp = datetime.now(UTC).isoformat()
        records = [
            ("mem-000001", "skill", "When editing board-state.json, drop the trailing comma."),
            ("mem-000002", "preference", "Operator prefers to communicate in Russian."),
            ("mem-000003", "fact", "The board-state.json notes live in the board repo."),
            ("mem-000004", "observation", "The API listens on port 48765."),
        ]
        for memory_id, kind, text in records:
            record = {"id": memory_id, "tenant_id": T, "scope": "global", "scope_key": "", "kind": kind, "text": text, "metadata": {},
                      "source_refs": [], "salience": 0.0, "embedding": None, "version": 1, "created_at": stamp, "updated_at": stamp,
                      "last_accessed_at": None, "access_count": 0}
            await db.execute("INSERT INTO memory_records(tenant_id, id, record) VALUES (?, ?, ?)", (T, memory_id, json.dumps(record)))
        step = MIGRATIONS[-1]
        assert isinstance(step, DataMigration) and "memory_history" in step.sql
        for _ in range(2):  # the second run changes nothing
            async with db.transaction() as conn:
                await step.step(conn)
            rows = {row["id"]: json.loads(row["record"]) for row in await db.fetchall("SELECT id, record FROM memory_records")}
            assert {i: r["kind"] for i, r in rows.items()} == {"mem-000001": "procedure", "mem-000002": "preference", "mem-000003": "project", "mem-000004": "fact"}
            assert "board-state.json" in rows["mem-000001"]["metadata"]["topics"] and "board-state.json" in rows["mem-000003"]["metadata"]["topics"]
    finally:
        await db.close()


# -- the routes -------------------------------------------------------------------------------


@pytest.fixture
async def client(settings: Settings, db: Database) -> Any:
    from daedalus.extensions.api import build_app
    from daedalus.host.session_runner import SessionManager
    from tests.support.models import model_config

    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
            yield c
    finally:
        await manager.close()


H = {"X-Daedalus-Token": "tok"}


async def test_the_memory_screen_routes(client: httpx.AsyncClient) -> None:
    add = lambda text, **extra: client.post("/api/memory", json={"scope": "global", "text": text, **extra}, headers=H)  # noqa: E731
    a = (await add("The bakery site deploys with the deploy script.", topics=["bakery"])).json()["record"]
    assert a["category"] == "project" and a["topics"] == ["bakery"]
    b = (await add("The bakery owner is called Anna.", category="person", topics=["bakery", "anna"])).json()["record"]
    listing = (await client.get("/api/memory", headers=H)).json()
    assert listing["topics"][0] == {"topic": "bakery", "count": 2} and listing["categories"]["person"] == 1 and listing["suggestions"] == 0
    edited = (await client.patch(f"/api/memory/{b['id']}", json={"category": "fact"}, headers=H)).json()
    assert edited["category"] == "fact" and edited["history_id"]
    merged = (await client.post("/api/memory/merge", json={"ids": [a["id"], b["id"]], "text": "Bakery: deploy script; owner Anna."}, headers=H)).json()
    assert merged["record"]["topics"] == ["bakery", "anna"]
    assert len((await client.get("/api/memory", headers=H)).json()["records"]) == 1
    history = (await client.get("/api/memory/history", headers=H)).json()["entries"]
    assert [e["action"] for e in history] == ["merge", "recategorise"]
    assert (await client.post(f"/api/memory/history/{history[0]['id']}/undo", headers=H)).status_code == 200
    assert (await client.post(f"/api/memory/history/{history[0]['id']}/undo", headers=H)).status_code == 409
    assert len((await client.get("/api/memory", headers=H)).json()["records"]) == 2
    assert (await client.post("/api/memory/topics/rename", json={"topic": "bakery", "to": "shop"}, headers=H)).json() == {"changed": 2}
    await add("The bot commit is 5cedcdd.", scope="session", scope_key="s1")
    await add("The bot commit is 5cedcdd.", scope="session", scope_key="s2")
    assert (await client.post("/api/memory/consolidate", headers=H)).json() == {"merged": 0, "suggested": 1}
    waiting = (await client.get("/api/memory/suggestions", headers=H)).json()
    assert len(waiting["suggestions"]) == 1 and set(waiting["records"]) == set(waiting["suggestions"][0]["memory_ids"])
    sid = waiting["suggestions"][0]["id"]
    assert (await client.post(f"/api/memory/suggestions/{sid}/apply", json={}, headers=H)).status_code == 200
    assert (await client.post(f"/api/memory/suggestions/{sid}/apply", json={}, headers=H)).status_code == 409
    assert (await client.get("/api/memory/suggestions", headers=H)).json()["suggestions"] == []
