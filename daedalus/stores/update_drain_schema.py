"""Keep admission closed until an update has an explicit durable outcome."""

MIGRATION = """
CREATE TABLE update_drains (
    id TEXT PRIMARY KEY,
    host_generation INTEGER NOT NULL CHECK(host_generation > 0),
    policy TEXT NOT NULL CHECK(policy IN ('checkpoint_supported','stop_all')),
    candidate_bot_sha TEXT NOT NULL,
    candidate_core_sha TEXT NOT NULL,
    candidate_compatibility_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('draining','blocked','ready','committed','resumed','aborted')),
    admission_closed_at TEXT NOT NULL,
    active_attempts_json TEXT NOT NULL CHECK(json_valid(active_attempts_json)),
    active_runs_json TEXT NOT NULL CHECK(json_valid(active_runs_json)),
    active_receipts_json TEXT NOT NULL CHECK(json_valid(active_receipts_json)),
    checkpoint_refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(checkpoint_refs_json)),
    blocker TEXT,
    receipt_id TEXT NOT NULL REFERENCES operation_receipts(id) DEFERRABLE INITIALLY DEFERRED,
    commit_receipt_id TEXT REFERENCES operation_receipts(id) DEFERRABLE INITIALLY DEFERRED,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX update_drains_one_active ON update_drains((1))
    WHERE state IN ('draining','blocked','ready','committed');
"""
