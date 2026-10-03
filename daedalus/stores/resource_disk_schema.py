"""Keep an optional free-space warning separate from kernel resource ceilings."""

MIGRATION = """
ALTER TABLE resource_profile_versions ADD COLUMN min_free_disk_bytes INTEGER NOT NULL DEFAULT 0
    CHECK(min_free_disk_bytes >= 0);

CREATE TABLE attempt_disk_preflights (
    attempt_id TEXT PRIMARY KEY REFERENCES execution_attempts(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    profile_revision INTEGER NOT NULL,
    folder_id TEXT NOT NULL REFERENCES project_folders(id),
    min_free_disk_bytes INTEGER NOT NULL CHECK(min_free_disk_bytes > 0),
    admission_json TEXT NOT NULL,
    preparation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id,profile_revision)
        REFERENCES resource_profile_versions(project_id,revision)
);
CREATE TRIGGER attempt_disk_preflights_immutable_update BEFORE UPDATE ON attempt_disk_preflights
BEGIN SELECT RAISE(ABORT,'attempt disk preflights are immutable'); END;
CREATE TRIGGER attempt_disk_preflights_immutable_delete BEFORE DELETE ON attempt_disk_preflights
BEGIN SELECT RAISE(ABORT,'attempt disk preflights are immutable'); END;

CREATE TABLE attempt_disk_entry_observations (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempt_disk_preflights(attempt_id),
    observation_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX attempt_disk_entry_attempt ON attempt_disk_entry_observations(attempt_id,observed_at);
CREATE TRIGGER attempt_disk_entry_immutable_update BEFORE UPDATE ON attempt_disk_entry_observations
BEGIN SELECT RAISE(ABORT,'attempt disk entry observations are immutable'); END;
CREATE TRIGGER attempt_disk_entry_immutable_delete BEFORE DELETE ON attempt_disk_entry_observations
BEGIN SELECT RAISE(ABORT,'attempt disk entry observations are immutable'); END;
"""
