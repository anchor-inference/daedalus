"""Inert schedule suggestions and their immutable source identity."""

MIGRATION = """
CREATE TABLE schedule_proposals (
    id TEXT PRIMARY KEY,
    source_session_id TEXT,
    source_run_id TEXT,
    source_command_id TEXT,
    source_project_id TEXT,
    source_actor_id TEXT NOT NULL,
    legacy_schedule_id TEXT UNIQUE REFERENCES schedules(id),
    proposal_revision INTEGER NOT NULL DEFAULT 1 CHECK (proposal_revision > 0),
    request_json TEXT NOT NULL,
    source_request_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','withdrawn','accepted')),
    accepted_schedule_id TEXT REFERENCES schedules(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_actor_id,source_command_id)
);
CREATE INDEX schedule_proposals_pending ON schedule_proposals(status,created_at);
CREATE TABLE schedule_proposal_receipts (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES schedule_proposals(id),
    actor_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('create','withdraw')),
    client_operation_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(actor_id,operation_kind,client_operation_id)
);
-- The old agent path wrote an enabled but unapproved schedule. Its original row
-- remains as provenance; only a row without a cycle or effect can be proposed.
INSERT INTO schedule_proposals(
    id,source_session_id,source_project_id,source_actor_id,legacy_schedule_id,
    request_json,source_request_digest,request_digest,created_at,updated_at
)
SELECT 'legacy-' || s.id,s.created_by_session,s.project_id,
       'legacy:' || coalesce(s.created_by_session,'unknown'),s.id,
       json_object('name',s.name,'prompt',s.prompt,'cron',s.cron,'run_at',s.run_at,
                   'files',s.files,'model',s.model,'kind',s.kind,
                   'run_in',s.run_in,'target_session',s.target_session,
                   'next_run_at',s.next_run_at,'workspace',s.workspace),
       'legacy:' || s.id,'legacy:' || s.id,s.created_at,coalesce(s.updated_at,s.created_at)
FROM schedules s
WHERE s.authority_state = 'needs_approval' AND s.grant_id IS NULL AND s.deleted_at IS NULL
  AND s.created_by_session IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM recurring_cycles c WHERE c.schedule_id = s.id);
UPDATE schedules SET enabled = 0 WHERE id IN
    (SELECT legacy_schedule_id FROM schedule_proposals WHERE legacy_schedule_id IS NOT NULL);
"""
