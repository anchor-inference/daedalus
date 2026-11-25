"""Standing approvals that belong to a project instead of one chat.

``IF NOT EXISTS``, since a migration after the chat-or-project rule is replayed over a finished database
by the test that rewinds that rule's number."""

MIGRATION = """
CREATE TABLE IF NOT EXISTS project_approval_rules (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    family TEXT NOT NULL,
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    via TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, id)
);
"""
