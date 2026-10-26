"""Where memory lives and the entries it is read in: Remember files under the chat's project or the
operator's global memory and never under one chat, Recall in any chat finds what another chat kept,
runs that are not the operator's conversation stay out of global memory, every record is filed in an
entry with a source row, and the migration moves the per-chat records kept before."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.memory import MemoryScope
from protocore.contracts.tools import ToolContext

from daedalus.config import MemoryConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.memory import entries as entries_module
from daedalus.memory.curator import MemoryCurator
from daedalus.memory.entries import EntryConflict, EntryNotEmpty, MemoryEntries, Target, migrated_scope, target_for
from daedalus.memory.scope import MemoryPlacement, PlacementRefused, resolve_write, stamp
from daedalus.memory.tools import build_memory_tools
from daedalus.stores.database import MIGRATIONS, Database, DataMigration, _memory_entries
from daedalus.stores.persistent import PersistentMemory
from tests.support.models import model_config

T = "t"


def ctx(session: str, placement: MemoryPlacement | None = None) -> ToolContext:
    metadata: dict[str, Any] = {"tool_call_id": "c"}
    if placement is not None:
        stamp(metadata, placement)
    return ToolContext(tenant_id=T, run_id="r1", session_id=session, metadata=metadata)


@pytest.fixture
async def memory(db: Database) -> PersistentMemory:
    store = PersistentMemory(db)
    await store.load()
    return store


@pytest.fixture
async def entries(db: Database, memory: PersistentMemory) -> MemoryEntries:
    return MemoryEntries(db, memory, T)


async def project(db: Database, project_id: str, name: str) -> str:
    await db.execute("INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)", (project_id, name, datetime.now(UTC).isoformat()))
    return project_id


def tools_for(memory: PersistentMemory, entries: MemoryEntries) -> dict[str, Any]:
    return {tool.name: tool for tool in build_memory_tools(memory, entries=entries, tenant_allowed=lambda: [], classifier=lambda: None)}


# -- the placement ----------------------------------------------------------------------------


def test_the_default_is_the_project_or_global_and_never_the_chat() -> None:
    assert MemoryPlacement().default() == (MemoryScope.global_, "")
    assert MemoryPlacement(project_id="p1").default() == (MemoryScope.project, "p1")
    assert MemoryPlacement(project_id="p1", operator=False).default() == (MemoryScope.project, "p1")
    assert MemoryPlacement(operator=False).default() is None
    assert MemoryPlacement(project_id="p1").scope_keys() == {MemoryScope.project: "p1"}
    assert MemoryPlacement().scope_keys() == {}


def test_a_run_that_is_not_the_operators_never_writes_global_memory() -> None:
    agent_in_project = MemoryPlacement(project_id="p1", operator=False)
    assert resolve_write(agent_in_project, None, None) == (MemoryScope.project, "p1")
    with pytest.raises(PlacementRefused, match="only the operator's own conversations"):
        resolve_write(agent_in_project, "global", None)
    with pytest.raises(PlacementRefused, match="no memory"):
        resolve_write(MemoryPlacement(operator=False), None, None)
    assert resolve_write(MemoryPlacement(operator=False), "project", "p2") == (MemoryScope.project, "p2")
    assert resolve_write(MemoryPlacement(project_id="p1"), "global", None) == (MemoryScope.global_, "")
    with pytest.raises(PlacementRefused, match="belongs to no project"):
        resolve_write(MemoryPlacement(), "project", None)
    with pytest.raises(PlacementRefused, match="global or project"):
        resolve_write(MemoryPlacement(), "session", None)


def test_a_later_placement_replaces_every_key_of_an_earlier_one() -> None:
    metadata: dict[str, Any] = {"unrelated": 1}
    stamp(metadata, MemoryPlacement(project_id="p1"))
    assert metadata["memory_default_scope"] == "project" and metadata["memory_scope_keys"] == {"project": "p1"}
    stamp(metadata, MemoryPlacement(operator=False))
    # A schedule's run after the operator's turn must not keep the operator's default.
    assert "memory_default_scope" not in metadata and metadata["daedalus.memory_writer"] == "agent" and metadata["unrelated"] == 1


# -- Remember and Recall across chats -----------------------------------------------------------


async def test_what_one_chat_remembers_another_chat_recalls(db: Database, memory: PersistentMemory, entries: MemoryEntries) -> None:
    tools = tools_for(memory, entries)
    kept = await tools["Remember"].invoke(ctx("chat-a", MemoryPlacement()), {"text": "The courier's rates change on the first of the month.", "category": "fact", "topics": ["courier"]})
    assert not kept.is_error and kept.metadata["scope"] == "global"
    # The defect this fixes: a note kept in one chat was invisible to every other chat.
    found = await tools["Recall"].invoke(ctx("chat-b", MemoryPlacement()), {"query": "courier rates"})
    assert found.metadata["count"] == 1 and "global/fact" in found.content
    # Without any placement (a tool called outside a run) the note still goes to global memory.
    bare = await tools["Remember"].invoke(ctx("chat-c"), {"text": "Bread is baked at four.", "category": "fact", "topics": ["bakery"]})
    assert bare.metadata["scope"] == "global"


async def test_a_project_note_stays_in_its_project(db: Database, memory: PersistentMemory, entries: MemoryEntries) -> None:
    await project(db, "p-bakery", "Bakery site")
    await project(db, "p-garden", "Garden")
    tools = tools_for(memory, entries)
    bakery = MemoryPlacement(project_id="p-bakery")
    kept = await tools["Remember"].invoke(ctx("chat-a", bakery), {"text": "Images are capped at 60 KB.", "category": "project", "topics": ["images"]})
    assert kept.metadata["scope"] == "project" and kept.metadata["scope_key"] == "p-bakery"
    assert (await tools["Recall"].invoke(ctx("chat-b", bakery), {"query": "images"})).metadata["count"] == 1
    assert (await tools["Recall"].invoke(ctx("chat-c", MemoryPlacement(project_id="p-garden")), {"query": "images"})).metadata["count"] == 0
    assert (await tools["Recall"].invoke(ctx("chat-d", MemoryPlacement()), {"query": "images"})).metadata["count"] == 0
    # Named explicitly, another project's memory can be read.
    assert (await tools["Recall"].invoke(ctx("chat-d", MemoryPlacement()), {"query": "images", "scope": "project", "scope_key": "p-bakery"})).metadata["count"] == 1
    # And global memory is read inside a project too.
    await tools["Remember"].invoke(ctx("chat-e", MemoryPlacement()), {"text": "The operator prefers short answers.", "category": "preference", "topics": ["answers"]})
    assert (await tools["Recall"].invoke(ctx("chat-b", bakery), {"query": "short answers"})).metadata["count"] == 1


async def test_a_staff_or_scheduled_run_is_refused_global_memory(db: Database, memory: PersistentMemory, entries: MemoryEntries) -> None:
    tools = tools_for(memory, entries)
    refused = await tools["Remember"].invoke(ctx("sub-1", MemoryPlacement(operator=False)), {"text": "A finding.", "category": "fact", "topics": ["x"]})
    assert refused.is_error and "in your answer" in refused.content and memory.records(T) == []
    refused = await tools["Remember"].invoke(ctx("sub-1", MemoryPlacement(operator=False)), {"text": "A finding.", "category": "fact", "topics": ["x"], "scope": "global"})
    assert refused.is_error and memory.records(T) == []
    missing = await tools["Remember"].invoke(ctx("chat-a", MemoryPlacement()), {"text": "A finding.", "category": "fact", "topics": ["x"], "scope": "project", "scope_key": "nope"})
    assert missing.is_error and "no project" in missing.content and memory.records(T) == []
    with pytest.raises(ValueError):
        await tools["Remember"].invoke(ctx("chat-a", MemoryPlacement()), {"text": "x", "category": "fact", "scope": "session"})


async def test_remember_files_an_entry_and_names_the_chat_that_taught_it(db: Database, memory: PersistentMemory, entries: MemoryEntries) -> None:
    await project(db, "p-bakery", "Bakery site")
    tools = tools_for(memory, entries)
    pref = await tools["Remember"].invoke(ctx("chat-a", MemoryPlacement()), {"text": "The operator wants answers in Russian.", "category": "preference", "topics": ["language"]})
    area = await tools["Remember"].invoke(ctx("chat-b", MemoryPlacement(project_id="p-bakery")), {"text": "The site deploys with scripts/deploy.sh.", "category": "procedure", "topics": ["deploy"]})
    listing = {(e["section"], e["slug"]): e for e in await entries.listing()}
    assert listing[("you", "preferences")]["count"] == 1 and listing[("area", "bakery-site")]["count"] == 1
    assert listing[("area", "bakery-site")]["project_id"] == "p-bakery" and listing[("area", "bakery-site")]["project_name"] == "Bakery site"
    assert pref.metadata["entry"] == listing[("you", "preferences")]["id"]
    sources = await entries.sources([area.metadata["memory_id"]])
    assert [(s["origin"], s["session_id"], s["run_id"]) for s in sources[area.metadata["memory_id"]]] == [("remember", "chat-b", "r1")]
    # The entry's version stepped when its bullet arrived: an edit made against the old one is refused.
    entry = await entries.get(listing[("you", "preferences")]["id"])
    assert entry is not None and entry.version == 2


# -- the entries ------------------------------------------------------------------------------


def test_the_filing_rule() -> None:
    assert target_for("fact", MemoryScope.project, "p1", "anything", []) == Target("area", project_id="p1")
    assert target_for("preference", MemoryScope.project, "p1", "anything", []) == Target("area", project_id="p1")
    assert target_for("preference", MemoryScope.global_, "", "Short answers.", []) == entries_module.PREFERENCES
    assert target_for("person", MemoryScope.global_, "", "The operator works nights.", []) == entries_module.PROFILE
    assert target_for("person", MemoryScope.global_, "", "Оператор живёт в Казани.", []) == entries_module.PROFILE
    assert target_for("person", MemoryScope.global_, "", "Anna owns the bakery.", []) == entries_module.PEOPLE
    assert target_for("fact", MemoryScope.global_, "", "x", ["courier", "rates"], known_topics={"rates"}).slug == "rates"
    assert target_for("fact", MemoryScope.global_, "", "x", ["courier"], known_topics={"rates"}) == entries_module.RECENT_WORK


async def test_entries_are_versioned_and_an_entry_with_bullets_is_not_deleted(db: Database, memory: PersistentMemory, entries: MemoryEntries) -> None:
    made = await entries.create("topic", "Languages", "Languages", summary="What the operator writes code in")
    assert made.slug == "languages" and made.version == 1
    with pytest.raises(ValueError, match="exists"):
        await entries.create("topic", "languages", "Again")
    with pytest.raises(ValueError, match="unknown section"):
        await entries.create("elsewhere", "x", "X")
    edited = await entries.update(made.id, if_version=1, summary="Python and TypeScript", by_operator=True)
    assert edited.version == 2 and edited.edited_by_operator and edited.summary == "Python and TypeScript"
    with pytest.raises(EntryConflict):
        await entries.update(made.id, if_version=1, title="Stale")
    with pytest.raises(KeyError):
        await entries.update("ent-none", if_version=1, title="x")
    result = await entries.remember(MemoryScope.global_, "", "Mostly Python.", kind="fact", metadata={"topics": ["languages"]}, origin="operator")
    assert entries_module.entry_of(result.record) == made.id
    with pytest.raises(EntryNotEmpty):
        await entries.delete(made.id)
    await memory.delete(T, result.record.id)
    assert await entries.delete(made.id) and await entries.get(made.id) is None


async def test_one_area_per_project_and_a_taken_name_gets_the_id(db: Database, memory: PersistentMemory, entries: MemoryEntries) -> None:
    await project(db, "p-one", "Shop")
    await project(db, "p-two", "Shop")
    one = await entries.ensure(Target("area", project_id="p-one"))
    assert await entries.ensure(Target("area", project_id="p-one")) == one
    two = await entries.ensure(Target("area", project_id="p-two"))
    assert (one.slug, two.slug) == ("shop", "shop-p-two") and two.title == "Shop"
    with pytest.raises(entries_module.UnknownProject):
        await entries.ensure(Target("area", project_id="p-none"))
    # The area outlives its project as a free area.
    await db.execute("DELETE FROM projects WHERE id = 'p-one'")
    kept = await entries.get(one.id)
    assert kept is not None and kept.project_id is None


async def test_records_written_around_the_entries_are_filed_at_start(db: Database, memory: PersistentMemory, entries: MemoryEntries) -> None:
    await memory.write(T, MemoryScope.global_, "", "The operator likes tea.", kind="preference", metadata={"topics": ["tea"]})
    await memory.write(T, MemoryScope.project, "p-gone", "A note of a deleted project.", kind="fact", metadata={"topics": []})
    assert await entries.file_unfiled() == 2
    assert await entries.file_unfiled() == 0
    slugs = {e.id: e.slug for e in await entries.list()}
    assert sorted(slugs[entries_module.entry_of(r)] for r in memory.records(T)) == ["preferences", "recent-work"]


async def test_a_merge_keeps_the_sources_of_what_it_absorbed(db: Database, memory: PersistentMemory, entries: MemoryEntries) -> None:
    a = (await entries.remember(MemoryScope.global_, "", "The API listens on port 48765.", kind="fact", metadata={"topics": ["api"]}, origin="remember", session_id="chat-a", similarity_threshold=1.01)).record
    b = (await entries.remember(MemoryScope.global_, "", "The API listens on port 48765 behind the proxy.", kind="fact", metadata={"topics": ["api"]}, origin="remember", session_id="chat-b", similarity_threshold=1.01)).record
    settings = MemoryConfig()
    curator = MemoryCurator(db, memory, T, lambda: settings)
    merged, history = await curator.merge([a.id, b.id], keep_id=a.id)
    sources = await entries.sources([merged.id])
    assert {s["session_id"] for s in sources[merged.id]} == {"chat-a", "chat-b"}
    await curator.undo(history)
    assert {s["session_id"] for s in (await entries.sources([b.id]))[b.id]} == {"chat-b"}


# -- the session's placement --------------------------------------------------------------------


@pytest.fixture
async def manager(settings: Settings, db: Database) -> Any:
    m = SessionManager(settings, model_config(), db=db)
    await m.start()
    yield m
    await m.close()


async def test_the_manager_places_each_run(manager: SessionManager, tmp_path: Path) -> None:
    root = tmp_path / "bakery"
    root.mkdir()
    bakery = await manager.projects.create("Bakery", [str(root)])
    in_project = await manager.create_session("a chat in the project", project_id=bakery.id)
    lone = await manager.create_session("a lone chat")
    assert manager.memory_placement(in_project) == MemoryPlacement(project_id=bakery.id)
    assert manager.memory_placement(lone) == MemoryPlacement()
    lone.run_origin = "schedule"
    assert manager.memory_placement(lone) == MemoryPlacement(operator=False)
    lone.run_origin = "autopilot"
    assert not manager.memory_placement(lone).operator
    staff = await manager.create_session("Ada · menu", project_id=bakery.id, metadata={"staff_session_id": "live-1"})
    assert manager.memory_placement(staff) == MemoryPlacement(project_id=bakery.id, operator=False)
    sub = await manager.create_session("a subagent", metadata={"subagent_of": lone.session.id})
    assert manager.memory_placement(sub) == MemoryPlacement(operator=False)
    # The engine a run is built on carries the placement to its tools.
    engine = await manager._build_engine(in_project, "run-1")
    try:
        assert engine.run_state.run_metadata["memory_scope_keys"] == {"project": bakery.id}
        assert engine.run_state.run_metadata["memory_default_scope"] == "project"
    finally:
        if in_project.provider_hold is not None:
            in_project.provider_hold.close()


# -- the routes -------------------------------------------------------------------------------


@pytest.fixture
async def client(settings: Settings, db: Database, manager: SessionManager) -> Any:
    from daedalus.extensions.api import build_app

    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        yield c


H = {"X-Daedalus-Token": "tok"}


async def test_the_entry_routes(client: httpx.AsyncClient, manager: SessionManager, tmp_path: Path) -> None:
    root = tmp_path / "bakery"
    root.mkdir()
    bakery = await manager.projects.create("Bakery", [str(root)])
    add = lambda text, **extra: client.post("/api/memory", json={"text": text, **extra}, headers=H)  # noqa: E731
    pref = (await add("The operator wants plain prose.", category="preference", topics=["style"])).json()["record"]
    note = (await add("The shop opens at eight.", category="fact", topics=["hours"], scope="project", scope_key=bakery.id)).json()["record"]
    assert note["scope"] == "project" and note["entry"]
    assert (await add("x", scope="project")).status_code == 422
    assert (await add("x", scope="project", scope_key="p-none")).status_code == 422
    assert (await add("x", scope="session", scope_key="s1")).status_code == 422
    listing = (await client.get("/api/memory/entries", headers=H)).json()
    assert listing["records"] == 2 and listing["unfiled"] == 0 and listing["sections"] == {"you": 1, "topic": 0, "area": 1}
    area = next(e for e in listing["entries"] if e["section"] == "area")
    assert area["project_name"] == "Bakery" and area["count"] == 1
    assert [e["slug"] for e in (await client.get("/api/memory/entries?section=you", headers=H)).json()["entries"]] == ["preferences"]
    assert (await client.get("/api/memory/entries?section=nowhere", headers=H)).status_code == 422
    detail = (await client.get(f"/api/memory/entries/{pref['entry']}", headers=H)).json()
    assert detail["entry"]["slug"] == "preferences" and [r["id"] for r in detail["records"]] == [pref["id"]]
    assert [s["origin"] for s in detail["records"][0]["sources"]] == ["operator"]
    assert (await client.get("/api/memory/entries/ent-none", headers=H)).status_code == 404
    everything = (await client.get("/api/memory", headers=H)).json()
    assert everything["projects"] == {bakery.id: "Bakery"}
    assert next(b for b in everything["buckets"] if b["scope"] == "project")["title"] == "Bakery"
    # The records a chat taught, which the companion's sheet lists.
    await manager.memory_entries.add_source(note["id"], "remember", session_id="chat-1")
    taught = (await client.get("/api/memory?source_session=chat-1", headers=H)).json()["records"]
    assert [r["id"] for r in taught] == [note["id"]]


# -- the migration ----------------------------------------------------------------------------


def _record(memory_id: str, scope: str, key: str, kind: str, text: str, topics: list[str]) -> dict[str, Any]:
    stamp_at = datetime(2026, 10, 1, tzinfo=UTC).isoformat()
    return {"id": memory_id, "tenant_id": T, "scope": scope, "scope_key": key, "kind": kind, "text": text, "metadata": {"topics": topics},
            "source_refs": [], "salience": 0.0, "embedding": None, "version": 1, "created_at": stamp_at, "updated_at": stamp_at,
            "last_accessed_at": None, "access_count": 0}


def test_the_migrations_scope_rule() -> None:
    sessions = {"in-project": "p1", "lone": ""}
    projects = {"p1"}
    assert migrated_scope("session", "in-project", "fact", session_project=sessions, projects=projects) == (MemoryScope.project, "p1")
    assert migrated_scope("session", "in-project", "preference", session_project=sessions, projects=projects) == (MemoryScope.global_, "")
    assert migrated_scope("session", "in-project", "person", session_project=sessions, projects=projects) == (MemoryScope.global_, "")
    assert migrated_scope("session", "lone", "project", session_project=sessions, projects=projects) == (MemoryScope.global_, "")
    assert migrated_scope("session", "deleted", "fact", session_project=sessions, projects=projects) == (MemoryScope.global_, "")
    assert migrated_scope("project", "p1", "fact", session_project=sessions, projects=projects) == (MemoryScope.project, "p1")
    assert migrated_scope("project", "chat-project", "fact", session_project=sessions, projects=projects) == (MemoryScope.global_, "")
    assert migrated_scope("user", "u", "fact", session_project=sessions, projects=projects) == (MemoryScope.global_, "")


async def test_the_migration_moves_the_per_chat_records(db: Database) -> None:
    assert isinstance(MIGRATIONS[-1], DataMigration) and "memory_entries" in MIGRATIONS[-1].sql
    at = datetime.now(UTC).isoformat()
    await project(db, "p-bakery", "Bakery site")
    await db.execute("INSERT INTO projects(id, name, created_at, settings) VALUES ('lone', 'lone', ?, '{\"ephemeral\": true}')", (at,))
    await db.execute("INSERT INTO projects(id, name, created_at, system) VALUES ('p-voice', 'Voice', ?, 'companion')", (at,))
    for sid, pid in (("s-bakery", "p-bakery"), ("s-lone", "lone"), ("s-voice", "p-voice")):
        await db.execute("INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, project_id) VALUES (?, ?, ?, ?, ?, ?)", (sid, T, sid, at, at, pid))
    records = [
        _record("mem-000001", "session", "s-bakery", "procedure", "Deploys run scripts/deploy.sh.", ["deploy"]),
        _record("mem-000002", "session", "s-bakery", "preference", "The operator wants plain prose.", ["style"]),
        _record("mem-000003", "session", "s-lone", "fact", "The courier's rates change monthly.", ["courier"]),
        _record("mem-000004", "session", "s-gone", "fact", "Courier pickups are at noon.", ["courier"]),
        _record("mem-000005", "session", "s-voice", "person", "Anna owns the bakery.", ["anna"]),
        _record("mem-000006", "session", "s-voice", "person", "The operator works nights.", ["schedule"]),
        _record("mem-000007", "global", "", "fact", "Tea is in the left cupboard.", ["tea"]),
    ]
    for record in records:
        await db.execute("INSERT INTO memory_records(tenant_id, id, record) VALUES (?, ?, ?)", (T, record["id"], json.dumps(record)))
    for _ in range(2):  # the second run changes nothing
        async with db.transaction() as conn:
            await _memory_entries(conn)
        rows = {row["id"]: json.loads(row["record"]) for row in await db.fetchall("SELECT id, record FROM memory_records")}
        entries = {row["id"]: (row["section"], row["slug"], row["project_id"]) for row in await db.fetchall("SELECT * FROM memory_entries")}
        placed = {i: (r["scope"], r["scope_key"], entries[r["metadata"]["entry"]][:2]) for i, r in rows.items()}
        assert placed == {
            "mem-000001": ("project", "p-bakery", ("area", "bakery-site")),
            "mem-000002": ("global", "", ("you", "preferences")),
            "mem-000003": ("global", "", ("topic", "courier")),
            "mem-000004": ("global", "", ("topic", "courier")),
            "mem-000005": ("global", "", ("topic", "people")),
            "mem-000006": ("global", "", ("you", "profile")),
            "mem-000007": ("global", "", ("topic", "recent-work")),
        }
        assert len(entries) == 6
        assert rows["mem-000001"]["version"] == 2 and rows["mem-000007"]["version"] == 1
        sources = {row["record_id"]: row["session_id"] for row in await db.fetchall("SELECT * FROM memory_sources WHERE origin = 'migration'")}
        assert sources["mem-000004"] == "s-gone" and sources["mem-000007"] == "" and len(sources) == 7
        history = await db.fetchall("SELECT * FROM memory_history WHERE actor = 'migration'")
        assert len(history) == 1 and "6 session->global" not in history[0]["summary"] and "5 session->global" in history[0]["summary"]
    # The move is undone from the Memory screen like any other change.
    memory = PersistentMemory(db)
    await memory.load()
    curator = MemoryCurator(db, memory, T, lambda: MemoryConfig())
    restored = await curator.undo(int(history[0]["id"]))
    assert {r.id: r.scope.value for r in restored}["mem-000001"] == "session"
    assert memory.get_record(T, "mem-000003") is not None and memory.get_record(T, "mem-000003").scope is MemoryScope.session  # type: ignore[union-attr]
