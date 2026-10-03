"""Distinguish a host-refused launch from work whose provider response disappeared."""

MIGRATION = """
ALTER TABLE execution_attempts ADD COLUMN runtime_entered_at TEXT;
CREATE TRIGGER execution_runtime_entry_no_replace
BEFORE UPDATE OF runtime_entered_at ON execution_attempts
WHEN OLD.runtime_entered_at IS NOT NULL AND NEW.runtime_entered_at IS NOT OLD.runtime_entered_at
BEGIN SELECT RAISE(ABORT,'runtime entry cannot be replaced'); END;
CREATE TABLE runtime_no_entry_observations (
    attempt_id TEXT PRIMARY KEY REFERENCES execution_attempts(id),
    staff_session_id TEXT NOT NULL REFERENCES staff_sessions(id),
    contract_revision INTEGER NOT NULL,
    host_generation INTEGER NOT NULL,
    runtime_kind TEXT NOT NULL CHECK(runtime_kind IN ('daedalus','cli')),
    reason TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE TRIGGER runtime_no_entry_no_update BEFORE UPDATE ON runtime_no_entry_observations
BEGIN SELECT RAISE(ABORT,'runtime observations are immutable'); END;
CREATE TRIGGER runtime_no_entry_no_delete BEFORE DELETE ON runtime_no_entry_observations
BEGIN SELECT RAISE(ABORT,'runtime observations are immutable'); END;
CREATE TRIGGER execution_runtime_entry_no_refused
BEFORE UPDATE OF runtime_entered_at ON execution_attempts
WHEN NEW.runtime_entered_at IS NOT NULL AND EXISTS (
    SELECT 1 FROM runtime_no_entry_observations WHERE attempt_id = OLD.id
)
BEGIN SELECT RAISE(ABORT,'a refused execution cannot enter its runtime'); END;
"""
