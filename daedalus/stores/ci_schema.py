"""Retain signed check observations and contract-bound required checks."""

MIGRATION = """
ALTER TABLE webhook_deliveries ADD COLUMN payload_digest TEXT;
CREATE TABLE ci_observations (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    head_sha TEXT NOT NULL CHECK(length(head_sha) IN (40,64)),
    run_id TEXT NOT NULL,
    run_attempt INTEGER NOT NULL DEFAULT 1 CHECK(run_attempt > 0),
    check_name TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','final')),
    conclusion TEXT CHECK(conclusion IN ('passed','failed','cancelled','unknown')),
    payload_digest TEXT NOT NULL CHECK(length(payload_digest) = 64),
    event_seq INTEGER,
    observed_at TEXT NOT NULL,
    UNIQUE(provider,delivery_id)
);
CREATE INDEX ci_observations_head ON ci_observations(provider,repository_id,head_sha,check_name,observed_at);
CREATE TABLE ci_required_checks (
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    contract_revision INTEGER NOT NULL,
    provider TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    check_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(task_id,contract_revision,provider,repository_id,check_name),
    FOREIGN KEY(task_id,contract_revision)
        REFERENCES task_contract_versions(task_id,contract_revision)
);
CREATE TRIGGER ci_observations_no_update BEFORE UPDATE ON ci_observations
BEGIN SELECT RAISE(ABORT,'CI observations are immutable'); END;
CREATE TRIGGER ci_observations_no_delete BEFORE DELETE ON ci_observations
BEGIN SELECT RAISE(ABORT,'CI observations are immutable'); END;
CREATE TRIGGER ci_required_checks_no_update BEFORE UPDATE ON ci_required_checks
BEGIN SELECT RAISE(ABORT,'CI requirements are versioned'); END;
CREATE TRIGGER ci_required_checks_no_delete BEFORE DELETE ON ci_required_checks
BEGIN SELECT RAISE(ABORT,'CI requirements are versioned'); END;
"""

__all__ = ["MIGRATION"]
