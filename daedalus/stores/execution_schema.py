"""Bind a worker attempt to its authenticated session and granted authority."""

MIGRATION = """
ALTER TABLE execution_attempts ADD COLUMN actor_id TEXT;
ALTER TABLE execution_attempts ADD COLUMN grant_id TEXT REFERENCES actor_grants(id) ON DELETE SET NULL;
ALTER TABLE execution_attempts ADD COLUMN grant_generation INTEGER;
ALTER TABLE execution_attempts ADD COLUMN staff_session_id TEXT REFERENCES staff_sessions(id) ON DELETE SET NULL;
ALTER TABLE execution_attempts ADD COLUMN runtime_kind TEXT CHECK(runtime_kind IN ('daedalus', 'cli'));
CREATE UNIQUE INDEX execution_attempt_staff_owner ON execution_attempts(staff_session_id)
    WHERE staff_session_id IS NOT NULL AND state IN ('queued', 'starting', 'running', 'waiting');
"""
