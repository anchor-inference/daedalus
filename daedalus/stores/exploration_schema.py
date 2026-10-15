"""Speculative attempts at one board task, recorded before any of them is launched."""

MIGRATION = """
CREATE TABLE IF NOT EXISTS explorations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    orchestrator_session_id TEXT NOT NULL,
    source_staff_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('launching', 'running', 'ready', 'chosen', 'abandoned')),
    budget_microusd INTEGER NOT NULL CHECK (budget_microusd > 0),
    goal_budget_id TEXT,
    deadline_at TEXT NOT NULL,
    ready_reason TEXT NOT NULL DEFAULT '',
    winner_slot INTEGER,
    decision_reason TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS explorations_one_open ON explorations(task_id)
    WHERE state IN ('launching', 'running', 'ready');
CREATE INDEX IF NOT EXISTS explorations_by_project ON explorations(project_id, state);
CREATE TABLE IF NOT EXISTS exploration_attempts (
    exploration_id TEXT NOT NULL REFERENCES explorations(id) ON DELETE CASCADE,
    slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 3),
    harness TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    approach TEXT NOT NULL DEFAULT '',
    staff_id TEXT,
    task_id TEXT,
    fork_session_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('planned', 'launching', 'running', 'reported', 'failed', 'unknown',
                                         'cancelled', 'won', 'lost')),
    result_id TEXT,
    detail TEXT NOT NULL DEFAULT '',
    cleaned_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (exploration_id, slot)
);
CREATE UNIQUE INDEX IF NOT EXISTS exploration_attempt_card ON exploration_attempts(task_id)
    WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS exploration_attempt_staff ON exploration_attempts(staff_id)
    WHERE staff_id IS NOT NULL;
"""


__all__ = ["MIGRATION"]
