"""Versioned project ceilings and exact attempt containment evidence."""

MIGRATION = """
CREATE TABLE resource_profile_versions (
    project_id TEXT NOT NULL REFERENCES projects(id),
    revision INTEGER NOT NULL CHECK(revision > 0),
    state TEXT NOT NULL CHECK(state IN ('enabled','disabled')),
    memory_bytes INTEGER NOT NULL CHECK(memory_bytes >= 0),
    cpu_millis INTEGER NOT NULL CHECK(cpu_millis >= 0),
    process_count INTEGER NOT NULL CHECK(process_count >= 0),
    disk_bytes INTEGER NOT NULL CHECK(disk_bytes >= 0),
    receipt_id TEXT NOT NULL REFERENCES operation_receipts(id) DEFERRABLE INITIALLY DEFERRED,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,revision)
);

CREATE TABLE attempt_resource_bindings (
    attempt_id TEXT PRIMARY KEY REFERENCES execution_attempts(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    profile_revision INTEGER NOT NULL,
    host_generation TEXT NOT NULL,
    env TEXT NOT NULL CHECK(env IN ('container','host')),
    daemon_instance TEXT NOT NULL,
    launch_id TEXT,
    limits_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('reserved','enforced','released','unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id,profile_revision)
        REFERENCES resource_profile_versions(project_id,revision),
    UNIQUE(host_generation,daemon_instance,launch_id)
);

CREATE TABLE attempt_resource_observations (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempt_resource_bindings(attempt_id),
    host_generation TEXT NOT NULL,
    daemon_instance TEXT NOT NULL,
    launch_id TEXT NOT NULL,
    terminal_id TEXT,
    observation_kind TEXT NOT NULL CHECK(observation_kind IN ('spawn','sample','stop','exit','unknown')),
    enforced INTEGER NOT NULL CHECK(enforced IN (0,1)),
    populated INTEGER CHECK(populated IN (0,1)),
    memory_peak_bytes INTEGER,
    cpu_usage_usec INTEGER,
    processes INTEGER,
    oom_kills INTEGER,
    pids_max_events INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL
);
CREATE INDEX attempt_resource_observations_attempt ON attempt_resource_observations(attempt_id,observed_at);

CREATE TRIGGER resource_profiles_immutable_update BEFORE UPDATE ON resource_profile_versions
BEGIN SELECT RAISE(ABORT,'resource profile versions are immutable'); END;
CREATE TRIGGER resource_profiles_immutable_delete BEFORE DELETE ON resource_profile_versions
BEGIN SELECT RAISE(ABORT,'resource profile versions are immutable'); END;
CREATE TRIGGER resource_observations_immutable_update BEFORE UPDATE ON attempt_resource_observations
BEGIN SELECT RAISE(ABORT,'attempt resource observations are immutable'); END;
CREATE TRIGGER resource_observations_immutable_delete BEFORE DELETE ON attempt_resource_observations
BEGIN SELECT RAISE(ABORT,'attempt resource observations are immutable'); END;
"""
