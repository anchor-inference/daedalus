"""Record where an imported private workspace came from.

The digest is unique so a second command cannot silently fork the same historical workspace.
"""

MIGRATION = """
CREATE TABLE workspace_archive_imports (
    archive_digest TEXT PRIMARY KEY,
    format_version INTEGER NOT NULL,
    source_project_id TEXT NOT NULL,
    project_id TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
    row_counts_json TEXT NOT NULL CHECK (json_valid(row_counts_json)),
    imported_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    operation_receipt_id TEXT NOT NULL REFERENCES operation_receipts(id) DEFERRABLE INITIALLY DEFERRED
);
"""
