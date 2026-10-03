"""Persist physical runtime observations separately from submitted result outcomes."""

MIGRATION = """
ALTER TABLE execution_attempts ADD COLUMN native_run_id TEXT;
ALTER TABLE execution_attempts ADD COLUMN runtime_instance TEXT;
-- Exit evidence outlives terminal history pruning.
CREATE TABLE terminal_exit_observations (
    terminal_id TEXT NOT NULL,
    runtime_instance TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(terminal_id,runtime_instance)
);
CREATE TRIGGER terminal_exit_observations_no_update BEFORE UPDATE ON terminal_exit_observations
BEGIN SELECT RAISE(ABORT,'terminal observations are immutable'); END;
CREATE TRIGGER terminal_exit_observations_no_delete BEFORE DELETE ON terminal_exit_observations
BEGIN SELECT RAISE(ABORT,'terminal observations are immutable'); END;
CREATE TABLE runtime_exit_observations (
    attempt_id TEXT NOT NULL REFERENCES execution_attempts(id),
    runtime_ref TEXT NOT NULL,
    provider_session_ref TEXT NOT NULL,
    staff_session_id TEXT NOT NULL REFERENCES staff_sessions(id),
    contract_revision INTEGER NOT NULL,
    host_generation INTEGER NOT NULL,
    runtime_kind TEXT NOT NULL CHECK(runtime_kind IN ('daedalus','cli')),
    runtime_instance TEXT,
    observed_status TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(attempt_id,runtime_ref)
);
CREATE TRIGGER runtime_exit_observations_no_update BEFORE UPDATE ON runtime_exit_observations
BEGIN SELECT RAISE(ABORT,'runtime observations are immutable'); END;
CREATE TRIGGER runtime_exit_observations_no_delete BEFORE DELETE ON runtime_exit_observations
BEGIN SELECT RAISE(ABORT,'runtime observations are immutable'); END;
"""
