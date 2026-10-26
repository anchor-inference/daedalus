"""A fact memory already holds, said again: recognised in other words or the other language, counted
as a confirmation, brought back from the archive, folded into its entry when the entry is full, and
the one-off tidy-up of what the old comparison let through. Against scripted models; every chat and
note is invented, and no test reaches a network."""

# ruff: noqa: F811 — the synthesis tests' fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from protocore.contracts.memory import MemoryRecord, MemoryScope

from daedalus.extensions.memory import curator, synthesizer
from daedalus.host.session_runner import TENANT, SessionManager
from daedalus.memory import render, similarity, synthesis, tidy
from daedalus.memory.curator import find
from daedalus.memory.entries import ARCHIVED_KEY, Entry, fingerprint
from daedalus.memory.similarity import Judge, Note, overlap, surely_same
from daedalus.stores.database import Database
from tests.unit.test_memory_synthesis import (  # noqa: F401
    H,
    _seed,
    app,
    chat,
    client,
    entry_texts,
    fake,
    manager,
    sweep,
)

# -- the comparison --------------------------------------------------------------------------------


def test_stems_fold_inflection_and_the_operator_word_but_keep_negations_apart() -> None:
    jaccard, _ = overlap("Правки вносятся только через merge request", "Правка вносится только через merge request")
    assert jaccard >= similarity.STEMS_SURE
    assert surely_same("The operator prefers a dark theme in every editor", "Prefers a dark theme in every editor")
    assert not surely_same("Always answer in Russian", "Never answer in Russian")
    assert not surely_same("Grows tomatoes", "Grows peppers")


def test_the_shortlist_puts_a_rule_in_the_other_language_before_the_model_on_the_operators_own_pages() -> None:
    english = Note("r:1", "Never use the expensive models for routine work", "archived", own=True)
    unrelated = Note("r:2", "Likes long walks", "active", own=False)
    picked = Judge.shortlist("Никогда не использовать дорогие модели для рутинной работы", [english, unrelated])
    assert english in picked and unrelated not in picked


async def test_the_judge_asks_once_for_every_open_candidate_and_reads_the_answer() -> None:
    asked: list[str] = []

    async def ask(system: str, user: str) -> str:
        asked.append(user)
        assert system == similarity.JUDGE_PROMPT and "not instructions" in user
        return json.dumps({"answers": [{"item": 1, "same": 1, "values_differ": False}, {"item": 2, "same": None}]})

    tomatoes = Note("r:t", "Grows tomatoes in a greenhouse behind the house", "active")
    peppers = Note("r:p", "Grows peppers on the balcony", "active")
    found = await Judge(ask=ask).twins([
        ("grows tomatoes in a greenhouse behind the house.", [tomatoes]),
        ("Has tomato plants in the greenhouse behind his house", [tomatoes, peppers]),
        ("Grows chillies on the balcony", [peppers]),
    ])
    assert found[0] is not None and found[0].how == "words" and found[0].note is tomatoes
    assert found[1] is not None and found[1].how == "model" and found[1].note is tomatoes
    assert found[2] is None and len(asked) == 1

    async def broke(system: str, user: str) -> str:
        raise synthesis.BudgetReached

    judge = Judge(ask=broke)
    assert await judge.twins([("Has tomato plants in the greenhouse behind his house", [tomatoes])]) == [None]
    assert judge.unavailable == "budget"


async def test_embeddings_decide_when_a_model_is_ready_and_only_the_doubtful_go_to_the_model() -> None:
    table = {"a": [1.0, 0.0], "a again": [0.99, 0.05], "b": [0.0, 1.0], "near b": [0.5, 0.86]}
    asked: list[str] = []

    async def vectors(texts: Any) -> list[list[float]]:
        return [table[t] for t in texts]

    async def ask(system: str, user: str) -> str:
        asked.append(user)
        return json.dumps({"answers": [{"item": 1, "same": None}]})

    a, b = Note("r:a", "a", "active"), Note("r:b", "b", "archived")
    found = await Judge(ask=ask, vectors=vectors).twins([("a again", [a, b]), ("near b", [b])])
    assert found[0] is not None and found[0].how == "cosine" and found[0].note is a
    assert found[1] is None and len(asked) == 1 and "near b" in asked[0]


