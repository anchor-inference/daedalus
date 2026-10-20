"""The migration that renames stored pictures the way the core reads them: a history, a transcript, a
paused run's snapshot and the queues keep every row, and only the names of their images change."""

from __future__ import annotations

import json
from pathlib import Path

from protocore.contracts.types import IMAGE_REFS_METADATA_KEY, Message, MessageRole, TextBlock, message_image_refs

from daedalus.stores.core_image_keys import rewritten
from daedalus.stores.database import MIGRATIONS, Database, DataMigration, _core_image_keys

BEFORE = next(index for index, migration in enumerate(MIGRATIONS) if isinstance(migration, DataMigration) and migration.step is _core_image_keys)
"""The schema this migration upgrades from, found by what it does rather than by its number."""

AT = "2026-10-01T00:00:00+00:00"

OLD_USER = {
    "role": "user",
    "content_blocks": [{"kind": "text", "text": "what is on this board?"}],
    "metadata": {"daedalus.origin": "operator", "image_refs": [{"ref": "blob-a", "mime": "image/jpeg"}, {"ref": "blob-b"}]},
}
OLD_TOOL_TEXT = {
    "role": "tool",
    "content_blocks": [{"kind": "tool_result", "tool_call_id": "c1", "content": 'grep found "image_refs": [] in wire.py'}],
    "metadata": {},
}
OLD_QUEUE_ITEM = {"id": "q1", "kind": "follow_up", "text": "and this", "attachments": [{"name": "shot.png", "mime_type": "image/png", "image_ref": "blob-c"}]}


def test_only_the_two_places_the_host_wrote_are_renamed() -> None:
    message, changed = rewritten(OLD_USER)
    assert changed and "image_refs" not in message["metadata"]
    images = message_image_refs(Message.model_validate(message))
    assert [(image.blob_ref, image.mime_type) for image in images] == [("blob-a", "image/jpeg"), ("blob-b", "image/png")]
    assert rewritten(OLD_TOOL_TEXT) == (OLD_TOOL_TEXT, False), "the words inside a tool's output are data"
    item, changed = rewritten(OLD_QUEUE_ITEM)
    assert changed and item["attachments"] == [{"name": "shot.png", "mime_type": "image/png", "blob_ref": "blob-c"}]
    again, changed = rewritten(item)
    assert again == item and not changed


async def test_every_stored_copy_is_renamed_once_and_nothing_else_moves(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    db = Database(path, local_env="container")
    await db.open()
    try:
        for key, message in (("k-user", OLD_USER), ("k-tool", OLD_TOOL_TEXT)):
            await db.execute("INSERT INTO session_messages(session_id, tenant_id, gen, key, message) VALUES ('s1', 't', 1, ?, ?)", (key, json.dumps(message)))
            await db.execute("INSERT INTO transcript(session_id, key, message) VALUES ('s1', ?, ?)", (key, json.dumps(message)))
        snapshot = {"schema_version": 7, "history": [OLD_USER, OLD_TOOL_TEXT], "steer_queue": [], "follow_up_queue": [OLD_QUEUE_ITEM]}
        await db.execute("INSERT INTO snapshots(run_id, tenant_id, session_id, state, snapshot, updated_at) VALUES ('r1', 't', 's1', 'running', ?, ?)", (json.dumps(snapshot), AT))
        await db.execute("INSERT INTO live_control(session_id, steer_queue, follow_up_queue, updated_at) VALUES ('s1', '[]', ?, ?)", (json.dumps([OLD_QUEUE_ITEM]), AT))
        await db.execute("UPDATE schema_version SET version = ?", (BEFORE,))
    finally:
        await db.close()

    db = Database(path, local_env="container")
    await db.open()
    try:
        assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
        for table in ("session_messages", "transcript"):
            rows = {r["key"]: json.loads(r["message"]) for r in await db.fetchall(f"SELECT key, message FROM {table}")}  # noqa: S608
            assert set(rows) == {"k-user", "k-tool"}
            user = Message.model_validate(rows["k-user"])
            assert [image.blob_ref for image in message_image_refs(user)] == ["blob-a", "blob-b"]
            assert user.metadata["daedalus.origin"] == "operator" and "image_refs" not in user.metadata
            assert rows["k-tool"] == OLD_TOOL_TEXT
        stored = json.loads((await db.fetchone("SELECT snapshot FROM snapshots WHERE run_id = 'r1'"))["snapshot"])
        assert stored["history"][0]["metadata"][IMAGE_REFS_METADATA_KEY][0]["blob_ref"] == "blob-a"
        assert stored["follow_up_queue"][0]["attachments"][0]["blob_ref"] == "blob-c"
        queue = json.loads((await db.fetchone("SELECT follow_up_queue FROM live_control WHERE session_id = 's1'"))["follow_up_queue"])
        assert queue[0]["attachments"] == [{"name": "shot.png", "mime_type": "image/png", "blob_ref": "blob-c"}]
        assert [r[0] for r in await db.fetchall("PRAGMA integrity_check")] == ["ok"]
        assert await db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        await db.close()
    # Opening again runs nothing twice.
    again = Database(path, local_env="container")
    await again.open()
    await again.close()


def test_a_message_the_host_writes_now_is_read_by_the_core() -> None:
    from daedalus.host.session_runner import _image_refs_metadata

    message = Message(role=MessageRole.user, content_blocks=[TextBlock(text="look")], metadata=_image_refs_metadata([("blob-d", "image/webp")]))
    assert [(image.blob_ref, image.mime_type) for image in message_image_refs(message)] == [("blob-d", "image/webp")]
