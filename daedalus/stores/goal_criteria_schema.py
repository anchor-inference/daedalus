"""Keep each goal's checkable criteria with its immutable revision."""

MIGRATION = """
ALTER TABLE project_goal_revisions ADD COLUMN checks_json TEXT
    CHECK (checks_json IS NULL OR json_valid(checks_json));
"""