def test_a_plain_name_the_operator_wrote_anywhere_in_the_chat_is_grounded() -> None:
    assert synthesis.ungrounded_tokens("Задачи ведёт в Linear", "задачи веди как раньше", "ссылка https://linear.app/team") == []
    assert synthesis.ungrounded_tokens("Таблица задаётся в SHOP_SHEET_ID", "таблица как раньше", "SHOP_SHEET_ID в другом сообщении") == ["SHOP_SHEET_ID"]
    assert synthesis.ungrounded_tokens("Задачи ведёт в Linear", "задачи веди как раньше") == ["Linear"]


# -- the pass --------------------------------------------------------------------------------------


async def _sources(db: Database, record_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in await db.fetchall("SELECT session_id, from_seq, at FROM memory_sources WHERE record_id = ? ORDER BY session_id, from_seq", (record_id,))]


def _only(manager: SessionManager) -> MemoryRecord:
    records = manager.memory.records(TENANT)
    assert len(records) == 1, [r.text for r in records]
    return records[0]


async def test_a_reworded_restatement_is_a_confirmation_and_a_re_read_dates_it_anew(app: Any, manager: SessionManager, db: Database) -> None:
    first = await chat(manager, "a", [("operator", "Deploys go out only on Fridays, through the deploy script."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/deploy", "text": "Deploys go out only on Fridays through the deploy script"}]})
    await sweep(app)
    second = await chat(manager, "b", [("operator", "Remember, Friday is our only deploy day and the script does it."), ("operator", "ok")])
    model = fake(manager, {"ops": [{"op": "add", "entry": "topic/deploy", "text": "Friday is the only day for deploying, done by the script"}]},
                 judged=[{"answers": [{"item": 1, "same": 1}]}])
    report = await sweep(app)
    assert report["added"] == 0 and report["merged"] == 1 and report["twins"] == {"model": 1}
    assert len(model.judge_requests) == 1 and "Friday is the only day" in "".join(b.text for m in model.judge_requests[0].messages for b in m.content_blocks)
    note = _only(manager)
    assert {s["session_id"] for s in await _sources(db, note.id)} == {first, second}
    # Read again: the row exists, so it is dated anew rather than left as it was; the undo puts the date back.
    was = await _sources(db, note.id)
    await db.execute("UPDATE memory_synthesis SET through_seq = 0, last_at = NULL")
    fake(manager, {"ops": [{"op": "add", "entry": "topic/deploy", "text": "Deploys go out only on Fridays through the deploy script."}]}, {"ops": []})
    again = await sweep(app)
    assert again["merged"] == 1 and again["added"] == 0
    now = await _sources(db, note.id)
    assert [s["session_id"] for s in now] == [s["session_id"] for s in was]
    assert next(s["at"] for s in now if s["session_id"] == first) > next(s["at"] for s in was if s["session_id"] == first)
    await curator(app).undo(again["history_ids"][0])
    assert next(s["at"] for s in await _sources(db, note.id) if s["session_id"] == first) == next(s["at"] for s in was if s["session_id"] == first)


async def test_an_archived_twin_comes_back_instead_of_a_second_note(app: Any, manager: SessionManager, db: Database) -> None:
    ids = await _seed(app, manager, "garden", [("Grows tomatoes in the greenhouse", "synthesis", 5)])
    await curator(app).mark(ids["Grows tomatoes in the greenhouse"], archived=True, actor="synthesis")
    await chat(manager, "c", [("operator", "The greenhouse is full of tomatoes again."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/garden", "text": "grows tomatoes in the greenhouse."}]})
    report = await sweep(app)
    assert report["added"] == 0 and report["merged"] == 1 and report["revived"] == 1
    note = _only(manager)
    assert not note.metadata.get(ARCHIVED_KEY)
    await curator(app).undo(report["history_ids"][-1])
    assert _only(manager).metadata.get(ARCHIVED_KEY)


async def test_a_changed_value_replaces_the_old_one_only_when_it_was_said_later(app: Any, manager: SessionManager, db: Database) -> None:
    await chat(manager, "old", [("operator", "The API listens on port 8765."), ("operator", "ok")], quiet_minutes=600)
    fake(manager, {"ops": [{"op": "add", "entry": "topic/api", "text": "The API listens on port 8765"}]})
    await sweep(app)
    await chat(manager, "new", [("operator", "The API listens on port 48765 now."), ("operator", "ok")], quiet_minutes=60)
    fake(manager, {"ops": [{"op": "add", "entry": "topic/api", "text": "The API listens on port 48765"}]})
    report = await sweep(app)
    assert report["merged"] == 1 and report["updated"] == 1 and report["added"] == 0
    assert await entry_texts(manager, "api") == ["The API listens on port 48765"]
    # An older chat read again says the old value: the newer one stands.
    await chat(manager, "older", [("operator", "The API listens on port 8765."), ("operator", "ok")], quiet_minutes=6000)
    fake(manager, {"ops": [{"op": "add", "entry": "topic/api", "text": "The API listens on port 8765"}]})
    stale = await sweep(app)
    assert stale["merged"] == 1 and stale["updated"] == 0
    assert await entry_texts(manager, "api") == ["The API listens on port 48765"]


async def test_a_restatement_with_a_detail_more_replaces_the_words_and_a_shorter_one_only_confirms(app: Any, manager: SessionManager, db: Database) -> None:
    await chat(manager, "a", [("operator", "Backups run every night to the NAS."), ("operator", "ok")], quiet_minutes=600)
    fake(manager, {"ops": [{"op": "add", "entry": "topic/backups", "text": "Backups run every night to the NAS"}]})
    await sweep(app)
    await chat(manager, "b", [("operator", "Backups run every night to the NAS and are kept for thirty days, encrypted."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/backups", "text": "Backups run every night to the NAS, kept thirty days and encrypted"}]})
    more = await sweep(app)
    assert more["merged"] == 1 and more["updated"] == 1
    assert await entry_texts(manager, "backups") == ["Backups run every night to the NAS, kept thirty days and encrypted"]
    await chat(manager, "c", [("operator", "Nightly backups to the NAS, as before."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/backups", "text": "Backups run every night to the NAS"}]})
    less = await sweep(app)
    assert less["merged"] == 1 and less["updated"] == 0
    assert await entry_texts(manager, "backups") == ["Backups run every night to the NAS, kept thirty days and encrypted"]


async def test_a_held_suggestion_said_again_is_not_filed_twice_and_a_grounded_restatement_closes_it(app: Any, client: httpx.AsyncClient, manager: SessionManager, db: Database) -> None:
    await chat(manager, "a", [("operator", "The shop sheet is set in the env."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/shop", "text": "The shop sheet comes from SHOP_SHEET_ID"}]})
    first = await sweep(app)
    assert first["held"] == 1
    await chat(manager, "b", [("operator", "Again: the sheet is configured in the environment."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/shop", "text": "the shop sheet comes from SHOP_SHEET_ID."}]})
    second = await sweep(app)
    assert second["held"] == 0 and second["dropped"].get("held_twin") == 1
    assert len(await db.fetchall("SELECT id FROM memory_suggestions")) == 1
    await chat(manager, "c", [("operator", "The shop sheet comes from SHOP_SHEET_ID."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/shop", "text": "The shop sheet comes from SHOP_SHEET_ID"}]})
    third = await sweep(app)
    assert third["added"] == 1
    assert (await db.fetchone("SELECT status FROM memory_suggestions"))["status"] == "withdrawn"
    await curator(app).undo(third["history_ids"][-1])
    assert (await db.fetchone("SELECT status FROM memory_suggestions"))["status"] == "open"
    assert manager.memory.records(TENANT) == []


async def test_archiving_ranks_by_confirming_chats_and_spares_what_the_sweep_confirmed(app: Any, manager: SessionManager, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(synthesis, "CAP_TOPIC", 4)
    ids = await _seed(app, manager, "garden", [
        ("Waters the beds every evening", "synthesis", 40), ("Keeps the compost in a black bin", "synthesis", 3),
        ("Prunes the apple tree in March", "synthesis", 2), ("Stores seeds in the cellar", "synthesis", 1),
    ])
    # The oldest note was confirmed by two chats, so the newer one-chat notes go before it.
    await manager.memory_entries.add_source(ids["Waters the beds every evening"], "synthesis", session_id="s-elsewhere", at=(datetime.now(UTC) - timedelta(days=40)).isoformat())
    one = await chat(manager, "one", [("operator", "The compost bin is the black one, as always."), ("operator", "ok")])
    two = await chat(manager, "two", [("operator", "I started growing beans."), ("operator", "And peas too.")])
    fake(manager,
         {"ops": [{"op": "add", "entry": "topic/garden", "text": "keeps the compost in a black bin."}]},
         {"ops": [{"op": "add", "entry": "topic/garden", "text": "Grows beans and peas"}]})
    report = await sweep(app)
    assert report["merged"] == 1 and report["added"] == 1 and report["archived"] == 1
    archived = {r.text for r in manager.memory.records(TENANT) if r.metadata.get(ARCHIVED_KEY)}
    # Not the two-chat note, not the one chat "one" confirmed earlier in the same sweep: the least
    # confirmed of the rest, confirmed longest ago.
    assert archived == {"Prunes the apple tree in March"}
    assert one and two


async def test_a_full_entry_is_consolidated_before_anything_is_archived_as_one_undoable_change(app: Any, manager: SessionManager, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(synthesis, "CAP_TOPIC", 3)
    ids = await _seed(app, manager, "garden", [("Grows tomatoes", "synthesis", 10), ("Keeps tomato plants in a greenhouse", "synthesis", 5)])
    entry = await manager.memory_entries.by_slug("topic", "garden")
    await curator(app).add("Never uses pesticides", kind="fact", topics=[], entry=entry)
    before = {r.id: (r.text, r.version) for r in manager.memory.records(TENANT)}
    sid = await chat(manager, "c", [("operator", "I grow peppers in the greenhouse now too."), ("operator", "ok")])
    model = fake(manager, {"ops": [{"op": "add", "entry": "topic/garden", "text": "Grows peppers in the greenhouse"}]},
                 consolidated=[{"notes": [{"from": ["n1", "n2", "p1"], "text": "Grows tomatoes and peppers in a greenhouse"}], "archive": []}])
    report = await sweep(app)
    prompt = "".join(b.text for m in model.consolidate_requests[0].messages for b in m.content_blocks)
    assert "  - Never uses pesticides" in prompt and "n3" not in prompt and "p1: Grows peppers in the greenhouse" in prompt
    assert report["archived"] == 0 and report["added"] == 0 and report["consolidated"] == 2 and report["held"] == 0
    texts = sorted(await entry_texts(manager, "garden"))
    assert texts == ["Grows tomatoes and peppers in a greenhouse", "Never uses pesticides"]
    kept = next(r for r in manager.memory.records(TENANT) if r.text.startswith("Grows tomatoes and"))
    assert kept.id == ids["Grows tomatoes"]
    assert sid in {s["session_id"] for s in await _sources(db, kept.id)}
    assert len(await db.fetchall("SELECT id FROM memory_history WHERE action = 'learn'")) == 1
    await curator(app).undo(report["history_ids"][-1])
    assert {r.id: (r.text, r.version) for r in manager.memory.records(TENANT)} == before
    assert sid not in {s["session_id"] for s in await db.fetchall("SELECT session_id FROM memory_sources")}


async def test_a_consolidation_naming_what_its_notes_never_said_is_refused(app: Any, manager: SessionManager, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(synthesis, "CAP_TOPIC", 2)
    await _seed(app, manager, "garden", [("Grows tomatoes", "synthesis", 10), ("Keeps tomato plants in a greenhouse", "synthesis", 5)])
    await chat(manager, "c", [("operator", "I grow peppers now."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/garden", "text": "Grows peppers"}]},
         consolidated=[{"notes": [{"from": ["n1", "n2"], "text": "Grows tomatoes in greenhouse GH-7"}]}])
    report = await sweep(app)
    assert report["dropped"].get("consolidate_refused") == 1 and report["consolidated"] == 0
    assert report["added"] == 1 and report["archived"] == 1  # what the consolidation could not do, archiving did


async def test_a_consolidation_too_long_to_keep_whole_is_left_undone(app: Any, manager: SessionManager, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(synthesis, "CAP_TOPIC", 2)
    await _seed(app, manager, "garden", [("Grows tomatoes", "synthesis", 10), ("Keeps tomato plants in a greenhouse", "synthesis", 5)])
    await chat(manager, "c", [("operator", "I grow peppers now."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/garden", "text": "Grows peppers"}]},
         consolidated=[{"notes": [{"from": ["n1", "n2"], "text": "Grows tomatoes in a greenhouse " * 12}]}])
    report = await sweep(app)
    assert report["dropped"].get("consolidate_long") == 1 and report["consolidated"] == 0
    assert report["added"] == 1 and report["archived"] == 1  # what the consolidation could not do, archiving did


async def test_one_chat_adds_at_most_its_share_and_the_rest_wait_with_the_true_reason(app: Any, client: httpx.AsyncClient, manager: SessionManager, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(synthesis, "CAP_TOPIC", 4)
    await chat(manager, "c", [("operator", "I keep bees, ducks and goats."), ("operator", "ok")])
    fake(manager, {"ops": [
        {"op": "add", "entry": "topic/farm", "text": "Keeps bees"}, {"op": "add", "entry": "topic/farm", "text": "Keeps ducks"},
        {"op": "add", "entry": "topic/farm", "text": "Keeps goats"},
    ]})
    report = await sweep(app)
    assert report["added"] == 2 and report["held"] == 1 and report["archived"] == 0
    held = (await client.get("/api/memory/suggestions", headers=H)).json()["suggestions"]
    assert [(s["why"], s["text"]) for s in held] == [("chat_full", "Keeps goats")]
    assert "this chat already added 2 notes to this entry" in held[0]["reason"]


# -- the tidying -----------------------------------------------------------------------------------


def _record(record_id: str, text: str, *, archived: bool = False, scope: MemoryScope = MemoryScope.global_) -> MemoryRecord:
    at = datetime(2026, 10, 1, tzinfo=UTC)
    return MemoryRecord(id=record_id, tenant_id=TENANT, scope=scope, scope_key="", text=text, kind="fact", metadata={ARCHIVED_KEY: True} if archived else {}, created_at=at, updated_at=at)


def test_the_tidying_never_pairs_two_archived_notes() -> None:
    assert find([_record("a", "Keeps bees in the garden", archived=True), _record("b", "Keeps bees in the garden", archived=True)]) == []
    assert len(find([_record("a", "Keeps bees in the garden", archived=True), _record("b", "Keeps bees in the garden")])) == 1


async def test_a_pair_filed_while_unsure_is_merged_once_it_becomes_sure(app: Any, manager: SessionManager, db: Database) -> None:
    entries = manager.memory_entries
    topic = await entries.ensure(synthesis.Target("topic", "bees", "Bees"))
    for _ in range(2):
        await entries.remember(MemoryScope.global_, "", "Keeps three hives", kind="fact", metadata={"topics": []}, origin="migration", entry=topic, similarity_threshold=1.1)
    a, b = manager.memory.records(TENANT)
    print_ = __import__("daedalus.memory.curator", fromlist=["_fingerprint"])._fingerprint("duplicate", [a, b])
    await db.execute(
        "INSERT INTO memory_suggestions(tenant_id, kind, memory_ids, fingerprint, keep_id, proposed_text, score, reason, status, created_at) VALUES (?, 'duplicate', ?, ?, ?, ?, 1, 'same text', 'open', ?)",
        (TENANT, json.dumps([a.id, b.id]), print_, a.id, a.text, datetime.now(UTC).isoformat()),
    )
    found = await curator(app).consolidate(force=True)
    assert found["merged"] == 1 and len(manager.memory.records(TENANT)) == 1
    assert (await db.fetchone("SELECT status FROM memory_suggestions"))["status"] == "applied"


async def _tidy_world(manager: SessionManager, db: Database) -> dict[str, str]:
    entries = manager.memory_entries
    deploy = await entries.ensure(synthesis.Target("topic", "deploy", "Deploy"))
    recent = await entries.ensure(synthesis.Target("topic", "recent-work", "Recent work"))
    ids: dict[str, str] = {}

    async def note(name: str, text: str, entry: Entry, origin: str = "synthesis", session: str = "s-1") -> None:
        result = await entries.remember(MemoryScope.global_, "", text, kind="fact", metadata={"topics": []}, origin=origin, session_id=session if origin == "synthesis" else "", entry=entry, similarity_threshold=1.1)
        ids[name] = result.record.id

    await note("keep", "Deploys go through deploy.sh on Fridays", deploy, session="s-1")
    await note("twin", "deploys go through deploy.sh on Fridays only", deploy, session="s-2")
    await note("folded", "Deploys go through deploy.sh, on Fridays.", deploy, session="s-3")
    await note("lost", "Staging lives on a second box behind the office router", deploy, session="s-4")
    await note("probe", "The secret word is MARIGOLD.", recent, origin="migration")
    for name in ("folded", "lost"):
        record = manager.memory.get_record(TENANT, ids[name])
        await manager.memory.restore(record.model_copy(update={"metadata": {**record.metadata, ARCHIVED_KEY: True}, "version": record.version + 1}))
    for text in ("The shop sheet comes from SHOP_SHEET_ID", "the shop sheet comes from SHOP_SHEET_ID.", "Deploys go through deploy.sh on Fridays"):
        await db.execute(
            "INSERT INTO memory_suggestions(tenant_id, kind, memory_ids, fingerprint, keep_id, proposed_text, score, reason, status, created_at, detail)"
            " VALUES (?, 'addition', '[]', ?, '', ?, 0, '', 'open', ?, ?)",
            (TENANT, fingerprint(text) + text[:3], text, datetime.now(UTC).isoformat(), json.dumps({"entry_key": "topic/deploy", "action": "add"})),
        )
    return ids


async def test_the_tidy_up_shows_a_dry_run_then_does_it_as_one_change_the_history_undoes(app: Any, client: httpx.AsyncClient, manager: SessionManager, db: Database) -> None:
    ids = await _tidy_world(manager, db)
    fake(manager)
    records = {r.id: (r.text, r.version, bool(r.metadata.get(ARCHIVED_KEY))) for r in manager.memory.records(TENANT)}
    sources = await db.fetchall("SELECT record_id, origin, session_id, from_seq FROM memory_sources ORDER BY 1, 2, 3")
    preview = (await client.post("/api/memory/tidy", json={"dry_run": True}, headers=H)).json()
    assert preview["dry_run"] and preview["history_id"] is None
    assert (preview["merged"], preview["folded"], preview["probes"], preview["closed"], preview["restored"]) == (1, 1, 1, 2, 1)
    assert [p["text"] for p in preview["probe_list"]] == ["The secret word is MARIGOLD."] and preview["emptied_entries"] == 1
    assert {r.id: (r.text, r.version, bool(r.metadata.get(ARCHIVED_KEY))) for r in manager.memory.records(TENANT)} == records
    done = (await client.post("/api/memory/tidy", json={"dry_run": False}, headers=H)).json()
    assert done["history_id"] and done["merged"] == 1
    left = {r.id: r for r in manager.memory.records(TENANT)}
    survivor = next(i for i in left if i != ids["lost"])
    # One of the pair stays (the more recently confirmed of two one-chat notes), with the longer words
    # and every source of the three; the distinct archived note is back.
    assert len(left) == 2 and survivor in {ids["keep"], ids["twin"]} and not left[ids["lost"]].metadata.get(ARCHIVED_KEY)
    assert left[survivor].text == "deploys go through deploy.sh on Fridays only"
    assert {r["session_id"] for r in await db.fetchall("SELECT session_id FROM memory_sources WHERE record_id = ?", (survivor,))} == {"s-1", "s-2", "s-3"}
    assert await manager.memory_entries.by_slug("topic", "recent-work") is None
    assert [r["status"] for r in await db.fetchall("SELECT status FROM memory_suggestions ORDER BY id")] == ["open", "withdrawn", "withdrawn"]
    history = await db.fetchone("SELECT action, summary FROM memory_history ORDER BY id DESC LIMIT 1")
    assert history["action"] == "tidy" and "1 merged" in history["summary"]
    undone = await client.post(f"/api/memory/history/{done['history_id']}/undo", headers=H)
    assert undone.status_code == 200
    assert {r.id: (r.text, r.version, bool(r.metadata.get(ARCHIVED_KEY))) for r in manager.memory.records(TENANT)} == records
    assert await db.fetchall("SELECT record_id, origin, session_id, from_seq FROM memory_sources ORDER BY 1, 2, 3") == sources
    assert [r["status"] for r in await db.fetchall("SELECT status FROM memory_suggestions ORDER BY id")] == ["open", "open", "open"]
    assert await manager.memory_entries.by_slug("topic", "recent-work") is not None
    assert not await manager.memory_entries.tombstoned("The secret word is MARIGOLD.")


async def test_the_tidy_up_waits_while_a_pass_reads(app: Any, client: httpx.AsyncClient, manager: SessionManager) -> None:
    learning = synthesizer(app)
    async with learning.lock:
        refused = await client.post("/api/memory/tidy", json={"dry_run": True}, headers=H)
    assert refused.status_code == 409


async def test_the_real_run_carries_out_the_plan_the_dry_run_showed_while_memory_is_unchanged(app: Any, client: httpx.AsyncClient, manager: SessionManager, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    await _tidy_world(manager, db)
    fake(manager)
    planned: list[int] = []
    real_plan = tidy.plan

    async def counted(*args: Any, **kwargs: Any) -> tidy.Plan:
        planned.append(1)
        return await real_plan(*args, **kwargs)

    monkeypatch.setattr(tidy, "plan", counted)
    await client.post("/api/memory/tidy", json={"dry_run": True}, headers=H)
    await client.post("/api/memory/tidy", json={"dry_run": False}, headers=H)
    assert len(planned) == 1
    # Memory changed since a dry run: the real run plans again.
    await client.post("/api/memory/tidy", json={"dry_run": True}, headers=H)
    entry = await manager.memory_entries.by_slug("topic", "deploy")
    await curator(app).add("Rollbacks take one command", kind="fact", topics=[], entry=entry)
    await client.post("/api/memory/tidy", json={"dry_run": False}, headers=H)
    assert len(planned) == 3


def test_only_a_legacy_note_of_a_probe_shape_is_a_probe() -> None:
    probe = _record("p", "The per-run cap is called run-cap and lives in config.toml.")
    assert tidy.is_probe(probe, {"migration"})
    assert not tidy.is_probe(probe, {"migration", "synthesis"})
    assert not tidy.is_probe(_record("q", "Deploys go through deploy.sh on Fridays"), {"migration"})


# -- what a chat is shown of a big area ------------------------------------------------------------


def _area(n: int) -> tuple[list[Entry], dict[str, list[MemoryRecord]]]:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    entries = [Entry(id="e-a", section="area", slug="shop", title="Shop", project_id="p-shop")]
    notes = [MemoryRecord(id=f"a{i:02d}", tenant_id=TENANT, scope=MemoryScope.project, scope_key="p-shop", text=f"Shop rule number {i}", kind="project",
                          metadata={"entry": "e-a"}, created_at=start + timedelta(minutes=i), updated_at=start + timedelta(minutes=i)) for i in range(n)]
    notes[0] = notes[0].model_copy(update={"text": "Payments go through the gateway's sandbox first"})
    return entries, {"e-a": notes}


def test_a_big_area_shows_what_the_chat_is_about_then_the_most_confirmed_and_the_same_bytes_each_time() -> None:
    entries, records = _area(30)
    plain = render.render(entries, records, project_id="p-shop")
    assert "Payments go through" not in plain.text and "Shop rule number 29" in plain.text  # newest, without a focus
    focus = render.Focus(hint="Fix the payments gateway", confirmations={"a01": 3})
    chosen = render.render(entries, records, project_id="p-shop", focus=focus)
    assert "Payments go through the gateway's sandbox first" in chosen.text and "Shop rule number 1\n" in chosen.text + "\n"
    assert "Shop rule number 10\n" not in chosen.text + "\n"
    assert render.render(entries, records, project_id="p-shop", focus=focus).text == chosen.text
    # The chat's title changed after its first run: what it holds stays, so nothing is told again.
    held = frozenset(line.hash for line in chosen.shown)
    retitled = render.render(entries, records, project_id="p-shop", focus=render.Focus(hint="Something else entirely", held=held, pinned_at="2026-10-02T00:00:00+00:00"))
    assert retitled.text == chosen.text
    # A note written after the block was pinned comes in.
    later = records["e-a"] + [records["e-a"][5].model_copy(update={"id": "new", "text": "A rule from today", "created_at": datetime(2026, 10, 3, tzinfo=UTC)})]
    grown = render.render(entries, {"e-a": later}, project_id="p-shop", focus=render.Focus(hint="Something else entirely", held=held, pinned_at="2026-10-02T00:00:00+00:00"))
    assert "A rule from today" in grown.text
