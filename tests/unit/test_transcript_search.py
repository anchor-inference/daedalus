"""Search over a contentless index: the hit names a transcript row, the row supplies the text."""

from __future__ import annotations

from protocore.contracts.types import Message, MessageRole, Session, TextBlock, ToolResultBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.sqlite import SqliteSessionStore, snippet

TENANT = "daedalus"


async def _seeded(db: Database) -> SqliteSessionStore:
    store = SqliteSessionStore(db)
    for sid in ("s1", "s2"):
        await store.create(Session(id=sid, tenant_id=TENANT, title=f"title {sid}"))
    await store.append_transcript(
        "s1",
        [
            Message(role=MessageRole.user, content_blocks=[TextBlock(text="please deploy config.toml to the staging box")]),
            Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Deployed; the port is 8765")]),
            Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="exit_code=0 rsync finished " + "filler " * 200)]),
        ],
    )
    await store.append_transcript("s2", [Message(role=MessageRole.user, content_blocks=[TextBlock(text="rsync somewhere else entirely")])])
    return store


async def test_text_is_still_found_and_the_hit_carries_its_row(db: Database) -> None:
    store = await _seeded(db)
    hits = await store.search_transcript("config.toml", session_id="s1")
    assert hits and hits[0]["role"] == "user" and hits[0]["session_id"] == "s1"
    assert "config.toml" in hits[0]["snippet"]
    assert isinstance(hits[0]["seq"], int)
    rows = await store.expand_transcript("s1", hits[0]["seq"], hits[0]["seq"])
    assert rows and "config.toml" in rows[0][1].content_blocks[0].text  # type: ignore[union-attr]


async def test_a_search_is_confined_to_its_session_unless_asked_otherwise(db: Database) -> None:
    store = await _seeded(db)
    assert [h["session_id"] for h in await store.search_transcript("rsync", session_id="s1")] == ["s1"]
    assert [h["session_id"] for h in await store.search_transcript("staging", session_id="s2")] == []
    everywhere = {h["session_id"] for h in await store.search_transcript("rsync", session_id=None)}
    assert everywhere == {"s1", "s2"}
    assert all(h["title"] for h in await store.search_transcript("rsync", session_id=None))


async def test_a_snippet_is_a_window_around_the_match(db: Database) -> None:
    store = await _seeded(db)
    hit = (await store.search_transcript("rsync", session_id="s1"))[0]
    assert "[rsync]" in hit["snippet"] and len(hit["snippet"]) < 250
    assert snippet("nothing to see", "absent") == "nothing to see"
    assert snippet("", "x") == ""


async def test_the_index_is_rebuilt_from_the_transcript_and_deleting_a_session_takes_it_along(settings: Settings, db: Database) -> None:
    store = await _seeded(db)
    await db.execute("DELETE FROM transcript_fts")
    await db.execute("DELETE FROM kv WHERE key = 'transcript_fts_watermark'")
    assert await store.search_transcript("config.toml", session_id="s1") == []
    assert await store.backfill_transcript_index() == 4
    assert await store.search_transcript("config.toml", session_id="s1")
    assert await store.backfill_transcript_index() == 0  # a second pass indexes nothing again

    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    state = await manager.create_session("doomed")
    await manager.sessions.append_transcript(state.session.id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text="unmistakable zaphodbeeblebrox")])])
    assert await manager.sessions.search_transcript("zaphodbeeblebrox", session_id=None)
    await manager.delete_session(state.session.id)
    assert await manager.sessions.search_transcript("zaphodbeeblebrox", session_id=None) == []
    assert (await db.fetchone("SELECT count(*) c FROM transcript_fts"))["c"] == 4  # only the seeded rows are left
    await manager.close()


def test_a_snippet_finds_its_word_after_letters_that_lengthen_when_lowered() -> None:
    # "İ".lower() is two characters, so an offset found in the lowered text pointed past the word in
    # the original, and the window was cut from somewhere after it.
    text = "İ" * 100 + " needle " + "x " * 200
    assert "[needle]" in snippet(text, "needle")
