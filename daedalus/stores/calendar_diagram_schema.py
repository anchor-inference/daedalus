"""Reconcile calendar and diagram tables across the two schema histories."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from daedalus.stores.database import Database


def migration(db: Database) -> str:
    """Keep existing calendar rows when a database already received these tables."""
    conn = sqlite3.connect(db.path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        has_share_token = any(row[1] == "share_token" for row in conn.execute("PRAGMA table_info(diagrams)"))
    finally:
        conn.close()
    share_column = "" if has_share_token else "ALTER TABLE diagrams ADD COLUMN share_token TEXT NOT NULL DEFAULT '';"
    return f"""
CREATE TABLE IF NOT EXISTS calendar_accounts (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK (provider IN ('google', 'outlook', 'yandex')),
    name TEXT NOT NULL,
    credentials_json TEXT NOT NULL,
    remote_calendar_id TEXT NOT NULL DEFAULT 'primary',
    cursor TEXT NOT NULL DEFAULT '',
    last_sync_at TEXT,
    sync_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_events (
    id TEXT PRIMARY KEY,
    account_id TEXT REFERENCES calendar_accounts(id) ON DELETE CASCADE,
    remote_id TEXT,
    remote_uid TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    all_day INTEGER NOT NULL DEFAULT 0,
    location TEXT NOT NULL DEFAULT '',
    recurrence TEXT NOT NULL DEFAULT '',
    dirty TEXT NOT NULL DEFAULT 'create' CHECK (dirty IN ('', 'create', 'update', 'delete')),
    etag TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, remote_id)
);
CREATE INDEX IF NOT EXISTS calendar_events_range ON calendar_events(start_at, end_at);
CREATE INDEX IF NOT EXISTS calendar_events_pending ON calendar_events(account_id, dirty);
CREATE TABLE IF NOT EXISTS diagrams (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    scene_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
{share_column}
CREATE UNIQUE INDEX IF NOT EXISTS diagrams_share_token ON diagrams(share_token) WHERE share_token != '';
CREATE TABLE IF NOT EXISTS diagram_revisions (
    diagram_id TEXT NOT NULL REFERENCES diagrams(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    scene_json TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    PRIMARY KEY (diagram_id, version)
);
INSERT OR IGNORE INTO diagram_revisions(diagram_id,version,title,scene_json,saved_at)
SELECT id,version,title,scene_json,updated_at FROM diagrams;
"""
