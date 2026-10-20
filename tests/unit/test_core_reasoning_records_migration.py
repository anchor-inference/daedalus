"""The migration that gives stored opaque reasoning the core's record format: a turn written through the
Codex route keeps its encrypted reasoning, and the next request of its session still sends it back."""

from __future__ import annotations

import json
from pathlib import Path

from protocore.contracts.types import Message
from protocore.providers.openai_compat.wire import REASONING_ENVELOPE, replayed_details

from daedalus.stores.core_reasoning_records import rewritten
from daedalus.stores.database import MIGRATIONS, Database, DataMigration, _core_reasoning_records

BEFORE = next(index for index, migration in enumerate(MIGRATIONS) if isinstance(migration, DataMigration) and migration.step is _core_reasoning_records)
AT = "2026-10-01T00:00:00+00:00"

DETAILS = [{"type": "reasoning.encrypted", "format": "openai-responses-v1", "index": 0, "data": "ZW5jcnlwdGVk"}]
OLD_TURN = {
    "role": "assistant",
    "content_blocks": [{"kind": "text", "text": "done"}],
    "reasoning_items": [{"format": "chat.reasoning_details", "endpoint": "codex", "model": "gpt-6-sol", "details": DETAILS, "tokens": 5}],
    "metadata": {},
}
QUOTED = {"role": "tool", "content_blocks": [{"kind": "tool_result", "tool_call_id": "c1", "content": "grep: chat.reasoning_details in openai_compat.py"}], "metadata": {}}


def test_a_record_of_the_old_format_is_sent_back_once_renamed() -> None:
    assert replayed_details(Message.model_validate(OLD_TURN), "codex", "gpt-6-sol") == [], "the core sends only its own format"
    turn, changed = rewritten(OLD_TURN)
    assert changed and turn["reasoning_items"][0]["format"] == REASONING_ENVELOPE
    assert replayed_details(Message.model_validate(turn), "codex", "gpt-6-sol") == DETAILS
    assert rewritten(QUOTED) == (QUOTED, False), "the words in a tool's output are data"
    assert rewritten(turn) == (turn, False)


async def test_every_stored_turn_is_renamed_once_and_nothing_else_moves(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    db = Database(path, local_env="container")
    await db.open()
    try:
        for key, message in (("k-turn", OLD_TURN), ("k-quote", QUOTED)):
            await db.execute("INSERT INTO session_messages(session_id, tenant_id, gen, key, message) VALUES ('s1', 't', 1, ?, ?)", (key, json.dumps(message)))
            await db.execute("INSERT INTO transcript(session_id, key, message) VALUES ('s1', ?, ?)", (key, json.dumps(message)))
        await db.execute("INSERT INTO snapshots(run_id, tenant_id, session_id, state, snapshot, updated_at) VALUES ('r1', 't', 's1', 'running', ?, ?)",
                         (json.dumps({"schema_version": 7, "history": [OLD_TURN]}), AT))
        await db.execute("UPDATE schema_version SET version = ?", (BEFORE,))
    finally:
        await db.close()

    db = Database(path, local_env="container")
    await db.open()
    try:
        assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
        for table in ("session_messages", "transcript"):
            rows = {r["key"]: json.loads(r["message"]) for r in await db.fetchall(f"SELECT key, message FROM {table}")}  # noqa: S608
            assert replayed_details(Message.model_validate(rows["k-turn"]), "codex", "gpt-6-sol") == DETAILS
            assert rows["k-quote"] == QUOTED
        stored = json.loads((await db.fetchone("SELECT snapshot FROM snapshots WHERE run_id = 'r1'"))["snapshot"])
        assert stored["history"][0]["reasoning_items"][0]["format"] == REASONING_ENVELOPE
        assert [r[0] for r in await db.fetchall("PRAGMA integrity_check")] == ["ok"]
        assert await db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        await db.close()
    again = Database(path, local_env="container")
    await again.open()
    await again.close()
