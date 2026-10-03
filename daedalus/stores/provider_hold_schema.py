"""Immutable provider refusal evidence and explicit resume decisions."""

MIGRATION = """
CREATE TABLE provider_failure_observations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    inference_reservation_id TEXT REFERENCES inference_reservations(id),
    execution_attempt_id TEXT REFERENCES execution_attempts(id),
    project_id TEXT REFERENCES projects(id),
    provider_id TEXT NOT NULL,
    provider_kind TEXT NOT NULL,
    provider_source_digest TEXT CHECK(provider_source_digest IS NULL OR length(provider_source_digest) = 64),
    model TEXT NOT NULL,
    status INTEGER NOT NULL CHECK(status BETWEEN 400 AND 599),
    failure_class TEXT NOT NULL CHECK(failure_class IN
        ('auth','billing','quota','rate','capacity','limited_unknown','capacity_unknown','other')),
    provider_code TEXT,
    reset_at TEXT,
    reset_source TEXT,
    retry_after_at TEXT,
    evidence_digest TEXT NOT NULL CHECK(length(evidence_digest) = 64),
    observed_at TEXT NOT NULL,
    CHECK((reset_at IS NULL AND reset_source IS NULL) OR
          (reset_at IS NOT NULL AND reset_source IS NOT NULL))
);
CREATE INDEX provider_failure_by_run ON provider_failure_observations(run_id,observed_at);
CREATE INDEX provider_failure_by_provider ON provider_failure_observations(provider_id,observed_at);
CREATE TRIGGER provider_failure_no_update BEFORE UPDATE ON provider_failure_observations
BEGIN SELECT RAISE(ABORT,'provider failure observations are immutable'); END;
CREATE TRIGGER provider_failure_no_delete BEFORE DELETE ON provider_failure_observations
BEGIN SELECT RAISE(ABORT,'provider failure observations are retained'); END;

CREATE TABLE provider_resume_holds (
    id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL UNIQUE REFERENCES provider_failure_observations(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    failed_run_id TEXT NOT NULL REFERENCES runs(id),
    project_id TEXT REFERENCES projects(id),
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('global','project')),
    scope_id TEXT NOT NULL,
    created_receipt_id TEXT NOT NULL REFERENCES operation_receipts(id) DEFERRABLE INITIALLY DEFERRED,
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN
        ('held','resume_queued','resumed','invalidated','unknown')),
    resume_action_id TEXT UNIQUE REFERENCES effect_outbox(id) DEFERRABLE INITIALLY DEFERRED,
    resumed_run_id TEXT REFERENCES runs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((scope_kind = 'global' AND scope_id = 'global' AND project_id IS NULL)
          OR (scope_kind = 'project' AND scope_id = project_id AND project_id IS NOT NULL))
);
CREATE INDEX provider_holds_by_session ON provider_resume_holds(session_id,state,created_at);
CREATE TRIGGER provider_hold_origin_immutable
BEFORE UPDATE OF observation_id,session_id,failed_run_id,project_id,provider_id,model,
                 scope_kind,scope_id,created_receipt_id,created_at ON provider_resume_holds
BEGIN SELECT RAISE(ABORT,'provider hold origin is immutable'); END;
CREATE TRIGGER provider_hold_no_delete BEFORE DELETE ON provider_resume_holds
BEGIN SELECT RAISE(ABORT,'provider hold decisions are retained'); END;

CREATE TABLE provider_hold_events (
    id TEXT PRIMARY KEY,
    hold_id TEXT NOT NULL REFERENCES provider_resume_holds(id),
    event TEXT NOT NULL CHECK(event IN ('created','resume_queued','run_pinned','resumed','invalidated','unknown')),
    receipt_id TEXT REFERENCES operation_receipts(id) DEFERRABLE INITIALLY DEFERRED,
    action_id TEXT REFERENCES effect_outbox(id) DEFERRABLE INITIALLY DEFERRED,
    run_id TEXT REFERENCES runs(id),
    reason TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL
);
CREATE INDEX provider_hold_events_by_hold ON provider_hold_events(hold_id,occurred_at);
CREATE UNIQUE INDEX provider_hold_run_pinned ON provider_hold_events(hold_id,event)
WHERE event = 'run_pinned';
CREATE TRIGGER provider_hold_event_no_update BEFORE UPDATE ON provider_hold_events
BEGIN SELECT RAISE(ABORT,'provider hold events are immutable'); END;
CREATE TRIGGER provider_hold_event_no_delete BEFORE DELETE ON provider_hold_events
BEGIN SELECT RAISE(ABORT,'provider hold events are retained'); END;
"""
