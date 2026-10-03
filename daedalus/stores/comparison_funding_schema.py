"""Reserve both alternative budgets and capacity before either runtime can start."""

MIGRATION = """
CREATE TABLE comparison_funding_slots (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES comparison_groups(id),
    slot INTEGER NOT NULL CHECK(slot IN (1,2)),
    staff_id TEXT NOT NULL REFERENCES staff(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    task_id TEXT NOT NULL REFERENCES board_tasks(id),
    contract_revision INTEGER NOT NULL,
    host_generation INTEGER NOT NULL,
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    allowance_microusd INTEGER NOT NULL CHECK(allowance_microusd > 0),
    rate_version TEXT NOT NULL CHECK(length(rate_version) = 64),
    quote_json TEXT NOT NULL CHECK(json_valid(quote_json)),
    attempt_id TEXT UNIQUE REFERENCES execution_attempts(id),
    state TEXT NOT NULL CHECK(state IN ('held','released')),
    created_at TEXT NOT NULL,
    launch_started_at TEXT,
    released_at TEXT,
    UNIQUE(group_id,slot),
    UNIQUE(group_id,staff_id)
);
CREATE TABLE comparison_funding_scopes (
    slot_id TEXT NOT NULL REFERENCES comparison_funding_slots(id),
    scope_key TEXT NOT NULL,
    cap_microusd INTEGER NOT NULL CHECK(cap_microusd >= 0),
    PRIMARY KEY(slot_id,scope_key)
);
CREATE INDEX comparison_capacity_held ON comparison_funding_slots(project_id,state);
ALTER TABLE inference_reservations ADD COLUMN execution_attempt_id TEXT REFERENCES execution_attempts(id);
ALTER TABLE inference_reservations ADD COLUMN comparison_slot_id TEXT REFERENCES comparison_funding_slots(id);
CREATE INDEX inference_attempt_cost ON inference_reservations(execution_attempt_id,state);
CREATE INDEX inference_funding_slot ON inference_reservations(comparison_slot_id,state);
CREATE TRIGGER inference_comparison_owner_on_insert BEFORE INSERT ON inference_reservations
WHEN NEW.comparison_slot_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM comparison_funding_slots s WHERE s.id = NEW.comparison_slot_id
    AND s.attempt_id = NEW.execution_attempt_id AND s.provider_id = NEW.provider_id
    AND s.model = NEW.model AND s.state = 'held'
)
BEGIN SELECT RAISE(ABORT,'inference must own its exact comparison allocation'); END;
CREATE TRIGGER comparison_funding_identity_immutable
BEFORE UPDATE OF id,group_id,slot,staff_id,project_id,task_id,contract_revision,host_generation,
    provider_id,model,allowance_microusd,rate_version,quote_json,created_at
ON comparison_funding_slots
BEGIN SELECT RAISE(ABORT,'comparison funding identity is immutable'); END;
CREATE TRIGGER comparison_funding_attempt_once
BEFORE UPDATE OF attempt_id ON comparison_funding_slots
WHEN OLD.attempt_id IS NOT NULL OR NEW.attempt_id IS NULL
BEGIN SELECT RAISE(ABORT,'comparison funding binds one execution'); END;
CREATE TRIGGER comparison_funding_release_once
BEFORE UPDATE OF state ON comparison_funding_slots
WHEN OLD.state = 'released' OR NEW.state != 'released'
BEGIN SELECT RAISE(ABORT,'released comparison funding cannot be reopened'); END;
CREATE TRIGGER comparison_funding_launch_once
BEFORE UPDATE OF launch_started_at ON comparison_funding_slots
WHEN OLD.launch_started_at IS NOT NULL OR NEW.launch_started_at IS NULL
BEGIN SELECT RAISE(ABORT,'comparison launch boundary is immutable'); END;
CREATE TRIGGER comparison_funding_no_delete BEFORE DELETE ON comparison_funding_slots
BEGIN SELECT RAISE(ABORT,'comparison funding retains charge provenance'); END;
CREATE TRIGGER inference_execution_identity_immutable
BEFORE UPDATE OF execution_attempt_id,comparison_slot_id ON inference_reservations
BEGIN SELECT RAISE(ABORT,'inference execution ownership is immutable'); END;
"""
