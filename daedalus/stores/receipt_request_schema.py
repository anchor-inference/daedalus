"""Retain the original compare-and-set input for host callbacks retried after a crash."""

MIGRATION = """
ALTER TABLE operation_receipts ADD COLUMN request_entity_revision INTEGER
    CHECK(request_entity_revision IS NULL OR request_entity_revision > 0);
"""
