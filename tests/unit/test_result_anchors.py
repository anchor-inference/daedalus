"""Result anchors accept only turns the host can verify in the result's project."""

from __future__ import annotations

import sqlite3

import pytest

from daedalus.stores.database import Database
from daedalus.stores.result_anchor_schema import MIGRATION
from daedalus.stores.result_anchors import ResultAnchorRefused, add_result_turn_anchor, result_turn_refs


async def test_result_anchor_scopes_and_checks_exact_source_turn(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now'),('other','Other','now')")
        await db.execute(
            "INSERT INTO board_tasks(id,project_id,title,status,priority,acceptance,checklist,depends_on,created_at,updated_at,brief_json) "
            "VALUES ('task','project','Draft','review',3,'','[]','[]','now','now','{}')"
        )
        await db.execute(
            "INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,snapshot_json,created_at) "
            "VALUES ('task',1,'operator','{}','now')"
        )
        await db.execute(
            "INSERT INTO result_receipts(id,task_id,contract_revision,outcome,original_text,original_digest,original_size_bytes,actor_id,created_at) "
            "VALUES ('result','task',1,'complete','Report','digest',6,'operator:1','now')"
        )
        await db.execute(
            "INSERT INTO sessions(id,tenant_id,project_id,title,created_at,last_message_at) "
            "VALUES ('session','tenant','project','Work','now','now'),('foreign','tenant','other','Other','now','now')"
        )
        await db.execute(
            "INSERT INTO session_messages(seq,session_id,tenant_id,message) "
            "VALUES (101,'session','tenant','{\"text\":\"observed\"}'),(102,'foreign','tenant','{\"text\":\"foreign\"}')"
        )
        async with db.transaction() as conn:
            anchor = await add_result_turn_anchor(conn, "result", "session", 101)
        assert len(anchor["source_digest"]) == 64
        assert (await result_turn_refs(db, "task", "result"))[0]["source_current"] is True
        with pytest.raises(ResultAnchorRefused, match="share a project"):
            async with db.transaction() as conn:
                await add_result_turn_anchor(conn, "result", "foreign", 102)
        with pytest.raises(ResultAnchorRefused, match="does not exist"):
            async with db.transaction() as conn:
                await add_result_turn_anchor(conn, "result", "session", 999)
        await db.execute("UPDATE session_messages SET message = '{\"text\":\"changed\"}' WHERE seq = 101")
        assert (await result_turn_refs(db, "task", "result"))[0]["source_current"] is False
        with pytest.raises(KeyError):
            await result_turn_refs(db, "another-task", "result")
    finally:
        await db.close()


def test_anchor_upgrade_keeps_legacy_count_without_inventing_a_digest(tmp_path) -> None:
    with sqlite3.connect(tmp_path / "old.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            "CREATE TABLE sessions(id TEXT PRIMARY KEY);"
            "CREATE TABLE result_receipts(id TEXT PRIMARY KEY);"
            "CREATE TABLE result_turn_anchors("
            "result_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,"
            "turn_seq INTEGER NOT NULL, PRIMARY KEY(result_id,session_id,turn_seq));"
            "INSERT INTO sessions(id) VALUES ('session');"
            "INSERT INTO result_receipts(id) VALUES ('result');"
            "INSERT INTO result_turn_anchors(result_id,session_id,turn_seq) VALUES ('result','session',7);"
        )
        conn.executescript(MIGRATION)
        assert conn.execute("SELECT result_id,session_id,turn_seq,source_digest FROM result_turn_anchors").fetchall() == [
            ("result", "session", 7, "")
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
