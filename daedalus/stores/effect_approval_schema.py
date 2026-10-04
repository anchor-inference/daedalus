"""Bind an operator's approval to one committed outbox payload and artifact revision."""

MIGRATION = """
CREATE TABLE effect_approvals (
    id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL REFERENCES effect_outbox(id) ON DELETE CASCADE,
    operation_digest TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    target_digest TEXT NOT NULL,
    artifact_revision TEXT NOT NULL,
    scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
    expires_at TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    revoked_at TEXT,
    receipt_id TEXT NOT NULL REFERENCES operation_receipts(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
    created_at TEXT NOT NULL,
    UNIQUE(operation_digest, actor_id, target_digest, artifact_revision)
);
CREATE INDEX effect_approvals_by_effect ON effect_approvals(effect_id, revoked_at);
"""
