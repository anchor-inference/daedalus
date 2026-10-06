"""The redacted view is computed once, stored beside the row, and rebuilt when it goes stale."""

from __future__ import annotations

import re

from protocore.contracts.types import Message, MessageRole, Session, TextBlock, ToolResultBlock

from daedalus.host.transcript_view import TOOL_RESULT_PREVIEW_CHARS, TranscriptViewBuilder, message_view
from daedalus.security import redact
from daedalus.stores.database import Database
from daedalus.stores.sqlite import SqliteSessionStore

TENANT = "daedalus"


async def _store(db: Database, sid: str) -> SqliteSessionStore:
    store = SqliteSessionStore(db, view=TranscriptViewBuilder())
    await store.create(Session(id=sid, tenant_id=TENANT, title="t"))
    return store


async def test_the_view_is_written_with_the_row_and_read_back_without_the_message(db: Database) -> None:
    store = await _store(db, "v1")
    await store.append_transcript("v1", [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="hello")])])
    row = await db.fetchone("SELECT view, view_key FROM transcript WHERE session_id = 'v1'")
    assert row["view"] and row["view_key"] == TranscriptViewBuilder().key()
    views = await store.list_transcript_views("v1", limit=10)
    assert len(views) == 1 and views[0]["text"] == "hello" and isinstance(views[0]["seq"], int)
    assert views[0] == message_view((await store.list_transcript("v1"))[0])


async def test_operator_message_keeps_its_client_id_in_the_stored_view(db: Database) -> None:
    store = await _store(db, "message-receipt")
    await store.append_transcript("message-receipt", [Message(
        role=MessageRole.user,
        content_blocks=[TextBlock(text="Attached files:\n- sample.txt")],
        metadata={"daedalus.origin": "operator", "daedalus.client_message_id": "send-1"},
    )])
    view = (await store.list_transcript_views("message-receipt", limit=10))[0]
    assert view["client_message_id"] == "send-1"
    assert view["text"] == "Attached files:\n- sample.txt"


async def test_a_secret_configured_later_reaches_rows_written_before_it(db: Database) -> None:
    secret = "abcd1234efgh5678ijkl"
    store = await _store(db, "v2")
    shared = redact.shared()
    before_values = shared.values
    try:
        await store.append_transcript("v2", [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=f"the key is {secret}")])])
        assert secret in (await store.list_transcript_views("v2", limit=10))[0]["text"]
        stale_key = (await db.fetchone("SELECT view_key FROM transcript WHERE session_id = 'v2'"))["view_key"]
        shared.add_values([secret])
        views = await store.list_transcript_views("v2", limit=10)
        assert secret not in views[0]["text"] and redact.MASK in views[0]["text"]
        fresh = await db.fetchone("SELECT view, view_key FROM transcript WHERE session_id = 'v2'")
        assert fresh["view_key"] != stale_key and secret not in fresh["view"]  # the rebuilt view was written back
    finally:
        shared.replace_values(before_values)


async def test_a_listed_tool_result_carries_a_preview_and_says_how_long_it_is(db: Database) -> None:
    store = await _store(db, "v3")
    body = "x" * 9000
    await store.append_transcript("v3", [Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content=body)])])
    view = (await store.list_transcript_views("v3", limit=10))[0]
    assert len(view["tool_results"][0]["content"]) == TOOL_RESULT_PREVIEW_CHARS
    assert view["tool_results"][0]["length"] == len(body)
    assert TOOL_RESULT_PREVIEW_CHARS <= 1000  # a listing is a listing; the expand endpoint has the rest


def test_a_new_secret_shape_invalidates_the_stored_views_by_itself() -> None:
    """A stored view is what the app draws. Adding a provider's key format without remembering to
    bump a hand-written number left every view already stored showing the secret it was added for."""
    builder = TranscriptViewBuilder()
    before = builder.key()
    assert redact.shapes_digest() in before
    shapes = redact._SHAPES
    try:
        redact._SHAPES = (*shapes, ("a_new_provider", re.compile(r"\bnp-[a-z0-9]{32}\b")))
        assert builder.key() != before, "a view stored before the shape existed would still be served"
    finally:
        redact._SHAPES = shapes
    assert builder.key() == before
