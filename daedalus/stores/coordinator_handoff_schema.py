"""Durable preparation and retirement state for a project's coordinator replacement."""

MIGRATION = """
CREATE TABLE coordinator_handoffs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    client_operation_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    expected_entity_revision INTEGER NOT NULL,
    old_session_id TEXT NOT NULL,
    replacement_session_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('preparing','blocked','retirement_pending','completed')),
    readiness_digest TEXT,
    readiness_json TEXT,
    blocker TEXT,
    receipt_id TEXT,
    response_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id,actor_id,client_operation_id),
    UNIQUE(project_id,replacement_session_id)
);
CREATE INDEX coordinator_handoffs_recovery ON coordinator_handoffs(state,project_id);
"""
