"""Preserve imported project spending evidence without creating spend authority."""

MIGRATION = """
CREATE TABLE historical_goal_budget_snapshots (
    archive_digest TEXT PRIMARY KEY REFERENCES workspace_archive_imports(archive_digest) ON DELETE CASCADE,
    project_id TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
    source_budget_id TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL CHECK(length(snapshot_digest) = 64),
    snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
    imported_at TEXT NOT NULL
);
"""
