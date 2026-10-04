"""Durable clocks for the distinct waits of one execution attempt."""

MIGRATION = """
CREATE TABLE attempt_phase_clocks (
    attempt_id TEXT NOT NULL REFERENCES execution_attempts(id) ON DELETE CASCADE,
    phase TEXT NOT NULL CHECK(phase IN ('prepare', 'spawn', 'auth', 'ready', 'first_output', 'idle')),
    started_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    last_signal_at TEXT,
    last_progress_at TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('active', 'completed', 'timed_out')),
    PRIMARY KEY(attempt_id, phase)
);
CREATE UNIQUE INDEX attempt_phase_one_active ON attempt_phase_clocks(attempt_id)
    WHERE outcome = 'active';
CREATE INDEX attempt_phase_due ON attempt_phase_clocks(deadline_at)
    WHERE outcome = 'active';
"""
