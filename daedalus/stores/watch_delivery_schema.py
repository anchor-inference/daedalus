"""Keep standing-watch delivery intent after a watch is disabled or removed."""

WATCH_DELIVERY_MIGRATION = """
ALTER TABLE watches ADD COLUMN deadline_at TEXT;
CREATE TABLE watch_deliveries_rebuilt (
    id TEXT PRIMARY KEY,
    watch_id TEXT NOT NULL,
    condition_revision INTEGER NOT NULL CHECK (condition_revision > 0),
    source_cursor TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'reconciling', 'failed', 'cancelled')),
    receipt_id TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    action_json TEXT NOT NULL DEFAULT '{}',
    detail TEXT NOT NULL DEFAULT '',
    staff_id TEXT,
    project_id TEXT,
    deadline_at TEXT,
    claimed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO watch_deliveries_rebuilt
    (id,watch_id,condition_revision,source_cursor,dedup_key,status,receipt_id,project_id,created_at,updated_at)
SELECT d.id,d.watch_id,d.condition_revision,d.source_cursor,d.dedup_key,d.status,d.receipt_id,
       w.project_id,d.created_at,d.updated_at
FROM watch_deliveries AS d LEFT JOIN watches AS w ON w.id = d.watch_id;
DROP TABLE watch_deliveries;
ALTER TABLE watch_deliveries_rebuilt RENAME TO watch_deliveries;
CREATE INDEX watch_delivery_pending ON watch_deliveries(status,created_at);
CREATE INDEX watch_delivery_watch ON watch_deliveries(watch_id,condition_revision,created_at);
CREATE INDEX watch_delivery_event ON app_events(json_extract(payload_json,'$.delivery_id'))
    WHERE type = 'watch.fired';
"""
