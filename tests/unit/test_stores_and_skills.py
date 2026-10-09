from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from protocore.contracts.memory import MemoryScope
from protocore.contracts.skills import SkillNotFoundError, SkillUpsertInput
from protocore.contracts.types import Event, Message, MessageRole, Run, RunStatus, Session, TextBlock

from daedalus.extensions.api import validate_init_data
from daedalus.host.skills import DirectorySkillStore
from daedalus.stores.database import Database
from daedalus.stores.persistent import PersistentMemory
from daedalus.stores.sqlite import (
    LiveControlStore,
    ReceiptConflict,
    SqliteEventStream,
    SqliteRunStore,
    SqliteSessionStore,
)


async def test_session_and_run_stores_roundtrip(db: Database) -> None:
    sessions = SqliteSessionStore(db)
    runs = SqliteRunStore(db)
    await sessions.create(Session(id="s1", tenant_id="t", title="one"))
    await sessions.append_message("s1", "t", Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")]))
    assert (await sessions.get("s1", "t")).title == "one"
    assert (await sessions.list_messages("s1", "t"))[0].content_blocks[0].text == "hi"
    await runs.create(Run(id="r1", tenant_id="t", session_id="s1", status=RunStatus.running))
    await runs.update_status("r1", "t", RunStatus.completed)
    assert (await runs.get("r1", "t")).status is RunStatus.completed


async def test_event_stream_keeps_latest_snapshot_apart_from_log(db: Database) -> None:
    stream = SqliteEventStream(db)
    stream.bind_run("r1", "s1")
    await stream.emit(Event(run_id="r1", name="state_snapshot", payload={"tenant_id": "t", "snapshot": {"state": "running", "x": 1}}))
    await stream.emit(Event(run_id="r1", name="state_snapshot", payload={"tenant_id": "t", "snapshot": {"state": "running", "x": 2}}))
    await stream.emit(Event(run_id="r1", name="tool_result", payload={"tenant_id": "t"}))
    assert (await stream.load_snapshot("r1"))["x"] == 2
    assert [name for _, e in await stream.list_events("r1") for name in [e.name]] == ["tool_result"]
    unfinished = await stream.unfinished_snapshots()
    assert unfinished[0]["session_id"] == "s1"


async def test_session_event_cursor_replays_through_an_atomic_watermark(db: Database) -> None:
    sessions = SqliteSessionStore(db)
    await sessions.create(Session(id="cursor", tenant_id="t", title="cursor"))
    stream = SqliteEventStream(db)
    first = await stream.publish_session(
        "cursor",
        SimpleNamespace(type=SimpleNamespace(value="message_start"), run_id="r1", payload={"model": "m"}),
    )
    cut = await stream.publish_session(
        "cursor",
        SimpleNamespace(type=SimpleNamespace(value="history_cut"), run_id="r1", payload={"through": 4}),
        history_rewrite=True,
    )
    assert (first["event_seq"], first["history_revision"]) == (1, 0)
    assert (cut["event_seq"], cut["history_revision"]) == (2, 1)
    replay = await stream.session_replay("cursor", after=0, through=2)
    assert [(item["event_seq"], item["kind"]) for item in replay["events"]] == [
        (1, "message_start"),
        (2, "history_cut"),
    ]
    assert replay["watermark"] == 2 and replay["history_revision"] == 1


async def test_trimmed_session_cursor_requires_resync(db: Database) -> None:
    sessions = SqliteSessionStore(db)
    await sessions.create(Session(id="trimmed", tenant_id="t", title="trimmed"))
    stream = SqliteEventStream(db)
    for index in range(4):
        await stream.publish_session(
            "trimmed",
            SimpleNamespace(type=SimpleNamespace(value="state_changed"), run_id="r", payload={"index": index}),
        )
    await stream.trim_session("trimmed", keep=2)
    replay = await stream.session_replay("trimmed", after=1)
    assert replay["resync_required"] is True
    assert replay["reason"] == "cursor_trimmed"


async def test_live_control_queues_and_overrides(db: Database) -> None:
    live = LiveControlStore(db)
    await live.enqueue("s", "follow_up", {"id": "q1", "kind": "follow_up", "text": "more", "attachments": []})
    await live.set_model("s", model_name="m2", thinking_enabled=False)
    state = await live.load("s")
    assert state["follow_up"][0]["text"] == "more" and state["model_name"] == "m2" and state["thinking_enabled"] is False


async def test_input_receipt_deduplicates_queue_after_lost_ack(db: Database) -> None:
    sessions = SqliteSessionStore(db)
    await sessions.create(Session(id="receipt", tenant_id="t", title="receipt"))
    live = LiveControlStore(db)
    item = {"id": "client-1", "kind": "steer", "text": "more", "attachments": []}
    first, created = await live.accept("receipt", "client-1", "steer", {"text": "more"}, queue_item=item)
    repeated, created_again = await live.accept("receipt", "client-1", "steer", {"text": "more"}, queue_item=item)
    assert created is True and created_again is False
    assert repeated == first and first["status"] == "queued"
    state = await live.load("receipt")
    assert [entry["id"] for entry in state["steer"]] == ["client-1"]
    assert state["queue_revision"] == 1
    with pytest.raises(ReceiptConflict):
        await live.accept("receipt", "client-1", "steer", {"text": "different"}, queue_item=item)


async def test_input_receipt_withdraw_loses_race_to_consumed(db: Database) -> None:
    sessions = SqliteSessionStore(db)
    await sessions.create(Session(id="race", tenant_id="t", title="race"))
    live = LiveControlStore(db)
    item = {"id": "client-2", "kind": "steer", "text": "more", "attachments": []}
    await live.accept("race", "client-2", "steer", {"text": "more"}, queue_item=item)
    consumed = await live.consume("race", "client-2", run_id="run", step_id="step", message_seq=7)
    returned = await live.withdraw("race", "client-2")
    assert consumed["status"] == "consumed" and returned == consumed


async def test_persistent_memory_survives_reload(db: Database) -> None:
    memory = PersistentMemory(db)
    await memory.load()
    result = await memory.write("t", MemoryScope.user, "owner", "the operator likes tea", kind="fact")
    fresh = PersistentMemory(db)
    await fresh.load()
    hits = await fresh.recall("t", "tea", scopes=[MemoryScope.user], scope_keys={MemoryScope.user: "owner"})
    assert hits and hits[0].record.id == result.record.id


async def test_directory_skill_store(tmp_path: Path) -> None:
    store = DirectorySkillStore(tmp_path / "skills")
    entry = await store.create("t", SkillUpsertInput(name="demo", description="a demo", body_md="# Body\ntext"))
    assert [e.name for e in await store.list("t")] == ["demo"]
    bundle = await store.load("t", entry.id)
    assert bundle.body.strip().startswith("# Body")
    (tmp_path / "skills" / entry.id / "extra.txt").write_text("x")
    files = await store.list_files("t", entry.id)
    assert {f.path for f in files} == {"SKILL.md", "extra.txt"}
    assert await store.load_file("t", entry.id, "extra.txt") == b"x"
    assert await store.load_file("t", entry.id, "../../etc/passwd") is None
    await store.set_enabled("t", entry.id, enabled=False)
    assert await store.list("t") == []
    assert [e.id for e in await store.list_subset("t", ["demo"])] == [entry.id]
    with pytest.raises(SkillNotFoundError):
        await store.load("t", entry.id)
    with pytest.raises(SkillNotFoundError):
        await store.load("t", "demo")


def _init_data(token: str, user_id: int, age: int = 0) -> str:
    fields = {"auth_date": str(int(time.time()) - age), "query_id": "q", "user": json.dumps({"id": user_id, "first_name": "A"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_init_data_validation() -> None:
    data = validate_init_data(_init_data("123:abc", 42), "123:abc")
    assert data["user"]["id"] == 42
    with pytest.raises(ValueError):
        validate_init_data(_init_data("123:abc", 42), "other")
    with pytest.raises(ValueError):
        validate_init_data(_init_data("123:abc", 42, age=100_000), "123:abc")


def test_config_with_optional_sections_round_trips_through_toml(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from daedalus.config import McpServerConfig, RuntimeConfig

    config = RuntimeConfig()
    config.mcp.servers["board"] = McpServerConfig(transport="http", url="https://example.test/mcp")
    path = tmp_path / "config.toml"
    config.save(path)  # an unset optional block must not break serialisation
    loaded = RuntimeConfig.load(path)
    assert loaded.mcp.servers["board"].url == "https://example.test/mcp"
    assert loaded.mcp.servers["board"].oauth is None


async def test_transcript_survives_history_rewrites(db) -> None:  # type: ignore[no-untyped-def]
    from protocore.contracts.types import Message, MessageRole, TextBlock

    from daedalus.stores.sqlite import SqliteSessionStore

    store = SqliteSessionStore(db)
    ask = Message(role=MessageRole.user, content_blocks=[TextBlock(text="the task")])
    reply = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="done")])
    assert await store.append_transcript("s1", [ask, reply]) == 2
    assert await store.append_transcript("s1", [ask, reply]) == 0  # idempotent
    summary = Message(role=MessageRole.user, content_blocks=[TextBlock(text="<compacted-turn>x</compacted-turn>")], metadata={"protocore.compaction_summary": True})
    await store.replace_messages("s1", "t", [summary])  # the model's history shrank
    await store.append_transcript("s1", [summary])
    texts = [m.content_blocks[0].text for m in await store.list_transcript("s1")]  # type: ignore[union-attr]
    assert texts == ["the task", "done", "<compacted-turn>x</compacted-turn>"]
    assert len(await store.list_transcript("s1", limit=2)) == 2


async def test_transcript_search_and_expand(db) -> None:  # type: ignore[no-untyped-def]
    from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResultBlock

    from daedalus.stores.sqlite import SqliteSessionStore, fts_query

    store = SqliteSessionStore(db)
    msgs = [
        Message(role=MessageRole.user, content_blocks=[TextBlock(text="please deploy config.toml to the staging box")]),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Deployed; the port is 8765 ⟦ deploy | status: completed; next: none | anchors: config.toml, 8765 ⟧")]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="exit_code=0 rsync finished")]),
    ]
    added = await store.append_transcript("s1", msgs)
    assert added == 3
    hits = await store.search_transcript("config.toml", session_id="s1")
    assert hits and hits[0]["role"] == "user" and "config.toml" in hits[0]["snippet"]
    assert await store.search_transcript("rsync", session_id="other") == []
    everywhere = await store.search_transcript("rsync", session_id=None)
    assert everywhere and everywhere[0]["session_id"] == "s1"
    rows = await store.expand_transcript("s1", hits[0]["seq"], hits[0]["seq"] + 1)
    assert [m.role.value for _, m in rows] == ["user", "assistant"]
    assert fts_query('"exact phrase"') == '"exact phrase"'
    assert fts_query("run-cap config.toml") == '"run-cap"* "config.toml"*'
    assert await store.backfill_transcript_index() == 0


def test_headline_split() -> None:
    from daedalus.host.prompts import split_headline

    body, head = split_headline("All done.\n\n⟦ deploy | status: completed; next: none | anchors: x ⟧")
    assert body == "All done." and head.startswith("⟦ deploy")
    assert split_headline("no headline here") == ("no headline here", "")


def test_headline_glued_to_the_last_sentence_is_split() -> None:
    """Models append the headline to the last paragraph despite the prompt; the operator saw it leak."""
    from daedalus.host.prompts import split_headline

    glued = "не пересылайте его посторонним. ⟦ проверка записи | status: проверено; next: нет | anchors: a.txt ⟧"
    body, head = split_headline(glued)
    assert body == "не пересылайте его посторонним." and head.startswith("⟦ проверка записи") and head.endswith("⟧")
    assert split_headline(glued + "\n")[0] == "не пересылайте его посторонним."
    # Mid-text, or a bracketed word without the format's separators, is the reader's text.
    quoted = "The line looks like ⟦ task | status: x | anchors: y ⟧ and is hidden from you."
    assert split_headline(quoted) == (quoted, "")
    assert split_headline("Press ⟦Enter⟧") == ("Press ⟦Enter⟧", "")
    # Half streamed, inline and on its own line.
    assert split_headline("The answer. ⟦ проверка | status: пров") == ("The answer.", "")
    assert split_headline("The answer.\n⟦ проверка | sta") == ("The answer.", "")
    # An unclosed bracket followed by more lines is not a headline being written.
    assert split_headline("a ⟦ b\nmore text") == ("a ⟦ b\nmore text", "")
