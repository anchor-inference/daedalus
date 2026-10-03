"""Durable authority and occurrence history for scheduled effects."""

MIGRATION = """
ALTER TABLE schedules ADD COLUMN schedule_revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE schedules ADD COLUMN project_id TEXT REFERENCES projects(id);
ALTER TABLE schedules ADD COLUMN actor_id TEXT;
ALTER TABLE schedules ADD COLUMN grant_id TEXT REFERENCES actor_grants(id);
ALTER TABLE schedules ADD COLUMN grant_generation INTEGER;
ALTER TABLE schedules ADD COLUMN authority_state TEXT NOT NULL DEFAULT 'needs_approval'
    CHECK (authority_state IN ('current','needs_approval'));
ALTER TABLE schedules ADD COLUMN output_contract_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE schedules ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC';
ALTER TABLE schedules ADD COLUMN updated_at TEXT;
ALTER TABLE schedules ADD COLUMN deleted_at TEXT;
ALTER TABLE lazy_notes ADD COLUMN cycle_id TEXT;
CREATE UNIQUE INDEX lazy_notes_cycle ON lazy_notes(cycle_id) WHERE cycle_id IS NOT NULL;
CREATE TABLE recurring_cycles (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    schedule_revision INTEGER NOT NULL CHECK (schedule_revision > 0),
    project_id TEXT REFERENCES projects(id),
    due_at TEXT NOT NULL,
    local_wall TEXT NOT NULL,
    utc_offset_seconds INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('agent','message','lazy','wake')),
    target_session TEXT,
    manual INTEGER NOT NULL DEFAULT 0 CHECK (manual IN (0,1)),
    intent_digest TEXT NOT NULL,
    output_contract_json TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    effect_id TEXT REFERENCES effect_outbox(id),
    action_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (action_state IN ('pending','dispatching','delivered','failed','unknown','needs_approval','skipped')),
    reconciled_by TEXT,
    reconciliation_reason TEXT,
    reconciled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX recurring_due_identity ON recurring_cycles(schedule_id,schedule_revision,due_at,utc_offset_seconds)
    WHERE manual = 0;
CREATE INDEX recurring_cycles_schedule ON recurring_cycles(schedule_id,created_at);
-- Existing rows have no authenticated standing grant. Preserve their exact cadence and content,
-- but require review before a future external effect can be admitted.
UPDATE schedules SET project_id = (
    SELECT project_id FROM sessions WHERE id = coalesce(schedules.target_session,schedules.created_by_session)
) WHERE coalesce(target_session,created_by_session) IS NOT NULL;
"""
