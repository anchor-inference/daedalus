"""Tie worker report authority to the approval that launched its exact session."""

MIGRATION = """
ALTER TABLE actor_grants ADD COLUMN parent_grant_id TEXT REFERENCES actor_grants(id) ON DELETE RESTRICT;
ALTER TABLE actor_grants ADD COLUMN parent_grant_generation INTEGER
    CHECK ((parent_grant_id IS NULL AND parent_grant_generation IS NULL)
        OR (parent_grant_id IS NOT NULL AND parent_grant_generation IS NOT NULL AND parent_grant_generation > 0));
ALTER TABLE actor_grants ADD COLUMN staff_session_id TEXT REFERENCES staff_sessions(id) ON DELETE RESTRICT;
CREATE INDEX actor_grants_by_parent ON actor_grants(parent_grant_id);
CREATE TRIGGER actor_grants_lineage_immutable BEFORE UPDATE OF
    parent_grant_id,parent_grant_generation,staff_session_id ON actor_grants
BEGIN SELECT RAISE(ABORT,'grant lineage is immutable'); END;
"""
