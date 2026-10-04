"""Durable attempt history for committed effects."""

MIGRATION = """
ALTER TABLE effect_outbox ADD COLUMN retry_at TEXT;
ALTER TABLE effect_outbox ADD COLUMN retry_blocked INTEGER NOT NULL DEFAULT 0 CHECK(retry_blocked IN (0, 1));
CREATE TABLE retry_attempts (
    op_id TEXT NOT NULL REFERENCES operation_receipts(id) ON DELETE CASCADE,
    effect_id TEXT NOT NULL REFERENCES effect_outbox(id) ON DELETE CASCADE,
    phase TEXT NOT NULL CHECK(phase IN ('delivery', 'status_read')),
    ordinal INTEGER NOT NULL CHECK(ordinal > 0),
    error_class TEXT,
    next_at TEXT,
    budget_left INTEGER NOT NULL CHECK(budget_left >= 0),
    state TEXT NOT NULL CHECK(state IN ('claimed', 'scheduled', 'completed', 'failed', 'unknown')),
    created_at TEXT NOT NULL,
    PRIMARY KEY(effect_id, phase, ordinal)
);
CREATE INDEX retry_attempts_by_operation ON retry_attempts(op_id, phase, ordinal);
CREATE INDEX effect_outbox_retry_due ON effect_outbox(state, retry_at, created_at);
"""
