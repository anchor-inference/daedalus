"""Durable whole-tree containment for writable CLI workers without a resource ceiling profile."""

MIGRATION = """
CREATE TABLE writer_attempt_bindings (
    attempt_id TEXT PRIMARY KEY REFERENCES execution_attempts(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    host_generation TEXT NOT NULL,
    env TEXT NOT NULL CHECK(env IN ('container','host')),
    daemon_instance TEXT NOT NULL,
    folder_id TEXT NOT NULL REFERENCES project_folders(id),
    folder_path_digest TEXT NOT NULL,
    launch_workspace_digest TEXT NOT NULL,
    launch_id TEXT,
    state TEXT NOT NULL CHECK(state IN ('reserved','enforced','released','unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(host_generation,daemon_instance,launch_id)
);

CREATE TABLE writer_attempt_observations (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES writer_attempt_bindings(attempt_id),
    host_generation TEXT NOT NULL,
    daemon_instance TEXT NOT NULL,
    launch_id TEXT NOT NULL,
    terminal_id TEXT,
    observation_kind TEXT NOT NULL CHECK(observation_kind IN ('spawn','sample','stop','exit','unknown')),
    enforced INTEGER NOT NULL CHECK(enforced IN (0,1)),
    populated INTEGER CHECK(populated IN (0,1)),
    reason TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL
);
CREATE INDEX writer_attempt_observations_attempt ON writer_attempt_observations(attempt_id,observed_at);
CREATE TRIGGER writer_attempt_observations_immutable_update BEFORE UPDATE ON writer_attempt_observations
BEGIN SELECT RAISE(ABORT,'writer attempt observations are immutable'); END;
CREATE TRIGGER writer_attempt_observations_immutable_delete BEFORE DELETE ON writer_attempt_observations
BEGIN SELECT RAISE(ABORT,'writer attempt observations are immutable'); END;
"""
