"""Durable, bounded fault identities for execution attempts."""

MIGRATION = """
CREATE TABLE attempt_faults (
    id INTEGER PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES execution_attempts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('adapter_error', 'renderer_error', 'launch_error', 'runtime_error', 'cancelled')),
    cancelled_by TEXT CHECK(cancelled_by IN ('operator', 'worker', 'system')),
    diagnostic_ref TEXT,
    redaction_version INTEGER NOT NULL CHECK(redaction_version > 0),
    created_at TEXT NOT NULL
);
CREATE INDEX attempt_faults_by_attempt ON attempt_faults(attempt_id, id DESC);
"""
