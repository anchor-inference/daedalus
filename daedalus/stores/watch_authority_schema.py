"""Keep the approval for each exact version of a standing watch."""

MIGRATION = """
CREATE TABLE watch_authorities (
    watch_id TEXT NOT NULL,
    condition_revision INTEGER NOT NULL CHECK (condition_revision > 0),
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    origin_class TEXT NOT NULL CHECK (origin_class IN ('operator', 'agent')),
    grant_id TEXT REFERENCES actor_grants(id),
    grant_generation INTEGER,
    action_kind TEXT NOT NULL CHECK (action_kind IN ('watch.wake', 'watch.tell', 'watch.notify')),
    action_digest TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY (watch_id, condition_revision),
    CHECK ((origin_class = 'operator' AND grant_id IS NULL AND grant_generation IS NULL)
        OR (origin_class = 'agent' AND grant_id IS NOT NULL AND grant_generation > 0))
);
CREATE INDEX watch_authorities_project ON watch_authorities(project_id,watch_id,condition_revision);
-- A role label in old rows is not evidence of a standing approval. Keep the rule for review,
-- but do not let it reserve an action until an authenticated actor approves its next version.
UPDATE watches SET enabled = 0, condition_revision = condition_revision + 1,
    state_json = json_set(CASE WHEN json_valid(state_json) THEN state_json ELSE '{}' END,
                          '$.stopped', 'needs_approval')
WHERE enabled = 1;
UPDATE watch_deliveries SET status = 'cancelled',updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE status = 'pending';
"""
