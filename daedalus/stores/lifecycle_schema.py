"""Pin cancellation ownership to an exact task contract or project goal revision."""

MIGRATION = """
ALTER TABLE lifecycle_parents ADD COLUMN goal_revision INTEGER;
ALTER TABLE lifecycle_parents ADD COLUMN contract_revision INTEGER;
ALTER TABLE lifecycle_owners ADD COLUMN source_revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE lifecycle_owners ADD COLUMN observed_identity TEXT NOT NULL DEFAULT '';
CREATE INDEX lifecycle_requested_children ON lifecycle_owners(cancel_state, parent_kind, parent_id);
"""


__all__ = ["MIGRATION"]
