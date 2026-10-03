"""Upgrade result anchors to verified source digests without changing an applied migration."""

MIGRATION = """
ALTER TABLE result_turn_anchors RENAME TO unverified_result_turn_anchors;
CREATE TABLE result_turn_anchors (
    result_id TEXT NOT NULL REFERENCES result_receipts(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_seq INTEGER NOT NULL CHECK (turn_seq > 0),
    source_digest TEXT NOT NULL,
    PRIMARY KEY (result_id, session_id, turn_seq)
);
INSERT INTO result_turn_anchors(result_id,session_id,turn_seq,source_digest)
SELECT result_id,session_id,turn_seq,'' FROM unverified_result_turn_anchors;
DROP TABLE unverified_result_turn_anchors;
"""
