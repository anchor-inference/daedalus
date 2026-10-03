"""Pin alternative attempts, reservations and a winner to one immutable task contract."""

MIGRATION = """
ALTER TABLE comparison_groups ADD COLUMN state TEXT NOT NULL DEFAULT 'legacy_unknown'
    CHECK (state IN ('legacy_unknown','planned','active','ready','chosen','blocked'));
ALTER TABLE comparison_groups ADD COLUMN budget_cap_microusd INTEGER NOT NULL DEFAULT 0
    CHECK (budget_cap_microusd >= 0);
ALTER TABLE comparison_groups ADD COLUMN reserved_microusd INTEGER NOT NULL DEFAULT 0
    CHECK (reserved_microusd >= 0);
ALTER TABLE comparison_groups ADD COLUMN created_by_actor_id TEXT NOT NULL DEFAULT '';
ALTER TABLE comparison_groups ADD COLUMN selected_verdict_id TEXT REFERENCES review_verdicts(id);
ALTER TABLE comparison_groups ADD COLUMN selection_receipt_id TEXT;
ALTER TABLE comparison_groups ADD COLUMN selected_at TEXT;

ALTER TABLE comparison_group_attempts ADD COLUMN slot INTEGER CHECK (slot BETWEEN 1 AND 2);
ALTER TABLE comparison_group_attempts ADD COLUMN budget_reservation_id TEXT;
ALTER TABLE comparison_group_attempts ADD COLUMN reserved_microusd INTEGER NOT NULL DEFAULT 0
    CHECK (reserved_microusd >= 0);
ALTER TABLE comparison_group_attempts ADD COLUMN rate_version TEXT;
ALTER TABLE comparison_group_attempts ADD COLUMN worktree_identity TEXT;
ALTER TABLE comparison_group_attempts ADD COLUMN admitted_at TEXT;
CREATE UNIQUE INDEX comparison_attempt_one_group ON comparison_group_attempts(attempt_id);
CREATE UNIQUE INDEX comparison_group_one_slot ON comparison_group_attempts(group_id,slot)
    WHERE slot IS NOT NULL;
CREATE INDEX comparison_worktree_identity ON comparison_group_attempts(worktree_identity)
    WHERE worktree_identity IS NOT NULL;

CREATE TABLE comparison_selection_receipts (
    id TEXT PRIMARY KEY,
    operation_receipt_id TEXT NOT NULL REFERENCES operation_receipts(id) DEFERRABLE INITIALLY DEFERRED,
    group_id TEXT NOT NULL REFERENCES comparison_groups(id),
    task_id TEXT NOT NULL REFERENCES board_tasks(id),
    contract_revision INTEGER NOT NULL,
    result_id TEXT NOT NULL REFERENCES result_receipts(id),
    verdict_id TEXT NOT NULL REFERENCES review_verdicts(id),
    attempt_id TEXT NOT NULL REFERENCES execution_attempts(id),
    head_sha TEXT,
    base_sha TEXT,
    comparison_digest TEXT NOT NULL CHECK (length(comparison_digest) = 64),
    observed_cost_microusd INTEGER NOT NULL CHECK (observed_cost_microusd >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(group_id)
);
CREATE TRIGGER comparison_selection_receipts_no_update
    BEFORE UPDATE ON comparison_selection_receipts
BEGIN SELECT RAISE(ABORT,'comparison selections are immutable'); END;
CREATE TRIGGER comparison_selection_receipts_no_delete
    BEFORE DELETE ON comparison_selection_receipts
BEGIN SELECT RAISE(ABORT,'comparison selections are immutable'); END;
"""


__all__ = ["MIGRATION"]
