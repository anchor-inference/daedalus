"""Pause path-backed approvals until an operator reviews kept attachment bytes."""

MIGRATION = """
CREATE TEMP TABLE schedule_attachment_repin AS
SELECT s.id,s.grant_id,s.project_id,s.created_by_session,s.created_at
FROM schedules s
WHERE s.deleted_at IS NULL AND s.authority_state='current' AND s.grant_id IS NOT NULL
  AND json_valid(s.files) AND json_array_length(s.files)>0
  AND EXISTS (SELECT 1 FROM json_each(s.files) f WHERE f.value NOT LIKE 'att:%');

INSERT INTO grant_events(grant_id,actor_id,generation,event,reason,at)
SELECT g.id,'host:schedule-attachment-review',g.generation+1,'revoked',
       'attachment bytes require renewed review',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM actor_grants g JOIN schedule_attachment_repin p ON p.grant_id=g.id
WHERE g.revoked_at IS NULL;
UPDATE actor_grants SET revoked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),generation=generation+1
WHERE id IN (SELECT grant_id FROM schedule_attachment_repin) AND revoked_at IS NULL;
UPDATE effect_outbox SET state='cancelled',error='attachment bytes require renewed review'
WHERE state='pending' AND grant_id IN (SELECT grant_id FROM schedule_attachment_repin);
UPDATE recurring_cycles SET action_state='needs_approval',
                            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE action_state='pending' AND effect_id IN
    (SELECT id FROM effect_outbox WHERE state='cancelled' AND error='attachment bytes require renewed review');

UPDATE schedules SET enabled=0,authority_state='needs_approval',grant_id=NULL,grant_generation=NULL,
                     schedule_revision=schedule_revision+1,
                     updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE id IN (SELECT id FROM schedule_attachment_repin);

INSERT INTO schedule_proposals(id,source_session_id,source_project_id,source_actor_id,legacy_schedule_id,
                               request_json,source_request_digest,request_digest,created_at,updated_at)
SELECT 'repin-' || s.id,s.created_by_session,s.project_id,'repin:' || s.id,s.id,
       json_object('name',s.name,'prompt',s.prompt,'cron',s.cron,'run_at',s.run_at,
                   'files',s.files,'model',s.model,'kind',s.kind,'run_in',s.run_in,
                   'target_session',s.target_session,'next_run_at',s.next_run_at,'workspace',s.workspace),
       'repin:' || s.id,'repin:' || s.id,
       strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM schedules s JOIN schedule_attachment_repin p ON p.id=s.id;
DROP TABLE schedule_attachment_repin;
"""
