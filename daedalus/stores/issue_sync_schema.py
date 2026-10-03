"""Immutable issue observations and explicit outbound ownership for project issue links."""

ISSUE_SYNC_MIGRATION = """
CREATE TABLE issue_sync_observations (
    id TEXT PRIMARY KEY,
    link_id TEXT REFERENCES issue_links(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider = 'github'),
    remote_id TEXT NOT NULL,
    remote_version TEXT NOT NULL,
    remote_digest TEXT NOT NULL,
    remote_json TEXT NOT NULL,
    local_revision INTEGER,
    origin_token TEXT,
    predecessor_id TEXT REFERENCES issue_sync_observations(id),
    observed_at TEXT NOT NULL,
    UNIQUE (project_id, provider, remote_id, remote_version, remote_digest)
);
CREATE INDEX issue_sync_observations_link ON issue_sync_observations(link_id, observed_at);
CREATE TABLE issue_sync_outbound (
    effect_id TEXT PRIMARY KEY REFERENCES effect_outbox(id) ON DELETE CASCADE,
    link_id TEXT NOT NULL REFERENCES issue_links(id) ON DELETE CASCADE,
    before_observation_id TEXT NOT NULL REFERENCES issue_sync_observations(id),
    origin_token TEXT NOT NULL UNIQUE,
    desired_digest TEXT NOT NULL,
    desired_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'preflight_conflict', 'unknown', 'verified')),
    observed_version TEXT,
    observed_digest TEXT,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX issue_sync_one_pending_push ON issue_sync_outbound(link_id)
    WHERE state IN ('queued', 'unknown');
CREATE TRIGGER issue_sync_observations_no_update BEFORE UPDATE ON issue_sync_observations
BEGIN SELECT RAISE(ABORT,'issue observations are immutable'); END;
CREATE TRIGGER issue_sync_observations_no_delete BEFORE DELETE ON issue_sync_observations
BEGIN SELECT RAISE(ABORT,'issue observations are immutable'); END;
"""
